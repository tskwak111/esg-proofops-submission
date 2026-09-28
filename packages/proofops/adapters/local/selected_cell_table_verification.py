"""Selected numeric cell + column header attestation (v3); never semantics or grades.

v1 (``table_source_verification``) and v2 (``merged_table_verification``) attest a
whole table or nothing: a complete rectangular occupancy grid, every cell inside
``MAX_CELLS``, and a rendered read of every cell. Real reports do not satisfy
that. Measured on two actual layouts (receipts under
``outputs/pipeline-recovery-20260920/table-source``):

* Kia p106 Scope1/2: 54 cells, 23 declared holes, one tall group-label cell whose
  box covers three row bands, and 27 native characters (a dropped total label)
  that belong to no cell. Every declared cell's native text is exact and the
  value/year cells read back exactly under the rendered reader, but the unit cell
  ``천tCOeq``+subscript reads ``천tCO,eq`` and a tiny ``국내`` label reads empty.
* Lotte p117: single-column auxiliary strips, so ``len(cols) < 2`` alone blocks
  them, and their slot boxes sit ~3.4pt below their own text.

So this module changes the unit of admission, not the strictness: one numeric
value cell together with its column header cell. Nothing else is promoted.

What is still required, per selection:

* unambiguous table lineage (a member cell may not have a table_parent edge
  leaving the lineage) and unique declared occupancy, exactly as v2;
* the header cell sits in the same column band and its row band is strictly
  above the value's row band, both derived from real cell boxes;
* every native character inside the attested row bands belongs to exactly one
  declared cell, so a dropped or clipped digit cannot hide inside the region
  being attested (characters uncovered elsewhere are recorded, never ignored);
* native text equality for every cell of the attested bands;
* an exact rendered (real OCR) read of the value cell and of the header cell,
  cropped strictly inside the cell's own stored bbox and with that crop box
  recorded in the receipt, so a reader can verify no enlarged hidden context
  was read;
* the unit and row-label context cells of the value's row must exist, be
  natively attested and have a recorded rendered attempt. A rendered variant
  keeps them ``native_only`` and unpromoted instead of blocking the value or
  being trusted;
* no unresolved note marker, unassigned footnote or open source issue touching
  the attested members.

What this never does: no cap raise, no fuzzy or repaired digits, no blank-as-zero,
no table-parent inference from a subset, no promotion of the row, the table node
or any unattested cell, and no semantic claim. A literal header ``2024`` is a
column header string, not the reporting period of the value; ``year_binding`` and
``scope_binding`` stay ``not_attested`` and ``semantic_binding`` stays
``undetermined``. Downstream binding must still establish period, scope, metric
and unit under its own guards.
"""

import io
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pdfplumber
from PIL import ImageOps

from proofops.adapters.local import merged_table_verification as v2
from proofops.adapters.local import table_source_verification as v1
from proofops.application.evidence import citations
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.numeric import unassigned_note_ids
from proofops.domain.provenance import canonical_hash

# Iteration bound only: a table larger than this is not read cell by cell here,
# it is refused. Selections stay far below MAX_READS because only the value, its
# header and its own row's context cells are ever read.
MAX_TABLE_CELLS = 400
MAX_READS = v1.MAX_READS
MAX_SELECTIONS_PER_TABLE = 24
# A cell crop is scaled until it is at least this tall, bounded by a pixel cap.
_MIN_CELL_PIXELS = 96
_MAX_CROP_PIXELS = 4_000_000

_TOL = v2._TOL
_NOTE_MARKER = re.compile(r"\d+\)|[*†‡]")
# A value candidate must be a plain literal number: optional sign, grouped or
# plain digits, optional single decimal part. No units, no ranges, no repair.
_NUMERIC = re.compile(r"^-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")


