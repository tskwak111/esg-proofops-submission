"""AT-011 span admission: independently attested external paragraph spans.

A trusted SpanVerifiedGraph carries verified_spans replayed independently of the
claim. When an external retrieved paragraph stays unverified as a whole but one
of its sentences is independently attested, retrieval must admit exactly that
span, never the unverified paragraph remainder. Conflicted/unreadable, forged,
wrong-tenant, and expanded spans stay rejected. No grade/label/graph mutation.
"""

from dataclasses import replace

import pytest
from proofops.application.evidence.retrieval import independently_attested_span_refs
from proofops.application.evidence.span_citations import span_verified_graph

from tests.acceptance.test_citations import OTHER, TENANT
from tests.acceptance.test_retrieval import corpus, retrieve

# "2030 emissions target 40%" is retrieved but is not the claim's own block.
EXTERNAL_TEXT = "2030 emissions target 40%"
SPAN_QUOTE = "emissions target"


def external_block(graph):
    return next(b for b in graph.blocks if b.raw_text == EXTERNAL_TEXT)


def make_unverified_external(graph, *, quality="unverified"):
    """Set the external paragraph to a non-verified quality, claim stays verified."""
    target = external_block(graph)
    return replace(
        graph,
        blocks=tuple(
            replace(b, quality=quality) if b.source_id == target.source_id else b
            for b in graph.blocks
        ),
    )


def attested_span(graph, *, quote=SPAN_QUOTE):
    block = external_block(graph)
    raw = block.raw_text
    start = raw.index(quote)
    ref = block.source_ref()
    return replace(
        ref,
        quote=quote,
        char_start=start,
        char_end=start + len(quote),
        verification_state="verified",
    )


def scoped_graph(quality="unverified", quote=SPAN_QUOTE):
    graph, claim = corpus()
    graph = make_unverified_external(graph, quality=quality)
    span = attested_span(graph, quote=quote)
    return span_verified_graph(graph, (span,), "e" * 64), claim, span


def test_external_unverified_paragraph_yields_only_the_attested_span():
    scoped, claim, span = scoped_graph()
    data = retrieve(scoped, claim).to_dict()
    external_id = external_block(scoped).source_id
    candidate = next(
        (c for c in data["evidence_candidates"] if c["source_id"] == external_id), None
    )
    assert candidate is not None, "verified external span must be admitted as a candidate"
    quotes = [ref["quote"] for ref in candidate["source_refs"]]
    # Only the exact attested span, never the whole unverified paragraph.
    assert quotes == [SPAN_QUOTE]
    assert EXTERNAL_TEXT not in quotes
    assert all(ref["verification_state"] == "verified" for ref in candidate["source_refs"])
    assert candidate["source_scope"] == "global_bound"
    # No grade/label/binding fabricated; graph block quality untouched.
    assert all(b["state"] == "undetermined" for b in data["candidate_bindings"])
    assert external_block(scoped).quality == "unverified"


@pytest.mark.parametrize("quality", ["conflicted", "unreadable"])
def test_conflicted_or_unreadable_external_block_stays_blocked(quality):
    # The span verifier only promotes unverified paragraphs; other states never.
    scoped, claim, span = scoped_graph(quality=quality)
    data = retrieve(scoped, claim).to_dict()
    external_id = external_block(scoped).source_id
    assert all(c["source_id"] != external_id for c in data["evidence_candidates"])
    assert independently_attested_span_refs(scoped, external_id, tenant_id=TENANT) == ()


def test_forged_span_is_rejected_and_not_admitted():
    graph, claim = corpus()
    graph = make_unverified_external(graph)
    span = attested_span(graph)
    forged = replace(span, quote=span.quote.replace("target", "goals"))
    scoped = span_verified_graph(graph, (forged,), "e" * 64)
    external_id = external_block(scoped).source_id
    assert independently_attested_span_refs(scoped, external_id, tenant_id=TENANT) == ()
    data = retrieve(scoped, claim).to_dict()
    assert all(c["source_id"] != external_id for c in data["evidence_candidates"])


def test_wrong_tenant_span_is_not_admitted_under_run_tenant():
    graph, claim = corpus()
    graph = make_unverified_external(graph)
    span = attested_span(graph)
    scoped = span_verified_graph(graph, (span,), "e" * 64)
    external_id = external_block(scoped).source_id
    # The graph is tenant TENANT; verifying under OTHER must raise, never admit.
    with pytest.raises(Exception):
        independently_attested_span_refs(scoped, external_id, tenant_id=OTHER)


