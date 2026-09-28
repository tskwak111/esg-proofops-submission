"""Real local parser/checkpoint projections for the three analysis GET routes."""

from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

from proofops.application.authorization import MembershipRecord

from tests.acceptance.test_upload import FOREIGN, TENANT
from tests.integration.test_local_extract_runner import extraction_setup
from tests.integration.test_local_parser_runner import runner_setup
from tests.integration.test_run_lifecycle import client, validate


def _http(service, runner, now, *, role="admin"):
    from proofops.adapters.local.analysis_store import LocalAnalysisStore
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops_api.routers.analysis import build_analysis_router

    claims = getattr(runner, "claims", None) or LocalClaimStore(
        service.store, service.uploads, runner.parser
    )
    http, auth = client(service, role=role)
    http.app.include_router(
        build_analysis_router(
            LocalAnalysisStore(
                service.store,
                service.uploads,
                runner.parser,
                claims,
                getattr(runner, "tags", None),
            ),
            auth,
            clock=lambda: now[0],
        )
    )
    return http, auth


def test_unpublished_parser_and_missing_or_untyped_runs_fail_closed(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    http, _ = _http(service, runner, now, role="viewer")

    assert http.get(f"/v1/runs/{run_id}/observations").status_code == 409
    assert http.get(f"/v1/runs/{uuid4()}/observations").status_code == 404
    assert http.get("/v1/runs/not-a-uuid/observations").status_code == 422


def test_actual_parser_and_extraction_publish_fixed_uncertain_analysis_pages(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    http, _ = _http(service, runner, now, role="viewer")
    base = f"/v1/runs/{run_id}"

    assert http.get(base + "/assurance").status_code == 409
    assert http.get(base + "/safe-harbor").status_code == 409
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    observations = http.get(base + "/observations")
    assert observations.status_code == 200, observations.text
    assert observations.headers["cache-control"] == "no-store"
    validate("ObservationPage", observations.json())
    assert observations.json()["items"]
    assert set(observations.json()) == {"items", "next_cursor", "snapshot_epoch"}
    assert all(
        set(item)
        == {
            "observation_id",
            "metric",
            "scope",
            "entity",
            "period",
            "value",
            "unit",
            "measurement_basis",
            "value_state",
            "evidence_refs",
        }
        for item in observations.json()["items"]
    )
    assert any(
        ref["verification_state"] == "candidate"
        for item in observations.json()["items"]
        for ref in item["evidence_refs"]
    )

    claims = runner.claims.list(TENANT, run_id)
    assurance = http.get(base + "/assurance")
    safe_harbor = http.get(base + "/safe-harbor")
    assert assurance.status_code == safe_harbor.status_code == 200
    assert assurance.headers["cache-control"] == safe_harbor.headers["cache-control"] == "no-store"
    validate("AssuranceMatchPage", assurance.json())
    validate("SafeHarborRecordPage", safe_harbor.json())
    assert len(assurance.json()["items"]) == len(safe_harbor.json()["items"]) == len(claims)
    assert all(
        item
        == {
            "status": "undetermined",
            "level": None,
            "provider": None,
            "statement_id": None,
            "metric_match": "unknown",
            "period_match": "unknown",
            "boundary_match": "unknown",
            "evidence_refs": [],
        }
        for item in assurance.json()["items"]
    )
    assert all(
        item
        == {
            "claim_id": item["claim_id"],
            "applicable": None,
            "category": None,
            "checklist": [],
            "reasonable_basis_documented": None,
            "legal_effect": "not_determined",
            "mapping_status": "unresolved",
            "gap_ids": ["GAP-002"],
        }
        for item in safe_harbor.json()["items"]
    )


def test_analysis_auth_pending_cursor_capacity_and_snapshot_survive_head_mutation(
    tmp_path, monkeypatch
):
    from proofops.adapters.local import analysis_store, catalog_pages

    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    http, auth = _http(service, runner, now)
    base = f"/v1/runs/{run_id}"
    assert http.get(base + "/observations").status_code == 200
    assert http.get(base + "/assurance").status_code == 409
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    normalize = analysis_store.normalize_tables

    def mutate_after_heavy_read(*args, **kwargs):
        result = normalize(*args, **kwargs)
        with service.store.jobs._transaction() as db:
            run = service.store.jobs._get(db, TENANT, run_id, "run", "META")
            service.store.jobs._bump_run(db, run)
        return result

    monkeypatch.setattr(analysis_store, "normalize_tables", mutate_after_heavy_read)
    assert http.get(base + "/observations").status_code == 409
    monkeypatch.setattr(analysis_store, "normalize_tables", normalize)

    first = http.get(base + "/safe-harbor", params={"limit": 1})
    assert first.status_code == 200 and first.json()["next_cursor"]
    cursor = first.json()["next_cursor"]
    last_claim_id = runner.claims.list(TENANT, run_id)[-1].claim_id
    with service.store.jobs._transaction() as db:
        run = service.store.jobs._get(db, TENANT, run_id, "run", "META")
        service.store.jobs._put(
            db,
            TENANT,
            run_id,
            "claim_head",
            last_claim_id,
            {"tag_revision": 999, "decision_revision": 0},
        )
        service.store.jobs._bump_run(db, run)
    second = http.get(base + "/safe-harbor", params={"limit": 1, "cursor": cursor})
    assert second.status_code == 200, second.text
    assert second.json()["snapshot_epoch"] == first.json()["snapshot_epoch"]
    assert (
        http.get(base + "/safe-harbor", params={"limit": 1, "cursor": cursor + "!"}).status_code
        == 400
    )
    assert http.get(base + "/safe-harbor", params={"limit": 2, "cursor": cursor}).status_code == 400

    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=FOREIGN))
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "admin", "active"))
    assert http.get(base + "/observations").status_code == 404
    assert http.get(f"/v1/runs/{uuid4()}/assurance").status_code == 404
    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=TENANT))

    monkeypatch.setattr(catalog_pages, "MAX_ROWS_PER_SNAPSHOT", 0)
    capped = http.get(base + "/assurance")
    assert capped.status_code == 503
    assert capped.json()["error"]["code"] == "CATALOG_CAPACITY_EXCEEDED"


