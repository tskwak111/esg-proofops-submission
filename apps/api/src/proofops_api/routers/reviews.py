"""Authorized review resolution with bounded input, CSRF, CAS and rate limits."""

import sqlite3
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.catalog_pages import CatalogCapacityExceeded, InvalidCatalogCursor
from proofops.application.reviews import ReviewRejected
from proofops.application.runs import RunRejected
from proofops.domain.errors import DomainValidationError
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
    _verify_csrf,
)
from proofops_api.dto import Decision
from proofops_api.middleware import RequestBodyTooLarge, read_bounded_json
from proofops_api.routers.documents import StrictDTO, _request_schema
from proofops_api.routers.sources import SourceRef
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field
from starlette.concurrency import run_in_threadpool


class Element(StrictDTO):
    element_id: str
    state: Literal["present", "absent", "unknown", "conflict", "not_applicable"]
    evidence_refs: list[SourceRef]
    normalized_value: str | None
    credited_from: UUID | None
    reason_code: str | None


class ReviewResolve(StrictDTO):
    base_tag_revision: Annotated[int, Field(ge=1)]
    track: Literal["goal", "performance", "management"]
    elements: list[Element]
    reason: Annotated[str, Field(min_length=5, max_length=1000)]


class Review(StrictDTO):
    review_id: UUID
    run_id: UUID
    claim_id: UUID
    status: Literal["open", "resolved", "superseded"]
    revision: Annotated[int, Field(ge=1)]
    base_tag_revision: Annotated[int, Field(ge=1)]
    reason_codes: list[str]


class ReviewResolution(StrictDTO):
    review: Review
    decision: Decision
    new_tag_revision: Annotated[int, Field(ge=1)]


class ReviewPage(StrictDTO):
    items: list[Review]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


def build_reviews_router(
    service, auth_store, *, allowed_origin: str, run_store=None, clock=time.time
):
    router = APIRouter(
        responses=_ERROR_RESPONSES,
        dependencies=[
            Security(
                APIKeyCookie(
                    name=SESSION_COOKIE_NAME,
                    scheme_name="sessionCookie",
                    auto_error=False,
                )
            )
        ],
    )

    request_schema = _request_schema(ReviewResolve) | {"x-minimum-role": "reviewer"}
    request_schema["parameters"].append(
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "schema": {"type": "string", "pattern": '^"[1-9][0-9]*"$'},
        }
    )

    @router.get(
        "/v1/runs/{run_id}/reviews",
        operation_id="reviews_list",
        response_model=ReviewPage,
        openapi_extra={
            "x-minimum-role": "reviewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def listed(
        request: Request,
        run_id: UUID,
        cursor: str | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ):
        now = clock()
        auth = _authorize(request, auth_store, now, "reviewer")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store, user_sub=auth.user_sub, operation_id="reviews_list", requests=120, now=now
        )
        if limited is not None:
            return limited
        if run_store is None:
            return _error_response(
                409, "REVIEW_QUEUE_UNAVAILABLE", "Review queue is not configured."
            )
        try:
            result = service.store.page(
                auth.tenant_id,
                str(run_id),
                cursor=cursor,
                limit=limit,
                now=int(now),
                cursors=run_store,
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (ReviewRejected, RunRejected) as error:
            return _error_response(error.status, error.code, "Review queue unavailable.")
        except InvalidCatalogCursor:
            return _error_response(
                400, "INVALID_CURSOR", "Pagination cursor is invalid or expired."
            )
        except CatalogCapacityExceeded:
            return _error_response(
                503, "CATALOG_CAPACITY_EXCEEDED", "Snapshot capacity is temporarily exhausted."
            )
        except (sqlite3.DatabaseError, ValueError, KeyError):
            return _error_response(409, "REVIEW_CONFLICT", "Review queue unavailable.")

    async def resolve_request(request: Request, review_id: UUID, *, reopen: bool):
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
            auth_store, user_sub=auth.user_sub, operation_id="review_resolve", requests=10, now=now
        )
        if limited is not None:
            return limited
        try:
            body = await read_bounded_json(request)
            result = await run_in_threadpool(
                service.resolve_review,
                auth,
                str(review_id),
                body,
                request.headers.get("If-Match"),
                request.headers.get("Idempotency-Key"),
                reopen=reopen,
            )
            return JSONResponse(
                result,
                headers={"ETag": f'"{result["review"]["revision"]}"', "Cache-Control": "no-store"},
            )
        except ReviewRejected as error:
            return _error_response(
                error.status, error.code, "Review request could not be processed."
            )
        except (RequestBodyTooLarge, DomainValidationError, ValueError, TypeError):
            return _error_response(422, "VALIDATION_ERROR", "Invalid review request.")
        except sqlite3.DatabaseError:
            return _error_response(
                409, "REVIEW_CONFLICT", "Review transaction could not be committed."
            )

    @router.post(
        "/v1/reviews/{review_id}/resolve",
        operation_id="review_resolve",
        response_model=ReviewResolution,
        openapi_extra=request_schema,
    )
    async def resolve(request: Request, review_id: UUID):
        return await resolve_request(request, review_id, reopen=False)

    @router.post(
        "/v1/reviews/{review_id}/re-review",
        operation_id="review_re_review",
        response_model=ReviewResolution,
        openapi_extra=request_schema,
    )
    async def re_review(request: Request, review_id: UUID):
        return await resolve_request(request, review_id, reopen=True)

    return router
