"""Fixed v1 run lifecycle HTTP boundary, mounted only by local synthetic composition."""

from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.application.runs import RunRejected, RunService
from proofops.domain.errors import DomainValidationError
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    AuthStore,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.dto import RunCreate
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.request_limits import READ_REQUESTS_PER_MINUTE, WRITE_REQUESTS_PER_MINUTE
from proofops_api.routers.documents import StrictDTO, _request_schema
from proofops_api.routers.registry import _authorize
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool


class Coverage(StrictDTO):
    pages_total: Annotated[int, Field(ge=0)]
    pages_processed: Annotated[int, Field(ge=0)]
    pages_unreadable: Annotated[int, Field(ge=0)]
    pages_unprocessed: Annotated[int, Field(ge=0)]
    chunks_discovered: Annotated[int, Field(ge=0)]
    chunks_processed: Annotated[int, Field(ge=0)]
    claims_discovered: Annotated[int, Field(ge=0)]
    claims_decided: Annotated[int, Field(ge=0)]
    claims_needs_review: Annotated[int, Field(ge=0)]
    full_scope: bool
    complete: bool


class Run(StrictDTO):
    run_id: UUID
    document_version_id: UUID
    status: Literal["queued", "running", "partial", "completed", "failed", "cancelled"]
    current_stage: str = "parse"
    revision: Annotated[int, Field(ge=1)]
    mutation_epoch: Annotated[int, Field(ge=0)]
    coverage: Coverage
    rule_pack_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    created_at: datetime


class RunPage(StrictDTO):
    items: list[Run]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


class ActionReason(StrictDTO):
    reason: Annotated[str, Field(min_length=5, max_length=1000)]


class Cost(StrictDTO):
    run_id: UUID
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    attempt_count: Annotated[int, Field(ge=0)]
    cache_hit_count: Annotated[int, Field(ge=0)]
    amount: str | None
    currency: Literal["USD"]
    pricing_snapshot_id: str | None
    cost_status: Literal["known", "partial", "unknown_cost"]


class AuditEvent(StrictDTO):
    event_id: UUID
    sequence: Annotated[int, Field(ge=1)]
    action: str
    actor_display: str
    target_id: UUID
    before_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None
    after_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    event_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")]
    previous_event_hash: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None
    created_at: datetime


class AuditEventPage(StrictDTO):
    items: list[AuditEvent]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


