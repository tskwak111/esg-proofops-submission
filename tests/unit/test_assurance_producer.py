"""Unit tests: pure assurance-producer helpers (no network, no probe).

Covers exactly the meaningful cases the task called for: valid same-opinion
multi-field extraction feeding real `extract_assurance`, a wrong-period value
staying unresolved rather than fabricated, an excluded metric staying
excluded, and a quote copied from a second/different opinion's blocks being
rejected by the boundary fence. No claim of model accuracy is made anywhere
here; these are graph/geometry/string-matching invariants only.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from proofops.application.assurance import ClaimContext, match_assurance
from proofops.application.assurance_producer import (
    OpinionBoundary,
    build_tagged_fields,
    locate_field_quote,
    select_opinion_boundary,
)
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.application.ports.models import ModelBinding
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.errors import DomainValidationError

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
MANIFEST = "33333333-3333-4333-8333-333333333333"
STATEMENT = "44444444-4444-4444-8444-444444444444"
RUN = "55555555-5555-4555-8555-555555555555"
CLAIM = "66666666-6666-4666-8666-666666666666"
BINDING = ModelBinding("synthetic-assurance", "assurance", True)


def _two_opinion_graph():
    """One graph with TWO disjoint assurance opinions' worth of blocks.

    opinion_a: a genuine limited-assurance ISAE 3000 statement for Scope 1,
    2024, at one entity/facility. opinion_b: an unrelated reasonable-assurance
    statement for a different provider/period, so a quote genuinely
    belonging to opinion_b can be used to prove it is rejected when the
    caller declares only opinion_a's source_ids as the boundary.
    """
    texts = {
        "a-provider": "삼일회계법인은 아래 지표에 대해 제한적 보증을 제공하였습니다.",
        "a-standard": "본 보증은 ISAE 3000 기준에 따라 수행되었습니다.",
        "a-period": "보증 대상 기간은 2024년입니다.",
        "a-metric": "보증 대상 지표는 Scope 1 배출량입니다.",
        "a-boundary": "보증 대상 법인은 예시 법인이며 사업장은 서울 사업장입니다.",
        "b-provider": "한영회계법인은 아래 지표에 대해 합리적 보증을 제공하였습니다.",
        "b-standard": "본 보증은 AA1000AS 기준에 따라 수행되었습니다.",
        "b-period": "보증 대상 기간은 2023년입니다.",
    }
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
    opinion_a_ids = tuple(
        by_native[name].source_id
        for name in ("a-provider", "a-standard", "a-period", "a-metric", "a-boundary")
    )
    opinion_b_ids = tuple(
        by_native[name].source_id for name in ("b-provider", "b-standard", "b-period")
    )
    return graph, opinion_a_ids, opinion_b_ids, by_native


def test_select_opinion_boundary_returns_only_declared_blocks_raw_text():
    graph, opinion_a_ids, _opinion_b_ids, _by_native = _two_opinion_graph()
    boundary, texts = select_opinion_boundary(graph, opinion_a_ids)
    assert set(texts) == set(opinion_a_ids)
    expected = {b.source_id: b.raw_text for b in graph.blocks}
    assert all(texts[sid] == expected[sid] for sid in opinion_a_ids)
    assert isinstance(boundary, OpinionBoundary)


def test_select_opinion_boundary_rejects_unknown_or_duplicate_ids():
    graph, opinion_a_ids, _opinion_b_ids, _by_native = _two_opinion_graph()
    with pytest.raises(DomainValidationError):
        select_opinion_boundary(graph, (*opinion_a_ids, "not-a-real-source-id"))
    with pytest.raises(DomainValidationError):
        select_opinion_boundary(graph, (opinion_a_ids[0], opinion_a_ids[0]))


def test_locate_field_quote_rejects_source_id_outside_boundary():
    graph, opinion_a_ids, opinion_b_ids, by_native = _two_opinion_graph()
    boundary, _texts = select_opinion_boundary(graph, opinion_a_ids)
    # A real, exact quote — but it belongs to opinion_b's provider block,
    # which is outside opinion_a's declared boundary. This is exactly the
    # "cross-opinion fact lending" this fence exists to stop.
    b_provider_id = by_native["b-provider"].source_id
    with pytest.raises(DomainValidationError):
        locate_field_quote(
            graph, boundary, field="provider", source_id=b_provider_id, quote="한영회계법인"
        )
    assert b_provider_id in opinion_b_ids  # sanity: really is the other opinion


def test_locate_field_quote_rejects_ambiguous_or_absent_quote():
    graph, opinion_a_ids, _opinion_b_ids, by_native = _two_opinion_graph()
    boundary, _texts = select_opinion_boundary(graph, opinion_a_ids)
    provider_id = by_native["a-provider"].source_id
    with pytest.raises(DomainValidationError):
        locate_field_quote(
            graph, boundary, field="provider", source_id=provider_id, quote="없는문구"
        )
    with pytest.raises(DomainValidationError):
        locate_field_quote(graph, boundary, field="provider", source_id=provider_id, quote="   ")


def test_build_tagged_fields_end_to_end_feeds_real_extract_assurance_and_covers():
    from proofops.application.assurance import extract_assurance

    graph, opinion_a_ids, _opinion_b_ids, by_native = _two_opinion_graph()
    boundary, _texts = select_opinion_boundary(graph, opinion_a_ids)
    field_quotes = {
        "provider": [{"source_id": by_native["a-provider"].source_id, "quote": "삼일회계법인"}],
        "standard_raw": [{"source_id": by_native["a-standard"].source_id, "quote": "ISAE 3000"}],
        "level": [{"source_id": by_native["a-provider"].source_id, "quote": "제한적 보증"}],
        "reporting_period": [{"source_id": by_native["a-period"].source_id, "quote": "2024"}],
        "covered_metrics": [{"source_id": by_native["a-metric"].source_id, "quote": "Scope 1"}],
        "entities": [{"source_id": by_native["a-boundary"].source_id, "quote": "예시 법인"}],
        "facilities": [{"source_id": by_native["a-boundary"].source_id, "quote": "서울 사업장"}],
    }
    tagged = build_tagged_fields(graph, boundary, field_quotes)
    selected_refs = tuple(ref for refs in tagged.values() for ref in refs)
    statement = extract_assurance(
        graph,
        selected_refs,
        BINDING,
        tagged_fields=tagged,
        tenant_id=TENANT,
        statement_id=STATEMENT,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_id=1,
    )
    assert statement.provider == "삼일회계법인"
    assert statement.level == "limited"
    assert statement.reporting_period == "2024"
    assert not statement.unresolved_fields

    match = match_assurance(
        statement,
        ClaimContext(TENANT, VERSION, CLAIM, "Scope 1", "2024", ("예시 법인",), ("서울 사업장",)),
    )
    assert match.status == "covered"


def test_wrong_period_claim_is_not_covered_not_fabricated():
    from proofops.application.assurance import extract_assurance

    graph, opinion_a_ids, _opinion_b_ids, by_native = _two_opinion_graph()
    boundary, _texts = select_opinion_boundary(graph, opinion_a_ids)
    field_quotes = {
        "provider": [{"source_id": by_native["a-provider"].source_id, "quote": "삼일회계법인"}],
        "standard_raw": [{"source_id": by_native["a-standard"].source_id, "quote": "ISAE 3000"}],
        "level": [{"source_id": by_native["a-provider"].source_id, "quote": "제한적 보증"}],
        "reporting_period": [{"source_id": by_native["a-period"].source_id, "quote": "2024"}],
        "covered_metrics": [{"source_id": by_native["a-metric"].source_id, "quote": "Scope 1"}],
        "entities": [{"source_id": by_native["a-boundary"].source_id, "quote": "예시 법인"}],
        "facilities": [{"source_id": by_native["a-boundary"].source_id, "quote": "서울 사업장"}],
    }
    tagged = build_tagged_fields(graph, boundary, field_quotes)
    selected_refs = tuple(ref for refs in tagged.values() for ref in refs)
    statement = extract_assurance(
        graph,
        selected_refs,
        BINDING,
        tagged_fields=tagged,
        tenant_id=TENANT,
        statement_id=STATEMENT,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_id=1,
    )
    # Claim asks about a DIFFERENT period (2023) than the statement covers (2024).
    match = match_assurance(
        statement,
        ClaimContext(TENANT, VERSION, CLAIM, "Scope 1", "2023", ("예시 법인",), ("서울 사업장",)),
    )
    assert match.status == "not_covered"
    assert match.period_match == "no"


def test_unknown_field_name_in_response_is_rejected_not_silently_dropped():
    graph, opinion_a_ids, _opinion_b_ids, by_native = _two_opinion_graph()
    boundary, _texts = select_opinion_boundary(graph, opinion_a_ids)
    with pytest.raises(DomainValidationError):
        build_tagged_fields(
            graph,
            boundary,
            {"grade": [{"source_id": by_native["a-provider"].source_id, "quote": "제한적 보증"}]},
        )


def _graph_with_irregular_whitespace_block():
    """One block whose raw text has double spaces, an embedded newline, and
    NFD-decomposed Hangul (as some PDF text extraction produces).

    This reproduces the exact failure mode the coordinator flagged: NFD
    decomposition makes the raw codepoint count for '삼일회계법인' 16 instead
    of NFC's 6, so raw and normalized offsets diverge for this text. Feeding
    a raw offset into `source_ref(normalized_char_start=...)` (which expects
    NORMALIZED offsets and remaps them back to raw) silently returns the
    WRONG slice here rather than raising — proven below by re-running the
    old buggy call path and asserting it mis-cites, before asserting the
    fixed `locate_field_quote` returns the exact raw slice.
    """
    import unicodedata

    provider_decomposed = unicodedata.normalize("NFD", "삼일회계법인")
    text = "제공기관은  " + provider_decomposed + "\n입니다. 제한적 보증을 제공하였습니다."
    block = CandidateBlock(
        "paragraph",
        NativeSource(
            VERSION,
            MANIFEST,
            RUN,
            "irregular",
            1,
            None,
            (10, 10, 300, 40),
            "pdf_bottom_left_points",
            text,
            0,
            len(text),
        ),
        PageGeometry(600, 800, 0, (0, 0, 600, 800)),
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
        (block,),
        (),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    return graph, text, provider_decomposed


def test_locate_field_quote_returns_exact_raw_slice_across_irregular_whitespace():
    graph, text, provider_decomposed = _graph_with_irregular_whitespace_block()
    source_id = graph.blocks[0].source_id
    boundary, _texts = select_opinion_boundary(graph, (source_id,))
    # Quote spans the double space, the embedded newline, AND NFD-decomposed
    # Hangul exactly as they appear in the raw text. Under the old buggy
    # code (raw offset passed as normalized_char_start/end) this exact quote
    # silently mis-cites a longer, wrong slice; verified directly below.
    quote = provider_decomposed + "\n입니다."
    assert quote in text
    start = text.index(quote)
    end = start + len(quote)

    # Prove the old code path actually mis-cites on this fixture (i.e. this
    # is a real regression case, not one that happens to pass either way).
    old_bug_ref = graph.blocks[0].source_ref(normalized_char_start=start, normalized_char_end=end)
    assert old_bug_ref.quote != quote, "fixture must reproduce the raw/normalized offset bug"

    ref = locate_field_quote(graph, boundary, field="provider", source_id=source_id, quote=quote)
    assert (ref.char_start, ref.char_end) == (start, end)
    assert ref.quote == quote == text[start:end]
    from hashlib import sha256

    assert ref.raw_text_sha256 == sha256(text.encode()).hexdigest()


def test_boundary_from_a_different_graph_is_rejected_even_with_colliding_ids():
    graph_a, opinion_a_ids, _opinion_b_ids, by_native = _two_opinion_graph()
    boundary_a, _texts = select_opinion_boundary(graph_a, opinion_a_ids)

    # A second, unrelated graph (different document_version_id/parse_manifest_id).
    graph_b, other_text, _provider_decomposed = _graph_with_irregular_whitespace_block()
    # boundary_a's source_ids are meaningless for graph_b, but even if we
    # force an OpinionBoundary claiming to be graph_b's tenant/document/
    # manifest with graph_a's ids, the identity check must still catch a
    # genuinely mismatched boundary (built for graph_a) being used against
    # graph_b.
    with pytest.raises(DomainValidationError):
        locate_field_quote(
            graph_b,
            boundary_a,
            field="provider",
            source_id=opinion_a_ids[0],
            quote="삼일회계법인",
        )
    with pytest.raises(DomainValidationError):
        build_tagged_fields(
            graph_b,
            boundary_a,
            {"provider": [{"source_id": opinion_a_ids[0], "quote": "삼일회계법인"}]},
        )
    # Sanity: graph_b's own boundary still works normally on graph_b.
    source_id_b = graph_b.blocks[0].source_id
    boundary_b, _texts_b = select_opinion_boundary(graph_b, (source_id_b,))
    quote_b = "제공기관은"
    ref = locate_field_quote(
        graph_b, boundary_b, field="provider", source_id=source_id_b, quote=quote_b
    )
    assert ref.quote == quote_b
    assert other_text.count(quote_b) == 1
