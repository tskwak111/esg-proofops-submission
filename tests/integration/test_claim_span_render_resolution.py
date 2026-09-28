"""Real fixture regression for the opt-in claim-span render-resolution wrapper.

Mirrors ``test_native_paragraph_typography.py``'s structure: a positive case
(the base verifier's rendered OCR leaves an otherwise fully-proven record
``rendered_quote_unresolved`` only because of a resolution-limited crop, and
the wrapper's own deterministic-scale reader resolves it exactly), a negative
case (a real content mismatch is never promoted, even though its crop is
also eligible for a retry), and a mixed-ref regression (two different refs
sharing one source_id -- one legitimately promotable, one altered -- must be
gated independently, never promoted together by source_id association).
"""

from dataclasses import replace
from hashlib import sha256

from proofops.application.evidence.citations import _normalized

from tests.acceptance.test_parsing import TENANT, candidate, pdf


def inputs(quote="emissions 1234 tCO2e", raw_text="Page 1 emissions 1234 tCO2e"):
    source = pdf()
    batch = replace(
        candidate("span", [("P", "paragraph", raw_text, (70, 710, 300, 740), ())]),
        source_sha256=sha256(source).hexdigest(),
    )
    from proofops.application.ingest.graph_fusion import fuse_candidates

    graph = fuse_candidates((batch,), tenant_id=TENANT)
    whole = graph.blocks[0].source_ref()
    start = raw_text.index(quote)
    ref = replace(whole, char_start=start, char_end=start + len(quote), quote=quote)
    return source, graph, ref


def test_wrapper_promotes_only_the_resolution_limited_render_gap(monkeypatch):
    from proofops.adapters.local import claim_source_verification as base
    from proofops.adapters.local import claim_span_render_resolution as wrapper
    from proofops.adapters.local import selected_cell_table_verification as cell_reader

    source, graph, ref = inputs()

    # Base verifier: fixed-scale whole-block render drops the space the way
    # the real KB "9.9억 원" -> "9.9억원" crop did.
    monkeypatch.setattr(
        base,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions1234 tCO2e"),
    )
    baseline = base.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    assert baseline["records"][0]["status"] == "unresolved"
    assert baseline["records"][0]["reason"] == "rendered_quote_unresolved"

    eligible = wrapper.eligible_render_resolution_sources(baseline)
    assert eligible == {ref.source_id}

    # Wrapper's own deterministic-scale reader (reused unchanged from
    # selected_cell_table_verification) resolves the same crop correctly.
    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text="Page 1 emissions 1234 tCO2e", scale=9),
    )
    result, proof = wrapper.apply_render_resolution(graph, source, (ref,), tenant_id=TENANT)
    record = result["records"][0]
    assert record["status"] == "verified"
    assert result["schema"] == wrapper.SCHEMA
    assert result["schema"] != baseline["schema"]
    assert result["base_records"] == baseline["records"]
    assert proof["promoted_source_ids"] == [ref.source_id]
    assert proof["promoted_record_count"] == 1
    assert proof["eligible_source_ids"] == [ref.source_id]
    assert proof["schema"] == "claim_span_render_resolution_proof_v1"

    # The base receipt/graph/verifier are byte-identical to the unwrapped path.
    assert base.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT) == baseline
    assert graph.blocks[0].quality == "unverified"

    # The admitted receipt pins the actual confirming retry evidence, not
    # only the separate proof: the same final "verified" status produced by
    # a DIFFERENT underlying retry image/scale must hash differently.
    assert result["render_retries"][ref.source_id]["rendered"]["scale"] == 9
    assert result["render_retries"][ref.source_id]["rendered_normalized"] == _normalized(
        "Page 1 emissions 1234 tCO2e"
    )
    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text="Page 1 emissions 1234 tCO2e", scale=42),
    )
    result_different_scale, _ = wrapper.apply_render_resolution(
        graph, source, (ref,), tenant_id=TENANT
    )
    assert result_different_scale["records"][0]["status"] == "verified"  # same final status
    assert result_different_scale["render_retries"][ref.source_id]["rendered"]["scale"] == 42
    assert result_different_scale["artifact_sha256"] != result["artifact_sha256"]


