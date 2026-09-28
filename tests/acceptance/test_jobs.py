"""AT-028: actual durable local transactions; all payloads/transports are synthetic."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from proofops.adapters.aws.jobs import LocalSQLiteJobStore
from proofops.adapters.local.audit_store import LocalSQLiteAuditStore
from proofops.application.ports.jobs import JobConflict, JobMessage, LeaseLost
from proofops.domain.audit import AuditConflict
from proofops_worker.consumer import claim_job, commit_job, consume_job
from proofops_worker.relay import relay_outbox

TENANT, OTHER, RUN, VERSION, JOB = (str(UUID(int=i)) for i in range(1, 6))
MESSAGE = JobMessage(TENANT, RUN, VERSION, JOB, "PARSE", "0", "a" * 64)


def seeded(path: Path) -> LocalSQLiteJobStore:
    store = LocalSQLiteJobStore(path)
    store.create_run(TENANT, RUN, VERSION)
    store.enqueue(MESSAGE, now=0)
    return store


def test_dead_worker_cannot_overwrite_restarted_worker_checkpoint(tmp_path):
    path = tmp_path / "jobs.sqlite"
    old_store = seeded(path)
    old = claim_job(old_store, MESSAGE, owner="dead", now=0, lease_seconds=10)
    assert old is not None
    assert claim_job(old_store, MESSAGE, owner="duplicate", now=9, lease_seconds=10) is None
    restarted = LocalSQLiteJobStore(path)
    new = claim_job(restarted, MESSAGE, owner="new", now=10, lease_seconds=10)
    assert new is not None and new.fencing_token > old.fencing_token
    next_message = replace(MESSAGE, job_id=str(UUID(int=6)), stage="TAG", shard="replica-1")
    assert commit_job(restarted, new, payload=b"new result", now=11, next_job=next_message)
    assert not commit_job(old_store, old, payload=b"late old result", now=12)
    assert restarted.read_checkpoint(MESSAGE) == b"new result"
    assert claim_job(restarted, MESSAGE, owner="duplicate", now=100, lease_seconds=10) is None
    assert len(restarted.pending_outbox(TENANT, RUN, now=100)) == 2
    assert restarted.get_job(MESSAGE)["attempt"] == 2


def test_expired_owner_input_owner_and_tenant_guards(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    lease = claim_job(store, MESSAGE, owner="worker", now=0, lease_seconds=10)
    assert lease is not None
    for forged in (
        replace(lease, owner="other"),
        replace(lease, fencing_token=99),
        replace(lease, message=replace(MESSAGE, input_hash="b" * 64)),
        replace(lease, message=replace(MESSAGE, tenant_id=OTHER)),
    ):
        assert not commit_job(store, forged, payload=b"forged", now=1)
    assert not commit_job(store, lease, payload=b"expired", now=10)
    assert store.read_checkpoint(MESSAGE) is None
    with pytest.raises(KeyError):
        store.get_job(replace(MESSAGE, tenant_id=OTHER))
    assert (
        claim_job(store, replace(MESSAGE, tenant_id=OTHER), owner="worker", now=0, lease_seconds=10)
        is None
    )
    with pytest.raises(KeyError):
        store.cancel_run(
            OTHER,
            RUN,
            expected_revision=1,
            idempotency_key="cancel-key-000001",
            reason="synthetic cancellation",
            actor_sub="synthetic-user",
            now=1,
        )


def test_two_connections_race_for_one_lease_and_heartbeat_fences(tmp_path):
    path = tmp_path / "jobs.sqlite"
    seeded(path)
    with ThreadPoolExecutor(max_workers=8) as workers:
        leases = list(
            workers.map(
                lambda i: claim_job(
                    LocalSQLiteJobStore(path), MESSAGE, owner=str(i), now=0, lease_seconds=10
                ),
                range(8),
            )
        )
    winners = [lease for lease in leases if lease is not None]
    assert len(winners) == 1
    store = LocalSQLiteJobStore(path)
    renewed = store.heartbeat(winners[0], now=9, lease_seconds=10)
    assert renewed.lease_until == 19
    assert claim_job(store, MESSAGE, owner="next", now=10, lease_seconds=10) is None
    with pytest.raises(LeaseLost):
        store.heartbeat(winners[0], now=19, lease_seconds=10)


def test_cancel_during_call_records_usage_but_never_publishes(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    calls = []

    def operation(lease):
        calls.append(lease)
        revision = store.get_run(TENANT, RUN)["revision"]
        store.cancel_run(
            TENANT,
            RUN,
            expected_revision=revision,
            idempotency_key="cancel-key-000001",
            reason="synthetic cancellation",
            actor_sub="synthetic-user",
            now=1,
        )
        return b"late response", {"input_tokens": 3, "output_tokens": 2}

    assert (
        consume_job(
            store, MESSAGE, owner="worker", clock=lambda: 1, lease_seconds=10, operation=operation
        )
        == "discarded"
    )
    assert len(calls) == 1
    assert store.read_checkpoint(MESSAGE) is None
    assert store.get_usage(MESSAGE, fencing_token=1) == {"input_tokens": 3, "output_tokens": 2}
    assert (
        consume_job(
            store, MESSAGE, owner="worker", clock=lambda: 2, lease_seconds=10, operation=operation
        )
        == "ignored"
    )
    assert len(calls) == 1
    assert store.pending_outbox(TENANT, RUN, now=10) == []


def test_cancel_retry_revision_idempotency_and_immutable_success(tmp_path):
    path = tmp_path / "jobs.sqlite"
    store = seeded(path)
    lease = claim_job(store, MESSAGE, owner="worker", now=0, lease_seconds=10)
    assert lease is not None
    assert commit_job(store, lease, payload=b"checkpoint", now=1)
    failed = replace(MESSAGE, job_id=str(UUID(int=6)), stage="TAG")
    store.enqueue(failed, now=2)
    lease = claim_job(store, failed, owner="worker", now=2, lease_seconds=10)
    assert lease is not None
    store.fail_job(lease, error_code="CITATION_INVALID", now=3)
    revision = store.get_run(TENANT, RUN)["revision"]
    with pytest.raises(JobConflict, match="revision"):
        store.retry_run(
            TENANT,
            RUN,
            expected_revision=revision - 1,
            idempotency_key="retry-key-000001",
            reason="retry",
            now=4,
            actor_sub="synthetic-user",
        )
    result = store.retry_run(
        TENANT,
        RUN,
        expected_revision=revision,
        idempotency_key="retry-key-000001",
        reason="retry",
        now=4,
        actor_sub="synthetic-user",
    )
    assert (
        store.retry_run(
            TENANT,
            RUN,
            expected_revision=revision,
            idempotency_key="retry-key-000001",
            reason="retry",
            now=5,
            actor_sub="synthetic-user",
        )
        == result
    )
    with pytest.raises(JobConflict, match="idempotency"):
        store.retry_run(
            TENANT,
            RUN,
            expected_revision=result["revision"],
            idempotency_key="retry-key-000001",
            reason="changed",
            now=6,
            actor_sub="synthetic-user",
        )
    assert store.read_checkpoint(MESSAGE) == b"checkpoint"
    assert store.get_job(MESSAGE)["attempt"] == 1
    assert store.get_job(failed)["status"] == "pending"
    assert not commit_job(store, lease, payload=b"late", now=6)
    event = LocalSQLiteAuditStore(path).events(TENANT, RUN)[0]
    action = store.get_action(TENANT, RUN, revision=result["revision"])
    assert (event.action, event.revision, event.actor_sub) == (
        "retry",
        result["revision"],
        "synthetic-user",
    )
    assert (event.before_hash, event.after_hash) == (
        action["before_hash"],
        action["after_hash"],
    )


def test_transient_retry_budget_and_nonretryable_error_preserved(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    for attempt in range(1, 4):
        lease = claim_job(store, MESSAGE, owner="worker", now=attempt * 10, lease_seconds=10)
        assert lease is not None
        state = store.fail_job(
            lease, error_code="MODEL_THROTTLED", now=attempt * 10 + 1, jitter=0.5
        )
        assert state["status"] == ("pending" if attempt < 3 else "failed")
        if attempt < 3:
            assert (
                claim_job(store, MESSAGE, owner="early", now=attempt * 10 + 1, lease_seconds=10)
                is None
            )
    assert store.get_job(MESSAGE)["attempt"] == 3
    assert store.get_job(MESSAGE)["error_code"] == "MODEL_THROTTLED"
    assert store.get_run(TENANT, RUN)["status"] == "failed"
    assert store.read_checkpoint(MESSAGE) is None


def test_atomic_commit_rejects_cross_tenant_next_job_without_checkpoint(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    lease = claim_job(store, MESSAGE, owner="worker", now=0, lease_seconds=10)
    assert lease is not None
    bad_next = replace(MESSAGE, tenant_id=OTHER, job_id=str(UUID(int=6)), stage="TAG")
    with pytest.raises(ValueError):
        commit_job(store, lease, payload=b"must rollback", now=1, next_job=bad_next)
    assert store.read_checkpoint(MESSAGE) is None
    assert store.get_job(MESSAGE)["status"] == "leased"
    assert len(store.pending_outbox(TENANT, RUN, now=2)) == 1


def test_outbox_redelivery_after_accept_then_transport_error(tmp_path):
    path = tmp_path / "jobs.sqlite"
    store = seeded(path)
    delivery = tmp_path / "synthetic-queue.txt"

    def publish(event):
        with delivery.open("a") as stream:
            stream.write(event["event_id"] + "\n")
        if len(delivery.read_text().splitlines()) == 1:
            raise OSError("synthetic failure after queue accepted message")

    assert relay_outbox(store, TENANT, RUN, now=0, publish=publish) == 0
    restarted = LocalSQLiteJobStore(path)
    assert relay_outbox(restarted, TENANT, RUN, now=100, publish=publish) == 1
    assert relay_outbox(restarted, TENANT, RUN, now=200, publish=publish) == 0
    events = delivery.read_text().splitlines()
    assert len(events) == 2 and events[0] == events[1]
    calls = []
    for _ in events:
        consume_job(
            restarted,
            MESSAGE,
            owner="worker",
            clock=lambda: 100,
            lease_seconds=10,
            operation=lambda lease: (calls.append(lease) or b"ok", {}),
        )
    assert len(calls) == 1
    assert restarted.read_checkpoint(MESSAGE) == b"ok"


@pytest.mark.parametrize(
    "changes", [{"tenant_id": "bad"}, {"input_hash": "bad"}, {"stage": "../PARSE"}, {"shard": ""}]
)
def test_message_validates_untrusted_queue_identity(changes):
    with pytest.raises(ValueError):
        replace(MESSAGE, **changes)


def test_transaction_rolls_back_checkpoint_when_next_stage_conflicts(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    next_job = replace(MESSAGE, job_id=str(UUID(int=6)), stage="TAG")
    store.enqueue(next_job, now=0)
    lease = claim_job(store, MESSAGE, owner="worker", now=0, lease_seconds=10)
    assert lease is not None
    with pytest.raises(JobConflict):
        commit_job(
            store,
            lease,
            payload=b"rollback",
            now=1,
            next_job=replace(next_job, input_hash="b" * 64),
        )
    assert store.read_checkpoint(MESSAGE) is None
    assert store.get_job(MESSAGE)["status"] == "leased"
    assert len(store.pending_outbox(TENANT, RUN, now=2)) == 2
    assert commit_job(store, lease, payload=b"safe", now=2, next_job=next_job)


def test_separate_process_commits_and_reopen_verifies_actual_bytes(tmp_path):
    import subprocess
    import sys

    path = tmp_path / "jobs.sqlite"
    store = seeded(path)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from proofops.adapters.local.job_store import LocalSQLiteJobStore
from proofops.application.ports.jobs import JobMessage
store = LocalSQLiteJobStore(sys.argv[1])
message = JobMessage(*sys.argv[2:])
lease = store.claim_job(message, owner='process', now=0, lease_seconds=10)
assert lease is not None
assert store.commit_job(lease, payload=b'persisted from process', now=1)
""",
            str(path),
            TENANT,
            RUN,
            VERSION,
            JOB,
            "PARSE",
            "0",
            "a" * 64,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert store.read_checkpoint(MESSAGE) == b"persisted from process"


