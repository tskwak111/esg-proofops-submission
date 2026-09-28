"""Local extraction publication through actual upload/parser and immutable replay."""

import json
import sqlite3
from dataclasses import asdict
from hashlib import sha256
from uuid import uuid4

import pytest
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.application.ports.jobs import JobMessage
from proofops_agent.extraction import StructuredClaimExtractor, SyntheticClaimExtractor
from proofops_worker.extract_runner import LocalExtractRunner

from tests.integration.test_local_parser_runner import TENANT, runner_setup


def test_extract_storage_and_runner_interfaces():
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops_worker.extract_runner import LocalExtractRunner

    assert all(hasattr(LocalClaimStore, name) for name in ("load", "list", "get", "load_snapshot"))
    assert hasattr(LocalExtractRunner, "run_once")


def extraction_setup(tmp_path, monkeypatch, *, extractor=None):
    extractor = extractor or SyntheticClaimExtractor()
    from tests.acceptance.test_upload import http_client
    from tests.integration import test_run_lifecycle as lifecycle

    original_setup = lifecycle.setup

    def setup(directory):
        service, body = original_setup(directory)
        service.extraction_profile = extractor.profile
        service.extraction_mode = "local_synthetic"
        document = service.uploads.version_snapshot(TENANT, body["document_version_id"])
        data = service.uploads.read_original(TENANT, body["document_version_id"])
        with http_client(service.uploads) as client:
            ticket_response = client.post(
                f"/v1/documents/{document['document_id']}/versions",
                json=dict(
                    document["metadata"],
                    filename="synthetic.pdf",
                    size_bytes=len(data),
                    sha256=sha256(data).hexdigest(),
                ),
            )
            assert ticket_response.status_code == 201, ticket_response.text
            ticket = ticket_response.json()
            uploaded = client.post(
                ticket["post_url"],
                data=ticket["post_fields"],
                files={"file": ("synthetic.pdf", data, "application/pdf")},
            )
            assert uploaded.status_code == 204, uploaded.text
            completed = client.post(
                f"/v1/uploads/{ticket['upload_id']}/complete",
                json=dict(sha256=sha256(data).hexdigest(), size_bytes=len(data)),
            )
            assert completed.status_code == 202, completed.text
            body["document_version_id"] = completed.json()["resource_id"]
        return service, body

    monkeypatch.setattr(lifecycle, "setup", setup)
    service, run_id, parser, now, stream = runner_setup(tmp_path, monkeypatch)
    message = JobMessage(
        **service.store.jobs.pending_outbox(TENANT, run_id, now=now[0])[0]["message"]
    )
    assert parser.run_once(tenant_id=TENANT, run_id=run_id) == "committed", (
        service.store.jobs.get_job(message).get("error_code"),
        stream.getvalue(),
    )
    runner = LocalExtractRunner(
        service.store,
        service.uploads,
        parser.parser,
        extractor=extractor,
        telemetry=parser.telemetry,
        clock=lambda: now[0],
    )
    return service, run_id, runner, now, stream


def extract_message(service, run_id, now):
    return next(
        JobMessage(**event["message"])
        for event in service.store.jobs.pending_outbox(TENANT, run_id, now=now)
        if event["message"]["stage"] == "extract"
    )


def test_actual_pdf_http_parse_extract_reopen_duplicate(tmp_path, monkeypatch):
    service, run_id, runner, now, stream = extraction_setup(tmp_path, monkeypatch)
    message = extract_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    discovery = runner.claims.load(TENANT, run_id)
    assert discovery.claims and discovery.synthetic
    assert all(claim.source_quality == "unverified" for claim in discovery.claims)
    assert all(claim.to_summary()["decision"] is None for claim in discovery.claims)
    checkpoint = service.store.jobs.read_checkpoint(message)
    envelope = json.loads(checkpoint)
    assert envelope["discovery"] == json.loads(json.dumps(asdict(discovery)))
    assert envelope["extraction_profile"] == asdict(SyntheticClaimExtractor.profile)
    assert envelope["coverage"]["claims_discovered"] == len(discovery.claims)
    assert envelope["coverage"]["chunks_discovered"] >= len(discovery.processed_source_ids)
    assert envelope["coverage"]["pages_unreadable"] == 2
    assert envelope["coverage"]["complete"] is False
    assert envelope["synthetic"] is True
    reopened = LocalClaimStore(
        LocalSQLiteRunStore(service.store.path), service.uploads, runner.parser
    )
    assert reopened.load(TENANT, run_id) == discovery
    assert reopened.get(TENANT, run_id, discovery.claims[0].claim_id) == discovery.claims[0]
    assert reopened.list(TENANT, run_id) == discovery.claims
    assert reopened.load_snapshot(TENANT, run_id) == envelope
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "pending_downstream"
    assert service.store.jobs.read_checkpoint(message) == checkpoint
    run = service.get(TENANT, run_id)
    assert run["status"] == "running" and run["current_stage"] == "tag"
    events = service.store.jobs.pending_outbox(TENANT, run_id, now=now[0])
    assert len(events) == 1 and events[0]["message"]["stage"] == "tag"
    assert service.store.jobs.get_usage(message, fencing_token=1)["model_calls"] == 0
    assert "1234 tCO2e" not in stream.getvalue()
    assert "raw_response_json" not in stream.getvalue()
    with pytest.raises(KeyError):
        reopened.get(TENANT, run_id, str(uuid4()))
    with pytest.raises(Exception):
        reopened.list(str(uuid4()), run_id)