def test_wrapper_never_promotes_a_real_wrong_value_even_when_render_eligible(monkeypatch):
    """Negative case: the higher-resolution retry reads the crop correctly,
    but it disagrees with the claimed raw text -- a genuine content error,
    not a resolution artifact -- so it must stay unresolved."""
    from proofops.adapters.local import claim_source_verification as base
    from proofops.adapters.local import claim_span_render_resolution as wrapper
    from proofops.adapters.local import selected_cell_table_verification as cell_reader

    source, graph, ref = inputs()

    monkeypatch.setattr(
        base,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions1234 tCO2e"),
    )
    baseline = base.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    assert baseline["records"][0]["reason"] == "rendered_quote_unresolved"

    # The higher-resolution retry now clearly reads a DIFFERENT number.
    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text="Page 1 emissions 1235 tCO2e", scale=9),
    )
    result, proof = wrapper.apply_render_resolution(graph, source, (ref,), tenant_id=TENANT)
    record = result["records"][0]
    assert record["status"] == "unresolved"
    assert proof["promoted_source_ids"] == []
    assert proof["promoted_record_count"] == 0
    assert graph.blocks[0].quality == "unverified"


def test_mixed_valid_and_invalid_ref_sharing_one_source_id_are_gated_independently(monkeypatch):
    """Regression for the reviewed defect: two different refs pointing at the
    SAME source_id (same block, same retry render) but with different
    quotes -- one the real, legitimate quote and one an altered/invalid quote
    an attacker or bug could construct -- must not be promoted together just
    because they share a source_id. Only the record whose own quote uniquely
    matches both native and retry text may be promoted."""
    from proofops.adapters.local import claim_source_verification as base
    from proofops.adapters.local import claim_span_render_resolution as wrapper
    from proofops.adapters.local import selected_cell_table_verification as cell_reader

    source, graph, valid_ref = inputs()
    # A second ref at the SAME source_id/char range but with an altered quote
    # that does not actually occur in the block's raw text.
    invalid_ref = replace(valid_ref, quote="emissions 9999 tCO2e")

    monkeypatch.setattr(
        base,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions1234 tCO2e"),
    )
    baseline = base.attest_claim_spans(graph, source, (valid_ref, invalid_ref), tenant_id=TENANT)
    # The invalid ref's own quote never occurs in the native text, so the base
    # verifier already leaves it source_invalid/text_mismatch-shaped, not
    # rendered_quote_unresolved; confirm the two records differ before wrapping.
    assert baseline["records"][0]["ref"]["quote"] == "emissions 1234 tCO2e"
    assert baseline["records"][1]["ref"]["quote"] == "emissions 9999 tCO2e"

    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text="Page 1 emissions 1234 tCO2e", scale=9),
    )
    result, proof = wrapper.apply_render_resolution(
        graph, source, (valid_ref, invalid_ref), tenant_id=TENANT
    )
    by_quote = {r["ref"]["quote"]: r for r in result["records"]}
    assert by_quote["emissions 1234 tCO2e"]["status"] == "verified"
    assert by_quote["emissions 9999 tCO2e"]["status"] == "unresolved"
    assert proof["promoted_record_count"] == 1
    # Both records share one source_id, but only the valid one is promoted.
    assert len({r["ref"]["source_id"] for r in result["records"]}) == 1
    assert graph.blocks[0].quality == "unverified"


def test_wrapper_never_touches_records_the_base_verifier_rejected_for_other_reasons():
    """A geometry/clipping/text-mismatch hold (never reaching a rendered read)
    is not eligible and is never retried."""
    from proofops.adapters.local import claim_span_render_resolution as wrapper

    source, graph, ref = inputs()
    from proofops.adapters.local import claim_source_verification as base

    baseline = base.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    forced = dict(
        baseline,
        records=[dict(baseline["records"][0], status="unresolved", reason="text_mismatch")],
    )
    assert wrapper.eligible_render_resolution_sources(forced) == frozenset()


