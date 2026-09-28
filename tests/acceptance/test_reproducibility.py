"""AT-033: reproducibility and immutable response-cache isolation."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml  # type: ignore[import-untyped]
from proofops.adapters.cache.aws import (
    CacheCollisionError,
    CacheCorruptionError,
    CacheNamespace,
    CacheRevokedError,
    ImmutableResponseCache,
    InMemoryImmutableCacheClient,
    cache_request,
)
from proofops.application.rulepacks import validate_rulepack
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash, provenance_hash, request_signature
from proofops.domain.rulepacks import canonical_json, pack_content_hash, snapshot_from_validated
from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags, RuleContext, evaluate
from proofops.domain.values import SourceRef

TENANT_A = "00000000-0000-4000-8000-000000000001"
TENANT_B = "00000000-0000-4000-8000-000000000002"
DOCUMENT_A = "00000000-0000-4000-8000-000000000011"
DOCUMENT_B = "00000000-0000-4000-8000-000000000012"
REQUEST_ID = "00000000-0000-4000-8000-000000000021"
PACKET = "a" * 64
PROMPT = "b" * 64
SCHEMA = "c" * 64


def _namespace(
    *, tenant_id: str = TENANT_A, document_version_id: str = DOCUMENT_A
) -> CacheNamespace:
    return CacheNamespace(
        tenant_id=tenant_id,
        consent_profile="approved-us-east-1",
        document_version_id=document_version_id,
        role="tagger",
    )


def _request(*, namespace: CacheNamespace | None = None, **changes: object):
    values: dict[str, object] = {
        "namespace": namespace or _namespace(),
        "request_id": REQUEST_ID,
        "temperature": 0.0,
        "model_id": "approved-model-id",
        "model_profile": "tagging-v1",
        "prompt_sha256": PROMPT,
        "schema_sha256": SCHEMA,
        "packet_sha256": PACKET,
        "tools": ({"name": "citation_lookup", "version": "1"},),
        "max_tokens": 1024,
        "replicate_id": 1,
        "extraction_epoch": 2,
    }
    values.update(changes)
    return cache_request(**values)  # type: ignore[arg-type]


def test_request_signature_binds_every_approved_request_input() -> None:
    """Removing any listed request field would make distinct model calls collide."""
    values: dict[str, object] = {
        "temperature": 0.0,
        "model_id": "approved-model-id",
        "model_profile": "tagging-v1",
        "prompt_sha256": PROMPT,
        "schema_sha256": SCHEMA,
        "packet_sha256": PACKET,
        "tools": ({"name": "citation_lookup", "version": "1"},),
        "max_tokens": 1024,
        "replicate_id": 1,
        "extraction_epoch": 2,
    }
    baseline = request_signature(**values)  # type: ignore[arg-type]
    for changed in (
        {"temperature": 0.1},
        {"model_id": "other-model"},
        {"model_profile": "tagging-v2"},
        {"prompt_sha256": "d" * 64},
        {"schema_sha256": "d" * 64},
        {"packet_sha256": "d" * 64},
        {"tools": ({"name": "citation_lookup", "version": "2"},)},
        {"max_tokens": 512},
        {"replicate_id": 2},
        {"extraction_epoch": 3},
    ):
        assert baseline != request_signature(**(values | changed))  # type: ignore[arg-type]


def test_semantic_hash_preserves_nested_source_content_and_audit_is_external() -> None:
    """Recursive actor/timestamp deletion would make these distinct source records collide."""
    source_a = {"source": {"actor": "issuer-a", "timestamp": "2025-01-01"}}
    source_b = {"source": {"actor": "issuer-b", "timestamp": "2025-01-02"}}
    assert canonical_hash(source_a) != canonical_hash(source_b)

    tags, context, pack = _engine_inputs()
    first = evaluate(tags, context, pack)
    assert first.semantic_hash == evaluate(tags, context, pack).semantic_hash
    source_fact = next(fact for fact in tags.facts if fact.evidence_refs)
    changed_source = replace(source_fact.evidence_refs[0], raw_text_sha256="f" * 64)
    source_tags = replace(
        tags,
        facts=tuple(
            replace(fact, evidence_refs=(changed_source,))
            if fact.name == source_fact.name
            else fact
            for fact in tags.facts
        ),
    )
    assert first.semantic_hash != evaluate(source_tags, context, pack).semantic_hash
    assert (
        first.semantic_hash
        != evaluate(replace(tags, model_sha256="f" * 64), context, pack).semantic_hash
    )
    assert (
        first.semantic_hash
        != evaluate(replace(tags, prompt_sha256="f" * 64), context, pack).semantic_hash
    )
    assert first.semantic_hash != evaluate(tags, context, _changed_rule_pack(pack)).semantic_hash

    assert provenance_hash(
        semantic_hash=first.semantic_hash,
        actor_id="reviewer-a",
        occurred_at="2026-09-09T00:00:00Z",
    ) != provenance_hash(
        semantic_hash=first.semantic_hash,
        actor_id="reviewer-b",
        occurred_at="2026-09-09T00:01:00Z",
    )


def test_raw_cache_is_conditional_and_never_shared_across_tenant_or_replicate() -> None:
    """A key-only test would miss accidental reads of another raw response."""
    client = InMemoryImmutableCacheClient()
    cache = ImmutableResponseCache(client)
    first = _request()
    other_tenant = _request(namespace=_namespace(tenant_id=TENANT_B))
    other_replicate = _request(replicate_id=2)

    cache.put_raw(first, b"replica-1")
    cache.put_raw(other_tenant, b"tenant-b")
    assert cache.get_raw(first, recovery_request_id=REQUEST_ID) == b"replica-1"
    assert cache.get_raw(other_tenant, recovery_request_id=REQUEST_ID) == b"tenant-b"
    assert cache.get_raw(other_replicate, recovery_request_id=REQUEST_ID) is None
    with pytest.raises(DomainValidationError):
        cache.get_raw(first, recovery_request_id="00000000-0000-4000-8000-000000000022")
    with pytest.raises(CacheCollisionError):
        cache.put_raw(first, b"overwrite")
    assert cache.get_raw(first, recovery_request_id=REQUEST_ID) == b"replica-1"


def test_cache_separates_raw_guarded_and_decision_and_detects_corruption() -> None:
    """Returning raw bytes as guarded/decision output or ignoring a bad digest is unsafe."""
    client = InMemoryImmutableCacheClient()
    cache = ImmutableResponseCache(client)
    request = _request()
    cache.put_raw(request, b"raw")
    assert cache.get_guarded(request) is None
    assert cache.get_decision(request, decision_semantic_hash="d" * 64) is None
    cache.put_guarded(request, b"guarded")
    cache.put_decision(request, decision_semantic_hash="d" * 64, payload=b"decision")
    assert cache.get_guarded(request) == b"guarded"
    assert cache.get_decision(request, decision_semantic_hash="d" * 64) == b"decision"
    client.corrupt_for_test(cache.raw_storage_key(request), b"tampered")
    with pytest.raises(CacheCorruptionError):
        cache.get_raw(request, recovery_request_id=REQUEST_ID)


def test_new_request_and_forged_replica_cannot_recover_prior_raw_response() -> None:
    cache = ImmutableResponseCache(InMemoryImmutableCacheClient())
    original = _request()
    cache.put_raw(original, b"original-request-only")
    new_request = replace(original, request_id="00000000-0000-4000-8000-000000000022")
    assert cache.get_raw(new_request, recovery_request_id=new_request.request_id) is None
    for changed in (replace(original, replicate_id=2), replace(original, extraction_epoch=3)):
        assert cache.get_raw(changed, recovery_request_id=changed.request_id) is None


def test_namespace_fields_cannot_alias_through_delimiters() -> None:
    cache = ImmutableResponseCache(InMemoryImmutableCacheClient())
    first = CacheNamespace(TENANT_A, "consent", DOCUMENT_A, f"tagger#DOC#{DOCUMENT_B}#ROLE#tagger")
    second = CacheNamespace(TENANT_A, f"consent#DOC#{DOCUMENT_A}#ROLE#tagger", DOCUMENT_B, "tagger")
    cache.put_raw(_request(namespace=first), b"document-a-only")
    assert cache.get_raw(_request(namespace=second), recovery_request_id=REQUEST_ID) is None


def test_revoke_document_removes_manifested_cache_entries_and_blocks_reuse() -> None:
    """Deleting only a blob while leaving its manifest would permit cache resurrection."""
    client = InMemoryImmutableCacheClient()
    cache = ImmutableResponseCache(client)
    revoked = _request()
    survivor = _request(namespace=_namespace(document_version_id=DOCUMENT_B))
    cache.put_raw(revoked, b"revoke-me")
    cache.put_raw(survivor, b"keep-me")

    cache.revoke_document(_namespace())
    assert cache.get_raw(revoked, recovery_request_id=REQUEST_ID) is None
    assert cache.get_raw(survivor, recovery_request_id=REQUEST_ID) == b"keep-me"
    with pytest.raises(CacheRevokedError):
        cache.put_raw(revoked, b"cannot-recreate")


def test_invalid_hash_inputs_fail_closed() -> None:
    """NaN temperatures and malformed hashes cannot have portable cache signatures."""
    with pytest.raises(DomainValidationError):
        _request(temperature=float("nan"))
    with pytest.raises(DomainValidationError):
        _request(schema_sha256="not-a-hash")


def _changed_rule_pack(pack):
    """Build a second self-verifying rule snapshot with a changed rule identifier."""
    files = {path: json.loads(payload) for path, payload in pack.content}
    files["rubric/performance.yaml"]["branches"][0]["id"] = "PERF_E0_REVISION_2"
    manifest = {
        "version": pack.version,
        "ontology_version": pack.ontology_version,
        "source_document_sha256": pack.source_document_sha256,
        "mode": pack.mode,
        "effective_date": pack.effective_date,
        "files": list(pack.files),
        "unresolved_gap_ids": list(pack.unresolved_gap_ids),
    }
    return replace(
        pack,
        sha256=pack_content_hash(manifest, files),
        content=tuple((path, canonical_json(files[path])) for path in sorted(files)),
    )


def _engine_inputs():
    """Small local synthetic fixture exercising the real pure rules engine."""
    root = Path(__file__).resolve().parents[2]
    tenant_id = "11111111-1111-4111-8111-111111111111"
    document_id = "22222222-2222-4222-8222-222222222222"
    claim_id = "33333333-3333-4333-8333-333333333333"
    manifest = yaml.safe_load((root / "config/rule_pack_manifest.yaml").read_text())
    files = {
        path: yaml.safe_load((root / "config" / path).read_text()) for path in manifest["files"]
    }
    manifest.update(rule_pack_id=claim_id, tenant_id=tenant_id, approved_by=None, approved_at=None)
    manifest["sha256"] = pack_content_hash(manifest, files)
    assert validate_rulepack(manifest, files, [f"GAP-{index:03}" for index in range(1, 11)]).ok
    pack = snapshot_from_validated(manifest, files)
    source = SourceRef(
        claim_id,
        document_id,
        document_id,
        1,
        None,
        (1, 1, 10, 10),
        "a" * 64,
        "synthetic evidence",
        0,
        18,
        "located",
        "verified",
    )

    def fact(name: str, state: str = "present") -> ConfirmedFact:
        values = {
            "name": name,
            "state": state,
            "evidence_refs": (source,) if state == "present" else (),
            "source_tenant_id": tenant_id,
            "citation_verified": state == "present",
            "binding_accepted": state == "present",
            "search_coverage_verified": state == "absent",
            "source_scope": "local_claim",
            "normalized_value": None,
        }
        if name == "assurance_covered":
            values.update(source_scope="global_bound", normalized_value="covered")
        if name == "numerical_check":
            values.update(source_scope="computed_check")
        return ConfirmedFact(**values)

    facts = tuple(
        fact(name)
        for name in (
            "quantitative_or_qualified_ordinal",
            "unit_or_qualified_ordinal",
            "comparison_baseline",
            "calculation_boundary",
            "method",
            "assurance_covered",
            "numerical_check",
        )
    ) + (fact("reduction_or_improvement_claim", "absent"),)
    tags = ConfirmedTags(
        tenant_id=tenant_id,
        document_version_id=document_id,
        claim_id=claim_id,
        track="performance",
        facts=facts,
        tag_revision=1,
        packet_sha256="b" * 64,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_hashes=("1" * 64, "2" * 64, "3" * 64),
        ontology_version=pack.ontology_version,
    )
    context = RuleContext(
        tenant_id=tenant_id,
        document_version_id=document_id,
        claim_id=claim_id,
        packet_sha256="b" * 64,
        local_synthetic=True,
    )
    return tags, context, pack
