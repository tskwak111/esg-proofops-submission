"""Incremental publication keeps the unchanged reader's exact receipt bytes."""

from copy import deepcopy
from dataclasses import asdict, replace
from random import Random

import pytest
from proofops.adapters.local import claim_source_verification as reader
from proofops.adapters.local import native_replay_cache as cache
from proofops.application.claims import (
    Claim,
    ClaimDiscovery,
    ClaimScope,
    ExtractionProfile,
    ExtractionReceipt,
)
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json

from tests.acceptance.test_parsing import FOREIGN, TENANT
from tests.integration.test_claim_source_verification import inputs


def test_attestation_storage_uses_one_total_budget_and_evicts_oldest(monkeypatch):
    monkeypatch.setattr(cache, "_MAX_CLAIM_ATTESTATION_BYTES", 8192)
    cache._claim_attestations.clear()
    receipt = {
        "records": [{"status": "verified", "ref": {"source_id": "source"}}],
        "readings": {"source": Random(42).randbytes(3000).hex()},
    }
    try:
        for key in ("oldest", "retained", "newest"):
            cache._remember_claim_attestation(key, receipt)
        assert "newest" in cache._claim_attestations
        assert "oldest" not in cache._claim_attestations
        assert sum(map(len, cache._claim_attestations.values())) <= 8192
        assert cache.marshal_loads(cache.decompress(cache._claim_attestations["newest"])) == receipt
    finally:
        cache._claim_attestations.clear()


@pytest.fixture
def attestation(monkeypatch):
    cache._claim_attestations.clear()
    cache._claim_replays.clear()
    source, graph, ref = inputs()
    calls = []
    original = reader.attest_claim_spans

    def counted(graph, source, refs, *, tenant_id):
        calls.append(tuple(refs))
        return original(graph, source, refs, tenant_id=tenant_id)

    monkeypatch.setattr(reader, "attest_claim_spans", counted)
    monkeypatch.setattr(
        reader,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions 1234 tCO2e"),
    )
    yield dict(reader=reader, graph=graph, source=source, tenant_id=TENANT), ref, calls, original
    cache._claim_attestations.clear()
    cache._claim_replays.clear()


def test_incremental_receipt_is_byte_identical_and_returns_deep_copies(attestation):
    args, ref, calls, original = attestation
    whole = args["graph"].blocks[0].source_ref()
    first = cache.attest_claims_cached(**args, refs=(ref,))
    assert first["records"][0]["status"] == "verified"
    first["records"].clear()
    refs = (whole, ref, ref)
    result = cache.attest_claims_cached(**args, refs=refs)
    assert calls == [(ref,), (whole,)]
    expected = original(args["graph"], args["source"], refs, tenant_id=TENANT)
    assert canonical_json(result).encode() == canonical_json(expected).encode()
    result["readings"].clear()
    assert cache.attest_claims_cached(**args, refs=refs) == expected
    assert len(calls) == 2


def test_changed_inputs_are_rechecked_and_failures_are_not_cached(attestation, monkeypatch):
    args, ref, calls, _ = attestation
    cache.attest_claims_cached(**args, refs=(ref,))
    for changed in ({"source": args["source"] + b"x"}, {"tenant_id": FOREIGN}):
        for _ in range(2):
            with pytest.raises(ValueError):
                cache.attest_claims_cached(**(args | changed), refs=(ref,))
    assert len(calls) == 5
    graph = replace(args["graph"], blocks=())
    for _ in range(2):
        result = cache.attest_claims_cached(**(args | {"graph": graph}), refs=(ref,))
        assert result["records"][0]["status"] == "unresolved"
    assert len(calls) == 7
    invalid = replace(ref, quote="emissions 9999 tCO2e")
    for _ in range(2):
        result = cache.attest_claims_cached(**args, refs=(invalid,))
        assert result["records"][0]["status"] == "unresolved"
    assert len(calls) == 9
    policy = reader.claim_source_policy()
    monkeypatch.setattr(reader, "claim_source_policy", lambda: dict(policy, revision="new"))
    cache.attest_claims_cached(**args, refs=(ref,))
    assert len(calls) == 10
    monkeypatch.setenv("DEVELOPER_DIR", "/different")
    cache.attest_claims_cached(**args, refs=(ref,))
    assert len(calls) == 11
    monkeypatch.setattr(cache, "version", lambda name: "different")
    cache.attest_claims_cached(**args, refs=(ref,))
    assert len(calls) == 12


