"""R02b: selected numeric cell + column head attestation over real PDF geometry.

Two layouts are exercised with real page text and real declared cell geometry: a
multi-column grid with holes and a missing header cell (the Kia p106 class) and a
right-aligned single-column strip whose head is left-aligned (the Lotte p117 /
KB p46 class). The rendered reader is stubbed here exactly as the other local
verifier tests do; the real Apple Vision evidence for both layouts is recorded in
``outputs/pipeline-recovery-20260920/table-source`` (Kia 238.6 + head 2024 read
exactly, KB 155,446/155,324/148,926 after the box repair).

The attestation is literal only. A promoted cell clears the downstream numeric
SOURCE gate; it is not a binding, a period, a unit or a grade.
"""

from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import TENANT, candidate

FOREIGN = "22222222-2222-4222-8222-222222222222"


def grid_pdf(words):
    """One page whose text is placed at explicit bottom-left baselines."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    page = writer.add_blank_page(width=600, height=800)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        "\n".join(f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET" for x, y, text in words).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def ink_boxes(source):
    """Actual ink box of every word on page 1, keyed by its text (top-left points)."""
    import io

    import pdfplumber

    with pdfplumber.open(io.BytesIO(source)) as document:
        page = document.pages[0]
        return page.height, {
            word["text"]: (word["x0"], word["top"], word["x1"], word["bottom"])
            for word in page.extract_words()
        }


def table_graph(source, cells, *, offset=0.0, skip=(), extra_blocks=()):
    """Declare a table over the real ink boxes; ``offset`` shifts every cell box.

    ``cells`` maps ``(row, column)`` to the word text of that cell. A non-zero
    offset reproduces the real pdfplumber slot defect where the declared box sits
    below its own text.
    """
    height, boxes = ink_boxes(source)
    blocks, edges, used = [], [], []
    for (row, column), text in sorted(cells.items()):
        if (row, column) in skip:
            continue
        x0, top, x1, bottom = boxes[text]
        top, bottom = top + offset, bottom + offset
        used.append((x0, top, x1, bottom))
        identifier = f"r{row}c{column}"
        blocks.append(
            (
                identifier,
                "table_cell",
                text,
                (x0, height - bottom, x1, height - top),
                (f"row number={row}", f"column number={column}"),
            )
        )
        edges.append((identifier, "T", "table_parent"))
    union = (
        min(box[0] for box in used) - 1,
        min(box[1] for box in used) - 1,
        max(box[2] for box in used) + 1,
        max(box[3] for box in used) + 1,
    )
    blocks.insert(
        0,
        (
            "T",
            "table",
            "\n".join(text for _, text in sorted(cells.items()) if _ not in skip),
            (union[0], height - union[3], union[2], height - union[1]),
            (),
        ),
    )
    batch = candidate("A", [*blocks, *extra_blocks], edges)
    typed = []
    for block in batch.blocks:
        if block.kind == "table_cell":
            row, column = (int(item.split("=")[1]) for item in block.context)
            block = replace(
                block,
                table_native_id="T",
                row_number=row,
                column_number=column,
                row_span=1,
                column_span=1,
            )
        typed.append(block)
    batch = replace(batch, blocks=tuple(typed), source_sha256=sha256(source).hexdigest())
    return fuse_candidates((batch,), tenant_id=TENANT)


# Layout A, the Kia p106 class: a year head, a unit cell, a row label and a value,
# with no header cell above the label column (a real hole) and a second data row.
LAYOUT_A_WORDS = [
    (200, 740, "2024"),
    (300, 740, "2023"),
    (72, 720, "Domestic"),
    (140, 720, "tCO2e"),
    (200, 720, "238.6"),
    (300, 720, "244.6"),
]
LAYOUT_A_CELLS = {
    (1, 3): "2024",
    (1, 4): "2023",
    (2, 1): "Domestic",
    (2, 2): "tCO2e",
    (2, 3): "238.6",
    (2, 4): "244.6",
}

# Layout B, the Lotte p117 / KB p46 class: one column, head left-aligned above a
# right-aligned number, so the two boxes neither nest nor overlap horizontally.
LAYOUT_B_WORDS = [(200, 740, "2025"), (206, 720, "155,446"), (72, 720, "Total")]
LAYOUT_B_CELLS = {(1, 1): "2025", (2, 1): "155,446", (2, 0): "Total"}


def attest(monkeypatch, graph, source, *, rendered=None):
    from proofops.adapters.local import selected_cell_table_verification as v3

    def reader(page, box):
        if rendered is not None:
            return rendered(page, box)
        return dict(status="read", text=page.crop(box).extract_text() or "", scale=3)

    monkeypatch.setattr(v3, "_rendered_cell", reader)
    return v3, v3.attest_tables(graph, source, tenant_id=TENANT)


@pytest.mark.parametrize(
    "words,cells,value,head",
    [
        (LAYOUT_A_WORDS, LAYOUT_A_CELLS, "238.6", "2024"),
        (LAYOUT_B_WORDS, LAYOUT_B_CELLS, "155,446", "2025"),
    ],
    ids=["multi_column_with_holes", "single_column_right_aligned"],
)
def test_each_layout_admits_its_value_with_the_head_of_its_own_column(
    monkeypatch, words, cells, value, head
):
    source = grid_pdf(words)
    graph = table_graph(source, cells)
    v3, receipt = attest(monkeypatch, graph, source)
    (record,) = receipt["records"]
    assert record["status"] == "partially_verified"
    selection = next(s for s in record["selections"] if s["value_text"] == value)
    assert selection["header_text"] == head
    # The head is a literal string above the value; no year/scope/unit is claimed.
    assert selection["header_role"] == "topmost_declared_cell_in_this_column"
    assert (selection["year_binding"], selection["scope_binding"], selection["unit_binding"]) == (
        "not_attested",
        "not_attested",
        "not_attested",
    )
    assert receipt["semantic_binding"] == "undetermined"
    value_cell = next(cell for cell in selection["cells"] if cell["role"] == "value")
    assert value_cell["attestation"] == "native_and_rendered"
    assert selection["promoted_source_ids"] == [value_cell["source_id"]]
    # Every rendered read records the exact crop it came from, and that crop
    # lies inside the cell's own stored bbox: no enlarged hidden context.
    from proofops.adapters.local import merged_table_verification as v2

    for cell in selection["cells"]:
        if "render_crop_box" in cell:
            assert v2._inside(cell["render_crop_box"], cell["bbox"])
    promoted = v3.replay_tables(receipt, graph, source, tenant_id=TENANT)
    verified = {b.source_id for b in promoted.blocks if b.quality == "verified"}
    assert verified == {s["promoted_source_ids"][0] for s in record["selections"]}
    # Neither the table node nor any non-value cell is promoted.
    assert all(
        b.quality == "unverified"
        for b in promoted.blocks
        if b.kind != "table_cell" or b.source_id not in verified
    )


def test_render_crop_outside_the_stored_bbox_is_held_not_promoted(monkeypatch):
    """An ink crop reaching outside the cell's own stored bbox must hold.

    The native words still match (their font boxes are inside), so only the
    crop-inside guard blocks promotion: the rendered read could otherwise
    smuggle neighbouring ink into an "exact" match.
    """
    from proofops.adapters.local import native_glyph_geometry

    source = grid_pdf(LAYOUT_A_WORDS)
    graph = table_graph(source, LAYOUT_A_CELLS)
    real = native_glyph_geometry.native_word_ink_geometry

    def shifted_ink(source_bytes, page_number, indices):
        result = real(source_bytes, page_number, indices)
        for match in result["matched_words"]:
            ink = match["ink_bbox"]
            match["ink_bbox"] = [ink[0], ink[1] + 50.0, ink[2], ink[3] + 50.0]
        return result

    monkeypatch.setattr(native_glyph_geometry, "native_word_ink_geometry", shifted_ink)
    _, receipt = attest(monkeypatch, graph, source)
    (record,) = receipt["records"]
    assert record["selections"] == []
    assert record["reason"] == "render_crop_outside_promoted_bbox"


def test_wrong_year_or_scope_column_is_never_borrowed_for_a_value(monkeypatch):
    """A value whose own column has no head must not take the neighbour's year."""
    words = [(200, 740, "2024"), (72, 720, "Domestic"), (300, 720, "238.6")]
    cells = {(1, 3): "2024", (2, 1): "Domestic", (2, 4): "238.6"}
    source = grid_pdf(words)
    graph = table_graph(source, cells)
    _, receipt = attest(monkeypatch, graph, source)
    (record,) = receipt["records"]
    assert record["selections"] == []
    assert record["reason"] == "no_numeric_selection"
    # And with both columns declared, each value keeps its own column's head.
    full = table_graph(grid_pdf(LAYOUT_A_WORDS), LAYOUT_A_CELLS)
    source_full = grid_pdf(LAYOUT_A_WORDS)
    _, both = attest(monkeypatch, full, source_full)
    pairs = {s["value_text"]: s["header_text"] for s in both["records"][0]["selections"]}
    assert pairs == {"238.6": "2024", "244.6": "2023"}


