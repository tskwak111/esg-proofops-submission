"""AT-010: synthetic corpus/transport/prices; real cache, budget and source guards."""

import json
from dataclasses import asdict, replace
from pathlib import Path
from uuid import UUID

import pytest
from proofops.adapters.aws.usage import LocalSQLiteUsageStore
from proofops.adapters.cache.aws import ImmutableResponseCache, InMemoryImmutableCacheClient
from proofops.application.budget import BudgetLimits, RoleLimit, TokenUsage, cost_summary
from proofops.application.evidence.retrieval import (
    freeze_packet,
    freeze_track_packet,
    retrieve_evidence,
)
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.tracks import TrackCandidate
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_binding import corpus, span, tags
from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_retrieval import SyntheticSearch
from tests.acceptance.test_rules import pack

ENSEMBLE = str(UUID(int=100))
BINDING = ModelBinding("synthetic-tagging-v1", "tagger", True)


class SyntheticResponder:
    """Test-only callable with distinct invocations; no product/model result implied."""

    def __init__(self, source, changes=None, fail=()):
        self.source = source
        self.changes = changes or {}
        self.fail = fail
        self.requests = []

    def __call__(self, request):
        from proofops.application.tagging.service import RawTagResponse

        self.requests.append(json.loads(json.dumps(request)))
        replica = request["replicate_id"]
        if replica in self.fail:
            raise RuntimeError("private provider error must not leak")
        element = dict(
            element_id="P1",
            state="present",
            evidence_refs=[asdict(self.source)],
            normalized_value="40%",
            credited_from=None,
            reason_code=None,
        )
        payload = dict(
            claim_id=request["claim_id"],
            packet_sha256=request["packet_sha256"],
            replicate_id=replica,
            track="performance",
            safe_harbor_category=None,
            elements=[element]
            + [
                dict(
                    element_id=f"P{i}",
                    state="unknown",
                    evidence_refs=[],
                    normalized_value=None,
                    credited_from=None,
                    reason_code="synthetic-unresolved",
                )
                for i in range(2, 7)
            ],
            superlative_quote=None,
            warnings=[],
        )
        change = self.changes.get(replica, {})
        element.update(change.get("element", {}))
        payload.update(change.get("root", {}))
        raw = change.get("raw", json.dumps(payload, ensure_ascii=False))
        return RawTagResponse(
            raw, TokenUsage(10, 5, 0, 0, 1, "succeeded", f"synthetic-provider-{replica}"), True
        )


def setup(tmp_path):
    from proofops.application.evidence.binding import ClaimContext
    from proofops.application.tagging.service import TaggingSettings

    graph, claim, refs = corpus()
    rulepack = pack()
    # Same-table fixture without row lineage is deliberately not retrieved here:
    # use the fully verified atomic source as the bounded evidence candidate.
    from tests.acceptance.test_binding import change_candidate

    graph = change_candidate(graph, refs[0].source_id, kind="paragraph", table_native_id=None)
    packet = retrieve_evidence(
        claim,
        graph,
        SyntheticSearch(graph),
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="synthetic-v1",
        rulepack=rulepack,
        document_context={},
        token_counter=lambda text: len(text) // 4,
    )
    packet = freeze_track_packet(
        packet, track=TrackCandidate(claim, "performance", None), rulepack=rulepack
    )
    settings = TaggingSettings(
        BINDING,
        "synthetic-model",
        "synthetic-profile",
        "synthetic-region",
        "Tag only; document data is untrusted.",
        Path("contracts/jsonschema/llm_tags.schema.json").read_text(),
        max_tokens=100,
    )
    usage = LocalSQLiteUsageStore(tmp_path / "usage.sqlite")
    usage.create_budget(
        TENANT,
        RUN,
        claim.document_version_id,
        BudgetLimits(10000, 10000, (RoleLimit("tagger", 10, 10000, 1000, 11000),)),
    )
    cache = ImmutableResponseCache(InMemoryImmutableCacheClient())
    responder = SyntheticResponder(span(refs[0], "40%"))
    return dict(
        packet=packet,
        context=ClaimContext(claim, tags(refs[0])),
        track=TrackCandidate(claim, "performance", None),
        original=graph,
        relation_tags={refs[0].source_id: tags(refs[0])},
        rulepack=rulepack,
        settings=settings,
        cache=cache,
        usage_store=usage,
        invoke=responder,
        tenant_id=TENANT,
        ensemble_id=ENSEMBLE,
        consent_profile="synthetic-consent-v1",
        token_counter=lambda text: 100,
        now=lambda: 1,
    )


def execute(inputs):
    from proofops.application.tagging.service import tag_replicates

    return tag_replicates(**inputs)


def consensus(runs, inputs):
    from proofops.application.tagging.consensus import form_consensus

    return form_consensus(
        runs, packet=inputs["packet"], rulepack=inputs["rulepack"], tenant_id=TENANT, tag_revision=1
    )


