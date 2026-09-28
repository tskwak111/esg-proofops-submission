"""Replay original-to-candidate provenance and prepare an unapproved account inventory.

This offline check never issues authenticated review, coverage or policy records.
Account-name matches are search hints, not an accounting definition of CAPEX.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from proofops.adapters.dart import ArtifactStore
from proofops.adapters.dart.candidates import add_operator_sr_sources, build_candidate_catalog
from proofops.adapters.reconciliation import FileSourceReader
from proofops.application.reconciliation import canonical_sha256

from evaluation.reconciliation_cli import load_json, write_json


def account_hint(row: dict[str, Any]) -> str:
    """Describe lexical candidates without accepting them into a CAPEX allowlist."""
    name = str(row.get("account_nm", ""))
    account = str(row.get("account_id", ""))
    purchase = bool(re.search(r"취득|구입|매입|PaymentsToAcquire|Purchase", name + account, re.I))
    asset = bool(re.search(r"유형자산|무형자산|PropertyPlant|Intangible", name + account, re.I))
    if purchase and asset and row.get("sj_div") == "CF":
        return "cash_flow_asset_purchase_candidate"
    if asset and row.get("sj_div") == "BS":
        return "balance_sheet_stock_not_cash_flow"
    if re.search(r"투자활동|InvestingActivities", name + account, re.I):
        return "broad_investing_flow_requires_disaggregation"
    return "unclassified"


def audit_sources(
    manifest: dict[str, Any],
    store_root: Path,
    *,
    corp_code: str,
    fy: int,
    rcept_no: str,
    consolidation: str,
    max_candidates: int = 2000,
    sr_manifest: dict[str, Any] | None = None,
    sr_root: Path | None = None,
) -> dict[str, Any]:
    if not store_root.is_dir():
        raise ValueError("original_store_missing")
    prepared = build_candidate_catalog(
        manifest,
        ArtifactStore(store_root),
        corp_code=corp_code,
        fy=fy,
        rcept_no=rcept_no,
        consolidation=consolidation,
        max_candidates=max_candidates,
    )
    if sr_manifest is not None:
        if sr_root is None:
            raise ValueError("sr_root_missing")
        prepared = add_operator_sr_sources(prepared, sr_manifest, sr_root)
    checks = []
    inventory = []
    # Reconstructed artifacts are temporary; input originals are never rewritten.
    with tempfile.TemporaryDirectory(prefix="proofops-source-audit-") as directory:
        root = Path(directory)
        for artifact in prepared.artifacts:
            path = root / artifact.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(artifact.payload)
        reader = FileSourceReader(root, prepared.artifact_index)
        for candidate in prepared.catalog["candidates"]:
            source = candidate["source"]
            reader.validate(source, reader(source))
            checks.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "candidate_type": candidate["candidate_type"],
                    "source_id": source["source_id"],
                    "artifact_sha256": source["artifact_sha256"],
                    "locator": source["locator"],
                    "quote_sha256": hashlib.sha256(source["quote"].encode("utf-8")).hexdigest(),
                    "lineage": candidate["lineage"],
                    "byte_locator_check": "passed",
                }
            )
            if candidate["candidate_type"] != "statement_row":
                continue
            row = candidate["raw"]
            inventory.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "source_id": source["source_id"],
                    "account_id": row.get("account_id"),
                    "account_name": row.get("account_nm"),
                    "statement": row.get("sj_div"),
                    "account_detail": row.get("account_detail"),
                    "period_label": row.get("thstrm_nm"),
                    "raw_amount": row.get("thstrm_amount"),
                    "currency": row.get("currency"),
                    "mapping_hint": account_hint(row),
                    "approved": False,
                    "include_in_capex": None,
                }
            )
    return {
        "schema_version": "reconciliation-source-audit-1",
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "identity": prepared.catalog["identity"],
        "synthetic": prepared.catalog["synthetic"],
        "collection_manifest_sha256": canonical_sha256(manifest),
        "catalog_sha256": canonical_sha256(prepared.catalog),
        "originals": [
            {key: entry[key] for key in ("source_id", "artifact_sha256", "locator")}
            for entry in manifest["artifacts"]
        ],
        "candidate_count": len(checks),
        "candidate_limit": prepared.catalog["limits"],
        "verified_byte_locator_count": len(checks),
        "checks": checks,
        "account_mapping_draft": {
            "status": "draft",
            "approved": False,
            "c3_threshold": None,
            "allowed_capex_account_ids": [],
            "inventory": inventory,
            "decisions_required": [
                "cash versus accrual definition and reporting boundary",
                "tangible/intangible/lease/business acquisition inclusion",
                "gross/net, sign, currency, period and duplicate-account treatment",
                "approved account allowlist and investment-to-CAPEX threshold",
            ],
        },
        "authenticated_review_created": False,
        "policy_approved": False,
        "search_complete": False,
        "semantic_accuracy": None,
        "limitations": [
            "Byte/locator replay is not independent extraction accuracy or issuer authentication.",
            "Candidate truncation does not establish complete disclosure search.",
            "Lexical account hints are not policy-approved CAPEX classifications.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--manifest", required=True, type=Path)
    cli.add_argument("--store", required=True, type=Path)
    cli.add_argument("--corp-code", required=True)
    cli.add_argument("--fy", required=True, type=int)
    cli.add_argument("--rcept-no", required=True)
    cli.add_argument("--consolidation", required=True, choices=["consolidated", "separate"])
    cli.add_argument("--max-candidates", default=2000, type=int)
    cli.add_argument("--sr-sources", type=Path)
    cli.add_argument("--output", required=True, type=Path)
    args = cli.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError("output_exists")
        result = audit_sources(
            load_json(args.manifest),
            args.store,
            corp_code=args.corp_code,
            fy=args.fy,
            rcept_no=args.rcept_no,
            consolidation=args.consolidation,
            max_candidates=args.max_candidates,
            sr_manifest=load_json(args.sr_sources) if args.sr_sources else None,
            sr_root=args.sr_sources.resolve().parent if args.sr_sources else None,
        )
        write_json(args.output, result)
    except (ValueError, OSError, KeyError, TypeError):
        print(json.dumps({"error": "source_audit_rejected"}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
