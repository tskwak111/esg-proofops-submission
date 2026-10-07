"""Document Parse budget, page limits, receipts and credential-safe failure checks."""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from proofops.adapters.local import upstage
from proofops.adapters.local.upstage_parse import (
    PARSE_MODEL_PINNED,
    UpstageParseProbe,
)
from proofops.domain.provenance import canonical_hash


def make_pdf(num_pages: int) -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_encrypted_pdf() -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("password")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def fake_response(pages: int, mode: str = "standard", model: str = PARSE_MODEL_PINNED):
    # Build usage with correct coverage: standard or enhanced list covers all pages
    if mode == "standard":
        usage = {"pages": pages, "standard": list(range(1, pages + 1))}
    else:
        usage = {"pages": pages, "enhanced": list(range(1, pages + 1))}
    return {
        "api": "1.0",
        "model": model,
        "content": {"text": "hello", "html": "<p>hello</p>"},
        "usage": usage,
        "elements": [],
    }


def test_wrong_billing_mode_keeps_reservation_and_raw_response(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret", tmp_path / "budget.sqlite3")
    response = fake_response(1, "enhanced")
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: response)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID"):
        client.parse(make_pdf(1), request_id="wrong-mode", mode="standard")
    assert client.summary()["unsettled_calls"] == 1
    saved = tmp_path / "parse-responses" / (canonical_hash("wrong-mode") + ".json")
    assert json.loads(saved.read_text()) == response


@pytest.fixture(autouse=True)
def fixed_pricing_date(monkeypatch):
    from datetime import UTC, datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, tzinfo=UTC)

    monkeypatch.setattr("proofops.adapters.local.upstage_parse.datetime", FixedDateTime)
    monkeypatch.setattr(upstage, "datetime", FixedDateTime)


def test_parse_success_standard(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(2)
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: fake_response(2, "standard"))
    result = client.parse(pdf, request_id="parse-std-1", mode="standard")
    assert result["pages"] == 2
    assert result["mode"] == "standard"
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.022")
    assert result["provider_model"] == PARSE_MODEL_PINNED
    assert result["raw_response"]["usage"]["pages"] == 2
    assert "request_hash" in result
    assert "response_sha256" in result
    assert "provider_model_hash" in result
    # raw response retained and hashes
    assert result["raw_response"] == fake_response(2, "standard")
    assert result["response_sha256"] == canonical_hash(fake_response(2, "standard"))
    # ledger updated
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.022")
    # request hash equals canonical hash of reserve body
    expected_body = {
        "model": PARSE_MODEL_PINNED,
        "mode": "standard",
        "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "pages": 2,
        "bytes_len": len(pdf),
    }
    assert result["request_hash"] == canonical_hash(expected_body)
    assert result["request_sha256"] == canonical_hash(expected_body)


def test_parse_success_enhanced(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(3)
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: fake_response(3, "enhanced"))
    result = client.parse(pdf, request_id="parse-enhanced-1", mode="enhanced")
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.099")
    assert result["mode"] == "enhanced"
    assert result["pages"] == 3
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.099")