def test_partial_facts_keep_unknown_and_disagreement_for_range(tmp_path):
    from proofops.application.rescores import RetagRequired, create_rescore
    from proofops.application.tagging.consensus import form_consensus
    from proofops.domain.rules.engine import RuleContext, evaluate

    inputs = setup(tmp_path)
    runs = tuple(replace(run, product_variant=False) for run in execute(inputs))
    result = form_consensus(
        runs,
        packet=inputs["packet"],
        rulepack=inputs["rulepack"],
        tenant_id=TENANT,
        tag_revision=1,
        profile="partial-facts-v1",
    )
    assert result.review_status == "needs_review"
    assert result.confirmed_tags is not None
    facts = {fact.name: fact for fact in result.confirmed_tags.facts}
    assert facts["quantitative_or_qualified_ordinal"].state == "present"
    assert facts["comparison_baseline"].state == "unknown"
    decision = evaluate(
        result.confirmed_tags,
        RuleContext(
            TENANT,
            inputs["context"].claim.document_version_id,
            inputs["context"].claim.claim_id,
            inputs["packet"].packet_sha256,
            local_synthetic=True,
        ),
        inputs["rulepack"],
    )
    assert decision.review_status == "needs_review"
    assert decision.evidence_grade is None
    assert (decision.grade_floor, decision.grade_ceiling) == ("E1", "E3")
    assert decision.grade_open_elements == ("P2", "P3", "P4")
    rescored = create_rescore(
        inputs["rulepack"],
        result.confirmed_tags,
        previous_pack=inputs["rulepack"],
        context=RuleContext(
            TENANT,
            inputs["context"].claim.document_version_id,
            inputs["context"].claim.claim_id,
            inputs["packet"].packet_sha256,
            local_synthetic=True,
        ),
    )
    assert not isinstance(rescored, RetagRequired)
    assert rescored.decision_status == decision.decision_status

    (tmp_path / "disagreement").mkdir()
    changed = setup(tmp_path / "disagreement")
    changed["invoke"].changes[3] = {
        "element": {"state": "unknown", "evidence_refs": [], "normalized_value": None}
    }
    disputed = form_consensus(
        execute(changed),
        packet=changed["packet"],
        rulepack=changed["rulepack"],
        tenant_id=TENANT,
        tag_revision=1,
        profile="partial-facts-v1",
    )
    assert {fact.name: fact.state for fact in disputed.confirmed_tags.facts}[
        "quantitative_or_qualified_ordinal"
    ] == "conflict"
    assert all(f.state != "absent" for f in disputed.confirmed_tags.facts)
    assert consensus(runs, inputs).confirmed_tags is None

    (tmp_path / "failed").mkdir()
    failed = setup(tmp_path / "failed")
    failed["invoke"].fail = (3,)
    projected = form_consensus(
        execute(failed),
        packet=failed["packet"],
        rulepack=failed["rulepack"],
        tenant_id=TENANT,
        tag_revision=1,
        profile="partial-facts-v1",
    )
    assert {fact.name: fact.state for fact in projected.confirmed_tags.facts}[
        "quantitative_or_qualified_ordinal"
    ] == "unknown"

    (tmp_path / "mixed-failure").mkdir()
    mixed = setup(tmp_path / "mixed-failure")
    mixed["invoke"].changes[2] = {
        "element": {"state": "unknown", "evidence_refs": [], "normalized_value": None}
    }
    mixed["invoke"].fail = (3,)
    mixed_result = form_consensus(
        execute(mixed),
        packet=mixed["packet"],
        rulepack=mixed["rulepack"],
        tenant_id=TENANT,
        tag_revision=1,
        profile="partial-facts-v1",
    )
    assert {fact.name: fact.state for fact in mixed_result.confirmed_tags.facts}[
        "quantitative_or_qualified_ordinal"
    ] == "conflict"


def test_all_present_partial_facts_still_require_review(tmp_path):
    from proofops.application.tagging.consensus import form_consensus
    from proofops_worker.tag_runner import _consensus_review_reason

    inputs = setup(tmp_path)
    runs = execute(inputs)
    # Synthetic post-guard votes exercise the all-present routing boundary.
    source = runs[0].guarded.elements[0].evidence_refs
    complete = tuple(
        replace(
            run,
            guarded=replace(
                run.guarded,
                elements=tuple(
                    replace(element, state="present", evidence_refs=source, normalized_value="40%")
                    for element in run.guarded.elements
                ),
            ),
        )
        for run in runs
    )
    result = form_consensus(
        complete,
        packet=inputs["packet"],
        rulepack=inputs["rulepack"],
        tenant_id=TENANT,
        tag_revision=1,
        profile="partial-facts-v1",
    )
    assert result.review_status == "needs_review"
    assert result.confirmed_tags is None
    assert _consensus_review_reason(result) == "PARTIAL_FACTS_REVIEW_REQUIRED"


