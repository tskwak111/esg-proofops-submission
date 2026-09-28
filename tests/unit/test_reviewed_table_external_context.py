"""R06h: caller-reviewed external footnotes/coverage must not disappear.

``review["context"]`` is the review's own list of footnote/coverage passages
outside the table grid (see the real Kia artifact under
``tests/fixtures/pipeline/reviewed-table-layout/kia-candidates.json``: a Scope 1/2
reclassification footnote and a facility-coverage note, neither referenced by
any candidate's ``notes``/``row_labels``). Before this fix, ``reviewed_table``
never read that key at all, so a real, reviewed, source-pinned passage silently
vanished from the numeric-input diagnostic instead of holding the observation.

The first four tests are fully portable: ``native_attested_layout`` is
monkeypatched so no PDF, OCR or real native verifier runs, following the same
synthetic-review pattern as ``test_reviewed_table.py``. The last test only reads
the real Kia review artifact already on disk; it makes no native or PDF call.
"""

import json
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest
from proofops.adapters.local import reviewed_table
from proofops.adapters.local.reviewed_table import (
    graph_from_review,
    numeric_input_report,
    reviewed_numeric_inputs,
)

APP = Path(__file__).resolve().parents[2]
KIA_REVIEW = APP / "tests/fixtures/pipeline/reviewed-table-layout/kia-candidates.json"

_TENANT = str(uuid5(NAMESPACE_URL, "proofops:reviewed-table:evaluation-tenant"))


def _cell(raw_text, bbox, row, column):
    return {
        "raw_text": raw_text,
        "bbox": bbox,
        "row": row,
        "column": column,
        "row_span": 1,
        "column_span": 1,
        "word_indices": [],
        "clipped_word_indices": [],
        "rotated_word_indices": [],
        "extraction_status": "source_text_extracted",
    }


def _review(source, *, context=None):
    review = {
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
        "words": [{"index": 0, "text": "Metric", "bbox": [0, 0, 25, 10]}],
        "cells": {
            "r0c0": _cell("Metric", [0, 0, 25, 10], 0, 0),
            "r0c1": _cell("Unit", [25, 0, 50, 10], 0, 1),
            "r0c2": _cell("2025", [50, 0, 75, 10], 0, 2),
            "r1c0": _cell("Scope 1", [0, 10, 25, 20], 1, 0),
            "r1c1": _cell("tCO2e", [25, 10, 50, 20], 1, 1),
            "r1c2": _cell("1,234", [50, 10, 75, 20], 1, 2),
        },
        "candidates": [
            {"source_cells": {"metric": "r1c0", "unit": "r1c1", "year": "r0c2", "value": "r1c2"}}
        ],
    }
    if context is not None:
        review["context"] = context
    return review


def _patch_native(monkeypatch):
    """Bypass the real native verifier/OCR; promote every reviewed cell.

    Only ``native_attested_layout``'s return shape matters to the callers under
    test (``reviewed_numeric_inputs``): the real, portable ``graph_from_review``
    still runs, so every downstream binding/hold check exercises real code.
    """

    def fake(review, source, *, tenant_id=_TENANT, verify_context=False, requested_values=False):
        assert requested_values is False  # These cases exercise the unchanged default policy.
        graph, native_ids = graph_from_review(review, source, tenant_id=tenant_id)
        canonical = {
            candidate.source.source_native_id: block.source_id
            for block in graph.blocks
            for candidate in block.candidates
        }
        cell_ids = {key: canonical[native] for key, native in native_ids.items()}
        promoted_ids = frozenset(cell_ids.values())
        receipt = {
            "schema": "fake",
            "policy_sha256": "0" * 64,
            "artifact_sha256": "0" * 64,
            "records": [],
        }
        return graph, receipt, cell_ids, canonical["reviewed-table"], promoted_ids, ()

    monkeypatch.setattr(reviewed_table, "native_attested_layout", fake)


def test_nonempty_external_context_holds_every_candidate_and_is_reported_once(monkeypatch):
    _patch_native(monkeypatch)
    source = b"source bytes"
    footnote = {
        "kind": "footnote",
        "raw_text": "1. reclassified into Scope 1 & 2",
        "bbox": [0, 90, 50, 99],
        "word_indices": [99],
        "clipped_word_indices": [],
        "rotated_word_indices": [],
        "extraction_status": "source_text_extracted",
    }
    review = _review(source, context=[footnote])
    inputs = reviewed_numeric_inputs(review, source)
    report = numeric_input_report(inputs, review)

    (candidate,) = report["candidates"]
    assert "external_context_unbound:footnote" in candidate["numeric_holds"]
    assert candidate["quality"] == "unverified"
    assert report["unresolved_external_context"] == [
        {
            **footnote,
            "review_lineage_complete": False,
            "source_verification": "not_run",
            "association_status": "unknown",
        }
    ]
    holds = report["holds"]
    assert any(h.startswith("reviewed_external_context_present_and_unbound:") for h in holds)


def test_empty_or_absent_external_context_adds_no_hold(monkeypatch):
    _patch_native(monkeypatch)
    source = b"source bytes"
    for context in (None, []):
        review = _review(source, context=context)
        inputs = reviewed_numeric_inputs(review, source)
        assert inputs.external_context == ()
        report = numeric_input_report(inputs, review)
        (candidate,) = report["candidates"]
        holds = candidate["numeric_holds"]
        assert not [h for h in holds if h.startswith("external_context_unbound:")]
        assert "unresolved_external_context" not in report


_MISSING_FIELDS = {"kind": "footnote"}
_EMPTY_WORD_INDICES = {
    "kind": "footnote",
    "raw_text": "x",
    "bbox": [0, 0, 1, 1],
    "word_indices": [],
    "clipped_word_indices": [],
    "rotated_word_indices": [],
    "extraction_status": "source_text_extracted",
}
_BAD_WORD_INDEX_TYPE = {**_EMPTY_WORD_INDICES, "word_indices": ["not-an-int"]}


@pytest.mark.parametrize("broken", [_MISSING_FIELDS, _EMPTY_WORD_INDICES, _BAD_WORD_INDEX_TYPE])
def test_malformed_external_context_is_refused_not_silently_dropped(monkeypatch, broken):
    _patch_native(monkeypatch)
    source = b"source bytes"
    review = _review(source, context=[broken])
    with pytest.raises(ValueError, match="reviewed external context"):
        reviewed_numeric_inputs(review, source)


@pytest.mark.parametrize("invalid", [None, {"not": "a list"}])
def test_malformed_context_is_not_a_list_is_refused(monkeypatch, invalid):
    _patch_native(monkeypatch)
    source = b"source bytes"
    review = _review(source)
    review["context"] = invalid
    with pytest.raises(ValueError, match="reviewed external context must be a list"):
        reviewed_numeric_inputs(review, source)


_KIA_SKIP_REASON = "local operator review is not in the repository"


@pytest.mark.skipif(not KIA_REVIEW.exists(), reason=_KIA_SKIP_REASON)
def test_actual_kia_context_is_a_real_unbound_reviewed_footnote_and_coverage_note():
    """Read-only: inspects the artifact already on disk, no native/PDF call."""
    review = json.loads(KIA_REVIEW.read_text())
    kinds = {entry["kind"] for entry in review["context"]}
    assert kinds == {"footnote", "coverage"}
    assert all(entry["extraction_status"] == "source_text_extracted" for entry in review["context"])
    # None of the 30 candidates bind any cell as a note; the context above is
    # exactly what was, until this fix, dropped rather than held.
    assert all(candidate["source_cells"]["notes"] == [] for candidate in review["candidates"])
