"""Caller-selected table-cell *literal text* attestation. Never semantics or grades.

Why this module exists
----------------------
``selected_cell_table_verification`` (v3) admits one literal numeric cell together
with its column header. By construction it can never admit the cells a reader
actually needs in order to know *what* a number is: the merged metric cell
(``직접 온실가스(Scope 1) 배출량 - 사업장별``, row_span 6) and the unit cell
(``tCO2eq``) are not numeric, so ``_selections`` never proposes them, and v3
promotes nothing else. Those cells reach the row-context path at best, where a
rendered variant leaves them ``native_only`` and unpromoted.

Measured on the real operator-reviewed Lotte page 117 grid
(``tests/fixtures/pipeline/reviewed-table-layout/lotte-candidates.json`` over
``tests/fixtures/source-documents/lotte.pdf``), the existing strict full-block reader
``claim_source_verification._read_paragraph`` reads those very cells back exactly
from the rendered page once their canonical box is the real glyph ink union that
``reviewed_table.graph_from_review`` already produces. This adapter is exactly
that: the existing reader, applied to caller-named ``table_cell`` blocks, with an
*exact full-literal* requirement instead of the paragraph verifier's
unique-substring quote rule.

What is attested
----------------
Only that this one declared cell's stored literal string is the string the source
really carries at the cell's own pinned box, proven twice and independently:

* natively, from real glyph ink inside the stored bbox -- ``_read_paragraph``
  requires every word of the page to be ink-resolved, refuses any word that
  crosses the box while sharing the box's inked rows, and requires the joined
  in-box words to normalize equal to the block's raw text;
* renderedly, by on-device OCR of a crop taken from that same stored bbox, whose
  pixel box is recomputed here and recorded, so a receipt reader can confirm no
  enlarged crop smuggled a neighbouring cell's ink in;
* and through the existing citation verifier, which re-checks the
  tenant/document/manifest/parser-run/source-hash chain, the candidate's presence
  in a real parse batch, the bbox, the page and the raw-text hash for a
  whole-cell span.

What is never attested
----------------------
No role, no year, no scope, no unit semantics, no metric-to-value linkage, no
numeric observation, no grade, no label. A promoted ``tCO2eq`` cell means the
five characters are really printed in that box; it does not say the number beside
it is measured in tonnes of CO2 equivalent. Those bindings stay the caller's
problem under their own guards, and the receipt records them as not attested.

Numeric literals (the period-header case)
----------------------------------------
A bare number is refused by default, so this adapter can never become a cheaper
path to a citable value. But a column-header literal such as ``2025`` is also a
bare number, and refusing it unconditionally would leave every period header
permanently unreachable. It is admitted only when the caller supplies a
``selected_cell_table_verification`` (v3) receipt that this module revalidates by
recomputing v3 in full and requiring an exact hash match -- the same check v3's own
replay makes. From that revalidated receipt, and only from it, a cell may be taken
as a column-head literal: it must be some selection's ``header_source_id`` with a
``column_head_literal`` role and an exact ``native_and_rendered`` read, and it must
not be any selection's promoted value cell. There is no four-digit regex, no
"row 0" guess and no caller-declared role. The cell still has to pass this
module's own native+rendered full-literal read; v3's receipt only supplies the
structural evidence that the cell is a header rather than a data value, and it
never makes the number a reporting period.

Non-weakening
-------------
* A numeric cell with no revalidated v3 header proof is refused, and a cell v3
  promoted as a *value* is refused even when a proof is supplied. Numeric value
  promotion stays v3's, with v3's column-band, row-band and full-coverage guards.
* Nothing is cast into a ``paragraph``, no kind is rewritten, the interactive-PDF
  and annotation-appearance checks run for real (never ``interactive=False``),
  and no existing verifier, policy or frozen receipt is touched.
* ``unknown`` / mismatch / unreadable stays held. A held cell is recorded with its
  reason and is never promoted.
* ``replay_context_cells`` recomputes the whole receipt and refuses on any
  difference, so a changed graph, a foreign source or a forged receipt cannot
  promote anything. Promotion touches only the attested ``table_cell`` blocks'
  ``quality``; the table node, the row nodes, every other cell and every issue
  (including an open ``table_vision_not_run``) are returned unchanged. The
  caller's graph object itself is never mutated.
"""

from __future__ import annotations

import io
import json
import math
from dataclasses import asdict, replace
from functools import lru_cache
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pdfplumber
from pdfminer.pdftypes import resolve1

