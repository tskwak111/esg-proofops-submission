"""Regression: published assurance is re-verified against the real run graph.

Proves the producer (LocalAssuranceStore.publish of an extract_assurance result)
and consumer (load + match_assurance) form a real callable path bound to the
run's committed graph, that publication advances mutation_epoch, and that a
wrong-tenant, tampered-source, or forged statement (even with a self-consistent
recomputed hash) is rejected rather than replayed as coverage.

Automatic assurance extraction and per-claim scope dimensions are NOT yet wired
into the pipeline; these tests publish a human/synthetic statement directly and
match with unknown claim dimensions, so status stays undetermined for real
synthetic (unverified) sources while the source-backed opinion still flows.
"""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import pytest
from proofops.adapters.local.assurance_store import (
    AssurancePublicationConflict,
    LocalAssuranceStore,
)
from proofops.application.assurance import ClaimContext, extract_assurance, match_assurance
from proofops.application.ports.models import ModelBinding
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash

from tests.integration.test_local_extract_runner import extraction_setup
from tests.integration.test_local_parser_runner import TENANT
from tests.integration.test_run_lifecycle import client  # noqa: F401  (fixture wiring)

FOREIGN = "77777777-7777-4777-8777-777777777777"
STATEMENT = "44444444-4444-4444-8444-444444444444"
CLAIM = "66666666-6666-4666-8666-666666666666"
BINDING = ModelBinding("synthetic-assurance", "assurance", True)


def _committed(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    store = LocalAssuranceStore(service.store, service.uploads, runner.parser)
    from proofops.adapters.local.run_artifacts import load_run_graph

    graph = load_run_graph(
        service.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    return service, run_id, store, graph


def _statement_from_graph(graph, **kw):
    """Build a real source-backed statement citing the run's own graph blocks."""
    blocks = sorted(graph.blocks, key=lambda b: b.source_id)[:2]
    provider = blocks[0].source_ref(
        normalized_char_start=0, normalized_char_end=len(blocks[0].normalized_text)
    )
    standard = blocks[1].source_ref(
        normalized_char_start=0, normalized_char_end=len(blocks[1].normalized_text)
    )
    fields = {"provider": (provider,), "standard_raw": (standard,)}
    refs = (provider, standard)
    options = dict(
        tagged_fields=fields,
        tenant_id=graph.tenant_id,
        statement_id=STATEMENT,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_id=1,
    )
    options.update(kw)
    return extract_assurance(graph, refs, BINDING, **options)


def _claim_ctx(version):
    return ClaimContext(TENANT, version, CLAIM, None, None, (), ())


def test_publish_reverifies_against_graph_and_load_replays(tmp_path, monkeypatch):
    service, run_id, store, graph = _committed(tmp_path, monkeypatch)
    statement = _statement_from_graph(graph)

    assert store.load(TENANT, run_id) is None
    before = service.store.jobs.get_run(TENANT, run_id)["mutation_epoch"]

    digest = store.publish(TENANT, run_id, statement)
    assert digest == statement.semantic_hash
    # Publication advances the run fence so stale analysis snapshots invalidate.
    after = service.store.jobs.get_run(TENANT, run_id)["mutation_epoch"]
    assert after == before + 1

    replayed = store.load(TENANT, run_id)
    assert replayed is not None
    assert replayed.semantic_hash == statement.semantic_hash
    assert replayed.source_refs == statement.source_refs

    # Real synthetic sources are unverified, so scope stays undetermined, but a
    # real source-backed statement now flows through the shared matcher.
    match = match_assurance(replayed, _claim_ctx(graph.document_version_id))
    assert match.statement_id == STATEMENT
    assert match.evidence_refs  # source-backed, not the old hard-coded empty


def test_republish_idempotent_but_conflict_on_change(tmp_path, monkeypatch):
    service, run_id, store, graph = _committed(tmp_path, monkeypatch)
    first = _statement_from_graph(graph)
    store.publish(TENANT, run_id, first)
    assert store.publish(TENANT, run_id, first) == first.semantic_hash

    second = _statement_from_graph(graph, replicate_id=2)
    assert second.semantic_hash != first.semantic_hash
    with pytest.raises(AssurancePublicationConflict):
        store.publish(TENANT, run_id, second)


def test_wrong_tenant_is_blocked(tmp_path, monkeypatch):
    service, run_id, store, graph = _committed(tmp_path, monkeypatch)
    statement = _statement_from_graph(graph)
    with pytest.raises(KeyError):
        store.publish(FOREIGN, run_id, statement)


def test_forged_statement_with_recomputed_hash_is_rejected(tmp_path, monkeypatch):
    service, run_id, store, graph = _committed(tmp_path, monkeypatch)
    genuine = _statement_from_graph(graph)

    # Forge a citation that does not exist in the real graph, then let the frozen
    # dataclass recompute a self-consistent semantic_hash. Re-extraction against
    # the trusted graph must still reject it: the hash alone is not verification.
    forged_ref = replace(genuine.source_refs[0], source_id=str(uuid4()), quote="forged coverage")
    forged = replace(
        genuine,
        source_refs=(forged_ref,) + genuine.source_refs[1:],
        tagged_fields=tuple(
            (name, (forged_ref,) if name == "provider" else refs)
            for name, refs in genuine.tagged_fields
        ),
    )
    # Its hash is internally consistent, proving hash-only checks are insufficient.
    from dataclasses import asdict

    assert forged.semantic_hash == canonical_hash(asdict(forged))
    assert forged.semantic_hash != genuine.semantic_hash

    with pytest.raises(DomainValidationError):
        store.publish(TENANT, run_id, forged)


def test_stale_run_epoch_during_verification_is_rejected(tmp_path, monkeypatch):
    service, run_id, store, graph = _committed(tmp_path, monkeypatch)
    statement = _statement_from_graph(graph)

    # Simulate a concurrent reparse advancing mutation_epoch between the graph
    # load and the writer transaction: the CAS fence must reject the publish
    # instead of binding the statement to a now-stale run snapshot.
    original = LocalAssuranceStore._reextract

    def racing_reextract(stmt, g):
        with service.store.jobs._transaction() as db:
            run = service.store.jobs._get(db, TENANT, run_id, "run", "META")
            service.store.jobs._bump_run(db, run)
        return original(stmt, g)

    monkeypatch.setattr(store, "_reextract", racing_reextract)
    with pytest.raises(AssurancePublicationConflict):
        store.publish(TENANT, run_id, statement)
    # Nothing was published, so a later clean publish still succeeds.
    monkeypatch.undo()
    assert store.load(TENANT, run_id) is None


def test_modified_source_after_publish_is_rejected_on_load(tmp_path, monkeypatch):
    import json

    service, run_id, store, graph = _committed(tmp_path, monkeypatch)
    store.publish(TENANT, run_id, _statement_from_graph(graph))

    # Tamper the stored source-backed quote without repairing the stored hash.
    with service.store.jobs._transaction() as db:
        raw = service.store.jobs._raw(db, TENANT, run_id, "assurance_statement", "STATEMENT")
        payload = json.loads(raw)
        payload["source_refs"][0]["quote"] = "forged"
        service.store.jobs._put(db, TENANT, run_id, "assurance_statement", "STATEMENT", payload)

    with pytest.raises(DomainValidationError):
        store.load(TENANT, run_id)