def test_async_parse_uses_one_shared_reservation_and_checks_batches(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(12)
    posted = []

    def submit(content, mode, path):
        posted.append((mode, path, content == pdf))
        return {"request_id": "provider-id"}

    monkeypatch.setattr(client, "_post_parse", submit)
    monkeypatch.setattr(
        client,
        "_async_status",
        lambda provider_id: {
            "status": "completed",
            "model": PARSE_MODEL_PINNED,
            "total_pages": 12,
            "completed_pages": 12,
            "batches": [
                {"download_url": "https://kr.files.upstage.ai/a"},
                {"download_url": "https://kr.files.upstage.ai/b"},
            ],
        },
    )
    monkeypatch.setattr(
        client,
        "_async_download",
        lambda url: {
            "model": PARSE_MODEL_PINNED,
            "elements": [],
            "usage": {
                "pages": 10 if url.endswith("a") else 2,
                "standard": list(range(1, 11)) if url.endswith("a") else [11, 12],
            },
        },
    )
    result = client.parse_async(pdf, request_id="async-12")
    assert posted == [("standard", "/v1/document-digitization/async", True)]
    assert len(result["raw_batches"]) == 2
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.132")


def test_async_parse_rejects_cost_above_one_reservation(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    monkeypatch.setattr(client, "_post_parse", lambda *_: pytest.fail("network dispatch"))
    with pytest.raises(ValueError, match="UPSTAGE_ASYNC_RESERVATION_LIMIT"):
        client.parse_async(make_pdf(91), request_id="too-expensive")
    assert client.summary()["calls"] == 0
    with pytest.raises(ValueError, match="UPSTAGE_DOWNLOAD_URL_INVALID"):
        client._async_download("https://evil.example/result")


def test_parser_routes_bad_standard_html_to_enhanced_without_promoting_blank_text(tmp_path):
    from uuid import uuid4

    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.ingest.graph_fusion import ParserProfile, SourceArtifact

    local_java = Path("/opt/homebrew/opt/openjdk@21/bin/java")
    java = os.getenv("PROOFOPS_TEST_JAVA") or (str(local_java) if local_java.is_file() else "java")
    executable = shutil.which(java)
    if not executable:
        pytest.skip("Java 21 unavailable")
    runtime = subprocess.run([executable, "-version"], capture_output=True, timeout=20, check=True)
    if re.search(r'version "21\.', runtime.stderr.decode(errors="replace")) is None:
        pytest.skip("Java 21 unavailable")
    pdf = make_pdf(1)
    source = SourceArtifact(
        str(uuid4()), str(uuid4()), str(uuid4()), hashlib.sha256(pdf).hexdigest(), "v1", pdf
    )
    profile = ParserProfile(
        str(uuid4()),
        physical_pages=(1,),
        java_executable=executable,
        timeout_seconds=120,
        table_auxiliary=False,
        parser_mode="upstage",
        vision_parse="off",
    )
    coords = [
        {"x": 0.1, "y": 0.1},
        {"x": 0.9, "y": 0.1},
        {"x": 0.9, "y": 0.9},
        {"x": 0.1, "y": 0.9},
    ]

    class Fake:
        def parse_async(self, pdf_bytes, *, request_id, mode):
            html = "<table>" if mode == "standard" else ("<table><tr><td>2035</td></tr></table>")
            batches = [
                {
                    "model": PARSE_MODEL_PINNED,
                    "usage": {"pages": 1, mode: [1]},
                    "elements": [
                        {
                            "page": 1,
                            "category": "table",
                            "coordinates": coords,
                            "content": {"text": "2035", "html": html},
                        }
                    ],
                }
            ]
            return {
                "raw_batches": batches,
                "response_sha256": canonical_hash(batches),
                "model": PARSE_MODEL_PINNED,
                "pages": 1,
            }

    parser = OpenDataLoaderParser(tmp_path / "artifacts", upstage_probe=Fake())
    graph = parser.parse(source, profile, tenant_id=source.tenant_id)
    assert any(b.kind == "table_cell" for b in graph.blocks)
    assert all(b.quality == "unlocated" for b in graph.blocks)
    loaded = parser.load_verified(source, profile, tenant_id=source.tenant_id)
    assert loaded.to_dict() == graph.to_dict()
    manifest = json.loads(
        (
            parser.artifact_root
            / source.tenant_id
            / source.document_version_id
            / profile.parse_manifest_id
            / "manifest.json"
        ).read_text()
    )
    assert manifest["upstage_parse"]["standard"]["html_failed_pages"] == [1]
    assert manifest["upstage_parse"]["enhanced"][0]["grounded_cells"] == 0


def test_upstage_glyph_boxes_cover_exact_native_ink_without_changing_legacy_profile():
    from uuid import uuid4

    import pdfplumber
    from proofops.adapters.local.claim_source_verification import _read_paragraph
    from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry
    from proofops.adapters.parsing.upstage_document import candidate_batch
    from proofops.application.ingest.graph_fusion import (
        ParserProfile,
        SourceArtifact,
        fuse_candidates,
    )
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=200)
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
    content.set_data(b"BT /F1 12 Tf 40 100 Td (Energy use fell 20% in 2025.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    pdf = stream.getvalue()
    source = SourceArtifact(
        str(uuid4()), str(uuid4()), str(uuid4()), hashlib.sha256(pdf).hexdigest(), "v1", pdf
    )
    coords = [
        {"x": 0.05, "y": 0.4},
        {"x": 0.95, "y": 0.4},
        {"x": 0.95, "y": 0.6},
        {"x": 0.05, "y": 0.6},
    ]
    response = [
        {
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 1, "standard": [1]},
            "elements": [
                {
                    "page": 1,
                    "category": "paragraph",
                    "coordinates": coords,
                    "content": {"text": "Energy use fell 20% in 2025."},
                }
            ],
        }
    ]
    legacy = ParserProfile(str(uuid4()), parser_mode="upstage")
    modern = ParserProfile(str(uuid4()), parser_mode="upstage", upstage_glyph_boxes=True)
    assert "upstage_glyph_boxes" not in legacy.config_snapshot()
    assert modern.config_hash() != legacy.config_hash()
    old = candidate_batch(
        source, legacy, (1,), response, mode="standard", config_hash=legacy.config_hash()
    )[0].blocks[0]
    new_batch = candidate_batch(
        source, modern, (1,), response, mode="standard", config_hash=modern.config_hash()
    )[0]
    new = new_batch.blocks[0]
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        words = doc.pages[0].extract_words()
        graph = fuse_candidates((new_batch,), tenant_id=source.tenant_id)
        reading = _read_paragraph(doc, pdf, graph.blocks[0], False, {})
    assert reading["reason"] not in {"clipped_or_rotated_words", "text_mismatch"}
    assert new.source.raw_text == " ".join(word["text"] for word in words)
    proof = native_word_ink_geometry(pdf, 1, list(range(len(words))))
    ink = [entry["ink_bbox"] for entry in proof["matched_words"]]
    assert old.bbox != new.bbox
    assert new.bbox[0] <= min(box[0] for box in ink)
    assert new.bbox[1] <= min(box[1] for box in ink)
    assert new.bbox[2] >= max(box[2] for box in ink)
    assert new.bbox[3] >= max(box[3] for box in ink)
    assert "native_glyph_ink_box" in new.context


def test_upstage_region_words_ground_multiline_paragraph_in_two_column_page():
    from uuid import uuid4

    import pdfplumber
    from proofops.adapters.local.claim_source_verification import _read_paragraph
    from proofops.adapters.parsing.upstage_document import candidate_batch
    from proofops.application.ingest.graph_fusion import (
        ParserProfile,
        SourceArtifact,
        fuse_candidates,
    )
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=200)
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
    # Two columns whose lines share baselines: page-wide word order interleaves them.
    content.set_data(
        b"BT /F1 12 Tf 20 120 Td (Energy use fell) Tj ET "
        b"BT /F1 12 Tf 220 120 Td (Water reuse rose) Tj ET "
        b"BT /F1 12 Tf 20 100 Td (20% in 2025.) Tj ET "
        b"BT /F1 12 Tf 220 100 Td (8% in 2024.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(content)
    stream = io.BytesIO()
    writer.write(stream)
    pdf = stream.getvalue()
    source = SourceArtifact(
        str(uuid4()), str(uuid4()), str(uuid4()), hashlib.sha256(pdf).hexdigest(), "v1", pdf
    )
    coords = [
        {"x": 0.03, "y": 0.3},
        {"x": 0.45, "y": 0.3},
        {"x": 0.45, "y": 0.55},
        {"x": 0.03, "y": 0.55},
    ]
    response = [
        {
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 1, "standard": [1]},
            "elements": [
                {
                    "page": 1,
                    "category": "paragraph",
                    "coordinates": coords,
                    "content": {"text": "Energy use fell\n20% in 2025."},
                }
            ],
        }
    ]
    glyph = ParserProfile(str(uuid4()), parser_mode="upstage", upstage_glyph_boxes=True)
    region = ParserProfile(
        str(uuid4()), parser_mode="upstage", upstage_glyph_boxes=True, upstage_region_words=True
    )
    assert "upstage_region_words" not in glyph.config_snapshot()
    assert region.config_hash() != glyph.config_hash()
    with pytest.raises(ValueError):
        ParserProfile(str(uuid4()), upstage_region_words=True)
    old = candidate_batch(
        source, glyph, (1,), response, mode="standard", config_hash=glyph.config_hash()
    )[0].blocks[0]
    assert old.bbox is None
    batch = candidate_batch(
        source, region, (1,), response, mode="standard", config_hash=region.config_hash()
    )[0]
    new = batch.blocks[0]
    assert new.bbox is not None
    assert new.source.raw_text == "Energy use fell 20% in 2025."
    assert "native_glyph_ink_box" in new.context
    with pdfplumber.open(io.BytesIO(pdf)) as doc:
        graph = fuse_candidates((batch,), tenant_id=source.tenant_id)
        reading = _read_paragraph(doc, pdf, graph.blocks[0], False, {})
    assert reading["reason"] not in {"clipped_or_rotated_words", "text_mismatch"}


