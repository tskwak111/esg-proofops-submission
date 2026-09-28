"""Offline raster OCR correspondence; never promotes source quality or semantic tags.

Callers must obtain request/receipt hash pins from trusted immutable storage.
No provider calls occur during preparation or replay. Native geometry/visibility
approval remains a separate required gate before any future production use.
"""

import io
import json
import math
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version

import pdfplumber
from PIL import ImageOps
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from proofops.adapters.local.upstage_parse import MAX_RESPONSE_BYTES, PARSE_MODEL_PINNED
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.provenance import canonical_hash


def prepare_raster_ocr(graph, source, source_ids, *, tenant_id):
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
        or not isinstance(source_ids, tuple)
        or not 1 <= len(source_ids) <= 10
        or any(not isinstance(sid, str) for sid in source_ids)
        or len(set(source_ids)) != len(source_ids)
    ):
        raise ValueError("RASTER_SOURCE_INVALID")
    blocks = {b.source_id: b for b in graph.blocks}
    writer, rows = PdfWriter(), []
    with pdfplumber.open(io.BytesIO(source)) as document:
        for index, sid in enumerate(source_ids, 1):
            block = blocks.get(sid)
            if block is None or block.kind != "paragraph" or block.winner is None:
                raise ValueError("RASTER_PARAGRAPH_REQUIRED")
            candidate = block.candidates[block.winner]
            box = candidate.bbox
            if box is None or candidate.has_invalid_geometry or not block.raw_text:
                raise ValueError("RASTER_GEOMETRY_INVALID")
            if not 1 <= block.page_num <= len(document.pages):
                raise ValueError("RASTER_PAGE_INVALID")
            page = document.pages[block.page_num - 1]
            if (
                any(
                    type(value) not in (int, float) or not math.isfinite(value)
                    for value in (page.width, page.height, *page.bbox)
                )
                or type(page.rotation) is not int
            ):
                raise ValueError("RASTER_GEOMETRY_INVALID")
            if (
                page.rotation
                or candidate.geometry.rotation
                or tuple(page.bbox[:2]) != (0, 0)
                or tuple(candidate.geometry.crop_box[:2]) != (0, 0)
                or abs(page.width - candidate.geometry.width_pt) > 0.001
                or abs(page.height - candidate.geometry.height_pt) > 0.001
                or not 0 <= box[0] < box[2] <= page.width
                or not 0 <= box[1] < box[3] <= page.height
                or page.width * page.height * 9 > 16_000_000
            ):
                raise ValueError("RASTER_GEOMETRY_UNSUPPORTED")
            pixels = [
                math.floor(box[0] * 3),
                math.floor(box[1] * 3),
                math.ceil(box[2] * 3),
                math.ceil(box[3] * 3),
            ]
            if (pixels[2] - pixels[0] + 12) * (pixels[3] - pixels[1] + 12) > 16_000_000:
                raise ValueError("RASTER_RENDER_LIMIT")
            with page.to_image(resolution=216).original as image, image.crop(pixels) as crop:
                with (
                    crop.convert("RGB") as rgb,
                    ImageOps.expand(rgb, border=6, fill="white") as padded,
                ):
                    width, height = padded.size
                    data = padded.tobytes()
                    png = io.BytesIO()
                    padded.save(png, format="PNG")
            stream = DecodedStreamObject()
            stream.set_data(data)
            compressed = stream.flate_encode()
            compressed.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(width),
                    NameObject("/Height"): NumberObject(height),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            out = writer.add_blank_page(width=width / 3, height=height / 3)
            out[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/XObject"): DictionaryObject(
                        {NameObject("/Im0"): writer._add_object(compressed)}
                    )
                }
            )
            commands = DecodedStreamObject()
            commands.set_data(f"q {width/3} 0 0 {height/3} 0 0 cm /Im0 Do Q".encode())
            out[NameObject("/Contents")] = writer._add_object(commands)
            rows.append(
                dict(
                    submitted_page=index,
                    source_id=sid,
                    physical_page=block.page_num,
                    bbox=list(box),
                    pixel_bbox=pixels,
                    padding_px=6,
                    image_sha256=sha256(png.getvalue()).hexdigest(),
                    native=block.raw_text,
                )
            )
    buffer = io.BytesIO()
    writer.write(buffer)
    data = buffer.getvalue()
    if len(data) > 10 * 1024 * 1024:
        raise ValueError("RASTER_SUBMISSION_TOO_LARGE")
    return data, dict(
        schema="raster_ocr_correspondence_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        renderer_version=version("pypdfium2"),
        pdfplumber_version=version("pdfplumber"),
        writer_version=version("pypdf"),
        image_version=version("Pillow"),
        input_pdf_sha256=sha256(data).hexdigest(),
        input_bytes=len(data),
        rows=rows,
    )


