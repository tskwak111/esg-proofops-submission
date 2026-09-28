"""Opt-in literal table verification; never semantic roles, note ownership or grades."""

import io
import json
import re
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pdfplumber

from proofops.adapters.local.source_verification import _rendered_text
from proofops.application.evidence import citations
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.numeric import unassigned_note_ids
from proofops.domain.provenance import canonical_hash

MAX_CELLS = 40
MAX_READS = 96


def policy_sha256():
    folder = Path(__file__).parent
    return canonical_hash(
        dict(
            schema="native_table_source_v1",
            files={
                name: sha256((folder / name).read_bytes()).hexdigest()
                for name in (
                    "table_source_verification.py",
                    "source_verification.py",
                    "native_ocr.swift",
                )
            },
            normalizer_sha256=sha256(Path(citations.__file__).read_bytes()).hexdigest(),
            readers={name: version(name) for name in ("pdfplumber", "pdfminer.six", "pypdfium2")},
        )
    )


def _inside(inner, outer):
    return (
        outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]
    )


def _structure(graph, table):
    if table.winner is None or table.bbox is None:
        raise ValueError("unresolved_table_structure")
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
    selected = [blocks[sid] for sid in sorted(members)]
    if any(
        b.kind not in {"table", "table_row", "table_cell"}
        or b.winner is None
        or b.quality not in {"unverified", "verified"}
        or b.bbox is None
        or b.page_num != table.page_num
        or (b.source_id != table.source_id and not _inside(b.bbox, table.bbox))
        for b in selected
    ):
        raise ValueError("unresolved_table_structure")
    if any(
        e.relation == "table_parent" and e.source_id in members and e.target_id not in members
        for e in graph.edges
    ):
        raise ValueError("ambiguous_table_owner")
    cells = [b for b in selected if b.kind == "table_cell"]
    if not 4 <= len(cells) <= MAX_CELLS:
        raise ValueError("table_cell_limit")
    positions = {}
    for cell in cells:
        c = cell.candidates[cell.winner]
        if (
            type(c.row_number) is not int
            or type(c.column_number) is not int
            or min(c.row_number, c.column_number) < 0
            or c.row_span not in (None, 1)
            or c.column_span not in (None, 1)
        ):
            raise ValueError("merged_or_unknown_grid")
        pos = c.row_number, c.column_number
        if pos in positions:
            raise ValueError("duplicate_cell_position")
        positions[pos] = cell
    rows = sorted({p[0] for p in positions})
    cols = sorted({p[1] for p in positions})
    # ponytail: only complete unmerged rectangles; repair ambiguous/merged structure upstream.
    if len(rows) < 2 or len(cols) < 2 or len(rows) * len(cols) != len(cells):
        raise ValueError("incomplete_rectangular_grid")
    for r in rows:
        for c in cols:
            box = positions[r, c].bbox
            column = positions[rows[0], c].bbox
            row = positions[r, cols[0]].bbox
            if any(abs(box[i] - column[i]) > 0.01 for i in (0, 2)) or any(
                abs(box[i] - row[i]) > 0.01 for i in (1, 3)
            ):
                raise ValueError("cell_alignment_mismatch")
        for a, b in zip(cols, cols[1:]):
            if positions[r, a].bbox[2] > positions[r, b].bbox[0] + 0.01:
                raise ValueError("column_geometry_mismatch")
    for c in cols:
        for a, b in zip(rows, rows[1:]):
            if positions[a, c].bbox[3] > positions[b, c].bbox[1] + 0.01:
                raise ValueError("row_geometry_mismatch")
    ordered = [positions[r, c] for r in rows for c in cols]
    expected = "\n".join("\t".join(positions[r, c].raw_text for c in cols) for r in rows)
    if _normalized(expected) != _normalized(table.raw_text):
        raise ValueError("table_text_grid_mismatch")
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
    return members, ordered


def attest_tables(graph, source, *, tenant_id):
    return json.loads(_attest_json(graph, source, tenant_id, policy_sha256()))


# ponytail: four immutable PDF/graph replays per process; persistent cache needs scoped storage.
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
                members, cells = _structure(graph, table)
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
                # Every native character in the region must belong wholly to exactly one cell.
                # Clipped words and overlapping grids cannot silently lose a digit or header.
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
                    rendered = _rendered_text(page, cell.bbox, padding_px=6)
                    record["cells"].append(
                        dict(
                            source_id=cell.source_id,
                            bbox=cell.bbox,
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
        schema="native_table_source_v1",
        policy_sha256=policy,
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(graph)),
        records=records,
        scope="literal_cell_text_and_rectangular_grid_only",
        semantic_binding="undetermined",
        external_context="not_attested",
    )
    result["artifact_sha256"] = canonical_hash(result)
    return json.dumps(result, ensure_ascii=False)


def replay_tables(receipt, graph, source, *, tenant_id):
    """Recompute source/geometry/OCR before quality promotion; never trust a caller flag."""
    expected = attest_tables(graph, source, tenant_id=tenant_id)
    # JSON tuple/list representation does not change the immutable artifact identity.
    if canonical_hash(receipt) != canonical_hash(expected):
        raise ValueError("table attestation mismatch")
    good = [r for r in expected["records"] if r["status"] == "verified"]
    members = {sid for r in good for sid in r["member_ids"]}
    tables = {r["table_id"] for r in good}
    return replace(
        graph,
        blocks=tuple(
            replace(b, quality="verified") if b.source_id in members else b for b in graph.blocks
        ),
        issues=tuple(
            replace(
                i,
                state="resolved",
                reason=(
                    "Native cell geometry and rendered text checked; "
                    "semantic binding remains unresolved."
                ),
            )
            if i.kind == "table_vision_not_run" and i.source_ids and set(i.source_ids) <= tables
            else i
            for i in graph.issues
        ),
    )
