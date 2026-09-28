"""AT-008: real local extraction over explicitly synthetic source graphs."""

import json
import unicodedata
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from uuid import UUID, uuid5

import jsonschema
import pytest
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "33333333-3333-4333-8333-333333333333"
MANIFEST = "44444444-4444-4444-8444-444444444444"
COMPOUND = (
    "당사는 2023년 Scope 1·2 배출량을 전년 대비 8% 감축했으며, "
    "2030년까지 2020년 대비 40% 감축을 목표로 합니다."
)


def graph_of(*texts):
    run = str(uuid5(UUID(MANIFEST), "synthetic-parser"))
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
        tuple(
            CandidateBlock(
                "paragraph",
                NativeSource(
                    VERSION,
                    MANIFEST,
                    run,
                    str(i),
                    i + 1,
                    None,
                    (10, 10, 590, 50),
                    "pdf_bottom_left_points",
                    text,
                    0,
                    len(text),
                ),
                PageGeometry(600, 800, 0, (0, 0, 600, 800)),
            )
            for i, text in enumerate(texts)
        ),
        synthetic=True,
    )
    return fuse_candidates((batch,), tenant_id=TENANT)


def discover(graph, **kwargs):
    from proofops.application.claims import ClaimScope, discover_atomic_claims
    from proofops_agent.extraction import SyntheticClaimExtractor

    return discover_atomic_claims(
        graph, ClaimScope(TENANT, VERSION, MANIFEST), extractor=SyntheticClaimExtractor(), **kwargs
    )


def test_compound_splits_without_borrowing_target_year_or_losing_source():
    graph = graph_of(COMPOUND)
    result = discover(graph)
    assert [claim.quote for claim in result.claims] == [
        "당사는 2023년 Scope 1·2 배출량을 전년 대비 8% 감축했으며,",
        "2030년까지 2020년 대비 40% 감축을 목표로 합니다.",
    ]
    assert len({claim.claim_id for claim in result.claims}) == 2
    for claim in result.claims:
        ref = claim.source_refs[0]
        assert COMPOUND[ref.char_start : ref.char_end] == claim.quote == ref.quote
        assert ref.bbox == (10, 750, 590, 790)
        assert ref.verification_state == "candidate"
        assert claim.tenant_id == TENANT and claim.source_quality == "unverified"
        assert claim.to_summary()["track"] is None
        assert claim.to_summary()["decision"] is None
    assert result.synthetic is True
    assert result.processed_source_ids == (graph.blocks[0].source_id,)


def test_full_has_no_topic_quota_and_preserves_repeated_claims_at_distinct_sources():
    result = discover(graph_of(*(["당사는 탄소 배출량을 8% 감축했습니다."] * 35)))
    assert len(result.claims) == 35
    assert len({claim.claim_id for claim in result.claims}) == 35
    assert len(result.processed_source_ids) == 35
    assert result.exclusions == ()


def test_non_claim_exclusions_and_unknown_are_distinct_and_located():
    result = discover(graph_of("온실가스란 열을 흡수하는 기체를 의미한다.", "해석할 수 없는 단편"))
    assert result.claims == ()
    assert [(item.reason, item.state) for item in result.exclusions] == [
        ("term_definition", "excluded"),
        ("unclassified", "unknown"),
    ]
    assert all(item.source_ref is not None for item in result.exclusions)


def test_subset_records_omitted_pages_and_full_refuses_hidden_page_filter():
    from proofops.application.claims import ClaimScope, discover_atomic_claims
    from proofops_agent.extraction import SyntheticClaimExtractor

    graph = graph_of(COMPOUND, "당사는 탄소 배출량을 10% 감축했습니다.")
    scope = ClaimScope(TENANT, VERSION, MANIFEST, "declared_subset", (2,))
    result = discover_atomic_claims(graph, scope, extractor=SyntheticClaimExtractor())
    assert len(result.claims) == 1 and result.claims[0].source_refs[0].page_num == 2
    assert result.exclusions[0].reason == "outside_declared_subset"
    with pytest.raises(ValueError):
        ClaimScope(TENANT, VERSION, MANIFEST, "full", (2,))
    with pytest.raises(ValueError):
        ClaimScope(TENANT, VERSION, MANIFEST, "declared_subset", (True,))


