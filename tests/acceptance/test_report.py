"""AT-021: report projection over explicit local-synthetic immutable inputs."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from csv import reader
from io import StringIO
from pathlib import Path

import pytest
from proofops.application.reporting import build_report_model, render_report

TENANT = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"
DOCUMENT = "55555555-5555-4555-8555-555555555555"
CLAIMS = (
    "33333333-3333-4333-8333-333333333331",
    "33333333-3333-4333-8333-333333333332",
    "33333333-3333-4333-8333-333333333333",
)
HASH = "a" * 64
MODEL = "b" * 64
PROMPT = "c" * 64
REPLICAS = ["1" * 64, "2" * 64, "3" * 64]


def source_ref(*, page=34, quote="2030년까지 탄소 배출량을 40% 감축하겠습니다."):
    return {
        "source_id": "66666666-6666-4666-8666-666666666666",
        "document_version_id": DOCUMENT,
        "parse_manifest_id": DOCUMENT,
        "page_num": page,
        "printed_page_label": None,
        "bbox": [120, 340, 480, 372],
        "raw_text_sha256": HASH,
        "quote": quote,
        "char_start": 0,
        "char_end": len(quote),
        "location_quality": "located",
        "verification_state": "verified",
    }


def manifest():
    return {
        "tenant_id": TENANT,
        "run_id": RUN,
        "document_version_id": DOCUMENT,
        "parse_manifest_id": DOCUMENT,
        "source_sha256": HASH,
        "mutation_epoch": 7,
        "claim_revision_refs": [
            {"claim_id": CLAIMS[0], "tag_revision": 1, "decision_revision": 1},
            {"claim_id": CLAIMS[1], "tag_revision": 2, "decision_revision": 2},
            {"claim_id": CLAIMS[2], "tag_revision": 0, "decision_revision": 0},
        ],
        "rule_pack_hashes": [HASH],
        "coverage": {
            "pages_total": 3,
            "pages_processed": 1,
            "pages_unreadable": 1,
            "pages_unprocessed": 1,
            "chunks_discovered": 3,
            "chunks_processed": 2,
            "claims_discovered": 3,
            "claims_decided": 1,
            "claims_needs_review": 1,
            "full_scope": True,
            "complete": False,
        },
        "unverified_basis": 2,
        "generated_at": "2026-09-09T00:00:00Z",
        "execution_profile": "local-synthetic-only",
    }


def decisions():
    return {
        CLAIMS[0]: {
            "tenant_id": TENANT,
            "document_version_id": DOCUMENT,
            "parse_manifest_id": DOCUMENT,
            "source_sha256": HASH,
            "claim_id": CLAIMS[0],
            "tag_revision": 1,
            "decision_revision": 1,
            "decision_status": "decided",
            "evidence_grade": "E1",
            "label": "INCOMPLETE",
            "review_status": "auto_confirmed",
            "missing_elements": ["G3", "G4"],
            "unresolved_elements": [],
            "gap_ids": ["GAP-005"],
            "rule_pack_sha256": HASH,
            "model_sha256": MODEL,
            "prompt_sha256": PROMPT,
            "replicate_hashes": REPLICAS,
            "source_refs": [source_ref()],
            "basis_refs": [
                json.dumps(
                    {
                        "source_section": "4.4",
                        "clause": None,
                        "verification_status": "unverified",
                        "rule_ids": ["goal-e1"],
                    }
                )
            ],
            "assurance": {
                "status": "not_covered",
                "level": None,
                "provider": None,
                "statement_id": None,
                "metric_match": "no",
                "period_match": "unknown",
                "boundary_match": "unknown",
                "evidence_refs": [],
            },
            "safe_harbor": {
                "claim_id": CLAIMS[0],
                "applicable": False,
                "category": None,
                "checklist": [],
                "reasonable_basis_documented": None,
                "legal_effect": "not_determined",
                "mapping_status": "unresolved",
                "gap_ids": [],
            },
        },
        CLAIMS[1]: {
            "tenant_id": TENANT,
            "document_version_id": DOCUMENT,
            "parse_manifest_id": DOCUMENT,
            "source_sha256": HASH,
            "claim_id": CLAIMS[1],
            "tag_revision": 2,
            "decision_revision": 2,
            "decision_status": "blocked_rule_gap",
            "evidence_grade": None,
            "label": None,
            "review_status": "needs_review",
            "missing_elements": [],
            "unresolved_elements": ["P6"],
            "gap_ids": ["GAP-003"],
            "rule_pack_sha256": HASH,
            "model_sha256": MODEL,
            "prompt_sha256": PROMPT,
            "replicate_hashes": REPLICAS,
            "source_refs": [],
            "basis_refs": [
                {
                    "source_section": "4.5",
                    "clause": None,
                    "verification_status": "unlicensed",
                    "rule_ids": [],
                }
            ],
            "assurance": {
                "status": "undetermined",
                "level": None,
                "provider": None,
                "statement_id": None,
                "metric_match": "unknown",
                "period_match": "unknown",
                "boundary_match": "unknown",
                "evidence_refs": [],
            },
            "safe_harbor": {
                "claim_id": CLAIMS[1],
                "applicable": True,
                "category": "emissions_estimate",
                "checklist": [
                    {
                        "element_id": "uncertainty",
                        "state": "unknown",
                        "evidence_refs": [],
                        "normalized_value": None,
                        "credited_from": None,
                        "reason_code": None,
                    }
                ],
                "reasonable_basis_documented": None,
                "legal_effect": "not_determined",
                "mapping_status": "unresolved",
                "gap_ids": ["GAP-001"],
            },
        },
        CLAIMS[2]: {
            "tenant_id": TENANT,
            "document_version_id": DOCUMENT,
            "parse_manifest_id": DOCUMENT,
            "source_sha256": HASH,
            "claim_id": CLAIMS[2],
            "tag_revision": 0,
            "decision_revision": 0,
            "decision_status": "not_run",
            "evidence_grade": None,
            "label": None,
            "source_refs": [source_ref(page=2, quote="합성 미태깅 주장")],
            "model_sha256": None,
            "prompt_sha256": None,
            "replicate_hashes": [],
        },
    }


def test_report_keeps_partial_work_unverified_clauses_and_source_locations_visible():
    model = build_report_model(manifest(), decisions())

    assert model["schema"] == "report_model_v1"
    assert model["partial"] is True
    assert model["unfinished_count"] == 2
    assert model["coverage"]["pages_unreadable"] == 1
    assert model["coverage"]["pages_unprocessed"] == 1
    assert model["unverified_clause_count"] == 2
    assert len(model["claims"]) == 3

    decided, blocked, not_run = model["claims"]
    assert decided["source_refs"][0] == decisions()[CLAIMS[0]]["source_refs"][0]
    assert decided["basis_refs"][0]["clause"] is None
    assert decided["basis_refs"][0]["verification_status"] == "unverified"
    assert blocked["decision_status"] == "blocked_rule_gap"
    assert blocked["evidence_grade"] is blocked["label"] is None
    assert blocked["unresolved_elements"] == ["P6"]
    assert blocked["safe_harbor"]["mapping_status"] == "unresolved"
    assert blocked["safe_harbor"]["legal_effect"] == "not_determined"
    assert not_run["decision_status"] == "not_run"
    assert not_run["assurance"] == not_run["safe_harbor"] == {"status": "not_run"}
    assert not_run["source_status"] == "available"
    assert not_run["source_refs"][0]["quote"] == "합성 미태깅 주장"
    assert not_run["tag_revision"] == 0
    assert not_run["model_sha256"] is None and not_run["replicate_hashes"] == []


def test_suggestion_names_only_missing_elements_and_never_fills_values():
    model = build_report_model(manifest(), decisions())
    suggestion = model["claims"][0]["suggestion"]

    assert suggestion == "원문 근거로 다음 결손 요소를 보완하세요: G3, G4."
    assert all(value not in suggestion for value in ("2020", "2030", "40%"))
    assert model["claims"][1]["suggestion"] is None
    assert model["claims"][2]["suggestion"] is None


def test_review_action_guides_unresolved_work_without_replacing_suggestion():
    """review_action is a distinct, additive follow-up for unresolved/not-run/blocked
    claims. It preserves suggestion (verified-missing only), keeps source/claim links,
    and never invents numbers, grades, or legal facts."""
    model = build_report_model(manifest(), decisions())
    decided, blocked, not_run = model["claims"]

    # suggestion keeps its exact original meaning and value.
    assert decided["suggestion"] == "원문 근거로 다음 결손 요소를 보완하세요: G3, G4."
    assert blocked["suggestion"] is None and not_run["suggestion"] is None

    # A fully decided claim with missing elements already covered by suggestion:
    # review_action still surfaces its distinct outstanding reasons (domain gap,
    # assurance not covered is not "not_run" here so excluded) without a grade.
    decided_action = decided["review_action"]
    assert "unresolved_evidence" not in decided_action["reasons"]
    assert "domain_gap" in decided_action["reasons"]
    assert decided_action["gap_ids"] == ["GAP-005"]
    assert "판정 보류" not in " ".join(decided_action["checks"])
    assert "basis_validation_pending" in decided_action["reasons"]
    assert decided_action["claim_id"] == CLAIMS[0]
    assert decided_action["source_pages"] == [34]
    assert all(
        value not in " ".join(decided_action["checks"])
        for value in ("2020", "2030", "40%", "E1", "E3")
    )

    # blocked_rule_gap: unresolved elements + unverified basis + gap + no source.
    # (assurance here is "undetermined" and safe_harbor is a real record, so those
    # "not_run" reasons must NOT fire — uncertainty is not converted to not-run.)
    action = blocked["review_action"]
    assert action is not None
    assert action["reasons"][0] == "unresolved_evidence"
    assert set(action["reasons"]) == {
        "unresolved_evidence",
        "source_location_missing",
        "basis_validation_pending",
        "domain_gap",
    }
    assert "assurance_not_run" not in action["reasons"]
    assert "safe_harbor_not_run" not in action["reasons"]
    assert action["unresolved_elements"] == ["P6"]
    assert action["gap_ids"] == ["GAP-003"]
    assert action["source_pages"] == []
    assert action["claim_id"] == CLAIMS[1]
    # Never invents a grade, number, or legal effect.
    joined = " ".join(action["checks"])
    assert all(token not in joined for token in ("E0", "E1", "E2", "E3", "위반", "달성"))
    # unresolved must not be reworded into absent/verified-missing.
    assert "결손" not in joined

    # not_run (untagged) claim: classification/processing is the outstanding work.
    action = not_run["review_action"]
    assert action is not None
    assert "not_processed" in action["reasons"]
    assert action["claim_id"] == CLAIMS[2]
    assert action["source_pages"] == [2]


def test_review_action_is_null_only_when_no_outstanding_review_remains():
    """A decided claim with sources, no missing/unresolved elements, no gaps, and
    completed assurance/safe_harbor has no review_action."""
    snapshot, revisions = manifest(), decisions()
    revisions[CLAIMS[0]]["missing_elements"] = []
    revisions[CLAIMS[0]]["gap_ids"] = []
    revisions[CLAIMS[0]]["basis_refs"] = []
    revisions[CLAIMS[0]]["assurance"] = {
        "status": "covered",
        "level": "limited",
        "provider": "Synthetic provider",
        "statement_id": "66666666-6666-4666-8666-666666666666",
        "metric_match": "yes",
        "period_match": "yes",
        "boundary_match": "yes",
        "evidence_refs": [source_ref()],
    }
    revisions[CLAIMS[0]]["safe_harbor"] = {
        "claim_id": CLAIMS[0],
        "applicable": True,
        "category": "emissions_estimate",
        "checklist": [],
        "reasonable_basis_documented": True,
        "legal_effect": "not_determined",
        "mapping_status": "approved",
        "gap_ids": [],
    }
    snapshot["unverified_basis"] = 1  # only CLAIMS[1] retains an unverified clause

    decided = build_report_model(snapshot, revisions)["claims"][0]
    assert decided["review_action"] is None
    assert decided["suggestion"] is None


@pytest.mark.parametrize(
    "change",
    [
        "tenant",
        "document",
        "source_document",
        "safe_harbor_claim",
        "untagged_model",
        "decided_no_source",
        "revision",
        "rule_pack",
        "decided_without_grade",
        "missing_claim",
    ],
)
def test_report_rejects_mixed_or_unpinned_revision_inputs(change):
    snapshot, revisions = manifest(), decisions()
    if change == "tenant":
        snapshot["tenant_id"] = "44444444-4444-4444-8444-444444444444"
    elif change == "document":
        revisions[CLAIMS[0]]["document_version_id"] = RUN
    elif change == "source_document":
        revisions[CLAIMS[0]]["source_refs"][0]["document_version_id"] = RUN
    elif change == "safe_harbor_claim":
        revisions[CLAIMS[1]]["safe_harbor"]["claim_id"] = CLAIMS[0]
    elif change == "untagged_model":
        revisions[CLAIMS[2]]["model_sha256"] = MODEL
    elif change == "decided_no_source":
        revisions[CLAIMS[0]]["source_refs"] = []
    elif change == "revision":
        revisions[CLAIMS[0]]["decision_revision"] = 9
    elif change == "rule_pack":
        revisions[CLAIMS[0]]["rule_pack_sha256"] = "b" * 64
    elif change == "decided_without_grade":
        revisions[CLAIMS[0]]["evidence_grade"] = None
    else:
        revisions.pop(CLAIMS[1])

    with pytest.raises(ValueError):
        build_report_model(snapshot, revisions)


def test_report_projection_is_deterministic_and_does_not_mutate_snapshot():
    snapshot, revisions = manifest(), decisions()
    before = deepcopy((snapshot, revisions))

    assert build_report_model(snapshot, revisions) == build_report_model(snapshot, revisions)
    assert (snapshot, revisions) == before


def test_wholly_unavailable_unfinished_claim_remains_explicit_not_run():
    snapshot, revisions = manifest(), decisions()
    revisions[CLAIMS[2]] = None

    claim = build_report_model(snapshot, revisions)["claims"][2]

    assert claim["decision_status"] == claim["source_status"] == "not_run"
    assert claim["source_refs"] == []
    assert claim["model_sha256"] is claim["prompt_sha256"] is None
    assert claim["replicate_hashes"] == []


def test_tagged_unfinished_claim_cannot_drop_its_provenance_record():
    snapshot, revisions = manifest(), decisions()
    snapshot["claim_revision_refs"][2]["tag_revision"] = 1
    revisions[CLAIMS[2]] = None

    with pytest.raises(ValueError):
        build_report_model(snapshot, revisions)


def test_pre_parse_partial_report_keeps_null_manifest_without_inventing_identity():
    snapshot = manifest()
    snapshot["parse_manifest_id"] = None
    snapshot["claim_revision_refs"] = []
    snapshot["coverage"] |= {
        "pages_processed": 0,
        "pages_unreadable": 0,
        "pages_unprocessed": 3,
        "chunks_discovered": 0,
        "chunks_processed": 0,
        "claims_discovered": 0,
        "claims_decided": 0,
        "claims_needs_review": 0,
    }
    snapshot["unverified_basis"] = 0

    model = build_report_model(snapshot, {})

    assert model["parse_manifest_id"] is None
    assert model["partial"] is True
    assert model["claims"] == []


def test_actual_rule_decision_and_safe_harbor_record_flow_into_report():
    from dataclasses import asdict

    from proofops.domain.rules.engine import evaluate
    from proofops.domain.rules.safe_harbor import record_safe_harbor

    from tests.acceptance.test_rules import inputs, pack

    tags, context = inputs()
    rulepack = pack()
    decision = evaluate(tags, context, rulepack)
    source = next(fact.evidence_refs[0] for fact in tags.facts if fact.evidence_refs)
    safe_harbor = record_safe_harbor(tags, context, rulepack)
    pinned = {
        "tenant_id": tags.tenant_id,
        "run_id": RUN,
        "document_version_id": tags.document_version_id,
        "parse_manifest_id": source.parse_manifest_id,
        "source_sha256": source.raw_text_sha256,
        "mutation_epoch": 1,
        "claim_revision_refs": [
            {
                "claim_id": tags.claim_id,
                "tag_revision": tags.tag_revision,
                "decision_revision": decision.decision_revision,
            }
        ],
        "rule_pack_hashes": [rulepack.sha256],
        "coverage": {
            "pages_total": 1,
            "pages_processed": 1,
            "pages_unreadable": 0,
            "pages_unprocessed": 0,
            "chunks_discovered": 1,
            "chunks_processed": 1,
            "claims_discovered": 1,
            "claims_decided": 1,
            "claims_needs_review": 0,
            "full_scope": True,
            "complete": True,
        },
        "unverified_basis": len(decision.basis_refs),
        "generated_at": "2026-09-09T00:00:00Z",
        "execution_profile": "local-synthetic-only",
    }
    revision = asdict(decision) | {
        "tenant_id": tags.tenant_id,
        "document_version_id": tags.document_version_id,
        "parse_manifest_id": source.parse_manifest_id,
        "source_sha256": source.raw_text_sha256,
        "claim_id": tags.claim_id,
        "model_sha256": tags.model_sha256,
        "prompt_sha256": tags.prompt_sha256,
        "replicate_hashes": list(tags.replicate_hashes),
        "source_refs": [asdict(source)],
        "assurance": {
            "status": "covered",
            "level": "limited",
            "provider": "Synthetic provider",
            "statement_id": source.source_id,
            "metric_match": "yes",
            "period_match": "yes",
            "boundary_match": "yes",
            "evidence_refs": [asdict(source)],
        },
        "safe_harbor": safe_harbor.to_api_dict(),
    }

    model = build_report_model(pinned, {tags.claim_id: revision})

    assert model["claims"][0]["evidence_grade"] == decision.evidence_grade == "E3"
    assert model["claims"][0]["safe_harbor"] == safe_harbor.to_api_dict()
    assert model["claims"][0]["source_refs"][0]["quote"] == source.quote


def test_stdlib_renderers_escape_html_and_guard_csv_formulas():
    revisions = decisions()
    revisions[CLAIMS[0]]["source_refs"] = [source_ref(quote='=HYPERLINK("bad")<script>')]
    model = build_report_model(manifest(), revisions)

    assert json.loads(render_report(model, "json")) == model
    html = render_report(model, "html").decode()
    assert "<script>" not in html and "&lt;script&gt;" in html
    for text in (
        "E1",
        "INCOMPLETE",
        "auto_confirmed",
        "tag revision 1",
        "decision revision 1",
        "미완료 2건",
        "미확인 조항 2건",
        "120.0, 340.0, 480.0, 372.0",
        "감사 세부정보",
        "model_sha256",
    ):
        assert text in html
    rows = list(reader(StringIO(render_report(model, "csv").decode())))
    quote = rows[1][rows[0].index("source_quotes")]
    assert quote.startswith("'") and not quote.startswith("=")
    assert json.loads(rows[1][rows[0].index("source_refs")]) == model["claims"][0]["source_refs"]
    # review_action is an additive column; blocked claim carries a follow-up, and the
    # decided claim's action (missing G3/G4 already in suggestion) surfaces its gap check.
    assert "review_action" in rows[0]
    blocked_action = rows[2][rows[0].index("review_action")]
    assert json.loads(blocked_action) == model["claims"][1]["review_action"]
    action_html = render_report(model, "html").decode()
    assert "다음 검토 작업:" in action_html and "미해결 요소의 원문 근거 귀속을 확인" in action_html
    with pytest.raises(ValueError):
        render_report(model, "pdf")


def test_unrun_html_reports_unevaluated_lists_and_keeps_raw_data_in_audit_details():
    model = build_report_model(manifest(), decisions())
    html = render_report(model, "html").decode()
    section = next(
        part for part in html.split("<section>") if f"<p>주장 ID: {CLAIMS[2]}</p>" in part
    )
    summary, audit = section.split("<details>", 1)
    assert "결손: 미평가 · 미해결: 미평가 · gaps: 미평가" in summary
    assert "<pre>" not in summary
    assert all(key in audit for key in ("basis_refs", "assurance", "safe_harbor"))
    assert json.loads(render_report(model, "json")) == model


def test_report_preview_renders_partial_and_unverified_states(tmp_path):
    root = Path(__file__).resolve().parents[2]
    esbuild = next((root / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    entry = tmp_path / "report-render.tsx"
    entry.write_text(
        """
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { ReportPreview } from COMPONENT;
import assert from "node:assert/strict";
const report=REPORT;
report.claims[0].claim_quote="HTML 근거와 구분되는 검토 주장";
report.claims[0].classification_review={origin:"ai_delegated_classification",track:"management",revision:1};
const html=renderToStaticMarkup(React.createElement(ReportPreview,{report}));
for (const text of ["검토용 부분 리포트","미완료 2건","판독 불가 1쪽","미처리 1쪽",
"p.34","G3, G4","조항 미확인","보증 범위 밖","세이프하버 미실행",MODEL,PROMPT,
"규칙 공백으로 미판정", "다음 검토 작업", "기준 조항의 대응",
"HTML 근거와 구분되는 검토 주장", "이전 스냅샷에 주장 문장이 저장되지 않았습니다",
"AI 위임 분류(사람 검토 아님)"] )
  assert.ok(html.includes(text), text);
