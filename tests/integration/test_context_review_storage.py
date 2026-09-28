"""Context persistence/carry/CAS seam; source validation has independent real-graph tests."""

from copy import deepcopy
from uuid import uuid4

import pytest
from proofops.application.reviews import ReviewRejected

from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_reviews import workspace
from tests.integration.test_ai_delegated_review import _actor


def test_context_is_immutable_carried_and_part_of_retry_identity(tmp_path, monkeypatch):
    _, service, inputs, review, body, _, _ = workspace(tmp_path)
    request = {"reviewed": "context"}
    receipt = {"request": request, "source_receipt": {"hash": "source"}, "projection": {}}
    calls = []

    def validate(actual, supplied, callback):
        assert actual is inputs and supplied == request
        calls.append(True)
        return deepcopy(receipt)

    monkeypatch.setattr(
        "proofops.application.claim_context_review.review_facility_context", validate
    )
    before = service.store.history(TENANT, RUN, review["claim_id"])
    key = str(uuid4())
    kwargs = dict(delegated_reviewer="context-test", delegation_authority="explicit test")
    result = service.resolve_ai_delegated_review(
        _actor(), review["review_id"], body, '"1"', key, context_review=request, **kwargs
    )
    after = service.store.history(TENANT, RUN, review["claim_id"])
    assert after["tags"][:-1] == before["tags"]
    assert after["tags"][-1]["claim_context_review"] == receipt
    assert (
        next(e for e in after["tags"][-1]["elements"] if e["element_id"] == "P6")["state"]
        == "unknown"
    )
    assert (
        service.resolve_ai_delegated_review(
            _actor(), review["review_id"], body, '"1"', key, context_review=request, **kwargs
        )
        == result
    )
    with pytest.raises(ReviewRejected):
        service.resolve_ai_delegated_review(
            _actor(),
            review["review_id"],
            body,
            '"1"',
            key,
            context_review={"reviewed": "changed"},
            **kwargs,
        )
    fresh = service.store.get(TENANT, review["review_id"])
    body = dict(body, base_tag_revision=result["new_tag_revision"])
    service.resolve_ai_delegated_review(
        _actor(),
        review["review_id"],
        body,
        f'"{fresh["revision"]}"',
        str(uuid4()),
        reopen=True,
        **kwargs,
    )
    carried = service.store.history(TENANT, RUN, review["claim_id"])["tags"][-1]
    assert carried["claim_context_review"]["request"] == request
    assert carried["claim_context_review"]["carried_from"]
    assert len(calls) == 2


@pytest.mark.parametrize("legacy_verifier", [False, True])
def test_context_review_uses_pinned_snapshot_without_nested_sqlite_writer(
    tmp_path, legacy_verifier
):
    import json
    import sqlite3
    from dataclasses import asdict, replace

    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.evidence.retrieval import freeze_packet
    from proofops.application.tagging.consensus import form_consensus
    from proofops.domain.provenance import canonical_hash

    from tests.unit.test_facility_context_review import case

    context_inputs, request = case()

    def prepare(inputs):
        claim = replace(context_inputs.context.claim, claim_id=inputs.context.claim.claim_id)
        packet = inputs.packet.to_dict()
        packet["graph_sha256"] = canonical_hash(asdict(context_inputs.original))
        packet = freeze_packet({k: v for k, v in packet.items() if k != "packet_sha256"})
        runs = tuple(
            replace(
                run,
                packet_sha256=packet.packet_sha256,
                graph_sha256=packet.to_dict()["graph_sha256"],
                guarded=replace(
                    run.guarded,
                    packet_sha256=packet.packet_sha256,
                    elements=tuple(
                        replace(e, state="unknown", evidence_refs=(), normalized_value=None)
                        for e in run.guarded.elements
                    ),
                ),
            )
            for run in inputs.tag_runs
        )
        return replace(
            inputs,
            context=replace(inputs.context, claim=claim),
            original=context_inputs.original,
            packet=packet,
            tag_runs=runs,
            rule_context=replace(inputs.rule_context, packet_sha256=packet.packet_sha256),
            consensus=form_consensus(
                runs, packet=packet, rulepack=inputs.rulepack, tenant_id=TENANT, tag_revision=1
            ),
        )

    _, service, inputs, review, body, _, _ = workspace(tmp_path, prepare_inputs=prepare)
    body["elements"] = [asdict(e) for e in inputs.consensus.candidate_elements]
    request["input_snapshot_sha256"] = canonical_hash(inputs.snapshot())
    run_store = LocalSQLiteRunStore(tmp_path / "state.sqlite")
    pinned = {"tenant_id": TENANT, "run_id": RUN, "claim_source_policy": {"reader": "pinned"}}
    with sqlite3.connect(run_store.path) as db:
        db.execute("INSERT INTO run_snapshots VALUES (?, ?, ?)", (TENANT, RUN, json.dumps(pinned)))
    service.load_run_snapshot = run_store.snapshot

    def attest(loaded, refs, *, pinned_run_snapshot=None):
        # Missing forwarding opens a real nested SQLite writer and fails with a lock.
        snapshot = pinned_run_snapshot or run_store.snapshot(TENANT, RUN)
        assert snapshot == pinned
        return loaded.original, {"records": [{"status": "verified"} for _ in refs]}

    def legacy_attest(loaded, refs):
        return loaded.original, {"records": [{"status": "verified"} for _ in refs]}

    service.verify_context_sources = legacy_attest if legacy_verifier else attest
    kwargs = dict(delegated_reviewer="sqlite-regression", delegation_authority="explicit test")
    result = service.resolve_ai_delegated_review(
        _actor(), review["review_id"], body, '"1"', str(uuid4()), context_review=request, **kwargs
    )
    assert result["review"]["status"] == "resolved"
    before = service.store.history(TENANT, RUN, review["claim_id"])
    receipt = before["tags"][-1]["claim_context_review"]
    assert receipt["projection"]["dimensions"]["facility"]["quote"] == "그린팩토리"
    assert receipt["projection"]["numeric_check"]["status"] == "needs_review"
    body["base_tag_revision"] = 2
    carried = service.resolve_ai_delegated_review(
        _actor(), review["review_id"], body, '"2"', str(uuid4()), reopen=True, **kwargs
    )
    assert carried["new_tag_revision"] == 3
    after = service.store.history(TENANT, RUN, review["claim_id"])
    assert after["tags"][:-1] == before["tags"]
    assert after["tags"][-1]["claim_context_review"]["source_receipt"] == receipt["source_receipt"]
    assert (
        next(e for e in after["tags"][-1]["elements"] if e["element_id"] == "P6")["state"]
        == "unknown"
    )
