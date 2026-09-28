from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from evaluation.reconciliation_benchmark import evaluate_manifest, main
from evaluation.reconciliation_cli import InputRejected


def digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def fixture(tmp_path: Path, *, split: str = "holdout", company_id: str = "00141529"):
    artifact_root = tmp_path / "prepared"
    artifact_root.mkdir()
    receipt = "20250318000979"
    original = json.dumps(
        {
            "status": "000",
            "list": [
                {
                    "corp_code": company_id,
                    "bsns_year": "2024",
                    "rcept_no": receipt,
                }
            ],
        },
        separators=(",", ":"),
    ).encode()
    derived = b"objective evidence text"
    (tmp_path / "original.json").write_bytes(original)
    (artifact_root / "candidate.txt").write_bytes(derived)
    original_hash = digest(original)
    artifact_hash = digest(derived)
    quote = derived.decode()
    locator = f"chars:0:{len(quote)}"
    catalog = {
        "schema_version": "reconciliation-candidates-1",
        "identity": {
            "corp_code": company_id,
            "fy": 2024,
            "rcept_no": receipt,
            "consolidation": "consolidated",
        },
        "limits": {"max_candidates": 2_000, "truncated": False},
        "artifacts": [
            {
                "document_id": "doc-1",
                "path": "candidate.txt",
                "format": "text",
                "sha256": artifact_hash,
                "lineage": {},
            }
        ],
        "candidates": [
            {
                "candidate_id": "candidate-1",
                "candidate_type": "sustainability_source",
                "verification_state": "candidate",
                "source": {
                    "source_id": "source-1",
                    "document_id": "doc-1",
                    "artifact_sha256": artifact_hash,
                    "locator": locator,
                    "quote": quote,
                },
                "lineage": {
                    "representation": "derived",
                    "original_artifact_sha256": original_hash,
                },
            }
        ],
    }
    (tmp_path / "candidates.json").write_text(json.dumps(catalog), encoding="utf-8")
    case = {
        "case_id": "case-1",
        "company_id": company_id,
        "split": split,
        "item": "C1",
        "candidate_catalog": "candidates.json",
        "artifact_root": "prepared",
        "originals": [{"path": "original.json", "sha256": original_hash}],
        "expectations": [
            {
                "candidate_type": "sustainability_source",
                "artifact_sha256": artifact_hash,
                "locator": locator,
                "quote_sha256": digest(quote.encode()),
                "original_artifact_sha256": original_hash,
            }
        ],
    }
    return {
        "schema_version": "reconciliation-evaluation-manifest-1",
        "dataset_id": "real-original-evaluation-1",
        "cases": [case],
    }


def metric(report: dict, name: str) -> dict:
    return next(value for value in report["metrics"] if value["name"] == name)


def test_objective_original_candidate_locator_and_hash_facts_pass(tmp_path):
    report = evaluate_manifest(fixture(tmp_path), manifest_root=tmp_path)
    assert report["overall_status"] == "pass"
    assert report["cases"][0]["status"] == "pass"
    assert metric(report, "case_pass_rate") == {
        "name": "case_pass_rate",
        "status": "scored",
        "value": 1.0,
        "numerator": 1,
        "denominator": 1,
    }
    assert report["claims"] == {
        "human_gold_labels_used": False,
        "policy_approved_by_evaluator": False,
        "semantic_or_grade_accuracy_measured": False,
    }


def test_tampered_candidate_bytes_fail_closed_without_hiding_denominators(tmp_path):
    manifest = fixture(tmp_path)
    (tmp_path / "prepared" / "candidate.txt").write_bytes(b"tampered")
    report = evaluate_manifest(manifest, manifest_root=tmp_path)
    case = report["cases"][0]
    assert report["overall_status"] == "fail"
    assert case["status"] == "fail"
    assert "candidate_source_verification_failed" in case["failure_codes"]
    assert metric(report, "candidate_expectation_exact_match_rate")["value"] == 1.0
    assert metric(report, "candidate_source_verification_rate") == {
        "name": "candidate_source_verification_rate",
        "status": "scored",
        "value": 0.0,
        "numerator": 0,
        "denominator": 1,
    }


def test_original_tamper_breaks_lineage_even_when_prepared_source_still_matches(tmp_path):
    manifest = fixture(tmp_path)
    (tmp_path / "original.json").write_bytes(b"different original")
    report = evaluate_manifest(manifest, manifest_root=tmp_path)
    assert report["cases"][0]["status"] == "fail"
    assert set(report["cases"][0]["failure_codes"]) == {
        "candidate_original_not_verified",
        "original_hash_mismatch",
    }
    assert metric(report, "original_artifact_hash_match_rate")["value"] == 0.0


def test_truncated_catalog_is_explicitly_incomplete_and_fails_case(tmp_path):
    manifest = fixture(tmp_path)
    catalog_path = tmp_path / "candidates.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["limits"]["truncated"] = True
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    report = evaluate_manifest(manifest, manifest_root=tmp_path)
    case = report["cases"][0]
    assert case["status"] == "fail"
    assert "candidate_catalog_truncated_incomplete_search" in case["failure_codes"]
    assert case["checks"]["candidate_catalog"] == {
        "truncated": True,
        "company_identity_match": True,
        "search_completeness": "incomplete_truncated",
    }
    assert metric(report, "candidate_expectation_exact_match_rate")["value"] == 1.0
    assert metric(report, "candidate_source_verification_rate")["value"] == 1.0


