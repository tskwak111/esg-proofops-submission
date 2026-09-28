"""Explicit bounded tag-stage recovery: carry the paid stage forward, re-send only
requests proven never to have reached the provider.

A stopped tag stage has three different kinds of residue, and they must never be
treated alike:

* **paid successes** — a settled ``succeeded`` ledger row *and* a complete
  receipt. Their result is already bought. Recovery replays them by carrying the
  committed checkpoint item forward verbatim, so nothing is re-requested and
  nothing is re-billed.
* **never-sent suppressed requests** — a settled ``failed`` row with
  ``latency_ms == 0``, no ``provider_request_id``, no token counts, no cost and
  **no receipt directory at all**. ``UpstageTaggingTransport._invoke``
  short-circuits before the network call when its receipt root is stopped, so
  these rows record a local suppression, not a provider outcome. They are the
  only work this recovery re-issues, and only after that proof is recomputed
  here from the actual ledger and the actual receipt tree.
* **unresolved real failures** — a settled ``failed`` row with a receipt and a
  non-zero latency. Whether the provider billed it cannot be known locally, so it
  is never retried. The operator must acknowledge it by exact ``request_id``
  before anything else in that receipt root may proceed, and it stays unresolved.

Nothing here deletes a stop, relaxes ``max_attempts``, widens a run budget,
rewrites a receipt, or republishes an immutable revision. A re-issued request
gets a genuinely new ``request_id``, derived from the recovery job, and therefore
its own attempt-1 reservation inside the run's existing immutable limits. The
burned never-sent ``request_id`` stays in the ledger exactly as it is, and the
recovery record links the two so the lineage is explicit rather than implied.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from proofops.application.ports.jobs import JobMessage
from proofops.application.tagging.consensus import reviewable_checkpoint
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import _require_sha256, _require_uuid
from proofops_agent.upstage_tagging import NEVER_SENT_ERROR_CODES, TransportResume

RECOVERY_KIND = "tag_recovery"
PROOF_KIND = "tag_recovery_proof"
SCHEMA = "tag_recovery_plan_v1"
PROOF_SCHEMA = "tag_recovery_proof_v1"
PREFIXES = ("preliminary", "elements", "relation")
MAX_CLAIMS = 200
# One claim can need three replicas at each of the three stages. A recovery starts a
# claim only when that whole set is still affordable, so the bound can never strand a
# claim half-tagged: a partly tagged claim would still publish an immutable
# needs_review revision and could then never be attempted again.
CLAIM_REQUEST_BUDGET = len(PREFIXES) * 3


class RecoveryRejected(ValueError):
    """The requested recovery is not provably safe; no state was changed."""


@dataclass(frozen=True, slots=True)
class TagRecoveryPlan:
    """The durable authorization for exactly one recovery job."""

    schema: str
    tenant_id: str
    run_id: str
    lineage_job_id: str
    lineage_checkpoint_sha256: str
    recovery_job_id: str
    shard: str
    receipts_root: str
    claim_ids: tuple[str, ...]
    reissued_request_ids: tuple[str, ...]
    acknowledged_failures: tuple[str, ...]
    max_new_requests: int
    proof_sha256: str
    authorized_by: str
    authorized_at: int

    def __post_init__(self) -> None:
        if self.schema != SCHEMA:
            raise RecoveryRejected("TAG_RECOVERY_SCHEMA_UNSUPPORTED")
        for name in ("tenant_id", "run_id", "lineage_job_id", "recovery_job_id"):
            _require_uuid(name, getattr(self, name))
        _require_sha256("lineage_checkpoint_sha256", self.lineage_checkpoint_sha256)
        _require_sha256("proof_sha256", self.proof_sha256)
        if self.recovery_job_id == self.lineage_job_id:
            raise RecoveryRejected("TAG_RECOVERY_JOB_MUST_BE_NEW")
        if self.shard != "tag-recovery-" + self.recovery_job_id:
            raise RecoveryRejected("TAG_RECOVERY_SHARD_MISMATCH")
        if not isinstance(self.receipts_root, str) or not Path(self.receipts_root).is_absolute():
            raise RecoveryRejected("TAG_RECOVERY_RECEIPTS_ROOT_REQUIRED")
        if Path(self.receipts_root).name != self.run_id:
            raise RecoveryRejected("TAG_RECOVERY_RECEIPTS_ROOT_MISMATCH")
        if not self.claim_ids or len(set(self.claim_ids)) != len(self.claim_ids):
            raise RecoveryRejected("TAG_RECOVERY_CLAIMS_REQUIRED")
        if len(self.claim_ids) > MAX_CLAIMS:
            raise RecoveryRejected("TAG_RECOVERY_CLAIM_BOUND_EXCEEDED")
        for claim_id in self.claim_ids:
            _require_uuid("claim_id", claim_id)
        for request_id in self.reissued_request_ids + self.acknowledged_failures:
            _require_uuid("request_id", request_id)
        if type(self.max_new_requests) is not int or not 1 <= self.max_new_requests <= 200:
            raise RecoveryRejected("TAG_RECOVERY_BOUND_INVALID")
        if self.max_new_requests < CLAIM_REQUEST_BUDGET:
            # A bound too small to finish even one claim would only strand work.
            raise RecoveryRejected("TAG_RECOVERY_BOUND_BELOW_ONE_CLAIM")
        if not isinstance(self.authorized_by, str) or not self.authorized_by.strip():
            raise RecoveryRejected("TAG_RECOVERY_AUTHOR_REQUIRED")
        if type(self.authorized_at) is not int or self.authorized_at < 0:
            raise RecoveryRejected("TAG_RECOVERY_TIME_INVALID")

    def to_dict(self) -> dict[str, Any]:
        return json.loads(canonical_json(asdict(self)))

    @classmethod
    def from_dict(cls, data) -> TagRecoveryPlan:
        try:
            return cls(
                **(
                    dict(data)
                    | dict(
                        claim_ids=tuple(data["claim_ids"]),
                        reissued_request_ids=tuple(data["reissued_request_ids"]),
                        acknowledged_failures=tuple(data["acknowledged_failures"]),
                    )
                )
            )
        except (KeyError, TypeError) as error:
            raise RecoveryRejected("TAG_RECOVERY_RECORD_INVALID") from error

    def message(self, *, document_version_id: str, input_hash: str) -> JobMessage:
        return JobMessage(
            self.tenant_id,
            self.run_id,
            document_version_id,
            self.recovery_job_id,
            "tag",
            self.shard,
            input_hash,
        )

    def transport_resume(self, *, already_dispatched: int) -> TransportResume:
        """Build the gate with the durable remainder of this job's one allowance."""
        return TransportResume(
            acknowledged_failures=frozenset(self.acknowledged_failures),
            max_new_requests=self.max_new_requests,
            already_dispatched=already_dispatched,
            expected_root=Path(self.receipts_root),
        )