def test_actual_token_overrun_stops_remaining_replicas_and_retains_raw_recovery(tmp_path):
    inputs = setup(tmp_path)
    responder = inputs["invoke"]

    def overrun(request):
        response = responder(request)
        return replace(response, usage=replace(response.usage, input_tokens=101))

    inputs["invoke"] = overrun
    runs = execute(inputs)
    assert len(responder.requests) == 1
    assert [run.status for run in runs[1:]] == ["budget_exhausted", "budget_exhausted"]
    assert runs[0].usage.input_tokens == 101 and runs[0].raw_response_json
    assert consensus(runs, inputs).confirmed_tags is None
    recovered = execute(inputs)
    assert len(responder.requests) == 1
    assert recovered[0].recovered
    assert recovered[0].raw_response_json == runs[0].raw_response_json
    assert [run.status for run in recovered[1:]] == ["budget_exhausted", "budget_exhausted"]


def test_three_distinct_calls_same_frozen_packet_and_raw_recovery_only(tmp_path):
    inputs = setup(tmp_path)
    before = inputs["packet"].payload_json
    runs = execute(inputs)
    assert [r.replicate_id for r in runs] == [1, 2, 3]
    assert len({r.request.request_signature for r in runs}) == 3
    assert len({r.request.request_id for r in runs}) == 3
    assert len({r.packet_sha256 for r in runs}) == 1
    assert len(inputs["invoke"].requests) == 3
    user_packets = [
        json.loads(request["user_json"])["untrusted_document_data"]
        for request in inputs["invoke"].requests
    ]
    assert user_packets[0] == user_packets[1] == user_packets[2]
    assert all(r.status == "needs_review" and r.guarded is not None for r in runs)
    assert all(r.raw_response_json and r.synthetic for r in runs)
    assert inputs["packet"].payload_json == before
    recovered = execute(inputs)
    assert len(inputs["invoke"].requests) == 3
    assert [r.raw_response_json for r in recovered] == [r.raw_response_json for r in runs]
    assert all(r.recovered for r in recovered)
    assert cost_summary(inputs["usage_store"], TENANT, RUN)["attempt_count"] == 3


def test_cross_replica_raw_payload_cannot_count_as_independent_vote(tmp_path):
    inputs = setup(tmp_path)
    runs = execute(inputs)
    cache = ImmutableResponseCache(InMemoryImmutableCacheClient())
    raw = inputs["cache"].get_raw(runs[0].request, recovery_request_id=runs[0].request.request_id)
    cache.put_raw(runs[1].request, raw)
    inputs["cache"] = cache
    retried = execute(inputs)
    assert retried[1].status == "invalid_cache"
    assert consensus(retried, inputs).review_status == "needs_review"
    assert len(inputs["invoke"].requests) == 3  # Ledger also blocks duplicate dispatch.


def test_new_ensemble_does_not_reuse_old_raw_responses(tmp_path):
    inputs = setup(tmp_path)
    first = execute(inputs)
    inputs["ensemble_id"] = str(UUID(int=101))
    second = execute(inputs)
    assert len(inputs["invoke"].requests) == 6
    assert all(not r.recovered for r in second)
    assert {r.request.request_id for r in first}.isdisjoint(r.request.request_id for r in second)


def test_unanimous_verified_p1_cannot_confirm_an_incomplete_performance_track(tmp_path):
    inputs = setup(tmp_path)
    runs = execute(inputs)
    result = consensus(runs, inputs)
    assert result.review_status == "needs_review" and result.confirmed_tags is None
    assert result.candidate_elements[0].state == "present"
    assert result.candidate_elements[0].normalized_value == "40%"
    assert all(
        ref.verification_state == "verified" for ref in result.candidate_elements[0].evidence_refs
    )
    assert all(dict(run.binding_hashes).get("P1") for run in runs)
    assert [e.state for e in result.candidate_elements] == ["present"] + ["unknown"] * 5
    assert {"REVIEW:P4", "REVIEW:P6"} <= set(result.reasons)
    assert len(result.replicate_hashes) == 3
    assert {run.packet_sha256 for run in runs} == {inputs["packet"].packet_sha256}


@pytest.mark.parametrize(
    "change",
    [
        {"root": {"replicate_id": 1}},
        {"root": {"packet_sha256": "0" * 64}},
        {"root": {"label": "SUBSTANTIATED"}},
        {"raw": "{truncated"},
        {"element": {"evidence_refs": []}},
        {"element": {"element_id": "P9"}},
    ],
)
def test_invalid_replica_retains_raw_and_requires_review(tmp_path, change):
    inputs = setup(tmp_path)
    inputs["invoke"].changes = {2: change}
    runs = execute(inputs)
    assert runs[1].raw_response_json is not None
    assert runs[1].status != "succeeded"
    assert consensus(runs, inputs).review_status == "needs_review"
    assert consensus(runs, inputs).confirmed_tags is None