def test_analysis_routes_enforce_independent_viewer_rate_limits(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    http, _ = _http(service, runner, now, role="viewer")
    url = f"/v1/runs/{run_id}/assurance"

    assert all(http.get(url).status_code == 200 for _ in range(120))
    limited = http.get(url)
    assert limited.status_code == 429
    assert limited.headers["retry-after"]
    assert http.get(f"/v1/runs/{run_id}/observations").status_code == 200


def test_safe_harbor_uses_latest_confirmed_head_without_inventing_legal_effect(
    tmp_path, monkeypatch
):
    from dataclasses import asdict

    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rules.engine import ConfirmedTags

    from tests.integration.test_local_tag_runner import verified_setup

    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    claim_id = runner.claims.list(TENANT, run_id)[0].claim_id
    inputs = runner.tags.load_inputs(TENANT, run_id, claim_id)
    first = inputs.tag_runs[0]
    confirmed = ConfirmedTags(
        TENANT,
        inputs.original.document_version_id,
        claim_id,
        "performance",
        (),
        2,
        inputs.packet.packet_sha256,
        first.model_sha256,
        first.prompt_sha256,
        inputs.consensus.replicate_hashes,
        inputs.rulepack.ontology_version,
        "forward_looking",
    )
    with service.store.jobs._transaction() as db:
        run = service.store.jobs._get(db, TENANT, run_id, "run", "META")
        service.store.jobs._put(
            db,
            TENANT,
            run_id,
            "tag_revision",
            f"{claim_id}:0000000002",
            {
                "tag_revision": 2,
                "confirmed_tags": asdict(confirmed),
                "elements": [],
                "origin": "local-synthetic-fixture",
                "input_snapshot_sha256": canonical_hash(inputs.snapshot()),
            },
            immutable=True,
        )
        service.store.jobs._put(
            db,
            TENANT,
            run_id,
            "claim_head",
            claim_id,
            {"tag_revision": 2, "decision_revision": 0},
        )
        service.store.jobs._bump_run(db, run)

    http, _ = _http(service, runner, now)
    response = http.get(f"/v1/runs/{run_id}/safe-harbor")
    assert response.status_code == 200, response.text
    validate("SafeHarborRecordPage", response.json())
    assert response.json()["items"] == [
        {
            "claim_id": claim_id,
            "applicable": True,
            "category": "forward_looking",
            "checklist": [
                {
                    "element_id": "assumptions",
                    "state": "unknown",
                    "evidence_refs": [],
                    "normalized_value": None,
                    "credited_from": None,
                    "reason_code": None,
                },
                {
                    "element_id": "scenario_or_premises",
                    "state": "unknown",
                    "evidence_refs": [],
                    "normalized_value": None,
                    "credited_from": None,
                    "reason_code": None,
                },
            ],
            "reasonable_basis_documented": None,
            "legal_effect": "not_determined",
            "mapping_status": "unresolved",
            "gap_ids": ["GAP-001"],
        }
    ]
