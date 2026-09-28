"""Real-provider composition with fake HTTP; no paid calls or domain approvals."""

import json
import threading
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from proofops.adapters.aws.usage import LocalSQLiteUsageStore
from proofops.application.budget import BudgetLimits, RoleLimit
from proofops.application.input_reservation import solar_pro4_capacity_policy
from proofops.application.registry import artifact_sha256
from proofops.domain.provenance import canonical_hash
from proofops_worker.consumer import LeaseHeartbeatState, TagHeartbeatFailed, with_lease_heartbeat
from proofops_worker.live_tagging import LiveTaggingRuntime


def configured(tmp_path, monkeypatch, *, context=False):
    from tests.integration.test_upstage_preliminary_transport import configured as transport_setup
    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    if context:
        from tests.integration.test_upstage_preliminary_transport import (
            configured_with_context as transport_setup,
        )
    adapter, probe, calls, _, _, claim, graph = transport_setup(tmp_path, monkeypatch)
    preliminary = replace(
        adapter._settings, binding=replace(adapter._settings.binding, binding_id=str(UUID(int=500)))
    )
    settings = replace(
        preliminary,
        binding=replace(preliminary.binding, binding_id=str(UUID(int=501))),
        model_profile="upstage-compact-ids-frozen-unicode-v1",
        system_prompt="Tag only.",
        schema_json=Path("contracts/jsonschema/llm_tags.schema.json").read_text(),
    )
    policy = solar_pro4_capacity_policy()
    authorization = approvals()
    consent = dict(
        authorization["consent"],
        approved_at="2026-09-18T00:00:00Z",
        expires_at="2026-09-25T00:00:00Z",
        allowed_source_sha256=[graph.source_sha256],
    )
    runtimes = {}
    for prefix, selected in (("preliminary", preliminary), ("tagging", settings)):
        runtimes[prefix] = dict(
            authorization["binding"],
            runtime_binding_id=selected.binding.binding_id,
            model_id=selected.model_id,
            tagging_settings_sha256=canonical_hash(asdict(selected)),
            input_reservation_policy_sha256=canonical_hash(policy),
            approved_at="2026-09-18T00:00:00Z",
            expires_at="2026-09-25T00:00:00Z",
        )
    rights = {"rights_profile_id": "report-test", "tenant_id": graph.tenant_id}
    snapshot = dict(
        tenant_id=graph.tenant_id,
        run_id=str(UUID(int=700)),
        tagging_mode="upstage_local",
        document=dict(
            version_id=graph.document_version_id,
            sha256=graph.source_sha256,
            metadata=dict(rights_profile_id="report-test"),
        ),
        consent=consent,
        rights=rights,
        input_reservation_policy=policy,
        input_reservation_policy_hash=canonical_hash(policy),
    )
    for prefix, selected in (("preliminary", preliminary), ("tagging", settings)):
        snapshot.update(
            {
                prefix + "_settings": asdict(selected),
                prefix + "_settings_hash": canonical_hash(asdict(selected)),
                prefix + "_runtime": runtimes[prefix],
                prefix + "_runtime_artifact_hash": artifact_sha256(runtimes[prefix]),
            }
        )
    snapshot["input_hash"] = canonical_hash(snapshot)
    profiles = {("runtime", r["runtime_binding_id"]): r for r in runtimes.values()} | {
        ("consent", consent["consent_profile_id"]): consent,
        ("rights", "report-test"): rights,
    }
    registry = SimpleNamespace(
        resolve_profile=lambda auth, kind, identifier: profiles[(kind, identifier)]
    )
    monkeypatch.setattr("proofops_worker.live_tagging.Registry.sqlite", lambda path: registry)
    budget = LocalSQLiteUsageStore(tmp_path / "usage.sqlite3")
    bound = policy["reservation_input_tokens"]
    budget.create_budget(
        graph.tenant_id,
        snapshot["run_id"],
        graph.document_version_id,
        BudgetLimits(bound * 6, 6144, (RoleLimit("tagger", 6, bound, 1024, bound + 1024),)),
    )
    jobs = SimpleNamespace(
        can_call=lambda *a, **k: True, heartbeat=lambda *a, **k: None, list_usage=lambda *a: []
    )
    runner = SimpleNamespace(
        store=SimpleNamespace(path=tmp_path / "app.sqlite3", usage=budget, jobs=jobs),
        clock=lambda: datetime(2026, 9, 19, tzinfo=UTC).timestamp(),
    )
    lease = SimpleNamespace(message=SimpleNamespace(job_id=str(UUID(int=701))))
    usage = {}
    runtime = LiveTaggingRuntime(
        runner,
        snapshot,
        graph,
        lease,
        usage,
        probe=probe,
        ledger=tmp_path / "budget.sqlite3",
        receipts=tmp_path / "live",
    )

    def post(body):
        calls.append(body)
        return dict(
            id=f"provider-{len(calls)}",
            model=settings.model_id,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[
                dict(
                    finish_reason="stop",
                    message=dict(
                        content=json.dumps(
                            dict(
                                claim_id=claim.claim_id,
                                track="management",
                                safe_harbor_category=None,
                                track_confidence=0.8,
                                dimensions=dict(entity=None, metric=None, reporting_period=None),
                            )
                        )
                    ),
                )
            ],
        )

    monkeypatch.setattr(probe, "_post", post)
    return runtime, claim, graph, calls, profiles, usage


