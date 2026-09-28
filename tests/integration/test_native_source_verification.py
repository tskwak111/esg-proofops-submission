import shutil
import sys
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import FOREIGN, TENANT, candidate, pdf


@pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None,
    reason="Positive rendered OCR requires macOS Swift + Apple Vision",
)
def test_native_attestation_replays_source_and_never_promotes_table_relationships():
    from proofops.adapters.local.source_verification import (
        attest_native_sources,
        replay_native_sources,
    )
    from proofops.application.evidence.citations import verify_source_ref

    source = pdf()
    batch = replace(
        candidate(
            "fixture",
            [
                # Keep the positive OCR fixture clear of the ambiguous CO2 glyphs.
                # Exact matching is unchanged; wrong text and table promotion remain rejected.
                ("good", "paragraph", "Page 1 emissions 1234", (70, 710, 199, 740), ()),
                ("wrong", "paragraph", "Page 1 emissions 1235 tCO2e", (70, 650, 300, 680), ()),
                ("cell", "table_cell", "1234", (400, 710, 440, 740), ()),
            ],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    receipt = attest_native_sources(graph, source, tenant_id=TENANT)
    verified = replay_native_sources(receipt, graph, source, tenant_id=TENANT)
    by_id = {b.candidates[0].source.source_native_id: b for b in verified.blocks}
    assert by_id["good"].quality == "verified"
    assert (
        verify_source_ref(by_id["good"].source_ref(), verified, tenant_id=TENANT).verification_state
        == "verified"
    )
    assert by_id["wrong"].quality == by_id["cell"].quality == "unverified"
    assert all(b.quality == "unverified" for b in graph.blocks)
    forged = deepcopy(receipt)
    forged["records"][0]["status"] = "made_up"
    with pytest.raises(ValueError):
        replay_native_sources(forged, graph, source, tenant_id=TENANT)
    with pytest.raises(ValueError):
        replay_native_sources(receipt, graph, source + b"changed", tenant_id=TENANT)
    with pytest.raises(ValueError):
        replay_native_sources(receipt, graph, source, tenant_id=FOREIGN)


@pytest.mark.parametrize("operation", [b"BT 3 Tr", b"BT"])
@pytest.mark.parametrize("geometry_mode", ["font", "glyph"])
def test_hidden_or_overpainted_native_text_is_not_verified(operation, geometry_mode):
    from io import BytesIO

    from proofops.adapters.local.source_verification import attest_native_sources
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf())))
    page = writer.pages[0]
    stream = DecodedStreamObject()
    content = page.get_contents().get_data().replace(b"BT", operation)
    if operation == b"BT":
        content += b" 1 1 1 rg 0 0 600 800 re f"
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    source = out.getvalue()
    batch = replace(
        candidate(
            "hidden",
            [
                ("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ()),
            ],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    assert (
        attest_native_sources(graph, source, tenant_id=TENANT, geometry_mode=geometry_mode)[
            "records"
        ][0]["status"]
        == "unresolved"
    )


@pytest.mark.parametrize("catalog_key", ["/AcroForm", "/OCProperties"])
def test_interactive_visibility_is_unresolved(catalog_key):
    from io import BytesIO

    from proofops.adapters.local.source_verification import attest_native_sources
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DictionaryObject, NameObject

    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf())))
    writer._root_object[NameObject(catalog_key)] = DictionaryObject()
    out = BytesIO()
    writer.write(out)
    source = out.getvalue()
    batch = replace(
        candidate(
            "interactive",
            [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    receipt = attest_native_sources(
        fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT
    )
    assert receipt["records"][0]["reason"] == "interactive_visibility_requires_review"
    assert receipt["records"][0]["status"] == "unresolved"


def test_missing_rendered_reader_never_approves_source(monkeypatch):
    from proofops.adapters.local import source_verification

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("swift unavailable")

    monkeypatch.setattr(source_verification.subprocess, "run", unavailable)
    source = pdf()
    batch = replace(
        candidate(
            "offline", [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    receipt = source_verification.attest_native_sources(
        fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT
    )
    record = receipt["records"][0]
    assert record["status"] == "unresolved"
    assert record["rendered"]["reason"] == "rendered_reader_unavailable"


def test_native_page_outside_document_is_rejected():
    from proofops.adapters.local.source_verification import attest_native_sources

    source = pdf()
    batch = candidate(
        "bad-page", [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
    )
    block = batch.blocks[0]
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=(replace(block, source=replace(block.source, physical_page=999)),),
    )
    with pytest.raises(ValueError, match="native page outside document"):
        attest_native_sources(fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT)


@pytest.mark.parametrize("inset,expected", [(0.0005, "verified"), (0.002, "unresolved")])
def test_bbox_rounding_tolerance_does_not_accept_real_clipping(monkeypatch, inset, expected):
    from proofops.adapters.local import source_verification

    calls = []

    def rendered(page, box):
        calls.append(box)
        return dict(status="read", text="Page 1 emissions 1234 tCO2e")

    monkeypatch.setattr(source_verification, "_rendered_text", rendered)
    source = pdf()
    batch = replace(
        candidate(
            "rounded",
            [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (72 + inset, 710, 300, 740), ())],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    record = source_verification.attest_native_sources(
        fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT
    )["records"][0]
    assert record["status"] == expected
    assert bool(calls) == (expected == "verified")
    if expected == "unresolved":
        assert record["reason"] == "clipped_or_rotated_words"


@pytest.mark.parametrize("form_kind", ["empty", "field", "xfa", "missing", "malformed"])
def test_empty_form_metadata_does_not_skip_native_and_rendered_checks(monkeypatch, form_kind):
    from io import BytesIO

    from proofops.adapters.local import source_verification
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import ArrayObject, DictionaryObject, NameObject, TextStringObject

    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf())))
    form = DictionaryObject({NameObject("/Fields"): ArrayObject()})
    if form_kind == "field":
        form[NameObject("/Fields")].append(DictionaryObject())
    elif form_kind == "xfa":
        form[NameObject("/XFA")] = ArrayObject()
    elif form_kind == "missing":
        del form[NameObject("/Fields")]
    elif form_kind == "malformed":
        form[NameObject("/Fields")] = TextStringObject("")
    writer._root_object[NameObject("/AcroForm")] = writer._add_object(form)
    out = BytesIO()
    writer.write(out)
    source = out.getvalue()
    batch = replace(
        candidate(
            "empty-form",
            [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    calls = []

    def rendered(page, box):
        calls.append(box)
        return dict(status="read", text="Page 1 emissions 1234 tCO2e")

    monkeypatch.setattr(source_verification, "_rendered_text", rendered)
    record = source_verification.attest_native_sources(
        fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT
    )["records"][0]
    assert record["status"] == ("verified" if form_kind == "empty" else "unresolved")
    assert bool(calls) == (form_kind == "empty")


@pytest.mark.parametrize("mode,expected", [("font", "unresolved"), ("glyph", "verified")])
def test_glyph_mode_resolves_displaced_font_boxes(monkeypatch, mode, expected):
    import pdfplumber
    from proofops.adapters.local import source_verification

    original = pdfplumber.page.Page.extract_words

    def displaced(page, **kwargs):
        words = original(page, **kwargs)
        return [dict(word, top=word["top"] + 20, bottom=word["bottom"] + 20) for word in words]

    monkeypatch.setattr(pdfplumber.page.Page, "extract_words", displaced)
    monkeypatch.setattr(
        source_verification,
        "_rendered_text",
        lambda page, box: dict(status="read", text="Page 1 emissions 1234 tCO2e"),
    )
    source = pdf()
    batch = replace(
        candidate(
            "displaced",
            [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    receipt = source_verification.attest_native_sources(
        graph, source, tenant_id=TENANT, geometry_mode=mode
    )
    assert receipt["records"][0]["status"] == expected
    if mode == "glyph":
        assert receipt["schema"] == "native_paragraph_attestation_v2"
        replayed = source_verification.replay_native_sources(
            receipt, graph, source, tenant_id=TENANT
        )
        assert replayed.blocks[0].quality == "verified"


@pytest.mark.parametrize("case", ["clipped", "hidden", "wrong_text", "unresolved_mapping"])
def test_glyph_mode_keeps_uncertain_sources_unresolved(monkeypatch, case):
    from proofops.adapters.local import source_verification

    monkeypatch.setattr(
        source_verification,
        "_rendered_text",
        lambda page, box: dict(
            status="read", text="" if case == "hidden" else "Page 1 emissions 1234 tCO2e"
        ),
    )
    if case == "unresolved_mapping":
        monkeypatch.setattr(
            source_verification, "native_word_ink_geometry", lambda *args: dict(status="unresolved")
        )
    source = pdf()
    batch = replace(
        candidate(
            "guarded",
            [
                (
                    "P",
                    "paragraph",
                    "Page 1 emissions 1235 tCO2e"
                    if case == "wrong_text"
                    else "Page 1 emissions 1234 tCO2e",
                    (78 if case == "clipped" else 70, 710, 300, 740),
                    (),
                )
            ],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    receipt = source_verification.attest_native_sources(
        fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT, geometry_mode="glyph"
    )
    assert receipt["records"][0]["status"] == "unresolved"


@pytest.mark.parametrize("inside", [False, True])
def test_unmapped_words_only_block_their_own_crop(monkeypatch, inside):
    from proofops.adapters.local import source_verification

    original = source_verification.native_word_ink_geometry

    def partial(*args):
        proof = original(*args)
        words = proof["matched_words"]
        victim = words[0] if inside else words[-1]
        proof["matched_words"] = [word for word in words if word != victim]
        proof["unresolved_word_indices"] = [victim["native_word_index"]]
        proof["status"] = "unresolved"
        return proof

    monkeypatch.setattr(source_verification, "native_word_ink_geometry", partial)
    monkeypatch.setattr(
        source_verification,
        "_rendered_text",
        lambda page, box: dict(status="read", text="Page 2 emissions 1234 tCO2e"),
    )
    source = pdf(table=True)
    batch = replace(
        candidate(
            "partial-map",
            [("P", "paragraph", "Page 2 emissions 1234 tCO2e", (70, 710, 300, 740), ())],
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    block = batch.blocks[0]
    batch = replace(batch, blocks=(replace(block, source=replace(block.source, physical_page=2)),))
    receipt = source_verification.attest_native_sources(
        fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT, geometry_mode="glyph"
    )
    assert receipt["records"][0]["status"] == ("unresolved" if inside else "verified")


def test_ocr_padding_preserves_crop_pixels_and_original_coordinates(monkeypatch):
    import json
    from pathlib import Path
    from types import SimpleNamespace

    from PIL import Image
    from proofops.adapters.local import source_verification

    monkeypatch.setattr(sys, "platform", "darwin")
    original = Image.new("RGB", (60, 60), "black")
    page = SimpleNamespace(
        width=20, height=20, to_image=lambda **kw: SimpleNamespace(original=original.copy())
    )
    seen = []

    def recognize(command, **kwargs):
        with Image.open(Path(command[-1])) as image:
            seen.append(image.copy())
        return SimpleNamespace(stdout=json.dumps({"text": "unchanged"}))

    monkeypatch.setattr(source_verification.subprocess, "run", recognize)
    result = source_verification._rendered_text(page, (2, 3, 12, 13), padding_px=6)
    assert result["pixel_bbox"] == [6, 9, 36, 39]
    assert result["padding_px"] == 6
    assert seen[0].size == (42, 42)
    assert seen[0].getpixel((0, 0)) == (255, 255, 255)
    assert seen[0].crop((6, 6, 36, 36)).tobytes() == original.crop((6, 9, 36, 39)).tobytes()


@pytest.mark.parametrize(
    "first,second,expected,calls_expected",
    [
        ("Page 1 emissions 1234 tCO2e", "wrong", "verified", [0]),
        ("Page 1 emissions 1235 tCO2e", "Page 1 emissions 1234 tCO2e", "verified", [0, 6]),
        ("Page 1 emissions 1235 tCO2e", "Page 1 emissions 1235 tCO2e", "unresolved", [0, 6]),
        ("", "Page 1 emissions 1234 tCO2e", "unresolved", [0]),
    ],
)
def test_padding_retry_is_bounded_and_keeps_both_readings(
    monkeypatch, first, second, expected, calls_expected
):
    from proofops.adapters.local import source_verification

    calls = []

    def rendered(page, box, *, padding_px=0):
        calls.append(padding_px)
        return dict(status="read", text=second if padding_px else first, padding_px=padding_px)

    monkeypatch.setattr(source_verification, "_rendered_text", rendered)
    source = pdf()
    batch = replace(
        candidate(
            "spacing", [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    record = source_verification.attest_native_sources(
        fuse_candidates((batch,), tenant_id=TENANT), source, tenant_id=TENANT
    )["records"][0]
    assert record["status"] == expected
    assert calls == calls_expected
    if len(calls) == 2:
        assert [r["text"] for r in record["rendered_attempts"]] == [first, second]
    else:
        assert "rendered_attempts" not in record


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_unsupported_ocr_platform_does_not_render_or_launch_swift(monkeypatch, platform):
    from types import SimpleNamespace

    from proofops.adapters.local import source_verification

    def forbidden(*args, **kwargs):
        raise AssertionError("unsupported OCR platform attempted native execution")

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(source_verification.subprocess, "run", forbidden)
    page = SimpleNamespace(width=20, height=20, to_image=forbidden)
    assert source_verification._rendered_text(page, (1, 1, 10, 10)) == {
        "status": "unresolved",
        "reason": "rendered_reader_unavailable",
        "error": "UnsupportedPlatform",
    }
