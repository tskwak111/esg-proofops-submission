"""Native admission of a reviewed table depends on ink-true cell geometry.

The reviewed rectangles in ``reviewed-table-layout`` are drawn on the extractor's
word boxes.  On ``lotte.pdf`` page 117 those boxes sit about 6pt below the glyph
ink they describe, so no reviewed rectangle contained its own text and the
existing selected-cell verifier refused every candidate.

Most tests here are **local integration checks**: they read the operator review
and the customer PDF under ``outputs/`` which are local, git-ignored inputs, and
they run the real on-device rendered reader.  They skip where those inputs are
absent and are not portable CI coverage.  ``test_pinned_word_index_must_be_a_real
_index`` and ``test_unresolvable_word_ink_keeps_the_reviewed_rectangle`` are
portable: they need no PDF.

They pin the guards that must keep holding after the geometry correction: exact
pinned-word lineage, a foreign source, a value whose declared text the source
does not show, and the fact that an omitted context cell never turns into unit or
semantic approval.
"""

import copy
import json
from hashlib import sha256
from pathlib import Path

import pytest
from proofops.adapters.local.reviewed_table import artifact_from_review, normalize_reviewed_layout
from proofops.adapters.local.selected_cell_table_verification import attest_tables

APP = Path(__file__).resolve().parents[2]
REVIEW = APP / "tests/fixtures/pipeline/reviewed-table-layout/lotte-candidates.json"
PDF = APP / "tests/fixtures/source-documents/lotte.pdf"
OTHER_PDF = APP / "tests/fixtures/source-documents/kia.pdf"

local_inputs = pytest.mark.skipif(
    not (REVIEW.exists() and PDF.exists() and OTHER_PDF.exists()),
    reason="local operator review and customer PDFs are not part of the repository",
)

# Lotte p117 row 2 is 여수공장(첨단); the reviewed columns kept here are the row
# label, the unit and the 2025 year column.  Row 0 is the header row.
KEEP = ("r0c1", "r0c2", "r0c3", "r2c1", "r2c2", "r2c3")
VALUE = "53,727"


def _trimmed(keep=KEEP):
    """One real reviewed row plus its header row; nothing is added or rewritten."""
    review = copy.deepcopy(json.loads(REVIEW.read_text()))
    review["cells"] = {key: cell for key, cell in review["cells"].items() if key in keep}
    pinned = {i for cell in review["cells"].values() for i in cell["word_indices"]}
    review["words"] = [word for word in review["words"] if word["index"] in pinned]
    # The bridge needs one explicit metric/unit/year/value role mapping. The row
    # label stands in for the metric role in this trimmed fixture; the merged
    # metric cell lives outside the kept columns, so this is a fixture choice and
    # asserts nothing about the source's metric semantics.
    review["candidates"] = [
        {
            "source_cells": {
                "metric": "r2c1",
                "unit": "r2c2" if "r2c2" in review["cells"] else "r0c2",
                "year": "r0c3",
                "value": "r2c3",
            }
        }
    ]
    return review


def _receipt(review):
    source = PDF.read_bytes()
    graph, _, _, _ = normalize_reviewed_layout(review, source)
    return attest_tables(graph, source, tenant_id=graph.tenant_id), graph


