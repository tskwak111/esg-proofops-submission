"""Actual local parsing, durable replay, and fenced publication; approvals synthetic."""

from dataclasses import replace
from hashlib import sha256
from io import StringIO
from uuid import uuid4

import pytest
from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
from proofops.application.ingest.graph_fusion import ParserProfile

from tests.acceptance.test_parsing import JAVA, MANIFEST, TENANT, pdf, source


def test_stable_config_excludes_invocation_identity_but_binds_all_parser_options():
    profile = ParserProfile(MANIFEST, java_executable=JAVA)
    assert hasattr(profile, "config_hash"), "ParserProfile needs stable executable config hashing"
    assert (
        profile.config_hash()
        == replace(profile, parse_manifest_id=str(uuid4()), physical_pages=(2,)).config_hash()
    )
    assert profile.config_hash() != replace(profile, table_auxiliary=False).config_hash()
    assert "parse_manifest_id" not in profile.config_snapshot()
    assert "physical_pages" not in profile.config_snapshot()


def test_actual_verified_reload_preserves_rich_graph_and_rejects_tampering(tmp_path):
    parser = OpenDataLoaderParser(tmp_path)
    assert hasattr(parser, "load_verified"), "Parser needs verified immutable replay"
    item, profile = source(pdf(table=True)), ParserProfile(MANIFEST, java_executable=JAVA)
    graph = parser.parse(item, profile, tenant_id=TENANT)
    loaded = parser.load_verified(item, profile, tenant_id=TENANT)
    assert loaded == graph
    assert any(issue.kind == "table_vision_not_run" for issue in loaded.issues)
    assert any(len(block.sources) == 2 for block in loaded.blocks)
    for bad_item, bad_profile, tenant in [
        (replace(item, object_version_id="other"), profile, TENANT),
        (replace(item, document_id=str(uuid4())), profile, TENANT),
        (replace(item, sha256="0" * 64), profile, TENANT),
        (item, replace(profile, physical_pages=(2,)), TENANT),
        (item, profile, str(uuid4())),
    ]:
        with pytest.raises(ParseFailure):
            parser.load_verified(bad_item, bad_profile, tenant_id=tenant)
    target = tmp_path / TENANT / item.document_version_id / MANIFEST / "candidates.json"
    target.chmod(0o600)
    target.write_text("[]")
    with pytest.raises(ParseFailure):
        parser.load_verified(item, profile, tenant_id=TENANT)


def test_manifest_binds_fusion_version_and_replays_legacy_without_rewriting(tmp_path):
    import json

    parser = OpenDataLoaderParser(tmp_path)
    item, profile = source(pdf(table=True)), ParserProfile(MANIFEST, java_executable=JAVA)
    graph = parser.parse(item, profile, tenant_id=TENANT)
    path = tmp_path / TENANT / item.document_version_id / MANIFEST / "manifest.json"
    manifest = json.loads(path.read_bytes())
    assert manifest["fusion_version"] == 3
    assert parser.load_verified(item, profile, tenant_id=TENANT) == graph
    path.chmod(0o600)
    for invalid in (5, "3", True, None):
        path.write_text(json.dumps({**manifest, "fusion_version": invalid}))
        with pytest.raises(ParseFailure, match="PARSER_ARTIFACT_INTEGRITY_MISMATCH"):
            parser.load_verified(item, profile, tenant_id=TENANT)
    # This simple table has identical v1/v2/v3 grouping.
    path.write_text(json.dumps({**manifest, "fusion_version": 2}))
    assert parser.load_verified(item, profile, tenant_id=TENANT) == graph
    # Old manifests omit the version.
    del manifest["fusion_version"]
    path.write_text(json.dumps(manifest))
    before = path.read_bytes()
    assert parser.load_verified(item, profile, tenant_id=TENANT) == graph
    assert path.read_bytes() == before


