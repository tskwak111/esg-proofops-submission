"""HTTP boundary for the company and runtime-option registry."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from proofops.application.registry import (
    IdempotencyConflict,
    Registry,
    create_company,
    list_companies,
    list_runtime_options,
)
from proofops_api.auth import (
    AuthStore,
    _authorize,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.dto import CompanyCreate
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.request_limits import READ_REQUESTS_PER_MINUTE, WRITE_REQUESTS_PER_MINUTE
from pydantic import ValidationError


def build_registry_router(
    registry: Registry, auth_store: AuthStore, *, allowed_origin: str, clock: Any = None
) -> APIRouter:
    """Return a router bound to explicit state; the composition root owns wiring."""
    router = APIRouter()
    now_fn = clock if clock is not None else time.time

    @router.get(
        "/v1/companies",
        operation_id="companies_list",
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-rate-limit": "120/min/user",
            "x-idempotency-required": False,
        },
    )
    def companies_list(
        request: Request, cursor: str | None = None, limit: int = 50
    ) -> JSONResponse:
        now = now_fn()
        auth = _authorize(request, auth_store, now, "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="companies_list",
            requests=READ_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        try:
            page = list_companies(
                registry, actor=auth.user_sub, tenant_id=auth.tenant_id, cursor=cursor, limit=limit
            )
        except ValueError as exc:
            return _error_response(400, "INVALID_CURSOR", str(exc))
        return JSONResponse(
            status_code=200,
            content={
                "items": [_company_json(company) for company in page.items],
                "next_cursor": page.next_cursor,
                "snapshot_epoch": page.snapshot_epoch,
            },
        )

    @router.post(
        "/v1/companies",
        operation_id="company_create",
        openapi_extra={
            "x-minimum-role": "editor",
            "x-rate-limit": "10/min/user",
            "x-idempotency-required": True,
        },
    )
    async def company_create(request: Request) -> JSONResponse:
        now = now_fn()
        auth = _authorize(request, auth_store, now, "editor")
        if isinstance(auth, JSONResponse):
            return auth
        session = auth_store.sessions.get(auth.session_id)
        if session is None or not _verify_csrf(
            request, session.csrf_hash, allowed_origin=allowed_origin
        ):
            return _error_response(403, "CSRF_INVALID", "missing/invalid CSRF token or origin")
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="company_create",
            requests=WRITE_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        key = request.headers.get("Idempotency-Key")
        if key is None or not 16 <= len(key) <= 128:
            return _error_response(
                400, "IDEMPOTENCY_KEY_INVALID", "Idempotency-Key must be 16..128 chars"
            )
        try:
            payload = await read_bounded_json(request)
            if not isinstance(payload, dict):
                raise ValueError("company body must be an object")
            company_input = CompanyCreate.model_validate(payload)
            _, company = create_company(
                registry,
                actor=auth.user_sub,
                tenant_id=auth.tenant_id,
                legal_name=company_input.legal_name,
                aliases=company_input.aliases,
                registration_identifier=company_input.registration_identifier,
                idempotency_key=key,
            )
        except IdempotencyConflict as exc:
            return _error_response(409, "IDEMPOTENCY_CONFLICT", str(exc))
        except RequestBodyTooLarge:
            return _error_response(413, "PAYLOAD_TOO_LARGE", "company payload is too large")
        except (ValidationError, ValueError):
            return _error_response(422, "VALIDATION_ERROR", "invalid company payload")
        return JSONResponse(status_code=201, content=_company_json(company))

    @router.get(
        "/v1/runtime-options",
        operation_id="runtime_options",
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-rate-limit": "120/min/user",
            "x-idempotency-required": False,
        },
    )
    def runtime_options(request: Request) -> JSONResponse:
        now = now_fn()
        auth = _authorize(request, auth_store, now, "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="runtime_options",
            requests=READ_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        choices = list_runtime_options(registry, actor=auth.user_sub, tenant_id=auth.tenant_id)
        return JSONResponse(
            status_code=200,
            content={
                "rights_profiles": [_option_json(option) for option in choices.rights_profiles],
                "consent_profiles": [_option_json(option) for option in choices.consent_profiles],
                "runtime_bindings": [_option_json(option) for option in choices.runtime_bindings],
                "rule_packs": [_rule_pack_json(pack) for pack in choices.rule_packs],
                "enabled_modes": list(choices.enabled_modes),
            },
        )

    return router


def _company_json(company: Any) -> dict[str, object]:
    return {
        "company_id": company.company_id,
        "legal_name": company.legal_name,
        "registration_identifier": company.registration_identifier,
        "aliases": list(company.aliases),
        "created_at": company.created_at,
    }


def _option_json(option: Any) -> dict[str, object]:
    return {"id": option.id, "name": option.name, "status": option.status, "reason": option.reason}


def _rule_pack_json(pack: Any) -> dict[str, object]:
    return {
        "rule_pack_id": pack.rule_pack_id,
        "version": pack.version,
        "sha256": pack.sha256,
        "status": pack.status,
        "mode": pack.mode,
        "effective_date": pack.effective_date,
        "unresolved_gap_ids": list(pack.unresolved_gap_ids),
    }
