"""Worker-side runtime for the bounded manual-classification reprocess.

The durable plan, CAS authorization and enqueue live in
``proofops.adapters.local.classification_reprocess`` so the API adapter can authorize without
importing the worker. This module is only the runtime consumption: given a claimed tag
job whose message matches a reprocess plan, it exposes the target claim, carries every
other committed claim forward verbatim, and builds the bounded transport allowance from
durable state so a crash or restart never renews the six-request ceiling.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from proofops.adapters.local.classification_reprocess import (  # re-exported for callers/tests
    CLAIM_REQUEST_BUDGET,
    REPROCESS_KIND,
    ClassificationReprocessPlan,
    ReprocessRejected,
    authorize_reprocess,
    issued_since_baseline,
    load_plan,
    plan_reprocess,
)
from proofops.application.ports.jobs import JobMessage
from proofops.application.tagging.consensus import reviewable_checkpoint
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops_agent.upstage_tagging import TransportResume


def _transport_resume(
    plan: ClassificationReprocessPlan, *, already_dispatched: int
) -> TransportResume:
    """The bounded remainder of this job's single allowance; no acknowledged failures.

    A reprocess targets a claim with no published revision and no unresolved failure of
    its own, so an outstanding failure in the tree must be resolved by recovery, never
    carried into a reprocess allowance.
    """
    return TransportResume(
        acknowledged_failures=frozenset(),
        max_new_requests=plan.max_new_requests,
        already_dispatched=already_dispatched,
        expected_root=Path(plan.receipts_root),
    )


class TagReprocess:
    """Bound reprocess state for one claimed job: the target claim, carry the rest."""

    def __init__(
        self,
        plan: ClassificationReprocessPlan,
        lineage: dict[str, Any],
        classification: dict[str, Any],
        already_dispatched: int,
    ):
        self.plan = plan
        self.claim_id = plan.claim_id
        self.claim_ids = frozenset({plan.claim_id})
        self.classification = classification
        self._items = {item["claim_id"]: item for item in lineage["claims"]}
        self._profile = lineage.get("fact_assembly_profile", "strict-v1")
        if plan.claim_id not in self._items:
            raise ReprocessRejected("TAG_REPROCESS_CLAIM_NOT_IN_LINEAGE")
        try:
            self.resume = _transport_resume(plan, already_dispatched=already_dispatched)
        except ValueError as error:
            raise ReprocessRejected("TAG_REPROCESS_ALLOWANCE_SPENT:" + str(error)) from None

    @classmethod
    def load(cls, store, tags, message: JobMessage) -> TagReprocess | None:
        plan = load_plan(store, message)
        if plan is None:
            return None
        run = store.jobs.get_run(message.tenant_id, message.run_id)
        if (
            run.get("tag_snapshot_sha256") != plan.lineage_checkpoint_sha256
            or JobMessage(**run["tag_job"]).job_id != plan.lineage_job_id
        ):
            raise ReprocessRejected("TAG_REPROCESS_LINEAGE_SUPERSEDED")
        with store.jobs._transaction() as db:
            raw = store.jobs._raw(
                db,
                plan.tenant_id,
                plan.run_id,
                "preliminary_classification",
                plan.classification_id,
            )
            if raw is None:
                raise ReprocessRejected("TAG_REPROCESS_CLASSIFICATION_MISSING")
            classification = json.loads(raw)
            head = store.jobs._raw(db, plan.tenant_id, plan.run_id, "claim_head", plan.claim_id)
        # Integrity: the stored record's own content-address must match the plan's pin
        # and its self-digest, so a tampered record can never seed a reprocess.
        expected = canonical_hash({k: v for k, v in classification.items() if k != "record_sha256"})
        if classification.get(
            "record_sha256"
        ) != expected or plan.classification_sha256 != classification.get("record_sha256"):
            raise ReprocessRejected("TAG_REPROCESS_CLASSIFICATION_MISMATCH")
        if head is not None:
            # A published claim can never be reprocessed; its revision is immutable.
            raise ReprocessRejected("ALREADY_TAGGED")
        receipts = Path(plan.receipts_root)
        # Baseline is pinned in the immutable plan at authorization; never recomputed
        # here, so a crash/restart resumes on the remainder of the single allowance.
        already = issued_since_baseline(
            store, plan.tenant_id, plan.run_id, receipts, plan.baseline_request_ids
        )
        return cls(
            plan,
            tags.load_snapshot(message.tenant_id, message.run_id),
            classification,
            len(already),
        )

    def can_attempt_claim(self) -> bool:
        # Any remaining allowance lets the attempt proceed: cached successful replicas
        # replay for free (never a new provider call) and only genuinely-new requests
        # consume the durable remainder, so a partially-completed crash still finishes
        # without renewing the ceiling. A fully spent allowance cannot be constructed
        # (TransportResume raises), so remaining is always >= 1 here.
        return self.resume.remaining > 0

    def carry_forward(self, claim_id: str) -> dict[str, Any]:
        item = self._items.get(claim_id)
        if item is None:
            raise ReprocessRejected("TAG_REPROCESS_CLAIM_NOT_IN_LINEAGE")
        return reviewable_checkpoint(json.loads(canonical_json(item)), pinned_profile=self._profile)

    def verify_published(self, store, *, db=None) -> int:
        """Every carried published item must still match its immutable revision.

        The review id is content-addressed on the review inputs, so a carried item that
        still resolves to a stored record with the same hash is byte-identical to what
        was published. Any drift stops the reprocess before it commits a checkpoint that
        would contradict an immutable revision. The target claim is skipped (it is being
        freshly tagged, not carried).
        """
        from uuid import NAMESPACE_URL, uuid5

        if db is None:
            with store.jobs._transaction() as db:
                return self.verify_published(store, db=db)
        verified = 0
        for claim_id, item in sorted(self._items.items()):
            if claim_id in self.claim_ids or item.get("review_inputs") is None:
                continue
            review_id = str(uuid5(NAMESPACE_URL, canonical_hash(item["review_inputs"])))
            stored = store.jobs._raw(
                db, self.plan.tenant_id, self.plan.run_id, "review_inputs", review_id
            )
            head = store.jobs._raw(
                db, self.plan.tenant_id, self.plan.run_id, "claim_head", claim_id
            )
            if (
                head is None
                or stored is None
                or canonical_hash(json.loads(stored)) != canonical_hash(item["review_inputs"])
            ):
                raise ReprocessRejected("TAG_REPROCESS_PUBLISHED_INPUT_DRIFT:" + claim_id)
            verified += 1
        return verified

    def verify_publication(self, store, db):
        """Recheck the authorized lineage inside the checkpoint publication transaction."""
        plan = self.plan
        current = store.jobs._get(db, plan.tenant_id, plan.run_id, "run", "META")
        if (
            current.get("tag_snapshot_sha256") != plan.lineage_checkpoint_sha256
            or current.get("tag_job", {}).get("job_id") != plan.lineage_job_id
            or store.jobs._raw(db, plan.tenant_id, plan.run_id, "claim_head", plan.claim_id)
            is not None
        ):
            raise ReprocessRejected("TAG_REPROCESS_LINEAGE_SUPERSEDED")
        self.verify_published(store, db=db)

    def summary(self) -> dict[str, Any]:
        return dict(
            schema="tag_reprocess_summary_v1",
            reprocess_job_id=self.plan.reprocess_job_id,
            lineage_job_id=self.plan.lineage_job_id,
            lineage_checkpoint_sha256=self.plan.lineage_checkpoint_sha256,
            attempted_claim=self.plan.claim_id,
            classification_id=self.plan.classification_id,
            classification_sha256=self.plan.classification_sha256,
            carried_claims=len(self._items) - 1,
            max_new_requests=self.plan.max_new_requests,
            already_dispatched_before_this_attempt=self.resume.already_dispatched,
            remaining_new_requests=self.resume.remaining,
            dispatched_new_requests=sorted(self.resume.dispatched),
        )


__all__ = [
    "CLAIM_REQUEST_BUDGET",
    "REPROCESS_KIND",
    "ClassificationReprocessPlan",
    "ReprocessRejected",
    "TagReprocess",
    "authorize_reprocess",
    "issued_since_baseline",
    "load_plan",
    "plan_reprocess",
]
