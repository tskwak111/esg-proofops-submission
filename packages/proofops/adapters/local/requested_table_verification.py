"""Opt-in requested numeric cells; legacy v3 and its immutable receipts stay intact.

Reuse v3's literal/geometry/row/column/coverage/OCR checks without raising its
selection or read bounds. Only scheduling changes: explicit source IDs instead
of the first 24 numbers. Semantics, external context and grades stay unresolved.
"""

import io
import json
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

import pdfplumber

from proofops.adapters.local import selected_cell_table_verification as v3
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.numeric import unassigned_note_ids
from proofops.domain.provenance import canonical_hash

SCHEMA = "native_requested_cell_table_source_v1"


def _selections(grid, requested):
    pairs = []
    for (row, column), cell in sorted(grid.items()):
        if cell.source_id not in requested:
            continue
        if not v3._NUMERIC.fullmatch(_normalized(cell.raw_text)):
            raise ValueError("requested_cell_not_numeric")
        above = [r for r, c in grid if c == column and r < row]
        if not above:
            raise ValueError("requested_cell_has_no_header")
        header_key = min(above), column
        pairs.append(((row, column), cell, header_key, grid[header_key]))
    return pairs


def attest_tables(graph, source, source_ids, *, tenant_id):
    if not isinstance(source_ids, list | tuple):
        raise ValueError("explicit source ID list required")
    if not 1 <= len(source_ids) <= v3.MAX_SELECTIONS_PER_TABLE:
        raise ValueError("requested cell limit")
    if any(not isinstance(sid, str) or not sid for sid in source_ids):
        raise ValueError("invalid requested source ID")
    requested = tuple(sorted(source_ids))
    if len(set(requested)) != len(requested):
        raise ValueError("duplicate requested source ID")
    cells = {b.source_id for b in graph.blocks if b.kind == "table_cell"}
    if not set(requested) <= cells:
        raise ValueError("requested source is not a table cell")
    policy = canonical_hash(
        {
            "schema": SCHEMA,
            "legacy_policy": v3.policy_sha256(),
            "verifier_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
        }
    )
    return json.loads(_attest_json(graph, source, tenant_id, requested, policy))


@lru_cache(maxsize=4)
def _attest_json(graph, source, tenant_id, requested, policy):
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
                members, blocks, grid = v3._grid(graph, table)
                if not members.intersection(requested):
                    record.update(status="not_requested")
                    continue
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
                pairs = _selections(grid, requested)
                if not pairs:
                    raise ValueError("no_numeric_selection")
                holds = []
                for value_key, value, header_key, header in pairs:
                    try:
                        record["selections"].append(
                            v3._attest_selection(
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
        schema=SCHEMA,
        requested_source_ids=list(requested),
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


def replay_tables(receipt, graph, source, source_ids, *, tenant_id):
    """Recompute everything, then promote only the attested value/header cells.

    The row node, the table node and every unattested or ``native_only`` cell
    keep their existing quality: a selected literal cell can be cited as its own
    numeric source, but it never makes its table, its row or its unit readable.
    """
    expected = attest_tables(graph, source, source_ids, tenant_id=tenant_id)
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