@pytest.mark.parametrize("state", ["absent", "not_applicable", "unknown", "conflict"])
def test_unproven_absence_and_applicability_never_become_confirmed_absence(tmp_path, state):
    inputs = setup(tmp_path)
    inputs["invoke"].changes = {
        i: {"element": {"state": state, "evidence_refs": [], "normalized_value": None}}
        for i in (1, 2, 3)
    }
    runs = execute(inputs)
    assert all(r.guarded.elements[0].state in ("unknown", "conflict") for r in runs)
    assert consensus(runs, inputs).review_status == "needs_review"


def test_two_to_one_critical_value_keeps_majority_candidate_and_requires_review(tmp_path):
    inputs = setup(tmp_path)
    inputs["invoke"].changes = {3: {"element": {"normalized_value": "0.4"}}}
    result = consensus(execute(inputs), inputs)
    assert result.review_status == "needs_review" and result.confirmed_tags is None
    assert result.candidate_elements[0].normalized_value == "40%"
    assert result.agreement == (("P1", 2),) + tuple((f"P{i}", 3) for i in range(2, 7))


def test_one_provider_failure_is_retained_sanitized_and_never_majority_auto_confirmed(tmp_path):
    inputs = setup(tmp_path)
    inputs["invoke"].fail = (2,)
    runs = execute(inputs)
    assert runs[1].status == "model_failed"
    assert "private" not in repr(runs[1])
    assert consensus(runs, inputs).review_status == "needs_review"
    assert cost_summary(inputs["usage_store"], TENANT, RUN)["cost_status"] == "unknown_cost"


def test_forged_citation_and_matching_words_from_other_product_fail_guard(tmp_path):
    inputs = setup(tmp_path)
    bad = asdict(inputs["invoke"].source) | {"quote": "41%", "verification_state": "verified"}
    inputs["invoke"].changes = {2: {"element": {"evidence_refs": [bad]}}}
    runs = execute(inputs)
    assert runs[1].guarded.elements[0].state == "unknown"
    assert consensus(runs, inputs).review_status == "needs_review"


@pytest.mark.parametrize("field", ["tenant_id", "graph_sha256", "rule_sha256", "atomic_quote"])
def test_packet_identity_is_checked_before_any_provider_or_cache_use(tmp_path, field):
    inputs = setup(tmp_path)
    data = inputs["packet"].to_dict()
    data.pop("packet_sha256")
    data[field] = str(UUID(int=99)) if field == "tenant_id" else "wrong"
    inputs["packet"] = freeze_packet(data)
    with pytest.raises(DomainValidationError):
        execute(inputs)
    assert inputs["invoke"].requests == []


def test_duplicate_replica_or_mixed_packet_cannot_form_consensus(tmp_path):
    inputs = setup(tmp_path)
    runs = execute(inputs)
    with pytest.raises(DomainValidationError):
        consensus((runs[0], runs[0], runs[2]), inputs)
    with pytest.raises(DomainValidationError):
        consensus((runs[0], replace(runs[1], packet_sha256="0" * 64), runs[2]), inputs)


def test_unanimous_invented_normalized_number_is_not_confirmed(tmp_path):
    inputs = setup(tmp_path)
    inputs["invoke"].changes = {i: {"element": {"normalized_value": "41%"}} for i in (1, 2, 3)}
    result = consensus(execute(inputs), inputs)
    assert result.review_status == "needs_review" and result.confirmed_tags is None


def test_budget_stops_provider_dispatch_and_preserves_partial_replicas(tmp_path):
    inputs = setup(tmp_path)
    usage = LocalSQLiteUsageStore(tmp_path / "limited.sqlite")
    usage.create_budget(
        TENANT,
        RUN,
        inputs["context"].claim.document_version_id,
        BudgetLimits(100, 100, (RoleLimit("tagger", 1, 100, 100, 200),)),
    )
    inputs["usage_store"] = usage
    runs = execute(inputs)
    assert [r.status for r in runs] == ["needs_review", "budget_exhausted", "budget_exhausted"]
    assert len(inputs["invoke"].requests) == 1
    assert consensus(runs, inputs).confirmed_tags is None


def test_concurrent_same_ensemble_cannot_double_dispatch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    inputs = setup(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(lambda _: execute(inputs), range(2)))
    assert len(inputs["invoke"].requests) == 3
    assert {
        r.replicate_id
        for batch in results
        for r in batch
        if r.status == "needs_review" and r.guarded is not None
    } == {
        1,
        2,
        3,
    }
    assert all(r.status == "needs_review" and r.guarded is not None for r in execute(inputs))


