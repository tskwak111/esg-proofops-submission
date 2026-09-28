"""Build the public NAVER demo from the immutable reviewed export."""

import argparse
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "apps/web/public/demo/naver-2025.json"
AUDIT_UNCERTAIN = {"f6bfe84f", "0598227c"}
NAVER_PDF_SHA256 = "75388f16c13739a671fd158c383502d81968fe587f2372ab0f9306f3d2c1a2c6"
LABELS = {"E0": "UNSUBSTANTIATED", "E1": "INCOMPLETE", "E2": "INCOMPLETE", "E3": "SUBSTANTIATED"}


def short(value, limit=200):
    return " ".join(str(value or "").split())[:limit]


def reference(ref):
    return {"page": ref.get("page_num"), "quote": short(ref.get("quote"))}


def build(export, demo_results=None):
    with zipfile.ZipFile(export) as archive:
        report = json.loads(archive.read("report.json"))
        manifest = json.loads(archive.read("manifest.json"))
    claims = []
    for item in report["claims"]:
        elements = item.get("tag_elements") or []
        first_id = elements[0]["element_id"][0] if elements else ""
        track = {"M": "management", "G": "goal", "P": "performance"}.get(first_id)
        refs = item.get("source_refs") or []
        claims.append(
            {
                "id": item["claim_id"],
                "page": refs[0]["page_num"] if refs else None,
                "track": track,
                "quote": short(item["claim_quote"]),
                "source_verified": any(ref.get("verification_state") == "verified" for ref in refs),
                "elements": [
                    {
                        "id": element["element_id"],
                        "state": element["state"],
                        "evidence": [reference(ref) for ref in element.get("evidence_refs", [])][
                            :6
                        ],
                    }
                    for element in elements
                ],
                "decision": {
                    "grade": item.get("evidence_grade"),
                    "label": item.get("label"),
                    "grade_range": item.get("grade_range"),
                    "status": item["decision_status"],
                    "missing": item.get("missing_elements") or [],
                    "unresolved": item.get("unresolved_elements") or [],
                },
                "review": {
                    "status": "user_confirmed"
                    if item["review_status"] == "ai_delegated_confirmed"
                    else item["review_status"],
                    "prior_status": item["review_status"],
                    "confirmed_by": "project owner"
                    if item["review_status"] == "ai_delegated_confirmed"
                    else None,
                    "confirmed_at": "2026-09-29"
                    if item["review_status"] == "ai_delegated_confirmed"
                    else None,
                    "tag_revision": item["tag_revision"],
                    "decision_revision": item["decision_revision"],
                    "audit": "uncertain" if item["claim_id"][:8] in AUDIT_UNCERTAIN else None,
                },
            }
        )
    result = {
        "title": "NAVER 2025 통합보고서 · 선택 페이지 검토",
        "generated_at": report["generated_at"],
        "partial": report["partial"],
        "review_confirmation": {
            "status": "user_confirmed",
            "confirmed_by": "project owner",
            "confirmed_at": "2026-09-29",
            "scope": "검토 기록 27건(독립 점검에서 추가 점검 대상으로 분류된 2건 포함)",
        },
        "processed_reports": [
            {
                "title": "NAVER 2025 통합보고서",
                "sha256": NAVER_PDF_SHA256,
                "result_path": "/demo",
                "run_label": "모델 실행 → 검토 결과",
                "scope": "선택 페이지 54/244쪽",
            }
        ],
        "coverage": {
            key: report["coverage"][key]
            for key in (
                "pages_processed",
                "pages_total",
                "pages_unprocessed",
                "pages_unreadable",
                "claims_discovered",
                "claims_decided",
                "claims_needs_review",
            )
        },
        "funnel": [
            {"label": "추출 주장", "count": 331},
            {"label": "원문 검증", "count": 272},
            {"label": "예비 분류 합의", "count": 60},
            {"label": "관계 시도", "count": 60},
            {"label": "요소 태그 발행", "count": 36},
            {"label": "검토 기록", "count": 27},
            {"label": "규칙 판정 기록", "count": 17},
        ],
        "funnel_source": (
            "모델 실행의 유료 호출 단계와 복사본 검토 결과; " "단계별 시점이 다릅니다."
        ),
        "run": {
            "model_ids": ["solar-pro4"],
            "model_note": "모델 실행 설정: solar-pro4 (추출·예비분류·관계·태깅).",
            "model_binding_hash": manifest.get("model_binding_hash"),
            "rule_pack_id": "e498dfd1-2b6c-4b30-b66d-ec014423ab9b",
            "rule_pack_name": "proofops-domain-v2.0-impl2",
            "rule_pack_hash": (report.get("rule_pack_hashes") or [None])[0],
            "model_cost_usd": 1.822535,
            "model_paid_calls": 2038,
            "model_elapsed_seconds": {
                "parse_extraction": 18556.5,
                "tagging": 4238.3,
                "local_postprocess": 264.3,
            },
            "review_seconds": 412.3,
            "review_model_calls": 0,
        },
        "audit": {"agreed": 15, "disagreed": 0, "uncertain": 2, "scope": "E3 17건 독립 점검"},
        "claims": claims,
    }
    if demo_results:
        result["demo_mode"] = True
        result["run"]["demo_mode"] = True
        result["generated_at"] = demo_results.get("finished_at", result["generated_at"])
        by_id = demo_results["claims"]
        for claim in claims:
            passed = by_id.get(claim["id"])
            claim["pipeline_stage"] = (
                passed.get("pipeline_stage", []) if passed else ["extracted", "source_checked"]
            )
            claim["mode"] = passed.get("mode") if passed else None
            claim["classification_mode"] = passed.get("classification_mode") if passed else None
            claim["pipeline_status"] = passed.get("stage", "not_run") if passed else "not_run"
            claim["mode_flags"] = {
                "relaxed_rules": bool(passed and passed.get("mode")),
                "single_replica": bool(passed and passed.get("mode")),
                "source_unverified": not claim["source_verified"],
                "classification_fallback": bool(passed and passed.get("classification_fallback")),
            }
            if (
                not passed
                or claim["review"]["status"] == "user_confirmed"
                or passed.get("stage") == "existing_tagged"
            ):
                continue
            if passed.get("track") in ("goal", "performance", "management"):
                claim["track"] = passed["track"]
            claim["source_verified"] = passed.get("source_verified", claim["source_verified"])
            claim["elements"] = [
                {
                    "id": element["element_id"],
                    "state": element.get("state", "unknown"),
                    "evidence": [reference(ref) for ref in element.get("evidence_refs", [])][:6],
                }
                for element in passed.get("elements", [])
            ]
            decision = passed.get("decision")
            if decision:
                grade_range = decision.get("grade_range")
                claim["decision"] = {
                    "grade": decision.get("evidence_grade"),
                    "label": decision.get("label"),
                    "grade_range": grade_range,
                    "status": decision.get("decision_status"),
                    "missing": decision.get("missing_elements") or [],
                    "unresolved": decision.get("unresolved_elements") or [],
                }
            else:
                claim["decision"]["status"] = claim["pipeline_status"]
                if not claim["source_verified"]:
                    claim["decision"]["display_note"] = "원문 미검증"
        for claim in claims:
            decision = claim["decision"]
            grade_range = decision.get("grade_range")
            engine_grade = decision.get("grade")
            display_grade = (
                engine_grade
                or (grade_range.get("floor") if isinstance(grade_range, dict) else None)
                or "E0"
            )
            claim["display_grade"] = display_grade
            claim["display_label"] = decision.get("label") or LABELS[display_grade]
            claim["display_note"] = (
                "원문 미검증"
                if not claim["source_verified"]
                else decision.get("display_note")
                or ("추정" if claim["mode_flags"]["relaxed_rules"] or not engine_grade else None)
            )
            decision["display_grade"] = display_grade
            decision["display_label"] = claim["display_label"]
            decision["display_note"] = claim["display_note"]
            decision["estimated"] = bool(claim["display_note"])
            if claim["display_note"]:
                claim["mode_flags"]["relaxed_rules"] = True
                claim["mode"] = "시연 모드"
        stages = [c["pipeline_status"] for c in claims]
        result["demo_coverage"] = {
            "claims_total": len(claims),
            "claims_with_display_grade": sum(c["display_grade"] is not None for c in claims),
            "claims_with_final_status": sum(s != "not_run" for s in stages),
            "engine_evaluated": sum("rule_evaluated" in c["pipeline_stage"] for c in claims),
            "source_unverified": sum(not c["source_verified"] for c in claims),
            "unclassified": stages.count("unclassified"),
        }
        result["funnel"] = [
            {"label": "추출 주장", "count": len(claims)},
            {"label": "원문 검증", "count": sum(c["source_verified"] for c in claims)},
            {"label": "예비 분류", "count": sum(c["track"] is not None for c in claims)},
            {
                "label": "관계 태깅 시도",
                "count": sum(
                    any(s.startswith("relation_") for s in c["pipeline_stage"]) for c in claims
                ),
            },
            {
                "label": "요소 태깅 시도",
                "count": sum(
                    any(s.startswith("elements_") for s in c["pipeline_stage"])
                    or bool(c["elements"])
                    for c in claims
                ),
            },
            {"label": "표시 등급", "count": sum(c["display_grade"] is not None for c in claims)},
        ]
        result["funnel_source"] = "선택 페이지 기존 검토와 시연 모드 단일 분류·관계·요소 호출 결과"
        result["run"]["demo_pass"] = {
            "calls": demo_results["paid_calls"],
            "spend_before_usd": demo_results["spend_before_usd"],
            "spend_after_usd": demo_results.get("spend_after_usd"),
            "cost_usd": demo_results.get("cost_usd"),
            "elapsed_seconds": demo_results.get("elapsed_seconds"),
        }
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--export", type=Path, required=True, help="Reviewed export ZIP")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--demo-results", type=Path, help="Optional local demo pass JSON")
    args = parser.parse_args()
    data = build(
        args.export,
        json.loads(args.demo_results.read_text()) if args.demo_results else None,
    )
    assert len(data["claims"]) == 331
    assert (
        sum(
            c["decision"]["grade"] == "E3"
            for c in data["claims"]
            if c["review"]["status"] == "user_confirmed"
        )
        == 17
    )
    assert (
        sum(
            c["decision"]["grade_range"] is not None
            for c in data["claims"]
            if c["review"]["status"] == "user_confirmed"
        )
        == 10
    )
    assert sum(c["review"]["status"] == "user_confirmed" for c in data["claims"]) == 27
    assert all(
        len(c["quote"]) <= 200
        and all(len(e["quote"]) <= 200 for t in c["elements"] for e in t["evidence"])
        for c in data["claims"]
    )
    assert all("tenant" not in json.dumps(c).lower() for c in data["claims"])
    payload = (json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    assert len(payload) < 3_000_000
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload)
    print(f'{args.output}: {len(payload)} bytes, {len(data["claims"])} claims')


if __name__ == "__main__":
    main()
