"""HTTP boundary, using tenant-resolved immutable approval artifacts.

This local router's replay/rate state is process-local. Non-local composition
must supply durable request control before enabling the endpoint. A successful
preflight response is informational and must never be reused as a dispatch
authorization token; the Bedrock adapter checks its run snapshots again.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from proofops.application.authorization import AuthContext, TenantNotFoundError
from proofops.application.preflight import (
    check_local_upstage_binding,
    check_runtime_binding,
    combine_build_checks,
)
from proofops.application.supply_chain import SupplyChainResult
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from proofops_api.auth import (
    AuthStore,
    _authenticate,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.request_limits import (
    LIVE_PROBES_PER_MINUTE,
    WRITE_REQUESTS_PER_MINUTE,
    RequestLimit,
)


class PreflightRequest(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    runtime_binding_id: UUID
    consent_profile_id: UUID
    include_live_model_probe: bool

    @field_validator("runtime_binding_id", "consent_profile_id", mode="before")
    @classmethod
    def parse_uuid(cls, value: Any) -> Any:
        return UUID(value) if isinstance(value, str) else value


ProfileResolver = Callable[[AuthContext, str, str], Mapping[str, Any]]


def build_preflight_router(
    auth_store: AuthStore,
    *,
    resolve_profile: ProfileResolver,
    allowed_regions: Sequence[str],
    allowed_origin: str,
    build_result: SupplyChainResult | None = None,
    clock: Callable[[], float] = time.time,
    app_env: str = "local",
) -> APIRouter:
    if app_env != "local":
        raise ValueError("preflight local request control requires durable deployment wiring")
    router = APIRouter()
    regions = tuple(allowed_regions)
    # ponytail: process-local lock/replay/rate limits; use durable CAS control
    # before enabling this endpoint on multiple API replicas.
    lock = threading.Lock()
    replays: dict[tuple[str, str, str], tuple[str, dict[str, Any], float]] = {}

    @router.post(
        "/v1/preflight",
        operation_id="preflight",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": PreflightRequest.model_json_schema()}},
            },
            "x-minimum-role": "admin",
            "x-rate-limit": "10/min/user",
            "x-idempotency-required": True,
        },
    )
    async def preflight(request: Request) -> JSONResponse:
        now = clock()
        auth = _authenticate(request, auth_store, now=now)
        if isinstance(auth, JSONResponse):
            return auth
        if not auth.has_capability("admin"):
            return _error_response(403, "FORBIDDEN", "administrator capability required")
        session = auth_store.sessions.get(auth.session_id)
        if session is None or not _verify_csrf(
            request, session.csrf_hash, allowed_origin=allowed_origin
        ):
            return _error_response(403, "CSRF_INVALID", "invalid CSRF token or origin")
        key = request.headers.get("Idempotency-Key", "")
        if not 16 <= len(key) <= 128:
            return _error_response(400, "INVALID_REQUEST", "valid Idempotency-Key required")
        try:
            payload = PreflightRequest.model_validate(await read_bounded_json(request))
        except RequestBodyTooLarge:
            return _error_response(413, "PAYLOAD_TOO_LARGE", "JSON body exceeds size limit")
        except (ValidationError, ValueError, UnicodeError):
            return _error_response(422, "VALIDATION_ERROR", "invalid preflight request")
        additional = (
            (
                RequestLimit(
                    "tenant",
                    auth.tenant_id,
                    "preflight_live_probe",
                    LIVE_PROBES_PER_MINUTE,
                ),
            )
            if payload.include_live_model_probe
            else ()
        )
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="preflight",
            requests=WRITE_REQUESTS_PER_MINUTE,
            now=now,
            additional=additional,
        )
        if limited is not None:
            return limited
        body_hash = payload.model_dump_json()
        scope = (auth.tenant_id, auth.user_sub, key)
        with lock:
            for saved_key, (_, _, saved_at) in list(replays.items()):
                if now - saved_at >= 86400:
                    del replays[saved_key]
            try:
                runtime = resolve_profile(auth, "runtime", str(payload.runtime_binding_id))
                consent = resolve_profile(auth, "consent", str(payload.consent_profile_id))
                if (
                    runtime.get("tenant_id") != auth.tenant_id
                    or consent.get("tenant_id") != auth.tenant_id
                    or runtime.get("runtime_binding_id") != str(payload.runtime_binding_id)
                    or consent.get("consent_profile_id") != str(payload.consent_profile_id)
                ):
                    raise TenantNotFoundError("profile not found")
                if scope in replays:
                    previous_body, response_body, _ = replays[scope]
                    if previous_body != body_hash:
                        return _error_response(409, "IDEMPOTENCY_CONFLICT", "request body changed")
                    return JSONResponse(content=response_body)
                checker = (
                    check_local_upstage_binding
                    if runtime.get("provider") == "upstage"
                    else check_runtime_binding
                )
                kwargs = (
                    {} if checker is check_local_upstage_binding else {"allowed_regions": regions}
                )
                result = checker(
                    binding=runtime,
                    consent=consent,
                    auth=auth,
                    checked_at=datetime.fromtimestamp(now, UTC).isoformat(),
                    include_live_model_probe=payload.include_live_model_probe,
                    **kwargs,
                )
                response_body = combine_build_checks(result, build_result).to_dict()
            except (TenantNotFoundError, LookupError):
                return _error_response(404, "RESOURCE_NOT_FOUND", "profile not found")
            except (ValueError, TypeError):
                return _error_response(409, "CONFIG_GATE_BLOCKED", "invalid approval artifacts")
            except Exception:
                return _error_response(
                    503, "DEPENDENCY_UNAVAILABLE", "profile resolver unavailable"
                )
            # Serialized response is detached from mutable profile/store records.
            replays[scope] = (body_hash, json.loads(json.dumps(response_body)), now)
            return JSONResponse(content=response_body)

    return router
