"""Relation runtime with real fences/ledger and fake HTTP, never paid inference."""

import json
from dataclasses import asdict, replace
from uuid import UUID

import pytest
from proofops.application.evidence.retrieval import freeze_packet
from proofops.application.registry import artifact_sha256
from proofops.application.tagging.relations import SYSTEM_PROMPT
from proofops.domain.provenance import canonical_hash
from proofops_worker.live_tagging import LiveTaggingRuntime

from tests.integration.test_live_tagging_worker import configured as base_setup


def configured(tmp_path, monkeypatch):
    old, claim, graph, calls, profiles, usage = base_setup(tmp_path, monkeypatch)
    settings = replace(
        old.preliminary_settings,
        binding=replace(old.preliminary_settings.binding, binding_id=str(UUID(int=502))),
        model_profile="upstage-relation-source-quotes-v1",
        system_prompt=SYSTEM_PROMPT,
    )
    grant = dict(
        old.snapshot["preliminary_runtime"],
        runtime_binding_id=settings.binding.binding_id,
        tagging_settings_sha256=canonical_hash(asdict(settings)),
    )
    profiles["runtime", settings.binding.binding_id] = grant
    snapshot = dict(old.snapshot)
    snapshot.pop("input_hash")
    snapshot.update(
        relation_settings=asdict(settings),
        relation_settings_hash=canonical_hash(asdict(settings)),
        relation_runtime=grant,
        relation_runtime_artifact_hash=artifact_sha256(grant),
    )
    snapshot["input_hash"] = canonical_hash(snapshot)
    runtime = LiveTaggingRuntime(
        old.runner,
        snapshot,
        graph,
        old.lease,
        usage,
        probe=old.preliminary_transport._probe,
        ledger=old.ledger,
        receipts=old.receipts,
    )
    refs = [b.source_ref() for b in graph.blocks]
    packet = freeze_packet(
        dict(
            tenant_id=graph.tenant_id,
            run_id=snapshot["run_id"],
            claim_id=claim.claim_id,
            document_version_id=graph.document_version_id,
            parse_manifest_id=graph.parse_manifest_id,
            source_sha256=graph.source_sha256,
            graph_sha256=canonical_hash(asdict(graph)),
            evidence_candidates=[dict(source_refs=[asdict(ref)]) for ref in refs],
            status="candidate",
        )
    )

    def post(body):
        calls.append(body)
        data = json.loads(body["messages"][1]["content"])
        content = dict(
            relations=[
                dict(
                    source_index=s["source_index"],
                    dimensions=dict(entity=None, metric=None, reporting_period=None),
                )
                for s in data["untrusted_document_data"]["sources"]
            ]
        )
        return dict(
            id=f"relations-{len(calls)}",
            model=settings.model_id,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(content)))],
        )

    monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", post)
    return runtime, claim, graph, packet, calls, profiles, usage


def test_three_relation_replicas_are_recoverable_and_exclude_atomic_claim(tmp_path, monkeypatch):
    runtime, claim, graph, packet, calls, _, usage = configured(tmp_path, monkeypatch)
    result = runtime.relations(claim, packet)
    assert result is not None
    assert set(result) == {b.source_id for b in graph.blocks} - {
        r.source_id for r in claim.source_refs
    }
    assert len(calls) == usage["settled_calls"] == 3
    records = runtime.relation_records[claim.claim_id]
    assert len({r["request_id"] for r in records}) == 3
    assert runtime.relations(claim, packet) == result
    assert len(calls) == 3
    assert all(json.loads(c["messages"][1]["content"])["claim_id"] == claim.claim_id for c in calls)


@pytest.mark.parametrize("fault", ["revoked", "foreign_packet", "expired_policy"])
def test_relation_authorization_failures_never_spend(tmp_path, monkeypatch, fault):
    runtime, claim, _, packet, calls, profiles, usage = configured(tmp_path, monkeypatch)
    if fault == "revoked":
        key = "runtime", runtime.relation_settings.binding.binding_id
        profiles[key] = dict(profiles[key], status="revoked")
    elif fault == "foreign_packet":
        raw = packet.to_dict()
        raw.pop("packet_sha256")
        packet = freeze_packet(raw | {"claim_id": str(UUID(int=999))})
    else:
        runtime.runner.clock = lambda: 1790294400
    assert runtime.relations(claim, packet) is None
    assert calls == [] and usage.get("model_calls", 0) == 0


