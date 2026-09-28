"""SEC-004 fixed POST boundary: admin, CSRF, rate limit and idempotent tombstone."""

import sqlite3
import time
from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.retention_store import RetentionRejected
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
from pydantic import Field
from starlette.concurrency import run_in_threadpool


class ActionReason(StrictDTO):
    reason: Annotated[str, Field(min_length=5, max_length=1000)]


class DeletionRequest(StrictDTO):
    deletion_id: UUID
    status: Literal["requested", "running", "blocked_retention", "completed", "failed"]
    requested_at: datetime


def build_deletion_router(store, auth_store, *, allowed_origin, clock=time.time):
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

    @router.post(
        "/v1/documents/{document_id}/deletion-requests",
        operation_id="document_delete",
        status_code=202,
        response_model=DeletionRequest,
        openapi_extra=_request_schema(ActionReason) | {"x-minimum-role": "admin"},
    )
    async def request_deletion(request: Request, document_id: UUID):
        now = clock()
        actor = _authorize(request, auth_store, now, "admin")
        if isinstance(actor, JSONResponse):
            return actor
        session = auth_store.sessions.get(actor.session_id)
        if session is None or not _verify_csrf(
            request, session.csrf_hash, allowed_origin=allowed_origin
        ):
            return _error_response(403, "CSRF_INVALID", "Invalid session or request origin.")
        limited = _request_limit_response(
            auth_store,
            user_sub=actor.user_sub,
            operation_id="document_delete",
            requests=10,
            now=now,
        )
        if limited is not None:
            return limited
        try:
            body = await read_bounded_json(request)
            result = await run_in_threadpool(
                store.request,
                actor,
                str(document_id),
                body,
                request.headers.get("Idempotency-Key"),
                now=now,
            )
            return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})
        except RetentionRejected as exc:
            return _error_response(exc.status, exc.code, "Deletion request could not be completed.")
        except RequestBodyTooLarge:
            return _error_response(413, "REQUEST_TOO_LARGE", "Deletion request is too large.")
        except (ValueError, TypeError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid deletion request.")
        except (sqlite3.DatabaseError, OSError):
            return _error_response(
                503, "DEPENDENCY_UNAVAILABLE", "Deletion request is unavailable."
            )

    return router
