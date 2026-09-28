"""Real bounded transport/ledger with an intercepted HTTP boundary; no paid calls."""

from pathlib import Path

import pytest
from proofops.adapters.local.upstage_parse import UpstageParseProbe
from proofops.application.ports.jobs import LeaseLost

from tests.integration.test_raster_request_authorization import setup_request
from tests.integration.test_upstage_parse import fixed_pricing_date  # noqa: F401


def setup_dispatch(tmp_path, monkeypatch):
    actual_parse = UpstageParseProbe.parse
    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    monkeypatch.setattr(UpstageParseProbe, "parse", actual_parse)
    probe = UpstageParseProbe("test-raster-secret", tmp_path / "shared-ledger.sqlite")
    calls = []

    def post(data, mode):
        calls.append((data, mode))
        return dict(
            model="document-parse-260128",
            usage=dict(pages=1, standard=[1]),
            elements=[dict(id=0, page=1, content=dict(text=graph.blocks[0].raw_text))],
        )

    monkeypatch.setattr(probe, "_post_parse", post)
    return service, runner, lease, graph, native, probe, calls


def dispatch(runner, lease, graph, native, probe, **kwargs):
    from proofops_worker.raster_runtime import dispatch_authorized_raster

    return dispatch_authorized_raster(
        runner,
        lease,
        graph,
        native,
        (graph.blocks[0].source_id,),
        probe=probe,
        ledger=probe.ledger,
        **kwargs,
    )


def test_one_call_persists_receipt_and_repeat_reuses_without_reservation(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_job_store import raster_receipt, raster_requests

    service, runner, lease, graph, native, probe, calls = setup_dispatch(tmp_path, monkeypatch)
    request, wrapper = dispatch(runner, lease, graph, native, probe)
    assert len(calls) == 1
    assert probe.summary()["calls"] == 1
    assert wrapper == raster_receipt(service.store.jobs, lease.message, request["request_id"])
    assert wrapper["receipt"]["provider_model"] == "document-parse-260128"
    assert raster_requests(service.store.jobs, lease.message)[0]["request"] == request
    assert dispatch(runner, lease, graph, native, probe) == (request, wrapper)
    assert len(calls) == 1 and probe.summary()["calls"] == 1
    assert service.store.jobs.read_checkpoint(lease.message) is None
    from proofops.adapters.local.job_store import LocalSQLiteJobStore
    from proofops.adapters.local.raster_visibility import corroborate_native_visibility
    from proofops.domain.provenance import canonical_hash

    source = service.uploads.read_original(lease.message.tenant_id, graph.document_version_id)
    verified, proof = corroborate_native_visibility(
        native,
        request["correspondence"],
        wrapper["receipt"],
        graph,
        source,
        request_sha256=canonical_hash(request["correspondence"]),
        receipt_sha256=wrapper["receipt_sha256"],
        tenant_id=lease.message.tenant_id,
    )
    assert verified.blocks[0].quality == "verified"
    assert graph.blocks[0].quality == "unverified"
    assert proof["corroborated_source_ids"] == [graph.blocks[0].source_id]
    # Restart the store and reclaim the expired job; reuse the original receipt.
    runner.store.jobs = LocalSQLiteJobStore(runner.store.path)
    now = runner.clock()
    runner.clock = lambda: now + 400
    replacement = runner.store.jobs.claim_job(
        lease.message, owner="restart", now=int(runner.clock()), lease_seconds=60
    )
    assert replacement is not None and replacement.fencing_token != lease.fencing_token
    assert dispatch(runner, replacement, graph, native, probe) == (request, wrapper)
    assert len(calls) == 1 and probe.summary()["calls"] == 1


def test_unknown_transport_is_never_redispatched(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_job_store import raster_requests

    service, runner, lease, graph, native, probe, calls = setup_dispatch(tmp_path, monkeypatch)

    def fail(data, mode):
        calls.append((data, mode))
        raise TimeoutError("ambiguous response")

    monkeypatch.setattr(probe, "_post_parse", fail)
    with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
        dispatch(runner, lease, graph, native, probe)
    with pytest.raises(ValueError, match="RASTER_REQUEST_PENDING"):
        dispatch(runner, lease, graph, native, probe)
    assert len(calls) == 1 and probe.summary()["unsettled_calls"] == 1
    assert len(raster_requests(service.store.jobs, lease.message)) == 1


def test_expired_lease_keeps_own_returned_receipt_without_publishing(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_job_store import raster_receipt, raster_requests

    service, runner, lease, graph, native, probe, calls = setup_dispatch(tmp_path, monkeypatch)
    post = probe._post_parse
    now = runner.clock()

    def expire(data, mode):
        response = post(data, mode)
        runner.clock = lambda: now + 1000
        return response

    monkeypatch.setattr(probe, "_post_parse", expire)
    with pytest.raises(LeaseLost):
        dispatch(runner, lease, graph, native, probe)
    request = raster_requests(service.store.jobs, lease.message)[0]["request"]
    assert raster_receipt(service.store.jobs, lease.message, request["request_id"]) is not None
    assert service.store.jobs.read_checkpoint(lease.message) is None
    assert len(calls) == 1


def test_different_ledger_is_rejected_without_registration(tmp_path, monkeypatch):
    from proofops.adapters.local.raster_job_store import raster_requests
    from proofops_worker.raster_runtime import dispatch_authorized_raster

    service, runner, lease, graph, native, probe, calls = setup_dispatch(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="RASTER_LEDGER_MISMATCH"):
        dispatch_authorized_raster(
            runner,
            lease,
            graph,
            native,
            (graph.blocks[0].source_id,),
            probe=probe,
            ledger=Path(tmp_path / "different.sqlite"),
        )
    assert calls == [] and raster_requests(service.store.jobs, lease.message) == ()
    assert probe.summary()["calls"] == 0