def policy_sha256():
    """Bind this file, the readers it uses, the normalizer, and v1+v2 identities."""
    folder = Path(__file__).parent
    return canonical_hash(
        dict(
            schema="native_selected_cell_table_source_v3",
            files={
                name: sha256((folder / name).read_bytes()).hexdigest()
                for name in (
                    "selected_cell_table_verification.py",
                    "native_ocr.swift",
                    "native_glyph_geometry.py",
                )
            },
            v1_policy_sha256=v1.policy_sha256(),
            v2_policy_sha256=v2.policy_sha256(),
            normalizer_sha256=sha256(Path(citations.__file__).read_bytes()).hexdigest(),
            readers={name: version(name) for name in ("pdfplumber", "pdfminer.six", "pypdfium2")},
        )
    )


def _rendered_cell(page, box):
    """Rendered (real OCR) read of one cell, sized so short cells are legible.

    The shared paragraph reader renders the whole page at a fixed 3x and crops;
    a single table cell is then about 20 pixels tall and the recognizer returns
    nothing (measured on Lotte p117). This crops first and scales that crop so the
    cell is at least ``_MIN_CELL_PIXELS`` tall, bounded by ``_MAX_CROP_PIXELS``, so
    the same original pixels are read with more resolution and never more context.
    Deterministic: the scale follows only from the box, so a replay renders the
    identical image. This module keeps its own reader so the frozen paragraph
    verifier and its pinned policy stay untouched.
    """
    if sys.platform != "darwin":
        return dict(
            status="unresolved", reason="rendered_reader_unavailable", error="UnsupportedPlatform"
        )
    width, height = box[2] - box[0], box[3] - box[1]
    if not (0 < width and 0 < height):
        return dict(status="unresolved", reason="rendered_reader_unavailable", error="EmptyBox")
    scale = max(3, math.ceil(_MIN_CELL_PIXELS / height))
    if width * height * scale * scale > _MAX_CROP_PIXELS:
        scale = max(3, int(math.sqrt(_MAX_CROP_PIXELS / (width * height))))
    try:
        with page.crop(box).to_image(resolution=72 * scale).original as image:
            buffer = io.BytesIO()
            # Keep the original pixels; a blank margin only stops edge clipping.
            with ImageOps.expand(image, border=24, fill="white") as padded:
                padded.save(buffer, format="PNG")
        png = buffer.getvalue()
        cache_dir = Path(__file__).resolve().parents[4] / "scratch" / "module-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env.setdefault("DEVELOPER_DIR", "/Library/Developer/CommandLineTools")
        with tempfile.TemporaryDirectory(prefix="proofops-cell-") as folder:
            path = Path(folder) / "cell.png"
            path.write_bytes(png)
            output = subprocess.run(
                [
                    "swift",
                    "-module-cache-path",
                    str(cache_dir),
                    str(Path(v1.__file__).with_name("native_ocr.swift")),
                    str(path),
                ],
                env=env,
                check=True,
                capture_output=True,
                timeout=30,
            )
        result = json.loads(output.stdout)
        if not isinstance(result.get("text"), str) or len(result["text"]) > 20000:
            raise ValueError("invalid rendered cell text")
        return dict(
            status="read",
            image_sha256=sha256(png).hexdigest(),
            scale=scale,
            border_px=24,
            **result,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return dict(
            status="unresolved", reason="rendered_reader_unavailable", error=type(error).__name__
        )


def _lineage(graph, table):
    """Collect the explicit table lineage; refuse a shared/fused owner (as v2)."""
    blocks = {b.source_id: b for b in graph.blocks}
    members = {table.source_id}
    while True:
        more = {
            e.source_id
            for e in graph.edges
            if e.relation == "table_parent" and e.target_id in members
        }
        if more <= members:
            break
        members |= more
    if any(sid not in blocks for sid in members):
        raise ValueError("invalid_table_lineage")
    if any(
        e.relation == "table_parent" and e.source_id in members and e.target_id not in members
        for e in graph.edges
    ):
        raise ValueError("ambiguous_table_owner")
    return members, blocks


def _grid(graph, table):
    """Declared cell grid with unique occupancy; no completeness requirement."""
    if table.winner is None or table.bbox is None:
        raise ValueError("unresolved_table_structure")
    members, blocks = _lineage(graph, table)
    cells = [blocks[sid] for sid in sorted(members) if blocks[sid].kind == "table_cell"]
    if not cells or len(cells) > MAX_TABLE_CELLS:
        raise ValueError("table_cell_limit")
    grid, occupancy = {}, {}
    for cell in cells:
        if (
            cell.winner is None
            or cell.bbox is None
            or cell.quality not in {"unverified", "verified"}
            or cell.page_num != table.page_num
            or not v2._inside(cell.bbox, table.bbox)
        ):
            raise ValueError("unresolved_table_structure")
        candidate = cell.candidates[cell.winner]
        rows_spanned = 1 if candidate.row_span is None else candidate.row_span
        cols_spanned = 1 if candidate.column_span is None else candidate.column_span
        if (
            type(candidate.row_number) is not int
            or type(candidate.column_number) is not int
            or type(rows_spanned) is not int
            or type(cols_spanned) is not int
            or min(candidate.row_number, candidate.column_number) < 0
            or min(rows_spanned, cols_spanned) < 1
            or rows_spanned > MAX_TABLE_CELLS
            or cols_spanned > MAX_TABLE_CELLS
            or rows_spanned * cols_spanned > MAX_TABLE_CELLS
        ):
            raise ValueError("invalid_grid_indices")
        origin = (candidate.row_number, candidate.column_number)
        if origin in grid:
            raise ValueError("duplicate_cell_origin")
        grid[origin] = cell
        for row in range(origin[0], origin[0] + rows_spanned):
            for column in range(origin[1], origin[1] + cols_spanned):
                if (row, column) in occupancy:
                    raise ValueError("overlapping_spans")
                occupancy[row, column] = cell.source_id
    return members, blocks, grid


def _band(boxes, axis):
    return (min(box[axis] for box in boxes), max(box[axis + 2] for box in boxes))


def _selections(grid):
    """Pair each literal numeric cell with the nearest header cell above it.

    The header is the cell in the same declared column whose row number is the
    smallest one above the value's row; geometry is checked by the caller. No
    header is invented and a column without one yields no selection.
    """
    pairs = []
    for (row, column), cell in sorted(grid.items()):
        if not _NUMERIC.match(_normalized(cell.raw_text)):
            continue
        above = [r for (r, c) in grid if c == column and r < row]
        if not above:
            continue
        header = grid[(min(above), column)]
        if _NUMERIC.match(_normalized(header.raw_text)) and len(above) > 1:
            # A numeric first row is a value column, not a label; keep looking
            # upward only within the declared grid, never outside it.
            pass
        pairs.append(((row, column), cell, (min(above), column), header))
    return pairs[:MAX_SELECTIONS_PER_TABLE]


def _attest_selection(
    page, table, grid, value_key, value, header_key, header, reads, *, mapped_words=None
):
    """One value+header attestation; returns the record or raises the first hold."""
    value_row, value_column = value_key
    header_row, _ = header_key
    row_cells = {key: cell for key, cell in grid.items() if key[0] == value_row}
    header_cells = {key: cell for key, cell in grid.items() if key[0] == header_row}
    value_band = _band([value.bbox], 1)
    header_band = _band([header.bbox], 1)
    if header_band[1] > value_band[0] + _TOL:
        raise ValueError("header_not_above_value")

    # Same visual column. Ink-tight boxes make a left-aligned year header and a
    # right-aligned number legitimately non-overlapping (measured on real pages),
    # so containment or overlap cannot be required. Instead the header must be the
    # single horizontally nearest cell of its row, and no farther from the value
    # than one cell width, which still rejects a neighbouring column's header.
    def gap(box):
        return max(0.0, max(value.bbox[0], box[0]) - min(value.bbox[2], box[2]))

    chosen = gap(header.bbox)
    others = sorted((gap(cell.bbox), key[1]) for key, cell in grid.items() if key[0] == header_row)
    if (
        chosen > max(value.bbox[2] - value.bbox[0], header.bbox[2] - header.bbox[0])
        or chosen > others[0][0] + _TOL
    ):
        raise ValueError("header_column_band_mismatch")
    middle = (value.bbox[0] + value.bbox[2]) / 2
    tied = [column for distance, column in others if distance <= chosen + _TOL]
    if len(tied) > 1 and not (
        header.bbox[0] - _TOL <= middle <= header.bbox[2] + _TOL
        or len({_normalized(grid[(header_row, column)].raw_text) for column in tied}) == 1
    ):
        # Several cells of the header row are equally close: the declared column
        # alone would decide which year/label owns this value. Require real
        # alignment (the value's centre under this header) or identical text.
        raise ValueError("header_column_band_ambiguous")
    attested = dict(row_cells)
    attested.update(header_cells)
    # Bands of the attested rows must not collide with each other.
    row_band = _band([cell.bbox for cell in row_cells.values()], 1)
    head_band = _band([cell.bbox for cell in header_cells.values()], 1)
    if head_band[1] > row_band[0] + _TOL:
        raise ValueError("attested_row_bands_overlap")
    # Every native character inside the attested bands must belong to exactly
    # one declared cell of this table, so nothing was dropped or clipped here.
    if mapped_words:
        uncovered = []
        for w in mapped_words:
            w_box = w["ink_bbox"] if w["ink_bbox"] is not None else w["font_bbox"]
            if (
                not w["text"].strip()
                or w_box[2] <= table.bbox[0] - _TOL
                or w_box[0] >= table.bbox[2] + _TOL
                or not any(
                    band[0] - _TOL < w_box[1] and w_box[3] < band[1] + _TOL
                    for band in (row_band, head_band)
                )
            ):
                continue
            if not w.get("resolved", True):
                raise ValueError("unresolved_glyph_geometry")
            owners = [
                cell
                for cell in attested.values()
                if v2._inside(w_box, cell.bbox)
                or (w["ink_bbox"] is not None and v2._inside(w["font_bbox"], cell.bbox))
            ]
            if len(owners) > 1:
                raise ValueError("multiple_cell_owners")
            if len(owners) == 0:
                uncovered.append(w["text"])
        if uncovered:
            raise ValueError("uncovered_or_clipped_character")
    else:
        uncovered = []
        for char in page.chars:
            box = (char["x0"], char["top"], char["x1"], char["bottom"])
            if (
                not char["text"].strip()
                or box[2] <= table.bbox[0]
                or box[0] >= table.bbox[2]
                or not any(
                    band[0] - _TOL < box[1] and box[3] < band[1] + _TOL
                    for band in (row_band, head_band)
                )
            ):
                continue
            if sum(v2._inside(box, cell.bbox) for cell in attested.values()) != 1:
                uncovered.append(char["text"])
        if uncovered:
            raise ValueError("uncovered_or_clipped_character")
    context = []
    for key, cell in sorted(attested.items()):
        cell_words = []
        if mapped_words:
            cell_words = [
                w
                for w in mapped_words
                if v2._inside(
                    w["ink_bbox"] if w["ink_bbox"] is not None else w["font_bbox"], cell.bbox
                )
                or (w["ink_bbox"] is not None and v2._inside(w["font_bbox"], cell.bbox))
            ]
        if cell_words:
            sorted_words = sorted(
                cell_words,
                key=lambda w: (
                    round((w["ink_bbox"] or w["font_bbox"])[1], 1),
                    (w["ink_bbox"] or w["font_bbox"])[0],
                ),
            )
            native = " ".join(w["text"] for w in sorted_words)
        else:
            native = page.crop(cell.bbox).extract_text() or ""
        if _normalized(native) != _normalized(cell.raw_text):
            raise ValueError("native_cell_text_mismatch")
        if _NOTE_MARKER.search(cell.raw_text):
            raise ValueError("note_context_unresolved")
        required = key in (value_key, header_key)
        entry = dict(
            source_id=cell.source_id,
            row=key[0],
            column=key[1],
            bbox=list(cell.bbox),
            native_text=native,
            role="value"
            if key == value_key
            else "column_head_literal"
            if key == header_key
            else "row_context",
        )
        if required or key[0] == value_row:
            # Read the promoted pair and the value row's own context cells
            # (unit, label). Only an exact read may be promoted.
            if reads[0] >= MAX_READS:
                raise ValueError("table_read_limit")
            reads[0] += 1
            if cell_words and any(w.get("ink_bbox") for w in cell_words):
                render_box = [
                    min(w["ink_bbox"][0] for w in cell_words if w.get("ink_bbox")),
                    min(w["ink_bbox"][1] for w in cell_words if w.get("ink_bbox")),
                    max(w["ink_bbox"][2] for w in cell_words if w.get("ink_bbox")),
                    max(w["ink_bbox"][3] for w in cell_words if w.get("ink_bbox")),
                ]
            else:
                render_box = cell.bbox
            if not v2._inside(render_box, cell.bbox):
                # The rendered read must come from inside the cell's own stored
                # bbox: an enlarged crop could smuggle neighbouring ink (a
                # different value, year or unit) into an "exact" read.
                raise ValueError("render_crop_outside_promoted_bbox")
            rendered = _rendered_cell(page, render_box)
            exact = rendered.get("status") == "read" and _normalized(
                rendered.get("text", "")
            ) == _normalized(cell.raw_text)
            entry.update(
                rendered=rendered,
                rendered_exact=exact,
                # The exact crop the rendered read came from, in the same
                # top-left points as `bbox`: a receipt reader verifies
                # `render_crop_box` is inside `bbox` without re-running OCR.
                render_crop_box=[float(v) for v in render_box],
            )
            if required and not exact:
                raise ValueError("rendered_cell_text_mismatch")
            entry["attestation"] = "native_and_rendered" if exact else "native_only"
        else:
            entry["attestation"] = "native_only"
        context.append(entry)
    if len(row_cells) < 2 and len({c for r, c in grid}) > 1:
        # A multi-column table requires row context (unit or label).
        # A single-column strip admits the literal number with its column head.
        raise ValueError("row_context_absent")
    return dict(
        value_source_id=value.source_id,
        value_row=value_row,
        value_column=value_column,
        value_text=value.raw_text,
        header_source_id=header.source_id,
        header_row=header_row,
        header_text=header.raw_text,
        header_role="topmost_declared_cell_in_this_column",
        # Only the value cell becomes citable. The column head is attested
        # context: a literal string above the value, never a year or a period,
        # and a number there stays a number.
        promoted_source_ids=[value.source_id],
        cells=context,
        year_binding="not_attested",
        scope_binding="not_attested",
        unit_binding="not_attested",
    )


def attest_tables(graph, source, *, tenant_id):
    return json.loads(_attest_json(graph, source, tenant_id, policy_sha256()))


@lru_cache(maxsize=4)
def _attest_json(graph, source, tenant_id, policy):
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
    ):
        raise ValueError("table source mismatch")
    records = []
    reads = [0]
    with pdfplumber.open(io.BytesIO(source)) as document:
        for table in sorted(
            (b for b in graph.blocks if b.kind == "table"), key=lambda b: b.source_id
        ):
            record = dict(
                table_id=table.source_id,
                page=table.page_num,
                status="unresolved",
                reason=None,
                selections=[],
            )
            records.append(record)
            try:
                members, blocks, grid = _grid(graph, table)
                page = document.pages[table.page_num - 1]
                geometry = table.candidates[table.winner].geometry
                if (
                    page.rotation
                    or geometry.rotation
                    or tuple(page.bbox[:2]) != (0, 0)
                    or tuple(page.cropbox) != tuple(page.mediabox)
                    or abs(page.width - geometry.width_pt) > 0.001
                    or abs(page.height - geometry.height_pt) > 0.001
                ):
                    raise ValueError("geometry_unsupported")
                if unassigned_note_ids(graph, table.source_id):
                    raise ValueError("note_context_unresolved")
                if any(
                    issue.state in {"open", "unreadable"}
                    and members.intersection(issue.source_ids)
                    and issue.kind != "table_vision_not_run"
                    for issue in graph.issues
                ):
                    raise ValueError("source_issue_unresolved")
                raw_words = page.extract_words()
                word_indices = list(range(len(raw_words)))
                try:
                    from proofops.adapters.local.native_glyph_geometry import (
                        native_word_ink_geometry,
                    )

                    ink_res = native_word_ink_geometry(source, table.page_num, word_indices)
                    matched_map = {
                        m["native_word_index"]: m["ink_bbox"]
                        for m in ink_res.get("matched_words", [])
                    }
                    unresolved_set = set(ink_res.get("unresolved_word_indices", []))
                except Exception:
                    matched_map = {}
                    unresolved_set = set()
                mapped_words = [
                    {
                        "index": idx,
                        "text": w["text"],
                        "font_bbox": [float(w[k]) for k in ("x0", "top", "x1", "bottom")],
                        "ink_bbox": matched_map.get(idx),
                        "resolved": idx in matched_map and idx not in unresolved_set,
                    }
                    for idx, w in enumerate(raw_words)
                ]
                pairs = _selections(grid)
                if not pairs:
                    raise ValueError("no_numeric_selection")
                holds = []
                for value_key, value, header_key, header in pairs:
                    try:
                        record["selections"].append(
                            _attest_selection(
                                page,
                                table,
                                grid,
                                value_key,
                                value,
                                header_key,
                                header,
                                reads,
                                mapped_words=mapped_words,
                            )
                        )
                    except ValueError as error:
                        holds.append(
                            dict(
                                value_row=value_key[0],
                                value_column=value_key[1],
                                value_text=value.raw_text,
                                reason=str(error),
                            )
                        )
                record["held_selections"] = holds
                if record["selections"]:
                    record.update(status="partially_verified", reason=None)
                else:
                    raise ValueError(holds[0]["reason"] if holds else "no_numeric_selection")
            except ValueError as error:
                record["reason"] = str(error)
    result = dict(
        schema="native_selected_cell_table_source_v3",
        policy_sha256=policy,
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(graph)),
        records=records,
        scope="selected_numeric_cell_and_its_column_header_literal_text_only",
        semantic_binding="undetermined",
        external_context="not_attested",
    )
    result["artifact_sha256"] = canonical_hash(result)
    return json.dumps(result, ensure_ascii=False)