def test_out_of_page_geometry_preserves_raw_candidate_without_usable_source(tmp_path, monkeypatch):
    import json

    parser = OpenDataLoaderParser(tmp_path)
    execute = parser._execute

    def out_of_page_box(command, work, env, profile):
        execute(command, work, env, profile)
        path = work / "source.json"
        raw = json.loads(path.read_bytes())
        raw["kids"][0]["bounding box"] = [-10, 10, 100, 30]
        path.write_text(json.dumps(raw))

    monkeypatch.setattr(parser, "_execute", out_of_page_box)
    item, profile = source(pdf(table=True)), ParserProfile(MANIFEST, java_executable=JAVA)
    graph = parser.parse(item, profile, tenant_id=TENANT)
    (issue,) = (i for i in graph.issues if i.kind == "source_geometry_invalid")
    assert issue.state == "unreadable"
    invalid = next(b for b in graph.blocks if b.source_id in issue.source_ids)
    assert invalid.bbox is None and invalid.quality == "unlocated"
    assert invalid.candidates[0].parser_bbox == (-10, 10, 100, 30)
    with pytest.raises(ValueError):
        invalid.source_ref()
    assert parser.load_verified(item, profile, tenant_id=TENANT) == graph
    assert any(b.bbox is not None for b in graph.blocks)


def runner_setup(tmp_path, monkeypatch, table_text_y_tolerance=None):
    from proofops.adapters.aws.bedrock import BedrockInvoker
    from proofops.adapters.local.models import SyntheticTagger

    def forbidden_model(*args, **kwargs):
        raise AssertionError("Local parser executor must never call a product model")

    monkeypatch.setattr(BedrockInvoker, "invoke", forbidden_model)
    monkeypatch.setattr(SyntheticTagger, "tag", forbidden_model)
    from proofops.application.telemetry import Telemetry

    from tests.integration import test_run_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "pdf", lambda count: pdf(table=True))
    service, body = lifecycle.setup(tmp_path)
    profile = ParserProfile(
        MANIFEST, java_executable=JAVA, table_text_y_tolerance=table_text_y_tolerance
    )
    assert hasattr(profile, "config_hash"), "ParserProfile needs stable executable config hashing"
    service.parser_profile_hash = profile.config_hash()
    service.parser_profile = profile.config_snapshot()
    http, _ = lifecycle.client(service)
    response = http.post("/v1/runs", json=body)
    assert response.status_code == 202, response.text
    from proofops_worker.local_runner import LocalParserRunner

    stream = StringIO()
    now = [lifecycle.NOW]
    runner = LocalParserRunner(
        service.store,
        service.uploads,
        OpenDataLoaderParser(tmp_path / "prepared"),
        profile=profile,
        telemetry=Telemetry(service="worker", env="test", stream=stream, hash_key=b"x" * 32),
        clock=lambda: now[0],
    )
    return service, response.json()["run_id"], runner, now, stream