def test_dropped_cell_text_and_shifted_boxes_and_bad_rendered_read_stay_blocked(monkeypatch):
    source = grid_pdf([*LAYOUT_A_WORDS, (240, 720, "999.9")])
    # A character inside the attested band that belongs to no declared cell is a
    # dropped cell: the region cannot be attested even though every declared
    # cell matches, because a digit could be hiding in the gap.
    graph = table_graph(source, LAYOUT_A_CELLS)
    _, receipt = attest(monkeypatch, graph, source)
    assert receipt["records"][0]["reason"] == "uncovered_or_clipped_character"
    assert receipt["records"][0]["selections"] == []

    # The real Lotte defect reproduced with its own measured numbers: rows
    # 12.755pt apart and every declared box 3.44pt below its own text, so each
    # box straddles two values.
    tight_words = [
        (200, 740, "2025"),
        (206, 740 - 12.755, "155,446"),
        (206, 740 - 25.51, "155,324"),
    ]
    tight_cells = {(1, 1): "2025", (2, 1): "155,446", (3, 1): "155,324"}
    tight = grid_pdf(tight_words)
    shifted = table_graph(tight, tight_cells, offset=3.44)
    _, receipt = attest(monkeypatch, shifted, tight)
    assert receipt["records"][0]["selections"] == []
    assert receipt["records"][0]["reason"] in (
        "native_cell_text_mismatch",
        "uncovered_or_clipped_character",
    )

    clean = grid_pdf(LAYOUT_A_WORDS)

    # A rendered read that disagrees with the pinned text blocks promotion even
    # though the native text matches.
    good = table_graph(clean, LAYOUT_A_CELLS)
    _, receipt = attest(
        monkeypatch,
        good,
        clean,
        rendered=lambda page, box: dict(status="read", text="238.7", scale=3),
    )
    assert receipt["records"][0]["reason"] == "rendered_cell_text_mismatch"
    _, receipt = attest(
        monkeypatch,
        good,
        clean,
        rendered=lambda page, box: dict(status="unresolved", reason="rendered_reader_unavailable"),
    )
    assert receipt["records"][0]["reason"] == "rendered_cell_text_mismatch"


