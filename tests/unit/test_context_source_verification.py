"""Routing and receipt compatibility for report-level source verification."""

from contextlib import nullcontext
from dataclasses import asdict, replace
from hashlib import sha256
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

import pytest
from proofops.adapters.local import claim_source_verification, table_span_source_verification
from proofops.adapters.local.tag_store import LocalTagStore
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_citations import TENANT, snapshot


def _case(monkeypatch, table_kind="table_cell"):
    graph, _ = snapshot("GRI Index AA1000AS v3")
    paragraph = replace(graph.blocks[0], quality="unverified")
    candidate = replace(
        paragraph.candidates[0],
        kind=table_kind,
        source=replace(paragraph.candidates[0].source, source_native_id="table-cell"),
    )
    cell = replace(
        paragraph,
        source_id=str(uuid5(NAMESPACE_URL, "table-cell")),
        kind=table_kind,
        candidates=(candidate,),
    )
    other = replace(
        paragraph,
        source_id=str(uuid5(NAMESPACE_URL, "whole-table")),
        kind="table",
        candidates=(
            replace(
                candidate,
                kind="table",
                source=replace(candidate.source, source_native_id="whole-table"),
            ),
        ),
    )
    source = b"source PDF bytes"
    graph = replace(
        graph,
        source_sha256=sha256(source).hexdigest(),
        blocks=(paragraph, cell, other),
        candidates=(
            replace(
                graph.candidates[0],
                source_sha256=sha256(source).hexdigest(),
                blocks=(paragraph.candidates[0], candidate, other.candidates[0]),
            ),
        ),
    )
    inputs = SimpleNamespace(
        context=SimpleNamespace(
            claim=SimpleNamespace(tenant_id=TENANT, document_version_id=graph.document_version_id)
        ),
        original=graph,
    )
    store = LocalTagStore(None, SimpleNamespace(read_original=lambda *_: source), None)
    refs = tuple(block.source_ref() for block in (paragraph, cell, other))

    def receipt(kind, selected):
        result = dict(
            schema=kind,
            policy={"schema": kind},
            records=[{"ref": asdict(ref), "status": "verified"} for ref in selected],
        )
        result["artifact_sha256"] = canonical_hash(result)
        return result

    calls = []

    def paragraphs(_graph, _source, selected, *, tenant_id):
        calls.append(("paragraph", selected, tenant_id))
        return receipt("claim_source_attestation_v1", selected)

    def tables(_graph, _source, selected, *, tenant_id):
        calls.append(("table", selected, tenant_id))
        return receipt("table_span_source_attestation_v1", selected)

    monkeypatch.setattr(claim_source_verification, "attest_claim_spans", paragraphs)
    monkeypatch.setattr(table_span_source_verification, "attest_table_spans", tables)
    return store, inputs, refs, calls, receipt


def test_paragraph_receipt_is_identical_to_r38(monkeypatch):
    store, inputs, refs, calls, receipt = _case(monkeypatch)
    _, actual = store.verify_context_sources(inputs, (refs[0],))
    expected = receipt("claim_source_attestation_v1", (refs[0],))
    assert actual == expected
    assert actual["artifact_sha256"] == expected["artifact_sha256"]
    assert calls == [("paragraph", (refs[0],), TENANT)]


