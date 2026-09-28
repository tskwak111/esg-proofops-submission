"""Build the reviewed Kia case snapshot with the pinned Python rules engine."""

import argparse
import csv
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from proofops.domain.rulepacks import RulePackSnapshot
from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags, RuleContext, evaluate
from proofops.domain.values import GRADE_LABEL_MAP, SourceRef

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "apps/web/public/demo/kia-2025.json"
TENANT = "00000000-0000-0000-0000-000000000000"
REPORT_URL = "https://worldwide.kia.com/ko/company/sustainability/esg/sustainability-report"
MANAGEMENT_FACTS = {
    "M1": "named_means_or_concrete_state",
    "M2": "org_boundary",
    "M3": "external_verification",
    "M4": "concrete_implementation_detail",
    "M5": "responsible_organization",
    "M6": "compensation_link",
}


def rows(source, name):
    with (source / name).open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def short(value):
    return " ".join(str(value or "").split())[:200]


def uid(value):
    return str(uuid5(NAMESPACE_URL, f"proofops-kia-2025:{value}"))


def source_ref(claim_id, name, page, quote):
    return SourceRef(
        source_id=uid(f"{claim_id}:{name}:{page}:{quote}"),
        document_version_id=uid("document-version"),
        parse_manifest_id=uid("source-locators"),
        page_num=int(page),
        printed_page_label=None,
        bbox=None,
        raw_text_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        quote=quote,
        char_start=0,
        char_end=len(quote),
        location_quality="located",
        verification_state="verified",
    )


def make_fact(claim_id, name, state, page="", quote="", scope="local_claim", coverage=""):
    evidence = bool(state == "present" and page and quote.strip())
    assert state != "present" or evidence, (claim_id, name)
    return ConfirmedFact(
        name=name,
        state=state,
        evidence_refs=(source_ref(claim_id, name, page, quote),) if evidence else (),
        source_tenant_id=TENANT if evidence else None,
        citation_verified=evidence,
        binding_accepted=evidence,
        search_coverage_verified=state == "absent" and coverage == "full_document",
        source_scope=scope,
        normalized_value="covered" if name == "assurance_covered" and evidence else None,
    )


