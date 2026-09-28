"""Raster parser worker fail-closed boundary."""

import json
from io import BytesIO

import pytest

from tests.integration.test_upstage_parse import fixed_pricing_date  # noqa: F401


def prose_pdf(*, heading=False):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        (b"BT /F1 24 Tf 72 770 Td (Environmental performance) Tj ET " if heading else b"")
        + b"BT /F1 12 Tf 72 720 Td (The company reduced emissions by 1234 tCO2e.) Tj "
        b"0 -18 Td (This paragraph is deliberately long enough for parser prose.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_configured_raster_probe_without_frozen_snapshot_fails_before_job_access(
    tmp_path, monkeypatch
):
    from proofops.adapters.local.upstage_parse import UpstageParseProbe
    from proofops_worker.local_runner import LocalParserRunner

    from tests.integration.test_local_parser_runner import TENANT, runner_setup

    service, run_id, runner, _now, _stream = runner_setup(tmp_path, monkeypatch)
    ledger = tmp_path / "raster-ledger.sqlite3"
    probe = UpstageParseProbe("test-secret", ledger)
    runner = LocalParserRunner(
        service.store,
        service.uploads,
        runner.parser,
        profile=runner.profile,
        telemetry=runner.telemetry,
        clock=runner.clock,
        verify_paragraphs=True,
        raster_probe=probe,
        raster_ledger=ledger,
    )

    with pytest.raises(ValueError, match="RASTER_OCR_RUNTIME_NOT_SUPPORTED"):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert "parse_job" not in service.store.jobs.get_run(TENANT, run_id)


