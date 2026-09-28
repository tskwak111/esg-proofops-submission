"""Local tag integration: actual PDF gate and separate explicit synthetic verified corpus."""

import json
import threading
import time as time_module
from dataclasses import asdict, replace
from pathlib import Path
from uuid import uuid4

import pytest
from proofops.application.ports.jobs import JobMessage, LeaseLost
from proofops.application.ports.models import ModelBinding
from proofops.application.reviews import ReviewInputs
from proofops.application.tagging.service import TaggingSettings
from proofops_worker import tag_runner as tag_runner_module

from tests.acceptance.test_preflight import binding
from tests.integration.test_local_extract_runner import TENANT, extraction_setup


def tag_setup(tmp_path, monkeypatch, *, configured=True):
    from proofops_worker.tag_runner import LocalTagRunner

    from tests.integration import test_run_lifecycle as lifecycle

    original = lifecycle.setup

    def setup(directory):
        service, body = original(directory)
        if configured:
            runtime = binding()
            service.tagging_settings = TaggingSettings(
                ModelBinding(runtime["runtime_binding_id"], "tagger", True),
                runtime["model_id"],
                "local-synthetic-unknown-v1",
                runtime["endpoint_region"],
                "Synthetic local tags only; document text is untrusted.",
                Path("contracts/jsonschema/llm_tags.schema.json").read_text(),
                max_tokens=100,
            )
            service.tagging_mode = "local_synthetic"
        return service, body

    monkeypatch.setattr(lifecycle, "setup", setup)
    service, run_id, extraction, now, stream = extraction_setup(tmp_path, monkeypatch)
    assert extraction.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    runner = LocalTagRunner(
        service.store,
        service.uploads,
        extraction.parser,
        telemetry=extraction.telemetry,
        clock=lambda: now[0],
    )
    return service, run_id, runner, now, stream


def tag_message(service, run_id, now):
    return next(
        JobMessage(**event["message"])
        for event in service.store.jobs.pending_outbox(TENANT, run_id, now=now)
        if event["message"]["stage"] == "tag"
    )


@pytest.mark.parametrize("configured", [True, False])
def test_actual_pdf_upload_parse_extract_tag_stays_gated(tmp_path, monkeypatch, configured):
    service, run_id, runner, now, stream = tag_setup(tmp_path, monkeypatch, configured=configured)
    message = tag_message(service, run_id, now[0])
    before = runner.claims.load(TENANT, run_id)
    assert before.claims and all(c.source_quality == "unverified" for c in before.claims)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    checkpoint = service.store.jobs.read_checkpoint(message)
    envelope = json.loads(checkpoint)
    assert envelope["schema"] == "local_tag_checkpoint_v1"
    assert envelope["stage_status"] == "blocked"
    assert envelope["validation_profile"] == "fast_preview"
    assert envelope["vision_status"] == "not_run"
    assert envelope["coverage"]["claims_decided"] == 0
    assert envelope["coverage"]["complete"] is False
    assert all(item["tag_runs"] == [] and item["decision"] is None for item in envelope["claims"])
    assert runner.claims.load(TENANT, run_id) == before
    assert service.store.jobs.get_run(TENANT, run_id)["status"] == "partial"
    assert service.store.jobs.get_usage(message, fencing_token=1)["model_calls"] == 0
    assert service.cost(TENANT, run_id)["attempt_count"] == 0
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    assert service.store.jobs.read_checkpoint(message) == checkpoint
    assert not service.store.jobs.pending_outbox(TENANT, run_id, now=now[0])
    assert "1234 tCO2e" not in stream.getvalue()


def test_sqlite_cache_reopen_is_immutable_and_tenant_scoped(tmp_path):
    from proofops.adapters.cache.aws import CacheCollisionError, ImmutableResponseCache
    from proofops.adapters.local.tag_cache import SQLiteImmutableCacheClient

    from tests.acceptance.test_tagging import execute, setup

    inputs = setup(tmp_path)
    path = tmp_path / "tag-cache.sqlite"
    inputs["cache"] = ImmutableResponseCache(SQLiteImmutableCacheClient(path))
    first = execute(inputs)
    inputs["cache"] = ImmutableResponseCache(SQLiteImmutableCacheClient(path))
    second = execute(inputs)
    assert len(inputs["invoke"].requests) == 3
    assert all(run.recovered for run in second)
    assert [run.raw_response_json for run in first] == [run.raw_response_json for run in second]
    request = first[0].request
    with pytest.raises(CacheCollisionError):
        inputs["cache"].put_raw(request, b"overwrite")
    foreign = replace(request, namespace=replace(request.namespace, tenant_id=str(uuid4())))
    assert inputs["cache"].get_raw(foreign, recovery_request_id=foreign.request_id) is None


