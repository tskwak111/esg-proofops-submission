"""Durable raster-request registration is scoped, immutable and fenced."""

import json
from copy import deepcopy
from uuid import UUID, uuid4, uuid5

import pytest
from proofops.application.ports.jobs import JobMessage, LeaseLost
from proofops.domain.provenance import canonical_hash

from tests.integration.test_raster_request_authorization import AUTH, prepare, setup_request


def _api():
    from proofops.adapters.local.raster_job_store import (
        finish_raster_request,
        raster_receipt,
        raster_requests,
        register_raster_request,
    )

    return register_raster_request, raster_requests, finish_raster_request, raster_receipt


def _receipt(request):
    body = dict(
        model="document-parse-260128",
        mode=request["mode"],
        pdf_sha256=request["correspondence"]["input_pdf_sha256"],
        pages=request["submitted_pages"],
        bytes_len=request["correspondence"]["input_bytes"],
    )
    raw = dict(
        model=body["model"],
        usage={"pages": body["pages"], body["mode"]: list(range(1, body["pages"] + 1))},
        elements=[],
    )
    return dict(
        model=body["model"],
        provider_model=body["model"],
        mode=body["mode"],
        pages=body["pages"],
        request_sha256=canonical_hash(body),
        response_sha256=canonical_hash(raw),
        raw_response=raw,
    )


def test_registration_is_immutable_across_restart_and_receipt_is_scoped(tmp_path, monkeypatch):
    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    register, requests, finish, receipt_for = _api()

    assert register(service.store.jobs, lease, request, now=int(service.clock())) is True
    assert register(service.store.jobs, lease, request, now=int(service.clock())) is False
    assert requests(service.store.jobs, lease.message) == (
        dict(
            request=request,
            request_sha256=canonical_hash(request),
            owner=lease.owner,
            fencing_token=lease.fencing_token,
        ),
    )
    restarted = type(service.store.jobs)(service.store.jobs.path)
    assert register(restarted, lease, request, now=int(service.clock())) is False

    receipt = _receipt(request)
    finish(restarted, lease, request["request_id"], receipt)
    assert receipt_for(restarted, lease.message, request["request_id"]) == dict(
        request_sha256=canonical_hash(request),
        receipt=receipt,
        receipt_sha256=canonical_hash(receipt),
    )


def test_registration_rejects_redispatch_and_enforces_fencing_and_max_calls(tmp_path, monkeypatch):
    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    register, _, finish, _ = _api()
    now = int(service.clock())
    assert register(service.store.jobs, lease, request, now=now) is True

    changed = deepcopy(request)
    changed["mode"] = "enhanced"
    with pytest.raises(ValueError):
        register(service.store.jobs, lease, changed, now=now)
    with pytest.raises(ValueError):
        finish(service.store.jobs, lease, request["request_id"], _receipt(changed))

    second_message = JobMessage(
        AUTH.tenant_id,
        lease.message.run_id,
        lease.message.document_version_id,
        str(uuid4()),
        "parse",
        "raster",
        lease.message.input_hash,
    )
    service.store.jobs.enqueue(second_message, now=now)
    second_lease = service.store.jobs.claim_job(
        second_message, owner="other", now=now, lease_seconds=60
    )
    second_request = deepcopy(request)
    second_request["job_id"] = second_message.job_id
    unsigned = {key: value for key, value in second_request.items() if key != "request_id"}
    second_request["request_id"] = str(uuid5(UUID(second_message.job_id), canonical_hash(unsigned)))
    with pytest.raises(ValueError, match="RASTER_MAX_CALLS_EXCEEDED"):
        register(service.store.jobs, second_lease, second_request, now=now)

    next_lease = service.store.jobs.claim_job(
        lease.message, owner="next", now=now + 61, lease_seconds=60
    )
    with pytest.raises(LeaseLost):
        register(service.store.jobs, lease, request, now=now + 61)
    finish(service.store.jobs, lease, request["request_id"], _receipt(request))
    with pytest.raises(LeaseLost):
        finish(service.store.jobs, next_lease, request["request_id"], _receipt(request))