@pytest.mark.parametrize("tolerance", [None, 4])
def test_http_upload_create_actual_parse_reopen_replay(tmp_path, monkeypatch, tolerance):
    import json

    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, stream = runner_setup(tmp_path, monkeypatch, tolerance)
    event = service.store.jobs.pending_outbox(TENANT, run_id, now=now[0])[0]
    message = JobMessage(**event["message"])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    checkpoint = service.store.jobs.read_checkpoint(message)
    envelope = json.loads(checkpoint)
    assert envelope["input_hash"] == message.input_hash
    assert envelope["stage_status"] == "completed"
    assert envelope["downstream_status"] == "pending"
    assert envelope["validation_profile"] == "fast_preview"
    graph = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    assert any(block.raw_text == "1234 tCO2e" for block in graph.blocks)
    assert {c.parser_name for c in graph.candidates} == {"opendataloader", "pdfplumber"}
    assert any(i.kind == "table_vision_not_run" for i in graph.issues)
    store = LocalSQLiteRunStore(service.store.path)
    runner.store = store
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "pending_downstream"
    assert store.jobs.read_checkpoint(message) == checkpoint
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id) == graph
    assert store.get(TENANT, run_id)["status"] != "completed"
    assert store.get(TENANT, run_id)["current_stage"] == "extract"
    coverage = store.get(TENANT, run_id)["coverage"]
    assert coverage["pages_processed"] + coverage["pages_unreadable"] == 3
    # OD treats repeated top-of-page text as headers: these pages remain unreadable.
    assert coverage["pages_unreadable"] == 2
    assert {i.page_num for i in graph.issues if i.state == "unreadable"} == {1, 3}
    assert store.get(TENANT, run_id)["coverage"]["complete"] is False
    assert "Page 2 emissions" not in stream.getvalue()
    assert all(json.loads(line)["stage"] == "PARSE" for line in stream.getvalue().splitlines())
    assert store.jobs.get_usage(message, fencing_token=1)["model_calls"] == 0
    assert sha256(checkpoint).hexdigest() == store.jobs.get_job(message)["artifact_ref"]["sha256"]


def test_parse_commit_advances_pending_stage_atomically(tmp_path):
    import json

    from proofops.application.ports.jobs import JobMessage

    from tests.integration.test_run_lifecycle import NOW, client, setup

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
    envelope = dict(
        schema="local_parser_checkpoint_v1",
        input_hash=message.input_hash,
        stage_status="completed",
        downstream_status="pending",
        coverage=coverage,
    )
    next_job = replace(message, job_id=str(uuid4()), stage="extract")
    assert store.commit_job(
        lease, payload=json.dumps(envelope).encode(), now=NOW, next_job=next_job
    )
    assert service.get(TENANT, run_id)["current_stage"] == "extract"
    assert service.get(TENANT, run_id)["coverage"] == coverage
    assert store.get_job(next_job)["status"] == "pending"


def test_crash_before_checkpoint_reuses_exact_manifest_without_reparsing(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=now[0])[0]["message"])
    original_commit = store.commit_job

    def crash(*args, **kwargs):
        raise RuntimeError("injected crash before checkpoint")

    monkeypatch.setattr(store, "commit_job", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    manifests = list(runner.parser.artifact_root.rglob("manifest.json"))
    assert len(manifests) == 1
    original_manifest = manifests[0].read_bytes()
    with pytest.raises(ParseFailure, match="PARSE_NOT_PUBLISHED"):
        runner.load_graph(tenant_id=TENANT, run_id=run_id)
    now[0] += 1000
    monkeypatch.setattr(store, "commit_job", original_commit)

    def forbidden(*args, **kwargs):
        raise AssertionError("Recovery must verify and reuse immutable parser artifacts")

    monkeypatch.setattr(runner.parser, "parse", forbidden)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert manifests[0].read_bytes() == original_manifest
    assert store.get_usage(message, fencing_token=2)["artifact_reused"] is True
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id).candidates


@pytest.mark.parametrize("boundary", ["cancel", "stale", "config", "source", "foreign"])
def test_forbidden_worker_cannot_publish(tmp_path, monkeypatch, boundary):
    from proofops.application.runs import RunRejected

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    original = runner.parser.parse
    if boundary in {"cancel", "stale"}:

        def interrupt(*args, **kwargs):
            result = original(*args, **kwargs)
            if boundary == "cancel":
                run = service.store.jobs.get_run(TENANT, run_id)
                service.store.jobs.cancel_run(
                    TENANT,
                    run_id,
                    expected_revision=run["revision"],
                    idempotency_key=str(uuid4()),
                    reason="Injected cancellation",
                    actor_sub="synthetic-fixture",
                    now=now[0],
                )
            else:
                now[0] += 1000
            return result

        monkeypatch.setattr(runner.parser, "parse", interrupt)
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "discarded"
    elif boundary == "config":
        runner.profile = replace(runner.profile, table_auxiliary=False)
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    elif boundary == "source":
        monkeypatch.setattr(service.uploads, "read_original", lambda *a: b"tampered PDF")
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    else:
        with pytest.raises(RunRejected, match="RESOURCE_NOT_FOUND"):
            runner.run_once(tenant_id=str(uuid4()), run_id=run_id)
    assert "parse_job" not in service.store.jobs.get_run(TENANT, run_id)
    assert service.store.get(TENANT, run_id)["coverage"]["pages_processed"] == 0


