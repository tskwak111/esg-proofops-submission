"""Turn coordinate-bearing Upstage async output into native-grounded candidates."""

from __future__ import annotations

import io
import math
from collections import Counter
from hashlib import sha256
from uuid import uuid4

import pdfplumber
from proofops.adapters.parsing.gemini_vision import _ground, compact, fragment
from proofops.adapters.parsing.html_table_cells import parse_table_cells
from proofops.application.ingest.graph_fusion import CandidateBatch, CandidateBlock, CandidateEdge
from proofops.domain.documents import NativeSource, PageGeometry
from pypdf import PdfReader, PdfWriter

MODEL = "document-parse-260128"


def selected_pdf(content: bytes, pages: tuple[int, ...]) -> bytes:
    reader = PdfReader(io.BytesIO(content), strict=True)
    if reader.is_encrypted or not pages or max(pages) > len(reader.pages):
        raise ValueError("UPSTAGE_PAGE_SELECTION_INVALID")
    writer = PdfWriter()
    for number in pages:
        writer.add_page(reader.pages[number - 1])
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _element_box(element, page):
    coords = element.get("coordinates")
    if (
        not isinstance(coords, list)
        or len(coords) != 4
        or any(
            not isinstance(point, dict)
            or set(point) != {"x", "y"}
            or any(
                type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1
                for value in point.values()
            )
            for point in coords
        )
    ):
        return None
    x0, x1 = min(p["x"] for p in coords), max(p["x"] for p in coords)
    y0, y1 = min(p["y"] for p in coords), max(p["y"] for p in coords)
    if x0 >= x1 or y0 >= y1:
        return None
    return (x0 * page.width, y0 * page.height, x1 * page.width, y1 * page.height)


