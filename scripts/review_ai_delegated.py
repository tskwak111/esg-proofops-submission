#!/usr/bin/env python3
"""Local trusted CLI for delegated tag review.

This is the ONLY path that records ``origin=ai_delegated`` /
``review_status=ai_delegated_confirmed``. It never exposes an HTTP endpoint
and never runs unattended: every write requires an explicit ``--apply`` flag
plus a non-blank ``--delegated-reviewer``/``--delegation-authority``.

What it does, in order:
1. Reads the explicit correction JSON (``base_tag_revision``/``track``/
   ``elements``/``reason`` -- the same 4 keys the human HTTP route accepts;
   any extra key such as ``origin`` is rejected) and the target review row
   (read-only) from the local state DB.
2. Dry run (default): prints review/claim/run ids, expected ``If-Match``
   revision, element count and the honest labels that WOULD be recorded,
   without writing anything.
3. With ``--apply``: calls the trusted backend operation
   ``ReviewService.resolve_ai_delegated_review`` with ``delegated_reviewer``
   and ``delegation_authority`` supplied as constructor arguments (never
   parsed from the correction JSON), reusing the exact same
   source/binding/If-Match/engine guards as the human route.

``--safe-harbor-review-json`` is an opt-in input for runs already pinned to the
opt-in checklist policy. It accepts only the configured category items and exact
verified refs from the immutable evidence packet; it does not enable the policy
or change old runs.

``--category-review-json`` is an opt-in correction that REMOVES a
safe-harbor category the frozen packet and every guarded tag-run header agree
on but the reviewed source does not support. It never adds or replaces a
category, never rewrites a header/packet/replica hash and creates no fact. The
dry run validates the ACTUAL request against the replayed loader inputs before
any write, so a rejected correction is reported without touching the database.
With ``--re-review`` the dry run also reads the CURRENT tag head through the
claim head pointer and refuses the same carried-safe-harbor conflict the
service refuses, instead of discovering it only at ``--apply``.

Use ``--re-review --apply`` to append a revision to a resolved review. The
correction must name the current tag revision; prior records remain immutable.

Provenance honesty: the new tag revision records
``reviewer_sub="ai-delegated-review:<operator>"``,
``origin="ai_delegated"`` and ``review_status="ai_delegated_confirmed"``,
plus ``review_origin="ai_project_interpretation"`` and the verbatim
``delegation_authority``. It is labelled "AI 검토(위임·사람 아님)" in the
web UI and is NOT accepted as a human gold approval (comparisons still
require ``human_confirmed``). No independent-human or gold claim is made.

Rollback: stop calling this tool; old ``human``/``consensus`` rows are
immutable and remain valid, and no DB schema change is involved. A category
correction is NOT reverted by this tool: stopping means no further correction
is recorded, and prior revisions plus the current corrected head stay intact.
A later re-review that supplies no correction CARRIES and re-validates the
prior receipt, so the removal is not silently undone; the observed category
would only reappear if this implementation itself were reverted or if the claim
were tagged again in a new run.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-db", default=str(ROOT / ".local" / "state.sqlite3"))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--review-id", required=True)
    parser.add_argument(
        "--correction-json",
        required=True,
        help="Path to explicit correction JSON (base_tag_revision/track/elements/reason only).",
    )
    parser.add_argument(
        "--applicability-review-json",
        help="Explicit single-claim trigger review JSON; complete replayed claim refs required.",
    )
    parser.add_argument(
        "--safe-harbor-review-json",
        help="Explicit category checklist facts; the run must use the opt-in pack.",
    )
    parser.add_argument(
        "--category-review-json",
        help="Explicit removal of a wrong safe-harbor category; observed value must be pinned.",
    )
    parser.add_argument(
        "--context-review-json", help="Source-backed facility section context JSON."
    )
    parser.add_argument(
        "--delegated-reviewer",
        required=True,
        help="Trusted local operator identifier.",
    )
    parser.add_argument(
        "--delegation-authority",
        default="user delegation 2026-09-20",
        help="Recorded verbatim in the tag row; does not itself grant authorization.",
    )
    parser.add_argument("--actor-sub", default=None)
    parser.add_argument(
        "--if-match", default=None, help='e.g. "1"; default reads current revision.'
    )
    parser.add_argument("--idempotency-key", required=True)
    parser.add_argument(
        "--re-review", action="store_true", help="Explicitly re-review a resolved item."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the write. Without this flag, only inspect (dry run).",
    )
    return parser.parse_args(argv)


def _carried_safe_harbor(store, tenant_id, review) -> bool:
    """Read the CURRENT tag head through the claim head pointer, read-only.

    Uses the same accessor chain as ``ReviewStore.resolve`` (claim head ->
    that exact tag revision) instead of guessing the newest stored row.
    Returns False when the claim has no resolved head yet.
    """
    jobs = getattr(store, "jobs", None)
    if jobs is None:
        return False
    with jobs._transaction() as db:
        try:
            head = jobs._get(db, tenant_id, review["run_id"], "claim_head", review["claim_id"])
            current = jobs._get(
                db,
                tenant_id,
                review["run_id"],
                "tag_revision",
                f'{review["claim_id"]}:{head["tag_revision"]:010}',
            )
        except (KeyError, TypeError):
            return False
    return isinstance(current.get("safe_harbor_review"), dict)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        UUID(args.tenant_id)
        UUID(args.review_id)
    except ValueError:
        print(json.dumps({"ok": False, "error": "tenant-id and review-id must be UUIDs"}))
        return 1
    try:
        body = json.loads(Path(args.correction_json).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(json.dumps({"ok": False, "error": "correction JSON unreadable"}))
        return 1
    if not isinstance(body, dict) or set(body) != {
        "base_tag_revision",
        "track",
        "elements",
        "reason",
    }:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "correction JSON must carry exactly "
                    "base_tag_revision/track/elements/reason",
                }
            )
        )
        return 1

    applicability_review = None
    if args.applicability_review_json:
        try:
            applicability_review = json.loads(
                Path(args.applicability_review_json).read_text(encoding="utf-8")
            )
            if not isinstance(applicability_review, dict):
                raise ValueError("expected object")
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "error": "applicability review JSON invalid"}))
            return 1

    safe_harbor_review = None
    if args.safe_harbor_review_json:
        try:
            safe_harbor_review = json.loads(
                Path(args.safe_harbor_review_json).read_text(encoding="utf-8")
            )
            if not isinstance(safe_harbor_review, dict):
                raise ValueError("expected object")
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "error": "safe-harbor review JSON invalid"}))
            return 1

    category_review = None
    if args.category_review_json:
        try:
            category_review = json.loads(
                Path(args.category_review_json).read_text(encoding="utf-8")
            )
            if not isinstance(category_review, dict):
                raise ValueError("expected object")
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "error": "category review JSON invalid"}))
            return 1
    if category_review is not None and safe_harbor_review is not None:
        # Same fail-closed rule as the service: the checklist documents the very
        # category being removed, so the two cannot share one revision.
        print(json.dumps({"ok": False, "error": "CATEGORY_REVIEW_CONFLICTS_SAFE_HARBOR"}))
        return 1

    context_review = None
    if args.context_review_json:
        try:
            context_review = json.loads(Path(args.context_review_json).read_text(encoding="utf-8"))
            if not isinstance(context_review, dict):
                raise ValueError("expected object")
        except (OSError, ValueError):
            print(json.dumps({"ok": False, "error": "context review JSON invalid"}))
            return 1

    database_path = Path(args.state_db)
    # Same wiring as apps/api composition: real loader guards, no shortcuts.
    from proofops.adapters.local.review_store import LocalSQLiteReviewStore  # noqa: E402
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore  # noqa: E402
    from proofops.adapters.local.run_store import LocalSQLiteRunStore  # noqa: E402
    from proofops.adapters.local.tag_store import LocalTagStore  # noqa: E402
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser  # noqa: E402
    from proofops.application.authorization import AuthContext  # noqa: E402
    from proofops.application.registry import Registry, RulePackChoice  # noqa: E402
    from proofops.application.reviews import (  # noqa: E402
        AI_DELEGATED_ORIGIN,
        AI_DELEGATED_REVIEW_STATUS,
        _review_category,
    )
    from proofops.application.reviews import (
        ReviewService as _ReviewService,
    )
    from proofops.application.uploads import UploadService  # noqa: E402

    rulepack_store = RulePackSqliteStore(database_path)

    def _active_packs(tenant_id: str) -> tuple[RulePackChoice, ...]:
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

    registry = Registry.sqlite(database_path, active_rule_packs=_active_packs)
    runs = LocalSQLiteRunStore(database_path, rulepacks=rulepack_store)
    uploads = UploadService(database_path, database_path.parent / "objects", registry)
    parser = OpenDataLoaderParser(database_path.parent / "parser-prepared")
    tags = LocalTagStore(runs, uploads, parser)
    service = _ReviewService(
        LocalSQLiteReviewStore(runs.jobs),
        load_inputs=tags.load_inputs,
        verify_context_sources=tags.verify_context_sources,
    )

    store = service.store
    try:
        review = store.get(args.tenant_id, args.review_id)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"review not found: {exc}"}))
        return 1
    if_match = args.if_match or f'"{review["revision"]}"'
    report: dict = {
        "review_id": review["review_id"],
        "run_id": review["run_id"],
        "claim_id": review["claim_id"],
        "status": review["status"],
        "revision": review["revision"],
        "base_tag_revision": review["base_tag_revision"],
        "if_match": if_match,
        "re_review": args.re_review,
        "element_count": len(body["elements"]) if isinstance(body.get("elements"), list) else 0,
        "applicability_review": applicability_review,
        "safe_harbor_review": safe_harbor_review,
        "category_review": category_review,
        "would_record_origin": AI_DELEGATED_ORIGIN,
        "would_record_review_status": AI_DELEGATED_REVIEW_STATUS,
        "would_record_reviewer_sub": f"ai-delegated-review:{args.delegated_reviewer}",
        "note": "AI 검토(위임·사람 아님); comparisons still require human_confirmed",
    }
    if category_review is not None:
        # Validate the ACTUAL request against the replayed loader inputs before
        # any write, in the dry run as well as before --apply. Read-only.
        if (
            args.re_review
            and safe_harbor_review is None
            and _carried_safe_harbor(store, args.tenant_id, review)
        ):
            # Same fail-closed rule as the service, reported before --apply:
            # the current head carries a checklist attestation for the very
            # category this request removes.
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "CATEGORY_REVIEW_CONFLICTS_SAFE_HARBOR",
                        "status": 409,
                        "stage": "category_review_validation",
                    }
                )
            )
            return 1
        try:
            loader = getattr(service, "load_inputs", None)
            if loader is None:
                raise ValueError("review service has no input loader")
            replayed = loader(args.tenant_id, review["run_id"], review["claim_id"])
            replayed.validate()
            _, receipt, superseded = _review_category(replayed, body["track"], category_review)
        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": getattr(exc, "code", type(exc).__name__),
                        "status": getattr(exc, "status", None),
                        "stage": "category_review_validation",
                    }
                )
            )
            return 1
        report["category_review_validation"] = {
            "ok": True,
            "observed_category": receipt["observed_category"],
            "observed_tag_run_headers": receipt["observed_tag_run_headers"],
            "corrected_category": receipt["corrected_category"],
            "superseded_checklist_items": list(superseded),
            "input_snapshot_sha256": receipt["identity"]["input_snapshot_sha256"],
            "packet_sha256": receipt["identity"]["packet_sha256"],
            "note": "removal only; observed headers and packet stay pinned",
        }
    if context_review is not None:
        from proofops.application.claim_context_review import review_facility_context

        try:
            replayed = service.load_inputs(args.tenant_id, review["run_id"], review["claim_id"])
            replayed.validate()
            receipt = review_facility_context(replayed, context_review, tags.verify_context_sources)
            report["context_review_validation"] = receipt["projection"]
        except (ValueError, KeyError, TypeError) as exc:
            print(
                json.dumps({"ok": False, "error": str(exc), "stage": "context_review_validation"})
            )
            return 1
    if not args.apply:
        report["applied"] = False
        report["note"] = (
            "dry run: pass --apply to write the AI-delegated revision; " + report["note"]
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not args.delegated_reviewer.strip() or not args.delegation_authority.strip():
        print(json.dumps({"ok": False, "error": "delegated reviewer/authority must be non-blank"}))
        return 1
    actor = AuthContext(
        args.actor_sub or args.delegated_reviewer.strip(),
        args.tenant_id,
        "reviewer",
        frozenset({"viewer", "reviewer"}),
        "local-cli",
    )
    # Local loader path: reuse the composition service's loader when present.
    if getattr(service, "load_inputs", None) is None:
        print(json.dumps({"ok": False, "error": "review service has no input loader"}))
        return 1
    try:
        result = service.resolve_ai_delegated_review(
            actor,
            args.review_id,
            body,
            if_match,
            args.idempotency_key,
            delegated_reviewer=args.delegated_reviewer,
            delegation_authority=args.delegation_authority,
            applicability_review=applicability_review,
            safe_harbor_review=safe_harbor_review,
            category_review=category_review,
            context_review=context_review,
            reopen=args.re_review,
        )
    except Exception as exc:  # noqa: BLE001
        code = getattr(exc, "code", type(exc).__name__)
        status = getattr(exc, "status", None)
        print(json.dumps({"ok": False, "error": code, "status": status}))
        return 1
    report["applied"] = True
    report["new_tag_revision"] = result["new_tag_revision"]
    report["review_status"] = result["decision"]["review_status"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