def test_worker_command_requires_scope_and_safe_runtime_logs(tmp_path):
    import os
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "proofops_worker.main", "--once"],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, "Worker must require explicit tenant and run"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "proofops_worker.main",
            "--once",
            "--tenant-id",
            TENANT,
            "--run-id",
            str(uuid4()),
        ],
        env={
            **os.environ,
            "LOCAL_DATABASE_PATH": str(tmp_path / "state.sqlite3"),
            "LOCAL_PARSER_PROFILE_PATH": str(tmp_path / "customer-secret-do-not-log"),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "customer-secret-do-not-log" not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr + result.stdout


def test_verified_load_rejects_self_consistent_manifest_with_lost_quality(tmp_path):
    import json

    parser = OpenDataLoaderParser(tmp_path)
    item, profile = source(pdf(table=True)), ParserProfile(MANIFEST, java_executable=JAVA)
    parser.parse(item, profile, tenant_id=TENANT)
    directory = tmp_path / TENANT / item.document_version_id / MANIFEST
    quality = directory / "quality.json"
    quality.chmod(0o600)
    issues = [i for i in json.loads(quality.read_bytes()) if i["kind"] != "table_vision_not_run"]
    quality.write_text(json.dumps(issues))
    manifest_path = directory / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["artifacts"]["quality.json"] = sha256(quality.read_bytes()).hexdigest()
    manifest_path.chmod(0o600)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ParseFailure):
        parser.load_verified(item, profile, tenant_id=TENANT)


