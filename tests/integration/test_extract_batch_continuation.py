"""R03c: bounded extraction continuation over a fake transport and the real ledger.

Two batches advance the same run through its eligible sources, a simulated restart
between them replays a retained receipt instead of paying for it again, and a
provider stop keeps the charged work while leaving the rest pending. Coverage is
checked by exact source identifier, not by counts alone.
"""

import json
import threading
import time as time_module

import pytest
from proofops.adapters.local.claim_store import extraction_batch_state
from proofops.application.ports.jobs import JobMessage
from proofops_worker import extract_runner as extract_runner_module
from proofops_worker.extract_batch import run_batches

from tests.acceptance.test_upload import TENANT
from tests.integration.test_real_extract_runner import FakeProbe, inject_graph, real_setup


def published_state(runner, run_id=None):
    envelope, _, _ = runner.claims.load_evidence(TENANT, run_id or runner.batch_run_id)
    return extraction_batch_state(envelope)


def setup_run(tmp_path, monkeypatch, *, paragraphs=5, limit=2, probe=None, extractor_budget=None):
    probe = probe or FakeProbe('{"claims":[]}')
    if extractor_budget is not None:
        from proofops.application.budget import BudgetLimits, RoleLimit

        from tests.integration import test_run_lifecycle as lifecycle

        original_lifecycle_setup = lifecycle.setup

        def patched_lifecycle_setup(directory):
            service, body = original_lifecycle_setup(directory)
            service.budget_limits = BudgetLimits(
                100000,
                100000,
                (
                    RoleLimit("tagger", 3, 5000, 1000, 10000),
                    RoleLimit("extractor", extractor_budget, 100000, 1024, 101024),
                ),
            )
            return service, body

        monkeypatch.setattr(lifecycle, "setup", patched_lifecycle_setup)
    service, run_id, runner, now, probe = real_setup(
        tmp_path, monkeypatch, limit=limit, probe=probe
    )
    graph = inject_graph(monkeypatch, service, run_id, ["paragraph"] * paragraphs + ["table"])
    runner.batch_run_id = run_id
    return service, run_id, runner, now, probe, graph


def test_two_bounded_batches_cover_every_source_without_repeating_a_call(tmp_path, monkeypatch):
    service, run_id, runner, now, probe, graph = setup_run(tmp_path, monkeypatch, paragraphs=5)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    first_head = service.store.jobs.get_run(TENANT, run_id)["extract_job"]
    first_checkpoint = service.store.jobs.read_checkpoint(JobMessage(**first_head))
    assert len(probe.calls) == 2
    state = published_state(runner)
    assert len(state["model_processed"]) == 2 and len(state["pending"]) == 3

    summary = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert summary["status"] == "committed"
    assert len(summary["processed"]) == 2 and len(summary["replayed"]) == 2
    # The two paid sources were replayed, not re-sent.
    assert len(probe.calls) == 4
    second_head = service.store.jobs.get_run(TENANT, run_id)["extract_job"]
    assert second_head["job_id"] != first_head["job_id"]
    # The earlier revision stays byte-identical and readable by its own message.
    assert service.store.jobs.read_checkpoint(JobMessage(**first_head)) == first_checkpoint

    after = published_state(runner)
    assert len(after["model_processed"]) == 4 and len(after["pending"]) == 1
    assert set(state["model_processed"]) < set(after["model_processed"])
    assert after["skipped_non_paragraph"] and not after["failed"]

    third = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert third["status"] == "committed" and len(third["processed"]) == 1
    assert len(probe.calls) == 5
    final = published_state(runner)
    eligible = {
        block.source_id
        for block in graph.blocks
        if block.kind == "paragraph" and block.page_num in (1, 2, 3)
    }
    assert set(final["model_processed"]) == eligible and not final["pending"]

    # Nothing left: no further revision is published and no call is made.
    done = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert done["status"] == "complete" and len(probe.calls) == 5

    # Every claim is unique and every source is accounted for exactly once.
    discovery = runner.claims.load(TENANT, run_id)
    assert len({claim.claim_id for claim in discovery.claims}) == len(discovery.claims)
    accounted = (
        set(final["model_processed"])
        | set(final["pending"])
        | final["skipped_non_paragraph"]
        | final["failed"]
        | final["excluded"]
    )
    assert accounted == {block.source_id for block in graph.blocks}
    usage = service.cost(TENANT, run_id)
    assert usage["attempt_count"] == 5