def test_bedrock_agent_uses_guarded_transport_and_retains_truncated_raw(tmp_path):
    from proofops.adapters.aws.bedrock import BedrockInvoker
    from proofops_agent.tagger import BedrockMessagesTagger

    from tests.acceptance.test_preflight import (
        AUTH,
        NOW,
        REGIONS,
        SyntheticBedrockClient,
        binding,
        consent,
    )

    inputs = setup(tmp_path)
    native_responder = inputs["invoke"]

    class SyntheticMessagesClient(SyntheticBedrockClient):
        def invoke_model(self, **kwargs):
            self.calls.append(kwargs)
            body = json.loads(kwargs["body"])
            user = json.loads(body["messages"][0]["content"][0]["text"])
            reply = native_responder(user)
            return {
                "body": json.dumps(
                    dict(
                        id=f"synthetic-{user['replicate_id']}",
                        content=[dict(type="text", text=reply.raw_response_json)],
                        stop_reason="max_tokens" if user["replicate_id"] == 2 else "end_turn",
                        usage=dict(input_tokens=10, output_tokens=5),
                    )
                ).encode()
            }

    runtime = binding()
    descriptor = ModelBinding(runtime["runtime_binding_id"], "tagger", True)
    inputs["settings"] = replace(
        inputs["settings"],
        binding=descriptor,
        model_id=runtime["model_id"],
        region=runtime["endpoint_region"],
    )
    client = SyntheticMessagesClient()
    adapter = BedrockMessagesTagger(
        BedrockInvoker(client, account_id=runtime["account_id"]),
        auth=AUTH,
        runtime_binding=runtime,
        consent=consent(),
        allowed_regions=REGIONS,
        document_rights="synthetic-public",
        checked_at=NOW,
        synthetic=True,
    )
    inputs["invoke"] = adapter.invoke
    runs = execute(inputs)
    assert [r.status for r in runs] == ["needs_review", "model_failed", "needs_review"]
    assert len(client.calls) == 3
    assert runs[1].raw_response_json and runs[1].provider_response_json
    assert consensus(runs, inputs).review_status == "needs_review"
    for call in client.calls:
        body = json.loads(call["body"])
        assert body["anthropic_version"] == "bedrock-2023-05-31"
        assert "tools" not in body
        assert "untrusted_document_data" in body["messages"][0]["content"][0]["text"]
        assert call["modelId"] == runtime["model_id"]


def test_agent_blocks_unapproved_binding_before_transport(tmp_path):
    from proofops.adapters.aws.bedrock import BedrockInvoker
    from proofops_agent.tagger import BedrockMessagesTagger

    from tests.acceptance.test_preflight import (
        AUTH,
        NOW,
        REGIONS,
        SyntheticBedrockClient,
        binding,
        consent,
    )

    runtime = binding()
    runtime["permissions_verified"] = False
    client = SyntheticBedrockClient()
    adapter = BedrockMessagesTagger(
        BedrockInvoker(client, account_id=runtime["account_id"]),
        auth=AUTH,
        runtime_binding=runtime,
        consent=consent(),
        allowed_regions=REGIONS,
        document_rights="synthetic-public",
        checked_at=NOW,
        synthetic=True,
    )
    with pytest.raises(ValueError):
        adapter.invoke(
            dict(
                tenant_id=TENANT,
                binding=dict(
                    binding_id=runtime["runtime_binding_id"], role="tagger", synthetic=True
                ),
                model_id=runtime["model_id"],
                region=runtime["endpoint_region"],
                max_tokens=100,
                temperature=0,
                system_prompt="tags only",
                user_json="{}",
            )
        )
    assert client.calls == []


def test_missing_normalized_numeric_value_cannot_hide_disagreement(tmp_path):
    inputs = setup(tmp_path)
    inputs["invoke"].changes = {i: {"element": {"normalized_value": None}} for i in (1, 2, 3)}
    assert consensus(execute(inputs), inputs).confirmed_tags is None


def test_provider_response_replay_with_rewritten_replica_requires_review(tmp_path):
    inputs = setup(tmp_path)
    runs = execute(inputs)
    replayed = tuple(
        replace(r, usage=replace(r.usage, provider_request_id="same-provider-response"))
        for r in runs
    )
    assert consensus(replayed, inputs).review_status == "needs_review"


def test_forged_document_context_cannot_send_gold_to_model(tmp_path):
    inputs = setup(tmp_path)
    data = inputs["packet"].to_dict()
    data.pop("packet_sha256")
    data["document_context"]["gold_label"] = "SUBSTANTIATED"
    inputs["packet"] = freeze_packet(data)
    with pytest.raises(DomainValidationError):
        execute(inputs)
    assert inputs["invoke"].requests == []


