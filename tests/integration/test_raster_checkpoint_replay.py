"""Trusted raster artifacts replay offline before a v5 checkpoint is published."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID, uuid5

import pytest
from proofops.domain.provenance import canonical_hash

from tests.integration.test_raster_request_authorization import prepare, setup_request


def _receipt(request, text):
    body = {
        "model": "document-parse-260128",
        "mode": request["mode"],
        "pdf_sha256": request["correspondence"]["input_pdf_sha256"],
        "pages": request["submitted_pages"],
        "bytes_len": request["correspondence"]["input_bytes"],
    }
    raw = {
        "model": body["model"],
        "usage": {"pages": body["pages"], body["mode"]: list(range(1, body["pages"] + 1))},
        "elements": [{"id": 0, "page": 1, "content": {"text": text}}],
    }
    return {
        "model": body["model"],
        "provider_model": body["model"],
        "mode": body["mode"],
        "pages": body["pages"],
        "request_sha256": canonical_hash(body),
        "response_sha256": canonical_hash(raw),
        "raw_response": raw,
    }


def _resign(request):
    unsigned = {key: value for key, value in request.items() if key != "request_id"}
    request["request_id"] = str(uuid5(UUID(request["job_id"]), canonical_hash(unsigned)))
    return request


def _stored(service, lease, request, text):
    from proofops.adapters.local.raster_job_store import (
        finish_raster_request,
        raster_receipt,
        raster_requests,
        register_raster_request,
    )

    register_raster_request(service.store.jobs, lease, request, now=int(service.clock()))
    finish_raster_request(service.store.jobs, lease, request["request_id"], _receipt(request, text))
    registrations = raster_requests(service.store.jobs, lease.message)
    return registrations, {
        request["request_id"]: raster_receipt(
            service.store.jobs, lease.message, request["request_id"]
        )
    }


def test_replay_composes_only_exact_registered_receipt(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_checkpoint import replay_raster_records

    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    registrations, receipts = _stored(service, lease, request, graph.blocks[0].raw_text)

    composed, coverage, refs = replay_raster_records(
        snapshot,
        lease.message,
        native,
        graph,
        service.uploads.read_original(lease.message.tenant_id, graph.document_version_id),
        registrations,
        receipts,
    )

    source_id = graph.blocks[0].source_id
    assert composed.blocks[0].quality == "verified"
    assert composed.blocks[0].raw_text == graph.blocks[0].raw_text
    assert coverage == {
        "eligible_source_ids": [source_id],
        "requested_source_ids": [source_id],
        "corroborated_source_ids": [source_id],
        "unresolved_source_ids": [],
        "failed_source_ids": [],
    }
    assert refs == [
        {
            "request_id": request["request_id"],
            "request_sha256": canonical_hash(request),
            "receipt_sha256": canonical_hash(receipts[request["request_id"]]["receipt"]),
        }
    ]


def test_empty_records_leave_eligible_sources_unresolved(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_checkpoint import replay_raster_records

    service, _, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    composed, coverage, refs = replay_raster_records(
        snapshot,
        lease.message,
        native,
        graph,
        service.uploads.read_original(lease.message.tenant_id, graph.document_version_id),
        (),
        {},
    )

    assert composed.blocks[0].quality == "unverified"
    assert coverage["eligible_source_ids"] == coverage["unresolved_source_ids"]
    assert coverage["requested_source_ids"] == coverage["corroborated_source_ids"] == []
    assert refs == []


@pytest.mark.parametrize(
    "fault",
    [
        "foreign_job",
        "foreign_receipt",
        "omitted",
        "extra_receipt",
        "duplicate",
        "out_of_scope",
        "native_mismatch",
    ],
)
def test_replay_rejects_untrusted_or_incomplete_artifact_sets(tmp_path, monkeypatch, fault):
    from proofops.adapters.local.raster_checkpoint import replay_raster_records

    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    registrations, receipts = _stored(service, lease, request, graph.blocks[0].raw_text)
    registrations, receipts, native = deepcopy(registrations), deepcopy(receipts), deepcopy(native)
    if fault == "foreign_job":
        request = registrations[0]["request"]
        request["job_id"] = "00000000-0000-4000-8000-000000000123"
        _resign(request)
        registrations[0]["request_sha256"] = canonical_hash(request)
        receipts = {request["request_id"]: next(iter(receipts.values()))}
        receipts[request["request_id"]]["request_sha256"] = canonical_hash(request)
    elif fault == "foreign_receipt":
        receipts[request["request_id"]]["request_sha256"] = "0" * 64
    elif fault == "omitted":
        receipts = {}
    elif fault == "extra_receipt":
        receipts["00000000-0000-4000-8000-000000000124"] = next(iter(receipts.values()))
    elif fault == "duplicate":
        registrations *= 2
    elif fault == "out_of_scope":
        request = registrations[0]["request"]
        request["requested_source_ids"] = ["foreign-source"]
        request["correspondence"]["rows"][0]["source_id"] = "foreign-source"
        _resign(request)
        registrations[0]["request_sha256"] = canonical_hash(request)
        receipts = {request["request_id"]: next(iter(receipts.values()))}
        receipts[request["request_id"]]["request_sha256"] = canonical_hash(request)
    else:
        native["records"][0]["reason"] = "forged"
        request = registrations[0]["request"]
        request["native_attestation_sha256"] = canonical_hash(native)
        _resign(request)
        registrations[0]["request_sha256"] = canonical_hash(request)
        receipts = {request["request_id"]: next(iter(receipts.values()))}
        receipts[request["request_id"]]["request_sha256"] = canonical_hash(request)

    with pytest.raises(ValueError):
        replay_raster_records(
            snapshot,
            lease.message,
            native,
            graph,
            service.uploads.read_original(lease.message.tenant_id, graph.document_version_id),
            tuple(registrations),
            receipts,
        )


def test_policy_drift_rejects_replay(tmp_path, monkeypatch):
    import proofops.adapters.local.raster_checkpoint as checkpoint

    service, _, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    monkeypatch.setattr(checkpoint, "raster_ocr_policy", lambda **_: {})
    with pytest.raises(ValueError, match="POLICY_CHANGED"):
        checkpoint.replay_raster_records(
            snapshot,
            lease.message,
            native,
            graph,
            service.uploads.read_original(lease.message.tenant_id, graph.document_version_id),
            (),
            {},
        )


def test_text_mismatch_cannot_promote_source(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_checkpoint import replay_raster_records

    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    registrations, receipts = _stored(service, lease, request, "different OCR text")
    composed, coverage, _ = replay_raster_records(
        snapshot,
        lease.message,
        native,
        graph,
        service.uploads.read_original(lease.message.tenant_id, graph.document_version_id),
        registrations,
        receipts,
    )

    assert composed.blocks[0].quality == "unverified"
    assert coverage["corroborated_source_ids"] == []
    assert coverage["unresolved_source_ids"] == [graph.blocks[0].source_id]
