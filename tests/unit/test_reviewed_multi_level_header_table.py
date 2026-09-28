"""Recover the Lotte p29 grid the parser never produced and feed the R12 table path.

Lotte p29 discloses 대기/폐기물/수질/폐수 in a card layout with no ruling lines, so the
production parser emitted zero ``table``/``table_cell`` blocks and zero
``table_parent`` edges for that page (``R12-lotte-no-table-lineage.json``).  The
claim ``10,551`` therefore reached the model with the two nearest *numbers*
(``105%``, ``831``) as its only context and every dimension came back null.

The page's real structure needs two things the reviewed-grid tool did not have:
a **banded column header** (``2025`` over ``연간목표``/``실적``/``달성률``/``증감사유``) and a
**unit printed inside the stub cell** (``폐수`` / ``(단위: 천 톤)``).  Both are opt-in:
a layout without ``header_rows``/``colspans``/``unit_in_metric_cell`` keeps its exact
previous meaning, output and parse identity.

Most tests are local integration checks: they read the customer PDF under
``outputs/`` (git-ignored) and run the real on-device rendered reader, so they skip
where those inputs are absent.  The reviewed grid is an AI visual review of the
printed page -- silver, not independent gold.  Nothing here promotes a value, a
unit, a period semantics or a grade, and the axes below are asserted at exactly
the verification state the real verifier returns, never at a wished-for one.
"""

from __future__ import annotations

import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

import pytest
from proofops.adapters.local.reviewed_table import _TENANT, native_attested_layout
from proofops.application.tagging.preliminary import _bounded_context_blocks, _context_entry
from proofops.application.tagging.table_sources import (
    COLUMN_HEADER,
    ROW_HEADER,
    ROW_QUALIFIER,
    table_structural_sources,
)
from pypdf import PdfWriter

from evaluation.table_layout_review import review_layout

APP = Path(__file__).resolve().parents[2]
LOTTE = APP / "tests/fixtures/source-documents/lotte.pdf"
KIA = APP / "tests/fixtures/source-documents/kia.pdf"
P29 = APP / "tests/fixtures/pipeline/table-recovery/lotte-p29-layout.json"
P117 = APP / "tests/fixtures/pipeline/reviewed-table-layout/lotte-layout.json"
KIA_LAYOUT = APP / "tests/fixtures/pipeline/reviewed-table-layout/kia-layout.json"

local_inputs = pytest.mark.skipif(
    not (LOTTE.exists() and KIA.exists()),
    reason="customer PDFs are not part of the repository",
)


def _spec(path: Path) -> dict:
    return json.loads(path.read_text())


def _axes(review: dict, source: bytes, cell_key: str):
    """Real canonical graph + the axes the R12 consumer derives for one value cell."""
    graph, _, cell_ids, _, _, context_receipts = native_attested_layout(
        review, source, tenant_id=_TENANT, verify_context=True
    )
    blocks = {block.source_id: block for block in graph.blocks}
    focal = blocks[cell_ids[cell_key]]
    result = table_structural_sources(
        graph, (focal.source_ref(),), tenant_id=_TENANT, max_sources=20, max_chars=2000
    )
    return graph, cell_ids, focal, result, context_receipts


def _roles(result):
    """Every axis actually offered, keyed by role and grid position."""
    return {
        (item.role, item.row_number, item.column_number): item
        for item in (*result.verified, *result.context_only)
    }


def _quotes(result):
    return {item.ref.quote for item in (*result.verified, *result.context_only)}