def test_restart_after_a_retained_receipt_replays_it_without_a_new_charge(tmp_path, monkeypatch):
    """Crash between a charged provider response and the batch commit.

    The receipt directory already holds the response, the run head never moved, so
    the retry must reuse that receipt rather than pay for the same source twice.
    """
    service, run_id, runner, now, probe, graph = setup_run(tmp_path, monkeypatch, paragraphs=4)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    head = service.store.jobs.get_run(TENANT, run_id)["extract_job"]
    assert len(probe.calls) == 2

    original_commit = service.store.jobs.commit_job
    monkeypatch.setattr(service.store.jobs, "commit_job", lambda *a, **k: False)
    crashed = runner.run_batch(tenant_id=TENANT, run_id=run_id, max_calls=1)
    assert crashed["status"] == "discarded"
    charged = len(probe.calls)
    assert charged == 3  # the provider was called and charged once
    # Nothing was published: the head and its snapshot are unchanged.
    assert service.store.jobs.get_run(TENANT, run_id)["extract_job"] == head
    assert len(published_state(runner)["model_processed"]) == 2
    billed = service.cost(TENANT, run_id)["attempt_count"]

    monkeypatch.setattr(service.store.jobs, "commit_job", original_commit)
    now[0] += 400  # the crashed lease expires, as after a real restart
    retried = runner.run_batch(tenant_id=TENANT, run_id=run_id, max_calls=1)
    assert retried["status"] == "committed" and len(retried["processed"]) == 1
    # The retained receipt was served: no second provider call, no second charge.
    assert len(probe.calls) == charged
    assert service.cost(TENANT, run_id)["attempt_count"] == billed
    state = published_state(runner)
    assert len(state["model_processed"]) == 3 and len(state["pending"]) == 1