def _receipt_ids(receipts: Path) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for prefix in PREFIXES:
        root = receipts / prefix
        found[prefix] = (
            {child.name for child in root.iterdir() if child.is_dir()} if root.is_dir() else set()
        )
    return found


def _stop_records(receipts: Path) -> dict[str, list[dict[str, Any]]]:
    stops: dict[str, list[dict[str, Any]]] = {}
    for prefix in PREFIXES:
        root = receipts / prefix
        entries = []
        if root.is_dir():
            for path in sorted(root.glob("transport-stop*.json")):
                try:
                    entries.append(json.loads(path.read_text()))
                except (OSError, ValueError):
                    raise RecoveryRejected("TAG_RECOVERY_STOP_UNREADABLE") from None
        stops[prefix] = entries
    return stops


def _classify(row, receipt_ids: set[str]) -> str:
    """Name the residue of one settled attempt from the ledger and the receipt tree.

    ``never_sent`` is deliberately hard to earn. It needs all of:

    * **no receipt directory** — structural proof, because every short-circuit in
      ``UpstageTaggingTransport._invoke`` returns before ``directory.mkdir()`` and
      a request that reached the provider always has its ``request.json`` written
      first, so a missing directory means the provider was never contacted;
    * an **explicitly known local-suppression ``error_code``** from
      :data:`NEVER_SENT_ERROR_CODES`, never an inference from timing;
    * no ``provider_request_id``, no input or output token count; and
    * an exactly zero cost, compared as :class:`~decimal.Decimal` so a tiny
      non-zero amount can never be rounded away by binary floating point.

    Anything else that failed is ``unresolved_failure``: it must be acknowledged
    by request_id before its receipt root may proceed, and it is never retried.
    """
    ledger, reservation = row["ledger"], row["reservation"]
    request_id = reservation["call"]["request_id"]
    if row["state"] == "released":
        return "released"
    if ledger is None:
        return "unsettled"
    usage = ledger["usage"]
    if usage["status"] == "succeeded":
        return "paid_success" if request_id in receipt_ids else "succeeded_without_receipt"
    cost = ledger["cost_decimal"]
    try:
        charged = cost is not None and Decimal(str(cost)) != 0
    except InvalidOperation:
        return "unresolved_failure"
    suppressed = (
        request_id not in receipt_ids
        and usage.get("error_code") in NEVER_SENT_ERROR_CODES
        and usage["provider_request_id"] is None
        and usage["latency_ms"] == 0
        and usage["input_tokens"] is None
        and usage["output_tokens"] is None
        and not charged
    )
    return "never_sent" if suppressed else "unresolved_failure"


