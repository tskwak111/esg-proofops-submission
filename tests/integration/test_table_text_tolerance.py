"""Explicit superscript line tolerance; historical parser identity stays unchanged."""

from dataclasses import asdict, replace
from io import BytesIO

import pytest
from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
from proofops.application.ingest.graph_fusion import ParserProfile, canonical_hash
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from tests.acceptance.test_parsing import JAVA, MANIFEST, TENANT, pdf, source


def test_optional_tolerance_preserves_legacy_identity_and_binds_explicit_value():
    profile = ParserProfile(MANIFEST)
    legacy = asdict(profile)
    legacy.pop("table_text_y_tolerance", None)
    legacy.pop("table_source_policy_sha256", None)
    legacy.pop("table_structure_repair", None)
    assert profile.invocation_snapshot() == legacy
    config = {k: v for k, v in legacy.items() if k not in {"parse_manifest_id", "physical_pages"}}
    assert profile.config_snapshot() == config
    assert profile.config_hash() == canonical_hash(config)
    assert (
        profile.config_hash() == "b1261fe4ba777eceb40b751ca9eda54d924c9c0931c44bd91fca51a06de48109"
    )
    for accepted in (0, 0.5):
        assert (
            replace(profile, table_text_y_tolerance=accepted).config_snapshot()[
                "table_text_y_tolerance"
            ]
            == accepted
        )
    tuned = replace(profile, table_text_y_tolerance=4)
    assert tuned.config_hash() != profile.config_hash()
    assert tuned.invocation_snapshot()["table_text_y_tolerance"] == 4
    assert ParserProfile(**tuned.invocation_snapshot()) == tuned


@pytest.mark.parametrize("value", [True, False, "4", -1, float("inf"), float("nan"), [], {}])
def test_invalid_tolerance_is_rejected(value):
    with pytest.raises(ValueError, match="tolerance"):
        ParserProfile(MANIFEST, table_text_y_tolerance=value)


def test_actual_superscript_extraction_and_immutable_replay(tmp_path):
    reader = PdfReader(BytesIO(pdf(table=True)))
    page = reader.pages[1]
    stream = DecodedStreamObject()
    stream.set_data(
        page.get_contents().get_data() + b"\n" + rb"BT /F1 12 Tf 110 619 Td (2\)) Tj ET"
    )
    page[NameObject("/Contents")] = stream
    writer = PdfWriter()
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    import pdfplumber

    with pdfplumber.open(BytesIO(output.getvalue())) as original:
        assert original.pages[0].find_tables()[0].extract()[1][0] == "2)\n2025"
    item = source(output.getvalue())
    parser = OpenDataLoaderParser(tmp_path)
    profile = ParserProfile(MANIFEST, java_executable=JAVA, table_text_y_tolerance=4)
    graph = parser.parse(item, profile, tenant_id=TENANT)
    assert any(
        block.source.source_native_id == "p1-t0-r1-c0" and block.source.raw_text == "2025 2)"
        for batch in graph.candidates
        if batch.parser_name == "pdfplumber"
        for block in batch.blocks
    )
    assert parser.load_verified(item, profile, tenant_id=TENANT) == graph
    with pytest.raises(ParseFailure):
        parser.load_verified(item, replace(profile, table_text_y_tolerance=None), tenant_id=TENANT)