def build_runs_router(
    service: RunService,
    auth_store: AuthStore,
    *,
    allowed_origin: str,
    app_env: str = "local",
    model_adapter: str = "synthetic",
    clock: Any = None,
) -> APIRouter:
    if app_env != "local" or model_adapter != "synthetic" or not service.local_synthetic:
        raise ValueError("run lifecycle requires local-synthetic composition")
    now_fn = clock if clock is not None else time.time
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

    def authorize(request, operation_id, write=False):
        now = now_fn()
        auth = _authorize(request, auth_store, now, "editor" if write else "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        if write:
            session = auth_store.sessions.get(auth.session_id)
            if session is None or not _verify_csrf(
                request, session.csrf_hash, allowed_origin=allowed_origin
            ):
                return _error_response(403, "CSRF_INVALID", "Invalid session or request origin.")
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id=operation_id,
            requests=WRITE_REQUESTS_PER_MINUTE if write else READ_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        return auth

    def failure(exc):
        if isinstance(exc, RunRejected):
            response = _error_response(exc.status, exc.code, "Run request could not be processed.")
            if exc.status == 429:
                response.headers["Retry-After"] = "1"
            return response
        if isinstance(exc, ValueError) and not isinstance(exc, DomainValidationError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid run request.")
        return _error_response(409, "RUN_CONFLICT", "Run transaction could not be committed.")

    async def payload(request, model: type[BaseModel]):
        if not 16 <= len(request.headers.get("Idempotency-Key", "")) <= 128:
            raise RunRejected("IDEMPOTENCY_KEY_INVALID", 400)
        try:
            data = await read_bounded_json(request)
        except (RequestBodyTooLarge, ValueError):
            raise RunRejected("VALIDATION_ERROR", 422) from None
        return model.model_validate(data).model_dump(mode="json", exclude_unset=True)

    @router.post(
        "/v1/runs",
        status_code=202,
        response_model=Run,
        operation_id="run_create",
        openapi_extra=_request_schema(RunCreate),
    )
    async def create_run(request: Request):
        auth = authorize(request, "run_create", True)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            body = await payload(request, RunCreate)
            result = await run_in_threadpool(
                service.create, auth, body, request.headers["Idempotency-Key"]
            )
            return JSONResponse(
                result, status_code=202, headers={"ETag": f'"{result["revision"]}"'}
            )
        except (ValueError, DomainValidationError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    read_contract = {
        "x-minimum-role": "viewer",
        "x-idempotency-required": False,
        "x-rate-limit": "120/min/user",
    }

    @router.get(
        "/v1/runs", response_model=RunPage, operation_id="runs_list", openapi_extra=read_contract
    )
    def list_runs(
        request: Request,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        auth = authorize(request, "runs_list")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(
                service.list(auth.tenant_id, cursor=cursor, limit=limit),
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, DomainValidationError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.get(
        "/v1/runs/{run_id}", response_model=Run, operation_id="run_get", openapi_extra=read_contract
    )
    def get_run(request: Request, run_id: UUID):
        auth = authorize(request, "run_get")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            result = service.get(auth.tenant_id, str(run_id))
            return JSONResponse(result, headers={"ETag": f'"{result["revision"]}"'})
        except (ValueError, DomainValidationError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    async def mutate(request, run_id, action):
        auth = authorize(request, f"run_{action}", True)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            body = await payload(request, ActionReason)
            match = re.fullmatch(r'"([1-9][0-9]*)"', request.headers.get("If-Match", ""))
            if match is None or len(match[1]) > 18:
                raise RunRejected("VALIDATION_ERROR", 422)
            result = await run_in_threadpool(
                service.action,
                auth,
                str(run_id),
                action,
                expected_revision=int(match[1]),
                key=request.headers["Idempotency-Key"],
                reason=body["reason"],
            )
            return JSONResponse(
                result, status_code=202, headers={"ETag": f'"{result["revision"]}"'}
            )
        except (ValueError, DomainValidationError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    action_contract = _request_schema(ActionReason)
    action_contract["parameters"].append(
        {"name": "If-Match", "in": "header", "required": True, "schema": {"type": "string"}}
    )

    @router.post(
        "/v1/runs/{run_id}/cancel",
        status_code=202,
        response_model=Run,
        operation_id="run_cancel",
        openapi_extra=action_contract,
    )
    async def cancel(request: Request, run_id: UUID):
        return await mutate(request, run_id, "cancel")

    @router.post(
        "/v1/runs/{run_id}/retry",
        status_code=202,
        response_model=Run,
        operation_id="run_retry",
        openapi_extra=action_contract,
    )
    async def retry(request: Request, run_id: UUID):
        return await mutate(request, run_id, "retry")

    @router.get(
        "/v1/runs/{run_id}/cost",
        response_model=Cost,
        operation_id="cost_get",
        openapi_extra=read_contract,
    )
    def cost(request: Request, run_id: UUID):
        auth = authorize(request, "cost_get")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(service.cost(auth.tenant_id, str(run_id)))
        except (ValueError, DomainValidationError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.get(
        "/v1/runs/{run_id}/audit",
        response_model=AuditEventPage,
        operation_id="audit_get",
        openapi_extra=read_contract,
    )
    def audit(
        request: Request,
        run_id: UUID,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        auth = authorize(request, "audit_get")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(
                service.audit(auth.tenant_id, str(run_id), cursor=cursor, limit=limit)
            )
        except (ValueError, DomainValidationError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    return router
