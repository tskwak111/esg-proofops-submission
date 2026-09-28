#!/usr/bin/env python3
"""Local trusted CLI for delegated manual preliminary classification.

The ONLY path that records ``origin=ai_delegated_classification``. It never exposes an
HTTP endpoint and never runs unattended: the write requires an explicit ``--apply`` plus
a non-blank ``--delegated-reviewer``/``--delegation-authority``. It reuses the exact same
source/lineage/If-Match/idempotency guards as the human route and enqueues the same
bounded reprocess job; it is NOT accepted as a human gold classification.

Dry run (default) validates the classification without recording it or enqueuing a job.
With ``--apply`` it calls ``LocalSQLiteClassificationStore.classify_ai_delegated``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))


def _parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--state-db", default=str(ROOT / ".local" / "state.sqlite3"))
    p.add_argument("--objects", default=None, help="Object store root (default: <db-dir>/objects)")
    p.add_argument("--tenant-id", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--claim-id", required=True)
    p.add_argument(
        "--body-json", required=True, help="track/safe_harbor_category/dimensions/reason"
    )
    p.add_argument("--if-match", default=None, help="Reviewed lineage ETag; required with --apply.")
    p.add_argument("--idempotency-key", required=True)
    p.add_argument("--delegated-reviewer", required=True, help="Operator identifier")
    p.add_argument("--delegation-authority", required=True)
    p.add_argument("--apply", action="store_true", help="Perform the write (else dry run).")
    return p.parse_args(argv)


def _build_store(database_path: Path, objects: Path):
    from proofops.adapters.local.classification_store import LocalSQLiteClassificationStore
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.adapters.local.tag_store import LocalTagStore
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.registry import Registry, RulePackChoice
    from proofops.application.uploads import UploadService

    rulepack_store = RulePackSqliteStore(database_path)

    def active(tenant_id):
        return tuple(
            RulePackChoice(
                pack.rule_pack_id,
                pack.tenant_id,
                pack.version,
                pack.sha256,
                pack.status,
                pack.mode,
                pack.effective_date,
                pack.unresolved_gap_ids,
            )
            for pack in rulepack_store.list_active_packs(tenant_id)
        )

    registry = Registry.sqlite(database_path, active_rule_packs=active)
    runs = LocalSQLiteRunStore(database_path, rulepacks=rulepack_store)
    uploads = UploadService(database_path, objects, registry)
    parser = OpenDataLoaderParser(database_path.parent / "parser-prepared")
    from proofops.adapters.local.claim_store import LocalClaimStore

    tags = LocalTagStore(runs, uploads, parser)
    claims = LocalClaimStore(runs, uploads, parser)
    return LocalSQLiteClassificationStore(runs, uploads, parser, tags, claims), registry, uploads


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        UUID(args.tenant_id)
        UUID(args.run_id)
        UUID(args.claim_id)
    except ValueError:
        print(json.dumps({"ok": False, "error": "tenant/run/claim ids must be UUIDs"}))
        return 1
    try:
        body = json.loads(Path(args.body_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "body JSON unreadable"}))
        return 1
    if not args.delegated_reviewer.strip() or not args.delegation_authority.strip():
        print(json.dumps({"ok": False, "error": "delegated reviewer/authority must be non-blank"}))
        return 1

    from time import time

    from proofops.application.authorization import AuthContext
    from proofops.application.tagging.manual_classification import (
        ClassificationRejected,
        validate_manual_classification,
    )

    database_path = Path(args.state_db)
    objects = Path(args.objects) if args.objects else database_path.parent / "objects"
    store, registry, uploads = _build_store(database_path, objects)
    actor = AuthContext(
        args.delegated_reviewer.strip(),
        args.tenant_id,
        "reviewer",
        frozenset({"viewer", "reviewer"}),
        "local-cli",
    )
    try:
        view = store.view(actor, args.run_id, args.claim_id)
        _, discovery, graph = store.claims.load_evidence(args.tenant_id, args.run_id)
        claim = next(c for c in discovery.claims if c.claim_id == args.claim_id)
        validate_manual_classification(claim, graph, body, tenant_id=args.tenant_id)
        if_match = args.if_match or view.get("etag")
        report = {
            "eligible": view["eligible"],
            "ineligible_reason": view["ineligible_reason"],
            "blocked_reason": view["blocked_reason"],
            "if_match": if_match,
            "would_record_origin": "ai_delegated_classification",
            "would_record_classified_by": (
                "ai-delegated-classification:" + args.delegated_reviewer.strip()
            ),
            "note": "AI classification (delegated, not human); not a gold classification",
        }
        if not args.apply:
            report["applied"] = False
            report["note"] = "dry run: pass --apply to write; " + report["note"]
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.if_match is None:
            print(
                json.dumps({"ok": False, "error": "--apply requires the reviewed --if-match token"})
            )
            return 1
        if if_match is None:
            print(json.dumps({"ok": False, "error": "no lineage etag available (ineligible)"}))
            return 1
        result = store.classify_ai_delegated(
            actor,
            args.run_id,
            args.claim_id,
            body,
            if_match,
            args.idempotency_key,
            delegated_reviewer=args.delegated_reviewer,
            delegation_authority=args.delegation_authority,
            now=int(time()),
        )
        report["applied"] = True
        report["classification_id"] = result["classification"]["classification_id"]
        report["reprocess_job_id"] = result["reprocess_job"]["job_id"]
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    except ClassificationRejected as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": getattr(exc, "code", str(exc)),
                    "status": getattr(exc, "status", None),
                }
            )
        )
        return 1
    finally:
        uploads.close()
        registry.close()


if __name__ == "__main__":
    raise SystemExit(main())
