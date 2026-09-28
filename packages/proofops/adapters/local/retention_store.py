"""Local-synthetic deletion requests, additive schema v1; no physical deletion enabled.

Unapproved customer policy always blocks physical deletion. Tombstones and the
metadata-only request audit commit together. Old payload/revision tables are untouched.
Rollback must keep tombstone guards and tables; removing guards resurrects access.
"""

import json
import math
from datetime import UTC, datetime
from uuid import uuid4

from proofops.application.retention import RESOURCE_KINDS
from proofops.application.uploads_security import UploadRejected
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_uuid


class RetentionRejected(ValueError):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def initialize_retention_schema(db):
    db.execute("CREATE TABLE IF NOT EXISTS retention_schema (version INTEGER PRIMARY KEY)")
    rows = db.execute("SELECT version FROM retention_schema").fetchall()
    if rows not in ([], [(1,)]):
        raise ValueError("unsupported retention schema")
    db.execute("INSERT OR IGNORE INTO retention_schema VALUES (1)")
    db.execute("""CREATE TABLE IF NOT EXISTS retention_tombstones (
        tenant_id TEXT NOT NULL, document_id TEXT NOT NULL, deletion_id TEXT NOT NULL,
        requested_at TEXT NOT NULL, requested_by TEXT NOT NULL, request_key TEXT NOT NULL,
        request_hash TEXT NOT NULL, manifest_json TEXT NOT NULL,
        PRIMARY KEY(tenant_id,document_id))""")
    for action in ("UPDATE", "DELETE"):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS retention_tombstones_{action.lower()}
            BEFORE {action} ON retention_tombstones
            BEGIN SELECT RAISE(ABORT, 'immutable deletion tombstone'); END""")
    db.execute("""CREATE TRIGGER IF NOT EXISTS retention_tombstones_insert
        BEFORE INSERT ON retention_tombstones WHEN EXISTS (
            SELECT 1 FROM retention_tombstones WHERE tenant_id=NEW.tenant_id
            AND document_id=NEW.document_id)
        BEGIN SELECT RAISE(ABORT, 'immutable deletion tombstone'); END""")


def document_is_deleted(db, tenant_id, document_id):
    """For caller-owned read/write transactions; no commits or connection ownership here."""
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", ("retention_tombstones",)
    ).fetchone():
        return False  # Pre-migration databases have no deletion requests yet.
    return (
        db.execute(
            "SELECT 1 FROM retention_tombstones WHERE tenant_id=? AND document_id=?",
            (tenant_id, document_id),
        ).fetchone()
        is not None
    )


class LocalRetentionStore:
    kind = "local-synthetic-only"

    def __init__(self, uploads):
        if not uploads.local_synthetic:
            raise ValueError("local-synthetic uploads required")
        self.uploads = uploads
        with uploads._transaction():
            initialize_retention_schema(uploads._db)

    def is_deleted(self, tenant_id, document_id):
        with self.uploads._lock:
            return document_is_deleted(self.uploads._db, tenant_id, document_id)

    @staticmethod
    def _response(row):
        return dict(deletion_id=row[0], status="blocked_retention", requested_at=row[1])

    def request(self, actor, document_id, body, key, *, now):
        if not actor.has_capability("admin"):
            raise RetentionRejected("FORBIDDEN", 403)
        _require_uuid("document_id", document_id)
        _require_uuid("tenant_id", actor.tenant_id)
        if (
            not isinstance(body, dict)
            or set(body) != {"reason"}
            or (
                not isinstance(body["reason"], str)
                or not 5 <= len(body["reason"]) <= 1000
                or len(body["reason"].strip()) < 5
            )
        ):
            raise RetentionRejected("VALIDATION_ERROR", 422)
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise RetentionRejected("IDEMPOTENCY_KEY_INVALID", 400)
        if type(now) not in (float, int) or not math.isfinite(now) or now < 0:
            raise RetentionRejected("VALIDATION_ERROR", 422)
        tenant = actor.tenant_id
        request_key = canonical_hash([actor.user_sub, document_id, key])
        request_hash = canonical_hash(body)
        uploads = self.uploads
        with uploads._transaction():
            db = uploads._db
            row = db.execute(
                """SELECT deletion_id,requested_at,request_key,request_hash
                FROM retention_tombstones WHERE tenant_id=? AND document_id=?""",
                (tenant, document_id),
            ).fetchone()
            if row:
                if row[3] != request_hash:
                    raise RetentionRejected("IDEMPOTENCY_CONFLICT")
                return self._response(row)
            try:
                uploads._get(tenant, "document", document_id)
            except UploadRejected:
                raise RetentionRejected("RESOURCE_NOT_FOUND", 404) from None
            # Inventory identifiers/hashes only. Unconnected stores remain not_run, never empty.
            versions, records = [], []
            for kind, identifier, raw in db.execute(
                "SELECT kind,id,data FROM upload_records WHERE tenant=?", (tenant,)
            ):
                value = json.loads(raw)
                if value.get("document_id") != document_id:
                    continue
                if kind == "version":
                    versions.append(identifier)
                records.append(
                    dict(kind=kind, resource_id=identifier, sha256=canonical_hash(value))
                )
            manifest = dict(
                tenant_id=tenant,
                document_id=document_id,
                execution_profile=self.kind,
                policy_status="requires_customer_approval",
                physical_deletion_status="not_run",
                document_version_ids=sorted(versions),
                categories={kind: "not_run" for kind in sorted(RESOURCE_KINDS)},
                upload_records=records,
            )
            deletion_id = str(uuid4())
            requested_at = datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")
            db.execute(
                "INSERT INTO retention_tombstones VALUES (?,?,?,?,?,?,?,?)",
                (
                    tenant,
                    document_id,
                    deletion_id,
                    requested_at,
                    actor.user_sub,
                    request_key,
                    request_hash,
                    json.dumps(manifest, sort_keys=True),
                ),
            )
            db.execute("DELETE FROM catalog_list_snapshots WHERE tenant_id=?", (tenant,))
            uploads._audit(
                tenant,
                document_id,
                "document.deletion_requested",
                actor.user_sub,
                after=dict(
                    deletion_id=deletion_id,
                    request_hash=request_hash,
                    manifest_sha256=canonical_hash(manifest),
                    status="blocked_retention",
                ),
            )
            return self._response((deletion_id, requested_at))
