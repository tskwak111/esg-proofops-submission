"""Browser session rotation and logout HTTP boundary."""

from __future__ import annotations

import time
from typing import Any, Protocol, cast

from fastapi import APIRouter, Request, Response
from proofops.application.authorization import (
    SessionExpiredError,
    SessionPort,
    SessionRecord,
    get_live_session,
)

from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    AuthStore,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.request_limits import WRITE_REQUESTS_PER_MINUTE


class SessionSecurityStore(SessionPort, Protocol):
    def revoke(self, session_id: str) -> None: ...


def rotate_session(
    *,
    session_id: str,
    tenant_id: str,
    now: float,
    session_store: SessionPort,
) -> SessionRecord:
    """Use the store's atomic, live-session-checked rotation operation."""
    return session_store.set_active_tenant(session_id, tenant_id, now=now)


def logout(*, session_id: str, now: float, session_store: SessionSecurityStore) -> None:
    """Reject dead sessions and immediately revoke a live server session."""
    get_live_session(session_id=session_id, now=now, session_port=session_store)
    session_store.revoke(session_id)


def build_session_router(
    auth_store: AuthStore,
    *,
    allowed_origin: str | None = None,
    clock: Any = None,
) -> APIRouter:
    router = APIRouter()
    now_fn = clock if clock is not None else time.time

    @router.post(
        "/v1/auth/logout",
        status_code=204,
        operation_id="logout",
        openapi_extra={
            "x-minimum-role": "all",
            "x-rate-limit": "10/min/user",
            "x-idempotency-required": False,
        },
    )
    def session_logout(request: Request) -> Response:
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
            return _error_response(
                403,
                "CSRF_INVALID",
                "missing/invalid CSRF token or origin",
            )
        limited = _request_limit_response(
            auth_store,
            user_sub=session_record.user_sub,
            operation_id="logout",
            requests=WRITE_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited

        try:
            logout(
                session_id=session_id,
                now=now,
                session_store=cast(SessionSecurityStore, auth_store.sessions),
            )
        except SessionExpiredError:
            return _error_response(401, "SESSION_EXPIRED", "session is missing or expired")

        response = Response(status_code=204)
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    return router
