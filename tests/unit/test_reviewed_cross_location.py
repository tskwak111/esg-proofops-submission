"""R06i: two reviewed grids without promoting unverified outside context.

R06g got a real claim span as far as the numeric service and was refused for two
named reasons: the claim lived in a different parse from the reviewed grid, and its
source was never natively verified. This module covers the change that removes
exactly those two -- ``graph_from_review(..., also=...)`` builds one explicitly new
combined snapshot, and the cross-location bridge natively re-attests both grids
before a binding is accepted -- plus the new guards that keep it honest.

Most checks here are portable: they use synthetic reviews and never open a PDF, run
OCR or call a native verifier. The last check is the real one, over the customer
PDF and the two reviewed layout artifacts on disk; it skips when those are absent.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
from proofops.adapters.local.reviewed_claim_bridge import (
    cross_location_case,
    prepare_cross_location_snapshot,
    reconcile_reviewed_context,
    shared_grid_identity,
)
from proofops.adapters.local.reviewed_table import graph_from_review

APP = Path(__file__).resolve().parents[2]
CASE_DIR = APP / "tests/fixtures/pipeline/cross-location"
CASES = CASE_DIR / "cases.json"
PDF = APP / "tests/fixtures/source-documents/kia.pdf"


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


def _review(source, *, page, layout, value="1,234", label="HEV", context=()):
    """A minimal one-row reviewed grid; no PDF or native reader is involved."""
    return {
        "status": "candidate_only",
        "source_sha256": sha256(source).hexdigest(),
        "physical_page": page,
        "coordinate_system": "pdf_top_left_points",
        "page_bbox": [0, 0, 100, 100],
        "layout_sha256": layout,
        "tool_sha256": "b" * 64,
        "reader_version": "test",
        "native_verification": "not_run",
        "semantic_verification": "not_run",
        "layout_interpretation": "manual_review_not_gold",
        "layout": {"reviewer": "operator"},
        "words": [],
        "cells": {
            "r0c0": _cell("지표명", [0, 0, 25, 10], 0, 0),
            "r0c1": _cell("단위", [25, 0, 50, 10], 0, 1),
            "r0c2": _cell("2022", [50, 0, 75, 10], 0, 2),
            "r1c0": _cell(label, [0, 10, 25, 20], 1, 0),
            "r1c1": _cell("대", [25, 10, 50, 20], 1, 1),
            "r1c2": _cell(value, [50, 10, 75, 20], 1, 2),
        },
        "candidates": [
            {
                "source_cells": {"metric": "r1c0", "unit": "r1c1", "year": "r0c2", "value": "r1c2"},
                "metric_raw": label,
                "unit_raw": "대",
                "year": "2022",
                "value_raw": value,
            }
        ],
        "context": list(context),
    }


def _context(kind, raw_text):
    return {
        "kind": kind,
        "raw_text": raw_text,
        "bbox": [0, 90, 50, 99],
        "word_indices": [7],
        "clipped_word_indices": [],
        "rotated_word_indices": [],
        "extraction_status": "source_text_extracted",
    }


# --- the combined snapshot --------------------------------------------------


def test_single_review_keeps_its_existing_identity_and_cell_ids():
    source = b"one source"
    graph, native_ids = graph_from_review(_review(source, page=7, layout="a" * 64), source)
    # Existing artifacts must still reproduce: unprefixed native ids, no table entry.
    assert native_ids["r1c2"] == "reviewed-cell:r1c2"
    assert "reviewed-table" not in native_ids
    assert {block.page_num for block in graph.blocks} == {7}


def test_combined_snapshot_holds_both_pages_under_its_own_new_manifest():
    source = b"one source"
    evidence = _review(source, page=106, layout="a" * 64)
    claim = _review(source, page=35, layout="c" * 64)
    alone = graph_from_review(evidence, source)[0]
    other = graph_from_review(claim, source)[0]
    combined, native_ids = graph_from_review(evidence, source, also=(claim,))

    # Same original bytes, so the document version is the same; the parse rule is
    # new, so the manifest is neither grid's own and no old manifest is reused.
    assert combined.source_sha256 == alone.source_sha256 == other.source_sha256
    assert combined.document_version_id == alone.document_version_id
    assert combined.parse_manifest_id not in {alone.parse_manifest_id, other.parse_manifest_id}
    # Both physical pages are present, each grid keeps its own page and table.
    assert {block.page_num for block in combined.blocks} == {35, 106}
    assert len([block for block in combined.blocks if block.kind == "table"]) == 2
    assert native_ids["r1c2"] == "reviewed-cell:r1c2"
    assert native_ids["1:r1c2"] == "reviewed-cell:1:r1c2"
    assert native_ids["1:reviewed-table"] == "1:reviewed-table"
    # Nothing is verified by being combined.
    assert {block.quality for block in combined.blocks} == {"unverified"}
    # Each grid's own reviewed layout hash stays on its own blocks.
    pages = {block.page_num: block.candidates[block.winner].context for block in combined.blocks}
    assert "layout_sha256=" + "a" * 64 in pages[106]
    assert "layout_sha256=" + "c" * 64 in pages[35]


def test_combined_snapshot_refuses_a_different_source_or_a_repeated_layout():
    source = b"one source"
    evidence = _review(source, page=106, layout="a" * 64)
    foreign = _review(b"other source", page=35, layout="c" * 64)
    foreign["source_sha256"] = sha256(source).hexdigest()  # only the hash is claimed
    foreign["cells"]["r1c2"]["raw_text"] = "9,999"
    with pytest.raises(ValueError, match="source"):
        graph_from_review(evidence, b"other source", also=(foreign,))
    with pytest.raises(ValueError, match="duplicate reviewed layout"):
        graph_from_review(evidence, source, also=(_review(source, page=35, layout="a" * 64),))


# --- the shared-grid ground -------------------------------------------------


def test_shared_grid_identity_needs_equal_values_labels_and_enough_cells():
    source = b"one source"
    evidence = _review(source, page=106, layout="a" * 64)
    claim = _review(source, page=35, layout="c" * 64)
    one_row = shared_grid_identity(evidence, claim)
    # One equal row is overlap only; even a large grid cannot establish population.
    assert one_row["shared_cells"] == 1 and one_row["equal_cells"] == 1
    assert one_row["satisfied"] is False and one_row["holds"] == [
        "population_not_established_by_equal_values"
    ]

    wide_evidence = _review(source, page=106, layout="a" * 64)
    wide_claim = _review(source, page=35, layout="c" * 64)
    for review in (wide_evidence, wide_claim):
        review["candidates"] = [
            {
                "source_cells": {"metric": "r1c0", "unit": "r1c1", "year": "r0c2", "value": "r1c2"},
                "metric_raw": label,
                "unit_raw": "대",
                "year": year,
                "value_raw": f"{index}",
            }
            for label in ("HEV", "PHEV")
            for index, year in enumerate(("2022", "2023", "2024"))
        ]
    assert shared_grid_identity(wide_evidence, wide_claim)["satisfied"] is False

    # One differing shared cell, and one row label only on one side, both fail it.
    wide_claim["candidates"][0] = {**wide_claim["candidates"][0], "value_raw": "42"}
    differing = shared_grid_identity(wide_evidence, wide_claim)
    assert differing["satisfied"] is False
    assert "shared_grid_values_differ" in differing["holds"]
    assert differing["differing_keys"] == [["HEV", "대", "2022"]]

    wide_claim["candidates"] = [
        {**item, "metric_raw": "EV"} for item in wide_claim["candidates"][:3]
    ]
    labels = shared_grid_identity(wide_evidence, wide_claim)
    assert labels["satisfied"] is False
    assert "shared_grid_row_labels_differ" in labels["holds"]


def test_marker_only_row_labels_are_shared_but_the_raw_literals_still_differ():
    source = b"one source"
    evidence = _review(source, page=106, layout="a" * 64, label="총 합계1")
    claim = _review(source, page=35, layout="c" * 64, label="총 합계")
    identity = shared_grid_identity(evidence, claim)
    # A trailing digit is preserved: this layer cannot know whether it is a note marker.
    assert identity["evidence_row_labels"] == ["총 합계1"]
    assert identity["claim_row_labels"] == ["총 합계"]
    # The cell literals themselves are untouched, so the domain still sees them differ.
    assert evidence["cells"]["r1c0"]["raw_text"] != claim["cells"]["r1c0"]["raw_text"]


# --- the reviewed-context dispositions -------------------------------------


def _reconcile(evidence, claim, dispositions, *, bound_period="2022", satisfied=True):
    return reconcile_reviewed_context(
        evidence_review=evidence,
        claim_review=claim,
        bound_period=bound_period,
        dispositions=dispositions,
        identity={"holds": [] if satisfied else ["shared_grid_too_small"]},
    )


def test_an_undisposed_reviewed_passage_holds_the_observation():
    source = b"one source"
    evidence = _review(source, page=106, layout="a" * 64, context=(_context("coverage", "국내"),))
    claim = _review(source, page=35, layout="c" * 64)
    records, holds = _reconcile(evidence, claim, [])
    assert holds == ("reviewed_context_disposition_missing:evidence:coverage",)
    assert [record["state"] for record in records] == ["unreconciled"]


def test_identical_qualifier_ground_requires_the_text_at_the_other_location():
    source = b"one source"
    note = _context("footnote", "1. 도매 기준")
    evidence = _review(source, page=106, layout="a" * 64, context=(note,))
    claim = _review(source, page=35, layout="c" * 64)
    disposition = {
        "location": "evidence",
        "kind": "footnote",
        "raw_text": "1. 도매 기준",
        "ground": "identical_qualifier_at_the_other_location",
        "reviewer": "ai review",
        "rationale": "same basis note at both locations",
    }
    _, holds = _reconcile(evidence, claim, [disposition])
    assert holds == ("reviewed_context_unreconciled:evidence:footnote",)

    claim_with_note = _review(source, page=35, layout="c" * 64, context=(note,))
    records, holds = _reconcile(
        evidence,
        claim_with_note,
        [disposition, {**disposition, "location": "claim"}],
    )
    assert len(holds) == 2
    assert [record["state"] for record in records] == ["unreconciled", "unreconciled"]
    assert {record["review_kind"] for record in records} == {
        "ai_delegated_domain_review_not_independent_gold"
    }


def test_period_ground_refuses_a_qualifier_that_names_the_bound_period():
    source = b"one source"
    note = _context("note", "*2025년 CEO Investor Day 발표 기준")
    claim = _review(source, page=35, layout="c" * 64, context=(note,))
    claim["candidates"].append(
        {
            "source_cells": {"metric": "r1c0", "unit": "r1c1", "year": "r0c2", "value": "r1c2"},
            "metric_raw": "HEV",
            "unit_raw": "대",
            "year": "2025(목표)",
            "value_raw": "2,000",
        }
    )
    evidence = _review(source, page=106, layout="a" * 64)
    disposition = {
        "location": "claim",
        "kind": "note",
        "raw_text": "*2025년 CEO Investor Day 발표 기준",
        "ground": "period_qualifier_outside_the_bound_period",
        "reviewer": "ai review",
        "rationale": "qualifies the separate 2025 plan column",
    }
    assert _reconcile(evidence, claim, [disposition], bound_period="2022")[1] == (
        "reviewed_context_unreconciled:claim:note",
    )
    # Neither year matching nor a different year establishes the note applicability.
    assert _reconcile(evidence, claim, [disposition], bound_period="2025(목표)")[1] == (
        "reviewed_context_unreconciled:claim:note",
    )


def test_shared_grid_ground_and_unmatched_or_unnamed_dispositions_are_refused():
    source = b"one source"
    evidence = _review(source, page=106, layout="a" * 64, context=(_context("coverage", "국내"),))
    claim = _review(source, page=35, layout="c" * 64)
    ground = {
        "location": "evidence",
        "kind": "coverage",
        "raw_text": "국내",
        "ground": "population_corroborated_by_shared_grid_identity",
        "reviewer": "ai review",
        "rationale": "every shared cell agrees",
    }
    assert _reconcile(evidence, claim, [ground])[1] == (
        "reviewed_context_unreconciled:evidence:coverage",
    )
    # The ground is only as good as the computed identity it names.
    assert _reconcile(evidence, claim, [ground], satisfied=False)[1] == (
        "reviewed_context_unreconciled:evidence:coverage",
    )
    # A free-text approval, a missing rationale and a stray disposition all hold.
    assert _reconcile(evidence, claim, [{**ground, "ground": "looks fine"}])[1] == (
        "reviewed_context_unreconciled:evidence:coverage",
    )
    assert _reconcile(evidence, claim, [{**ground, "rationale": "  "}])[1] == (
        "reviewed_context_unreconciled:evidence:coverage",
    )
    assert (
        "reviewed_context_disposition_unmatched:1"
        in _reconcile(evidence, claim, [ground, {**ground, "raw_text": "해외"}])[1]
    )


# --- the real report -------------------------------------------------------

requires_real_inputs = pytest.mark.skipif(
    not (PDF.exists() and CASES.exists()),
    reason="customer PDF or reviewed cross-location inputs unavailable",
)


@pytest.fixture(scope="module")
def real_cases():
    inputs = json.loads(CASES.read_text())
    source = (APP / inputs["source"]).read_bytes()
    evidence = json.loads((CASE_DIR / inputs["evidence_review"]).read_text())
    claim = json.loads((CASE_DIR / inputs["claim_review"]).read_text())
    snapshot = prepare_cross_location_snapshot(evidence, claim, source)
    return inputs, snapshot


@requires_real_inputs
def test_real_kia_body_and_appendix_rows_preserve_cells_but_hold_unverified_context(real_cases):
    inputs, snapshot = real_cases
    cases = {
        case["label"]: cross_location_case(
            snapshot,
            evidence_cells=case["evidence_cells"],
            claim_cells=case["claim_cells"],
            dispositions=inputs["dispositions"],
            reviewer=inputs["reviewer"],
            label=case["label"],
        )
        for case in inputs["cases"]
    }
    decided = cases["computable_cross_page_consistency_hev_2022"]

    # Two different physical pages of one report, in one snapshot that is neither
    # grid's own parse manifest.
    assert decided["claim"]["page_num"] == 35
    assert decided["evidence_observation"]["page_num"] == 106
    assert [side["physical_page"] for side in decided["snapshot"]["combined_of"]] == [106, 35]

    # Both sides are natively verified, not asserted: every claim and evidence cell
    # verified through the citation verifier and is in a replayed native receipt.
    assert decided["claim"]["holds"] == []
    assert len(decided["evidence_observation"]["holds"]) == 5
    assert decided["claim"]["source_quality"] == "verified"
    assert decided["evidence_observation"]["quality"] == "unverified"
    assert decided["snapshot"]["context_receipt_sha256"]

    # The binding's dimensions are the claim page's own literals.
    assert decided["binding"]["metric_raw"] == decided["claim"]["cell_literals"]["metric"]
    assert decided["binding"]["reported_value"] == decided["claim"]["cell_literals"]["value"]

    # The pure domain check refuses an unproven semantic binding.
    outcome = decided["numeric_service_call"]["outcome"]
    assert outcome["status"] == "needs_review"
    assert outcome["reason"] == "binding_not_accepted"
    assert decided["binding"]["binding_accepted"] is False
    assert decided["claim"]["cell_literals"]["value"] == "254,327"
    assert decided["evidence_observation"]["value_decimal"] == "254327"
    assert decided["numeric_service_call"]["has_findings"] is False

    # Every reviewed outside passage stays unresolved despite the reviewer proposal.
    reconciliation = decided["reviewed_context_reconciliation"]
    assert len(reconciliation) == 5
    assert {record["state"] for record in reconciliation} == {"unreconciled"}
    assert decided["shared_grid_identity"]["equal_cells"] == 9

    # The same verified cells do not become a comparison across years or rows.
    wrong_year = cases["held_wrong_year_claim_2022_against_appendix_2023"]
    assert wrong_year["claim"]["holds"] == [] and wrong_year["evidence_observation"]["holds"]
    assert wrong_year["numeric_service_call"]["outcome"]["status"] == "needs_review"
    assert (
        wrong_year["binding"]["reporting_period"]
        != wrong_year["evidence_observation"]["reporting_period"]
    )
    assert wrong_year["numeric_service_call"]["has_findings"] is False

    wrong_scope = cases["held_wrong_scope_total_row_against_appendix_hev_row"]
    assert wrong_scope["binding"]["metric_raw"] == "총 합계"
    assert wrong_scope["evidence_observation"]["metric_raw"] == "HEV"
    assert wrong_scope["numeric_service_call"]["outcome"]["status"] == "needs_review"
    assert wrong_scope["numeric_service_call"]["has_findings"] is False


def test_equal_reviewed_values_and_qualifiers_never_prove_population_or_note_scope():
    source = b"one source"
    for kind, text, ground in (
        ("coverage", "국내+해외", "population_corroborated_by_shared_grid_identity"),
        ("footnote", "1. 도매 기준", "identical_qualifier_at_the_other_location"),
        ("note", "2025년 발표 기준", "period_qualifier_outside_the_bound_period"),
    ):
        evidence = _review(source, page=106, layout="a" * 64, context=(_context(kind, text),))
        claim = _review(source, page=35, layout="c" * 64, context=(_context(kind, text),))
        dispositions = [
            dict(
                location=location,
                kind=kind,
                raw_text=text,
                ground=ground,
                reviewer="AI",
                rationale="candidate matching only",
            )
            for location in ("evidence", "claim")
        ]
        records, holds = _reconcile(evidence, claim, dispositions)
        assert holds and all(record["state"] == "unreconciled" for record in records)