def _durable_failures(receipts: Path) -> dict[str, list[str]]:
    """Which request_ids does the receipt tree itself still hold unresolved?

    This mirrors ``TransportResume._scan`` exactly, so the authorization and the
    gate can never disagree: a failure visible only on disk, with no ledger row,
    must still be acknowledged or nothing will arm.
    """
    found: dict[str, list[str]] = {}
    for prefix in PREFIXES:
        root = receipts / prefix
        unresolved: set[str] = set()
        if root.is_dir():
            for child in sorted(root.iterdir()):
                if child.is_dir():
                    response = child / "response.json"
                    if not response.is_file():
                        unresolved.add(child.name)
                        continue
                    try:
                        status = json.loads(response.read_text())["usage"]["status"]
                    except (OSError, ValueError, KeyError, TypeError):
                        raise RecoveryRejected("TAG_RECOVERY_RECEIPT_UNREADABLE") from None
                    if status != "succeeded":
                        unresolved.add(child.name)
        for record in _stop_records(receipts)[prefix]:
            identifier = record.get("request_id")
            if isinstance(identifier, str):
                unresolved.add(identifier)
        found[prefix] = sorted(unresolved)
    return found


def recovery_proof(usage_rows, receipts: Path) -> dict[str, Any]:
    """Recompute, from the ledger and the receipt tree, what each attempt actually cost.

    This is the whole safety argument for re-issuing work, so it is derived here
    rather than trusted from a prior report. ``baseline_request_ids`` pins every
    request_id that already existed, which is what later makes the recovery's own
    allowance countable across a crash or restart.
    """
    by_prefix = _receipt_ids(receipts)
    every_receipt: set[str] = set()
    for ids in by_prefix.values():
        every_receipt |= ids
    classes: dict[str, list[str]] = {}
    for row in usage_rows:
        request_id = row["reservation"]["call"]["request_id"]
        classes.setdefault(_classify(row, every_receipt), []).append(request_id)
    ledger_ids = {row["reservation"]["call"]["request_id"] for row in usage_rows}
    durable = _durable_failures(receipts)
    orphans = {
        identifier
        for unresolved in durable.values()
        for identifier in unresolved
        if identifier not in ledger_ids
    }
    if orphans:
        # A durable failure with no reservation row is still an unresolved outcome
        # and still blocks its root until it is acknowledged by request_id.
        classes["receipt_failure_without_ledger"] = sorted(orphans)
    return {
        "schema": PROOF_SCHEMA,
        "receipt_counts": {prefix: len(ids) for prefix, ids in by_prefix.items()},
        "stop_records": _stop_records(receipts),
        "durable_failures": durable,
        "attempts_total": len(usage_rows),
        "classes": {name: sorted(ids) for name, ids in sorted(classes.items())},
        "class_counts": {name: len(ids) for name, ids in sorted(classes.items())},
        "baseline_request_ids": sorted(ledger_ids | every_receipt | orphans),
    }


def issued_since_baseline(store, tenant_id, run_id, receipts: Path, baseline) -> list[str]:
    """Which request_ids exist now that the pinned baseline did not contain?

    Both sources are durable, so this survives a crash or a restart: a receipt
    directory is created before the provider call and a reservation row is written
    before it, which means a request this recovery already issued is counted even
    if the process died immediately afterwards.
    """
    known = frozenset(baseline)
    present: set[str] = set()
    for ids in _receipt_ids(receipts).values():
        present |= ids
    present |= {
        row["reservation"]["call"]["request_id"] for row in store.usage.cost_data(tenant_id, run_id)
    }
    return sorted(present - known)


