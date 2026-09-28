"""Local-synthetic-only SQLite audit storage for development and acceptance tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from proofops.domain.audit import (
    AuditConflict,
    AuditEvent,
    AuditHead,
    AuditIntegrityError,
    ChangeSet,
    new_audit_event,
    verify_audit_chain,
)
from proofops.domain.errors import DomainValidationError


def initialize_audit_schema(connection: sqlite3.Connection) -> None:
    """Create additive audit-prefixed schema inside the caller's transaction."""
    connection.execute("CREATE TABLE IF NOT EXISTS audit_schema (version INTEGER PRIMARY KEY)")
    versions = [row[0] for row in connection.execute("SELECT version FROM audit_schema")]
    if versions and versions != [1]:
        raise AuditIntegrityError("unsupported audit schema version")
    connection.execute("INSERT OR IGNORE INTO audit_schema VALUES (1)")
    connection.execute("""CREATE TABLE IF NOT EXISTS audit_heads (
        tenant_id TEXT NOT NULL, run_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        event_hash TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (tenant_id, run_id))""")
    connection.execute("""CREATE TABLE IF NOT EXISTS audit_events (
        tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, event_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence > 0),
        actor_sub TEXT NOT NULL, action TEXT NOT NULL, target_id TEXT NOT NULL,
        before_hash TEXT, after_hash TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision > 0),
        previous_event_hash TEXT, event_hash TEXT NOT NULL,
        timestamp TEXT NOT NULL, reason TEXT,
        PRIMARY KEY (tenant_id, run_id, sequence),
        UNIQUE (tenant_id, run_id, event_id))""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS audit_events_no_update
        BEFORE UPDATE ON audit_events BEGIN
        SELECT RAISE(ABORT, 'audit event is immutable'); END""")
    connection.execute("""CREATE TRIGGER IF NOT EXISTS audit_events_no_delete
        BEFORE DELETE ON audit_events BEGIN
        SELECT RAISE(ABORT, 'audit event is immutable'); END""")


def read_audit_head(connection: sqlite3.Connection, tenant_id: str, run_id: str) -> AuditHead:
    row = connection.execute(
        """SELECT sequence, event_hash FROM audit_heads
        WHERE tenant_id=? AND run_id=?""",
        (tenant_id, run_id),
    ).fetchone()
    return (
        AuditHead.empty(tenant_id, run_id)
        if row is None
        else AuditHead(tenant_id, run_id, row[0], row[1])
    )


def append_audit_transaction(
    *,
    connection: sqlite3.Connection,
    change: ChangeSet,
    expected_head: AuditHead,
    event_id: str,
    timestamp: str,
) -> tuple[AuditEvent, AuditHead]:
    """Append using the caller's open transaction; this function never commits."""
    if not connection.in_transaction:
        raise ValueError("active transaction required for atomic audit append")
    if read_audit_head(connection, change.tenant_id, change.run_id) != expected_head:
        raise AuditConflict("audit HEAD is stale; no event was appended")
    event, new_head = new_audit_event(change, expected_head, event_id=event_id, timestamp=timestamp)
    try:
        connection.execute(
            """INSERT INTO audit_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.tenant_id,
                event.run_id,
                event.event_id,
                event.sequence,
                event.actor_sub,
                event.action,
                event.target_id,
                event.before_hash,
                event.after_hash,
                event.revision,
                event.previous_event_hash,
                event.event_hash,
                event.timestamp,
                event.reason,
            ),
        )
        if expected_head.sequence == 0:
            connection.execute(
                "INSERT INTO audit_heads VALUES (?, ?, ?, ?, ?)",
                (
                    event.tenant_id,
                    event.run_id,
                    event.sequence,
                    event.event_hash,
                    event.timestamp,
                ),
            )
        else:
            updated = connection.execute(
                """UPDATE audit_heads SET sequence=?, event_hash=?, updated_at=?
                WHERE tenant_id=? AND run_id=? AND sequence=? AND event_hash=?""",
                (
                    event.sequence,
                    event.event_hash,
                    event.timestamp,
                    event.tenant_id,
                    event.run_id,
                    expected_head.sequence,
                    expected_head.event_hash,
                ),
            )
            if updated.rowcount != 1:
                raise AuditConflict("audit HEAD is stale; no event was appended")
    except sqlite3.IntegrityError:
        raise AuditConflict("audit append condition failed; no event was appended") from None
    return event, new_head


class LocalSQLiteAuditStore:
    """Durable local adapter; production distributed writes use DynamoDB."""

    kind = "local-synthetic-only"

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("durable audit requires a file-backed database")
        self.path = str(path)
        with self._transaction() as connection:
            initialize_audit_schema(connection)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # ponytail: one SQLite writer; use DynamoDB transactions for distributed writers.
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def append(
        self,
        change: ChangeSet,
        expected_head: AuditHead,
        *,
        event_id: str,
        timestamp: str,
    ) -> tuple[AuditEvent, AuditHead]:
        with self._transaction() as connection:
            return append_audit_transaction(
                connection=connection,
                change=change,
                expected_head=expected_head,
                event_id=event_id,
                timestamp=timestamp,
            )

    def get_head(self, tenant_id: str, run_id: str) -> AuditHead:
        with self._transaction() as connection:
            try:
                return read_audit_head(connection, tenant_id, run_id)
            except DomainValidationError as error:
                raise AuditIntegrityError("stored audit HEAD is invalid") from error

    def events(self, tenant_id: str, run_id: str) -> tuple[AuditEvent, ...]:
        with self._transaction() as connection:
            head = read_audit_head(connection, tenant_id, run_id)
            rows = connection.execute(
                """SELECT tenant_id, run_id, event_id, sequence, actor_sub, action,
                target_id, before_hash, after_hash, revision, previous_event_hash,
                event_hash, timestamp, reason FROM audit_events
                WHERE tenant_id=? AND run_id=? ORDER BY sequence""",
                (tenant_id, run_id),
            ).fetchall()
        try:
            events = tuple(AuditEvent(*row) for row in rows)
            verify_audit_chain(events, expected_head=head)
        except DomainValidationError as error:
            raise AuditIntegrityError("stored audit chain is invalid") from error
        return events
