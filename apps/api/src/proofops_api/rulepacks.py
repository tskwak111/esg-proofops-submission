"""FastAPI boundary for RulePack activation."""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Collection
from datetime import date
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.rulepack_store import (
    IdempotencyConflict,
    RulePackNotFound,
    RulePackSqliteStore,
    StaleRulePackRevision,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    AuthStore,
    _authenticate,
    _error_body,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.request_limits import READ_REQUESTS_PER_MINUTE, WRITE_REQUESTS_PER_MINUTE

_SESSION_COOKIE = APIKeyCookie(
    name=SESSION_COOKIE_NAME,
    scheme_name="sessionCookie",
    auto_error=False,
)


class ActionReason(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    reason: Annotated[str, StringConstraints(min_length=5, max_length=1000)]


class RulePack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_pack_id: UUID
    version: str
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    status: Literal["draft", "validated", "active", "retired"]
    mode: Literal["disclosure", "advertising"]
    effective_date: date
    unresolved_gap_ids: list[str]


class RulePackPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    items: list[RulePack]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


class ErrorDetails(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    field: str | None = None
    reason: str | None = None
    current_revision: int | None = Field(default=None, ge=1)
    retry_after_seconds: int | None = Field(default=None, ge=0)


class ErrorValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    message: str
    request_id: UUID
    retryable: bool
    details: ErrorDetails = Field(default_factory=ErrorDetails)


class Error(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    error: ErrorValue


_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": Error, "description": "Invalid request"},
    401: {"model": Error, "description": "Unauthenticated"},
    403: {"model": Error, "description": "Forbidden or CSRF failure"},
    404: {"model": Error, "description": "Not found or tenant mismatch"},
    409: {"model": Error, "description": "Conflict / gate blocked"},
    412: {"model": Error, "description": "Stale revision"},
    422: {"model": Error, "description": "Schema or domain validation error"},
    429: {"model": Error, "description": "Rate/budget limit"},
    503: {"model": Error, "description": "Dependency unavailable"},
}


def build_rulepack_router(
    store: RulePackSqliteStore,
    auth_store: AuthStore,
    *,
    gap_ids: Collection[str],
    allowed_origin: str,
    clock: Any = None,
) -> APIRouter:
    router = APIRouter()
    now_fn = clock if clock is not None else time.time

    @router.get(
        "/v1/rule-packs",
        operation_id="rulepacks_list",
        response_model=RulePackPage,
        responses=_ERROR_RESPONSES,
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-rate-limit": "120/min/user",
            "x-idempotency-required": False,
        },
    )
    def listed(
        request: Request,
        _session_id: Annotated[str | None, Security(_SESSION_COOKIE)],
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> Response:
        now = now_fn()
        auth = _authenticate(request, auth_store, now=now)
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="rulepacks_list",
            requests=READ_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        try:
            body = RulePackPage.model_validate(
                store.list(auth.tenant_id, cursor=cursor, limit=limit, now=now)
            ).model_dump(mode="json")
        except ValueError:
            return _error_response(400, "INVALID_CURSOR", "invalid pagination cursor")
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @router.post(
        "/v1/rule-packs/{rule_pack_id}/activate",
        operation_id="rulepack_activate",
        response_model=RulePack,
        responses=_ERROR_RESPONSES,
        openapi_extra={
            "x-minimum-role": "admin",
            "x-rate-limit": "10/min/user",
            "x-idempotency-required": True,
            "parameters": [
                {
                    "name": "X-CSRF-Token",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string"},
                },
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 16, "maxLength": 128},
                },
                {
                    "name": "If-Match",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "pattern": r'^"[1-9][0-9]*"$'},
                },
            ],
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": ActionReason.model_json_schema()}},
            },
        },
    )
    async def activate(
        rule_pack_id: UUID,
        request: Request,
        _session_id: Annotated[str | None, Security(_SESSION_COOKIE)],
    ) -> Response:
        now = now_fn()
        auth = _authenticate(request, auth_store, now=now)
        if isinstance(auth, JSONResponse):
            return auth
        if not auth.has_capability("admin"):
            return _error_response(403, "FORBIDDEN", "admin capability required")
        session = auth_store.sessions.get(auth.session_id)
        if session is None:
            return _error_response(401, "SESSION_EXPIRED", "session is missing or expired")
        if not _verify_csrf(request, session.csrf_hash, allowed_origin=allowed_origin):
            return _error_response(403, "CSRF_INVALID", "missing/invalid CSRF token or origin")
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="rulepack_activate",
            requests=WRITE_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        idempotency_key = request.headers.get("Idempotency-Key")
        if idempotency_key is None or not 16 <= len(idempotency_key) <= 128:
            return _error_response(
                422,
                "VALIDATION_ERROR",
                "Idempotency-Key must contain 16 to 128 characters",
            )
        match = re.fullmatch(r'"([1-9][0-9]*)"', request.headers.get("If-Match", ""))
        if match is None:
            return _error_response(422, "VALIDATION_ERROR", "If-Match must be a quoted revision")
        try:
            payload = ActionReason.model_validate(await read_bounded_json(request))
        except (RequestBodyTooLarge, ValidationError, ValueError, UnicodeError):
            return _error_response(422, "VALIDATION_ERROR", "invalid activation request")

        try:
            result = store.activate(
                tenant_id=auth.tenant_id,
                rule_pack_id=str(rule_pack_id),
                expected_revision=int(match.group(1)),
                idempotency_key=idempotency_key,
                actor=auth.user_sub,
                reason=payload.reason,
                gap_ids=gap_ids,
                now=now,
            )
        except RulePackNotFound:
            return _error_response(404, "RESOURCE_NOT_FOUND", "rule pack not found")
        except IdempotencyConflict:
            return _error_response(409, "IDEMPOTENCY_CONFLICT", "idempotency key conflict")
        except StaleRulePackRevision as exc:
            body = _error_body(
                "STALE_RULEPACK_REVISION",
                "rule pack revision changed; reload before activating",
                str(uuid.uuid4()),
            )
            body["error"]["details"] = {"current_revision": exc.current_revision}
            return JSONResponse(
                status_code=412,
                content=body,
            )
        except ValueError:
            return _error_response(
                409, "RULEPACK_ACTIVATION_BLOCKED", "rule pack is not activatable"
            )

        body = RulePack.model_validate(result.body).model_dump(mode="json")
        return JSONResponse(
            status_code=200,
            content=body,
            headers={"ETag": f'"{result.revision}"'},
        )

    return router
