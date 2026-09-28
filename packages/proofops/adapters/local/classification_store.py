"""R22 local persistence for manual preliminary classification + bounded reprocess.

Schema v1 adds immutable ``preliminary_classification`` / ``preliminary_classification_head``
/ ``preliminary_classification_idempotency`` kinds and an atomic
CAS + idempotent enqueue of exactly one bounded reprocess tag job. Existing readers
ignore these kinds; rollback stops the writer/route and retains all records (no
destructive down migration). Re-running initialization is idempotent.

Nothing here computes a grade, a label, a decision or a model confidence. The record
holds only the reviewer's track/category/dimension source refs; the reprocess job runs
the real element-tagging stage through the existing worker path.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from proofops.application.authorization import AuthContext
from proofops.application.tagging.manual_classification import (
    ALLOWED_AXES,
    REQUIRED_AXES,
    ClassificationRejected,
    classification_snapshot,
    validate_manual_classification,
)
from proofops.application.tagging.preliminary import _sources
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_uuid

RECORD_KIND = "preliminary_classification"
HEAD_KIND = "preliminary_classification_head"
IDEMPOTENCY_KIND = "preliminary_classification_idempotency"
SCHEMA_TABLE = "preliminary_classification_schema"

ALLOWED_TRACKS = ("goal", "performance", "management")
ALLOWED_SAFE_HARBOR_CATEGORIES = (
    None,
    "forward_looking",
    "emissions_estimate",
    "third_party_information",
)
_BLOCKED_REASON = "PRELIMINARY_TAGS_UNRESOLVED"


class ClassificationStoreError(ClassificationRejected):
    """Store-level rejection with an HTTP status; nothing was persisted."""


class LocalSQLiteClassificationStore:
    """Immutable classification records + one atomic bounded reprocess enqueue."""

    def __init__(self, store, uploads, parser, tags, claims):
        self.store = store
        self.uploads = uploads
        self.parser = parser
        self.tags = tags
        self.claims = claims
        self.jobs = store.jobs
        with self.jobs._transaction() as db:
            db.execute(f"CREATE TABLE IF NOT EXISTS {SCHEMA_TABLE} (version INTEGER PRIMARY KEY)")
            versions = db.execute(f"SELECT version FROM {SCHEMA_TABLE}").fetchall()
            if versions and versions != [(1,)]:
                raise ClassificationStoreError("UNSUPPORTED_CLASSIFICATION_SCHEMA", 409)
            db.execute(f"INSERT OR IGNORE INTO {SCHEMA_TABLE} VALUES (1)")
            kinds = f"'{RECORD_KIND}','{IDEMPOTENCY_KIND}','tag_reprocess'"
            for action in ("UPDATE", "DELETE"):
                db.execute(
                    f"""CREATE TRIGGER IF NOT EXISTS classification_v1_immutable_{action.lower()}
                    BEFORE {action} ON job_records WHEN OLD.kind IN ({kinds})
                    BEGIN SELECT RAISE(ABORT, 'classification artifact is immutable'); END"""
                )
            db.execute(
                f"""CREATE TRIGGER IF NOT EXISTS classification_v1_immutable_insert
                BEFORE INSERT ON job_records WHEN NEW.kind IN ({kinds}) AND EXISTS (
                SELECT 1 FROM job_records WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
                AND kind=NEW.kind AND record_id=NEW.record_id)
                BEGIN SELECT RAISE(ABORT, 'classification artifact already exists'); END"""
            )

    # ---------------------------------------------------------------- read side

    def _receipts_root(self, run_id: str) -> Path:
        return Path(self.store.path).parent / "tagging-receipts" / run_id

    def _lineage_token(
        self, tag_snapshot_sha256: str | None, current_record_sha256: str | None
    ) -> str | None:
        if tag_snapshot_sha256 is None:
            return None
        return canonical_hash([tag_snapshot_sha256, current_record_sha256])

    def _current_record(self, db, tenant_id, run_id, claim_id):
        raw = self.jobs._raw(db, tenant_id, run_id, HEAD_KIND, claim_id)
        return None if raw is None else json.loads(raw)

    def _outstanding_tag_jobs(self, db, tenant_id, run_id) -> list[str]:
        outstanding = []
        for record in self.jobs._all(db, tenant_id, run_id, "job"):
            message = record["message"]
            if message["stage"] == "tag" and record["status"] in {"pending", "leased"}:
                outstanding.append(message["job_id"] + ":" + record["status"])
        return sorted(outstanding)

    def view(self, actor: AuthContext, run_id: str, claim_id: str) -> dict:
        """Read-only eligibility + numbered sources + current classification state.

        Foreign-tenant or unknown claim -> RESOURCE_NOT_FOUND (404) via the tenant-
        scoped claim/tag loaders. Never enqueues and never records anything.
        """
        if not isinstance(actor, AuthContext) or not actor.has_capability("viewer"):
            raise ClassificationStoreError("FORBIDDEN", 403)
        tenant_id = actor.tenant_id
        _require_uuid("run_id", run_id)
        _require_uuid("claim_id", claim_id)
        try:
            _, discovery, graph = self.claims.load_evidence(tenant_id, run_id)
        except (KeyError, ValueError) as error:
            raise ClassificationStoreError("RESOURCE_NOT_FOUND", 404) from error
        claim = next((c for c in discovery.claims if c.claim_id == claim_id), None)
        if claim is None:
            raise ClassificationStoreError("RESOURCE_NOT_FOUND", 404)

        run = self.jobs.get_run(tenant_id, run_id)
        tag_snapshot_sha256 = run.get("tag_snapshot_sha256")

        # Numbered sources exactly as the preliminary validator will accept them.
        sources = []
        blocked_reason = None
        preliminary_agreement: dict = {}
        try:
            envelope = self.tags.load_snapshot(tenant_id, run_id)
        except (KeyError, ValueError):
            envelope = None
        if envelope is not None:
            item = next((c for c in envelope["claims"] if c["claim_id"] == claim_id), None)
            if item is not None:
                blocked_reason = item.get("reason")
                preliminary_agreement = item.get("preliminary_agreement") or {}
        try:
            sources = [
                dict(source_index=i, quote=ref.quote, source_ref=asdict(ref))
                for i, ref in enumerate(_sources(claim, graph, tenant_id))
            ]
            source_ok = True
        except ValueError:
            source_ok = False

        with self.jobs._transaction() as db:
            current = self._current_record(db, tenant_id, run_id, claim_id)
            has_head = self.jobs._raw(db, tenant_id, run_id, "claim_head", claim_id) is not None
            outstanding = self._outstanding_tag_jobs(db, tenant_id, run_id)

        ineligible_reason = None
        if has_head:
            ineligible_reason = "ALREADY_TAGGED"
        elif not source_ok or claim.source_quality != "verified":
            ineligible_reason = "SOURCE_UNVERIFIED"
        elif blocked_reason != _BLOCKED_REASON:
            ineligible_reason = "NOT_PRELIMINARY_BLOCKED"
        elif tag_snapshot_sha256 is None:
            ineligible_reason = "LINEAGE_UNAVAILABLE"
        elif outstanding:
            ineligible_reason = "RUN_JOB_OUTSTANDING"
        eligible = ineligible_reason is None

        etag = (
            None
            if ineligible_reason == "ALREADY_TAGGED"
            else self._lineage_token(
                tag_snapshot_sha256, current["record_sha256"] if current else None
            )
        )
        job_status = None
        if outstanding:
            job_status = outstanding[0].split(":", 1)[1]

        return dict(
            schema_version=1,
            run_id=run_id,
            claim_id=claim_id,
            eligible=eligible,
            ineligible_reason=ineligible_reason,
            blocked_reason=blocked_reason,
            etag=None if etag is None else f'"{etag}"',
            sources=sources,
            dimension_axes=list(REQUIRED_AXES)
            + sorted(
                (
                    set(preliminary_agreement.get("dimensions", {}))
                    | set((current or {}).get("dimensions", {}))
                )
                & ALLOWED_AXES - set(REQUIRED_AXES)
            ),
            preliminary_agreement=preliminary_agreement,
            # Frozen contract field names. The reviewed classification record stays
            # discoverable even after tag publication (ALREADY_TAGGED), preserving
            # provenance; only its editability (etag) is withdrawn.
            current_classification=current,
            pending_job=(
                dict(job_id=outstanding[0].split(":", 1)[0], status=job_status)
                if outstanding
                else None
            ),
            allowed_tracks=list(ALLOWED_TRACKS),
            allowed_safe_harbor_categories=list(ALLOWED_SAFE_HARBOR_CATEGORIES),
        )

    # --------------------------------------------------------------- write side

    def record_and_enqueue(
        self,
        actor: AuthContext,
        run_id: str,
        claim_id: str,
        body: dict,
        if_match: str,
        idempotency_key: str,
        *,
        origin: str,
        classified_by: str,
        delegation_authority: str | None,
        now: int,
    ) -> dict:
        """Atomic: CAS on lineage token, immutable record, one bounded reprocess enqueue.

        Idempotent by ``idempotency_key``: a replay with the same bytes returns the same
        record + job even after eligibility became false; a conflicting reuse is 409.
        The first-write precondition is the lineage hash (``if_match``), never an
        invented tag revision. Source-unverified or ambiguous spans are rejected.
        """
        from proofops.adapters.local.classification_reprocess import (
            ReprocessRejected,
            authorize_reprocess,
            plan_reprocess,
        )

        if not isinstance(actor, AuthContext) or not actor.has_capability("reviewer"):
            raise ClassificationStoreError("FORBIDDEN", 403)
        _require_uuid("run_id", run_id)
        _require_uuid("claim_id", claim_id)
        if not isinstance(if_match, str) or not if_match.strip():
            raise ClassificationStoreError("IF_MATCH_REQUIRED", 400)
        if re.fullmatch(r'"[a-f0-9]{64}"', if_match) is None:
            raise ClassificationStoreError("IF_MATCH_REQUIRED", 400)
        token = if_match[1:-1]
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 128:
            raise ClassificationStoreError("IDEMPOTENCY_KEY_INVALID", 400)
        tenant_id = actor.tenant_id

        try:
            _, discovery, graph = self.claims.load_evidence(tenant_id, run_id)
        except (KeyError, ValueError) as error:
            raise ClassificationStoreError("RESOURCE_NOT_FOUND", 404) from error
        claim = next((c for c in discovery.claims if c.claim_id == claim_id), None)
        if claim is None:
            raise ClassificationStoreError("RESOURCE_NOT_FOUND", 404)

        # Validate the reviewer's classification (source-verified spans only).
        classification = validate_manual_classification(claim, graph, body, tenant_id=tenant_id)

        run = self.jobs.get_run(tenant_id, run_id)
        tag_snapshot_sha256 = run.get("tag_snapshot_sha256")
        if tag_snapshot_sha256 is None:
            raise ClassificationStoreError("LINEAGE_UNAVAILABLE", 409)

        replay_key = canonical_hash([actor.user_sub, "classification_resolve", idempotency_key])
        request_hash = canonical_hash(
            [run_id, claim_id, body, token, origin, classified_by, delegation_authority]
        )

        record = classification_snapshot(
            classification,
            tenant_id=tenant_id,
            run_id=run_id,
            claim=claim,
            graph=graph,
            lineage_checkpoint_sha256=tag_snapshot_sha256,
            origin=origin,
            classified_by=classified_by,
            reason=body["reason"],
            delegation_authority=delegation_authority,
        )
        classification_id = str(record_id_from(record))
        record["classification_id"] = classification_id
        record["created_at"] = datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")
        record["record_sha256"] = canonical_hash(
            {k: v for k, v in record.items() if k != "record_sha256"}
        )

        # Fast idempotent replay before rechecking eligibility, including after publication.
        with self.jobs._transaction() as db:
            replay_raw = self.jobs._raw(db, tenant_id, run_id, IDEMPOTENCY_KIND, replay_key)
            if replay_raw is not None:
                replay = json.loads(replay_raw)
                if replay["request_hash"] != request_hash:
                    raise ClassificationStoreError("IDEMPOTENCY_CONFLICT", 409)
                return replay["response"]
        try:
            plan, plan_run = plan_reprocess(
                self.store,
                self.tags,
                tenant_id=tenant_id,
                run_id=run_id,
                claim_id=claim_id,
                classification_id=classification_id,
                classification_sha256=record["record_sha256"],
                receipts=self._receipts_root(run_id),
                authorized_by=classified_by,
                now=now,
            )
            if plan.lineage_checkpoint_sha256 != tag_snapshot_sha256:
                raise ClassificationStoreError("STALE_CLASSIFICATION", 412)
            item = next(
                c
                for c in self.tags.load_snapshot(tenant_id, run_id)["claims"]
                if c["claim_id"] == claim_id
            )
            required_axes = set(REQUIRED_AXES) | set(
                (item.get("preliminary_agreement") or {}).get("dimensions", {})
            )
            if not required_axes <= set(body["dimensions"]):
                raise ClassificationStoreError("VALIDATION_ERROR")
            with self.jobs._transaction() as db:
                replay_raw = self.jobs._raw(db, tenant_id, run_id, IDEMPOTENCY_KIND, replay_key)
                if replay_raw is not None:
                    replay = json.loads(replay_raw)
                    if replay["request_hash"] != request_hash:
                        raise ClassificationStoreError("IDEMPOTENCY_CONFLICT", 409)
                    return replay["response"]
                current = self._current_record(db, tenant_id, run_id, claim_id)
                current_run = self.jobs._get(db, tenant_id, run_id, "run", "META")
                expected_token = self._lineage_token(
                    current_run.get("tag_snapshot_sha256"),
                    current["record_sha256"] if current else None,
                )
                if token != expected_token:
                    raise ClassificationStoreError("STALE_CLASSIFICATION", 412)
                self.jobs._put(
                    db, tenant_id, run_id, RECORD_KIND, classification_id, record, immutable=True
                )
                self.jobs._put(db, tenant_id, run_id, HEAD_KIND, claim_id, record)
                message = authorize_reprocess(self.store, plan, plan_run, db=db, now=now)
                response = dict(
                    schema_version=1,
                    classification={
                        key: record[key]
                        for key in (
                            "classification_id",
                            "claim_id",
                            "run_id",
                            "track",
                            "safe_harbor_category",
                            "revision",
                            "origin",
                            "classified_by",
                        )
                    },
                    reprocess_job=dict(
                        job_id=message.job_id, status="pending", claim_ids=[claim_id]
                    ),
                )
                self.jobs._put(
                    db,
                    tenant_id,
                    run_id,
                    IDEMPOTENCY_KIND,
                    replay_key,
                    dict(request_hash=request_hash, response=response),
                    immutable=True,
                )
                return response
        except ReprocessRejected as error:
            raise ClassificationStoreError(error.code, error.status) from error

    def classify_ai_delegated(
        self,
        actor: AuthContext,
        run_id: str,
        claim_id: str,
        body: dict,
        if_match: str,
        idempotency_key: str,
        *,
        delegated_reviewer: str,
        delegation_authority: str,
        now: int,
    ) -> dict:
        """Trusted local-only AI-delegated classification (never reachable over HTTP).

        Records distinct ``ai_delegated_classification`` provenance
        (``classified_by=ai-delegated-classification:<operator>``) and the verbatim
        delegation authority; identical source/lineage/idempotency guards as the human
        route. Not accepted as a human gold classification.
        """
        from proofops.application.tagging.manual_classification import (
            AI_DELEGATED_ORIGIN,
            AI_DELEGATED_REVIEWER_PREFIX,
        )

        if not isinstance(delegated_reviewer, str) or not delegated_reviewer.strip():
            raise ClassificationStoreError("VALIDATION_ERROR")
        if not isinstance(delegation_authority, str) or not delegation_authority.strip():
            raise ClassificationStoreError("VALIDATION_ERROR")
        reviewer = delegated_reviewer.strip()
        if len(reviewer) > 320 or "\n" in reviewer:
            raise ClassificationStoreError("VALIDATION_ERROR")
        return self.record_and_enqueue(
            actor,
            run_id,
            claim_id,
            body,
            if_match,
            idempotency_key,
            origin=AI_DELEGATED_ORIGIN,
            classified_by=f"{AI_DELEGATED_REVIEWER_PREFIX}{reviewer}",
            delegation_authority=delegation_authority,
            now=now,
        )


def record_id_from(record: dict) -> str:
    """Content-addressed classification id (uuid5 over the pinned record body)."""
    from uuid import NAMESPACE_URL, uuid5

    return str(
        uuid5(
            NAMESPACE_URL,
            canonical_hash(
                {
                    k: v
                    for k, v in record.items()
                    if k not in ("record_sha256", "classification_id", "created_at")
                }
            ),
        )
    )


__all__ = [
    "ALLOWED_SAFE_HARBOR_CATEGORIES",
    "ALLOWED_TRACKS",
    "ClassificationStoreError",
    "LocalSQLiteClassificationStore",
]
