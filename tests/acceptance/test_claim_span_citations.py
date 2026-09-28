from dataclasses import replace

import pytest
from proofops.application.evidence.span_citations import span_verified_graph, verify_source_ref

from tests.acceptance.test_citations import OTHER, TENANT, snapshot


def test_only_attested_span_and_its_subspans_are_verified():
    graph, whole = snapshot("앞문장 오류. 배출량은 2025년 100톤이다. 다른 주장.")
    graph = replace(graph, blocks=(replace(graph.blocks[0], quality="unverified"),))
    quote = "배출량은 2025년 100톤이다."
    start = whole.quote.index(quote)
    ref = replace(
        whole,
        quote=quote,
        char_start=start,
        char_end=start + len(quote),
        verification_state="verified",
    )
    scoped = span_verified_graph(graph, (ref,), "a" * 64)
    assert verify_source_ref(ref, scoped, tenant_id=TENANT).verification_state == "verified"
    year = replace(ref, quote="2025", char_start=start + 6, char_end=start + 10)
    # Use actual raw offsets; neither graph nor verifier repairs bad offsets.
    pos = whole.quote.index("2025")
    year = replace(year, char_start=pos, char_end=pos + 4)
    assert verify_source_ref(year, scoped, tenant_id=TENANT).verification_state == "verified"
    assert scoped.blocks[0].quality == graph.blocks[0].quality == "unverified"
    assert verify_source_ref(whole, scoped, tenant_id=TENANT).verification_state == "rejected"
    for changed in (
        replace(ref, quote=quote.replace("100", "10")),
        replace(ref, page_num=99),
        replace(ref, raw_text_sha256="b" * 64),
        replace(ref, char_start=start - 1),
    ):
        assert verify_source_ref(changed, scoped, tenant_id=TENANT).verification_state == "rejected"
    with pytest.raises(ValueError):
        verify_source_ref(ref, scoped, tenant_id=OTHER)
    assert verify_source_ref(ref, graph, tenant_id=TENANT).verification_state == "rejected"


def test_conflicted_block_cannot_be_promoted_by_span_receipt():
    graph, ref = snapshot()
    graph = replace(graph, blocks=(replace(graph.blocks[0], quality="conflicted"),))
    scoped = span_verified_graph(graph, (replace(ref, verification_state="verified"),), "a" * 64)
    assert verify_source_ref(ref, scoped, tenant_id=TENANT).verification_state == "rejected"


def test_retrieval_accepts_attested_atomic_claim_without_admitting_whole_paragraph():
    from proofops.application.evidence.retrieval import SearchResult, retrieve_evidence

    from tests.acceptance.test_retrieval import RUN, corpus, pack

    graph, claim = corpus()
    sid = claim.source_refs[0].source_id
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, quality="unverified") if b.source_id == sid else b for b in graph.blocks
        ),
    )
    ref = replace(claim.source_refs[0], verification_state="verified")
    scoped = span_verified_graph(graph, (ref,), "c" * 64)

    class Search:
        synthetic = True

        def search(self, *a, **k):
            return SearchResult(status="bounded")

    packet = retrieve_evidence(
        claim,
        scoped,
        Search(),
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="test-span",
        rulepack=pack(),
        document_context={},
        token_counter=lambda text: len(text) // 4,
    )
    assert packet.to_dict()["status"] == "candidate"
    assert packet.to_dict()["claim_source_refs"][0]["verification_state"] == "verified"
    assert next(b for b in scoped.blocks if b.source_id == sid).quality == "unverified"


def test_verified_prose_span_does_not_inherit_unverified_layout_table():
    from proofops.application.evidence.retrieval import SearchResult, retrieve_evidence
    from proofops.application.ingest.graph_fusion import CandidateEdge, fuse_candidates

    from tests.acceptance.test_retrieval import RUN, corpus, pack

    base, claim = corpus()
    batch = base.candidates[0]
    parent = replace(batch.blocks[1], kind="table")
    graph = fuse_candidates(
        (
            replace(
                batch,
                blocks=(batch.blocks[0], parent),
                edges=(CandidateEdge("0", "1", "table_parent"),),
            ),
        ),
        tenant_id=TENANT,
    )
    from proofops.application.ingest.graph_fusion import QualityIssue

    table_id = next(b.source_id for b in graph.blocks if b.kind == "table")
    graph = replace(
        graph,
        issues=(
            QualityIssue(
                "layout", "invalid_bbox", 1, (table_id,), "open", "Unlocated layout table"
            ),
        ),
    )
    block = next(b for b in graph.blocks if b.kind == "paragraph")
    ref = replace(block.source_ref(), verification_state="verified")
    claim = replace(claim, source_refs=(ref,), source_quality="verified")
    scoped = span_verified_graph(graph, (ref,), "d" * 64)

    class Search:
        synthetic = True

        def search(self, *a, **k):
            return SearchResult(status="bounded")

    packet = retrieve_evidence(
        claim,
        scoped,
        Search(),
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="test-layout",
        rulepack=pack(),
        document_context={},
        token_counter=lambda text: len(text) // 4,
    )
    assert packet.to_dict()["status"] == "candidate"
    assert [c["source_scope"] for c in packet.to_dict()["evidence_candidates"]] == ["local_claim"]
    assert all(b.quality == "unverified" for b in scoped.blocks)

    from proofops.application.evidence.retrieval import evidence_issue_ids

    assert evidence_issue_ids(scoped, {block.source_id}, (ref,)) == frozenset()
    assert evidence_issue_ids(graph, {block.source_id}, (ref,)) == frozenset({"layout"})
    assert evidence_issue_ids(scoped, {table_id}, (ref,)) == frozenset({"layout"})
    damaged = replace(
        scoped,
        issues=(
            *scoped.issues,
            QualityIssue(
                "direct", "invalid_bbox", 1, (block.source_id,), "open", "Unreadable claim"
            ),
        ),
    )
    assert evidence_issue_ids(damaged, {block.source_id}, (ref,)) == frozenset({"direct"})