def test_open_parse_conflict_blocks_even_a_forged_verified_quality(tmp_path):
    from proofops.application.ingest.graph_fusion import QualityIssue

    inputs = setup(tmp_path)
    source = inputs["invoke"].source
    inputs["original"] = replace(
        inputs["original"],
        issues=(
            QualityIssue(
                str(UUID(int=102)),
                "parse_conflict",
                1,
                (source.source_id,),
                "open",
                "synthetic conflict",
            ),
        ),
    )
    data = inputs["packet"].to_dict()
    data.pop("packet_sha256")
    data["graph_sha256"] = canonical_hash(asdict(inputs["original"]))
    inputs["packet"] = freeze_packet(data)
    with pytest.raises(DomainValidationError):
        execute(inputs)
    assert inputs["invoke"].requests == []


def test_same_fact_different_verified_citations_union_without_false_disagreement(tmp_path):
    inputs = setup(tmp_path)
    graph, claim, refs = corpus()
    data = inputs["packet"].to_dict()
    data.pop("packet_sha256")
    data["graph_sha256"] = canonical_hash(asdict(graph))
    data["evidence_candidates"].append(
        dict(
            source_id=refs[1].source_id,
            source_scope="same_table",
            allowed_elements=["P1"],
            source_refs=[asdict(refs[1])],
        )
    )
    inputs["packet"] = freeze_packet(data)
    inputs["original"] = graph
    inputs["relation_tags"][refs[1].source_id] = tags(refs[1])
    inputs["invoke"].changes = {3: {"element": {"evidence_refs": [asdict(span(refs[1], "40%"))]}}}
    result = consensus(execute(inputs), inputs)
    assert result.review_status == "needs_review" and result.confirmed_tags is None
    assert result.agreement == tuple((f"P{i}", 3) for i in range(1, 7))
    assert result.candidate_elements[0].state == "present"
    assert len(result.candidate_elements[0].evidence_refs) == 2
    assert "REVIEW:P1" not in result.reasons


def test_review_runs_preserve_explicit_product_attribution(tmp_path):
    inputs = setup(tmp_path)
    runs = execute(inputs)
    assert all(run.product_variant is True for run in runs)
    assert consensus(runs, inputs).confirmed_tags is None


def test_relabelled_claim_version_cannot_reuse_original_graph(tmp_path):
    from proofops.application.evidence.binding import ClaimContext

    inputs = setup(tmp_path)
    claim = replace(inputs["context"].claim, document_version_id=str(UUID(int=500)))
    inputs["context"] = ClaimContext(claim, inputs["context"].dimensions)
    inputs["track"] = replace(inputs["track"], claim=claim)
    data = inputs["packet"].to_dict()
    data.pop("packet_sha256")
    data["document_version_id"] = claim.document_version_id
    inputs["packet"] = freeze_packet(data)
    with pytest.raises(DomainValidationError):
        execute(inputs)
    assert inputs["invoke"].requests == []


@pytest.mark.parametrize(
    "raw_provider",
    [
        '{"content":[',
        *[
            json.dumps(
                {
                    "content": [{"type": "text", "text": "{}"}],
                    "stop_reason": "end_turn",
                    "usage": usage,
                }
            )
            for usage in (None, [], "invalid", {"input_tokens": "ten"})
        ],
        *[
            json.dumps({"content": content, "stop_reason": "end_turn", "usage": {}})
            for content in (None, 1, {}, "invalid")
        ],
    ],
)
def test_agent_retains_malformed_provider_body_for_review(tmp_path, raw_provider):
    from proofops.adapters.aws.bedrock import BedrockInvoker
    from proofops_agent.tagger import BedrockMessagesTagger

    from tests.acceptance.test_preflight import (
        AUTH,
        NOW,
        REGIONS,
        SyntheticBedrockClient,
        binding,
        consent,
    )

    class SyntheticMalformedClient(SyntheticBedrockClient):
        def invoke_model(self, **kwargs):
            self.calls.append(kwargs)
            return {"body": raw_provider.encode()}

    runtime = binding()
    client = SyntheticMalformedClient()
    adapter = BedrockMessagesTagger(
        BedrockInvoker(client, account_id=runtime["account_id"]),
        auth=AUTH,
        runtime_binding=runtime,
        consent=consent(),
        allowed_regions=REGIONS,
        document_rights="synthetic-public",
        checked_at=NOW,
        synthetic=True,
    )
    inputs = setup(tmp_path)
    inputs["settings"] = replace(
        inputs["settings"],
        binding=ModelBinding(runtime["runtime_binding_id"], "tagger", True),
        model_id=runtime["model_id"],
        region=runtime["endpoint_region"],
    )
    inputs["invoke"] = adapter.invoke
    runs = execute(inputs)
    assert len(client.calls) == 3
    assert all(run.provider_response_json == raw_provider for run in runs)
    assert all(run.usage.status == "failed" and run.raw_response_json is None for run in runs)
    assert all(run.usage.input_tokens is None for run in runs)
    recovered = execute(inputs)
    assert len(client.calls) == 3
    assert all(run.recovered and run.provider_response_json == raw_provider for run in recovered)
    assert consensus(runs, inputs).confirmed_tags is None


