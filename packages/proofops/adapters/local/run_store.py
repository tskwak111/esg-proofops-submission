"""Additive local run schema v1; retaining rollback is in evidence/local-run-integration.md."""

from __future__ import annotations

import base64
import hmac
import json
import secrets
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from proofops.adapters.aws.usage import LocalSQLiteUsageStore
from proofops.adapters.local.audit_store import append_audit_transaction, read_audit_head
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
from proofops.adapters.local.job_store import (
    LocalSQLiteJobStore,
    run_is_deleted,
    version_is_deleted,
)
from proofops.adapters.local.rulepack_store import RulePackSqliteStore
from proofops.application.mode_gate import select_mode_rulepack
from proofops.application.ports.jobs import JobConflict, JobMessage
from proofops.application.registry import artifact_sha256
from proofops.application.rulepacks import RunSnapshot
from proofops.application.runs import RunRejected, validate_raster_snapshot
from proofops.domain.audit import ChangeSet
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json


class LocalSQLiteRunStore:
    def __init__(self, path, *, rulepacks=None):
        self.path = str(path)
        if self.path == ":memory:":
            raise ValueError("durable run storage requires a file")
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.rulepacks = rulepacks or RulePackSqliteStore(path)
        if Path(self.rulepacks.path).resolve() != Path(self.path).resolve():
            raise ValueError("rulepacks/jobs/usage/runs must share one database")
        self.jobs = LocalSQLiteJobStore(path)
        self.usage = LocalSQLiteUsageStore(path)
        with self.jobs._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS run_schema (version INTEGER PRIMARY KEY)")
            versions = db.execute("SELECT version FROM run_schema").fetchall()
            if versions and versions != [(1,)]:
                raise ValueError("unsupported run schema")
            db.execute("INSERT OR IGNORE INTO run_schema VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS run_snapshots (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (tenant_id, run_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS run_idempotency (
                tenant_id TEXT NOT NULL, key_hash TEXT NOT NULL, request_hash TEXT NOT NULL,
                response TEXT NOT NULL, expires_at INTEGER NOT NULL,
                PRIMARY KEY (tenant_id, key_hash))""")
            db.execute("""CREATE TABLE IF NOT EXISTS run_cursor_key (
                id INTEGER PRIMARY KEY CHECK(id=1), secret BLOB NOT NULL)""")
            db.execute(
                "INSERT OR IGNORE INTO run_cursor_key VALUES (1, ?)", (secrets.token_bytes(32),)
            )
            self._cursor_key = db.execute(
                "SELECT secret FROM run_cursor_key WHERE id=1"
            ).fetchone()[0]
            initialize_catalog_pages(db)
            for operation in ("UPDATE", "DELETE"):
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS run_snapshot_no_{operation.lower()}
                    BEFORE {operation} ON run_snapshots BEGIN
                    SELECT RAISE(ABORT, 'immutable run snapshot'); END""")

    @staticmethod
    def _replay(db, tenant, body, key, now):
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise RunRejected("IDEMPOTENCY_KEY_INVALID", 400)
        if version_is_deleted(db, tenant, body.get("document_version_id")):
            raise RunRejected("RESOURCE_NOT_FOUND", 404)
        row = db.execute(
            "SELECT request_hash, response, expires_at FROM run_idempotency "
            "WHERE tenant_id=? AND key_hash=?",
            (tenant, canonical_hash(key)),
        ).fetchone()
        if row is not None and row[2] > now:
            if row[0] != canonical_hash(body):
                raise RunRejected("IDEMPOTENCY_CONFLICT")
            return json.loads(row[1])
        return None

    def replay(self, tenant, body, key, *, now):
        with self.jobs._transaction() as db:
            return self._replay(db, tenant, body, key, now)

    def create(self, auth, body, key, snapshot, limits, *, now):
        tenant, run_id = auth.tenant_id, str(uuid4())
        with self.jobs._transaction() as db:
            replay = self._replay(db, tenant, body, key, now)
            if replay is not None:
                return replay
            try:
                validate_raster_snapshot(snapshot)
                if snapshot.get("fact_assembly_profile", "strict-v1") not in (
                    "strict-v1",
                    "partial-facts-v1",
                ):
                    raise ValueError("unknown fact assembly profile")
                if "report_level_link" in snapshot:
                    from proofops.application.tagging.report_level_link import validate_config

                    validate_config(snapshot["report_level_link"])
                    if any(
                        ref.get("document_version_id") != snapshot["document"]["version_id"]
                        for refs in snapshot["report_level_link"]["refs"].values()
                        for ref in refs
                        if "source_id" in ref
                    ):
                        raise ValueError("report-level link document mismatch")
                if "claim_source_policy" in snapshot:
                    from proofops.adapters.local.claim_source_policies import (
                        publication_reader,
                    )

                    # Admissible for a NEW run only: the current base verifier or
                    # the current opt-in render-resolution wrapper.
                    publication_reader(snapshot["claim_source_policy"])
                    if snapshot.get("extraction_mode") != "upstage_probe":
                        raise ValueError("claim source policy mismatch")
                relation_fields = (
                    "relation_settings",
                    "relation_settings_hash",
                    "relation_runtime",
                    "relation_runtime_artifact_hash",
                )
                relation_present = [field in snapshot for field in relation_fields]
                if any(relation_present) and (
                    not all(relation_present) or snapshot.get("tagging_mode") != "upstage_local"
                ):
                    raise ValueError("relation snapshot requires complete upstage_local group")
                if snapshot.get("tagging_mode") == "upstage_local":
                    if (
                        snapshot.get("extraction_mode") != "upstage_probe"
                        or body["mode"] != "disclosure"
                    ):
                        raise ValueError("live tagging requires disclosure probe context")
                    for label, frozen in (
                        ("preliminary_settings", "preliminary_settings_hash"),
                        ("tagging_settings", "tagging_settings_hash"),
                        ("input_reservation_policy", "input_reservation_policy_hash"),
                    ):
                        if label not in snapshot or canonical_hash(snapshot[label]) != snapshot.get(
                            frozen
                        ):
                            raise ValueError("live tagging snapshot identity mismatch")
                    if (
                        "preliminary_prompt_sha256" in snapshot
                        and canonical_hash(snapshot["preliminary_settings"]["system_prompt"])
                        != snapshot["preliminary_prompt_sha256"]
                    ):
                        raise ValueError("live tagging preliminary prompt mismatch")
                    if "relation_settings" in snapshot and (
                        canonical_hash(snapshot["relation_settings"])
                        != snapshot["relation_settings_hash"]
                    ):
                        raise ValueError("live tagging relation snapshot identity mismatch")
                    for label, frozen in (
                        ("preliminary_runtime", "preliminary_runtime_artifact_hash"),
                        ("tagging_runtime", "tagging_runtime_artifact_hash"),
                    ):
                        if label not in snapshot or artifact_sha256(
                            snapshot[label]
                        ) != snapshot.get(frozen):
                            raise ValueError("live tagging runtime identity mismatch")
                    if (
                        "relation_runtime" in snapshot
                        and artifact_sha256(snapshot["relation_runtime"])
                        != snapshot["relation_runtime_artifact_hash"]
                    ):
                        raise ValueError("live tagging relation runtime identity mismatch")
                    rulepack = self.rulepacks.extraction_snapshot_transaction(
                        db, tenant, body["mode"], body["rule_pack_id"]
                    )
                    if (
                        rulepack.status == "active"
                        and rulepack.approved_by
                        and rulepack.approved_at
                    ):
                        rulepack = self.rulepacks.active_snapshot_transaction(
                            db, tenant, body["mode"], body["rule_pack_id"]
                        )
                        select_mode_rulepack(body["mode"], rulepack, tenant_id=tenant)
                        snapshot = dict(snapshot, rulepack_use="approved_grading")
                    else:
                        snapshot = dict(snapshot, rulepack_use="candidate_tagging_reference_only")
                elif snapshot.get("extraction_mode") == "upstage_probe":
                    if (
                        snapshot.get("tagging_settings")
                        or snapshot.get("relation_settings")
                        or body["mode"] != "disclosure"
                    ):
                        raise ValueError("extraction-only runtime required")
                    rulepack = self.rulepacks.extraction_snapshot_transaction(
                        db, tenant, body["mode"], body["rule_pack_id"]
                    )
                    snapshot = dict(snapshot, rulepack_use="extraction_reference_only")
                else:
                    rulepack = self.rulepacks.active_snapshot_transaction(
                        db, tenant, body["mode"], body["rule_pack_id"]
                    )
                    select_mode_rulepack(body["mode"], rulepack, tenant_id=tenant)
            except (LookupError, ValueError):
                raise RunRejected("CONFIG_GATE_BLOCKED") from None
            if "extraction_profile" in snapshot and (
                canonical_hash(snapshot["extraction_profile"])
                != snapshot["extraction_profile_hash"]
            ):
                raise RunRejected("CONFIG_GATE_BLOCKED")
            if self.jobs.active_run_count(db, tenant) >= 2:
                raise RunRejected("TENANT_RUN_LIMIT", 429)
            snapshot = dict(snapshot, rulepack=asdict(rulepack), tenant_id=tenant, run_id=run_id)
            snapshot["input_hash"] = canonical_hash(snapshot)
            document_id = body["document_version_id"]
            self.jobs.create_run_transaction(db, tenant, run_id, document_id)
            self.jobs.enqueue_transaction(
                db,
                JobMessage(
                    tenant,
                    run_id,
                    document_id,
                    str(uuid4()),
                    "parse",
                    "full" if body["scope"] == "full" else "declared_subset",
                    snapshot["input_hash"],
                ),
                now=now,
            )
            self.usage.create_budget_transaction(db, tenant, run_id, document_id, limits)
            self.rulepacks.add_run_snapshot_transaction(
                db, RunSnapshot(run_id, tenant, rulepack.rule_pack_id, rulepack.sha256, "queued")
            )
            db.execute(
                "INSERT INTO run_snapshots VALUES (?, ?, ?)",
                (tenant, run_id, canonical_json(snapshot)),
            )
            run = self.jobs._get(db, tenant, run_id, "run", "META")
            run.update(
                mode=snapshot["mode"],
                scope=snapshot["scope"],
                selected_pages=snapshot["selected_pages"],
                rule_pack_id=rulepack.rule_pack_id,
                rule_pack_sha256=rulepack.sha256,
                parser_profile_hash=snapshot["parser_profile_hash"],
                model_binding_hash=snapshot["model_binding_hash"],
            )
            self.jobs._put(db, tenant, run_id, "run", "META", run)
            response = self._project(run, snapshot)
            append_audit_transaction(
                connection=db,
                change=ChangeSet(
                    tenant,
                    run_id,
                    auth.user_sub,
                    "run.create",
                    run_id,
                    None,
                    canonical_hash({"run": run, "snapshot": snapshot}),
                    1,
                    None,
                ),
                expected_head=read_audit_head(db, tenant, run_id),
                event_id=str(uuid4()),
                timestamp=snapshot["created_at"],
            )
            db.execute(
                "INSERT OR REPLACE INTO run_idempotency VALUES (?, ?, ?, ?, ?)",
                (
                    tenant,
                    canonical_hash(key),
                    canonical_hash(body),
                    canonical_json(response),
                    now + 86400,
                ),
            )
            return response

    @staticmethod
    def _snapshot(db, tenant, run_id):
        if run_is_deleted(db, tenant, run_id):
            raise RunRejected("RESOURCE_NOT_FOUND", 404)
        row = db.execute(
            "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?", (tenant, run_id)
        ).fetchone()
        if row is None:
            raise RunRejected("RESOURCE_NOT_FOUND", 404)
        return json.loads(row[0])

    def snapshot(self, tenant, run_id):
        with self.jobs._transaction() as db:
            return self._snapshot(db, tenant, run_id)

    @staticmethod
    def _project(run, snapshot):
        count = snapshot["document"]["page_count"]
        coverage = run.get(
            "coverage",
            dict(
                pages_total=count,
                pages_processed=0,
                pages_unreadable=0,
                pages_unprocessed=count,
                chunks_discovered=0,
                chunks_processed=0,
                claims_discovered=0,
                claims_decided=0,
                claims_needs_review=0,
                full_scope=snapshot["scope"] == "full",
                complete=False,
            ),
        )
        projection = dict(
            run_id=run["run_id"],
            document_version_id=run["document_version_id"],
            status=run["status"],
            revision=run["revision"],
            mutation_epoch=run["mutation_epoch"],
            current_stage=run.get("current_stage", "parse"),
            coverage=coverage,
            rule_pack_sha256=snapshot["rulepack"]["sha256"],
            created_at=snapshot["created_at"],
        )
        return json.loads(canonical_json(projection))

    def get(self, tenant, run_id):
        with self.jobs._transaction() as db:
            snapshot = self._snapshot(db, tenant, run_id)
            return self._project(self.jobs._get(db, tenant, run_id, "run", "META"), snapshot)

    def list(self, tenant, *, cursor=None, limit=50, now):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RunRejected("VALIDATION_ERROR", 422)
        with self.jobs._transaction() as db:
            try:
                return catalog_page(
                    db,
                    tenant_id=tenant,
                    endpoint="runs",
                    query={"limit": limit},
                    cursor=cursor,
                    limit=limit,
                    now=now,
                    load_items=lambda: sorted(
                        (
                            self._project(json.loads(row[1]), self._snapshot(db, tenant, row[0]))
                            for row in db.execute(
                                "SELECT run_id,value FROM job_records "
                                "WHERE tenant_id=? AND kind='run' AND record_id='META'",
                                (tenant,),
                            )
                            if not run_is_deleted(db, tenant, row[0])
                        ),
                        key=lambda item: (item["created_at"], item["run_id"]),
                    ),
                )
            except InvalidCatalogCursor:
                raise RunRejected("INVALID_CURSOR", 400) from None
            except CatalogCapacityExceeded:
                raise RunRejected("CATALOG_CAPACITY") from None

    def action(self, auth, run_id, action, *, expected_revision, key, reason, now):
        snapshot = self.snapshot(auth.tenant_id, run_id)
        method = self.jobs.cancel_run if action == "cancel" else self.jobs.retry_run
        try:
            result = method(
                auth.tenant_id,
                run_id,
                expected_revision=expected_revision,
                idempotency_key=key,
                reason=reason,
                actor_sub=auth.user_sub,
                now=now,
            )
        except JobConflict as exc:
            code, status = {
                "stale revision": ("STALE_RUN_REVISION", 412),
                "tenant run limit": ("TENANT_RUN_LIMIT", 429),
            }.get(str(exc), ("RUN_CONFLICT", 409))
            raise RunRejected(code, status) from None
        return self._project(result, snapshot)

    def _encode_cursor(self, payload):
        data = canonical_json(payload).encode()
        return base64.urlsafe_b64encode(
            data + hmac.digest(self._cursor_key, data, "sha256")
        ).decode()

    def _decode_cursor(self, cursor, tenant, run_id, limit, now, *, endpoint="audit"):
        try:
            if len(cursor) > 2048:
                raise ValueError
            raw = base64.b64decode(cursor, altchars=b"-_", validate=True)
            data, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(signature, hmac.digest(self._cursor_key, data, "sha256")):
                raise ValueError
            payload = json.loads(data)
            if payload["scope"] != [tenant, run_id, endpoint, limit] or now >= payload["expires"]:
                raise ValueError
            return payload
        except (ValueError, KeyError, TypeError):
            raise RunRejected("INVALID_CURSOR", 400) from None

    def audit(self, tenant, run_id, *, cursor, limit, now):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise RunRejected("VALIDATION_ERROR", 422)
        with self.jobs._transaction() as db:
            self._snapshot(db, tenant, run_id)
            run = self.jobs._get(db, tenant, run_id, "run", "META")
            page = (
                self._decode_cursor(cursor, tenant, run_id, limit, now)
                if cursor
                else dict(
                    scope=[tenant, run_id, "audit", limit],
                    expires=now + 900,
                    after=0,
                    cutoff=read_audit_head(db, tenant, run_id).sequence,
                    epoch=run["mutation_epoch"],
                )
            )
            rows = db.execute(
                """SELECT event_id, sequence, action, actor_sub, target_id,
                before_hash, after_hash, event_hash, previous_event_hash, timestamp
                FROM audit_events WHERE tenant_id=? AND run_id=? AND sequence>? AND sequence<=?
                ORDER BY sequence LIMIT ?""",
                (tenant, run_id, page["after"], page["cutoff"], limit + 1),
            ).fetchall()
        fields = (
            "event_id",
            "sequence",
            "action",
            "actor_display",
            "target_id",
            "before_hash",
            "after_hash",
            "event_hash",
            "previous_event_hash",
            "created_at",
        )
        items = [dict(zip(fields, row, strict=True)) for row in rows[:limit]]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = self._encode_cursor(dict(page, after=items[-1]["sequence"]))
        return dict(items=items, next_cursor=next_cursor, snapshot_epoch=page["epoch"])
