"""Opt-in merged/ODL-repaired table source verification (v2); never grades or semantics.

This adapter is a strict extension of ``table_source_verification`` (v1). v1 is
frozen: its hashes and behaviour do not change and this module never
monkeypatches or mutates it. v2 differs from v1 in exactly two ways:

* explicit ``row_span``/``column_span`` merged cells are allowed, checked with a
  complete, non-overlapping occupancy grid instead of a plain ``rows*cols``
  rectangle; and
* ODL header-repair emits *text-sized* cell boxes that are smaller than their
  grid slot, so cells align by ordered, non-overlapping row/column *bands*
  rather than exact rectangular cell edges.

Everything else is inherited from v1's contract: full native-character coverage
with local rendered-OCR exact match, ``MAX_CELLS``/``MAX_READS`` limits, note and
issue gates, immutable raw graph and receipts, and ``semantic_binding`` left
undetermined. Unknown, conflicting, unlocated grids, duplicated merged text,
unresolved notes, and descendants that are not individually source-matched all
stay blocked; nothing is implicitly promoted to ``verified``.
"""

import io
import json
import re
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pdfplumber

from proofops.adapters.local import table_source_verification as v1
from proofops.adapters.local.source_verification import _rendered_text
from proofops.application.evidence import citations
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.numeric import unassigned_note_ids
from proofops.domain.provenance import canonical_hash

# Keep v1 unchanged; v2 accounts only for ODL coordinate rounding.
MAX_CELLS = v1.MAX_CELLS
MAX_READS = v1.MAX_READS

# Tolerance for band membership / ordering (parser coords rounded to 0.001pt).
_TOL = 0.001


def _inside(inner, outer):
    return (
        outer[0] - _TOL <= inner[0] < inner[2] <= outer[2] + _TOL
        and outer[1] - _TOL <= inner[1] < inner[3] <= outer[3] + _TOL
    )


def policy_sha256():
    """Bind this v2 file, its swift reader, the normalizer, readers, and v1.

    The v1 policy digest is embedded as a dependency so that any change to the
    frozen v1 verifier (which would be a contract break) also changes v2's
    identity, while v2 keeps its own separate schema/file binding.
    """
    folder = Path(__file__).parent
    return canonical_hash(
        dict(
            schema="native_merged_table_source_v2",
            files={
                name: sha256((folder / name).read_bytes()).hexdigest()
                for name in ("merged_table_verification.py", "native_ocr.swift")
            },
            v1_policy_sha256=v1.policy_sha256(),
            normalizer_sha256=sha256(Path(citations.__file__).read_bytes()).hexdigest(),
            readers={name: version(name) for name in ("pdfplumber", "pdfminer.six", "pypdfium2")},
        )
    )


def _table_members(graph, table):
    """Collect the explicit table lineage exactly like v1 (no inference)."""
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


def _ordered_bands(intervals):
    """Return band extents keyed by grid index after proving order/non-overlap.

    ``intervals`` maps an ordinal index (row or column number) to a ``(lo, hi)``
    extent. Canonical graph bboxes are ALWAYS top-left, so increasing row/column
    indices must map to spatially ASCENDING, non-overlapping extents (a lower
    row number sits higher on the page => smaller top-left y). This replaces
    v1's exact-edge equality so text-sized ODL boxes still prove a coherent grid
    without inventing edges, while a swapped row/column ordering is rejected.

    The returned mapping preserves the original index keys (which may start at 1,
    as real ODL tables do) so callers look up a span's outer band by actual
    index, never by an enumerated position.
    """
    keys = sorted(intervals)
    ordered = [intervals[key] for key in keys]
    for lo, hi in ordered:
        if hi <= lo:
            raise ValueError("degenerate_band")
    for (_, prev_hi), (next_lo, _) in zip(ordered, ordered[1:]):
        if next_lo + _TOL < prev_hi:
            # Overlapping or swapped (descending) ordinals in a top-left axis.
            raise ValueError("band_overlap_or_disorder")
    return dict(intervals)


