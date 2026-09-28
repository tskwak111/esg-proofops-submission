"""Local evidence search: bounded BM25 over an explicit page set (TASK owned)."""

from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.application.evidence.retrieval import SearchScope

from tests.acceptance.test_claims import TENANT, graph_of

OTHER = "22222222-2222-4222-8222-222222222222"
GENERATION = "korean-bigram-bm25-v2"


def make_search(graph, pages=None, index_generation=GENERATION, tenant_id=TENANT):
    from proofops.adapters.local.evidence_search import LocalEvidenceSearch

    if pages is None:
        pages = tuple(b.page_num for b in graph.blocks)
    return LocalEvidenceSearch(
        graph, tenant_id=tenant_id, pages=pages, index_generation=index_generation
    )


def test_search_terms_exported_with_korean_bigram_routing():
    from proofops.adapters.local.evidence_search import search_terms

    assert search_terms("온실가스") == ["온실", "실가", "가스"]
    assert search_terms("탄소 4 톤") == ["탄소", "4"]


def test_scope_exact_match_required_foreign_fails_before_search():
    graph = graph_of("온실가스 배출량", "사회 본문")
    search = make_search(graph)
    foreign = SearchScope(TENANT, graph.document_version_id, graph.parse_manifest_id, "wrong")
    with pytest.raises(ValueError):
        search.search(foreign, "온실가스")
    # Foreign scope must fail before search, including vector calls (never not_run).
    with pytest.raises(ValueError):
        search.search(foreign, "온실가스", vector=(1.0,))
    other_tenant = SearchScope(
        OTHER, graph.document_version_id, graph.parse_manifest_id, GENERATION
    )
    with pytest.raises(ValueError):
        search.search(other_tenant, "온실가스", vector=(1.0,))


def test_pages_strict_positive_int_bool_forbidden():
    graph = graph_of("온실가스 배출량", "사회 본문")
    for bad in (True, False, 0, -1, 1.0, "1", None):
        with pytest.raises(ValueError):
            make_search(graph, pages=(bad,))


def test_pages_must_occur_in_graph():
    graph = graph_of("온실가스 배출량", "사회 본문")
    with pytest.raises(ValueError):
        make_search(graph, pages=(999,))
    with pytest.raises(ValueError):
        make_search(graph, pages=(1, 999))


def test_empty_pages_allowed_as_explicit_zero_coverage():
    graph = graph_of("온실가스 배출량", "사회 본문")
    search = make_search(graph, pages=())
    assert search.pages == frozenset()
    result = search.search(search.scope, "온실가스")
    assert result.hits == ()
    assert result.status == "bounded"


def test_zero_match_returns_bounded_empty():
    graph = graph_of("온실가스 배출량", "사회 본문")
    search = make_search(graph)
    result = search.search(search.scope, "zzzqqq-no-such-token")
    assert result.hits == ()
    assert result.status == "bounded"


def test_top20_deterministic_positive_hits():
    texts = [f"배출량 데이터 {i}호 환경 보고" for i in range(25)]
    graph = graph_of(*texts)
    search = make_search(graph)
    first = search.search(search.scope, "배출량")
    second = search.search(search.scope, "배출량")
    assert len(first.hits) == 20
    assert [h.source_id for h in first.hits] == [h.source_id for h in second.hits]
    assert len({h.source_id for h in first.hits}) == 20
    assert first.status == "bounded"


def test_vector_not_none_yields_not_run_never_fake_vectors():
    graph = graph_of("온실가스 배출량", "사회 본문")
    search = make_search(graph)
    for vector in ((1.0,), (0.0, 0.0), ()):
        result = search.search(search.scope, "온실가스", vector=vector)
        assert result.status == "not_run"
        assert result.hits == ()


def test_hits_hash_original_raw_text_and_provenance():
    graph = graph_of("온실가스를 감축하였습니다.", "온실가스 배출량", "직원 수")
    search = make_search(graph)
    assert search.synthetic is True
    assert search.graph is graph
    assert search.pages == frozenset(b.page_num for b in graph.blocks)
    assert search.scope.tenant_id == TENANT
    assert search.scope.document_version_id == graph.document_version_id
    assert search.scope.parse_manifest_id == graph.parse_manifest_id
    assert search.scope.index_generation == GENERATION
    result = search.search(search.scope, "온실가스")
    assert result.hits
    blocks = {b.source_id: b for b in graph.blocks}
    for hit in result.hits:
        assert hit.scope == search.scope
        assert sha256(blocks[hit.source_id].raw_text.encode()).hexdigest() == (hit.raw_text_sha256)


