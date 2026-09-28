"""Original-PDF attestation for exact table-cell and table-row source spans."""

import io
import math
from contextlib import closing
from ctypes import c_double
from dataclasses import asdict, replace
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from unicodedata import combining, normalize

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from pdfminer.pdftypes import resolve1

from proofops.adapters.local import claim_source_verification as claim_verifier
from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry
from proofops.adapters.local.source_verification import _rendered_text
from proofops.application import claims as claim_validation
from proofops.application.evidence.citations import _LIGATURES, _normalized, verify_source_ref
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.provenance import canonical_hash

_TABLE_KINDS = {"table_cell", "table_row"}
_MAX_ROW_CELLS = 40
_LEGACY_TABLE_VERIFIER_SHA256 = "2973082b442d9d26504f50446fc1c9babbc154a75917992ff2d31aba479fc521"


def table_span_policy():
    """Pinned v1 policy; preserve its stored full-cell receipt bytes."""
    local = Path(__file__).parent
    return dict(
        schema="table_span_source_policy_v1",
        verifier_sha256=_LEGACY_TABLE_VERIFIER_SHA256,
        quote_guard_sha256=sha256(Path(claim_verifier.__file__).read_bytes()).hexdigest(),
        claim_validation_sha256=sha256(Path(claim_validation.__file__).read_bytes()).hexdigest(),
        glyph_geometry_sha256=sha256(
            local.joinpath("native_glyph_geometry.py").read_bytes()
        ).hexdigest(),
        rendered_reader_sha256=sha256(
            local.joinpath("source_verification.py").read_bytes()
        ).hexdigest(),
        ocr_script_sha256=sha256(local.joinpath("native_ocr.swift").read_bytes()).hexdigest(),
        citation_sha256=sha256(
            Path(verify_source_ref.__code__.co_filename).read_bytes()
        ).hexdigest(),
        readers={name: version(name) for name in ("pdfplumber", "pdfminer.six", "pypdfium2")},
    )


def line_span_policy():
    """Separate v2 policy for glyph-bounded quote crops."""
    local = Path(__file__).parent
    return dict(
        schema="table_span_line_source_policy_v2",
        verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        glyph_geometry_sha256=sha256(
            local.joinpath("native_glyph_geometry.py").read_bytes()
        ).hexdigest(),
        rendered_reader_sha256=sha256(
            local.joinpath("source_verification.py").read_bytes()
        ).hexdigest(),
        ocr_script_sha256=sha256(local.joinpath("native_ocr.swift").read_bytes()).hexdigest(),
        citation_sha256=sha256(
            Path(verify_source_ref.__code__.co_filename).read_bytes()
        ).hexdigest(),
        quote_guard_sha256=sha256(Path(claim_verifier.__file__).read_bytes()).hexdigest(),
        readers={name: version(name) for name in ("pdfplumber", "pdfminer.six", "pypdfium2")},
    )


def line_span_policy_sha256():
    return canonical_hash(line_span_policy())


def _candidate_bound(graph, candidate):
    return any(candidate in batch.blocks for batch in graph.candidates)


def _has_winner(block):
    return type(block.winner) is int and 0 <= block.winner < len(block.candidates)


def _inside(inner, outer):
    return (
        outer[0] - 0.001 <= inner[0] < inner[2] <= outer[2] + 0.001
        and outer[1] - 0.001 <= inner[1] < inner[3] <= outer[3] + 0.001
    )


def _selected_box(block):
    return block.candidates[block.winner].bbox if _has_winner(block) else None


def _matches_raw_text(text, raw_text):
    return isinstance(text, str) and _normalized(text) == _normalized(raw_text)


def _matches_line_text(text, quote):
    return isinstance(text, str) and _normalized(text).replace(" ", "") == _normalized(
        quote
    ).replace(" ", "")


def _candidate_table_matches(graph, candidate, table_id):
    if candidate.table_native_id is None:
        return True
    for batch in graph.candidates:
        if candidate not in batch.blocks:
            continue
        table_candidates = [
            item
            for item in batch.blocks
            if item.kind == "table" and item.source.source_native_id == candidate.table_native_id
        ]
        owners = [
            block.source_id
            for block in graph.blocks
            if block.kind == "table" and any(item in block.candidates for item in table_candidates)
        ]
        return len(table_candidates) == 1 and owners == [table_id]
    return False


