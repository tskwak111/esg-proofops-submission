"""AT-023: local synthetic year snapshots; no model, AWS, or customer data."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from proofops.application.comparisons import (
    ApprovedVersion,
    TargetSnapshot,
    compare_years,
)

TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN_TENANT = "22222222-2222-4222-8222-222222222222"
COMPANY = "33333333-3333-4333-8333-333333333333"
FOREIGN_COMPANY = "44444444-4444-4222-8222-444444444444"
CURRENT_VERSION = "55555555-5555-4555-8555-555555555555"
PRIOR_VERSION = "66666666-6666-4666-8666-666666666666"
COMPARISON = "77777777-7777-4777-8777-777777777777"


def target(
    claim_id: str,
    key: str,
    content: str,
    grade: str | None = "E1",
) -> TargetSnapshot:
    return TargetSnapshot(
        claim_id=claim_id,
        comparison_key=key,
        content_sha256=sha256(content.encode()).hexdigest(),
        decision_revision=1,
        evidence_grade=grade,
    )


def version(
    year: int,
    version_id: str,
    *targets: TargetSnapshot,
    tenant_id: str = TENANT,
    company_id: str = COMPANY,
    approved: bool = True,
) -> ApprovedVersion:
    return ApprovedVersion(
        tenant_id=tenant_id,
        company_id=company_id,
        document_version_id=version_id,
        report_year=year,
        approved=approved,
        targets=targets,
    )


def validate_comparison(payload: dict) -> None:
    root = Path(__file__).resolve().parents[2]
    schema = json.loads((root / "contracts/jsonschema/api_models.schema.json").read_text())
    Draft202012Validator(schema["$defs"]["Comparison"]).validate(payload)


def test_missing_prior_is_not_run_and_cannot_upgrade_current_grade() -> None:
    current_target = target(
        "88888888-8888-4888-8888-888888888888",
        "climate/net-zero",
        "Current target with current-year evidence only",
        "E1",
    )
    current = version(2026, CURRENT_VERSION, current_target)
    before = asdict(current)

    result = compare_years(current, None, comparison_id=COMPARISON)
    payload = result.to_api_dict()

    assert payload == {
        "comparison_id": COMPARISON,
        "status": "not_run",
        "reason": "prior_document_version_missing",
        "changes": [],
    }
    assert asdict(current) == before
    assert current.targets[0].evidence_grade == "E1"
    assert not ({"evidence_grade", "label"} & set(payload))
    validate_comparison(payload)


def test_previous_year_emits_only_change_candidates() -> None:
    modified_id = "88888888-8888-4888-8888-888888888888"
    removed_id = "99999999-9999-4999-8999-999999999999"
    new_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    prior_modified_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    current = version(
        2026,
        CURRENT_VERSION,
        target(modified_id, "climate/net-zero", "2050 target", "E1"),
        target(new_id, "water/withdrawal", "New water target", "E0"),
    )
    prior = version(
        2025,
        PRIOR_VERSION,
        target(prior_modified_id, "climate/net-zero", "2040 target", "E3"),
        target(removed_id, "waste/landfill", "Prior waste target", "E3"),
    )

    payload = compare_years(current, prior, comparison_id=COMPARISON).to_api_dict()

    assert payload["status"] == "completed" and payload["reason"] is None
    assert payload["changes"] == [
        {
            "current_claim_id": modified_id,
            "prior_claim_id": prior_modified_id,
            "type": "modified",
            "reason": "target_content_changed",
        },
        {
            "current_claim_id": new_id,
            "prior_claim_id": None,
            "type": "new",
            "reason": "target_new_in_current_version",
        },
        {
            "current_claim_id": None,
            "prior_claim_id": removed_id,
            "type": "removed_candidate",
            "reason": "target_missing_from_current_version",
        },
    ]
    assert current.targets[0].evidence_grade == "E1"
    assert all(
        "grade" not in key and "label" not in key for item in payload["changes"] for key in item
    )
    validate_comparison(payload)


@pytest.mark.parametrize(
    "prior",
    [
        version(2025, PRIOR_VERSION, tenant_id=FOREIGN_TENANT),
        version(2025, PRIOR_VERSION, company_id=FOREIGN_COMPANY),
    ],
)
def test_cross_tenant_or_company_comparison_is_rejected(prior: ApprovedVersion) -> None:
    with pytest.raises(ValueError, match="identity"):
        compare_years(version(2026, CURRENT_VERSION), prior, comparison_id=COMPARISON)


@pytest.mark.parametrize("which", ["current", "prior"])
def test_unapproved_version_is_rejected(which: str) -> None:
    current = version(2026, CURRENT_VERSION, approved=which != "current")
    prior = version(2025, PRIOR_VERSION, approved=which != "prior")

    with pytest.raises(ValueError, match="approved"):
        compare_years(current, prior, comparison_id=COMPARISON)


def test_non_previous_year_is_not_run_without_change_claims() -> None:
    current = version(2026, CURRENT_VERSION, target(COMPARISON, "climate", "current"))
    prior = version(2024, PRIOR_VERSION, target(PRIOR_VERSION, "climate", "prior"))

    result = compare_years(current, prior, comparison_id=COMPARISON)

    assert result.status == "not_run"
    assert result.reason == "prior_document_version_not_previous_year"
    assert result.changes == ()


def test_duplicate_matching_key_is_ambiguous_instead_of_guessed() -> None:
    duplicate = target(COMPARISON, "climate", "one")
    other_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    current = version(
        2026,
        CURRENT_VERSION,
        duplicate,
        replace(duplicate, claim_id=other_id),
    )
    prior = version(2025, PRIOR_VERSION, target(PRIOR_VERSION, "climate", "prior"))

    result = compare_years(current, prior, comparison_id=COMPARISON)

    assert result.to_api_dict()["changes"] == [
        {
            "current_claim_id": COMPARISON,
            "prior_claim_id": None,
            "type": "ambiguous",
            "reason": "comparison_key_not_unique",
        },
        {
            "current_claim_id": other_id,
            "prior_claim_id": None,
            "type": "ambiguous",
            "reason": "comparison_key_not_unique",
        },
        {
            "current_claim_id": None,
            "prior_claim_id": PRIOR_VERSION,
            "type": "ambiguous",
            "reason": "comparison_key_not_unique",
        },
    ]


def test_comparison_is_stateless_under_concurrent_replay() -> None:
    current = version(2026, CURRENT_VERSION, target(COMPARISON, "climate", "current"))
    prior = version(2025, PRIOR_VERSION, target(PRIOR_VERSION, "climate", "prior"))

    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(
            workers.map(
                lambda _: compare_years(current, prior, comparison_id=COMPARISON),
                range(32),
            )
        )

    assert all(result == results[0] for result in results)


def test_comparison_page_marks_not_run_and_removal_as_candidates(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    esbuild = next((root / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    entry = tmp_path / "comparison-render.tsx"
    entry.write_text(
        """
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { ComparisonPage } from COMPONENT;
import assert from "node:assert/strict";
const missing=renderToStaticMarkup(React.createElement(ComparisonPage,{
  currentYear:2026,priorVersions:[],comparison:{comparison_id:"id",status:"not_run",
  reason:"prior_document_version_missing",changes:[]}}));