def test_duplicate_block_ids_rejected():
    graph = graph_of("온실가스 배출량", "사회 본문")
    duplicated = replace(graph, blocks=(graph.blocks[0], graph.blocks[0]))
    with pytest.raises(ValueError):
        make_search(duplicated)


def test_graph_identity_validated_before_indexing():
    graph = graph_of("온실가스 배출량", "사회 본문")
    with pytest.raises(ValueError):
        make_search(graph, tenant_id=OTHER)


def test_ranking_values_preserved_for_valid_inputs():
    graph = graph_of(
        "온실가스를 감축하였습니다.",
        "온실가스 사회 본문",
        "온실가스 배출량",
        "직원 수",
        "온실가스 검증 의견서",
    )
    search = make_search(graph)
    blocks = {b.source_id: b for b in graph.blocks}
    hits = search.search(search.scope, "온실가스를 감축하였습니다.").hits
    # Explicit full-page coverage: page 2 ("온실가스 사회 본문") also routes,
    # unlike the section-map subset in evaluation/section_pipeline.py. Only the
    # unrelated page 4 ("직원 수") stays out, with identical BM25 values.
    assert {blocks[h.source_id].page_num for h in hits} == {1, 2, 3, 5}


def test_far_page_numeric_hit_does_not_gain_direct_evidence_scope():
    from proofops.application.evidence.retrieval import retrieve_evidence

    from tests.acceptance.test_citations import RUN
    from tests.acceptance.test_retrieval import corpus
    from tests.acceptance.test_rules import pack

    graph, claim = corpus()
    search = make_search(graph)
    packet = retrieve_evidence(
        claim,
        graph,
        search,
        tenant_id=TENANT,
        run_id=RUN,
        index_generation=search.scope.index_generation,
        rulepack=pack(),
        document_context={},
        token_counter=len,
    ).to_dict()
    numeric_id = next(b.source_id for b in graph.blocks if "2030" in b.raw_text)
    candidate = next(c for c in packet["evidence_candidates"] if c["source_id"] == numeric_id)
    assert candidate["source_scope"] == "global_bound"
    assert "G1" not in candidate["allowed_elements"]
    assert "P1" not in candidate["allowed_elements"]
    assert all(b["state"] == "undetermined" for b in packet["candidate_bindings"])
    assert packet["search_coverage"]["not_found_state"] == "unknown"


