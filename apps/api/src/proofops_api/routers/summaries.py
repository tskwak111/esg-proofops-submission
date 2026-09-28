"""Viewer-only local summary endpoint over one immutable SQLite snapshot."""

from __future__ import annotations

import sqlite3
import time
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.application.runs import RunRejected
from proofops.domain.errors import DomainValidationError
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
)
from proofops_api.routers.documents import StrictDTO
from proofops_api.routers.runs import Coverage
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field


class GradeCounts(StrictDTO):
    E0: Annotated[int, Field(ge=0)]
    E1: Annotated[int, Field(ge=0)]
    E2: Annotated[int, Field(ge=0)]
    E3: Annotated[int, Field(ge=0)]


class MissingElementCount(StrictDTO):
    element_id: str
    count: Annotated[int, Field(ge=0)]


class Summary(StrictDTO):
    run_id: UUID
    snapshot_epoch: Annotated[int, Field(ge=0)]
    coverage: Coverage
    grade_counts: GradeCounts
    undecided_count: Annotated[int, Field(ge=0)]
    not_applicable_count: Annotated[int, Field(ge=0)]
    deferred_count: Annotated[int, Field(ge=0)]
    unverified_basis_count: Annotated[int, Field(ge=0)]
    missing_by_element: list[MissingElementCount]
    applicable_count: Annotated[int, Field(ge=0)]
    satisfied_count: Annotated[int, Field(ge=0)]
    fulfillment_rate: Annotated[float, Field(ge=0, le=1)] | None
    # Optional for old Summary artifacts; new producers always return int or null.
    undetermined_applicability_count: Annotated[int, Field(ge=0)] | None = None


def build_summaries_router(store, auth_store, *, clock=time.time) -> APIRouter:
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

    @router.get(
        "/v1/runs/{run_id}/summary",
        operation_id="summary_get",
        response_model=Summary,
        openapi_extra={
            "x-minimum-role": "viewer",
            "x-idempotency-required": False,
            "x-rate-limit": "120/min/user",
        },
    )
    def get_summary(request: Request, run_id: UUID):
        now = clock()
        auth = _authorize(request, auth_store, now, "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="summary_get",
            requests=120,
            now=now,
        )
        if limited is not None:
            return limited
        try:
            return JSONResponse(
                store.get(auth.tenant_id, str(run_id)),
                headers={"Cache-Control": "no-store"},
            )
        except (KeyError, RunRejected):
            return _error_response(404, "RESOURCE_NOT_FOUND", "Summary was not found.")
        except (ValueError, DomainValidationError, sqlite3.DatabaseError):
            return _error_response(
                409, "SUMMARY_UNAVAILABLE", "Summary could not be read consistently."
            )

    return router
