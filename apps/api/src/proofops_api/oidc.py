"""BFF authorization-code + PKCE redirects; configuration is composition-owned."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Callable
from typing import Protocol, cast
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse
from proofops.adapters.aws.oidc import OIDCConfig, OIDCProvider, OIDCRejected, OIDCUnavailable
from proofops.adapters.local.auth_store import hash_token, new_session_id
from proofops.adapters.local.oidc_store import InMemoryOIDCStore, PendingLogin
from proofops.application.authorization import SessionPort, SessionRecord

from proofops_api.auth import SESSION_COOKIE_NAME, AuthStore, _error_response

OIDC_COOKIE_NAME = "__Host-proofops_oidc"


class _LoginSessions(SessionPort, Protocol):
    def put(self, record: SessionRecord) -> None: ...
    def revoke(self, session_id: str) -> None: ...


def _private(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def build_oidc_router(
    auth_store: AuthStore,
    *,
    config: OIDCConfig | None = None,
    provider: OIDCProvider | None = None,
    transactions: InMemoryOIDCStore | None = None,
    clock: Callable[[], float] = time.time,
) -> APIRouter:
    """Default: mounted but disabled. In-memory transactions are explicit local-only wiring."""
    router = APIRouter()
    store = transactions if transactions is not None else InMemoryOIDCStore()
    sessions = cast(_LoginSessions, auth_store.sessions)

    def available() -> bool:
        return (
            config is not None
            and provider is not None
            and provider.config == config
            and provider.ready
            and callable(getattr(sessions, "put", None))
            and callable(getattr(sessions, "revoke", None))
        )

    def error(status: int, code: str) -> Response:
        return _private(_error_response(status, code, "OIDC login unavailable or rejected"))

    @router.get("/auth/login")
    def login(request: Request) -> Response:
        if not available():
            return error(503, "OIDC_UNAVAILABLE")
        assert config is not None
        if (
            str(request.url).split("?", 1)[0] != config.app_origin + "/auth/login"
            or any(key != "return_to" for key in request.query_params)
            or len(request.query_params.getlist("return_to")) > 1
        ):
            return error(400, "OIDC_INVALID_REQUEST")
        return_to = request.query_params.get("return_to", config.return_to_allowlist[0])
        if return_to not in config.return_to_allowlist:
            return error(400, "OIDC_INVALID_REQUEST")
        state, nonce, verifier, binding = (secrets.token_urlsafe(32) for _ in range(4))
        now = clock()
        try:
            store.put(
                state,
                PendingLogin(
                    nonce, verifier, hash_token(binding), return_to, now + config.state_ttl_seconds
                ),
                now=now,
            )
        except (RuntimeError, ValueError):
            return error(503, "OIDC_UNAVAILABLE")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        response = RedirectResponse(
            config.authorization_endpoint
            + "?"
            + urlencode(
                {
                    "client_id": config.client_id,
                    "response_type": "code",
                    "scope": "openid",
                    "redirect_uri": config.redirect_uri,
                    "state": state,
                    "nonce": nonce,
                    "code_challenge": challenge.rstrip(b"=").decode(),
                    "code_challenge_method": "S256",
                }
            ),
            status_code=302,
        )
        response.set_cookie(
            OIDC_COOKIE_NAME,
            binding,
            max_age=config.state_ttl_seconds,
            secure=True,
            httponly=True,
            samesite="lax",
            path="/",
        )
        return _private(response)

    @router.get("/auth/callback")
    def callback(request: Request) -> Response:
        if not available():
            return error(503, "OIDC_UNAVAILABLE")
        assert config is not None and provider is not None
        if str(request.url).split("?", 1)[0] != config.redirect_uri:
            return error(400, "OIDC_INVALID_REQUEST")
        query = request.query_params
        if any(
            key not in {"state", "code", "error", "error_description", "iss"} for key in query
        ) or any(len(query.getlist(key)) != 1 for key in query):
            return error(400, "OIDC_INVALID_REQUEST")
        state, binding = query.get("state", ""), request.cookies.get(OIDC_COOKIE_NAME, "")
        if not 1 <= len(state) <= 256 or not 1 <= len(binding) <= 256:
            return error(401, "OIDC_REJECTED")
        now = clock()
        pending = store.consume(state, binding, now=now)
        if pending is None:
            return error(401, "OIDC_REJECTED")
        code = query.get("code", "")
        if (
            "error" in query
            or not 1 <= len(code) <= 4096
            or ("iss" in query and query["iss"] != config.issuer)
            or not store.reserve_code(code, now=now)
        ):
            return error(401, "OIDC_REJECTED")
        sid: str | None = None
        try:
            user_sub, refresh = provider.exchange(
                code=code, verifier=pending.verifier, nonce=pending.nonce
            )
            sid = new_session_id()
            provider.persist_refresh(hash_token(sid), user_sub, refresh)
            now = clock()
            record = SessionRecord(sid, user_sub, None, "", now + 8 * 3600, now + 1800, False)
            sessions.put(record)
            token = sessions.csrf_token_for(sid)
            saved = sessions.get(sid)
            if (
                token is None
                or saved is None
                or not secrets.compare_digest(hash_token(token), saved.csrf_hash)
            ):
                sessions.revoke(sid)
                raise OIDCUnavailable("session persistence unavailable")
            old_sid = request.cookies.get(SESSION_COOKIE_NAME)
            if old_sid:
                sessions.revoke(old_sid)
        except OIDCRejected:
            return error(401, "OIDC_REJECTED")
        except Exception:
            if sid is not None:
                try:
                    sessions.revoke(sid)
                except Exception:
                    # No cookie is issued; a durable adapter must reconcile failed writes.
                    pass
            return error(503, "OIDC_UNAVAILABLE")
        assert sid is not None
        response = RedirectResponse(pending.return_to, status_code=302)
        response.set_cookie(
            SESSION_COOKIE_NAME, sid, secure=True, httponly=True, samesite="lax", path="/"
        )
        response.delete_cookie(
            OIDC_COOKIE_NAME, secure=True, httponly=True, samesite="lax", path="/"
        )
        return _private(response)

    return router
