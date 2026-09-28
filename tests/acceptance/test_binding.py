"""AT-013: explicitly synthetic source/tag fixtures, real citation and binding code."""

from dataclasses import asdict, replace

import pytest
from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.errors import DomainValidationError

from tests.acceptance.test_citations import MANIFEST, OTHER, RUN, TENANT, VERSION
from tests.acceptance.test_rules import pack

DIMENSIONS = dict(
    entity="회사A",
    product="제품A",
    material="재활용 플라스틱",
    metric="함유비율",
    facility="공장A",
    scope="Scope 1",
    reporting_period="2025",
    boundary="국내",
)


def corpus(*, claim_dimensions=None, **other_dimensions):
    values = DIMENSIONS | (claim_dimensions or {})
    texts = [" | ".join(values.values()) + " | 40%"]
    texts.append(" | ".join((values | other_dimensions).values()) + " | 40%")
    candidates = tuple(
        CandidateBlock(
            "table_cell",
            NativeSource(
                VERSION,
                MANIFEST,
                RUN,
                str(i),
                1,
                None,
                (10, 10 + i * 50, 500, 40 + i * 50),
                "pdf_bottom_left_points",
                text,
                0,
                len(text),
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
            table_native_id="table-1",
            row_number=i,
            column_number=1,
        )
        for i, text in enumerate(texts)
    )
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
        candidates,
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    refs = tuple(
        next(b for b in graph.blocks if b.sources[0].source_native_id == str(i)).source_ref()
        for i in range(2)
    )
    profile = ExtractionProfile("c" * 64, "d" * 64, pack().sha256, True)
    claim = Claim(
        OTHER,
        TENANT,
        VERSION,
        MANIFEST,
        graph.source_sha256,
        refs[0].quote,
        (refs[0],),
        "verified",
        (),
        ExtractionReceipt(refs[0].source_id, "f" * 64, None, None, profile, "ok"),
    )
    return graph, claim, refs


def span(ref, text):
    start = ref.quote.index(text)
    return replace(
        ref,
        char_start=ref.char_start + start,
        char_end=ref.char_start + start + len(text),
        quote=text,
    )


def tags(ref, values=DIMENSIONS):
    return {name: span(ref, value) for name, value in values.items()}


def bind(graph, claim, ref, relation_tags, *, dimensions=None, element_id="P1", **kwargs):
    from proofops.application.evidence.binding import ClaimContext, accept_binding

    context = ClaimContext(claim, tags(claim.source_refs[0]) if dimensions is None else dimensions)
    return accept_binding(
        context,
        ref,
        relation_tags,
        original=graph,
        tenant_id=kwargs.pop("tenant_id", TENANT),
        rulepack=pack(),
        element_id=element_id,
        **kwargs,
    )


def test_same_page_other_product_ratio_is_rejected():
    graph, claim, refs = corpus(product="제품B")
    assert (
        bind(graph, claim, span(refs[1], "40%"), tags(refs[1], DIMENSIONS | {"product": "제품B"}))
        == "rejected"
    )


def test_verified_direct_ratio_is_accepted_without_mutating_provenance():
    graph, claim, refs = corpus()
    before = asdict(graph), asdict(claim), tags(refs[0])
    assert bind(graph, claim, span(refs[0], "40%"), tags(refs[0])) == "accepted"
    assert (asdict(graph), asdict(claim), tags(refs[0])) == before
    assert claim.receipt.profile.synthetic and graph.candidates[0].synthetic
    assert claim.source_refs[0].verification_state == "candidate"


@pytest.mark.parametrize("dimension", DIMENSIONS)
def test_each_mismatched_dimension_rejects_binding(dimension):
    values = DIMENSIONS | {dimension: "다른값"}
    graph, claim, refs = corpus(**{dimension: "다른값"})
    assert bind(graph, claim, span(refs[1], "40%"), tags(refs[1], values)) == "rejected"


@pytest.mark.parametrize("quality", ["unverified", "conflicted", "unreadable", "unlocated"])
def test_unresolved_source_is_undetermined_and_preserved(quality):
    graph, claim, refs = corpus()
    graph = replace(graph, blocks=tuple(replace(b, quality=quality) for b in graph.blocks))
    assert bind(graph, claim, refs[0], tags(refs[0])) == "undetermined"
    assert all(b.quality == quality for b in graph.blocks)


@pytest.mark.parametrize(
    "changes",
    [
        {"quote": "41%"},
        {"raw_text_sha256": "0" * 64},
        {"document_version_id": RUN},
        {"parse_manifest_id": RUN},
    ],
)
def test_forged_verified_ref_cannot_be_accepted(changes):
    graph, claim, refs = corpus()
    ref = replace(span(refs[0], "40%"), verification_state="verified", **changes)
    assert bind(graph, claim, ref, tags(refs[0])) == "rejected"


@pytest.mark.parametrize("side", ["claim", "evidence"])
def test_missing_dimension_stays_undetermined(side):
    graph, claim, refs = corpus()
    values = tags(refs[0]) | {"product": None}
    assert (
        bind(
            graph,
            claim,
            span(refs[0], "40%"),
            values if side == "evidence" else tags(refs[0]),
            dimensions=values if side == "claim" else None,
        )
        == "undetermined"
    )


def test_borrowing_matching_product_from_another_row_cannot_accept_ratio():
    graph, claim, refs = corpus(product="제품B")
    forged = tags(refs[1], DIMENSIONS | {"product": "제품B"}) | {"product": span(refs[0], "제품A")}
    assert bind(graph, claim, span(refs[1], "40%"), forged) == "rejected"


def test_forged_matching_dimension_quote_is_rejected():
    graph, claim, refs = corpus(product="제품B")
    forged = tags(refs[1], DIMENSIONS | {"product": "제품B"})
    forged["product"] = replace(forged["product"], quote="제품A")
    assert bind(graph, claim, span(refs[1], "40%"), forged) == "rejected"


def test_matching_table_row_context_is_accepted():
    graph, claim, refs = corpus()
    assert bind(graph, claim, span(refs[1], "40%"), tags(refs[1])) == "accepted"


def change_candidate(graph, source_id, **changes):
    block = next(b for b in graph.blocks if b.source_id == source_id)
    candidate = replace(block.candidates[0], **changes)
    return replace(
        graph,
        blocks=tuple(
            replace(b, candidates=(candidate,), kind=candidate.kind) if b == block else b
            for b in graph.blocks
        ),
        candidates=tuple(
            replace(
                batch,
                blocks=tuple(candidate if c == block.candidates[0] else c for c in batch.blocks),
            )
            for batch in graph.candidates
        ),
    )


@pytest.mark.parametrize("changes", [{"column_number": 2}, {"table_native_id": "other-table"}])
def test_wrong_table_or_year_column_cannot_supply_direct_number(changes):
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, **changes)
    assert bind(graph, claim, span(refs[1], "40%"), tags(refs[1])) == "rejected"