def _table_layout(graph, target):
    """Return a unique table/row/column layout or refuse ambiguous geometry."""
    blocks = {block.source_id: block for block in graph.blocks}
    parents = {}
    children = {}
    for edge in graph.edges:
        if edge.relation == "table_parent":
            parents.setdefault(edge.source_id, []).append(edge.target_id)
            children.setdefault(edge.target_id, []).append(edge.source_id)
    if target.kind == "table_row":
        row = target
    elif target.kind == "table_cell":
        row_ids = [
            source_id
            for source_id in parents.get(target.source_id, ())
            if blocks.get(source_id) and blocks[source_id].kind == "table_row"
        ]
        if len(row_ids) != 1:
            return None
        row = blocks[row_ids[0]]
    else:
        return None
    table_ids = [
        source_id
        for source_id in parents.get(row.source_id, ())
        if blocks.get(source_id) and blocks[source_id].kind == "table"
    ]
    if len(table_ids) != 1:
        return None
    table_id = table_ids[0]
    table = blocks.get(table_id)
    if (
        table is None
        or not _has_winner(table)
        or table.quality not in {"verified", "unverified"}
        or not _candidate_bound(graph, table.candidates[table.winner])
    ):
        return None
    table_box = _selected_box(table)
    if (
        table.page_num != row.page_num
        or table.candidates[table.winner].source.physical_page != row.page_num
        or table_box is None
    ):
        return None
    if not _candidate_table_matches(graph, row.candidates[row.winner], table_id):
        return None
    direct_tables = [
        source_id
        for source_id in parents.get(target.source_id, ())
        if blocks.get(source_id) and blocks[source_id].kind == "table"
    ]
    if direct_tables and (len(direct_tables) != 1 or direct_tables[0] != table_id):
        return None

    row_ids = [
        edge.source_id
        for edge in graph.edges
        if edge.relation == "table_parent"
        and edge.target_id == table_id
        and blocks.get(edge.source_id) is not None
        and blocks[edge.source_id].kind == "table_row"
    ]
    if row.source_id not in row_ids or len(set(row_ids)) != len(row_ids):
        return None
    table_rows = [
        blocks[source_id] for source_id in row_ids if blocks[source_id].page_num == row.page_num
    ]
    if any(
        not _has_winner(item)
        or item.quality not in {"verified", "unverified"}
        or item.page_num != row.page_num
        or item.candidates[item.winner].source.physical_page != row.page_num
        or (row_box := _selected_box(item)) is None
        or not _inside(row_box, table_box)
        for item in table_rows
    ):
        return None

    rows = {}
    all_cells = []
    for item in table_rows:
        source_ids = children.get(item.source_id, ())
        cells = [blocks.get(source_id) for source_id in source_ids]
        if (
            not cells
            or len(cells) > _MAX_ROW_CELLS
            or len(set(source_ids)) != len(source_ids)
            or any(cell is None for cell in cells)
        ):
            return None
        if any(
            [
                parent
                for parent in parents.get(cell.source_id, ())
                if blocks.get(parent) and blocks[parent].kind == "table_row"
            ]
            != [item.source_id]
            or [
                parent
                for parent in parents.get(cell.source_id, ())
                if blocks.get(parent) and blocks[parent].kind == "table"
            ]
            not in ([], [table_id])
            for cell in cells
        ):
            return None
        box = _selected_box(item)
        if any(
            cell.kind != "table_cell"
            or not _has_winner(cell)
            or cell.quality not in {"verified", "unverified"}
            or cell.page_num != item.page_num
            or not _candidate_bound(graph, cell.candidates[cell.winner])
            or not _candidate_table_matches(graph, cell.candidates[cell.winner], table_id)
            or (cell_box := _selected_box(cell)) is None
            or not _inside(cell_box, box)
            or not (
                cell.candidates[cell.winner].row_span is None
                or type(cell.candidates[cell.winner].row_span) is int
                and cell.candidates[cell.winner].row_span == 1
            )
            or not (
                cell.candidates[cell.winner].column_span is None
                or type(cell.candidates[cell.winner].column_span) is int
                and cell.candidates[cell.winner].column_span == 1
            )
            for cell in cells
        ):
            return None
        numbers = [cell.candidates[cell.winner].row_number for cell in cells]
        row_number = item.candidates[item.winner].row_number
        if row_number is not None:
            if type(row_number) is not int or any(
                n is not None and n != row_number for n in numbers
            ):
                return None
            identity = ("index", row_number)
        elif all(type(number) is int for number in numbers) and len(set(numbers)) == 1:
            identity = ("index", numbers[0])
        elif all(number is None for number in numbers):
            identity = None
        else:
            return None
        columns = [cell.candidates[cell.winner].column_number for cell in cells]
        if any(type(column) is not int for column in columns) or len(set(columns)) != len(columns):
            return None
        ordered = sorted(zip(columns, cells, strict=True), key=lambda entry: entry[0])
        if any(
            _selected_box(left)[2] > _selected_box(right)[0] + 0.001
            for (_, left), (_, right) in zip(ordered, ordered[1:])
        ):
            return None
        rows[item.source_id] = dict(
            block=item, box=box, identity=identity, cells=[c for _, c in ordered]
        )
        all_cells.extend(cells)

    ordered_rows = sorted(rows.values(), key=lambda entry: entry["box"][1])
    if any(
        left["box"][3] > right["box"][1] + 0.001
        for left, right in zip(ordered_rows, ordered_rows[1:])
    ):
        return None
    if all(entry["identity"] is None for entry in ordered_rows):
        for index, entry in enumerate(ordered_rows):
            entry["identity"] = ("geometry", index)
    elif any(entry["identity"] is None for entry in ordered_rows):
        return None
    identities = [entry["identity"] for entry in ordered_rows]
    if len(set(identities)) != len(identities):
        return None
    if identities[0][0] == "index" and any(
        left[1] >= right[1] for left, right in zip(identities, identities[1:])
    ):
        return None

    for index, cell in enumerate(all_cells):
        box = _selected_box(cell)
        if any(
            min(box[2], other[2]) > max(box[0], other[0]) + 0.001
            and min(box[3], other[3]) > max(box[1], other[1]) + 0.001
            for other in (_selected_box(item) for item in all_cells[index + 1 :])
        ):
            return None
    column_centers = {}
    for cell in all_cells:
        column = cell.candidates[cell.winner].column_number
        box = _selected_box(cell)
        column_centers.setdefault(column, []).append((box[0] + box[2]) / 2)
    ordered_columns = [
        (column, min(centers), max(centers)) for column, centers in sorted(column_centers.items())
    ]
    if any(
        left[2] >= right[1] - 0.001 for left, right in zip(ordered_columns, ordered_columns[1:])
    ):
        return None

    table_cell_ids = {
        source_id
        for source_id in children.get(table_id, ())
        if blocks.get(source_id) is not None
        and blocks[source_id].kind == "table_cell"
        and blocks[source_id].page_num == row.page_num
    }
    if table_cell_ids - {cell.source_id for cell in all_cells}:
        return None

    return dict(table_id=table_id, rows=rows)