def test_budget_sharing_and_exhaustion_across_probes(tmp_path, monkeypatch):
    path = tmp_path / "budget.sqlite3"
    text_client = upstage.UpstageProbe("test-secret", path)
    parse_client = UpstageParseProbe("test-secret", path)

    # Mock both to fail with OSError -> keep reservation
    monkeypatch.setattr(
        text_client, "_post", lambda body: (_ for _ in ()).throw(OSError("net fail"))
    )
    monkeypatch.setattr(
        parse_client,
        "_post_parse",
        lambda pdf_bytes, mode: (_ for _ in ()).throw(OSError("net fail")),
    )

    pdf = make_pdf(1)
    # 5 text calls
    for i in range(5):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            text_client.complete("sys", "{}", request_id=f"text-{i}")
    # 5 parse calls -> total 10 reservations
    for i in range(5):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            parse_client.parse(pdf, request_id=f"parse-{i}", mode="standard")

    assert Decimal(parse_client.summary()["committed_usd"]) == Decimal(10)
    assert Decimal(text_client.summary()["committed_usd"]) == Decimal(10)

    # Next call from either should be BUDGET_EXHAUSTED before network
    monkeypatch.setattr(
        parse_client, "_post_parse", lambda pdf_bytes, mode: pytest.fail("budget allowed network")
    )
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        parse_client.parse(pdf, request_id="parse-over", mode="standard")
    monkeypatch.setattr(text_client, "_post", lambda body: pytest.fail("budget allowed network"))
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        text_client.complete("sys", "{}", request_id="text-over")