def setup_worker(tmp_path, monkeypatch, *, heading=False):
    from proofops.adapters.local.run_artifacts import load_run_inputs
    from proofops.adapters.local.upstage_parse import UpstageParseProbe
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.telemetry import Telemetry
    from proofops_worker.local_runner import LocalParserRunner

    from tests.integration import test_raster_request_authorization as authorization

    actual_parse = UpstageParseProbe.parse
    monkeypatch.setattr(authorization, "pdf", lambda: prose_pdf(heading=heading))
    service, _prepared, lease, _graph, _native, _snapshot = authorization.setup_request(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(UpstageParseProbe, "parse", actual_parse)
    _snapshot, _source, profile = load_run_inputs(
        service.store,
        service.uploads,
        tenant_id=authorization.AUTH.tenant_id,
        run_id=lease.message.run_id,
    )
    ledger = tmp_path / "shared-ledger.sqlite"
    probe = UpstageParseProbe("test-raster-secret", ledger)
    calls = []

    def pinned_response(data, mode):
        calls.append((data, mode))
        return {
            "model": "document-parse-260128",
            "usage": {"pages": 1, "standard": [1]},
            "elements": [
                {
                    "id": 0,
                    "page": 1,
                    "content": {
                        "text": "The company reduced emissions by 1234 tCO2e. "
                        "This paragraph is deliberately long enough for parser prose."
                    },
                }
            ],
        }

    monkeypatch.setattr(probe, "_post_parse", pinned_response)
    runner = LocalParserRunner(
        service.store,
        service.uploads,
        OpenDataLoaderParser(tmp_path / "prepared"),
        profile=profile,
        telemetry=Telemetry(service="worker", env="test", stream=None, hash_key=b"x" * 32),
        clock=lambda: int(service.clock()) + 120,
        verify_paragraphs=True,
        raster_probe=probe,
        raster_ledger=ledger,
    )

    return service, runner, lease, probe, calls


def test_frozen_raster_run_publishes_v5_with_no_eligible_sources(tmp_path, monkeypatch):
    service, runner, lease, probe, calls = setup_worker(tmp_path, monkeypatch)
    assert (
        runner.run_once(tenant_id=lease.message.tenant_id, run_id=lease.message.run_id)
        == "committed"
    )
    checkpoint = service.store.jobs.read_checkpoint(lease.message)
    assert checkpoint is not None
    payload = json.loads(checkpoint)
    assert payload["schema"] == "local_parser_checkpoint_v5"
    assert payload["raster_ocr_coverage"]["eligible_source_ids"] == []
    assert payload["raster_ocr_coverage"]["unresolved_source_ids"] == []
    assert payload["raster_ocr_coverage"]["failed_source_ids"] == []
    assert calls == []
    assert probe.summary()["calls"] == 0


def test_frozen_raster_recovers_paragraph_and_replays_offline(tmp_path, monkeypatch):
    service, runner, lease, probe, calls = setup_worker(tmp_path, monkeypatch, heading=True)
    identity = dict(tenant_id=lease.message.tenant_id, run_id=lease.message.run_id)
    assert runner.run_once(**identity) == "committed"
    checkpoint = service.store.jobs.read_checkpoint(lease.message)
    payload = json.loads(checkpoint)
    coverage = payload["raster_ocr_coverage"]
    assert len(coverage["eligible_source_ids"]) == 1, payload["native_paragraph_attestation"]
    assert coverage["corroborated_source_ids"] == coverage["eligible_source_ids"]
    assert coverage["unresolved_source_ids"] == []
    assert len(calls) == 1
    monkeypatch.setattr(
        probe, "parse", lambda *a, **k: pytest.fail("offline replay called provider")
    )
    # Reopen durable storage to exclude in-memory writer state from replay.
    runner.store = type(service.store)(service.store.path)
    graph = runner.load_graph(**identity)
    assert [b.quality for b in graph.blocks if b.kind == "paragraph"] == ["verified"]
    assert service.store.jobs.read_checkpoint(lease.message) == checkpoint
    usage = service.store.jobs.list_usage(**identity)
    assert sum(item["model_calls"] for item in usage) == 1
    assert sum(item.get("document_parse_pages", 0) for item in usage) == 1


def test_raster_timeout_retains_reservation_without_publishing(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_job_store import raster_requests

    service, runner, lease, probe, calls = setup_worker(tmp_path, monkeypatch, heading=True)
    identity = dict(tenant_id=lease.message.tenant_id, run_id=lease.message.run_id)

    def timeout(data, mode):
        calls.append((data, mode))
        raise TimeoutError("ambiguous response")

    monkeypatch.setattr(probe, "_post_parse", timeout)
    assert runner.run_once(**identity) in {"failed", "retry"}
    assert len(calls) == 1
    assert service.store.jobs.read_checkpoint(lease.message) is None
    assert "parse_job" not in service.store.jobs.get_run(**identity)
    assert len(raster_requests(service.store.jobs, lease.message)) == 1
    assert probe.summary()["unsettled_calls"] == 1
    usage = service.store.jobs.list_usage(**identity)
    assert len(usage[-1]["raster_pending_request_ids"]) == 1
    assert usage[-1]["note_pending_usage"]["unsettled_calls"] == 1


@pytest.mark.parametrize("tamper", ["coverage", "receipt", "graph"])
def test_v5_reader_rejects_changed_publication(tmp_path, monkeypatch, tamper):
    service, runner, lease, probe, calls = setup_worker(tmp_path, monkeypatch, heading=True)
    identity = dict(tenant_id=lease.message.tenant_id, run_id=lease.message.run_id)
    assert runner.run_once(**identity) == "committed"
    # Warm any expensive replay cache before altering the publication envelope.
    runner.load_graph(**identity)
    payload = json.loads(service.store.jobs.read_checkpoint(lease.message))
    if tamper == "coverage":
        # Still structurally valid, but disagrees with the independently replayed receipt.
        coverage = payload["raster_ocr_coverage"]
        coverage["unresolved_source_ids"] = coverage["eligible_source_ids"]
        coverage["corroborated_source_ids"] = []
    elif tamper == "receipt":
        payload["raster_ocr_artifacts"][0]["receipt_sha256"] = "0" * 64
    else:
        payload["graph_sha256"] = "0" * 64
    monkeypatch.setattr(
        service.store.jobs, "read_checkpoint", lambda *a, **k: json.dumps(payload).encode()
    )
    monkeypatch.setattr(probe, "parse", lambda *a, **k: pytest.fail("reader invoked provider"))
    with pytest.raises(
        ValueError, match="(RASTER_.*(INVALID|MISMATCH)|NOTE_REVIEW_GRAPH_MISMATCH)"
    ):
        runner.load_graph(**identity)
    assert len(calls) == 1
