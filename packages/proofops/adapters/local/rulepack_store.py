"""Durable local RulePack store for TASK-025 HTTP integration.

SQLite schema version 1 mirrors the DynamoDB contract locally: immutable
RulePack revisions, CAS heads, a per-tenant/mode active pointer, frozen run
snapshots, and tenant-scoped idempotency records are committed together.
Production remains fail-closed until the AWS adapter task supplies DynamoDB.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from proofops.adapters.local.catalog_pages import initialize as initialize_catalog_pages
from proofops.adapters.local.catalog_pages import page as catalog_page
from proofops.application.rulepacks import (
    RulePackRecord,
    RulePackRegistry,
    RunSnapshot,
    activate_rulepack,
)
from proofops.domain.rulepacks import canonical_json

_ROUTE = "POST /v1/rule-packs/{rule_pack_id}/activate"


class RulePackNotFound(LookupError):
    pass


class IdempotencyConflict(ValueError):
    pass


class StaleRulePackRevision(ValueError):
    def __init__(self, current_revision: int) -> None:
        super().__init__(f"stale rule-pack revision; current revision is {current_revision}")
        self.current_revision = current_revision


@dataclass(frozen=True, slots=True)
class StoredActivation:
    body: dict[str, Any]
    revision: int


class RulePackSqliteStore:
    """Local-only transactional store; one SQLite file is shared by API replicas."""

    def __init__(
        self,
        path: str | Path,
        *,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.path = str(path)
        self._id_factory = id_factory or (lambda: str(uuid4()))
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS rulepack_schema_metadata (
                    component TEXT PRIMARY KEY,
                    version INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS rulepack_revisions (
                    tenant_id TEXT NOT NULL,
                    rule_pack_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    record_json TEXT NOT NULL,
                    files_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, rule_pack_id, revision)
                );
                CREATE TABLE IF NOT EXISTS rulepack_heads (
                    tenant_id TEXT NOT NULL,
                    rule_pack_id TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    PRIMARY KEY (tenant_id, rule_pack_id)
                );
                CREATE TABLE IF NOT EXISTS rulepack_active_pointers (
                    tenant_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    rule_pack_id TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, mode)
                );
                CREATE TABLE IF NOT EXISTS rulepack_run_snapshots (
                    tenant_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, run_id)
                );
                CREATE TABLE IF NOT EXISTS rulepack_idempotency_records (
                    tenant_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    response_json TEXT NOT NULL,
                    response_revision INTEGER NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (tenant_id, route, key_hash)
                );
                CREATE TABLE IF NOT EXISTS rulepack_activation_events (
                    tenant_id TEXT NOT NULL,
                    activation_id TEXT NOT NULL,
                    rule_pack_id TEXT NOT NULL,
                    actor_sub TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    before_pack_id TEXT,
                    after_pack_id TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    PRIMARY KEY (tenant_id, activation_id)
                );
                INSERT OR IGNORE INTO rulepack_schema_metadata VALUES ('rulepacks', 1);
                """
            )
            connection.execute("BEGIN IMMEDIATE")
            initialize_catalog_pages(connection)
            version = connection.execute(
                "SELECT version FROM rulepack_schema_metadata WHERE component='rulepacks'"
            ).fetchone()["version"]
            if version != 1:
                raise RuntimeError(f"unsupported local rulepack schema version: {version}")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            # ponytail: SQLite serializes local writes; AWS replicas use DynamoDB transactions.
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def add_pack(self, pack: RulePackRecord, files_content: Mapping[str, Any]) -> int:
        record_json = canonical_json(pack.to_dict())
        files_json = canonical_json(files_content)
        with self._transaction() as connection:
            row = connection.execute(
                """SELECT h.revision, r.record_json, r.files_json
                   FROM rulepack_heads h
                   JOIN rulepack_revisions r USING (tenant_id, rule_pack_id, revision)
                   WHERE h.tenant_id = ? AND h.rule_pack_id = ?""",
                (pack.tenant_id, pack.rule_pack_id),
            ).fetchone()
            if row is not None:
                if row["record_json"] == record_json and row["files_json"] == files_json:
                    return int(row["revision"])
                raise ValueError("immutable rule-pack identity cannot be overwritten")
            connection.execute(
                "INSERT INTO rulepack_revisions VALUES (?, ?, 1, ?, ?)",
                (pack.tenant_id, pack.rule_pack_id, record_json, files_json),
            )
            connection.execute(
                "INSERT INTO rulepack_heads VALUES (?, ?, 1)",
                (pack.tenant_id, pack.rule_pack_id),
            )
        return 1

    def add_run_snapshot(self, snapshot: RunSnapshot) -> None:
        with self._transaction() as connection:
            self.add_run_snapshot_transaction(connection, snapshot)

    def add_run_snapshot_transaction(self, connection, snapshot: RunSnapshot) -> None:
        """Join a caller-owned run transaction without committing it."""
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        payload = canonical_json(
            {
                "run_id": snapshot.run_id,
                "tenant_id": snapshot.tenant_id,
                "rule_pack_id": snapshot.rule_pack_id,
                "rule_pack_sha256": snapshot.rule_pack_sha256,
                "status": snapshot.status,
            }
        )
        try:
            connection.execute(
                "INSERT INTO rulepack_run_snapshots VALUES (?, ?, ?)",
                (snapshot.tenant_id, snapshot.run_id, payload),
            )
        except sqlite3.IntegrityError:
            row = connection.execute(
                """SELECT snapshot_json FROM rulepack_run_snapshots
                   WHERE tenant_id=? AND run_id=?""",
                (snapshot.tenant_id, snapshot.run_id),
            ).fetchone()
            if row is None or row[0] != payload:
                raise ValueError("immutable run snapshot cannot be overwritten") from None

    def get_run_snapshot(self, tenant_id: str, run_id: str) -> RunSnapshot:
        with self._read() as connection:
            row = connection.execute(
                "SELECT snapshot_json FROM rulepack_run_snapshots WHERE tenant_id=? AND run_id=?",
                (tenant_id, run_id),
            ).fetchone()
        if row is None:
            raise LookupError("run snapshot not found")
        return RunSnapshot(**json.loads(row["snapshot_json"]))

    def active_pack_id(self, tenant_id: str, mode: str) -> str | None:
        with self._read() as connection:
            row = connection.execute(
                "SELECT rule_pack_id FROM rulepack_active_pointers WHERE tenant_id=? AND mode=?",
                (tenant_id, mode),
            ).fetchone()
        return None if row is None else str(row["rule_pack_id"])

    def get_pack_with_files(
        self, tenant_id: str, rule_pack_id: str
    ) -> tuple[RulePackRecord, dict[str, Any]]:
        """Read-only lookup of the exact stored record + files for one pack id.

        Used by the local coordinator review CLI to fetch a candidate pack
        by id without duplicating `_load_registry`'s tenant-wide scan and
        without ever mutating the row in place (packs are immutable by
        (tenant_id, rule_pack_id); promotion requires a new pack id).
        """
        with self._read() as connection:
            row = connection.execute(
                """SELECT r.record_json, r.files_json
                   FROM rulepack_heads h
                   JOIN rulepack_revisions r USING (tenant_id, rule_pack_id, revision)
                   WHERE h.tenant_id=? AND h.rule_pack_id=?""",
                (tenant_id, rule_pack_id),
            ).fetchone()
        if row is None:
            raise RulePackNotFound("rule pack not found")
        record = RulePackRecord.from_dict(json.loads(row["record_json"]))
        files = json.loads(row["files_json"])
        return record, files

    def extraction_snapshot_transaction(self, connection, tenant_id, mode, rule_pack_id):
        """Pin validated content for extraction only; do not activate or approve rules."""
        from proofops.application.rulepacks import validate_rulepack
        from proofops.domain.rulepacks import snapshot_from_validated

        if not connection.in_transaction or mode != "disclosure":
            raise ValueError("extraction reference requires local disclosure transaction")
        row = connection.execute(
            "SELECT r.record_json, r.files_json FROM rulepack_heads h "
            "JOIN rulepack_revisions r USING (tenant_id, rule_pack_id, revision) "
            "WHERE h.tenant_id=? AND h.rule_pack_id=?",
            (tenant_id, rule_pack_id),
        ).fetchone()
        if row is None:
            raise RulePackNotFound("rule pack not found")
        record, files = json.loads(row[0]), json.loads(row[1])
        if record["mode"] != mode or record["status"] not in {"draft", "validated", "active"}:
            raise ValueError("rule pack reference unavailable")
        result = validate_rulepack(record, files, tuple(f"GAP-{i:03d}" for i in range(1, 11)))
        if not result.ok:
            raise ValueError("rule pack reference validation failed")
        return snapshot_from_validated(record, files)

    def active_snapshot_transaction(self, connection, tenant_id, mode, rule_pack_id):
        """Read exact active head and retained content while creation holds its write lock."""
        from proofops.application.rulepacks import validate_rulepack
        from proofops.domain.rulepacks import snapshot_from_validated

        if not connection.in_transaction:
            raise ValueError("active transaction required")
        row = connection.execute(
            """SELECT r.record_json, r.files_json
               FROM rulepack_active_pointers a
               JOIN rulepack_heads h USING (tenant_id, rule_pack_id)
               JOIN rulepack_revisions r USING (tenant_id, rule_pack_id, revision)
               WHERE a.tenant_id=? AND a.mode=? AND a.rule_pack_id=?""",
            (tenant_id, mode, rule_pack_id),
        ).fetchone()
        if row is None:
            raise RulePackNotFound("active rule pack not found")
        record, files = json.loads(row[0]), json.loads(row[1])
        result = validate_rulepack(
            record, files_content=files, gap_ids=tuple(f"GAP-{i:03d}" for i in range(1, 11))
        )
        if not result.ok:
            raise ValueError("active rule pack validation failed")
        return snapshot_from_validated(record, files)

    def list_active_packs(self, tenant_id: str) -> tuple[RulePackRecord, ...]:
        """Read current active records from the durable pointer, tenant-scoped."""
        with self._read() as connection:
            rows = connection.execute(
                """SELECT r.record_json
                   FROM rulepack_active_pointers a
                   JOIN rulepack_heads h USING (tenant_id, rule_pack_id)
                   JOIN rulepack_revisions r USING (tenant_id, rule_pack_id, revision)
                   WHERE a.tenant_id=?
                   ORDER BY a.mode, a.rule_pack_id""",
                (tenant_id,),
            ).fetchall()
        return tuple(RulePackRecord.from_dict(json.loads(row["record_json"])) for row in rows)

    def list(self, tenant_id: str, *, cursor: str | None, limit: int, now: float) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        with self._transaction() as connection:
            # ponytail: v1 RulePack has no created_at; first immutable rowid is the
            # local creation order until the fixed contract adds a timestamp.
            return catalog_page(
                connection,
                tenant_id=tenant_id,
                endpoint="rule-packs",
                query={"limit": limit},
                cursor=cursor,
                limit=limit,
                now=now,
                load_items=lambda: (
                    _project_rule_pack(RulePackRecord.from_dict(json.loads(row[0])))
                    for row in connection.execute(
                        """SELECT r.record_json, MIN(history.rowid) AS created_order
                        FROM rulepack_heads h
                        JOIN rulepack_revisions r
                        USING (tenant_id,rule_pack_id,revision)
                        JOIN rulepack_revisions history USING (tenant_id,rule_pack_id)
                        WHERE h.tenant_id=?
                        GROUP BY h.rule_pack_id
                        ORDER BY created_order, h.rule_pack_id""",
                        (tenant_id,),
                    )
                ),
            )

    def activate(
        self,
        *,
        tenant_id: str,
        rule_pack_id: str,
        expected_revision: int,
        idempotency_key: str,
        actor: str,
        reason: str,
        gap_ids: Collection[str],
        now: float,
    ) -> StoredActivation:
        request_hash = hashlib.sha256(
            canonical_json({"rule_pack_id": rule_pack_id, "reason": reason}).encode("ascii")
        ).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()

        with self._transaction() as connection:
            replay = connection.execute(
                """SELECT request_hash, response_json, response_revision, expires_at
                   FROM rulepack_idempotency_records
                   WHERE tenant_id=? AND route=? AND key_hash=?""",
                (tenant_id, _ROUTE, key_hash),
            ).fetchone()
            if replay is not None:
                if float(replay["expires_at"]) > now:
                    if replay["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used for another request"
                        )
                    return StoredActivation(
                        body=json.loads(replay["response_json"]),
                        revision=int(replay["response_revision"]),
                    )
                connection.execute(
                    """DELETE FROM rulepack_idempotency_records
                       WHERE tenant_id=? AND route=? AND key_hash=?""",
                    (tenant_id, _ROUTE, key_hash),
                )

            head = connection.execute(
                "SELECT revision FROM rulepack_heads WHERE tenant_id=? AND rule_pack_id=?",
                (tenant_id, rule_pack_id),
            ).fetchone()
            if head is None:
                raise RulePackNotFound("rule pack not found")
            current_revision = int(head["revision"])
            if current_revision != expected_revision:
                raise StaleRulePackRevision(current_revision)

            registry, files_by_id, revisions = self._load_registry(connection, tenant_id)
            try:
                updated, record = activate_rulepack(
                    registry,
                    rule_pack_id,
                    tenant_id,
                    actor=actor,
                    reason=reason,
                    files_content=files_by_id[rule_pack_id],
                    gap_ids=gap_ids,
                )
            except LookupError as exc:
                raise RulePackNotFound("rule pack not found") from exc

            return self._commit_activation(
                connection,
                tenant_id=tenant_id,
                rule_pack_id=rule_pack_id,
                registry=registry,
                updated=updated,
                record=record,
                files_by_id=files_by_id,
                revisions=revisions,
                current_revision=current_revision,
                key_hash=key_hash,
                request_hash=request_hash,
                now=now,
            )

    def record_ai_delegated_review(
        self,
        *,
        tenant_id: str,
        rule_pack_id: str,
        expected_revision: int,
        idempotency_key: str,
        reviewer: str,
        reviewed_at: str,
        source_authority: str,
        note: str,
        gap_ids: Collection[str],
        now: float,
    ) -> StoredActivation:
        """Store-level path for AT-R07: coordinator/admin-boundary AI-delegated

        review that activates a real (non-synthetic) run default without a
        human `approved_by`. Mirrors `activate()`'s CAS/idempotency/append-
        only structure exactly; the only different step is which application
        function computes the new registry state.
        """
        from proofops.application.rulepacks import record_ai_delegated_review as _record

        route = _ROUTE + "#ai-delegated-review"
        request_hash = hashlib.sha256(
            canonical_json(
                {
                    "rule_pack_id": rule_pack_id,
                    "reviewer": reviewer,
                    "reviewed_at": reviewed_at,
                    "source_authority": source_authority,
                    "note": note,
                }
            ).encode("ascii")
        ).hexdigest()
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()

        with self._transaction() as connection:
            replay = connection.execute(
                """SELECT request_hash, response_json, response_revision, expires_at
                   FROM rulepack_idempotency_records
                   WHERE tenant_id=? AND route=? AND key_hash=?""",
                (tenant_id, route, key_hash),
            ).fetchone()
            if replay is not None:
                if float(replay["expires_at"]) > now:
                    if replay["request_hash"] != request_hash:
                        raise IdempotencyConflict(
                            "idempotency key was already used for another request"
                        )
                    return StoredActivation(
                        body=json.loads(replay["response_json"]),
                        revision=int(replay["response_revision"]),
                    )
                connection.execute(
                    """DELETE FROM rulepack_idempotency_records
                       WHERE tenant_id=? AND route=? AND key_hash=?""",
                    (tenant_id, route, key_hash),
                )

            head = connection.execute(
                "SELECT revision FROM rulepack_heads WHERE tenant_id=? AND rule_pack_id=?",
                (tenant_id, rule_pack_id),
            ).fetchone()
            if head is None:
                raise RulePackNotFound("rule pack not found")
            current_revision = int(head["revision"])
            if current_revision != expected_revision:
                raise StaleRulePackRevision(current_revision)

            registry, files_by_id, revisions = self._load_registry(connection, tenant_id)
            try:
                updated, record, provenance = _record(
                    registry,
                    rule_pack_id,
                    tenant_id,
                    reviewer=reviewer,
                    reviewed_at=reviewed_at,
                    source_authority=source_authority,
                    note=note,
                    files_content=files_by_id[rule_pack_id],
                    gap_ids=gap_ids,
                )
            except LookupError as exc:
                raise RulePackNotFound("rule pack not found") from exc
            del provenance  # already encoded into record.reason by the application layer

            return self._commit_activation(
                connection,
                tenant_id=tenant_id,
                rule_pack_id=rule_pack_id,
                registry=registry,
                updated=updated,
                record=record,
                files_by_id=files_by_id,
                revisions=revisions,
                current_revision=current_revision,
                key_hash=key_hash,
                request_hash=request_hash,
                now=now,
                route=route,
            )

    def _commit_activation(
        self,
        connection,
        *,
        tenant_id: str,
        rule_pack_id: str,
        registry: RulePackRegistry,
        updated: RulePackRegistry,
        record,
        files_by_id: dict[str, dict[str, Any]],
        revisions: dict[str, int],
        current_revision: int,
        key_hash: str,
        request_hash: str,
        now: float,
        route: str = _ROUTE,
    ) -> StoredActivation:
        """Shared append-only tail for both human and AI-delegated activation.

        Writes a new immutable revision only for packs that actually changed,
        advances the (tenant, mode) active pointer, appends one activation
        event, and records one idempotency response. No historic row is
        mutated; a stale expected_revision still raises before any write.
        """
        before = {pack.rule_pack_id: pack for pack in registry.packs}
        response_revision = current_revision
        for pack in updated.packs:
            if before[pack.rule_pack_id] == pack:
                continue
            revision = revisions[pack.rule_pack_id] + 1
            connection.execute(
                "INSERT INTO rulepack_revisions VALUES (?, ?, ?, ?, ?)",
                (
                    tenant_id,
                    pack.rule_pack_id,
                    revision,
                    canonical_json(pack.to_dict()),
                    canonical_json(files_by_id[pack.rule_pack_id]),
                ),
            )
            changed = connection.execute(
                """UPDATE rulepack_heads SET revision=?
                   WHERE tenant_id=? AND rule_pack_id=? AND revision=?""",
                (revision, tenant_id, pack.rule_pack_id, revisions[pack.rule_pack_id]),
            )
            if changed.rowcount != 1:
                raise StaleRulePackRevision(revisions[pack.rule_pack_id])
            if pack.rule_pack_id == rule_pack_id:
                response_revision = revision

        activated = updated.get_pack(tenant_id, rule_pack_id)
        connection.execute(
            """INSERT INTO rulepack_active_pointers VALUES (?, ?, ?)
               ON CONFLICT(tenant_id, mode) DO UPDATE SET rule_pack_id=excluded.rule_pack_id""",
            (tenant_id, activated.mode, rule_pack_id),
        )
        body = _project_rule_pack(activated)
        connection.execute(
            "INSERT INTO rulepack_activation_events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                self._id_factory(),
                rule_pack_id,
                record.actor,
                record.reason,
                record.before_pack_id,
                record.after_pack_id,
                datetime.fromtimestamp(now, tz=UTC).isoformat().replace("+00:00", "Z"),
            ),
        )
        connection.execute(
            "INSERT INTO rulepack_idempotency_records VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
                route,
                key_hash,
                request_hash,
                canonical_json(body),
                response_revision,
                now + 86_400,
            ),
        )
        return StoredActivation(body=body, revision=response_revision)

    @staticmethod
    def _load_registry(
        connection: sqlite3.Connection, tenant_id: str
    ) -> tuple[RulePackRegistry, dict[str, dict[str, Any]], dict[str, int]]:
        rows = connection.execute(
            """SELECT h.rule_pack_id, h.revision, r.record_json, r.files_json
               FROM rulepack_heads h
               JOIN rulepack_revisions r USING (tenant_id, rule_pack_id, revision)
               WHERE h.tenant_id=?""",
            (tenant_id,),
        ).fetchall()
        packs = tuple(RulePackRecord.from_dict(json.loads(row["record_json"])) for row in rows)
        files = {str(row["rule_pack_id"]): json.loads(row["files_json"]) for row in rows}
        revisions = {str(row["rule_pack_id"]): int(row["revision"]) for row in rows}
        active = tuple(
            (tenant_id, str(row["mode"]), str(row["rule_pack_id"]))
            for row in connection.execute(
                "SELECT mode, rule_pack_id FROM rulepack_active_pointers WHERE tenant_id=?",
                (tenant_id,),
            )
        )
        runs = tuple(
            RunSnapshot(**json.loads(row["snapshot_json"]))
            for row in connection.execute(
                "SELECT snapshot_json FROM rulepack_run_snapshots WHERE tenant_id=?",
                (tenant_id,),
            )
        )
        return RulePackRegistry(packs=packs, runs=runs, active=active), files, revisions


def _project_rule_pack(pack: RulePackRecord) -> dict[str, Any]:
    return {
        "rule_pack_id": pack.rule_pack_id,
        "version": pack.version,
        "sha256": pack.sha256,
        "status": pack.status,
        "mode": pack.mode,
        "effective_date": pack.effective_date,
        "unresolved_gap_ids": list(pack.unresolved_gap_ids),
    }