@pytest.mark.parametrize("element_id", ["P1", "G1", "G2", "G3", "G5"])
def test_global_numeric_and_year_evidence_is_rejected(element_id):
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, kind="paragraph", table_native_id=None)
    assert bind(graph, claim, refs[1], tags(refs[1]), element_id=element_id) == "rejected"


@pytest.mark.parametrize("element_id", ["G4", "P3", "P4", "M3"])
def test_matching_global_scope_method_or_assurance_can_bind(element_id):
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, kind="paragraph", table_native_id=None)
    assert bind(graph, claim, refs[1], tags(refs[1]), element_id=element_id) == "accepted"


def test_ambiguous_explicit_link_remains_undetermined():
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, kind="paragraph", table_native_id=None)
    assert bind(graph, claim, refs[1], tags(refs[1]), element_id="G6") == "undetermined"


def test_cross_tenant_claim_or_graph_is_denied():
    graph, claim, refs = corpus()
    for bad_graph, bad_claim in (
        (replace(graph, tenant_id=RUN), claim),
        (graph, replace(claim, tenant_id=RUN)),
    ):
        with pytest.raises(DomainValidationError, match="identity|tenant"):
            bind(bad_graph, bad_claim, refs[0], tags(refs[0]))


def test_grades_and_unsourced_dimension_values_are_invalid_input():
    graph, claim, refs = corpus()
    for invalid in (tags(refs[0]) | {"grade": "E3"}, tags(refs[0]) | {"product": "제품A"}):
        with pytest.raises(DomainValidationError):
            bind(graph, claim, refs[0], invalid)


