"""Isolated local-synthetic-only deletion adapter; NEVER wired to customer/runtime storage.

Private additive schema v1: data DB contains synthetic resources, separate ledger DB
contains immutable tombstones/attempts. Restore only data, retain/replay ledger before
serving. Rollback stops the worker and retains ledger; deleted payloads cannot be undone.
"""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from proofops.application.retention import (
    DeletionManifest,
    DeletionResult,
    DeletionTarget,
    valid_time,
)
from proofops.domain.values import _require_uuid


def _manifest(raw):
    data = json.loads(raw)
    data["targets"] = tuple(DeletionTarget(**t) for t in data["targets"])
    return DeletionManifest(**data)


def _result(raw):
    data = json.loads(raw)
    for key in ("deleted", "remaining"):
        data[key] = tuple(DeletionTarget(**t) for t in data[key])
    data["error_codes"] = tuple(data["error_codes"])
    return DeletionResult(**data)


class LocalSyntheticDeletionStore:
    kind = "local-synthetic-only"

    def __init__(self, data_path, ledger_path):
        if ":memory:" in (str(data_path), str(ledger_path)):
            raise ValueError("durable files required")
        self.data_path, self.ledger_path = str(data_path), str(ledger_path)
        if Path(data_path).resolve() == Path(ledger_path).resolve():
            raise ValueError("tombstone ledger must survive data restore separately")
        with self._transaction() as db:
            for schema in ("main", "data"):
                db.execute(f"CREATE TABLE IF NOT EXISTS {schema}.deletion_schema (version INTEGER)")
                rows = db.execute(f"SELECT version FROM {schema}.deletion_schema").fetchall()
                if rows not in ([], [(1,)]):
                    raise ValueError("unsupported synthetic deletion schema")
                if not rows:
                    db.execute(f"INSERT INTO {schema}.deletion_schema VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS data.resources (
                tenant_id TEXT, document_id TEXT, document_version TEXT, kind TEXT,
                resource_id TEXT, payload BLOB, expires_at INTEGER,
                PRIMARY KEY(tenant_id,document_id,document_version,kind,resource_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS tombstones (
                tenant_id TEXT, document_id TEXT, manifest TEXT,
                PRIMARY KEY(tenant_id,document_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS attempts (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT, document_id TEXT, result TEXT)""")
            for table in ("tombstones", "attempts"):
                for action in ("UPDATE", "DELETE"):
                    db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()}
                        BEFORE {action} ON {table}
                        BEGIN SELECT RAISE(ABORT, 'immutable deletion history'); END""")
                # Prevent INSERT OR REPLACE from erasing earlier history.
                keys = ("tenant_id", "document_id") if table == "tombstones" else ("sequence",)
                match = " AND ".join(f"{key}=NEW.{key}" for key in keys)
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_insert
                    BEFORE INSERT ON {table} WHEN EXISTS (SELECT 1 FROM {table} WHERE {match})
                    BEGIN SELECT RAISE(ABORT, 'immutable deletion history'); END""")

    @contextmanager
    def _transaction(self):
        # ponytail: one SQLite writer; distributed fences required for cloud workers.
        db = sqlite3.connect(self.ledger_path, timeout=10, isolation_level=None)
        try:
            db.execute("ATTACH DATABASE ? AS data", (self.data_path,))
            db.execute("PRAGMA data.secure_delete=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _is_deleted(db, tenant, document):
        return (
            db.execute(
                "SELECT 1 FROM tombstones WHERE tenant_id=? AND document_id=?", (tenant, document)
            ).fetchone()
            is not None
        )

    def put(self, tenant, document, version, kind, resource_id, payload, *, expires_at=0):
        _require_uuid("tenant_id", tenant)
        _require_uuid("document_id", document)
        valid_time(expires_at)
        DeletionTarget(version, kind, resource_id, sha256(payload).hexdigest())
        with self._transaction() as db:
            if self._is_deleted(db, tenant, document):
                raise ValueError("document tombstone blocks writes")
            db.execute(
                "INSERT INTO data.resources VALUES (?,?,?,?,?,?,?)",
                (tenant, document, version, kind, resource_id, payload, expires_at),
            )

    def read(self, tenant, document, version, kind, resource_id):
        with self._transaction() as db:
            if self._is_deleted(db, tenant, document):
                raise KeyError("resource not found")
            row = db.execute(
                """SELECT payload FROM data.resources WHERE tenant_id=?
                AND document_id=? AND document_version=? AND kind=? AND resource_id=?""",
                (tenant, document, version, kind, resource_id),
            ).fetchone()
            if row is None:
                raise KeyError("resource not found")
            return row[0]

    def request(self, tenant, document, *, requested_by, now):
        with self._transaction() as db:
            row = db.execute(
                "SELECT manifest FROM tombstones WHERE tenant_id=? AND document_id=?",
                (tenant, document),
            ).fetchone()
            if row:
                return _manifest(row[0])
            session = _Session(db, tenant, document)
            manifest = DeletionManifest(
                tenant, document, str(uuid4()), requested_by, now, session.inventory()
            )
            if not manifest.targets:
                raise KeyError("resource not found")
            db.execute(
                "INSERT INTO tombstones VALUES (?,?,?)",
                (tenant, document, json.dumps(asdict(manifest))),
            )
            return manifest

    @contextmanager
    def scope(self, manifest):
        with self._transaction() as db:
            row = db.execute(
                "SELECT manifest FROM tombstones WHERE tenant_id=? AND document_id=?",
                (manifest.tenant_id, manifest.document_id),
            ).fetchone()
            if row is None or _manifest(row[0]) != manifest:
                raise ValueError("unknown or altered deletion manifest")
            yield _Session(db, manifest.tenant_id, manifest.document_id)

    def tombstones(self):
        with self._transaction() as db:
            return tuple(
                _manifest(row[0])
                for row in db.execute(
                    "SELECT manifest FROM tombstones ORDER BY tenant_id,document_id"
                )
            )

    def attempts(self, tenant, document):
        with self._transaction() as db:
            return tuple(
                _result(row[0])
                for row in db.execute(
                    "SELECT result FROM attempts WHERE tenant_id=? AND document_id=? "
                    "ORDER BY sequence",
                    (tenant, document),
                )
            )


class _Session:
    def __init__(self, db, tenant, document):
        self.db, self.tenant, self.document = db, tenant, document

    def inventory(self):
        return tuple(
            DeletionTarget(version, kind, key, sha256(payload).hexdigest())
            for version, kind, key, payload in self.db.execute(
                """SELECT document_version,kind,resource_id,payload FROM data.resources
            WHERE tenant_id=? AND document_id=? ORDER BY document_version,kind,resource_id""",
                (self.tenant, self.document),
            )
        )

    def delete(self, target):
        try:
            self.db.execute(
                """DELETE FROM data.resources WHERE tenant_id=? AND document_id=?
                AND document_version=? AND kind=? AND resource_id=?""",
                (
                    self.tenant,
                    self.document,
                    target.document_version,
                    target.kind,
                    target.resource_id,
                ),
            )
        except sqlite3.DatabaseError:
            raise RuntimeError("DELETE_FAILED") from None

    def record(self, result):
        self.db.execute(
            "INSERT INTO attempts(tenant_id,document_id,result) VALUES (?,?,?)",
            (self.tenant, self.document, json.dumps(asdict(result))),
        )