def test_consumer_classified_failure_usage_and_delivery_limit(tmp_path):
    from proofops_worker.consumer import StageFailure

    store = seeded(tmp_path / "jobs.sqlite")
    calls = []

    def fail(lease):
        calls.append(lease)
        raise StageFailure("PROVIDER_5XX", usage={"input_tokens": 7})

    assert (
        consume_job(
            store,
            MESSAGE,
            owner="worker",
            clock=lambda: 1,
            lease_seconds=10,
            operation=fail,
            jitter=lambda: 0.5,
        )
        == "retry"
    )
    assert store.get_usage(MESSAGE, fencing_token=1) == {"input_tokens": 7}
    assert (
        consume_job(
            store,
            MESSAGE,
            owner="worker",
            clock=lambda: 10,
            lease_seconds=10,
            operation=fail,
            receive_count=6,
        )
        == "failed"
    )
    assert len(calls) == 1
    assert store.get_job(MESSAGE)["error_code"] == "SQS_RECEIVE_LIMIT"
    assert store.get_run(TENANT, RUN)["status"] == "failed"


def test_cancel_is_idempotent_and_cannot_be_resumed_by_retry(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    args = dict(expected_revision=1, idempotency_key="cancel-key-000001", reason="cancel")
    cancelled = store.cancel_run(TENANT, RUN, **args, actor_sub="synthetic-user", now=1)
    assert store.cancel_run(TENANT, RUN, **args, actor_sub="synthetic-user", now=1) == cancelled
    with pytest.raises(JobConflict):
        store.retry_run(
            TENANT,
            RUN,
            expected_revision=cancelled["revision"],
            idempotency_key="retry-key-000001",
            reason="retry",
            now=10,
            actor_sub="synthetic-user",
        )
    assert store.get_job(MESSAGE)["status"] == "cancelled"


def test_checkpoint_and_usage_corruption_never_silently_replaced(tmp_path):
    import sqlite3

    path = tmp_path / "jobs.sqlite"
    store = seeded(path)
    lease = claim_job(store, MESSAGE, owner="worker", now=0, lease_seconds=10)
    assert lease is not None
    store.record_usage(lease, {"input_tokens": 1})
    store.record_usage(lease, {"input_tokens": 1})
    with pytest.raises(JobConflict):
        store.record_usage(lease, {"input_tokens": 2})
    assert commit_job(store, lease, payload=b"verified", now=1)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE job_records SET value=? WHERE kind='artifact'", (b"bad",))
    with pytest.raises(JobConflict, match="corruption"):
        store.read_checkpoint(MESSAGE)


def test_busy_delivery_is_deferred_until_lease_can_be_recovered(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    lease = claim_job(store, MESSAGE, owner="dead", now=0, lease_seconds=10)
    assert lease is not None
    calls = []

    def operation(current):
        calls.append(current)
        return b"recovered", {}

    assert (
        consume_job(
            store,
            MESSAGE,
            owner="duplicate",
            clock=lambda: 1,
            lease_seconds=10,
            operation=operation,
        )
        == "deferred"
    )
    assert calls == []
    assert (
        consume_job(
            store, MESSAGE, owner="new", clock=lambda: 10, lease_seconds=10, operation=operation
        )
        == "committed"
    )
    assert len(calls) == 1


def test_same_ids_in_two_tenants_have_separate_checkpoints(tmp_path):
    store = seeded(tmp_path / "jobs.sqlite")
    store.create_run(OTHER, RUN, VERSION)
    other_message = replace(MESSAGE, tenant_id=OTHER)
    store.enqueue(other_message, now=0)
    for message, payload in ((MESSAGE, b"tenant one"), (other_message, b"tenant two")):
        lease = claim_job(store, message, owner="worker", now=0, lease_seconds=10)
        assert lease is not None
        assert commit_job(store, lease, payload=payload, now=1)
    assert store.read_checkpoint(MESSAGE) == b"tenant one"
    assert store.read_checkpoint(other_message) == b"tenant two"


def test_schema_reopen_preserves_other_components_and_rejects_newer_version(tmp_path):
    import sqlite3

    path = tmp_path / "jobs.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserved')")
        db.execute("PRAGMA user_version=99")
    seeded(path)
    LocalSQLiteJobStore(path)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM unrelated").fetchone() == ("preserved",)
        assert db.execute("PRAGMA user_version").fetchone() == (99,)
        db.execute("UPDATE job_schema SET version=2")
    with pytest.raises(JobConflict, match="schema version"):
        LocalSQLiteJobStore(path)


def test_action_actor_time_before_after_and_idempotency_expiry_survive_reopen(tmp_path):
    from proofops.domain.provenance import canonical_hash

    path = tmp_path / "jobs.sqlite"
    store = seeded(path)
    args = dict(expected_revision=1, idempotency_key="cancel-key-000001", reason="cancel")
    cancelled = store.cancel_run(TENANT, RUN, actor_sub="reviewer-one", now=100, **args)
    reopened = LocalSQLiteJobStore(path)
    action = reopened.get_action(TENANT, RUN, revision=cancelled["revision"])
    assert action["actor_sub"] == "reviewer-one"
    assert action["timestamp"] == "1970-01-01T00:01:40Z"
    assert action["before"]["run"]["status"] == "queued"
    assert action["after"]["run"]["status"] == "cancelled"
    assert action["before_hash"] == canonical_hash(action["before"])
    assert action["after_hash"] == canonical_hash(action["after"])
    assert (
        reopened.cancel_run(TENANT, RUN, actor_sub="reviewer-two", now=86499, **args) == cancelled
    )
    with pytest.raises(JobConflict, match="revision"):
        reopened.cancel_run(TENANT, RUN, actor_sub="reviewer-two", now=86500, **args)
    later = reopened.cancel_run(
        TENANT,
        RUN,
        actor_sub="reviewer-two",
        now=86500,
        expected_revision=cancelled["revision"],
        idempotency_key=args["idempotency_key"],
        reason="new request after expiry",
    )
    assert later["revision"] == cancelled["revision"] + 1
    assert reopened.get_action(TENANT, RUN, revision=cancelled["revision"]) == action
    assert (
        reopened.get_action(TENANT, RUN, revision=later["revision"])["actor_sub"] == "reviewer-two"
    )
    events = LocalSQLiteAuditStore(path).events(TENANT, RUN)
    assert [(event.sequence, event.revision, event.actor_sub) for event in events] == [
        (1, cancelled["revision"], "reviewer-one"),
        (2, later["revision"], "reviewer-two"),
    ]
    assert events[1].previous_event_hash == events[0].event_hash


def test_audit_failure_rolls_back_cancel_action_and_idempotency(tmp_path):
    """A failed canonical append must leave no unaudited run mutation or action envelope."""
    path = tmp_path / "jobs.sqlite"
    store = seeded(path)
    with sqlite3.connect(path) as connection:
        connection.execute(
            """CREATE TRIGGER synthetic_audit_insert_failure
            BEFORE INSERT ON audit_events BEGIN
            SELECT RAISE(ABORT, 'synthetic audit failure'); END"""
        )
    args = dict(
        expected_revision=1,
        idempotency_key="cancel-key-000001",
        reason="cancel",
        actor_sub="synthetic-user",
        now=1,
    )
    with pytest.raises(AuditConflict):
        store.cancel_run(TENANT, RUN, **args)
    assert store.get_run(TENANT, RUN)["status"] == "queued"
    assert store.get_job(MESSAGE)["status"] == "pending"
    with pytest.raises(KeyError):
        store.get_action(TENANT, RUN, revision=2)
    assert LocalSQLiteAuditStore(path).events(TENANT, RUN) == ()

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER synthetic_audit_insert_failure")
    assert store.cancel_run(TENANT, RUN, **args)["status"] == "cancelled"
