"""Session/tenant HTTP boundary.

Wires proofops.application.authorization into FastAPI: cookie/session
extraction, CSRF+Origin verification on state-changing requests, and the
Session/TenantSelect/Error response shapes from contracts/openapi.yaml.

Scope and deferrals:
- Cognito OIDC/PKCE login+callback is outside this boundary; this module
  only consumes an established server session record. `COGNITO_*` and
  `SESSION_SECRET_ARN` are cloud/staging inputs this
  module never reads or fabricates.
- Session/membership storage in this file is whatever
  `state.composition.auth_store` provides. Local dev/tests wire the
  in-memory adapter (proofops.adapters.local.auth_store); non-local
  composition must supply a real DynamoDB-backed store before this router
  is safe to serve traffic, matching the fail-closed pattern in
  packages/proofops/composition.py.

HTTP contract:
- no/expired/revoked session -> 401 AUTH_REQUIRED / SESSION_EXPIRED
- missing/invalid CSRF token or Origin mismatch on state-changing requests -> 403 CSRF_INVALID
- GET live session without an active tenant -> 200 with null tenant/role and CSRF
- tenant-scoped authorization without an active tenant, or capability missing -> 403 FORBIDDEN
- tenant-scoped id unreachable (nonexistent OR not-a-member) -> 404 RESOURCE_NOT_FOUND,
  identical response shape either way (AT-037: existence is never leaked)
- successful tenant switch rotates the session (new session id + CSRF, old id revoked)
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from proofops.application.authorization import (
    AuthContext,
    Capability,
    CapabilityDeniedError,
    MembershipPort,
    SessionExpiredError,
    SessionPort,
    TenantNotFoundError,
    authorize,
    get_live_session,
    select_tenant,
)
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json, verify_csrf
from proofops_api.request_limits import (
    READ_REQUESTS_PER_MINUTE,
    WRITE_REQUESTS_PER_MINUTE,
    LocalRequestLimiter,
    RequestLimit,
)
from proofops_api.telemetry import current_request_id

SESSION_COOKIE_NAME = "__Host-proofops_session"


class _StrictDTO(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Session(_StrictDTO):
    user_id: str
    tenant_id: uuid.UUID | None
    role: Literal["viewer", "editor", "reviewer", "admin"] | None
    csrf_token: str
    expires_at: str


class TenantSelect(_StrictDTO):
    tenant_id: uuid.UUID

    @field_validator("tenant_id", mode="before")
    @classmethod
    def _coerce_uuid(cls, value: Any) -> Any:
        if isinstance(value, str):
            return uuid.UUID(value)
        return value


def _error_body(code: str, message: str, request_id: str, *, retryable: bool = False) -> dict:
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "retryable": retryable,
        }
    }


def _error_response(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=_error_body(code, message, current_request_id() or str(uuid.uuid4())),
    )


@dataclass(frozen=True, slots=True)
class AuthStore:
    """Local composition-root wiring. Not a public contract:
    the auth_store attribute name/shape may change once real Cognito +
    DynamoDB adapters land; only SessionPort/MembershipPort are the
    stable boundary."""

    sessions: SessionPort
    memberships: MembershipPort
    request_limits: LocalRequestLimiter = field(default_factory=LocalRequestLimiter)

    @staticmethod
    def hash_csrf(token: str) -> str:
        from proofops.adapters.local.auth_store import hash_token

        return hash_token(token)


def _epoch_to_rfc3339(epoch_seconds: float) -> str:
    import datetime

    return (
        datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.UTC)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _verify_csrf(request: Request, csrf_hash: str, *, allowed_origin: str | None = None) -> bool:
    """Constant-time-ish CSRF check plus same-origin enforcement.

    docs/11: "변경 요청은 CSRF token+Origin 을 검사한다". Both must hold for
    any state-changing (non-GET) request.
    """
    return verify_csrf(
        origin=request.headers.get("Origin"),
        csrf_token=request.headers.get("X-CSRF-Token"),
        csrf_hash=csrf_hash,
        allowed_origin=allowed_origin,
    )


def _authenticate(
    request: Request, auth_store: AuthStore, *, now: float
) -> AuthContext | JSONResponse:
    """Resolve the session cookie into an AuthContext, or an error response.

    Returns the AuthContext on success. On failure returns the exact
    JSONResponse the caller should send: this keeps the 401/403/404 mapping
    in one place so every route applies it identically.
    """
    return _authorize(request, auth_store, now, "viewer")


def _authorize(
    request: Request,
    auth_store: AuthStore,
    now: float,
    capability: Capability,
) -> AuthContext | JSONResponse:
    """Authorize one tenant capability with the shared HTTP error mapping."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id is None:
        return _error_response(401, "AUTH_REQUIRED", "no session cookie presented")
    try:
        return authorize(
            session_id=session_id,
            now=now,
            session_port=auth_store.sessions,
            membership_port=auth_store.memberships,
            required_capability=capability,
        )
    except SessionExpiredError:
        return _error_response(401, "SESSION_EXPIRED", "session is missing or expired")
    except CapabilityDeniedError:
        return _error_response(403, "FORBIDDEN", "no active tenant or insufficient capability")
    except TenantNotFoundError:
        return _error_response(404, "RESOURCE_NOT_FOUND", "tenant not found")


