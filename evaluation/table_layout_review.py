"""Operator-reviewed explicit PDF grid, candidate artifacts only; no runtime admission.

Run: python -m evaluation.table_layout_review --pdf SOURCE --layout REVIEW.json
Output goes to stdout. Layout JSON supplies source hash, physical_page, x/y grid
boundaries, rowspans [row,column,span], columns and explicit child:parent rows.
All row/column indices are zero-based; row 0 contains the year headers.

Three opt-in layout keys carry printed shapes the single-header form cannot state.
Omitting all three keeps a layout's previous meaning and output exactly:

``header_rows``
    Depth of the column-header band (default 1).  Data rows start below it and the
    year cell is still read from row 0, so a banded header such as ``2025`` over
    ``연간목표``/``실적`` stays one year with an explicit per-column sub-header.
``colspans`` ``[row, column, span]``
    A horizontally merged cell.  A centred band label is one printed cell: cutting
    it at every column boundary would clip its own glyphs and the source-boundary
    check would (correctly) hold the whole row instead.
``columns.unit_in_metric_cell``
    The unit literal is printed inside the stub cell (``폐수`` / ``(단위: 천 톤)``)
    rather than in its own column.  Exactly one of this and ``columns.unit`` is
    required, and the shared cell is reported as a hold, never split into a unit.

Nothing added here interprets a sub-header, a band or a shared unit literal: the
reviewed grid stays candidate-only and records why each binding is held.
"""

import argparse
import io
import json
import math
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path
from time import perf_counter

import pdfplumber
from proofops.adapters.local.table_layout_context import _typed_word, validate_word_geometry

from evaluation.table_numeric_candidates import parse_numeric_literal, period_qualifier


def _contains(outer, inner):
    return (
        outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]
    )


def _merge(merges, covered, row, column, row_span, column_span):
    """Record one reviewed merged cell; a slot may be claimed only once."""
    slots = {
        (r, c) for r in range(row, row + row_span) for c in range(column, column + column_span)
    }
    if covered & slots:
        raise ValueError("overlapping reviewed spans")
    covered |= slots
    merges[row, column] = (row_span, column_span)