@local_inputs
def test_banded_header_grid_is_recovered_from_the_source():
    review = review_layout(LOTTE, _spec(P29))
    cells = review["cells"]
    # The stub head spans the whole header depth: that is what declares depth 2
    # to every downstream consumer, including table_sources._table_layout.
    assert (cells["r0c0"]["row_span"], cells["r0c0"]["raw_text"]) == (2, "항목")
    # The year band is one printed cell over 연간목표/실적/달성률/증감사유. A per-column
    # boundary would cut the centred "2025" glyphs, which the source-boundary
    # check would (correctly) hold rather than silently accept.
    assert (cells["r0c2"]["column_span"], cells["r0c2"]["raw_text"]) == (4, "2025")
    assert (cells["r0c6"]["column_span"], cells["r0c6"]["raw_text"]) == (2, "2026")
    assert cells["r1c2"]["raw_text"] == "연간목표"
    assert cells["r1c3"]["raw_text"] == "실적"
    assert cells["r5c0"]["raw_text"] == "폐수\n(단위: 천 톤)"
    assert cells["r5c3"]["raw_text"] == "10,551"
    assert cells["r5c2"]["raw_text"] == "11,118"
    assert cells["r4c3"]["raw_text"] == "831"
    # No reviewed rectangle may clip or rotate its own words.
    assert all(cell["extraction_status"] == "source_text_extracted" for cell in cells.values())
    assert not any(
        cell["clipped_word_indices"] or cell["rotated_word_indices"] for cell in cells.values()
    )
    # Data rows start below the header band, one candidate per declared year column.
    assert {c["row"] for c in review["candidates"]} == {2, 3, 4, 5}
    assert {c["column"] for c in review["candidates"]} == {2, 3}
    assert all(c["year"] == "2025" for c in review["candidates"])
    assert all(c["status"] == "candidate_only" and not c["verified"] for c in review["candidates"])
    actual = next(c for c in review["candidates"] if (c["row"], c["column"]) == (5, 3))
    assert actual["value_raw"] == "10,551"
    assert actual["source_cells"]["column_qualifier"] == "r1c3"
    assert actual["column_qualifier_raw"] == "실적"
    # The tool never decides that 실적 means an actual, and never splits the unit
    # out of the stub cell it shares with the row label.
    assert "column_qualifier_semantics_not_resolved" in actual["hold_reasons"]
    assert "unit_literal_shares_the_metric_stub_cell" in actual["hold_reasons"]
    assert actual["binding_status"] == "held"
    assert actual["unit_canonical"] is None
    # The printed footnotes stay caller-reviewed page context, never row context:
    # the second one names 대기오염물질/폐기물 and must not reach 폐수 or 수질.
    assert [c["kind"] for c in review["context"]] == ["footnote", "footnote"]
    assert "달성률(%)" in review["context"][0]["raw_text"]
    assert "대기오염물질" in review["context"][1]["raw_text"]
    assert "폐수" not in review["context"][1]["raw_text"]
    # The 수질 row label stays its own cell; 831 never inherits the 폐수 stub.
    assert cells["r4c0"]["raw_text"] == "수질\n(단위: 톤)"


@local_inputs
def test_actual_value_gets_its_real_row_unit_and_2025_actual_header():
    review = review_layout(LOTTE, _spec(P29))
    _, _, focal, result, _ = _axes(review, LOTTE.read_bytes(), "r5c3")
    assert focal.raw_text == "10,551"
    assert result.resolved
    assert (result.focal_row_number, result.focal_column_number) == (5, 3)
    axes = _roles(result)
    # Row identity and unit, as one printed source literal.
    assert axes[ROW_HEADER, 5, 0].ref.quote == "폐수\n(단위: 천 톤)"
    # Complete column header: the year band and this column's own sub-header.
    assert axes[COLUMN_HEADER, 0, 2].ref.quote == "2025"
    assert axes[COLUMN_HEADER, 0, 2].column_span == 4
    assert axes[COLUMN_HEADER, 1, 3].ref.quote == "실적"
    # The 수질 row above shares the column but is neither this row nor a header.
    assert "831" not in _quotes(result)
    # The 2025 target in the same row is a weaker row qualifier, never the metric
    # and never this value's column header.
    assert axes[ROW_QUALIFIER, 5, 2].ref.quote == "11,118"
    assert axes[ROW_QUALIFIER, 5, 1].ref.quote == "14,300"
    assert not any(
        item.role in (ROW_HEADER, COLUMN_HEADER) and item.ref.quote in ("11,118", "14,300")
        for item in (*result.verified, *result.context_only)
    )
    # 항목 is the stub head of the header band, not an axis of this value.
    assert "항목" not in _quotes(result)
    # Nothing to the right of the value is offered: 105%, the 증감사유 prose, the
    # 2026 band and the 2030 target are not this cell's axes.
    assert not {"105%", "7,150", "10,066", "2026", "2030", "목표"} & _quotes(result)
    assert len(axes) == 5