def test_foreign_credited_source_is_not_accepted(tmp_path):
    inputs = setup(tmp_path)
    inputs["invoke"].changes = {2: {"element": {"credited_from": str(UUID(int=999))}}}
    assert consensus(execute(inputs), inputs).confirmed_tags is None


def test_search_metadata_cannot_smuggle_gold_into_rendered_model_input(tmp_path):
    inputs = setup(tmp_path)
    data = inputs["packet"].to_dict()
    data.pop("packet_sha256")
    data["search_coverage"]["gold_label"] = "secret-answer-must-not-render"
    inputs["packet"] = freeze_packet(data)
    execute(inputs)
    assert all(
        "secret-answer-must-not-render" not in r["user_json"] for r in inputs["invoke"].requests
    )


def test_retrieve_select_tag_consensus_uses_complete_track_catalog(tmp_path):
    from proofops.application.evidence.retrieval import freeze_track_packet

    inputs = setup(tmp_path)
    retrieval = retrieve_evidence(
        inputs["context"].claim,
        inputs["original"],
        SyntheticSearch(inputs["original"]),
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="synthetic-v1",
        rulepack=inputs["rulepack"],
        document_context={},
        token_counter=lambda text: len(text) // 4,
    )
    before = retrieval.payload_json
    full_catalog = retrieval.to_dict()["allowed_elements"]
    assert set(full_catalog) == {
        e["id"] for e in inputs["rulepack"].file_content("rubric/elements.yaml")["elements"]
    }
    selected = freeze_track_packet(retrieval, track=inputs["track"], rulepack=inputs["rulepack"])
    assert selected.packet_sha256 != retrieval.packet_sha256
    assert selected.to_dict()["retrieval_packet_sha256"] == retrieval.packet_sha256
    assert selected.to_dict()["allowed_elements"] == [f"P{i}" for i in range(1, 7)]
    assert retrieval.payload_json == before
    assert (
        freeze_track_packet(selected, track=inputs["track"], rulepack=inputs["rulepack"])
        == selected
    )
    inputs["packet"] = selected
    elements = [
        dict(
            element_id=f"P{i}",
            state="unknown",
            evidence_refs=[],
            normalized_value=None,
            credited_from=None,
            reason_code="synthetic-unresolved",
        )
        for i in range(1, 7)
    ]
    inputs["invoke"].changes = {i: {"root": {"elements": elements}} for i in (1, 2, 3)}
    runs = execute(inputs)
    assert all(r.guarded is not None and r.status == "needs_review" for r in runs)
    assert {r.packet_sha256 for r in runs} == {selected.packet_sha256}
    assert all(
        json.loads(request["user_json"])["untrusted_document_data"]["allowed_elements"]
        == [f"P{i}" for i in range(1, 7)]
        for request in inputs["invoke"].requests
    )
    result = consensus(runs, inputs)
    assert [e.element_id for e in result.candidate_elements] == [f"P{i}" for i in range(1, 7)]
    assert all(e.state == "unknown" for e in result.candidate_elements)
    assert result.review_status == "needs_review" and result.confirmed_tags is None
    assert not any("ELEMENT_TRACK_MISMATCH" in reason for reason in result.reasons)
    inputs["packet"] = retrieval
    with pytest.raises(DomainValidationError):
        consensus(runs, inputs)
    with pytest.raises(DomainValidationError):
        execute(inputs)
    assert len(inputs["invoke"].requests) == 3


@pytest.mark.parametrize("track_name", ["goal", "performance", "management"])
def test_selected_packet_requires_complete_catalog(tmp_path, track_name):
    from proofops.application.evidence.retrieval import freeze_track_packet
    from proofops.domain.rules.engine import MAPPINGS

    inputs = setup(tmp_path)
    retrieval = retrieve_evidence(
        inputs["context"].claim,
        inputs["original"],
        SyntheticSearch(inputs["original"]),
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="synthetic-v1",
        rulepack=inputs["rulepack"],
        document_context={},
        token_counter=lambda text: len(text) // 4,
    )
    track = replace(inputs["track"], track=track_name)
    selected = freeze_track_packet(retrieval, track=track, rulepack=inputs["rulepack"])
    assert selected.to_dict()["allowed_elements"] == list(MAPPINGS[track_name])
    narrowed = selected.to_dict()
    narrowed.pop("packet_sha256")
    narrowed["allowed_elements"].pop()
    inputs.update(packet=freeze_packet(narrowed), track=track)
    with pytest.raises(DomainValidationError):
        execute(inputs)
    assert inputs["invoke"].requests == []