def test_pinned_reader_verifies_paragraph_and_records_policy_for_replay(monkeypatch):
    from proofops.adapters.local import claim_source_policies

    store, inputs, refs, calls, receipt = _case(monkeypatch)
    inputs.run_id = "pinned-run"
    policy = dict(schema="claim_span_bullet_alignment_policy_v1", wrapper_sha256="a" * 64)
    store.store = SimpleNamespace(
        snapshot=lambda tenant_id, run_id: (
            {"claim_source_policy": policy}
            if (tenant_id, run_id) == (TENANT, "pinned-run")
            else None
        )
    )
    verified = SimpleNamespace()

    def configured_reader(recorded_policy):
        assert recorded_policy == policy
        return verified

    monkeypatch.setattr(claim_source_policies, "claim_source_reader", configured_reader)

    default = receipt("claim_source_attestation_v1", (refs[0],))
    default["records"][0]["status"] = "unresolved"
    default["artifact_sha256"] = canonical_hash(
        {k: v for k, v in default.items() if k != "artifact_sha256"}
    )
    monkeypatch.setattr(claim_source_verification, "attest_claim_spans", lambda *_a, **_kw: default)

    def attest(_graph, _source, selected, *, tenant_id):
        calls.append(("configured", selected, tenant_id))
        result = receipt("claim_source_attestation_v1", selected)
        result["policy"] = policy
        result["artifact_sha256"] = canonical_hash(
            {k: v for k, v in result.items() if k != "artifact_sha256"}
        )
        return result

    verified.attest_claim_spans = attest
    assert default["records"][0]["status"] == "unresolved"
    _, actual = store.verify_context_sources(inputs, (refs[0],))
    _, replay = store.verify_context_sources(inputs, (refs[0],))

    assert actual["records"][0]["status"] == "verified"
    assert actual["policy"] == policy
    assert actual["reader_policy_sha256"] == canonical_hash(policy)
    assert actual == replay
    assert calls == [
        ("configured", (refs[0],), TENANT),
        ("configured", (refs[0],), TENANT),
    ]


def test_table_only_ref_keeps_table_receipt_when_run_pins_paragraph_reader(monkeypatch):
    from proofops.adapters.local import claim_source_policies

    store, inputs, refs, _, _ = _case(monkeypatch)
    _, baseline = store.verify_context_sources(inputs, (refs[1],))
    inputs.run_id = "pinned-run"
    policy = {"schema": "claim_span_bullet_alignment_policy_v1", "wrapper_sha256": "a" * 64}
    store.store = SimpleNamespace(snapshot=lambda *_: {"claim_source_policy": policy})
    configured = SimpleNamespace(
        attest_claim_spans=lambda *_args, **_kwargs: pytest.fail(
            "table-only refs must not call the paragraph reader"
        )
    )
    monkeypatch.setattr(claim_source_policies, "claim_source_reader", lambda _: configured)

    _, actual = store.verify_context_sources(inputs, (refs[1],))

    assert actual == baseline


def test_only_explicit_replay_uses_stored_reader(monkeypatch):
    from proofops.adapters.local import claim_source_policies

    store, inputs, refs, _, receipt = _case(monkeypatch)
    inputs.run_id = "pinned-run"
    inputs.context.claim.claim_id = "claim-1"
    pinned_policy = {"schema": "claim_span_bullet_alignment_policy_v1", "wrapper_sha256": "a" * 64}
    legacy_policy = claim_source_verification.claim_source_policy()
    stored = receipt("claim_source_attestation_v1", (refs[0],))
    stored["policy"] = legacy_policy
    stored["artifact_sha256"] = canonical_hash(
        {key: value for key, value in stored.items() if key != "artifact_sha256"}
    )
    tag = {"report_level_review": [{"refs": [asdict(refs[0])], "source_receipt": stored}]}
    jobs = SimpleNamespace(
        _transaction=lambda: nullcontext(object()),
        _get=lambda _db, _tenant, _run, kind, _key: {"tag_revision": 1}
        if kind == "claim_head"
        else tag,
    )
    store.store = SimpleNamespace(
        snapshot=lambda *_: {"claim_source_policy": pinned_policy}, jobs=jobs
    )
    selected = []

    def pinned_attestation(*_args, **_kwargs):
        result = receipt("claim_source_attestation_v1", (refs[0],))
        result["policy"] = pinned_policy
        result["artifact_sha256"] = canonical_hash(
            {key: value for key, value in result.items() if key != "artifact_sha256"}
        )
        return result

    pinned_reader = SimpleNamespace(attest_claim_spans=pinned_attestation)
    legacy_reader = SimpleNamespace(attest_claim_spans=lambda *_args, **_kwargs: stored)

    def dispatch(policy):
        selected.append(policy)
        return legacy_reader if policy == legacy_policy else pinned_reader

    monkeypatch.setattr(claim_source_policies, "claim_source_reader", dispatch)
    _, fresh = store.verify_context_sources(inputs, (refs[0],))
    assert fresh["policy"] == pinned_policy
    assert selected == [pinned_policy]

    _, actual = store.verify_context_sources(inputs, (refs[0],), replay_receipt=stored)
    assert actual == stored
    assert selected == [pinned_policy, legacy_policy]

    tampered = dict(stored, policy={**legacy_policy, "verifier_sha256": "0" * 64})
    tampered["artifact_sha256"] = canonical_hash(
        {key: value for key, value in tampered.items() if key != "artifact_sha256"}
    )
    tag["report_level_review"][0]["source_receipt"] = tampered
    with pytest.raises(ValueError, match="CONTEXT_SOURCE_REPLAY_MISMATCH"):
        store.verify_context_sources(inputs, (refs[0],), replay_receipt=tampered)


