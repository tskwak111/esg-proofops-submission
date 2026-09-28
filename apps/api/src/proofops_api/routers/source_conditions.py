"""Reviewer-only source facts and ownership assessments; no grade approval."""

import json
import sqlite3
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyCookie
from proofops.application.reviews import ReviewRejected
from proofops.application.runs import RunRejected
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.routers.analysis import Observation
from proofops_api.routers.documents import StrictDTO
from proofops_api.routers.sources import QualityIssue, SourceRef
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field
from starlette.concurrency import run_in_threadpool

Digest = Annotated[str, Field(pattern="^[0-9a-f]{64}$")]


class CanonicalFragment(StrictDTO):
    source_id: UUID
    parser_run_id: UUID
    source_native_id: str
    char_start: Annotated[int, Field(ge=0)]
    char_end: Annotated[int, Field(ge=1)]
    raw_text_sha256: Digest


class NativeFragment(StrictDTO):
    note_artifact_sha256: Digest
    packet_sha256: Digest
    physical_page: Annotated[int, Field(ge=1)]
    fragment_ids: list[str]
    native_word_indices: list[Annotated[int, Field(ge=0)]]


class SourceConditionFragment(StrictDTO):
    id: Digest
    fragment: CanonicalFragment | NativeFragment
    text: str
    bbox: tuple[float, float, float, float] | None
    physical_page: Annotated[int, Field(ge=1)]


class SourceConditionState(StrictDTO):
    classifications: dict[str, dict]
    citations: dict[str, dict]
    ownership: dict[str, dict]
    conditions: dict[str, dict]
    claim_bindings: dict[str, dict]


class SourceConditionRevision(StrictDTO):
    schema_: Literal["source_condition_review_v1"] = Field(alias="schema")
    tenant_id: UUID
    run_id: UUID
    document_version_id: UUID
    source_snapshot_sha256: Digest
    revision: Annotated[int, Field(ge=1)]
    parent_sha256: Digest | None
    actor_sub: str
    created_at: str
    reason: str | None = None
    state: SourceConditionState
    revision_sha256: Digest


class SourceConditionEnvelope(StrictDTO):
    review: SourceConditionRevision
    fragments: list[SourceConditionFragment]
    issues: list[QualityIssue]
    coverage_status: Literal["unknown"]


class SourceConditionDisplay(StrictDTO):
    review_revision: Annotated[int, Field(ge=1)]
    review_sha256: Digest
    source_snapshot_sha256: Digest
    fragment_id: Digest
    display: dict
    image_url: str


class SourceConditionPublish(StrictDTO):
    pass


class SourceCitation(StrictDTO):
    id: Digest
    fragment: CanonicalFragment | NativeFragment
    state: Literal["confirmed", "unknown", "conflict", "unreadable"]
    source_view_receipt: SourceConditionDisplay | None


class SourceClassification(StrictDTO):
    id: Digest
    fragment: CanonicalFragment | NativeFragment
    state: Literal["note", "not_note", "unknown", "conflict"]
    source_view_receipt: SourceConditionDisplay | None
    reason: Annotated[str, Field(min_length=5, max_length=1000)]


class SourceOwnershipTarget(StrictDTO):
    source_id: UUID
    table_id: UUID
    row: Annotated[int, Field(ge=0)] | None
    column: Annotated[int, Field(ge=0)] | None
    row_span: Annotated[int, Field(ge=1)] | None
    column_span: Annotated[int, Field(ge=1)] | None


class SourceOwnership(StrictDTO):
    id: Digest
    fragment_id: Digest
    targets: Annotated[list[SourceOwnershipTarget], Field(max_length=16)]
    state: Literal["linked", "unknown", "conflict"]
    evidence_refs: Annotated[list[SourceRef], Field(max_length=32)]


class SourceCondition(StrictDTO):
    id: Digest
    fragment_id: Digest
    ownership_id: Digest
    kind: Literal["unit_literal", "scope_literal", "unsupported_prose"]
    state: Literal["tagged", "unknown", "conflict", "unsupported"]
    value_refs: Annotated[list[SourceRef], Field(max_length=32)]


