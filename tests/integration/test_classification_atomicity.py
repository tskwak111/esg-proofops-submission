"""Failed enqueue must leave classification, plan and idempotency state untouched."""

import json

import pytest
from proofops_api.routers.classification import ClassificationView

from tests.integration.test_manual_classification_reprocess import _actor, _blocked_setup, _body


def test_enqueue_failure_rolls_back_classification(tmp_path, monkeypatch):
    _, run_id, _, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(store, run_id, claim_id, actor)
    with store.jobs._transaction() as db:
        before = db.execute(
            "SELECT * FROM job_records ORDER BY tenant_id,run_id,kind,record_id"
        ).fetchall()

    def failed_enqueue(*args, **kwargs):
        raise RuntimeError("injected enqueue failure")

    monkeypatch.setattr(store.jobs, "enqueue_transaction", failed_enqueue)
    with pytest.raises(RuntimeError, match="injected enqueue failure"):
        store.record_and_enqueue(
            actor,
            run_id,
            claim_id,
            body,
            '"' + view["etag"].strip('"') + '"',
            "atomic-classification-failure",
            origin="human_classification",
            classified_by=actor.user_sub,
            delegation_authority=None,
            now=int(now[0]),
        )
    with store.jobs._transaction() as db:
        after = db.execute(
            "SELECT * FROM job_records ORDER BY tenant_id,run_id,kind,record_id"
        ).fetchall()
    assert after == before


def test_real_projection_matches_http_source_contract(tmp_path, monkeypatch):
    _, run_id, _, _, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    view = store.view(_actor(), run_id, claim_id)
    ClassificationView.model_validate_json(json.dumps(view))
    assert view["etag"].startswith('"') and view["etag"].endswith('"')
    assert view["sources"][0]["quote"] == view["sources"][0]["source_ref"]["quote"]


def test_lineage_change_during_tagging_cannot_publish(tmp_path, monkeypatch):
    _, run_id, runner, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(store, run_id, claim_id, actor)
    store.record_and_enqueue(
        actor,
        run_id,
        claim_id,
        body,
        view["etag"],
        "classification-fenced-publish",
        origin="human_classification",
        classified_by=actor.user_sub,
        delegation_authority=None,
        now=int(now[0]),
    )
    commit = store.jobs.commit_job

    def concurrent_change(lease, **kwargs):
        with store.jobs._transaction() as db:
            run = store.jobs._get(db, actor.tenant_id, run_id, "run", "META")
            run["tag_snapshot_sha256"] = "f" * 64
            store.jobs._put(db, actor.tenant_id, run_id, "run", "META", run)
        return commit(lease, **kwargs)

    monkeypatch.setattr(store.jobs, "commit_job", concurrent_change)
    try:
        runner.run_once(tenant_id=actor.tenant_id, run_id=run_id)
    except ValueError:
        pass  # Explicitly rejected before publication; no fabricated revision.
    assert runner.claims.current_tag(actor.tenant_id, run_id, claim_id) is None


def test_reprocess_restart_keeps_immutable_request_allowance(tmp_path, monkeypatch):
    import sqlite3
    from pathlib import Path
    from uuid import uuid4

    from proofops.application.ports.jobs import JobMessage
    from proofops_worker.tag_reprocess import ReprocessRejected, TagReprocess

    _, run_id, runner, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(store, run_id, claim_id, actor)
    accepted = store.record_and_enqueue(
        actor,
        run_id,
        claim_id,
        body,
        view["etag"],
        "classification-restart-bound",
        origin="human_classification",
        classified_by=actor.user_sub,
        delegation_authority=None,
        now=int(now[0]),
    )
    event = next(
        e
        for e in store.jobs.pending_outbox(actor.tenant_id, run_id, now=int(now[0]))
        if e["message"]["job_id"] == accepted["reprocess_job"]["job_id"]
    )
    message = JobMessage(**event["message"])
    first = TagReprocess.load(store.store, runner.tags, message)
    assert first.resume.remaining == 6
    receipts = Path(first.plan.receipts_root) / "elements"
    (receipts / str(uuid4())).mkdir(parents=True)
    replay = TagReprocess.load(store.store, runner.tags, message)
    assert replay.resume.remaining == 5 and replay.can_attempt_claim()
    for _ in range(5):
        (receipts / str(uuid4())).mkdir()
    with pytest.raises(ReprocessRejected, match="ALLOWANCE_SPENT"):
        TagReprocess.load(store.store, runner.tags, message)
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with store.jobs._transaction() as db:
            db.execute("UPDATE job_records SET value=value WHERE kind='tag_reprocess'")