@pytest.mark.parametrize("field", ["tenant_id", "document_version_id", "parse_manifest_id"])
def test_graph_identity_mismatch_fails_before_extraction(field):
    graph = replace(graph_of(COMPOUND), **{field: str(uuid5(UUID(TENANT), "foreign"))})
    with pytest.raises(ValueError, match="identity"):
        discover(graph)


def test_conflict_unreadable_and_unlocated_are_never_excluded_as_non_claims():
    graph = graph_of(COMPOUND, COMPOUND, COMPOUND)
    graph = replace(
        graph,
        blocks=tuple(
            replace(block, quality=quality, winner=None if quality == "conflicted" else 0)
            for block, quality in zip(graph.blocks, ("conflicted", "unreadable", "unlocated"))
        ),
    )
    result = discover(graph)
    assert result.claims == ()
    assert [(item.reason, item.state) for item in result.exclusions] == [
        ("conflicted", "conflict"),
        ("unreadable", "unreadable"),
        ("unlocated", "unknown"),
    ]
    assert not result.processed_source_ids


def test_unicode_offsets_and_immutable_replay_keep_raw_text():
    raw = unicodedata.normalize("NFD", "당사는 탄소 배출량을 8% 감축했습니다.")
    graph = graph_of(raw)
    first, second = discover(graph), discover(graph)
    assert first == second
    claim = first.claims[0]
    assert claim.quote == raw and claim.source_refs[0].char_end == len(raw)
    with pytest.raises(FrozenInstanceError):
        claim.quote = "changed"
    changed = discover(graph_of("당사는 탄소 배출량을 9% 감축했습니다."))
    assert changed.claims[0].claim_id != claim.claim_id
    assert first.claims[0].quote == raw


def test_summary_obeys_existing_api_schema():
    schema = json.loads(Path("contracts/jsonschema/api_models.schema.json").read_text())
    validator = jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/ClaimSummary", "$defs": schema["$defs"]},
        format_checker=jsonschema.FormatChecker(),
    )
    for claim in discover(graph_of(COMPOUND)).claims:
        validator.validate(claim.to_summary())


@pytest.mark.parametrize("mutation", ["grade", "hallucination", "bool_offset", "overlap"])
def test_structured_boundary_rejects_grades_invented_quotes_and_bad_offsets(mutation):
    from proofops.application.claims import validate_extraction_response

    span = dict(char_start=0, char_end=2, quote="탄소", kind="claim", reason=None, topic_ids=[])
    payload = {"spans": [span]}
    if mutation == "grade":
        span["evidence_grade"] = "E3"
    elif mutation == "hallucination":
        span["quote"] = "감축"
    elif mutation == "bool_offset":
        span["char_start"] = False
    else:
        payload["spans"].append(dict(span))
    with pytest.raises(ValueError):
        validate_extraction_response(payload, "탄소")


def test_missing_extraction_spans_stay_unknown_and_timeout_keeps_other_blocks():
    from proofops.application.claims import ClaimScope, discover_atomic_claims
    from proofops_agent.extraction import StructuredClaimExtractor, SyntheticClaimExtractor

    local = SyntheticClaimExtractor()

    def respond(packet):
        if packet["untrusted_document_data"]["page_num"] == 1:
            raise TimeoutError("private document content must never become an exclusion reason")
        return {"spans": []}

    extractor = StructuredClaimExtractor(local.profile, respond)
    result = discover_atomic_claims(
        graph_of(COMPOUND, COMPOUND), ClaimScope(TENANT, VERSION, MANIFEST), extractor=extractor
    )
    assert result.claims == ()
    assert [(item.reason, item.state) for item in result.exclusions] == [
        ("extraction_failed", "unknown"),
        ("unprocessed_span", "unknown"),
    ]
    assert len(result.receipts) == 2
    assert result.receipts[1].response_sha256 is not None


def test_empty_source_and_pageless_quality_issues_are_retained():
    from proofops.application.ingest.graph_fusion import QualityIssue

    issue = QualityIssue(
        str(uuid5(UUID(MANIFEST), "issue")),
        "no_extractable_text",
        2,
        (),
        "unreadable",
        "No text on source page",
    )
    graph = replace(graph_of(""), issues=(issue,))
    result = discover(graph)
    assert result.claims == ()
    assert result.exclusions[0].reason == "empty_source"
    assert result.exclusions[0].state == "unknown"
    assert result.quality_issues == (issue,)


