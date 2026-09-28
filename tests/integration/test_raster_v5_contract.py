"""v5 carries an explicit complete raster coverage and immutable artifact references."""

from copy import deepcopy
from uuid import uuid4

import pytest
from proofops.adapters.local.run_artifacts import checkpoint_note_reviews
from proofops.adapters.parsing.opendataloader import ParseFailure


def envelope():
    return dict(
        schema="local_parser_checkpoint_v5",
        runtime_note_review_artifacts=[],
        graph_sha256="a" * 64,
        raster_ocr_policy_sha256="b" * 64,
        raster_ocr_artifacts=[],
        raster_ocr_coverage=dict(
            eligible_source_ids=["source"],
            requested_source_ids=[],
            corroborated_source_ids=[],
            unresolved_source_ids=["source"],
            failed_source_ids=[],
        ),
    )


def test_complete_v5_shape_keeps_unrequested_sources_unresolved():
    assert checkpoint_note_reviews(envelope()) == ()


@pytest.mark.parametrize(
    "fault", ["missing", "extra", "unresolved", "promoted", "failed", "duplicate", "pin"]
)
def test_incomplete_or_falsified_v5_shape_is_rejected(fault):
    value = deepcopy(envelope())
    if fault == "missing":
        value.pop("raster_ocr_policy_sha256")
    elif fault == "extra":
        value["raster_ocr_unrecognized"] = True
    elif fault == "unresolved":
        value["raster_ocr_coverage"]["unresolved_source_ids"] = []
    elif fault == "promoted":
        value["raster_ocr_coverage"]["corroborated_source_ids"] = ["source"]
    elif fault == "failed":
        value["raster_ocr_coverage"]["failed_source_ids"] = ["source"]
    elif fault == "duplicate":
        ref = dict(request_id=str(uuid4()), request_sha256="c" * 64, receipt_sha256="d" * 64)
        value["raster_ocr_artifacts"] = [ref, ref]
    else:
        value["raster_ocr_policy_sha256"] = "not-a-hash"
    with pytest.raises(ParseFailure, match="RASTER_OCR_CHECKPOINT_INVALID"):
        checkpoint_note_reviews(value)


@pytest.mark.parametrize(
    "fault", ["missing_ref", "foreign_ref", "receipt_hash", "requested", "downgrade"]
)
def test_v5_commit_requires_exact_owned_records(tmp_path, monkeypatch, fault):
    import json
    from dataclasses import replace

    from proofops.adapters.local.raster_job_store import (
        finish_raster_request,
        register_raster_request,
    )
    from proofops.adapters.local.run_artifacts import native_paragraph_policy
    from proofops.domain.provenance import canonical_hash

    from tests.integration.test_raster_job_store import _receipt
    from tests.integration.test_raster_request_authorization import prepare, setup_request

    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    jobs, now = service.store.jobs, int(runner.clock())
    register_raster_request(jobs, lease, request, now=now)
    receipt = _receipt(request)
    finish_raster_request(jobs, lease, request["request_id"], receipt)
    jobs.bind_parser_note_reviews(lease, (), now=now)
    jobs.bind_parser_native_policy(lease, native_paragraph_policy(), now=now)
    value = envelope()
    source_id = graph.blocks[0].source_id
    value.update(
        input_hash=lease.message.input_hash,
        stage_status="completed",
        downstream_status="pending",
        native_paragraph_attestation=native,
        native_paragraph_policy_sha256=canonical_hash(native_paragraph_policy()),
        raster_ocr_policy_sha256=snapshot["raster_ocr_policy_hash"],
        raster_ocr_artifacts=[
            dict(
                request_id=request["request_id"],
                request_sha256=canonical_hash(request),
                receipt_sha256=canonical_hash(receipt),
            )
        ],
        raster_ocr_coverage=dict(
            eligible_source_ids=[source_id],
            requested_source_ids=[source_id],
            corroborated_source_ids=[],
            unresolved_source_ids=[source_id],
            failed_source_ids=[],
        ),
        coverage=service.get(lease.message.tenant_id, lease.message.run_id)["coverage"]
        | dict(pages_processed=1, pages_unprocessed=2),
    )
    if fault == "missing_ref":
        value["raster_ocr_artifacts"] = []
    elif fault == "foreign_ref":
        value["raster_ocr_artifacts"][0]["request_id"] = str(uuid4())
    elif fault == "receipt_hash":
        value["raster_ocr_artifacts"][0]["receipt_sha256"] = "0" * 64
    elif fault == "requested":
        value["raster_ocr_coverage"]["requested_source_ids"] = []
    else:
        value["schema"] = "local_parser_checkpoint_v4"
        value = {k: v for k, v in value.items() if not k.startswith("raster_ocr_")}
    with pytest.raises(ValueError, match="RASTER_CHECKPOINT_(INPUT|POLICY)_MISMATCH"):
        jobs.commit_job(
            lease,
            payload=json.dumps(value).encode(),
            now=now,
            next_job=replace(lease.message, job_id=str(uuid4()), stage="extract"),
        )
    assert jobs.read_checkpoint(lease.message) is None
    assert "parse_job" not in jobs.get_run(lease.message.tenant_id, lease.message.run_id)
