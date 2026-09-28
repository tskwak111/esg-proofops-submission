"""Persistence boundary fixtures; no real provider calls or domain approvals."""

import json
from dataclasses import asdict, replace

import pytest
from proofops.adapters.local.review_store import LocalSQLiteReviewStore
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.application.reviews import ReviewRejected, ReviewService
from proofops.application.tagging.consensus import form_consensus
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_reviews import workspace
from tests.integration.test_live_tagging_worker import configured


def prepare(tmp_path, monkeypatch, fault=None):
    source = tmp_path / "source"
    source.mkdir()
    _, _, inputs, _, _, _, _ = workspace(source)
    transport = tmp_path / "transport"
    transport.mkdir()
    runtime, _, _, _, _, _ = configured(transport, monkeypatch)
    runs = tuple(
        replace(
            r,
            synthetic=False,
            model_sha256=runtime.settings.model_sha256,
            prompt_sha256=canonical_hash(
                runtime.settings.system_for_track(
                    inputs.packet.to_dict()["track"],
                    inputs.packet.to_dict()["safe_harbor_category"],
                )
            ),
        )
        for r in inputs.tag_runs
    )
    inputs = replace(
        inputs,
        tag_runs=runs,
        rule_context=replace(inputs.rule_context, local_synthetic=False),
        consensus=form_consensus(
            runs,
            packet=inputs.packet,
            rulepack=inputs.rulepack,
            tenant_id=inputs.original.tenant_id,
            tag_revision=1,
        ),
    )
    frozen = dict(
        runtime.snapshot,
        tenant_id=inputs.original.tenant_id,
        run_id=inputs.run_id,
        document=dict(
            version_id=inputs.original.document_version_id,
            sha256=inputs.original.source_sha256,
        ),
        rulepack=asdict(inputs.rulepack),
        rulepack_use="candidate_tagging_reference_only",
    )
    if fault == "source":
        frozen["document"]["sha256"] = canonical_hash("another document")
    if fault == "mode":
        frozen["tagging_mode"] = "local_synthetic"
    frozen.pop("input_hash")
    frozen["input_hash"] = canonical_hash(frozen)
    if fault == "hash":
        frozen["input_hash"] = "b" * 64
    if fault in {"synthetic_receipt", "model", "prompt"}:
        changes = (
            {"synthetic": True}
            if fault == "synthetic_receipt"
            else {fault + "_sha256": canonical_hash("different setting")}
        )
        changed = tuple(replace(r, **changes) for r in runs)
        inputs = replace(
            inputs,
            tag_runs=changed,
            consensus=form_consensus(
                changed,
                packet=inputs.packet,
                rulepack=inputs.rulepack,
                tenant_id=inputs.original.tenant_id,
                tag_revision=1,
            ),
        )
    store = LocalSQLiteRunStore(tmp_path / "live.sqlite")
    store.jobs.create_run(
        inputs.original.tenant_id, inputs.run_id, inputs.original.document_version_id
    )
    if fault != "missing":
        with store.jobs._transaction() as db:
            db.execute(
                "INSERT INTO run_snapshots VALUES (?, ?, ?)",
                (inputs.original.tenant_id, inputs.run_id, json.dumps(frozen)),
            )
    service = ReviewService(
        LocalSQLiteReviewStore(store.jobs), load_inputs=lambda tenant, run, claim: inputs
    )
    return service, inputs, store


def test_live_candidate_publication_survives_reopen(tmp_path, monkeypatch):
    service, inputs, store = prepare(tmp_path, monkeypatch)
    review = service.publish(inputs)
    assert "RULEPACK_APPROVAL_REQUIRED" in review["reason_codes"]
    reopened = LocalSQLiteReviewStore(LocalSQLiteRunStore(store.path).jobs)
    assert reopened.get(inputs.original.tenant_id, review["review_id"]) == review
    assert service.publish(inputs) == review
    history = reopened.history(inputs.original.tenant_id, inputs.run_id, review["claim_id"])
    assert history == service.store.history(
        inputs.original.tenant_id, inputs.run_id, review["claim_id"]
    )


@pytest.mark.parametrize(
    "fault", ["missing", "source", "mode", "hash", "synthetic_receipt", "model", "prompt"]
)
def test_live_publication_rejects_unpinned_results(tmp_path, monkeypatch, fault):
    service, inputs, store = prepare(tmp_path, monkeypatch, fault)
    with pytest.raises(ReviewRejected, match="REVIEW_INPUT_MISMATCH"):
        service.publish(inputs)
    with store.jobs._transaction() as db:
        assert (
            db.execute("SELECT count(*) FROM job_records WHERE kind='review_head'").fetchone()[0]
            == 0
        )
