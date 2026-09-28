"""Composition wiring for the API service and session/tenant auth store.

Reads process config at the boundary and builds the explicit composition.
Non-local environments fail closed instead of serving fake results: the
in-memory auth_store is local-only, matching every other local
adapter. Real Cognito + DynamoDB-backed session/membership stores are not
wired here; APP_ENV=staging/production already fails closed in
proofops.composition.build_composition before this module's auth wiring
would matter.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from proofops.adapters.local.analysis_store import LocalAnalysisStore
from proofops.adapters.local.assurance_store import LocalAssuranceStore
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.classification_store import LocalSQLiteClassificationStore
from proofops.adapters.local.comparison_store import LocalComparisonStore
from proofops.adapters.local.evaluation_store import LocalEvaluationStore
from proofops.adapters.local.export_store import LocalExportStore
from proofops.adapters.local.reconciliation_store import LocalReconciliationStore
from proofops.adapters.local.rescore_store import LocalSQLiteRescoreStore
from proofops.adapters.local.retention_store import LocalRetentionStore
from proofops.adapters.local.review_store import LocalSQLiteReviewStore
from proofops.adapters.local.rulepack_store import RulePackSqliteStore
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.adapters.local.source_condition_review import LocalSourceConditionReview
from proofops.adapters.local.summary_store import LocalSummaryStore
from proofops.adapters.local.tag_store import LocalTagStore
from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
from proofops.application.exports import ExportService
from proofops.application.registry import Registry, RulePackChoice
from proofops.application.rescores import RescoreService
from proofops.application.reviews import ReviewService
from proofops.application.runs import RunService
from proofops.application.uploads import UploadService
from proofops.composition import build_composition as build_proofops_composition

from proofops_api.local_runtime import load_local_runtime

API_VERSION = "0.0.0"


@dataclass(frozen=True, slots=True)
class ApiComposition:
    """Wraps the proofops-level composition with API-owned local adapters."""

    proofops: Any
    auth_store: Any
    registry: Registry
    rulepack_store: RulePackSqliteStore
    uploads: UploadService
    runs: RunService
    parser: OpenDataLoaderParser
    claims: LocalClaimStore
    tags: LocalTagStore
    reviews: ReviewService
    classifications: LocalSQLiteClassificationStore
    source_conditions: LocalSourceConditionReview
    rescores: RescoreService
    summaries: LocalSummaryStore
    evaluations: LocalEvaluationStore
    analysis: LocalAnalysisStore
    assurance: LocalAssuranceStore
    exports: ExportService
    retention: LocalRetentionStore
    comparisons: LocalComparisonStore
    reconciliation: LocalReconciliationStore
    app_origin: str | None
    allowed_processing_regions: tuple[str, ...]

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - passthrough
        return getattr(self.proofops, name)


def _build_local_auth_store() -> Any:
    from proofops.adapters.local.auth_store import (
        InMemoryMembershipStore,
        InMemorySessionStore,
    )

    from proofops_api.auth import AuthStore

    return AuthStore(sessions=InMemorySessionStore(), memberships=InMemoryMembershipStore())


def build_composition() -> ApiComposition:
    proofops_composition = build_proofops_composition(
        app_env=os.environ.get("APP_ENV", "local"),
        model_adapter=os.environ.get("MODEL_ADAPTER", "synthetic"),
    )
    # build_proofops_composition already fails closed for non-local app_env
    # before this line is reached, so the local-only auth_store below is
    # only ever constructed when app_env == "local".
    auth_store = _build_local_auth_store()
    database_path = Path(os.environ.get("LOCAL_DATABASE_PATH", ".local/state.sqlite3"))
    database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    rulepack_store = RulePackSqliteStore(database_path)

    def active_rule_packs(tenant_id: str) -> tuple[RulePackChoice, ...]:
        return tuple(
            RulePackChoice(
                pack.rule_pack_id,
                pack.tenant_id,
                pack.version,
                pack.sha256,
                pack.status,
                pack.mode,
                pack.effective_date,
                pack.unresolved_gap_ids,
            )
            for pack in rulepack_store.list_active_packs(tenant_id)
        )

    registry = Registry.sqlite(database_path, active_rule_packs=active_rule_packs)
    uploads = UploadService(database_path, database_path.parent / "objects", registry)
    allowed_regions = tuple(
        region.strip()
        for region in os.environ.get("MODEL_ALLOWED_PROCESSING_REGIONS", "").split(",")
        if region.strip()
    )
    runs = RunService(
        LocalSQLiteRunStore(database_path, rulepacks=rulepack_store),
        uploads,
        registry,
        allowed_regions=allowed_regions,
        **load_local_runtime(os.environ),
    )
    parser = OpenDataLoaderParser(database_path.parent / "parser-prepared")
    tags = LocalTagStore(runs.store, uploads, parser)
    claims = LocalClaimStore(runs.store, uploads, parser)
    assurance = LocalAssuranceStore(runs.store, uploads, parser)
    return ApiComposition(
        proofops=proofops_composition,
        auth_store=auth_store,
        registry=registry,
        rulepack_store=rulepack_store,
        uploads=uploads,
        runs=runs,
        parser=parser,
        claims=claims,
        tags=tags,
        classifications=LocalSQLiteClassificationStore(runs.store, uploads, parser, tags, claims),
        reviews=ReviewService(
            LocalSQLiteReviewStore(runs.store.jobs),
            load_inputs=tags.load_inputs,
            verify_context_sources=tags.verify_context_sources,
            load_run_snapshot=runs.store.snapshot,
        ),
        source_conditions=LocalSourceConditionReview(runs.store, uploads, parser),
        rescores=RescoreService(
            LocalSQLiteRescoreStore(runs.store),
            load_inputs=tags.load_inputs,
            verify_context_sources=tags.verify_context_sources,
        ),
        summaries=LocalSummaryStore(runs.store, claims),
        evaluations=LocalEvaluationStore(database_path),
        analysis=LocalAnalysisStore(runs.store, uploads, parser, claims, tags, assurance=assurance),
        assurance=assurance,
        exports=ExportService(LocalExportStore(runs.store, claims)),
        retention=LocalRetentionStore(uploads),
        comparisons=LocalComparisonStore(
            runs.store,
            uploads,
            claims,
            enabled=os.environ.get("ENABLE_YEAR_COMPARISON", "false") == "true",
        ),
        app_origin=os.environ.get("APP_ORIGIN"),
        reconciliation=LocalReconciliationStore(
            database_path,
            database_path.parent / "reconciliation-artifacts",
            run_store=runs.store,
            claims=claims,
            tags=tags,
        ),
        allowed_processing_regions=allowed_regions,
    )