def test_foreign_tenant_and_foreign_source_bytes_are_refused(monkeypatch):
    from proofops.adapters.local import selected_cell_table_verification as v3

    source = grid_pdf(LAYOUT_A_WORDS)
    graph = table_graph(source, LAYOUT_A_CELLS)
    monkeypatch.setattr(
        v3, "_rendered_cell", lambda page, box: dict(status="read", text="x", scale=3)
    )
    with pytest.raises(ValueError):
        v3.attest_tables(graph, source, tenant_id=FOREIGN)
    with pytest.raises(ValueError):
        v3.attest_tables(graph, source + b"x", tenant_id=TENANT)


def test_promoted_cell_clears_the_downstream_numeric_source_gate(monkeypatch):
    """The actual downstream admission: the domain numeric source gate.

    A cleared source gate means the literal value may be cited as its own source.
    It is not a claim binding and not a grade: period, scope and unit stay unset
    here because the attestation asserts none of them.
    """
    from proofops.application.evidence.citations import verify_source_ref
    from proofops.application.ingest.normalize import Observation
    from proofops.domain.numeric import observation_source_holds

    source = grid_pdf(LAYOUT_A_WORDS)
    graph = table_graph(source, LAYOUT_A_CELLS)
    v3, receipt = attest(monkeypatch, graph, source)
    selection = next(s for s in receipt["records"][0]["selections"] if s["value_text"] == "238.6")
    promoted = v3.replay_tables(receipt, graph, source, tenant_id=TENANT)

    def build(current):
        cell = next(b for b in current.blocks if b.source_id == selection["value_source_id"])
        ref = verify_source_ref(cell.source_ref(), current, tenant_id=TENANT)
        candidate_cell = cell.candidates[cell.winner]
        return Observation(
            observation_id="00000000-0000-4000-8000-000000000001",
            tenant_id=TENANT,
            document_version_id=current.document_version_id,
            parse_manifest_id=current.parse_manifest_id,
            source_sha256=current.source_sha256,
            table_id=receipt["records"][0]["table_id"],
            row=candidate_cell.row_number,
            column=candidate_cell.column_number,
            metric_raw=cell.raw_text,
            scope=None,
            subject=None,
            reporting_period="",
            scope2_basis=None,
            organizational_boundary=None,
            baseline_period=None,
            category=None,
            method=None,
            unit_raw=None,
            unit_canonical=None,
            scale_multiplier="1",
            value_raw=cell.raw_text,
            value_decimal=None,
            value_state="reported",
            denominator=None,
            quality=cell.quality,
            footnotes=(),
            source_refs=(ref,),
            source_blocks=(cell,),
            parent_relations=((cell.source_id, receipt["records"][0]["table_id"], "table_parent"),),
        )

    assert (
        "observation_source_unverified" in observation_source_holds(build(graph), graph)["reasons"]
    )
    assert observation_source_holds(build(promoted), promoted)["reasons"] == []
    # The unit cell was attested natively only, so it never becomes verified
    # observation metadata.
    unit = next(cell for cell in selection["cells"] if cell["native_text"] == "tCO2e")
    assert unit["attestation"] in ("native_only", "native_and_rendered")
    assert unit["source_id"] not in selection["promoted_source_ids"]