class SyntheticVerifiedParser:
    """Separate test corpus: explicit verified synthetic paragraph, never real parser promotion.

    The generated PDF and extraction stores are real. This adapter deliberately
    substitutes the parser/validation boundary only for downstream local tests.
    It persists its own immutable manifest and rebuilds the same synthetic graph.
    """

    synthetic = True

    def __init__(self, path):
        self.artifact_root = Path(path)

    def parse(self, source, profile, *, tenant_id):
        from hashlib import sha256
        from io import BytesIO
        from uuid import UUID, uuid5

        from proofops.application.ingest.graph_fusion import (
            CandidateBatch,
            CandidateBlock,
            fuse_candidates,
        )
        from proofops.domain.documents import NativeSource, PageGeometry
        from proofops.domain.rulepacks import canonical_json
        from pypdf import PdfReader

        assert tenant_id == source.tenant_id
        text = PdfReader(BytesIO(source.content)).pages[0].extract_text().strip()
        parser_id = str(uuid5(UUID(profile.parse_manifest_id), "explicit-verified-synthetic"))
        native = NativeSource(
            source.document_version_id,
            profile.parse_manifest_id,
            parser_id,
            "synthetic-paragraph",
            1,
            None,
            (72, 700, 550, 740),
            "pdf_bottom_left_points",
            text,
            0,
            len(text),
        )
        batch = CandidateBatch(
            tenant_id,
            source.document_version_id,
            profile.parse_manifest_id,
            source.sha256,
            parser_id,
            "synthetic-verified",
            "1",
            "synthetic",
            profile.config_hash(),
            (CandidateBlock("paragraph", native, PageGeometry(600, 800, 0, (0, 0, 600, 800))),),
            synthetic=True,
        )
        graph = fuse_candidates((batch,), tenant_id=tenant_id)
        graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
        directory = (
            self.artifact_root / tenant_id / source.document_version_id / profile.parse_manifest_id
        )
        manifest = canonical_json(
            dict(
                synthetic=True,
                source_sha256=sha256(source.content).hexdigest(),
                profile=asdict(profile),
                graph=asdict(graph),
                artifacts=[],
            )
        ).encode()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "manifest.json"
        if target.exists():
            assert target.read_bytes() == manifest
        else:
            with target.open("xb") as out:
                out.write(manifest)
        return graph

    def load_verified(self, source, profile, *, tenant_id, manifest_sha256):
        from hashlib import sha256

        target = (
            self.artifact_root
            / tenant_id
            / source.document_version_id
            / profile.parse_manifest_id
            / "manifest.json"
        )
        assert sha256(target.read_bytes()).hexdigest() == manifest_sha256
        return self.parse(source, profile, tenant_id=tenant_id)


def verified_setup(tmp_path, monkeypatch):
    from io import StringIO

    from proofops.application.budget import BudgetLimits, RoleLimit
    from proofops.application.evidence.binding import ClaimContext
    from proofops.application.ingest.graph_fusion import ParserProfile
    from proofops.application.rulepacks import RulePackRecord
    from proofops.application.tagging.tracks import TrackCandidate
    from proofops.application.telemetry import Telemetry
    from proofops_agent.extraction import SyntheticClaimExtractor
    from proofops_agent.synthetic_tagging import SyntheticTaggingTransport
    from proofops_worker.extract_runner import LocalExtractRunner
    from proofops_worker.local_runner import LocalParserRunner
    from proofops_worker.tag_runner import LocalTagRunner

    from tests.acceptance.test_parsing import pdf
    from tests.acceptance.test_rules import pack
    from tests.integration import test_run_lifecycle as lifecycle

    full = pack()
    monkeypatch.setattr(lifecycle, "pdf", lambda count: pdf())
    monkeypatch.setattr(lifecycle, "_files", lambda: {p: full.file_content(p) for p in full.files})
    monkeypatch.setattr(
        lifecycle,
        "_pack",
        lambda: RulePackRecord(
            **{
                k: v
                for k, v in (
                    asdict(full)
                    | dict(
                        status="validated",
                        approved_by="synthetic-fixture",
                        approved_at="2026-09-08T10:00:00Z",
                    )
                ).items()
                if k != "content"
            }
        ),
    )
    service, body = lifecycle.setup(tmp_path)
    service.extraction_profile = SyntheticClaimExtractor.profile
    service.extraction_mode = "local_synthetic"
    runtime = binding()
    service.tagging_settings = TaggingSettings(
        ModelBinding(runtime["runtime_binding_id"], "tagger", True),
        runtime["model_id"],
        "local-synthetic-unknown-v1",
        runtime["endpoint_region"],
        "Explicit synthetic tags only",
        Path("contracts/jsonschema/llm_tags.schema.json").read_text(),
        max_tokens=4000,
    )
    service.tagging_mode = "local_synthetic"
    service.budget_limits = BudgetLimits(
        100000, 100000, (RoleLimit("tagger", 3, 50000, 50000, 100000),)
    )
    http, _ = lifecycle.client(service)
    response = http.post("/v1/runs", json=body)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    now = [lifecycle.NOW]
    parser = SyntheticVerifiedParser(tmp_path / "synthetic-prepared")
    stream = StringIO()
    telemetry = Telemetry(service="worker", env="test", stream=stream, hash_key=b"x" * 32)
    parse_runner = LocalParserRunner(
        service.store,
        service.uploads,
        parser,
        profile=ParserProfile(str(uuid4()), **service.parser_profile),
        telemetry=telemetry,
        clock=lambda: now[0],
    )
    assert parse_runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    extraction = LocalExtractRunner(
        service.store,
        service.uploads,
        parser,
        extractor=SyntheticClaimExtractor(),
        telemetry=telemetry,
        clock=lambda: now[0],
    )
    assert extraction.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    class RecordingSyntheticTransport(SyntheticTaggingTransport):
        def __init__(self):
            self.requests = []

        def invoke(self, request):
            self.requests.append(request)
            return super().invoke(request)

    def preliminary(claim, graph):
        # Classification is explicit fixture input; missing dimensions stay unknown.
        return TrackCandidate(claim, "performance", None), ClaimContext(claim, {}), {}

    runner = LocalTagRunner(
        service.store,
        service.uploads,
        parser,
        telemetry=telemetry,
        transport=RecordingSyntheticTransport(),
        preliminary=preliminary,
        clock=lambda: now[0],
    )
    return service, run_id, runner, now, stream


