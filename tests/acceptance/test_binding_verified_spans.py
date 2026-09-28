"""AT-013 span admission at binding: trusted verified atomic spans inside an
otherwise-unverified paragraph must be able to bind for an otherwise valid local
element, without promoting block quality, exposing the paragraph remainder,
trusting client verification flags, or relaxing any guard/criterion/grade.

The claim's own block intentionally stays ``unverified``; a SpanVerifiedGraph
replays an independently attested verified span that exactly contains the local
ref. Missing winner (conflicted), forged spans, ordinary unverified graphs,
cross-tenant, dimension conflicts and semantic mismatch remain blocked. Uses the
real citation/binding code with explicitly synthetic fixtures; no grade, label,
graph, or claim mutation occurs.
"""

from dataclasses import asdict, replace

import pytest
from proofops.application.evidence.span_citations import span_verified_graph, verify_source_ref
from proofops.domain.errors import DomainValidationError

from tests.acceptance.test_binding import DIMENSIONS, bind, corpus, span, tags
from tests.acceptance.test_citations import RUN, TENANT


def _unverified(graph, source_id, *, quality="unverified"):
    """Force a single block to a non-verified quality; the rest stay verified."""
    return replace(
        graph,
        blocks=tuple(
            replace(b, quality=quality) if b.source_id == source_id else b for b in graph.blocks
        ),
    )


def _whole_block_span(graph, source_id):
    """A verified span covering the entire canonical block (contains local refs)."""
    block = next(b for b in graph.blocks if b.source_id == source_id)
    ref = block.source_ref()
    return replace(ref, verification_state="verified")


def _scoped(graph, source_id, span_ref=None, *, quality="unverified"):
    unverified = _unverified(graph, source_id, quality=quality)
    attested = _whole_block_span(unverified, source_id) if span_ref is None else span_ref
    return span_verified_graph(unverified, (attested,), "e" * 64)


@pytest.mark.parametrize("element_id", ["M1", "P1"])
def test_attested_atomic_span_binds_for_valid_local_element(element_id):
    """A valid local element citing a verified atomic span inside an unverified
    paragraph is accepted, without promoting the block or leaking the remainder."""
    graph, claim, refs = corpus()
    scoped = _scoped(graph, refs[0].source_id)
    before = asdict(scoped), asdict(claim)
    local = span(refs[0], "40%")
    assert bind(scoped, claim, local, {}, dimensions={}, element_id=element_id) == "accepted"
    # No promotion, no mutation of the shared graph/claim revisions.
    unresolved_block = next(b for b in scoped.blocks if b.source_id == refs[0].source_id)
    assert unresolved_block.quality == "unverified"
    assert (asdict(scoped), asdict(claim)) == before


def test_unverified_remainder_outside_the_attested_span_stays_undetermined():
    """A trusted span covering only part of the block cannot lend verification to a
    ref that reaches outside it; the remainder stays unresolved, never accepted."""
    graph, claim, refs = corpus()
    block = next(b for b in graph.blocks if b.source_id == refs[0].source_id)
    raw = block.raw_text
    quote = DIMENSIONS["entity"]  # e.g. "회사A" near the start of the row
    start = raw.index(quote)
    partial = replace(
        block.source_ref(),
        quote=quote,
        char_start=start,
        char_end=start + len(quote),
        verification_state="verified",
    )
    scoped = _scoped(graph, refs[0].source_id, partial)
    # The "40%" ref lies outside the attested span -> unresolved, not accepted.
    assert bind(scoped, claim, span(refs[0], "40%"), {}, dimensions={}) == "undetermined"


def test_forged_span_cannot_lend_verification():
    """A stored span whose quote does not match the raw text fails the legacy
    verifier, so the enclosed ref stays undetermined (never accepted)."""
    graph, claim, refs = corpus()
    forged = replace(_whole_block_span(graph, refs[0].source_id), quote="forged text")
    scoped = _scoped(graph, refs[0].source_id, forged)
    assert bind(scoped, claim, span(refs[0], "40%"), {}, dimensions={}) == "undetermined"


