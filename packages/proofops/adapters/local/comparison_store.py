"""Immutable local-synthetic year-comparison receipts (TASK-023)."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date
from pathlib import Path
from uuid import uuid4

from proofops.adapters.local.summary_store import LocalSummaryStore
from proofops.application.comparisons import (
    ApprovedVersion,
    Comparison,
    TargetSnapshot,
    compare_years,
)
from proofops.application.uploads_security import UploadRejected
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_uuid


class ComparisonRejected(ValueError):
    def __init__(self, code: str, status: int = 409) -> None:
        super().__init__(code)
        self.code, self.status = code, status


class LocalComparisonStore:
    """Compare frozen local artifacts; never call a model or change claim heads."""

    kind = "local-synthetic-only"

    def __init__(self, runs, uploads, claims, *, enabled: bool = False) -> None:
        if type(enabled) is not bool:
            raise ValueError("enabled must be boolean")
        if Path(runs.path).resolve() != Path(claims.store.path).resolve():
            raise ValueError("runs and claims must share one local database")
        self.runs, self.uploads, self.claims, self.jobs = runs, uploads, claims, runs.jobs
        self.summaries = LocalSummaryStore(runs, claims)
        self.enabled = enabled
        with self.jobs._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS comparison_schema (version INTEGER PRIMARY KEY)")
            versions = db.execute("SELECT version FROM comparison_schema").fetchall()
            if versions and versions != [(1,)]:
                raise ComparisonRejected("UNSUPPORTED_COMPARISON_SCHEMA")
            db.execute("INSERT OR IGNORE INTO comparison_schema VALUES (1)")
            kinds = "'comparison_request','comparison_receipt'"
            for action in ("UPDATE", "DELETE"):
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS comparison_no_{action.lower()}
                    BEFORE {action} ON job_records WHEN OLD.kind IN ({kinds})
                    BEGIN SELECT RAISE(ABORT, 'immutable comparison receipt'); END""")
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS comparison_no_duplicate
                BEFORE INSERT ON job_records WHEN NEW.kind IN ({kinds}) AND EXISTS (
                SELECT 1 FROM job_records WHERE tenant_id=NEW.tenant_id
                AND run_id=NEW.run_id AND kind=NEW.kind AND record_id=NEW.record_id)
                BEGIN SELECT RAISE(ABORT, 'comparison receipt exists'); END""")

    @staticmethod
    def _validate_body(body, key) -> str:
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise ComparisonRejected("IDEMPOTENCY_KEY_INVALID", 400)
        if not isinstance(body, dict) or set(body) != {"prior_document_version_id"}:
            raise ComparisonRejected("VALIDATION_ERROR", 422)
        try:
            _require_uuid("prior_document_version_id", body["prior_document_version_id"])
        except ValueError:
            raise ComparisonRejected("VALIDATION_ERROR", 422) from None
        return body["prior_document_version_id"]

    @staticmethod
    def _periods_compatible(current: dict, prior: dict) -> bool:
        try:
            current_start = date.fromisoformat(current["metadata"]["period_start"])
            current_end = date.fromisoformat(current["metadata"]["period_end"])
            prior_start = date.fromisoformat(prior["metadata"]["period_start"])
            prior_end = date.fromisoformat(prior["metadata"]["period_end"])
        except (KeyError, TypeError, ValueError):
            return False
        return (
            current["document_type"] == prior["document_type"]
            and (current_start.month, current_start.day, current_end.month, current_end.day)
            == (prior_start.month, prior_start.day, prior_end.month, prior_end.day)
            and current_start.year == current["report_year"]
            and current_end.year == current["report_year"]
            and prior_start.year == prior["report_year"]
            and prior_end.year == prior["report_year"]
        )

    def _version(self, tenant: str, version_id: str) -> dict:
        try:
            visible = self.uploads.get_version(tenant, version_id)
            version = self.uploads.version_snapshot(tenant, version_id)
        except (KeyError, UploadRejected):
            raise ComparisonRejected("RESOURCE_NOT_FOUND", 404) from None
        if (
            visible.get("version_id") != version.get("version_id")
            or version.get("status") != "ready"
            or version.get("local_synthetic") is not True
        ):
            raise ComparisonRejected("COMPARISON_SOURCE_UNAVAILABLE")
        return version

    def _run_for_version(self, db, tenant: str, version_id: str) -> tuple[str, dict] | None:
        matches = []
        for run_id, raw in db.execute(
            "SELECT run_id,value FROM job_records WHERE tenant_id=? "
            "AND kind='run' AND record_id='META'",
            (tenant,),
        ):
            run = json.loads(raw)
            if run.get("document_version_id") == version_id and "extract_job" in run:
                matches.append((run_id, run))
        if len(matches) > 1:
            raise ComparisonRejected("PRIOR_COMPARISON_ARTIFACT_AMBIGUOUS")
        return matches[0] if matches else None

    def _replay(self, db, tenant, run_id, request_key, request_hash):
        raw = self.jobs._raw(db, tenant, run_id, "comparison_request", request_key)
        if raw is None:
            return None
        request = json.loads(raw)
        if request.get("request_hash") != request_hash:
            raise ComparisonRejected("IDEMPOTENCY_CONFLICT")
        receipt = self.jobs._get(db, tenant, run_id, "comparison_receipt", request["comparison_id"])
        self._verify_access(tenant, receipt)
        return request["response"]

    @staticmethod
    def _verify_receipt(receipt: dict) -> None:
        if receipt.get("execution_profile") != LocalComparisonStore.kind or receipt.get(
            "sha256"
        ) != canonical_hash(receipt.get("payload")):
            raise ComparisonRejected("COMPARISON_INTEGRITY_FAILED")

    def _verify_access(self, tenant: str, receipt: dict) -> None:
        self._verify_receipt(receipt)
        payload = receipt.get("payload")
        if not isinstance(payload, dict):
            raise ComparisonRejected("COMPARISON_INTEGRITY_FAILED")
        try:
            version_ids = (
                payload["current_document_version_id"],
                payload["prior_document_version_id"],
            )
        except KeyError:
            raise ComparisonRejected("COMPARISON_INTEGRITY_FAILED") from None
        for version_id in version_ids:
            self._version(tenant, version_id)

    def _targets(self, db, tenant: str, run_id: str, version: dict, claims) -> tuple:
        targets = []
        run = self.jobs._get(db, tenant, run_id, "run", "META")
        for claim in claims:
            if claim.tenant_id != tenant or claim.document_version_id != version["version_id"]:
                raise ComparisonRejected("COMPARISON_SOURCE_UNVERIFIED")
            current = self.claims.current_tag(tenant, run_id, claim.claim_id, connection=db)
            if (
                current is None
                or current["decision"] is None
                or current["epoch"] != run["mutation_epoch"]
            ):
                raise ComparisonRejected("APPROVED_CLAIM_SNAPSHOT_MISSING")
            tag, decision = current["tag"], current["decision"]
            confirmed = tag.get("confirmed_tags")
            head = self.jobs._get(db, tenant, run_id, "claim_head", claim.claim_id)
            decision_record = self.jobs._get(
                db,
                tenant,
                run_id,
                "decision_revision",
                f'{claim.claim_id}:{head["decision_revision"]:010}',
            )
            snapshot = self.runs._snapshot(db, tenant, run_id)
            if (
                tag.get("origin") != "human"
                or not isinstance(confirmed, dict)
                or confirmed.get("tenant_id") != tenant
                or confirmed.get("document_version_id") != version["version_id"]
                or confirmed.get("claim_id") != claim.claim_id
                or confirmed.get("tag_revision") != head["tag_revision"]
                or not isinstance(confirmed.get("facts"), list)
                or decision.get("review_status") != "human_confirmed"
                or not self.summaries._decision_has_valid_lineage(
                    db, tenant, run_id, claim.claim_id, head, decision_record, tag, snapshot
                )
            ):
                raise ComparisonRejected("APPROVED_CLAIM_SNAPSHOT_MISSING")
            for fact in confirmed.get("facts", []):
                if not isinstance(fact, dict):
                    raise ComparisonRejected("APPROVED_CLAIM_SNAPSHOT_MISSING")
                refs = fact.get("evidence_refs", [])
                if fact.get("state") == "present" and (
                    not refs
                    or fact.get("source_tenant_id") != tenant
                    or any(
                        not isinstance(ref, dict)
                        or ref.get("document_version_id") != version["version_id"]
                        or ref.get("verification_state") != "verified"
                        for ref in refs
                    )
                ):
                    raise ComparisonRejected("COMPARISON_SOURCE_UNVERIFIED")
            if confirmed.get("track") != "goal":
                continue
            if not claim.topic_ids:
                raise ComparisonRejected("TARGET_COMPARISON_KEY_MISSING")
            targets.append(
                TargetSnapshot(
                    claim_id=claim.claim_id,
                    comparison_key="|".join(sorted(claim.topic_ids)),
                    content_sha256=canonical_hash(
                        {"quote": claim.quote, "topic_ids": sorted(claim.topic_ids)}
                    ),
                    decision_revision=head["decision_revision"],
                    evidence_grade=decision_record["decision"].get("evidence_grade"),
                )
            )
        return tuple(targets)

    @staticmethod
    def _approved(tenant: str, version: dict, targets: tuple) -> ApprovedVersion:
        return ApprovedVersion(
            tenant_id=tenant,
            company_id=version["company"]["company_id"],
            document_version_id=version["version_id"],
            report_year=version["report_year"],
            approved=True,
            targets=targets,
        )

    def _save(
        self,
        db,
        *,
        tenant,
        run_id,
        request_key,
        request_hash,
        response,
        payload,
    ):
        replay = self._replay(db, tenant, run_id, request_key, request_hash)
        if replay is not None:
            return replay
        receipt = {
            "execution_profile": self.kind,
            "payload": payload,
            "sha256": canonical_hash(payload),
        }
        self.jobs._put(
            db,
            tenant,
            run_id,
            "comparison_receipt",
            payload["comparison"]["comparison_id"],
            receipt,
            immutable=True,
        )
        self.jobs._put(
            db,
            tenant,
            run_id,
            "comparison_request",
            request_key,
            {
                "request_hash": request_hash,
                "comparison_id": payload["comparison"]["comparison_id"],
                "response": response,
            },
            immutable=True,
        )
        return response

    def create(self, actor, run_id: str, body: dict, key: str) -> dict:
        if not self.enabled:
            raise ComparisonRejected("YEAR_COMPARISON_DISABLED")
        if not actor.has_capability("editor"):
            raise ComparisonRejected("FORBIDDEN", 403)
        try:
            _require_uuid("run_id", run_id)
        except ValueError:
            raise ComparisonRejected("VALIDATION_ERROR", 422) from None
        prior_version_id = self._validate_body(body, key)
        tenant = actor.tenant_id
        request_key = canonical_hash([actor.user_sub, "comparison_create", key])
        request_hash = canonical_hash([run_id, body])
        with self.jobs._transaction() as db:
            replay = self._replay(db, tenant, run_id, request_key, request_hash)
            if replay is not None:
                return replay
            try:
                initial_current = self.jobs._get(db, tenant, run_id, "run", "META")
                current_snapshot = self.runs._snapshot(db, tenant, run_id)
            except KeyError:
                raise ComparisonRejected("RESOURCE_NOT_FOUND", 404) from None
        current_version = self._version(tenant, initial_current["document_version_id"])
        prior_version = self._version(tenant, prior_version_id)
        if current_snapshot.get("document") != current_version:
            raise ComparisonRejected("COMPARISON_SOURCE_IDENTITY_MISMATCH")
        if current_version["company"]["company_id"] != prior_version["company"]["company_id"]:
            raise ComparisonRejected("RESOURCE_NOT_FOUND", 404)

        comparison_id, job_id = str(uuid4()), str(uuid4())
        response = {
            "job_id": job_id,
            "resource_id": comparison_id,
            "status": "ready",
            "status_url": f"/v1/comparisons/{comparison_id}",
        }
        base_pins = {
            "tenant_id": tenant,
            "current_run_id": run_id,
            "current_document_version_id": current_version["version_id"],
            "prior_document_version_id": prior_version_id,
            "current_mutation_epoch": initial_current["mutation_epoch"],
        }
        with self.jobs._transaction() as db:
            prior_match = self._run_for_version(db, tenant, prior_version_id)
        if prior_match is None:
            comparison = Comparison(
                comparison_id, "not_run", "prior_comparison_artifact_missing", ()
            )
            payload = base_pins | {"comparison": comparison.to_api_dict(), "prior_run_id": None}
        elif not self._periods_compatible(current_version, prior_version):
            comparison = Comparison(comparison_id, "not_run", "document_periods_not_comparable", ())
            payload = base_pins | {
                "comparison": comparison.to_api_dict(),
                "prior_run_id": prior_match[0],
            }
        else:
            prior_run_id, initial_prior = prior_match
            if "extract_job" not in initial_current:
                raise ComparisonRejected("CURRENT_COMPARISON_ARTIFACT_MISSING")
            current_claims = self.claims.list(tenant, run_id)
            prior_claims = self.claims.list(tenant, prior_run_id)
            try:
                with self.jobs._transaction() as db:
                    current = self.jobs._get(db, tenant, run_id, "run", "META")
                    prior = self.jobs._get(db, tenant, prior_run_id, "run", "META")
                    if (
                        current["mutation_epoch"] != initial_current["mutation_epoch"]
                        or current["document_version_id"] != current_version["version_id"]
                        or prior["mutation_epoch"] != initial_prior["mutation_epoch"]
                        or prior["document_version_id"] != prior_version_id
                    ):
                        raise ComparisonRejected("COMPARISON_SNAPSHOT_CONFLICT")
                    current_targets = self._targets(
                        db, tenant, run_id, current_version, current_claims
                    )
                    prior_targets = self._targets(
                        db, tenant, prior_run_id, prior_version, prior_claims
                    )
            except ComparisonRejected as error:
                reasons = {
                    "APPROVED_CLAIM_SNAPSHOT_MISSING": "approved_claim_snapshot_missing",
                    "COMPARISON_SOURCE_UNVERIFIED": "comparison_source_unverified",
                    "TARGET_COMPARISON_KEY_MISSING": "target_comparison_key_missing",
                }
                if error.code not in reasons:
                    raise
                comparison = Comparison(comparison_id, "not_run", reasons[error.code], ())
                payload = base_pins | {
                    "comparison": comparison.to_api_dict(),
                    "prior_run_id": prior_run_id,
                    "prior_mutation_epoch": initial_prior["mutation_epoch"],
                }
            else:
                comparison = compare_years(
                    self._approved(tenant, current_version, current_targets),
                    self._approved(tenant, prior_version, prior_targets),
                    comparison_id=comparison_id,
                )
                payload = base_pins | {
                    "comparison": comparison.to_api_dict(),
                    "prior_run_id": prior_run_id,
                    "prior_mutation_epoch": initial_prior["mutation_epoch"],
                    "current_targets_sha256": canonical_hash(
                        [asdict(target) for target in current_targets]
                    ),
                    "prior_targets_sha256": canonical_hash(
                        [asdict(target) for target in prior_targets]
                    ),
                }
        with self.jobs._transaction() as db:
            current = self.jobs._get(db, tenant, run_id, "run", "META")
            if current["mutation_epoch"] != initial_current["mutation_epoch"]:
                raise ComparisonRejected("COMPARISON_SNAPSHOT_CONFLICT")
            saved_prior_run_id = payload.get("prior_run_id")
            if saved_prior_run_id is not None and "prior_mutation_epoch" in payload:
                prior = self.jobs._get(db, tenant, saved_prior_run_id, "run", "META")
                if prior["mutation_epoch"] != payload["prior_mutation_epoch"]:
                    raise ComparisonRejected("COMPARISON_SNAPSHOT_CONFLICT")
            return self._save(
                db,
                tenant=tenant,
                run_id=run_id,
                request_key=request_key,
                request_hash=request_hash,
                response=response,
                payload=payload,
            )

    def get(self, actor, comparison_id: str) -> dict:
        try:
            _require_uuid("comparison_id", comparison_id)
        except ValueError:
            raise ComparisonRejected("RESOURCE_NOT_FOUND", 404) from None
        with self.jobs._transaction() as db:
            row = db.execute(
                "SELECT value FROM job_records WHERE tenant_id=? "
                "AND kind='comparison_receipt' AND record_id=?",
                (actor.tenant_id, comparison_id),
            ).fetchone()
        if row is None:
            raise ComparisonRejected("RESOURCE_NOT_FOUND", 404)
        receipt = json.loads(row[0])
        self._verify_access(actor.tenant_id, receipt)
        return receipt["payload"]["comparison"]