console.log("ReportPreview audit-state checks passed");
""".replace("REACT", json.dumps(str(root / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(root / "apps/web/node_modules/react-dom/server.node.js")))
        .replace(
            "COMPONENT", json.dumps(str(root / "apps/web/src/features/reports/ReportPreview.tsx"))
        )
        .replace("REPORT", json.dumps(build_report_model(manifest(), decisions())))
        .replace("MODEL", json.dumps(MODEL))
        .replace("PROMPT", json.dumps(PROMPT))
    )
    bundle = tmp_path / "report-render.cjs"
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


def test_report_preserves_claim_quote_separately_from_evidence_and_escapes_it():
    records = decisions()
    quote = "=검토 주장 <script>alert(1)</script>"
    for record in records.values():
        record["claim_quote"] = quote
    model = build_report_model(manifest(), records)
    assert all(claim["claim_quote"] == quote for claim in model["claims"])
    assert model["claims"][0]["source_refs"][0]["quote"] != quote
    html = render_report(model, "html").decode()
    assert "검토 대상 주장" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html
    rows = list(reader(StringIO(render_report(model, "csv").decode())))
    assert rows[1][rows[0].index("claim_quote")] == "'" + quote
    old = build_report_model(manifest(), decisions())
    assert all(claim["claim_quote"] is None for claim in old["claims"])
    assert "이전 스냅샷에 주장 문장이 저장되지 않았습니다" in render_report(old, "html").decode()
    records[CLAIMS[0]]["claim_quote"] = {"untrusted": "value"}
    with pytest.raises(ValueError, match="claim_quote"):
        build_report_model(manifest(), records)


def test_classification_review_provenance_rejects_unknown_origin():
    records = decisions()
    records[CLAIMS[0]]["classification_review"] = dict(
        classification_id=CLAIMS[0],
        record_sha256=HASH,
        revision=1,
        origin="approved_by_expert",
        track="goal",
    )
    with pytest.raises(ValueError, match="classification review provenance"):
        build_report_model(manifest(), records)


def _tag_element(element_id, state, value, quote):
    ref = source_ref(quote=quote)
    return {
        "element_id": element_id,
        "state": state,
        "evidence_refs": [ref] if state == "present" else [],
        "normalized_value": value,
        "credited_from": None,
        "reason_code": None,
    }


def test_tag_elements_round_trip_values_states_and_exact_quotes():
    records = decisions()
    records[CLAIMS[0]]["tag_elements"] = [
        _tag_element("G1", "present", "2040년", "2040년까지 탄소중립을 달성하겠습니다."),
        _tag_element("G2", "present", "RE100 및 탄소중립", "RE100 및 탄소중립을 추진합니다."),
        _tag_element("G6", "present", None, "재생에너지 조달을 확대하고 있습니다."),
    ]
    records[CLAIMS[1]]["tag_elements"] = []
    model = build_report_model(manifest(), records)
    decided, blocked, not_run = model["claims"]
    assert [e["element_id"] for e in decided["tag_elements"]] == ["G1", "G2", "G6"]
    assert decided["tag_elements"][0]["normalized_value"] == "2040년"
    assert decided["tag_elements"][2]["state"] == "present"
    assert decided["tag_elements"][2]["normalized_value"] is None
    quote = decided["tag_elements"][1]["evidence_refs"][0]["quote"]
    assert quote == "RE100 및 탄소중립을 추진합니다."
    # No grade/label inference from tag values.
    assert decided["evidence_grade"] == "E1" and decided["label"] == "INCOMPLETE"
    assert blocked["tag_elements"] == []
    assert not_run["tag_elements"] is None
    assert (
        json.loads(render_report(model, "json"))["claims"][0]["tag_elements"]
        == decided["tag_elements"]
    )
    rows = list(reader(StringIO(render_report(model, "csv").decode())))
    assert rows[0][-2:] == ["tag_elements", "grade_range"]
    assert json.loads(rows[1][rows[0].index("tag_elements")]) == decided["tag_elements"]
    assert rows[3][rows[0].index("tag_elements")] == "null"
    html = render_report(model, "html").decode()
    assert "태그 요소" in html and "2040년" in html and "RE100 및 탄소중립을 추진합니다." in html
    assert "태그 요소 미포함(이전 스냅샷)" in html
    assert "태그된 요소 없음(미태깅)" in html


def test_tag_elements_old_snapshot_null_and_untagged_empty_are_distinct():
    old = build_report_model(manifest(), decisions())
    assert all(claim["tag_elements"] is None for claim in old["claims"])
    records = decisions()
    records[CLAIMS[2]]["tag_elements"] = []
    model = build_report_model(manifest(), records)
    assert model["claims"][2]["tag_elements"] == []
    assert model["claims"][0]["tag_elements"] is None
    html = render_report(model, "html").decode()
    assert "태그 요소 미포함(이전 스냅샷)" in html
    assert "태그된 요소 없음(미태깅)" in html


@pytest.mark.parametrize(
    "change",
    [
        "duplicate",
        "foreign",
        "sourceless_present",
        "unverified_present",
        "malformed",
        "untagged_with_elements",
    ],
)
def test_tag_elements_fail_closed(change):
    records = decisions()
    good = [
        _tag_element("G1", "present", "2040년", "2040년까지 탄소중립을 달성하겠습니다."),
        _tag_element("G2", "unknown", None, "x"),
    ]
    good[1]["evidence_refs"] = []
    if change == "duplicate":
        records[CLAIMS[0]]["tag_elements"] = [good[0], dict(good[0])]
    elif change == "foreign":
        bad = _tag_element("G1", "present", "2040년", "2040년 목표")
        bad["evidence_refs"][0]["document_version_id"] = RUN
        records[CLAIMS[0]]["tag_elements"] = [bad]
    elif change == "sourceless_present":
        bad = _tag_element("G1", "present", "2040년", "2040년 목표")
        bad["evidence_refs"] = []
        records[CLAIMS[0]]["tag_elements"] = [bad]
    elif change == "unverified_present":
        bad = _tag_element("G1", "present", "2040년", "2040년 목표")
        bad["evidence_refs"][0]["verification_state"] = "candidate"
        records[CLAIMS[0]]["tag_elements"] = [bad]
    elif change == "malformed":
        records[CLAIMS[0]]["tag_elements"] = [{"element_id": "G1"}]
    else:
        records[CLAIMS[2]]["tag_elements"] = [good[0]]
    with pytest.raises(ValueError):
        build_report_model(manifest(), records)


def test_tag_elements_escape_html_and_guard_csv_formulas():
    records = decisions()
    records[CLAIMS[0]]["tag_elements"] = [
        _tag_element("G1", "present", '=HYPERLINK("bad")', "<script>alert(1)</script>")
    ]
    model = build_report_model(manifest(), records)
    html = render_report(model, "html").decode()
    assert "<script>" not in html and "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    rows = list(reader(StringIO(render_report(model, "csv").decode())))
    cell = rows[1][rows[0].index("tag_elements")]
    assert "<script>" in cell
    assert json.loads(cell[1:] if cell.startswith("'") else cell)[0]["normalized_value"].startswith(
        "="
    )


def test_tag_elements_separates_present_and_review_candidates():
    records = decisions()
    records[CLAIMS[0]]["tag_elements"] = [
        _tag_element("G1", "present", "2040년", "2040년 목표"),
        _tag_element("G2", "unknown", None, "알 수 없는 목표"),
        _tag_element("G6", "unknown", None, "x"),  # Without quote (evidence_refs = [])
        _tag_element("G4", "conflict", "범위 A", "범위 B"),
        _tag_element("G5", "absent", None, "부재 확인 범위"),
    ]
    # Manually add evidence to G2 and G4 so they become candidates
    records[CLAIMS[0]]["tag_elements"][1]["evidence_refs"] = [source_ref(quote="알 수 없는 목표")]
    records[CLAIMS[0]]["tag_elements"][3]["evidence_refs"] = [source_ref(quote="범위 B")]
    records[CLAIMS[0]]["tag_elements"][4]["evidence_refs"] = [source_ref(quote="부재 확인 범위")]
    # Remove evidence from G6
    records[CLAIMS[0]]["tag_elements"][2]["evidence_refs"] = []

    model = build_report_model(manifest(), records)
    html = render_report(model, "html").decode()

    assert "<h3>추출된 항목 (present)</h3>" in html
    assert "<h3>미해결 태그 요소 검토 후보</h3>" in html
    assert "<h3>기타 태그 요소</h3>" in html

    # Verify separation
    present_idx = html.find("<h3>추출된 항목 (present)</h3>")
    candidate_idx = html.find("<h3>미해결 태그 요소 검토 후보</h3>")
    other_idx = html.find("<h3>기타 태그 요소</h3>")

    g1_idx = html.find("<li>G1 · ")
    g2_idx = html.find("<li>G2 · ")
    g4_idx = html.find("<li>G4 · ")
    g6_idx = html.find("<li>G6 · ")

    assert present_idx < g1_idx < candidate_idx
    assert candidate_idx < g2_idx < g4_idx < other_idx
    assert other_idx < g6_idx
    assert other_idx < html.find("<li>G5 · ")


def _range_snapshot(**range_fields):
    data = decisions()
    claim = data[CLAIMS[1]]
    claim.update(decision_status="blocked_evidence", gap_ids=[], unresolved_elements=["M2", "M3"])
    claim.update(range_fields)
    return data


def test_grade_range_is_reported_separately_from_grade_in_all_formats():
    model = build_report_model(
        manifest(),
        _range_snapshot(grade_floor="E1", grade_ceiling="E3", grade_open_elements=["M2", "M3"]),
    )
    blocked = model["claims"][1]
    assert blocked["evidence_grade"] is None and blocked["label"] is None
    assert blocked["grade_range"] == dict(floor="E1", ceiling="E3", open_elements=["M2", "M3"])
    rows = list(reader(StringIO(render_report(model, "csv").decode())))
    assert rows[0][-1] == "grade_range"
    assert json.loads(rows[2][-1])["floor"] == "E1"
    html = render_report(model, "html").decode()
    assert "가능 등급 범위: E1 ~ E3 (확정 등급 아님)" in html
    assert "M2 · 적용범위 (조직경계·사업장)" in html


def test_old_snapshot_and_decided_claims_have_null_grade_range():
    model = build_report_model(manifest(), decisions())
    assert [claim["grade_range"] for claim in model["claims"]] == [None, None, None]
    assert "가능 등급 범위" not in render_report(model, "html").decode()


@pytest.mark.parametrize(
    "fields",
    [
        dict(grade_floor="E3", grade_ceiling="E1", grade_open_elements=["M2"]),
        dict(grade_floor="E1", grade_ceiling="E9", grade_open_elements=["M2"]),
        dict(grade_floor="E1", grade_ceiling="E3", grade_open_elements=[]),
        dict(grade_floor="E1", grade_ceiling=None, grade_open_elements=["M2"]),
    ],
)
def test_grade_range_fails_closed(fields):
    with pytest.raises(ValueError):
        build_report_model(manifest(), _range_snapshot(**fields))


def test_decided_claim_cannot_carry_grade_range():
    data = decisions()
    data[CLAIMS[0]].update(grade_floor="E1", grade_ceiling="E3", grade_open_elements=["G3"])
    with pytest.raises(ValueError, match="grade range"):
        build_report_model(manifest(), data)
