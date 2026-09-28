"""R06b bounded opinion: deterministic batches keep the full 126-block boundary.

No network, no credentials. FakeProbe never touches the network.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from proofops.adapters.local.upstage_assurance import (
    MAX_ASSURANCE_BATCHES,
    MAX_ASSURANCE_REQUEST_BYTES,
    SYSTEM_PROMPT,
    UpstageAssuranceExtractor,
    _transport_body_len,
)
from proofops.application.assurance import extract_assurance
from proofops.application.assurance_producer import select_opinion_boundary
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.application.ports.models import ModelBinding
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.provenance import canonical_hash

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
MANIFEST = "33333333-3333-4333-8333-333333333333"
STATEMENT = "44444444-4444-4444-8444-444444444444"
RUN = "55555555-5555-4555-8555-555555555555"
BINDING = ModelBinding("synthetic-assurance", "assurance", True)

FIXTURE_REQUEST = (
    Path(__file__).resolve().parents[2]
    / "tests/fixtures/pipeline/kia-assurance-request"
    / "9e1e46d4-b365-4748-ad76-3e060d18badd"
    / "request.json"
)


class FakeProbe:
    """Caller-owned transport double; per-batch content via function."""

    def __init__(self, content_fn=None, error_fn=None):
        self.content_fn = content_fn
        self.error_fn = error_fn
        self.calls: list[dict] = []

    @property
    def model(self):
        return "solar-pro3"

    def complete(self, system, user_json, *, request_id, max_tokens=1024, json_mode=False):
        self.calls.append(
            dict(
                system=system,
                user_json=user_json,
                request_id=request_id,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
        )
        if self.error_fn is not None:
            error = self.error_fn(request_id, user_json, len(self.calls) - 1)
            if error is not None:
                raise error
        content = self.content_fn(request_id, user_json, len(self.calls) - 1)
        return {
            "model": "solar-pro3",
            "provider_request_id": f"fake-{request_id}",
            "provider_model": "solar-pro3-260323",
            "input_tokens": 80,
            "output_tokens": 40,
            "response_sha256": canonical_hash(content),
            "content": content,
        }


def _graph_from_texts(texts: dict[str, str]):
    blocks = tuple(
        CandidateBlock(
            "paragraph",
            NativeSource(
                VERSION,
                MANIFEST,
                RUN,
                name,
                i + 1,
                None,
                (10, 10, 300, 40),
                "pdf_bottom_left_points",
                text,
                0,
                len(text),
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )
        for i, (name, text) in enumerate(texts.items())
    )
    batch = CandidateBatch(
        TENANT,
        VERSION,
        MANIFEST,
        "a" * 64,
        RUN,
        "synthetic",
        "fixture-v1",
        "synthetic",
        "b" * 64,
        blocks,
        (),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    by_native = {b.sources[0].source_native_id: b for b in graph.blocks}
    return graph, by_native


def _unique_prefix_quote(text: str, length: int = 10) -> str:
    stripped = text.strip()
    quote = stripped[: max(1, min(length, len(stripped)))]
    assert quote and text.count(quote) == 1, "fixture quote must be unambiguous in its block"
    return quote


def test_actual_kia_126_blocks_split_within_limit_and_include_all(tmp_path):
    """Actual sized request (16829B > 16384) splits into <=4 batches, all 126 kept."""
    fixture = json.loads(FIXTURE_REQUEST.read_text())
    fixture_blocks = json.loads(fixture["user_json"])["untrusted_document_data"]["blocks"]
    assert len(fixture_blocks) == 126
    full_body = json.dumps(
        {
            "model": "solar-pro3",
            "messages": [
                {"role": "system", "content": fixture["system_prompt"]},
                {"role": "user", "content": fixture["user_json"]},
            ],
            "max_tokens": fixture["max_tokens"],
            "temperature": 0,
            "stream": False,
            "response_format": {"type": "json_object"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    assert len(full_body) == 16829
    assert len(full_body) > MAX_ASSURANCE_REQUEST_BYTES

    texts = {f"b{i:03d}": blk["text"] for i, blk in enumerate(fixture_blocks)}
    graph, by_native = _graph_from_texts(texts)
    ids = tuple(by_native[f"b{i:03d}"].source_id for i in range(126))
    boundary, _ = select_opinion_boundary(graph, ids)

    first_text = texts["b000"]
    quote = _unique_prefix_quote(first_text)
    empty_content = json.dumps({"fields": {}})

    def content_fn(_rid, user_json, _n):
        payload = json.loads(user_json)
        block_texts = [b["text"] for b in payload["untrusted_document_data"]["blocks"]]
        if first_text in block_texts:
            idx = block_texts.index(first_text)
            return json.dumps({"fields": {"provider": [{"source_index": idx, "quote": quote}]}})
        return empty_content

    probe = FakeProbe(content_fn=content_fn)
    extractor = UpstageAssuranceExtractor(probe, tmp_path / "receipts")
    request_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    tagged = extractor.extract_tagged_fields(graph, boundary, request_id=request_id)

    # Observed full opinion needs exactly 2 batches, bounded by 4.
    assert len(probe.calls) == 2
    assert len(probe.calls) <= MAX_ASSURANCE_BATCHES
    for call in probe.calls:
        size = _transport_body_len(SYSTEM_PROMPT, call["user_json"], "solar-pro3", 2048)
        assert size <= MAX_ASSURANCE_REQUEST_BYTES
    # All 126 blocks are sent exactly once, in declared order.
    seen: list[str] = []
    for call in probe.calls:
        payload = json.loads(call["user_json"])
        seen.extend(blk["text"] for blk in payload["untrusted_document_data"]["blocks"])
    assert seen == [texts[f"b{i:03d}"] for i in range(126)]

    manifest = json.loads((tmp_path / "receipts" / request_id / "manifest.json").read_text())
    assert manifest["batch_count"] == 2
    assert len(manifest["boundary_source_ids"]) == 126
    assert sum(len(b["source_ids"]) for b in manifest["batches"]) == 126
    assert (tmp_path / "receipts" / request_id / "result.json").exists()

    # Field quote stays an exact substring of its correct source block.
    refs = tagged["provider"]
    assert len(refs) == 1
    assert refs[0].source_id == by_native["b000"].source_id
    assert refs[0].quote == quote
    assert quote in first_text


def test_batched_field_quotes_map_to_correct_batch_sources(tmp_path):
    fixture = json.loads(FIXTURE_REQUEST.read_text())
    fixture_blocks = json.loads(fixture["user_json"])["untrusted_document_data"]["blocks"]
    texts = {f"b{i:03d}": blk["text"] for i, blk in enumerate(fixture_blocks)}
    graph, by_native = _graph_from_texts(texts)
    ids = tuple(by_native[f"b{i:03d}"].source_id for i in range(126))
    boundary, _ = select_opinion_boundary(graph, ids)

    q0 = _unique_prefix_quote(texts["b000"])
    # Last block lives in the second batch (greedy split is 119+7).
    q_last = _unique_prefix_quote(texts["b125"])

    def content_fn(request_id, user_json, _n):
        payload = json.loads(user_json)
        n = len(payload["untrusted_document_data"]["blocks"])
        # Batch with 119 blocks is the first; batch with 7 is the second.
        if n > 50:
            return json.dumps({"fields": {"provider": [{"source_index": 0, "quote": q0}]}})
        # q_last is the last text of the whole opinion; find its batch-local index.
        idx = next(
            i
            for i, blk in enumerate(payload["untrusted_document_data"]["blocks"])
            if blk["text"] == texts["b125"]
        )
        assert payload["untrusted_document_data"]["blocks"][idx]["text"].count(q_last) == 1
        return json.dumps({"fields": {"covered_metrics": [{"source_index": idx, "quote": q_last}]}})

    probe = FakeProbe(content_fn=content_fn)
    extractor = UpstageAssuranceExtractor(probe, tmp_path / "receipts")
    tagged = extractor.extract_tagged_fields(
        graph, boundary, request_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    )
    assert tagged["provider"][0].source_id == by_native["b000"].source_id
    assert tagged["provider"][0].quote == q0
    assert tagged["covered_metrics"][0].source_id == by_native["b125"].source_id
    assert tagged["covered_metrics"][0].quote == q_last


def test_partial_failure_publishes_nothing_but_retains_receipts(tmp_path):
    texts = {
        "k0": "삼일회계법인 " + "가" * 2500,
        "k1": "ISAE 3000 " + "나" * 2500,
        "k2": "2024년 " + "다" * 2500,
    }
    graph, by_native = _graph_from_texts(texts)
    ids = tuple(by_native[name].source_id for name in ("k0", "k1", "k2"))
    boundary, _ = select_opinion_boundary(graph, ids)
    # Sanity: this sizing really forces batching (single body exceeds the limit).

    def content_fn(_rid, _uj, _n):
        return json.dumps({"fields": {"provider": [{"source_index": 0, "quote": "삼일회계법인"}]}})

    def error_fn(_rid, _uj, call_index):
        if call_index == 1:
            return ValueError("UPSTREAM_UNAVAILABLE")
        return None

    probe = FakeProbe(content_fn=content_fn, error_fn=error_fn)
    extractor = UpstageAssuranceExtractor(probe, tmp_path / "receipts")
    request_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    with pytest.raises(ValueError, match="UPSTREAM_UNAVAILABLE"):
        extractor.extract_tagged_fields(graph, boundary, request_id=request_id)
    directory = tmp_path / "receipts" / request_id
    assert not (directory / "result.json").exists()
    assert (directory / "failure.json").exists()
    # Successful first batch is retained; replay with the same id is blocked
    # (no blind resend, no repeat billing).
    assert (directory / "batch_00" / "raw_response.json").exists()
    with pytest.raises(ValueError, match="ASSURANCE_EXTRACTION_RECEIPT_EXISTS"):
        extractor.extract_tagged_fields(graph, boundary, request_id=request_id)
    assert len(probe.calls) == 2


def test_conflicting_period_across_batches_stays_unresolved(tmp_path):
    texts = {
        "p0": "보증 대상 기간은 2024년입니다. " + "가" * 2500,
        "p1": "추가 본문 " + "나" * 2500,
        "p2": "보증 대상 기간은 2023년입니다. " + "다" * 2500,
    }
    graph, by_native = _graph_from_texts(texts)
    ids = tuple(by_native[name].source_id for name in ("p0", "p1", "p2"))
    boundary, _ = select_opinion_boundary(graph, ids)

    def content_fn(_rid, user_json, _n):
        payload = json.loads(user_json)
        block_texts = [b["text"] for b in payload["untrusted_document_data"]["blocks"]]
        if any("2024" in t for t in block_texts):
            idx = next(i for i, t in enumerate(block_texts) if "2024" in t)
            fields = {"reporting_period": [{"source_index": idx, "quote": "2024"}]}
            return json.dumps({"fields": fields})
        if any("2023" in t for t in block_texts):
            idx = next(i for i, t in enumerate(block_texts) if "2023" in t)
            fields = {"reporting_period": [{"source_index": idx, "quote": "2023"}]}
            return json.dumps({"fields": fields})
        return json.dumps({"fields": {}})

    probe = FakeProbe(content_fn=content_fn)
    extractor = UpstageAssuranceExtractor(probe, tmp_path / "receipts")
    tagged = extractor.extract_tagged_fields(
        graph, boundary, request_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    )
    assert 2 <= len(probe.calls) <= MAX_ASSURANCE_BATCHES
    # Both conflicting values are preserved; nothing is picked here.
    assert len(tagged["reporting_period"]) == 2
    selected = tuple(ref for refs in tagged.values() for ref in refs)
    statement = extract_assurance(
        graph,
        selected,
        BINDING,
        tagged_fields=tagged,
        tenant_id=TENANT,
        statement_id=STATEMENT,
        model_sha256=extractor.model_sha256,
        prompt_sha256=extractor.prompt_sha256,
        replicate_id=1,
    )
    assert statement.reporting_period is None
    assert "reporting_period" in statement.unresolved_fields


def test_valid_single_batch_keeps_legacy_receipt_profile(tmp_path):
    texts = {
        "a-provider": "삼일회계법인은 아래 지표에 대해 제한적 보증을 제공하였습니다.",
        "a-standard": "본 보증은 ISAE 3000 기준에 따라 수행되었습니다.",
        "a-period": "보증 대상 기간은 2024년입니다.",
    }
    graph, by_native = _graph_from_texts(texts)
    ids = tuple(by_native[name].source_id for name in texts)
    boundary, _ = select_opinion_boundary(graph, ids)
    content = json.dumps({"fields": {"provider": [{"source_index": 0, "quote": "삼일회계법인"}]}})
    probe = FakeProbe(content_fn=lambda _rid, _uj, _n: content)
    extractor = UpstageAssuranceExtractor(probe, tmp_path / "receipts")
    request_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    tagged = extractor.extract_tagged_fields(graph, boundary, request_id=request_id)
    assert len(probe.calls) == 1
    assert probe.calls[0]["request_id"] == request_id
    directory = tmp_path / "receipts" / request_id
    assert (directory / "request.json").exists()
    assert (directory / "raw_response.json").exists()
    assert (directory / "result.json").exists()
    assert not (directory / "manifest.json").exists()
    assert list(directory.glob("batch_*")) == []
    assert tagged["provider"][0].source_id == by_native["a-provider"].source_id
