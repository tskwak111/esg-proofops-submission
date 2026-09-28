"""AT-034: local measurements must never be presented as deployed SLO evidence."""

import json
from dataclasses import asdict
from uuid import uuid4

from tests.load.read_api import exercise_load_profile


def test_local_load_measurement_is_explicitly_not_deployed_slo_evidence(
    tmp_path, monkeypatch
) -> None:
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.summary_store import LocalSummaryStore
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops_api.routers.summaries import build_summaries_router

    from tests.integration import test_run_lifecycle as lifecycle

    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")
    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(tmp_path / "health.sqlite3"))
    run_root = tmp_path / "runs"
    run_root.mkdir()
    service, body = lifecycle.setup(run_root)
    run_client, auth = lifecycle.client(service)
    claims = LocalClaimStore(
        service.store, service.uploads, OpenDataLoaderParser(tmp_path / "parser")
    )
    run_client.app.include_router(
        build_summaries_router(LocalSummaryStore(service.store, claims), auth)
    )
    created = []

    def accept_run(tenant_id):
        assert tenant_id == lifecycle.TENANT
        response = run_client.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())})
        if response.status_code == 202:
            created.append(response.json()["run_id"])
        return response.status_code

    def read_summary():
        assert len(created) == 2, "read load must include two active runs"
        response = run_client.get(f"/v1/runs/{created[0]}/summary")
        assert response.status_code == 200, response.text
        assert response.json()["coverage"]["pages_unprocessed"] == 3
        return response.status_code

    with run_client:
        measurement = exercise_load_profile(
            read=read_summary,
            accept_run=accept_run,
            tenant_ids=(lifecycle.TENANT,),
        )

    assert measurement.environment == "local_synthetic"
    print(json.dumps(asdict(measurement)))
    assert measurement.read_count == 20
    assert measurement.accept_count == 2
    assert set(measurement.read_status_codes) == {200}
    assert set(measurement.accept_status_codes) == {202}
    assert measurement.certification_status == "not_certified"
    assert measurement.read_p95_seconds >= 0
    assert measurement.accept_p95_seconds >= 0
    assert measurement.read_p95_target_seconds == 1.0
    assert measurement.accept_p95_target_seconds == 2.0
