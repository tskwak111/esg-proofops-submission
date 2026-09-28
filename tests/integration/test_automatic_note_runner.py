"""Automatic note worker, durable mode and per-request cost boundaries."""

import json
import sqlite3
from io import BytesIO

import pytest
from pypdf import PdfReader, PdfWriter

from tests.acceptance.test_parsing import TENANT
from tests.integration import test_local_parser_runner as fixtures


def setup(tmp_path, monkeypatch, *, tables=True):
    original = fixtures.pdf(table=tables)
    reader = PdfReader(BytesIO(original))
    writer = PdfWriter()
    for _ in range(3):
        writer.add_page(reader.pages[1])
    buffer = BytesIO()
    writer.write(buffer)
    monkeypatch.setattr(fixtures, "pdf", lambda **kwargs: buffer.getvalue())
    service, run_id, runner, now, stream = fixtures.runner_setup(tmp_path, monkeypatch)
    ledger = tmp_path / "test-only-ledger.sqlite"
    with sqlite3.connect(ledger) as db:
        db.execute(
            "CREATE TABLE probe_calls(request_id TEXT PRIMARY KEY, committed TEXT, receipt TEXT)"
        )
        db.execute("INSERT INTO probe_calls VALUES ('foreign', '7', NULL)")

    class Client:
        model = "synthetic-test"
        calls = 0

        def __init__(self):
            self.ledger = ledger
            self.after_call = lambda: None

        def summary(self):
            return {"calls": self.calls}

        def complete(self, system, content, *, request_id, **kwargs):
            self.calls += 1
            with sqlite3.connect(ledger) as db:
                db.execute(
                    "INSERT INTO probe_calls VALUES (?, '0.001', ?)",
                    (request_id, json.dumps({"input_tokens": 2, "output_tokens": 1})),
                )
            self.after_call()
            return {"content": '{"notes":[]}', "provider_model": self.model}

    client = Client()
    from proofops_worker.local_runner import LocalParserRunner

    runner = LocalParserRunner(
        service.store,
        service.uploads,
        runner.parser,
        profile=runner.profile,
        telemetry=runner.telemetry,
        clock=lambda: now[0],
        note_client=client,
    )
    return service, run_id, runner, now, client


def test_automatic_notes_recover_without_key_and_account_only_owned_requests(tmp_path, monkeypatch):
    service, run_id, runner, now, client = setup(tmp_path, monkeypatch)
    original_commit = service.store.jobs.commit_job

    def crash(*args, **kwargs):
        raise RuntimeError("crash before checkpoint")

    monkeypatch.setattr(service.store.jobs, "commit_job", crash)
    with pytest.raises(RuntimeError, match="crash before checkpoint"):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert client.calls == 3
    monkeypatch.setattr(service.store.jobs, "commit_job", original_commit)
    now[0] += 1000
    runner.note_client = None
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    graph = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    assert len([i for i in graph.issues if i.kind == "table_note_review"]) == 3
    usages = service.store.jobs.list_usage(TENANT, run_id)
    assert sum(u["model_calls"] for u in usages) == 3
    from decimal import Decimal

    assert sum(Decimal(u.get("committed_or_reserved_usd", "0")) for u in usages) == Decimal("0.003")
    assert client.calls == 3
    from proofops.application.ports.jobs import JobMessage

    run = service.store.jobs.get_run(TENANT, run_id)
    envelope = json.loads(service.store.jobs.read_checkpoint(JobMessage(**run["parse_job"])))
    assert envelope["schema"] == "local_parser_checkpoint_v3"
    assert envelope["note_review_policy_sha256"]


def test_cancellation_between_note_pages_prevents_next_call(tmp_path, monkeypatch):
    service, run_id, runner, now, client = setup(tmp_path, monkeypatch)

    def cancel():
        run = service.store.jobs.get_run(TENANT, run_id)
        service.store.jobs.cancel_run(
            TENANT,
            run_id,
            expected_revision=run["revision"],
            now=now[0],
            idempotency_key="note-cancel-test-0001",
            reason="test cancellation",
            actor_sub="test",
        )

    client.after_call = cancel
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "discarded"
    assert client.calls == 1
    assert "parse_job" not in service.store.jobs.get_run(TENANT, run_id)


