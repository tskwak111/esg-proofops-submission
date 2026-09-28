"""Pure audit-report projection over one immutable export snapshot."""

from __future__ import annotations

import json
from collections.abc import Mapping
from csv import writer
from dataclasses import asdict
from html import escape
from io import StringIO
from typing import Any

from proofops.application.summaries import _validated_coverage
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import (
    _element_from_dict,
    _require_sha256,
    _require_strict_int,
    _require_uuid,
    _source_ref_from_dict,
)

_STATUSES = frozenset(
    {"decided", "blocked_evidence", "blocked_rule_gap", "not_applicable", "not_run"}
)
_REVIEWS = frozenset(
    {"auto_confirmed", "needs_review", "human_confirmed", "ai_delegated_confirmed"}
)
_LABELS = {
    "E0": "UNSUBSTANTIATED",
    "E1": "INCOMPLETE",
    "E2": "INCOMPLETE",
    "E3": "SUBSTANTIATED",
}


_ELEMENT_LABELS = {
    "G1": "목표연도",
    "G2": "목표수치·지표",
    "G3": "기준연도·기준값",
    "G4": "적용범위 (Scope·조직경계)",
    "G5": "현재 이행률·진척",
    "G6": "전환계획·달성수단",
    "G7": "상쇄(탄소배출권) 사용 계획",
    "G8": "과학기반 목표 검증",
    "P1": "정량수치와 단위",
    "P2": "비교기준 (전년·기준연도)",
    "P3": "산정방법론과 경계",
    "P4": "보증 연결",
    "P5": "절대량/원단위 구분 명시",
    "P6": "본문 수치와 데이터 표의 일치",
    "M1": "이행방법·명명된 표준",
    "M2": "적용범위 (조직경계·사업장)",
    "M3": "외부검증",
    "M4": "이행 실적의 구체성",
    "M5": "담당 조직·거버넌스",
    "M6": "경영진 보상 연동",
}
_ELEMENT_STATES = {"present": "있음", "absent": "없음", "unknown": "미상", "conflict": "불일치"}


def _element_label(element_id: str) -> str:
    name = _ELEMENT_LABELS.get(element_id)
    return f"{element_id} · {name}" if name else element_id


def _copy(value: Any) -> Any:
    return json.loads(canonical_json(value))


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list | tuple) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError(f"{name} must contain non-empty strings")
    return list(value)


def _grade_range(record: Mapping[str, object], status: object) -> dict[str, Any] | None:
    """Reachable ladder grades from engine v3+; absent on older snapshots."""
    floor, ceiling = record.get("grade_floor"), record.get("grade_ceiling")
    open_elements = record.get("grade_open_elements") or []
    if floor is None and ceiling is None and not open_elements:
        return None
    if (
        status != "blocked_evidence"
        or floor not in _LABELS
        or ceiling not in _LABELS
        or str(floor) > str(ceiling)
        or not open_elements
    ):
        raise ValueError("grade range must be an ordered range on an evidence-blocked claim")
    return dict(
        floor=floor,
        ceiling=ceiling,
        open_elements=_strings(open_elements, "grade_open_elements"),
    )


