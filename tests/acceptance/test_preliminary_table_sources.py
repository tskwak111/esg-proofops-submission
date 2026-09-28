"""R12: table-structural claim sources. Synthetic corpus, real validators, no API calls.

Covers the association rules that make a table fragment judgeable without
fabricating anything: adjacent-row rejection, peer-value demotion, unverified
axes staying interpretation context, ambiguous multi-cell holds, and the
opt-in envelope/validator/transport boundaries.
"""

from dataclasses import replace

import pytest
from proofops.application.tagging.preliminary import (
    CONTEXT_SCHEMA,
    SCHEMA,
    TABLE_SCHEMA,
    preliminary_request,
    preliminary_table_request,
    validate_preliminary_table_sources,
)
from proofops.application.tagging.table_sources import table_structural_sources
from proofops.domain.errors import DomainValidationError

from tests.acceptance.test_citations import OTHER, TENANT


def table_corpus(*, verified_axes=True, second_page=False):
    """A real two-row indicator table with a two-row header band.

    Layout (row, column), spans shown where they matter::

        (1,1) 지표 rs=2   (1,2) 단위 rs=2   (1,3) 배출량 cs=2
                                           (2,3) 2024   (2,4) 2025
        (3,1) 온실가스 배출량 (3,2) tCO2e   (3,3) 1,000  (3,4) 900
        (4,1) 용수 사용량     (4,2) ton     (4,3) 5,000  (4,4) 4,800

    The claim is the atomic paragraph inside cell (3,4) -- the bare value
    ``900``. Every cell also owns an atomic paragraph child, which is what an
    existing run's paragraph-scoped source verification can actually attest.
    """
    from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
    from proofops.application.ingest.graph_fusion import (
        CandidateBatch,
        CandidateBlock,
        CanonicalEdge,
        fuse_candidates,
    )
    from proofops.domain.documents import NativeSource, PageGeometry

    from tests.acceptance.test_citations import MANIFEST, RUN, VERSION
    from tests.acceptance.test_rules import pack

    def native(native_id, page, bbox, text):
        return NativeSource(
            VERSION,
            MANIFEST,
            RUN,
            native_id,
            page,
            None,
            bbox,
            "pdf_bottom_left_points",
            text,
            0,
            len(text),
        )

    geometry = PageGeometry(600, 800, 0, (0, 0, 600, 800))

    def cell(native_id, bbox, text, row, column, row_span=1, column_span=1, page=1):
        return CandidateBlock(
            "table_cell",
            native(native_id, page, bbox, text),
            geometry,
            table_native_id="t1" if page == 1 else "t2",
            row_number=row,
            column_number=column,
            row_span=row_span,
            column_span=column_span,
        )

    def atom(native_id, bbox, text, page=1):
        return CandidateBlock("paragraph", native(native_id, page, bbox, text), geometry)

    cells = {
        "h_metric": ("지표", (10, 760, 150, 790), 1, 1, 2, 1),
        "h_unit": ("단위", (150, 760, 250, 790), 1, 2, 2, 1),
        "h_measure": ("배출량", (250, 775, 500, 790), 1, 3, 1, 2),
        "h_2024": ("2024", (250, 760, 380, 775), 2, 3, 1, 1),
        "h_2025": ("2025", (380, 760, 500, 775), 2, 4, 1, 1),
        "r1_metric": ("온실가스 배출량", (10, 730, 150, 760), 3, 1, 1, 1),
        "r1_unit": ("tCO2e", (150, 730, 250, 760), 3, 2, 1, 1),
        "r1_2024": ("1,000", (250, 730, 380, 760), 3, 3, 1, 1),
        "r1_2025": ("900", (380, 730, 500, 760), 3, 4, 1, 1),
        "r2_metric": ("용수 사용량", (10, 700, 150, 730), 4, 1, 1, 1),
        "r2_unit": ("ton", (150, 700, 250, 730), 4, 2, 1, 1),
        "r2_2024": ("5,000", (250, 700, 380, 730), 4, 3, 1, 1),
        "r2_2025": ("4,800", (380, 700, 500, 730), 4, 4, 1, 1),
    }
    candidates = []
    for name, (text, bbox, row, column, row_span, column_span) in cells.items():
        candidates.append(cell(name, bbox, text, row, column, row_span, column_span))
        candidates.append(atom(f"{name}-atom", bbox, text))
    # A same-shaped cell on another physical page: never a lineage for page 1.
    if second_page:
        candidates.append(cell("other_metric", (10, 730, 150, 760), "다른 지표", 3, 1, page=2))
        candidates.append(atom("other_metric-atom", (10, 730, 150, 760), "다른 지표", page=2))
    candidates.append(atom("loose", (10, 400, 500, 430), "표 밖의 짧은 문단."))

    batch = CandidateBatch(
        TENANT,
        VERSION,
        MANIFEST,
        "a" * 64,
        RUN,
        "synthetic",
        "1",
        "synthetic",
        "b" * 64,
        tuple(candidates),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)

    def find(native_id):
        return next(b for b in graph.blocks if b.sources[0].source_native_id == native_id)

    edges = []
    for name in cells:
        edges.append(
            CanonicalEdge(find(f"{name}-atom").source_id, find(name).source_id, "table_parent")
        )
    if second_page:
        edges.append(
            CanonicalEdge(
                find("other_metric-atom").source_id, find("other_metric").source_id, "table_parent"
            )
        )
    names = set(cells) | ({"other_metric"} if second_page else set())
    quality = {f"{name}-atom" for name in names} if verified_axes else {"r1_2025-atom"}
    graph = replace(
        graph,
        blocks=tuple(
            replace(
                block,
                quality=(
                    "verified"
                    if block.sources[0].source_native_id in quality | {"loose"}
                    else "unverified"
                ),
            )
            for block in graph.blocks
        ),
        edges=tuple(edges),
    )

    def block(native_id):
        return next(b for b in graph.blocks if b.sources[0].source_native_id == native_id)

    focal = block("r1_2025-atom")
    profile = ExtractionProfile("c" * 64, "d" * 64, pack().sha256, True)
    ref = focal.source_ref()
    claim = Claim(
        OTHER,
        TENANT,
        VERSION,
        MANIFEST,
        graph.source_sha256,
        ref.quote,
        (ref,),
        "verified",
        (),
        ExtractionReceipt(ref.source_id, "f" * 64, None, None, profile, "ok"),
    )
    return graph, claim, block