@pytest.mark.parametrize("change", ["disable", "model", "manual"])
def test_automatic_mode_cannot_change_after_preparation(tmp_path, monkeypatch, change):
    service, run_id, runner, now, client = setup(tmp_path, monkeypatch)
    commit = service.store.jobs.commit_job

    def crash(*args, **kwargs):
        raise RuntimeError("checkpoint crash")

    if change == "disable":
        client.after_call = crash
    monkeypatch.setattr(service.store.jobs, "commit_job", crash)
    with pytest.raises(RuntimeError):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    monkeypatch.setattr(service.store.jobs, "commit_job", commit)
    now[0] += 1000
    arguments = {}
    if change == "disable":
        # The first request crashed before note binding.
        runner.note_client = None
    elif change == "model":
        client.model = "changed-model"
    else:
        runner.note_client = None
        arguments["note_review_artifacts"] = ("{}",)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id, **arguments) in {"retry", "failed"}
    assert "parse_job" not in service.store.jobs.get_run(TENANT, run_id)
    assert client.calls == (1 if change == "disable" else 3)


def test_orphan_usage_reconciles_once_and_rejects_foreign_or_duplicate_ids(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobConflict, JobMessage

    service, run_id, runner, now, client = setup(tmp_path, monkeypatch)
    record = service.store.jobs.record_usage
    captured = []

    def crash(lease, usage):
        captured.append((lease, usage))
        raise RuntimeError("crash before usage")

    monkeypatch.setattr(service.store.jobs, "record_usage", crash)
    with pytest.raises(RuntimeError):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert not service.store.jobs.list_usage(TENANT, run_id)
    monkeypatch.setattr(service.store.jobs, "record_usage", record)
    now[0] += 1000
    runner.note_client = None
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert client.calls == 3
    assert sum(u["model_calls"] for u in service.store.jobs.list_usage(TENANT, run_id)) == 3
    old_lease, old_usage = captured[0]
    with pytest.raises(JobConflict, match="already recorded"):
        record(old_lease, old_usage)
    from uuid import uuid4

    with pytest.raises(JobConflict, match="not registered"):
        record(old_lease, {"note_request_ids": [str(uuid4())]})
    run = service.store.jobs.get_run(TENANT, run_id)
    assert len(service.store.jobs.parser_note_requests(JobMessage(**run["parse_job"]))) == 3


def test_unsettled_note_request_never_publishes_and_can_reconcile_later(tmp_path, monkeypatch):
    service, run_id, runner, now, client = setup(tmp_path, monkeypatch)
    request_ids = []

    def fail(system, content, *, request_id, **kwargs):
        request_ids.append(request_id)
        with sqlite3.connect(client.ledger) as db:
            db.execute("INSERT INTO probe_calls VALUES (?, '1.00', NULL)", (request_id,))
        raise ValueError("UPSTAGE_REQUEST_FAILED")

    client.complete = fail
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) in {"retry", "failed"}
    assert "parse_job" not in service.store.jobs.get_run(TENANT, run_id)
    assert service.cost(TENANT, run_id)["amount"] is None
    # A provider response is settled later; only the test ledger is edited.
    with sqlite3.connect(client.ledger) as db:
        db.execute(
            "UPDATE probe_calls SET committed='0.001', receipt=? WHERE request_id=?",
            (json.dumps({"input_tokens": 2, "output_tokens": 1}), request_ids[0]),
        )
    now[0] += 1000
    runner.note_client = None
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert len(request_ids) == 1
    assert service.cost(TENANT, run_id)["amount"] == "0.001"


