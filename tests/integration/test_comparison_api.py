"""TASK-023 local-synthetic API integration; no model, AWS, or customer data."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace
from hashlib import sha256
from io import StringIO
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.comparison_store import LocalComparisonStore
from proofops.adapters.local.retention_store import LocalRetentionStore
from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.application.authorization import AuthContext, MembershipRecord
from proofops.application.evidence.citations import verify_source_ref
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot
from proofops.domain.rules.engine import RuleContext, evaluate
from proofops_api.auth import AuthStore
from proofops_api.routers.comparisons import build_comparisons_router

from tests.acceptance.test_rulepack_api import _seed_auth
from tests.acceptance.test_upload import FOREIGN, TENANT
from tests.integration.test_local_tag_runner import verified_setup
from tests.integration.test_run_lifecycle import validate

ORIGIN = "https://testserver"


def upload_version(service, base, data: bytes, year: int, *, document_id=None) -> str:
    metadata = dict(
        base["metadata"],
        filename=f"synthetic-{year}.pdf",
        size_bytes=len(data),
        sha256=sha256(data).hexdigest(),
        report_year=year,
        period_start=f"{year}-01-01",
        period_end=f"{year}-12-31",
    )
    ticket = service.uploads.initiate_upload(
        TENANT, document_id or base["document_id"], metadata, str(uuid4())
    )
    service.uploads.receive_content(TENANT, ticket["upload_id"], data, "application/pdf")
    return service.uploads.complete_upload(
        TENANT,
        ticket["upload_id"],
        {"sha256": metadata["sha256"], "size_bytes": len(data)},
        str(uuid4()),
    )["version_id"]


def create_run(service, prior_run_id: str, version_id: str) -> str:
    snapshot = service.store.snapshot(TENANT, prior_run_id)
    body = {
        "document_version_id": version_id,
        "mode": snapshot["mode"],
        "scope": snapshot["scope"],
        "rule_pack_id": snapshot["rulepack"]["rule_pack_id"],
        "consent_profile_id": snapshot["consent"]["consent_profile_id"],
        "runtime_binding_id": snapshot["runtime"]["runtime_binding_id"],
    }
    from tests.integration.test_run_lifecycle import client

    http, _ = client(service)
    response = http.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def publish_claims(service, parser, run_id: str, now: list[int]) -> LocalClaimStore:
    from proofops.application.ingest.graph_fusion import ParserProfile
    from proofops.application.telemetry import Telemetry
    from proofops_agent.extraction import SyntheticClaimExtractor
    from proofops_worker.extract_runner import LocalExtractRunner
    from proofops_worker.local_runner import LocalParserRunner

    telemetry = Telemetry(service="worker", env="test", stream=StringIO(), hash_key=b"x" * 32)
    parse = LocalParserRunner(
        service.store,
        service.uploads,
        parser,
        profile=ParserProfile(str(uuid4()), **service.parser_profile),
        telemetry=telemetry,
        clock=lambda: now[0],
    )
    assert parse.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    extract = LocalExtractRunner(
        service.store,
        service.uploads,
        parser,
        extractor=SyntheticClaimExtractor(),
        telemetry=telemetry,
        clock=lambda: now[0],
    )
    assert extract.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    return extract.claims


def approve_targets(service, claims: LocalClaimStore, run_id: str, grade: str) -> None:
    from tests.acceptance.test_rules import inputs

    discovered = claims.list(TENANT, run_id)
    snapshot = service.store.snapshot(TENANT, run_id)
    graph = load_run_graph(
        service.store, service.uploads, claims.parser, tenant_id=TENANT, run_id=run_id
    )
    rulepack = RulePackSnapshot(**snapshot["rulepack"])
    assert discovered
    with service.store.jobs._transaction() as db:
        run = service.store.jobs._get(db, TENANT, run_id, "run", "META")
        for claim in discovered:
            refs = tuple(
                verify_source_ref(ref, graph, tenant_id=TENANT) for ref in claim.source_refs
            )
            assert refs and all(ref.verification_state == "verified" for ref in refs)
            changes = {"scope": "absent"} if grade == "E1" else {}
            base, _ = inputs("goal", **changes)
            packet_sha256 = canonical_hash(
                {"claim_id": claim.claim_id, "source_refs": [asdict(ref) for ref in refs]}
            )
            tags = replace(
                base,
                tenant_id=TENANT,
                document_version_id=claim.document_version_id,
                claim_id=claim.claim_id,
                packet_sha256=packet_sha256,
                ontology_version=rulepack.ontology_version,
                facts=tuple(
                    replace(
                        fact,
                        evidence_refs=refs,
                        source_tenant_id=TENANT,
                    )
                    if fact.state == "present"
                    else fact
                    for fact in base.facts
                ),
            )
            context = RuleContext(
                TENANT,
                claim.document_version_id,
                claim.claim_id,
                packet_sha256,
                local_synthetic=True,
                company_id=snapshot["document"]["company"]["company_id"],
            )
            ruled = evaluate(tags, context, rulepack)
            assert ruled.evidence_grade == grade
            tag = {
                "tag_revision": 1,
                "confirmed_tags": asdict(tags),
                "origin": "human",
                "execution_profile": "local-synthetic-only",
            }
            decision = {
                "decision_revision": 1,
                "decision": asdict(ruled),
                "api": ruled.to_api_dict() | {"review_status": "human_confirmed"},
            }
            service.store.jobs._put(
                db,
                TENANT,
                run_id,
                "tag_revision",
                f"{claim.claim_id}:0000000001",
                tag,
                immutable=True,
            )
            service.store.jobs._put(
                db,
                TENANT,
                run_id,
                "decision_revision",
                f"{claim.claim_id}:0000000001",
                decision,
                immutable=True,
            )
            service.store.jobs._put(
                db,
                TENANT,
                run_id,
                "claim_head",
                claim.claim_id,
                {"tag_revision": 1, "decision_revision": 1},
            )
        service.store.jobs._bump_run(db, run)


def api(store: LocalComparisonStore, *, role: str = "editor"):
    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    token = _seed_auth(auth, role=role)
    app = FastAPI()
    app.include_router(
        build_comparisons_router(store, auth, allowed_origin=ORIGIN, clock=lambda: 1)
    )
    http = TestClient(app, base_url=ORIGIN)
    http.cookies.set("__Host-proofops_session", "admin-session")
    http.headers.update(
        {"Origin": ORIGIN, "X-CSRF-Token": token, "Idempotency-Key": "comparison-key-0001"}
    )
    return http, auth


def setup_comparison(tmp_path, monkeypatch, *, approve=True):
    service, prior_run_id, prior_runner, now, _ = verified_setup(tmp_path, monkeypatch)
    prior_version_id = service.store.jobs.get_run(TENANT, prior_run_id)["document_version_id"]
    prior_version = service.uploads.version_snapshot(TENANT, prior_version_id)
    original = service.uploads.read_original(TENANT, prior_version_id)
    current_data = original.replace(b"1234", b"2234")
    assert current_data != original
    prior_document = service.uploads.get_document(TENANT, prior_version["document_id"])
    current_document = service.uploads.create_document(
        TENANT,
        {
            "company_id": prior_document["company_id"],
            "title": "Synthetic current-year report",
            "document_type": prior_document["document_type"],
        },
        str(uuid4()),
    )
    current_version_id = upload_version(
        service,
        prior_version,
        current_data,
        2026,
        document_id=current_document["document_id"],
    )
    current_run_id = create_run(service, prior_run_id, current_version_id)
    current_claims = publish_claims(service, prior_runner.parser, current_run_id, now)
    prior_claims = LocalClaimStore(service.store, service.uploads, prior_runner.parser)
    if approve:
        approve_targets(service, prior_claims, prior_run_id, "E3")
        approve_targets(service, current_claims, current_run_id, "E1")
    store = LocalComparisonStore(service.store, service.uploads, current_claims, enabled=True)
    return service, store, current_run_id, current_version_id, prior_version_id


def test_api_compares_published_versions_without_mutating_current_grade(tmp_path, monkeypatch):
    service, store, run_id, current_version_id, prior_version_id = setup_comparison(
        tmp_path, monkeypatch
    )
    http, auth = api(store)
    current_claim = store.claims.list(TENANT, run_id)[0]
    before = store.claims.current_tag(TENANT, run_id, current_claim.claim_id)

    created = http.post(
        f"/v1/runs/{run_id}/comparisons",
        json={"prior_document_version_id": prior_version_id},
    )
    assert created.status_code == 202, created.text
    validate("JobAccepted", created.json())
    assert created.json()["status"] == "ready"
    result = http.get(created.json()["status_url"])
    assert result.status_code == 200 and result.headers["cache-control"] == "no-store"
    validate("Comparison", result.json())
    assert result.json()["status"] == "completed"
    assert result.json()["changes"][0]["type"] == "modified"
    assert not ({"evidence_grade", "label"} & set(result.json()))
    assert store.claims.current_tag(TENANT, run_id, current_claim.claim_id) == before
    assert before["decision"]["evidence_grade"] == "E1"
    assert service.uploads.version_snapshot(TENANT, current_version_id)["report_year"] == 2026

    replay = http.post(
        f"/v1/runs/{run_id}/comparisons",
        json={"prior_document_version_id": prior_version_id},
    )
    assert replay.content == created.content
    conflict = http.post(
        f"/v1/runs/{run_id}/comparisons",
        json={"prior_document_version_id": current_version_id},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=FOREIGN))
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "editor", "active"))
    assert http.get(created.json()["status_url"]).status_code == 404


def test_missing_prior_artifact_is_immutable_not_run(tmp_path, monkeypatch):
    service, store, run_id, _, prior_version_id = setup_comparison(tmp_path, monkeypatch)
    base = service.uploads.version_snapshot(TENANT, prior_version_id)
    missing_artifact_version = upload_version(
        service, base, service.uploads.read_original(TENANT, prior_version_id), 2025
    )
    http, _ = api(store)

    created = http.post(
        f"/v1/runs/{run_id}/comparisons",
        json={"prior_document_version_id": missing_artifact_version},
    )
    result = http.get(created.json()["status_url"])

    assert created.status_code == 202 and result.status_code == 200
    assert result.json()["status"] == "not_run"
    assert result.json()["reason"] == "prior_comparison_artifact_missing"
    assert result.json()["changes"] == []
    with sqlite3.connect(service.store.path) as outside:
        with pytest.raises(sqlite3.IntegrityError):
            outside.execute(
                "UPDATE job_records SET value=? WHERE kind='comparison_receipt'",
                (json.dumps({"tampered": True}),),
            )
    with service.store.jobs._transaction() as db:
        assert service.store.jobs._all(db, TENANT, run_id, "comparison_receipt")


def test_missing_approved_snapshot_is_not_run_without_change_candidates(tmp_path, monkeypatch):
    _, store, run_id, _, prior_version_id = setup_comparison(tmp_path, monkeypatch, approve=False)
    http, _ = api(store)

    created = http.post(
        f"/v1/runs/{run_id}/comparisons",
        json={"prior_document_version_id": prior_version_id},
    )
    assert created.status_code == 202, created.text
    result = http.get(created.json()["status_url"])
    assert result.json() | {"comparison_id": None} == {
        "comparison_id": None,
        "status": "not_run",
        "reason": "approved_claim_snapshot_missing",
        "changes": [],
    }


@pytest.mark.parametrize("deleted", ["current", "prior"])
def test_deleted_source_hides_saved_comparison_and_idempotent_replay(
    tmp_path, monkeypatch, deleted
):
    service, store, run_id, current_version_id, prior_version_id = setup_comparison(
        tmp_path, monkeypatch
    )
    http, _ = api(store)
    url = f"/v1/runs/{run_id}/comparisons"
    body = {"prior_document_version_id": prior_version_id}
    created = http.post(url, json=body)
    assert created.status_code == 202, created.text

    version_id = current_version_id if deleted == "current" else prior_version_id
    document_id = service.uploads.version_snapshot(TENANT, version_id)["document_id"]
    actor = AuthContext(
        "synthetic-admin",
        TENANT,
        "admin",
        frozenset({"viewer", "editor", "reviewer", "admin"}),
        "synthetic-session",
    )
    LocalRetentionStore(service.uploads).request(
        actor,
        document_id,
        {"reason": "Explicit synthetic deletion request"},
        str(uuid4()),
        now=1,
    )

    assert http.get(created.json()["status_url"]).status_code == 404
    replay = http.post(url, json=body)
    assert replay.status_code == 404
    assert replay.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_default_gate_auth_csrf_foreign_resource_and_rate_limits(tmp_path, monkeypatch):
    service, store, run_id, _, prior_version_id = setup_comparison(tmp_path, monkeypatch)
    disabled = LocalComparisonStore(service.store, service.uploads, store.claims)
    http, _ = api(disabled)
    url = f"/v1/runs/{run_id}/comparisons"
    body = {"prior_document_version_id": prior_version_id}
    assert http.post(url, json=body).json()["error"]["code"] == "YEAR_COMPARISON_DISABLED"

    reviewer, _ = api(store, role="reviewer")
    assert reviewer.post(url, json=body).status_code == 403
    no_csrf, _ = api(store)
    assert no_csrf.post(url, json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    enabled, _ = api(store)
    foreign = enabled.post(url, json={"prior_document_version_id": str(uuid4())})
    assert foreign.status_code == 404

    writes, _ = api(store)
    assert all(writes.post(url, json=body).status_code == 202 for _ in range(10))
    assert writes.post(url, json=body).status_code == 429

    reads, _ = api(store, role="viewer")
    missing = f"/v1/comparisons/{uuid4()}"
    assert all(reads.get(missing).status_code == 404 for _ in range(120))
    assert reads.get(missing).status_code == 429