def _request_limit_response(
    auth_store: AuthStore,
    *,
    user_sub: str,
    operation_id: str,
    requests: int,
    now: float,
    additional: tuple[RequestLimit, ...] = (),
) -> JSONResponse | None:
    retry_after = auth_store.request_limits.consume(
        (RequestLimit("user", user_sub, operation_id, requests), *additional),
        now=now,
    )
    if retry_after is None:
        return None
    response = _error_response(429, "RATE_LIMITED", "request rate limit exceeded")
    response.headers["Retry-After"] = str(retry_after)
    return response


def build_auth_router(
    auth_store: AuthStore,
    *,
    clock: Any = None,
    allowed_origin: str | None = None,
) -> APIRouter:
    """Build the /v1/session[...] router bound to a specific AuthStore.

    `clock` defaults to `time.time`; tests may inject a fixed clock. Kept as
    a constructor parameter (not a global) so the fail-closed local/staging
    distinction in packages/proofops/composition.py stays the single place
    that decides which store backs this router.
    """
    import time

    router = APIRouter()
    now_fn = clock if clock is not None else time.time

    @router.get(
        "/v1/session",
        operation_id="session_read",
        openapi_extra={
            "x-minimum-role": "all",
            "x-rate-limit": "120/min/user",
            "x-idempotency-required": False,
        },
    )
    def session_read(request: Request) -> Response:
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if session_id is None:
            return _error_response(401, "AUTH_REQUIRED", "no session cookie presented")
        now = now_fn()
        try:
            session_record = get_live_session(
                session_id=session_id,
                now=now,
                session_port=auth_store.sessions,
            )
        except SessionExpiredError:
            return _error_response(401, "SESSION_EXPIRED", "session is missing or expired")

        ctx: AuthContext | None = None
        if session_record.active_tenant_id is not None:
            result = _authenticate(request, auth_store, now=now)
            if isinstance(result, JSONResponse):
                return result
            ctx = result
        limited = _request_limit_response(
            auth_store,
            user_sub=session_record.user_sub,
            operation_id="session_read",
            requests=READ_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        body = Session(
            user_id=session_record.user_sub,
            tenant_id=uuid.UUID(ctx.tenant_id) if ctx else None,
            role=ctx.role if ctx else None,
            csrf_token=_csrf_token_for(auth_store, session_record.session_id),
            expires_at=_epoch_to_rfc3339(session_record.expires_at),
        )
        return JSONResponse(status_code=200, content=body.model_dump(mode="json"))

    @router.post(
        "/v1/session/tenant",
        operation_id="tenant_switch",
        openapi_extra={
            "x-minimum-role": "all",
            "x-rate-limit": "10/min/user",
            "x-idempotency-required": False,
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": TenantSelect.model_json_schema()}},
            },
        },
    )
    async def tenant_switch(request: Request) -> Response:
        session_id = request.cookies.get(SESSION_COOKIE_NAME)
        if session_id is None:
            return _error_response(401, "AUTH_REQUIRED", "no session cookie presented")
        now = now_fn()
        try:
            session_record = get_live_session(
                session_id=session_id,
                now=now,
                session_port=auth_store.sessions,
            )
        except SessionExpiredError:
            return _error_response(401, "SESSION_EXPIRED", "session is missing or expired")
        if not _verify_csrf(
            request,
            session_record.csrf_hash,
            allowed_origin=allowed_origin,
        ):
            return _error_response(403, "CSRF_INVALID", "missing/invalid CSRF token or origin")
        limited = _request_limit_response(
            auth_store,
            user_sub=session_record.user_sub,
            operation_id="tenant_switch",
            requests=WRITE_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited

        try:
            payload = TenantSelect.model_validate(await read_bounded_json(request))
        except RequestBodyTooLarge:
            return _error_response(413, "PAYLOAD_TOO_LARGE", "JSON body exceeds size limit")
        except (ValidationError, ValueError):
            return _error_response(422, "VALIDATION_ERROR", "invalid tenant selection")

        try:
            ctx = select_tenant(
                session_id=session_id,
                tenant_id=str(payload.tenant_id),
                now=now,
                session_port=auth_store.sessions,
                membership_port=auth_store.memberships,
            )
        except SessionExpiredError:
            return _error_response(401, "SESSION_EXPIRED", "session is missing or expired")
        except TenantNotFoundError:
            return _error_response(404, "RESOURCE_NOT_FOUND", "tenant not found")

        new_session = auth_store.sessions.get(ctx.session_id)
        assert new_session is not None
        body = Session(
            user_id=ctx.user_sub,
            tenant_id=uuid.UUID(ctx.tenant_id),
            role=ctx.role,
            csrf_token=_csrf_token_for(auth_store, ctx.session_id),
            expires_at=_epoch_to_rfc3339(new_session.expires_at),
        )
        response = JSONResponse(status_code=200, content=body.model_dump(mode="json"))
        response.set_cookie(
            SESSION_COOKIE_NAME,
            ctx.session_id,
            httponly=True,
            secure=True,
            samesite="lax",
            path="/",
        )
        return response

    return router


def _csrf_token_for(auth_store: AuthStore, session_id: str) -> str:
    """Issue a CSRF token only when it matches the stored hash."""
    token = auth_store.sessions.csrf_token_for(session_id)
    record = auth_store.sessions.get(session_id)
    if (
        token is not None
        and record is not None
        and secrets.compare_digest(auth_store.hash_csrf(token), record.csrf_hash)
    ):
        return token
    raise RuntimeError("session store could not issue a CSRF token matching its stored hash")