def _union(boxes):
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def candidate_batch(source, profile, selected, batches, *, mode: str, config_hash: str):
    if not isinstance(batches, list) or not batches or mode not in {"standard", "enhanced"}:
        raise ValueError("UPSTAGE_RESPONSE_INVALID")
    if sha256(source.content).hexdigest() != source.sha256:
        raise ValueError("UPSTAGE_SOURCE_MISMATCH")
    run_id = str(uuid4())
    blocks, edges, stats = [], [], Counter()
    seen_pages, pages_with_output = set(), set()
    failed_html, low_grounding = set(), set()
    with pdfplumber.open(io.BytesIO(source.content)) as document:
        page_words = {p: document.pages[p - 1].extract_words() for p in selected}
        headings = {p: "" for p in selected}
        cell_counts = {p: [0, 0] for p in selected}

        def add(
            kind,
            text,
            page_num,
            identifier,
            provider_box,
            *,
            parent=None,
            row=None,
            column=None,
            row_span=None,
            column_span=None,
            derived_box=None,
        ):
            if not isinstance(text, str):
                raise ValueError("UPSTAGE_ELEMENT_INVALID")
            page = document.pages[page_num - 1]
            plain = page.rotation == 0 and tuple(page.cropbox[:2]) == (0, 0)
            box, offset = (
                _ground(text, page_words[page_num], provider_box)
                if plain and provider_box is not None
                else (None, None)
            )
            if derived_box is not None and plain:
                box, offset = derived_box, None
            if box is None:
                stats["vision_only"] += 1
                if kind == "table_cell":
                    stats["vision_only_cells"] += 1
            else:
                stats["grounded"] += 1
                if kind == "table_cell":
                    stats["grounded_cells"] += 1
            native_box = (
                (box[0], page.height - box[3], box[2], page.height - box[1]) if box else None
            )
            geometry = PageGeometry(
                page.width, page.height, page.rotation, tuple(map(float, page.cropbox))
            )
            context = (
                (
                    "section=" + headings[page_num],
                    "provider_bbox=" + ",".join(f"{n:.3f}" for n in provider_box),
                )
                if provider_box
                else ("section=" + headings[page_num],)
            )
            context += (
                ("vision_only",)
                if box is None
                else (
                    (
                        f"native_char_start={offset}",
                        f"native_char_end={offset + len(compact(text))}",
                    )
                    if offset is not None
                    else ("derived_from_native_cells",)
                )
            )
            native = NativeSource(
                source.document_version_id,
                profile.parse_manifest_id,
                run_id,
                identifier,
                page_num,
                None,
                native_box,
                "pdf_bottom_left_points",
                text,
                0,
                len(text),
            )
            blocks.append(
                CandidateBlock(
                    kind,
                    native,
                    geometry,
                    context,
                    table_native_id=parent,
                    row_number=row,
                    column_number=column,
                    row_span=row_span,
                    column_span=column_span,
                )
            )
            if parent:
                edges.append(CandidateEdge(identifier, parent, "table_parent"))
            return box

        for batch_index, batch in enumerate(batches):
            if batch.get("model") not in {MODEL, "document-parse"} or not isinstance(
                batch.get("elements"), list
            ):
                raise ValueError("UPSTAGE_RESPONSE_INVALID")
            usage = batch.get("usage", {})
            observed = usage.get(mode)
            if not isinstance(observed, list) or usage.get("pages") != len(observed):
                raise ValueError("UPSTAGE_RESPONSE_INVALID")
            if (
                any(type(p) is not int for p in observed)
                or len(set(observed)) != len(observed)
                or seen_pages.intersection(observed)
            ):
                raise ValueError("UPSTAGE_PAGE_MAPPING_INVALID")
            seen_pages.update(observed)
            for index, element in enumerate(batch["elements"]):
                if not isinstance(element, dict) or type(element.get("page")) is not int:
                    raise ValueError("UPSTAGE_ELEMENT_INVALID")
                page_index = element["page"]
                if not 1 <= page_index <= len(selected) or page_index not in observed:
                    raise ValueError("UPSTAGE_PAGE_MAPPING_INVALID")
                page_num = selected[page_index - 1]
                page = document.pages[page_num - 1]
                provider_box = _element_box(element, page)
                category = element.get("category")
                content = element.get("content")
                if not isinstance(category, str) or not isinstance(content, dict):
                    raise ValueError("UPSTAGE_ELEMENT_INVALID")
                text = content.get("text", "")
                if not isinstance(text, str):
                    raise ValueError("UPSTAGE_ELEMENT_INVALID")
                identifier = f"up-{mode}-{batch_index}-{index}"
                pages_with_output.add(page_num) if text.strip() else None
                stats["elements"] += 1
                if category == "table":
                    stats["tables"] += 1
                    try:
                        cells = parse_table_cells(content.get("html"))
                    except ValueError:
                        failed_html.add(page_num)
                        add("table", text, page_num, identifier, provider_box)
                        continue
                    cell_boxes = []
                    grid = {}
                    cell_counts[page_num][1] += len(cells)
                    for ci, cell in enumerate(cells):
                        cell_id = f"{identifier}-c{ci}"
                        cell_box = add(
                            "table_cell",
                            cell["text"],
                            page_num,
                            cell_id,
                            provider_box,
                            parent=identifier,
                            row=cell["row"],
                            column=cell["column"],
                            row_span=cell["row_span"],
                            column_span=cell["column_span"],
                        )
                        cell_boxes.append(cell_box)
                        cell_counts[page_num][0] += cell_box is not None
                        grid[cell["row"], cell["column"]] = cell["text"]
                    nr = max(c["row"] + c["row_span"] for c in cells)
                    nc = max(c["column"] + c["column_span"] for c in cells)
                    table_text = "\n".join(
                        "\t".join(grid.get((r, c), "") for c in range(nc)) for r in range(nr)
                    )
                    add(
                        "table",
                        table_text,
                        page_num,
                        identifier,
                        provider_box,
                        derived_box=_union(cell_boxes) if all(cell_boxes) else None,
                    )
                else:
                    kind = (
                        "heading"
                        if category.startswith("heading")
                        else "figure"
                        if category in {"figure", "image", "chart"}
                        else "unknown"
                        if category in {"header", "footer"}
                        else "paragraph"
                    )
                    if category.startswith("heading"):
                        headings[page_num] = text.strip()
                    elif category == "paragraph":
                        stats["prose"] += 1
                        stats["fragments"] += fragment(text)
                    add(kind, text, page_num, identifier, provider_box)
        if seen_pages != set(range(1, len(selected) + 1)):
            raise ValueError("UPSTAGE_PAGE_MAPPING_INVALID")
        for page_num, (grounded, total) in cell_counts.items():
            if total >= 4 and grounded / total < 0.5:
                low_grounding.add(page_num)
        # An absent native layer or empty provider output needs the image fallback.
        gemini_pages = sorted(
            p
            for p in selected
            if p not in pages_with_output
            or not (document.pages[p - 1].extract_text() or "").strip()
        )
    batch = CandidateBatch(
        source.tenant_id,
        source.document_version_id,
        profile.parse_manifest_id,
        source.sha256,
        run_id,
        f"upstage_async_{mode}",
        MODEL,
        "upstage-document-parse",
        config_hash,
        tuple(blocks),
        tuple(edges),
    )
    metrics = dict(
        mode=mode,
        selected_pages=list(selected),
        elements=stats["elements"],
        tables=stats["tables"],
        cells=sum(v[1] for v in cell_counts.values()),
        grounded=stats["grounded"],
        vision_only=stats["vision_only"],
        grounded_cells=stats["grounded_cells"],
        vision_only_cells=stats["vision_only_cells"],
        html_failed_pages=sorted(failed_html),
        low_grounding_pages=sorted(low_grounding),
        gemini_pages=gemini_pages,
        fragment_rate=stats["fragments"] / max(1, stats["prose"]),
    )
    return batch, metrics