@pytest.mark.parametrize("omission", [None, "parents", "tables"])
def test_tagger_rechecks_orphan_notes_before_budget_or_model_call(tmp_path, omission):
    from proofops.application.evidence.binding import ClaimContext

    from tests.acceptance.test_retrieval import corpus as retrieval_corpus
    from tests.acceptance.test_retrieval import orphan_corpus, retrieve

    inputs = setup(tmp_path)
    clean, claim = retrieval_corpus(table=True)
    original, same_claim = orphan_corpus()
    assert claim == same_claim
    track = TrackCandidate(claim, "performance", None)
    packet = freeze_track_packet(retrieve(clean, claim), track=track, rulepack=inputs["rulepack"])
    data = packet.to_dict()
    data["graph_sha256"] = canonical_hash(asdict(original))
    kinds = {b.source_id: b.kind for b in original.blocks}
    if omission == "tables":
        data["evidence_candidates"] = [
            c for c in data["evidence_candidates"] if not kinds[c["source_id"]].startswith("table")
        ]
        data["candidate_bindings"] = [
            b for b in data["candidate_bindings"] if not kinds[b["source_id"]].startswith("table")
        ]
    elif omission == "parents":
        for c in data["evidence_candidates"]:
            c["source_refs"] = [r for r in c["source_refs"] if kinds[r["source_id"]] != "table"]
    inputs.update(
        packet=freeze_packet(data),
        original=original,
        context=ClaimContext(claim, {}),
        track=track,
        relation_tags={},
    )
    with pytest.raises(DomainValidationError, match="unassigned footnote"):
        execute(inputs)
    assert inputs["invoke"].requests == []
    assert cost_summary(inputs["usage_store"], TENANT, RUN)["attempt_count"] == 0


def test_table_issue_rechecked_when_all_table_candidates_are_omitted(tmp_path):
    from proofops.application.evidence.binding import ClaimContext
    from proofops.application.ingest.graph_fusion import QualityIssue

    from tests.acceptance.test_retrieval import corpus as retrieval_corpus
    from tests.acceptance.test_retrieval import retrieve

    inputs = setup(tmp_path)
    graph, claim = retrieval_corpus(table=True)
    table_id = next(b.source_id for b in graph.blocks if b.kind == "table")
    track = TrackCandidate(claim, "performance", None)
    packet = freeze_track_packet(retrieve(graph, claim), track=track, rulepack=inputs["rulepack"])
    graph = replace(
        graph,
        issues=(QualityIssue("review", "table_note_review", 1, (table_id,), "open", "Unresolved"),),
    )
    data = packet.to_dict()
    data["graph_sha256"] = canonical_hash(asdict(graph))
    kinds = {b.source_id: b.kind for b in graph.blocks}
    for key in ("evidence_candidates", "candidate_bindings"):
        data[key] = [c for c in data[key] if not kinds[c["source_id"]].startswith("table")]
    inputs.update(
        packet=freeze_packet(data),
        original=graph,
        context=ClaimContext(claim, {}),
        track=track,
        relation_tags={},
    )
    with pytest.raises(DomainValidationError, match="open source issue"):
        execute(inputs)
    assert inputs["invoke"].requests == []
    assert cost_summary(inputs["usage_store"], TENANT, RUN)["attempt_count"] == 0


@pytest.mark.parametrize("omit_scope", [False, True])
def test_unanimous_model_present_cannot_hide_omitted_cross_source_scope(tmp_path, omit_scope):
    inputs = setup(tmp_path)
    graph, claim, refs = corpus()
    data = inputs["packet"].to_dict()
    data.pop("packet_sha256")
    data["graph_sha256"] = canonical_hash(asdict(graph))
    data["evidence_candidates"].append(
        dict(
            source_id=refs[1].source_id,
            source_scope="same_table",
            allowed_elements=["P1"],
            source_refs=[asdict(refs[1])],
        )
    )
    expected, actual = tags(refs[0]), tags(refs[1])
    if omit_scope:
        expected.pop("scope")
        actual.pop("scope")
    inputs.update(
        packet=freeze_packet(data),
        original=graph,
        context=replace(inputs["context"], dimensions=expected),
        relation_tags={refs[1].source_id: actual},
    )
    inputs["invoke"].source = span(refs[1], "40%")
    runs = execute(inputs)
    assert all(json.loads(r.raw_response_json)["elements"][0]["state"] == "present" for r in runs)
    assert all(
        r.guarded.elements[0].state == ("unknown" if omit_scope else "present") for r in runs
    )
    result = consensus(runs, inputs)
    assert result.candidate_elements[0].state == ("unknown" if omit_scope else "present")
    assert result.confirmed_tags is None