def eligible_claims(items, *, published, never_sent, unresolved, limit: int):
    """Which committed claims may be re-issued, and which request_ids that replaces.

    A claim qualifies only when it is still blocked on unresolved preliminary tags,
    holds no published revision, has at least one provably never-sent attempt, and
    holds no unresolved attempt of its own. That last condition is what keeps the one
    real transport failure out: its claim stays blocked and is never retried.

    Pure, so the same rule can be replayed against real state read-only.
    """
    never, blocked_by = set(never_sent), set(unresolved)
    eligible: list[str] = []
    reissued: list[str] = []
    for item in items:
        if len(eligible) >= limit:
            break
        claim_id = item["claim_id"]
        if claim_id in published or item["status"] != "blocked":
            continue
        if item.get("reason") != "PRELIMINARY_TAGS_UNRESOLVED":
            continue
        attempts = {record["request_id"] for record in item.get("preliminary_records", [])}
        # This job regenerates the whole replica set. Mixed paid/suppressed sets
        # must stay held until individual paid replicas can be reused unchanged.
        if not attempts or not attempts <= never or attempts & blocked_by:
            continue
        eligible.append(claim_id)
        reissued.extend(sorted(attempts & never))
    return eligible, reissued


def _lineage(store, tenant_id: str, run_id: str) -> tuple[JobMessage, dict[str, Any]]:
    run = store.jobs.get_run(tenant_id, run_id)
    if "tag_job" not in run or "tag_snapshot_sha256" not in run:
        raise RecoveryRejected("TAG_RECOVERY_LINEAGE_REQUIRED")
    return JobMessage(**run["tag_job"]), run


def plan_recovery(
    store,
    tags,
    *,
    tenant_id: str,
    run_id: str,
    receipts: Path,
    claim_limit: int,
    max_new_requests: int,
    acknowledged_failures,
    authorized_by: str,
    now: int,
) -> tuple[TagRecoveryPlan, dict[str, Any], dict[str, Any]]:
    """Derive one bounded recovery plan; read-only, no model call and no write."""
    if type(claim_limit) is not int or not 1 <= claim_limit <= MAX_CLAIMS:
        raise RecoveryRejected("TAG_RECOVERY_CLAIM_BOUND_INVALID")
    lineage, run = _lineage(store, tenant_id, run_id)
    envelope = tags.load_snapshot(tenant_id, run_id)
    proof = recovery_proof(store.usage.cost_data(tenant_id, run_id), receipts)
    acknowledged = frozenset(acknowledged_failures)
    # Everything the gate will refuse to arm past must be named here, from both the
    # ledger and the receipt tree, so authorization and execution cannot disagree.
    unresolved = set(proof["classes"].get("unresolved_failure", ())) | set(
        proof["classes"].get("receipt_failure_without_ledger", ())
    )
    for name in ("unsettled", "succeeded_without_receipt"):
        if proof["classes"].get(name):
            # An unsettled reservation or a paid call with no durable receipt is a
            # different, unproven condition; it is never resumed past silently.
            raise RecoveryRejected("TAG_RECOVERY_LEDGER_UNPROVEN:" + name)
    if unresolved - acknowledged:
        raise RecoveryRejected(
            "TAG_RECOVERY_ACKNOWLEDGEMENT_REQUIRED:" + ",".join(sorted(unresolved - acknowledged))
        )
    if acknowledged - unresolved:
        raise RecoveryRejected(
            "TAG_RECOVERY_ACKNOWLEDGEMENT_UNKNOWN:" + ",".join(sorted(acknowledged - unresolved))
        )
    never_sent = set(proof["classes"].get("never_sent", ()))
    if not never_sent:
        raise RecoveryRejected("TAG_RECOVERY_NOTHING_NEVER_SENT")
    with store.jobs._transaction() as db:
        published = {
            item["claim_id"]
            for item in envelope["claims"]
            if store.jobs._raw(db, tenant_id, run_id, "claim_head", item["claim_id"]) is not None
        }
    eligible, reissued = eligible_claims(
        envelope["claims"],
        published=published,
        never_sent=never_sent,
        unresolved=unresolved,
        limit=claim_limit,
    )
    if not eligible:
        raise RecoveryRejected("TAG_RECOVERY_NO_ELIGIBLE_CLAIM")
    recovery_job_id = str(uuid4())
    plan = TagRecoveryPlan(
        SCHEMA,
        tenant_id,
        run_id,
        lineage.job_id,
        run["tag_snapshot_sha256"],
        recovery_job_id,
        "tag-recovery-" + recovery_job_id,
        str(Path(receipts).resolve()),
        tuple(eligible),
        tuple(sorted(reissued)),
        tuple(sorted(acknowledged)),
        max_new_requests,
        canonical_hash(proof),
        authorized_by,
        now,
    )
    return plan, proof, run