def test_long_continuation_chain_keeps_distinct_immutable_shards(tmp_path, monkeypatch):
    from dataclasses import replace

    from proofops.application.ingest.graph_fusion import fuse_candidates

    from tests.integration import test_real_extract_runner as fixture

    original_graph = fixture.graph_of_kinds

    def dense_graph(*args, **kwargs):
        batch = original_graph(*args, **kwargs).candidates[0]
        # Keep all 60 fixture paragraphs inside the three selected pages.
        blocks = tuple(
            replace(
                c,
                source=replace(c.source, native_bbox=(10, 10 + i // 3 * 30, 590, 30 + i // 3 * 30)),
            )
            for i, c in enumerate(batch.blocks)
        )
        return fuse_candidates((replace(batch, blocks=blocks),), tenant_id=TENANT)

    monkeypatch.setattr(fixture, "graph_of_kinds", dense_graph)
    service, run_id, runner, _, probe, graph = setup_run(
        tmp_path, monkeypatch, paragraphs=60, limit=1, extractor_budget=80
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    first = JobMessage(**service.store.jobs.get_run(TENANT, run_id)["extract_job"])
    checkpoint = service.store.jobs.read_checkpoint(first)
    results = run_batches(runner, tenant_id=TENANT, run_id=run_id, batches=70)
    assert results[-1]["status"] == "complete"
    expected = sum(b.kind == "paragraph" and b.page_num in (1, 2, 3) for b in graph.blocks)
    assert expected > 40 and len(probe.calls) == expected
    assert not published_state(runner)["pending"]
    assert service.store.jobs.read_checkpoint(first) == checkpoint


def test_receipt_replay_serves_a_retained_result_and_refuses_a_foreign_packet(tmp_path):
    from pathlib import Path

    from proofops.application.claims import ExtractionProfile
    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    probe = FakeProbe('{"claims":["the company cut emissions"]}')
    receipts = Path(tmp_path) / "receipts"
    extractor = UpstageClaimExtractor(probe, receipts)
    packet = dict(
        tenant_id=TENANT,
        document_version_id="11111111-1111-4111-8111-111111111111",
        parse_manifest_id="44444444-4444-4444-8444-444444444444",
        source_sha256="a" * 64,
        extraction_profile=json.loads(
            json.dumps(
                dict(
                    model_sha256=extractor.profile.model_sha256,
                    prompt_sha256=extractor.profile.prompt_sha256,
                    rule_sha256=extractor.profile.rule_sha256,
                    synthetic=False,
                    replicate_id=1,
                    extraction_epoch=1,
                )
            )
        ),
        untrusted_document_data=dict(
            source_id="22222222-2222-4222-8222-222222222222",
            page_num=1,
            kind="paragraph",
            text="the company cut emissions",
        ),
    )
    first = extractor.extract(packet)
    assert len(probe.calls) == 1
    # Same packet again: retained result, no second provider call.
    assert extractor.extract(packet) == first
    assert len(probe.calls) == 1

    # A different packet that collides with nothing still calls; a tampered packet
    # reusing the same identity is refused rather than served from the receipt.
    stored = next(path for path in receipts.iterdir() if path.is_dir())
    tampered = json.loads((stored / "packet.json").read_text())
    tampered["untrusted_document_data"]["text"] = "the company raised emissions"
    import pytest

    with pytest.raises(ValueError, match="EXTRACTION_RECEIPT_EXISTS"):
        extractor._replay_receipt(stored, tampered, "deadbeef", stored.name)
    assert isinstance(extractor.profile, ExtractionProfile)


def test_provider_stop_keeps_charged_work_and_leaves_the_rest_pending(tmp_path, monkeypatch):
    service, run_id, runner, now, probe, graph = setup_run(tmp_path, monkeypatch, paragraphs=4)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert len(probe.calls) == 2
    state = published_state(runner)
    assert len(state["pending"]) == 2

    # The next provider call stops on budget; the batch must still publish.
    from proofops.adapters.local import upstage as upstage_module

    stop = "BUDGET_EXHAUSTED"
    assert stop in upstage_module.UPSTAGE_TRANSPORT_STOP_CODES
    original = runner.extractor.extract

    def stopping(packet):
        raise ValueError(stop)

    monkeypatch.setattr(runner.extractor, "extract", stopping)
    summary = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert summary["status"] == "committed"
    assert summary["stop_code"] == stop and summary["processed"] == []
    published = published_state(runner)
    # The two paid sources keep their results; the rest stay pending, not absent.
    assert len(published["model_processed"]) == 2
    assert len(published["pending"]) == 2 and published["budget_stopped"]
    assert set(published["pending"]) == set(state["pending"])
    assert service.cost(TENANT, run_id)["attempt_count"] == 2

    # With the provider available again the pending sources are processed.
    monkeypatch.setattr(runner.extractor, "extract", original)
    resumed = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert resumed["status"] == "committed" and len(resumed["processed"]) == 2
    assert not published_state(runner)["pending"]


def test_batch_runner_stops_on_halt_and_validates_its_bound(tmp_path, monkeypatch):
    import pytest

    service, run_id, runner, now, probe, graph = setup_run(tmp_path, monkeypatch, paragraphs=3)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    results = run_batches(runner, tenant_id=TENANT, run_id=run_id, batches=5)
    assert [item["status"] for item in results][-1] == "complete"
    assert sum(len(item.get("processed", [])) for item in results) == 1
    with pytest.raises(ValueError, match="EXTRACTION_BATCH_COUNT_INVALID"):
        run_batches(runner, tenant_id=TENANT, run_id=run_id, batches=0)


def test_max_calls_cannot_raise_frozen_limit(tmp_path, monkeypatch):
    """CLI max-calls may narrow frozen limit but must fail closed if raised."""
    import pytest

    service, run_id, runner, now, probe, graph = setup_run(
        tmp_path, monkeypatch, paragraphs=5, limit=2
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    # Narrowing is allowed
    summary = runner.run_batch(tenant_id=TENANT, run_id=run_id, max_calls=1)
    assert summary["status"] == "committed" and len(summary["processed"]) == 1

    # Raising beyond frozen limit fails closed
    with pytest.raises(ValueError, match="EXTRACTION_INPUT_INVALID"):
        runner.run_batch(tenant_id=TENANT, run_id=run_id, max_calls=3)


def test_run_batch_blocked_after_tag_published(tmp_path, monkeypatch):
    """Extraction continuation explicitly blocks once tag stage is published."""
    service, run_id, runner, now, probe, graph = setup_run(
        tmp_path, monkeypatch, paragraphs=4, limit=2
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    # Simulate downstream tag publication
    with service.store.jobs._transaction() as db:
        run = service.store.jobs._get(db, TENANT, run_id, "run", "META")
        run["tag_job"] = {"job_id": "tag-job-test", "stage": "tag"}
        service.store.jobs._put(db, TENANT, run_id, "run", "META", run)

    blocked = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert blocked["status"] == "blocked"
    assert "cannot continue extraction after tag/review publication" in blocked["detail"]


def test_per_run_extractor_role_budget_cap(tmp_path, monkeypatch):
    """Cumulative extractor role budget cap latches budget stop before fresh model call."""
    service, run_id, runner, now, probe, graph = setup_run(
        tmp_path, monkeypatch, paragraphs=5, limit=2, extractor_budget=3
    )
    # First run processes 2 calls
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert len(probe.calls) == 2

    # Second batch wants window of 2, but role budget cap of 3 allows only 1 fresh call!
    summary = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert summary["status"] == "committed"
    assert len(summary["processed"]) == 1
    assert summary["stop_code"] == "BUDGET_EXHAUSTED"
    assert len(probe.calls) == 3

    # Stored state honestly records budget stop for remaining pending sources
    state = published_state(runner)
    assert len(state["model_processed"]) == 3
    assert state["budget_stopped"]


def test_failure_then_next_batch_advances_healthy_sources_and_counts_toward_cap(
    tmp_path, monkeypatch
):
    """Failed responses leave the next window, replay without a call, and count toward the cap."""
    call_count = 0

    def mock_post(body):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            content = "not-valid-json{{{"
        else:
            content = '{"claims":[]}'
        return dict(
            id="test-provider",
            model="solar-pro3",
            usage=dict(prompt_tokens=50, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=content))],
        )

    probe = FakeProbe('{"claims":[]}')
    probe._client._post = mock_post
    service, run_id, runner, now, probe, graph = setup_run(
        tmp_path, monkeypatch, paragraphs=4, limit=2, probe=probe, extractor_budget=3
    )

    # First run: 1st paragraph succeeds, 2nd paragraph fails schema validation
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert len(probe.calls) == 2
    state1 = published_state(runner)
    assert len(state1["model_processed"]) == 1
    assert len(state1["failed"]) == 1

    # Second run:
    # 1. The failed 2nd paragraph is in completed, so it does NOT reoccupy the window.
    # 2. The 3rd paragraph advances into the window!
    # 3. Total charged so far is 2 (1 success + 1 charged failure).
    # 4. Total cap is 3, so only 1 more call is allowed.
    # 5. 3rd paragraph consumes the 3rd call; 4th paragraph stops on budget.
    summary = runner.run_batch(tenant_id=TENANT, run_id=run_id)
    assert summary["status"] == "committed"
    assert len(summary["processed"]) == 1
    assert summary["stop_code"] == "BUDGET_EXHAUSTED"
    assert len(probe.calls) == 3

    # Verify final state:
    state2 = published_state(runner)
    assert len(state2["model_processed"]) == 2
    assert len(state2["failed"]) == 1
    assert state2["budget_stopped"]


def test_forged_result_json_without_raw_or_differing_quotes_refused(tmp_path):
    """Replay refuses a result when raw data is missing or its quotes differ."""
    from dataclasses import asdict
    from pathlib import Path
    from uuid import UUID, uuid5

    import pytest
    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rulepacks import canonical_json
    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    probe = FakeProbe('{"claims":[]}')
    receipts = Path(tmp_path) / "receipts"
    extractor = UpstageClaimExtractor(probe, receipts)
    packet = dict(
        tenant_id=TENANT,
        document_version_id="11111111-1111-4111-8111-111111111111",
        parse_manifest_id="44444444-4444-4444-8444-444444444444",
        source_sha256="a" * 64,
        extraction_profile=asdict(extractor.profile),
        untrusted_document_data=dict(
            source_id="22222222-2222-4222-8222-222222222222",
            page_num=1,
            kind="paragraph",
            text="emissions cut reported in full",
        ),
    )
    packet_sha = canonical_hash(packet)
    req_id = str(uuid5(UUID(packet["parse_manifest_id"]), packet_sha))
    req_dir = receipts / req_id
    req_dir.mkdir(parents=True)
    (req_dir / "packet.json").write_text(canonical_json(packet))
    (req_dir / "request.json").write_text(canonical_json({"request_id": req_id}))

    # Missing raw_response.json raises EXTRACTION_RECEIPT_UNSETTLED, even with a result.
    forged_result = {
        "request_id": req_id,
        "packet_sha256": packet_sha,
        "profile": asdict(extractor.profile),
        "spans": [
            {
                "char_start": 0,
                "char_end": 9,
                "quote": "emissions",
                "kind": "claim",
                "reason": None,
                "topic_ids": [],
            }
        ],
    }
    (req_dir / "result.json").write_text(canonical_json(forged_result))
    with pytest.raises(ValueError, match="EXTRACTION_RECEIPT_UNSETTLED"):
        extractor.extract(packet)

    # A result/raw quote mismatch raises EXTRACTION_RECEIPT_EXISTS.
    raw_content = json.dumps({"claims": ["emissions cut reported"]})
    (req_dir / "raw_response.json").write_text(canonical_json({"content": raw_content}))
    with pytest.raises(ValueError, match="EXTRACTION_RECEIPT_EXISTS"):
        extractor.extract(packet)


def test_unsettled_receipt_not_automatically_charged(tmp_path):
    """Unsettled receipt directory raises EXTRACTION_RECEIPT_UNSETTLED without new provider call."""
    from pathlib import Path

    import pytest
    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    probe = FakeProbe('{"claims":[]}')
    receipts = Path(tmp_path) / "receipts"
    extractor = UpstageClaimExtractor(probe, receipts)
    from dataclasses import asdict

    packet = dict(
        tenant_id=TENANT,
        document_version_id="11111111-1111-4111-8111-111111111111",
        parse_manifest_id="44444444-4444-4444-8444-444444444444",
        source_sha256="a" * 64,
        extraction_profile=asdict(extractor.profile),
        untrusted_document_data=dict(
            source_id="22222222-2222-4222-8222-222222222222",
            page_num=1,
            kind="paragraph",
            text="emissions cut reported",
        ),
    )
    # Simulate crashed state: directory exists with only packet.json (call was never settled)
    from uuid import UUID, uuid5

    from proofops.domain.provenance import canonical_hash
    from proofops.domain.rulepacks import canonical_json

    packet_sha = canonical_hash(packet)
    req_id = str(uuid5(UUID(packet["parse_manifest_id"]), packet_sha))
    req_dir = receipts / req_id
    req_dir.mkdir(parents=True)
    (req_dir / "packet.json").write_text(canonical_json(packet))

    with pytest.raises(ValueError, match="EXTRACTION_RECEIPT_UNSETTLED"):
        extractor.extract(packet)
    # No provider call was made!
    assert len(probe.calls) == 0


def test_cli_restart_after_batch_duplicate_and_orphan_tag_lease(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops_worker import composition
    from proofops_worker.extract_runner import LocalExtractRunner

    from evaluation.local_upstage_pilot import run_live_stages

    service, run_id, runner, now, probe, _ = setup_run(tmp_path, monkeypatch, paragraphs=5)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert runner.run_batch(tenant_id=TENANT, run_id=run_id)["status"] == "committed"
    jobs = service.store.jobs
    head = JobMessage(**jobs.get_run(TENANT, run_id)["extract_job"])
    checkpoint = jobs.read_checkpoint(head)
    tag = next(
        JobMessage(**event["message"])
        for event in jobs.pending_outbox(TENANT, run_id, now=now[0])
        if event["message"]["stage"] == "tag"
    )
    lease = jobs.claim_job(tag, owner="interrupted-tag", now=now[0], lease_seconds=300)
    assert lease is not None
    assert runner.run_batch(tenant_id=TENANT, run_id=run_id)["status"] == "blocked"
    assert len(probe.calls) == 4
    now[0] += 301
    restarted = LocalExtractRunner(
        LocalSQLiteRunStore(service.store.path),
        service.uploads,
        runner.parser,
        extractor=runner.extractor,
        telemetry=runner.telemetry,
        clock=lambda: now[0],
    )
    stages, extraction_outcomes = [], []

    def run_extract(**kwargs):
        outcome = restarted.run_once(**kwargs)
        extraction_outcomes.append(outcome)
        return outcome

    def tag_once(**kwargs):
        state = published_state(restarted, run_id)
        assert not state["pending"], "tagging started before extraction finished"
        assert len(state["model_processed"]) == len(probe.calls) == 5
        assert jobs.claim_job(tag, owner="stale-delivery", now=now[0], lease_seconds=300) is None
        assert not jobs.can_call(lease, now=now[0])
        assert not jobs.commit_job(lease, payload=b"{}", now=now[0])
        return "committed"

    def build(**options):
        stage = options["stage"]
        stages.append(stage)
        return SimpleNamespace(
            run_once=run_extract
            if stage == "extract"
            else tag_once
            if stage == "tag"
            else lambda **_: "pending_downstream",
            run_batch=restarted.run_batch,
            uploads=SimpleNamespace(
                close=lambda: None, registry=SimpleNamespace(close=lambda: None)
            ),
        )

    monkeypatch.setattr(composition, "build_composition", build)
    args = SimpleNamespace(
        verify_paragraphs=False,
        native_quote_typography=False,
        raster_ocr=False,
        extraction_total_calls=6,
        max_calls=2,
    )
    assert run_live_stages(args, tenant_id=TENANT, run_id=run_id)["exit_code"] == 0
    assert extraction_outcomes == ["ignored"]  # stale committed batch outbox after restart
    assert stages == ["parse", "extract", "tag"]
    assert jobs.read_checkpoint(head) == checkpoint
    assert service.cost(TENANT, run_id)["attempt_count"] == len(probe.calls) == 5


def test_continuation_fences_orphan_tag_atomically_before_provider_work(tmp_path):
    from dataclasses import asdict, replace
    from uuid import UUID

    import pytest
    from proofops.application.ports.jobs import LeaseLost

    from tests.acceptance.test_jobs import MESSAGE, RUN, seeded
    from tests.acceptance.test_jobs import TENANT as JOB_TENANT

    jobs = seeded(tmp_path / "jobs.sqlite")
    tag = replace(MESSAGE, job_id=str(UUID(int=81)), stage="tag")
    batch = replace(MESSAGE, job_id=str(UUID(int=82)), stage="extract", shard="batch")
    jobs.enqueue(tag, now=0)
    jobs.enqueue(batch, now=0)
    # Fixture represents the already-published extract head (no PDF subprocess).
    with jobs._transaction() as db:
        run = jobs._get(db, JOB_TENANT, RUN, "run", "META")
        run["extract_job"] = asdict(replace(MESSAGE, stage="extract"))
        jobs._put(db, JOB_TENANT, RUN, "run", "META", run)
    old = jobs.claim_job(tag, owner="orphan", now=0, lease_seconds=300)
    assert old is not None
    assert jobs.claim_job(batch, owner="resume", now=299, lease_seconds=300) is None
    assert jobs.delivery_status(tag) == "leased"
    new = jobs.claim_job(batch, owner="resume", now=300, lease_seconds=300)
    assert new is not None
    assert jobs.delivery_status(tag) == "superseded"
    assert jobs.get_job(tag)["fencing_token"] > old.fencing_token
    assert jobs.claim_job(tag, owner="duplicate", now=301, lease_seconds=300) is None
    with pytest.raises(LeaseLost):
        jobs.heartbeat(old, now=301, lease_seconds=300)
    assert not jobs.commit_job(old, payload=b"stale", now=301)
    assert not any(
        event["message"]["stage"] == "tag"
        for event in jobs.pending_outbox(JOB_TENANT, RUN, now=301)
    )

    # A prior immutable tag/review publication still blocks even without run markers.
    with jobs._transaction() as db:
        jobs._put(db, JOB_TENANT, RUN, "tag_revision", "prior:1", {}, immutable=True)
    assert jobs.claim_job(batch, owner="later", now=601, lease_seconds=300) is None


@pytest.mark.parametrize("keepalive", [False, True])
@pytest.mark.parametrize("entrypoint", ["batch", "once"])
def test_batch_over_lease_publication_result_depends_on_lease_keepalive(
    tmp_path, monkeypatch, keepalive, entrypoint
):
    """R10 recovery: a post-model step that outlives the lease must not silently
    discard a batch that otherwise succeeded (Lotte's actual failure), and a real
    renewal failure must still block publication (parser suite's existing coverage).

    Without ``with_lease_heartbeat`` wrapping the batch operation, a single
    heartbeat taken before the model call does not cover work that runs after
    it (checkpoint assembly here stands in for the real bottleneck: claim-span
    attestation cost that grows with the run's accumulated processed count).
    The lease then expires before ``commit_job`` runs its own ownership fence,
    and the batch is discarded even though nothing about the extraction itself
    failed. This is red on ``keepalive=False`` (bypassing the fix) and green on
    ``keepalive=True`` (the fix applied), proving the keepalive is what changes
    the outcome rather than test timing alone.
    """
    service, run_id, runner, now, probe, graph = setup_run(
        tmp_path, monkeypatch, paragraphs=3, limit=1
    )
    if entrypoint == "batch":
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
        assert len(probe.calls) == 1

    if not keepalive:
        # Bypass the fix: run the batch operation directly, without the
        # keepalive wrapper consume_job normally applies around it.
        monkeypatch.setattr(
            extract_runner_module,
            "with_lease_heartbeat",
            lambda store, lease, clock, operation: operation(lease),
        )

    # Wall-clock-driven runner clock: any heartbeat renews in real time,
    # matching production timing rather than a manually-advanced integer clock.
    started = time_module.monotonic()
    base_now = int(now[0])
    runner.clock = lambda: base_now + time_module.monotonic() - started

    original_consume_job = extract_runner_module.consume_job

    def short_lease_consume(*args, **kwargs):
        kwargs["lease_seconds"] = 2
        return original_consume_job(*args, **kwargs)

    monkeypatch.setattr(extract_runner_module, "consume_job", short_lease_consume)

    # The model-call heartbeat inside FencedLive.extract always requests a
    # fixed 300s extension in production. Cap every heartbeat's grant at this
    # batch's own short lease window so the reproduction reflects "the post
    # model step outlasts the lease" rather than being masked by an unrelated
    # hardcoded renewal window.
    original_heartbeat = service.store.jobs.heartbeat

    def capped_heartbeat(lease, *, now, lease_seconds):
        return original_heartbeat(lease, now=now, lease_seconds=min(lease_seconds, 2))

    monkeypatch.setattr(service.store.jobs, "heartbeat", capped_heartbeat)

    target, attribute = (
        (runner, "_publication_payload")
        if entrypoint == "batch"
        else (extract_runner_module, "load_run_graph")
    )
    original_publication_payload = getattr(target, attribute)

    def slow_publication_payload(*args, **kwargs):
        # Stands in for the real bottleneck (claim-span attestation growing
        # with accumulated processed sources), landing after the last model
        # call and outlasting the batch's lease window.
        time_module.sleep(2.5)
        return original_publication_payload(*args, **kwargs)

    monkeypatch.setattr(target, attribute, slow_publication_payload)

    summary = (
        runner.run_batch(tenant_id=TENANT, run_id=run_id)
        if entrypoint == "batch"
        else {"status": runner.run_once(tenant_id=TENANT, run_id=run_id)}
    )
    monkeypatch.setattr(target, attribute, original_publication_payload)
    elapsed = time_module.monotonic() - started
    assert elapsed > 2

    if keepalive:
        assert summary["status"] == "committed"
        state = published_state(runner, run_id)
        assert len(state["model_processed"]) == (2 if entrypoint == "batch" else 1)
    else:
        assert summary["status"] == "discarded"
        if entrypoint == "batch":
            assert len(published_state(runner, run_id)["model_processed"]) == 1
        else:
            assert "extract_job" not in service.store.jobs.get_run(TENANT, run_id)

    assert not any(t.name.startswith("lease-heartbeat:") for t in threading.enumerate())