@pytest.mark.parametrize("fault", ["disagree", "duplicate_provider", "malformed"])
def test_relation_disagreement_or_malformed_response_is_not_binding(tmp_path, monkeypatch, fault):
    runtime, claim, _, packet, calls, _, usage = configured(tmp_path, monkeypatch)
    original = runtime.preliminary_transport._probe._post

    def post(body):
        result = original(body)
        if fault == "duplicate_provider":
            result["id"] = "same"
        if fault == "malformed":
            result["choices"][0]["message"]["content"] = '{"relations":[]}'
        if fault == "disagree" and len(calls) == 2:
            payload = json.loads(result["choices"][0]["message"]["content"])
            payload["relations"][0]["dimensions"]["facility"] = None
            result["choices"][0]["message"]["content"] = json.dumps(payload)
        return result

    monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", post)
    result = runtime.relations(claim, packet)
    if fault == "disagree":
        assert result is not None
        assert all(value is None for roles in result.values() for value in roles.values())
        assert any("facility" in roles for roles in result.values())
    else:
        assert result is None
    assert len(calls) == usage["settled_calls"] == (1 if fault == "malformed" else 3)


def test_incomplete_relation_receipt_is_not_retried(tmp_path, monkeypatch):
    runtime, claim, _, packet, calls, _, _ = configured(tmp_path, monkeypatch)
    assert runtime.relations(claim, packet) is not None
    rid = runtime.relation_records[claim.claim_id][0]["request_id"]
    (runtime.receipts / "relation" / rid / "response.json").unlink()
    assert runtime.relations(claim, packet) is None
    assert len(calls) == 3


def test_relation_stage_publishes_pinned_reviews_and_replays_without_calls(tmp_path, monkeypatch):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops_worker.tag_runner import LocalTagRunner

    from tests.integration.test_live_tagging_pipeline import _pipeline_setup

    ctx = _pipeline_setup(tmp_path, monkeypatch, relation_stage=True)
    runner, tenant, run_id = ctx["tag_runner"], ctx["tenant"], ctx["run_id"]
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    envelope = runner.tags.load_snapshot(tenant, run_id)
    assert envelope["relation_settings_hash"]
    assert len(envelope["claims"]) == 2
    for record in envelope["claims"]:
        assert len(record["relation_records"]) == 3
        assert all(r["status"] == "validated_candidate" for r in record["relation_records"])
        assert len(record["tag_runs"]) == 3
        assert record["decision"] is None
        own = {r["source_id"] for r in record["review_inputs"]["claim"]["source_refs"]}
        external = [sid for sid in record["review_inputs"]["relation_tags"] if ":" not in sid]
        assert external and not own.intersection(external)
    before = len(ctx["calls"])
    reopened = LocalTagRunner(
        LocalSQLiteRunStore(ctx["service"].store.path),
        ctx["service"].uploads,
        runner.parser,
        telemetry=runner.telemetry,
        live_factory=runner.live_factory,
        clock=runner.clock,
    )
    for record in envelope["claims"]:
        assert reopened.tags.load_inputs(tenant, run_id, record["claim_id"]).decision is None
    assert reopened.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    assert len(ctx["calls"]) == before
    assert ctx["tag_probe"].summary()["calls"] == 18


def test_relation_conflict_keeps_local_review_but_never_binds_external_source(
    tmp_path, monkeypatch
):
    from proofops.application.evidence.binding import accept_binding, relation_tags_for

    from tests.integration.test_live_tagging_pipeline import _pipeline_setup

    ctx = _pipeline_setup(tmp_path, monkeypatch, relation_stage=True)
    probe = ctx["tag_probe"]
    original = probe._post
    relation_calls = 0

    def post(body):
        nonlocal relation_calls
        response = original(body)
        if body["messages"][0]["content"].startswith(SYSTEM_PROMPT):
            relation_calls += 1
            if relation_calls % 3 == 2:
                payload = json.loads(response["choices"][0]["message"]["content"])
                for row in payload["relations"]:
                    row["dimensions"]["facility"] = None
                response["choices"][0]["message"]["content"] = json.dumps(payload)
        return response

    monkeypatch.setattr(probe, "_post", post)
    runner, tenant, run_id = ctx["tag_runner"], ctx["tenant"], ctx["run_id"]
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    records = runner.tags.load_snapshot(tenant, run_id)["claims"]
    assert len(records) == 2
    for record in records:
        assert "review_inputs" in record
        inputs = runner.tags.load_inputs(tenant, run_id, record["claim_id"])
        assert inputs.decision is None
        assert len(record["relation_records"]) == len(record["tag_runs"]) == 3
        for block in inputs.original.blocks:
            ref = block.source_ref()
            roles = relation_tags_for(ref, inputs.relation_tags)
            state = accept_binding(
                inputs.context,
                ref,
                roles,
                original=inputs.original,
                tenant_id=tenant,
                rulepack=inputs.rulepack,
                element_id="M1",
            )
            if ref.source_id in {r.source_id for r in inputs.context.claim.source_refs}:
                assert state == "accepted"
            else:
                assert roles is not None and roles["facility"] is None
                assert state == "undetermined"
    before = len(ctx["calls"])
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    assert len(ctx["calls"]) == before
    assert probe.summary()["calls"] == 18
