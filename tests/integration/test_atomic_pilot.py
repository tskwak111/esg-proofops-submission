import json

from evaluation.atomic_pilot import extract_selected
from tests.acceptance.test_claims import COMPOUND, TENANT, graph_of
from tests.integration.test_section_pipeline import section_map


def test_exact_atomic_spans_and_invalid_model_output_stay_source_bound(tmp_path):
    graph = graph_of(COMPOUND)
    source = graph.blocks[0].source_id
    pieces = [
        "당사는 2023년 Scope 1·2 배출량을 전년 대비 8% 감축했으며,",
        "2030년까지 2020년 대비 40% 감축을 목표로 합니다.",
    ]

    class Client:
        def __init__(self, claims):
            self.claims = claims

        def complete(self, *args, **kwargs):
            return dict(content=json.dumps({"claims": self.claims}), provider_model="test")

    discovery, records = extract_selected(
        graph, section_map(graph), [source], Client(pieces), tmp_path / "valid", tenant_id=TENANT
    )
    assert [c.quote for c in discovery.claims] == pieces
    assert all(c.source_quality == "unverified" for c in discovery.claims)
    assert records[0]["status"] == "passed"
    assert all(
        c.receipt.packet_sha256 == records[0]["application_packet_sha256"] for c in discovery.claims
    )
    failed, records = extract_selected(
        graph,
        section_map(graph),
        [source],
        Client(["invented 90% reduction"]),
        tmp_path / "invalid",
        tenant_id=TENANT,
    )
    assert failed.claims == () and records[0]["status"] == "failed"
    assert any(
        x.state == "unknown" and x.reason == "atomic_response_invalid" for x in failed.exclusions
    )
    assert list((tmp_path / "invalid").glob("*/response.json"))


def test_context_only_sentence_cannot_become_new_claim(tmp_path):
    graph = graph_of(COMPOUND + " 업계는 일반적으로 에너지를 사용합니다.")
    source = graph.blocks[0].source_id

    class Client:
        def complete(self, system, user_json, **kwargs):
            return dict(
                content=json.dumps({"claims": ["업계는 일반적으로 에너지를 사용합니다."]}),
                provider_model="test",
            )

    discovery, records = extract_selected(
        graph,
        section_map(graph),
        {source: [(0, len(COMPOUND))]},
        Client(),
        tmp_path / "context",
        tenant_id=TENANT,
    )
    assert discovery.claims == ()
    assert records[0]["status"] == "failed"


def test_replay_preserves_omissions_without_network_and_refuses_changed_prompt(tmp_path):
    from evaluation.atomic_pilot import ReplayClient

    graph = graph_of(COMPOUND)
    source = graph.blocks[0].source_id
    first = COMPOUND.split(",")[0] + ","

    class Client:
        def complete(self, *args, **kwargs):
            return dict(content=json.dumps({"claims": [first]}), provider_model="test")

    _, original = extract_selected(
        graph, section_map(graph), [source], Client(), tmp_path / "original", tenant_id=TENANT
    )
    replay = ReplayClient(tmp_path / "original")
    discovery, records = extract_selected(
        graph, section_map(graph), [source], replay, tmp_path / "replay", tenant_id=TENANT
    )
    assert [c.quote for c in discovery.claims] == [first]
    assert records[0]["replay_of_request_id"] == original[0]["request_id"]
    coverage = records[0]["targets"][0]
    assert coverage["coverage"] == "partial"
    assert coverage["unreturned_spans"][0]["quote"].strip() == COMPOUND[len(first) :].strip()
    import pytest

    with pytest.raises(ValueError):
        replay.complete("changed prompt", "{}")


def test_candidate_artifact_must_match_original_source():
    from copy import deepcopy
    from dataclasses import asdict

    import pytest

    from evaluation.atomic_pilot import selected_targets

    graph = graph_of(COMPOUND)
    block = graph.blocks[0]
    candidate = dict(
        source_id=block.source_id,
        span=dict(char_start=0, char_end=len(COMPOUND), quote=COMPOUND),
        source_ref=asdict(block.source_ref()),
        source_quality=block.quality,
    )
    assert selected_targets(graph, {"claims": [candidate]}) == {
        block.source_id: [(0, len(COMPOUND))]
    }
    for field, value in [("quote", "invented"), ("char_start", True)]:
        changed = deepcopy(candidate)
        changed["span"][field] = value
        with pytest.raises(ValueError):
            selected_targets(graph, {"claims": [changed]})
    changed = deepcopy(candidate)
    changed["source_ref"]["parse_manifest_id"] = "11111111-1111-4111-8111-111111111111"
    with pytest.raises(ValueError):
        selected_targets(graph, {"claims": [changed]})


def test_target_only_trial_omits_context_without_rewriting_target(tmp_path):
    graph = graph_of(COMPOUND)
    source = graph.blocks[0].source_id
    start = COMPOUND.index("2030년")

    class Client:
        def complete(self, system, user_json, **kwargs):
            data = json.loads(user_json)["untrusted_document_data"]
            assert data["context"] == ""
            assert data["targets"][0]["quote"] == COMPOUND[start:]
            return dict(content=json.dumps({"claims": [COMPOUND[start:]]}), provider_model="test")

    discovery, records = extract_selected(
        graph,
        section_map(graph),
        {source: [(start, len(COMPOUND))]},
        Client(),
        tmp_path / "target-only",
        tenant_id=TENANT,
        include_context=False,
    )
    assert discovery.claims[0].quote == COMPOUND[start:]
    assert records[0]["targets"][0]["coverage"] == "full_text_returned"