def _row_cells(graph, row):
    layout = _table_layout(graph, row)
    if layout is None or row.source_id not in layout["rows"]:
        return None
    cells = layout["rows"][row.source_id]["cells"]
    if "\t".join(cell.raw_text for cell in cells) != row.raw_text:
        return None
    return cells


def _row_segments(row, cells, ref):
    cursor = 0
    segments = []
    for index, cell in enumerate(cells):
        start, end = cursor, cursor + len(cell.raw_text)
        left, right = max(start, ref.char_start), min(end, ref.char_end)
        if left < right:
            segments.append((cell, row.raw_text[left:right]))
        cursor = end + (index < len(cells) - 1)
    if not segments or _normalized(" ".join(text for _, text in segments)) != _normalized(
        ref.quote
    ):
        return None
    return segments


def _page_reading(source, document, block, glyphs):
    if not _has_winner(block):
        return dict(status="unresolved", reason="source_invalid", bbox=None)
    candidate = block.candidates[block.winner]
    box, geometry = candidate.bbox, candidate.geometry
    unresolved = dict(status="unresolved", reason="geometry_unsupported", bbox=box)
    if (
        box is None
        or candidate.has_invalid_geometry
        or not 1 <= block.page_num <= len(document.pages)
    ):
        return unresolved
    page = document.pages[block.page_num - 1]
    if (
        page.rotation
        or geometry.rotation
        or tuple(page.bbox[:2]) != (0, 0)
        or tuple(geometry.crop_box[:2]) != (0, 0)
        or abs(page.width - geometry.width_pt) > 0.001
        or abs(page.height - geometry.height_pt) > 0.001
        or not (0 <= box[0] < box[2] <= page.width and 0 <= box[1] < box[3] <= page.height)
    ):
        return unresolved
    if block.page_num not in glyphs:
        words = page.extract_words()
        try:
            proof = native_word_ink_geometry(source, block.page_num, list(range(len(words))))
        except ValueError:
            proof = {}
        glyphs[block.page_num] = words, proof
    words, proof = glyphs[block.page_num]
    if not isinstance(proof.get("matched_words"), list):
        return dict(status="unresolved", reason="glyph_geometry_unresolved", bbox=box)
    boxes = {word["native_word_index"]: word["ink_bbox"] for word in proof["matched_words"]}
    missing = set(proof.get("unresolved_word_indices", ()))
    if set(boxes) | missing != set(range(len(words))) or any(
        words[i]["x1"] > box[0]
        and words[i]["x0"] < box[2]
        and words[i]["bottom"] > box[1]
        and words[i]["top"] < box[3]
        for i in missing
    ):
        return dict(status="unresolved", reason="glyph_geometry_unresolved", bbox=box)
    selected = []
    for index, word in enumerate(words):
        if index not in boxes:
            continue
        word_box = boxes[index]
        if (
            word_box[2] <= box[0]
            or word_box[0] >= box[2]
            or word_box[3] <= box[1]
            or word_box[1] >= box[3]
        ):
            continue
        if not word["upright"] or not _inside(word_box, box):
            return dict(status="unresolved", reason="clipped_or_rotated_words", bbox=box)
        selected.append(dict(index=index, text=word["text"], bbox=word_box))
    native_parts, offset = [], 0
    for word in selected:
        word["char_start"] = offset
        offset += len(word["text"])
        word["char_end"] = offset
        native_parts.append(word["text"])
        offset += 1
    native = " ".join(native_parts)
    rendered = _rendered_text(page, box, padding_px=6)
    return dict(
        status="read",
        reason=None,
        page=block.page_num,
        bbox=box,
        native_text=native,
        words=selected,
        rendered=rendered,
        glyph_geometry=proof,
    )