@local_inputs
def test_canonical_cell_box_is_the_ink_union_of_the_reviewed_words_only():
    """Geometry is corrected from pinned lineage; membership and grid are untouched."""
    review = _trimmed()
    artifact = artifact_from_review(review, PDF.read_bytes())
    cells = {cell["cell_key"]: cell for cell in artifact["canonical_table"]["cells"]}
    value = cells["r2c3"]
    reviewed = review["cells"]["r2c3"]

    assert artifact["canonical_cell_geometry"]["cells_from_native_word_ink"] == len(KEEP)
    assert artifact["canonical_cell_geometry"]["cells_from_operator_reviewed_region"] == []
    assert value["canonical_bbox_source"] == "native_word_ink_union"
    # Same words, same text, same grid position and span as the review.
    assert value["word_indices"] == reviewed["word_indices"]
    assert value["raw_text"] == reviewed["raw_text"] == VALUE
    assert (value["row"], value["column"]) == (reviewed["row"], reviewed["column"])
    assert (value["row_span"], value["column_span"]) == (
        reviewed["row_span"],
        reviewed["column_span"],
    )
    # The reviewed rectangle is retained, and the correction is a real shift up,
    # not a widening: the canonical box is strictly smaller and higher.
    assert value["bbox"] == reviewed["bbox"]
    assert value["canonical_bbox"][1] < reviewed["bbox"][1]
    assert value["canonical_bbox"][3] < reviewed["bbox"][3]
    assert value["canonical_bbox"][0] >= reviewed["bbox"][0]
    assert value["canonical_bbox"][2] <= reviewed["bbox"][2]
    assert value["reviewed_region_offset_pt"][1] == pytest.approx(-2.494, abs=0.01)
    # Changed parse, separate identity: the R02h v1 layout/source ids are not
    # reused, while the raw source version is still the same PDF.
    assert artifact["geometry_schema"] == "reviewed_table_native_ink_geometry_v2"
    assert artifact["source"]["source_sha256"] == review["source_sha256"]
    assert artifact["source"]["layout_sha256"] == review["layout_sha256"]
    # Still a candidate: native geometry is not semantics and not admission.
    assert artifact["eligible_for_admission"] is False
    assert artifact["semantic_verification"] == "not_run"
    assert all(cell["quality"] == "unverified" for cell in artifact["canonical_table"]["cells"])


@local_inputs
def test_reviewed_value_cell_passes_native_selected_cell_verification():
    """The real verifier admits the real value cell against the real PDF."""
    receipt, graph = _receipt(_trimmed())
    (record,) = receipt["records"]
    assert record["status"] == "partially_verified", record["reason"]
    (selection,) = record["selections"]
    assert selection["value_text"] == VALUE
    assert selection["header_text"] == "2025"
    assert selection["promoted_source_ids"] == [selection["value_source_id"]]
    # The rendered read is a real OCR read of a crop inside the promoted cell box.
    promoted = {cell["source_id"]: cell for cell in selection["cells"]}
    for source_id in (selection["value_source_id"], selection["header_source_id"]):
        cell = promoted[source_id]
        assert cell["rendered"]["status"] == "read"
        assert cell["rendered_exact"] is True
        assert cell["attestation"] == "native_and_rendered"
        crop, box = cell["render_crop_box"], cell["bbox"]
        assert box[0] <= crop[0] and crop[2] <= box[2]
        assert box[1] <= crop[1] and crop[3] <= box[3]
    assert selection["value_text"] == promoted[selection["value_source_id"]]["native_text"]
    # Nothing semantic was decided by the native receipt.
    assert selection["year_binding"] == "not_attested"
    assert selection["unit_binding"] == "not_attested"
    assert receipt["semantic_binding"] == "undetermined"
    assert receipt["source_sha256"] == graph.source_sha256


@local_inputs
def test_omitted_context_cell_never_becomes_unit_or_semantic_approval():
    """Dropping the unit cell may leave a sourced number attested, never its unit.

    The selected-cell receipt promotes one literal numeric cell and says so; it
    does not bind a unit, a period or a metric.  So an omitted unit cell must not
    silently gain one, and the row must not claim complete context.
    """
    receipt, _ = _receipt(_trimmed(keep=("r0c1", "r0c2", "r0c3", "r2c1", "r2c3")))
    (record,) = receipt["records"]
    for selection in record["selections"]:
        assert selection["unit_binding"] == "not_attested"
        assert selection["year_binding"] == "not_attested"
        assert selection["scope_binding"] == "not_attested"
        assert selection["promoted_source_ids"] == [selection["value_source_id"]]
        roles = [cell["role"] for cell in selection["cells"]]
        assert roles.count("value") == 1
        # No cell of the omitted unit column is present, so nothing stands in for it.
        assert all(
            cell["native_text"] != "tCO2eq" for cell in selection["cells"] if cell["row"] != 0
        )
    assert receipt["semantic_binding"] == "undetermined"
    assert receipt["external_context"] == "not_attested"


