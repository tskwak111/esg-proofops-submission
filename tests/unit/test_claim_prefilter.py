"""Prefiltering saves input without asserting that deferred text is not a claim."""

import json
from dataclasses import replace

import pytest

from evaluation import claim_prefilter as prefilter
from tests.acceptance.test_claims import TENANT, graph_of


def test_candidates_keep_qualitative_and_implicit_claims_and_defer_unknown():
    graph = graph_of("친환경 제품입니다.", "이를 통해 12% 줄였습니다.", "일반 소개입니다.")
    plan = prefilter.prepare(graph, tenant_id=TENANT, pages=(1, 2, 3), mode="filtered")
    targets = [b for p in plan["packets"] for b in p["untrusted_document_data"]["targets"]]
    assert [b["sentences"][0]["text"] for b in targets] == [
        "친환경 제품입니다.",
        "이를 통해 12% 줄였습니다.",
    ]
    assert [(d["reason"], d["state"]) for d in plan["deferred"]] == [
        ("no_lexical_signal", "unknown")
    ]
    assert plan["coverage"] == "declared_subset_prefilter_preview"
    with pytest.raises(ValueError):
        prefilter.prepare(graph, tenant_id="22222222-2222-4222-8222-222222222222", pages=(1,))


def test_context_is_preserved_and_cannot_be_extracted_as_a_target():
    graph = graph_of("친환경 제품입니다.", "문맥입니다.")
    batch = graph.candidates[0]
    second = replace(
        batch.blocks[1],
        source=replace(batch.blocks[1].source, physical_page=1, native_bbox=(10, 80, 590, 120)),
    )
    from proofops.application.ingest.graph_fusion import fuse_candidates

    graph = fuse_candidates((replace(batch, blocks=(batch.blocks[0], second)),), tenant_id=TENANT)
    plan = prefilter.prepare(graph, tenant_id=TENANT, pages=(1,), mode="filtered")
    packet = plan["packets"][0]
    target = packet["untrusted_document_data"]["targets"][0]
    context = packet["untrusted_document_data"]["context"][0]
    assert context["text"] == "문맥입니다."
    payload = {"sentence_ids": [target["sentences"][0]["sentence_id"]]}
    claims = prefilter.validate(payload, packet, graph)
    assert claims[0]["source_ref"]["quote"] == "친환경 제품입니다."
    assert claims[0]["source_ref"]["verification_state"] == "candidate"
    assert claims[0]["atomicity"] == "not_reviewed"
    payload["sentence_ids"] = [context["source_id"] + ":0"]
    with pytest.raises(ValueError):
        prefilter.validate(payload, packet, graph)


def test_sentence_spans_preserve_decimal_and_original_offsets():
    text = "  12.5% 감축했습니다.\n친환경 제품입니다!  "
    spans = prefilter.sentence_spans(text)
    assert [text[start:end] for start, end in spans] == [
        "12.5% 감축했습니다.",
        "친환경 제품입니다!",
    ]


def test_oversized_and_unreadable_blocks_stay_unresolved_without_truncation():
    graph = graph_of("환경 " * 6000, "배출량 감축")
    graph = replace(graph, blocks=(graph.blocks[0], replace(graph.blocks[1], quality="unreadable")))
    plan = prefilter.prepare(graph, tenant_id=TENANT, pages=(1, 2))
    assert not plan["packets"]
    assert {item["reason"] for item in plan["deferred"]} == {"packet_too_large", "unreadable"}
    assert all(item["state"] in ("unknown", "unreadable") for item in plan["deferred"])


def test_batching_bound_and_model_response_guard():
    graph = graph_of(*["온실가스를 줄였습니다. " * 50] * 8)
    plan = prefilter.prepare(graph, tenant_id=TENANT, pages=tuple(range(1, 9)), mode="all_text")
    assert len(plan["packets"]) > 1
    assert len([b for p in plan["packets"] for b in p["untrusted_document_data"]["targets"]]) == 8
    for packet in plan["packets"]:
        assert len(json.dumps(packet).encode()) <= prefilter.MAX_PACKET_BYTES
    packet = plan["packets"][0]
    assert prefilter.validate({"sentence_ids": []}, packet, graph) == []
    with pytest.raises(ValueError):
        prefilter.validate({"sentence_ids": [], "grade": "E3"}, packet, graph)


def test_sentence_ids_cannot_invent_rewrite_duplicate_or_change_source():
    graph = graph_of("2024년 배출량을 12.5% 감축했습니다.")
    packet = prefilter.prepare(graph, tenant_id=TENANT, pages=(1,))["packets"][0]
    sentence = packet["untrusted_document_data"]["targets"][0]["sentences"][0]
    sid = sentence["sentence_id"]
    for payload in (
        {"sentence_ids": ["invented"]},
        {"sentence_ids": [sid, sid]},
        {"sentence_ids": [sid], "quote": "rewritten"},
    ):
        with pytest.raises(ValueError):
            prefilter.validate(payload, packet, graph)
    sentence["text"] = "changed"
    with pytest.raises(ValueError):
        prefilter.validate({"sentence_ids": [sid]}, packet, graph)


def test_short_blocks_are_batched_without_dropping_targets():
    graph = graph_of(*["환경 목표"] * 19)
    plan = prefilter.prepare(graph, tenant_id=TENANT, pages=tuple(range(1, 20)))
    counts = [len(p["untrusted_document_data"]["targets"]) for p in plan["packets"]]
    assert counts == [8, 8, 3]