@pytest.mark.parametrize("with_notes", [False, True])
def test_real_worker_command_reopens_uploaded_run_and_publishes_once(
    tmp_path, monkeypatch, with_notes
):
    import json
    import os
    import subprocess
    import sys
    import time
    from datetime import UTC, datetime

    from proofops.application.registry import Registry
    from proofops.application.uploads import UploadService

    from tests.integration import test_run_lifecycle as lifecycle

    original_setup_upload = lifecycle.setup_upload
    database = tmp_path / "runs.sqlite"
    uploads = UploadService(database, tmp_path / "objects", Registry.sqlite(database))
    from tests.acceptance import test_preflight

    monkeypatch.setattr(lifecycle, "NOW", int(time.time()))
    monkeypatch.setattr(test_preflight, "NOW", datetime.now(UTC).replace(microsecond=0).isoformat())
    monkeypatch.setattr(
        lifecycle,
        "setup_upload",
        lambda directory, **kwargs: original_setup_upload(directory, service=uploads, **kwargs),
    )
    service, run_id, runner, _, _ = runner_setup(tmp_path, monkeypatch)
    config = tmp_path / "parser-config.json"
    config.write_text(json.dumps(runner.profile.config_snapshot()))
    env = {
        **os.environ,
        "LOCAL_DATABASE_PATH": str(database),
        "LOCAL_PARSER_PROFILE_PATH": str(config),
        "APP_ENV": "local",
        "MODEL_ADAPTER": "synthetic",
    }
    command = [
        sys.executable,
        "-m",
        "proofops_worker.main",
        "--tenant-id",
        TENANT,
        "--run-id",
        run_id,
        "--once",
    ]
    note_args = []
    if with_notes:
        from proofops.adapters.local.table_notes import freeze_note_review, prepare, validate

        runner.parser = OpenDataLoaderParser(tmp_path / "parser-prepared")
        _, original, profile = runner._inputs(TENANT, run_id)
        graph = runner.parser.parse(original, profile, tenant_id=TENANT)
        table_id = next(b.source_id for b in graph.blocks if b.kind == "table")
        packet = prepare(graph, original.content, [table_id], tenant_id=TENANT)
        result = validate({"notes": []}, packet, graph, original.content, tenant_id=TENANT)
        artifact = freeze_note_review(graph, original.content, packet, result, tenant_id=TENANT)
        note_file = tmp_path / "customer-secret-note-artifact.json"
        note_args = ["--note-review-artifact", str(note_file)]
        for invalid in (b"\xff", b"x" * (16 * 1024 * 1024 + 1)):
            note_file.write_bytes(invalid)
            rejected = subprocess.run(
                command + note_args, env=env, capture_output=True, text=True, timeout=60
            )
            assert rejected.returncode != 0
            assert "customer-secret-note-artifact" not in rejected.stdout + rejected.stderr
            assert "parse_job" not in service.store.jobs.get_run(TENANT, run_id)
        note_file.write_text(artifact)

    first = subprocess.run(command + note_args, env=env, capture_output=True, text=True, timeout=60)
    assert first.returncode == 0, first.stdout + first.stderr
    assert first.stdout.strip().endswith("committed")
    assert "1234 tCO2e" not in first.stdout + first.stderr
    second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    assert second.returncode == 0
    assert second.stdout.strip() == "pending_downstream"
    runner.parser = OpenDataLoaderParser(tmp_path / "parser-prepared")
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id).candidates
    assert service.get(TENANT, run_id)["current_stage"] == "extract"
    if with_notes:
        assert any(
            i.kind == "table_note_review"
            for i in runner.load_graph(tenant_id=TENANT, run_id=run_id).issues
        )
        # A misplaced flag must fail before extraction, without echoing its sensitive path.
        wrong_stage = subprocess.run(
            command + ["--stage", "extract"] + note_args,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert wrong_stage.returncode != 0
        assert "customer-secret-note-artifact" not in wrong_stage.stdout + wrong_stage.stderr
        note_file.write_text("{}")
        changed = subprocess.run(
            command + note_args, env=env, capture_output=True, text=True, timeout=60
        )
        assert changed.returncode != 0
        assert "customer-secret-note-artifact" not in changed.stdout + changed.stderr
        assert any(
            i.kind == "table_note_review"
            for i in runner.load_graph(tenant_id=TENANT, run_id=run_id).issues
        )


def test_superseded_worker_cannot_replace_new_worker_checkpoint(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=now[0])[0]["message"])
    original = runner.parser.parse
    committed = []

    def supersede(*args, **kwargs):
        graph = original(*args, **kwargs)
        now[0] += 1000
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
        committed.append(store.read_checkpoint(message))
        return graph

    monkeypatch.setattr(runner.parser, "parse", supersede)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "discarded"
    assert store.read_checkpoint(message) == committed[0]
    assert store.get_job(message)["fencing_token"] == 2
    assert store.get_usage(message, fencing_token=1)["parser_executions"] == 1
    assert store.get_usage(message, fencing_token=2)["artifact_reused"] is True