def test_same_block_different_atomic_claim_does_not_become_direct_evidence():
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[0].source_id, kind="paragraph", table_native_id=None)
    atomic = replace(
        claim.source_refs[0],
        char_end=claim.quote.index(" | 40%"),
        quote=claim.quote.split(" | 40%")[0],
    )
    claim = replace(claim, quote=atomic.quote, source_refs=(atomic,))
    assert bind(graph, claim, span(refs[0], "40%"), tags(refs[0])) == "rejected"


@pytest.mark.parametrize("field", ["row_number", "column_number"])
@pytest.mark.parametrize("value", [None, True, -1])
def test_missing_or_invalid_table_coordinates_stay_undetermined(field, value):
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, **{field: value})
    assert bind(graph, claim, span(refs[1], "40%"), tags(refs[1])) == "undetermined"


def test_real_retrieval_packet_to_binding_preserves_candidate_revision():
    from proofops.application.evidence.retrieval import retrieve_evidence
    from proofops.domain.values import SourceRef

    from tests.acceptance.test_retrieval import SyntheticSearch

    graph, claim, refs = corpus(product="제품B")
    for ref in refs:
        graph = change_candidate(graph, ref.source_id, kind="paragraph", table_native_id=None)
    # Explicit synthetic token counter, not a production model tokenizer.
    packet = retrieve_evidence(
        claim,
        graph,
        SyntheticSearch(graph),
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="synthetic-1",
        rulepack=pack(),
        document_context={},
        token_counter=lambda text: len(text) // 4,
    )
    data = packet.to_dict()
    local = next(c for c in data["evidence_candidates"] if c["source_scope"] == "local_claim")
    source = SourceRef(**local["source_refs"][0])
    assert bind(graph, claim, source, tags(source)) == "accepted"
    assert (
        bind(
            graph, claim, refs[1], tags(refs[1], DIMENSIONS | {"product": "제품B"}), element_id="P3"
        )
        == "rejected"
    )
    assert packet.to_dict() == data
    assert all(b["state"] == "undetermined" for b in data["candidate_bindings"])


def test_cross_source_empty_or_missing_required_roles_never_accept():
    graph, claim, refs = corpus()
    assert bind(graph, claim, refs[1], {}, dimensions={}) == "undetermined"
    assert bind(graph, claim, refs[1], tags(refs[1]) | {"reporting_period": None}) == "undetermined"


def test_explicit_same_column_header_edge_is_required():
    from proofops.application.ingest.graph_fusion import CanonicalEdge

    graph, claim, refs = corpus()
    values = tags(refs[1]) | {"reporting_period": span(refs[0], "2025")}
    assert bind(graph, claim, span(refs[1], "40%"), values) == "rejected"
    graph = replace(
        graph, edges=(CanonicalEdge(refs[1].source_id, refs[0].source_id, "table_parent"),)
    )
    assert bind(graph, claim, span(refs[1], "40%"), values) == "accepted"


def test_same_row_year_from_wrong_column_cannot_be_borrowed():
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, row_number=0, column_number=2)
    values = tags(refs[0]) | {"reporting_period": span(refs[1], "2025")}
    assert bind(graph, claim, span(refs[0], "40%"), values) == "rejected"


def test_same_row_product_anchor_can_bind_its_value_cell():
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, row_number=0, column_number=2)
    values = tags(refs[0]) | {"product": span(refs[1], "제품A")}
    assert bind(graph, claim, span(refs[0], "40%"), values) == "accepted"


def test_product_header_must_cover_entire_merged_value_row_span():
    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[0].source_id, row_span=2)
    graph = change_candidate(graph, refs[1].source_id, row_number=0, column_number=2)
    values = tags(refs[0]) | {"product": span(refs[1], "제품A")}
    assert bind(graph, claim, span(refs[0], "40%"), values) == "rejected"
    graph = change_candidate(graph, refs[1].source_id, row_span=2)
    assert bind(graph, claim, span(refs[0], "40%"), values) == "accepted"