def axes(graph, claim):
    return table_structural_sources(graph, claim.source_refs, tenant_id=TENANT)


def test_row_and_column_headers_resolve_and_the_adjacent_row_is_rejected():
    graph, claim, _ = table_corpus()
    result = axes(graph, claim)
    assert result.resolved
    assert (result.focal_table_native_id, result.focal_row_number, result.focal_column_number) == (
        "t1",
        3,
        4,
    )
    offered = {(item.role, item.ref.quote) for item in result.verified + result.context_only}
    assert offered == {
        ("row_header", "온실가스 배출량"),
        ("row_header", "tCO2e"),
        ("column_header", "배출량"),
        ("column_header", "2025"),
        ("row_qualifier", "1,000"),
    }
    # The adjacent row's own metric, unit and values are never offered, and the
    # other year column's header is not the focal column's header.
    assert not {"용수 사용량", "ton", "5,000", "4,800", "2024"} & {
        item.ref.quote for item in result.verified + result.context_only
    }


def test_a_peer_value_in_the_same_row_is_a_qualifier_never_the_row_header():
    graph, claim, _ = table_corpus()
    peer = next(item for item in axes(graph, claim).verified if item.ref.quote == "1,000")
    assert (peer.role, peer.association) == ("row_qualifier", "row_covered")


def test_unverified_axis_cells_stay_interpretation_context_and_are_never_numbered():
    graph, claim, _ = table_corpus(verified_axes=False)
    result = axes(graph, claim)
    assert result.resolved
    assert result.verified == ()
    assert {item.ref.verification_state for item in result.context_only} == {"rejected"}
    envelope = preliminary_table_request(claim, graph, tenant_id=TENANT)
    sources = envelope["untrusted_document_data"]["sources"]
    assert sources == [{"source_index": 0, "text": "900"}]
    blocks = envelope["untrusted_document_data"]["context_blocks"]
    table_blocks = [b for b in blocks if b["role"].startswith("table_")]
    assert len(table_blocks) >= 2
    # The context_index sequence stays contiguous across two or more axes.
    assert [b["context_index"] for b in blocks] == list(range(len(blocks)))
    assert {b["quality"] for b in table_blocks} == {"unverified"}


def test_a_multi_source_claim_with_one_unresolved_source_holds_the_whole_claim():
    graph, claim, block = table_corpus()
    loose = block("loose").source_ref()
    widened = replace(
        claim,
        quote=claim.quote + " " + loose.quote,
        source_refs=(*claim.source_refs, loose),
    )
    assert not axes(graph, widened).resolved