def test_explicit_replay_rejects_false_reader_policy_hash(monkeypatch):
    from proofops.adapters.local import claim_source_policies

    store, inputs, refs, _, receipt = _case(monkeypatch)
    inputs.run_id = "pinned-run"
    inputs.context.claim.claim_id = "claim-1"
    policy = {"schema": "claim_span_bullet_alignment_policy_v1", "wrapper_sha256": "a" * 64}

    def attestation(*_args, **_kwargs):
        result = receipt("claim_source_attestation_v1", (refs[0],))
        result["policy"] = policy
        result["artifact_sha256"] = canonical_hash(
            {key: value for key, value in result.items() if key != "artifact_sha256"}
        )
        return result

    stored = attestation()
    stored["reader_policy_sha256"] = "0" * 64
    stored["artifact_sha256"] = canonical_hash(
        {key: value for key, value in stored.items() if key != "artifact_sha256"}
    )
    tag = {"report_level_review": [{"refs": [asdict(refs[0])], "source_receipt": stored}]}
    jobs = SimpleNamespace(
        _transaction=lambda: nullcontext(object()),
        _get=lambda _db, _tenant, _run, kind, _key: {"tag_revision": 1}
        if kind == "claim_head"
        else tag,
    )
    store.store = SimpleNamespace(snapshot=lambda *_: {"claim_source_policy": policy}, jobs=jobs)
    monkeypatch.setattr(
        claim_source_policies,
        "claim_source_reader",
        lambda _: SimpleNamespace(attest_claim_spans=attestation),
    )

    with pytest.raises(ValueError, match="CONTEXT_SOURCE_REPLAY_MISMATCH"):
        store.verify_context_sources(inputs, (refs[0],), replay_receipt=stored)


@pytest.mark.parametrize("table_kind", ("table_cell", "table_row"))
def test_table_and_mixed_receipts_replay_identically(monkeypatch, table_kind):
    store, inputs, refs, calls, _ = _case(monkeypatch, table_kind)
    scoped, table_only = store.verify_context_sources(inputs, (refs[1],))
    assert table_only["records"][0]["status"] == "verified"
    assert scoped.verified_spans[-1].source_id == refs[1].source_id
    _, first = store.verify_context_sources(inputs, refs[:2])
    _, replay = store.verify_context_sources(inputs, refs[:2])
    assert canonical_hash(first) == canonical_hash(replay)
    assert first["artifact_sha256"] == canonical_hash(
        {key: value for key, value in first.items() if key != "artifact_sha256"}
    )
    assert set(first["policy_hashes"]) == {"paragraph", "table"}
    assert [record["ref"]["source_id"] for record in first["records"]] == [
        refs[0].source_id,
        refs[1].source_id,
    ]
    assert calls[-2:] == [
        ("paragraph", (refs[0],), TENANT),
        ("table", (refs[1],), TENANT),
    ]


def test_whole_table_and_unverified_table_record_are_rejected(monkeypatch):
    store, inputs, refs, calls, _ = _case(monkeypatch)
    with pytest.raises(ValueError, match="CONTEXT_SOURCE_REJECTED"):
        store.verify_context_sources(inputs, (refs[2],))
    assert calls == []

    def unresolved(_graph, _source, selected, *, tenant_id):
        return {"records": [{"status": "unresolved"}]}

    monkeypatch.setattr(table_span_source_verification, "attest_table_spans", unresolved)
    with pytest.raises(ValueError, match="CONTEXT_SOURCE_REJECTED"):
        store.verify_context_sources(inputs, (refs[0], refs[1]))