def test_takeover_before_reservation_does_not_finalize_zero_cost(tmp_path, monkeypatch):
    service, run_id, runner, now, client = setup(tmp_path, monkeypatch)
    complete = client.complete
    takeovers = []

    def delayed(system, content, **kwargs):
        now[0] += 1000
        takeovers.append(runner.run_once(tenant_id=TENANT, run_id=run_id))
        return complete(system, content, **kwargs)

    client.complete = delayed
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "discarded"
    assert takeovers == ["retry"]
    usages = service.store.jobs.list_usage(TENANT, run_id)
    assert sum(u["model_calls"] for u in usages) == 1
    assert service.cost(TENANT, run_id)["amount"] == "0.001"


@pytest.mark.parametrize("mode", ["automatic", "wrong-stage", "mixed"])
def test_note_cli_mode_is_explicit_and_rejects_invalid_combinations(monkeypatch, mode):
    from types import SimpleNamespace
    from uuid import uuid4

    from proofops_worker import main

    calls = []
    runner = SimpleNamespace(
        run_once=lambda **kwargs: "committed",
        uploads=SimpleNamespace(close=lambda: None, registry=SimpleNamespace(close=lambda: None)),
    )

    def compose(**kwargs):
        calls.append(kwargs)
        return runner

    monkeypatch.setattr(main, "build_composition", compose)
    args = [
        "worker",
        "--once",
        "--tenant-id",
        TENANT,
        "--run-id",
        str(uuid4()),
        "--review-table-notes",
    ]
    if mode == "wrong-stage":
        args += ["--stage", "extract"]
    if mode == "mixed":
        args += ["--note-review-artifact", "customer-secret-not-read"]
    monkeypatch.setattr("sys.argv", args)
    if mode == "automatic":
        main.main()
        assert calls == [
            {
                "stage": "parse",
                "review_table_notes": True,
                "verify_paragraphs": False,
                "native_typography_tolerance": False,
                "raster_ocr": False,
            }
        ]
    else:
        with pytest.raises(SystemExit) as error:
            main.main()
        assert error.value.code == 1
        assert not calls


def test_no_tables_still_reviews_selected_pages_and_replays_without_calls(tmp_path, monkeypatch):
    service, run_id, runner, now, client = setup(tmp_path, monkeypatch, tables=False)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert client.calls == 3
    assert not any(
        b.kind == "table" for b in runner.load_graph(tenant_id=TENANT, run_id=run_id).blocks
    )
    graph = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    assert {i.page_num for i in graph.issues if i.kind == "table_note_review"} == {1, 2, 3}
    from proofops.adapters.local.run_artifacts import validate_automatic_note_reviews
    from proofops.application.ports.jobs import JobMessage

    message = JobMessage(**service.store.jobs.get_run(TENANT, run_id)["parse_job"])
    artifacts = service.store.jobs.parser_note_reviews(message)
    policy = service.store.jobs.parser_note_policy(message)
    for bad, selected in (
        (artifacts[:-1], (1, 2, 3)),
        (artifacts[:1] * 3, (1, 2, 3)),
        (artifacts, (1, 2, 4)),
    ):
        with pytest.raises(ValueError):
            validate_automatic_note_reviews(graph, bad, policy, selected_pages=selected)
    runner.note_client = None
    runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert client.calls == 3
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id) == graph


def test_legacy_no_table_coverage_remains_unexamined():
    from types import SimpleNamespace

    from proofops.adapters.local.run_artifacts import validate_automatic_note_reviews

    graph = SimpleNamespace(blocks=())
    policy = dict(
        mode="automatic_v1", model="old", extractor_sha256="a" * 64, batch_sha256="b" * 64
    )
    validate_automatic_note_reviews(graph, (), policy, selected_pages=(1, 2, 3))
    with pytest.raises(ValueError):
        validate_automatic_note_reviews(
            graph, (), dict(policy, mode="automatic_pages_v2"), selected_pages=(1, 2, 3)
        )
