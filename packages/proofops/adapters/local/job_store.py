"""Local-synthetic-only durable jobs. Not a distributed AWS adapter.

SQLite schema v1 is additive and private to this adapter; unknown versions fail
closed. Rollback retains the DB and immutable artifacts with workers stopped.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from proofops.adapters.local.audit_store import (
    append_audit_transaction,
    initialize_audit_schema,
    read_audit_head,
)
from proofops.application.ports.jobs import JobConflict, JobLease, JobMessage, LeaseLost
from proofops.domain.audit import ChangeSet
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_strict_int, _require_uuid


def version_is_deleted(db, tenant, version_id):
    if (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='retention_tombstones'"
        ).fetchone()
        is None
    ):
        return False
    return (
        db.execute(
            "SELECT 1 FROM upload_records v JOIN retention_tombstones t "
            "ON t.tenant_id=v.tenant AND t.document_id=json_extract(v.data,'$.document_id') "
            "WHERE v.tenant=? AND v.kind='version' AND v.id=?",
            (tenant, version_id),
        ).fetchone()
        is not None
    )


def run_is_deleted(db, tenant, run_id):
    row = db.execute(
        "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? "
        "AND kind='run' AND record_id='META'",
        (tenant, run_id),
    ).fetchone()
    return row is not None and version_is_deleted(
        db, tenant, json.loads(row[0])["document_version_id"]
    )


class LocalSQLiteJobStore:
    kind = "local-synthetic-only"

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("durable jobs require a file-backed database")
        self.path = str(path)
        with self._transaction() as db:
            initialize_audit_schema(db)
            db.execute("CREATE TABLE IF NOT EXISTS job_schema (version INTEGER PRIMARY KEY)")
            versions = db.execute("SELECT version FROM job_schema").fetchall()
            if versions and versions != [(1,)]:
                raise JobConflict("unsupported job schema version")
            db.execute("INSERT OR IGNORE INTO job_schema VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS job_records (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, kind TEXT NOT NULL,
                record_id TEXT NOT NULL, value BLOB NOT NULL,
                PRIMARY KEY (tenant_id, run_id, kind, record_id))""")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # ponytail: one SQLite writer; use DynamoDB transactions for distributed workers.
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _scope(tenant: str, run: str) -> None:
        _require_uuid("tenant_id", tenant)
        _require_uuid("run_id", run)

    @staticmethod
    def _time(now: int, duration: int = 1) -> None:
        if _require_strict_int("now", now) < 0:
            raise ValueError("now must be non-negative")
        if _require_strict_int("duration", duration) < 1:
            raise ValueError("duration must be positive")

    @staticmethod
    def _raw(db, tenant, run, kind, key) -> bytes | None:
        row = db.execute(
            """SELECT value FROM job_records
            WHERE tenant_id=? AND run_id=? AND kind=? AND record_id=?""",
            (tenant, run, kind, key),
        ).fetchone()
        return None if row is None else row[0]

    def _get(self, db, tenant, run, kind, key) -> dict[str, Any]:
        if kind == "run" and run_is_deleted(db, tenant, run):
            raise KeyError("resource not found")
        raw = self._raw(db, tenant, run, kind, key)
        if raw is None:
            raise KeyError("resource not found")
        return json.loads(raw)

    @staticmethod
    def _put(db, tenant, run, kind, key, value, *, immutable=False) -> None:
        encoded = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        )
        sql = "INSERT" if immutable else "INSERT OR REPLACE"
        db.execute(
            f"{sql} INTO job_records VALUES (?, ?, ?, ?, ?)",
            (tenant, run, kind, key, encoded),
        )

    @staticmethod
    def _all(db, tenant, run, kind) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in db.execute(
                "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? AND kind=?",
                (tenant, run, kind),
            )
        ]

    def create_run(self, tenant_id: str, run_id: str, document_version_id: str) -> None:
        with self._transaction() as db:
            self.create_run_transaction(db, tenant_id, run_id, document_version_id)

    def create_run_transaction(self, db, tenant_id, run_id, document_version_id) -> None:
        """Compose initial job state in a caller-owned transaction; never commit."""
        if not db.in_transaction:
            raise ValueError("active transaction required")
        self._scope(tenant_id, run_id)
        _require_uuid("document_version_id", document_version_id)
        self._put(
            db,
            tenant_id,
            run_id,
            "run",
            "META",
            {
                "tenant_id": tenant_id,
                "run_id": run_id,
                "document_version_id": document_version_id,
                "status": "queued",
                "revision": 1,
                "mutation_epoch": 0,
                "cancellation_requested": False,
                "execution_profile": self.kind,
            },
            immutable=True,
        )

    def get_run(self, tenant_id: str, run_id: str) -> dict[str, Any]:
        self._scope(tenant_id, run_id)
        with self._transaction() as db:
            return self._get(db, tenant_id, run_id, "run", "META")

    def _bump_run(self, db, run, status=None, *, refresh_claim_counts=False) -> None:
        if refresh_claim_counts and "coverage" in run:
            coverage = run["coverage"]
            # Keep blocked records without heads and unprocessed work as published.
            processed = coverage["claims_decided"] + coverage["claims_needs_review"]
            decided = db.execute(
                """SELECT count(*) FROM job_records AS head
                JOIN job_records AS decision ON decision.tenant_id=head.tenant_id
                  AND decision.run_id=head.run_id AND decision.kind='decision_revision'
                  AND decision.record_id=head.record_id || ':' || printf('%010d',
                    json_extract(CAST(head.value AS TEXT), '$.decision_revision'))
                WHERE head.tenant_id=? AND head.run_id=? AND head.kind='claim_head'
                  AND json_extract(CAST(decision.value AS TEXT),
                    '$.decision.decision_status')='decided'""",
                (run["tenant_id"], run["run_id"]),
            ).fetchone()[0]
            if decided > processed:
                raise ValueError("decided claims exceed processed coverage")
            coverage.update(claims_decided=decided, claims_needs_review=processed - decided)
        run["revision"] += 1
        run["mutation_epoch"] += 1
        if status is not None:
            run["status"] = status
        self._put(db, run["tenant_id"], run["run_id"], "run", "META", run)

    @staticmethod
    def active_run_count(db, tenant_id) -> int:
        return sum(
            not run_is_deleted(db, tenant_id, row[0])
            for row in db.execute(
                """SELECT run_id FROM job_records WHERE tenant_id=? AND kind='run'
                   AND json_extract(CAST(value AS TEXT), '$.status') IN ('queued', 'running')""",
                (tenant_id,),
            )
        )

    def _job(self, db, message: JobMessage) -> dict[str, Any]:
        job = self._get(db, message.tenant_id, message.run_id, "job", message.job_id)
        if job["message"] != asdict(message):
            raise KeyError("resource not found")
        return job

    def get_job(self, message: JobMessage) -> dict[str, Any]:
        with self._transaction() as db:
            return self._job(db, message)

    def delivery_status(self, message: JobMessage) -> str:
        try:
            return self.get_job(message)["status"]
        except KeyError:
            return "not_found"

    def _save_job(self, db, message, job) -> None:
        self._put(db, message.tenant_id, message.run_id, "job", message.job_id, job)

    def _enqueue(self, db, message, now) -> None:
        tenant, run_id = message.tenant_id, message.run_id
        run = self._get(db, tenant, run_id, "run", "META")
        if (
            run["cancellation_requested"]
            or run["document_version_id"] != message.document_version_id
        ):
            raise JobConflict("run is cancelled or document version differs")
        for existing in self._all(db, tenant, run_id, "job"):
            previous = existing["message"]
            if previous["job_id"] == message.job_id or (previous["stage"], previous["shard"]) == (
                message.stage,
                message.shard,
            ):
                if previous != asdict(message):
                    raise JobConflict("immutable stage/shard/input identity conflict")
                return
        self._save_job(
            db,
            message,
            {
                "message": asdict(message),
                "status": "pending",
                "attempt": 0,
                "retry_attempt": 0,
                "fencing_token": 0,
                "lease_owner": None,
                "lease_until": None,
                "heartbeat_at": None,
                "artifact_ref": None,
                "error_code": None,
                "next_attempt_at": now,
            },
        )
        self._event(db, message, now, "initial")

    def enqueue(self, message: JobMessage, *, now: int) -> None:
        self._time(now)
        with self._transaction() as db:
            self._enqueue(db, message, now)

    def enqueue_transaction(self, db, message: JobMessage, *, now: int) -> None:
        """Same engine and connection as enqueue(), without opening a transaction."""
        if not db.in_transaction:
            raise ValueError("active transaction required")
        self._time(now)
        self._enqueue(db, message, now)

    def _event(self, db, message, now, cause) -> None:
        event_id = str(uuid5(NAMESPACE_URL, canonical_hash([asdict(message), cause])))
        self._put(
            db,
            message.tenant_id,
            message.run_id,
            "outbox",
            event_id,
            {
                "event_id": event_id,
                "event_type": "stage_ready",
                "job_id": message.job_id,
                "message": asdict(message),
                "status": "pending",
                "next_attempt_at": now,
                "attempts": 0,
            },
            immutable=True,
        )

    def claim_job(
        self, message: JobMessage, *, owner: str, now: int, lease_seconds: int
    ) -> JobLease | None:
        self._time(now, lease_seconds)
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("owner must be non-empty")
        with self._transaction() as db:
            try:
                job = self._job(db, message)
                run = self._get(db, message.tenant_id, message.run_id, "run", "META")
            except KeyError:
                return None
            if run["cancellation_requested"] or job["next_attempt_at"] > now:
                return None
            if not (
                job["status"] == "pending"
                or (job["status"] == "leased" and job["lease_until"] <= now)
            ):
                return None
            if message.stage == "extract" and "extract_job" in run:
                from proofops.adapters.local.claim_store import guard_extract_continuation

                try:
                    guard_extract_continuation(db, self, run, now=now, supersede=True)
                except ValueError:
                    return None
            job.update(
                status="leased",
                lease_owner=owner,
                lease_until=now + lease_seconds,
                heartbeat_at=now,
                attempt=job["attempt"] + 1,
                fencing_token=job["fencing_token"] + 1,
                retry_attempt=job["retry_attempt"] + 1,
            )
            lease = JobLease(
                message, owner, job["fencing_token"], job["attempt"], job["lease_until"]
            )
            self._save_job(db, message, job)
            self._put(
                db,
                message.tenant_id,
                message.run_id,
                "lease",
                str(lease.fencing_token) + ":" + message.job_id,
                asdict(lease),
                immutable=True,
            )
            self._bump_run(db, run, "running")
            return lease

    def _owned(self, db, lease, now) -> tuple[dict[str, Any], dict[str, Any]] | None:
        message = lease.message
        try:
            job = self._job(db, message)
            run = self._get(db, message.tenant_id, message.run_id, "run", "META")
        except KeyError:
            return None
        if (
            run["cancellation_requested"]
            or job["status"] != "leased"
            or job["fencing_token"] != lease.fencing_token
            or job["lease_owner"] != lease.owner
            or job["attempt"] != lease.attempt
            or job["lease_until"] <= now
        ):
            return None
        return job, run

    def can_call(self, lease: JobLease, *, now: int) -> bool:
        self._time(now)
        with self._transaction() as db:
            return self._owned(db, lease, now) is not None

    def heartbeat(self, lease: JobLease, *, now: int, lease_seconds: int) -> JobLease:
        self._time(now, lease_seconds)
        with self._transaction() as db:
            owned = self._owned(db, lease, now)
            if owned is None:
                raise LeaseLost("LEASE_LOST")
            job, _ = owned
            job.update(heartbeat_at=now, lease_until=max(job["lease_until"], now + lease_seconds))
            self._save_job(db, lease.message, job)
            return JobLease(
                lease.message, lease.owner, lease.fencing_token, lease.attempt, job["lease_until"]
            )

    def bind_parser_note_policy(self, lease, policy, *, now):
        self._time(now)
        message = lease.message
        if message.stage != "parse":
            raise ValueError("parser policy requires parse stage")
        with self._transaction() as db:
            if self._owned(db, lease, now) is None:
                raise LeaseLost("LEASE_LOST")
            raw = self._raw(
                db, message.tenant_id, message.run_id, "parser_note_policy", message.job_id
            )
            if raw is not None:
                if json.loads(raw) != policy:
                    raise ValueError("NOTE_REVIEW_POLICY_MISMATCH")
            else:
                self._put(
                    db,
                    message.tenant_id,
                    message.run_id,
                    "parser_note_policy",
                    message.job_id,
                    policy,
                    immutable=True,
                )

    def parser_note_policy(self, message):
        with self._transaction() as db:
            self._job(db, message)
            raw = self._raw(
                db, message.tenant_id, message.run_id, "parser_note_policy", message.job_id
            )
            return None if raw is None else json.loads(raw)

    def bind_parser_native_policy(self, lease, policy, *, now):
        self._time(now)
        message = lease.message
        if message.stage != "parse":
            raise ValueError("parser policy requires parse stage")
        from proofops.adapters.local.run_artifacts import native_paragraph_policy

        if policy is not None and policy != native_paragraph_policy():
            raise ValueError("NATIVE_PARAGRAPH_POLICY_MISMATCH")
        with self._transaction() as db:
            if self._owned(db, lease, now) is None:
                raise LeaseLost("LEASE_LOST")
            raw = self._raw(
                db, message.tenant_id, message.run_id, "parser_native_policy", message.job_id
            )
            if raw is not None:
                if json.loads(raw) != policy:
                    raise ValueError("NATIVE_PARAGRAPH_POLICY_MISMATCH")
            else:
                self._put(
                    db,
                    message.tenant_id,
                    message.run_id,
                    "parser_native_policy",
                    message.job_id,
                    policy,
                    immutable=True,
                )

    def parser_native_policy(self, message):
        with self._transaction() as db:
            self._job(db, message)
            raw = self._raw(
                db, message.tenant_id, message.run_id, "parser_native_policy", message.job_id
            )
            return None if raw is None else json.loads(raw)

    def register_parser_note_request(self, lease, request_id, *, now):
        self._time(now)
        _require_uuid("request_id", request_id)
        message = lease.message
        with self._transaction() as db:
            if message.stage != "parse" or self._owned(db, lease, now) is None:
                raise LeaseLost("LEASE_LOST")
            self._put(
                db,
                message.tenant_id,
                message.run_id,
                "parser_note_request",
                message.job_id + ":" + request_id,
                dict(request_id=request_id, fencing_token=lease.fencing_token, owner=lease.owner),
                immutable=True,
            )

    def finish_parser_note_request(self, lease, request_id):
        """Retain return evidence even if ownership expired during the HTTP call."""
        message = lease.message
        key = message.job_id + ":" + request_id
        with self._transaction() as db:
            registered = self._get(
                db, message.tenant_id, message.run_id, "parser_note_request", key
            )
            if registered != dict(
                request_id=request_id, fencing_token=lease.fencing_token, owner=lease.owner
            ):
                raise LeaseLost("unknown note request owner")
            if self._raw(db, message.tenant_id, message.run_id, "parser_note_return", key) is None:
                self._put(
                    db,
                    message.tenant_id,
                    message.run_id,
                    "parser_note_return",
                    key,
                    registered,
                    immutable=True,
                )

    def parser_note_requests(self, message, *, returned=False):
        with self._transaction() as db:
            self._job(db, message)
            return tuple(
                json.loads(row[0])["request_id"]
                for row in db.execute(
                    "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? "
                    "AND kind=? AND record_id LIKE ? ORDER BY record_id",
                    (
                        message.tenant_id,
                        message.run_id,
                        "parser_note_return" if returned else "parser_note_request",
                        message.job_id + ":%",
                    ),
                )
            )

    def bind_parser_note_reviews(self, lease, artifacts: tuple[str, ...], *, now: int):
        self._time(now)
        message = lease.message
        if message.stage != "parse" or not isinstance(artifacts, tuple):
            raise ValueError("invalid parser note input")
        if any(
            not isinstance(item, str) or len(item.encode("utf-8")) > 16 * 1024 * 1024
            for item in artifacts
        ):
            raise ValueError("invalid parser note input")
        with self._transaction() as db:
            if self._owned(db, lease, now) is None:
                raise LeaseLost("LEASE_LOST")
            raw = self._raw(
                db, message.tenant_id, message.run_id, "parser_note_input", message.job_id
            )
            if raw is not None:
                pinned = tuple(json.loads(raw))
                if artifacts and artifacts != pinned:
                    raise ValueError("NOTE_REVIEW_INPUT_ALREADY_BOUND")
                return pinned
            self._put(
                db,
                message.tenant_id,
                message.run_id,
                "parser_note_input",
                message.job_id,
                list(artifacts),
                immutable=True,
            )
            return artifacts

    def parser_note_reviews(self, message):
        with self._transaction() as db:
            self._job(db, message)
            raw = self._raw(
                db, message.tenant_id, message.run_id, "parser_note_input", message.job_id
            )
            return None if raw is None else tuple(json.loads(raw))

    def commit_job(
        self,
        lease: JobLease,
        *,
        payload: bytes,
        now: int,
        next_job: JobMessage | None = None,
        publish=None,
    ) -> bool:
        self._time(now)
        if not isinstance(payload, bytes):
            raise ValueError("checkpoint must be bytes")
        message = lease.message
        if next_job is not None and (
            next_job.tenant_id,
            next_job.run_id,
            next_job.document_version_id,
        ) != (message.tenant_id, message.run_id, message.document_version_id):
            raise ValueError("next job must belong to the same tenant/run/document version")
        with self._transaction() as db:
            owned = self._owned(db, lease, now)
            if owned is None:
                return False
            job, run = owned
            before_hash = canonical_hash(run)
            try:
                envelope = json.loads(payload)
            except (ValueError, UnicodeDecodeError):
                envelope = None
            if message.stage == "parse":
                pinned = self._raw(
                    db, message.tenant_id, message.run_id, "parser_note_input", message.job_id
                )
                policy_raw = self._raw(
                    db, message.tenant_id, message.run_id, "parser_note_policy", message.job_id
                )
                policy = None if policy_raw is None else json.loads(policy_raw)
                native_raw = self._raw(
                    db, message.tenant_id, message.run_id, "parser_native_policy", message.job_id
                )
                native_policy = None if native_raw is None else json.loads(native_raw)
                if (
                    native_raw is None
                    and isinstance(envelope, dict)
                    and envelope.get("schema")
                    in {"local_parser_checkpoint_v4", "local_parser_checkpoint_v5"}
                ):
                    raise ValueError("NATIVE_PARAGRAPH_INPUT_NOT_BOUND")
                if native_policy is not None and (
                    not isinstance(envelope, dict)
                    or envelope.get("schema")
                    not in {"local_parser_checkpoint_v4", "local_parser_checkpoint_v5"}
                    or envelope.get("native_paragraph_policy_sha256")
                    != canonical_hash(native_policy)
                ):
                    raise ValueError("NATIVE_PARAGRAPH_POLICY_MISMATCH")
                if (
                    isinstance(envelope, dict)
                    and native_policy is None
                    and (
                        envelope.get("schema")
                        in {"local_parser_checkpoint_v4", "local_parser_checkpoint_v5"}
                        or any(key.startswith("native_paragraph_") for key in envelope)
                    )
                ):
                    raise ValueError("NATIVE_PARAGRAPH_POLICY_MISMATCH")
                if native_policy is not None:
                    from proofops.adapters.local.run_artifacts import (
                        checkpoint_native_attestation,
                    )

                    if not isinstance(envelope, dict) or (
                        checkpoint_native_attestation(envelope) is None
                    ):
                        raise ValueError("NATIVE_PARAGRAPH_CHECKPOINT_INPUT_MISMATCH")
                if policy is not None and (
                    pinned is None
                    or not isinstance(envelope, dict)
                    or envelope.get("schema")
                    not in {
                        "local_parser_checkpoint_v3",
                        "local_parser_checkpoint_v4",
                        "local_parser_checkpoint_v5",
                    }
                    or envelope.get("note_review_policy_sha256") != canonical_hash(policy)
                ):
                    raise ValueError("NOTE_REVIEW_POLICY_MISMATCH")
                if (
                    isinstance(envelope, dict)
                    and policy is None
                    and (
                        envelope.get("schema") == "local_parser_checkpoint_v3"
                        or "note_review_policy_sha256" in envelope
                    )
                ):
                    raise ValueError("NOTE_REVIEW_POLICY_MISMATCH")
                if (
                    pinned is None
                    and isinstance(envelope, dict)
                    and envelope.get("schema") == "local_parser_checkpoint_v2"
                ):
                    raise ValueError("NOTE_REVIEW_INPUT_NOT_BOUND")
                if pinned is not None:
                    from proofops.adapters.local.run_artifacts import checkpoint_note_reviews

                    if not isinstance(envelope, dict) or checkpoint_note_reviews(envelope) != tuple(
                        json.loads(pinned)
                    ):
                        raise ValueError("NOTE_REVIEW_CHECKPOINT_INPUT_MISMATCH")
            if isinstance(envelope, dict) and str(envelope.get("schema", "")).startswith(
                "local_parser_checkpoint_"
            ):
                from proofops.adapters.local.run_artifacts import checkpoint_note_reviews

                checkpoint_note_reviews(envelope)
                coverage = envelope.get("coverage", {})
                counts = (
                    "pages_total",
                    "pages_processed",
                    "pages_unreadable",
                    "pages_unprocessed",
                    "chunks_discovered",
                    "chunks_processed",
                    "claims_discovered",
                    "claims_decided",
                    "claims_needs_review",
                )
                if (
                    message.stage != "parse"
                    or next_job is None
                    or next_job.stage != "extract"
                    or next_job.input_hash != message.input_hash
                    or envelope.get("input_hash") != message.input_hash
                    or envelope.get("stage_status") != "completed"
                    or envelope.get("downstream_status") != "pending"
                    or not isinstance(coverage, dict)
                    or set(coverage) != {*counts, "full_scope", "complete"}
                    or any(
                        type(coverage.get(key)) is not int or coverage[key] < 0 for key in counts
                    )
                    or coverage["complete"] is not False
                    or type(coverage["full_scope"]) is not bool
                    or coverage["pages_total"] != sum(coverage[k] for k in counts[1:4])
                    or any(coverage[k] != 0 for k in counts[4:])
                ):
                    raise ValueError("invalid local parse stage checkpoint")
                row = db.execute(
                    "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?",
                    (message.tenant_id, message.run_id),
                ).fetchone()
                if row is None:
                    raise ValueError("parse checkpoint requires a frozen run snapshot")
                snapshot = json.loads(row[0])
                if (
                    snapshot["input_hash"] != message.input_hash
                    or snapshot["document"]["version_id"] != message.document_version_id
                    or coverage["pages_total"] != snapshot["document"]["page_count"]
                    or coverage["full_scope"] != (snapshot["scope"] == "full")
                    or coverage["pages_unprocessed"]
                    != coverage["pages_total"] - len(snapshot["selected_pages"])
                ):
                    raise ValueError("parse coverage differs from frozen run selection")
                from proofops.adapters.local.raster_job_store import (
                    validate_raster_checkpoint_bindings,
                )

                validate_raster_checkpoint_bindings(db, self, message, snapshot, envelope)
                run.update(current_stage="extract", coverage=coverage, parse_job=asdict(message))
            if (
                message.stage == "extract"
                and "parse_job" in run
                and (
                    not isinstance(envelope, dict)
                    or envelope.get("schema")
                    not in {"local_extract_checkpoint_v1", "local_extract_checkpoint_v2"}
                )
            ):
                raise ValueError("local extract requires versioned claim snapshot")
            if isinstance(envelope, dict) and envelope.get("schema") in {
                "local_extract_checkpoint_v1",
                "local_extract_checkpoint_v2",
            }:
                from proofops.adapters.local.claim_store import validate_extract_commit

                validate_extract_commit(db, self, run, message, envelope, next_job, now=now)
                run.update(
                    current_stage="tag",
                    coverage=envelope["coverage"],
                    extract_job=asdict(message),
                    claim_snapshot_sha256=hashlib.sha256(payload).hexdigest(),
                )
            if message.stage == "tag" and "extract_job" in run:
                from proofops.adapters.local.tag_store import validate_tag_commit

                if not isinstance(envelope, dict) or publish is None:
                    raise ValueError("tag stage requires fenced versioned publication")
                coverage = validate_tag_commit(db, self, run, message, envelope, next_job)
                run.update(
                    current_stage="tag" if envelope["stage_status"] == "blocked" else "review",
                    status="partial",
                    coverage=coverage,
                    tag_job=asdict(message),
                    tag_stage_status=envelope["stage_status"],
                    tag_snapshot_sha256=hashlib.sha256(payload).hexdigest(),
                )
            elif publish is not None:
                raise ValueError("publication hook is restricted to local tag checkpoints")
            key = f"job/{message.job_id}/attempt/{lease.fencing_token}/checkpoint"
            self._put(
                db, message.tenant_id, message.run_id, "artifact", key, payload, immutable=True
            )
            job.update(
                status="succeeded",
                artifact_ref={
                    "key": key,
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "byte_size": len(payload),
                    "storage": self.kind,
                },
            )
            self._save_job(db, message, job)
            if next_job is not None:
                self._enqueue(db, next_job, now)
            if publish is not None:
                publish(db)
            self._bump_run(db, run, refresh_claim_counts=publish is not None)
            if publish is not None:
                append_audit_transaction(
                    connection=db,
                    change=ChangeSet(
                        message.tenant_id,
                        message.run_id,
                        "local-tag-worker",
                        "tag_stage_published",
                        message.job_id,
                        before_hash,
                        canonical_hash(run),
                        run["revision"],
                        envelope["stage_status"],
                    ),
                    expected_head=read_audit_head(db, message.tenant_id, message.run_id),
                    event_id=str(
                        uuid5(
                            NAMESPACE_URL,
                            canonical_hash(
                                [
                                    message.tenant_id,
                                    message.run_id,
                                    message.job_id,
                                    lease.fencing_token,
                                ]
                            ),
                        )
                    ),
                    timestamp=datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z"),
                )
            return True

    def read_checkpoint(self, message: JobMessage) -> bytes | None:
        with self._transaction() as db:
            ref = self._job(db, message)["artifact_ref"]
            if ref is None:
                return None
            payload = self._raw(db, message.tenant_id, message.run_id, "artifact", ref["key"])
            if (
                payload is None
                or hashlib.sha256(payload).hexdigest() != ref["sha256"]
                or len(payload) != ref["byte_size"]
            ):
                raise JobConflict("checkpoint corruption")
            return payload

    def record_usage(self, lease: JobLease, usage: dict[str, Any]) -> None:
        message = lease.message
        key = str(lease.fencing_token) + ":" + message.job_id
        with self._transaction() as db:
            recorded = self._get(db, message.tenant_id, message.run_id, "lease", key)
            # A renewed lease keeps the original immutable attempt identity.
            candidate = asdict(lease)
            candidate["lease_until"] = recorded["lease_until"]
            if candidate != recorded:
                raise LeaseLost("unknown attempt identity")
            if self._raw(db, message.tenant_id, message.run_id, "usage", key) is not None:
                if self._get(db, message.tenant_id, message.run_id, "usage", key) != usage:
                    raise JobConflict("immutable usage conflict")
                return
            note_ids = usage.get("note_request_ids", [])
            if note_ids:
                if message.stage != "parse" or len(set(note_ids)) != len(note_ids):
                    raise JobConflict("invalid note usage IDs")
                for identifier in note_ids:
                    _require_uuid("request_id", identifier)
                    if (
                        self._raw(
                            db,
                            message.tenant_id,
                            message.run_id,
                            "parser_note_request",
                            message.job_id + ":" + identifier,
                        )
                        is None
                    ):
                        raise JobConflict("note request not registered to job")
                for row in db.execute(
                    "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? AND kind='usage'",
                    (message.tenant_id, message.run_id),
                ):
                    if set(note_ids) & set(json.loads(row[0]).get("note_request_ids", [])):
                        raise JobConflict("note request usage already recorded")
            self._put(db, message.tenant_id, message.run_id, "usage", key, usage, immutable=True)

    def list_usage(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        with self._transaction() as db:
            self._get(db, tenant_id, run_id, "run", "META")
            return [
                json.loads(row[0])
                for row in db.execute(
                    "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? AND kind='usage'",
                    (tenant_id, run_id),
                )
            ]

    def get_usage(self, message: JobMessage, *, fencing_token: int) -> dict[str, Any]:
        with self._transaction() as db:
            self._job(db, message)
            return self._get(
                db,
                message.tenant_id,
                message.run_id,
                "usage",
                str(fencing_token) + ":" + message.job_id,
            )

    def fail_job(
        self,
        lease: JobLease,
        *,
        error_code: str,
        now: int,
        jitter: float = 0.5,
        retry_after: int | None = None,
    ) -> dict[str, Any]:
        self._time(now)
        if not isinstance(jitter, int | float) or isinstance(jitter, bool) or not 0 <= jitter <= 1:
            raise ValueError("jitter must be in [0, 1]")
        if not isinstance(error_code, str) or not error_code:
            raise ValueError("error_code required")
        if retry_after is not None:
            self._time(retry_after)
        with self._transaction() as db:
            owned = self._owned(db, lease, now)
            if owned is None:
                raise LeaseLost("LEASE_LOST")
            job, run = owned
            transient = error_code in {
                "MODEL_THROTTLED",
                "PROVIDER_5XX",
                "MODEL_TIMEOUT",
                "NOTE_ACCOUNTING_PENDING",
            }
            retry = transient and job["retry_attempt"] < 3
            delay = (
                min(60, retry_after)
                if retry_after is not None
                else (2 ** job["retry_attempt"] * jitter)
            )
            job.update(
                status="pending" if retry else "failed",
                error_code=error_code,
                next_attempt_at=now + delay,
            )
            self._save_job(db, lease.message, job)
            if retry:
                self._event(db, lease.message, now + delay, f"retry:{lease.fencing_token}")
            self._bump_run(
                db,
                run,
                "running"
                if retry
                else "partial"
                if any(
                    item["status"] == "succeeded"
                    for item in self._all(db, lease.message.tenant_id, lease.message.run_id, "job")
                )
                else "failed",
            )
            return job

    def _action(
        self, tenant, run_id, *, action, expected_revision, idempotency_key, reason, now, actor_sub
    ):
        self._scope(tenant, run_id)
        self._time(now)
        if _require_strict_int("expected_revision", expected_revision) < 1:
            raise JobConflict("revision must be positive")
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 128:
            raise ValueError("invalid idempotency key")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason required")
        if not isinstance(actor_sub, str) or not actor_sub.strip():
            raise ValueError("server actor_sub required")
        timestamp = datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")
        key = canonical_hash([action, idempotency_key])
        request_hash = canonical_hash([action, expected_revision, reason])
        with self._transaction() as db:
            run = self._get(db, tenant, run_id, "run", "META")
            if self._raw(db, tenant, run_id, "idempotency", key) is not None:
                previous = self._get(db, tenant, run_id, "idempotency", key)
                if now < previous.get("expires_at", 0):
                    if previous["request_hash"] != request_hash:
                        raise JobConflict("idempotency key reused with different request")
                    return previous["response"]
            if run["revision"] != expected_revision:
                raise JobConflict("stale revision")
            jobs = self._all(db, tenant, run_id, "job")
            before = deepcopy({"run": run, "jobs": jobs})
            if action == "cancel":
                if run["status"] == "completed":
                    raise JobConflict("completed run cannot be cancelled")
                run["cancellation_requested"] = True
                for job in jobs:
                    if job["status"] in {"pending", "leased", "failed"}:
                        job["status"] = "cancelled"
                        self._save_job(db, JobMessage(**job["message"]), job)
                self._bump_run(db, run, "cancelled")
            else:
                if run["status"] not in {"partial", "failed"} or run["cancellation_requested"]:
                    raise JobConflict("run is not retryable")
                if self.active_run_count(db, tenant) >= 2:
                    raise JobConflict("tenant run limit")
                for job in jobs:
                    if job["status"] == "failed":
                        job.update(status="pending", next_attempt_at=now, retry_attempt=0)
                        message = JobMessage(**job["message"])
                        self._save_job(db, message, job)
                        self._event(db, message, now, f"manual:{expected_revision}")
                self._bump_run(db, run, "running")
            self._put(
                db,
                tenant,
                run_id,
                "idempotency",
                key,
                {"request_hash": request_hash, "response": run, "expires_at": now + 86400},
            )
            after = {"run": run, "jobs": self._all(db, tenant, run_id, "job")}
            before_hash = canonical_hash(before)
            after_hash = canonical_hash(after)
            event_id = str(uuid5(NAMESPACE_URL, canonical_hash([tenant, run_id, run["revision"]])))
            self._put(
                db,
                tenant,
                run_id,
                "action",
                str(run["revision"]),
                {
                    "schema_version": 1,
                    "tenant_id": tenant,
                    "run_id": run_id,
                    "event_id": event_id,
                    "actor_sub": actor_sub,
                    "timestamp": timestamp,
                    "target_id": run_id,
                    "action": action,
                    "reason": reason,
                    "revision": run["revision"],
                    "before": before,
                    "after": after,
                    "before_hash": before_hash,
                    "after_hash": after_hash,
                },
                immutable=True,
            )
            append_audit_transaction(
                connection=db,
                change=ChangeSet(
                    tenant_id=tenant,
                    run_id=run_id,
                    actor_sub=actor_sub,
                    action=action,
                    target_id=run_id,
                    before_hash=before_hash,
                    after_hash=after_hash,
                    revision=run["revision"],
                    reason=reason,
                ),
                expected_head=read_audit_head(db, tenant, run_id),
                event_id=event_id,
                timestamp=timestamp,
            )
            return run

    def cancel_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
        actor_sub: str,
        now: int,
    ) -> dict[str, Any]:
        return self._action(
            tenant_id,
            run_id,
            action="cancel",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            reason=reason,
            now=now,
            actor_sub=actor_sub,
        )

    def retry_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        reason: str,
        now: int,
        actor_sub: str,
    ) -> dict[str, Any]:
        return self._action(
            tenant_id,
            run_id,
            action="retry",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            reason=reason,
            now=now,
            actor_sub=actor_sub,
        )

    def get_action(self, tenant_id: str, run_id: str, *, revision: int) -> dict[str, Any]:
        """Full immutable action envelope committed with its canonical audit event."""
        self._scope(tenant_id, run_id)
        with self._transaction() as db:
            return self._get(db, tenant_id, run_id, "action", str(revision))

    def pending_outbox(self, tenant_id: str, run_id: str, *, now: int) -> list[dict[str, Any]]:
        self._scope(tenant_id, run_id)
        self._time(now)
        with self._transaction() as db:
            run = self._get(db, tenant_id, run_id, "run", "META")
            if run["cancellation_requested"]:
                return []
            return sorted(
                (
                    event
                    for event in self._all(db, tenant_id, run_id, "outbox")
                    if event["status"] == "pending" and event["next_attempt_at"] <= now
                ),
                key=lambda event: (event["next_attempt_at"], event["event_id"]),
            )

    def mark_outbox(
        self,
        tenant_id: str,
        run_id: str,
        event_id: str,
        *,
        now: int,
        sent: bool,
        expected_attempts: int,
    ) -> bool:
        self._scope(tenant_id, run_id)
        self._time(now)
        with self._transaction() as db:
            event = self._get(db, tenant_id, run_id, "outbox", event_id)
            if event["status"] != "pending" or event["attempts"] != expected_attempts:
                return False
            event.update(
                status="sent" if sent else "pending",
                attempts=expected_attempts + 1,
                next_attempt_at=now + min(60, 2 ** min(expected_attempts + 1, 6)),
            )
            self._put(db, tenant_id, run_id, "outbox", event_id, event)
            return True