def test_changed_reading_falls_back_to_whole_reader_and_storage_is_bounded(
    attestation, monkeypatch
):
    args, ref, calls, original = attestation
    cache.attest_claims_cached(**args, refs=(ref,))
    monkeypatch.setattr(
        reader,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="different"),
    )
    whole = args["graph"].blocks[0].source_ref()
    result = cache.attest_claims_cached(**args, refs=(ref, whole))
    assert calls == [(ref,), (whole,), (ref, whole)]
    assert result == original(args["graph"], args["source"], (ref, whole), tenant_id=TENANT)
    assert all(r["status"] == "unresolved" for r in result["records"])
    monkeypatch.setattr(cache, "_MAX_CLAIM_ATTESTATION_BYTES", 1)
    cache._claim_attestations.clear()
    for _ in range(2):
        cache.attest_claims_cached(**args, refs=(ref,))
    assert not cache._claim_attestations


def test_large_native_receipt_is_reused_losslessly_with_bounded_storage(attestation, monkeypatch):
    args, ref, calls, _ = attestation
    # Full-report glyph readings exceed 8 MiB even though their JSON compresses well.
    geometry = "source-bound glyph geometry " * 400_000
    monkeypatch.setattr(
        reader,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions 1234 tCO2e", geometry=geometry),
    )
    first = cache.attest_claims_cached(**args, refs=(ref,))
    expected = canonical_json(first)
    first["readings"].clear()
    second = cache.attest_claims_cached(**args, refs=(ref,))
    assert calls == [(ref,)]
    assert canonical_json(second) == expected
    assert sum(len(blob) for blob in cache._claim_attestations.values()) <= 8 * 1024 * 1024


def discovery_for(graph, refs):
    scope = ClaimScope(TENANT, graph.document_version_id, graph.parse_manifest_id)
    extraction = ExtractionReceipt(
        refs[0].source_id,
        "a" * 64,
        None,
        None,
        ExtractionProfile("b" * 64, "c" * 64, "d" * 64, True),
        "extracted",
    )
    claims = tuple(
        Claim(
            str(index),
            TENANT,
            graph.document_version_id,
            graph.parse_manifest_id,
            graph.source_sha256,
            ref.quote,
            (ref,),
            "unverified",
            (),
            extraction,
        )
        for index, ref in enumerate(refs)
    )
    return ClaimDiscovery(scope, graph.source_sha256, claims, (), (), (extraction,), True, ())


@pytest.mark.parametrize("rendered_status", ["read", "unavailable", "error"])
def test_completed_negative_ocr_reuses_exact_record_but_unavailable_retries(
    attestation, monkeypatch, rendered_status
):
    args, ref, calls, original = attestation
    monkeypatch.setattr(
        reader,
        "_rendered_text",
        lambda *a, **k: dict(status=rendered_status, text="wrong number 9999"),
    )
    expected = original(args["graph"], args["source"], (ref,), tenant_id=TENANT)
    for _ in range(2):
        result = cache.attest_claims_cached(**args, refs=(ref,))
        assert canonical_json(result) == canonical_json(expected)
        assert result["records"][0]["status"] == "unresolved"
    assert len(calls) == (1 if rendered_status == "read" else 2)


def test_same_request_replay_matches_original_bytes_without_repeating_ocr(attestation, monkeypatch):
    args, ref, calls, _ = attestation
    # The span is readable; its containing paragraph remains unresolved.
    monkeypatch.setattr(
        reader,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page I emissions 1234 tCO2e"),
    )
    whole = args["graph"].blocks[0].source_ref()
    discovery = discovery_for(args["graph"], (whole, ref, ref))
    discovery = replace(
        discovery,
        claims=discovery.claims
        + (
            replace(discovery.claims[0], source_quality="unknown"),
            replace(discovery.claims[1], source_quality="verified"),
            replace(discovery.claims[0], source_refs=()),
        ),
    )
    receipt = cache.attest_claims_cached(**args, refs=reader.discovery_refs(discovery))
    replay_args = args | dict(
        policy=reader.claim_source_policy(), receipt=receipt, discovery=discovery
    )
    result = cache.replay_claims_cached(**replay_args)
    assert len(calls) == 1
    expected = reader.replay_claim_spans(
        receipt, args["graph"], args["source"], discovery, tenant_id=TENANT
    )
    assert canonical_json([asdict(x) for x in result]) == canonical_json(
        [asdict(x) for x in expected]
    )
    assert [c.source_quality for c in result[0].claims] == [
        "unverified",
        "verified",
        "verified",
        "unknown",
        "verified",
        "unverified",
    ]
    assert result[1].blocks[0].quality == "unverified"
    calls.clear()
    assert cache.replay_claims_cached(**replay_args) == result
    assert not calls
    forged = deepcopy(receipt)
    forged["records"][0].update(status="verified", reason=None)
    forged["artifact_sha256"] = canonical_hash(
        {k: v for k, v in forged.items() if k != "artifact_sha256"}
    )
    for warm in (True, False):
        if not warm:
            cache._claim_attestations.clear()
            cache._claim_replays.clear()
        with pytest.raises(ValueError, match="CLAIM_SOURCE_RECEIPT_MISMATCH"):
            cache.replay_claims_cached(**(replay_args | {"receipt": forged}))


