#!/usr/bin/env python3
"""Local administrative CLI for delegated rule-pack review.

This is the only path that lets an operator, acting under an explicit
user domain-judgment delegation, activate a validated rule-pack for REAL
(non-synthetic) runs without a human `approved_by`. It never exposes an
HTTP endpoint and never runs unattended: every write requires an explicit
`--apply` flag plus a non-blank `--reviewer`/`--reviewed-at`/`--note`.

What it does, in order:
1. Reads the exact candidate pack + its files from the local state DB by
   `--rule-pack-id` (read-only; never mutates the row in place -- packs are
   immutable by (tenant_id, rule_pack_id)).
2. Prints the pack's target content hash, mode, status, and any
   `unresolved_gap_ids` so the operator sees exactly what is being
   reviewed before anything is written.
3. Structurally validates the pack via the existing `validate_rulepack`
   (same fail-closed GAP-001/008/009 checks human activation uses).
4. Without `--apply`: stops here (dry run). Prints whether the pack would
   be eligible and why not, if not.
5. With `--apply`: if the candidate is `status="draft"`, writes a NEW pack
   record (new uuid4 id, same content, `status="validated"`) via
   `store.add_pack` -- a genuine new immutable revision, never a raw SQLite
   UPDATE of the draft row. If the candidate is already `status="validated"`,
   reviews it directly. Then calls `store.record_ai_delegated_review` on
   that pack id with the supplied reviewer/reviewed_at/source_authority/note.

This tool makes no legal, official-standard, or independent-expert-gold
claim. The `--source-authority` value is recorded verbatim in the audit
trail; it does not itself grant authorization beyond what the operator
supplies.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from proofops.adapters.local.rulepack_store import (  # noqa: E402
    IdempotencyConflict,
    RulePackNotFound,
    RulePackSqliteStore,
    StaleRulePackRevision,
)
from proofops.application.rulepacks import (  # noqa: E402
    RulePackRecord,
    compute_pack_sha256,
    validate_rulepack,
)
from proofops.domain.rules.safe_harbor import CHECKLIST_POLICY_V1  # noqa: E402


def _default_gap_ids() -> list[str]:
    gaps_path = ROOT / "contracts" / "domain_gaps.json"
    return [entry["id"] for entry in json.loads(gaps_path.read_text(encoding="utf-8"))]


def derive_checklist_policy_pack(
    source: RulePackRecord,
    files: dict,
    *,
    version: str,
    reviewer: str,
    reviewed_at: str,
    source_authority: str,
    note: str,
) -> tuple[RulePackRecord, dict]:
    """Build a new immutable candidate only; no storage/default activation side effect."""
    if not isinstance(version, str) or not version.strip() or version == source.version:
        raise ValueError("checklist policy requires a new nonblank pack version")
    checked = validate_rulepack(source.to_dict(), files, _default_gap_ids())
    if not checked.ok:
        raise ValueError("; ".join(checked.errors))
    changed = deepcopy(files)
    for content in changed.values():
        content["version"] = version
    config = changed["regulatory/safe_harbor.yaml"]
    if config.get("reasonable_basis_boolean_mapping") is not None:
        raise ValueError("derive checklist policy from a legacy null mapping pack")
    config["reasonable_basis_boolean_mapping"] = CHECKLIST_POLICY_V1
    config["checklist_policy_review"] = {
        "policy_identifier": CHECKLIST_POLICY_V1,
        "gap_id": "GAP-001",
        "source_section": "4.6",
        "review_origin": "ai_project_interpretation",
        "reviewer": reviewer,
        "reviewed_at": reviewed_at,
        "source_authority": source_authority,
        "note": note,
        "before_sha256": source.sha256,
        "before_version": source.version,
        "boundary_vectors": [
            {"states": ["present", "present"], "expected": True},
            {"states": ["absent", "present"], "expected": False},
            {"states": ["absent", "unknown"], "expected": False},
            {"states": ["unknown", "present"], "expected": None},
            {"states": ["conflict", "present"], "expected": None},
            {"states": ["not_applicable", "present"], "expected": None},
            {"states": ["missing", "present"], "expected": None},
            {"states": [], "expected": None},
            {"input": "unverified absent", "expected": "rejected"},
            {"input": "unreadable", "expected": "unknown upstream; never absent"},
        ],
    }
    data = source.to_dict() | {
        "rule_pack_id": str(uuid4()),
        "version": version,
        "status": "validated",
        "approved_by": None,
        "approved_at": None,
    }
    data["sha256"] = compute_pack_sha256(data, changed)
    result = validate_rulepack(data, changed, _default_gap_ids())
    if not result.ok:
        raise ValueError("; ".join(result.errors))
    return RulePackRecord.from_dict(data), changed


def promote_and_review_pack(
    store: RulePackSqliteStore,
    *,
    tenant_id: str,
    draft_pack: RulePackRecord,
    files: dict,
    reviewer: str,
    reviewed_at: str,
    source_authority: str,
    note: str,
    now: float,
    gap_ids: list[str] | None = None,
) -> str:
    """Minimal composition helper for callers (e.g. a pilot's new-run path)
    that already build a draft `RulePackRecord` + files dict and want to
    explicitly opt in to AI-delegated review for THIS run only, instead of
    always leaving the pack as an unapproved draft (which forces
    `run_store.create()` into `rulepack_use="candidate_tagging_reference_only"`
    and blocks real grading).

    Call this INSTEAD OF `store.add_pack(draft_pack, files)` when the caller
    wants to opt in; skip it entirely to keep today's draft-only behavior.
    Returns the new (validated, active) `rule_pack_id` to pass as the run's
    `rule_pack_id`. Writes two new immutable revisions (validated + active
    pointer update) via the same `add_pack`/`record_ai_delegated_review`
    paths used everywhere else; never mutates `draft_pack`'s own row.
    """
    store.add_pack(draft_pack, files)
    new_pack_dict = {
        **draft_pack.to_dict(),
        "rule_pack_id": str(uuid4()),
        "status": "validated",
    }
    new_pack_dict["sha256"] = compute_pack_sha256(new_pack_dict, files)
    new_pack = RulePackRecord.from_dict(new_pack_dict)
    store.add_pack(new_pack, files)
    store.record_ai_delegated_review(
        tenant_id=tenant_id,
        rule_pack_id=new_pack.rule_pack_id,
        expected_revision=1,
        idempotency_key=f"review-{new_pack.rule_pack_id}-{reviewer}",
        reviewer=reviewer,
        reviewed_at=reviewed_at,
        source_authority=source_authority,
        note=note,
        gap_ids=gap_ids or _default_gap_ids(),
        now=now,
    )
    return new_pack.rule_pack_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--state-db", default=str(ROOT / ".local" / "state.sqlite3"))
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--rule-pack-id", required=True, help="Candidate pack to review.")
    parser.add_argument("--reviewer", required=True, help="Operator identifier")
    parser.add_argument("--reviewed-at", required=True, help="Timezone-aware ISO 8601 timestamp.")
    parser.add_argument(
        "--source-authority",
        default="user delegation 2026-09-20",
        help="Recorded verbatim in the audit trail; does not itself grant authorization.",
    )
    parser.add_argument("--note", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the write. Without this flag, the pack is only inspected/validated.",
    )
    parser.add_argument("--idempotency-key", default=None)
    parser.add_argument(
        "--checklist-policy",
        choices=[CHECKLIST_POLICY_V1],
        default=None,
        help="Explicitly derive a NEW pack for project checklist completeness only.",
    )
    parser.add_argument("--derived-version", default=None)
    parser.add_argument("--now", type=float, default=None, help="Unix timestamp; default is now.")
    args = parser.parse_args(argv)
    if bool(args.checklist_policy) != bool(args.derived_version):
        parser.error("--checklist-policy and --derived-version must be supplied together")

    import time as _time

    now = args.now if args.now is not None else _time.time()
    gap_ids = _default_gap_ids()

    store = RulePackSqliteStore(args.state_db)
    try:
        record, files = store.get_pack_with_files(args.tenant_id, args.rule_pack_id)
    except RulePackNotFound:
        print(json.dumps({"ok": False, "error": "rule pack not found"}))
        return 1

    report: dict = {
        "rule_pack_id": record.rule_pack_id,
        "tenant_id": record.tenant_id,
        "mode": record.mode,
        "status": record.status,
        "sha256": record.sha256,
        "unresolved_gap_ids": list(record.unresolved_gap_ids),
        "approved_by": record.approved_by,
    }

    if args.checklist_policy:
        try:
            record, files = derive_checklist_policy_pack(
                record,
                files,
                version=args.derived_version,
                reviewer=args.reviewer,
                reviewed_at=args.reviewed_at,
                source_authority=args.source_authority,
                note=args.note,
            )
        except ValueError as exc:
            report.update(applied=False, error=str(exc))
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 1
        report.update(
            derived_from_pack_id=args.rule_pack_id,
            new_validated_pack_id=record.rule_pack_id,
            new_validated_sha256=record.sha256,
            checklist_policy=args.checklist_policy,
            grade_mapping_status="unresolved",
        )

    result = validate_rulepack(record.to_dict(), files, gap_ids)
    report["structurally_valid"] = result.ok
    report["validation_errors"] = list(result.errors)
    if not result.ok:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    if record.approved_by:
        report["eligible_for_ai_delegated_review"] = False
        report["reason"] = "pack already has a human approved_by"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    if record.status not in ("draft", "validated"):
        report["eligible_for_ai_delegated_review"] = False
        report["reason"] = f"status={record.status} cannot be reviewed"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    report["eligible_for_ai_delegated_review"] = True

    if not args.apply:
        report["applied"] = False
        report["note"] = "dry run: pass --apply to write the review and activate this pack"
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    target_pack_id = record.rule_pack_id
    target_revision = 1
    if args.checklist_policy:
        store.add_pack(record, files)
    if record.status == "draft":
        new_pack_dict = {
            **record.to_dict(),
            "rule_pack_id": str(uuid4()),
            "status": "validated",
        }
        new_pack_dict["sha256"] = compute_pack_sha256(new_pack_dict, files)
        new_pack = RulePackRecord.from_dict(new_pack_dict)
        store.add_pack(new_pack, files)
        target_pack_id = new_pack.rule_pack_id
        report["promoted_from_draft_pack_id"] = record.rule_pack_id
        report["new_validated_pack_id"] = target_pack_id
        report["new_validated_sha256"] = new_pack.sha256

    idempotency_key = args.idempotency_key or f"review-{target_pack_id}-{args.reviewer}"
    try:
        stored = store.record_ai_delegated_review(
            tenant_id=args.tenant_id,
            rule_pack_id=target_pack_id,
            expected_revision=target_revision,
            idempotency_key=idempotency_key,
            reviewer=args.reviewer,
            reviewed_at=args.reviewed_at,
            source_authority=args.source_authority,
            note=args.note,
            gap_ids=gap_ids,
            now=now,
        )
    except (StaleRulePackRevision, IdempotencyConflict, ValueError) as exc:
        report["applied"] = False
        report["error"] = str(exc)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1

    report["applied"] = True
    report["activated_pack"] = stored.body
    report["note"] = (
        "New runs for this (tenant, mode) now use this pack. The pack that was "
        "previously active, if any, is retired (preserved, not deleted). Any "
        "already-running run keeps its frozen rule_pack_sha256 snapshot."
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
