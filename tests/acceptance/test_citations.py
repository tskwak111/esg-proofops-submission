"""AT-012: synthetic original snapshots; real graph/geometry/citation functions."""

import json
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.errors import DomainValidationError

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
MANIFEST = "33333333-3333-4333-8333-333333333333"
RUN = "44444444-4444-4444-8444-444444444444"
OTHER = "55555555-5555-4555-8555-555555555555"


def snapshot(text="🌱 당사는 2030년까지 배출량 40% 감축", **source_changes):
    source = NativeSource(
        VERSION,
        MANIFEST,
        RUN,
        "synthetic-block",
        3,
        "1",
        (20, 30, 100, 60),
        "pdf_bottom_left_points",
        text,
        0,
        len(text),
    )
    candidate = CandidateBlock(
        "paragraph",
        replace(source, **source_changes),
        PageGeometry(800, 600, 90, (10, 20, 610, 820)),
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
        (candidate,),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    # Synthetic human-confirmed source quality, never automatic parser approval.
    graph = replace(graph, blocks=(replace(graph.blocks[0], quality="verified"),))
    return graph, graph.blocks[0].source_ref()


def verify(ref, graph, **kwargs):
    from proofops.application.evidence.citations import verify_source_ref

    return verify_source_ref(ref, graph, tenant_id=kwargs.get("tenant_id", TENANT))


def test_exact_partial_quote_round_trips_rotated_source_and_fixed_v1_schema():
    graph, ref = snapshot()
    raw = graph.blocks[0].raw_text
    start = raw.index("2030")
    ref = replace(ref, quote=raw[start : start + 4], char_start=start, char_end=start + 4)
    result = verify(ref, graph)
    assert result == replace(ref, verification_state="verified")
    assert result.bbox == (10, 10, 40, 90)
    assert ref.verification_state == "candidate"
    assert graph.candidates[0].synthetic is True
    schema = json.loads(Path("contracts/jsonschema/api_models.schema.json").read_text())
    validator = Draft202012Validator(
        {"$ref": "#/$defs/SourceRef", "$defs": schema["$defs"]},
        format_checker=FormatChecker(),
    )
    validator.validate(json.loads(json.dumps(asdict(result))))


@pytest.mark.parametrize(
    "changes",
    [
        {"quote": "🌱 당사는 2030년까지 배출량 41% 감축"},
        {"quote": "🌱 당사는 2040년까지 배출량 40% 감축"},
        {"quote": "배출량 40% 감축"},  # Exists, but not at the claimed offsets.
        {"quote": ""},
        {"char_end": 999},
        {"char_start": 1},  # Astral emoji is one Python code point, not two UTF-16 units.
        {"char_end": 0, "quote": ""},
        {"raw_text_sha256": "0" * 64},
        {"source_id": OTHER},
        {"document_version_id": OTHER},
        {"parse_manifest_id": OTHER},
        {"page_num": 1},  # Printed label is not a physical page.
        {"printed_page_label": "3"},
        {"bbox": (11, 10, 40, 90)},
        {"bbox": None},
        {"location_quality": "unlocated"},
        {"location_quality": "unreadable"},
    ],
)
def test_forged_verified_flag_cannot_bypass_original_snapshot_checks(changes):
    graph, ref = snapshot()
    forged = replace(ref, verification_state="verified", **changes)
    assert verify(forged, graph) == replace(forged, verification_state="rejected")


@pytest.mark.parametrize(
    ("raw", "quote"),
    [
        ("각 cafe\u0301", "각 café"),
        ("oﬃce\t Scope\u00a01\n  감축", "office Scope 1 감축"),
        ("eﬀect ﬁnal ﬂow ﬄ ſt ﬆ", "effect final flow ffl ſt st"),
    ],
)
def test_allowed_normalization_returns_original_quote_and_raw_offsets(raw, quote):
    graph, ref = snapshot(raw)
    normalized = replace(ref, quote=quote)
    result = verify(normalized, graph)
    assert result == replace(ref, verification_state="verified")
    assert result.raw_text_sha256 == sha256(raw.encode()).hexdigest()
    assert normalized.quote == quote  # Caller revision remains immutable.
    assert result.char_end == len(raw)


@pytest.mark.parametrize(
    ("raw", "quote"),
    [
        ("−40%", "40%"),
        ("1.00", "100"),
        ("1 00", "100"),
        ("Scope 1", "scope 1"),
        ("①", "1"),
        ("2023", "2024"),
        ("40%", "40 percentage points"),
        ("   \n", " "),
    ],
)
def test_normalization_never_erases_material_differences(raw, quote):
    graph, ref = snapshot(raw)
    assert verify(replace(ref, quote=quote), graph).verification_state == "rejected"


@pytest.mark.parametrize("quality", ["unverified", "conflicted", "unreadable", "unlocated"])
def test_unresolved_source_quality_is_not_promoted_or_changed(quality):
    graph, ref = snapshot()
    graph = replace(graph, blocks=(replace(graph.blocks[0], quality=quality),))
    assert verify(ref, graph).verification_state == "rejected"
    assert graph.blocks[0].quality == quality


def test_missing_location_and_winner_cannot_be_verified():
    graph, ref = snapshot(native_bbox=None)
    assert verify(ref, graph).verification_state == "rejected"
    assert ref.bbox is None
    graph, ref = snapshot()
    graph = replace(graph, blocks=(replace(graph.blocks[0], winner=None),))
    assert verify(ref, graph).verification_state == "rejected"


def test_tenant_is_required_and_cross_tenant_snapshot_is_denied():
    from proofops.application.evidence.citations import verify_source_ref

    graph, ref = snapshot()
    with pytest.raises(TypeError):
        verify_source_ref(ref, graph)
    with pytest.raises(DomainValidationError, match="tenant"):
        verify(ref, graph, tenant_id=OTHER)


@pytest.mark.parametrize("change", ["duplicate", "artifact", "candidate", "version"])
def test_inconsistent_snapshot_provenance_fails_closed(change):
    graph, ref = snapshot()
    if change == "duplicate":
        graph = replace(graph, blocks=graph.blocks * 2)
    elif change == "artifact":
        graph = replace(graph, source_sha256="c" * 64)
    elif change == "candidate":
        graph = replace(graph, candidates=())
    else:
        graph = replace(graph, document_version_id=OTHER)
    assert verify(ref, graph).verification_state == "rejected"


def test_nfc_normalized_offsets_are_mapped_by_existing_geometry_before_verification():
    graph, _ = snapshot("🌱 각 cafe\u0301")
    ref = graph.blocks[0].source_ref(normalized_char_start=2, normalized_char_end=3)
    assert (ref.char_start, ref.char_end, ref.quote) == (2, 5, "각")
    assert verify(ref, graph).verification_state == "verified"


def test_quote_hash_cannot_replace_full_raw_block_hash():
    graph, ref = snapshot()
    forged = replace(
        ref,
        quote="2030",
        char_start=6,
        char_end=10,
        raw_text_sha256=sha256(b"2030").hexdigest(),
    )
    assert verify(forged, graph).verification_state == "rejected"


def test_unselected_parser_candidate_cannot_supply_the_quote():
    graph, ref = snapshot()
    original = graph.blocks[0]
    losing = original.candidates[0]
    winning = replace(losing, source=replace(losing.source, raw_text=ref.quote + " revised"))
    graph = replace(
        graph,
        blocks=(replace(original, candidates=(winning, losing)),),
        candidates=(replace(graph.candidates[0], blocks=(winning,)),),
    )
    assert verify(ref, graph).verification_state == "rejected"


def test_untrusted_dictionaries_are_not_original_snapshots():
    graph, ref = snapshot()
    with pytest.raises(DomainValidationError, match="original canonical snapshot"):
        verify(ref, graph.to_dict())
    with pytest.raises(DomainValidationError, match="SourceRef"):
        verify(asdict(ref), graph)


def test_actual_local_pdf_parser_to_citation_verification(tmp_path):
    from io import BytesIO

    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.ingest.graph_fusion import ParserProfile
    from pypdf import PdfWriter

    from tests.acceptance.test_parsing import JAVA, pdf, source

    # Generated PDF body text and real Java parser, no model/parse-output mock.
    writer = PdfWriter(clone_from=BytesIO(pdf()))
    for page in writer.pages:
        stream = page["/Contents"]
        stream.set_data(stream.get_data().replace(b"720 Td", b"400 Td"))
    output = BytesIO()
    writer.write(output)
    graph = OpenDataLoaderParser(tmp_path).parse(
        source(output.getvalue()),
        ParserProfile(MANIFEST, java_executable=JAVA),
        tenant_id=TENANT,
    )
    block = next(b for b in graph.blocks if b.raw_text.strip() and b.bbox is not None)
    assert block.raw_text == "Page 1 emissions 1234 tCO2e"
    ref = block.source_ref()
    assert verify(ref, graph).verification_state == "rejected"  # Parser is unverified.
    # Explicit synthetic human-confirmation fixture, not a product approval path.
    approved = replace(
        graph,
        blocks=tuple(replace(b, quality="verified") if b == block else b for b in graph.blocks),
    )
    assert verify(ref, approved) == replace(ref, verification_state="verified")
    assert (
        verify(replace(ref, quote=ref.quote + " invented"), approved).verification_state
        == "rejected"
    )