def test_eligible_sources_require_a_stored_reading():
    from proofops.adapters.local import claim_span_render_resolution as wrapper

    shaped = dict(
        records=[
            dict(ref=dict(source_id="a"), status="unresolved", reason="rendered_quote_unresolved")
        ],
        readings={},
    )
    assert wrapper.eligible_render_resolution_sources(shaped) == frozenset()


def test_replay_preserves_pre_existing_verified_spans_on_an_already_scoped_graph(monkeypatch):
    """Regression for the reviewed defect: the real R12 runtime graph is
    already a SpanVerifiedGraph with its own pre-existing verified_spans
    from an earlier, separate receipt. span_citations.span_verified_graph
    REPLACES verified_spans wholesale, so calling it with only this
    wrapper's newly-promoted refs would silently drop that prior scope.
    replay_render_resolution must union, not replace."""
    from proofops.adapters.local import claim_source_verification as base
    from proofops.adapters.local import claim_span_render_resolution as wrapper
    from proofops.adapters.local import selected_cell_table_verification as cell_reader
    from proofops.application.evidence import span_citations

    source, graph, ref = inputs()

    # Simulate "graph is already a SpanVerifiedGraph": an earlier, separate
    # receipt already scoped one unrelated span on this same block trusted,
    # exactly as replay_claim_spans would have already done earlier.
    pre_existing_ref = replace(ref, char_start=0, char_end=6, quote="Page 1")
    already_scoped = span_citations.span_verified_graph(
        graph, (replace(pre_existing_ref, verification_state="verified"),), "prior-receipt-sha"
    )
    assert len(already_scoped.verified_spans) == 1

    monkeypatch.setattr(
        base,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions1234 tCO2e"),
    )
    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text="Page 1 emissions 1234 tCO2e", scale=9),
    )
    result, proof = wrapper.apply_render_resolution(
        already_scoped, source, (ref,), tenant_id=TENANT
    )
    scoped, replay_proof = wrapper.replay_render_resolution(
        result, already_scoped, source, tenant_id=TENANT
    )
    assert replay_proof == proof
    # The prior span must still be present, AND the newly promoted one added.
    span_keys = {(s.source_id, s.char_start, s.char_end) for s in scoped.verified_spans}
    assert (
        pre_existing_ref.source_id,
        pre_existing_ref.char_start,
        pre_existing_ref.char_end,
    ) in span_keys
    assert len(scoped.verified_spans) == 2
    prior_check = span_citations.verify_source_ref(pre_existing_ref, scoped, tenant_id=TENANT)
    assert prior_check.verification_state == "verified"
    new_check = span_citations.verify_source_ref(ref, scoped, tenant_id=TENANT)
    assert new_check.verification_state == "verified"


def test_policy_pins_base_and_cell_reader_hashes():
    from proofops.adapters.local import claim_span_render_resolution as wrapper

    policy = wrapper.claim_span_render_resolution_policy()
    assert policy["schema"] == "claim_span_render_resolution_policy_v1"
    assert policy["base"]["schema"] == "claim_source_policy_v2"
    assert isinstance(policy["cell_reader_sha256"], str) and len(policy["cell_reader_sha256"]) == 64
    assert isinstance(policy["wrapper_sha256"], str) and len(policy["wrapper_sha256"]) == 64