def review_layout(pdf_path, spec):
    """Extract source text under an explicit reviewed grid; never infer layout roles."""
    started = perf_counter()
    path = Path(pdf_path)
    if path.stat().st_size > 100 * 1024 * 1024:
        raise ValueError("PDF size limit")
    source = path.read_bytes()
    source_hash = sha256(source).hexdigest()
    if source_hash != spec["source_sha256"]:
        raise ValueError("layout source mismatch")
    if not isinstance(spec.get("reviewer"), str) or not spec["reviewer"].strip():
        raise ValueError("explicit layout reviewer required")
    x, y = spec["x"], spec["y"]
    for axis in (x, y):
        if (
            not 2 <= len(axis) <= 100
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in axis)
            or any(a >= b for a, b in zip(axis, axis[1:]))
        ):
            raise ValueError("finite increasing grid boundaries required")
    if len(x) * len(y) > 1000:
        raise ValueError("grid cell limit")
    columns = spec["columns"]
    # Opt-in header depth. 1 is the original single year-header row, so an existing
    # layout keeps its exact data-row range and year cell.
    header_rows = spec.get("header_rows", 1)
    if type(header_rows) is not int or not 1 <= header_rows < len(y) - 1:
        raise ValueError("header rows must leave at least one data row")
    unit_in_metric_cell = columns.get("unit_in_metric_cell", False)
    if type(unit_in_metric_cell) is not bool or unit_in_metric_cell == ("unit" in columns):
        raise ValueError("exactly one of columns.unit or columns.unit_in_metric_cell required")
    role_columns = [columns["metric"], *([] if unit_in_metric_cell else [columns["unit"]])]
    role_columns += [*columns["years"]]
    role_columns += [columns[k] for k in ("dimension", "notes") if k in columns]
    if (
        not columns["years"]
        or len(set(role_columns)) != len(role_columns)
        or any(type(c) is not int or not 0 <= c < len(x) - 1 for c in role_columns)
    ):
        raise ValueError("distinct in-grid role columns required")
    parents = {int(child): parent for child, parent in spec.get("parents", {}).items()}
    if any(type(p) is not int or not header_rows <= p < r < len(y) - 1 for r, p in parents.items()):
        raise ValueError("parent must precede a data row")
    settings = dict(
        vertical_strategy="explicit",
        horizontal_strategy="explicit",
        explicit_vertical_lines=x,
        explicit_horizontal_lines=y,
        snap_tolerance=0,
        intersection_tolerance=0,
    )
    with pdfplumber.open(io.BytesIO(source)) as document:
        physical_page = spec["physical_page"]
        if type(physical_page) is not int or not 1 <= physical_page <= len(document.pages):
            raise ValueError("physical page outside PDF")
        page = document.pages[physical_page - 1]
        if page.rotation or page.bbox[:2] != (0, 0) or page.cropbox != page.mediabox:
            raise ValueError("rotated or offset PDF geometry unsupported")
        region = [x[0], y[0], x[-1], y[-1]]
        if not _contains(page.bbox, region):
            raise ValueError("grid outside page")
        native = page.extract_words(return_chars=True)
        validate_word_geometry(native, page.width, page.height)
        words = [_typed_word(w, i) for i, w in enumerate(native)]

        def extract(box):
            if (
                len(box) != 4
                or any(type(n) not in (int, float) or not math.isfinite(n) for n in box)
                or not _contains(page.bbox, box)
            ):
                raise ValueError("invalid source box")
            # ponytail: bounded page-word scan; use a spatial index only for larger grids.
            overlap = [
                w
                for w in words
                if w["bbox"][0] < box[2]
                and w["bbox"][2] > box[0]
                and w["bbox"][1] < box[3]
                and w["bbox"][3] > box[1]
            ]
            clipped = [w["index"] for w in overlap if not _contains(box, w["bbox"])]
            rotated = [w["index"] for w in overlap if not native[w["index"]]["upright"]]
            return dict(
                bbox=list(box),
                raw_text=page.crop(box).extract_text() or "",
                word_indices=[w["index"] for w in overlap],
                clipped_word_indices=clipped,
                rotated_word_indices=rotated,
                extraction_status="held" if clipped or rotated else "source_text_extracted",
            )

        tables = page.crop(region).find_tables(settings)
        if len(tables) != 1 or len(tables[0].rows) != len(y) - 1:
            raise ValueError("explicit grid did not produce one complete table")
        boxes = {
            (r, c): box for r, row in enumerate(tables[0].rows) for c, box in enumerate(row.cells)
        }
        spans, covered = {}, set()
        for row, col, span in spec.get("rowspans", []):
            if any(type(v) is not int for v in (row, col, span)) or not (
                0 <= row < len(y) - 1 and 0 <= col < len(x) - 1 and 2 <= span <= len(y) - 1 - row
            ):
                raise ValueError("invalid reviewed rowspan")
            # A merged stub head may declare the header depth, but no merge may join
            # the header band to a data row. With header_rows=1 this is the previous
            # rule (row >= 1) exactly, because a span of 2 can never fit in row 0.
            if not (row + span <= header_rows or header_rows <= row):
                raise ValueError("reviewed rowspan crosses the header band")
            _merge(spans, covered, row, col, span, 1)
        for row, col, span in spec.get("colspans", []):
            if any(type(v) is not int for v in (row, col, span)) or not (
                0 <= row < len(y) - 1 and 0 <= col < len(x) - 1 and 2 <= span <= len(x) - 1 - col
            ):
                raise ValueError("invalid reviewed colspan")
            _merge(spans, covered, row, col, 1, span)
        cells, slots = {}, {}
        for (row, col), box in boxes.items():
            if (row, col) in covered and (row, col) not in spans:
                continue
            row_span, column_span = spans.get((row, col), (1, 1))
            key = f"r{row}c{col}"
            if box is None:
                raise ValueError("missing explicit grid cell")
            cell = dict(
                key=key,
                row=row,
                column=col,
                row_span=row_span,
                column_span=column_span,
                **extract(
                    [
                        box[0],
                        box[1],
                        box[2] if column_span == 1 else x[col + column_span],
                        y[row + row_span],
                    ]
                ),
            )
            cells[key] = cell
            for r in range(row, row + row_span):
                for c in range(col, col + column_span):
                    slots[r, c] = key
        context = [
            dict(kind=c["kind"], **extract(c["bbox"])) for c in spec.get("context_boxes", [])
        ]
        candidates = []
        for row in range(header_rows, len(y) - 1):
            own = slots[row, columns["metric"]]
            metric = slots[parents.get(row, row), columns["metric"]]
            labels = [own] if row in parents else []
            if "dimension" in columns:
                labels.append(slots[row, columns["dimension"]])
            notes = [slots[row, columns["notes"]]] if "notes" in columns else []
            for column in columns["years"]:
                year = slots[0, column]
                # Deepest header cell of this column under a banded header
                # (``실적``/``연간목표``). Carried as a real source-bound cell only: this
                # tool does not decide that it means an actual or a target.
                qualifier = slots[header_rows - 1, column]
                refs = dict(
                    metric=metric,
                    unit=own if unit_in_metric_cell else slots[row, columns["unit"]],
                    year=year,
                    value=slots[row, column],
                    row_labels=labels,
                    notes=notes,
                )
                if qualifier != year:
                    refs["column_qualifier"] = qualifier
                selected = list(
                    dict.fromkeys(
                        [
                            metric,
                            refs["unit"],
                            refs["year"],
                            refs["value"],
                            *labels,
                            *notes,
                            *([refs["column_qualifier"]] if qualifier != year else []),
                        ]
                    )
                )
                reasons = [
                    f"source_text_boundary:{key}"
                    for key in selected
                    if cells[key]["extraction_status"] == "held"
                ]
                reasons += [f"empty_cell:{key}" for key in selected if not cells[key]["raw_text"]]
                number, number_reason = parse_numeric_literal(cells[refs["value"]]["raw_text"])
                period = period_qualifier(cells[refs["year"]]["raw_text"])
                if number is None:
                    reasons.append(number_reason)
                if period["year"] is None:
                    reasons.append("year_header_unresolved")
                if unit_in_metric_cell:
                    # The printed unit shares the stub cell with the row label; the
                    # literal is preserved whole and never split into a unit.
                    reasons.append("unit_literal_shares_the_metric_stub_cell")
                if qualifier != year:
                    reasons.append("column_qualifier_semantics_not_resolved")
                candidates.append(
                    dict(
                        row=row,
                        column=column,
                        status="candidate_only",
                        verified=False,
                        binding_status="held" if reasons else "complete_candidate",
                        hold_reasons=reasons,
                        semantic_binding="operator_layout_candidate",
                        source_cells=refs,
                        metric_raw=cells[metric]["raw_text"],
                        row_labels=[cells[k]["raw_text"] for k in labels],
                        unit_raw=cells[refs["unit"]]["raw_text"],
                        unit_canonical=None,
                        year=period["year"],
                        value_raw=cells[refs["value"]]["raw_text"],
                        value_decimal=number,
                        parent_relation=None
                        if row not in parents
                        else dict(child=own, parent=metric, origin="explicit_operator_input"),
                        **(
                            {"column_qualifier_raw": cells[qualifier]["raw_text"]}
                            if qualifier != year
                            else {}
                        ),
                    )
                )
        used = {i for c in [*cells.values(), *context] for i in c["word_indices"]}
        return dict(
            status="candidate_only",
            eligible_for_scoring=False,
            native_verification="not_run",
            semantic_verification="not_run",
            source_path=str(path.resolve()),
            source_sha256=source_hash,
            physical_page=physical_page,
            coordinate_system="pdf_top_left_points",
            page_bbox=list(page.bbox),
            layout_interpretation="manual_AI_review_not_independent_gold",
            layout=spec,
            layout_sha256=sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest(),
            tool_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
            reader_version=version("pdfplumber"),
            settings=settings,
            cells=cells,
            words=[w for w in words if w["index"] in used],
            context=context,
            candidates=candidates,
            elapsed_seconds=round(perf_counter() - started, 4),
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--layout", type=Path, required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            review_layout(args.pdf, json.loads(args.layout.read_text())),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