def test_cancelled_before_delivery_never_prepares_or_calls_model(tmp_path, monkeypatch):
    from proofops.adapters.aws.bedrock import BedrockInvoker
    from proofops.adapters.local.models import SyntheticTagger

    service, run_id, runner, now, _ = runner_setup(tmp_path, monkeypatch)

    def forbidden(*args, **kwargs):
        raise AssertionError("No provider, synthetic tagger, or parser call is allowed")

    monkeypatch.setattr(BedrockInvoker, "invoke", forbidden)
    monkeypatch.setattr(SyntheticTagger, "tag", forbidden)
    monkeypatch.setattr(runner.parser, "parse", forbidden)
    service.store.jobs.cancel_run(
        TENANT,
        run_id,
        expected_revision=1,
        idempotency_key=str(uuid4()),
        reason="Cancel before worker starts",
        actor_sub="synthetic-fixture",
        now=now[0],
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "cancelled"
    assert not list(runner.parser.artifact_root.rglob("manifest.json"))


@pytest.mark.parametrize(
    "invalid", ["input_hash", "complete", "page_count", "stage", "next_job", "unprocessed"]
)
def test_invalid_parse_progress_cannot_partially_commit(tmp_path, invalid):
    import json

    from proofops.application.ports.jobs import JobMessage

    from tests.integration.test_run_lifecycle import NOW, client, setup

    service, body = setup(tmp_path)
    http, _ = client(service)
    run_id = http.post("/v1/runs", json=body).json()["run_id"]
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=NOW)[0]["message"])
    lease = store.claim_job(message, owner="worker", now=NOW, lease_seconds=60)
    before = store.get_run(TENANT, run_id)
    envelope = dict(
        schema="local_parser_checkpoint_v1",
        input_hash=message.input_hash,
        stage_status="completed",
        downstream_status="pending",
        coverage=service.get(TENANT, run_id)["coverage"],
    )
    next_job = replace(message, job_id=str(uuid4()), stage="extract")
    if invalid == "input_hash":
        envelope["input_hash"] = "0" * 64
    elif invalid == "complete":
        envelope["coverage"]["complete"] = True
    elif invalid == "page_count":
        envelope["coverage"]["pages_processed"] = 99
    elif invalid == "stage":
        envelope["stage_status"] = "pending"
    elif invalid == "next_job":
        next_job = None
    with pytest.raises(ValueError):
        store.commit_job(lease, payload=json.dumps(envelope).encode(), now=NOW, next_job=next_job)
    assert store.read_checkpoint(message) is None
    assert store.get_run(TENANT, run_id) == before
    assert len(store.pending_outbox(TENANT, run_id, now=NOW)) == 1


def test_note_review_checkpoint_reopens_exact_derived_graph(tmp_path, monkeypatch):
    import json
    from dataclasses import asdict

    from proofops.adapters.local.table_notes import freeze_note_review, prepare, validate
    from proofops.domain.provenance import canonical_hash

    service, run_id, runner, now, stream = runner_setup(tmp_path, monkeypatch)
    _, source, profile = runner._inputs(TENANT, run_id)
    base = runner.parser.parse(source, profile, tenant_id=TENANT)
    table_id = next(b.source_id for b in base.blocks if b.kind == "table")
    packet = prepare(base, source.content, [table_id], tenant_id=TENANT)
    checked = validate({"notes": []}, packet, base, source.content, tenant_id=TENANT)
    artifact = freeze_note_review(base, source.content, packet, checked, tenant_id=TENANT)
    commit = service.store.jobs.commit_job

    def crash(*args, **kwargs):
        proposed = json.loads(kwargs["payload"])
        downgraded = {
            k: v
            for k, v in proposed.items()
            if k not in {"runtime_note_review_artifacts", "graph_sha256"}
        }
        downgraded["schema"] = "local_parser_checkpoint_v1"
        with pytest.raises(ValueError, match="NOTE_REVIEW_CHECKPOINT_INPUT_MISMATCH"):
            commit(*args, **(kwargs | {"payload": json.dumps(downgraded).encode()}))
        raise RuntimeError("crash after note validation")

    monkeypatch.setattr(service.store.jobs, "commit_job", crash)
    with pytest.raises(RuntimeError, match="crash after note validation"):
        runner.run_once(tenant_id=TENANT, run_id=run_id, note_review_artifacts=(artifact,))
    monkeypatch.setattr(service.store.jobs, "commit_job", commit)
    now[0] += 1000
    # Recovery with no argument must reload the durable input, never drop notes.
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.ports.jobs import JobMessage

    runner.store = LocalSQLiteRunStore(service.store.path)
    run = runner.store.jobs.get_run(TENANT, run_id)
    message = JobMessage(**run["parse_job"])
    raw = runner.store.jobs.read_checkpoint(message)
    envelope = json.loads(raw)
    assert envelope["schema"] == "local_parser_checkpoint_v2"
    assert envelope["runtime_note_review_artifacts"] == [artifact]
    view = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    assert view.blocks == base.blocks and view.edges == base.edges
    assert len(view.issues) == len(base.issues) + 1
    assert envelope["graph_sha256"] == canonical_hash(asdict(view))
    assert (
        runner.run_once(tenant_id=TENANT, run_id=run_id, note_review_artifacts=(artifact,))
        == "pending_downstream"
    )
    with pytest.raises(ValueError, match="NOTE_REVIEW_ALREADY_PUBLISHED"):
        runner.run_once(tenant_id=TENANT, run_id=run_id, note_review_artifacts=(artifact, artifact))
    assert runner.store.jobs.read_checkpoint(message) == raw

    read = runner.store.jobs.read_checkpoint
    for changes in (
        {"schema": "local_parser_checkpoint_v3"},
        {"schema": "local_parser_checkpoint_v1"},
        {"runtime_note_review_artifacts": []},
        {"runtime_note_review_artifacts": [artifact, artifact]},
        {"graph_sha256": "0" * 64},
    ):
        monkeypatch.setattr(
            runner.store.jobs,
            "read_checkpoint",
            lambda message, changes=changes: json.dumps(envelope | changes).encode(),
        )
        with pytest.raises(ParseFailure, match="NOTE_REVIEW|CHECKPOINT_SCHEMA"):
            runner.load_graph(tenant_id=TENANT, run_id=run_id)
    monkeypatch.setattr(runner.store.jobs, "read_checkpoint", read)
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id) == view