def _normalized_glyph_stream(parts):
    """Normalize text with a conservative source-glyph map for exact quote spans."""
    clusters = []
    normalized_chars, origins = [], []

    def flush():
        if clusters:
            text = normalize("NFC", "".join(char for char, _ in clusters))
            glyph_ids = frozenset(index for _, ids in clusters for index in ids)
            normalized_chars.extend(text)
            origins.extend([glyph_ids] * len(text))
            clusters.clear()

    for char, ids in parts:
        for translated in char.translate(_LIGATURES):
            if clusters and combining(translated) == 0:
                current = normalize("NFC", "".join(value for value, _ in clusters))
                extended = normalize("NFC", current + translated)
                if extended.startswith(current):
                    flush()
            clusters.append((translated, ids))
    flush()

    collapsed, mapped, pending, pending_space = [], [], set(), False
    for char, glyph_ids in zip(normalized_chars, origins, strict=True):
        if char.isspace():
            if collapsed:
                pending.update(glyph_ids)
                pending_space = True
        else:
            if pending_space:
                collapsed.append(" ")
                mapped.append(frozenset(pending))
                pending.clear()
                pending_space = False
            collapsed.append(char)
            mapped.append(glyph_ids)
    return "".join(collapsed), mapped


def _line_region(source, page, reading, quote, cell_box):
    """Map a unique native quote to one visual line and refuse intruding glyphs."""
    quote = _normalized(quote)
    text = reading.get("native_text")
    proof = reading.get("glyph_geometry", {})
    if not isinstance(text, str) or not isinstance(proof.get("matched_words"), list):
        return dict(reason="glyph_geometry_unresolved", region=None)
    words = page.extract_words(return_chars=True)
    proof_by_index = {
        item.get("native_word_index"): item
        for item in proof["matched_words"]
        if type(item.get("native_word_index")) is int
    }
    parts = []
    for position, item in enumerate(reading.get("words", ())):
        index = item.get("index")
        if type(index) is not int or not 0 <= index < len(words):
            return dict(reason="glyph_text_alignment_unresolved", region=None)
        word = words[index]
        chars = word.get("chars")
        mapped = proof_by_index.get(index, {}).get("pdfium_char_indices")
        if (
            word.get("text") != item.get("text")
            or not isinstance(chars, list)
            or not isinstance(mapped, list)
            or len(chars) != len(mapped)
            or "".join(char.get("text", "") for char in chars) != word.get("text")
        ):
            return dict(reason="glyph_text_alignment_unresolved", region=None)
        if position:
            parts.append((" ", frozenset()))
        for char, glyph_index in zip(chars, mapped, strict=True):
            value = char.get("text")
            if not isinstance(value, str) or len(value) != 1 or type(glyph_index) is not int:
                return dict(reason="glyph_text_alignment_unresolved", region=None)
            parts.append((value, frozenset((glyph_index,))))
    native, source_glyphs = _normalized_glyph_stream(parts)
    if native != _normalized(text):
        return dict(reason="glyph_text_alignment_unresolved", region=None)
    if not claim_verifier._unique_quote(native, quote):
        return dict(reason="native_quote_unresolved", region=None)
    start = native.find(quote)
    selected_ids = []
    for char, glyph_ids in zip(
        native[start : start + len(quote)], source_glyphs[start : start + len(quote)], strict=True
    ):
        if not char.isspace() and not glyph_ids:
            return dict(reason="glyph_quote_unmapped", region=None)
        for glyph_id in glyph_ids:
            if glyph_id not in selected_ids:
                selected_ids.append(glyph_id)
    if not selected_ids:
        return dict(reason="glyph_quote_unmapped", region=None)
    selected_set = set(selected_ids)

    try:
        with pdfium.PdfDocument(source) as native_document:
            if not 1 <= reading.get("page", 0) <= len(native_document):
                return dict(reason="glyph_geometry_unresolved", region=None)
            with closing(native_document[reading["page"] - 1]) as native_page:
                with closing(native_page.get_textpage()) as text_page:
                    count = text_page.count_chars()
                    if not 0 < count <= 20000 or any(index >= count for index in selected_ids):
                        return dict(reason="glyph_geometry_unresolved", region=None)
                    glyphs = {}
                    baselines = []
                    for index in range(count):
                        codepoint = pdfium_raw.FPDFText_GetUnicode(text_page, index)
                        try:
                            glyph_text = chr(codepoint)
                            left, bottom, right, top = text_page.get_charbox(index, loose=False)
                        except (ValueError, pdfium.PdfiumError):
                            return dict(reason="glyph_geometry_unresolved", region=None)
                        box = [left, page.height - top, right, page.height - bottom]
                        if not all(math.isfinite(value) for value in box):
                            return dict(reason="glyph_geometry_unresolved", region=None)
                        if not (
                            0 <= box[0] < box[2] <= page.width
                            and 0 <= box[1] < box[3] <= page.height
                        ):
                            if glyph_text.isspace():
                                continue
                            return dict(reason="glyph_geometry_unresolved", region=None)
                        glyphs[index] = dict(text=glyph_text, bbox=box)
                        if index in selected_set and not glyph_text.isspace():
                            origin_x, origin_y = c_double(), c_double()
                            if not pdfium_raw.FPDFText_GetCharOrigin(
                                text_page, index, origin_x, origin_y
                            ) or not math.isfinite(origin_y.value):
                                return dict(reason="glyph_geometry_unresolved", region=None)
                            baselines.append(origin_y.value)
    except (OSError, ValueError, pdfium.PdfiumError):
        return dict(reason="glyph_geometry_unresolved", region=None)

    selected = [glyphs.get(index) for index in selected_ids]
    if any(item is None for item in selected):
        return dict(reason="glyph_geometry_unresolved", region=None)
    boxes = [item["bbox"] for item in selected]
    region = [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]
    if (
        not baselines
        or max(baselines) - min(baselines) > 0.001
        or max(box[1] for box in boxes) >= min(box[3] for box in boxes) - 0.001
    ):
        return dict(reason="quote_not_one_visual_line", region=region)
    if not _inside(region, cell_box):
        return dict(reason="line_region_outside_cell", region=region)
    if any(
        index not in selected_set
        and not item["text"].isspace()
        and min(region[2], item["bbox"][2]) > max(region[0], item["bbox"][0]) + 0.001
        and min(region[3], item["bbox"][3]) > max(region[1], item["bbox"][1]) + 0.001
        for index, item in glyphs.items()
    ):
        return dict(reason="foreign_glyph_in_region", region=region)
    return dict(reason=None, region=region, native_text=native[start : start + len(quote)])