def build(source: Path):
    pack = RulePackSnapshot(**json.loads((ROOT / "api/rulepack.json").read_text()))
    locators = json.loads((source / "source-locators.json").read_text())
    source_hash = next(iter(locators.values()))["document_sha256"]
    claim_rows = rows(source, "claims.csv")
    management_rows = rows(source, "management-candidates.csv")
    element_rows = rows(source, "elements.csv")
    claims = []
    for row in claim_rows + management_rows:
        claim_id = row["claim_id"]
        is_management = claim_id.startswith("MGT-")
        track = row["track"]
        page = int(row["physical_page"])
        quote = short(row["quote"])
        facts = []
        elements = []
        if is_management:
            for element_id, name in MANAGEMENT_FACTS.items():
                state = row[element_id]
                evidence = [{"page": page, "quote": quote}] if state == "present" else []
                facts.append(make_fact(claim_id, name, state, page, quote))
                elements.append({"id": element_id, "state": state, "evidence": evidence})
        else:
            for item in (e for e in element_rows if e["claim_id"] == claim_id):
                name, state = item["element_id"], item["state"]
                evidence_quote = short(item["quote"])
                evidence_page = item["physical_page"].split(";")[0]
                scope = "global_bound" if name == "assurance_covered" else "local_claim"
                facts.append(
                    make_fact(
                        claim_id,
                        name,
                        state,
                        evidence_page,
                        evidence_quote,
                        scope,
                        item["search_coverage"],
                    )
                )
                elements.append(
                    {
                        "id": name,
                        "state": state,
                        "evidence": [{"page": int(evidence_page), "quote": evidence_quote}]
                        if state == "present"
                        else [],
                    }
                )
        packet_hash = hashlib.sha256(f"{source_hash}:{claim_id}".encode()).hexdigest()
        tag = ConfirmedTags(
            tenant_id=TENANT,
            document_version_id=uid("document-version"),
            claim_id=uid(claim_id),
            track=track,
            facts=tuple(facts),
            tag_revision=1,
            packet_sha256=packet_hash,
            model_sha256=hashlib.sha256(b"reviewed-case").hexdigest(),
            prompt_sha256=hashlib.sha256(b"not-applicable").hexdigest(),
            replicate_hashes=tuple(
                hashlib.sha256(f"{claim_id}:{i}".encode()).hexdigest() for i in range(3)
            ),
            ontology_version=pack.ontology_version,
        )
        context = RuleContext(
            TENANT, tag.document_version_id, tag.claim_id, packet_hash, local_synthetic=True
        )
        decision = evaluate(tag, context, pack)
        result = decision.to_api_dict()
        grade_range = result["grade_range"]
        claims.append(
            {
                "id": claim_id,
                "page": page,
                "track": track,
                "quote": quote,
                "statement": row.get("statement") or quote,
                "source_verified": True,
                "elements": elements,
                "decision": {
                    "grade": result["evidence_grade"],
                    "label": result["label"],
                    "grade_range": grade_range,
                    "status": result["decision_status"],
                    "missing": result["missing_elements"],
                    "unresolved": decision.unresolved_elements,
                    "display_grade": result["evidence_grade"] or (grade_range or {}).get("floor"),
                    "display_label": result["label"]
                    or GRADE_LABEL_MAP.get((grade_range or {}).get("floor")),
                    "estimated": result["evidence_grade"] is None and grade_range is not None,
                },
                "review": {
                    "status": "user_confirmed",
                    "confirmed_by": "project owner",
                    "confirmed_at": "2026-09-29",
                    "tag_revision": 1,
                    "decision_revision": 1,
                    "audit": None,
                },
            }
        )

    calculations = {
        r["case_id"]: r for r in json.loads((source / "numeric-calculations.json").read_text())
    }
    numeric_rows = rows(source, "numeric.csv")
    numeric_checks = []
    for case_id in ("DOC034-N01", "DOC034-N02", "DOC034-N03"):
        observed = [r for r in numeric_rows if r["case_id"] in (case_id, f"{case_id}-BASE")]
        calc = calculations[case_id]
        if case_id == "DOC034-N01":
            terms, total = calc["reason"].split(":", 1)[1].split("=")
            assert (
                sum(map(Decimal, terms.split("+")))
                == Decimal(total)
                == Decimal(observed[-1]["value_decimal"])
            )
        else:
            baseline, current = (Decimal(r["value_decimal"]) for r in observed)
            computed = (
                (1 - current / baseline) * 100
                if calc["operator"] == "product_reduction"
                else (current / baseline - 1) * 100
            )
            assert abs(computed - Decimal(calc["computed_percent"])) < Decimal("0.000001")
            assert abs(computed - Decimal(calc["reported_percent"])) < 1
        numeric_checks.append(
            {
                "id": case_id,
                "claim_id": observed[-1]["claim_id"],
                "status": calc.get("status", "consistent"),
                "reported": observed[-1]["value_raw"],
                "computed_percent": calc.get("computed_percent"),
                "reported_percent": calc.get("reported_percent"),
                "observations": [
                    {
                        "page": int(r["physical_page"]),
                        "quote": short(r["quote"]),
                        "value": r["value_raw"],
                        "unit": r["unit"],
                    }
                    for r in observed
                ],
                "note": short(observed[-1]["reason"]),
            }
        )
    assurance = [
        {
            "id": r["case_id"],
            "claim_id": r["claim_id"],
            "status": r["expected_status"],
            "provider": r["provider"],
            "level": r["level"],
            "period": f"{r['period_start']} ~ {r['period_end']}",
            "pages": [int(x) for x in r["physical_page"].split(";") if x],
            "quote": short(r["quote"]),
            "metrics": short(r["metrics"]),
            "note": short(r["reason"]),
        }
        for r in rows(source, "assurance.csv")
    ]
    assert len(claims) == 10 and len(numeric_checks) == 3 and len(assurance) == 4
    assert sum(c["decision"]["status"] == "decided" for c in claims) == 2
    assert sum(c["decision"]["estimated"] for c in claims) == 8
    assert all(
        len(c["quote"]) <= 200
        and all(len(e["quote"]) <= 200 for t in c["elements"] for e in t["evidence"])
        for c in claims
    )
    evidence_pages = {c["page"] for c in claims}
    evidence_pages.update(
        ref["page"] for c in claims for element in c["elements"] for ref in element["evidence"]
    )
    evidence_pages.update(ref["page"] for item in numeric_checks for ref in item["observations"])
    evidence_pages.update(page for item in assurance for page in item["pages"])
    return {
        "title": "기아 2025 지속가능경영보고서 · 검토 사례",
        "generated_at": datetime.now(UTC).isoformat(),
        "partial": True,
        "review_confirmation": {
            "status": "user_confirmed",
            "confirmed_by": "project owner",
            "confirmed_at": "2026-09-29",
            "scope": "선택 주장 5건과 관리형 사례 5건",
        },
        "processed_reports": [
            {
                "title": "기아 2025 지속가능경영보고서",
                "sha256": source_hash,
                "result_path": "/demo/kia",
                "run_label": "사용자 확인 태그 → 규칙엔진 재계산",
                "scope": "선택 주장 10건 · 인용 페이지",
            }
        ],
        "coverage": {
            "pages_processed": len(evidence_pages),
            "pages_total": 134,
            "pages_unprocessed": 134 - len(evidence_pages),
            "pages_unreadable": 0,
            "claims_discovered": len(claims),
            "claims_decided": sum(c["decision"]["status"] == "decided" for c in claims),
            "claims_needs_review": sum(c["decision"]["status"] != "decided" for c in claims),
        },
        "funnel": [
            {"label": stage, "count": len(claims)}
            for stage in ("주장 선정", "원문 확인", "요소 태깅", "사용자 확인", "규칙 계산")
        ],
        "funnel_source": "선택 사례 10건의 사용자 확인 태그와 로컬 규칙엔진 계산",
        "run": {
            "model_ids": [],
            "model_note": "검토 자료의 사용자 확인 태그를 사용",
            "model_binding_hash": None,
            "rule_pack_id": pack.rule_pack_id,
            "rule_pack_name": pack.version,
            "rule_pack_hash": pack.sha256,
            "demo_mode": True,
        },
        "audit": {"agreed": 10, "disagreed": 0, "uncertain": 0, "scope": "사용자 확인 태그"},
        "source_url": REPORT_URL,
        "claims": claims,
        "numeric_checks": numeric_checks,
        "assurance": assurance,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True, help="Reviewed source directory")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    data = build(args.source)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(
        f"{args.output}: {len(data['claims'])} claims, {data['coverage']['claims_decided']} decided"
    )


if __name__ == "__main__":
    main()
