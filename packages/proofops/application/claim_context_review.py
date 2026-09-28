"""Explicit source-backed facility section review; never a numeric pass from itself."""

from dataclasses import asdict
from decimal import Decimal, InvalidOperation

from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.numeric_analysis import analyze_numeric_consistency
from proofops.domain.numeric import unit_note_literal
from proofops.domain.periods import is_supported_period
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _source_ref_from_dict

POLICY = "facility_section_context_v1"


def review_facility_context(inputs, request, verify_sources):
    """Review only a bounded paragraph section, with explicit literal role tags.

    No observation/binding is manufactured from a narrative claim. The existing
    numeric service decides that a check without a bound table remains on hold.
    Same-section comparison candidates remain unresolved and are retained.
    """
    if (
        not isinstance(request, dict)
        or set(request) != {"policy", "input_snapshot_sha256", "dimensions", "section_end"}
        or request["policy"] != POLICY
        or request["input_snapshot_sha256"] != canonical_hash(inputs.snapshot())
        or not isinstance(request["dimensions"], dict)
        or set(request["dimensions"]) != {"facility", "reporting_period", "metric", "value", "unit"}
        or verify_sources is None
    ):
        raise ValueError("CONTEXT_REVIEW_INVALID")
    claim = inputs.context.claim
    if claim.source_quality != "verified" or len(claim.source_refs) != 1:
        raise ValueError("CONTEXT_CLAIM_SOURCE_REQUIRED")
    own = claim.source_refs[0]
    roles = {k: _source_ref_from_dict(v) for k, v in request["dimensions"].items()}
    end = _source_ref_from_dict(request["section_end"])
    graph, source_receipt = verify_sources(inputs, (*roles.values(), end))
    roles = {k: verify_source_ref(r, graph, tenant_id=claim.tenant_id) for k, r in roles.items()}
    end = verify_source_ref(end, graph, tenant_id=claim.tenant_id)
    if any(r.verification_state != "verified" for r in (*roles.values(), end)):
        raise ValueError("CONTEXT_SOURCE_REJECTED")
    for role in ("metric", "value", "unit", "reporting_period"):
        ref = roles[role]
        if ref.source_id != own.source_id or not (
            own.char_start <= ref.char_start < ref.char_end <= own.char_end
        ):
            raise ValueError("CONTEXT_LOCAL_DIMENSION_REQUIRED")
    try:
        valid_value = Decimal(roles["value"].quote).is_finite()
    except InvalidOperation:
        valid_value = False
    if (
        not valid_value
        or unit_note_literal("unit: " + roles["unit"].quote) != roles["unit"].quote
        or not is_supported_period(roles["reporting_period"].quote)
        or roles["value"].char_end > roles["unit"].char_start
        or own.quote[
            roles["value"].char_end - own.char_start : roles["unit"].char_start - own.char_start
        ].strip()
    ):
        raise ValueError("CONTEXT_QUANTITY_OR_PERIOD_INVALID")
    facility = roles["facility"]
    if any(r.bbox is None or r.page_num != own.page_num for r in (own, facility, end)):
        raise ValueError("CONTEXT_SECTION_UNLOCATED")
    if not (
        facility.bbox[3] <= own.bbox[1] < own.bbox[3] <= end.bbox[1]
        and all(own.bbox[0] <= (r.bbox[0] + r.bbox[2]) / 2 <= own.bbox[2] for r in (facility, end))
        and facility.source_id != end.source_id
    ):
        raise ValueError("CONTEXT_SECTION_MISMATCH")
    # Frozen model candidates remain the inventory, even after repeated reviews.
    candidates = {}
    for run in inputs.tag_runs:
        if run.guarded:
            for element in run.guarded.elements:
                if element.element_id == "P6":
                    for ref in element.evidence_refs:
                        candidates[canonical_hash(asdict(ref))] = ref
    considered = []
    for ref in candidates.values():
        checked = verify_source_ref(ref, graph, tenant_id=claim.tenant_id)
        status = "unresolved"
        if checked.verification_state == "verified":
            if ref.source_id == own.source_id:
                status = "claim_source"
            elif (
                ref.page_num == own.page_num
                and ref.bbox
                and (
                    ref.bbox[3] <= facility.bbox[3]
                    or ref.bbox[1] >= end.bbox[1]
                    or ref.bbox[2] <= own.bbox[0]
                    or ref.bbox[0] >= own.bbox[2]
                )
            ):
                status = "outside_reviewed_section"
        considered.append(dict(source_ref=asdict(checked), status=status))
    # A narrative span is not a second, independently bound table observation.
    result = analyze_numeric_consistency(
        tenant_id=claim.tenant_id, original=graph, observations=(), bindings=(), claims=(claim,)
    )
    numeric = dict(
        status="needs_review", reason="no_comparable_table_observation", considered=considered
    )
    return dict(
        policy=POLICY,
        request=request,
        source_receipt=source_receipt,
        identity=dict(
            tenant_id=claim.tenant_id,
            run_id=inputs.run_id,
            claim_id=claim.claim_id,
            document_version_id=claim.document_version_id,
            parse_manifest_id=claim.parse_manifest_id,
            source_sha256=claim.source_sha256,
        ),
        numeric_result=asdict(result),
        projection=dict(
            origin="ai_delegated",
            dimensions={k: asdict(r) for k, r in roles.items()},
            numeric_check=numeric,
        ),
    )
