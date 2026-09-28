"""AT-022: actual file-backed local audit durability; all identities are synthetic."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from proofops.adapters.aws.audit import append_audit_transaction as append_aws
from proofops.adapters.local.audit_store import (
    LocalSQLiteAuditStore,
    initialize_audit_schema,
)
from proofops.adapters.local.audit_store import (
    append_audit_transaction as append_local,
)
from proofops.domain.audit import AuditConflict, AuditHead, AuditIntegrityError, ChangeSet
from proofops.domain.provenance import canonical_hash

TENANT_A, TENANT_B, RUN, TARGET = (str(UUID(int=value)) for value in range(1, 5))
NOW = "2026-09-09T01:02:03Z"


def change(
    *,
    tenant_id: str = TENANT_A,
    before_hash: str | None = None,
    after_hash: str = "a" * 64,
    revision: int = 1,
    actor_sub: str = "synthetic-reviewer",
    reason: str = "synthetic reviewer correction",
) -> ChangeSet:
    return ChangeSet(
        tenant_id=tenant_id,
        run_id=RUN,
        actor_sub=actor_sub,
        action="review_resolved",
        target_id=TARGET,
        before_hash=before_hash,
        after_hash=after_hash,
        revision=revision,
        reason=reason,
    )


def append(store: LocalSQLiteAuditStore, changes: ChangeSet, head: AuditHead):
    return store.append(
        changes,
        head,
        event_id=str(UUID(int=100 + changes.revision)),
        timestamp=NOW,
    )


def test_reopen_preserves_export_snapshot_and_prior_event(tmp_path: Path) -> None:
    """Process-local state or mutable event aliases would lose/change revision one."""
    path = tmp_path / "state.sqlite3"
    store = LocalSQLiteAuditStore(path)
    current = {"claim_id": TARGET, "decision": {"grade": "E1", "revision": 1}}
    export_snapshot = canonical_hash(current)
    first_hash = canonical_hash(current["decision"])
    first, head = append(store, change(after_hash=first_hash), AuditHead.empty(TENANT_A, RUN))

    current["decision"] = {"grade": "E3", "revision": 2}
    second_hash = canonical_hash(current["decision"])
    reopened = LocalSQLiteAuditStore(path)
    append(
        reopened,
        change(before_hash=first_hash, after_hash=second_hash, revision=2),
        reopened.get_head(TENANT_A, RUN),
    )

    events = LocalSQLiteAuditStore(path).events(TENANT_A, RUN)
    assert export_snapshot == canonical_hash(
        {"claim_id": TARGET, "decision": {"grade": "E1", "revision": 1}}
    )
    assert events[0] == first
    assert [(event.sequence, event.revision) for event in events] == [(1, 1), (2, 2)]
    assert events[1].previous_event_hash == events[0].event_hash
    with pytest.raises(FrozenInstanceError):
        first.after_hash = second_hash  # type: ignore[misc]


def test_two_connections_racing_same_head_commit_one_actor(tmp_path: Path) -> None:
    """Without BEGIN IMMEDIATE plus HEAD CAS, both actors could claim sequence one."""
    path = tmp_path / "state.sqlite3"
    LocalSQLiteAuditStore(path)
    head = AuditHead.empty(TENANT_A, RUN)

    def race(revision: int) -> str:
        try:
            append(
                LocalSQLiteAuditStore(path),
                change(revision=revision, actor_sub=f"actor-{revision}"),
                head,
            )
            return f"actor-{revision}"
        except AuditConflict:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(race, (1, 2)))

    events = LocalSQLiteAuditStore(path).events(TENANT_A, RUN)
    assert sorted(results) in (["actor-1", "conflict"], ["actor-2", "conflict"])
    assert len(events) == 1
    assert events[0].actor_sub in {"actor-1", "actor-2"}


def test_connection_scoped_append_rolls_back_with_domain_mutation(tmp_path: Path) -> None:
    """Committing inside append would leave an event after its caller rolls back."""
    path = tmp_path / "state.sqlite3"
    store = LocalSQLiteAuditStore(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        append_local(
            connection=connection,
            change=change(),
            expected_head=AuditHead.empty(TENANT_A, RUN),
            event_id=str(UUID(int=101)),
            timestamp=NOW,
        )
        connection.rollback()
    finally:
        connection.close()

    assert store.events(TENANT_A, RUN) == ()
    assert store.get_head(TENANT_A, RUN) == AuditHead.empty(TENANT_A, RUN)


def test_connection_scoped_append_rejects_autocommit_before_writing(tmp_path: Path) -> None:
    """An autocommit connection could persist EVENT before a later HEAD failure."""
    path = tmp_path / "state.sqlite3"
    LocalSQLiteAuditStore(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        with pytest.raises(ValueError, match="active transaction"):
            append_local(
                connection=connection,
                change=change(),
                expected_head=AuditHead.empty(TENANT_A, RUN),
                event_id=str(UUID(int=101)),
                timestamp=NOW,
            )
    finally:
        connection.close()

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT count(*) FROM audit_events").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM audit_heads").fetchone() == (0,)


def test_tenants_are_isolated_and_storage_tamper_is_detected(tmp_path: Path) -> None:
    """Broad queries or unverified rows would leak tenants or accept altered history."""
    path = tmp_path / "state.sqlite3"
    store = LocalSQLiteAuditStore(path)
    append(store, change(), AuditHead.empty(TENANT_A, RUN))
    append(store, change(tenant_id=TENANT_B), AuditHead.empty(TENANT_B, RUN))
    assert len(store.events(TENANT_A, RUN)) == 1
    assert len(store.events(TENANT_B, RUN)) == 1
    assert store.events(str(UUID(int=99)), RUN) == ()

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE audit_events SET reason='tampered' WHERE tenant_id=? AND run_id=?",
                (TENANT_A, RUN),
            )
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.execute(
            "UPDATE audit_events SET reason='tampered' WHERE tenant_id=? AND run_id=?",
            (TENANT_A, RUN),
        )
    with pytest.raises(AuditIntegrityError):
        LocalSQLiteAuditStore(path).events(TENANT_A, RUN)


def test_schema_is_additive_reopenable_and_rejects_newer_version(tmp_path: Path) -> None:
    """Using global user_version or replacing tables would damage colocated components."""
    path = tmp_path / "state.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
        connection.execute("INSERT INTO unrelated VALUES ('preserved')")
        connection.execute("PRAGMA user_version=99")
    LocalSQLiteAuditStore(path)
    LocalSQLiteAuditStore(path)
    with sqlite3.connect(path) as row_connection:
        row_connection.row_factory = sqlite3.Row
        initialize_audit_schema(row_connection)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM unrelated").fetchone() == ("preserved",)
        assert connection.execute("PRAGMA user_version").fetchone() == (99,)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'audit_%'"
            )
        }
        assert names == {"audit_schema", "audit_heads", "audit_events"}
        assert {row[1] for row in connection.execute("PRAGMA table_info(audit_events)")} == {
            "tenant_id",
            "run_id",
            "event_id",
            "sequence",
            "actor_sub",
            "action",
            "target_id",
            "before_hash",
            "after_hash",
            "revision",
            "previous_event_hash",
            "event_hash",
            "timestamp",
            "reason",
        }
        connection.execute("UPDATE audit_schema SET version=2")
    with pytest.raises(AuditIntegrityError, match="schema version"):
        LocalSQLiteAuditStore(path)


class RecordingDynamoClient:
    """Explicit local request recorder; the durable behavior tests use SQLite above."""

    kind = "local-contract-test-only"

    def __init__(self) -> None:
        self.request: dict[str, Any] | None = None

    def transact_write_items(self, **request: Any) -> dict[str, object]:
        self.request = request
        return {}


def test_aws_request_keeps_event_put_and_head_cas_in_one_transaction() -> None:
    """Splitting event/HEAD writes or omitting revision would permit unaudited mutation."""
    client = RecordingDynamoClient()
    first, head = append_aws(
        client,
        table_name="synthetic-local-audit",
        change=change(),
        expected_head=AuditHead.empty(TENANT_A, RUN),
        event_id_factory=lambda: str(UUID(int=101)),
        clock=lambda: NOW,
    )
    assert head.sequence == 1
    assert client.request is not None
    writes = client.request["TransactItems"]
    assert len(writes) == 2
    assert writes[0]["Put"]["Item"]["revision"] == {"N": "1"}
    assert writes[0]["Put"]["Item"]["event_hash"] == {"S": first.event_hash}
    assert "ExpressionAttributeValues" not in writes[1]["Put"]