def _basis_refs(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        raise ValueError("basis_refs must be an array")
    result = []
    for item in value:
        if isinstance(item, str):
            try:
                item = json.loads(item)
            except json.JSONDecodeError as exc:
                raise ValueError("basis_refs must contain valid JSON") from exc
        if not isinstance(item, Mapping):
            raise ValueError("basis_refs must contain objects")
        basis = item.get("basis", item)
        if not isinstance(basis, Mapping):
            raise ValueError("basis reference is malformed")
        clause = basis.get("clause")
        status = basis.get("verification_status")
        if clause is not None and not isinstance(clause, str):
            raise ValueError("basis clause must be text or null")
        if status not in ("verified", "unverified", "unlicensed"):
            raise ValueError("basis verification status is required")
        copied = _copy(item)
        if "basis" in copied:
            copied = {"element_id": copied.get("element_id"), **copied["basis"]}
        result.append(copied)
    return result


def _source_refs(
    value: object, document_version_id: str, parse_manifest_id: str | None
) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        raise ValueError("source_refs must be an array")
    result = []
    for ref in value:
        if not isinstance(ref, Mapping):
            raise ValueError("source_refs must contain objects")
        source = _source_ref_from_dict(_copy(ref))
        if (source.document_version_id, source.parse_manifest_id) != (
            document_version_id,
            parse_manifest_id,
        ):
            raise ValueError("source reference is outside the snapshot manifest")
        copied = asdict(source)
        copied["bbox"] = list(source.bbox) if source.bbox is not None else None
        result.append(copied)
    return result


def _tag_elements(
    value: object, document_version_id: str, parse_manifest_id: str | None
) -> list[dict[str, Any]] | None:
    """Project optional immutable tag elements without grading or inference.

    Missing key (old snapshots) yields None = unavailable, never an empty
    evaluated list. Empty list means untagged (no tagged elements). Each entry
    keeps element_id/state/normalized_value plus validated evidence_refs with
    exact quotes. State is preserved even when normalized_value is null.
    """
    if value is None:
        return None
    if not isinstance(value, list | tuple):
        raise ValueError("tag_elements must be an array or null")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("tag_elements must contain objects")
        try:
            element = _element_from_dict(_copy(item))
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("tag element is malformed") from exc
        if element.element_id in seen:
            raise ValueError("duplicate tag element_id")
        seen.add(element.element_id)
        refs = _source_refs(
            _copy(item.get("evidence_refs")), document_version_id, parse_manifest_id
        )
        if element.state == "present":
            if not refs or any(ref.get("verification_state") != "verified" for ref in refs):
                raise ValueError("source-less/unverified present is prohibited")
        result.append(
            {
                "element_id": element.element_id,
                "state": element.state,
                "normalized_value": element.normalized_value,
                "credited_from": element.credited_from,
                "reason_code": element.reason_code,
                "evidence_refs": refs,
            }
        )
    return result


def _assurance(
    value: object, document_version_id: str, parse_manifest_id: str | None
) -> dict[str, Any]:
    if value is None:
        return {"status": "not_run"}
    if not isinstance(value, Mapping):
        raise ValueError("assurance must be an object or null")
    copied = _copy(value)
    if set(copied) != {
        "status",
        "level",
        "provider",
        "statement_id",
        "metric_match",
        "period_match",
        "boundary_match",
        "evidence_refs",
    } or copied["status"] not in ("covered", "not_covered", "undetermined"):
        raise ValueError("invalid assurance status")
    if copied["level"] not in ("limited", "reasonable", "none", None):
        raise ValueError("invalid assurance level")
    if copied["provider"] is not None and not isinstance(copied["provider"], str):
        raise ValueError("invalid assurance provider")
    if copied["statement_id"] is not None:
        _require_uuid("statement_id", copied["statement_id"])
    if any(
        copied[name] not in ("yes", "no", "unknown")
        for name in (
            "metric_match",
            "period_match",
            "boundary_match",
        )
    ):
        raise ValueError("invalid assurance dimension match")
    copied["evidence_refs"] = _source_refs(
        copied["evidence_refs"], document_version_id, parse_manifest_id
    )
    return copied


def _safe_harbor(
    value: object,
    claim_id: str,
    document_version_id: str,
    parse_manifest_id: str | None,
) -> dict[str, Any]:
    if value is None:
        return {"status": "not_run"}
    if not isinstance(value, Mapping):
        raise ValueError("safe_harbor must be an object or null")
    copied = _copy(value)
    required = {
        "claim_id",
        "applicable",
        "category",
        "checklist",
        "reasonable_basis_documented",
        "legal_effect",
        "mapping_status",
        "gap_ids",
    }
    if (
        set(copied) != required
        or copied["claim_id"] != claim_id
        or not (type(copied["applicable"]) is bool or copied["applicable"] is None)
        or copied["legal_effect"] != "not_determined"
        or copied["mapping_status"] not in ("approved", "unresolved")
        or not isinstance(copied["checklist"], list)
    ):
        raise ValueError("safe_harbor does not match the immutable claim record")
    _strings(copied["gap_ids"], "safe_harbor.gap_ids")
    for item in copied["checklist"]:
        if not isinstance(item, dict) or not isinstance(item.get("element_id"), str):
            raise ValueError("safe_harbor checklist is malformed")
        item["evidence_refs"] = _source_refs(
            item.get("evidence_refs"), document_version_id, parse_manifest_id
        )
        if item.get("state") == "present" and not item["evidence_refs"]:
            raise ValueError("source-less safe_harbor present is prohibited")
    return copied


def _provenance(
    record: Mapping[str, object],
    *,
    tag_revision: int,
    parse_manifest_id: str | None,
    source_sha256: str,
) -> dict[str, Any]:
    if (
        record.get("parse_manifest_id") != parse_manifest_id
        or record.get("source_sha256") != source_sha256
    ):
        raise ValueError("claim provenance does not match the manifest")
    model = record.get("model_sha256")
    prompt = record.get("prompt_sha256")
    replicas = record.get("replicate_hashes")
    if not isinstance(replicas, list | tuple):
        raise ValueError("replicate_hashes must be an array")
    if tag_revision == 0:
        if model is not None or prompt is not None or replicas:
            raise ValueError("untagged claim cannot carry model provenance")
        return {"model_sha256": None, "prompt_sha256": None, "replicate_hashes": []}
    if len(replicas) != 3:
        raise ValueError("tagged claim must preserve exactly three replicate hashes")
    return {
        "model_sha256": _require_sha256("model_sha256", model),
        "prompt_sha256": _require_sha256("prompt_sha256", prompt),
        "replicate_hashes": [_require_sha256("replicate_hash", value) for value in replicas],
    }


# Human-readable "what to check next" text per reason. These describe review work,
# never a grade, number, or legal conclusion. `suggestion` still owns verified-missing
# elements; review_action covers the remaining unresolved / not-run / blocked states.
_REVIEW_CHECKS = {
    "not_processed": (
        "판정이 아직 실행되지 않았습니다. "
        "주장 상세에서 원문 검증·분류·태깅 상태를 확인하고 미완료 단계를 진행하세요."
    ),
    "unresolved_evidence": "미해결 요소의 원문 근거 귀속을 확인(absent 단정 금지): ",
    "basis_validation_pending": (
        "기준 조항의 대응이 미확인입니다. 승인된 기준 원문과 해당 요소의 대응을 확인하세요."
    ),
    "source_location_missing": "확정된 원문 위치가 없습니다. 근거 페이지·좌표를 먼저 확보하세요.",
    "assurance_not_run": "보증 대조 미실행. 보증서 기관·기간·지표·경계를 대조하세요.",
    "safe_harbor_not_run": "세이프하버 점검 미실행. 적용 여부와 체크리스트를 검토하세요.",
    "domain_gap": "아직 확정되지 않은 규칙 항목을 확인하고 해당 판정에 미치는 영향을 검토하세요: ",
}


def _review_action(claim: dict[str, Any]) -> dict[str, Any] | None:
    """Project a distinct, source-preserving follow-up for unresolved review work.

    Additive to `suggestion` (verified-missing only). Returns None when a decided
    claim has no outstanding review reason. Never converts unknown/unresolved into
    absent and never invents numbers, grades, or legal facts.
    """
    reasons: list[str] = []
    checks: list[str] = []
    status = claim["decision_status"]
    unresolved: list[str] = claim["unresolved_elements"]
    gaps: list[str] = claim["gap_ids"]
    if status == "not_run":
        reasons.append("not_processed")
        checks.append(_REVIEW_CHECKS["not_processed"])
    if unresolved:
        reasons.append("unresolved_evidence")
        checks.append(_REVIEW_CHECKS["unresolved_evidence"] + ", ".join(unresolved))
    if claim["source_status"] == "not_run" and status != "not_run":
        reasons.append("source_location_missing")
        checks.append(_REVIEW_CHECKS["source_location_missing"])
    unverified_basis = any(
        basis.get("clause") is None or basis.get("verification_status") != "verified"
        for basis in claim["basis_refs"]
    )
    if unverified_basis:
        reasons.append("basis_validation_pending")
        checks.append(_REVIEW_CHECKS["basis_validation_pending"])
    if claim["assurance"].get("status") == "not_run":
        reasons.append("assurance_not_run")
        checks.append(_REVIEW_CHECKS["assurance_not_run"])
    if claim["safe_harbor"].get("status") == "not_run":
        reasons.append("safe_harbor_not_run")
        checks.append(_REVIEW_CHECKS["safe_harbor_not_run"])
    if gaps:
        reasons.append("domain_gap")
        checks.append(_REVIEW_CHECKS["domain_gap"] + ", ".join(gaps))
    if not reasons:
        return None
    return {
        "claim_id": claim["claim_id"],
        "reasons": reasons,
        "checks": checks,
        "unresolved_elements": list(unresolved),
        "gap_ids": list(gaps),
        "source_pages": [source["page_num"] for source in claim["source_refs"]],
    }


def _unfinished(
    ref: Mapping[str, object],
    record: Mapping[str, object] | None,
    *,
    tenant_id: str,
    document_version_id: str,
    parse_manifest_id: str | None,
    source_sha256: str,
) -> dict[str, Any]:
    tag_revision = _require_strict_int("tag_revision", ref["tag_revision"])
    result = {
        "claim_id": ref["claim_id"],
        "tag_revision": tag_revision,
        "decision_revision": 0,
        "decision_status": "not_run",
        "evidence_grade": None,
        "label": None,
        "grade_range": None,
        "review_status": "needs_review",
        "missing_elements": [],
        "unresolved_elements": [],
        "gap_ids": [],
        "rule_pack_sha256": None,
        "source_refs": [],
        "source_status": "not_run",
        "basis_refs": [],
        "assurance": {"status": "not_run"},
        "safe_harbor": {"status": "not_run"},
        "suggestion": None,
    }
    if record is None:
        if tag_revision:
            raise ValueError("tagged unfinished claim must preserve its provenance record")
        return result | {
            "model_sha256": None,
            "prompt_sha256": None,
            "replicate_hashes": [],
            "tag_elements": None,
        }
    if (
        record.get("tenant_id") != tenant_id
        or record.get("document_version_id") != document_version_id
        or record.get("claim_id") != ref["claim_id"]
        or record.get("tag_revision") != ref["tag_revision"]
        or record.get("decision_revision") != 0
        or record.get("decision_status") not in (None, "not_run")
        or record.get("evidence_grade") is not None
        or record.get("label") is not None
    ):
        raise ValueError("unfinished claim does not match the pinned revision")
    sources = _source_refs(record.get("source_refs"), document_version_id, parse_manifest_id)
    tag_elements = _tag_elements(record.get("tag_elements"), document_version_id, parse_manifest_id)
    if tag_revision == 0 and tag_elements:
        raise ValueError("untagged claim cannot carry tag elements")
    return (
        result
        | _provenance(
            record,
            tag_revision=tag_revision,
            parse_manifest_id=parse_manifest_id,
            source_sha256=source_sha256,
        )
        | {
            "unresolved_elements": _strings(
                record.get("unresolved_elements", []), "unresolved_elements"
            ),
            "source_refs": sources,
            "source_status": "available" if sources else "not_run",
            "tag_elements": tag_elements,
        }
    )


def build_report_model(
    manifest: Mapping[str, object],
    decisions: Mapping[str, Mapping[str, object] | None],
) -> dict[str, Any]:
    """Build report data without grading or inventing corrective values."""
    if not isinstance(manifest, Mapping) or not isinstance(decisions, Mapping):
        raise ValueError("snapshot manifest and decisions must be mappings")
    tenant_id = _require_uuid("tenant_id", manifest.get("tenant_id"))
    run_id = _require_uuid("run_id", manifest.get("run_id"))
    document_version_id = _require_uuid("document_version_id", manifest.get("document_version_id"))
    raw_parse_manifest_id = manifest.get("parse_manifest_id")
    parse_manifest_id: str | None = None
    if raw_parse_manifest_id is not None:
        parse_manifest_id = _require_uuid("parse_manifest_id", raw_parse_manifest_id)
    source_sha256 = _require_sha256("source_sha256", manifest.get("source_sha256"))
    epoch = _require_strict_int("mutation_epoch", manifest.get("mutation_epoch"))
    if epoch < 0:
        raise ValueError("mutation_epoch must be non-negative")
    generated_at = manifest.get("generated_at")
    profile = manifest.get("execution_profile")
    if not isinstance(generated_at, str) or not generated_at or not isinstance(profile, str):
        raise ValueError("generated_at and execution_profile are required")
    coverage = _validated_coverage(manifest.get("coverage"))
    raw_hashes = manifest.get("rule_pack_hashes")
    if not isinstance(raw_hashes, list | tuple) or not raw_hashes:
        raise ValueError("rule_pack_hashes must be a non-empty array")
    rule_pack_hashes = tuple(_require_sha256("rule_pack_sha256", value) for value in raw_hashes)
    if len(set(rule_pack_hashes)) != len(rule_pack_hashes):
        raise ValueError("rule_pack_hashes must be unique")
    unverified_basis = manifest.get("unverified_basis")
    if _require_strict_int("unverified_basis", unverified_basis) < 0:
        raise ValueError("unverified_basis must be non-negative")

    refs = manifest.get("claim_revision_refs")
    if not isinstance(refs, list | tuple) or len(refs) != coverage["claims_discovered"]:
        raise ValueError("claim revision refs must cover every discovered claim")
    if parse_manifest_id is None and (refs or decisions):
        raise ValueError("null parse manifest is valid only before any claim is discovered")
    seen = set()
    claims = []
    for ref in refs:
        if not isinstance(ref, Mapping) or set(ref) != {
            "claim_id",
            "tag_revision",
            "decision_revision",
        }:
            raise ValueError("invalid claim revision reference")
        claim_id = ref["claim_id"]
        _require_uuid("claim_id", claim_id)
        tag_revision = _require_strict_int("tag_revision", ref["tag_revision"])
        decision_revision = _require_strict_int("decision_revision", ref["decision_revision"])
        if (
            claim_id in seen
            or tag_revision < 0
            or decision_revision < 0
            or (decision_revision > 0 and tag_revision < 1)
            or (tag_revision == 0 and decision_revision != 0)
        ):
            raise ValueError("invalid or duplicate claim revision reference")
        seen.add(claim_id)
        if claim_id not in decisions:
            raise ValueError("snapshot decision is missing")
        record = decisions[claim_id]
        if decision_revision == 0:
            claims.append(
                _unfinished(
                    ref,
                    record,
                    tenant_id=tenant_id,
                    document_version_id=document_version_id,
                    parse_manifest_id=parse_manifest_id,
                    source_sha256=source_sha256,
                )
            )
            continue
        if not isinstance(record, Mapping):
            raise ValueError("decision revision is missing")
        if (
            record.get("tenant_id") != tenant_id
            or record.get("document_version_id") != document_version_id
            or record.get("parse_manifest_id") != parse_manifest_id
            or record.get("source_sha256") != source_sha256
            or record.get("claim_id") != claim_id
            or record.get("tag_revision") != tag_revision
            or record.get("decision_revision") != decision_revision
        ):
            raise ValueError("decision does not match the pinned revision")
        rule_hash = _require_sha256("rule_pack_sha256", record.get("rule_pack_sha256"))
        if rule_hash not in rule_pack_hashes:
            raise ValueError("decision rule pack is not pinned by the manifest")
        status = record.get("decision_status")
        grade = record.get("evidence_grade")
        label = record.get("label")
        if status not in _STATUSES:
            raise ValueError("invalid decision status")
        if status == "decided":
            if grade not in _LABELS or label != _LABELS[grade]:
                raise ValueError("decided claim needs the fixed grade and label")
        elif grade is not None or label is not None:
            raise ValueError("unfinished decision cannot carry a grade or label")
        review = record.get("review_status")
        if review not in _REVIEWS:
            raise ValueError("invalid review status")
        missing = _strings(record.get("missing_elements"), "missing_elements")
        unresolved = _strings(record.get("unresolved_elements"), "unresolved_elements")
        gaps = _strings(record.get("gap_ids"), "gap_ids")
        basis = _basis_refs(record.get("basis_refs"))
        sources = _source_refs(record.get("source_refs"), document_version_id, parse_manifest_id)
        tag_elements = _tag_elements(
            record.get("tag_elements"), document_version_id, parse_manifest_id
        )
        if status == "decided" and not sources:
            raise ValueError("decided claim must preserve its source reference")
        provenance = _provenance(
            record,
            tag_revision=tag_revision,
            parse_manifest_id=parse_manifest_id,
            source_sha256=source_sha256,
        )
        claims.append(
            {
                "claim_id": claim_id,
                "tag_revision": tag_revision,
                "decision_revision": decision_revision,
                "decision_status": status,
                "evidence_grade": grade,
                "label": label,
                "grade_range": _grade_range(record, status),
                "review_status": review,
                "missing_elements": missing,
                "unresolved_elements": unresolved,
                "gap_ids": gaps,
                "rule_pack_sha256": rule_hash,
                "source_refs": sources,
                "source_status": "available" if sources else "not_run",
                "basis_refs": basis,
                "tag_elements": tag_elements,
                **provenance,
                "assurance": _assurance(
                    record.get("assurance"), document_version_id, parse_manifest_id
                ),
                "safe_harbor": _safe_harbor(
                    record.get("safe_harbor"),
                    claim_id,
                    document_version_id,
                    parse_manifest_id,
                ),
                "suggestion": (
                    f"원문 근거로 다음 결손 요소를 보완하세요: {', '.join(missing)}."
                    if missing
                    else None
                ),
            }
        )
    if set(decisions) != seen:
        raise ValueError("decisions contain claims outside the snapshot")
    for claim in claims:
        record = decisions[claim["claim_id"]]
        quote = record.get("claim_quote") if record is not None else None
        if quote is not None and (not isinstance(quote, str) or not quote.strip()):
            raise ValueError("claim_quote must be non-empty text or null")
        claim["claim_quote"] = quote
        classification = record.get("classification_review") if record is not None else None
        if classification is not None:
            if (
                not isinstance(classification, dict)
                or set(classification)
                != {"classification_id", "record_sha256", "revision", "origin", "track"}
                or classification["origin"]
                not in {"human_classification", "ai_delegated_classification"}
                or classification["track"] not in {"goal", "performance", "management"}
                or type(classification["revision"]) is not int
                or classification["revision"] < 1
            ):
                raise ValueError("invalid classification review provenance")
            _require_uuid("classification_id", classification["classification_id"])
            _require_sha256("record_sha256", classification["record_sha256"])
            classification = dict(classification)
        claim["classification_review"] = classification
        claim["review_action"] = _review_action(claim)
    unfinished_count = sum(item["decision_status"] != "decided" for item in claims)
    unverified_clause_count = sum(
        basis.get("clause") is None or basis["verification_status"] != "verified"
        for claim in claims
        for basis in claim["basis_refs"]
    )
    if coverage["claims_decided"] != len(claims) - unfinished_count:
        raise ValueError("coverage does not match the pinned decision statuses")
    if unverified_basis != unverified_clause_count:
        raise ValueError("manifest unverified basis count does not match decisions")
    return {
        "schema": "report_model_v1",
        "tenant_id": tenant_id,
        "run_id": run_id,
        "document_version_id": document_version_id,
        "parse_manifest_id": parse_manifest_id,
        "source_sha256": source_sha256,
        "snapshot_epoch": epoch,
        "generated_at": generated_at,
        "execution_profile": profile,
        "rule_pack_hashes": list(rule_pack_hashes),
        "coverage": coverage,
        "unverified_basis": unverified_basis,
        "partial": bool(not coverage["complete"] or unfinished_count or unverified_clause_count),
        "unfinished_count": unfinished_count,
        "unverified_clause_count": unverified_clause_count,
        "claims": claims,
    }


def _csv_safe(value: object) -> str:
    text = value if isinstance(value, str) else canonical_json(value)
    return f"'{text}" if text.lstrip().startswith(("=", "+", "-", "@")) else text


def render_report(model: Mapping[str, object], output_format: str) -> bytes:
    """Render fixed report data with stdlib-only output escaping."""
    if not isinstance(model, Mapping) or model.get("schema") != "report_model_v1":
        raise ValueError("report_model_v1 is required")
    if output_format == "json":
        return canonical_json(model).encode()
    claims = model.get("claims")
    if not isinstance(claims, list) or any(not isinstance(item, Mapping) for item in claims):
        raise ValueError("report claims are required")
    if output_format == "csv":
        fields = (
            "claim_id",
            "decision_status",
            "evidence_grade",
            "label",
            "review_status",
            "tag_revision",
            "decision_revision",
            "missing_elements",
            "unresolved_elements",
            "suggestion",
            "review_action",
            "source_pages",
            "source_quotes",
            "source_refs",
            "basis_refs",
            "assurance",
            "safe_harbor",
            "model_sha256",
            "prompt_sha256",
            "replicate_hashes",
            "rule_pack_sha256",
            "claim_quote",
            "classification_review",
            "tag_elements",
            "grade_range",
        )
        stream = StringIO(newline="")
        rows = writer(stream)
        rows.writerow(fields)
        for claim in claims:
            sources = claim["source_refs"]
            values = {
                **claim,
                "source_pages": [source["page_num"] for source in sources],
                "source_quotes": "\n".join(source["quote"] for source in sources),
            }
            rows.writerow(_csv_safe(values.get(field)) for field in fields)
        return stream.getvalue().encode()
    if output_format == "html":
        items = []
        for claim in claims:
            sources = (
                "".join(
                    f"<li>p.{source['page_num']}"
                    f" [{', '.join(str(value) for value in source['bbox'])}]"
                    f": {escape(source['quote'])}</li>"
                    if source["bbox"] is not None
                    else f"<li>p.{source['page_num']}: {escape(source['quote'])}</li>"
                    for source in claim["source_refs"]
                )
                or "<li>원문 근거 위치 미실행</li>"
            )
            grade_label = (
                f"{claim['evidence_grade']} / {claim['label']}"
                if claim["evidence_grade"] is not None
                else "미판정"
            )
            grade_range = claim.get("grade_range")
            range_html = (
                f"<p>가능 등급 범위: {escape(grade_range['floor'])} ~ "
                f"{escape(grade_range['ceiling'])} (확정 등급 아님) · "
                "확인하면 범위가 좁혀지는 요소: "
                f"{escape(', '.join(map(_element_label, grade_range['open_elements'])))}</p>"
                if grade_range
                else ""
            )
            audit_details = escape(canonical_json(claim))
            empty_result = "미평가" if claim["decision_status"] == "not_run" else "없음"
            action = claim.get("review_action")
            if action:
                action_html = (
                    '<div class="review-action"><p>다음 검토 작업:</p><ul>'
                    + "".join(f"<li>{escape(check)}</li>" for check in action["checks"])
                    + "</ul></div>"
                )
            else:
                action_html = "<p>자동 생성된 후속 검토 안내 없음</p>"
            quote = claim.get("claim_quote") or "이전 스냅샷에 주장 문장이 저장되지 않았습니다"
            tag_elements = claim.get("tag_elements")
            if tag_elements is None:
                tag_html = "<p>태그 요소 미포함(이전 스냅샷)</p>"
            elif not tag_elements:
                tag_html = "<p>태그된 요소 없음(미태깅)</p>"
            else:
                present_parts = []
                candidate_parts = []
                other_parts = []
                for element in tag_elements:
                    value = element.get("normalized_value")
                    value_text = escape(value) if isinstance(value, str) else "기록 없음"
                    evidence_refs = element.get("evidence_refs", [])
                    quotes = (
                        "".join(
                            f"<li>p.{ref['page_num']}: {escape(ref['quote'])}</li>"
                            for ref in evidence_refs
                        )
                        or "<li>인용 없음</li>"
                    )
                    item_html = (
                        f"<li>{escape(_element_label(element['element_id']))}: "
                        f"{escape(_ELEMENT_STATES.get(element['state'], element['state']))}"
                        f" ({escape(element['state'])}) · 값: {value_text}"
                        f"<ul>{quotes}</ul></li>"
                    )
                    if element["state"] == "present":
                        present_parts.append(item_html)
                    elif element["state"] in ("unknown", "conflict") and evidence_refs:
                        candidate_parts.append(item_html)
                    else:
                        other_parts.append(item_html)

                tag_html = ""
                if present_parts:
                    tag_html += (
                        "<h3>추출된 항목 (present)</h3><ul>" + "".join(present_parts) + "</ul>"
                    )
                if candidate_parts:
                    tag_html += (
                        "<h3>미해결 태그 요소 검토 후보</h3><ul>"
                        + "".join(candidate_parts)
                        + "</ul>"
                    )
                if other_parts:
                    tag_html += "<h3>기타 태그 요소</h3><ul>" + "".join(other_parts) + "</ul>"
            classification = claim.get("classification_review")
            classification_html = ""
            if classification:
                origin = (
                    "AI 위임 분류(사람 검토 아님)"
                    if classification["origin"] == "ai_delegated_classification"
                    else "사람 분류 검토"
                )
                classification_html = (
                    f"<p>선행분류 기록: {origin} · {escape(classification['track'])} · "
                    f"revision {classification['revision']} (등급 승인 아님)</p>"
                )
            missing = ", ".join(map(_element_label, claim["missing_elements"])) or empty_result
            unresolved = (
                ", ".join(map(_element_label, claim["unresolved_elements"])) or empty_result
            )
            items.append(
                "<section>"
                f"<h2>검토 대상 주장</h2><p>{escape(quote)}</p>"
                f"<p>주장 ID: {escape(claim['claim_id'])}</p>{classification_html}"
                f"<p>판정: {escape(grade_label)} ({escape(claim['decision_status'])})</p>"
                f"{range_html}"
                f"<p>검토: {escape(claim['review_status'])} · "
                f"tag revision {claim['tag_revision']} · decision revision "
                f"{claim['decision_revision']}</p>"
                f"<p>결손: {escape(missing)} · "
                f"미해결: {escape(unresolved)} · "
                f"gaps: {escape(', '.join(claim['gap_ids']) or empty_result)}</p>"
                f"<p>{escape(claim.get('suggestion') or '확정된 수정 제안 없음')}</p>"
                f"{action_html}"
                f"<h3>원문 근거</h3><ul>{sources}</ul>"
                f"{tag_html}"
                f"<details><summary>감사 세부정보</summary><pre>{audit_details}</pre></details>"
                "</section>"
            )
        status = "검토용 부분 리포트" if model.get("partial") else "완료 리포트"
        coverage_details = escape(canonical_json(model["coverage"]))
        return (
            '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<style>body{max-width:960px;margin:32px auto;padding:0 20px;'
            'font:16px/1.6 system-ui,sans-serif;color:#1e293b;background:#f8fafc}'
            'section{margin:20px 0;padding:20px;border:1px solid #cbd5e1;'
            'border-radius:6px;background:white;break-inside:avoid}'
            'h2{font-size:1.05rem}h2,li,pre{overflow-wrap:anywhere}'
            'pre{white-space:pre-wrap;font-size:.85rem}'
            'summary{cursor:pointer;padding:8px 0}li+li{margin-top:8px}'
            '.review-action{border-left:3px solid #0369a1;padding-left:16px}'
            '@media print{body{margin:0;background:white}section{border-radius:0}}'
            '</style>'
            f"<title>감사 리포트</title></head><body><h1>감사 리포트</h1><p>{status}</p>"
            f"<p>미완료 {model['unfinished_count']}건 · "
            f"미확인 조항 {model['unverified_clause_count']}건</p>"
            f"<details><summary>처리 범위</summary><pre>{coverage_details}</pre></details>"
            + "".join(items)
            + "</body></html>"
        ).encode()
    raise ValueError("output_format must be json, csv, or html")