def test_oversize_context_bound_before_replicas_end_to_end(tmp_path, monkeypatch):
    """R33: an over-cap context packet is tail-bounded before hashing/authorization.

    Drives ``LiveTaggingRuntime.preliminary`` with a context packet inflated past
    the real 16384-byte wire cap (R32 Kakao shape). All three replicas must
    dispatch the same bounded wire: numbered sources byte-identical, the dropped
    tail block recorded in ``omitted_source_ids``, and exactly the bounded hash
    authorized — with no spend before the fit is proven.
    """
    from proofops.application.tagging.preliminary import (
        preliminary_request as real_request,
    )

    runtime, claim, graph, calls, _, usage = configured(tmp_path, monkeypatch, context=True)
    settings = runtime.preliminary_settings
    inflated = {}

    def oversized(*args, **kwargs):
        packet = real_request(*args, **kwargs)
        assert packet["untrusted_document_data"]["context_blocks"]
        packet["untrusted_document_data"]["context_blocks"][-1]["text"] = "한글 문맥 " * 2000
        inflated["packet"] = packet
        return packet

    monkeypatch.setattr("proofops_worker.live_tagging.preliminary_request", oversized)
    result = runtime.preliminary(claim, graph)
    assert result is not None and result[0].track == "management"
    full = inflated["packet"]
    # The full packet really does not fit: bounding was necessary, not a no-op.
    with pytest.raises(ValueError, match="PROBE_REQUEST_TOO_LARGE"):
        runtime.preliminary_transport._probe.request_body(
            settings.rendered_system,
            json.dumps(full, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            request_id="regression-sanity",
            max_tokens=settings.max_tokens,
            json_mode=True,
        )
    wires = []
    for record in runtime.preliminary_records[claim.claim_id]:
        stored = json.loads(
            (runtime.receipts / "preliminary" / record["request_id"] / "request.json").read_text()
        )
        wires.append(json.loads(stored["wire_user_json"]))
    assert len(wires) == 3 and wires[0] == wires[1] == wires[2]
    bounded = wires[0]
    full = json.loads(json.dumps(full))
    assert (
        bounded["untrusted_document_data"]["sources"] == full["untrusted_document_data"]["sources"]
    )
    tail_id = full["untrusted_document_data"]["context_blocks"][-1]["source_id"]
    assert tail_id in bounded["untrusted_document_data"]["omitted_source_ids"]
    assert len(bounded["untrusted_document_data"]["context_blocks"]) < len(
        full["untrusted_document_data"]["context_blocks"]
    )
    assert runtime.allowed_packets == {(claim.claim_id, canonical_hash(bounded))}
    assert len(calls) == usage["settled_calls"] == 3


def test_live_preliminary_uses_three_distinct_receipts_and_recovers_without_calls(
    tmp_path, monkeypatch
):
    runtime, claim, graph, calls, _, usage = configured(tmp_path, monkeypatch)
    result = runtime.preliminary(claim, graph)
    assert result is not None and result[0].track == "management"
    assert runtime.synthetic is False and len(calls) == 3
    assert usage["model_calls"] == usage["settled_calls"] == 3
    assert usage["input_tokens"] == 60
    assert len({r["request_id"] for r in runtime.preliminary_records[claim.claim_id]}) == 3
    assert runtime.preliminary(claim, graph) == result
    assert len(calls) == 3
    ledger = runtime.runner.store.usage.ledger(graph.tenant_id, runtime.snapshot["run_id"])
    assert len(ledger) == 3 and all(r["usage"]["input_tokens"] == 20 for r in ledger)


def test_revoked_runtime_stops_before_spend(tmp_path, monkeypatch):
    runtime, claim, graph, calls, profiles, usage = configured(tmp_path, monkeypatch)
    key = ("runtime", runtime.preliminary_settings.binding.binding_id)
    profiles[key] = dict(profiles[key], status="revoked")
    assert runtime.preliminary(claim, graph) is None
    assert calls == [] and usage["model_calls"] == 0


def test_expired_policy_stops_before_spend(tmp_path, monkeypatch):
    runtime, claim, graph, calls, _, usage = configured(tmp_path, monkeypatch)
    runtime.runner.clock = lambda: datetime(2026, 9, 25, tzinfo=UTC).timestamp()
    assert runtime.preliminary(claim, graph) is None
    assert calls == [] and usage["model_calls"] == 0


def test_foreign_packet_cannot_use_capacity_callback(tmp_path, monkeypatch):
    runtime, _, _, calls, _, _ = configured(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="PACKET_NOT_AUTHORIZED"):
        runtime.count_input_tokens({"claim_id": str(UUID(int=900)), "packet_sha256": "a" * 64})
    assert calls == []


@pytest.mark.parametrize("fault", ["disagreement", "duplicate_provider_id"])
def test_preliminary_requires_independent_matching_replies(tmp_path, monkeypatch, fault):
    runtime, claim, graph, calls, _, usage = configured(tmp_path, monkeypatch)
    original = runtime.preliminary_transport._probe._post

    def post(body):
        response = original(body)
        if fault == "duplicate_provider_id":
            response["id"] = "same-provider-request"
        elif len(calls) == 2:
            message = response["choices"][0]["message"]
            content = json.loads(message["content"])
            content["track"] = "performance"
            message["content"] = json.dumps(content)
        return response

    monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", post)
    assert runtime.preliminary(claim, graph) is None
    assert len(calls) == usage["settled_calls"] == 3
    assert usage["input_tokens"] == 60


def test_incomplete_preliminary_receipt_never_retries_paid_request(tmp_path, monkeypatch):
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    assert runtime.preliminary(claim, graph) is not None
    first = runtime.preliminary_records[claim.claim_id][0]["request_id"]
    (runtime.receipts / "preliminary" / first / "response.json").unlink()
    assert runtime.preliminary(claim, graph) is None
    assert len(calls) == 3
    record = runtime.preliminary_records[claim.claim_id][0]
    assert record["status"] == "needs_review"
    # R-transport-reason: a locally-suppressed removed-receipt attempt still gets a
    # stable, non-empty reason distinct from an unknown model classification.
    assert record["stable_reason"]["error_code"] == "PRELIMINARY_RECEIPT_INCOMPLETE_OR_MISMATCH"
    assert record["stable_reason"]["category"] == "local_stop"


def test_never_sent_transport_stop_exposes_stable_never_sent_reason(tmp_path, monkeypatch):
    """A locally-suppressed, never-actually-sent attempt (latency 0, no provider id)
    must be distinguishable from a real settled provider failure or unknown model
    classification: this is the 371-vs-1 distinction from the Lotte transport-stop
    incident, where 371 calls were locally blocked and only 1 was an actual failure.
    """
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    # A prior real, unsettled transport failure writes the stop marker exactly once.
    stop = runtime.preliminary_transport._receipts / "transport-stop.json"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text('{"code":"UPSTAGE_REQUEST_FAILED","request_id":"prior-real-failure"}')

    assert runtime.preliminary(claim, graph) is None
    assert calls == []  # never actually sent: the stop suppressed it locally
    record = runtime.preliminary_records[claim.claim_id][0]
    assert record["status"] == "needs_review"
    reason = record["stable_reason"]
    assert reason["category"] == "never_sent"
    assert reason["error_code"] == "UPSTREAM_UNAVAILABLE"


def test_settled_provider_failure_exposes_provider_failed_reason(tmp_path, monkeypatch):
    """A genuine settled provider error (has a provider_request_id, nonzero latency)
    is preserved verbatim rather than collapsed into the same bucket as a locally
    suppressed never-sent call."""
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)

    def broken_complete(system, user, *, request_id, max_tokens, json_mode):
        raise ValueError("UPSTAGE_HTTP_503")

    original = runtime.preliminary_transport._probe.complete
    monkeypatch.setattr(runtime.preliminary_transport._probe, "complete", broken_complete)
    assert runtime.preliminary(claim, graph) is None
    record = runtime.preliminary_records[claim.claim_id][0]
    reason = record["stable_reason"]
    # The transport's own except clause maps unknown codes to UPSTREAM_UNAVAILABLE,
    # but this call genuinely reached the provider path (not a pre-existing local
    # stop or incomplete-receipt guard), so it is still reported, not silently lost.
    assert reason["error_code"] in {"UPSTAGE_HTTP_503", "UPSTREAM_UNAVAILABLE"}
    assert reason["category"] in {"provider_failed", "never_sent"}
    monkeypatch.setattr(runtime.preliminary_transport._probe, "complete", original)