def test_duplicate_request_keeps_original_and_no_retry(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: fake_response(1, "standard"))
    result = client.parse(pdf, request_id="dup-id", mode="standard")
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.011")

    # duplicate should raise without calling transport again
    def fail(pdf_bytes, mode):
        pytest.fail("duplicate triggered network")

    monkeypatch.setattr(client, "_post_parse", fail)
    with pytest.raises(ValueError, match="DUPLICATE_PROBE_REQUEST"):
        client.parse(pdf, request_id="dup-id", mode="standard")
    # committed only once
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.011")
    assert client.summary()["calls"] == 1


def test_page_count_bounds_rejected_without_reservation(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    monkeypatch.setattr(
        client, "_post_parse", lambda pdf_bytes, mode: pytest.fail("invalid pdf dispatched")
    )

    # Create PDF with 11 pages
    pdf_11 = make_pdf(11)
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf_11, request_id="too-many-pages", mode="standard")
    assert client.summary()["calls"] == 0

    # oversized >10MB
    pdf_small = make_pdf(1)
    oversized = pdf_small + b"a" * (10 * 1024 * 1024)
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(oversized, request_id="too-large", mode="standard")
    assert client.summary()["calls"] == 0

    # encrypted
    enc_pdf = make_encrypted_pdf()
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(enc_pdf, request_id="encrypted", mode="standard")
    assert client.summary()["calls"] == 0

    # invalid mode
    valid_pdf = make_pdf(1)
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(valid_pdf, request_id="bad-mode", mode="auto")
    assert client.summary()["calls"] == 0

    # non-bytes
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse("not-bytes", request_id="bad-type", mode="standard")  # type: ignore
    assert client.summary()["calls"] == 0


def test_page_count_mismatch_retains_reservation(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(2)

    # usage.pages != submitted pages
    def bad_pages(pdf_bytes, mode):
        return fake_response(3, "standard")  # 3 vs 2

    monkeypatch.setattr(client, "_post_parse", bad_pages)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client.parse(pdf, request_id="mismatch-pages", mode="standard")
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)
    assert client.summary()["unsettled_calls"] == 1