def _open_authorizations(jobs, db, plan: TagRecoveryPlan) -> list[str]:
    """Which prior authorizations for this same lineage are still unfinished?

    A second authorization against the same committed stage would enqueue a second
    tag job, and both would then carry the same lineage forward and mint their own
    new requests. Each would also arm its own allowance, so the bound would silently
    double. Only authorizations whose job has actually settled are allowed to be
    followed by another one for the same lineage.
    """
    open_jobs = []
    for raw_plan in jobs._all(db, plan.tenant_id, plan.run_id, RECOVERY_KIND):
        prior = TagRecoveryPlan.from_dict(raw_plan)
        if prior.recovery_job_id == plan.recovery_job_id:
            raise RecoveryRejected("TAG_RECOVERY_ALREADY_AUTHORIZED")
        if prior.lineage_checkpoint_sha256 != plan.lineage_checkpoint_sha256:
            continue
        record = jobs._raw(db, plan.tenant_id, plan.run_id, "job", prior.recovery_job_id)
        status = "missing" if record is None else json.loads(record)["status"]
        if status in {"missing", "pending", "leased"}:
            open_jobs.append(prior.recovery_job_id + ":" + status)
    return sorted(open_jobs)


def authorize_recovery(store, plan: TagRecoveryPlan, proof, run, *, now: int) -> JobMessage:
    """Record the authorization and enqueue its job inside one transaction."""
    if canonical_hash(proof) != plan.proof_sha256:
        raise RecoveryRejected("TAG_RECOVERY_PROOF_MISMATCH")
    message = plan.message(
        document_version_id=run["document_version_id"],
        input_hash=JobMessage(**run["tag_job"]).input_hash,
    )
    jobs = store.jobs
    with jobs._transaction() as db:
        current = jobs._get(db, plan.tenant_id, plan.run_id, "run", "META")
        if (
            current["revision"] != run["revision"]
            or current.get("tag_snapshot_sha256") != plan.lineage_checkpoint_sha256
            or json.loads(canonical_json(current.get("tag_job")))
            != json.loads(canonical_json(run["tag_job"]))
        ):
            raise RecoveryRejected("TAG_RECOVERY_RUN_CHANGED")
        # Same transaction as the write, so two concurrent operators cannot both
        # observe "no open authorization" and then both record one.
        outstanding = _open_authorizations(jobs, db, plan)
        if outstanding:
            raise RecoveryRejected("TAG_RECOVERY_ALREADY_PENDING:" + ",".join(outstanding))
        jobs._put(
            db, plan.tenant_id, plan.run_id, PROOF_KIND, plan.recovery_job_id, proof, immutable=True
        )
        jobs._put(
            db,
            plan.tenant_id,
            plan.run_id,
            RECOVERY_KIND,
            plan.recovery_job_id,
            plan.to_dict(),
            immutable=True,
        )
        jobs.enqueue_transaction(db, message, now=now)
    return message


def load_plan(store, message: JobMessage) -> TagRecoveryPlan | None:
    with store.jobs._transaction() as db:
        raw = store.jobs._raw(db, message.tenant_id, message.run_id, RECOVERY_KIND, message.job_id)
    if raw is None:
        return None
    plan = TagRecoveryPlan.from_dict(json.loads(raw))
    if (
        plan.message(document_version_id=message.document_version_id, input_hash=message.input_hash)
        != message
    ):
        raise RecoveryRejected("TAG_RECOVERY_MESSAGE_MISMATCH")
    return plan