def test_expanded_span_beyond_attested_offsets_is_rejected():
    graph, claim = corpus()
    graph = make_unverified_external(graph)
    span = attested_span(graph)
    # Store the true attested span, but request a wider ref than was replayed.
    scoped = span_verified_graph(graph, (span,), "e" * 64)
    block = external_block(scoped)
    wider = replace(
        span,
        quote=block.raw_text,
        char_start=0,
        char_end=len(block.raw_text),
    )
    from proofops.application.evidence.span_citations import verify_source_ref

    assert verify_source_ref(wider, scoped, tenant_id=TENANT).verification_state == "rejected"
    data = retrieve(scoped, claim).to_dict()
    candidate = next(c for c in data["evidence_candidates"] if c["source_id"] == block.source_id)
    assert [ref["quote"] for ref in candidate["source_refs"]] == [SPAN_QUOTE]


def test_disjoint_verified_spans_are_admitted_deterministically():
    graph, claim = corpus()
    graph = make_unverified_external(graph)
    block = external_block(graph)
    raw = block.raw_text  # "2030 emissions target 40%"
    ref = block.source_ref()
    a_start = raw.index("2030")
    b_start = raw.index("40%")
    span_a = replace(
        ref,
        quote="2030",
        char_start=a_start,
        char_end=a_start + 4,
        verification_state="verified",
    )
    span_b = replace(
        ref,
        quote="40%",
        char_start=b_start,
        char_end=b_start + 3,
        verification_state="verified",
    )
    # Insert out of order; admission must be deterministic by char offsets.
    scoped = span_verified_graph(graph, (span_b, span_a), "e" * 64)
    admitted = independently_attested_span_refs(scoped, block.source_id, tenant_id=TENANT)
    assert [r.quote for r in admitted] == ["2030", "40%"]
    data = retrieve(scoped, claim).to_dict()
    candidate = next(c for c in data["evidence_candidates"] if c["source_id"] == block.source_id)
    quotes = [ref["quote"] for ref in candidate["source_refs"]]
    assert quotes == ["2030", "40%"]
    assert raw not in quotes


def test_no_span_verified_graph_admits_nothing_extra():
    graph, claim = corpus()
    graph = make_unverified_external(graph)
    external_id = external_block(graph).source_id
    assert independently_attested_span_refs(graph, external_id, tenant_id=TENANT) == ()
    data = retrieve(graph, claim).to_dict()
    assert all(c["source_id"] != external_id for c in data["evidence_candidates"])
    assert external_id in data["search_coverage"]["unprocessed_source_ids"]


def test_attested_span_ignores_unrelated_unverified_table_ancestor():
    """An external verified span must not fail on an unrelated unverified table ancestor.

    A paragraph carrying a table_parent edge to an unverified table would break
    the whole-block quality gate if ancestry were bundled. Because the attested
    atomic prose skips parser table ancestry, admission yields the span alone.
    """
    from proofops.application.ingest.graph_fusion import CandidateEdge, fuse_candidates

    from tests.acceptance.test_retrieval import corpus as _corpus  # keep import local

    base, claim = _corpus(table=True)
    batch = base.candidates[0]
    # Attach the standalone paragraph (block "2") to the table as an ancestor.
    graph = fuse_candidates(
        (replace(batch, edges=(*batch.edges, CandidateEdge("2", "table", "table_parent"))),),
        tenant_id=TENANT,
    )
    para = next(b for b in graph.blocks if b.raw_text == "emissions methodology scope")
    # Claim block verified; paragraph + its table ancestor stay unverified.
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, quality="verified")
            if b.raw_text == "We reduce emissions"
            else replace(b, quality="unverified")
            for b in graph.blocks
        ),
    )
    raw = para.raw_text
    quote = "methodology"
    start = raw.index(quote)
    span = replace(
        para.source_ref(),
        quote=quote,
        char_start=start,
        char_end=start + len(quote),
        verification_state="verified",
    )
    scoped = span_verified_graph(graph, (span,), "e" * 64)
    admitted = independently_attested_span_refs(scoped, para.source_id, tenant_id=TENANT)
    assert [r.quote for r in admitted] == [quote]
    data = retrieve(scoped, claim).to_dict()
    candidate = next(
        (c for c in data["evidence_candidates"] if c["source_id"] == para.source_id), None
    )
    assert candidate is not None
    quotes = [ref["quote"] for ref in candidate["source_refs"]]
    assert quotes == [quote]
    assert raw not in quotes
    # Table ancestor is neither promoted nor merged into the atomic prose candidate.
    assert all(ref["source_id"] == para.source_id for ref in candidate["source_refs"])
