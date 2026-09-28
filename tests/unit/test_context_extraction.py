"""Offline acceptance for the target+bounded-context helper (real graphs)."""

from dataclasses import replace
from uuid import UUID, uuid5

import pytest
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry

from evaluation.context_extraction import (
    CONTEXT_SYSTEM,
    build_context_packet,
    validate_target_quotes,
)

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "33333333-3333-4333-8333-333333333333"
MANIFEST = "44444444-4444-4444-8444-444444444444"
TARGET = "당사는 목표를 달성한다."
NEAR = "인접: 기준연도 문장이다."
FAR = "먼 문맥 문장이다."
GEO = PageGeometry(600, 800, 0, (0, 0, 600, 800))


def graph_of(*items):
    """Items are (kind, page, bbox, text)."""
    run = str(uuid5(UUID(MANIFEST), "context-probe"))
    batch = CandidateBatch(
        TENANT,
        VERSION,
        MANIFEST,
        "c" * 64,
        run,
        "synthetic-text",
        "1",
        "synthetic",
        "d" * 64,
        tuple(
            CandidateBlock(
                kind,
                NativeSource(
                    VERSION,
                    MANIFEST,
                    run,
                    str(i),
                    page,
                    None,
                    bbox,
                    "pdf_bottom_left_points",
                    text,
                    0,
                    len(text),
                ),
                GEO,
            )
            for i, (kind, page, bbox, text) in enumerate(items)
        ),
        synthetic=True,
    )
    return fuse_candidates((batch,), tenant_id=TENANT)


def sample():
    return graph_of(
        ("paragraph", 1, (10, 10, 100, 20), TARGET),
        ("paragraph", 1, (10, 25, 100, 35), NEAR),
        ("paragraph", 1, (10, 300, 100, 310), FAR),
        ("paragraph", 2, (10, 10, 100, 20), "다른 페이지 문장이다."),
        ("table_cell", 1, (10, 40, 100, 50), "표 셀 문맥이다."),
    )


def by_text(graph, text):
    return next(b for b in graph.blocks if b.normalized_text == text)


def packet(**bounds):
    graph = sample()
    target = by_text(graph, TARGET)
    return graph, target, build_context_packet(graph, target.source_id, tenant_id=TENANT, **bounds)


def test_packet_preserves_identity_and_useful_nearby_text():
    graph, target, packet_data = packet(max_context_chars=500, max_context_blocks=5)
    data = packet_data["untrusted_document_data"]
    assert (packet_data["tenant_id"], packet_data["source_sha256"]) == (TENANT, graph.source_sha256)
    assert data["target"]["text"] == TARGET and "quote" not in data["target"]["source_ref"]
    quotes = [b["quote"] for b in data["context_blocks"]]
    assert NEAR in quotes and FAR in quotes  # useful nearby text
    assert all(q not in ("다른 페이지 문장이다.", "표 셀 문맥이다.") for q in quotes)
    assert TARGET not in quotes  # never merge target text
    assert "heading" in CONTEXT_SYSTEM.lower() and "context" in CONTEXT_SYSTEM.lower()


def test_neighbor_only_invented_quote_rejected():
    _, _, packet_data = packet(max_context_chars=500, max_context_blocks=5)
    data = packet_data["untrusted_document_data"]
    assert any("기준연도" in b["quote"] for b in data["context_blocks"])
    with pytest.raises(ValueError):
        validate_target_quotes({"claims": ["기준연도 문장이다."]}, data["target"]["text"])
    spans = validate_target_quotes({"claims": ["목표를 달성한다."]}, data["target"]["text"])
    assert spans[0].quote == "목표를 달성한다."


def test_scope_and_target_failures_fail_closed():
    graph, target, _ = packet(max_context_chars=500, max_context_blocks=5)
    with pytest.raises(ValueError):
        build_context_packet(
            graph,
            str(uuid5(UUID(TENANT), "missing")),
            tenant_id=TENANT,
            max_context_chars=500,
            max_context_blocks=5,
        )
    bad = replace(by_text(graph, TARGET), quality="conflicted", winner=None)
    graph_bad = replace(graph, blocks=(bad,))
    with pytest.raises(ValueError):
        build_context_packet(
            graph_bad, bad.source_id, tenant_id=TENANT, max_context_chars=500, max_context_blocks=5
        )
    with pytest.raises(ValueError):  # foreign tenant fails canonical boundary
        build_context_packet(
            graph,
            target.source_id,
            tenant_id=str(uuid5(UUID(TENANT), "x")),
            max_context_chars=500,
            max_context_blocks=5,
        )
    with pytest.raises(ValueError):  # duplicate IDs fail
        dup = replace(graph, blocks=(target, target))
        build_context_packet(
            dup, target.source_id, tenant_id=TENANT, max_context_chars=500, max_context_blocks=5
        )
    with pytest.raises(ValueError):
        validate_target_quotes({"claims": ["어디에도 없음"]}, "목표 텍스트이다.")


def test_deterministic_bounded_context():
    _, _, first = packet(max_context_chars=40, max_context_blocks=1)
    _, _, second = packet(max_context_chars=40, max_context_blocks=1)
    assert first == second
    data = first["untrusted_document_data"]
    assert len(data["context_blocks"]) <= 1
    assert sum(len(b["quote"]) + 1 for b in data["context_blocks"]) - 1 <= 40
    assert data["context_policy"]["max_context_chars"] == 40