@local_inputs
def test_recovered_axes_stay_unverified_until_native_attestation_promotes_them():
    """The axes are real source literals but NOT verified refs; record exactly why.

    Reversing this assertion requires a real receipt change, not a code comment, so
    the three distinct refusals below stay visible instead of being assumed away.
    """
    review = review_layout(LOTTE, _spec(P29))
    _, cell_ids, _, result, context_receipts = _axes(review, LOTTE.read_bytes(), "r5c3")
    assert result.verified == ()
    assert len(result.context_only) == 5
    assert {item.ref.verification_state for item in result.context_only} == {"rejected"}
    held = {
        record["source_id"]: record["reason"]
        for proof in context_receipts
        for record in proof["records"]
        if record["status"] != "verified"
    }
    # 실적: the on-device reader does not return the two glyphs of this small crop.
    assert held[cell_ids["r1c3"]] == "rendered_cell_literal_mismatch"
    # 폐수 (단위: 천 톤): the reader reads "천톤" without the printed word space.
    assert held[cell_ids["r5c0"]] == "rendered_cell_literal_mismatch"
    # 2025: a numeric cell may only be promoted by the selected-cell verifier,
    # which refuses this page's bands; the context verifier is never a shortcut.
    assert held[cell_ids["r0c2"]] == "numeric_cell_requires_selected_cell_verifier"
    # Sibling stub cells of the same shape do verify, so this is three specific
    # reader/gate refusals, not a broken bridge.
    verified = {
        record["source_id"]
        for proof in context_receipts
        for record in proof["records"]
        if record["status"] == "verified"
    }
    assert {cell_ids[key] for key in ("r2c0", "r3c0", "r4c0", "r1c2")} <= verified


@local_inputs
def test_actual_extraction_packet_context_replaces_the_nearest_numbers():
    """The packet the R12 extraction path really builds for 10,551 on this grid."""
    review = review_layout(LOTTE, _spec(P29))
    graph, _, focal, result, _ = _axes(review, LOTTE.read_bytes(), "r5c3")
    blocks = {block.source_id: block for block in graph.blocks}
    bounded = table_structural_sources(
        graph, (focal.source_ref(),), tenant_id=_TENANT, max_sources=6, max_chars=1000
    )
    axes = [
        _context_entry(index, blocks[item.ref.source_id], "table_" + item.role)
        for index, item in enumerate(bounded.verified + bounded.context_only)
    ]
    assert [(entry["role"], entry["text"]) for entry in axes] == [
        ("table_row_header", "폐수\n(단위: 천 톤)"),
        ("table_column_header", "2025"),
        ("table_column_header", "실적"),
        ("table_row_qualifier", "14,300"),
        ("table_row_qualifier", "11,118"),
    ]
    # The nearest-bbox context this replaces: on the reviewed grid the neighbours
    # of the value cell are again other numbers, which is the R12 defect.
    neighbours, _ = _bounded_context_blocks(
        graph, (focal.source_ref(),), max_context_chars=2000, max_context_blocks=4
    )
    assert all(entry["role"] == "nearby" for entry in neighbours)
    assert "폐수\n(단위: 천 톤)" not in {entry["text"] for entry in neighbours}
    # Axes are interpretation context only; none is a numbered evidence source.
    assert bounded.verified == ()


@local_inputs
def test_target_value_gets_the_target_column_header_not_the_actual_one():
    review = review_layout(LOTTE, _spec(P29))
    _, _, focal, result, _ = _axes(review, LOTTE.read_bytes(), "r5c2")
    assert focal.raw_text == "11,118"
    axes = _roles(result)
    assert axes[COLUMN_HEADER, 1, 2].ref.quote == "연간목표"
    assert axes[COLUMN_HEADER, 0, 2].ref.quote == "2025"
    assert axes[ROW_HEADER, 5, 0].ref.quote == "폐수\n(단위: 천 톤)"
    assert "실적" not in _quotes(result)
    assert "10,551" not in _quotes(result)