class SourceAnnotationWrite(StrictDTO):
    schema_version: Literal[1]
    base_source_revision: Annotated[int, Field(ge=1)]
    source_snapshot_sha256: Digest
    classifications: Annotated[list[SourceClassification], Field(max_length=16)]
    citations: Annotated[list[SourceCitation], Field(max_length=16)]
    ownership: Annotated[list[SourceOwnership], Field(max_length=16)]
    conditions: Annotated[list[SourceCondition], Field(max_length=16)]
    claim_bindings: Annotated[list[dict], Field(max_length=0)]
    reason: Annotated[str, Field(min_length=5, max_length=1000)]


class SourceAnnotationResult(StrictDTO):
    review: SourceConditionRevision
    numeric_receipts: Annotated[list[dict], Field(max_length=0)]
    coverage_status: Literal["unknown"]


class SourceObservationPreviewRequest(StrictDTO):
    revision: Annotated[int, Field(ge=1)]
    table_id: UUID
    bindings: Annotated[
        list[
            dict[
                Literal[
                    "metric_raw",
                    "scope",
                    "subject",
                    "reporting_period",
                    "scope2_basis",
                    "organizational_boundary",
                    "unit_raw",
                    "value_raw",
                    "denominator",
                    "baseline_period",
                    "category",
                    "method",
                ],
                UUID,
            ]
        ],
        Field(min_length=1, max_length=16),
    ]


class ObservationSourceHolds(StrictDTO):
    observation_id: UUID
    issue_ids: list[UUID]
    note_source_ids: list[UUID]
    reasons: list[str]


class SourceObservationPreviewResponse(StrictDTO):
    review_revision: Annotated[int, Field(ge=1)]
    review_sha256: Digest
    source_snapshot_sha256: Digest
    normalization_sha256: Digest
    request: SourceObservationPreviewRequest
    observations: list[Observation]
    source_holds: list[ObservationSourceHolds]
    issues: list[QualityIssue]
    status: Literal["proposal_only"]
    coverage_status: Literal["unknown"]
    preview_sha256: Digest


