"""Compose verified local API routes; unconfigured live adapters remain unavailable."""

from __future__ import annotations

import secrets
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from proofops.application.telemetry import Telemetry
from starlette.middleware.cors import CORSMiddleware

from proofops_api.auth import build_auth_router
from proofops_api.composition import API_VERSION, build_composition
from proofops_api.dto import Health
from proofops_api.middleware import BrowserSecurityHeadersMiddleware, LocalPdfRequestMiddleware
from proofops_api.oidc import build_oidc_router
from proofops_api.preflight import build_preflight_router
from proofops_api.routers.analysis import build_analysis_router
from proofops_api.routers.claims import build_claims_router
from proofops_api.routers.classification import build_classification_router
from proofops_api.routers.comparisons import build_comparisons_router
from proofops_api.routers.deletion import build_deletion_router
from proofops_api.routers.documents import build_documents_router
from proofops_api.routers.evaluations import build_evaluations_router
from proofops_api.routers.exports import build_exports_router
from proofops_api.routers.reconciliation import build_reconciliation_router
from proofops_api.routers.registry import build_registry_router
from proofops_api.routers.rescores import build_rescores_router
from proofops_api.routers.reviews import build_reviews_router
from proofops_api.routers.runs import build_runs_router
from proofops_api.routers.source_conditions import build_source_conditions_router
from proofops_api.routers.sources import build_sources_router
from proofops_api.routers.summaries import build_summaries_router
from proofops_api.rulepacks import build_rulepack_router
from proofops_api.session import build_session_router
from proofops_api.telemetry import TelemetryMiddleware, logging_config


def create_app() -> FastAPI:
    composition = build_composition()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            composition.uploads.close()
            composition.registry.close()

    app = FastAPI(title="ProofOps API", version=API_VERSION, lifespan=lifespan)
    app.add_middleware(LocalPdfRequestMiddleware)
    app.state.composition = composition
    app_origin = composition.app_origin
    app.include_router(build_oidc_router(composition.auth_store))
    app.include_router(build_auth_router(composition.auth_store, allowed_origin=app_origin))
    app.include_router(build_session_router(composition.auth_store, allowed_origin=app_origin))
    app.include_router(
        build_registry_router(
            composition.registry, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(
        build_documents_router(
            composition.uploads,
            composition.auth_store,
            allowed_origin=app_origin or "",
            app_env="local",
            model_adapter="synthetic",
        )
    )
    app.include_router(
        build_runs_router(composition.runs, composition.auth_store, allowed_origin=app_origin or "")
    )
    app.include_router(
        build_claims_router(
            composition.claims,
            composition.auth_store,
            tags=composition.tags,
            assurance=composition.assurance,
        )
    )
    app.include_router(
        build_reviews_router(
            composition.reviews,
            composition.auth_store,
            allowed_origin=app_origin or "",
            run_store=composition.runs.store,
        )
    )
    app.include_router(
        build_rescores_router(
            composition.rescores, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(build_summaries_router(composition.summaries, composition.auth_store))
    app.include_router(
        build_classification_router(
            composition.classifications, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(build_evaluations_router(composition.evaluations, composition.auth_store))
    app.include_router(build_analysis_router(composition.analysis, composition.auth_store))
    app.include_router(
        build_reconciliation_router(
            composition.reconciliation, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(
        build_source_conditions_router(
            composition.source_conditions, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(
        build_exports_router(
            composition.exports, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(
        build_deletion_router(
            composition.retention, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(
        build_comparisons_router(
            composition.comparisons, composition.auth_store, allowed_origin=app_origin or ""
        )
    )
    app.include_router(
        build_sources_router(
            composition.runs.store,
            composition.uploads,
            composition.parser,
            composition.auth_store,
            allowed_origin=app_origin,
        )
    )
    app.include_router(
        build_preflight_router(
            composition.auth_store,
            resolve_profile=composition.registry.resolve_profile,
            allowed_regions=composition.allowed_processing_regions,
            build_result=composition.runs.build_result,
            allowed_origin=app_origin or "",
        )
    )
    app.include_router(
        build_rulepack_router(
            composition.rulepack_store,
            composition.auth_store,
            gap_ids=tuple(f"GAP-{number:03}" for number in range(1, 11)),
            allowed_origin=app_origin or "",
        )
    )
    if app_origin:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=[app_origin],
            allow_credentials=True,
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "If-Match", "Idempotency-Key"],
            expose_headers=[
                "ETag",
                "Retry-After",
                "X-Page-Width-Pt",
                "X-Page-Height-Pt",
                "X-Source-Highlight",
            ],
        )
    app.add_middleware(BrowserSecurityHeadersMiddleware)
    app.add_middleware(
        TelemetryMiddleware,
        telemetry=Telemetry(
            service="api", env="local", stream=sys.stdout, hash_key=secrets.token_bytes(32)
        ),
    )

    @app.get("/v1/health/live", operation_id="liveness")
    def liveness() -> dict[str, object]:
        body = Health(status="ok", version=API_VERSION, checks=("baseline",))
        return body.model_dump(mode="json")

    @app.get("/v1/health/ready", operation_id="readiness")
    def readiness() -> dict[str, object]:
        body = Health(
            status="not_ready",
            version=API_VERSION,
            checks=("baseline:cloud-not-wired",),
        )
        return body.model_dump(mode="json")

    return app


app = create_app()


def main() -> None:
    import uvicorn

    uvicorn.run(
        "proofops_api.main:app",
        host="0.0.0.0",
        port=8000,
        access_log=False,
        log_config=logging_config(env="local"),
    )
