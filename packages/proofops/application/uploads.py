"""Local upload service: real PDF verification, synthetic SQLite/filesystem storage.

No S3 identity is invented. Production must supply approved versioned storage and
job transport; this service is mounted only by local-synthetic composition.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Any
from uuid import UUID, uuid4

from proofops.adapters.local.catalog_pages import (
    CatalogCapacityExceeded,
    InvalidCatalogCursor,
)
from proofops.adapters.local.catalog_pages import (
    initialize as initialize_catalog_pages,
)
from proofops.adapters.local.catalog_pages import (
    page as catalog_page,
)
from proofops.adapters.local.retention_store import document_is_deleted
from proofops.application.registry import Registry, RegistryNotFound
from proofops.application.uploads_security import (
    LocalUploadVault,
    PdfLimits,
    QuarantinedPdf,
    UploadRejected,
)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _uuid(value: object) -> None:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise UploadRejected("VALIDATION_ERROR") from None


def _text(value: object, maximum: int = 200) -> None:
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise UploadRejected("VALIDATION_ERROR")


def validate_payload(kind: str, body: Mapping[str, Any]) -> dict[str, Any]:
    """Strict trust boundary shared by HTTP and direct application callers."""
    required = {
        "document": {"company_id", "title", "document_type"},
        "version": {
            "filename",
            "size_bytes",
            "sha256",
            "report_year",
            "industry_system",
            "period_start",
            "period_end",
            "rights_profile_id",
        },
        "complete": {"sha256", "size_bytes"},
    }[kind]
    optional = {"industry_code", "consolidation_scope"} if kind == "version" else set()
    if (
        not isinstance(body, Mapping)
        or not required <= body.keys()
        or body.keys() - required - optional
    ):
        raise UploadRejected("VALIDATION_ERROR")
    result = json.loads(_json(dict(body)))
    if kind == "document":
        _uuid(result["company_id"])
        _text(result["title"])
        if result["document_type"] not in ("sustainability_report", "annual_report_section"):
            raise UploadRejected("VALIDATION_ERROR")
        return result
    if (
        type(result["size_bytes"]) is not int
        or result["size_bytes"] < 1
        or not isinstance(result["sha256"], str)
        or re.fullmatch("[0-9a-f]{64}", result["sha256"]) is None
    ):
        raise UploadRejected("VALIDATION_ERROR")
    if result["size_bytes"] > 104_857_600:
        raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
    if kind == "version":
        _text(result["filename"])
        _uuid(result["rights_profile_id"])
        if (
            type(result["report_year"]) is not int
            or not 1900 <= result["report_year"] <= 2200
            or result["industry_system"] not in ("gics", "sasb", "custom", "unknown")
        ):
            raise UploadRejected("VALIDATION_ERROR")
        for key in optional:
            if result.get(key) is not None and not isinstance(result[key], str):
                raise UploadRejected("VALIDATION_ERROR")
        try:
            start, end = result["period_start"], result["period_end"]
            if (
                not isinstance(start, str)
                or not isinstance(end, str)
                or date.fromisoformat(start).isoformat() != start
                or date.fromisoformat(end).isoformat() != end
                or start > end
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise UploadRejected("VALIDATION_ERROR") from None
    return result


class UploadService:
    """Explicit synthetic local adapter with durable state and create-only source files."""

    local_synthetic = True

    def __init__(
        self,
        database_path: Path,
        object_root: Path,
        registry: Registry,
        *,
        limits: PdfLimits = PdfLimits(),
        clock: Callable[[], float] = time.time,
    ):
        self.registry, self.limits, self.clock = registry, limits, clock
        self.root = Path(object_root)
        self.vault = LocalUploadVault(self.root)
        self._lock = RLock()
        self._db = sqlite3.connect(
            database_path, check_same_thread=False, timeout=60, isolation_level=None
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS upload_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._db.execute("INSERT OR IGNORE INTO upload_metadata VALUES ('schema_version','1')")
        version = self._db.execute(
            "SELECT value FROM upload_metadata WHERE key='schema_version'"
        ).fetchone()
        if version != ("1",):
            self._db.close()
            raise ValueError("unsupported upload component schema")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS upload_records (
                tenant TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY (tenant, kind, id));
            CREATE TABLE IF NOT EXISTS upload_idempotency (
                tenant TEXT NOT NULL, route TEXT NOT NULL, key TEXT NOT NULL,
                fingerprint TEXT NOT NULL, response TEXT NOT NULL, expires REAL NOT NULL,
                PRIMARY KEY (tenant, route, key));
            CREATE TABLE IF NOT EXISTS upload_audit (
                tenant TEXT NOT NULL, document_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                data TEXT NOT NULL, PRIMARY KEY (tenant, document_id, sequence));
            CREATE TRIGGER IF NOT EXISTS immutable_upload_audit_update
            BEFORE UPDATE ON upload_audit
            BEGIN SELECT RAISE(ABORT, 'immutable upload audit'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_upload_audit_delete
            BEFORE DELETE ON upload_audit
            BEGIN SELECT RAISE(ABORT, 'immutable upload audit'); END;
            CREATE TRIGGER IF NOT EXISTS immutable_document_version
            BEFORE UPDATE ON upload_records WHEN OLD.kind = 'version'
            BEGIN SELECT RAISE(ABORT, 'immutable document version'); END;
        """)
        with self._transaction():
            initialize_catalog_pages(self._db)

    def close(self) -> None:
        self._db.close()

    @contextmanager
    def _transaction(self):
        # ponytail: local global writer lock; per-record cloud transactions for deployment.
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    def _get(self, tenant: str, kind: str, identifier: str) -> dict[str, Any]:
        _uuid(tenant)
        _uuid(identifier)
        with self._lock:
            row = self._db.execute(
                "SELECT data FROM upload_records WHERE tenant=? AND kind=? AND id=?",
                (tenant, kind, identifier),
            ).fetchone()
        if row is None:
            raise UploadRejected("NOT_FOUND")
        record = json.loads(row[0])
        if record.get("schema_version") != 1:
            raise UploadRejected("SCHEMA_VERSION_UNSUPPORTED")
        with self._lock:
            if document_is_deleted(self._db, tenant, record.get("document_id")):
                raise UploadRejected("NOT_FOUND")
        return record

    def _put(self, tenant: str, kind: str, identifier: str, value: dict, *, update=False):
        if document_is_deleted(self._db, tenant, value.get("document_id")):
            raise UploadRejected("NOT_FOUND")
        if update:
            self._db.execute(
                "UPDATE upload_records SET data=? WHERE tenant=? AND kind=? AND id=?",
                (_json(value), tenant, kind, identifier),
            )
        else:
            self._db.execute(
                "INSERT INTO upload_records VALUES (?,?,?,?)",
                (tenant, kind, identifier, _json(value)),
            )

    def _replay(self, tenant: str, route: str, key: str, body: dict) -> dict | None:
        _uuid(tenant)
        _text(key, 128)
        if len(key) < 16:
            raise UploadRejected("IDEMPOTENCY_KEY_INVALID")
        row = self._db.execute(
            "SELECT fingerprint,response,expires FROM upload_idempotency "
            "WHERE tenant=? AND route=? AND key=?",
            (tenant, route, key),
        ).fetchone()
        if row and row[2] > self.clock():
            if row[0] != sha256(_json(body).encode()).hexdigest():
                raise UploadRejected("IDEMPOTENCY_CONFLICT")
            response = json.loads(row[1])
            for kind, field in (
                ("document", "document_id"),
                ("version", "version_id"),
                ("upload", "upload_id"),
            ):
                if field in response:
                    self._get(tenant, kind, response[field])
            return response
        return None

    def _remember(self, tenant: str, route: str, key: str, body: dict, response: dict):
        self._db.execute(
            "INSERT OR REPLACE INTO upload_idempotency VALUES (?,?,?,?,?,?)",
            (
                tenant,
                route,
                key,
                sha256(_json(body).encode()).hexdigest(),
                _json(response),
                self.clock() + 86400,
            ),
        )

    def _company(self, tenant: str, company_id: str) -> dict:
        try:
            return asdict(self.registry.get_company(tenant_id=tenant, company_id=company_id))
        except RegistryNotFound:
            raise UploadRejected("NOT_FOUND") from None

    def create_document(
        self,
        tenant: str,
        body: Mapping[str, Any],
        key: str,
        *,
        actor_sub: str = "local-synthetic-service",
    ) -> dict:
        _text(actor_sub)
        payload = validate_payload("document", body)
        company = self._company(tenant, payload["company_id"])
        with self._transaction():
            replay = self._replay(tenant, "documents", key, payload)
            if replay is not None:
                return replay
            document = dict(
                payload,
                document_id=str(uuid4()),
                latest_version_id=None,
                revision=1,
                created_at=self._timestamp(),
            )
            self._put(
                tenant,
                "document",
                document["document_id"],
                dict(document, company=company, schema_version=1),
            )
            self._audit(
                tenant, document["document_id"], "document.created", actor_sub, after=document
            )
            self._remember(tenant, "documents", key, payload, document)
            return document

    def get_document(self, tenant: str, document_id: str) -> dict:
        stored = self._get(tenant, "document", document_id)
        return {
            key: value for key, value in stored.items() if key not in ("company", "schema_version")
        }

    def list_documents(
        self, tenant: str, *, cursor: str | None = None, limit: int = 50, now: float | None = None
    ) -> dict:
        return self._catalog_page(
            tenant,
            "documents",
            {"limit": limit},
            cursor,
            limit,
            lambda: sorted(
                (
                    {
                        key: value
                        for key, value in document.items()
                        if key not in ("company", "schema_version")
                    }
                    for row in self._db.execute(
                        "SELECT data FROM upload_records WHERE tenant=? AND kind='document'",
                        (tenant,),
                    )
                    if not document_is_deleted(
                        self._db, tenant, (document := json.loads(row[0]))["document_id"]
                    )
                ),
                key=lambda item: (item["created_at"], item["document_id"]),
            ),
            self.clock() if now is None else now,
        )

    def list_versions(
        self,
        tenant: str,
        document_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        now: float | None = None,
    ) -> dict:
        self._get(tenant, "document", document_id)
        return self._catalog_page(
            tenant,
            "document_versions",
            {"document_id": document_id, "limit": limit},
            cursor,
            limit,
            lambda: sorted(
                (
                    self._project_version(snapshot)
                    for row in self._db.execute(
                        "SELECT data FROM upload_records WHERE tenant=? AND kind='version'",
                        (tenant,),
                    )
                    if (snapshot := json.loads(row[0]))["document_id"] == document_id
                ),
                key=lambda item: (item["created_at"], item["version_id"]),
            ),
            self.clock() if now is None else now,
        )

    def _catalog_page(self, tenant, endpoint, query, cursor, limit, load_items, now):
        _uuid(tenant)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise UploadRejected("VALIDATION_ERROR")
        try:
            with self._transaction():
                return catalog_page(
                    self._db,
                    tenant_id=tenant,
                    endpoint=endpoint,
                    query=query,
                    cursor=cursor,
                    limit=limit,
                    now=now,
                    load_items=load_items,
                )
        except InvalidCatalogCursor:
            raise UploadRejected("INVALID_CURSOR") from None
        except CatalogCapacityExceeded:
            raise UploadRejected("CATALOG_CAPACITY") from None

    def initiate_upload(
        self,
        tenant: str,
        document_id: str,
        body: Mapping[str, Any],
        key: str,
        *,
        actor_sub: str = "local-synthetic-service",
    ) -> dict:
        _text(actor_sub)
        payload = validate_payload("version", body)
        if payload["size_bytes"] > self.limits.max_bytes:
            raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
        document = self._get(tenant, "document", document_id)
        try:
            self.registry.validate_document(
                tenant_id=tenant,
                company_id=document["company_id"],
                rights_profile_id=payload["rights_profile_id"],
            )
        except RegistryNotFound:
            raise UploadRejected("NOT_FOUND") from None
        route = f"documents/{document_id}/versions"
        with self._transaction():
            replay = self._replay(tenant, route, key, payload)
            if replay is not None:
                return replay
            identifier = str(uuid4())
            ticket = dict(
                upload_id=identifier,
                document_id=document_id,
                post_url=f"/local/uploads/{identifier}/content",
                post_fields={"ticket": secrets.token_urlsafe(32)},
                expires_at=self._timestamp(self.clock() + 600),
            )
            self._put(
                tenant,
                "upload",
                identifier,
                dict(
                    ticket=ticket,
                    metadata=payload,
                    document=document,
                    version_id=str(uuid4()),
                    object_version_id=f"local-synthetic:{uuid4()}",
                    status="pending",
                    created_at=self._timestamp(),
                    expires=self.clock() + 600,
                    received=False,
                    schema_version=1,
                ),
            )
            self._audit(
                tenant,
                document_id,
                "upload.initiated",
                actor_sub,
                after=payload,
                upload_id=identifier,
                source_sha256=payload["sha256"],
            )
            self._remember(tenant, route, key, payload, ticket)
            return ticket

    def check_receipt(self, tenant: str, upload_id: str) -> dict:
        item = self._get(tenant, "upload", upload_id)
        self._pending(item)
        if item["received"]:
            raise UploadRejected("VERSION_CONFLICT")
        return item["ticket"]

    def _pending(self, item: dict):
        if item["status"] != "pending":
            raise UploadRejected("VERSION_CONFLICT")
        if self.clock() >= item["expires"]:
            raise UploadRejected("UPLOAD_EXPIRED")

    def receive_content(self, tenant: str, upload_id: str, content: bytes, media_type: str) -> None:
        with self._transaction():
            item = self._get(tenant, "upload", upload_id)
            self._pending(item)
            if item["received"]:
                raise UploadRejected("VERSION_CONFLICT")
            if not isinstance(content, bytes) or media_type != "application/pdf":
                raise UploadRejected("PDF_INVALID")
            if len(content) > self.limits.max_bytes:
                raise UploadRejected("UPLOAD_LIMIT_EXCEEDED")
            if (
                len(content) != item["metadata"]["size_bytes"]
                or sha256(content).hexdigest() != item["metadata"]["sha256"]
            ):
                raise UploadRejected("UPLOAD_INTEGRITY_MISMATCH")
            target = self.root / "quarantine" / tenant / f"{upload_id}.pdf"
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with NamedTemporaryFile(dir=target.parent) as staged:
                staged.write(content)
                staged.flush()
                os.fsync(staged.fileno())
                os.chmod(staged.name, 0o400)
                try:
                    os.link(staged.name, target)
                except FileExistsError:
                    if target.is_symlink() or target.read_bytes() != content:
                        raise UploadRejected("VERSION_CONFLICT") from None
            item["received"] = True
            self._put(tenant, "upload", upload_id, item, update=True)

    def validation_job(self, tenant: str, upload_id: str) -> dict:
        item = self._get(tenant, "upload", upload_id)
        if "validation_job" not in item:
            raise UploadRejected("UPLOAD_NOT_READY")
        return item["validation_job"]

    def enqueue_validation(
        self, tenant: str, upload_id: str, body: Mapping[str, Any], key: str
    ) -> dict:
        payload = validate_payload("complete", body)
        with self._transaction():
            item = self._get(tenant, "upload", upload_id)
            self._replay(tenant, f"uploads/{upload_id}/complete", key, payload)
            if any(payload[field] != item["metadata"][field] for field in ("sha256", "size_bytes")):
                raise UploadRejected("UPLOAD_INTEGRITY_MISMATCH")
            if item["status"] == "accepted":
                return item["validation_job"]
            self._pending(item)
            if not item["received"]:
                raise UploadRejected("UPLOAD_NOT_READY")
            if "validation_job" not in item:
                item["validation_job"] = dict(
                    job_id=str(uuid4()),
                    upload_id=upload_id,
                    tenant_id=tenant,
                    object_version_id=item["object_version_id"],
                    version_id=item["version_id"],
                    status="queued",
                    attempt=0,
                    result_version_id=None,
                    error_code=None,
                    created_at=self._timestamp(),
                )
                self._put(tenant, "upload", upload_id, item, update=True)
            return item["validation_job"]

    def complete_upload(
        self,
        tenant: str,
        upload_id: str,
        body: Mapping[str, Any],
        key: str,
        *,
        actor_sub: str = "local-synthetic-service",
    ) -> dict:
        _text(actor_sub)
        payload = validate_payload("complete", body)
        route = f"uploads/{upload_id}/complete"
        self.enqueue_validation(tenant, upload_id, payload, key)
        with self._transaction():
            item = self._get(tenant, "upload", upload_id)
            replay = self._replay(tenant, route, key, payload)
            if replay is not None:
                return replay
            if any(payload[field] != item["metadata"][field] for field in ("sha256", "size_bytes")):
                raise UploadRejected("UPLOAD_INTEGRITY_MISMATCH")
            if item["status"] == "accepted":
                result = self.get_version(tenant, item["version_id"])
                self._remember(tenant, route, key, payload, result)
                return result
            self._pending(item)
            if not item["received"]:
                raise UploadRejected("UPLOAD_NOT_READY")
            item["validation_job"].update(
                status="running", attempt=item["validation_job"]["attempt"] + 1
            )
            self._put(tenant, "upload", upload_id, item, update=True)
        # A restarted local runner resumes the same job and immutable input. Duplicate
        # runners may verify identical bytes, but only one accepted checkpoint commits.
        # Verify outside the DB transaction. Concurrent completions use the same fixed
        # object/version and publish identical bytes; only one transaction advances HEAD.
        source_path = self.root / "quarantine" / tenant / f"{upload_id}.pdf"
        with source_path.open("rb") as source_file:
            content = source_file.read(self.limits.max_bytes + 1)
        source = QuarantinedPdf(
            tenant,
            item["version_id"],
            item["object_version_id"],
            content,
            item["metadata"]["size_bytes"],
            item["metadata"]["sha256"],
        )
        try:
            verified = self.vault.verify_and_promote(source, self.limits, tenant_id=tenant)
        except UploadRejected as exc:
            with self._transaction():
                current = self._get(tenant, "upload", upload_id)
                if current["status"] == "pending":
                    current["status"] = "rejected"
                    current["validation_job"].update(status="failed", error_code=str(exc))
                    self._put(tenant, "upload", upload_id, current, update=True)
            raise
        with self._transaction():
            current = self._get(tenant, "upload", upload_id)
            if current["status"] == "accepted":
                result = self.get_version(tenant, current["version_id"])
            else:
                self._pending(current)
                result = dict(
                    version_id=item["version_id"],
                    document_id=item["document"]["document_id"],
                    sha256=source.expected_sha256,
                    report_year=item["metadata"]["report_year"],
                    page_count=verified.page_count,
                    status="ready",
                    created_at=item["created_at"],
                )
                snapshot = dict(
                    result,
                    tenant_id=tenant,
                    metadata=item["metadata"],
                    company=item["document"]["company"],
                    document_type=item["document"]["document_type"],
                    object_version_id=item["object_version_id"],
                    s3_version_id=None,
                    local_synthetic=True,
                    schema_version=1,
                )
                self._audit(
                    tenant,
                    result["document_id"],
                    "upload.accepted",
                    actor_sub,
                    before=self.get_document(tenant, result["document_id"]),
                    after=snapshot,
                    upload_id=upload_id,
                    version_id=item["version_id"],
                    job_id=current["validation_job"]["job_id"],
                    source_sha256=source.expected_sha256,
                )
                self._put(tenant, "version", item["version_id"], snapshot)
                document = self._get(tenant, "document", result["document_id"])
                latest_id = document["latest_version_id"]
                latest = self.get_version(tenant, latest_id) if latest_id else None
                if latest is None or (
                    datetime.fromisoformat(result["created_at"]),
                    result["version_id"],
                ) > (
                    datetime.fromisoformat(latest["created_at"]),
                    latest["version_id"],
                ):
                    document.update(
                        latest_version_id=item["version_id"], revision=document["revision"] + 1
                    )
                    self._put(tenant, "document", result["document_id"], document, update=True)
                current["status"] = "accepted"
                current["validation_job"].update(
                    status="ready", result_version_id=item["version_id"], error_code=None
                )
                self._put(tenant, "upload", upload_id, current, update=True)
            self._remember(tenant, route, key, payload, result)
            return result

    def _audit(
        self,
        tenant: str,
        document_id: str,
        action: str,
        actor_sub: str,
        *,
        after: dict,
        before: dict | None = None,
        upload_id: str | None = None,
        version_id: str | None = None,
        job_id: str | None = None,
        source_sha256: str | None = None,
    ) -> None:
        row = self._db.execute(
            "SELECT sequence,data FROM upload_audit WHERE tenant=? AND document_id=? "
            "ORDER BY sequence DESC LIMIT 1",
            (tenant, document_id),
        ).fetchone()
        event = dict(
            tenant_id=tenant,
            document_id=document_id,
            upload_id=upload_id,
            version_id=version_id,
            job_id=job_id,
            actor_sub=actor_sub,
            action=action,
            reason="local_upload_lifecycle",
            source_sha256=source_sha256,
            sequence=row[0] + 1 if row else 1,
            event_id=str(uuid4()),
            timestamp=self._timestamp(),
            schema_version=1,
            before_hash=sha256(_json(before).encode()).hexdigest() if before else None,
            after_hash=sha256(_json(after).encode()).hexdigest(),
            previous_event_hash=json.loads(row[1])["event_hash"] if row else None,
        )
        event["event_hash"] = sha256(_json(event).encode()).hexdigest()
        self._db.execute(
            "INSERT INTO upload_audit VALUES (?,?,?,?)",
            (tenant, document_id, event["sequence"], _json(event)),
        )

    def read_upload_audit(self, tenant: str, document_id: str) -> tuple[dict, ...]:
        self.get_document(tenant, document_id)
        with self._lock:
            rows = self._db.execute(
                "SELECT data FROM upload_audit WHERE tenant=? AND document_id=? "
                "ORDER BY sequence",
                (tenant, document_id),
            ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def version_snapshot(self, tenant: str, version_id: str) -> dict:
        return self._get(tenant, "version", version_id)

    def get_version(self, tenant: str, version_id: str) -> dict:
        snapshot = self.version_snapshot(tenant, version_id)
        return self._project_version(snapshot)

    @staticmethod
    def _project_version(snapshot: dict) -> dict:
        return {
            key: snapshot[key]
            for key in (
                "version_id",
                "document_id",
                "sha256",
                "report_year",
                "page_count",
                "status",
                "created_at",
            )
        }

    def read_original(self, tenant: str, version_id: str) -> bytes:
        snapshot = self.version_snapshot(tenant, version_id)
        path = self.root / "original" / tenant / f"{version_id}.pdf"
        with path.open("rb") as original:
            content = original.read(self.limits.max_bytes + 1)
        if (
            len(content) != snapshot["metadata"]["size_bytes"]
            or sha256(content).hexdigest() != snapshot["sha256"]
        ):
            raise UploadRejected("UPLOAD_INTEGRITY_MISMATCH")
        return content

    def _timestamp(self, timestamp: float | None = None) -> str:
        return (
            datetime.fromtimestamp(self.clock() if timestamp is None else timestamp, UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
