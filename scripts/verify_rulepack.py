#!/usr/bin/env python3
"""Verify a rule-pack against YAML sources + gap registry (TASK-025, FR-025).

Reads already-parsed YAML only; performs no network, AWS, or model calls.
Exit 0 when the pack validates, 1 otherwise. Prints a JSON report to stdout.

Usage:
    python scripts/verify_rulepack.py --pack <pack.json> \
        [--config-dir config] [--gaps contracts/domain_gaps.json]

<pack.json> carries the pack identity/metadata (rule_pack_id, tenant_id,
version, effective_date, mode, status, ontology_version,
source_document_sha256, files, sha256, unresolved_gap_ids, approved_by,
approved_at). The files listed there are loaded as YAML from --config-dir.
Gap ids are loaded from the gap registry JSON (each entry's "id").

This script never invents clause numbers, legal effect, or approvals: packs
whose basis claims verification without reviewer/URL/date evidence, whose
safe-harbor grade mapping is set without approval, or whose timeline enables
automatic legal applicability are rejected (docs/31 GAP-001/008/009).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packages"))

from proofops.application.rulepacks import _is_safe_path, validate_rulepack  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a ProofOps rule-pack.")
    parser.add_argument("--pack", required=True, help="Path to pack metadata JSON.")
    parser.add_argument("--config-dir", default="config", help="Directory holding rule YAML.")
    parser.add_argument(
        "--gaps", default="contracts/domain_gaps.json", help="Gap registry JSON path."
    )
    args = parser.parse_args()

    pack_path = Path(args.pack)
    config_dir = Path(args.config_dir).resolve()
    gaps_path = Path(args.gaps)

    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "errors": [f"cannot read pack: {exc}"]}))
        return 1

    if not isinstance(pack, dict) or not isinstance(pack.get("files"), list):
        print(json.dumps({"ok": False, "errors": ["pack must be an object with a files array"]}))
        return 1
    files = pack["files"]
    files_content: dict[str, dict] = {}
    errors: list[str] = []
    for rel in files:
        if not isinstance(rel, str) or not _is_safe_path(rel):
            errors.append("unsafe file path rejected before reading")
            continue
        path = (config_dir / rel).resolve()
        if not path.is_relative_to(config_dir):
            errors.append("file resolves outside the configuration directory")
            continue
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"cannot read YAML file: {rel}: {exc}")
            continue
        if not isinstance(doc, dict):
            errors.append(f"YAML file is not a mapping: {rel}")
            continue
        files_content[rel] = doc

    try:
        gaps = json.loads(gaps_path.read_text(encoding="utf-8"))
        gap_ids = [entry["id"] for entry in gaps]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(json.dumps({"ok": False, "errors": [f"cannot read gap registry: {exc}"]}))
        return 1

    result = validate_rulepack(pack, files_content, gap_ids)
    all_errors = errors + list(result.errors)
    report = {
        "ok": not all_errors and result.ok,
        "errors": all_errors,
        "computed_sha256": result.computed_sha256,
        "declared_sha256": pack.get("sha256"),
        "pack_status": pack.get("status"),
        "activatable": (
            result.ok
            and not errors
            and pack.get("status") == "validated"
            and bool(pack.get("approved_by"))
            and bool(pack.get("approved_at"))
        ),
        "note": (
            "Structural validation only: clause/legal approval, model bindings, "
            "and account gates are verified by their owners, not by this script."
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
