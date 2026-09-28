"""Actual PDF text origins, tight glyph bounds, and fail-closed diagnostics."""

import json
from contextlib import closing
from hashlib import sha256
from io import BytesIO

import pdfplumber
import pypdfium2 as pdfium
import pytest
from proofops.domain.provenance import canonical_hash
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, RectangleObject


def pdf(content=b"BT /F1 12 Tf 30 100 Td (AB) Tj ET", *, rotation=0, crop=None):
    writer = PdfWriter()
    page = writer.add_blank_page(300, 200)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    if rotation:
        page.rotate(rotation)
    if crop:
        page[NameObject("/CropBox")] = RectangleObject(crop)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def geometry(source, indices=None, page=1):
    from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry

    return native_word_ink_geometry(source, page, [0] if indices is None else indices)


def test_exact_origin_matching_keeps_original_words_and_font_boxes():
    source = pdf(b"BT /F1 12 Tf 30 100 Td (AB) Tj 120 0 Td (AB) Tj ET")
    result = geometry(source, [0, 1])
    assert set(result) == {
        "schema",
        "source_sha256",
        "physical_page",
        "native_word_indices",
        "reader",
        "reader_version",
        "matcher_sha256",
        "origin_tolerance_pt",
        "matched_words",
        "unresolved_word_indices",
        "ink_bbox",
        "status",
        "proof_sha256",
    }
    assert result["schema"] == "native_glyph_geometry_v1"
    assert result["reader"] == "pdfium" and isinstance(result["reader_version"], str)
    assert result["source_sha256"] == sha256(source).hexdigest()
    assert len(result["matcher_sha256"]) == 64
    assert result["physical_page"] == 1 and result["native_word_indices"] == [0, 1]
    assert result["origin_tolerance_pt"] == 0.001
    assert result["status"] == "matched" and result["unresolved_word_indices"] == []
    with pdfplumber.open(BytesIO(source)) as document, pdfium.PdfDocument(source) as native:
        words = document.pages[0].extract_words()
        with closing(native[0]) as page, closing(page.get_textpage()) as text:
            for index, record in enumerate(result["matched_words"]):
                assert set(record) == {
                    "native_word_index",
                    "pdfium_char_indices",
                    "font_bbox",
                    "ink_bbox",
                }
                assert record["native_word_index"] == index
                assert record["font_bbox"] == [
                    words[index][k] for k in ("x0", "top", "x1", "bottom")
                ]
                chars = record["pdfium_char_indices"]
                assert len(chars) == len(set(chars)) == 2
                boxes = [text.get_charbox(i) for i in chars]
                assert record["ink_bbox"] == [
                    min(b[0] for b in boxes),
                    200 - max(b[3] for b in boxes),
                    max(b[2] for b in boxes),
                    200 - min(b[1] for b in boxes),
                ]
                assert record["ink_bbox"] != record["font_bbox"]
    first, second = result["matched_words"]
    assert set(first["pdfium_char_indices"]).isdisjoint(second["pdfium_char_indices"])
    assert result["ink_bbox"] == [*first["ink_bbox"][:2], *second["ink_bbox"][2:]]
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    assert result["proof_sha256"] == canonical_hash(
        {k: v for k, v in result.items() if k != "proof_sha256"}
    )
    assert result == geometry(source, [0, 1])


def test_duplicate_glyph_origins_remain_ambiguous_and_partial_has_no_union():
    source = pdf(
        b"BT /F1 12 Tf 30 100 Td (A) Tj ET "
        b"BT /F1 12 Tf 30 100 Td (A) Tj ET "
        b"BT /F1 12 Tf 150 100 Td (B) Tj ET"
    )
    result = geometry(source, [0, 1])
    assert result["status"] == "unresolved" and result["ink_bbox"] is None
    assert result["unresolved_word_indices"] == [0]
    assert [r["native_word_index"] for r in result["matched_words"]] == [1]


@pytest.mark.parametrize(
    "indices", [[], [True], [0.0], [-1], [0, 0], [1, 0], [99], list(range(1001)), (0,), "0"]
)
def test_malformed_indices_rejected(indices):
    with pytest.raises(ValueError):
        geometry(pdf(), indices)


@pytest.mark.parametrize("page", [True, 0, -1, 1.0, 2])
def test_invalid_physical_page_rejected(page):
    with pytest.raises(ValueError):
        geometry(pdf(), page=page)


@pytest.mark.parametrize("source", [b"", b"not a pdf", "pdf", b" " * (100 * 1024 * 1024 + 1)])
def test_invalid_source_rejected(source):
    with pytest.raises(ValueError):
        geometry(source)


@pytest.mark.parametrize("options", [{"rotation": 90}, {"crop": (10, 0, 300, 200)}])
def test_unsupported_page_geometry_rejected(options):
    with pytest.raises(ValueError):
        geometry(pdf(**options))


