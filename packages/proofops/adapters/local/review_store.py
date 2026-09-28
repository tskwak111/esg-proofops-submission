"""Local review persistence in the existing job_records transaction.

Schema v1 adds kinds, immutable triggers and a version marker only. Existing
readers ignore these kinds; rollback stops review writers/routes and retains all
records (no destructive down migration). Re-running initialization is idempotent.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from uuid import uuid4

from proofops.adapters.local.audit_store import append_audit_transaction, read_audit_head
from proofops.adapters.local.job_store import LocalSQLiteJobStore
from proofops.application.authorization import AuthContext
from proofops.application.reviews import ReviewRejected
from proofops.domain.audit import ChangeSet
from proofops.domain.provenance import canonical_hash


class LocalSQLiteReviewStore:
    kind = "local-synthetic-only"

    def __init__(self, jobs: LocalSQLiteJobStore):
        self.jobs = jobs
        with jobs._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS review_schema (version INTEGER PRIMARY KEY)")
            versions = db.execute("SELECT version FROM review_schema").fetchall()
            if versions and versions != [(1,)]:
                raise ReviewRejected("UNSUPPORTED_REVIEW_SCHEMA", 409)
            db.execute("INSERT OR IGNORE INTO review_schema VALUES (1)")
            kinds = (
                "'review_revision','review_inputs','tag_revision',"
                "'decision_revision','review_idempotency'"
            )
            for action in ("UPDATE", "DELETE"):
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS review_immutable_{action.lower()}
                    BEFORE {action} ON job_records WHEN OLD.kind IN ({kinds})
                    BEGIN SELECT RAISE(ABORT, 'review artifact is immutable'); END""")
            # INSERT OR REPLACE must not bypass append-only update/delete guards.
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS review_immutable_insert
                BEFORE INSERT ON job_records WHEN NEW.kind IN ({kinds}) AND EXISTS (
                SELECT 1 FROM job_records WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
                AND kind=NEW.kind AND record_id=NEW.record_id)
                BEGIN SELECT RAISE(ABORT, 'review artifact already exists'); END""")

    def _lookup(self, db, tenant, review_id):
        rows = db.execute(
            """SELECT value FROM job_records
            WHERE tenant_id=? AND kind='review_head' AND record_id=?""",
            (tenant, review_id),
        ).fetchall()
        if len(rows) != 1:
            raise ReviewRejected("RESOURCE_NOT_FOUND", 404)
        review = json.loads(rows[0][0])
        self._run(db, tenant, review["run_id"])
        return review

    def _source_schema(self, db):
        db.execute(
            "CREATE TABLE IF NOT EXISTS source_condition_schema (version INTEGER PRIMARY KEY)"
        )
        versions = db.execute("SELECT version FROM source_condition_schema").fetchall()
        if versions and versions != [(1,)]:
            raise ReviewRejected("UNSUPPORTED_SOURCE_CONDITION_SCHEMA", 409)
        db.execute("INSERT OR IGNORE INTO source_condition_schema VALUES (1)")
        kinds = (
            "'source_condition_inputs','source_condition_revision','source_condition_idempotency',"
            "'numeric_check_receipt','source_view_receipt'"
        )
        for action in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS source_condition_immutable_{action.lower()}
                BEFORE {action} ON job_records WHEN OLD.kind IN ({kinds})
                BEGIN SELECT RAISE(ABORT, 'source condition artifact is immutable'); END""")
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS source_condition_immutable_insert
            BEFORE INSERT ON job_records WHEN NEW.kind IN ({kinds}) AND EXISTS (
            SELECT 1 FROM job_records WHERE tenant_id=NEW.tenant_id AND run_id=NEW.run_id
            AND kind=NEW.kind AND record_id=NEW.record_id)
            BEGIN SELECT RAISE(ABORT, 'source condition artifact already exists'); END""")

    def publish_source_conditions(self, inputs, *, expected_epoch=None):
        """Persist a trusted loader snapshot, not source or condition approval."""
        tenant, run_id = inputs["tenant_id"], inputs["run_id"]
        with self.jobs._transaction() as db:
            self._source_schema(db)
            run = self._run(db, tenant, run_id)
            if expected_epoch is not None and (
                type(expected_epoch) is not int or run["mutation_epoch"] != expected_epoch
            ):
                raise ReviewRejected("STALE_REVIEW_REVISION", 412)
            if inputs["document_version_id"] != run["document_version_id"]:
                raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
            digest = canonical_hash(inputs)
            prior = self.jobs._raw(db, tenant, run_id, "source_condition_head", "HEAD")
            if prior:
                current = self._source_record(db, tenant, run_id)
                if current["source_snapshot_sha256"] != digest:
                    raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
                return current
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            initial = dict(
                schema="source_condition_review_v1",
                tenant_id=tenant,
                run_id=run_id,
                document_version_id=run["document_version_id"],
                source_snapshot_sha256=digest,
                revision=1,
                parent_sha256=None,
                actor_sub="system:source-condition-publisher",
                created_at=now,
                state={
                    name: {}
                    for name in (
                        "classifications",
                        "citations",
                        "ownership",
                        "conditions",
                        "claim_bindings",
                    )
                },
            )
            initial["revision_sha256"] = canonical_hash(initial)
            self.jobs._put(
                db, tenant, run_id, "source_condition_inputs", "INPUTS", inputs, immutable=True
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "source_condition_revision",
                "0000000001",
                initial,
                immutable=True,
            )
            self.jobs._put(db, tenant, run_id, "source_condition_head", "HEAD", initial)
            append_audit_transaction(
                connection=db,
                change=ChangeSet(
                    tenant,
                    run_id,
                    initial["actor_sub"],
                    "source_condition_published",
                    run_id,
                    None,
                    initial["revision_sha256"],
                    1,
                    None,
                ),
                expected_head=read_audit_head(db, tenant, run_id),
                event_id=str(uuid4()),
                timestamp=now,
            )
            self.jobs._bump_run(db, run)
            return initial

    def source_conditions(self, tenant, run_id, *, revision=None):
        with self.jobs._transaction() as db:
            return self._source_record(db, tenant, run_id, revision)

    def _source_record(self, db, tenant, run_id, revision=None):
        run = self._run(db, tenant, run_id)
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_condition_schema'"
        ).fetchone():
            raise ReviewRejected("SOURCE_REVIEW_NOT_PUBLISHED", 409)
        if db.execute("SELECT version FROM source_condition_schema").fetchall() != [(1,)]:
            raise ReviewRejected("UNSUPPORTED_SOURCE_CONDITION_SCHEMA", 409)
        if revision is not None and (type(revision) is not int or revision < 1):
            raise ReviewRejected("VALIDATION_ERROR", 422)
        kind, key = (
            ("source_condition_head", "HEAD")
            if revision is None
            else ("source_condition_revision", f"{revision:010}")
        )
        raw = self.jobs._raw(db, tenant, run_id, kind, key)
        if raw is None:
            raise ReviewRejected("SOURCE_REVIEW_NOT_PUBLISHED", 409)
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409) from None
        if not isinstance(result, dict) or type(result.get("revision")) is not int:
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
        latest = db.execute(
            "SELECT MAX(record_id) FROM job_records WHERE tenant_id=? AND run_id=? "
            "AND kind='source_condition_revision'",
            (tenant, run_id),
        ).fetchone()[0]
        if result["revision"] < 1 or (
            f"{result['revision']:010}" != (latest if revision is None else key)
        ):
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
        inputs = self.jobs._get(db, tenant, run_id, "source_condition_inputs", "INPUTS")
        backing = self.jobs._raw(
            db, tenant, run_id, "source_condition_revision", f"{result['revision']:010}"
        )
        if (
            result.get("schema") != "source_condition_review_v1"
            or (result.get("tenant_id"), result.get("run_id"), result.get("document_version_id"))
            != (tenant, run_id, run["document_version_id"])
            or canonical_hash(inputs) != result.get("source_snapshot_sha256")
            or canonical_hash({k: v for k, v in result.items() if k != "revision_sha256"})
            != result.get("revision_sha256")
            or backing != raw
        ):
            raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
        return result

    @staticmethod
    def _source_request(actor, body, expected, key):
        if not isinstance(actor, AuthContext) or not actor.has_capability("reviewer"):
            raise ReviewRejected("FORBIDDEN", 403)
        if (
            type(expected) is not int
            or expected < 1
            or not isinstance(key, str)
            or not 16 <= len(key) <= 128
        ):
            raise ReviewRejected("VALIDATION_ERROR", 400)
        if (
            not isinstance(body, dict)
            or type(body.get("base_source_revision")) is not int
            or body["base_source_revision"] < 1
            or not isinstance(body.get("reason"), str)
            or not 5 <= len(body["reason"].strip()) <= 1000
        ):
            raise ReviewRejected("VALIDATION_ERROR", 422)

    def source_condition_retry(self, actor, run_id, body, expected, key):
        self._source_request(actor, body, expected, key)
        with self.jobs._transaction() as db:
            self._source_record(db, actor.tenant_id, run_id, revision=1)
            return self._source_retry(db, actor, run_id, body, expected, key)

    def _source_retry(self, db, actor, run_id, body, expected, key):
        replay_key = canonical_hash([actor.user_sub, "source_condition_resolve", key])
        row = db.execute(
            "SELECT value FROM job_records WHERE tenant_id=? "
            "AND kind='source_condition_idempotency' AND record_id=?",
            (actor.tenant_id, replay_key),
        ).fetchone()
        if row:
            replay = json.loads(row[0])
            if replay["request_hash"] != canonical_hash([run_id, body, expected]):
                raise ReviewRejected("IDEMPOTENCY_CONFLICT", 409)
            return replay["response"]
        return None

    def resolve_source_conditions(
        self, actor, run_id, body, expected, key, build, *, expected_epoch=None
    ):
        """Atomic persistence boundary; build validates factual upserts, never HTTP outcomes.

        No source issue, claim tag or grade is changed here. The application must
        supply source-replayed inputs and validate the complete effective state.
        """
        self._source_request(actor, body, expected, key)
        tenant = actor.tenant_id
        with self.jobs._transaction() as db:
            self._source_schema(db)
            run = self._run(db, tenant, run_id)
            replay_key = canonical_hash([actor.user_sub, "source_condition_resolve", key])
            request_hash = canonical_hash([run_id, body, expected])
            replay = self._source_retry(db, actor, run_id, body, expected, key)
            if replay is not None:
                return replay
            if expected_epoch is not None and (
                type(expected_epoch) is not int or run["mutation_epoch"] != expected_epoch
            ):
                raise ReviewRejected("STALE_REVIEW_REVISION", 412)
            current = self._source_record(db, tenant, run_id)
            if current["revision"] != expected or body.get("base_source_revision") != expected:
                raise ReviewRejected("STALE_REVIEW_REVISION", 412)
            if body.get("source_snapshot_sha256") != current["source_snapshot_sha256"]:
                raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
            state = build(json.loads(json.dumps(current["state"])))
            if (
                not isinstance(state, dict)
                or set(state) != set(current["state"])
                or any(not isinstance(value, dict) for value in state.values())
            ):
                raise ReviewRejected("VALIDATION_ERROR", 422)
            for category in ("citations", "classifications"):
                for annotation in state[category].values():
                    receipt = (
                        annotation.get("source_view_receipt")
                        if isinstance(annotation, dict)
                        else None
                    )
                    if receipt is None:
                        continue
                    identifier = canonical_hash(receipt)
                    prior = self.jobs._raw(db, tenant, run_id, "source_view_receipt", identifier)
                    if prior is None:
                        self.jobs._put(
                            db,
                            tenant,
                            run_id,
                            "source_view_receipt",
                            identifier,
                            receipt,
                            immutable=True,
                        )
                    elif canonical_hash(json.loads(prior)) != identifier:
                        raise ReviewRejected("SOURCE_REVIEW_INPUT_MISMATCH", 409)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            successor = current | dict(
                revision=expected + 1,
                parent_sha256=current["revision_sha256"],
                state=state,
                actor_sub=actor.user_sub,
                created_at=now,
                reason=body["reason"],
            )
            del successor["revision_sha256"]
            successor["revision_sha256"] = canonical_hash(successor)
            self.jobs._put(
                db,
                tenant,
                run_id,
                "source_condition_revision",
                f"{expected + 1:010}",
                successor,
                immutable=True,
            )
            self.jobs._put(db, tenant, run_id, "source_condition_head", "HEAD", successor)
            append_audit_transaction(
                connection=db,
                change=ChangeSet(
                    tenant,
                    run_id,
                    actor.user_sub,
                    "source_condition_reviewed",
                    run_id,
                    current["revision_sha256"],
                    successor["revision_sha256"],
                    expected + 1,
                    body["reason"],
                ),
                expected_head=read_audit_head(db, tenant, run_id),
                event_id=str(uuid4()),
                timestamp=now,
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "source_condition_idempotency",
                replay_key,
                dict(request_hash=request_hash, response=successor),
                immutable=True,
            )
            self.jobs._bump_run(db, run)
            return successor

    def _run(self, db, tenant, run_id):
        try:
            return self.jobs._get(db, tenant, run_id, "run", "META")
        except KeyError:
            raise ReviewRejected("RESOURCE_NOT_FOUND", 404) from None

    def get(self, tenant_id, review_id):
        with self.jobs._transaction() as db:
            return self._lookup(db, tenant_id, review_id)

    def publish(self, inputs, review):
        with self.jobs._transaction() as db:
            tenant, run_id = inputs.context.claim.tenant_id, inputs.run_id
            previous = self.jobs._raw(db, tenant, run_id, "review_head", review["review_id"])
            result = self.publish_transaction(db, inputs, review)
            if previous is None:
                self.jobs._bump_run(db, self._run(db, tenant, run_id))
            return result

    def _validate_live_publication(self, db, inputs):
        from proofops.adapters.local.tag_store import tagging_settings

        try:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_snapshots'"
            ).fetchone():
                raise ValueError("missing run snapshot")
            row = db.execute(
                "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?",
                (inputs.context.claim.tenant_id, inputs.run_id),
            ).fetchone()
            frozen = json.loads(row[0]) if row else {}
            settings = tagging_settings(frozen)
            packet = inputs.packet.to_dict()
            prompt_hash = canonical_hash(
                settings.system_for_track(packet["track"], packet["safe_harbor_category"])
            )
            if (
                frozen.get("tagging_mode") != "upstage_local"
                or canonical_hash({k: v for k, v in frozen.items() if k != "input_hash"})
                != frozen.get("input_hash")
                or frozen["tenant_id"] != inputs.original.tenant_id
                or frozen["run_id"] != inputs.run_id
                or frozen["document"]["version_id"] != inputs.original.document_version_id
                or frozen["document"]["sha256"] != inputs.original.source_sha256
                or canonical_hash(frozen["rulepack"]) != canonical_hash(asdict(inputs.rulepack))
                or frozen.get("report_level_link") != inputs.report_level_link
                or frozen.get("rulepack_use")
                not in {"candidate_tagging_reference_only", "approved_grading"}
                or (
                    frozen["rulepack_use"] == "candidate_tagging_reference_only"
                    and inputs.decision is not None
                )
                or not inputs.tag_runs
                or any(
                    receipt.synthetic is not False
                    or receipt.model_sha256 != settings.model_sha256
                    or receipt.prompt_sha256 != prompt_hash
                    for receipt in inputs.tag_runs
                )
            ):
                raise ValueError("live publication pins mismatch")
        except (KeyError, TypeError, ValueError):
            raise ReviewRejected("REVIEW_INPUT_MISMATCH", 409) from None

    def publish_transaction(self, db, inputs, review):
        """Join the fenced tag checkpoint transaction; no commit or epoch bump."""
        if not db.in_transaction:
            raise ValueError("active transaction required")
        tenant, run_id, claim_id = inputs.context.claim.tenant_id, inputs.run_id, review["claim_id"]
        snapshot = inputs.snapshot()
        run = self._run(db, tenant, run_id)
        if run["document_version_id"] != inputs.original.document_version_id:
            raise ReviewRejected("REVIEW_INPUT_MISMATCH", 409)
        if not inputs.rule_context.local_synthetic:
            self._validate_live_publication(db, inputs)
        prior = self.jobs._raw(db, tenant, run_id, "review_head", review["review_id"])
        if prior:
            stored = self.jobs._get(db, tenant, run_id, "review_inputs", review["review_id"])
            if canonical_hash(stored) != canonical_hash(snapshot):
                raise ReviewRejected("REVIEW_PUBLICATION_CONFLICT", 409)
            return json.loads(prior)
        head = self.jobs._raw(db, tenant, run_id, "claim_head", claim_id)
        if head is not None:
            raise ReviewRejected("REVIEW_ALREADY_PUBLISHED", 409)
        tag = dict(
            tag_revision=inputs.tag_revision,
            confirmed_tags=asdict(inputs.consensus.confirmed_tags)
            if inputs.consensus.confirmed_tags
            else None,
            elements=[asdict(e) for e in inputs.consensus.candidate_elements],
            origin="consensus",
            inputs=snapshot,
        )
        if "fact_assembly" in snapshot:
            tag["fact_assembly"] = snapshot["fact_assembly"]
        if "report_level_link" in snapshot:
            tag["report_level_link"] = {
                "policy": snapshot["report_level_link"]["policy"],
                "policy_hash": snapshot["report_level_link"]["policy_hash"],
            }
            tag["report_level_review"] = snapshot["report_level_review"]
        decision_revision = inputs.decision.decision_revision if inputs.decision else 0
        self.jobs._put(
            db, tenant, run_id, "review_inputs", review["review_id"], snapshot, immutable=True
        )
        self.jobs._put(
            db,
            tenant,
            run_id,
            "tag_revision",
            f"{claim_id}:{inputs.tag_revision:010}",
            tag,
            immutable=True,
        )
        if inputs.decision:
            self.jobs._put(
                db,
                tenant,
                run_id,
                "decision_revision",
                f"{claim_id}:{decision_revision:010}",
                dict(
                    decision_revision=decision_revision,
                    decision=asdict(inputs.decision),
                    api=inputs.decision.to_api_dict(),
                    **(
                        {"fact_assembly": snapshot["fact_assembly"]}
                        if "fact_assembly" in snapshot
                        else {}
                    ),
                ),
                immutable=True,
            )
        self.jobs._put(
            db,
            tenant,
            run_id,
            "claim_head",
            claim_id,
            dict(tag_revision=inputs.tag_revision, decision_revision=decision_revision),
        )
        self.jobs._put(db, tenant, run_id, "review_head", review["review_id"], review)
        self.jobs._put(
            db,
            tenant,
            run_id,
            "review_revision",
            f'{review["review_id"]}:0000000001',
            review,
            immutable=True,
        )
        append_audit_transaction(
            connection=db,
            change=ChangeSet(
                tenant,
                run_id,
                "system:tagger",
                "review_opened",
                review["review_id"],
                None,
                canonical_hash(tag),
                1,
                "Tagging queued for human review.",
            ),
            expected_head=read_audit_head(db, tenant, run_id),
            event_id=str(uuid4()),
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
        return review

    def resolve(self, actor, review_id, body, expected, key, build, *, reopen=False):
        with self.jobs._transaction() as db:
            review = self._lookup(db, actor.tenant_id, review_id)
            tenant, run_id, claim_id = actor.tenant_id, review["run_id"], review["claim_id"]
            replay_key = canonical_hash([actor.user_sub, "review_resolve", key])
            # Preserve the exact legacy identity for the ordinary resolve so old
            # stored idempotency receipts still replay byte-for-byte. Only the
            # new explicit re-review action extends the identity with a marker.
            request_hash = canonical_hash(
                [review_id, body, expected, "reopen"] if reopen else [review_id, body, expected]
            )
            replay_row = db.execute(
                """SELECT value FROM job_records WHERE tenant_id=?
                AND kind='review_idempotency' AND record_id=?""",
                (tenant, replay_key),
            ).fetchone()
            replay = replay_row[0] if replay_row else None
            if replay:
                previous = json.loads(replay)
                if previous["request_hash"] != request_hash:
                    raise ReviewRejected("IDEMPOTENCY_CONFLICT", 409)
                return previous["response"]
            head = self.jobs._get(db, tenant, run_id, "claim_head", claim_id)
            # Two explicit lifecycle transitions, both strict-CAS on the review
            # revision and both anchored to the CURRENT tag head (never a stale
            # base). A resolved review can only be improved through an explicit
            # ``reopen`` action, never a silent second resolve; an open review can
            # only be resolved through the ordinary (non-reopen) path.
            if reopen:
                valid = (
                    review["revision"] == expected
                    and review["status"] == "resolved"
                    and body["base_tag_revision"] == head["tag_revision"]
                )
            else:
                valid = (
                    review["revision"] == expected
                    and review["status"] == "open"
                    and body["base_tag_revision"] == review["base_tag_revision"]
                    and head["tag_revision"] == body["base_tag_revision"]
                )
            if not valid:
                raise ReviewRejected("STALE_REVIEW_REVISION", 412)
            initial = self.jobs._get(
                db, tenant, run_id, "tag_revision", f'{claim_id}:{head["tag_revision"]:010}'
            )
            tag, decision = build(review, initial, head["decision_revision"] + 1)
            now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
            tag["created_at"] = now
            resolved = review | {"status": "resolved", "revision": expected + 1}
            response = dict(
                review=resolved, decision=decision["api"], new_tag_revision=tag["tag_revision"]
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "tag_revision",
                f'{claim_id}:{tag["tag_revision"]:010}',
                tag,
                immutable=True,
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "decision_revision",
                f'{claim_id}:{decision["decision_revision"]:010}',
                decision,
                immutable=True,
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "review_revision",
                f"{review_id}:{expected + 1:010}",
                dict(
                    review=resolved,
                    resolution=response,
                    resolved_by=actor.user_sub,
                    resolved_at=now,
                ),
                immutable=True,
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "claim_head",
                claim_id,
                dict(
                    tag_revision=tag["tag_revision"],
                    decision_revision=decision["decision_revision"],
                ),
            )
            self.jobs._put(db, tenant, run_id, "review_head", review_id, resolved)
            self.jobs._bump_run(db, self._run(db, tenant, run_id), refresh_claim_counts=True)
            append_audit_transaction(
                connection=db,
                change=ChangeSet(
                    tenant,
                    run_id,
                    actor.user_sub,
                    "review_rereviewed" if reopen else "review_resolved",
                    review_id,
                    canonical_hash(initial),
                    canonical_hash(dict(tag=tag, decision=decision)),
                    expected + 1,
                    body["reason"],
                ),
                expected_head=read_audit_head(db, tenant, run_id),
                event_id=str(uuid4()),
                timestamp=now,
            )
            self.jobs._put(
                db,
                tenant,
                run_id,
                "review_idempotency",
                replay_key,
                dict(request_hash=request_hash, response=response),
                immutable=True,
            )
            return response

    def page(self, tenant_id, run_id, *, cursor, limit, now, cursors):
        from proofops.adapters.local.catalog_pages import initialize, page

        with self.jobs._transaction() as db:
            run = self._run(db, tenant_id, run_id)
            initialize(db)
            return page(
                db,
                tenant_id=tenant_id,
                endpoint="reviews",
                query={"run_id": run_id},
                cursor=cursor,
                limit=limit,
                now=now,
                load_items=lambda: dict(
                    items=(
                        json.loads(row[0])
                        for row in db.execute(
                            "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? "
                            "AND kind='review_head' ORDER BY record_id",
                            (tenant_id, run_id),
                        )
                    ),
                    snapshot_epoch=run["mutation_epoch"],
                ),
            )

    def history(self, tenant_id, run_id, claim_id):
        with self.jobs._transaction() as db:
            self._run(db, tenant_id, run_id)

            def revisions(kind):
                return [
                    json.loads(row[0])
                    for row in db.execute(
                        """SELECT value FROM job_records WHERE tenant_id=? AND run_id=? AND kind=?
                    AND record_id LIKE ? ORDER BY record_id""",
                        (tenant_id, run_id, kind, f"{claim_id}:%"),
                    )
                ]

            return dict(tags=revisions("tag_revision"), decisions=revisions("decision_revision"))
