"""Offline objective verification benchmark for reconciliation candidates.

The harness measures byte/hash/locator/quote/lineage facts only.  It does not
create human gold labels, approve policy, or score C1--C4 semantic accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from proofops.adapters.reconciliation import FileSourceReader

from evaluation.reconciliation_cli import InputRejected, load_json, write_json

SCHEMA_VERSION = "reconciliation-evaluation-manifest-1"
REPORT_VERSION = "reconciliation-evaluation-report-1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CORP_CODE = re.compile(r"^[0-9]{8}$")
_RECEIPT = re.compile(r"^[0-9]{14}$")
_SPLITS = {"development", "holdout"}
_ITEMS = {"C1", "C2", "C3", "C4"}
_CANDIDATE_TYPES = {
    "statement_row",
    "document_element",
    "xbrl_fact",
    "sustainability_source",
}
_CASE_FIELDS = {
    "case_id",
    "company_id",
    "split",
    "item",
    "candidate_catalog",
    "artifact_root",
    "originals",
    "expectations",
}
_EXPECTATION_FIELDS = {
    "candidate_type",
    "artifact_sha256",
    "locator",
    "quote_sha256",
    "original_artifact_sha256",
}
_ORIGINAL_FIELDS = {"path", "sha256"}
_MAX_ORIGINAL_BYTES = 50 * 1024 * 1024
UNMEASURED_SEMANTICS = (
    "candidate relevance or correctness for a reconciliation claim",
    "candidate discovery completeness, recall, or absence of omitted evidence",
    "C1-C4 reconciliation status, grade, label, or accounting interpretation",
    "policy approval, search completeness, and human review quality",
    "independent authentication of the manifest's company identity or split provenance",
)


def _text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputRejected(f"{name}_invalid")
    return value


def _hash(name: str, value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise InputRejected(f"{name}_invalid")
    return value


def _relative(name: str, value: object) -> str:
    text = _text(name, value)
    path = Path(text)
    if path.is_absolute() or ":" in text or any(part == ".." for part in path.parts):
        raise InputRejected(f"{name}_invalid")
    return text


def _contained(root: Path, relative: str, *, directory: bool = False) -> Path:
    try:
        value = (root / relative).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise InputRejected("referenced_path_unavailable") from exc
    if not value.is_relative_to(root) or (not value.is_dir() if directory else not value.is_file()):
        raise InputRejected("referenced_path_outside_manifest_root")
    return value


def _canonical_hash(value: object) -> str:
    raw = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _validate_manifest(value: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    if set(value) != {"schema_version", "dataset_id", "cases"}:
        raise InputRejected("manifest_fields_invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise InputRejected("manifest_schema_invalid")
    dataset_id = _text("dataset_id", value.get("dataset_id"))
    cases = value.get("cases")
    if not isinstance(cases, list):
        raise InputRejected("cases_invalid")
    case_ids: set[str] = set()
    company_splits: dict[str, set[str]] = defaultdict(set)
    original_splits: dict[str, set[str]] = defaultdict(set)
    validated: list[dict[str, Any]] = []
    for raw in cases:
        if not isinstance(raw, dict) or set(raw) != _CASE_FIELDS:
            raise InputRejected("case_fields_invalid")
        case_id = _text("case_id", raw["case_id"])
        company_id = _text("company_id", raw["company_id"])
        if _CORP_CODE.fullmatch(company_id) is None:
            raise InputRejected("company_id_invalid")
        if case_id in case_ids:
            raise InputRejected("duplicate_case_id")
        split = raw["split"]
        item = raw["item"]
        if not isinstance(split, str) or split not in _SPLITS:
            raise InputRejected("case_split_invalid")
        if not isinstance(item, str) or item not in _ITEMS:
            raise InputRejected("case_item_invalid")
        catalog = _relative("candidate_catalog", raw["candidate_catalog"])
        artifact_root = _relative("artifact_root", raw["artifact_root"])
        originals = raw["originals"]
        expectations = raw["expectations"]
        if not isinstance(originals, list) or not originals:
            raise InputRejected("originals_missing_or_invalid")
        if not isinstance(expectations, list) or not expectations:
            raise InputRejected("expectations_missing_or_invalid")
        clean_originals = []
        seen_paths: set[str] = set()
        for original in originals:
            if not isinstance(original, dict) or set(original) != _ORIGINAL_FIELDS:
                raise InputRejected("original_fields_invalid")
            path = _relative("original_path", original["path"])
            digest = _hash("original_sha256", original["sha256"])
            if path in seen_paths:
                raise InputRejected("duplicate_original_path")
            seen_paths.add(path)
            original_splits[digest].add(split)
            clean_originals.append({"path": path, "sha256": digest})
        clean_expectations = []
        for expectation in expectations:
            if not isinstance(expectation, dict) or set(expectation) != _EXPECTATION_FIELDS:
                raise InputRejected("expectation_fields_invalid")
            candidate_type = expectation["candidate_type"]
            if not isinstance(candidate_type, str) or candidate_type not in _CANDIDATE_TYPES:
                raise InputRejected("expectation_candidate_type_invalid")
            clean_expectations.append(
                {
                    "candidate_type": candidate_type,
                    "artifact_sha256": _hash(
                        "expectation_artifact_sha256", expectation["artifact_sha256"]
                    ),
                    "locator": _text("expectation_locator", expectation["locator"]),
                    "quote_sha256": _hash("expectation_quote_sha256", expectation["quote_sha256"]),
                    "original_artifact_sha256": _hash(
                        "expectation_original_artifact_sha256",
                        expectation["original_artifact_sha256"],
                    ),
                }
            )
        if len({_canonical_hash(item) for item in clean_expectations}) != len(clean_expectations):
            raise InputRejected("duplicate_expectation")
        case_ids.add(case_id)
        company_splits[company_id].add(split)
        validated.append(
            {
                **raw,
                "case_id": case_id,
                "company_id": company_id,
                "candidate_catalog": catalog,
                "artifact_root": artifact_root,
                "originals": clean_originals,
                "expectations": clean_expectations,
            }
        )
    if any(len(splits) != 1 for splits in company_splits.values()):
        raise InputRejected("development_holdout_company_overlap")
    if any(len(splits) != 1 for splits in original_splits.values()):
        raise InputRejected("development_holdout_original_overlap")
    return dataset_id, validated


def _artifact_index(
    catalog: Mapping[str, Any],
) -> tuple[dict[str, dict[str, str]], bool, dict[str, Any]]:
    if catalog.get("schema_version") != "reconciliation-candidates-1":
        raise ValueError("candidate_catalog_schema_invalid")
    artifacts = catalog.get("artifacts")
    candidates = catalog.get("candidates")
    limits = catalog.get("limits")
    identity = catalog.get("identity")
    if (
        not isinstance(artifacts, list)
        or not artifacts
        or not isinstance(candidates, list)
        or not isinstance(limits, Mapping)
        or type(limits.get("truncated")) is not bool
        or not isinstance(identity, Mapping)
    ):
        raise ValueError("candidate_catalog_content_invalid")
    corp_code = identity.get("corp_code")
    fiscal_year = identity.get("fy")
    receipt = identity.get("rcept_no")
    if (
        not isinstance(corp_code, str)
        or _CORP_CODE.fullmatch(corp_code) is None
        or type(fiscal_year) is not int
        or not 1900 <= fiscal_year <= 2200
        or not isinstance(receipt, str)
        or _RECEIPT.fullmatch(receipt) is None
    ):
        raise ValueError("candidate_catalog_identity_invalid")
    index: dict[str, dict[str, str]] = {}
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise ValueError("candidate_artifact_invalid")
        document_id = item.get("document_id")
        if not isinstance(document_id, str) or not document_id or document_id in index:
            raise ValueError("candidate_artifact_identity_invalid")
        path, format_name, digest = item.get("path"), item.get("format"), item.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(format_name, str)
            or not isinstance(digest, str)
        ):
            raise ValueError("candidate_artifact_fields_invalid")
        index[document_id] = {"path": path, "format": format_name, "sha256": digest}
    if any(not isinstance(candidate, Mapping) for candidate in candidates):
        raise ValueError("candidate_entry_invalid")
    return (
        index,
        limits["truncated"],
        {
            "corp_code": corp_code,
            "fy": fiscal_year,
            "rcept_no": receipt,
        },
    )


def _read_original(path: Path) -> tuple[str, bytes]:
    try:
        with path.open("rb") as stream:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_ORIGINAL_BYTES:
                raise ValueError("original_size_or_type_invalid")
            payload = stream.read(_MAX_ORIGINAL_BYTES + 1)
        if len(payload) > _MAX_ORIGINAL_BYTES or path.stat().st_size != info.st_size:
            raise ValueError("original_changed_or_too_large")
    except OSError as exc:
        raise ValueError("original_unreadable") from exc
    return hashlib.sha256(payload).hexdigest(), payload


def _reject_constant(_value: str) -> Any:
    raise ValueError("statement_json_non_finite")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("statement_json_duplicate_key")
        result[key] = value
    return result


def _verify_statement_identity(payload: bytes, identity: Mapping[str, Any]) -> bool:
    try:
        parsed = json.loads(
            payload.decode("utf-8-sig", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("statement_json_invalid") from exc
    if not isinstance(parsed, Mapping):
        raise ValueError("statement_json_shape_invalid")
    rows = parsed.get("list")
    if (
        parsed.get("status") != "000"
        or not isinstance(rows, list)
        or not rows
        or any(not isinstance(row, Mapping) for row in rows)
    ):
        raise ValueError("statement_json_shape_invalid")
    return all(
        row.get("corp_code") == identity["corp_code"]
        and row.get("bsns_year") == str(identity["fy"])
        and row.get("rcept_no") == identity["rcept_no"]
        for row in rows
    )


def _metric(name: str, numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "name": name,
        "status": "scored" if denominator else "not_run",
        "value": numerator / denominator if denominator else None,
        "numerator": numerator,
        "denominator": denominator,
    }


def _evaluate_case(case: Mapping[str, Any], root: Path) -> tuple[dict[str, Any], dict[str, int]]:
    failures: list[str] = []
    original_passes = 0
    verified_original_hashes: set[str] = set()
    verified_originals: list[tuple[str, bytes]] = []
    for original in case["originals"]:
        try:
            path = _contained(root, original["path"])
            actual, payload = _read_original(path)
            if actual != original["sha256"]:
                failures.append("original_hash_mismatch")
            else:
                original_passes += 1
                verified_original_hashes.add(actual)
                verified_originals.append((Path(original["path"]).suffix.lower(), payload))
        except (InputRejected, ValueError):
            failures.append("original_verification_failed")

    expectation_matches = 0
    source_verifications = 0
    expectation_results: list[dict[str, Any]] = []
    catalog_truncated: bool | None = None
    company_identity_match: bool | None = None
    statement_identity_passes = 0
    statement_identity_checks = 0
    try:
        catalog_path = _contained(root, case["candidate_catalog"])
        artifact_root = _contained(root, case["artifact_root"], directory=True)
        catalog = load_json(catalog_path)
        artifact_index, catalog_truncated, catalog_identity = _artifact_index(catalog)
        reader = FileSourceReader(artifact_root, artifact_index)
        candidates = catalog["candidates"]
        company_identity_match = catalog_identity["corp_code"] == case["company_id"]
        if not company_identity_match:
            failures.append("catalog_company_identity_mismatch")
        for suffix, payload in verified_originals:
            if suffix != ".json":
                continue
            statement_identity_checks += 1
            try:
                if _verify_statement_identity(payload, catalog_identity):
                    statement_identity_passes += 1
                else:
                    failures.append("original_statement_identity_mismatch")
            except ValueError:
                failures.append("original_statement_identity_invalid")
        if catalog_truncated:
            failures.append("candidate_catalog_truncated_incomplete_search")
    except (InputRejected, ValueError, OSError):
        candidates = []
        reader = None
        failures.append("candidate_catalog_or_artifacts_invalid")

    for index, expected in enumerate(case["expectations"]):
        item_failures: list[str] = []
        matching = []
        for candidate in candidates:
            source = candidate.get("source")
            lineage = candidate.get("lineage")
            if not isinstance(source, Mapping) or not isinstance(lineage, Mapping):
                continue
            quote = source.get("quote")
            if (
                candidate.get("candidate_type") == expected["candidate_type"]
                and source.get("artifact_sha256") == expected["artifact_sha256"]
                and source.get("locator") == expected["locator"]
                and isinstance(quote, str)
                and hashlib.sha256(quote.encode("utf-8")).hexdigest() == expected["quote_sha256"]
                and lineage.get("original_artifact_sha256") == expected["original_artifact_sha256"]
            ):
                matching.append(candidate)
        if len(matching) != 1:
            item_failures.append(
                "candidate_not_found" if not matching else "candidate_match_not_unique"
            )
        else:
            expectation_matches += 1
            if expected["original_artifact_sha256"] not in verified_original_hashes:
                item_failures.append("candidate_original_not_verified")
            try:
                if reader is None or not reader.validate(matching[0]["source"]):
                    raise ValueError("source_not_verified")
                source_verifications += 1
            except (ValueError, OSError):
                item_failures.append("candidate_source_verification_failed")
        failures.extend(item_failures)
        expectation_results.append(
            {
                "expectation_index": index,
                "status": "pass" if not item_failures else "fail",
                "failure_codes": sorted(set(item_failures)),
            }
        )

    result = {
        "case_id": case["case_id"],
        "company_id": case["company_id"],
        "split": case["split"],
        "item": case["item"],
        "status": "pass" if not failures else "fail",
        "failure_codes": sorted(set(failures)),
        "checks": {
            "original_artifacts": {
                "passed": original_passes,
                "denominator": len(case["originals"]),
            },
            "original_statement_identity": {
                "passed": statement_identity_passes,
                "denominator": statement_identity_checks,
            },
            "candidate_catalog": {
                "truncated": catalog_truncated,
                "company_identity_match": company_identity_match,
                "search_completeness": (
                    "incomplete_truncated" if catalog_truncated else "not_measured"
                ),
            },
            "expectations": expectation_results,
        },
    }
    counts = {
        "original_passes": original_passes,
        "originals": len(case["originals"]),
        "statement_identity_passes": statement_identity_passes,
        "statement_identity_checks": statement_identity_checks,
        "catalog_company_matches": int(company_identity_match is True),
        "catalog_company_checks": int(company_identity_match is not None),
        "expectation_matches": expectation_matches,
        "source_verifications": source_verifications,
        "expectations": len(case["expectations"]),
        "case_passes": int(not failures),
    }
    return result, counts


def evaluate_manifest(manifest: Mapping[str, Any], *, manifest_root: Path) -> dict[str, Any]:
    """Evaluate a validated manifest without changing any referenced artifact."""
    dataset_id, cases = _validate_manifest(manifest)
    root = manifest_root.resolve(strict=True)
    if not root.is_dir():
        raise InputRejected("manifest_root_invalid")
    results = []
    totals: defaultdict[str, int] = defaultdict(int)
    for case in cases:
        result, counts = _evaluate_case(case, root)
        results.append(result)
        for key, value in counts.items():
            totals[key] += value
    metrics = [
        _metric("case_pass_rate", totals["case_passes"], len(cases)),
        _metric(
            "original_artifact_hash_match_rate", totals["original_passes"], totals["originals"]
        ),
        _metric(
            "original_statement_identity_match_rate",
            totals["statement_identity_passes"],
            totals["statement_identity_checks"],
        ),
        _metric(
            "catalog_company_identity_match_rate",
            totals["catalog_company_matches"],
            totals["catalog_company_checks"],
        ),
        _metric(
            "candidate_expectation_exact_match_rate",
            totals["expectation_matches"],
            totals["expectations"],
        ),
        _metric(
            "candidate_source_verification_rate",
            totals["source_verifications"],
            totals["expectations"],
        ),
    ]
    return {
        "schema_version": REPORT_VERSION,
        "dataset_id": dataset_id,
        "manifest_content_sha256": _canonical_hash(manifest),
        "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "overall_status": (
            "pass" if cases and all(r["status"] == "pass" for r in results) else "fail"
        ),
        "cases": results,
        "metrics": metrics,
        "unmeasured_semantics": list(UNMEASURED_SEMANTICS),
        "claims": {
            "human_gold_labels_used": False,
            "policy_approved_by_evaluator": False,
            "semantic_or_grade_accuracy_measured": False,
        },
    }


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--manifest", required=True, type=Path)
    cli.add_argument("--output", required=True, type=Path)
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.output.exists():
            raise InputRejected("output_exists_use_new_revision")
        manifest = load_json(args.manifest)
        report = evaluate_manifest(manifest, manifest_root=args.manifest.resolve().parent)
        write_json(args.output, report)
        return 0 if report["overall_status"] == "pass" else 1
    except (InputRejected, PermissionError, FileExistsError) as exc:
        code = str(exc) if isinstance(exc, InputRejected) else "evaluation_input_rejected"
        print(json.dumps({"error": code}), file=sys.stderr)
        return 2
    except Exception:
        print('{"error":"evaluation_failed"}', file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
