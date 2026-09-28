"""Long parser operations renew real SQLite leases; failed renewals cannot publish."""

import sqlite3
import threading
import time
from uuid import uuid4

import pytest
from proofops.application.ports.jobs import JobMessage
from proofops_worker import local_runner

from tests.acceptance.test_parsing import TENANT
from tests.integration.test_local_parser_runner import runner_setup


def short_lease_runner(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    started = time.monotonic()
    runner.clock = lambda: now[0] + time.monotonic() - started
    consume = local_runner.consume_job

    def short_consume(*args, **kwargs):
        kwargs["lease_seconds"] = 2
        return consume(*args, **kwargs)

    monkeypatch.setattr(local_runner, "consume_job", short_consume)
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=now[0])[0]["message"])
    return runner, store, run_id, message, now


def assert_stopped(message):
    assert not any(t.name == f"lease-heartbeat:{message.job_id}" for t in threading.enumerate())


@pytest.mark.parametrize("phase", ["parse", "native"])
def test_short_lease_survives_long_parser_and_native_proof(tmp_path, monkeypatch, phase):
    runner, store, run_id, message, _ = short_lease_runner(tmp_path, monkeypatch)
    if phase == "parse":
        target, attribute = runner.parser, "parse"
    else:
        from proofops.adapters.local import source_verification

        runner.verify_paragraphs = True
        target, attribute = source_verification, "attest_native_sources"
    original = getattr(target, attribute)

    def slow(*args, **kwargs):
        time.sleep(2.5)  # Real operation outlasts the original two-second lease.
        return original(*args, **kwargs)

    monkeypatch.setattr(target, attribute, slow)
    started = time.monotonic()
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert time.monotonic() - started > 2
    job = store.get_job(message)
    assert job["heartbeat_at"] > int(runner.clock()) - 2
    assert store.read_checkpoint(message) is not None
    assert store.get_usage(message, fencing_token=1)["model_calls"] == 0
    assert_stopped(message)


@pytest.mark.parametrize(
    "boundary", ["success", "cancel", "owner", "heartbeat", "error", "interrupt"]
)
def test_keepalive_real_repository(tmp_path, monkeypatch, boundary):
    """Exercise real timed SQLite renewal independently of the PDF upload watchdog."""
    from tests.acceptance.test_jobs import MESSAGE, RUN, TENANT, seeded

    store = seeded(tmp_path / "jobs.sqlite")
    started = time.monotonic()
    offset = [1]

    def clock():
        return int(offset[0] + time.monotonic() - started)

    rejected = threading.Event()
    heartbeat = store.heartbeat

    def watched_heartbeat(*args, **kwargs):
        try:
            if boundary == "heartbeat":
                raise sqlite3.OperationalError("injected renewal failure")
            return heartbeat(*args, **kwargs)
        except Exception:
            rejected.set()
            raise

    monkeypatch.setattr(store, "heartbeat", watched_heartbeat)

    def operation(lease):
        if boundary == "success":
            time.sleep(2.5)
        elif boundary == "cancel":
            store.cancel_run(
                TENANT,
                RUN,
                expected_revision=store.get_run(TENANT, RUN)["revision"],
                idempotency_key=str(uuid4()),
                reason="lease regression",
                actor_sub="synthetic-fixture",
                now=clock(),
            )
        elif boundary == "owner":
            offset[0] += 1000
            assert (
                store.claim_job(MESSAGE, owner="replacement", now=clock(), lease_seconds=30)
                is not None
            )
        elif boundary == "error":
            raise RuntimeError("injected parser failure")
        elif boundary == "interrupt":
            raise KeyboardInterrupt("injected SIGINT")
        if boundary in {"cancel", "owner", "heartbeat"}:
            assert rejected.wait(3)
        return b"verified checkpoint", {"model_calls": 0}

    def run():
        return local_runner.consume_job(
            store,
            MESSAGE,
            owner="parser",
            clock=clock,
            lease_seconds=2,
            operation=lambda lease: local_runner._with_parser_heartbeat(
                store, lease, clock, operation
            ),
        )

    if boundary in {"error", "interrupt"}:
        with pytest.raises(RuntimeError if boundary == "error" else KeyboardInterrupt):
            run()
    else:
        assert run() == {"success": "committed", "heartbeat": "failed"}.get(boundary, "discarded")
        assert store.get_usage(MESSAGE, fencing_token=1) == {"model_calls": 0}
    assert store.read_checkpoint(MESSAGE) == (
        b"verified checkpoint" if boundary == "success" else None
    )
    if boundary == "success":
        assert time.monotonic() - started > 2
        assert store.get_job(MESSAGE)["heartbeat_at"] > 1
    assert_stopped(MESSAGE)


