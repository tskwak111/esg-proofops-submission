"""Admin-only projection of immutable local evaluation artifacts."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Request, Security
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyCookie
from proofops.adapters.local.evaluation_store import (
    EvaluationCorrupt,
    EvaluationNotFound,
    LocalEvaluationStore,
)
from proofops_api.auth import (
    SESSION_COOKIE_NAME,
    AuthStore,
    _authorize,
    _error_response,
    _request_limit_response,
)
from proofops_api.request_limits import READ_REQUESTS_PER_MINUTE
from proofops_api.rulepacks import _ERROR_RESPONSES
from pydantic import BaseModel, ConfigDict, Field


class EvaluationMetricDTO(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    name: str
    value: float | None
    denominator: Annotated[int, Field(ge=0)]


class EvaluationDTO(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    evaluation_id: UUID
    dataset_id: str
    split: Literal["development", "validation", "holdout"]
    status: Literal["queued", "completed", "failed"]
    metrics: list[EvaluationMetricDTO]
    report_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None


def build_evaluations_router(
    store: LocalEvaluationStore,
    auth_store: AuthStore,
    *,
    clock: Callable[[], float] = time.time,
) -> APIRouter:
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
        "/v1/evaluations/{evaluation_id}",
        operation_id="evaluation_get",
        response_model=EvaluationDTO,
        openapi_extra={
            "x-minimum-role": "admin",
            "x-rate-limit": "120/min/user",
            "x-idempotency-required": False,
        },
    )
    def evaluation_get(request: Request, evaluation_id: UUID) -> JSONResponse:
        now = clock()
        auth = _authorize(request, auth_store, now, "admin")
        if isinstance(auth, JSONResponse):
            return auth
        limited = _request_limit_response(
            auth_store,
            user_sub=auth.user_sub,
            operation_id="evaluation_get",
            requests=READ_REQUESTS_PER_MINUTE,
            now=now,
        )
        if limited is not None:
            return limited
        try:
            report = store.get(auth.tenant_id, str(evaluation_id))
        except EvaluationNotFound:
            return _error_response(404, "RESOURCE_NOT_FOUND", "evaluation not found")
        except EvaluationCorrupt:
            return _error_response(
                409,
                "EVALUATION_CORRUPT",
                "Evaluation artifact failed integrity verification.",
            )
        body = EvaluationDTO.model_validate(
            {
                "evaluation_id": evaluation_id,
                "dataset_id": report["dataset_id"],
                "split": report["split"],
                "status": report["status"],
                "metrics": [
                    {
                        "name": metric["name"],
                        "value": metric["value"],
                        "denominator": metric["denominator"],
                    }
                    for metric in report["metrics"]
                ],
                "report_sha256": report["report_sha256"],
            }
        ).model_dump(mode="json")
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    return router
