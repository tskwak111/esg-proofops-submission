from hashlib import sha256

import pytest
from proofops.adapters.local.reviewed_table import artifact_from_review


def _review(source):
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
            "r0c0": {
                "raw_text": "Metric",
                "bbox": [0, 0, 25, 10],
                "row": 0,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "word_indices": [0],
                "clipped_word_indices": [],
                "rotated_word_indices": [],
                "extraction_status": "source_text_extracted",
            },
            "r0c1": {
                "raw_text": "Unit",
                "bbox": [25, 0, 50, 10],
                "row": 0,
                "column": 1,
                "row_span": 1,
                "column_span": 1,
                "word_indices": [1],
                "clipped_word_indices": [],
                "rotated_word_indices": [],
                "extraction_status": "source_text_extracted",
            },
            "r0c2": {
                "raw_text": "2025",
                "bbox": [50, 0, 75, 10],
                "row": 0,
                "column": 2,
                "row_span": 1,
                "column_span": 1,
                "word_indices": [2],
                "clipped_word_indices": [],
                "rotated_word_indices": [],
                "extraction_status": "source_text_extracted",
            },
            "r1c0": {
                "raw_text": "Scope 1",
                "bbox": [0, 10, 25, 20],
                "row": 1,
                "column": 0,
                "row_span": 1,
                "column_span": 1,
                "word_indices": [3],
                "clipped_word_indices": [],
                "rotated_word_indices": [],
                "extraction_status": "source_text_extracted",
            },
            "r1c1": {
                "raw_text": "tCO₂e",
                "bbox": [25, 10, 50, 20],
                "row": 1,
                "column": 1,
                "row_span": 1,
                "column_span": 1,
                "word_indices": [4],
                "clipped_word_indices": [],
                "rotated_word_indices": [],
                "extraction_status": "source_text_extracted",
            },
            "r1c2": {
                "raw_text": "1,234",
                "bbox": [50, 10, 75, 20],
                "row": 1,
                "column": 2,
                "row_span": 1,
                "column_span": 1,
                "word_indices": [5],
                "clipped_word_indices": [],
                "rotated_word_indices": [],
                "extraction_status": "source_text_extracted",
            },
        },
        "candidates": [
            {"source_cells": {"metric": "r1c0", "unit": "r1c1", "year": "r0c2", "value": "r1c2"}}
        ],
    }


def test_reviewed_layout_stays_unverified_but_uses_explicit_roles():
    source = b"source bytes"
    artifact = artifact_from_review(_review(source), source)
    (observation,) = artifact["numeric_observations"]
    assert artifact["status"] == "unverified_candidate"
    assert artifact["eligible_for_admission"] is False
    assert observation["value_decimal"] == "1234"
    assert observation["unit_raw"] == "tCO₂e"
    assert observation["unit_interpretation"] == "raw_literal_only"
    assert all(cell["quality"] == "unverified" for cell in artifact["canonical_table"]["cells"])


def test_reviewed_layout_rejects_held_cell_boundary():
    source = b"source bytes"
    review = _review(source)
    review["cells"]["r1c2"]["extraction_status"] = "held"
    with pytest.raises(ValueError, match="source boundary held"):
        artifact_from_review(review, source)
