"""Manual preliminary classification route: authorized reviewer classifies a
source-verified claim stopped at PRELIMINARY_TAGS_UNRESOLVED, then a bounded reprocess
tag job reaches the existing element review.

GET is a read-only eligibility + numbered-sources + current-state projection (viewer).
POST records an immutable human classification and atomically enqueues exactly one
bounded reprocess job (reviewer + CSRF + exact If-Match lineage token + Idempotency).
The HTTP origin is always ``human_classification``; the trusted local AI-delegated
method is separate and never reachable here. No grade/label/decision/confidence is
ever accepted from the client, and a source-unverified span is rejected.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.classification_store import ClassificationStoreError
from proofops.application.tagging.manual_classification import (
    HUMAN_ORIGIN,
    ClassificationRejected,
)
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.routers.documents import StrictDTO, _request_schema
from proofops_api.routers.sources import SourceRef
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field
from starlette.concurrency import run_in_threadpool


class ClassificationSpan(StrictDTO):
    source_index: Annotated[int, Field(ge=0)]
    quote: Annotated[str, Field(min_length=1)]
    start: Annotated[int, Field(ge=0)] | None = None
    end: Annotated[int, Field(ge=1)] | None = None


class ClassificationRequest(StrictDTO):
    track: Literal["goal", "performance", "management"]
    safe_harbor_category: (
        Literal["forward_looking", "emissions_estimate", "third_party_information"] | None
    )
    dimensions: dict[
        Literal[
            "entity",
            "metric",
            "reporting_period",
            "facility",
            "scope",
            "product",
            "material",
            "boundary",
        ],
        ClassificationSpan | None,
    ]
    reason: Annotated[str, Field(min_length=5, max_length=1000)]


class ClassificationSource(StrictDTO):
    source_index: int
    quote: str
    source_ref: SourceRef


class ClassificationView(StrictDTO):
    schema_version: Literal[1]
    run_id: UUID
    claim_id: UUID
    eligible: bool
    ineligible_reason: str | None
    blocked_reason: str | None
    etag: str | None
    sources: list[ClassificationSource]
    dimension_axes: list[str]
    preliminary_agreement: dict
    current_classification: dict | None
    pending_job: dict | None
    allowed_tracks: list[str]
    allowed_safe_harbor_categories: list[str | None]


class ClassificationAccepted(StrictDTO):
    schema_version: Literal[1]
    classification: dict
    reprocess_job: dict


def build_classification_router(store, auth_store, *, allowed_origin: str, clock=time.time):
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

    request_schema = _request_schema(ClassificationRequest) | {"x-minimum-role": "reviewer"}
    body_schema = request_schema["requestBody"]["content"]["application/json"]["schema"]
    span_schema = body_schema.pop("$defs")["ClassificationSpan"]
    body_schema["properties"]["dimensions"]["additionalProperties"]["anyOf"][0] = span_schema
    request_schema["parameters"].append(
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "pattern": '^"[a-f0-9]{64}"$'},
        }
    )

    def _fail(exc):
        if isinstance(exc, ClassificationStoreError | ClassificationRejected):
            return _error_response(
                getattr(exc, "status", 422),
                str(getattr(exc, "code", "VALIDATION_ERROR")).split(":", 1)[0],
                "Classification request could not be processed.",
            )
        if isinstance(exc, sqlite3.DatabaseError):
            return _error_response(409, "CLASSIFICATION_CONFLICT", "Classification unavailable.")
        return _error_response(422, "VALIDATION_ERROR", "Invalid classification request.")

    @router.get(
        "/v1/runs/{run_id}/claims/{claim_id}/classification",
        operation_id="claim_classification_get",
        response_model=ClassificationView,
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def get_classification(request: Request, run_id: UUID, claim_id: UUID):
        now = clock()
        auth = _authorize(request, auth_store, now, "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="claim_classification_get",
            requests=120,
            now=now,
        )
        if limited is not None:
            return limited
        try:
            body = store.view(auth, str(run_id), str(claim_id))
        except (ClassificationStoreError, ClassificationRejected) as exc:
            return _fail(exc)
        except (ValueError, KeyError, sqlite3.DatabaseError) as exc:
            return _fail(exc)
        headers = {"Cache-Control": "no-store"}
        if body.get("etag") is not None:
            headers["ETag"] = body["etag"]
        return JSONResponse(body, headers=headers)

    @router.post(
        "/v1/runs/{run_id}/claims/{claim_id}/classification",
        operation_id="claim_classification_create",
        status_code=202,
        response_model=ClassificationAccepted,
        openapi_extra=request_schema,
    )
    async def create_classification(request: Request, run_id: UUID, claim_id: UUID):
        now = clock()
        auth = _authorize(request, auth_store, now, "reviewer")
        if isinstance(auth, JSONResponse):
            return auth
        session = auth_store.sessions.get(auth.session_id)
        if session is None or not _verify_csrf(
            request, session.csrf_hash, allowed_origin=allowed_origin
        ):
            return _error_response(403, "CSRF_INVALID", "Invalid session or request origin.")
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="claim_classification_create",
            requests=10,
            now=now,
        )
        if limited is not None:
            return limited
        try:
            body = await read_bounded_json(request)
            ClassificationRequest.model_validate(body)
            if_match = request.headers.get("If-Match")
            if if_match is None or re.fullmatch(r'"[a-f0-9]{64}"', if_match) is None:
                return _error_response(
                    400, "IF_MATCH_REQUIRED", "A quoted lineage ETag is required."
                )
            result = await run_in_threadpool(
                store.record_and_enqueue,
                auth,
                str(run_id),
                str(claim_id),
                body,
                request.headers.get("If-Match"),
                request.headers.get("Idempotency-Key"),
                origin=HUMAN_ORIGIN,
                classified_by=auth.user_sub,
                delegation_authority=None,
                now=int(now),
            )
            return JSONResponse(
                result,
                status_code=202,
                headers={
                    "ETag": f'"{result["classification"]["revision"]}"',
                    "Cache-Control": "no-store",
                },
            )
        except (ClassificationStoreError, ClassificationRejected) as exc:
            return _fail(exc)
        except (RequestBodyTooLarge, ValueError, TypeError, KeyError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid classification request.")
        except sqlite3.DatabaseError:
            return _error_response(
                409, "CLASSIFICATION_CONFLICT", "Classification could not be committed."
            )

    return router


__all__ = ["build_classification_router"]
