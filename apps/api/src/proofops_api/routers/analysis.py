"""Viewer-only local analysis routes over immutable run artifacts."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Query, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.analysis_store import SafeHarborPending
from proofops.adapters.local.catalog_pages import CatalogCapacityExceeded, InvalidCatalogCursor
from proofops.application.runs import RunRejected
from proofops.domain.errors import DomainValidationError
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    _authorize,
    _error_response,
    _request_limit_response,
)
from proofops_api.request_limits import READ_REQUESTS_PER_MINUTE
from proofops_api.routers.claims import AssuranceMatch
from proofops_api.routers.documents import StrictDTO
from proofops_api.routers.reviews import Element
from proofops_api.routers.sources import SourceRef
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import Field


class Observation(StrictDTO):
    observation_id: UUID
    metric: str
    scope: str | None
    entity: str | None
    period: str
    value: str | None
    unit: str | None
    measurement_basis: str | None
    value_state: Literal["value", "missing", "unreadable", "conflict"]
    evidence_refs: list[SourceRef]


class SafeHarborRecord(StrictDTO):
    claim_id: UUID
    applicable: bool | None
    category: Literal["forward_looking", "emissions_estimate", "third_party_information"] | None
    checklist: list[Element]
    reasonable_basis_documented: bool | None
    legal_effect: Literal["not_determined"]
    mapping_status: Literal["approved", "unresolved"]
    gap_ids: list[str]


class ObservationPage(StrictDTO):
    items: list[Observation]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


class AssuranceMatchPage(StrictDTO):
    items: list[AssuranceMatch]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


class SafeHarborRecordPage(StrictDTO):
    items: list[SafeHarborRecord]
    next_cursor: str | None
    snapshot_epoch: Annotated[int, Field(ge=0)] | None


def build_analysis_router(store, auth_store, *, clock=time.time) -> APIRouter:
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
    contract = {
        "x-minimum-role": "viewer",
        "x-idempotency-required": False,
        "x-rate-limit": "120/min/user",
    }

    def failure(exc, endpoint):
        if isinstance(exc, InvalidCatalogCursor):
            return _error_response(
                400, "INVALID_CURSOR", "Pagination cursor is invalid or expired."
            )
        if isinstance(exc, CatalogCapacityExceeded):
            return _error_response(
                503,
                "CATALOG_CAPACITY_EXCEEDED",
                "Snapshot capacity is temporarily exhausted.",
            )
        if isinstance(exc, RunRejected):
            return _error_response(exc.status, exc.code, "Analysis request could not be processed.")
        if isinstance(exc, SafeHarborPending):
            return _error_response(
                409,
                "SAFE_HARBOR_PENDING",
                "Confirmed safe-harbor inputs are not published consistently.",
            )
        return _error_response(
            409,
            "ARTIFACT_UNAVAILABLE",
            f"{endpoint} analysis is pending or failed integrity checks.",
        )

    def read(request, run_id, endpoint, cursor, limit, model):
        auth_now = time.time()
        auth = _authorize(request, auth_store, auth_now, "viewer")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id=endpoint,
            requests=READ_REQUESTS_PER_MINUTE,
            now=auth_now,
        )
        if limited is not None:
            return limited
        try:
            payload = getattr(store, endpoint.removesuffix("_get"))(
                auth.tenant_id,
                run_id,
                cursor=cursor,
                limit=limit,
                now=clock(),
            )
            body = model.model_validate_json(json.dumps(payload)).model_dump(mode="json")
            return JSONResponse(body, headers={"Cache-Control": "no-store"})
        except (ValueError, KeyError, OSError, DomainValidationError, sqlite3.DatabaseError) as exc:
            return failure(exc, endpoint)

    def add(path, operation_id, response_model):
        def endpoint(
            request: Request,
            run_id: UUID,
            cursor: str | None = None,
            limit: Annotated[int, Query(ge=1, le=100)] = 50,
        ):
            return read(request, str(run_id), operation_id, cursor, limit, response_model)

        router.add_api_route(
            path,
            endpoint,
            methods=["GET"],
            operation_id=operation_id,
            response_model=response_model,
            openapi_extra=contract,
        )

    add("/v1/runs/{run_id}/observations", "observations_get", ObservationPage)
    add("/v1/runs/{run_id}/assurance", "assurance_get", AssuranceMatchPage)
    add("/v1/runs/{run_id}/safe-harbor", "safe_harbor_get", SafeHarborRecordPage)
    return router
