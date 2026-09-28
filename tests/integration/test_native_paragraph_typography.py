"""Real native+Vision regression for the quote-typography tolerance wrapper.

Single-paragraph fixtures only (per the R02c task): one minimal curly-quote
positive case and one wrong-word/number negative case, never the full
multi-hundred-block production graph, to keep this fast and Swift/Vision-only
(no paid model calls).
"""

import shutil
import sys
from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pdfplumber
import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from tests.acceptance.test_parsing import TENANT, candidate

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or shutil.which("swift") is None,
    reason="Positive rendered OCR requires macOS Swift + Apple Vision",
)

# WinAnsiEncoding byte codes for curly quotes, so the PDF's real content
# stream (and pdfplumber's real extraction) carries actual Unicode curly
# quotation marks, matching the kia-assurance observation exactly.
_LDQUO, _RDQUO, _LSQUO, _RSQUO = 0x93, 0x94, 0x91, 0x92


def _quote_pdf(raw_bytes: bytes) -> tuple[bytes, tuple[float, float, float, float]]:
    """One page, one Helvetica/WinAnsiEncoding text run; returns (pdf, pdf_bbox)."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    commands = b"BT /F1 24 Tf 72 700 Td (" + raw_bytes + b") Tj ET"
    stream = DecodedStreamObject()
    stream.set_data(commands)
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    data = output.getvalue()
    with pdfplumber.open(BytesIO(data)) as document:
        words = document.pages[0].extract_words()
        height = document.pages[0].height
        x0 = min(w["x0"] for w in words)
        x1 = max(w["x1"] for w in words)
        top = min(w["top"] for w in words)
        bottom = max(w["bottom"] for w in words)
    # pdfplumber reports top-left coordinates; the candidate/graph fixtures
    # use PDF-native bottom-left points. Pad 1-2pt so the rendered crop and
    # exact-quantization check in attest_native_sources are not edge-cased.
    box = (x0 - 2.0, height - bottom - 2.0, x1 + 3.0, height - top + 1.0)
    return data, box


def test_curly_quote_paragraph_is_rendered_text_unresolved_by_base_verifier():
    """Ground the fix: reproduce the exact real-world observation on a
    minimal fixture before trusting the wrapper to reclassify it."""
    from proofops.adapters.local.source_verification import attest_native_sources

    raw_bytes = (
        bytes([_LDQUO])
        + b"quoted value"
        + bytes([_RDQUO])
        + b" and "
        + bytes([_LSQUO])
        + b"single"
        + bytes([_RSQUO])
    )
    source, box = _quote_pdf(raw_bytes)
    raw_text = "\u201cquoted value\u201d and \u2018single\u2019"
    batch = replace(
        candidate("fixture", [("q", "paragraph", raw_text, box, ())]),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    receipt = attest_native_sources(graph, source, tenant_id=TENANT, geometry_mode="glyph")
    record = receipt["records"][0]
    assert record["status"] == "unresolved"
    assert record["reason"] == "rendered_text_unresolved"
    assert record["rendered"]["status"] == "read"
    # Real Apple Vision behavior we are compensating for: same words/digits,
    # straight quotes instead of curly.
    rendered_text = record["rendered"]["text"]
    assert rendered_text.translate({0x201C: '"', 0x201D: '"', 0x2018: "'", 0x2019: "'"}) == (
        rendered_text
    )
    assert '"' in rendered_text or "'" in rendered_text
    assert "\u201c" not in rendered_text and "\u2018" not in rendered_text


def test_typography_wrapper_promotes_only_the_quote_mismatch():
    from proofops.adapters.local.native_paragraph_typography import (
        apply_typography_tolerance,
        eligible_typography_sources,
    )
    from proofops.adapters.local.source_verification import attest_native_sources

    raw_bytes = (
        bytes([_LDQUO])
        + b"quoted value"
        + bytes([_RDQUO])
        + b" and "
        + bytes([_LSQUO])
        + b"single"
        + bytes([_RSQUO])
    )
    source, box = _quote_pdf(raw_bytes)
    raw_text = "\u201cquoted value\u201d and \u2018single\u2019"
    batch = replace(
        candidate("fixture", [("q", "paragraph", raw_text, box, ())]),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    receipt = attest_native_sources(graph, source, tenant_id=TENANT, geometry_mode="glyph")
    record = receipt["records"][0]
    assert record["reason"] == "rendered_text_unresolved"
    source_id = record["source_id"]

    eligible = eligible_typography_sources(receipt)
    assert set(eligible) == {source_id}

    result, proof = apply_typography_tolerance(receipt, graph, source, tenant_id=TENANT)
    promoted = {b.source_id: b.quality for b in result.blocks}
    assert promoted[source_id] == "verified"
    assert proof["promoted_source_ids"] == [source_id]
    assert proof["base_verified_source_ids"] == []
    assert proof["schema"] == "native_paragraph_typography_proof_v1"

    # The base receipt, base graph, and base verifier are byte-identical to
    # the unwrapped path: no old behavior changed.
    from proofops.adapters.local.source_verification import replay_native_sources

    unwrapped = replay_native_sources(receipt, graph, source, tenant_id=TENANT)
    assert all(b.quality == "unverified" for b in unwrapped.blocks)
    assert attest_native_sources(graph, source, tenant_id=TENANT, geometry_mode="glyph") == receipt


def test_typography_wrapper_never_promotes_a_real_wrong_word_or_number():
    """Negative case: a genuine content mismatch (wrong digit) must never be
    promoted, even though it also carries curly quotes elsewhere on the page."""
    from proofops.adapters.local.native_paragraph_typography import (
        apply_typography_tolerance,
        eligible_typography_sources,
    )
    from proofops.adapters.local.source_verification import attest_native_sources

    # Claimed raw text says "1234" and a straight-looking curly-quoted word,
    # but the actual glyphs on the page say "1235": a real OCR-detectable
    # content error, not a typography artifact.
    raw_bytes = bytes([_LDQUO]) + b"total 1235" + bytes([_RDQUO])
    source, box = _quote_pdf(raw_bytes)
    claimed_raw_text = "\u201ctotal 1234\u201d"
    batch = replace(
        candidate("fixture", [("q", "paragraph", claimed_raw_text, box, ())]),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    receipt = attest_native_sources(graph, source, tenant_id=TENANT, geometry_mode="glyph")
    record = receipt["records"][0]
    # The base verifier rejects this before it ever reaches rendered-text
    # comparison, because the native words themselves ("1235") do not match
    # the claimed raw text ("1234").
    assert record["status"] == "unresolved"
    assert record["reason"] == "text_mismatch"
    assert record.get("rendered") is None

    eligible = eligible_typography_sources(receipt)
    assert eligible == {}

    result, proof = apply_typography_tolerance(receipt, graph, source, tenant_id=TENANT)
    assert all(b.quality == "unverified" for b in result.blocks)
    assert proof["promoted_source_ids"] == []