@pytest.mark.parametrize("rendered_status", ["unavailable", "error"])
def test_unavailable_replay_is_never_memoized(attestation, monkeypatch, rendered_status):
    args, ref, calls, original = attestation
    monkeypatch.setattr(
        reader,
        "_rendered_text",
        lambda *a, **k: dict(status=rendered_status),
    )
    receipt = original(args["graph"], args["source"], (ref,), tenant_id=TENANT)
    replay_args = args | dict(
        policy=reader.claim_source_policy(),
        receipt=receipt,
        discovery=discovery_for(args["graph"], (ref,)),
    )
    for _ in range(2):
        result = cache.replay_claims_cached(**replay_args)
        assert result[0].claims[0].source_quality == "unverified"
    assert len(calls) == 2
    assert not cache._claim_attestations
    assert not cache._claim_replays


def test_warm_replay_rejects_changed_inputs_and_effective_reader_policy(attestation, monkeypatch):
    args, ref, _, _ = attestation
    receipt = cache.attest_claims_cached(**args, refs=(ref,))
    discovery = discovery_for(args["graph"], (ref,))
    replay_args = args | dict(
        policy=reader.claim_source_policy(), receipt=receipt, discovery=discovery
    )
    cache.replay_claims_cached(**replay_args)
    changed_graph = replace(args["graph"], blocks=())
    for changed in (
        {"tenant_id": FOREIGN},
        {"source": args["source"] + b"x"},
        {"graph": changed_graph},
        {"discovery": replace(discovery, claims=())},
    ):
        with pytest.raises(ValueError):
            cache.replay_claims_cached(**(replay_args | changed))
    policy = reader.claim_source_policy()
    monkeypatch.setattr(reader, "claim_source_policy", lambda: dict(policy, revision="changed"))
    with pytest.raises(ValueError, match="CLAIM_SOURCE_RECEIPT_MISMATCH"):
        cache.replay_claims_cached(**replay_args)


def test_incremental_negative_and_positive_records_match_real_reader(attestation, monkeypatch):
    args, ref, calls, original = attestation
    whole = args["graph"].blocks[0].source_ref()
    monkeypatch.setattr(
        reader,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page I emissions 1234 tCO2e"),
    )
    first = cache.attest_claims_cached(**args, refs=(whole,))
    assert first["records"][0]["status"] == "unresolved"
    refs = (ref, whole, whole)
    result = cache.attest_claims_cached(**args, refs=refs)
    assert calls == [(whole,), (ref,)]
    assert canonical_json(result) == canonical_json(
        original(args["graph"], args["source"], refs, tenant_id=TENANT)
    )
    assert [r["status"] for r in result["records"]] == ["verified", "unresolved", "unresolved"]
    first["readings"].clear()
    assert cache.attest_claims_cached(**args, refs=refs) == result
    assert len(calls) == 2


def test_unknown_projection_version_uses_original_replay(attestation, monkeypatch):
    args, ref, calls, _ = attestation
    receipt = cache.attest_claims_cached(**args, refs=(ref,))
    discovery = discovery_for(args["graph"], (ref,))
    monkeypatch.setattr(cache, "_CLAIM_PROJECTION_VERIFIER", "unknown-version")
    result = cache.replay_claims_cached(
        **args, policy=reader.claim_source_policy(), receipt=receipt, discovery=discovery
    )
    assert len(calls) == 2
    assert result == reader.replay_claim_spans(
        receipt, args["graph"], args["source"], discovery, tenant_id=TENANT
    )