from proofops.adapters.local import claim_source_verification as claim_source
from proofops.adapters.local import merged_table_verification as v2
from proofops.adapters.local import native_glyph_geometry, source_verification
from proofops.adapters.local import selected_cell_table_verification as v3
from proofops.application.evidence import citations
from proofops.application.evidence.citations import _normalized, verify_source_ref
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.numeric import unassigned_note_ids
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef

SCHEMA = "reviewed_context_cell_literal_v1"
# Iteration/read bound only. A caller asking for more cells than this is refused
# rather than silently truncated.
MAX_CELLS = 24
# `source_verification._rendered_text` renders the page at 216dpi (72 * 3) and
# crops in those pixels. Recomputed here to prove the crop box.
_RENDER_SCALE = 3


def context_cell_policy() -> dict:
    """Pin this file and every piece of code that produced the reading."""
    local = Path(source_verification.__file__).parent
    return dict(
        schema=SCHEMA,
        verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        # The full-block native+rendered reader this adapter reuses unchanged.
        source_reader_sha256=sha256(Path(claim_source.__file__).read_bytes()).hexdigest(),
        rendered_reader_sha256=sha256(Path(source_verification.__file__).read_bytes()).hexdigest(),
        rendered_ocr_sha256=sha256((local / "native_ocr.swift").read_bytes()).hexdigest(),
        glyph_verifier_sha256=sha256(Path(native_glyph_geometry.__file__).read_bytes()).hexdigest(),
        normalizer_sha256=sha256(Path(citations.__file__).read_bytes()).hexdigest(),
        # Carries claim_validator/citation hashes and native_paragraph_policy().
        claim_source_policy=claim_source.claim_source_policy(),
        # Bound so a receipt states which numeric verifier it declines to replace.
        selected_cell_policy_sha256=v3.policy_sha256(),
        readers={name: version(name) for name in ("pdfplumber", "pdfminer.six", "pypdfium2")},
    )


def _requested(source_ids) -> tuple[str, ...]:
    if isinstance(source_ids, str):
        raise ValueError("CONTEXT_CELL_SELECTION_INVALID")
    requested = tuple(source_ids)
    if not requested or len(requested) > MAX_CELLS:
        raise ValueError("CONTEXT_CELL_SELECTION_INVALID")
    if any(not isinstance(sid, str) or not sid for sid in requested):
        raise ValueError("CONTEXT_CELL_SELECTION_INVALID")
    if len(set(requested)) != len(requested):
        raise ValueError("CONTEXT_CELL_SELECTION_DUPLICATE")
    return requested


def _table_owner(graph, cell):
    """The cell's single declared table. A shared or fused owner is refused."""
    owners = {
        edge.target_id
        for edge in graph.edges
        if edge.relation == "table_parent" and edge.source_id == cell.source_id
    }
    if len(owners) != 1:
        raise ValueError("ambiguous_table_owner")
    if any(
        edge.relation == "table_parent" and edge.target_id == cell.source_id for edge in graph.edges
    ):
        # This cell owns other cells: it is not a leaf cell of the declared grid.
        raise ValueError("ambiguous_table_owner")
    blocks = {block.source_id: block for block in graph.blocks}
    table = blocks.get(owners.pop())
    if (
        table is None
        or table.kind != "table"
        or table.winner is None
        or table.bbox is None
        or table.page_num != cell.page_num
        or not v2._inside(cell.bbox, table.bbox)
    ):
        raise ValueError("invalid_table_lineage")
    return table


def _citation_state(graph, cell, tenant_id) -> str:
    """Existing citation verifier over this cell's own whole-literal span.

    Quality is forced verified for this one block in a throwaway copy only, the
    way ``attest_claim_spans`` already does, so the verifier's *provenance* checks
    (parse batch membership, parser run, source hash, bbox, page, raw-text hash)
    run before the block has been promoted. The kind is not touched and the
    caller's graph is not touched.
    """
    candidate = cell.candidates[cell.winner]
    raw = candidate.source.raw_text
    if raw != cell.raw_text or not raw:
        raise ValueError("cell_raw_text_lineage_mismatch")
    ref = SourceRef(
        source_id=cell.source_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        page_num=candidate.source.physical_page,
        printed_page_label=candidate.source.printed_page_label,
        bbox=candidate.bbox,
        raw_text_sha256=sha256(raw.encode("utf-8")).hexdigest(),
        quote=raw,
        char_start=0,
        char_end=len(raw),
        location_quality="located",
        verification_state="candidate",
    )
    checked = replace(
        graph,
        blocks=tuple(
            replace(block, quality="verified") if block.source_id == cell.source_id else block
            for block in graph.blocks
        ),
    )
    return verify_source_ref(ref, checked, tenant_id=tenant_id).verification_state