def test_replay_scopes_a_span_verified_graph_never_a_whole_block_flip(monkeypatch):
    """Real production-shaped consumer path: replay_render_resolution must
    validate the receipt by recomputation and return a SpanVerifiedGraph
    whose verify_source_ref only reports "verified" for a ref inside an
    actually-promoted span -- never by flipping graph.blocks quality."""
    from proofops.adapters.local import claim_source_verification as base
    from proofops.adapters.local import claim_span_render_resolution as wrapper
    from proofops.adapters.local import selected_cell_table_verification as cell_reader
    from proofops.application.evidence import span_citations

    source, graph, ref = inputs()
    monkeypatch.setattr(
        base,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions1234 tCO2e"),
    )
    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text="Page 1 emissions 1234 tCO2e", scale=9),
    )
    result, proof = wrapper.apply_render_resolution(graph, source, (ref,), tenant_id=TENANT)
    assert proof["promoted_record_count"] == 1

    scoped, replay_proof = wrapper.replay_render_resolution(result, graph, source, tenant_id=TENANT)
    assert replay_proof == proof
    assert isinstance(scoped, span_citations.SpanVerifiedGraph)
    # The underlying block quality on the ORIGINAL graph is never touched.
    assert graph.blocks[0].quality == "unverified"
    assert scoped.blocks == graph.blocks

    verified_ref = span_citations.verify_source_ref(ref, scoped, tenant_id=TENANT)
    assert verified_ref.verification_state == "verified"

    # A DIFFERENT range on the same block that was never promoted must not be
    # silently verified just because some other span on that source_id was.
    from dataclasses import replace

    unrelated_range = replace(ref, char_start=0, char_end=6, quote="Page 1")
    unrelated_result = span_citations.verify_source_ref(unrelated_range, scoped, tenant_id=TENANT)
    assert unrelated_result.verification_state != "verified"

    # Tampering with the receipt must be rejected by recomputation, not trusted.
    tampered = dict(result, records=[dict(result["records"][0], status="verified")])
    tampered["records"][0]["reason"] = None
    import pytest

    with pytest.raises(ValueError):
        wrapper.replay_render_resolution(
            dict(tampered, schema="not-the-real-schema"), graph, source, tenant_id=TENANT
        )


def test_apply_to_discovery_only_promotes_claims_whose_every_ref_verifies(monkeypatch):
    from dataclasses import replace

    from proofops.adapters.local import claim_source_verification as base
    from proofops.adapters.local import claim_span_render_resolution as wrapper
    from proofops.adapters.local import selected_cell_table_verification as cell_reader
    from proofops.application.claims import Claim, ClaimDiscovery, ClaimScope, ExtractionReceipt

    source, graph, ref = inputs()
    monkeypatch.setattr(
        base,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions1234 tCO2e"),
    )
    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text="Page 1 emissions 1234 tCO2e", scale=9),
    )
    result, proof = wrapper.apply_render_resolution(graph, source, (ref,), tenant_id=TENANT)

    from proofops.application.claims import ExtractionProfile

    extraction = ExtractionReceipt(
        ref.source_id,
        "a" * 64,
        None,
        None,
        ExtractionProfile("b" * 64, "c" * 64, "d" * 64, True),
        "extracted",
    )
    resolvable = Claim(
        "c1",
        TENANT,
        graph.document_version_id,
        graph.parse_manifest_id,
        graph.source_sha256,
        ref.quote,
        (ref,),
        "unverified",
        (),
        extraction,
    )
    # A second, unrelated claim whose ref was never render-resolution-eligible.
    other_ref = replace(ref, char_start=0, char_end=6, quote="Page 1")
    unresolvable = Claim(
        "c2",
        TENANT,
        graph.document_version_id,
        graph.parse_manifest_id,
        graph.source_sha256,
        other_ref.quote,
        (other_ref,),
        "unverified",
        (),
        extraction,
    )
    scope = ClaimScope(TENANT, graph.document_version_id, graph.parse_manifest_id)
    discovery = ClaimDiscovery(
        scope,
        graph.source_sha256,
        (resolvable, unresolvable),
        (),
        (),
        (extraction,),
        True,
        (),
    )
    new_discovery, scoped, replay_proof = wrapper.apply_to_discovery(
        result, discovery, graph, source, tenant_id=TENANT
    )
    assert replay_proof == proof
    by_id = {c.claim_id: c for c in new_discovery.claims}
    assert by_id["c1"].source_quality == "verified"
    assert by_id["c2"].source_quality == "unverified"
