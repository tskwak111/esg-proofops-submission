"""Tenant-scoped immutable extraction reads; pending tags are not invented."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.catalog_pages import CatalogCapacityExceeded, InvalidCatalogCursor
from proofops.application.assurance import ClaimContext as AssuranceContext
from proofops.application.assurance import claim_context_from_review_inputs, match_assurance
from proofops.application.runs import RunRejected
from proofops.domain.provenance import canonical_hash
from proofops.domain.rules.engine import MAPPINGS
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
)
from proofops_api.dto import Decision
from proofops_api.routers.documents import StrictDTO
from proofops_api.routers.reviews import Element
from proofops_api.routers.sources import SourceRef
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field

Track = Literal["goal", "performance", "management"]
Grade = Literal["E0", "E1", "E2", "E3"]
ReviewStatus = Literal[
    "auto_confirmed", "needs_review", "human_confirmed", "ai_delegated_confirmed"
]

# Suggested next action per worker blocked_reason code (tag_runner.py). Text
# only; never changes what is blocked or bypasses the source-verification gate.
_BLOCKED_ACTION_TEXT: dict[str, str] = {
    "DOMAIN_RULEPACK_UNAPPROVED": (
        "이 실행의 규칙집은 검토 전입니다. 검토가 완료된 규칙집으로 새 판정을 실행해 주세요."
    ),
    "CONSENSUS_UNRESOLVED": (
        "태깅 결과가 일치하지 않습니다. 원문과 항목별 응답을 비교해 태깅을 검토해 주세요."
    ),
    "RELATION_TAGS_UNRESOLVED": (
        "주장과 근거의 연결이 확정되지 않았습니다. 연도·대상·범위를 확인해 주세요."
    ),
    "SOURCE_VALIDATION_REQUIRED": "원문 근거 검증을 다시 확인해 주세요.",
    "TAGGING_RUNTIME_REQUIRED": (
        "태깅 모델 런타임이 아직 연결되지 않았습니다. 운영 설정을 확인해 주세요."
    ),
    "PRELIMINARY_TAGS_REQUIRED": (
        "예비 태깅 런타임이 아직 연결되지 않았습니다. 운영 설정을 확인해 주세요."
    ),
    "PRELIMINARY_TAGS_UNRESOLVED": (
        "예비 태깅 결과를 확정할 수 없습니다. 원문과 항목별 응답을 검토해 주세요."
    ),
    "SOURCE_LOCATION_REQUIRED": "이 주장의 원문 위치를 다시 파싱하거나 범위를 추가해 주세요.",
    "RULEPACK_CATALOG_REQUIRED": (
        "이 실행에 승인된 요소 카탈로그(rubric)가 없습니다. 규칙집을 확인해 주세요."
    ),
    "EVIDENCE_PACKET_BLOCKED": "근거 패킷이 차단되었습니다. 근거 검색 범위를 다시 확인해 주세요.",
}

_PRELIMINARY_UNRESOLVED_ACTION = {
    "agreed": (
        "세 차례 분석 모두 주장 유형을 정하지 못했습니다. "
        "원문 문맥과 주장 내용을 확인한 뒤 태깅을 검토해 주세요."
    ),
    "conflict": (
        "예비 태깅 복제본의 응답이 서로 다릅니다. 항목별 응답을 비교해 태깅을 검토해 주세요."
    ),
    "incomplete": (
        "예비 태깅에 필요한 응답이 모두 확인되지 않았습니다. 응답 상태를 검토해 주세요."
    ),
    "unknown": _BLOCKED_ACTION_TEXT["PRELIMINARY_TAGS_UNRESOLVED"],
}


def _preliminary_unresolved_action(agreement):
    """Explain stored agreement without changing the blocked state or decision."""
    if not isinstance(agreement, dict):
        return _PRELIMINARY_UNRESOLVED_ACTION["unknown"]
    validated = agreement.get("validated_replicates")
    # Missing legacy counts cannot establish either completeness or agreement.
    if type(validated) is not int or not 0 <= validated <= 3:
        return _PRELIMINARY_UNRESOLVED_ACTION["unknown"]
    if validated < 3:
        return _PRELIMINARY_UNRESOLVED_ACTION["incomplete"]
    fields = agreement.get("fields")
    dimensions = agreement.get("dimensions")
    states = [
        entry.get("state")
        for group in (fields, dimensions)
        if isinstance(group, dict)
        for entry in group.values()
        if isinstance(entry, dict)
    ]
    if "conflict" in states:
        return _PRELIMINARY_UNRESOLVED_ACTION["conflict"]
    if "unresolved" in states:
        return _PRELIMINARY_UNRESOLVED_ACTION["incomplete"]
    track = fields.get("track") if isinstance(fields, dict) else None
    if (
        isinstance(track, dict)
        and track.get("state") == "agreed"
        and track.get("replicate_values") == [None, None, None]
    ):
        return _PRELIMINARY_UNRESOLVED_ACTION["agreed"]
    return _PRELIMINARY_UNRESOLVED_ACTION["unknown"]


class ClaimSummary(StrictDTO):
    claim_id: UUID
    page_num: Annotated[int, Field(ge=1)]
    quote: str
    track: Track | None
    topic_ids: list[str]
    decision: Decision | None
    revision: Annotated[int, Field(ge=1)]


class ClaimSummaryPage(StrictDTO):
    items: list[ClaimSummary]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


class AssuranceMatch(StrictDTO):
    status: Literal["covered", "not_covered", "undetermined"]
    level: Literal["limited", "reasonable", "none"] | None
    provider: str | None
    statement_id: UUID | None
    metric_match: Literal["yes", "no", "unknown"]
    period_match: Literal["yes", "no", "unknown"]
    boundary_match: Literal["yes", "no", "unknown"]
    evidence_refs: list[SourceRef]


class BasisRef(StrictDTO):
    standard: str
    clause: str | None
    summary: str
    verification_status: Literal["verified", "unverified", "unlicensed"]


class FieldAgreement(StrictDTO):
    field_id: str
    status: Literal["agreed", "conflict", "unresolved"]
    replicate_values: list[object]


class RawCandidate(StrictDTO):
    source_ref: SourceRef
    status: Literal["candidate", "unverified", "unconfirmed"]
    reason: str | None = None


class ReviewProjection(StrictDTO):
    """Additive, read-only view of a blocked/in-progress tagging attempt.

    Sourced from `LocalTagStore.load_snapshot(...)["claims"][i]`: `reason`,
    `original_packet.evidence_candidates[].source_refs[].quote`,
    `preliminary_agreement.fields`/`.dimensions`, and `raw_candidate_review.candidates`.
    Never a substitute for ConfirmedTags or an accepted binding: nothing here can feed the final
    grade or the tag-edit endpoint without its own source verification.
    `schema_version` lets the UI reject payloads it does not recognize
    instead of guessing at an unfamiliar shape. Older records that predate
    `original_packet`/`preliminary_agreement`/`raw_candidate_review` still report `blocked_reason`
    alone with empty candidates/fields.
    """

    schema_version: Literal[1] = 1
    candidate_snippets: list[str]
    blocked_reason: str | None
    blocked_action: str | None
    field_agreements: list[FieldAgreement]
    raw_candidates: list[RawCandidate] = Field(default_factory=list)


class ReviewedDimensions(StrictDTO):
    facility: SourceRef
    reporting_period: SourceRef
    metric: SourceRef
    value: SourceRef
    unit: SourceRef


class NumericCandidateReview(StrictDTO):
    source_ref: SourceRef
    status: Literal["claim_source", "outside_reviewed_section", "unresolved"]


class NumericContextCheck(StrictDTO):
    status: Literal["needs_review"]
    reason: Literal["no_comparable_table_observation"]
    considered: list[NumericCandidateReview]


class ReviewedContext(StrictDTO):
    origin: Literal["ai_delegated"]
    dimensions: ReviewedDimensions
    numeric_check: NumericContextCheck


class SubmittedReview(StrictDTO):
    schema_version: Literal[1]
    status: Literal["reference_only"]
    origin: Literal["data_manager_submission", "ai_corrected_submission"]
    tenant_id: UUID
    run_id: UUID
    document_version_id: UUID
    claim_id: UUID
    external_claim_id: str
    reference_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    source_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    run_input_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    claim_snapshot_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    claim_source_quality: str
    files_sha256: dict[str, Annotated[str, Field(pattern="^[0-9a-f]{64}$")]]
    tables: dict[Literal["claims", "elements", "numeric", "assurance"], list[dict[str, str]]]


class ClaimDetail(StrictDTO):
    claim: ClaimSummary
    source_refs: list[SourceRef]
    elements: list[Element]
    assurance: AssuranceMatch
    replicate_request_ids: list[str]
    packet_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None
    suggestion: str | None
    basis_refs: list[BasisRef]
    tag_status: Literal["tagged", "untagged"] | None = None
    rulepack_approved_by: str | None = None
    review_projection: ReviewProjection | None = None
    reviewed_context: ReviewedContext | None = None
    submitted_reviews: list[SubmittedReview] = Field(default_factory=list)


def build_claims_router(claims, auth_store, *, tags=None, assurance=None, clock=time.time):
    router = APIRouter(
        responses=_ERROR_RESPONSES,
        dependencies=[
            Security(
                APIKeyCookie(
                    name=SESSION_COOKIE_NAME, scheme_name="sessionCookie", auto_error=False
                )
            )
        ],
    )
    contract = {
        "x-minimum-role": "viewer",
        "x-idempotency-required": False,
        "x-rate-limit": "120/min/user",
    }
    store = claims.store

    def load(request, run_id, operation):
        now = time.time()
        auth = _authorize(request, auth_store, now, "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store, user_sub=auth.user_sub, operation_id=operation, requests=120, now=now
        )
        if limited is not None:
            return limited
        return claims.load(auth.tenant_id, run_id)

    def rulepack_approved_by(tenant_id, run_id):
        # Best-effort, read-only projection of the run's pinned rulepack
        # approver so the UI can label AI-delegated review distinctly from a
        # human approval. Absence of a snapshot/field must not raise.
        try:
            return store.snapshot(tenant_id, run_id)["rulepack"].get("approved_by")
        except (ValueError, KeyError, sqlite3.DatabaseError):
            return None

    def review_projection(tenant_id, run_id, claim_id):
        # Read-only projection of a blocked/in-progress tagging attempt from
        # LocalTagStore.load_snapshot(...)["claims"][i]. A tag stage that
        # never started (no "tag_job" on the run) has nothing to project, so
        # that specific KeyError degrades to None; any other integrity
        # failure (checkpoint hash/pin mismatch) is a real error and must
        # propagate to the caller's failure() handling, not be hidden as an
        # absent projection.
        if tags is None:
            return None
        try:
            envelope = tags.load_snapshot(tenant_id, run_id)
        except KeyError as exc:
            if exc.args == ("tag_job",):
                return None
            raise
        item = next((c for c in envelope["claims"] if c["claim_id"] == claim_id), None)
        if item is None:
            return None
        blocked_reason = item.get("reason")
        has_projection_fields = (
            "original_packet" in item
            or "preliminary_agreement" in item
            or "raw_candidate_review" in item
        )
        if blocked_reason is None and not has_projection_fields:
            # Older record predating this projection: nothing additive to show.
            return None
        original_packet = item.get("original_packet") or {}
        snippets = [
            ref.get("quote", "")
            for candidate in original_packet.get("evidence_candidates", [])
            for ref in candidate.get("source_refs", [])
            if ref.get("quote")
        ]
        agreement = item.get("preliminary_agreement") or {}
        agreements = [
            dict(
                field_id=field_id,
                status=entry["state"],
                replicate_values=entry["replicate_values"],
            )
            for group in ("fields", "dimensions")
            for field_id, entry in (agreement.get(group) or {}).items()
            if isinstance(entry, dict)
            and entry.get("state") in ("agreed", "conflict", "unresolved")
        ]
        raw_review = item.get("raw_candidate_review")
        raw_candidates = []
        if raw_review is not None:
            if not isinstance(raw_review, dict) or raw_review.get("schema_version") != 1:
                raise RunRejected("ARTIFACT_UNAVAILABLE", 409)
            raw_candidates_list = raw_review.get("candidates")
            if not isinstance(raw_candidates_list, list):
                raise RunRejected("ARTIFACT_UNAVAILABLE", 409)
            for cand in raw_candidates_list:
                if not isinstance(cand, dict):
                    continue
                cand_status = cand.get("status")
                # Candidates never carry accepted or verified labels.
                if cand_status in ("accepted", "verified") or cand_status not in (
                    "candidate",
                    "unverified",
                    "unconfirmed",
                ):
                    continue
                sref = cand.get("source_ref")
                if not isinstance(sref, dict):
                    continue
                try:
                    ref = SourceRef.model_validate_json(json.dumps(sref))
                except ValueError:
                    continue
                if ref.location_quality != "located" or ref.bbox is None:
                    continue
                raw_candidates.append(
                    dict(
                        source_ref=sref,
                        status=cand_status,
                        reason=cand.get("reason"),
                    )
                )
        return dict(
            schema_version=1,
            candidate_snippets=snippets,
            blocked_reason=blocked_reason,
            blocked_action=(
                _preliminary_unresolved_action(item.get("preliminary_agreement"))
                if blocked_reason == "PRELIMINARY_TAGS_UNRESOLVED"
                else _BLOCKED_ACTION_TEXT.get(blocked_reason)
            ),
            field_agreements=agreements,
            raw_candidates=raw_candidates,
        )

    def failure(exc):
        if isinstance(exc, InvalidCatalogCursor):
            return _error_response(
                400, "INVALID_CURSOR", "Pagination cursor is invalid or expired."
            )
        if isinstance(exc, CatalogCapacityExceeded):
            return _error_response(
                503, "CATALOG_CAPACITY_EXCEEDED", "Snapshot capacity is temporarily exhausted."
            )
        if isinstance(exc, RunRejected):
            return _error_response(exc.status, exc.code, "Claim request could not be processed.")
        return _error_response(
            409, "ARTIFACT_UNAVAILABLE", "Claims are pending or failed integrity checks."
        )

    @router.get(
        "/v1/runs/{run_id}/claims",
        response_model=ClaimSummaryPage,
        operation_id="claims_list",
        openapi_extra=contract,
    )
    def listed(
        request: Request,
        run_id: UUID,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        track: Track | None = None,
        grade: Grade | None = None,
        review_status: ReviewStatus | None = None,
    ):
        try:
            discovery = load(request, str(run_id), "claims_list")
            if isinstance(discovery, JSONResponse):
                return discovery
            if tags is not None:
                return JSONResponse(
                    claims.page(
                        discovery,
                        str(run_id),
                        cursor=cursor,
                        limit=limit,
                        now=int(clock()),
                        track=track,
                        grade=grade,
                        review_status=review_status,
                    ),
                    headers={"Cache-Control": "no-store"},
                )
            tenant = discovery.scope.tenant_id
            digest, now = canonical_hash(asdict(discovery)), int(clock())
            filters = [track, grade, review_status]
            page = (
                store._decode_cursor(cursor, tenant, str(run_id), limit, now, endpoint="claims")
                if cursor
                else dict(
                    scope=[tenant, str(run_id), "claims", limit],
                    expires=now + 900,
                    after=0,
                    snapshot=digest,
                    filters=filters,
                    epoch=store.get(tenant, str(run_id))["mutation_epoch"],
                )
            )
            if page["snapshot"] != digest or page["filters"] != filters:
                raise RunRejected("INVALID_CURSOR", 400)
            items = [
                claim.to_summary()
                for claim in sorted(
                    discovery.claims, key=lambda c: (c.source_refs[0].page_num, c.claim_id)
                )
            ]
            items = [
                item
                for item in items
                if (track is None or item["track"] == track)
                and (grade is None or (item["decision"] or {}).get("evidence_grade") == grade)
                and (
                    review_status is None
                    or (item["decision"] or {}).get("review_status") == review_status
                )
            ]
            after = page["after"] + limit
            return JSONResponse(
                dict(
                    items=items[page["after"] : after],
                    next_cursor=store._encode_cursor(dict(page, after=after))
                    if after < len(items)
                    else None,
                    snapshot_epoch=page["epoch"],
                ),
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, KeyError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.get(
        "/v1/runs/{run_id}/claims/{claim_id}",
        operation_id="claim_get",
        openapi_extra=contract,
        response_model=ClaimDetail,
    )
    def detail(request: Request, run_id: UUID, claim_id: UUID):
        try:
            discovery = load(request, str(run_id), "claim_get")
            if isinstance(discovery, JSONResponse):
                return discovery
            claim = next((c for c in discovery.claims if c.claim_id == str(claim_id)), None)
            if claim is None:
                raise RunRejected("RESOURCE_NOT_FOUND", 404)
            current = claims.current_tag(claim.tenant_id, str(run_id), claim.claim_id)
            # Replay the run's published source-validated assurance opinion (or
            # None) so detail and the analysis list feed the identical matcher.
            # Claim scope comes only from validated preliminary dimensions
            # loaded via LocalTagStore.load_inputs through the shared helper;
            # missing/invalid context degrades to None/() (undetermined).
            statement = (
                assurance.load(claim.tenant_id, str(run_id)) if assurance is not None else None
            )

            # Reuse one verified read; only untagged context is best-effort.
            try:
                review_inputs = (
                    tags.load_inputs(claim.tenant_id, str(run_id), claim.claim_id)
                    if tags is not None
                    else None
                )
            except Exception:
                if current is not None and tags is not None:
                    raise
                review_inputs = None

            def _assurance_context():
                try:
                    return claim_context_from_review_inputs(
                        review_inputs,
                        tenant_id=claim.tenant_id,
                        document_version_id=claim.document_version_id,
                        claim_id=claim.claim_id,
                    )
                except Exception:
                    return AssuranceContext(
                        claim.tenant_id,
                        claim.document_version_id,
                        claim.claim_id,
                        None,
                        None,
                        (),
                        (),
                    )

            match = match_assurance(statement, _assurance_context())
            if current is None:
                # Extraction-only review: tagging not yet published. Return the
                # original extraction refs without inventing tag artifacts
                # (no elements, replicate ids, or tag packet) and without an
                # ETag, so no tag edits can be conditioned until a tag exists.
                # Assurance reflects the run's published statement when present;
                # otherwise the matcher preserves the absence as undetermined.
                summary = claims.summary(claim, None)
                body = dict(
                    claim=summary,
                    source_refs=[asdict(ref) for ref in claim.source_refs],
                    elements=[],
                    submitted_reviews=claims.submitted_reviews(
                        claim.tenant_id, str(run_id), claim.claim_id
                    ),
                    assurance=match.to_dict(),
                    replicate_request_ids=[],
                    packet_sha256=None,
                    suggestion=None,
                    basis_refs=[],
                    tag_status="untagged",
                    rulepack_approved_by=rulepack_approved_by(claim.tenant_id, str(run_id)),
                    review_projection=review_projection(
                        claim.tenant_id, str(run_id), claim.claim_id
                    ),
                )
                return JSONResponse(
                    ClaimDetail.model_validate_json(json.dumps(body)).model_dump(mode="json"),
                    headers={"Cache-Control": "no-store"},
                )
            if tags is None:
                raise RunRejected("TAGGING_NOT_PUBLISHED")
            assert review_inputs is not None
            inputs = review_inputs
            tag = current["tag"]
            summary = claims.summary(claim, current)
            # Assurance reflects the run's published statement when present;
            # otherwise the matcher preserves the absence as undetermined.
            body = dict(
                claim=summary,
                source_refs=[asdict(ref) for ref in claim.source_refs],
                elements=[
                    next(
                        (
                            element
                            for element in tag["elements"]
                            if element["element_id"] == element_id
                        ),
                        dict(
                            element_id=element_id,
                            state="unknown",
                            evidence_refs=[],
                            normalized_value=None,
                            credited_from=None,
                            reason_code=None,
                        ),
                    )
                    for element_id in MAPPINGS[summary["track"]]
                ]
                if summary["track"]
                else tag["elements"],
                submitted_reviews=claims.submitted_reviews(
                    claim.tenant_id, str(run_id), claim.claim_id
                ),
                reviewed_context=(tag.get("claim_context_review") or {}).get("projection"),
                assurance=match.to_dict(),
                replicate_request_ids=[run.request.request_id for run in inputs.tag_runs],
                packet_sha256=inputs.packet.packet_sha256,
                suggestion=None,
                basis_refs=[],
                tag_status="tagged",
                rulepack_approved_by=rulepack_approved_by(claim.tenant_id, str(run_id)),
                review_projection=review_projection(claim.tenant_id, str(run_id), claim.claim_id),
            )
            return JSONResponse(
                ClaimDetail.model_validate_json(json.dumps(body)).model_dump(mode="json"),
                headers={"Cache-Control": "no-store", "ETag": f'"{tag["tag_revision"]}"'},
            )
        except (ValueError, KeyError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    return router