def _line_attestation(source, page, reading, quote, cell_box):
    result = _line_region(source, page, reading, quote, cell_box)
    if result["reason"] is not None:
        return dict(status="unresolved", **result, rendered=None)
    rendered = _rendered_text(page, result["region"], padding_px=6)
    status = (
        "verified"
        if (rendered.get("status") == "read" and _matches_line_text(rendered.get("text"), quote))
        else "unresolved"
    )
    return dict(
        status=status,
        reason=None
        if status == "verified"
        else (
            "rendered_line_mismatch"
            if rendered.get("status") == "read"
            else "rendered_line_unresolved"
        ),
        region=result["region"],
        native_text=result["native_text"],
        comparison="nfc_ignore_whitespace_v1",
        rendered=rendered,
    )


def _record_line_attestation(record, source, page, reading, quote, cell_box):
    attestation = _line_attestation(source, page, reading, quote, cell_box)
    record.update(
        method="native_glyph_line_v2",
        line_policy_sha256=line_span_policy_sha256(),
        line_attestation=attestation,
    )
    if attestation["status"] == "verified":
        record.update(status="verified", reason=None)


def _checked_ref(graph, ref, block, tenant_id):
    checked = replace(
        graph,
        blocks=tuple(
            replace(item, quality="verified") if item.source_id == ref.source_id else item
            for item in graph.blocks
        ),
    )
    return verify_source_ref(ref, checked, tenant_id=tenant_id).verification_state == "verified"