def test_cancelled_lease_and_receipt_mismatch_are_rejected_but_own_late_receipt_is_retained(
    tmp_path, monkeypatch
):
    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    register, _, finish, receipt_for = _api()
    now = int(service.clock())
    assert register(service.store.jobs, lease, request, now=now) is True
    receipt = _receipt(request)

    # The call was authorized before expiry; its exact returned receipt remains audit evidence.
    finish(service.store.jobs, lease, request["request_id"], receipt)
    finish(service.store.jobs, lease, request["request_id"], receipt)
    bad = deepcopy(receipt)
    bad["raw_response"]["elements"] = [dict(id=1, page=1, content=dict(text="different"))]
    bad["response_sha256"] = canonical_hash(bad["raw_response"])
    with pytest.raises(ValueError):
        finish(service.store.jobs, lease, request["request_id"], bad)
    assert (
        receipt_for(service.store.jobs, lease.message, request["request_id"])["receipt"] == receipt
    )

    run = service.store.jobs.get_run(AUTH.tenant_id, lease.message.run_id)
    service.store.jobs.cancel_run(
        AUTH.tenant_id,
        lease.message.run_id,
        expected_revision=run["revision"],
        idempotency_key="00000000-0000-4000-8000-000000000000",
        reason="test cancellation",
        actor_sub="test",
        now=now,
    )
    other = deepcopy(request)
    other["request_id"] = request["request_id"].replace("0", "1", 1)
    with pytest.raises(LeaseLost):
        register(service.store.jobs, lease, other, now=now)


def test_unknown_or_cross_tenant_messages_cannot_read_raster_records(tmp_path, monkeypatch):
    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    register, requests, _, receipt_for = _api()
    assert register(service.store.jobs, lease, request, now=int(service.clock())) is True
    foreign = JobMessage(
        str(uuid4()),
        lease.message.run_id,
        lease.message.document_version_id,
        lease.message.job_id,
        "parse",
        lease.message.shard,
        lease.message.input_hash,
    )
    with pytest.raises(KeyError):
        requests(service.store.jobs, foreign)
    with pytest.raises(KeyError):
        receipt_for(service.store.jobs, foreign, request["request_id"])


def test_tampered_stored_request_hash_is_rejected_on_read(tmp_path, monkeypatch):
    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    register, requests, _, _ = _api()
    assert register(service.store.jobs, lease, request, now=int(service.clock())) is True
    with service.store.jobs._transaction() as db:
        stored = service.store.jobs._get(
            db, AUTH.tenant_id, lease.message.run_id, "raster_request", request["request_id"]
        )
        stored["request_sha256"] = "0" * 64
        db.execute(
            "UPDATE job_records SET value=? WHERE tenant_id=? AND run_id=? AND kind=? "
            "AND record_id=?",
            (
                json.dumps(stored, sort_keys=True, separators=(",", ":")),
                AUTH.tenant_id,
                lease.message.run_id,
                "raster_request",
                request["request_id"],
            ),
        )
    with pytest.raises(ValueError, match="RASTER_REQUEST_INTEGRITY_MISMATCH"):
        requests(service.store.jobs, lease.message)


@pytest.mark.parametrize(
    "usage",
    [
        {"pages": 1, "enhanced": [1]},
        {"pages": 1, "standard": [True]},
        {"pages": 1, "standard": [1, 1]},
        {"pages": 1},
    ],
)
def test_receipt_billing_must_match_registered_mode(tmp_path, monkeypatch, usage):
    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    register, _, finish, receipt_for = _api()
    register(service.store.jobs, lease, request, now=int(service.clock()))
    receipt = _receipt(request)
    receipt["raw_response"]["usage"] = usage
    receipt["response_sha256"] = canonical_hash(receipt["raw_response"])
    with pytest.raises(ValueError, match="RASTER_BILLING_PAGES_INVALID"):
        finish(service.store.jobs, lease, request["request_id"], receipt)
    assert receipt_for(service.store.jobs, lease.message, request["request_id"]) is None


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_registration_never_authorizes_two_calls(tmp_path, monkeypatch, same_request):
    from concurrent.futures import ThreadPoolExecutor
    from dataclasses import replace
    from threading import Barrier

    service, runner, lease, graph, native, _ = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    register, requests, _, _ = _api()
    now = int(service.clock())
    other_lease, other_request = lease, request
    if not same_request:
        message = replace(lease.message, job_id=str(uuid4()), shard="other")
        service.store.jobs.enqueue(message, now=now)
        other_lease = service.store.jobs.claim_job(
            message, owner="other", now=now, lease_seconds=60
        )
        other_request = deepcopy(request)
        other_request["job_id"] = message.job_id
        unsigned = {k: v for k, v in other_request.items() if k != "request_id"}
        other_request["request_id"] = str(uuid5(UUID(message.job_id), canonical_hash(unsigned)))
    barrier = Barrier(2)

    def attempt(args):
        owned, prepared = args
        barrier.wait(timeout=5)
        try:
            return register(service.store.jobs, owned, prepared, now=now)
        except ValueError as exc:
            assert str(exc) == "RASTER_MAX_CALLS_EXCEEDED"
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [(lease, request), (other_lease, other_request)]))
    assert results.count(True) == 1
    assert (False in results) if same_request else ("RASTER_MAX_CALLS_EXCEEDED" in results)
    registrations = {
        r["request"]["request_id"]
        for owned in (lease, other_lease)
        for r in requests(service.store.jobs, owned.message)
    }
    assert len(registrations) == 1