@local_inputs
def test_neighbour_row_keeps_its_own_metric_and_never_borrows_the_one_below():
    """831 is 수질, not 폐수: the row separation holds in both directions."""
    review = review_layout(LOTTE, _spec(P29))
    _, _, focal, result, _ = _axes(review, LOTTE.read_bytes(), "r4c3")
    assert focal.raw_text == "831"
    axes = _roles(result)
    assert axes[ROW_HEADER, 4, 0].ref.quote == "수질\n(단위: 톤)"
    assert axes[ROW_HEADER, 4, 0].ref.verification_state == "verified"
    assert "폐수\n(단위: 천 톤)" not in _quotes(result)
    assert not {"10,551", "11,118", "14,300"} & _quotes(result)
    assert axes[COLUMN_HEADER, 1, 3].ref.quote == "실적"


@local_inputs
def test_existing_single_level_layouts_keep_their_exact_output():
    for pdf, layout, page in ((LOTTE, P117, 117), (KIA, KIA_LAYOUT, 106)):
        review = review_layout(pdf, _spec(layout))
        assert review["physical_page"] == page
        assert len(review["candidates"]) == 30
        assert all(c["binding_status"] == "complete_candidate" for c in review["candidates"])
        assert all(cell["column_span"] == 1 for cell in review["cells"].values())
        assert all("column_qualifier" not in c["source_cells"] for c in review["candidates"])
        assert all("column_qualifier_raw" not in c for c in review["candidates"])


@local_inputs
def test_second_reviewed_layout_reuses_the_same_axis_path_on_its_own_page():
    """Same code, another page and grid: no p29 coordinate, label or span is reused."""
    review = review_layout(LOTTE, _spec(P117))
    graph, _, focal, result, _ = _axes(review, LOTTE.read_bytes(), "r2c3")
    assert {block.page_num for block in graph.blocks} == {117}
    assert result.resolved
    axes = _roles(result)
    assert {item.role for item in axes.values()} >= {ROW_HEADER, COLUMN_HEADER}
    # p117 has a single header row, so its year header is row 0 and there is no
    # sub-header cell at all -- the depth is read from that grid, not from p29's.
    assert all(row == 0 for role, row, _ in axes if role == COLUMN_HEADER)
    assert "폐수\n(단위: 천 톤)" not in _quotes(result)
    assert any(item.ref.verification_state == "verified" for item in result.verified)


def test_span_and_unit_source_guards_fail_closed(tmp_path):
    """Portable: malformed opt-in input must stop the tool, never widen a cell."""
    base = _spec(P29)
    source = tmp_path / "grid.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=1000, height=800)
    writer.write(source)
    base.update(source_sha256=sha256(source.read_bytes()).hexdigest(), physical_page=1)
    for mutate, message in (
        (lambda s: s["colspans"].append([0, 3, 2]), "overlapping reviewed spans"),
        (lambda s: s["rowspans"].append([0, 3, 2]), "overlapping reviewed spans"),
        (lambda s: s["rowspans"].append([1, 1, 3]), "crosses the header band"),
        (lambda s: s["colspans"].append([0, 8, 2]), "invalid reviewed colspan"),
        (lambda s: s["colspans"].append([0, 1, 1]), "invalid reviewed colspan"),
        (lambda s: s["columns"].update(unit=1), "exactly one"),
        (lambda s: s["columns"].pop("unit_in_metric_cell"), "exactly one"),
        (lambda s: s.update(header_rows=6), "header rows"),
        (lambda s: s.update(header_rows=True), "header rows"),
        (lambda s: s.update(parents={"3": 1}), "parent must precede a data row"),
    ):
        spec = deepcopy(base)
        mutate(spec)
        with pytest.raises(ValueError, match=message):
            review_layout(source, spec)