def build_source_conditions_router(service, auth_store, *, allowed_origin, clock=time.time):
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
    read_contract = {
        "x-minimum-role": "reviewer",
        "x-idempotency-required": False,
        "x-rate-limit": "120/min/user",
    }
    path = "/v1/runs/{run_id}/source-condition-review"

    def authorize(request, operation, write=False):
        now = clock()
        actor = _authorize(request, auth_store, now, "reviewer")
        if isinstance(actor, JSONResponse):
            return actor
        if write:
            session = auth_store.sessions.get(actor.session_id)
            if session is None or not _verify_csrf(
                request, session.csrf_hash, allowed_origin=allowed_origin
            ):
                return _error_response(403, "CSRF_INVALID", "Invalid session or request origin.")
        limited = _request_limit_response(
            auth_store,
            user_sub=actor.user_sub,
            operation_id=operation,
            requests=10 if write else 120,
            now=now,
        )
        return limited if limited is not None else actor

    def failure(error):
        if isinstance(error, ReviewRejected | RunRejected):
            return _error_response(error.status, error.code, "Source review request rejected.")
        return _error_response(
            409,
            "SOURCE_REVIEW_INPUT_MISMATCH",
            "Source review artifacts are unavailable or inconsistent.",
        )

    def response(value):
        parsed = SourceConditionEnvelope.model_validate_json(json.dumps(value))
        return JSONResponse(
            parsed.model_dump(mode="json", by_alias=True, exclude_unset=True),
            headers={"Cache-Control": "no-store", "ETag": f'"{value["review"]["revision"]}"'},
        )

    @router.post(
        path,
        operation_id="source_condition_publish",
        response_model=SourceConditionEnvelope,
        openapi_extra={
            **read_contract,
            "x-rate-limit": "10/min/user",
            "parameters": [
                {
                    "name": "X-CSRF-Token",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                }
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {"schema": SourceConditionPublish.model_json_schema()}
                },
            },
        },
    )
    async def publish(request: Request, run_id: UUID):
        actor = authorize(request, "source_condition_publish", True)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            body = await read_bounded_json(request)
        except RequestBodyTooLarge:
            return _error_response(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")
        except ValueError:
            return _error_response(422, "VALIDATION_ERROR", "Invalid JSON request.")
        if body != {}:
            return _error_response(422, "VALIDATION_ERROR", "An empty object is required.")
        try:
            return response(await run_in_threadpool(service.publish, actor, str(run_id)))
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as error:
            return failure(error)

    @router.get(
        path,
        operation_id="source_condition_get",
        response_model=SourceConditionEnvelope,
        openapi_extra=read_contract,
    )
    def get(request: Request, run_id: UUID, revision: Annotated[int | None, Query(ge=1)] = None):
        actor = authorize(request, "source_condition_get")
        if isinstance(actor, JSONResponse):
            return actor
        try:
            return response(service.get(actor, str(run_id), revision=revision))
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as error:
            return failure(error)

    @router.get(
        path + "/source-view",
        operation_id="source_condition_view",
        response_model=SourceConditionDisplay,
        responses={
            200: {"content": {"image/png": {"schema": {"type": "string", "format": "binary"}}}}
        },
        openapi_extra=read_contract,
    )
    def view(
        request: Request,
        run_id: UUID,
        revision: Annotated[int, Query(ge=1)],
        fragment_id: Annotated[str, Query(pattern="^[0-9a-f]{64}$")],
        format: Literal["receipt", "image"] = "receipt",
    ):
        actor = authorize(request, "source_condition_view")
        if isinstance(actor, JSONResponse):
            return actor
        try:
            receipt, png = service.view(
                actor, str(run_id), revision=revision, fragment_id=fragment_id
            )
            headers = {
                "Cache-Control": "no-store, private",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "ETag": f'"{revision}"',
            }
            if format == "image":
                return Response(png, media_type="image/png", headers=headers)
            parsed = SourceConditionDisplay.model_validate_json(json.dumps(receipt))
            return JSONResponse(parsed.model_dump(mode="json"), headers=headers)
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as error:
            return failure(error)

    from proofops_api.routers.documents import _request_schema

    preview_contract = _request_schema(SourceObservationPreviewRequest) | {
        "x-minimum-role": "reviewer",
        "x-idempotency-required": False,
    }
    preview_contract["parameters"] = [preview_contract["parameters"][0]]

    @router.post(
        path + "/observations",
        operation_id="source_observations_preview",
        response_model=SourceObservationPreviewResponse,
        openapi_extra=preview_contract,
    )
    async def preview_observations(request: Request, run_id: UUID):
        actor = authorize(request, "source_observations_preview", True)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            body = await read_bounded_json(request)
            preview_request = SourceObservationPreviewRequest.model_validate_json(json.dumps(body))
        except RequestBodyTooLarge:
            return _error_response(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")
        except (ValueError, TypeError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid observation preview request.")
        try:
            result = await run_in_threadpool(
                service.preview_observations,
                actor,
                str(run_id),
                preview_request.model_dump(mode="json"),
            )
            parsed = SourceObservationPreviewResponse.model_validate_json(json.dumps(result))
            return JSONResponse(
                parsed.model_dump(mode="json"), headers={"Cache-Control": "no-store"}
            )
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as error:
            return failure(error)

    write_contract = _request_schema(SourceAnnotationWrite) | {"x-minimum-role": "reviewer"}
    write_contract["parameters"].append(
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "pattern": '^"[1-9][0-9]*"$'},
        }
    )

    @router.post(
        path + "/revisions",
        operation_id="source_condition_resolve",
        response_model=SourceAnnotationResult,
        openapi_extra=write_contract,
    )
    async def resolve(request: Request, run_id: UUID):
        actor = authorize(request, "source_condition_resolve", True)
        if isinstance(actor, JSONResponse):
            return actor
        try:
            body = await read_bounded_json(request)
            SourceAnnotationWrite.model_validate_json(json.dumps(body))
        except RequestBodyTooLarge:
            return _error_response(413, "PAYLOAD_TOO_LARGE", "Request body is too large.")
        except (ValueError, TypeError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid source annotation.")
        try:
            result = await run_in_threadpool(
                service.resolve,
                actor,
                str(run_id),
                body,
                request.headers.get("If-Match"),
                request.headers.get("Idempotency-Key"),
            )
            parsed = SourceAnnotationResult.model_validate_json(json.dumps(result))
            return JSONResponse(
                parsed.model_dump(mode="json", by_alias=True, exclude_unset=True),
                headers={"Cache-Control": "no-store", "ETag": f'"{result["review"]["revision"]}"'},
            )
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as error:
            return failure(error)

    return router
