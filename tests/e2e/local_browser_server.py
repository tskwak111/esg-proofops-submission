"""Test-only same-origin browser harness for the composed local API and built web UI."""

from __future__ import annotations

import argparse
import io
import json
import os
import secrets
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "22222222-2222-4222-8222-222222222222"
USER = "local-browser-fixture-user"
RIGHTS = "33333333-3333-4333-8333-333333333333"


def build_app(
    *,
    database: Path,
    dist: Path,
    origin: str,
    include_prior_version: bool = False,
    include_reconciliation: bool = False,
):
    review_fixture: dict[str, Any] = {}
    os.environ.update(
        APP_ENV="local",
        MODEL_ADAPTER="synthetic",
        APP_ORIGIN=origin,
        LOCAL_DATABASE_PATH=str(database),
        MODEL_ALLOWED_PROCESSING_REGIONS="ap-northeast-2",
    )
    import proofops_api.composition as composition_module
    import proofops_api.preflight as preflight_module
    from proofops.adapters.local.auth_store import hash_token, new_session_id
    from proofops.application.authorization import MembershipRecord, SessionRecord
    from proofops.application.budget import BudgetLimits, RoleLimit
    from proofops.application.ingest.graph_fusion import ParserProfile
    from proofops.application.registry import artifact_sha256
    from proofops.application.supply_chain import SupplyChainResult
    from proofops.application.telemetry import Telemetry
    from proofops_agent.extraction import SyntheticClaimExtractor
    from proofops_worker.extract_runner import LocalExtractRunner
    from proofops_worker.local_runner import LocalParserRunner

    from tests.acceptance.test_parsing import JAVA, MANIFEST
    from tests.acceptance.test_preflight import binding, consent
    from tests.acceptance.test_rulepack_api import GAP_IDS
    from tests.acceptance.test_rules import pack as full_pack

    base_composition = composition_module.build_composition
    base_preflight_router = preflight_module.build_preflight_router
    parser_profile = ParserProfile(MANIFEST, java_executable=JAVA)
    limits = BudgetLimits(
        100000,
        100000,
        (RoleLimit("tagger", 3, 50000, 50000, 100000),),
    )
    supply_chain = SupplyChainResult(True)

    def fixture_composition():
        from proofops.adapters.local.analysis_store import LocalAnalysisStore
        from proofops.adapters.local.claim_store import LocalClaimStore
        from proofops.adapters.local.comparison_store import LocalComparisonStore
        from proofops.adapters.local.export_store import LocalExportStore
        from proofops.adapters.local.reconciliation_store import LocalReconciliationStore
        from proofops.adapters.local.rescore_store import LocalSQLiteRescoreStore
        from proofops.adapters.local.review_store import LocalSQLiteReviewStore
        from proofops.adapters.local.summary_store import LocalSummaryStore
        from proofops.adapters.local.tag_store import LocalTagStore
        from proofops.application.exports import ExportService
        from proofops.application.ports.models import ModelBinding
        from proofops.application.rescores import RescoreService
        from proofops.application.reviews import ReviewService
        from proofops.application.rulepacks import RulePackRecord
        from proofops.application.tagging.service import TaggingSettings

        from tests.integration.test_local_tag_runner import SyntheticVerifiedParser

        composition = base_composition()
        full = full_pack()
        pack = RulePackRecord(
            **{
                key: value
                for key, value in (
                    asdict(full)
                    | {
                        "status": "validated",
                        "approved_by": "local-browser-fixture",
                        "approved_at": "2026-09-08T10:00:00Z",
                    }
                ).items()
                if key != "content"
            }
        )
        composition.rulepack_store.add_pack(
            pack, {path: full.file_content(path) for path in full.files}
        )
        composition.rulepack_store.activate(
            tenant_id=TENANT,
            rule_pack_id=pack.rule_pack_id,
            expected_revision=1,
            idempotency_key="local-browser-rulepack-activation",
            actor="local-browser-fixture",
            reason="Synthetic browser fixture approval",
            gap_ids=GAP_IDS,
            now=time.time(),
        )
        runtime = binding()
        approved_consent = consent()
        approved_at = datetime.fromtimestamp(int(time.time()) - 1, UTC).isoformat()
        runtime.update(approved_at=approved_at, checked_at=approved_at)
        approved_consent["approved_at"] = approved_at
        approved_consent["allowed_document_rights"] = [RIGHTS]
        for kind, artifact, identifier, name in (
            (
                "rights",
                {
                    "tenant_id": TENANT,
                    "rights_profile_id": RIGHTS,
                    "status": "approved",
                    "version": "1",
                },
                RIGHTS,
                "합성 공개 보고서 사용 승인",
            ),
            (
                "consent",
                approved_consent,
                approved_consent["consent_profile_id"],
                "합성 문서 처리 동의",
            ),
            ("runtime", runtime, runtime["runtime_binding_id"], "합성 로컬 실행 환경"),
        ):
            composition.registry.with_option(
                TENANT,
                kind,
                identifier,
                name,
                status="approved",
                version=str(artifact["version"]),
                artifact=artifact,
                sha256=artifact_sha256(artifact),
                approved_by="local-browser-fixture",
                approved_at=approved_at,
                local_synthetic=True,
            )
        composition.registry.with_enabled_mode(TENANT, "disclosure")
        composition.runs.parser_profile_hash = parser_profile.config_hash()
        composition.runs.parser_profile = parser_profile.config_snapshot()
        composition.runs.extraction_profile = SyntheticClaimExtractor.profile
        composition.runs.extraction_mode = "local_synthetic"
        composition.runs.budget_limits = limits
        composition.runs.build_result = supply_chain
        composition.runs.tagging_settings = TaggingSettings(
            ModelBinding(runtime["runtime_binding_id"], "tagger", True),
            runtime["model_id"],
            "local-synthetic-unknown-v1",
            runtime["endpoint_region"],
            "Explicit synthetic tags only",
            (ROOT / "contracts/jsonschema/llm_tags.schema.json").read_text(),
            max_tokens=min(4000, runtime["max_output_tokens"]),
        )
        composition.runs.tagging_mode = "local_synthetic"

        primary = composition.parser
        synthetic = SyntheticVerifiedParser(primary.artifact_root)

        class FixtureParser:
            artifact_root = primary.artifact_root
            synthetic_fixture = synthetic

            def parse(self, *args, **kwargs):
                return primary.parse(*args, **kwargs)

            def load_verified(self, source, profile, *, tenant_id, manifest_sha256=None):
                manifest = (
                    self.artifact_root
                    / tenant_id
                    / source.document_version_id
                    / profile.parse_manifest_id
                    / "manifest.json"
                )
                if json.loads(manifest.read_bytes()).get("synthetic") is True:
                    return synthetic.load_verified(
                        source, profile, tenant_id=tenant_id, manifest_sha256=manifest_sha256
                    )
                return primary.load_verified(
                    source, profile, tenant_id=tenant_id, manifest_sha256=manifest_sha256
                )

        fixture_parser = FixtureParser()
        tags = LocalTagStore(composition.runs.store, composition.uploads, fixture_parser)
        claims = LocalClaimStore(composition.runs.store, composition.uploads, fixture_parser)
        object.__setattr__(composition, "parser", fixture_parser)
        object.__setattr__(composition, "claims", claims)
        object.__setattr__(composition, "tags", tags)
        object.__setattr__(
            composition,
            "reconciliation",
            LocalReconciliationStore(
                database,
                database.parent / "reconciliation-artifacts",
                run_store=composition.runs.store,
                claims=claims,
                tags=tags,
            ),
        )
        object.__setattr__(
            composition, "summaries", LocalSummaryStore(composition.runs.store, claims)
        )
        object.__setattr__(
            composition, "exports", ExportService(LocalExportStore(composition.runs.store, claims))
        )
        object.__setattr__(
            composition,
            "comparisons",
            LocalComparisonStore(
                composition.runs.store,
                composition.uploads,
                claims,
                enabled=composition.comparisons.enabled,
            ),
        )
        object.__setattr__(
            composition,
            "analysis",
            LocalAnalysisStore(
                composition.runs.store, composition.uploads, fixture_parser, claims, tags
            ),
        )
        object.__setattr__(
            composition,
            "rescores",
            RescoreService(
                LocalSQLiteRescoreStore(composition.runs.store), load_inputs=tags.load_inputs
            ),
        )
        object.__setattr__(
            composition,
            "reviews",
            ReviewService(
                LocalSQLiteReviewStore(composition.runs.store.jobs),
                load_inputs=tags.load_inputs,
            ),
        )
        return composition

    def fixture_preflight_router(*args, **kwargs):
        kwargs["build_result"] = supply_chain
        return base_preflight_router(*args, **kwargs)

    composition_module.build_composition = fixture_composition
    preflight_module.build_preflight_router = fixture_preflight_router
    from proofops_api.auth import SESSION_COOKIE_NAME, _error_response, _verify_csrf
    from proofops_api.main import app
    from proofops_api.routers.registry import _authorize

    composition = app.state.composition
    composition.auth_store.memberships.put(MembershipRecord(TENANT, USER, "editor", "active"))
    composition.auth_store.memberships.put(MembershipRecord(FOREIGN, USER, "admin", "active"))
    company = composition.registry.create_company(
        actor=USER,
        tenant_id=TENANT,
        legal_name="합성 브라우저 검증 기업",
        idempotency_key="local-browser-company-seed",
    )

    # Separate verified synthetic fixture: same durable upload/run/checkpoint stores as the app.
    from proofops.application.authorization import AuthContext
    from proofops.application.evidence.binding import ClaimContext
    from proofops.application.preflight import check_runtime_binding, combine_build_checks
    from proofops.application.tagging.tracks import TrackCandidate
    from proofops_agent.synthetic_tagging import SyntheticTaggingTransport
    from proofops_worker.tag_runner import LocalTagRunner

    from tests.acceptance.test_parsing import pdf

    content = pdf()
    document = composition.uploads.create_document(
        TENANT,
        {
            "company_id": company.company_id,
            "title": "검증된 합성 브라우저 리뷰",
            "document_type": "sustainability_report",
        },
        "local-browser-review-document",
    )
    metadata = {
        "filename": "synthetic-review.pdf",
        "size_bytes": len(content),
        "sha256": sha256(content).hexdigest(),
        "report_year": 2025,
        "industry_system": "unknown",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "rights_profile_id": RIGHTS,
    }
    ticket = composition.uploads.initiate_upload(
        TENANT, document["document_id"], metadata, "local-browser-review-version"
    )
    composition.uploads.receive_content(TENANT, ticket["upload_id"], content, "application/pdf")
    version = composition.uploads.complete_upload(
        TENANT,
        ticket["upload_id"],
        {"sha256": metadata["sha256"], "size_bytes": len(content)},
        "local-browser-review-complete",
    )
    if include_prior_version:
        prior_metadata = metadata | {
            "report_year": 2024,
            "period_start": "2024-01-01",
            "period_end": "2024-12-31",
        }
        prior_ticket = composition.uploads.initiate_upload(
            TENANT, document["document_id"], prior_metadata, "local-browser-prior-version"
        )
        composition.uploads.receive_content(
            TENANT, prior_ticket["upload_id"], content, "application/pdf"
        )
        prior_version = composition.uploads.complete_upload(
            TENANT,
            prior_ticket["upload_id"],
            {"sha256": metadata["sha256"], "size_bytes": len(content)},
            "local-browser-prior-complete",
        )
        review_fixture["prior_document_version_id"] = prior_version["version_id"]
    fixture_auth = AuthContext(
        USER,
        TENANT,
        "admin",
        frozenset({"viewer", "editor", "reviewer", "admin"}),
        "local-browser-fixture-session",
    )
    pack = full_pack()
    runtime = binding()
    approved_consent = consent()
    stored_runtime = composition.registry.resolve_profile(
        fixture_auth, "runtime", runtime["runtime_binding_id"]
    )
    stored_consent = composition.registry.resolve_profile(
        fixture_auth, "consent", approved_consent["consent_profile_id"]
    )
    readiness = combine_build_checks(
        check_runtime_binding(
            binding=stored_runtime,
            consent=stored_consent,
            auth=fixture_auth,
            allowed_regions=composition.runs.allowed_regions,
            checked_at=datetime.now(UTC).isoformat(),
        ),
        composition.runs.build_result,
    )
    assert readiness.ready and RIGHTS in stored_consent["allowed_document_rights"], readiness
    created = composition.runs.create(
        fixture_auth,
        {
            "document_version_id": version["version_id"],
            "mode": "disclosure",
            "scope": "full",
            "rule_pack_id": pack.rule_pack_id,
            "consent_profile_id": approved_consent["consent_profile_id"],
            "runtime_binding_id": runtime["runtime_binding_id"],
        },
        "local-browser-review-run",
    )
    run_id = created["run_id"]
    telemetry = Telemetry(service="worker", env="test", stream=io.StringIO(), hash_key=b"x" * 32)
    profile = ParserProfile(str(uuid4()), **dict(composition.runs.parser_profile))
    synthetic_parser = composition.parser.synthetic_fixture
    assert (
        LocalParserRunner(
            composition.runs.store,
            composition.uploads,
            synthetic_parser,
            profile=profile,
            telemetry=telemetry,
        ).run_once(tenant_id=TENANT, run_id=run_id)
        == "committed"
    )
    assert (
        LocalExtractRunner(
            composition.runs.store,
            composition.uploads,
            synthetic_parser,
            extractor=SyntheticClaimExtractor(),
            telemetry=telemetry,
        ).run_once(tenant_id=TENANT, run_id=run_id)
        == "committed"
    )

    def preliminary(claim, graph):
        return TrackCandidate(claim, "performance", None), ClaimContext(claim, {}), {}

    tagger = LocalTagRunner(
        composition.runs.store,
        composition.uploads,
        synthetic_parser,
        telemetry=telemetry,
        transport=SyntheticTaggingTransport(),
        preliminary=preliminary,
    )
    tag_result = tagger.run_once(tenant_id=TENANT, run_id=run_id)
    assert tag_result == "needs_review", tag_result
    with composition.runs.store.jobs._transaction() as connection:
        review = composition.runs.store.jobs._all(connection, TENANT, run_id, "review_head")[0]
    review_fixture.update(
        run_id=run_id,
        claim_id=review["claim_id"],
        review_id=review["review_id"],
        document_version_id=version["version_id"],
    )
    if include_reconciliation:
        from tests.e2e.reconciliation_fixture import seed_reconciliation_cases

        cases = seed_reconciliation_cases(
            composition, fixture_auth, run_id, review["claim_id"], database.parent / "drafts"
        )
        review_fixture["reconciliation_case_ids"] = [case["case_id"] for case in cases]

    @app.get("/__e2e/login", include_in_schema=False)
    def fixture_login() -> RedirectResponse:
        session_id = new_session_id()
        csrf_token = secrets.token_urlsafe(32)
        deadline = time.time() + 3600
        composition.auth_store.sessions.put_with_token(
            SessionRecord(
                session_id=session_id,
                user_sub=USER,
                active_tenant_id=TENANT,
                csrf_hash=hash_token(csrf_token),
                expires_at=deadline,
                idle_deadline=deadline,
                revoked=False,
            ),
            csrf_token,
        )
        composition.auth_store.memberships.put(MembershipRecord(TENANT, USER, "admin", "active"))
        response = RedirectResponse("/documents/new", status_code=303)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            session_id,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    @app.post("/__e2e/runs/{run_id}/advance", include_in_schema=False)
    async def fixture_advance(request: Request, run_id: str) -> JSONResponse:
        auth = _authorize(request, composition.auth_store, time.time(), "admin")
        if isinstance(auth, JSONResponse):
            return auth
        session = composition.auth_store.sessions.get(auth.session_id)
        if session is None or not _verify_csrf(request, session.csrf_hash, allowed_origin=origin):
            return _error_response(403, "CSRF_INVALID", "invalid fixture request")
        try:
            payload = await request.json()
        except ValueError:
            return _error_response(422, "VALIDATION_ERROR", "invalid fixture request")
        stage = payload.get("stage") if isinstance(payload, dict) else None
        telemetry = Telemetry(
            service="worker", env="test", stream=io.StringIO(), hash_key=b"x" * 32
        )
        if stage == "parse":
            result = LocalParserRunner(
                composition.runs.store,
                composition.uploads,
                composition.parser,
                profile=parser_profile,
                telemetry=telemetry,
            ).run_once(tenant_id=TENANT, run_id=run_id)
        elif stage == "extract":
            result = LocalExtractRunner(
                composition.runs.store,
                composition.uploads,
                composition.parser,
                extractor=SyntheticClaimExtractor(),
                telemetry=telemetry,
            ).run_once(tenant_id=TENANT, run_id=run_id)
        else:
            return _error_response(422, "VALIDATION_ERROR", "unsupported fixture stage")
        return JSONResponse({"result": result, "run": composition.runs.get(TENANT, run_id)})

    @app.get("/__e2e/state/{version_id}", include_in_schema=False)
    def fixture_state(version_id: str) -> JSONResponse:
        snapshot = composition.uploads.version_snapshot(TENANT, version_id)
        content = composition.uploads.read_original(TENANT, version_id)
        document = composition.uploads.get_document(TENANT, snapshot["document_id"])
        return JSONResponse(
            {
                "version_id": version_id,
                "stored_sha256": sha256(content).hexdigest(),
                "stored_size_bytes": len(content),
                "snapshot_sha256": snapshot["sha256"],
                "page_count": snapshot["page_count"],
                "document_revision": document["revision"],
                "local_synthetic": snapshot["local_synthetic"],
            }
        )

    @app.get("/__e2e/review-fixture", include_in_schema=False)
    def fixture_review() -> JSONResponse:
        return JSONResponse(review_fixture | {"local_synthetic": True})

    @app.get("/documents/new", include_in_schema=False)
    def documents_new() -> FileResponse:
        return FileResponse(dist / "index.html")

    @app.get("/runs/{path:path}", include_in_schema=False)
    def run_page(path: str) -> FileResponse:
        return FileResponse(dist / "index.html")

    app.mount("/", StaticFiles(directory=dist, html=True), name="web")
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=4190)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--origin")
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--dist", type=Path, default=ROOT / "apps/web/dist")
    parser.add_argument("--include-prior-version", action="store_true")
    parser.add_argument("--include-reconciliation", action="store_true")
    args = parser.parse_args()
    display_host = f"[{args.host}]" if ":" in args.host else args.host
    origin = args.origin or f"http://{display_host}:{args.port}"
    import uvicorn

    uvicorn.run(
        build_app(
            database=args.database,
            dist=args.dist,
            origin=origin,
            include_prior_version=args.include_prior_version,
            include_reconciliation=args.include_reconciliation,
        ),
        host=args.host,
        port=args.port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
