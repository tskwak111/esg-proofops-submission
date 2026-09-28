"""Legacy parser checkpoints must not carry reserved raster OCR metadata."""

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from proofops.adapters.parsing.opendataloader import ParseFailure
from proofops.application.ports.jobs import JobMessage

from tests.acceptance.test_parsing import TENANT
from tests.integration.test_local_parser_runner import runner_setup
from tests.integration.test_run_lifecycle import NOW, client, setup

RASTER_FIELDS = (
    "raster_ocr_policy_sha256",
    "raster_ocr_artifacts",
    "raster_ocr_coverage",
    "raster_ocr_extra",
)


def _legacy_v1_checkpoint(message, coverage, field):
    return {
        "schema": "local_parser_checkpoint_v1",
        "input_hash": message.input_hash,
        "stage_status": "completed",
        "downstream_status": "pending",
        "coverage": coverage,
        field: "reserved",
    }


@pytest.mark.parametrize("field", RASTER_FIELDS)
def test_commit_rejects_legacy_raster_fields_before_publication_or_enqueue(tmp_path, field):
    service, body = setup(tmp_path)
    http, _ = client(service)
    run_id = http.post("/v1/runs", json=body).json()["run_id"]
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=NOW)[0]["message"])
    lease = store.claim_job(message, owner="worker", now=NOW, lease_seconds=60)
    coverage = service.get(TENANT, run_id)["coverage"] | {
        "pages_processed": 3,
        "pages_unprocessed": 0,
    }

    with pytest.raises(ParseFailure, match="RASTER_OCR_CHECKPOINT_UNSUPPORTED"):
        store.commit_job(
            lease,
            payload=json.dumps(_legacy_v1_checkpoint(message, coverage, field)).encode(),
            now=NOW,
            next_job=replace(message, job_id=str(uuid4()), stage="extract"),
        )

    assert store.read_checkpoint(message) is None
    assert "parse_job" not in store.get_run(TENANT, run_id)
    events = store.pending_outbox(TENANT, run_id, now=NOW)
    assert [event["message"]["stage"] for event in events] == ["parse"]


@pytest.mark.parametrize("field", RASTER_FIELDS)
def test_read_rejects_legacy_raster_fields_without_changing_published_work(
    tmp_path, monkeypatch, field
):
    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    store = service.store.jobs
    message = JobMessage(**store.get_run(TENANT, run_id)["parse_job"])
    read_checkpoint = store.read_checkpoint
    published = read_checkpoint(message)
    tampered = json.loads(published)
    tampered[field] = "reserved"
    monkeypatch.setattr(store, "read_checkpoint", lambda _: json.dumps(tampered).encode())

    with pytest.raises(ParseFailure, match="RASTER_OCR_CHECKPOINT_UNSUPPORTED"):
        runner.load_graph(tenant_id=TENANT, run_id=run_id)

    assert read_checkpoint(message) == published
    assert store.get_run(TENANT, run_id)["current_stage"] == "extract"
    events = store.pending_outbox(TENANT, run_id, now=now[0])
    assert [event["message"]["stage"] for event in events] == ["extract"]


@pytest.mark.parametrize("schema", range(1, 5))
@pytest.mark.parametrize("field", RASTER_FIELDS)
def test_each_legacy_schema_rejects_reserved_raster_field(schema, field):
    from proofops.adapters.local.run_artifacts import checkpoint_note_reviews

    with pytest.raises(ParseFailure, match="RASTER_OCR_CHECKPOINT_UNSUPPORTED"):
        checkpoint_note_reviews({"schema": f"local_parser_checkpoint_v{schema}", field: "reserved"})


def test_v5_parser_checkpoint_requires_complete_raster_fields():
    from proofops.adapters.local.run_artifacts import checkpoint_note_reviews

    with pytest.raises(ParseFailure, match="RASTER_OCR_CHECKPOINT_INVALID"):
        checkpoint_note_reviews({"schema": "local_parser_checkpoint_v5"})