@pytest.mark.parametrize("boundary", ["cancel_before", "cancel_inflight", "stale", "crash"])
def test_atomic_fence_cancel_and_recovery(tmp_path, monkeypatch, boundary):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = extract_message(service, run_id, now[0])
    before = service.get(TENANT, run_id)["coverage"]
    original_extract = runner.extractor.extract

    def cancel():
        jobs.cancel_run(
            TENANT,
            run_id,
            expected_revision=jobs.get_run(TENANT, run_id)["revision"],
            idempotency_key=str(uuid4()),
            reason="test cancel",
            actor_sub="synthetic-test",
            now=now[0],
        )

    if boundary == "cancel_before":
        cancel()

        def forbidden(packet):
            raise AssertionError("extractor must not run")

        monkeypatch.setattr(runner.extractor, "extract", forbidden)
    elif boundary in {"cancel_inflight", "stale"}:

        def interrupt(packet):
            response = original_extract(packet)
            if boundary == "cancel_inflight":
                cancel()
            else:
                now[0] += 1000
            return response

        monkeypatch.setattr(runner.extractor, "extract", interrupt)
    else:
        original_event = jobs._event

        def crash(db, message, now, cause):
            original_event(db, message, now, cause)
            if message.stage == "tag":
                raise RuntimeError("crash inside commit after artifact and outbox insertion")

        monkeypatch.setattr(jobs, "_event", crash)
    if boundary == "crash":
        with pytest.raises(RuntimeError, match="crash inside commit"):
            runner.run_once(tenant_id=TENANT, run_id=run_id)
    else:
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == (
            "cancelled" if boundary == "cancel_before" else "discarded"
        )
    assert jobs.read_checkpoint(message) is None
    assert service.get(TENANT, run_id)["coverage"] == before
    assert "extract_job" not in jobs.get_run(TENANT, run_id)
    with sqlite3.connect(service.store.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE kind='job' AND "
                "json_extract(CAST(value AS TEXT), '$.message.stage')='tag'"
            ).fetchone()[0]
            == 0
        )
    with pytest.raises(KeyError):
        runner.claims.load(TENANT, run_id)
    if boundary in {"crash", "stale"}:
        if boundary == "crash":
            monkeypatch.setattr(jobs, "_event", original_event)
        monkeypatch.setattr(runner.extractor, "extract", original_extract)
        now[0] += 1000
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
        assert runner.claims.list(TENANT, run_id)


def test_failed_unknown_excluded_chunks_preserve_denominator(tmp_path, monkeypatch):
    def respond(packet):
        source = packet["untrusted_document_data"]
        text = source["text"]
        if "1234" in text:
            raise TimeoutError
        return dict(
            spans=[
                dict(
                    char_start=0,
                    char_end=len(text),
                    quote=text,
                    kind="excluded",
                    reason="synthetic-test-exclusion",
                    topic_ids=[],
                )
            ]
        )

    extractor = StructuredClaimExtractor(SyntheticClaimExtractor.profile, respond)
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch, extractor=extractor)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    result = runner.claims.load(TENANT, run_id)
    assert any(receipt.status == "failed" for receipt in result.receipts)
    assert any(exclusion.state == "unknown" for exclusion in result.exclusions)
    assert any(exclusion.state == "excluded" for exclusion in result.exclusions)
    assert not result.claims
    coverage = service.get(TENANT, run_id)["coverage"]
    assert coverage["chunks_processed"] < coverage["chunks_discovered"]
    assert coverage["claims_decided"] == 0 and not coverage["complete"]


