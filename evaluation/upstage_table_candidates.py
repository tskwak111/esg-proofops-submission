"""Offline original-bound table candidates; no calls, cell geometry or quality approval."""

from __future__ import annotations

import io
import math
from hashlib import sha256
from importlib.metadata import version
from uuid import UUID, uuid5

from proofops.adapters.local.upstage_parse import ALLOWED_MODELS, ALLOWED_MODES, PARSE_MODEL_PINNED
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    CandidateEdge,
    SourceArtifact,
)
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_uuid
from pypdf import PdfReader, PdfWriter

from evaluation.html_table_cells import parse_table_cells


def build_table_batch(source, receipt, *, physical_pages, parse_manifest_id, tenant_id):
    """Rebuild the exact submitted subset before assigning original-page provenance."""
    _require_uuid("parse_manifest_id", parse_manifest_id)
    if not isinstance(source, SourceArtifact) or not isinstance(receipt, dict):
        raise ValueError("UPSTAGE_TABLE_INPUT_INVALID")
    if source.tenant_id != tenant_id or sha256(source.content).hexdigest() != source.sha256:
        raise ValueError("UPSTAGE_TABLE_SOURCE_MISMATCH")
    if (
        not isinstance(physical_pages, tuple)
        or not 1 <= len(physical_pages) <= 10
        or any(type(p) is not int or p < 1 for p in physical_pages)
        or len(set(physical_pages)) != len(physical_pages)
        or len(source.content) > 100 * 1024 * 1024
    ):
        raise ValueError("UPSTAGE_TABLE_PAGE_MAP_INVALID")
    reader = PdfReader(io.BytesIO(source.content))
    if reader.is_encrypted or max(physical_pages) > len(reader.pages):
        raise ValueError("UPSTAGE_TABLE_PAGE_MAP_INVALID")
    writer = PdfWriter()
    geometries = {}
    for number in physical_pages:
        page = reader.pages[number - 1]
        writer.add_page(page)  # clone before pypdf accessors materialize default CropBox
        box = tuple(map(float, page.mediabox))
        # ponytail: provider crop/rotation conventions need separate measured fixtures.
        if page.rotation or box[:2] != (0, 0) or tuple(map(float, page.cropbox)) != box:
            raise ValueError("UPSTAGE_TABLE_GEOMETRY_UNSUPPORTED")
        geometries[number] = PageGeometry(box[2], box[3], 0, box)
    stream = io.BytesIO()
    writer.write(stream)
    subset = stream.getvalue()
    raw = receipt.get("raw_response")
    mode = receipt.get("mode")
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("usage"), dict)
        or not isinstance(raw.get("model"), str)
        or not isinstance(mode, str)
    ):
        raise ValueError("UPSTAGE_TABLE_RECEIPT_MISMATCH")
    request = dict(
        model=PARSE_MODEL_PINNED,
        mode=mode,
        pdf_sha256=sha256(subset).hexdigest(),
        pages=len(physical_pages),
        bytes_len=len(subset),
    )
    if (
        mode not in ALLOWED_MODES
        or receipt.get("request_sha256") != canonical_hash(request)
        or receipt.get("response_sha256") != canonical_hash(raw)
        or raw.get("model") not in ALLOWED_MODELS
        or receipt.get("provider_model") != raw["model"]
        or not isinstance(raw.get("elements"), list)
        or len(raw["elements"]) > 10000
        or type(raw.get("usage", {}).get("pages")) is not int
        or raw["usage"]["pages"] != len(physical_pages)
    ):
        raise ValueError("UPSTAGE_TABLE_RECEIPT_MISMATCH")
    config = canonical_hash(
        dict(
            converter="upstage-table-candidates-v1",
            request_sha256=receipt["request_sha256"],
            response_sha256=receipt["response_sha256"],
            physical_pages=physical_pages,
            pypdf_version=version("pypdf"),
        )
    )
    run_id = str(uuid5(UUID(parse_manifest_id), config))
    blocks, edges, identifiers = [], [], set()
    for element in raw["elements"]:
        if not isinstance(element, dict):
            raise ValueError("UPSTAGE_TABLE_ELEMENT_INVALID")
        identifier, page_index = element.get("id"), element.get("page")
        if (
            type(identifier) is not int
            or identifier < 0
            or identifier in identifiers
            or type(page_index) is not int
            or not 1 <= page_index <= len(physical_pages)
        ):
            raise ValueError("UPSTAGE_TABLE_ELEMENT_INVALID")
        identifiers.add(identifier)
        if element.get("category") != "table":
            continue
        page = physical_pages[page_index - 1]
        geometry = geometries[page]
        coords = element.get("coordinates")
        if (
            not isinstance(coords, list)
            or len(coords) != 4
            or any(
                not isinstance(p, dict)
                or set(p) != {"x", "y"}
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1
                    for v in p.values()
                )
                for p in coords
            )
        ):
            raise ValueError("UPSTAGE_TABLE_COORDINATES_INVALID")
        x0, x1 = min(p["x"] for p in coords), max(p["x"] for p in coords)
        y0, y1 = min(p["y"] for p in coords), max(p["y"] for p in coords)
        if (
            x0 >= x1
            or y0 >= y1
            or {(p["x"], p["y"]) for p in coords} != {(x, y) for x in (x0, x1) for y in (y0, y1)}
        ):
            raise ValueError("UPSTAGE_TABLE_COORDINATES_INVALID")
        content = element.get("content")
        if not isinstance(content, dict):
            raise ValueError("UPSTAGE_TABLE_CONTENT_INVALID")
        cells = parse_table_cells(content.get("html"))
        text = content.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 200000:
            raise ValueError("UPSTAGE_TABLE_TEXT_INVALID")
        table_id = f"table-{identifier}"
        width, height = geometry.width_pt, geometry.height_pt
        native = NativeSource(
            source.document_version_id,
            parse_manifest_id,
            run_id,
            table_id,
            page,
            None,
            (x0 * width, (1 - y1) * height, x1 * width, (1 - y0) * height),
            "pdf_bottom_left_points",
            text,
            0,
            len(text),
        )
        blocks.append(
            CandidateBlock(
                "table",
                native,
                geometry,
                parser_bbox=(x0, y0, x1, y1),
                parser_coordinate_system="upstage_normalized_top_left",
                parser_to_canonical=(width, 0, 0, height, 0, 0),
                table_native_id=table_id,
            )
        )
        for cell in cells:
            cell_id = f"{table_id}-r{cell['row']}-c{cell['column']}"
            native = NativeSource(
                source.document_version_id,
                parse_manifest_id,
                run_id,
                cell_id,
                page,
                None,
                None,
                "pdf_bottom_left_points",
                cell["text"],
                0,
                len(cell["text"]),
            )
            blocks.append(
                CandidateBlock(
                    "table_cell",
                    native,
                    geometry,
                    table_native_id=table_id,
                    row_number=cell["row"],
                    column_number=cell["column"],
                    row_span=cell["row_span"],
                    column_span=cell["column_span"],
                )
            )
            edges.append(CandidateEdge(cell_id, table_id, "table_parent"))
            if len(blocks) > 10000:
                raise ValueError("UPSTAGE_TABLE_CELL_LIMIT")
    return CandidateBatch(
        source.tenant_id,
        source.document_version_id,
        parse_manifest_id,
        source.sha256,
        run_id,
        "upstage-document-parse",
        raw["model"],
        "upstage-document-parse",
        config,
        tuple(blocks),
        tuple(edges),
        synthetic=source.synthetic,
    )
