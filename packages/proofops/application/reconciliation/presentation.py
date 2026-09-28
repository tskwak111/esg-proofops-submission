"""Versioned presentation of verified reconciliation results; no verdict changes."""

from __future__ import annotations

from typing import Any


def project_result(
    result: dict[str, Any],
    packet: dict[str, Any],
    documents: dict[str, Any],
    candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Versioned presentation companion; strict result1.1 stays untouched."""
    completed = result.get("execution_state") == "completed"
    sources = {s["source_id"]: s for s in packet.get("sources", [])}
    for candidate in candidates or []:
        # Only source IDs actually returned by the verified service can be displayed.
        if candidate.get("source_id") in result.get("source_ids", []):
            sources.setdefault(candidate["source_id"], candidate)
    explanation = sources.get(result.get("explanation_source_id")) if completed else None
    refs = []
    for source_id in result.get("source_ids", []):
        source = sources.get(source_id)
        if source is None:
            continue
        document = documents.get(source["document_id"], {})
        refs.append(
            {
                **source,
                "system": document.get("source_system"),
                "corp_code": document.get("corp_code"),
                "fy": document.get("fiscal_year"),
                "fetched_at": document.get("fetched_at"),
                "verification_state": "verified" if completed else "not_verified",
            }
        )
    return {
        "projection_schema_version": "reconciliation-presentation-1",
        "claim_id": result.get("claim_id"),
        "item": result.get("item"),
        "execution_state": result["execution_state"],
        "status": result.get("status"),
        "synthetic": result.get("synthetic", False),
        "sustainability_value": result.get("sustainability_value"),
        "financial_value": result.get("financial_value"),
        "explanation_present": explanation is not None,
        "explanation_location": explanation,
        "difference_note": {
            "matched": "공시 간 비교 또는 차이 설명 확인",
            "needs_explanation": "설명 보완 권장",
            "not_applicable": "적용 대상 아님",
        }.get(str(result.get("status")), "검토 대기"),
        "allowed_difference_type": None,
        "source_refs": refs,
        "basis": [],
        "confidence": None,
        "notice": "본 기능은 회계 처리의 적정성을 판단하지 않으며, "
        "공시 간 차이에 대한 설명의 존재 여부만 점검합니다.",
    }