def test_collect_raw_candidate_review_unverified_table_positive_and_guards():
    """R04 positive: unverified table is surfaced in raw_candidate_review.

    Gated from retrieve_evidence and downstream confirmed tags.
    """
    import copy
    from uuid import UUID, uuid5

    from proofops.adapters.local.evidence_search import (
        collect_raw_candidate_review,
        validate_raw_candidate,
    )
    from proofops.application.ingest.graph_fusion import (
        CandidateBatch,
        CandidateBlock,
        fuse_candidates,
    )
    from proofops.domain.documents import NativeSource, PageGeometry

    from tests.acceptance.test_claims import MANIFEST, VERSION

    run = str(uuid5(UUID(MANIFEST), "synthetic-parser"))
    p_text = "당사는 온실가스 배출량을 매년 공시합니다."
    t_text = "2023년 온실가스 배출량 표: Scope 1 1234 tCO2e, Scope 2 5678 tCO2e"
    batch = CandidateBatch(
        TENANT,
        VERSION,
        MANIFEST,
        "a" * 64,
        run,
        "synthetic-text",
        "1",
        "synthetic",
        "b" * 64,
        (
            CandidateBlock(
                "paragraph",
                NativeSource(
                    VERSION,
                    MANIFEST,
                    run,
                    "p1",
                    1,
                    None,
                    (10, 10, 590, 50),
                    "pdf_bottom_left_points",
                    p_text,
                    0,
                    len(p_text),
                ),
                PageGeometry(600, 800, 0, (0, 0, 600, 800)),
            ),
            CandidateBlock(
                "table",
                NativeSource(
                    VERSION,
                    MANIFEST,
                    run,
                    "t1",
                    2,
                    None,
                    (10, 60, 590, 400),
                    "pdf_bottom_left_points",
                    t_text,
                    0,
                    len(t_text),
                ),
                PageGeometry(600, 800, 0, (0, 0, 600, 800)),
            ),
        ),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    table_block = next(b for b in graph.blocks if b.kind == "table")
    assert table_block.quality == "unverified"

    search = make_search(graph)
    review = collect_raw_candidate_review(search, "온실가스 배출량")

    assert review["schema_version"] == 1
    candidates = review["candidates"]
    assert len(candidates) >= 1

    table_cand = next(c for c in candidates if c["source_ref"]["page_num"] == 2)
    assert table_cand["status"] == "unverified"
    assert table_cand["reason"] == "UNVERIFIED_SOURCE"
    assert table_cand["source_ref"]["location_quality"] == "located"
    assert table_cand["source_ref"]["bbox"] is not None
    assert "1234 tCO2e" in table_cand["source_ref"]["quote"]

    # Negative 1: Identity mismatch (tampered document_version_id / tenant_id)
    tampered_id = copy.deepcopy(table_cand)
    tampered_id["source_ref"]["document_version_id"] = "99999999-9999-4999-8999-999999999999"
    assert not validate_raw_candidate(tampered_id, graph, tenant_id=TENANT)
    assert not validate_raw_candidate(table_cand, graph, tenant_id=OTHER)

    # Negative 2: Tampered raw_text_sha256
    tampered_hash = copy.deepcopy(table_cand)
    tampered_hash["source_ref"]["raw_text_sha256"] = "f" * 64
    assert not validate_raw_candidate(tampered_hash, graph, tenant_id=TENANT)

    # Negative 3: Tampered quote
    tampered_quote = copy.deepcopy(table_cand)
    tampered_quote["source_ref"]["quote"] = "조작된 배출량"
    assert not validate_raw_candidate(tampered_quote, graph, tenant_id=TENANT)

    # Negative 4: Unlocated geometry or tampered bbox coordinates
    unlocated = copy.deepcopy(table_cand)
    unlocated["source_ref"]["location_quality"] = "unlocated"
    unlocated["source_ref"]["bbox"] = None
    assert not validate_raw_candidate(unlocated, graph, tenant_id=TENANT)

    tampered_bbox = copy.deepcopy(table_cand)
    tampered_bbox["source_ref"]["bbox"] = (10.0, 60.0, 590.0, 999.0)
    assert not validate_raw_candidate(tampered_bbox, graph, tenant_id=TENANT)

    # Negative 5: Accepted label prohibited (must stay unconfirmed/unverified)
    tampered_status = copy.deepcopy(table_cand)
    tampered_status["status"] = "verified"
    assert not validate_raw_candidate(tampered_status, graph, tenant_id=TENANT)
    tampered_status["status"] = "accepted"
    assert not validate_raw_candidate(tampered_status, graph, tenant_id=TENANT)

    # Negative 6: Bool offset rejected
    bool_offset = copy.deepcopy(table_cand)
    bool_offset["source_ref"]["char_start"] = True
    assert not validate_raw_candidate(bool_offset, graph, tenant_id=TENANT)

    # Negative 7: Empty span rejected (start == end)
    empty_span = copy.deepcopy(table_cand)
    empty_span["source_ref"]["char_start"] = 5
    empty_span["source_ref"]["char_end"] = 5
    empty_span["source_ref"]["quote"] = ""
    assert not validate_raw_candidate(empty_span, graph, tenant_id=TENANT)

    # Negative 8: Mismatched printed_page_label and verification_state
    mismatched_label = copy.deepcopy(table_cand)
    mismatched_label["source_ref"]["printed_page_label"] = "99"
    assert not validate_raw_candidate(mismatched_label, graph, tenant_id=TENANT)

    mismatched_vstate = copy.deepcopy(table_cand)
    mismatched_vstate["source_ref"]["verification_state"] = "verified"
    assert not validate_raw_candidate(mismatched_vstate, graph, tenant_id=TENANT)

    # Positive control: valid candidate passes
    assert validate_raw_candidate(table_cand, graph, tenant_id=TENANT)

    # Guard: retrieve_evidence filters out unverified sources from original_packet
    from proofops.application.claims import ClaimScope, discover_atomic_claims
    from proofops.application.evidence.retrieval import retrieve_evidence
    from proofops_agent.extraction import SyntheticClaimExtractor

    from tests.acceptance.test_rules import pack

    claims_discovery = discover_atomic_claims(
        graph, ClaimScope(TENANT, VERSION, MANIFEST), extractor=SyntheticClaimExtractor()
    )
    if claims_discovery.claims:
        claim = claims_discovery.claims[0]
        packet = retrieve_evidence(
            claim,
            graph,
            search,
            tenant_id=TENANT,
            run_id="55555555-5555-4555-8555-555555555555",
            index_generation=search.scope.index_generation,
            rulepack=pack(),
            document_context={},
            token_counter=len,
        ).to_dict()
        # The unverified table block MUST NOT be promoted to evidence_candidates
        table_sources = [
            c for c in packet["evidence_candidates"] if c["source_id"] == table_block.source_id
        ]
        assert len(table_sources) == 0