def test_needs_review_records_explicit_consensus_reason_not_none(tmp_path, monkeypatch):
    # Regression: unconfirmed consensus previously published needs_review with
    # reason=None, losing the "why". A validated (approved_grading) rulepack whose
    # replicates stay unknown must report CONSENSUS_UNRESOLVED, not a domain gate.
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    envelope = runner.tags.load_snapshot(TENANT, run_id)
    assert envelope.get("rulepack_use") != "candidate_tagging_reference_only"
    record = envelope["claims"][0]
    assert record["status"] == "needs_review"
    assert record["decision"] is None
    assert record["reason"] == "CONSENSUS_UNRESOLVED"
    assert record["review_inputs"] is not None


def test_verified_synthetic_three_calls_publish_initial_review_and_reopen(tmp_path, monkeypatch):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops_worker.tag_runner import LocalTagRunner

    service, run_id, runner, now, stream = verified_setup(tmp_path, monkeypatch)
    message = tag_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    requests = runner.transport.requests
    assert len(requests) == 3
    assert {r["replicate_id"] for r in requests} == {1, 2, 3}
    assert len({r["request_signature"] for r in requests}) == 3
    assert len({r["packet_sha256"] for r in requests}) == 1
    envelope = runner.tags.load_snapshot(TENANT, run_id)
    assert envelope["coverage"]["complete"] is False
    record = envelope["claims"][0]
    assert len(record["tag_runs"]) == 3 and record["decision"] is None
    inputs = runner.tags.load_inputs(TENANT, run_id, record["claim_id"])
    assert inputs.packet != inputs.original_packet
    assert inputs.packet.to_dict()["allowed_elements"] == [f"P{i}" for i in range(1, 7)]
    assert all(
        run.provider_response_json and run.raw_response_json and run.usage
        for run in inputs.tag_runs
    )
    assert all(e.state == "unknown" for e in inputs.consensus.candidate_elements)
    reopened = LocalTagRunner(
        LocalSQLiteRunStore(service.store.path),
        service.uploads,
        runner.parser,
        telemetry=runner.telemetry,
        clock=lambda: now[0],
    )
    assert reopened.tags.load_inputs(TENANT, run_id, record["claim_id"]) == inputs
    assert reopened.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    assert len(requests) == 3 and service.cost(TENANT, run_id)["attempt_count"] == 3
    with service.store.jobs._transaction() as db:
        heads = service.store.jobs._all(db, TENANT, run_id, "review_head")
        assert len(heads) == 1 and heads[0]["base_tag_revision"] == 1
        assert len(service.store.jobs._all(db, TENANT, run_id, "tag_revision")) == 1
        assert service.store.jobs._all(db, TENANT, run_id, "decision_revision") == []
        assert (
            db.execute(
                "SELECT count(*) FROM audit_events WHERE tenant_id=? AND run_id=? "
                "AND action='tag_stage_published'",
                (TENANT, run_id),
            ).fetchone()[0]
            == 1
        )
    assert service.store.jobs.read_checkpoint(message)
    assert not service.store.jobs.pending_outbox(TENANT, run_id, now=now[0])
    assert "Page 1 emissions" not in stream.getvalue()


