"""R22 bounded manual-classification reprocess authorization (local adapter).

The durable plan, its CAS-guarded authorization and its enqueue live here so the API
adapter can atomically record a classification and enqueue exactly one bounded tag job
without importing the worker app. The worker's runtime consumption (carry-forward and
transport allowance) lives in ``proofops_worker.tag_reprocess``.

This is deliberately NOT ``tag_recovery``: recovery re-issues only provably never-sent
requests and this module never touches that eligibility. It is a separate authorized
supplier-override job. It enqueues a new tag job with a new ``job_id``; bounds its new
provider requests (max 6 = 3 element + 3 relation replicas for the one claim) inside
the run's existing immutable limits with no run-budget increase; refuses stale lineage
and any outstanding tag job; and refuses a claim that already holds a published head.
Nothing here rewrites a receipt, widens a budget, clears a stop, or republishes an
immutable revision.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from proofops.application.ports.jobs import JobMessage
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import _require_sha256, _require_uuid

REPROCESS_KIND = "tag_reprocess"
SCHEMA = "tag_reprocess_plan_v1"
PREFIXES = ("preliminary", "elements", "relation")
# The one claim buys at most three element replicas and three relation replicas; the
# preliminary stage is supplied by the recorded classification and never re-requested.
CLAIM_REQUEST_BUDGET = 6


class ReprocessRejected(ValueError):
    """The requested reprocess is not provably safe; no state was changed."""

    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True, slots=True)
class ClassificationReprocessPlan:
    """The durable authorization for exactly one manual-classification reprocess job."""

    schema: str
    tenant_id: str
    run_id: str
    lineage_job_id: str
    lineage_checkpoint_sha256: str
    reprocess_job_id: str
    shard: str
    receipts_root: str
    claim_id: str
    classification_id: str
    classification_sha256: str
    baseline_request_ids: tuple[str, ...]
    max_new_requests: int
    authorized_by: str
    authorized_at: int

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise ReprocessRejected("TAG_REPROCESS_SCHEMA_UNSUPPORTED")
        for name in ("tenant_id", "run_id", "lineage_job_id", "reprocess_job_id", "claim_id"):
            _require_uuid(name, getattr(self, name))
        _require_sha256("lineage_checkpoint_sha256", self.lineage_checkpoint_sha256)
        _require_sha256("classification_sha256", self.classification_sha256)
        if not isinstance(self.baseline_request_ids, tuple) or list(
            self.baseline_request_ids
        ) != sorted(set(self.baseline_request_ids)):
            # Deterministic, deduplicated: the pinned baseline is what makes this job's
            # own spend countable across a crash/restart without renewal.
            raise ReprocessRejected("TAG_REPROCESS_BASELINE_INVALID")
        for identifier in self.baseline_request_ids:
            _require_uuid("baseline_request_id", identifier)
        if self.reprocess_job_id == self.lineage_job_id:
            raise ReprocessRejected("TAG_REPROCESS_JOB_MUST_BE_NEW")
        if self.shard != "tag-reprocess-" + self.reprocess_job_id:
            raise ReprocessRejected("TAG_REPROCESS_SHARD_MISMATCH")
        if not isinstance(self.receipts_root, str) or not Path(self.receipts_root).is_absolute():
            raise ReprocessRejected("TAG_REPROCESS_RECEIPTS_ROOT_REQUIRED")
        if Path(self.receipts_root).name != self.run_id:
            raise ReprocessRejected("TAG_REPROCESS_RECEIPTS_ROOT_MISMATCH")
        if type(self.max_new_requests) is not int or self.max_new_requests != CLAIM_REQUEST_BUDGET:
            # A single claim's fixed ceiling; never widened, never shrunk below a claim.
            raise ReprocessRejected("TAG_REPROCESS_BOUND_INVALID")
        if not isinstance(self.authorized_by, str) or not self.authorized_by.strip():
            raise ReprocessRejected("TAG_REPROCESS_AUTHOR_REQUIRED")
        if type(self.authorized_at) is not int or self.authorized_at < 0:
            raise ReprocessRejected("TAG_REPROCESS_TIME_INVALID")

    def to_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json(asdict(self)))

    @classmethod
    def from_dict(cls, data) -> ClassificationReprocessPlan:
        try:
            return cls(
                **(dict(data) | {"baseline_request_ids": tuple(data["baseline_request_ids"])})
            )
        except (KeyError, TypeError) as error:
            raise ReprocessRejected("TAG_REPROCESS_RECORD_INVALID") from error

    def message(self, *, document_version_id: str, input_hash: str) -> JobMessage:
        return JobMessage(
            self.tenant_id,
            self.run_id,
            document_version_id,
            self.reprocess_job_id,
            "tag",
            self.shard,
            input_hash,
        )


def baseline_request_ids(store, tenant_id: str, run_id: str, receipts: Path) -> list[str]:
    """Every request_id that already exists, from the ledger and the receipt tree."""
    present: set[str] = {
        row["reservation"]["call"]["request_id"] for row in store.usage.cost_data(tenant_id, run_id)
    }
    for prefix in PREFIXES:
        root = receipts / prefix
        if root.is_dir():
            present |= {child.name for child in root.iterdir() if child.is_dir()}
    return sorted(present)


def issued_since_baseline(store, tenant_id, run_id, receipts: Path, baseline) -> list[str]:
    """Which request_ids exist now that the pinned baseline did not contain?

    Both the reservation ledger and the receipt directories are durable and written
    before the provider call, so a request this reprocess already issued is counted
    even if the process died immediately afterwards -- the allowance is never renewed.
    """
    known = frozenset(baseline)
    present = set(baseline_request_ids(store, tenant_id, run_id, receipts))
    return sorted(present - known)


def outstanding_tag_jobs(jobs, db, tenant_id: str, run_id: str) -> list[str]:
    """Every unsettled tag job for this run, so two callbacks can never race."""
    outstanding = []
    for record in jobs._all(db, tenant_id, run_id, "job"):
        message = record["message"]
        if message["stage"] == "tag" and record["status"] in {"pending", "leased"}:
            outstanding.append(message["job_id"] + ":" + record["status"])
    return sorted(outstanding)


def _lineage(store, tenant_id: str, run_id: str) -> tuple[JobMessage, dict[str, Any]]:
    run = store.jobs.get_run(tenant_id, run_id)
    if "tag_job" not in run or "tag_snapshot_sha256" not in run:
        raise ReprocessRejected("TAG_REPROCESS_LINEAGE_REQUIRED")
    return JobMessage(**run["tag_job"]), run


def plan_reprocess(
    store,
    tags,
    *,
    tenant_id: str,
    run_id: str,
    claim_id: str,
    classification_id: str,
    classification_sha256: str,
    receipts: Path,
    authorized_by: str,
    now: int,
) -> tuple[ClassificationReprocessPlan, dict[str, Any]]:
    """Derive one bounded reprocess plan; read-only, no model call and no write."""
    _require_uuid("claim_id", claim_id)
    _require_uuid("classification_id", classification_id)
    _require_sha256("classification_sha256", classification_sha256)
    lineage, run = _lineage(store, tenant_id, run_id)
    envelope = tags.load_snapshot(tenant_id, run_id)
    item = next((c for c in envelope["claims"] if c["claim_id"] == claim_id), None)
    if item is None:
        raise ReprocessRejected("TAG_REPROCESS_CLAIM_NOT_IN_LINEAGE")
    if item["status"] != "blocked" or item.get("reason") != "PRELIMINARY_TAGS_UNRESOLVED":
        raise ReprocessRejected("NOT_PRELIMINARY_BLOCKED")
    with store.jobs._transaction() as db:
        if store.jobs._raw(db, tenant_id, run_id, "claim_head", claim_id) is not None:
            raise ReprocessRejected("ALREADY_TAGGED")
    reprocess_job_id = str(uuid4())
    plan = ClassificationReprocessPlan(
        SCHEMA,
        tenant_id,
        run_id,
        lineage.job_id,
        run["tag_snapshot_sha256"],
        reprocess_job_id,
        "tag-reprocess-" + reprocess_job_id,
        str(Path(receipts).resolve()),
        claim_id,
        classification_id,
        classification_sha256,
        tuple(baseline_request_ids(store, tenant_id, run_id, Path(receipts).resolve())),
        CLAIM_REQUEST_BUDGET,
        authorized_by,
        now,
    )
    return plan, run


def authorize_reprocess(
    store, plan: ClassificationReprocessPlan, run, *, db, now: int
) -> JobMessage:
    """Record the authorization and enqueue its job inside one transaction.

    Refuses if the run drifted from the pinned lineage or if any tag job is still
    outstanding, so two reprocess/recovery callbacks can never race the same lineage.
    """
    message = plan.message(
        document_version_id=run["document_version_id"],
        input_hash=JobMessage(**run["tag_job"]).input_hash,
    )
    jobs = store.jobs
    current = jobs._get(db, plan.tenant_id, plan.run_id, "run", "META")
    if (
        current["revision"] != run["revision"]
        or current.get("tag_snapshot_sha256") != plan.lineage_checkpoint_sha256
        or json.loads(canonical_json(current.get("tag_job")))
        != json.loads(canonical_json(run["tag_job"]))
    ):
        raise ReprocessRejected("STALE_CLASSIFICATION", 412)
    existing = jobs._raw(db, plan.tenant_id, plan.run_id, REPROCESS_KIND, plan.reprocess_job_id)
    if existing is not None:
        raise ReprocessRejected("TAG_REPROCESS_ALREADY_AUTHORIZED")
    outstanding = outstanding_tag_jobs(jobs, db, plan.tenant_id, plan.run_id)
    if outstanding:
        raise ReprocessRejected("RUN_JOB_OUTSTANDING:" + ",".join(outstanding))
    if jobs._raw(db, plan.tenant_id, plan.run_id, "claim_head", plan.claim_id) is not None:
        raise ReprocessRejected("ALREADY_TAGGED")
    jobs._put(
        db,
        plan.tenant_id,
        plan.run_id,
        REPROCESS_KIND,
        plan.reprocess_job_id,
        plan.to_dict(),
        immutable=True,
    )
    jobs.enqueue_transaction(db, message, now=now)
    return message


def load_plan(store, message: JobMessage) -> ClassificationReprocessPlan | None:
    with store.jobs._transaction() as db:
        raw = store.jobs._raw(db, message.tenant_id, message.run_id, REPROCESS_KIND, message.job_id)
    if raw is None:
        return None
    plan = ClassificationReprocessPlan.from_dict(json.loads(raw))
    if (
        plan.message(document_version_id=message.document_version_id, input_hash=message.input_hash)
        != message
    ):
        raise ReprocessRejected("TAG_REPROCESS_MESSAGE_MISMATCH")
    return plan


__all__ = [
    "CLAIM_REQUEST_BUDGET",
    "REPROCESS_KIND",
    "ClassificationReprocessPlan",
    "ReprocessRejected",
    "authorize_reprocess",
    "baseline_request_ids",
    "issued_since_baseline",
    "load_plan",
    "outstanding_tag_jobs",
    "plan_reprocess",
]