def validate_raster_billing(usage, mode, pages):
    """The pinned raster mode must account for every submitted page exactly once."""
    if (
        not isinstance(usage, dict)
        or mode not in ("standard", "enhanced")
        or type(pages) is not int
        or not 1 <= pages <= 10
    ):
        raise ValueError("RASTER_BILLING_PAGES_INVALID")
    billed = usage.get(mode)
    other = usage.get("enhanced" if mode == "standard" else "standard", [])
    if (
        not isinstance(billed, list)
        or any(type(p) is not int for p in billed)
        or sorted(billed) != list(range(1, pages + 1))
        or other != []
    ):
        raise ValueError("RASTER_BILLING_PAGES_INVALID")


def replay_raster_ocr(
    request, receipt, graph, source, *, request_sha256, receipt_sha256, tenant_id
):
    if (
        not isinstance(request, dict)
        or not isinstance(receipt, dict)
        or not isinstance(request.get("rows"), list)
        or not 1 <= len(request["rows"]) <= 10
        or any(
            not isinstance(r, dict) or not isinstance(r.get("source_id"), str)
            for r in request["rows"]
        )
        or not isinstance(receipt.get("raw_response"), dict)
        or not isinstance(receipt.get("mode"), str)
        or receipt.get("mode") not in {"standard", "enhanced"}
    ):
        raise ValueError("RASTER_ARTIFACT_SHAPE_INVALID")
    raw = receipt["raw_response"]
    elements = raw.get("elements")
    if (
        not isinstance(raw.get("usage"), dict)
        or not isinstance(elements, list)
        or len(elements) > 10000
        or any(
            not isinstance(e, dict)
            or not isinstance(e.get("content"), dict)
            or not isinstance(e["content"].get("text"), str)
            for e in elements
        )
    ):
        raise ValueError("RASTER_ELEMENTS_INVALID")
    if sum(len(e["content"]["text"]) for e in elements) > 200000:
        raise ValueError("RASTER_TEXT_LIMIT")
    if len(json.dumps(raw, ensure_ascii=False, allow_nan=False).encode()) > MAX_RESPONSE_BYTES:
        raise ValueError("RASTER_RESPONSE_LIMIT")
    if canonical_hash(request) != request_sha256 or canonical_hash(receipt) != receipt_sha256:
        raise ValueError("RASTER_ARTIFACT_PIN_MISMATCH")
    _, rebuilt = prepare_raster_ocr(
        graph, source, tuple(r["source_id"] for r in request["rows"]), tenant_id=tenant_id
    )
    if rebuilt != request:
        raise ValueError("RASTER_REQUEST_REPLAY_MISMATCH")
    pages = len(request["rows"])
    raw = receipt["raw_response"]
    body = dict(
        model=PARSE_MODEL_PINNED,
        mode=receipt["mode"],
        pdf_sha256=request["input_pdf_sha256"],
        pages=pages,
        bytes_len=request["input_bytes"],
    )
    usage = raw.get("usage", {})
    if (
        receipt.get("model") != PARSE_MODEL_PINNED
        or receipt.get("mode") not in {"standard", "enhanced"}
        or not isinstance(receipt.get("provider_model"), str)
        or receipt.get("provider_model") != PARSE_MODEL_PINNED
        or raw.get("model") != receipt["provider_model"]
        or receipt.get("request_sha256") != canonical_hash(body)
        or receipt.get("response_sha256") != canonical_hash(raw)
        or type(usage.get("pages")) is not int
        or usage["pages"] != pages
        or type(receipt.get("pages")) is not int
        or receipt["pages"] != pages
    ):
        raise ValueError("RASTER_PROVIDER_RECEIPT_INVALID")
    validate_raster_billing(usage, receipt["mode"], pages)
    texts, ids = {p: [] for p in range(1, pages + 1)}, set()
    for e in elements:
        if (
            type(e.get("id")) is not int
            or e["id"] < 0
            or e["id"] in ids
            or type(e.get("page")) is not int
            or e["page"] not in texts
            or not isinstance(e.get("content"), dict)
            or not isinstance(e["content"].get("text"), str)
        ):
            raise ValueError("RASTER_ELEMENTS_INVALID")
        ids.add(e["id"])
        texts[e["page"]].append(e["content"]["text"])
    return [
        dict(
            source_id=row["source_id"],
            external_ocr=" ".join(texts[row["submitted_page"]]),
            exact_normalized_match=_normalized(row["native"])
            == _normalized(" ".join(texts[row["submitted_page"]])),
        )
        for row in request["rows"]
    ]