def test_company_label_must_match_catalog_corp_code_without_suppressing_source_metrics(
    tmp_path,
):
    manifest = fixture(tmp_path)
    manifest["cases"][0]["company_id"] = "00126380"
    report = evaluate_manifest(manifest, manifest_root=tmp_path)
    case = report["cases"][0]
    assert case["status"] == "fail"
    assert "catalog_company_identity_mismatch" in case["failure_codes"]
    assert case["checks"]["candidate_catalog"]["company_identity_match"] is False
    assert metric(report, "catalog_company_identity_match_rate")["value"] == 0.0
    assert metric(report, "candidate_expectation_exact_match_rate")["value"] == 1.0
    assert metric(report, "candidate_source_verification_rate")["value"] == 1.0


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("corp_code", "00999999"),
        ("bsns_year", "2023"),
        ("rcept_no", "20240101000000"),
    ],
)
def test_statement_original_rows_must_match_catalog_identity(tmp_path, field, changed):
    manifest = fixture(tmp_path)
    original_path = tmp_path / "original.json"
    parsed = json.loads(original_path.read_text(encoding="utf-8"))
    parsed["list"][0][field] = changed
    payload = json.dumps(parsed, separators=(",", ":")).encode()
    original_path.write_bytes(payload)
    changed_hash = digest(payload)
    manifest["cases"][0]["originals"][0]["sha256"] = changed_hash
    manifest["cases"][0]["expectations"][0]["original_artifact_sha256"] = changed_hash
    catalog_path = tmp_path / "candidates.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    catalog["candidates"][0]["lineage"]["original_artifact_sha256"] = changed_hash
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    report = evaluate_manifest(manifest, manifest_root=tmp_path)
    case = report["cases"][0]
    assert "original_statement_identity_mismatch" in case["failure_codes"]
    assert case["checks"]["original_artifacts"] == {"passed": 1, "denominator": 1}
    assert case["checks"]["original_statement_identity"] == {"passed": 0, "denominator": 1}
    assert metric(report, "original_statement_identity_match_rate")["value"] == 0.0


def test_development_and_holdout_company_overlap_is_rejected(tmp_path):
    manifest = fixture(tmp_path)
    second = dict(manifest["cases"][0])
    second.update(case_id="case-2", split="development")
    manifest["cases"].append(second)
    with pytest.raises(InputRejected, match="development_holdout_company_overlap"):
        evaluate_manifest(manifest, manifest_root=tmp_path)


def test_original_digest_cannot_cross_development_and_holdout(tmp_path):
    manifest = fixture(tmp_path)
    second = dict(manifest["cases"][0])
    second.update(case_id="case-2", company_id="00126380", split="development")
    manifest["cases"].append(second)
    with pytest.raises(InputRejected, match="development_holdout_original_overlap"):
        evaluate_manifest(manifest, manifest_root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("split", [], "case_split_invalid"),
        ("item", {}, "case_item_invalid"),
        ("company_id", [], "company_id_invalid"),
    ],
)
def test_unhashable_case_enum_values_are_input_rejections(tmp_path, field, value, error):
    manifest = fixture(tmp_path)
    manifest["cases"][0][field] = value
    with pytest.raises(InputRejected, match=error):
        evaluate_manifest(manifest, manifest_root=tmp_path)


def test_unhashable_candidate_type_is_an_input_rejection(tmp_path):
    manifest = fixture(tmp_path)
    manifest["cases"][0]["expectations"][0]["candidate_type"] = []
    with pytest.raises(InputRejected, match="expectation_candidate_type_invalid"):
        evaluate_manifest(manifest, manifest_root=tmp_path)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda case: case.pop("expectations"),
        lambda case: case.update(expectations=[]),
        lambda case: case["expectations"][0].update(quote_sha256="not-a-hash"),
        lambda case: case["expectations"][0].pop("locator"),
    ],
)
def test_missing_or_invalid_expectations_are_input_errors(tmp_path, mutation):
    manifest = fixture(tmp_path)
    mutation(manifest["cases"][0])
    with pytest.raises(InputRejected):
        evaluate_manifest(manifest, manifest_root=tmp_path)


def test_zero_cases_never_reports_false_one_hundred_percent(tmp_path):
    manifest = {
        "schema_version": "reconciliation-evaluation-manifest-1",
        "dataset_id": "empty-held-out-set",
        "cases": [],
    }
    report = evaluate_manifest(manifest, manifest_root=tmp_path)
    assert report["overall_status"] == "fail"
    assert all(
        item["status"] == "not_run" and item["value"] is None and item["denominator"] == 0
        for item in report["metrics"]
    )


def test_cli_output_is_immutable_and_contains_no_manifest_paths_or_quotes(tmp_path):
    manifest = fixture(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    output = tmp_path / "report.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert main(["--manifest", str(manifest_path), "--output", str(output)]) == 0
    raw = output.read_text(encoding="utf-8")
    assert "objective evidence text" not in raw
    assert "candidate.txt" not in raw
    assert main(["--manifest", str(manifest_path), "--output", str(output)]) == 2


def test_cli_rejects_invalid_expectation_without_writing_report(tmp_path):
    manifest = fixture(tmp_path)
    manifest["cases"][0]["expectations"] = []
    manifest_path = tmp_path / "manifest.json"
    output = tmp_path / "report.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert main(["--manifest", str(manifest_path), "--output", str(output)]) == 2
    assert not output.exists()