def test_snapshot_tampering_rejected_even_if_checkpoint_pointer_rehashed(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    message = extract_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    jobs = service.store.jobs
    with jobs._transaction() as db:
        job = jobs._job(db, message)
        envelope = json.loads(jobs._raw(db, TENANT, run_id, "artifact", job["artifact_ref"]["key"]))
        envelope["discovery"]["claims"][0]["source_quality"] = "verified"
        raw = json.dumps(envelope).encode()
        jobs._put(db, TENANT, run_id, "artifact", job["artifact_ref"]["key"], raw)
        job["artifact_ref"].update(sha256=sha256(raw).hexdigest(), byte_size=len(raw))
        jobs._save_job(db, message, job)
        run = jobs._get(db, TENANT, run_id, "run", "META")
        run["claim_snapshot_sha256"] = sha256(raw).hexdigest()
        jobs._put(db, TENANT, run_id, "run", "META", run)
    with pytest.raises(ValueError, match="REPLAY_MISMATCH"):
        runner.claims.load(TENANT, run_id)


def test_superseded_delivery_cannot_overwrite_new_checkpoint(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = extract_message(service, run_id, now[0])
    original = runner.extractor.extract
    committed = []

    def supersede(packet):
        response = original(packet)
        monkeypatch.setattr(runner.extractor, "extract", original)
        now[0] += 1000
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
        committed.append(jobs.read_checkpoint(message))
        return response

    monkeypatch.setattr(runner.extractor, "extract", supersede)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "discarded"
    assert jobs.read_checkpoint(message) == committed[0]
    assert jobs.get_job(message)["fencing_token"] == 2
    assert jobs.get_usage(message, fencing_token=1)["model_calls"] == 0
    assert jobs.get_usage(message, fencing_token=2)["model_calls"] == 0
    assert runner.claims.list(TENANT, run_id)


@pytest.mark.parametrize("pin", ["model_sha256", "prompt_sha256", "rule_sha256"])
def test_mismatched_extractor_pins_fail_before_call(tmp_path, monkeypatch, pin):
    from dataclasses import replace

    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    message = extract_message(service, run_id, now[0])
    monkeypatch.setattr(
        runner.extractor, "profile", replace(runner.extractor.profile, **{pin: "f" * 64})
    )

    def forbidden(packet):
        raise AssertionError("mismatched extractor must not run")

    monkeypatch.setattr(runner.extractor, "extract", forbidden)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    assert service.store.jobs.read_checkpoint(message) is None
    assert service.store.jobs.get_usage(message, fencing_token=1)["extractor_calls"] == 0


@pytest.mark.parametrize(
    "field",
    [
        "input_hash",
        "schema",
        "manifest_sha256",
        "extraction_profile_hash",
        "rule_pack_sha256",
        "model_binding_hash",
        "coverage",
    ],
)
def test_invalid_commit_payload_cannot_publish_any_partial_state(tmp_path, monkeypatch, field):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = extract_message(service, run_id, now[0])
    original = jobs.commit_job
    coverage = service.get(TENANT, run_id)["coverage"]

    def invalid(lease, *, payload, now, next_job=None):
        value = json.loads(payload)
        if field == "coverage":
            value[field]["complete"] = True
        else:
            value[field] = "0" * 64
        return original(lease, payload=json.dumps(value).encode(), now=now, next_job=next_job)

    monkeypatch.setattr(jobs, "commit_job", invalid)
    with pytest.raises(ValueError):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert jobs.read_checkpoint(message) is None
    assert "extract_job" not in jobs.get_run(TENANT, run_id)
    assert service.get(TENANT, run_id)["coverage"] == coverage
    assert all(
        event["message"]["stage"] != "tag"
        for event in jobs.pending_outbox(TENANT, run_id, now=now[0])
    )


def test_explicit_worker_cli_extract_and_reopen(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    import time
    from datetime import UTC, datetime

    from proofops.application.registry import Registry
    from proofops.application.uploads import UploadService

    from tests.acceptance import test_preflight
    from tests.integration import test_run_lifecycle as lifecycle

    database = tmp_path / "runs.sqlite"
    uploads = UploadService(database, tmp_path / "objects", Registry.sqlite(database))
    original_upload = lifecycle.setup_upload
    monkeypatch.setattr(lifecycle, "NOW", int(time.time()))
    monkeypatch.setattr(test_preflight, "NOW", datetime.now(UTC).replace(microsecond=0).isoformat())
    monkeypatch.setattr(
        lifecycle,
        "setup_upload",
        lambda directory, **kwargs: original_upload(directory, service=uploads, **kwargs),
    )
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    # The CLI expects the existing parser artifact directory used by composition.
    (tmp_path / "prepared").rename(tmp_path / "parser-prepared")
    from proofops.adapters.local.run_artifacts import load_run_inputs
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser

    runner.parser = OpenDataLoaderParser(tmp_path / "parser-prepared")
    _, _, profile = load_run_inputs(service.store, uploads, tenant_id=TENANT, run_id=run_id)
    config = tmp_path / "parser-config.json"
    config.write_text(json.dumps(profile.config_snapshot()))
    env = dict(
        os.environ,
        LOCAL_DATABASE_PATH=str(database),
        LOCAL_PARSER_PROFILE_PATH=str(config),
        APP_ENV="local",
        MODEL_ADAPTER="synthetic",
    )
    command = [
        sys.executable,
        "-m",
        "proofops_worker.main",
        "--tenant-id",
        TENANT,
        "--run-id",
        run_id,
        "--once",
        "--stage",
        "extract",
    ]
    blocked = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert blocked.returncode == 1
    env["LOCAL_EXTRACTION_MODE"] = "local_synthetic"
    first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stdout.strip().endswith("committed")
    assert "1234 tCO2e" not in first.stdout + first.stderr
    second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert second.returncode == 0 and second.stdout.strip() == "pending_downstream"
    store = LocalClaimStore(LocalSQLiteRunStore(database), uploads, runner.parser)
    assert store.list(TENANT, run_id)


def test_manual_retry_reuses_parser_and_publishes_after_profile_restored(tmp_path, monkeypatch):
    from dataclasses import replace

    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = extract_message(service, run_id, now[0])
    profile = runner.extractor.profile
    monkeypatch.setattr(runner.extractor, "profile", replace(profile, prompt_sha256="f" * 64))
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    assert jobs.read_checkpoint(message) is None
    monkeypatch.setattr(runner.extractor, "profile", profile)
    jobs.retry_run(
        TENANT,
        run_id,
        expected_revision=jobs.get_run(TENANT, run_id)["revision"],
        idempotency_key=str(uuid4()),
        reason="restore pinned synthetic extractor",
        actor_sub="synthetic-test",
        now=now[0],
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert runner.claims.list(TENANT, run_id)
    assert jobs.get_job(message)["fencing_token"] == 2


def test_unpinned_legacy_run_never_silently_selects_synthetic(tmp_path, monkeypatch):
    service, run_id, parser, now, stream = runner_setup(tmp_path, monkeypatch)
    assert parser.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    extractor = SyntheticClaimExtractor()

    def forbidden(packet):
        raise AssertionError("legacy run must not silently select an extractor")

    monkeypatch.setattr(extractor, "extract", forbidden)
    runner = LocalExtractRunner(
        service.store,
        service.uploads,
        parser.parser,
        extractor=extractor,
        telemetry=parser.telemetry,
        clock=lambda: now[0],
    )
    message = extract_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    assert service.store.jobs.read_checkpoint(message) is None
    assert service.store.jobs.get_usage(message, fencing_token=1)["extractor_calls"] == 0


def test_cancel_between_call_check_and_heartbeat_retains_usage(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = extract_message(service, run_id, now[0])
    original = jobs.heartbeat

    def cancel(lease, *, now, lease_seconds):
        jobs.cancel_run(
            TENANT,
            run_id,
            expected_revision=jobs.get_run(TENANT, run_id)["revision"],
            idempotency_key=str(uuid4()),
            reason="race test",
            actor_sub="synthetic-test",
            now=now,
        )
        return original(lease, now=now, lease_seconds=lease_seconds)

    monkeypatch.setattr(jobs, "heartbeat", cancel)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "discarded"
    assert jobs.read_checkpoint(message) is None
    assert jobs.get_usage(message, fencing_token=1) == {"model_calls": 0, "extractor_calls": 0}


def test_extract_replays_published_note_graph_after_restart(tmp_path, monkeypatch):
    from proofops.adapters.local.table_notes import freeze_note_review, prepare, validate
    from proofops.domain.provenance import canonical_hash
    from proofops_worker.local_runner import LocalParserRunner

    run_parse = LocalParserRunner.run_once

    def parse_with_notes(self, *, tenant_id, run_id):
        _, source, profile = self._inputs(tenant_id, run_id)
        graph = self.parser.parse(source, profile, tenant_id=tenant_id)
        table_id = next(b.source_id for b in graph.blocks if b.kind == "table")
        packet = prepare(graph, source.content, [table_id], tenant_id=tenant_id)
        checked = validate({"notes": []}, packet, graph, source.content, tenant_id=tenant_id)
        artifact = freeze_note_review(graph, source.content, packet, checked, tenant_id=tenant_id)
        return run_parse(
            self, tenant_id=tenant_id, run_id=run_id, note_review_artifacts=(artifact,)
        )

    monkeypatch.setattr(LocalParserRunner, "run_once", parse_with_notes)
    service, run_id, runner, now, stream = extraction_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    reopened = LocalSQLiteRunStore(service.store.path)
    claims = LocalClaimStore(reopened, service.uploads, runner.parser)
    snapshot = claims.load_snapshot(TENANT, run_id)
    from proofops.adapters.local.run_artifacts import load_run_graph

    graph = load_run_graph(
        reopened, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    assert any(i.kind == "table_note_review" and i.state == "open" for i in graph.issues)
    assert snapshot["graph_sha256"] == canonical_hash(asdict(graph))
    assert claims.load(TENANT, run_id).synthetic
    assert snapshot["coverage"]["complete"] is False