@pytest.mark.parametrize(
    "boundary", ["crash_after_review", "cancel_before", "cancel_inflight", "stale"]
)
def test_tag_publication_fence_and_durable_recovery(tmp_path, monkeypatch, boundary):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops_worker.tag_runner import LocalTagRunner

    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = tag_message(service, run_id, now[0])
    before = jobs.get_run(TENANT, run_id)
    invoke = runner.transport.invoke

    def cancel():
        jobs.cancel_run(
            TENANT,
            run_id,
            expected_revision=jobs.get_run(TENANT, run_id)["revision"],
            idempotency_key=str(uuid4()),
            reason="synthetic cancellation",
            actor_sub="synthetic-test",
            now=now[0],
        )

    if boundary == "cancel_before":
        cancel()
    elif boundary == "crash_after_review":
        publish = runner.reviews.store.publish_transaction

        def crash(*args, **kwargs):
            publish(*args, **kwargs)
            raise RuntimeError("synthetic crash after review heads and audit")

        monkeypatch.setattr(runner.reviews.store, "publish_transaction", crash)
    else:

        def interrupt(request):
            response = invoke(request)
            if boundary == "cancel_inflight":
                cancel()
            else:
                now[0] += 1000
            return response

        monkeypatch.setattr(runner.transport, "invoke", interrupt)
    if boundary == "crash_after_review":
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
        assert jobs.get_job(message)["error_code"] == "TAG_UNEXPECTED_RuntimeError"
    else:
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == (
            "cancelled" if boundary == "cancel_before" else "LEASE_LOST"
        )
    assert jobs.read_checkpoint(message) is None
    assert jobs.get_run(TENANT, run_id)["coverage"] == before["coverage"]
    with jobs._transaction() as db:
        for kind in (
            "claim_head",
            "tag_revision",
            "decision_revision",
            "review_head",
            "review_inputs",
        ):
            assert jobs._all(db, TENANT, run_id, kind) == []
        assert (
            db.execute(
                "SELECT count(*) FROM audit_events WHERE tenant_id=? AND run_id=? "
                "AND action IN ('tag_stage_published','review_opened')",
                (TENANT, run_id),
            ).fetchone()[0]
            == 0
        )
    expected_calls = (
        0 if boundary == "cancel_before" else 3 if boundary == "crash_after_review" else 1
    )
    assert len(runner.transport.requests) == expected_calls
    assert service.cost(TENANT, run_id)["attempt_count"] == expected_calls
    if boundary in {"crash_after_review", "stale"}:
        monkeypatch.setattr(runner.transport, "invoke", invoke)
        now[0] += 1000
        if boundary == "crash_after_review":
            jobs.retry_run(
                TENANT,
                run_id,
                expected_revision=jobs.get_run(TENANT, run_id)["revision"],
                idempotency_key=str(uuid4()),
                reason="retry after recorded tag failure",
                now=now[0],
                actor_sub="synthetic-test",
            )
        reopened = LocalTagRunner(
            LocalSQLiteRunStore(service.store.path),
            service.uploads,
            runner.parser,
            telemetry=runner.telemetry,
            transport=runner.transport,
            preliminary=runner.preliminary,
            clock=lambda: now[0],
        )
        assert reopened.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
        assert len(runner.transport.requests) == 3
        assert service.cost(TENANT, run_id)["attempt_count"] == 3
        inputs = reopened.tags.load_inputs(
            TENANT, run_id, reopened.claims.list(TENANT, run_id)[0].claim_id
        )
        assert sum(run.recovered for run in inputs.tag_runs) == expected_calls
        assert len({run.request.request_signature for run in inputs.tag_runs}) == 3
        assert jobs.get_job(message)["fencing_token"] == 2
        expected_mutations = 6 if boundary == "crash_after_review" else 4
        assert (
            jobs.get_run(TENANT, run_id)["mutation_epoch"]
            == before["mutation_epoch"] + expected_mutations
        )


@pytest.mark.parametrize("keepalive", [False, True])
def test_tag_operation_over_lease_publication_depends_on_lease_keepalive(
    tmp_path, monkeypatch, keepalive
):
    """R10 recovery (Lotte TAG failure): a post-call step that outlives the
    job's lease must not silently discard a batch whose model calls already
    succeeded and were already paid for.

    The recorded failure was not a stuck provider call (each replica call
    fenced and returned well within its own timeout); it was the job's total
    wall time across many claims and calls exceeding the 300s lease with only
    point heartbeats taken immediately before each call. Nothing renews the
    lease *during* a slow step between two calls, so the lease can expire
    before the eventual commit's own ownership fence runs, discarding
    already-completed, already-billed replica responses with no clean
    failure reason.

    ``ReviewInputs.validate`` stands in for that post-call bottleneck: it runs
    once per claim after all three replica calls, right before publication.
    This is red on ``keepalive=False`` (bypassing the fix, matching the
    unpatched behavior) and green on ``keepalive=True`` (the fix applied),
    proving the keepalive changes the outcome rather than test timing alone.
    An independently invalidated lease (a real renewal failure) must still
    block publication in both cases.
    """
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs

    if not keepalive:
        # Bypass the fix: run the tag operation directly, without the
        # keepalive wrapper run_once normally applies around it.
        monkeypatch.setattr(
            tag_runner_module,
            "with_lease_heartbeat",
            lambda store, lease, clock, operation, **kwargs: operation(lease),
        )

    # Wall-clock-driven runner clock: any heartbeat renews in real time,
    # matching production timing rather than a manually-advanced integer clock.
    started = time_module.monotonic()
    base_now = now[0]
    runner.clock = lambda: base_now + time_module.monotonic() - started

    original_claim_job = jobs.claim_job

    def short_lease_claim_job(message, *, owner, now, lease_seconds):
        return original_claim_job(message, owner=owner, now=now, lease_seconds=2)

    monkeypatch.setattr(jobs, "claim_job", short_lease_claim_job)

    # Cap every heartbeat's grant at this test's own short lease window so the
    # reproduction reflects "the post-call step outlasts the lease" rather
    # than being masked by the production 300s renewal request.
    original_heartbeat = jobs.heartbeat

    def capped_heartbeat(lease, *, now, lease_seconds):
        return original_heartbeat(lease, now=now, lease_seconds=min(lease_seconds, 2))

    monkeypatch.setattr(jobs, "heartbeat", capped_heartbeat)

    original_validate = ReviewInputs.validate

    def slow_validate(self):
        # Stands in for the real bottleneck (a step after the last model call
        # that outlasts the batch's lease window before publication).
        time_module.sleep(2.5)
        return original_validate(self)

    monkeypatch.setattr(ReviewInputs, "validate", slow_validate)

    status = runner.run_once(tenant_id=TENANT, run_id=run_id)
    monkeypatch.setattr(ReviewInputs, "validate", original_validate)
    elapsed = time_module.monotonic() - started
    assert elapsed > 2
    assert len(runner.transport.requests) == 3

    if keepalive:
        assert status == "needs_review"
        assert runner.claims.list(TENANT, run_id)
        record = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]
        assert record["tag_runs"] and len(record["tag_runs"]) == 3
    else:
        assert status == "LEASE_LOST"
        assert "tag_job" not in jobs.get_run(TENANT, run_id)

    assert not any(t.name.startswith("lease-heartbeat:") for t in threading.enumerate())

    # Independently: a real renewal failure (lease invalidated by another
    # actor, or by simple staleness) must still block publication even with
    # the keepalive wrapper. This exercises the same job-store invariant the
    # fix relies on (StageFailure on a genuinely lost lease), independent of
    # this run's own tag delivery so it holds whether or not that delivery
    # already committed.
    monkeypatch.setattr(jobs, "heartbeat", original_heartbeat)
    from tests.acceptance.test_jobs import MESSAGE, seeded

    isolated = seeded(tmp_path / "isolated-lease-jobs.sqlite")
    stale_message = replace(MESSAGE, job_id=str(uuid4()), stage="tag", shard="replica-lease-check")
    isolated.enqueue(stale_message, now=0)
    stale_lease = isolated.claim_job(stale_message, owner="orphan", now=0, lease_seconds=300)
    assert stale_lease is not None
    with pytest.raises(LeaseLost):
        isolated.heartbeat(stale_lease, now=301, lease_seconds=300)
    assert not isolated.commit_job(stale_lease, payload=b"stale", now=301)