def test_mode_page_lists_validation_retains_reservation(tmp_path, monkeypatch):
    pdf = make_pdf(3)

    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")

    # Case 1: duplicate in list
    def dup_list(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 3, "standard": [1, 2, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client, "_post_parse", dup_list)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client.parse(pdf, request_id="dup-list", mode="standard")
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)

    # Case 2: missing coverage (gap)
    client2 = UpstageParseProbe("test-secret-parse", tmp_path / "budget2.sqlite3")

    def gap_list(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 3, "standard": [1, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client2, "_post_parse", gap_list)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client2.parse(pdf, request_id="gap", mode="standard")
    assert Decimal(client2.summary()["committed_usd"]) == Decimal(1)

    # Case 3: bool in list (True is 1)
    client3 = UpstageParseProbe("test-secret-parse", tmp_path / "budget3.sqlite3")

    def bool_list(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 2, "standard": [True, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client3, "_post_parse", bool_list)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client3.parse(pdf, request_id="bool", mode="standard")
    assert Decimal(client3.summary()["committed_usd"]) == Decimal(1)

    # Case 4: overlapping across modes
    client4 = UpstageParseProbe("test-secret-parse", tmp_path / "budget4.sqlite3")

    def overlap(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 2, "standard": [1], "enhanced": [1, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client4, "_post_parse", overlap)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client4.parse(pdf, request_id="overlap", mode="standard")
    assert Decimal(client4.summary()["committed_usd"]) == Decimal(1)

    # Case 5: out of range
    client5 = UpstageParseProbe("test-secret-parse", tmp_path / "budget5.sqlite3")

    def out_of_range(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 2, "standard": [1, 3]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client5, "_post_parse", out_of_range)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client5.parse(pdf, request_id="oor", mode="standard")
    assert Decimal(client5.summary()["committed_usd"]) == Decimal(1)


def test_invalid_usage_retained_reservation_various(tmp_path, monkeypatch):
    pdf = make_pdf(1)
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")

    # usage bool
    def bool_pages(pdf_bytes, mode):
        return {"api": "1.0", "model": PARSE_MODEL_PINNED, "usage": {"pages": True}, "content": {}}

    monkeypatch.setattr(client, "_post_parse", bool_pages)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client.parse(pdf, request_id="bool-pages", mode="standard")
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)

    # wrong model
    client2 = UpstageParseProbe("test-secret-parse", tmp_path / "budget2b.sqlite3")

    def wrong_model(pdf_bytes, mode):
        return {"api": "1.0", "model": "another-provider", "usage": {"pages": 1}, "content": {}}

    monkeypatch.setattr(client2, "_post_parse", wrong_model)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client2.parse(pdf, request_id="wrong-model", mode="standard")
    assert Decimal(client2.summary()["committed_usd"]) == Decimal(1)

    # empty model
    client3 = UpstageParseProbe("test-secret-parse", tmp_path / "budget3b.sqlite3")

    def empty_model(pdf_bytes, mode):
        return {"api": "1.0", "model": "", "usage": {"pages": 1}, "content": {}}

    monkeypatch.setattr(client3, "_post_parse", empty_model)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client3.parse(pdf, request_id="empty-model", mode="standard")
    assert Decimal(client3.summary()["committed_usd"]) == Decimal(1)

    # missing usage
    client4 = UpstageParseProbe("test-secret-parse", tmp_path / "budget4b.sqlite3")

    def no_usage(pdf_bytes, mode):
        return {"api": "1.0", "model": PARSE_MODEL_PINNED, "content": {}}

    monkeypatch.setattr(client4, "_post_parse", no_usage)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client4.parse(pdf, request_id="no-usage", mode="standard")
    assert Decimal(client4.summary()["committed_usd"]) == Decimal(1)


def test_alias_model_accepted(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)
    monkeypatch.setattr(
        client,
        "_post_parse",
        lambda pdf_bytes, mode: fake_response(1, "standard", model="document-parse"),
    )
    result = client.parse(pdf, request_id="alias", mode="standard")
    assert result["provider_model"] == "document-parse"
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.011")


def test_no_credential_in_errors(tmp_path, monkeypatch):
    secret = "super-secret-parse-key-123"
    client = UpstageParseProbe(secret, tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)

    def fail_with_secret(pdf_bytes, mode):
        raise OSError(f"failed with {secret} leaked")

    monkeypatch.setattr(client, "_post_parse", fail_with_secret)
    with pytest.raises(ValueError) as exc:
        client.parse(pdf, request_id="cred-fail", mode="standard")
    assert secret not in str(exc.value)
    # also ensure sanitized to UPSTAGE_REQUEST_FAILED
    assert "UPSTAGE_REQUEST_FAILED" in str(exc.value)
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)

    # HTTP error sanitized but allowed codes pass through
    def http_500(pdf_bytes, mode):
        raise ValueError("UPSTAGE_HTTP_500")

    monkeypatch.setattr(client, "_post_parse", http_500)
    with pytest.raises(ValueError, match="UPSTAGE_HTTP_500") as exc2:
        client.parse(pdf, request_id="http500", mode="standard")
    assert secret not in str(exc2.value)

    # receipt invalid should not leak
    def bad_receipt(pdf_bytes, mode):
        return {"api": "1.0", "model": "", "usage": {"pages": 1}}

    monkeypatch.setattr(client, "_post_parse", bad_receipt)
    with pytest.raises(ValueError) as exc3:
        client.parse(pdf, request_id="bad-receipt-cred", mode="standard")
    assert secret not in str(exc3.value)


def test_success_cost_exact_and_ledger_row_update(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    for pages, mode, expected in [
        (1, "standard", Decimal("0.011")),
        (10, "standard", Decimal("0.11")),
        (1, "enhanced", Decimal("0.033")),
        (10, "enhanced", Decimal("0.33")),
    ]:
        pdf = make_pdf(pages)
        monkeypatch.setattr(
            client,
            "_post_parse",
            lambda pdf_bytes, mode=mode, pages=pages: fake_response(pages, mode),
        )
        result = client.parse(pdf, request_id=f"cost-{pages}-{mode}", mode=mode)
        assert Decimal(result["cost_with_vat_reserve_usd"]) == expected
        assert Decimal(result["cost_with_vat_reserve_usd"]) <= Decimal("1.00")
        # verify ledger receipt stored
        import sqlite3

        with sqlite3.connect(client.ledger) as db:
            row = db.execute(
                "SELECT committed, receipt FROM probe_calls WHERE request_id=?",
                (f"cost-{pages}-{mode}",),
            ).fetchone()
            assert row is not None
            assert Decimal(row[0]) == expected
            receipt = json.loads(row[1])
            assert receipt["response_sha256"] == canonical_hash(fake_response(pages, mode))
            assert receipt["request_sha256"] == canonical_hash(
                {
                    "model": PARSE_MODEL_PINNED,
                    "mode": mode,
                    "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                    "pages": pages,
                    "bytes_len": len(pdf),
                }
            )
            assert receipt["raw_response"] == fake_response(pages, mode)


def test_invalid_mode_and_request_id_validation(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)
    monkeypatch.setattr(
        client, "_post_parse", lambda pdf_bytes, mode: pytest.fail("should not reach network")
    )
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf, request_id="", mode="standard")
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf, request_id="x" * 129, mode="standard")
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf, request_id="valid", mode="standard ")


