from copy import deepcopy

import pytest
from proofops.application.claims import ClaimScope, discover_atomic_claims
from proofops.application.evidence.retrieval import SearchScope, retrieve_evidence
from proofops.domain.provenance import canonical_hash
from proofops_agent.extraction import SyntheticClaimExtractor

from evaluation.report_sections import POLICY_HASH, build_map
from evaluation.section_pipeline import SectionSearch, plan_for_graph
from tests.acceptance.test_claims import COMPOUND, TENANT, graph_of
from tests.acceptance.test_rules import pack


def section_map(graph):
    value = build_map(
        5,
        [
            dict(page=1, title="Environment", path=["Environment"]),
            dict(page=2, title="Social", path=["Social"]),
            dict(page=3, title="ESG DATA", path=["ESG DATA"]),
            dict(page=4, title="Social", path=["ESG DATA", "Social"]),
            dict(page=5, title="Appendix", path=["Appendix"]),
        ],
    )
    value.update(source_sha256=graph.source_sha256, page_count=5, policy_sha256=POLICY_HASH)
    value["map_sha256"] = canonical_hash(value)
    return value


def test_scoped_claims_and_search_keep_all_data_and_missing_pages():
    graph = graph_of(
        COMPOUND, "배출량 사회 본문", "Scope 1 배출량 환경 데이터", "Scope 1 배출량 사회 데이터"
    )
    mapped = section_map(graph)
    plan = plan_for_graph(graph, mapped, tenant_id=TENANT)
    assert plan["mode"] == "all_text"
    assert {
        b["page"] for p in plan["packets"] for b in p["untrusted_document_data"]["targets"]
    } == {1}
    assert plan["missing_evidence_pages"] == [5]
    discovery = discover_atomic_claims(
        graph, ClaimScope(**plan["scope"]), extractor=SyntheticClaimExtractor()
    )
    assert len(discovery.claims) == 2
    search = SectionSearch(graph, mapped, tenant_id=TENANT)
    result = search.search(search.scope, "배출량")
    blocks = {b.source_id: b for b in graph.blocks}
    assert {blocks[h.source_id].page_num for h in result.hits} == {1, 3, 4}
    assert result.status == "bounded"
    packet = retrieve_evidence(
        discovery.claims[0],
        graph,
        search,
        tenant_id=TENANT,
        run_id="55555555-5555-4555-8555-555555555555",
        index_generation=search.scope.index_generation,
        rulepack=pack(),
        document_context={},
        token_counter=len,
    ).to_dict()
    assert packet["search_coverage"]["not_found_state"] == "unknown"
    assert all(b["state"] == "undetermined" for b in packet["candidate_bindings"])
    # Source quality is unverified: retrieval must retain unresolved sources, not promote hits.
    assert packet["evidence_candidates"] == []
    assert {h.source_id for h in result.hits} <= set(
        packet["search_coverage"]["unprocessed_source_ids"]
    )
    assert search.search(search.scope, "배출량", vector=(1.0,)).status == "not_run"
    with pytest.raises(ValueError):
        search.search(
            SearchScope(TENANT, graph.document_version_id, graph.parse_manifest_id, "wrong"),
            "배출량",
        )
    altered = deepcopy(mapped)
    altered["claim_candidate_pages"] = [2]
    with pytest.raises(ValueError):
        plan_for_graph(graph, altered, tenant_id=TENANT)
    altered["map_sha256"] = canonical_hash({k: v for k, v in altered.items() if k != "map_sha256"})
    with pytest.raises(ValueError):
        plan_for_graph(graph, altered, tenant_id=TENANT)
    with pytest.raises(ValueError):
        plan_for_graph(graph, mapped, tenant_id="22222222-2222-4222-8222-222222222222")


def test_table_row_context_is_candidate_only_and_keeps_headers():
    from evaluation.section_pipeline import table_row_contexts
    from tests.acceptance.test_parsing import TENANT as table_tenant
    from tests.acceptance.test_table_bindings import setup_case

    graph, ids, _ = setup_case()
    result = table_row_contexts(graph, ids["r1c4"], tenant_id=table_tenant)
    assert len(result) == 1
    row = result[0]
    assert row["row"] == 1
    assert row["binding_status"] == "undetermined"
    assert ids["r1c0"] in {x["source_id"] for x in row["cells"]}
    assert ids["r0c4"] in {x["source_id"] for x in row["first_row_context"]}
    assert ids["r2c4"] not in {x["source_id"] for x in row["cells"]}
    with pytest.raises(ValueError):
        table_row_contexts(graph, ids["r1c4"], tenant_id="22222222-2222-4222-8222-222222222222")


