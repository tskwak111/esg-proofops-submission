"""Reconciliation product HTTP boundary over immutable local case snapshots.

Registration is deliberately absent: a case enters the store through the local
operator importer, never through this router. Reviewers confirm the stored
bindings, admins approve the bound policy, and evaluation recomputes from
server-owned snapshots. A blocked result is a successful response.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyCookie
from proofops.adapters.local.reconciliation_store import ReconciliationRejected
from proofops.domain.errors import DomainValidationError
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.routers.documents import StrictDTO, _request_schema
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool

ITEMS = Literal["C1", "C2", "C3", "C4"]
SHA256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
UUID_STR = Annotated[
    str,
    Field(pattern=r"^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$"),
]
REASON = Annotated[str, Field(min_length=1, max_length=2000)]
IF_MATCH = {
    "name": "If-Match",
    "in": "header",
    "required": True,
    "schema": {"type": "string", "pattern": '^"[1-9][0-9]*"$'},
}
NO_STORE = {"Cache-Control": "no-store"}


class ReviewConfirm(StrictDTO):
    """A reviewer confirms the stored bindings; it never chooses an outcome."""

    reason: REASON
    confirm_source_bindings: Literal[True]
    confirm_decision_bindings: Literal[True]
    confirm_search_coverage: bool


class PolicyApproval(StrictDTO):
    approved: bool
    reason: REASON


class Evaluate(StrictDTO):
    """Evaluation takes no input: the server owns every snapshot it reads."""


class SourceRef(StrictDTO):
    source_id: str
    document_id: str
    artifact_sha256: SHA256
    locator: str
    quote: str
    format: Literal["text", "xml", "html", "pdf"]
    byte_size: int
    binding_state: Literal["draft", "confirmed"]
    document_role: Literal["sustainability", "financial"] | None


class ReconciliationResult(StrictDTO):
    """Strict 1.1 result. `status` is meaningful only once completed."""

    schema_version: Literal["1.1"]
    claim_id: str
    item: ITEMS
    execution_state: Literal["completed", "blocked", "not_run"]
    status: Literal["matched", "needs_explanation", "not_applicable"] | None
    review_required: bool
    reason_codes: list[str]
    source_ids: list[str]
    explanation_source_id: str | None
    sustainability_value: str | None
    financial_value: str | None
    packet_sha256: SHA256
    policy_sha256: SHA256
    synthetic: bool
    engine_version: str


class LatestResult(StrictDTO):
    revision: int
    result: ReconciliationResult
    projection: dict[str, Any]
    created_at: str


class CaseDetail(StrictDTO):
    case_id: UUID_STR
    run_id: UUID_STR
    claim_id: UUID_STR
    item: ITEMS
    revision: int
    synthetic: bool
    review_state: Literal["pending", "reviewed"]
    policy_approved: bool
    packet: dict[str, Any]
    policy: dict[str, Any]
    sources: list[SourceRef]
    latest_result: LatestResult | None
    provenance: dict[str, Any]
    coverage_confirmed: bool
    policy_approval: dict[str, Any] | None


class CaseList(StrictDTO):
    items: list[CaseDetail]


class RevisionSnapshot(StrictDTO):
    case_id: UUID_STR
    revision: int
    kind: Literal["registration", "review", "policy_approval", "evaluation"]
    actor: str
    created_at: str
    snapshot_sha256: SHA256
    provenance: dict[str, Any] | None
    event: dict[str, Any] | None
    result: ReconciliationResult | None
    projection: dict[str, Any] | None


def build_reconciliation_router(store, auth_store, *, allowed_origin: str, clock=time.time):
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

    def authorize(request: Request, operation: str, role: str, requests: int):
        now = clock()
        auth = _authorize(request, auth_store, now, role)
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id=operation,
            requests=requests,
            now=now,
        )
        return limited or auth

    def guard_csrf(request: Request, auth):
        session = auth_store.sessions.get(auth.session_id)
        if session is None or not _verify_csrf(
            request, session.csrf_hash, allowed_origin=allowed_origin
        ):
            return _error_response(403, "CSRF_INVALID", "Invalid session or request origin.")
        return None

    def failure(error: Exception):
        if isinstance(error, ReconciliationRejected):
            return _error_response(
                error.status, error.code, "Reconciliation request could not be processed."
            )
        if isinstance(error, RequestBodyTooLarge):
            return _error_response(413, "PAYLOAD_TOO_LARGE", "JSON body exceeds size limit.")
        if isinstance(error, ValidationError | DomainValidationError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid reconciliation request.")
        return _error_response(
            409, "RECONCILIATION_CONFLICT", "Reconciliation state could not be committed."
        )

    HANDLED = (
        ReconciliationRejected,
        RequestBodyTooLarge,
        ValidationError,
        DomainValidationError,
        sqlite3.DatabaseError,
        KeyError,
        TypeError,
        ValueError,
    )

    def case_response(detail: dict[str, Any]) -> JSONResponse:
        """Validate the stored projection before it leaves the process."""
        body = CaseDetail.model_validate(detail).model_dump(mode="json")
        return JSONResponse(body, headers={"ETag": f'"{detail["revision"]}"', **NO_STORE})

    async def mutate(request: Request, operation, role, model, call, case_id):
        auth = authorize(request, operation, role, 30)
        if isinstance(auth, JSONResponse):
            return auth
        refused = guard_csrf(request, auth)
        if refused is not None:
            return refused
        try:
            body = model.model_validate_json(json.dumps(await read_bounded_json(request)))
            detail = await run_in_threadpool(
                call,
                auth,
                str(case_id),
                body.model_dump(mode="json"),
                request.headers.get("If-Match"),
                request.headers.get("Idempotency-Key"),
            )
            return case_response(detail)
        except HANDLED as error:
            return failure(error)

    @router.get(
        "/v1/runs/{run_id}/claims/{claim_id}/reconciliation",
        operation_id="reconciliation_list",
        response_model=CaseList,
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def list_cases(request: Request, run_id: UUID, claim_id: UUID):
        auth = authorize(request, "reconciliation_list", "viewer", 120)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            listed = store.list_for_claim(auth, str(run_id), str(claim_id))
            body = CaseList.model_validate(listed).model_dump(mode="json")
            return JSONResponse(body, headers=dict(NO_STORE))
        except HANDLED as error:
            return failure(error)

    @router.get(
        "/v1/reconciliation/cases/{case_id}",
        operation_id="reconciliation_case_get",
        response_model=CaseDetail,
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def get_case(request: Request, case_id: UUID):
        auth = authorize(request, "reconciliation_case_get", "viewer", 120)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return case_response(store.get_case(auth, str(case_id)))
        except HANDLED as error:
            return failure(error)

    review_schema = _request_schema(ReviewConfirm) | {"x-minimum-role": "reviewer"}
    review_schema["parameters"].append(dict(IF_MATCH))

    @router.post(
        "/v1/reconciliation/cases/{case_id}/review",
        operation_id="reconciliation_review",
        response_model=CaseDetail,
        openapi_extra=review_schema,
    )
    async def review(request: Request, case_id: UUID):
        return await mutate(
            request, "reconciliation_review", "reviewer", ReviewConfirm, store.review, case_id
        )

    approval_schema = _request_schema(PolicyApproval) | {"x-minimum-role": "admin"}
    approval_schema["parameters"].append(dict(IF_MATCH))

    @router.post(
        "/v1/reconciliation/cases/{case_id}/policy-approval",
        operation_id="reconciliation_policy_approval",
        response_model=CaseDetail,
        openapi_extra=approval_schema,
    )
    async def approve_policy(request: Request, case_id: UUID):
        return await mutate(
            request,
            "reconciliation_policy_approval",
            "admin",
            PolicyApproval,
            store.approve_policy,
            case_id,
        )

    evaluate_schema = _request_schema(Evaluate) | {"x-minimum-role": "editor"}
    evaluate_schema["parameters"].append(dict(IF_MATCH))

    @router.post(
        "/v1/reconciliation/cases/{case_id}/evaluate",
        operation_id="reconciliation_evaluate",
        response_model=CaseDetail,
        openapi_extra=evaluate_schema,
    )
    async def evaluate(request: Request, case_id: UUID):
        return await mutate(
            request, "reconciliation_evaluate", "editor", Evaluate, store.evaluate, case_id
        )

    @router.get(
        "/v1/reconciliation/cases/{case_id}/revisions/{revision}",
        operation_id="reconciliation_revision_get",
        response_model=RevisionSnapshot,
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def get_revision(request: Request, case_id: UUID, revision: Annotated[int, Field(ge=1)]):
        auth = authorize(request, "reconciliation_revision_get", "viewer", 120)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            snapshot = store.revision(auth, str(case_id), revision)
            body = RevisionSnapshot.model_validate(snapshot).model_dump(mode="json")
            return JSONResponse(body, headers=dict(NO_STORE))
        except HANDLED as error:
            return failure(error)

    @router.get(
        "/v1/reconciliation/cases/{case_id}/sources/{source_id}/content",
        operation_id="reconciliation_source_content",
        response_class=Response,
        responses={200: {"content": {"application/octet-stream": {}}}},
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "60/min/user",
        },
    )
    def source_content(
        request: Request,
        case_id: UUID,
        source_id: Annotated[str, Field(min_length=1, max_length=200)],
    ):
        auth = authorize(request, "reconciliation_source_content", "viewer", 60)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            payload, filename, digest = store.source_content(auth, str(case_id), source_id)
        except HANDLED as error:
            return failure(error)
        # Always an inert download: originals are never rendered as active documents.
        safe = "".join(
            character
            for character in filename
            if character.isascii() and (character.isalnum() or character in "._-")
        )
        return Response(
            content=payload,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="{safe or "source.bin"}"',
                "X-Content-Type-Options": "nosniff",
                "X-Content-SHA256": digest,
                **NO_STORE,
            },
        )

    return router
