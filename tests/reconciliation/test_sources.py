"""Adversarial local artifact and format-aware source validation tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from proofops.adapters.reconciliation import FileSourceReader, SourceReadError


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _ref(document_id: str, payload: bytes, locator: str, quote: str) -> dict:
    return {
        "source_id": f"source-{document_id}",
        "document_id": document_id,
        "artifact_sha256": _sha(payload),
        "locator": locator,
        "quote": quote,
    }


def test_text_offsets_are_unicode_codepoints_and_exact(tmp_path: Path):
    payload = "앞쪽 검증문구 뒤쪽".encode()
    path = tmp_path / "source.txt"
    path.write_bytes(payload)
    reader = FileSourceReader(
        tmp_path,
        {"doc": {"path": "source.txt", "format": "text", "sha256": _sha(payload)}},
    )
    ref = _ref("doc", payload, "chars:3:7", "검증문구")

    assert reader(ref) == payload
    assert reader.validate(ref)

    with pytest.raises(SourceReadError, match="quote_mismatch"):
        reader.validate({**ref, "quote": "검증 문구"})


@pytest.mark.parametrize(
    ("name", "format_name", "payload", "locator", "quote"),
    [
        (
            "source.xml",
            "xml",
            b'<root><fact id="fact-1">Exact XML quote</fact></root>',
            "id:fact-1",
            "Exact XML quote",
        ),
        (
            "source.html",
            "html",
            b'<html><p id="fact-1">Exact <b>HTML</b> quote</p></html>',
            "id:fact-1",
            "Exact HTML quote",
        ),
    ],
)
def test_xml_and_html_id_locators(name, format_name, payload, locator, quote, tmp_path: Path):
    (tmp_path / name).write_bytes(payload)
    reader = FileSourceReader(
        tmp_path,
        {"doc": {"path": name, "format": format_name, "sha256": _sha(payload)}},
    )
    assert reader.validate(_ref("doc", payload, locator, quote))


def test_pdf_page_quote_is_extracted_without_bbox(tmp_path: Path):
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 72 200 Td (Verified PDF quote) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    path = tmp_path / "source.pdf"
    with path.open("wb") as stream:
        writer.write(stream)
    payload = path.read_bytes()
    reader = FileSourceReader(
        tmp_path,
        {"doc": {"path": "source.pdf", "format": "pdf", "sha256": _sha(payload)}},
    )

    assert reader.validate(_ref("doc", payload, "page:1", "Verified PDF quote"))
    with pytest.raises(SourceReadError, match="page_out_of_range"):
        reader.validate(_ref("doc", payload, "page:2", "Verified PDF quote"))


def test_pdf_explicit_whitespace_locator_preserves_text_and_refuses_ambiguity(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                }
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 20 250 Td (NAVER   reused) Tj 0 -20 Td "
        b"(870000 cups in 2025) Tj 0 -20 Td (duplicate   words) Tj "
        b"0 -20 Td (duplicate words) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    path = tmp_path / "source.pdf"
    writer.write(path)
    payload = path.read_bytes()
    reader = FileSourceReader(
        tmp_path, {"doc": {"path": path.name, "format": "pdf", "sha256": _sha(payload)}}
    )
    quote = "NAVER reused 870000 cups in 2025"
    ref = _ref("doc", payload, "page:1:whitespace-v1", quote)
    assert reader.validate(ref)
    with pytest.raises(SourceReadError, match="quote_mismatch"):
        reader.validate({**ref, "locator": "page:1"})
    for wrong in (
        quote.replace("870000", "870001"),
        quote.replace("reused", "re used"),
        quote.replace("2025", "2024"),
        "duplicate words",
    ):
        with pytest.raises(SourceReadError):
            reader.validate({**ref, "quote": wrong})
    with pytest.raises(SourceReadError, match="unsupported_locator"):
        reader.validate({**ref, "locator": "page:1:whitespace-v2"})


def test_reader_rejects_root_escape_and_changed_artifact(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    with pytest.raises(SourceReadError, match="path_outside_root"):
        FileSourceReader(
            root,
            {"doc": {"path": "../outside.txt", "format": "text", "sha256": _sha(b"outside")}},
        )

    path = root / "source.txt"
    path.write_bytes(b"original")
    reader = FileSourceReader(
        root,
        {"doc": {"path": "source.txt", "format": "text", "sha256": _sha(b"original")}},
    )
    path.write_bytes(b"tampered")
    with pytest.raises(SourceReadError, match="artifact_hash_mismatch"):
        reader(_ref("doc", b"original", "chars:0:8", "original"))


def test_reader_rejects_unsupported_format_and_locator(tmp_path: Path):
    payload = b"plain"
    (tmp_path / "source.bin").write_bytes(payload)
    with pytest.raises(SourceReadError, match="unsupported_format"):
        FileSourceReader(
            tmp_path,
            {"doc": {"path": "source.bin", "format": "docx", "sha256": _sha(payload)}},
        )

    (tmp_path / "source.txt").write_bytes(payload)
    reader = FileSourceReader(
        tmp_path,
        {"doc": {"path": "source.txt", "format": "text", "sha256": _sha(payload)}},
    )
    with pytest.raises(SourceReadError, match="unsupported_locator"):
        reader.validate(_ref("doc", payload, "page:1", "plain"))