def test_slow_provider_call_renews_tag_lease_until_call_returns(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    original_claim = jobs.claim_job
    original_heartbeat = jobs.heartbeat

    def short_claim(message, *, owner, now, lease_seconds):
        return original_claim(message, owner=owner, now=now, lease_seconds=2)

    def capped_heartbeat(lease, *, now, lease_seconds):
        return original_heartbeat(lease, now=now, lease_seconds=min(lease_seconds, 2))

    monkeypatch.setattr(jobs, "claim_job", short_claim)
    monkeypatch.setattr(jobs, "heartbeat", capped_heartbeat)
    started = time_module.monotonic()
    base_now = now[0]
    runner.clock = lambda: base_now + int(time_module.monotonic() - started)
    invoke = runner.transport.invoke
    delayed = False

    def slow_first_call(request):
        nonlocal delayed
        if not delayed:
            delayed = True
            time_module.sleep(2.25)
        return invoke(request)

    monkeypatch.setattr(runner.transport, "invoke", slow_first_call)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    assert len(runner.transport.requests) == 3
    record = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]
    assert len(record["tag_runs"]) == 3


def test_tag_renewal_failure_stops_reservations_and_returns_specific_code(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    original_claim = jobs.claim_job
    original_heartbeat = jobs.heartbeat

    def short_claim(message, *, owner, now, lease_seconds):
        return original_claim(message, owner=owner, now=now, lease_seconds=2)

    def renewal_fails_on_keepalive(lease, *, now, lease_seconds):
        if threading.current_thread().name.startswith("lease-heartbeat:"):
            raise LeaseLost("LEASE_LOST")
        return original_heartbeat(lease, now=now, lease_seconds=min(lease_seconds, 2))

    monkeypatch.setattr(jobs, "claim_job", short_claim)
    monkeypatch.setattr(jobs, "heartbeat", renewal_fails_on_keepalive)
    started = time_module.monotonic()
    base_now = now[0]
    runner.clock = lambda: base_now + int(time_module.monotonic() - started)
    invoke = runner.transport.invoke

    def slow_first_call(request):
        if not runner.transport.requests:
            time_module.sleep(1.1)
        return invoke(request)

    monkeypatch.setattr(runner.transport, "invoke", slow_first_call)
    message = tag_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "LEASE_HEARTBEAT_FAILED"
    assert len(runner.transport.requests) == 1
    assert service.cost(TENANT, run_id)["attempt_count"] == 1
    failed_job = jobs.get_job(message)
    assert failed_job["error_code"] == "LEASE_HEARTBEAT_FAILED"
    assert failed_job["message"]["stage"] == "tag"
    assert failed_job["heartbeat_at"] is not None
    assert jobs.read_checkpoint(message) is None


def test_failed_renewal_during_last_element_call_prevents_claim_publication(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    original_claim, original_heartbeat = jobs.claim_job, jobs.heartbeat

    def short_claim(message, *, owner, now, lease_seconds):
        return original_claim(message, owner=owner, now=now, lease_seconds=2)

    def renewal_fails_on_keepalive(lease, *, now, lease_seconds):
        if threading.current_thread().name.startswith("lease-heartbeat:"):
            raise LeaseLost("LEASE_LOST")
        return original_heartbeat(lease, now=now, lease_seconds=min(lease_seconds, 2))

    monkeypatch.setattr(jobs, "claim_job", short_claim)
    monkeypatch.setattr(jobs, "heartbeat", renewal_fails_on_keepalive)
    started, base_now = time_module.monotonic(), now[0]
    runner.clock = lambda: base_now + int(time_module.monotonic() - started)
    invoke = runner.transport.invoke

    def slow_last_call(request):
        if len(runner.transport.requests) == 2:
            time_module.sleep(1.1)
        return invoke(request)

    monkeypatch.setattr(runner.transport, "invoke", slow_last_call)
    message = tag_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "LEASE_HEARTBEAT_FAILED"
    assert len(runner.transport.requests) == 3
    assert jobs.read_checkpoint(message) is None
    with jobs._transaction() as db:
        assert jobs._all(db, TENANT, run_id, "tag_revision") == []
        assert jobs._all(db, TENANT, run_id, "review_head") == []


def test_later_claim_failure_keeps_earlier_claim_published_and_replayable(tmp_path, monkeypatch):
    service, run_id, runner, now, stream = verified_setup(tmp_path, monkeypatch)
    extraction, discovery, graph = runner.claims.load_evidence(TENANT, run_id)
    first = discovery.claims[0]
    later = replace(first, claim_id=str(uuid4()))
    discovery = replace(discovery, claims=(first, later))
    monkeypatch.setattr(
        runner.claims,
        "load_evidence",
        lambda tenant_id, target_run: (extraction, discovery, graph),
    )
    preliminary = runner.preliminary

    def fail_on_later_claim(claim, document):
        if claim.claim_id == later.claim_id:
            raise RuntimeError("private provider detail must not be logged")
        return preliminary(claim, document)

    runner.preliminary = fail_on_later_claim
    message = tag_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    with service.store.jobs._transaction() as db:
        assert service.store.jobs._get(db, TENANT, run_id, "claim_head", first.claim_id)
        with pytest.raises(KeyError):
            service.store.jobs._get(db, TENANT, run_id, "claim_head", later.claim_id)
    assert "tag_job" not in service.store.jobs.get_run(TENANT, run_id)
    assert len(runner.tags.load_inputs(TENANT, run_id, first.claim_id).tag_runs) == 3
    assert service.store.jobs.get_job(message)["error_code"] == "TAG_UNEXPECTED_RuntimeError"
    rows = [json.loads(line) for line in stream.getvalue().splitlines()]
    failure = next(row for row in rows if row.get("event") == "operation_failed")
    assert failure["stage"] == "TAG"
    assert failure["exception_type"] == "RuntimeError"
    assert failure["last_successful_heartbeat_at"] is not None
    assert "private provider detail" not in stream.getvalue()


def test_lost_fence_cannot_publish_the_current_claim(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    extraction, discovery, graph = runner.claims.load_evidence(TENANT, run_id)
    first = discovery.claims[0]
    later = replace(first, claim_id=str(uuid4()))
    discovery = replace(discovery, claims=(first, later))
    monkeypatch.setattr(
        runner.claims,
        "load_evidence",
        lambda tenant_id, target_run: (extraction, discovery, graph),
    )
    original_publish = runner._publish_claim
    message = tag_message(service, run_id, now[0])

    def fence_later_claim(lease, inputs, heartbeat_state=None):
        if inputs.context.claim.claim_id == later.claim_id:
            now[0] += 301
            replacement_lease = jobs.claim_job(
                message, owner="replacement-worker", now=now[0], lease_seconds=300
            )
            assert replacement_lease is not None
        return original_publish(lease, inputs, heartbeat_state)

    monkeypatch.setattr(runner, "_publish_claim", fence_later_claim)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "LEASE_LOST"
    with jobs._transaction() as db:
        assert jobs._get(db, TENANT, run_id, "claim_head", first.claim_id)
        with pytest.raises(KeyError):
            jobs._get(db, TENANT, run_id, "claim_head", later.claim_id)


@pytest.mark.parametrize(
    "field", ["claim_snapshot_sha256", "input_hash", "tagging_settings_hash", "coverage"]
)
def test_tag_checkpoint_tamper_preserves_incremental_claim_publication(
    tmp_path, monkeypatch, field
):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = tag_message(service, run_id, now[0])
    commit = jobs.commit_job

    def corrupt(lease, *, payload, **kwargs):
        envelope = json.loads(payload)
        if field == "coverage":
            envelope[field]["complete"] = True
        else:
            envelope[field] = "0" * 64
        return commit(lease, payload=json.dumps(envelope).encode(), **kwargs)

    monkeypatch.setattr(jobs, "commit_job", corrupt)
    with pytest.raises(ValueError):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert jobs.read_checkpoint(message) is None
    with jobs._transaction() as db:
        assert len(jobs._all(db, TENANT, run_id, "review_head")) == 1
        assert len(jobs._all(db, TENANT, run_id, "claim_head")) == 1
        assert len(jobs._all(db, TENANT, run_id, "tag_revision")) == 1
    assert len(jobs.pending_outbox(TENANT, run_id, now=now[0])) == 1


def test_unknown_preliminary_and_missing_transport_make_no_requests(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    runner.preliminary = None
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    assert runner.transport.requests == []
    assert service.cost(TENANT, run_id)["attempt_count"] == 0
    record = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]
    assert record["reason"] == "PRELIMINARY_TAGS_REQUIRED" and record["tag_runs"] == []


def test_cli_tag_gate_and_only_explicit_local_mode_wires_transport(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys
    import time
    from datetime import UTC, datetime

    from proofops.application.registry import Registry
    from proofops.application.uploads import UploadService
    from proofops_worker.composition import build_composition

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
    service, run_id, runner, now, _ = tag_setup(tmp_path, monkeypatch)
    (tmp_path / "prepared").rename(tmp_path / "parser-prepared")
    config = tmp_path / "parser-config.json"
    config.write_text(json.dumps(service.store.snapshot(TENANT, run_id)["parser_profile"]))
    for key, value in dict(
        LOCAL_DATABASE_PATH=str(database),
        LOCAL_PARSER_PROFILE_PATH=str(config),
        APP_ENV="local",
        MODEL_ADAPTER="synthetic",
    ).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("LOCAL_TAGGING_MODE", raising=False)
    composition = build_composition(stage="tag")
    assert composition.transport is None
    composition.uploads.close()
    composition.uploads.registry.close()
    monkeypatch.setenv("LOCAL_TAGGING_MODE", "local_synthetic")
    composition = build_composition(stage="tag")
    assert composition.transport.synthetic is True
    assert composition.preliminary is None
    composition.uploads.close()
    composition.uploads.registry.close()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "proofops_worker.main",
            "--tenant-id",
            TENANT,
            "--run-id",
            run_id,
            "--once",
            "--stage",
            "tag",
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("blocked")
    assert "1234 tCO2e" not in result.stdout + result.stderr
    assert service.store.jobs.get_run(TENANT, run_id)["status"] == "partial"
    assert service.cost(TENANT, run_id)["attempt_count"] == 0


@pytest.mark.parametrize("boundary", ["profile", "source", "foreign_tenant"])
def test_tag_pins_fail_before_any_request(tmp_path, monkeypatch, boundary):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    snapshot = service.store.snapshot(TENANT, run_id)
    if boundary == "profile":
        original = runner.store.snapshot

        def corrupt(tenant_id, target):
            frozen = original(tenant_id, target)
            frozen["tagging_settings"]["system_prompt"] += "tampered"
            return frozen

        monkeypatch.setattr(runner.store, "snapshot", corrupt)
    elif boundary == "source":
        original_read = runner.uploads.read_original
        monkeypatch.setattr(
            runner.uploads,
            "read_original",
            lambda tenant_id, version_id: original_read(tenant_id, version_id) + b"changed",
        )
    if boundary == "foreign_tenant":
        from proofops.application.runs import RunRejected

        with pytest.raises(RunRejected) as failure:
            runner.run_once(tenant_id=str(uuid4()), run_id=run_id)
        assert failure.value.status == 404 and failure.value.code == "RESOURCE_NOT_FOUND"
    else:
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    assert runner.transport.requests == []
    assert service.cost(TENANT, run_id)["attempt_count"] == 0
    assert snapshot["tagging_settings"]["system_prompt"].endswith("tags only")


def test_failed_delivery_recovery_acknowledges_without_repeating_work(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    jobs = service.store.jobs
    message = tag_message(service, run_id, now[0])
    lease = jobs.claim_job(message, owner="synthetic-crashed-worker", now=now[0], lease_seconds=300)
    jobs.fail_job(lease, error_code="TAG_INPUT_INVALID", now=now[0])
    # Simulate crash between durable failure and the transport acknowledgement.
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "ignored"
    assert jobs.pending_outbox(TENANT, run_id, now=now[0]) == []
    assert runner.transport.requests == []
    assert jobs.read_checkpoint(message) is None


def test_tag_delivery_replays_verified_source_once_per_operation(tmp_path, monkeypatch):
    service, run_id, runner, _, _ = verified_setup(tmp_path, monkeypatch)
    original = runner.parser.load_verified
    reads = []

    def checked(*args, **kwargs):
        reads.append(kwargs["manifest_sha256"])
        return original(*args, **kwargs)

    monkeypatch.setattr(runner.parser, "load_verified", checked)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    assert len(runner.transport.requests) == 3
    assert len(reads) == 1
    # A new operation must still check source integrity, rather than use a stale cache.
    runner.tags.load_snapshot(TENANT, run_id)
    assert len(reads) == 2


def test_unresolved_preliminary_is_claim_block_not_failed_job(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    runner.preliminary = lambda claim, graph: None
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    assert runner.transport.requests == []
    record = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]
    assert record["reason"] == "PRELIMINARY_TAGS_UNRESOLVED"
    assert record["decision"] is None and record["tag_runs"] == []
    # The consensus stop is downstream of local candidate retrieval, so review
    # still receives the traceable candidates and their recorded reasons.
    packet = record["original_packet"]
    assert packet["status"] == "candidate"
    assert packet["claim_id"] == record["claim_id"]
    assert [c["source_id"] for c in packet["evidence_candidates"]] == [
        ref["source_id"] for ref in packet["claim_source_refs"]
    ]
    assert packet["search_coverage"]["lexical_status"] == "bounded"
    assert "retrieval_packet_sha256" not in packet  # never frozen to a track
    assert record["review_inputs"] is None
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"


def test_unverified_source_reaches_bounded_candidates_without_model_or_decision(
    tmp_path, monkeypatch
):
    """A source-traceable but unverified claim must not look like no information.

    The same verified corpus is read back with fast_preview source quality, which
    is the state the real Lotte run published before span verification recovered.
    """
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    original = runner.claims.load_evidence

    def downgraded(tenant_id, target):
        extraction, discovery, graph = original(tenant_id, target)
        claims = tuple(replace(claim, source_quality="unverified") for claim in discovery.claims)
        return extraction, replace(discovery, claims=claims), graph

    monkeypatch.setattr(runner.claims, "load_evidence", downgraded)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    assert runner.transport.requests == []
    assert service.cost(TENANT, run_id)["attempt_count"] == 0
    record = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]
    assert record["reason"] == "SOURCE_VALIDATION_REQUIRED"
    assert record["status"] == "blocked"
    assert record["tag_runs"] == [] and record["decision"] is None
    assert record["review_inputs"] is None
    packet = record["original_packet"]
    # Bounded candidates for review, but the packet stays blocked_evidence so no
    # track packet, tag, present state or grade can ever be built from it.
    assert packet["status"] == "blocked_evidence"
    assert packet["evidence_candidates"]
    assert all(b["state"] == "undetermined" for b in packet["candidate_bindings"])
    from proofops.application.evidence.retrieval import freeze_packet, freeze_track_packet
    from proofops.application.tagging.tracks import TrackCandidate
    from proofops.domain.errors import DomainValidationError
    from proofops.domain.rulepacks import RulePackSnapshot

    _, discovery, _ = downgraded(TENANT, run_id)
    rulepack = RulePackSnapshot(**service.store.snapshot(TENANT, run_id)["rulepack"])
    with pytest.raises(DomainValidationError):
        freeze_track_packet(
            freeze_packet({k: v for k, v in packet.items() if k != "packet_sha256"}),
            track=TrackCandidate(discovery.claims[0], "performance", None),
            rulepack=rulepack,
        )


def test_fabricated_raw_quote_never_reaches_retrieval_search_or_a_model(tmp_path, monkeypatch):
    """A ref that no longer matches the pinned parsed text is not traceable.

    verify_source_ref returns rejected rather than raising, so the candidate path
    checks locatability itself: no search, no GRI routing, no packet, no model.
    """
    from proofops_worker import tag_runner as module

    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    original = runner.claims.load_evidence

    def fabricated(tenant_id, target):
        extraction, discovery, graph = original(tenant_id, target)
        claims = []
        for claim in discovery.claims:
            refs = tuple(replace(ref, quote=ref.quote + " 9999 tCO2e") for ref in claim.source_refs)
            claims.append(
                replace(
                    claim,
                    source_quality="unverified",
                    source_refs=refs,
                    quote=" ".join(ref.quote for ref in refs),
                )
            )
        return extraction, replace(discovery, claims=tuple(claims)), graph

    monkeypatch.setattr(runner.claims, "load_evidence", fabricated)
    monkeypatch.setattr(
        module,
        "retrieve_evidence",
        lambda *a, **k: pytest.fail("fabricated quote must not reach retrieval"),
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    assert runner.transport.requests == []
    assert service.cost(TENANT, run_id)["attempt_count"] == 0
    record = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]
    assert record["reason"] == "SOURCE_VALIDATION_REQUIRED"
    assert record["candidate_retrieval"] == "SOURCE_LOCATION_REQUIRED"
    assert "original_packet" not in record
    assert record["decision"] is None and record["review_inputs"] is None


def test_foreign_tenant_claim_is_rejected_before_any_retrieval_or_request(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    original = runner.claims.load_evidence

    def foreign(tenant_id, target):
        extraction, discovery, graph = original(tenant_id, target)
        claims = tuple(replace(claim, tenant_id=str(uuid4())) for claim in discovery.claims)
        return extraction, replace(discovery, claims=claims), graph

    monkeypatch.setattr(runner.claims, "load_evidence", foreign)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "failed"
    assert runner.transport.requests == []
    assert service.cost(TENANT, run_id)["attempt_count"] == 0
    assert "tag_job" not in service.store.jobs.get_run(TENANT, run_id)


def test_null_track_tuple_is_blocked_not_an_uncaught_exception(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    classify = runner.preliminary
    runner.preliminary = lambda claim, graph: (None, *classify(claim, graph)[1:])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    assert runner.transport.requests == []
    assert (
        runner.tags.load_snapshot(TENANT, run_id)["claims"][0]["reason"]
        == "PRELIMINARY_TAGS_UNRESOLVED"
    )


def test_checkpoint_cannot_mislabel_execution_provenance(tmp_path, monkeypatch):
    service, run_id, runner, _, _ = verified_setup(tmp_path, monkeypatch)
    execute = runner._execute

    def wrong_marker(*args, **kwargs):
        payload = execute(*args, **kwargs)
        envelope = json.loads(payload)
        envelope["synthetic"] = False
        return json.dumps(envelope).encode()

    monkeypatch.setattr(runner, "_execute", wrong_marker)
    with pytest.raises(ValueError, match="TAG_CHECKPOINT_INVALID"):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert "tag_job" not in service.store.jobs.get_run(TENANT, run_id)
