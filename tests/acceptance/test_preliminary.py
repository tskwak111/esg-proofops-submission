"""Synthetic corpus, real source-bound preliminary response validation; no API calls."""

from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from proofops.application.tagging import tracks
from proofops.domain.errors import DomainValidationError

from tests.acceptance.test_binding import DIMENSIONS, corpus
from tests.acceptance.test_citations import OTHER, TENANT


def context_corpus():
    """A real paragraph-with-heading-and-neighbor graph for context tests.

    Block 0 ("body") is a longer paragraph; the claim is a strict sub-span of
    it (the atomic quote), so block 0 itself is the expected parent_paragraph.
    Block 1 ("heading") is linked to block 0 via a real section_parent edge.
    Block 2 ("neighbor") is same-page prose near block 0, unlinked by any edge.
    Block 3 ("title_fragment") is a short, unrelated heading-only fragment on
    a different page with no section_parent link to the claim -- it must never
    appear as context for this claim.
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

    body_text = "회사A는 2025년 온실가스 배출량을 40% 감축했다고 발표했다."
    heading_text = "환경 성과"
    neighbor_text = "본 절은 2025년 성과를 다룬다."
    fragment_text = "부록"

    def block(kind, native_id, page, bbox, text):
        return CandidateBlock(
            kind,
            NativeSource(
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
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )

    candidates = (
        block("paragraph", "body", 1, (10, 700, 500, 730), body_text),
        block("heading", "heading", 1, (10, 750, 500, 780), heading_text),
        block("paragraph", "neighbor", 1, (10, 650, 500, 680), neighbor_text),
        block("heading", "fragment", 2, (10, 750, 500, 780), fragment_text),
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

    def find(native_id):
        return next(b for b in graph.blocks if b.sources[0].source_native_id == native_id)

    body, heading, neighbor, fragment = (
        find("body"),
        find("heading"),
        find("neighbor"),
        find("fragment"),
    )
    graph = replace(
        graph, edges=(CanonicalEdge(body.source_id, heading.source_id, "section_parent"),)
    )
    body = next(b for b in graph.blocks if b.source_id == body.source_id)

    atomic = "회사A는 2025년 온실가스 배출량을 40% 감축했다"
    start = body.normalized_text.index(atomic)
    claim_ref = replace(
        body.source_ref(),
        char_start=start,
        char_end=start + len(atomic),
        quote=atomic,
    )
    profile = ExtractionProfile("c" * 64, "d" * 64, pack().sha256, True)
    claim = Claim(
        OTHER,
        TENANT,
        VERSION,
        MANIFEST,
        graph.source_sha256,
        atomic,
        (claim_ref,),
        "verified",
        (),
        ExtractionReceipt(claim_ref.source_id, "f" * 64, None, None, profile, "ok"),
    )
    return (
        graph,
        claim,
        dict(body=body, heading=heading, neighbor=neighbor, fragment=fragment, atomic=atomic),
    )


def payload(claim):
    return dict(
        claim_id=claim.claim_id,
        track="performance",
        safe_harbor_category=None,
        track_confidence=0.8,
        dimensions={
            name: dict(
                source_index=0,
                start=claim.quote.index(text),
                end=claim.quote.index(text) + len(text),
                quote=text,
            )
            for name, text in DIMENSIONS.items()
        },
    )


def validate(claim, graph, response, **kwargs):
    from proofops.application.tagging.preliminary import validate_preliminary

    return validate_preliminary(claim, graph, response, tenant_id=kwargs.get("tenant_id", TENANT))


def test_preliminary_boundary_exists():
    # Fail on the missing feature, before importing its implementation.
    import importlib.util

    assert importlib.util.find_spec("proofops.application.tagging.preliminary") is not None


def test_verified_dimensions_preserve_literal_sources_without_grading():
    graph, claim, _ = corpus()
    before = asdict(graph), asdict(claim)
    result = validate(claim, graph, payload(claim))
    assert isinstance(result.track, tracks.TrackCandidate)
    assert result.track.track == "performance" and result.track_confidence == 0.8
    assert result.context.claim == claim
    for name, ref in result.context.dimensions.items():
        assert ref.quote == DIMENSIONS[name] and ref.verification_state == "verified"
        assert ref.bbox == claim.source_refs[0].bbox
    assert (asdict(graph), asdict(claim)) == before


def test_null_track_and_dimensions_remain_unresolved():
    graph, claim, _ = corpus()
    raw = payload(claim) | dict(
        track=None,
        track_confidence=None,
        dimensions=dict(entity=None, metric=None, reporting_period=None),
    )
    result = validate(claim, graph, raw)
    assert result.track is None and result.track_confidence is None
    assert result.context.dimensions == raw["dimensions"]


@pytest.mark.parametrize(
    "change",
    [
        {"grade": "E3"},
        {"claim_id": OTHER + "x"},
        {"track": "unknown"},
        {"track_confidence": True},
        {"track_confidence": float("nan")},
        {"track_confidence": 1.1},
        {"track_confidence": None},
        {"safe_harbor_category": "safe"},
        {"dimensions": {}},
        {"dimensions": {"entity": None, "metric": None, "reporting_period": None, "grade": None}},
    ],
)
def test_invalid_or_grading_fields_are_rejected(change):
    graph, claim, _ = corpus()
    with pytest.raises(DomainValidationError):
        validate(claim, graph, payload(claim) | change)


@pytest.mark.parametrize(
    "change",
    [
        {"source_index": True},
        {"source_index": 1},
        {"start": True},
        {"start": -1},
        {"end": 9999},
        {"quote": "다른 회사"},
        {"verification_state": "verified"},
    ],
)
def test_model_cannot_forge_or_relocate_source_refs(change):
    graph, claim, _ = corpus()
    raw = deepcopy(payload(claim))
    raw["dimensions"]["entity"].update(change)
    with pytest.raises(DomainValidationError):
        validate(claim, graph, raw)


@pytest.mark.parametrize("quality", ["unverified", "conflicted", "unreadable", "unlocated"])
def test_verified_flag_does_not_override_original_graph(quality):
    graph, claim, _ = corpus()
    graph = replace(graph, blocks=tuple(replace(b, quality=quality) for b in graph.blocks))
    with pytest.raises(DomainValidationError):
        validate(claim, graph, payload(claim))


def test_identity_and_claim_text_are_verified_before_model_input():
    from proofops.application.tagging.preliminary import preliminary_request

    graph, claim, _ = corpus()
    for forged in (
        replace(claim, quote="forged"),
        replace(claim, tenant_id=OTHER),
        replace(claim, source_sha256="0" * 64),
        replace(claim, source_quality="unverified"),
    ):
        with pytest.raises(DomainValidationError):
            preliminary_request(forged, graph, tenant_id=TENANT)
    with pytest.raises(DomainValidationError):
        validate(claim, graph, payload(claim), tenant_id=OTHER)


def test_request_is_bounded_to_atomic_sources_and_pins_provenance():
    from proofops.application.tagging.preliminary import preliminary_request
    from proofops.domain.provenance import canonical_hash

    graph, claim, _ = corpus()
    request = preliminary_request(claim, graph, tenant_id=TENANT)
    assert request["schema"] == "preliminary-source-quotes-v2"
    assert request["claim_sha256"] == canonical_hash(asdict(claim))
    assert request["graph_sha256"] == canonical_hash(asdict(graph))
    assert request["untrusted_document_data"]["sources"] == [dict(source_index=0, text=claim.quote)]
    assert "topic_ids" not in request["untrusted_document_data"]


def test_blank_dimension_is_not_verified_evidence():
    graph, claim, _ = corpus()
    raw = payload(claim)
    start = claim.quote.index(" ")
    raw["dimensions"]["entity"] = dict(source_index=0, start=start, end=start + 1, quote=" ")
    with pytest.raises(DomainValidationError):
        validate(claim, graph, raw)


def test_extreme_integer_confidence_rejected_at_boundary():
    graph, claim, _ = corpus()
    with pytest.raises(DomainValidationError):
        validate(claim, graph, payload(claim) | dict(track_confidence=10**500))


def test_unique_literal_quote_resolved_locally_without_model_offsets():
    graph, claim, _ = corpus()
    raw = payload(claim)
    for item in raw["dimensions"].values():
        del item["start"], item["end"]
    result = validate(claim, graph, raw)
    assert result.context.dimensions["entity"].char_start == claim.source_refs[0].char_start
    assert result.context.dimensions["metric"].quote == DIMENSIONS["metric"]


def test_ambiguous_quote_only_is_rejected_instead_of_choosing_first_match():
    graph, claim, _ = corpus()
    raw = payload(claim)
    raw["dimensions"]["entity"] = dict(source_index=0, quote="A")
    assert claim.quote.count("A") > 1
    with pytest.raises(DomainValidationError):
        validate(claim, graph, raw)


def test_quote_offsets_are_relative_to_the_atomic_span_not_the_whole_block():
    graph, claim, refs = corpus()
    start = refs[0].quote.index(DIMENSIONS["metric"])
    atomic = replace(refs[0], char_start=start, quote=refs[0].quote[start:])
    claim = replace(claim, source_refs=(atomic,), quote=atomic.quote)
    raw = dict(
        claim_id=claim.claim_id,
        track="performance",
        safe_harbor_category=None,
        track_confidence=0.5,
        dimensions=dict(
            entity=None,
            reporting_period=None,
            metric=dict(source_index=0, quote=DIMENSIONS["metric"]),
        ),
    )
    result = validate(claim, graph, raw)
    assert result.context.dimensions["metric"].char_start == start
    assert result.context.dimensions["metric"].char_end == start + len(DIMENSIONS["metric"])


def test_safe_harbor_category_is_retained_even_when_track_is_unresolved():
    graph, claim, _ = corpus()
    raw = payload(claim) | dict(
        track=None, track_confidence=None, safe_harbor_category="third_party_information"
    )
    result = validate(claim, graph, raw)
    assert result.track is None
    assert result.safe_harbor_category == "third_party_information"


# --- R03b: opt-in interpretation-only context (never indexable, never grading) ---


def test_omitting_include_context_reproduces_legacy_envelope_byte_for_byte():
    from proofops.application.tagging.preliminary import SCHEMA, preliminary_request

    graph, claim, fixture = context_corpus()
    default = preliminary_request(claim, graph, tenant_id=TENANT)
    explicit_off = preliminary_request(claim, graph, tenant_id=TENANT, include_context=False)
    assert default == explicit_off
    assert default["schema"] == SCHEMA
    assert set(default["untrusted_document_data"]) == {"sources"}
    assert "context_policy" not in default
    assert default["untrusted_document_data"]["sources"] == [
        dict(source_index=0, text=fixture["atomic"])
    ]


def test_include_context_surfaces_parent_paragraph_heading_and_neighbor():
    from proofops.application.tagging.preliminary import (
        CONTEXT_SCHEMA,
        CONTEXT_SYSTEM_SUFFIX,
        SYSTEM_PROMPT,
        preliminary_request,
    )
    from proofops.domain.provenance import canonical_hash

    graph, claim, fixture = context_corpus()
    request = preliminary_request(claim, graph, tenant_id=TENANT, include_context=True)
    assert request["schema"] == CONTEXT_SCHEMA
    # prompt_sha256 always pins the prompt actually rendered for this request.
    assert request["prompt_sha256"] == canonical_hash(SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX)
    data = request["untrusted_document_data"]
    assert set(data) == {"sources", "context_blocks", "omitted_source_ids"}
    roles = {block["role"]: block for block in data["context_blocks"]}
    assert roles["parent_paragraph"]["source_id"] == fixture["body"].source_id
    assert roles["parent_paragraph"]["text"] == fixture["body"].raw_text
    assert roles["heading"]["source_id"] == fixture["heading"].source_id
    assert roles["heading"]["text"] == fixture["heading"].raw_text
    assert roles["nearby"]["source_id"] == fixture["neighbor"].source_id
    # The unrelated cross-page fragment is never pulled in as context.
    assert fixture["fragment"].source_id not in {b["source_id"] for b in data["context_blocks"]}
    # Full provenance retained, not a bare index/text pair.
    for block in data["context_blocks"]:
        assert block["page_num"] == 1
        assert block["quality"] == "verified"
        assert isinstance(block["source_ref"], dict) and block["source_ref"]["source_id"]
    assert data["sources"] == [dict(source_index=0, text=fixture["atomic"])]


def test_ambiguous_multiple_section_parents_never_guess_a_heading():
    """Two distinct declared section_parent targets for the same claim source
    stay unresolved as a heading -- an arbitrary pick is never made."""
    from proofops.application.ingest.graph_fusion import CanonicalEdge
    from proofops.application.tagging.preliminary import preliminary_request

    graph, claim, fixture = context_corpus()
    graph = replace(
        graph,
        edges=(
            CanonicalEdge(
                fixture["body"].source_id, fixture["heading"].source_id, "section_parent"
            ),
            CanonicalEdge(
                fixture["body"].source_id, fixture["fragment"].source_id, "section_parent"
            ),
        ),
    )
    request = preliminary_request(claim, graph, tenant_id=TENANT, include_context=True)
    roles = {block["role"] for block in request["untrusted_document_data"]["context_blocks"]}
    assert "heading" not in roles
    # parent_paragraph (from the claim's own containing block) is unaffected.
    assert "parent_paragraph" in roles


def test_table_row_excluded_from_nearby_context():
    """table_row is a numeric-heavy kind like table/table_cell; it must never
    enter 'nearby' context even though it is not itself the excluded table."""
    from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
    from proofops.application.ingest.graph_fusion import (
        CandidateBatch,
        CandidateBlock,
        fuse_candidates,
    )
    from proofops.application.tagging.preliminary import preliminary_request
    from proofops.domain.documents import NativeSource, PageGeometry

    from tests.acceptance.test_citations import MANIFEST, RUN, VERSION
    from tests.acceptance.test_rules import pack

    body_text = "회사A는 2025년 배출량을 감축했다."
    row_text = "Scope 1 | 120 tCO2e"

    def block(kind, native_id, page, bbox, text):
        return CandidateBlock(
            kind,
            NativeSource(
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
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )

    candidates = (
        block("paragraph", "body2", 1, (10, 700, 500, 730), body_text),
        block("table_row", "row2", 1, (10, 650, 500, 680), row_text),
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
    body = next(b for b in graph.blocks if b.sources[0].source_native_id == "body2")

    claim_ref = body.source_ref()
    profile = ExtractionProfile("c" * 64, "d" * 64, pack().sha256, True)
    claim = Claim(
        OTHER,
        TENANT,
        VERSION,
        MANIFEST,
        graph.source_sha256,
        body_text,
        (claim_ref,),
        "verified",
        (),
        ExtractionReceipt(claim_ref.source_id, "f" * 64, None, None, profile, "ok"),
    )
    request = preliminary_request(claim, graph, tenant_id=TENANT, include_context=True)
    context_ids = {b["source_id"] for b in request["untrusted_document_data"]["context_blocks"]}
    row = next(b for b in graph.blocks if b.sources[0].source_native_id == "row2")
    assert row.source_id not in context_ids


def test_context_blocks_are_never_indexable_by_dimension_source_index():
    """Structural guard: a dimension's source_index only ever resolves against
    the claim's own verified sources tuple; context_blocks is a disjoint list
    with a disjoint context_index namespace, so a model response cannot point
    a dimension quote at heading/neighbor/parent-paragraph text even if it
    tried -- the only sources tuple that exists at validation time is the
    claim's own atomic quote."""
    graph, claim, fixture = context_corpus()
    raw = dict(
        claim_id=claim.claim_id,
        track="performance",
        safe_harbor_category=None,
        track_confidence=0.8,
        dimensions=dict(
            entity=dict(source_index=0, quote="회사A"),
            metric=None,
            reporting_period=dict(source_index=0, quote="2025년"),
        ),
    )
    result = validate(claim, graph, raw)
    assert result.context.dimensions["entity"].quote == "회사A"
    assert result.context.dimensions["reporting_period"].quote == "2025년"
    # A quote that exists ONLY in the heading/neighbor context text (never in
    # the claim's own atomic quote) cannot be resolved: index 0 is the only
    # valid source_index, and its text is the atomic claim quote alone.
    assert "성과" not in fixture["atomic"] and "성과" in fixture["heading"].normalized_text
    borrowed = raw | dict(
        dimensions=dict(entity=None, metric=None, reporting_period=None)
        | dict(entity=dict(source_index=0, quote="성과"))
    )
    with pytest.raises(DomainValidationError):
        validate(claim, graph, borrowed)
    # An out-of-range source_index (pretending context had its own indexable
    # slot) is rejected the same way an out-of-range index always was.
    out_of_range = raw | dict(
        dimensions=dict(entity=None, metric=None, reporting_period=None)
        | dict(entity=dict(source_index=1, quote="회사A"))
    )
    with pytest.raises(DomainValidationError):
        validate(claim, graph, out_of_range)