@pytest.mark.parametrize("other_cell", ["r2_2025-atom", "r1_2024-atom"])
def test_distinct_cells_do_not_share_one_claims_axes(other_cell):
    graph, claim, block = table_corpus()
    other = block(other_cell).source_ref()
    widened = replace(
        claim, quote=claim.quote + " " + other.quote, source_refs=(*claim.source_refs, other)
    )
    assert not axes(graph, widened).resolved


def test_a_same_shaped_cell_on_another_page_is_not_a_lineage_or_an_axis():
    graph, claim, _ = table_corpus(second_page=True)
    result = axes(graph, claim)
    assert result.resolved
    assert "다른 지표" not in {item.ref.quote for item in result.verified + result.context_only}


def test_a_claim_outside_any_table_resolves_to_nothing_instead_of_guessing():
    graph, claim, block = table_corpus()
    loose = block("loose").source_ref()
    outside = replace(claim, quote=loose.quote, source_refs=(loose,))
    result = axes(graph, outside)
    assert (result.lineage, result.verified, result.context_only) == ("unresolved", (), ())


def test_the_legacy_and_context_envelopes_are_unchanged_byte_for_byte():
    graph, claim, _ = table_corpus()
    assert preliminary_request(claim, graph, tenant_id=TENANT)["schema"] == SCHEMA
    context = preliminary_request(claim, graph, tenant_id=TENANT, include_context=True)
    assert context["schema"] == CONTEXT_SCHEMA
    assert "table_policy" not in context
    assert all(
        set(entry) == {"source_index", "text"}
        for entry in context["untrusted_document_data"]["sources"]
    )


def test_the_table_envelope_appends_verified_axes_after_the_claims_own_sources():
    graph, claim, _ = table_corpus()
    envelope = preliminary_table_request(claim, graph, tenant_id=TENANT)
    sources = envelope["untrusted_document_data"]["sources"]
    assert envelope["schema"] == TABLE_SCHEMA
    assert envelope["table_policy"]["role_basis"] == "structural_layout_interpretation"
    assert sources[0] == {"source_index": 0, "text": "900"}
    assert [entry["source_index"] for entry in sources] == list(range(len(sources)))
    roles = {entry["text"]: entry["table_role"] for entry in sources[1:]}
    assert roles["온실가스 배출량"] == "row_header"
    assert roles["2025"] == "column_header"


def _response(**dimensions):
    return dict(
        claim_id=None,
        track=None,
        safe_harbor_category=None,
        track_confidence=None,
        dimensions={"entity": None, "metric": None, "reporting_period": None, **dimensions},
    )


def test_dimensions_may_quote_a_table_axis_and_keep_that_axis_real_provenance():
    graph, claim, block = table_corpus()
    response = _response(
        metric={"source_index": 1, "quote": "온실가스 배출량"},
        reporting_period={"source_index": 4, "quote": "2025"},
    )
    envelope = preliminary_table_request(claim, graph, tenant_id=TENANT)
    offered = envelope["untrusted_document_data"]["sources"]
    response["claim_id"] = claim.claim_id
    response["dimensions"]["metric"]["source_index"] = next(
        entry["source_index"] for entry in offered if entry["text"] == "온실가스 배출량"
    )
    response["dimensions"]["reporting_period"]["source_index"] = next(
        entry["source_index"] for entry in offered if entry["text"] == "2025"
    )
    result = validate_preliminary_table_sources(claim, graph, response, tenant_id=TENANT)
    metric = result.context.dimensions["metric"]
    period = result.context.dimensions["reporting_period"]
    assert metric.source_id == block("r1_metric-atom").source_id
    assert period.source_id == block("h_2025-atom").source_id
    assert (metric.verification_state, period.verification_state) == ("verified", "verified")
    assert result.track is None  # no grade, no label, no invented track


def test_an_index_the_server_never_offered_is_refused():
    graph, claim, _ = table_corpus()
    offered = preliminary_table_request(claim, graph, tenant_id=TENANT)
    count = len(offered["untrusted_document_data"]["sources"])
    response = _response(metric={"source_index": count, "quote": "온실가스 배출량"})
    response["claim_id"] = claim.claim_id
    with pytest.raises(DomainValidationError):
        validate_preliminary_table_sources(claim, graph, response, tenant_id=TENANT)


def test_an_unverified_axis_quote_cannot_become_a_dimension():
    graph, claim, _ = table_corpus(verified_axes=False)
    response = _response(metric={"source_index": 1, "quote": "온실가스 배출량"})
    response["claim_id"] = claim.claim_id
    with pytest.raises(DomainValidationError):
        validate_preliminary_table_sources(claim, graph, response, tenant_id=TENANT)