def _render_crop_box(box) -> list[int]:
    return [
        math.floor(box[0] * _RENDER_SCALE),
        math.floor(box[1] * _RENDER_SCALE),
        math.ceil(box[2] * _RENDER_SCALE),
        math.ceil(box[3] * _RENDER_SCALE),
    ]


def _column_head_literals(header_receipt, graph, source, tenant_id):
    """Revalidate a v3 receipt and return the cells it proves are column heads.

    Minimal new logic on purpose: v3 is recomputed and compared exactly, which is
    the same admission check ``v3.replay_tables`` performs, and the header set is
    read back out of that revalidated receipt. A cell v3 promoted as a *value* is
    excluded even if it also appears as a header somewhere, so a data value can
    never enter through this door.
    """
    if header_receipt is None:
        return frozenset(), None
    expected = v3.attest_tables(graph, source, tenant_id=tenant_id)
    if canonical_hash(header_receipt) != canonical_hash(expected):
        raise ValueError("SELECTED_CELL_RECEIPT_MISMATCH")
    values, heads = set(), set()
    for record in expected["records"]:
        for selection in record["selections"]:
            values.update(selection["promoted_source_ids"])
            for cell in selection["cells"]:
                if (
                    cell["source_id"] == selection["header_source_id"]
                    and cell["role"] == "column_head_literal"
                    and cell.get("attestation") == "native_and_rendered"
                    and cell.get("rendered_exact") is True
                ):
                    heads.add(cell["source_id"])
    return frozenset(heads - values), expected["artifact_sha256"]


def _attest_cell(
    document, source, graph, cell, interactive, glyphs, tenant_id, readings, column_heads
):
    """Return the record for one cell; raise ``ValueError(hold_reason)`` instead
    of promoting anything uncertain."""
    if cell.kind != "table_cell":
        raise ValueError("not_a_declared_table_cell")
    if cell.winner is None or cell.bbox is None or cell.quality not in {"verified", "unverified"}:
        raise ValueError("unresolved_cell_structure")
    literal_role = "row_or_label_context_literal"
    if v3._NUMERIC.match(_normalized(cell.raw_text)):
        if cell.source_id not in column_heads:
            # Numeric promotion belongs to the selected-cell verifier and its
            # column/row/coverage guards. This adapter never becomes a shortcut.
            raise ValueError("numeric_cell_requires_selected_cell_verifier")
        # Structural role evidence only; still proven again natively+rendered below.
        literal_role = "column_head_literal_proven_by_selected_cell_receipt"
    table = _table_owner(graph, cell)
    if unassigned_note_ids(graph, cell.source_id):
        raise ValueError("note_context_unresolved")
    if any(
        issue.state in {"open", "unreadable"} and {cell.source_id} & set(issue.source_ids)
        for issue in graph.issues
    ):
        raise ValueError("source_issue_unresolved")
    if _citation_state(graph, cell, tenant_id) != "verified":
        raise ValueError("citation_provenance_unverified")
    if cell.source_id not in readings:
        readings[cell.source_id] = claim_source._read_paragraph(
            document, source, cell, interactive, glyphs
        )
    reading = readings[cell.source_id]
    if reading["reason"] != "rendered_quote_unresolved":
        # Every native/geometry/glyph/interactive hold of the existing reader, and
        # an unavailable rendered reader, arrive here and stay held.
        raise ValueError(reading["reason"])
    native = _normalized(cell.raw_text)
    rendered = _normalized(reading["rendered"].get("text", ""))
    if not native:
        raise ValueError("empty_cell_literal")
    if rendered != native:
        # Exact full literal required. No fuzzy match, no substring, no repair.
        raise ValueError("rendered_cell_literal_mismatch")
    crop = _render_crop_box(cell.bbox)
    if reading["rendered"].get("pixel_bbox") != crop:
        raise ValueError("render_crop_outside_cell_bbox")
    candidate = cell.candidates[cell.winner]
    return dict(
        source_id=cell.source_id,
        source_native_id=candidate.source.source_native_id,
        table_id=table.source_id,
        page_num=cell.page_num,
        row=candidate.row_number,
        column=candidate.column_number,
        row_span=1 if candidate.row_span is None else candidate.row_span,
        column_span=1 if candidate.column_span is None else candidate.column_span,
        bbox=[float(v) for v in cell.bbox],
        native_literal=cell.raw_text,
        rendered_literal=reading["rendered"].get("text", ""),
        normalized_literal=native,
        render_crop_pixel_bbox=crop,
        render_scale=_RENDER_SCALE,
        render_padding_px=reading["rendered"].get("padding_px"),
        render_image_sha256=reading["rendered"].get("image_sha256"),
        literal_role=literal_role,
        attestation="native_and_rendered_full_literal",
    )