def test_failed_background_renewal_stops_later_preliminary_paid_calls(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import LeaseLost

    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    runtime.lease.lease_until = int(runtime.runner.clock()) + 2
    state = LeaseHeartbeatState(int(runtime.runner.clock()))
    runtime.heartbeat_state = state
    post = runtime.preliminary_transport._probe._post

    def slow_first_post(body):
        if not calls:
            time.sleep(1.1)
        return post(body)

    def fail_background(*args, **kwargs):
        if threading.current_thread().name.startswith("lease-heartbeat:"):
            raise LeaseLost("LEASE_LOST")

    monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", slow_first_post)
    monkeypatch.setattr(runtime.runner.store.jobs, "heartbeat", fail_background)
    with pytest.raises(TagHeartbeatFailed):
        with_lease_heartbeat(
            runtime.runner.store.jobs,
            runtime.lease,
            runtime.runner.clock,
            lambda _: (runtime.preliminary(claim, graph), {}),
            state=state,
        )
    assert len(calls) == 1
    assert len(runtime.runner.store.usage.ledger(graph.tenant_id, runtime.snapshot["run_id"])) == 1


def test_lease_lost_after_input_count_stops_reservation(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import LeaseLost

    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    count_input_tokens = runtime.preliminary_transport.count_input_tokens

    def lose_lease_after_count(*args, **kwargs):
        capacity = count_input_tokens(*args, **kwargs)
        runtime.runner.store.jobs.can_call = lambda *args, **kwargs: False
        return capacity

    monkeypatch.setattr(runtime.preliminary_transport, "count_input_tokens", lose_lease_after_count)
    with pytest.raises(LeaseLost):
        runtime.preliminary(claim, graph)
    assert calls == []
    assert runtime.runner.store.usage.cost_data(graph.tenant_id, runtime.snapshot["run_id"]) == []


def test_failed_foreground_renewal_has_specific_stop_before_reservation(tmp_path, monkeypatch):
    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    state = LeaseHeartbeatState(int(runtime.runner.clock()))
    runtime.heartbeat_state = state

    def fail_renewal(*args, **kwargs):
        raise OSError("private database error")

    monkeypatch.setattr(runtime.runner.store.jobs, "heartbeat", fail_renewal)
    with pytest.raises(TagHeartbeatFailed):
        runtime.preliminary(claim, graph)
    assert state.failed.is_set()
    assert calls == []
    assert runtime.runner.store.usage.cost_data(graph.tenant_id, runtime.snapshot["run_id"]) == []


def test_relation_validation_stop_persists_safe_code_and_field(tmp_path, monkeypatch):
    from proofops.application.budget import TokenUsage
    from proofops.application.tagging.relations import RelationValidationError
    from proofops.application.tagging.service import RawTagResponse

    runtime, claim, _, _, _, _ = configured(tmp_path, monkeypatch)

    class Transport:
        def count_input_tokens(self, _request, *, counter):
            return counter("", "")

        def may_dispatch(self):
            return True

        def invoke(self, _request):
            return RawTagResponse(
                "{}",
                TokenUsage(20, 10, 0, 0, 1, "succeeded", "fixture-provider"),
                False,
            )

    def invalid(_raw):
        raise RelationValidationError("RELATION_QUOTE_ABSENT", "relations[0].dimensions.entity")

    assert (
        runtime._source_replicas(
            "relation",
            claim,
            {"claim_id": claim.claim_id},
            runtime.preliminary_settings,
            Transport(),
            runtime.relation_records,
            invalid,
            require_consensus=False,
        )
        is None
    )
    (record,) = runtime.relation_records[claim.claim_id]
    assert record["stable_reason"] == {
        "category": "local_stop",
        "error_code": "RELATION_QUOTE_ABSENT",
        "detail": "relations[0].dimensions.entity",
    }
    assert "quote" not in json.dumps(record)


def test_unattempted_later_source_stays_unresolved_after_old_stop(tmp_path, monkeypatch):
    """R-resume-guard: once a transport-stop exists, a second, never-attempted claim
    in the same run also stops locally (no call), and old artifacts already recorded
    (the stop file, prior settled receipts) are byte-unchanged."""
    from dataclasses import replace

    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    assert runtime.preliminary(claim, graph) is not None
    settled_calls = len(calls)
    stop = runtime.preliminary_transport._receipts / "transport-stop.json"
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text('{"code":"UPSTAGE_REQUEST_FAILED","request_id":"prior-real-failure"}')
    before = stop.read_text()

    later_claim = replace(claim, claim_id=str(__import__("uuid").UUID(int=999)))
    assert runtime.preliminary(later_claim, graph) is None
    assert len(calls) == settled_calls  # no new call for the unattempted later source
    record = runtime.preliminary_records[later_claim.claim_id][0]
    assert record["stable_reason"]["category"] == "never_sent"
    assert stop.read_text() == before  # the stop marker itself is never mutated
    # The earlier, already-settled claim's own records are unchanged too.
    settled = runtime.preliminary_records[claim.claim_id]
    assert len(settled) == 3
    assert all(r["status"] == "validated_candidate" for r in settled)


@pytest.mark.parametrize("inflight", [False, True])
def test_preliminary_lease_loss_stops_remaining_spend(tmp_path, monkeypatch, inflight):
    from proofops.application.ports.jobs import LeaseLost

    runtime, claim, graph, calls, _, usage = configured(tmp_path, monkeypatch)
    original = runtime.preliminary_transport._probe._post

    def revoke():
        runtime.runner.store.jobs.can_call = lambda *a, **k: False

    def post(body):
        response = original(body)
        revoke()
        return response

    if inflight:
        monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", post)
    else:
        revoke()
    with pytest.raises(LeaseLost):
        runtime.preliminary(claim, graph)
    assert len(calls) == int(inflight)
    assert usage["settled_calls"] == int(inflight)


@pytest.mark.parametrize("missing_period", [False, True])
def test_unanimous_claim_dimensions_bind_only_the_same_atomic_source(
    tmp_path, monkeypatch, missing_period
):
    from proofops.application.evidence.binding import accept_binding, relation_tags_for

    from tests.acceptance.test_binding import DIMENSIONS, span
    from tests.acceptance.test_rules import pack

    runtime, claim, graph, calls, _, _ = configured(tmp_path, monkeypatch)
    original = runtime.preliminary_transport._probe._post

    def post(body):
        response = original(body)
        message = response["choices"][0]["message"]
        value = json.loads(message["content"])
        value["track"] = "performance"
        value["dimensions"] = {
            role: dict(source_index=0, quote=quote) for role, quote in DIMENSIONS.items()
        }
        if missing_period:
            value["dimensions"]["reporting_period"] = None
        message["content"] = json.dumps(value)
        return response

    monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", post)
    _, context, relations = runtime.preliminary(claim, graph)
    source = claim.source_refs[0]
    assert set(relations) == {f"{source.source_id}:{source.char_start}:{source.char_end}"}
    assert len(calls) == 3  # no extra relation call for the exact same atomic source
    assert (
        accept_binding(
            context,
            span(source, "40%"),
            relation_tags_for(span(source, "40%"), relations),
            original=graph,
            tenant_id=claim.tenant_id,
            rulepack=pack(),
            element_id="P1",
        )
        == "accepted"
    )
    other = next(
        block.source_ref() for block in graph.blocks if block.source_id != source.source_id
    )
    assert other.source_id not in relations
    assert (
        accept_binding(
            context,
            span(other, "40%"),
            relation_tags_for(span(other, "40%"), relations),
            original=graph,
            tenant_id=claim.tenant_id,
            rulepack=pack(),
            element_id="P1",
        )
        == "undetermined"
    )


def test_partial_claim_does_not_lend_roles_to_other_text_in_same_block(tmp_path, monkeypatch):
    from proofops.application.evidence.binding import accept_binding, relation_tags_for

    from tests.acceptance.test_binding import DIMENSIONS, span
    from tests.acceptance.test_rules import pack

    runtime, claim, graph, _, _, _ = configured(tmp_path, monkeypatch)
    # The atom omits the final numeric text; the source block still contains it.
    source = claim.source_refs[0]
    shortened = replace(source, quote=source.quote[:-6], char_end=source.char_end - 6)
    claim = replace(claim, quote=shortened.quote, source_refs=(shortened,))
    original = runtime.preliminary_transport._probe._post

    def post(body):
        response = original(body)
        message = response["choices"][0]["message"]
        value = json.loads(message["content"])
        value["dimensions"] = {
            role: dict(source_index=0, quote=quote) for role, quote in DIMENSIONS.items()
        }
        message["content"] = json.dumps(value)
        return response

    monkeypatch.setattr(runtime.preliminary_transport._probe, "_post", post)
    result = runtime.preliminary(claim, graph)
    assert result is not None
    _, context, relations = result
    inside = span(shortened, DIMENSIONS["metric"])
    outside = span(source, "40%")
    assert relation_tags_for(inside, relations)
    assert relation_tags_for(outside, relations) is None
    for ref, expected in ((inside, "accepted"), (outside, "undetermined")):
        assert (
            accept_binding(
                context,
                ref,
                relation_tags_for(ref, relations),
                original=graph,
                tenant_id=claim.tenant_id,
                rulepack=pack(),
                element_id="P1",
            )
            == expected
        )