def test_invalid_note_review_never_publishes_parse_or_extraction(tmp_path, monkeypatch):
    service, run_id, runner, now, stream = runner_setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="immutable tuple"):
        runner.run_once(tenant_id=TENANT, run_id=run_id, note_review_artifacts=None)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id, note_review_artifacts=("{}",)) in {
        "retry",
        "failed",
    }
    run = service.store.jobs.get_run(TENANT, run_id)
    assert "parse_job" not in run and "extract_job" not in run
    assert all(
        event["message"]["stage"] == "parse"
        for event in service.store.jobs.pending_outbox(TENANT, run_id, now=now[0])
    )
    with pytest.raises(ParseFailure, match="PARSE_NOT_PUBLISHED"):
        runner.load_graph(tenant_id=TENANT, run_id=run_id)


def test_unbound_v2_checkpoint_cannot_advance_extraction(tmp_path):
    import json

    from proofops.application.ports.jobs import JobMessage

    from tests.integration.test_run_lifecycle import NOW, client, setup

    service, body = setup(tmp_path)
    http, _ = client(service)
    run_id = http.post("/v1/runs", json=body).json()["run_id"]
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=NOW)[0]["message"])
    lease = store.claim_job(message, owner="worker", now=NOW, lease_seconds=60)
    envelope = dict(
        schema="local_parser_checkpoint_v2",
        input_hash=message.input_hash,
        stage_status="completed",
        downstream_status="pending",
        runtime_note_review_artifacts=["{}"],
        graph_sha256="0" * 64,
        coverage=service.get(TENANT, run_id)["coverage"]
        | {
            "pages_processed": 3,
            "pages_unprocessed": 0,
        },
    )
    with pytest.raises(ValueError, match="NOTE_REVIEW_INPUT_NOT_BOUND"):
        store.commit_job(
            lease,
            payload=json.dumps(envelope).encode(),
            now=NOW,
            next_job=replace(message, job_id=str(uuid4()), stage="extract"),
        )
    assert "parse_job" not in store.get_run(TENANT, run_id)