def attest_table_spans(graph, source_pdf_bytes, refs, *, tenant_id):
    """Attest exact cell/row quotes using original native glyphs and a cropped OCR read."""
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source_pdf_bytes, bytes)
        or len(source_pdf_bytes) > 100 * 1024 * 1024
        or sha256(source_pdf_bytes).hexdigest() != graph.source_sha256
    ):
        raise ValueError("TABLE_SPAN_SOURCE_MISMATCH")
    blocks = {block.source_id: block for block in graph.blocks}
    records, readings, glyphs = [], {}, {}
    with pdfplumber.open(io.BytesIO(source_pdf_bytes)) as document:
        interactive = "OCProperties" in document.doc.catalog or (
            "AcroForm" in document.doc.catalog
            and not claim_verifier._pushbuttons_only(resolve1(document.doc.catalog.get("AcroForm")))
        )
        for ref in refs:
            record = dict(ref=asdict(ref), status="unresolved", reason="source_invalid")
            records.append(record)
            block = blocks.get(ref.source_id)
            if (
                block is None
                or block.kind not in _TABLE_KINDS
                or block.quality not in {"verified", "unverified"}
                or not _has_winner(block)
                or not _candidate_bound(graph, block.candidates[block.winner])
                or not _checked_ref(graph, ref, block, tenant_id)
            ):
                if block is not None and block.kind not in _TABLE_KINDS:
                    record["reason"] = "unsupported_block_kind"
                continue
            box = _selected_box(block)
            if box is None:
                record["reason"] = "geometry_unsupported"
                continue
            if not 1 <= block.page_num <= len(document.pages):
                record["reason"] = "page_unavailable"
                continue
            if interactive or claim_verifier._appearance_overlaps(
                document.pages[block.page_num - 1], box
            ):
                record["reason"] = "interactive_visibility_requires_review"
                continue
            if block.source_id not in readings:
                if block.kind == "table_cell":
                    layout = _table_layout(graph, block)
                    readings[block.source_id] = (
                        dict(status="unresolved", reason="cell_structure_unresolved")
                        if layout is None
                        else _page_reading(source_pdf_bytes, document, block, glyphs)
                    )
                else:
                    cells = _row_cells(graph, block)
                    readings[block.source_id] = (
                        dict(status="unresolved", reason="row_structure_unresolved")
                        if cells is None
                        else dict(
                            status="read",
                            cells=[
                                dict(
                                    source_id=cell.source_id,
                                    raw_text=cell.raw_text,
                                    reading=_page_reading(source_pdf_bytes, document, cell, glyphs),
                                )
                                for cell in cells
                            ],
                        )
                    )
            reading = readings[block.source_id]
            quote = _normalized(ref.quote)
            if block.kind == "table_cell":
                if reading["status"] != "read":
                    record["reason"] = reading["reason"]
                elif not _matches_raw_text(reading["native_text"], block.raw_text):
                    record["reason"] = "native_cell_text_mismatch"
                    _record_line_attestation(
                        record,
                        source_pdf_bytes,
                        document.pages[block.page_num - 1],
                        reading,
                        quote,
                        box,
                    )
                elif reading["rendered"].get("status") != "read" or not _matches_raw_text(
                    reading["rendered"].get("text"), block.raw_text
                ):
                    record["reason"] = "rendered_cell_text_mismatch"
                    _record_line_attestation(
                        record,
                        source_pdf_bytes,
                        document.pages[block.page_num - 1],
                        reading,
                        quote,
                        box,
                    )
                elif not claim_verifier._unique_quote(reading["native_text"], quote):
                    record["reason"] = "native_quote_unresolved"
                else:
                    rendered_ok = claim_verifier._unique_quote(reading["rendered"]["text"], quote)
                    record.update(
                        status="verified" if rendered_ok else "unresolved",
                        reason=None if rendered_ok else "rendered_quote_unresolved",
                    )
                record["reading_sha256"] = canonical_hash(reading)
                continue
            cells = _row_cells(graph, block)
            segments = _row_segments(block, cells, ref) if cells is not None else None
            if segments is None or reading["status"] != "read":
                record["reason"] = "row_structure_unresolved"
                record["reading_sha256"] = canonical_hash(reading)
                continue
            by_id = {item["source_id"]: item["reading"] for item in reading["cells"]}
            if any(by_id[cell.source_id]["status"] != "read" for cell in cells):
                record["reason"] = "cell_reading_unresolved"
                record["reading_sha256"] = canonical_hash(reading)
                continue
            native_row = " ".join(by_id[cell.source_id]["native_text"] for cell in cells)

            def try_row_line():
                if len(segments) != 1 or not claim_verifier._unique_quote(native_row, quote):
                    return
                selected_cell, _ = segments[0]
                _record_line_attestation(
                    record,
                    source_pdf_bytes,
                    document.pages[block.page_num - 1],
                    by_id[selected_cell.source_id],
                    quote,
                    _selected_box(selected_cell),
                )

            if any(
                not _matches_raw_text(by_id[cell.source_id]["native_text"], cell.raw_text)
                for cell in cells
            ):
                record["reason"] = "native_cell_text_mismatch"
                try_row_line()
                record["reading_sha256"] = canonical_hash(reading)
                continue
            if any(
                by_id[cell.source_id]["rendered"].get("status") != "read"
                or not _matches_raw_text(
                    by_id[cell.source_id]["rendered"].get("text"), cell.raw_text
                )
                for cell in cells
            ):
                record["reason"] = "rendered_cell_text_mismatch"
                try_row_line()
                record["reading_sha256"] = canonical_hash(reading)
                continue
            rendered_row = " ".join(
                by_id[cell.source_id]["rendered"].get("text", "") for cell in cells
            )
            if not claim_verifier._unique_quote(native_row, quote):
                record["reason"] = "native_quote_unresolved"
            elif not claim_verifier._unique_quote(rendered_row, quote):
                record["reason"] = "rendered_quote_unresolved"
            elif any(
                not claim_verifier._unique_quote(by_id[cell.source_id]["native_text"], part)
                or not claim_verifier._unique_quote(
                    by_id[cell.source_id]["rendered"].get("text", ""), part
                )
                for cell, part in segments
            ):
                record["reason"] = "row_cell_quote_unresolved"
            else:
                record.update(status="verified", reason=None)
            record["reading_sha256"] = canonical_hash(reading)
    receipt = dict(
        schema="table_span_source_attestation_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        policy=table_span_policy(),
        records=records,
        readings=readings,
    )
    receipt["artifact_sha256"] = canonical_hash(receipt)
    return receipt