def test_auxiliary_slot_boxes_snap_to_their_own_ink_or_stay_unrepaired():
    """The genuine upstream fix for the Lotte class, and its refusals."""
    from proofops.adapters.parsing.odl_table_repair import locate_auxiliary_cells

    words = {
        1: [
            {"text": "2025", "bbox": [200.0, 100.0, 224.0, 107.0]},
            {"text": "155,446", "bbox": [200.0, 120.0, 240.0, 127.0]},
        ]
    }
    slots = [
        dict(
            type="table",
            id="t0",
            **{"page number": 1, "bounding box": [198.0, 103.0, 242.0, 130.0]},
            rows=[
                dict(
                    type="table row",
                    id="t0-r0",
                    **{"page number": 1, "bounding box": [198.0, 103.0, 242.0, 116.0]},
                    cells=[
                        dict(
                            type="table cell",
                            id="t0-r0-c0",
                            content="2025",
                            **{
                                "page number": 1,
                                "bounding box": [198.0, 103.0, 242.0, 116.0],
                                "row number": 1,
                                "column number": 1,
                            },
                        )
                    ],
                ),
                dict(
                    type="table row",
                    id="t0-r1",
                    **{"page number": 1, "bounding box": [198.0, 116.0, 242.0, 130.0]},
                    cells=[
                        dict(
                            type="table cell",
                            id="t0-r1-c0",
                            content="155,446",
                            **{
                                "page number": 1,
                                "bounding box": [198.0, 116.0, 242.0, 130.0],
                                "row number": 2,
                                "column number": 1,
                            },
                        )
                    ],
                ),
            ],
        )
    ]
    located, receipts = locate_auxiliary_cells(slots, words)
    assert [r["status"] for r in receipts] == ["repaired"]
    boxes = [cell["bounding box"] for row in located[0]["rows"] for cell in row["cells"]]
    assert boxes == [[200.0, 100.0, 224.0, 107.0], [200.0, 120.0, 240.0, 127.0]]
    assert located[0]["bounding box"] == [200.0, 100.0, 240.0, 127.0]
    # The input is never mutated and no text is rewritten.
    assert slots[0]["rows"][0]["cells"][0]["bounding box"] == [198.0, 103.0, 242.0, 116.0]

    # Refusals: a slot whose text does not match the ink it owns, and a slot with
    # text but no ink at all. Both leave the table untouched.
    wrong = locate_auxiliary_cells(slots, {1: [dict(words[1][0], text="2024"), words[1][1]]})
    assert [r["reason"] for r in wrong[1]] == ["cell_text_word_mismatch"]
    assert wrong[0][0]["rows"][0]["cells"][0]["bounding box"] == [198.0, 103.0, 242.0, 116.0]
    elsewhere = locate_auxiliary_cells(
        slots, {1: [{"text": "far", "bbox": [10.0, 10.0, 20.0, 17.0]}]}
    )
    assert [r["reason"] for r in elsewhere[1]] == ["cell_text_without_words"]


def test_frozen_table_policies_and_their_dispatch_are_unchanged():
    from proofops.adapters.local import merged_table_verification as v2
    from proofops.adapters.local import selected_cell_table_verification as v3
    from proofops.adapters.local import table_source_verification as v1
    from proofops.adapters.parsing.opendataloader import (
        _locates_auxiliary_cells,
        _table_verifier,
    )

    assert len({v1.policy_sha256(), v2.policy_sha256(), v3.policy_sha256()}) == 3
    assert _table_verifier(v1.policy_sha256()) is v1
    assert _table_verifier(v2.policy_sha256()) is v2
    assert _table_verifier(v3.policy_sha256()) is v3
    # The box repair is opt-in: only the v3 policy asks for it.
    assert _locates_auxiliary_cells({"table_source_policy_sha256": v3.policy_sha256()})
    assert not _locates_auxiliary_cells({"table_source_policy_sha256": v2.policy_sha256()})
    assert not _locates_auxiliary_cells({})
