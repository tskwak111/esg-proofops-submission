"""R06c: real claim dimensions feed assurance matching (no invented scope)."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from proofops.application.assurance import (
    claim_context_from_review_inputs,
    extract_assurance,
    match_assurance,
)
from proofops.application.evidence.binding import ClaimContext as EvidenceContext
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.application.ports.models import ModelBinding
from proofops.domain.documents import NativeSource, PageGeometry

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
MANIFEST = "33333333-3333-4333-8333-333333333333"
STATEMENT = "44444444-4444-4444-8444-444444444444"
RUN = "55555555-5555-4555-8555-555555555555"
CLAIM = "66666666-6666-4666-8666-666666666666"
BINDING = ModelBinding("synthetic-assurance", "assurance", True)

CLAIM_TEXT = "예시법인은 2025년 서울 사업장에서 Scope 1 배출량을 보고하였다."


def _graph():
    texts = {
        "claim": CLAIM_TEXT,
        "provider": "예시 보증기관",
        "standard": "ISAE 3000",
        "level": "제한적 보증",
        "period": "2025",
        "metric": "Scope 1",
        "entity": "예시법인",
        "facility": "서울 사업장",
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
    return graph, by_native


def _subref(claim_block, quote):
    text = claim_block.normalized_text
    start = text.index(quote)
    ref = claim_block.source_ref(
        normalized_char_start=start, normalized_char_end=start + len(quote)
    )
    assert ref.quote == quote
    return replace(ref, verification_state="verified")


def _inputs(graph, by_native, *, dims_override=None, claim_id=CLAIM):
    from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt

    claim_block = by_native["claim"]
    claim_ref = replace(claim_block.source_ref(), verification_state="verified")
    claim = Claim(
        claim_id=claim_id,
        tenant_id=TENANT,
        document_version_id=VERSION,
        parse_manifest_id=MANIFEST,
        source_sha256="a" * 64,
        quote=CLAIM_TEXT,
        source_refs=(claim_ref,),
        source_quality="verified",
        topic_ids=(),
        receipt=ExtractionReceipt(
            source_id=claim_block.source_id,
            packet_sha256="b" * 64,
            response_sha256=None,
            raw_response_json=None,
            profile=ExtractionProfile(
                model_sha256="c" * 64,
                prompt_sha256="d" * 64,
                rule_sha256="e" * 64,
                synthetic=True,
            ),
            status="committed",
        ),
    )
    dims = {
        "entity": _subref(claim_block, "예시법인"),
        "metric": _subref(claim_block, "Scope 1"),
        "reporting_period": _subref(claim_block, "2025년"),
        "facility": _subref(claim_block, "서울 사업장"),
    }
    if dims_override is not None:
        dims.update(dims_override)
    evidence = EvidenceContext(claim, dims)
    return SimpleNamespace(context=evidence, original=graph), claim


def _statement(graph, by_native):
    def full(name, field):
        ref = replace(by_native[name].source_ref(), verification_state="verified")
        return field, (ref,)

    tagged = dict(
        [
            full("provider", "provider"),
            full("standard", "standard_raw"),
            full("level", "level"),
            full("period", "reporting_period"),
            full("metric", "covered_metrics"),
            full("entity", "entities"),
            full("facility", "facilities"),
        ]
    )
    refs = tuple(r for items in tagged.values() for r in items)
    return extract_assurance(
        graph,
        refs,
        BINDING,
        tagged_fields=tagged,
        tenant_id=TENANT,
        statement_id=STATEMENT,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_id=1,
    )


def test_valid_dims_produce_metric_and_period_comparison():
    graph, by_native = _graph()
    inputs, _ = _inputs(graph, by_native)
    ctx = claim_context_from_review_inputs(
        inputs, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    assert ctx.metric == "Scope 1"
    assert ctx.reporting_period == "2025"  # exact YYYY년 -> YYYY
    assert ctx.entities == ("예시법인",)
    assert ctx.facilities == ("서울 사업장",)
    statement = _statement(graph, by_native)
    assert not statement.unresolved_fields
    result = match_assurance(statement, ctx)
    assert (result.metric_match, result.period_match, result.boundary_match) == (
        "yes",
        "yes",
        "yes",
    )
    assert result.status == "covered"


def test_period_mismatch_is_no_not_forced_covered():
    graph, by_native = _graph()
    claim_block = by_native["claim"]
    other = _subref(by_native["metric"], "Scope 1")  # out-of-claim but valid elsewhere
    _ = (claim_block, other)
    # Build dims with a different year inside the claim text is impossible
    # (claim says 2025년), so mutate the statement period instead.
    inputs, _ = _inputs(graph, by_native)
    ctx = claim_context_from_review_inputs(
        inputs, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    statement = _statement(graph, by_native)
    wrong = replace(statement, reporting_period="2024")
    result = match_assurance(wrong, ctx)
    assert result.period_match == "no"
    assert result.status == "not_covered"


def test_missing_and_unresolved_context_stays_unknown():
    graph, by_native = _graph()
    empty = claim_context_from_review_inputs(
        None, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    assert (empty.metric, empty.reporting_period, empty.entities, empty.facilities) == (
        None,
        None,
        (),
        (),
    )
    statement = _statement(graph, by_native)
    assert match_assurance(statement, empty).status == "undetermined"
    assert match_assurance(statement, empty).metric_match == "unknown"
    # Empty facility list is NOT universal coverage.
    inputs, _ = _inputs(graph, by_native, dims_override={"facility": None, "entity": None})
    ctx = claim_context_from_review_inputs(
        inputs, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    assert ctx.entities == () and ctx.facilities == ()
    assert match_assurance(statement, ctx).boundary_match == "unknown"
    assert match_assurance(statement, ctx).status == "undetermined"


def test_foreign_and_out_of_claim_refs_rejected():
    graph, by_native = _graph()
    claim_block = by_native["claim"]
    foreign_metric = replace(
        by_native["metric"].source_ref(), verification_state="verified"
    )  # valid graph ref but outside the atomic claim span
    assert foreign_metric.source_id != claim_block.source_id
    inputs, _ = _inputs(graph, by_native, dims_override={"metric": foreign_metric})
    ctx = claim_context_from_review_inputs(
        inputs, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    assert ctx.metric is None
    forged = replace(_subref(claim_block, "Scope 1"), quote="Scope 3")
    inputs2, _ = _inputs(graph, by_native, dims_override={"metric": forged})
    ctx2 = claim_context_from_review_inputs(
        inputs2, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    assert ctx2.metric is None
    statement = _statement(graph, by_native)
    assert match_assurance(statement, ctx).metric_match == "unknown"


def test_range_period_is_not_inferred_and_list_detail_share_helper():
    graph, by_native = _graph()
    # Non-year literal stays literal -> matcher keeps period unknown.
    inputs, _ = _inputs(graph, by_native)
    ctx = claim_context_from_review_inputs(
        inputs, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    assert ctx.reporting_period == "2025"
    from proofops.application.assurance import _normalize_reporting_period, _normalize_year

    assert _normalize_reporting_period("2023~2024") == "2023~2024"
    assert _normalize_reporting_period("FY24") == "FY24"
    assert _normalize_reporting_period("2025년") == "2025"
    assert _normalize_year("2024 년도") == "2024"
    assert _normalize_year("2024년도") == "2024"
    assert _normalize_year("2023~2024") is None
    assert _normalize_year("FY24") is None
    # List and detail call the same helper with the same inputs: identical result.
    again = claim_context_from_review_inputs(
        inputs, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    assert again == ctx
    statement = _statement(graph, by_native)
    assert match_assurance(statement, ctx).to_dict() == match_assurance(statement, again).to_dict()
    # Both routers resolve scope through the one shared helper.
    import inspect

    import proofops.adapters.local.analysis_store as store_mod
    import proofops_api.routers.claims as claims_mod

    assert "claim_context_from_review_inputs" in inspect.getsource(
        store_mod.LocalAnalysisStore.assurance
    )
    src = inspect.getsource(claims_mod.build_claims_router)
    assert src.count("claim_context_from_review_inputs") >= 1


def test_live_년도_suffix_same_and_different_year_at_match_time():
    """Live Kia case: stored statement literal is `2024 년도`; match-time only."""
    from proofops.application.assurance import ClaimContext

    graph, by_native = _graph()
    statement = _statement(graph, by_native)
    assert statement.reporting_period == "2025"  # stored raw field preserved
    live = replace(statement, reporting_period="2024 년도")
    assert live.reporting_period == "2024 년도"  # stored raw, not rewritten
    same = ClaimContext(TENANT, VERSION, CLAIM, "Scope 1", "2024", ("예시법인",), ("서울 사업장",))
    same_suffix = ClaimContext(
        TENANT, VERSION, CLAIM, "Scope 1", "2024년", ("예시법인",), ("서울 사업장",)
    )
    assert match_assurance(live, same).period_match == "yes"
    assert match_assurance(live, same_suffix).period_match == "yes"
    different = ClaimContext(
        TENANT, VERSION, CLAIM, "Scope 1", "2023", ("예시법인",), ("서울 사업장",)
    )
    assert match_assurance(live, different).period_match == "no"
    ranged = ClaimContext(
        TENANT, VERSION, CLAIM, "Scope 1", "2023~2024", ("예시법인",), ("서울 사업장",)
    )
    assert match_assurance(live, ranged).period_match == "unknown"


def test_explicit_period_exclusion_uses_same_exact_year_normalization():
    graph, by_native = _graph()
    inputs, _ = _inputs(graph, by_native)
    ctx = claim_context_from_review_inputs(
        inputs, tenant_id=TENANT, document_version_id=VERSION, claim_id=CLAIM
    )
    statement = replace(_statement(graph, by_native), excluded_periods=("2025 년도",))
    assert match_assurance(statement, ctx).status == "not_covered"
    unknown = replace(statement, excluded_periods=("2024~2025",))
    assert match_assurance(unknown, ctx).status == "undetermined"
    other = replace(statement, excluded_periods=("2024년도",))
    assert match_assurance(other, ctx).status == "covered"