def test_ordinary_unverified_graph_without_spans_stays_undetermined():
    """An unverified block on a plain CanonicalDocumentGraph (no verified_spans)
    cannot bind; behavior is unchanged from before the fix."""
    graph, claim, refs = corpus()
    unverified = _unverified(graph, refs[0].source_id)
    assert bind(unverified, claim, span(refs[0], "40%"), {}, dimensions={}) == "undetermined"
    assert all(
        b.quality == ("unverified" if b.source_id == refs[0].source_id else "verified")
        for b in unverified.blocks
    )


@pytest.mark.parametrize("quality", ["conflicted", "unreadable", "unlocated"])
def test_conflicted_or_unreadable_block_with_span_stays_blocked(quality):
    """The span verifier only rescues ``unverified`` paragraphs; other qualities
    (including conflicted, which has no winner) stay undetermined."""
    graph, claim, refs = corpus()
    scoped = _scoped(graph, refs[0].source_id, quality=quality)
    assert bind(scoped, claim, span(refs[0], "40%"), {}, dimensions={}) == "undetermined"


def test_missing_winner_stays_undetermined():
    """A block whose winner is None (conflicted) can never bind, span or not."""
    graph, claim, refs = corpus()
    scoped = _scoped(graph, refs[0].source_id)
    scoped = replace(
        scoped,
        blocks=tuple(
            replace(b, winner=None) if b.source_id == refs[0].source_id else b
            for b in scoped.blocks
        ),
    )
    assert bind(scoped, claim, span(refs[0], "40%"), {}, dimensions={}) == "undetermined"


def test_cross_tenant_span_graph_is_denied():
    """Verifying an attested span under a different run tenant must raise, not admit."""
    graph, claim, refs = corpus()
    scoped = _scoped(graph, refs[0].source_id)
    with pytest.raises(DomainValidationError, match="identity|tenant"):
        bind(scoped, replace(claim, tenant_id=RUN), span(refs[0], "40%"), {}, dimensions={})


def test_conflicting_local_role_still_rejected_over_attested_span():
    """The attested span does not relax role guards: a role whose quote is not the
    attested raw text cannot be verified, so it stays blocked (never accepted)."""
    graph, claim, refs = corpus()
    scoped = _scoped(graph, refs[0].source_id)
    forged_entity = replace(span(refs[0], "회사A"), quote="forged")
    # Forged role sits on an unverified block with no covering span -> unresolved,
    # never accepted; the guard is not relaxed by the local 40% span.
    assert (
        bind(scoped, claim, span(refs[0], "40%"), {"entity": forged_entity}, dimensions={})
        == "undetermined"
    )
    # A verified block with the same forged role is a hard rejection, unchanged.
    verified_graph, vclaim, vrefs = corpus()
    assert (
        bind(
            verified_graph,
            vclaim,
            span(vrefs[0], "40%"),
            {"entity": forged_entity},
            dimensions={},
        )
        == "rejected"
    )


def test_cross_source_semantic_mismatch_remains_blocked():
    """Even with an attested local span, cross-source numeric evidence with a
    product mismatch stays rejected; semantic dimensions are unchanged."""
    graph, claim, refs = corpus(product="제품B")
    scoped = _scoped(graph, refs[0].source_id)
    assert (
        bind(scoped, claim, span(refs[1], "40%"), tags(refs[1], DIMENSIONS | {"product": "제품B"}))
        == "rejected"
    )


def test_expanded_ref_beyond_attested_span_is_not_verified():
    """A ref wider than the replayed span is rejected by the span verifier itself,
    confirming the guard the binding fix relies on."""
    graph, _, refs = corpus()
    partial_quote = DIMENSIONS["entity"]
    block = next(b for b in graph.blocks if b.source_id == refs[0].source_id)
    start = block.raw_text.index(partial_quote)
    partial = replace(
        block.source_ref(),
        quote=partial_quote,
        char_start=start,
        char_end=start + len(partial_quote),
        verification_state="verified",
    )
    scoped = _scoped(graph, refs[0].source_id, partial)
    wider = replace(partial, quote=block.raw_text, char_start=0, char_end=len(block.raw_text))
    assert verify_source_ref(wider, scoped, tenant_id=TENANT).verification_state == "rejected"