def test_structured_source_binding_and_extraction_revision_change_identity():
    from proofops.application.claims import ClaimScope, discover_atomic_claims
    from proofops_agent.extraction import StructuredClaimExtractor, SyntheticClaimExtractor

    local = SyntheticClaimExtractor()
    graph = graph_of(COMPOUND)
    scope = ClaimScope(TENANT, VERSION, MANIFEST)
    response = local.extract({"untrusted_document_data": {"text": COMPOUND}})
    adapter = StructuredClaimExtractor(local.profile, lambda packet: response)
    first = discover_atomic_claims(graph, scope, extractor=adapter)
    next_adapter = replace(adapter, profile=replace(adapter.profile, extraction_epoch=2))
    second = discover_atomic_claims(graph, scope, extractor=next_adapter)
    assert first.claims[0].claim_id != second.claims[0].claim_id
    assert first.receipts[0].packet_sha256 != second.receipts[0].packet_sha256
    response["spans"][0]["quote"] = "mutated"
    assert first.claims[0].quote.startswith("당사는 2023년")
    assert "mutated" not in first.receipts[0].raw_response_json


def test_actual_local_pdf_parser_to_claim_discovery(tmp_path):
    from io import BytesIO

    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.ingest.graph_fusion import ParserProfile
    from pypdf import PdfWriter

    from tests.acceptance.test_parsing import JAVA, pdf, source

    # Put claims in the page body: the parser intentionally removes running headers.
    writer = PdfWriter(clone_from=BytesIO(pdf()))
    for page in writer.pages:
        stream = page["/Contents"]
        stream.set_data(stream.get_data().replace(b"720 Td", b"400 Td"))
    output = BytesIO()
    writer.write(output)
    graph = OpenDataLoaderParser(tmp_path).parse(
        source(output.getvalue()), ParserProfile(MANIFEST, java_executable=JAVA), tenant_id=TENANT
    )
    result = discover(graph)
    assert len(result.claims) == 3
    assert {claim.quote for claim in result.claims} == {
        "Page 1 emissions 1234 tCO2e",
        "Page 2 emissions 1234 tCO2e",
        "Page 3 emissions 1234 tCO2e",
    }
    for claim in result.claims:
        ref = claim.source_refs[0]
        block = next(block for block in graph.blocks if block.source_id == ref.source_id)
        assert block.raw_text[ref.char_start : ref.char_end] == claim.quote
        assert claim.source_sha256 == graph.source_sha256 and ref.bbox is not None
    assert result.synthetic


def test_rejected_model_output_preserves_unknown_and_continues():
    from proofops.application.claims import (
        ClaimScope,
        ExtractionOutputError,
        discover_atomic_claims,
    )
    from proofops_agent.extraction import StructuredClaimExtractor, SyntheticClaimExtractor

    def respond(packet):
        if packet["untrusted_document_data"]["page_num"] == 1:
            raise ExtractionOutputError("untrusted content")
        return {"spans": []}

    result = discover_atomic_claims(
        graph_of(COMPOUND, COMPOUND),
        ClaimScope(TENANT, VERSION, MANIFEST),
        extractor=StructuredClaimExtractor(SyntheticClaimExtractor.profile, respond),
    )
    assert result.claims == ()
    assert [(x.reason, x.state) for x in result.exclusions] == [
        ("extraction_failed", "unknown"),
        ("unprocessed_span", "unknown"),
    ]
    assert [r.status for r in result.receipts] == ["failed", "processed"]


@pytest.mark.parametrize(
    ("text", "quote"),
    [
        ("지분투자 및 PPA 추진", "투자 및 PPA 추진"),
        ("1500톤 감축", "500톤 감축"),
        ("재생에너지 공급 확대", "재생에너지 공"),
        ("1,500톤 감축", "500톤 감축"),
        ("-20% 변화", "20% 변화"),
        ("감축률 12.5%", "감축률 12.5"),
    ],
)
def test_claim_quote_cannot_cut_inside_a_word_or_number(text, quote):
    from proofops.application.claims import validate_extraction_response

    start = text.index(quote)
    span = dict(
        char_start=start,
        char_end=start + len(quote),
        quote=quote,
        kind="claim",
        reason=None,
        topic_ids=[],
    )
    with pytest.raises(ValueError, match="token boundary"):
        validate_extraction_response({"spans": [span]}, text)
    full = dict(span, char_start=0, char_end=len(text), quote=text)
    assert validate_extraction_response({"spans": [full]}, text)[0].quote == text
