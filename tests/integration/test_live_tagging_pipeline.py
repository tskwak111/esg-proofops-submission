"""Real-mode LocalTagRunner pipeline over a genuine RunService upstage_local run.

Coverage: genuine ``RunService.create`` snapshot (``LOCAL_TAGGING_MODE`` is not
edited in), real ``LocalParserRunner`` / ``LocalExtractRunner`` checkpoints,
then ``LocalTagRunner`` with ``LiveTaggingRuntime`` publishing a candidate
review, plus reopen/read replay and duplicate-billing assertions.

Fakes (documented, no paid calls, no domain approvals):

- Source parser is a mocked verified native response derived from
  ``test_local_tag_runner`` (``SyntheticVerifiedParser`` with native provenance fixture markers):
  real PDF bytes/upload stores are used, only the parse/validation boundary is
  substituted, and the resulting claims stay candidate tags for human review.
- All external model HTTP is faked at ``UpstageProbe._post``: the ledger,
  reservations, receipts, authorization, and both real transports
  (``UpstageClaimExtractor``, ``UpstagePreliminaryTransport`` /
  ``UpstageTaggingTransport`` via ``LiveTaggingRuntime``) stay real. No real
  network, no paid dispatch, no credentials.
- Draft rulepack => ``rulepack_use=candidate_tagging_reference_only`` => no
  grade is evaluated or published; element replies stay ``unknown``.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, replace
from datetime import UTC as _UTC
from datetime import datetime as _RealDatetime
from io import StringIO
from uuid import uuid4

import pytest
from proofops.adapters.local.upstage import MODEL, MODEL_PRO4, UpstageProbe
from proofops.application.ingest.graph_fusion import ParserProfile
from proofops.application.telemetry import Telemetry

from tests.acceptance.test_preflight import AUTH
from tests.integration.test_live_tagging_runtime_config import (
    LIVE_CLOCK,
    _live_service,
)
from tests.integration.test_local_tag_runner import SyntheticVerifiedParser


@pytest.fixture(autouse=True)
def _freeze_upstage_price_clock(monkeypatch):
    """Keep the offline probe price guard deterministic (fake HTTP only)."""

    class FixedDateTime(_RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return _RealDatetime(2026, 9, 10, tzinfo=_UTC).astimezone(tz)

    monkeypatch.setattr("proofops.adapters.local.upstage.datetime", FixedDateTime)


def _fake_post_factory(calls):
    """Fake ``UpstageProbe._post``: extraction, preliminary, and element wires.

    Returns provider-shaped payloads with distinct fake IDs per call while the
    real ``UpstageProbe.complete`` ledger/reservation/receipt path stays live.
    Extraction returns the exact source text as one claim; preliminary returns
    a matching management track with null dimensions; element tagging returns
    all-``unknown`` elements (no fake domain approvals).
    """

    from proofops.application.tagging.preliminary import SYSTEM_PROMPT as PRELIM_SYSTEM
    from proofops.application.tagging.relations import SYSTEM_PROMPT as RELATION_SYSTEM
    from proofops_agent.upstage_extraction import SYSTEM_PROMPT as EXTRACT_SYSTEM

    def post(body):
        calls.append(body)
        system = body["messages"][0]["content"]
        user_raw = body["messages"][1]["content"]
        provider_id = f"fake-provider-{len(calls)}"
        base = {
            "id": provider_id,
            "model": body["model"],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
        if system == EXTRACT_SYSTEM:
            payload = json.loads(user_raw)
            text = payload["untrusted_document_data"]["text"].strip()
            content = json.dumps({"claims": [text]}, ensure_ascii=False)
        elif system.startswith(RELATION_SYSTEM):
            envelope = json.loads(user_raw)
            content = json.dumps(
                dict(
                    relations=[
                        dict(
                            source_index=source["source_index"],
                            dimensions=dict(entity=None, metric=None, reporting_period=None),
                        )
                        for source in envelope["untrusted_document_data"]["sources"]
                    ]
                )
            )
        elif system.startswith(PRELIM_SYSTEM):
            envelope = json.loads(user_raw)
            content = json.dumps(
                {
                    "claim_id": envelope["claim_id"],
                    "track": "management",
                    "safe_harbor_category": None,
                    "track_confidence": 0.8,
                    "dimensions": {
                        "entity": None,
                        "metric": None,
                        "reporting_period": None,
                    },
                },
                ensure_ascii=False,
            )
        else:
            wire_user = json.loads(user_raw)
            match = re.search(r'"track":\s*"(goal|performance|management)"', system)
            track = match.group(1) if match else "management"
            harbor = re.search(
                r'"safe_harbor_category":\s*(null|"(?:forward_looking|'
                r'emissions_estimate|third_party_information)")',
                system,
            )
            safe_harbor = (
                None if harbor is None or harbor.group(1) == "null" else harbor.group(1).strip('"')
            )
            allowed = wire_user["untrusted_document_data"]["allowed_elements"]
            content = json.dumps(
                {
                    "claim_id": wire_user["claim_id"],
                    "packet_sha256": wire_user["packet_sha256"],
                    "replicate_id": wire_user["replicate_id"],
                    "track": track,
                    "safe_harbor_category": safe_harbor,
                    "elements": [
                        {
                            "element_id": element_id,
                            "state": "unknown",
                            "evidence_refs": [],
                            "normalized_value": None,
                            "credited_from": None,
                            "reason_code": None,
                        }
                        for element_id in allowed
                    ],
                    "superlative_quote": None,
                    "warnings": [],
                },
                ensure_ascii=False,
            )
        return dict(
            base,
            choices=[{"finish_reason": "stop", "message": {"content": content}}],
        )

    return post


def _pipeline_setup(
    tmp_path, monkeypatch, *, relation_stage=False, fact_assembly_profile="strict-v1"
):
    """Build a genuine upstage_local run plus parse/extract checkpoints."""
    from proofops.application.rulepacks import RulePackRecord
    from proofops_worker.extract_runner import LocalExtractRunner
    from proofops_worker.live_tagging import LiveTaggingRuntime
    from proofops_worker.local_runner import LocalParserRunner
    from proofops_worker.tag_runner import LocalTagRunner

    from tests.acceptance.test_parsing import pdf
    from tests.acceptance.test_rules import pack as full_pack
    from tests.integration import test_run_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "pdf", lambda count: pdf())
    service, body, _, _ = _live_service(tmp_path)
    service.fact_assembly_profile = fact_assembly_profile
    if relation_stage:
        from tests.integration.test_relation_runtime_config import _relation

        service.relation_settings = _relation(service)
    tenant = AUTH.tenant_id
    from proofops.application.registry import Registry

    registry = Registry.sqlite(service.store.path)
    for option in service.registry._options.values():
        values = asdict(option)
        values["option_id"] = values.pop("id")
        registry.with_option(**values)
    service.registry = registry
    full = full_pack()
    pack = RulePackRecord(
        **{
            key: value
            for key, value in (
                asdict(full)
                | dict(
                    rule_pack_id=str(uuid4()),
                    tenant_id=tenant,
                    status="validated",
                    approved_by=None,
                    approved_at=None,
                )
            ).items()
            if key != "content"
        }
    )
    service.store.rulepacks.add_pack(pack, {p: full.file_content(p) for p in full.files})
    body["rule_pack_id"] = pack.rule_pack_id
    run_id = service.create(AUTH, body, str(uuid4()))["run_id"]
    snapshot = service.store.snapshot(tenant, run_id)
    assert snapshot["tagging_mode"] == "upstage_local"
    assert snapshot["rulepack_use"] == "candidate_tagging_reference_only"

    now = [LIVE_CLOCK]
    stream = StringIO()
    telemetry = Telemetry(service="worker", env="test", stream=stream, hash_key=b"x" * 32)

    class FixtureNativeParser(SyntheticVerifiedParser):
        # Offline fixture for a verified native parser response. No production
        # quality claim: this entire adapter and its verification are mocked.
        def parse(self, source, profile, *, tenant_id):
            graph = super().parse(source, profile, tenant_id=tenant_id)
            if relation_stage:
                from proofops.application.ingest.graph_fusion import fuse_candidates

                batch = graph.candidates[0]
                candidate = batch.blocks[0]
                text = candidate.source.raw_text + " Supporting context."
                extra = replace(
                    candidate,
                    source=replace(
                        candidate.source,
                        source_native_id="external-context",
                        native_bbox=(72, 600, 550, 640),
                        raw_text=text,
                        char_end=len(text),
                    ),
                )
                graph = fuse_candidates(
                    (replace(batch, blocks=batch.blocks + (extra,)),), tenant_id=tenant_id
                )
                graph = replace(
                    graph,
                    blocks=tuple(replace(block, quality="verified") for block in graph.blocks),
                )
            return replace(
                graph,
                candidates=tuple(replace(batch, synthetic=False) for batch in graph.candidates),
            )

    parser = FixtureNativeParser(tmp_path / "synthetic-prepared")
    profile = ParserProfile(str(uuid4()), **service.parser_profile)
    parse_runner = LocalParserRunner(
        service.store,
        service.uploads,
        parser,
        profile=profile,
        telemetry=telemetry,
        clock=lambda: now[0],
    )
    assert parse_runner.run_once(tenant_id=tenant, run_id=run_id) == "committed"

    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    calls: list = []
    fake_post = _fake_post_factory(calls)
    extract_probe = UpstageProbe("test-not-a-key", tmp_path / "extract-budget.sqlite3")
    tag_probe = UpstageProbe("test-not-a-key", tmp_path / "tag-budget.sqlite3", model=MODEL_PRO4)
    assert extract_probe.model == MODEL
    monkeypatch.setattr(extract_probe, "_post", fake_post)
    monkeypatch.setattr(tag_probe, "_post", fake_post)
    extractor = UpstageClaimExtractor(extract_probe, tmp_path / "extract-receipts", max_tokens=1024)
    extract_runner = LocalExtractRunner(
        service.store,
        service.uploads,
        parser,
        extractor=extractor,
        telemetry=telemetry,
        clock=lambda: now[0],
    )
    assert extract_runner.run_once(tenant_id=tenant, run_id=run_id) == "committed"

    runtimes: list = []
    receipts_root = tmp_path / "tag-receipts" / run_id

    def live_factory(owner, frozen, graph, lease, usage):
        runtime = LiveTaggingRuntime(
            owner,
            frozen,
            graph,
            lease,
            usage,
            probe=tag_probe,
            ledger=tmp_path / "tag-budget.sqlite3",
            receipts=receipts_root,
        )
        runtimes.append(runtime)
        return runtime

    tag_runner = LocalTagRunner(
        service.store,
        service.uploads,
        parser,
        telemetry=telemetry,
        live_factory=live_factory,
        clock=lambda: now[0],
    )
    return {
        "service": service,
        "tenant": tenant,
        "run_id": run_id,
        "pack": pack,
        "now": now,
        "stream": stream,
        "telemetry": telemetry,
        "parser": parser,
        "tag_runner": tag_runner,
        "runtimes": runtimes,
        "calls": calls,
        "extract_probe": extract_probe,
        "tag_probe": tag_probe,
    }


def test_live_pipeline_publishes_candidate_review_and_replays(tmp_path, monkeypatch):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops_worker.tag_runner import LocalTagRunner

    ctx = _pipeline_setup(tmp_path, monkeypatch)
    service, tenant, run_id = ctx["service"], ctx["tenant"], ctx["run_id"]
    runner, calls = ctx["tag_runner"], ctx["calls"]
    extract_calls = len(calls)

    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    assert len(calls) == extract_calls + 6  # 3 preliminary + 3 element, all fake HTTP
    assert len(ctx["runtimes"]) == 1
    live = ctx["runtimes"][0]
    assert live.synthetic is False

    claims = runner.claims.list(tenant, run_id)
    assert len(claims) == 1 and claims[0].source_quality == "verified"
    claim_id = claims[0].claim_id
    assert len(live.preliminary_records[claim_id]) == 3
    assert all(
        record["status"] == "validated_candidate" for record in live.preliminary_records[claim_id]
    )

    envelope = runner.tags.load_snapshot(tenant, run_id)
    assert envelope["synthetic"] is False
    assert envelope["stage_status"] == "needs_review"
    assert envelope["rulepack_use"] == "candidate_tagging_reference_only"
    (record,) = envelope["claims"]
    assert record["reason"] == "DOMAIN_RULEPACK_UNAPPROVED"
    assert record["claim_id"] == claim_id
    assert len(record["tag_runs"]) == 3
    assert record["decision"] is None  # draft rulepack => no grade
    assert record["review_inputs"] is not None
    assert len({run["request"]["request_signature"] for run in record["tag_runs"]}) == 3
    assert len({run["packet_sha256"] for run in record["tag_runs"]}) == 1
    assert all(run["raw_response_json"] and run["usage"] for run in record["tag_runs"])

    inputs = runner.tags.load_inputs(tenant, run_id, claim_id)
    assert inputs.packet != inputs.original_packet
    assert all(element.state == "unknown" for element in inputs.consensus.candidate_elements)
    assert inputs.consensus.confirmed_tags is None
    assert inputs.decision is None

    from proofops.adapters.local.review_store import LocalSQLiteReviewStore

    reviews = LocalSQLiteReviewStore(service.store.jobs)
    history = reviews.history(tenant, run_id, claim_id)
    assert len(history["tags"]) == 1 and history["decisions"] == []

    with service.store.jobs._transaction() as db:
        heads = service.store.jobs._all(db, tenant, run_id, "review_head")
        assert len(heads) == 1 and heads[0]["base_tag_revision"] == 1
        assert "RULEPACK_APPROVAL_REQUIRED" in heads[0]["reason_codes"]
        assert service.store.jobs._all(db, tenant, run_id, "decision_revision") == []
        assert (
            db.execute(
                "SELECT count(*) FROM audit_events WHERE tenant_id=? AND run_id=? "
                "AND action='tag_stage_published'",
                (tenant, run_id),
            ).fetchone()[0]
            == 1
        )

    # Tag receipts are real and immutable; provider IDs are distinct per replica.
    receipt_ids = [
        run["request_id"] if isinstance(run, dict) else run.request.request_id
        for run in inputs.tag_runs
    ]
    assert len(set(receipt_ids)) == 3
    summary = ctx["tag_probe"].summary()
    assert summary["calls"] == 6 and summary["unsettled_calls"] == 0
    assert ctx["extract_probe"].summary()["unsettled_calls"] == 0

    # Reopen/read replay: no new HTTP, no new billing, identical inputs.
    before_attempts = service.cost(tenant, run_id)["attempt_count"]
    reopened = LocalTagRunner(
        LocalSQLiteRunStore(service.store.path),
        service.uploads,
        runner.parser,
        telemetry=runner.telemetry,
        live_factory=runner.live_factory,
        clock=runner.clock,
    )
    assert reopened.tags.load_inputs(tenant, run_id, claim_id) == inputs
    assert reopened.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    assert len(calls) == extract_calls + 6
    assert service.cost(tenant, run_id)["attempt_count"] == before_attempts
    assert "1234 tCO2e" not in ctx["stream"].getvalue()


@pytest.mark.parametrize("selected", ["strict-v1", "partial-facts-v1"])
def test_fact_assembly_env_selection_is_pinned_and_replayed(tmp_path, monkeypatch, selected):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops_api.local_runtime import load_local_runtime
    from proofops_worker.tag_runner import LocalTagRunner

    from tests.integration.test_live_tagging_runtime_config import (
        POLICY,
        _base_settings,
        _config_paths,
        _preliminary_dict,
        _tagging_dict,
    )

    settings = _base_settings()
    settings.update(
        build_root=str(tmp_path),
        preliminary_settings=_preliminary_dict(),
        tagging_settings=_tagging_dict(synthetic=False),
        input_reservation_policy=dict(POLICY),
    )
    env = _config_paths(tmp_path, settings) | {
        "LOCAL_EXTRACTION_MODE": "upstage_probe",
        "LOCAL_TAGGING_MODE": "upstage_local",
    }
    if selected != "strict-v1":
        env["LOCAL_FACT_ASSEMBLY_PROFILE"] = selected
    runtime = load_local_runtime(env)
    assert runtime.get("fact_assembly_profile", "strict-v1") == selected
    ctx = _pipeline_setup(
        tmp_path,
        monkeypatch,
        fact_assembly_profile=runtime.get("fact_assembly_profile", "strict-v1"),
    )
    service, tenant, run_id = ctx["service"], ctx["tenant"], ctx["run_id"]
    snapshot = service.store.snapshot(tenant, run_id)
    assert snapshot.get("fact_assembly_profile", "strict-v1") == selected
    assert ctx["tag_runner"].run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    claim_id = ctx["tag_runner"].claims.list(tenant, run_id)[0].claim_id
    reopened = LocalTagRunner(
        LocalSQLiteRunStore(service.store.path),
        service.uploads,
        ctx["parser"],
        telemetry=ctx["telemetry"],
        live_factory=ctx["tag_runner"].live_factory,
        clock=ctx["tag_runner"].clock,
    )
    inputs = reopened.tags.load_inputs(tenant, run_id, claim_id)
    assert inputs.fact_assembly_profile == selected
    assert reopened.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"


def test_category_only_preliminary_disagreement_keeps_track_retrieval_and_needs_review(
    tmp_path, monkeypatch
):
    """Three replicas agree on the track and differ only on the safe-harbor category.

    That must stay an explicit needs-review with field-level evidence of what was
    agreed, plus the local candidate retrieval, instead of looking like no
    information. The unagreed category must read as a conflict, never as null.
    """
    from proofops.application.tagging.preliminary import SYSTEM_PROMPT as PRELIM_SYSTEM

    ctx = _pipeline_setup(tmp_path, monkeypatch)
    service, tenant, run_id = ctx["service"], ctx["tenant"], ctx["run_id"]
    runner, calls = ctx["tag_runner"], ctx["calls"]
    extract_calls = len(calls)
    fake_post = ctx["tag_probe"]._post
    preliminary_calls = []

    def varied(body):
        response = fake_post(body)
        if body["messages"][0]["content"].startswith(PRELIM_SYSTEM):
            preliminary_calls.append(body)
            content = json.loads(response["choices"][0]["message"]["content"])
            content["safe_harbor_category"] = (
                "forward_looking" if len(preliminary_calls) == 3 else None
            )
            response["choices"][0]["message"]["content"] = json.dumps(content, ensure_ascii=False)
        return response

    monkeypatch.setattr(ctx["tag_probe"], "_post", varied)
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "blocked"
    # Only the three preliminary replicas ran; no element tagging was purchased.
    assert len(preliminary_calls) == 3
    assert len(calls) == extract_calls + 3
    assert ctx["tag_probe"].summary()["calls"] == 3
    assert ctx["tag_probe"].summary()["unsettled_calls"] == 0

    (record,) = runner.tags.load_snapshot(tenant, run_id)["claims"]
    assert record["reason"] == "PRELIMINARY_TAGS_UNRESOLVED"
    assert record["tag_runs"] == [] and record["decision"] is None
    assert record["review_inputs"] is None
    assert len(record["preliminary_records"]) == 3
    assert all(r["status"] == "validated_candidate" for r in record["preliminary_records"])

    agreement = record["preliminary_agreement"]
    assert agreement["schema"] == "preliminary_field_agreement_v1"
    assert agreement["validated_replicates"] == 3
    assert agreement["fields"]["track"] == dict(
        state="agreed",
        replicate_values=["management"] * 3,
        distinct_count=1,
    )
    category = agreement["fields"]["safe_harbor_category"]
    assert category["state"] == "conflict"
    assert category["replicate_values"] == [None, None, "forward_looking"]
    assert category["distinct_count"] == 2
    assert all(
        agreement["dimensions"][axis]["state"] == "agreed"
        for axis in ("entity", "metric", "reporting_period")
    )

    packet = record["original_packet"]
    assert packet["status"] == "candidate" and packet["evidence_candidates"]
    assert packet["claim_id"] == record["claim_id"]
    assert "track" not in packet and "retrieval_packet_sha256" not in packet

    with pytest.raises(KeyError):
        runner.tags.load_inputs(tenant, run_id, record["claim_id"])
    from proofops.adapters.local.review_store import LocalSQLiteReviewStore

    history = LocalSQLiteReviewStore(service.store.jobs).history(tenant, run_id, record["claim_id"])
    assert history["tags"] == [] and history["decisions"] == []
    assert "1234 tCO2e" not in ctx["stream"].getvalue()


def test_live_publication_crash_rolls_back_and_recovers_without_rebilling(tmp_path, monkeypatch):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops_worker.tag_runner import LocalTagRunner

    ctx = _pipeline_setup(tmp_path, monkeypatch)
    runner, service = ctx["tag_runner"], ctx["service"]
    tenant, run_id = ctx["tenant"], ctx["run_id"]
    before_calls = len(ctx["calls"])
    publish = runner.reviews.store.publish_transaction

    def crash(db, inputs):
        publish(db, inputs)
        raise RuntimeError("injected crash after review publication")

    monkeypatch.setattr(runner.reviews.store, "publish_transaction", crash)
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "failed"
    assert len(ctx["calls"]) == before_calls + 6
    assert "tag_job" not in service.store.jobs.get_run(tenant, run_id)
    with service.store.jobs._transaction() as db:
        for kind in (
            "claim_head",
            "tag_revision",
            "decision_revision",
            "review_head",
            "review_inputs",
        ):
            assert service.store.jobs._all(db, tenant, run_id, kind) == []
    billed = service.cost(tenant, run_id)
    service.store.jobs.retry_run(
        tenant,
        run_id,
        expected_revision=service.store.jobs.get_run(tenant, run_id)["revision"],
        idempotency_key=str(uuid4()),
        reason="retry after recorded tag failure",
        now=ctx["now"][0],
        actor_sub="synthetic-test",
    )
    ctx["now"][0] += 1000
    reopened = LocalTagRunner(
        LocalSQLiteRunStore(service.store.path),
        service.uploads,
        runner.parser,
        telemetry=runner.telemetry,
        live_factory=runner.live_factory,
        clock=runner.clock,
    )
    assert reopened.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    assert len(ctx["calls"]) == before_calls + 6
    assert ctx["tag_probe"].summary()["calls"] == 6
    after = service.cost(tenant, run_id)
    assert after["attempt_count"] == billed["attempt_count"]
    assert after["amount"] == billed["amount"]
    claim = reopened.claims.list(tenant, run_id)[0]
    inputs = reopened.tags.load_inputs(tenant, run_id, claim.claim_id)
    assert all(receipt.recovered for receipt in inputs.tag_runs)
    assert inputs.decision is None


@pytest.mark.parametrize("inflight", [False, True])
def test_live_cancellation_stops_calls_and_leaves_no_review(tmp_path, monkeypatch, inflight):
    ctx = _pipeline_setup(tmp_path, monkeypatch)
    runner, service = ctx["tag_runner"], ctx["service"]
    tenant, run_id = ctx["tenant"], ctx["run_id"]
    before_calls = len(ctx["calls"])

    def cancel():
        service.store.jobs.cancel_run(
            tenant,
            run_id,
            expected_revision=service.store.jobs.get_run(tenant, run_id)["revision"],
            idempotency_key=str(uuid4()),
            reason="test cancellation",
            actor_sub="test",
            now=ctx["now"][0],
        )

    original = ctx["tag_probe"]._post

    def post(body):
        response = original(body)
        cancel()
        return response

    if inflight:
        monkeypatch.setattr(ctx["tag_probe"], "_post", post)
    else:
        cancel()
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == (
        "LEASE_LOST" if inflight else "cancelled"
    )
    assert len(ctx["calls"]) == before_calls + int(inflight)
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "cancelled"
    assert ctx["tag_probe"].summary()["calls"] == int(inflight)
    assert ctx["tag_probe"].summary()["unsettled_calls"] == 0
    with service.store.jobs._transaction() as db:
        assert service.store.jobs._all(db, tenant, run_id, "review_head") == []
        assert service.store.jobs._all(db, tenant, run_id, "decision_revision") == []
