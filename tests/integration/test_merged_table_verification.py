"""Real PDF bytes, synthetic graph, explicitly mocked OCR for merged/ODL-repaired tables.

The v2 opt-in verifier accepts explicit row/column spans and ODL-repair
text-sized cell boxes that align by ordered nonoverlapping bands rather than
exact rectangular cell edges. It must never promote unknown/conflict/unlocated
grids, duplicated merged text, unresolved notes, or unmatched descendants.
Live LG/HMM replay and parser integration are owned by the coordinator.
"""

from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO

import pytest
from proofops.application.ingest.graph_fusion import QualityIssue, fuse_candidates
from proofops.domain.provenance import canonical_hash
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from tests.acceptance.test_parsing import TENANT, candidate

# A 2x3 grid where the unit column ("TJ") is a single merged cell spanning both
# data rows. Each cell box is text-sized (smaller than its grid slot) as ODL
# repair emits; the cells still fall into ordered, nonoverlapping bands.
#   col0 (label)   col1 (2024)  col2 (2025)
#   row0: Energy      124          131         <- header-ish data
#   row1: TJ (merged over rows 0..1 in col? no) ...
#
# We model: rows 0 and 1, columns 0..2. Column 0 row0/row1 hold labels,
# column 1/2 hold numbers, and a merged unit cell "TJ" spans rows 0..1 in a
# dedicated column 3. Text is never duplicated across the rows it spans.

# Layout in native PDF bottom-left points (as candidate() consumes them). The
# canonical graph bbox is top-left (y' = page_height - y), so to make row index
# increase spatially DOWNWARD (ascending canonical y) row 0 must sit HIGHER on
# the page (larger native y) than row 1.
# Each entry: native_id, text, (row, col, row_span, col_span), native cell bbox.
CELLS = [
    ("C00", "Label", (0, 0, 1, 1), (30, 730, 90, 745)),
    ("C01", "124", (0, 1, 1, 1), (150, 730, 180, 745)),
    ("C02", "131", (0, 2, 1, 1), (250, 730, 280, 745)),
    ("C10", "Scope", (1, 0, 1, 1), (30, 700, 95, 715)),
    ("C11", "25", (1, 1, 1, 1), (150, 700, 175, 715)),
    ("C12", "79", (1, 2, 1, 1), (250, 700, 275, 715)),
    # Merged unit cell spanning both rows in its own column (single text once).
    ("U", "TJ", (0, 3, 2, 1), (330, 700, 360, 745)),
]
TABLE_BBOX = (20, 695, 380, 750)