def test_year_header_must_cover_entire_merged_value_column_span():
    from proofops.application.ingest.graph_fusion import CanonicalEdge

    graph, claim, refs = corpus()
    graph = change_candidate(graph, refs[1].source_id, column_span=2)
    graph = replace(
        graph, edges=(CanonicalEdge(refs[1].source_id, refs[0].source_id, "table_parent"),)
    )
    values = tags(refs[1]) | {"reporting_period": span(refs[0], "2025")}
    assert bind(graph, claim, span(refs[1], "40%"), values) == "rejected"
    graph = change_candidate(graph, refs[0].source_id, column_span=2)
    assert bind(graph, claim, span(refs[1], "40%"), values) == "accepted"


def test_parallel_binding_checks_preserve_shared_revisions():
    from concurrent.futures import ThreadPoolExecutor

    from proofops.application.evidence.binding import ClaimContext, accept_binding

    graph, claim, refs = corpus(product="제품B")
    dimensions = tags(refs[0])
    context = ClaimContext(claim, dimensions)
    dimensions.clear()  # Constructor snapshots caller-owned mapping.
    snapshot = asdict(graph), asdict(claim)
    rulepack = pack()

    def evaluate(index):
        values = DIMENSIONS if index == 0 else DIMENSIONS | {"product": "제품B"}
        return accept_binding(
            context,
            span(refs[index], "40%"),
            tags(refs[index], values),
            original=graph,
            tenant_id=TENANT,
            rulepack=rulepack,
            element_id="P1",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(evaluate, [0, 1, 0, 1])) == [
            "accepted",
            "rejected",
            "accepted",
            "rejected",
        ]
    assert (asdict(graph), asdict(claim)) == snapshot


def test_matching_metric_text_tagged_as_period_stays_undetermined():
    graph, claim, refs = corpus()
    expected = tags(refs[0])
    actual = tags(refs[1])
    expected["reporting_period"] = expected["metric"]
    actual["reporting_period"] = actual["metric"]
    assert bind(graph, claim, refs[1], actual, dimensions=expected) == "undetermined"


@pytest.mark.parametrize("case", ["inside", "outside", "overlap", "malformed", "escaped_role"])
def test_scoped_relation_roles_cannot_escape_their_atomic_span(case):
    from proofops.application.evidence.binding import relation_tags_for

    _, _, refs = corpus()
    source = refs[0]
    value = span(source, "40%")
    key = f"{source.source_id}:{source.char_start}:{source.char_end}"
    relations = {key: tags(source)}
    expected = tags(source)
    if case == "outside":
        relations = {f"{source.source_id}:0:{value.char_start}": tags(source)}
        expected = None
    elif case == "overlap":
        value = span(source, DIMENSIONS["metric"])
        relations[f"{source.source_id}:0:{source.char_end - 1}"] = tags(source)
        expected = None
    elif case == "malformed":
        relations[f"{source.source_id}:bad:range"] = tags(source)
        expected = None
    elif case == "escaped_role":
        relations = {f"{source.source_id}:{value.char_start}:{value.char_end}": tags(source)}
        expected = None
    # Legacy whole-block roles must never override a scoped rejection.
    relations[source.source_id] = tags(source)
    assert relation_tags_for(value, relations) == expected
    assert relation_tags_for(value, {source.source_id: tags(source)}) == tags(source)


@pytest.mark.parametrize("element", ["M1", "G1", "P1"])
def test_verified_atomic_local_evidence_does_not_need_cross_source_join_keys(element):
    graph, claim, refs = corpus()
    local = span(refs[0], "40%" if element != "G1" else "2025")
    # This tests attribution only; semantic sufficiency is still element tagging.
    assert bind(graph, claim, local, {}, dimensions={}, element_id=element) == "accepted"
    assert bind(graph, claim, refs[1], {}, dimensions={}, element_id=element) == "undetermined"


def test_local_optional_role_is_still_verified_when_its_counterpart_is_null():
    graph, claim, refs = corpus()
    local = span(refs[0], "40%")
    forged = replace(span(refs[0], "회사A"), quote="forged")
    assert bind(graph, claim, local, {"entity": forged}, dimensions={}) == "rejected"
    # A matching word from another source must not be borrowed as a local role.
    graph = change_candidate(graph, refs[1].source_id, table_native_id=None)
    assert (
        bind(graph, claim, local, {"entity": span(refs[1], "회사A")}, dimensions={}) == "rejected"
    )


