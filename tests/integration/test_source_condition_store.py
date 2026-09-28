"""Source-review persistence only; synthetic inputs do not approve source facts."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from proofops.adapters.local.job_store import LocalSQLiteJobStore
from proofops.adapters.local.review_store import LocalSQLiteReviewStore
from proofops.application.authorization import AuthContext
from proofops.application.reviews import ReviewRejected

from tests.acceptance.test_citations import RUN
from tests.acceptance.test_parsing import TENANT, VERSION


def test_source_revisions_are_atomic_immutable_and_replayable(tmp_path, monkeypatch):
    jobs = LocalSQLiteJobStore(tmp_path / "state.sqlite")
    jobs.create_run(TENANT, RUN, VERSION)
    store = LocalSQLiteReviewStore(jobs)
    inputs = dict(tenant_id=TENANT, run_id=RUN, document_version_id=VERSION, source_sha256="a" * 64)
    initial = store.publish_source_conditions(inputs)
    assert store.publish_source_conditions(inputs) == initial
    with jobs._transaction() as db:
        assert db.execute(
            "SELECT action, after_hash FROM audit_events WHERE action='source_condition_published'"
        ).fetchall() == [("source_condition_published", initial["revision_sha256"])]
    actor = AuthContext("reviewer", TENANT, "reviewer", frozenset({"reviewer"}), "session")
    body = dict(
        base_source_revision=1,
        source_snapshot_sha256=initial["source_snapshot_sha256"],
        reason="Synthetic source annotation",
    )

    def submit(key):
        try:
            return store.resolve_source_conditions(
                actor,
                RUN,
                body,
                1,
                key,
                lambda state: {**state, "classifications": {"note-1": "unknown"}},
            )
        except ReviewRejected as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ["source-review-key-0001", "source-review-key-0002"]))
    winner = next(r for r in results if isinstance(r, dict))
    assert results.count(412) == 1 and winner["revision"] == 2
    key = "source-review-key-0001" if isinstance(results[0], dict) else "source-review-key-0002"
    assert submit(key) == winner
    assert store.source_conditions(TENANT, RUN, revision=1) == initial
    assert store.source_conditions(TENANT, RUN) == winner
    import proofops.adapters.local.review_store as module

    append = module.append_audit_transaction

    def fail_audit(*args, **kwargs):
        raise RuntimeError("storage failure")

    monkeypatch.setattr(module, "append_audit_transaction", fail_audit)
    changed = body | {"base_source_revision": 2}
    with pytest.raises(RuntimeError, match="storage failure"):
        store.resolve_source_conditions(
            actor, RUN, changed, 2, "source-review-key-0003", lambda state: state
        )
    assert store.source_conditions(TENANT, RUN) == winner
    monkeypatch.setattr(module, "append_audit_transaction", append)
    third = store.resolve_source_conditions(
        actor, RUN, changed, 2, "source-review-key-0003", lambda state: state
    )
    assert third["revision"] == 3 and submit(key) == winner
    assert store.publish_source_conditions(inputs) == third
    with pytest.raises(ReviewRejected) as conflict:
        store.resolve_source_conditions(
            actor, RUN, body | {"reason": "Changed request"}, 1, key, lambda state: state
        )
    assert conflict.value.status == 409
    with jobs._transaction() as db:
        for kind in ("numeric_check_receipt", "source_view_receipt"):
            jobs._put(db, TENANT, RUN, kind, "receipt-1", {"synthetic": True}, immutable=True)
            for statement in (
                f"UPDATE job_records SET value='{{}}' WHERE kind='{kind}'",
                f"DELETE FROM job_records WHERE kind='{kind}'",
                f"INSERT OR REPLACE INTO job_records SELECT * FROM job_records WHERE kind='{kind}'",
            ):
                with pytest.raises(sqlite3.IntegrityError):
                    db.execute(statement)
        for statement in (
            "UPDATE job_records SET value='{}' WHERE kind='source_condition_revision'",
            "DELETE FROM job_records WHERE kind='source_condition_revision'",
            "INSERT OR REPLACE INTO job_records SELECT * FROM job_records "
            "WHERE kind='source_condition_revision'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(statement)
    assert store.source_conditions(TENANT, RUN) == third
    with jobs._transaction() as db:
        jobs._put(db, TENANT, RUN, "source_condition_head", "HEAD", initial)
    for read in (
        lambda: store.source_conditions(TENANT, RUN),
        lambda: store.publish_source_conditions(inputs),
    ):
        with pytest.raises(ReviewRejected, match="SOURCE_REVIEW_INPUT_MISMATCH"):
            read()
    assert store.source_conditions(TENANT, RUN, revision=1) == initial
    with jobs._transaction() as db:
        db.execute("INSERT INTO source_condition_schema VALUES (2)")
    with pytest.raises(ReviewRejected, match="UNSUPPORTED_SOURCE_CONDITION_SCHEMA"):
        store.source_conditions(TENANT, RUN)


def test_source_write_boundary_rejects_malformed_inputs_and_foreign_access(tmp_path):
    jobs = LocalSQLiteJobStore(tmp_path / "state.sqlite")
    jobs.create_run(TENANT, RUN, VERSION)
    store = LocalSQLiteReviewStore(jobs)
    initial = store.publish_source_conditions(
        dict(tenant_id=TENANT, run_id=RUN, document_version_id=VERSION)
    )
    actor = AuthContext("reviewer", TENANT, "reviewer", frozenset({"reviewer"}), "session")
    body = dict(
        base_source_revision=1,
        source_snapshot_sha256=initial["source_snapshot_sha256"],
        reason="Synthetic annotation",
    )
    for invalid in (None, [], body | {"base_source_revision": True}, body | {"reason": ""}):
        with pytest.raises(ReviewRejected):
            store.resolve_source_conditions(
                actor, RUN, invalid, 1, "source-review-key-0001", lambda state: state
            )
    viewer = AuthContext("viewer", TENANT, "viewer", frozenset({"viewer"}), "session")
    foreign = AuthContext("other", VERSION, "reviewer", frozenset({"reviewer"}), "session")
    for unauthorized, status in ((viewer, 403), (foreign, 404)):
        with pytest.raises(ReviewRejected) as denied:
            store.resolve_source_conditions(
                unauthorized, RUN, body, 1, "source-review-key-0001", lambda state: state
            )
        assert denied.value.status == status
    assert store.source_conditions(TENANT, RUN) == initial