def attest_context_cells(graph, source, source_ids, *, tenant_id, header_receipt=None) -> dict:
    """Receipt for the caller-named cells' literal text. Nothing is promoted here.

    ``header_receipt`` is an optional ``selected_cell_table_verification`` receipt.
    It is revalidated here and is the only way a bare-numeric column-header literal
    becomes eligible; it never affects any other cell.
    """
    requested = _requested(source_ids)
    column_heads, header_sha256 = _column_head_literals(header_receipt, graph, source, tenant_id)
    return json.loads(
        _attest_json(
            graph,
            source,
            requested,
            tenant_id,
            column_heads,
            header_sha256,
            canonical_hash(context_cell_policy()),
        )
    )


@lru_cache(maxsize=4)
def _attest_json(graph, source, requested, tenant_id, column_heads, header_sha256, policy_sha256):
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
    ):
        raise ValueError("CONTEXT_SOURCE_MISMATCH")
    blocks = {block.source_id: block for block in graph.blocks}
    if len(blocks) != len(graph.blocks):
        raise ValueError("CONTEXT_GRAPH_SOURCE_IDS_NOT_UNIQUE")
    records, readings, glyphs = [], {}, {}
    with pdfplumber.open(io.BytesIO(source)) as document:
        # Same interactive-content gate as the paragraph claim verifier. Optional
        # content groups or real form fields can repaint a cell, so the reading is
        # never trusted there; this is deliberately not bypassed.
        interactive = "OCProperties" in document.doc.catalog or (
            "AcroForm" in document.doc.catalog
            and not claim_source._pushbuttons_only(resolve1(document.doc.catalog.get("AcroForm")))
        )
        for source_id in requested:
            record = dict(source_id=source_id, status="held", reason="cell_not_found")
            cell = blocks.get(source_id)
            if cell is not None:
                try:
                    record = dict(
                        status="verified",
                        reason=None,
                        **_attest_cell(
                            document,
                            source,
                            graph,
                            cell,
                            interactive,
                            glyphs,
                            tenant_id,
                            readings,
                            column_heads,
                        ),
                    )
                except ValueError as error:
                    record = dict(source_id=source_id, status="held", reason=str(error))
            if source_id in readings:
                record["reading_sha256"] = canonical_hash(readings[source_id])
            records.append(record)
    receipt = dict(
        schema=SCHEMA,
        policy_sha256=policy_sha256,
        policy=context_cell_policy(),
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(graph)),
        interactive_content_present=interactive,
        requested_source_ids=list(requested),
        # Which cells a revalidated v3 receipt proved to be column-head literals,
        # and that receipt's identity. Empty and None when no proof was supplied.
        column_head_literal_source_ids=sorted(column_heads),
        selected_cell_receipt_sha256=header_sha256,
        records=records,
        readings=readings,
        scope="caller_selected_table_cell_full_literal_text_only",
        role_binding="not_attested",
        metric_binding="not_attested",
        unit_binding="not_attested",
        year_binding="not_attested",
        period_binding="not_attested",
        scope_binding="not_attested",
        value_linkage="not_attested",
        semantic_binding="undetermined",
        grade_effect="none",
        external_context="not_attested",
    )
    receipt["artifact_sha256"] = canonical_hash(receipt)
    return json.dumps(receipt, ensure_ascii=False)


def replay_context_cells(receipt, graph, source, source_ids, *, tenant_id, header_receipt=None):
    """Recompute the receipt exactly, then promote only the attested cells.

    A changed graph, a different source, a different selection, a different or
    missing header proof, or any edited receipt field raises. The returned graph is
    a new object: the caller's graph, its table and row nodes, its other cells and
    all of its issues are unchanged.
    """
    expected = attest_context_cells(
        graph, source, source_ids, tenant_id=tenant_id, header_receipt=header_receipt
    )
    if canonical_hash(receipt) != canonical_hash(expected):
        raise ValueError("CONTEXT_CELL_RECEIPT_MISMATCH")
    promote = {
        record["source_id"] for record in expected["records"] if record["status"] == "verified"
    }
    return replace(
        graph,
        blocks=tuple(
            replace(block, quality="verified")
            if block.source_id in promote and block.kind == "table_cell"
            else block
            for block in graph.blocks
        ),
    )