@pytest.mark.parametrize("boundary", ["cancel", "owner", "heartbeat", "error", "interrupt"])
def test_lease_failures_block_publication_and_stop_thread(tmp_path, monkeypatch, boundary):
    runner, store, run_id, message, now = short_lease_runner(tmp_path, monkeypatch)
    original = runner.parser.load_verified
    heartbeat = store.heartbeat
    rejected = threading.Event()

    def watched_heartbeat(*args, **kwargs):
        try:
            if boundary == "heartbeat":
                raise sqlite3.OperationalError("injected heartbeat failure")
            return heartbeat(*args, **kwargs)
        except Exception:
            rejected.set()
            raise

    monkeypatch.setattr(store, "heartbeat", watched_heartbeat)

    def interrupt(*args, **kwargs):
        graph = original(*args, **kwargs)
        if boundary == "cancel":
            run = store.get_run(TENANT, run_id)
            store.cancel_run(
                TENANT,
                run_id,
                expected_revision=run["revision"],
                idempotency_key=str(uuid4()),
                reason="lease regression",
                actor_sub="synthetic-fixture",
                now=int(runner.clock()),
            )
        elif boundary == "owner":
            now[0] += 1000
            assert (
                store.claim_job(
                    message, owner="replacement", now=int(runner.clock()), lease_seconds=30
                )
                is not None
            )
        elif boundary == "error":
            raise RuntimeError("injected parser failure")
        elif boundary == "interrupt":
            raise KeyboardInterrupt("injected SIGINT")
        assert rejected.wait(3), "keepalive must observe renewal failure"
        return graph

    monkeypatch.setattr(runner.parser, "load_verified", interrupt)
    if boundary in {"error", "interrupt"}:
        exception = RuntimeError if boundary == "error" else KeyboardInterrupt
        with pytest.raises(exception):
            runner.run_once(tenant_id=TENANT, run_id=run_id)
    else:
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) in {"discarded", "failed"}
    assert store.read_checkpoint(message) is None
    assert "parse_job" not in store.get_run(TENANT, run_id)
    assert_stopped(message)


def test_parser_cli_reclaims_expired_lease_and_reuses_prepared(tmp_path, monkeypatch, capsys):
    from proofops_worker import main as worker_cli

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=now[0])[0]["message"])
    _, source, profile = runner._inputs(TENANT, run_id)
    runner.parser.parse(source, profile, tenant_id=TENANT)
    manifest = runner._directory(source, profile) / "manifest.json"
    before = manifest.read_bytes()
    assert store.claim_job(message, owner="stopped", now=now[0], lease_seconds=2) is not None
    now[0] += 3

    def forbidden(*args, **kwargs):
        raise AssertionError("Restart must reuse prepared artifacts")

    monkeypatch.setattr(runner.parser, "parse", forbidden)
    monkeypatch.setattr(worker_cli, "build_composition", lambda **kwargs: runner)
    monkeypatch.setattr(
        "sys.argv", ["proofops-worker", "--tenant-id", TENANT, "--run-id", run_id, "--once"]
    )
    worker_cli.main()
    assert capsys.readouterr().out.strip() == "committed"
    assert manifest.read_bytes() == before
    assert store.get_job(message)["fencing_token"] == 2
    assert store.get_usage(message, fencing_token=2)["artifact_reused"] is True
    assert store.read_checkpoint(message) is not None
    assert_stopped(message)