def _structure(graph, table):
    """Validate a merged-capable grid via occupancy + ordered band alignment.

    Returns ``(members, ordered_cells, span_by_id)`` where ``ordered_cells`` are
    the merged unit cells in reading order (each merged unit appears once).
    """
    if table.winner is None or table.bbox is None:
        raise ValueError("unresolved_table_structure")
    members, blocks = _table_members(graph, table)
    selected = [blocks[sid] for sid in sorted(members)]
    # Structural children other than cells (heading/paragraph from ODL repair)
    # may exist but must never be treated as table geometry here.
    if any(
        b.kind not in {"table", "table_row", "table_cell", "heading", "paragraph"}
        or b.winner is None
        or b.quality not in {"unverified", "verified"}
        or b.bbox is None
        or b.page_num != table.page_num
        or (b.source_id != table.source_id and not _inside(b.bbox, table.bbox))
        for b in selected
    ):
        raise ValueError("unresolved_table_structure")

    cells = [b for b in selected if b.kind == "table_cell"]
    if not 4 <= len(cells) <= MAX_CELLS:
        raise ValueError("table_cell_limit")

    span_by_id = {}
    origins = {}
    for cell in cells:
        c = cell.candidates[cell.winner]
        rs = 1 if c.row_span is None else c.row_span
        cs = 1 if c.column_span is None else c.column_span
        if (
            type(c.row_number) is not int
            or type(c.column_number) is not int
            or type(rs) is not int
            or type(cs) is not int
            or min(c.row_number, c.column_number) < 0
            or min(rs, cs) < 1
            # Bound each span and the expanded footprint BEFORE any range loop so
            # a malicious/broken span cannot force unbounded iteration.
            or rs > MAX_CELLS
            or cs > MAX_CELLS
            or rs * cs > MAX_CELLS
        ):
            raise ValueError("invalid_grid_indices")
        origin = (c.row_number, c.column_number)
        if origin in origins:
            raise ValueError("duplicate_cell_origin")
        origins[origin] = cell
        span_by_id[cell.source_id] = (rs, cs)

    # Total expanded occupancy is bounded by MAX_CELLS**2 before expansion.
    if sum(rs * cs for rs, cs in span_by_id.values()) > MAX_CELLS * MAX_CELLS:
        raise ValueError("occupancy_too_large")

    # Occupancy grid: every covered (row, col) belongs to exactly one cell and
    # the covered region is a complete rectangle with no holes or overlaps.
    occupancy = {}
    for origin, cell in origins.items():
        rs, cs = span_by_id[cell.source_id]
        for r in range(origin[0], origin[0] + rs):
            for col in range(origin[1], origin[1] + cs):
                if (r, col) in occupancy:
                    raise ValueError("overlapping_spans")
                occupancy[r, col] = cell
    rows = sorted({r for r, _ in occupancy})
    cols = sorted({c for _, c in occupancy})
    if len(rows) < 2 or len(cols) < 2:
        raise ValueError("incomplete_occupancy_grid")
    if rows != list(range(rows[0], rows[0] + len(rows))) or cols != list(
        range(cols[0], cols[0] + len(cols))
    ):
        raise ValueError("incomplete_occupancy_grid")
    if len(occupancy) != len(rows) * len(cols):
        raise ValueError("incomplete_occupancy_grid")

    # Derive ordered, non-overlapping column x-bands and row y-bands from the
    # cells that occupy a single column / single row, then confirm every cell
    # (including merged units) sits within the union of its spanned bands.
    col_bands = {}
    for c in cols:
        singles = [
            origins[o].bbox
            for o, cell in origins.items()
            if o[1] == c and span_by_id[cell.source_id][1] == 1
        ]
        if not singles:
            raise ValueError("no_single_column_anchor")
        col_bands[c] = (min(b[0] for b in singles), max(b[2] for b in singles))
    row_bands = {}
    for r in rows:
        singles = [
            origins[o].bbox
            for o, cell in origins.items()
            if o[0] == r and span_by_id[cell.source_id][0] == 1
        ]
        if not singles:
            raise ValueError("no_single_row_anchor")
        row_bands[r] = (min(b[1] for b in singles), max(b[3] for b in singles))
    ordered_cols = _ordered_bands(col_bands)
    ordered_rows = _ordered_bands(row_bands)

    for origin, cell in origins.items():
        rs, cs = span_by_id[cell.source_id]
        spanned_x = [ordered_cols[origin[1] + i] for i in range(cs)]
        spanned_y = [ordered_rows[origin[0] + i] for i in range(rs)]
        left = min(b[0] for b in spanned_x) - _TOL
        right = max(b[1] for b in spanned_x) + _TOL
        top = min(b[0] for b in spanned_y) - _TOL
        bottom = max(b[1] for b in spanned_y) + _TOL
        box = cell.bbox
        if not (left <= box[0] and box[2] <= right and top <= box[1] and box[3] <= bottom):
            raise ValueError("cell_outside_bands")

    ordered = [origins[o] for o in sorted(origins)]

    # Table text must equal the ordered grid with merged units placed once at
    # their origin and blank elsewhere (no text duplication across the span).
    grid_text = {}
    for origin, cell in origins.items():
        grid_text[origin] = cell.raw_text
    expected = "\n".join("\t".join(grid_text.get((r, c), "") for c in cols) for r in rows)
    if _normalized(expected) != _normalized(table.raw_text):
        raise ValueError("table_text_grid_mismatch")

    # Restore v1's row-text + cell-ancestry validation so a fabricated
    # table_row quote cannot be promoted alongside the cells. Each row's cell
    # children must sit inside the row bbox and its raw_text must equal the
    # ordered tab-join of those children (merged units contribute their single
    # text once, in row-origin order).
    for row in (b for b in selected if b.kind == "table_row"):
        children = [
            cell
            for cell in ordered
            if any(
                e.relation == "table_parent"
                and e.source_id == cell.source_id
                and e.target_id == row.source_id
                for e in graph.edges
            )
        ]
        if (
            not children
            or any(not _inside(cell.bbox, row.bbox) for cell in children)
            or _normalized(row.raw_text)
            != _normalized("\t".join(cell.raw_text for cell in children))
        ):
            raise ValueError("row_text_grid_mismatch")

    if unassigned_note_ids(graph, table.source_id) or re.search(r"\d+\)|[*†‡]", table.raw_text):
        raise ValueError("note_context_unresolved")
    if any(
        i.state in {"open", "unreadable"}
        and members.intersection(i.source_ids)
        and i.kind != "table_vision_not_run"
        for i in graph.issues
    ):
        raise ValueError("source_issue_unresolved")
    return members, ordered, span_by_id