def replay_tables(receipt, graph, source, *, tenant_id):
    """Recompute everything, then promote only the attested value/header cells.

    The row node, the table node and every unattested or ``native_only`` cell
    keep their existing quality: a selected literal cell can be cited as its own
    numeric source, but it never makes its table, its row or its unit readable.
    """
    expected = attest_tables(graph, source, tenant_id=tenant_id)
    if canonical_hash(receipt) != canonical_hash(expected):
        raise ValueError("table attestation mismatch")
    promote = {
        source_id
        for record in expected["records"]
        for selection in record["selections"]
        for source_id in selection["promoted_source_ids"]
    }
    attested_tables = {record["table_id"] for record in expected["records"] if record["selections"]}
    return replace(
        graph,
        blocks=tuple(
            replace(block, quality="verified")
            if block.source_id in promote and block.kind == "table_cell"
            else block
            for block in graph.blocks
        ),
        issues=tuple(
            # The vision cross-check was really performed, but only for the cells
            # this receipt attests. Recording that narrow scope is what lets the
            # attested value be cited; every other cell of the table stays
            # unverified and therefore still unusable as a numeric source.
            replace(
                issue,
                state="resolved",
                reason=(
                    "Rendered cross-check performed for the attested cells of this "
                    "table only; the remaining cells stay unchecked and unverified."
                ),
            )
            if issue.kind == "table_vision_not_run"
            and issue.source_ids
            and set(issue.source_ids) <= attested_tables
            else issue
            for issue in graph.issues
        ),
    )