def test_table_context_first_row_handles_one_based_parser_indices():
    from dataclasses import replace

    from proofops.application.ingest.graph_fusion import fuse_candidates

    from evaluation.section_pipeline import table_row_contexts
    from tests.acceptance.test_parsing import TENANT as table_tenant
    from tests.acceptance.test_table_bindings import setup_case

    graph, _, _ = setup_case()
    batch = graph.candidates[0]
    batch = replace(
        batch,
        blocks=tuple(
            replace(b, row_number=b.row_number + 1) if b.row_number is not None else b
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=table_tenant)
    source = next(b for b in graph.blocks if b.normalized_text == "8,889,779")
    (row,) = table_row_contexts(graph, source.source_id, tenant_id=table_tenant)
    assert "2025" in [x["source_ref"]["quote"] for x in row["first_row_context"]]


def test_korean_claim_search_finds_data_and_appendix_despite_particles():
    graph = graph_of(
        "온실가스를 감축하였습니다.",
        "온실가스 사회 본문",
        "온실가스 배출량",
        "직원 수",
        "온실가스 검증 의견서",
    )
    search = SectionSearch(graph, section_map(graph), tenant_id=TENANT)
    blocks = {b.source_id: b for b in graph.blocks}
    hits = search.search(search.scope, "온실가스를 감축하였습니다.").hits
    assert {blocks[h.source_id].page_num for h in hits} == {1, 3, 5}
    assert all(blocks[h.source_id].quality == "unverified" for h in hits)


def test_section_search_matches_numeric_tokens_not_substrings():
    graph = graph_of("탄소 4 톤", "사회 본문", "직원 54 명", "직원 400 명", "폐기물 4 톤")
    search = SectionSearch(graph, section_map(graph), tenant_id=TENANT)
    blocks = {b.source_id: b for b in graph.blocks}
    hits = search.search(search.scope, "4").hits
    assert {blocks[h.source_id].page_num for h in hits} == {1, 5}
    assert search.search(search.scope, "...").hits == ()


def test_parent_search_keeps_context_separate_and_rejects_false_parent():
    from dataclasses import asdict

    text = "REC 조달을 실시했다. 이를 통해 탄소 감축 효과를 얻었다."
    graph = graph_of(text, "사회 본문", "REC 구매 산정 방법", "사회 데이터", "보증 의견서")
    block = graph.blocks[0]

    def candidate(start, end):
        return dict(
            source_id=block.source_id,
            span=dict(char_start=start, char_end=end, quote=text[start:end]),
            source_ref=asdict(
                block.source_ref(normalized_char_start=start, normalized_char_end=end)
            ),
            source_quality=block.quality,
        )

    parent = candidate(0, len(text))
    child = candidate(text.index("이를 통해"), len(text))
    search = SectionSearch(graph, section_map(graph), tenant_id=TENANT)
    result = search.search_with_parent(search.scope, child, parent)
    ids = {b.source_id: b.page_num for b in graph.blocks}
    assert 3 not in {ids[h["source_id"]] for h in result["routes"][0]["hits"]}
    assert 3 in {ids[h["source_id"]] for h in result["routes"][1]["hits"]}
    assert result["claim"] == child and result["parent"] == parent
    assert result["binding_status"] == "undetermined"
    assert result["routes"][1]["role"] == "parent_context_only"
    assert all(len(route["hits"]) <= 20 for route in result["routes"])
    with pytest.raises(ValueError):
        search.search_with_parent(search.scope, parent, child)
    bad = deepcopy(parent)
    bad["span"]["quote"] = "invented"
    with pytest.raises(ValueError):
        search.search_with_parent(search.scope, child, bad)


def test_section_search_uses_local_adapter_and_exposes_unparsed_evidence_pages():
    from proofops.adapters.local.evidence_search import LocalEvidenceSearch

    graph = graph_of(COMPOUND, "사회 본문", "배출량 데이터", "사회 데이터")
    search = SectionSearch(graph, section_map(graph), tenant_id=TENANT)
    assert isinstance(search, LocalEvidenceSearch)
    assert search.missing_pages == (5,)
    assert search.pages == frozenset((1, 3, 4))
    assert search.coverage["evidence_candidate_pages"] == [1, 3, 4, 5]