def test_title_or_incomplete_fragment_target_stays_unextracted_even_with_nearby_valid_claim():
    """Contrastive case: when the CLAIM ITSELF is a heading/title fragment
    (not the atomic quote used above), providing rich context about a nearby
    valid claim on the same page must not manufacture dimensions for the
    fragment: only its own single source (the fragment text) is indexable,
    so a model attempting to answer from context alone still fails the same
    literal-quote-must-exist-in-source_index-0 check as before context
    existed."""
    from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
    from proofops.domain.errors import DomainValidationError as DVE

    graph, _, fixture = context_corpus()
    from tests.acceptance.test_rules import pack

    fragment = fixture["fragment"]
    profile = ExtractionProfile("c" * 64, "d" * 64, pack().sha256, True)
    fragment_claim = Claim(
        OTHER,
        TENANT,
        fragment.sources[0].document_version_id,
        fragment.sources[0].parse_manifest_id,
        graph.source_sha256,
        fragment.normalized_text,
        (fragment.source_ref(),),
        "verified",
        (),
        ExtractionReceipt(fragment.source_id, "f" * 64, None, None, profile, "ok"),
    )
    raw = dict(
        claim_id=fragment_claim.claim_id,
        track="performance",
        safe_harbor_category=None,
        track_confidence=0.8,
        # A model incorrectly trying to borrow the neighbor claim's numbers.
        dimensions=dict(
            entity=None, metric=None, reporting_period=dict(source_index=0, quote="2025년")
        ),
    )
    with pytest.raises(DVE):
        validate(fragment_claim, graph, raw)
    # A null-dimensions response for the fragment is accepted (stays unresolved).
    result = validate(
        fragment_claim,
        graph,
        raw | dict(dimensions=dict(entity=None, metric=None, reporting_period=None)),
    )
    assert result.context.dimensions == dict(entity=None, metric=None, reporting_period=None)


