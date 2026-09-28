"""Comparison HTTP boundary over immutable local artifacts."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.comparison_store import ComparisonRejected
from proofops.domain.errors import DomainValidationError
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.routers.documents import JobAccepted, StrictDTO, _request_schema
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field, ValidationError
from starlette.concurrency import run_in_threadpool


class ComparisonCreate(StrictDTO):
    prior_document_version_id: UUID


class ComparisonChange(StrictDTO):
    current_claim_id: UUID | None
    prior_claim_id: UUID | None
    type: Literal["modified", "removed_candidate", "new", "ambiguous"]
    reason: Annotated[str, Field(min_length=1)]


class Comparison(StrictDTO):
    comparison_id: UUID
    status: Literal["queued", "completed", "not_run", "failed"]
    reason: str | None
    changes: list[ComparisonChange]


def build_comparisons_router(store, auth_store, *, allowed_origin: str, clock=time.time):
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

    def failure(error: Exception):
        if isinstance(error, ComparisonRejected):
            return _error_response(
                error.status, error.code, "Comparison request could not be processed."
            )
        if isinstance(error, RequestBodyTooLarge):
            return _error_response(413, "PAYLOAD_TOO_LARGE", "JSON body exceeds size limit.")
        if isinstance(error, ValidationError | DomainValidationError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid comparison request.")
        return _error_response(
            409, "COMPARISON_CONFLICT", "Comparison transaction could not be committed."
        )

    @router.post(
        "/v1/runs/{run_id}/comparisons",
        operation_id="comparison_create",
        status_code=202,
        response_model=JobAccepted,
        openapi_extra=_request_schema(ComparisonCreate),
    )
    async def create(request: Request, run_id: UUID):
        auth = authorize(request, "comparison_create", "editor", 10)
        if isinstance(auth, JSONResponse):
            return auth
        session = auth_store.sessions.get(auth.session_id)
        if session is None or not _verify_csrf(
            request, session.csrf_hash, allowed_origin=allowed_origin
        ):
            return _error_response(403, "CSRF_INVALID", "Invalid session or request origin.")
        try:
            body = ComparisonCreate.model_validate_json(
                json.dumps(await read_bounded_json(request))
            ).model_dump(mode="json")
            result = await run_in_threadpool(
                store.create,
                auth,
                str(run_id),
                body,
                request.headers.get("Idempotency-Key"),
            )
            return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})
        except (
            ComparisonRejected,
            RequestBodyTooLarge,
            ValidationError,
            DomainValidationError,
            sqlite3.DatabaseError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return failure(error)

    @router.get(
        "/v1/comparisons/{comparison_id}",
        operation_id="comparison_get",
        response_model=Comparison,
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def get(request: Request, comparison_id: UUID):
        auth = authorize(request, "comparison_get", "viewer", 120)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            result = store.get(auth, str(comparison_id))
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (
            ComparisonRejected,
            DomainValidationError,
            sqlite3.DatabaseError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            return failure(error)

    return router