def _draw_text(rows_text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    stream = DecodedStreamObject()
    # rows_text: list of (text, x_pt, baseline_y_pt) in PDF bottom-left points.
    stream.set_data(
        "\n".join(f"BT /F1 10 Tf {x} {y} Td ({text}) Tj ET" for text, x, y in rows_text).encode()
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def fixture(cells=CELLS):
    # Build the graph first (bboxes are native/bottom-left), then draw each cell
    # text into the page at the *canonical* top-left region pdfplumber will crop,
    # so native extraction and rendered OCR both see the exact cell text.
    grid = {(sp[0], sp[1]): text for _id, text, sp, _box in cells}
    n_rows = max(sp[0] + sp[2] for _id, _t, sp, _b in cells)
    n_cols = max(sp[1] + sp[3] for _id, _t, sp, _b in cells)
    table_text = "\n".join(
        "\t".join(grid.get((r, c), "") for c in range(n_cols)) for r in range(n_rows)
    )

    raw = [("T", "table", table_text, TABLE_BBOX, ())]
    edges = []
    for cid, text, _sp, box in cells:
        raw.append((cid, "table_cell", text, box, ()))
        edges.append((cid, "T", "table_parent"))

    batch = candidate("odl_header_v1", raw, edges, family="opendataloader")
    span = {cid: sp for cid, _t, sp, _b in cells}
    batch = replace(
        batch,
        blocks=tuple(
            replace(
                b,
                table_native_id="T",
                row_number=span[b.source.source_native_id][0],
                column_number=span[b.source.source_native_id][1],
                row_span=span[b.source.source_native_id][2],
                column_span=span[b.source.source_native_id][3],
            )
            if b.kind == "table_cell"
            else b
            for b in batch.blocks
        ),
    )

    # Draw using the canonical bbox (top-left points). PDF content uses a
    # bottom-left origin, so baseline y = page_height - canonical_bottom + 3.
    draw = []
    for b in batch.blocks:
        if b.kind != "table_cell":
            continue
        cbox = b.bbox
        draw.append((b.source.raw_text, cbox[0] + 1, 800 - cbox[3] + 3))
    source = _draw_text(draw)

    batch = replace(batch, source_sha256=sha256(source).hexdigest())
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    table = next(b for b in graph.blocks if b.kind == "table")
    graph = replace(
        graph,
        issues=graph.issues
        + (
            QualityIssue(
                "vision", "table_vision_not_run", 1, (table.source_id,), "open", "not_run"
            ),
        ),
    )
    return source, graph


def ocr(page, box, **kwargs):
    return {
        "status": "read",
        "text": page.crop(box).extract_text() or "",
        "image_sha256": "a" * 64,
        "reader": "mocked_for_test",
    }


def _module():
    from proofops.adapters.local import merged_table_verification as v

    return v


def test_policy_binds_v2_file_and_v1_dependency():
    v = _module()
    from proofops.adapters.local import table_source_verification as v1

    digest = v.policy_sha256()
    assert isinstance(digest, str) and len(digest) == 64
    # v2 policy must differ from v1 (own file + v1 dependency bound in).
    assert digest != v1.policy_sha256()
    # Deterministic and stable across calls.
    assert digest == v.policy_sha256()


def test_merged_positive_replays_and_is_immutable(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    source, graph = fixture()
    before = canonical_hash(asdict(graph))
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert receipt["records"][0]["status"] == "verified", receipt["records"][0]
    merged = next(c for c in receipt["records"][0]["cells"] if c["native_text"] == "TJ")
    assert merged["row_span"] == 2 and merged["column_span"] == 1
    # The merged unit text appears exactly once across the receipt.
    assert sum(c["native_text"] == "TJ" for c in receipt["records"][0]["cells"]) == 1
    checked = v.replay_tables(receipt, graph, source, tenant_id=TENANT)
    assert all(b.quality == "verified" for b in checked.blocks if b.kind != "table" or True)
    assert checked.issues[0].state == "resolved"
    # Raw graph and receipt are untouched by attest/replay.
    assert canonical_hash(asdict(graph)) == before
    assert receipt["semantic_binding"] == "undetermined"
    assert receipt["scope"].endswith("bands_only") or "band" in receipt["scope"]
    # Replay recomputes; tampered source or tenant is rejected.
    with pytest.raises(ValueError):
        v.replay_tables(receipt, graph, source + b"x", tenant_id=TENANT)
    with pytest.raises(ValueError):
        v.replay_tables(receipt, graph, source, tenant_id="22222222-2222-4222-8222-222222222222")


def test_altered_receipt_cannot_promote(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    source, graph = fixture()
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    # Tamper an attested field: replay recomputes and the hash must diverge.
    tampered = dict(receipt)
    tampered["records"] = [
        dict(r, cells=[dict(c, native_text="TAMPERED") for c in r.get("cells", [])])
        for r in receipt["records"]
    ]
    with pytest.raises(ValueError):
        v.replay_tables(tampered, graph, source, tenant_id=TENANT)


def _fuse(cells):
    """Rebuild a graph from a mutated CELLS list."""
    return fixture(cells)


def test_overlapping_spans_are_rejected(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    # Make the merged unit cell claim col 2 as well, overlapping C02/C12.
    bad = [
        (cid, text, (sp[0], sp[1], sp[2], 2) if cid == "U" else sp, box)
        if cid == "U"
        else (cid, text, sp, box)
        for cid, text, sp, box in CELLS
    ]
    # U now spans columns 3..4 -> shift its col origin to 2 to force overlap.
    bad = [
        ("U", "TJ", (0, 2, 2, 1), (330, 700, 360, 745)) if cid == "U" else (cid, text, sp, box)
        for cid, text, sp, box in CELLS
    ]
    source, graph = _fuse(bad)
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_incomplete_occupancy_is_rejected(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    # Drop a cell so the occupancy grid has a hole.
    holed = [c for c in CELLS if c[0] != "C11"]
    source, graph = _fuse(holed)
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_wrong_year_or_unit_cannot_promote(monkeypatch):
    v = _module()
    for mutation, target, wrong in (("year", "131", "132"), ("unit", "TJ", "GJ")):
        v._attest_json.cache_clear()
        # Change graph raw_text without changing the drawn PDF -> mismatch.
        source, graph = fixture()
        monkeypatch.setattr(v, "_rendered_text", ocr)
        batch = graph.candidates[0]
        graph2 = fuse_candidates(
            (
                replace(
                    batch,
                    blocks=tuple(
                        replace(b, source=replace(b.source, raw_text=wrong))
                        if b.kind == "table_cell" and b.source.raw_text == target
                        else b
                        for b in batch.blocks
                    ),
                ),
            ),
            tenant_id=TENANT,
        )
        table = next(b for b in graph2.blocks if b.kind == "table")
        graph2 = replace(
            graph2,
            issues=graph2.issues
            + (
                QualityIssue(
                    "vision", "table_vision_not_run", 1, (table.source_id,), "open", "not_run"
                ),
            ),
        )
        receipt = v.attest_tables(graph2, source, tenant_id=TENANT)
        assert not any(r["status"] == "verified" for r in receipt["records"]), mutation


def test_clipped_character_cannot_promote(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    # Shrink the merged cell so one of its characters falls outside every cell.
    clipped = [
        ("U", "TJ", (0, 3, 2, 1), (330, 700, 336, 705)) if cid == "U" else (cid, text, sp, box)
        for cid, text, sp, box in CELLS
    ]
    source, graph = _fuse(clipped)
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_ocr_disagreement_cannot_promote(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    source, graph = fixture()
    monkeypatch.setattr(
        v, "_rendered_text", lambda *a, **kw: dict(status="read", text="999", image_sha256="b" * 64)
    )
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_unresolved_note_blocks_promotion(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    source, graph = fixture()
    table = next(b for b in graph.blocks if b.kind == "table")
    graph = replace(
        graph,
        issues=graph.issues
        + (
            QualityIssue(
                "note", "table_note_review", 1, (table.source_id,), "open", "unknown ownership"
            ),
        ),
    )
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_unlocated_table_cannot_promote(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    source, graph = fixture()
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, winner=None, quality="conflicted") if b.kind == "table" else b
            for b in graph.blocks
        ),
    )
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_swapped_row_ordinals_are_rejected(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    # Swap the row numbers so the ordinal order disagrees with the top-left
    # canonical y order: row 0 now sits lower on the page than row 1.
    swapped = [
        (cid, text, (1 - sp[0], sp[1], sp[2], sp[3]) if cid != "U" else sp, box)
        for cid, text, sp, box in CELLS
    ]
    # Keep the merged unit consistent with the new ordering (still origin row 0).
    source, graph = _fuse(swapped)
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_unbounded_span_is_rejected(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    # A cell claiming an enormous row span must be rejected before expansion.
    huge = [
        ("U", "TJ", (0, 3, 10_000, 1), (330, 700, 360, 745)) if cid == "U" else (cid, text, sp, box)
        for cid, text, sp, box in CELLS
    ]
    source, graph = _fuse(huge)
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph, source, tenant_id=TENANT)
    assert not any(r["status"] == "verified" for r in receipt["records"])


def test_descendant_heading_not_source_matched_stays_unverified(monkeypatch):
    v = _module()
    v._attest_json.cache_clear()
    source, graph = fixture()
    # Add a heading child under a cell whose raw_text is NOT in the PDF: it may
    # be checked but must never inherit verified.
    batch = graph.candidates[0]
    from proofops.application.ingest.graph_fusion import CandidateBlock
    from proofops.domain.documents import NativeSource, PageGeometry

    run = batch.blocks[0].source.parser_run_id
    heading = CandidateBlock(
        "heading",
        NativeSource(
            batch.document_version_id,
            batch.parse_manifest_id,
            run,
            "H1",
            1,
            None,
            (30, 700, 60, 712),
            "pdf_bottom_left_points",
            "PhantomHeading",
            0,
            13,
        ),
        PageGeometry(600, 800, 0, (0, 0, 600, 800)),
    )
    batch = replace(
        batch,
        blocks=batch.blocks + (heading,),
        edges=batch.edges + (type(batch.edges[0])("H1", "C00", "table_parent"),),
    )
    graph2 = fuse_candidates((batch,), tenant_id=TENANT)
    table = next(b for b in graph2.blocks if b.kind == "table")
    graph2 = replace(
        graph2,
        issues=graph2.issues
        + (
            QualityIssue(
                "vision", "table_vision_not_run", 1, (table.source_id,), "open", "not_run"
            ),
        ),
    )
    monkeypatch.setattr(v, "_rendered_text", ocr)
    receipt = v.attest_tables(graph2, source, tenant_id=TENANT)
    checked = v.replay_tables(receipt, graph2, source, tenant_id=TENANT)
    heading_block = next(b for b in checked.blocks if b.kind == "heading")
    assert heading_block.quality != "verified"


def test_native_containment_allows_only_parser_rounding():
    from proofops.adapters.local import merged_table_verification as v

    assert v._inside((0.9997, 1.0002, 1.9998, 2.0003), (1, 1, 2, 2))
    assert not v._inside((0.998, 1, 2, 2), (1, 1, 2, 2))
    assert not v._inside((1, 1, 2, 2.002), (1, 1, 2, 2))