class TagRecovery:
    """Bound recovery state for one claimed job: what to redo and what to carry."""

    def __init__(self, plan: TagRecoveryPlan, lineage: dict[str, Any], already_dispatched: int):
        self.plan = plan
        self.claim_ids = frozenset(plan.claim_ids)
        self._items = {item["claim_id"]: item for item in lineage["claims"]}
        self._profile = lineage.get("fact_assembly_profile", "strict-v1")
        if not self.claim_ids <= set(self._items):
            raise RecoveryRejected("TAG_RECOVERY_CLAIM_NOT_IN_LINEAGE")
        try:
            self.resume = plan.transport_resume(already_dispatched=already_dispatched)
        except ValueError as error:
            # A fully spent allowance is refused outright rather than renewed.
            raise RecoveryRejected("TAG_RECOVERY_ALLOWANCE_SPENT:" + str(error)) from None

    @classmethod
    def load(cls, store, tags, message: JobMessage) -> TagRecovery | None:
        plan = load_plan(store, message)
        if plan is None:
            return None
        run = store.jobs.get_run(message.tenant_id, message.run_id)
        if (
            run.get("tag_snapshot_sha256") != plan.lineage_checkpoint_sha256
            or JobMessage(**run["tag_job"]).job_id != plan.lineage_job_id
        ):
            # The lineage this authorization was derived from is no longer the
            # committed one; re-authorize against the current stage instead.
            raise RecoveryRejected("TAG_RECOVERY_LINEAGE_SUPERSEDED")
        with store.jobs._transaction() as db:
            raw = store.jobs._raw(db, plan.tenant_id, plan.run_id, PROOF_KIND, plan.recovery_job_id)
        if raw is None:
            raise RecoveryRejected("TAG_RECOVERY_PROOF_MISSING")
        proof = json.loads(raw)
        if canonical_hash(proof) != plan.proof_sha256:
            raise RecoveryRejected("TAG_RECOVERY_PROOF_MISMATCH")
        # Recount this job's own spend from durable state, so a crashed or
        # restarted attempt continues on the remainder of the single authorized
        # allowance instead of receiving a second one.
        issued = issued_since_baseline(
            store,
            plan.tenant_id,
            plan.run_id,
            Path(plan.receipts_root),
            proof["baseline_request_ids"],
        )
        return cls(plan, tags.load_snapshot(message.tenant_id, message.run_id), len(issued))

    def can_attempt_claim(self) -> bool:
        """Is a whole claim still affordable, replicas at every stage included?"""
        return self.resume.remaining >= CLAIM_REQUEST_BUDGET

    def carry_forward(self, claim_id: str) -> dict[str, Any]:
        """Return the committed item for a claim this recovery is not attempting.

        The bytes are the ones already validated and, where published, already
        bound to an immutable revision; they are neither recomputed nor re-bought,
        so the paid result is preserved rather than replaced by a fresh guess.
        """
        item = self._items.get(claim_id)
        if item is None:
            raise RecoveryRejected("TAG_RECOVERY_CLAIM_NOT_IN_LINEAGE")
        return reviewable_checkpoint(json.loads(canonical_json(item)), pinned_profile=self._profile)

    def verify_published(self, store) -> int:
        """Check every carried published item still matches its immutable revision.

        The review identifier is content-addressed on the review inputs, so a
        carried item that still resolves to a stored record with the same hash is
        byte-identical to what was published. Any drift stops the recovery instead
        of committing a checkpoint that contradicts an immutable revision.
        """
        verified = 0
        with store.jobs._transaction() as db:
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
                if head is None:
                    continue
                if stored is None or canonical_hash(json.loads(stored)) != canonical_hash(
                    item["review_inputs"]
                ):
                    raise RecoveryRejected("TAG_RECOVERY_PUBLISHED_INPUT_DRIFT:" + claim_id)
                verified += 1
        return verified

    def summary(self) -> dict[str, Any]:
        return dict(
            schema="tag_recovery_summary_v1",
            recovery_job_id=self.plan.recovery_job_id,
            lineage_job_id=self.plan.lineage_job_id,
            lineage_checkpoint_sha256=self.plan.lineage_checkpoint_sha256,
            attempted_claims=sorted(self.claim_ids),
            carried_claims=len(self._items) - len(self.claim_ids),
            reissued_request_ids=list(self.plan.reissued_request_ids),
            acknowledged_failures=list(self.plan.acknowledged_failures),
            max_new_requests=self.plan.max_new_requests,
            already_dispatched_before_this_attempt=self.resume.already_dispatched,
            remaining_new_requests=self.resume.remaining,
            dispatched_new_requests=sorted(self.resume.dispatched),
        )


__all__ = [
    "MAX_CLAIMS",
    "PROOF_KIND",
    "RECOVERY_KIND",
    "RecoveryRejected",
    "TagRecovery",
    "TagRecoveryPlan",
    "authorize_recovery",
    "load_plan",
    "plan_recovery",
    "eligible_claims",
    "recovery_proof",
]
