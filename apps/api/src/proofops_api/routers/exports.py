"""Authenticated local export metadata, creation and short-lived private downloads."""

import sqlite3
import time
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse, Response
from fastapi.security import APIKeyCookie
from proofops.application.exports import ExportRejected
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.routers.documents import StrictDTO, _request_schema
from proofops_api.routers.sources import Download
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field
from starlette.concurrency import run_in_threadpool


class ExportCreate(StrictDTO):
    formats: Annotated[
        list[Literal["json", "csv", "html"]],
        Field(min_length=1, max_length=3, json_schema_extra={"uniqueItems": True}),
    ]
    allow_partial: bool


class Export(StrictDTO):
    export_id: UUID
    run_id: UUID
    state: Literal["queued", "building", "ready", "failed"]
    snapshot_epoch: Annotated[int, Field(ge=0)]
    partial: bool
    manifest_sha256: Annotated[str, Field(pattern="^[0-9a-f]{64}$")] | None
    created_at: datetime


def build_exports_router(service, auth_store, *, allowed_origin: str, clock=time.time):
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
        "x-minimum-role": "viewer",
        "x-idempotency-required": False,
        "x-rate-limit": "120/min/user",
    }

    def authorize(request, operation, *, mutation=False, limit=120):
        auth = _authorize(request, auth_store, clock(), "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        if mutation:
            session = auth_store.sessions.get(auth.session_id)
            if session is None or not _verify_csrf(
                request, session.csrf_hash, allowed_origin=allowed_origin
            ):
                return _error_response(403, "CSRF_INVALID", "Invalid session or request origin.")
        limited = _request_limit_response(
            auth_store, user_sub=auth.user_sub, operation_id=operation, requests=limit, now=clock()
        )
        return limited if limited is not None else auth

    def failure(exc):
        if isinstance(exc, ExportRejected):
            return _error_response(exc.status, exc.code, "Export request could not be completed.")
        if isinstance(exc, RequestBodyTooLarge):
            return _error_response(413, "REQUEST_TOO_LARGE", "Export request is too large.")
        return _error_response(409, "EXPORT_INTEGRITY_FAILED", "Export inputs are unavailable.")

    @router.post(
        "/v1/runs/{run_id}/exports",
        operation_id="export_create",
        status_code=202,
        response_model=Export,
        openapi_extra=_request_schema(ExportCreate)
        | {
            "x-minimum-role": "viewer",
            "x-idempotency-required": True,
            "x-rate-limit": "10/min/user",
        },
    )
    async def create(request: Request, run_id: UUID):
        auth = authorize(request, "export_create", mutation=True, limit=10)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            try:
                body = await read_bounded_json(request)
            except RequestBodyTooLarge:
                raise
            except ValueError:
                return _error_response(422, "VALIDATION_ERROR", "Invalid JSON export request.")
            result = await run_in_threadpool(
                service.create,
                auth,
                str(run_id),
                body,
                request.headers.get("Idempotency-Key"),
                now=clock(),
            )
            return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.get(
        "/v1/exports/{export_id}",
        operation_id="export_get",
        response_model=Export,
        openapi_extra=read_contract,
    )
    def get(request: Request, export_id: UUID):
        auth = authorize(request, "export_get")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(
                service.get(auth, str(export_id), now=clock()),
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.post(
        "/v1/exports/{export_id}/download",
        operation_id="export_download",
        response_model=Download,
        openapi_extra=read_contract | {"x-rate-limit": "60/min/user"},
    )
    def download(request: Request, export_id: UUID):
        auth = authorize(request, "export_download", mutation=True, limit=60)
        if isinstance(auth, JSONResponse):
            return auth
        try:
            return JSONResponse(
                service.authorize_download(auth, str(export_id), now=clock()),
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    @router.get("/v1/exports/{export_id}/content", include_in_schema=False)
    def content(
        request: Request,
        export_id: UUID,
        ticket: Annotated[str, Query(min_length=40, max_length=64)],
    ):
        auth = authorize(request, "export_content")
        if isinstance(auth, JSONResponse):
            return auth
        try:
            payload = service.store.content(auth, str(export_id), ticket, now=clock())
            return Response(
                payload,
                media_type="application/zip",
                headers={
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Disposition": f'attachment; filename="proofops-{export_id}.zip"',
                },
            )
        except (ValueError, KeyError, TypeError, OSError, sqlite3.DatabaseError) as exc:
            return failure(exc)

    return router