def test_invalid_word_bounds_and_non_upright_text_are_unresolved():
    for content in (
        b"BT /F1 12 Tf -10 100 Td (AB) Tj ET",
        b"BT /F1 12 Tf 0 1 -1 0 100 100 Tm (AB) Tj ET",
    ):
        result = geometry(pdf(content))
        assert result["status"] == "unresolved"
        assert result["ink_bbox"] is None and result["unresolved_word_indices"] == [0]


@pytest.mark.parametrize("box", [(0, 0, 0, 0), (30, 100, 35, float("nan")), (-1, 100, 35, 110)])
def test_invalid_tight_glyph_bounds_unresolved(monkeypatch, box):
    monkeypatch.setattr(pdfium.PdfTextPage, "get_charbox", lambda *a, **kw: box)
    result = geometry(pdf())
    assert result["matched_words"] == [] and result["unresolved_word_indices"] == [0]
    assert result["status"] == "unresolved" and result["ink_bbox"] is None


def test_character_inventory_limit():
    with pytest.raises(ValueError, match="inventory"):
        geometry(pdf(b"BT /F1 1 Tf 0 100 Td (" + b"A" * 20001 + b") Tj ET"))


@pytest.mark.parametrize(
    "offset,expected", [(0.0005, "matched"), (0.002, "unresolved"), (float("nan"), "unresolved")]
)
def test_origin_tolerance_is_absolute_and_never_text_or_proximity(monkeypatch, offset, expected):
    import pypdfium2.raw as raw

    original = raw.FPDFText_GetCharOrigin

    def shifted(textpage, index, x, y):
        ok = original(textpage, index, x, y)
        x.value += offset
        return ok

    monkeypatch.setattr(raw, "FPDFText_GetCharOrigin", shifted)
    result = geometry(pdf())
    assert result["status"] == expected
    assert (result["ink_bbox"] is None) == (expected == "unresolved")


def test_same_origin_with_different_unicode_is_unmatched(monkeypatch):
    import pypdfium2.raw as raw

    monkeypatch.setattr(raw, "FPDFText_GetUnicode", lambda *args: ord("Z"))
    result = geometry(pdf())
    assert result["unresolved_word_indices"] == [0] and result["matched_words"] == []
    assert result["ink_bbox"] is None


def test_equal_origins_on_distinct_pages_have_distinct_proofs():
    from pypdf import PdfReader

    writer = PdfWriter()
    for _ in range(2):
        writer.add_page(PdfReader(BytesIO(pdf())).pages[0])
    output = BytesIO()
    writer.write(output)
    first, second = (geometry(output.getvalue(), page=n) for n in (1, 2))
    assert first["status"] == second["status"] == "matched"
    assert first["ink_bbox"] == second["ink_bbox"]
    assert first["proof_sha256"] != second["proof_sha256"]


def test_glyph_reader_error_leaves_word_unresolved(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise pdfium.PdfiumError("unavailable glyph")

    monkeypatch.setattr(pdfium.PdfTextPage, "get_charbox", unavailable)
    result = geometry(pdf())
    assert result["status"] == "unresolved" and result["ink_bbox"] is None
    assert result["unresolved_word_indices"] == [0]


def test_invisible_text_geometry_is_not_visibility_evidence():
    source = pdf(b"BT /F1 12 Tf 3 Tr 30 100 Td (AB) Tj ET")
    assert geometry(source)["status"] == "matched"
    with pdfium.PdfDocument(source) as document, closing(document[0]) as page:
        with closing(page.render()) as bitmap:
            assert all(bounds == (255, 255) for bounds in bitmap.to_pil().getextrema())


@pytest.mark.parametrize("spacing", [-0.17, 0.17])
def test_split_text_operators_preserve_character_spacing(spacing):
    prefix = f"BT /F1 12 Tf {spacing} Tc 30 100 Td ".encode()
    combined = geometry(pdf(prefix + b"(AB) Tj ET"))
    split = geometry(pdf(prefix + b"(A) Tj (B) Tj ET"))
    assert combined["status"] == split["status"] == "matched"
    assert split["ink_bbox"] == pytest.approx(combined["ink_bbox"], abs=0.001)


@pytest.mark.parametrize(
    "operators", [b"[(A) 0 (B)] TJ", b"(A) Tj [] TJ (B) Tj", b"(A) Tj [0 (B)] TJ"]
)
def test_text_array_adjustments_do_not_duplicate_character_spacing(operators):
    prefix = b"BT /F1 12 Tf -0.17 Tc 30 100 Td "
    expected = geometry(pdf(prefix + b"(AB) Tj ET"))
    actual = geometry(pdf(prefix + operators + b" ET"))
    assert actual["status"] == "matched"
    assert actual["ink_bbox"] == pytest.approx(expected["ink_bbox"], abs=0.001)