def test_missing_mode_usage_retains_budget_and_private_archive(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret", tmp_path / "budget.sqlite3")
    response = fake_response(1)
    response["usage"] = {"pages": 1}
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: response)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID"):
        client.parse(make_pdf(1), request_id="missing-mode", mode="standard")
    assert client.summary()["unsettled_calls"] == 1
    archive = next((tmp_path / "parse-responses").glob("*.json"))
    assert archive.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("transport", ["text", "parse"])
def test_missing_reservation_cannot_return_settled_success(tmp_path, monkeypatch, transport):
    import sqlite3

    cls = upstage.UpstageProbe if transport == "text" else UpstageParseProbe
    client = cls("test-secret", tmp_path / "budget.sqlite3")

    def post(*args):
        with sqlite3.connect(client.ledger) as db:
            db.execute("DELETE FROM probe_calls WHERE request_id='lost'")
        if transport == "parse":
            return fake_response(1)
        return {
            "id": "fake",
            "model": "solar-pro3",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }

    monkeypatch.setattr(client, "_post" if transport == "text" else "_post_parse", post)
    with pytest.raises(ValueError, match="BUDGET_SETTLEMENT_INVALID"):
        if transport == "text":
            client.complete("JSON", "{}", request_id="lost")
        else:
            client.parse(make_pdf(1), request_id="lost", mode="standard")