assert.ok(missing.includes("전년 문서가 없어 비교를 실행하지 않았습니다."));
const unavailable=renderToStaticMarkup(React.createElement(ComparisonPage,{
  currentYear:2026,priorVersions:[{document_version_id:"prior",report_year:2025}],
  selectedPriorVersionId:"prior",comparison:{comparison_id:"id",status:"not_run",
  reason:"prior_comparison_artifact_missing",changes:[]}}));
assert.ok(unavailable.includes("전년 문서의 승인된 비교 근거가 없어 비교를 실행하지 않았습니다."));
assert.ok(!unavailable.includes("직전 연도 문서가 아니어서"));
const completed=renderToStaticMarkup(React.createElement(ComparisonPage,{
  currentYear:2026,priorVersions:[{document_version_id:"prior",report_year:2025}],
  selectedPriorVersionId:"prior",comparison:{comparison_id:"id",status:"completed",reason:null,
  changes:[{current_claim_id:null,prior_claim_id:"claim",type:"removed_candidate",
  reason:"target_missing_from_current_version"}]}}));
for (const text of ["목표 삭제 후보","삭제로 확정하지 않습니다.",
"전년 근거는 현재 연도 등급에 사용하지 않습니다."])
  assert.ok(completed.includes(text), text);
assert.ok(!completed.includes("E3"));
console.log("ComparisonPage candidate-state checks passed");
""".replace("REACT", json.dumps(str(root / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(root / "apps/web/node_modules/react-dom/server.node.js")))
        .replace(
            "COMPONENT",
            json.dumps(str(root / "apps/web/src/features/comparison/ComparisonPage.tsx")),
        )
    )
    bundle = tmp_path / "comparison-render.cjs"
    subprocess.run(
        [
            str(esbuild),
            str(entry),
            "--bundle",
            "--platform=node",
            "--format=cjs",
            "--jsx=automatic",
            f"--outfile={bundle}",
        ],
        check=True,
        capture_output=True,
    )
    rendered = subprocess.run(["node", str(bundle)], check=True, capture_output=True, text=True)
    assert "checks passed" in rendered.stdout