def attest_tables(graph, source, *, tenant_id):
    return json.loads(_attest_json(graph, source, tenant_id, policy_sha256()))


# Four immutable PDF/graph replays per process, mirroring v1's bounded cache.
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
    reads = 0
    with pdfplumber.open(io.BytesIO(source)) as pdf:
        for table in sorted(
            (b for b in graph.blocks if b.kind == "table"), key=lambda b: b.source_id
        ):
            record = dict(
                table_id=table.source_id,
                page=table.page_num,
                status="unresolved",
                reason=None,
                cells=[],
            )
            records.append(record)
            try:
                members, cells, span_by_id = _structure(graph, table)
                if reads + len(cells) > MAX_READS:
                    raise ValueError("table_read_limit")
                page = pdf.pages[table.page_num - 1]
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
                # Every native character in the table region must belong wholly
                # to exactly one merged cell. Clipped words or overlapping boxes
                # cannot silently drop a digit, header, or merged unit token.
                for char in page.chars:
                    box = (char["x0"], char["top"], char["x1"], char["bottom"])
                    if (
                        not char["text"].strip()
                        or box[2] <= table.bbox[0]
                        or box[0] >= table.bbox[2]
                        or box[3] <= table.bbox[1]
                        or box[1] >= table.bbox[3]
                    ):
                        continue
                    if sum(_inside(box, c.bbox) for c in cells) != 1:
                        raise ValueError("uncovered_or_clipped_character")
                for cell in cells:
                    native = page.crop(cell.bbox).extract_text() or ""
                    if _normalized(native) != _normalized(cell.raw_text):
                        raise ValueError("native_cell_text_mismatch")
                    reads += 1
                    # Blank margin helps short reversed-color headers; source pixels stay unchanged.
                    rendered = _rendered_text(page, cell.bbox, padding_px=24)
                    rs, cs = span_by_id[cell.source_id]
                    record["cells"].append(
                        dict(
                            source_id=cell.source_id,
                            bbox=cell.bbox,
                            row_span=rs,
                            column_span=cs,
                            native_text=native,
                            rendered=rendered,
                        )
                    )
                    if rendered.get("status") != "read" or _normalized(
                        rendered.get("text", "")
                    ) != _normalized(cell.raw_text):
                        raise ValueError("rendered_cell_text_mismatch")
                record.update(status="verified", member_ids=sorted(members))
            except ValueError as error:
                record["reason"] = str(error)
    result = dict(
        schema="native_merged_table_source_v2",
        policy_sha256=policy,
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(graph)),
        records=records,
        scope="merged_cell_text_and_ordered_nonoverlapping_bands_only",
        semantic_binding="undetermined",
        external_context="not_attested",
    )
    result["artifact_sha256"] = canonical_hash(result)
    return json.dumps(result, ensure_ascii=False)


def replay_tables(receipt, graph, source, *, tenant_id):
    """Recompute source/geometry/OCR before quality promotion; never trust a flag.

    Only the ``table_cell`` (and their ``table``/``table_row``) members of a
    verified record are promoted. Descendant heading/paragraph children are
    never promoted here: they must be verified individually by the paragraph
    verifier against their own source, so a merged-cell attestation cannot make
    them inherit ``verified``.
    """
    expected = attest_tables(graph, source, tenant_id=tenant_id)
    if canonical_hash(receipt) != canonical_hash(expected):
        raise ValueError("table attestation mismatch")
    good = [r for r in expected["records"] if r["status"] == "verified"]
    verified_cells = {c["source_id"] for r in good for c in r["cells"]}
    tables = {r["table_id"] for r in good}
    # Promote only the table node and its verified cells (plus rows fully made of
    # verified cells); heading/paragraph descendants are intentionally excluded.
    row_children = {}
    for edge in graph.edges:
        if edge.relation == "table_parent":
            row_children.setdefault(edge.target_id, set()).add(edge.source_id)
    kinds = {b.source_id: b.kind for b in graph.blocks}
    promote = set(verified_cells) | tables
    for row_id, children in row_children.items():
        if kinds.get(row_id) == "table_row" and children and children <= verified_cells:
            promote.add(row_id)

    return replace(
        graph,
        blocks=tuple(
            replace(b, quality="verified")
            if b.source_id in promote and b.kind in {"table", "table_row", "table_cell"}
            else b
            for b in graph.blocks
        ),
        issues=tuple(
            replace(
                i,
                state="resolved",
                reason=(
                    "Merged cell occupancy, ordered band geometry and rendered "
                    "text checked; semantic binding remains unresolved."
                ),
            )
            if i.kind == "table_vision_not_run" and i.source_ids and set(i.source_ids) <= tables
            else i
            for i in graph.issues
        ),
    )