def test_valid_short_atomic_target_remains_extractable_with_context_present():
    """Contrastive case: a valid short target's own dimension quotes keep
    resolving normally even when include_context=True was used to build the
    outbound request (context never blocks or dilutes a legitimate quote)."""
    from proofops.application.tagging.preliminary import preliminary_request

    graph, claim, fixture = context_corpus()
    request = preliminary_request(graph=graph, claim=claim, tenant_id=TENANT, include_context=True)
    assert request["untrusted_document_data"]["sources"][0]["text"] == fixture["atomic"]
    raw = dict(
        claim_id=claim.claim_id,
        track="performance",
        safe_harbor_category=None,
        track_confidence=0.9,
        dimensions=dict(
            entity=None,
            metric=dict(source_index=0, quote="온실가스 배출량"),
            reporting_period=None,
        ),
    )
    result = validate(claim, graph, raw)
    assert result.context.dimensions["metric"].quote == "온실가스 배출량"


def test_different_tenant_denied_before_any_context_lookup():
    graph, claim, _ = context_corpus()
    from proofops.application.tagging.preliminary import preliminary_request

    with pytest.raises(DomainValidationError):
        preliminary_request(claim, graph, tenant_id=OTHER, include_context=True)


def test_old_packet_unchanged_when_context_flag_absent_from_call_site():
    """Simulates an existing caller (e.g. live_tagging.py today) that never
    passes include_context: the resulting packet is identical to before this
    feature existed, proving the legacy path is untouched by the addition."""
    from proofops.application.tagging.preliminary import preliminary_request

    graph, claim, fixture = context_corpus()
    legacy_style_call = preliminary_request(claim, graph, tenant_id=TENANT)
    assert legacy_style_call == dict(
        schema="preliminary-source-quotes-v2",
        tenant_id=TENANT,
        claim_id=claim.claim_id,
        claim_sha256=legacy_style_call["claim_sha256"],
        graph_sha256=legacy_style_call["graph_sha256"],
        prompt_sha256=legacy_style_call["prompt_sha256"],
        untrusted_document_data=dict(sources=[dict(source_index=0, text=fixture["atomic"])]),
    )