@local_inputs
def test_value_text_that_the_source_does_not_show_is_still_refused():
    review = _trimmed()
    review["cells"]["r2c3"]["raw_text"] = "3,727"
    receipt, _ = _receipt(review)
    (record,) = receipt["records"]
    assert record["selections"] == []
    assert record["reason"] == "native_cell_text_mismatch"


@local_inputs
def test_foreign_source_is_refused_before_any_geometry_is_derived():
    with pytest.raises(ValueError, match="reviewed layout source mismatch"):
        artifact_from_review(_trimmed(), OTHER_PDF.read_bytes())


@local_inputs
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda review: review["words"].pop(), "lineage incomplete"),
        (lambda review: review["words"].append(review["words"][0]), "lineage incomplete"),
        (
            lambda review: review["words"][0].update(text=review["words"][0]["text"] + "X"),
            "lineage drifted",
        ),
        (
            lambda review: review["words"][0].update(
                bbox=[review["words"][0]["bbox"][0] + 1, *review["words"][0]["bbox"][1:]]
            ),
            "lineage drifted",
        ),
    ],
)
def test_pinned_word_lineage_must_match_the_reader_exactly(mutate, message):
    """Moving a box is only safe while every pinned word is provably the same word."""
    review = _trimmed()
    mutate(review)
    with pytest.raises(ValueError, match=message):
        artifact_from_review(review, PDF.read_bytes())


def test_pinned_word_index_must_be_a_real_index():
    """Portable: a non-index cannot address a word, so no box is derived from it."""
    source = b"not a pdf"
    review = _synthetic(source)
    review["cells"]["r1c2"]["word_indices"] = [-1]
    with pytest.raises(ValueError, match="word index invalid"):
        artifact_from_review(review, source)


def test_unresolvable_word_ink_keeps_the_reviewed_rectangle():
    """Portable: a source whose ink cannot be read falls back, never admits."""
    source = b"not a pdf"
    artifact = artifact_from_review(_synthetic(source), source)
    geometry = artifact["canonical_cell_geometry"]
    assert geometry["pinned_word_ink"]["status"] == "operator_reviewed_region"
    assert geometry["cells_from_native_word_ink"] == 0
    for cell in artifact["canonical_table"]["cells"]:
        assert cell["canonical_bbox_source"] == "operator_reviewed_region"
        assert cell["canonical_bbox"] == cell["bbox"]
        assert cell["canonical_bbox_hold_reason"] == "pinned_word_ink_unresolved"
    assert artifact["eligible_for_admission"] is False


def _synthetic(source: bytes) -> dict:
    """Minimal in-repo review; no PDF and no customer document is involved."""
    cell = {
        "row_span": 1,
        "column_span": 1,
        "clipped_word_indices": [],
        "rotated_word_indices": [],
        "extraction_status": "source_text_extracted",
    }
    return {
        "status": "candidate_only",
        "source_sha256": sha256(source).hexdigest(),
        "physical_page": 1,
        "coordinate_system": "pdf_top_left_points",
        "page_bbox": [0, 0, 100, 100],
        "layout_sha256": "a" * 64,
        "tool_sha256": "b" * 64,
        "reader_version": "test",
        "native_verification": "not_run",
        "semantic_verification": "not_run",
        "layout_interpretation": "manual_review_not_gold",
        "layout": {"reviewer": "operator"},
        "cells": {
            "r0c2": {
                **cell,
                "raw_text": "2025",
                "bbox": [50, 0, 75, 10],
                "row": 0,
                "column": 2,
                "word_indices": [2],
            },
            "r1c0": {
                **cell,
                "raw_text": "Scope 1",
                "bbox": [0, 10, 25, 20],
                "row": 1,
                "column": 0,
                "word_indices": [3],
            },
            "r1c1": {
                **cell,
                "raw_text": "tCO₂e",
                "bbox": [25, 10, 50, 20],
                "row": 1,
                "column": 1,
                "word_indices": [4],
            },
            "r1c2": {
                **cell,
                "raw_text": "1,234",
                "bbox": [50, 10, 75, 20],
                "row": 1,
                "column": 2,
                "word_indices": [5],
            },
        },
        "candidates": [
            {"source_cells": {"metric": "r1c0", "unit": "r1c1", "year": "r0c2", "value": "r1c2"}}
        ],
    }