@pytest.mark.parametrize(
    "role,quote,expected",
    [("entity", "제품A", "rejected"), ("reporting_period", "함유비율", "undetermined")],
)
def test_direct_local_identity_does_not_hide_conflicting_or_invalid_roles(role, quote, expected):
    graph, claim, refs = corpus()
    values = {role: span(refs[0], quote)}
    dimensions = {} if role == "reporting_period" else {"entity": span(refs[0], "회사A")}
    assert bind(graph, claim, span(refs[0], "40%"), values, dimensions=dimensions) == expected


@pytest.mark.parametrize(
    "axis,value,element_id",
    [
        ("boundary", "연결 기준", "P3"),
        ("boundary", "별도 환경 데이터", "P1"),
        ("boundary", "consolidated basis", "P3"),
        ("scope", "Scope 2", "P1"),
    ],
)
def test_omitted_explicit_axis_cannot_accept_cross_source_evidence(axis, value, element_id):
    graph, claim, refs = corpus(**{axis: value})
    expected, actual = tags(refs[0]), tags(refs[1], DIMENSIONS | {axis: value})
    expected.pop(axis)
    actual.pop(axis)
    assert (
        bind(graph, claim, span(refs[1], "40%"), actual, dimensions=expected, element_id=element_id)
        == "undetermined"
    )


@pytest.mark.parametrize("scope", ["Scope 1 & 2", "Scope 1+2", "Scope 1·2", "Scope 1·Scope 2"])
def test_incomplete_scope_span_cannot_hide_an_untagged_scope(scope):
    graph, claim, refs = corpus(scope=scope)
    # Both tag maps claim Scope 1, despite the evidence explicitly including Scope 2.
    assert bind(graph, claim, span(refs[1], "40%"), tags(refs[1])) == "undetermined"


@pytest.mark.parametrize("scope", ["Scope 1", "Scope 1+2", "Scope 1·2", "Scope 1·Scope 2"])
def test_explicit_scope_with_complete_matching_roles_still_binds(scope):
    values = DIMENSIONS | {"scope": scope}
    graph, claim, refs = corpus(claim_dimensions={"scope": scope})
    assert (
        bind(
            graph,
            claim,
            span(refs[1], "40%"),
            tags(refs[1], values),
            dimensions=tags(refs[0], values),
        )
        == "accepted"
    )
    # Literal containment does not require cross-source role extraction.
    assert bind(graph, claim, span(refs[0], "40%"), {}, dimensions={}) == "accepted"


@pytest.mark.parametrize("boundary_tag", [None, "국내", "별도"])
def test_consolidated_claim_never_accepts_separate_boundary_even_if_tags_omit_it(boundary_tag):
    expected_values = DIMENSIONS | {"boundary": "국내 연결 기준"}
    actual_values = DIMENSIONS | {"boundary": "국내 별도 기준"}
    graph, claim, refs = corpus(
        claim_dimensions={"boundary": expected_values["boundary"]},
        boundary=actual_values["boundary"],
    )
    expected, actual = tags(refs[0], expected_values), tags(refs[1], actual_values)
    if boundary_tag is None:
        expected.pop("boundary")
        actual.pop("boundary")
    else:
        expected["boundary"] = span(refs[0], "연결" if boundary_tag == "별도" else "국내")
        actual["boundary"] = span(refs[1], boundary_tag)
    result = bind(graph, claim, span(refs[1], "40%"), actual, dimensions=expected)
    assert result == ("rejected" if boundary_tag == "별도" else "undetermined")


@pytest.mark.parametrize("element_id", ["P1", "P3", "G4", "M3"])
def test_complete_consolidated_boundary_preserves_allowed_local_or_global_evidence(element_id):
    values = DIMENSIONS | {"boundary": "연결 기준"}
    graph, claim, refs = corpus(claim_dimensions={"boundary": "연결 기준"})
    expected, actual = tags(refs[0], values), tags(refs[1], values)
    expected["boundary"], actual["boundary"] = span(refs[0], "연결"), span(refs[1], "연결")
    if element_id != "P1":
        graph = change_candidate(graph, refs[1].source_id, kind="paragraph", table_native_id=None)
    assert (
        bind(graph, claim, refs[1], actual, dimensions=expected, element_id=element_id)
        == "accepted"
    )
