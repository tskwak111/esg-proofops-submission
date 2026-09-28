"""Operator commands for the explicit bounded tag-stage recovery.

``inspect`` is read-only: it recomputes, from the local ledger and the local
receipt tree, which attempts were paid, which were provably never sent, and which
remain unresolved. It contacts nothing and writes nothing except its own report.

``authorize`` writes one immutable authorization plus its proof and enqueues one
recovery job. It still contacts nothing: the subsequent
``proofops-worker --stage tag`` run is what performs the bounded work, and it
refuses to start unless every unresolved failure already in the receipt tree was
named here by exact ``request_id``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from time import time
from uuid import UUID

from proofops.domain.rulepacks import canonical_json

from proofops_worker.composition import build_composition
from proofops_worker.tag_recovery import (
    RecoveryRejected,
    authorize_recovery,
    plan_recovery,
    recovery_proof,
)


def _receipts(runner, run_id: str) -> Path:
    return Path(runner.store.path).parent / "tagging-receipts" / run_id


def _report(path: str | None, payload: dict) -> None:
    text = canonical_json(payload)
    if path:
        Path(path).write_text(text + "\n")
    print(text)


def _inspect(runner, options) -> int:
    tenant_id, run_id = str(options.tenant_id), str(options.run_id)
    receipts = _receipts(runner, run_id)
    proof = recovery_proof(runner.store.usage.cost_data(tenant_id, run_id), receipts)
    limits = runner.store.usage.cost_data(tenant_id, run_id)
    payload = dict(
        command="inspect",
        tenant_id=tenant_id,
        run_id=run_id,
        receipts_root=str(receipts),
        attempts=len(limits),
        class_counts=proof["class_counts"],
        stop_records=proof["stop_records"],
        receipt_counts=proof["receipt_counts"],
        unresolved_failures=proof["classes"].get("unresolved_failure", []),
        never_sent_sample=proof["classes"].get("never_sent", [])[:10],
        never_sent_total=len(proof["classes"].get("never_sent", [])),
        proof_sha256=None,
    )
    _report(options.report, payload)
    return 0


def _authorize(runner, options) -> int:
    tenant_id, run_id = str(options.tenant_id), str(options.run_id)
    receipts = _receipts(runner, run_id)
    plan, proof, run = plan_recovery(
        runner.store,
        runner.tags,
        tenant_id=tenant_id,
        run_id=run_id,
        receipts=receipts,
        claim_limit=options.claim_limit,
        max_new_requests=options.max_new_requests,
        acknowledged_failures=frozenset(str(value) for value in options.acknowledge_stop),
        authorized_by=options.authorized_by,
        now=int(time()),
    )
    payload = dict(
        command="authorize",
        applied=bool(options.confirm),
        plan=plan.to_dict(),
        class_counts=proof["class_counts"],
        proposed_new_requests=len(plan.claim_ids) * 9,
        max_new_requests=plan.max_new_requests,
    )
    if not options.confirm:
        payload["note"] = "dry run; pass --confirm to record the authorization and enqueue the job"
        _report(options.report, payload)
        return 0
    message = authorize_recovery(runner.store, plan, proof, run, now=int(time()))
    payload["enqueued"] = dict(job_id=message.job_id, shard=message.shard, stage=message.stage)
    _report(options.report, payload)
    return 0


def main(argv=None) -> int:
    arguments = argparse.ArgumentParser(description="Inspect or authorize one tag-stage recovery")
    arguments.add_argument("command", choices=("inspect", "authorize"))
    arguments.add_argument("--tenant-id", required=True, type=UUID)
    arguments.add_argument("--run-id", required=True, type=UUID)
    arguments.add_argument("--report", default=None, metavar="FILE")
    arguments.add_argument(
        "--acknowledge-stop",
        action="append",
        default=[],
        type=UUID,
        metavar="REQUEST_ID",
        help=(
            "Acknowledge one unresolved failure by exact request_id. Its outcome stays "
            "unresolved and is never retried; the stop, receipt and ledger row are kept."
        ),
    )
    arguments.add_argument("--claim-limit", type=int, default=10)
    arguments.add_argument("--max-new-requests", type=int, default=30)
    arguments.add_argument("--authorized-by", default="")
    arguments.add_argument("--confirm", action="store_true")
    options = arguments.parse_args(argv)
    if options.command == "authorize" and not options.authorized_by.strip():
        raise SystemExit("--authorized-by is required to record an authorization")
    runner = build_composition(stage="tag")
    try:
        if options.command == "inspect":
            return _inspect(runner, options)
        return _authorize(runner, options)
    except RecoveryRejected as error:
        print(json.dumps(dict(rejected=str(error))), file=sys.stderr)
        return 1
    finally:
        runner.uploads.close()
        runner.uploads.registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
