"""The reviewed grid reaches the existing numeric service, and holds there.

``evaluation/reviewed_table_bridge.py --numeric-input`` normalizes the reviewed
roles over the natively attested graph and really calls
``application.numeric_analysis.analyze_numeric_consistency``. On both actual pages
the service can read **no** observation as a numeric source, because the existing
selected-cell policy promotes the value cell only, so the metric, unit and year
cells of every reviewed value stay uncitable. These tests pin that honest result and
the guards that must keep producing it: a foreign source, a role pinned outside the
value's row, and an unpromoted value cell.

Most tests here are **local integration checks**: they read the operator review and
the customer PDFs under ``outputs/``, which are local git-ignored inputs, and they
run the real on-device rendered reader. They skip when those inputs are absent and
are not portable CI coverage.
"""

import copy
import json
from pathlib import Path

import pytest
from proofops.adapters.local.reviewed_table import (
    numeric_input_report,
    reviewed_numeric_inputs,
)

APP = Path(__file__).resolve().parents[2]
REVIEWS = APP / "tests/fixtures/pipeline/reviewed-table-layout"
FIXTURES = APP / "tests/fixtures/source-documents"

local_inputs = pytest.mark.skipif(
    not all(
        (REVIEWS / f"{name}-candidates.json").exists() and (FIXTURES / f"{name}.pdf").exists()
        for name in ("lotte", "kia")
    ),
    reason="local operator review and customer PDFs are not part of the repository",
)

# The dimension cells of a reviewed value are never promoted by the existing
# policy, so these three holds are expected on every normalized observation.
DIMENSION_HOLDS = {
    "source_unverified:metric_raw",
    "source_unverified:reporting_period",
    "source_unverified:unit_raw",
}


def _review(name):
    return json.loads((REVIEWS / f"{name}-candidates.json").read_text())


def _inputs(name, review=None, pdf=None):
    source = (FIXTURES / f"{pdf or name}.pdf").read_bytes()
    return reviewed_numeric_inputs(review if review is not None else _review(name), source)


def _trimmed(roles):
    """One real Lotte row plus its header row, with an explicit role mapping.

    Only cells the review already pinned are kept; nothing is added or rewritten.
    ``metric`` is the row label because the merged metric cell lives outside the
    kept columns, so it is a fixture choice and asserts nothing about the source's
    metric semantics.
    """
    review = copy.deepcopy(_review("lotte"))
    keep = set(roles.values()) | {"r0c1", "r0c2", "r0c3", "r2c1", "r2c2", "r2c3"}
    review["cells"] = {key: cell for key, cell in review["cells"].items() if key in keep}
    pinned = {i for cell in review["cells"].values() for i in cell["word_indices"]}
    review["words"] = [word for word in review["words"] if word["index"] in pinned]
    review["candidates"] = [{"source_cells": dict(roles)}]
    return review


@local_inputs
def test_actual_lotte_grid_reaches_the_numeric_service_and_holds_every_dimension():
    inputs = _inputs("lotte")
    report = numeric_input_report(inputs, _review("lotte"))

    # Every reviewed candidate normalized over the promoted graph.
    assert report["observation_count"] == 30
    assert all(c["normalization_status"] == "normalized" for c in report["candidates"])
    # The existing verifier promoted exactly the 14 R02i value cells.
    assert report["native_receipt"]["promoted_cell_count"] == 14
    assert (
        report["native_receipt"]["policy_sha256"]
        == "61c079da238fdc0e22f0cfbc25c69c3599c73d0867bf81ac5ed8762ff40d6828"
    )
    # None of them is a numeric source: the metric/unit/year cells stay uncitable.
    assert report["usable_numeric_source_count"] == 0
    assert DIMENSION_HOLDS <= set(report["blocking_holds"])
    assert all(report["blocking_holds"][hold] == 30 for hold in DIMENSION_HOLDS)
    assert all(item.quality == "unverified" for item in inputs.observations)

    # The service really ran, with no claim and no binding, and found nothing.
    call = report["numeric_service_call"]
    assert call["invocation"] == "application.numeric_analysis.analyze_numeric_consistency"
    assert (call["claims_supplied"], call["bindings_supplied"]) == (0, 0)
    assert call["observations_supplied"] == 30
    assert call["outcome_count"] == 0
    assert call["has_findings"] is False
    # No admission, no grade, and the real claim bridge is reported unresolved.
    assert report["eligible_for_admission"] is False
    assert report["semantic_verification"] == "not_run"
    assert report["claim_bridge"] == "unresolved"
    assert len(report["blockers"]) == 2


@local_inputs
def test_actual_kia_normalizable_rows_and_natively_attested_cells_are_disjoint():
    """Kia holds for a different reason: the two sets do not overlap at all."""
    inputs = _inputs("kia")
    report = numeric_input_report(inputs, _review("kia"))

    held = [c for c in report["candidates"] if c["normalization_status"] == "held"]
    assert len(held) == 18
    assert {c["hold_reason"] for c in held} == {"binding crosses value row/column"}
    assert {c["numeric_usability"] for c in held} == {"not_normalized"}
    assert report["observation_count"] == 12
    assert report["native_receipt"]["promoted_cell_count"] == 12
    assert report["usable_numeric_source_count"] == 0

    # Every normalized parent row's own value cell is outside the promoted set, so
    # the 12 attested child cells and the 12 usable rows share nothing.
    value_cells = {source for item in inputs.observations for source, _, _ in item.parent_relations}
    assert len(value_cells) == 12
    assert not value_cells & inputs.promoted_source_ids
    assert report["blocking_holds"]["source_unverified:value_raw"] == 12
    assert report["numeric_service_call"]["outcome_count"] == 0


@local_inputs
def test_unbound_reviewed_row_label_and_note_are_preserved_and_hold_the_observation():
    """A reviewed facility and footnote the 4 roles drop must not vanish silently.

    Lotte candidate 0 pins ``row_labels=[r1c1]`` (여수공장(기초)) and ``notes=[r1c6]``.
    The four-role normalization binds neither, so both are kept per candidate and each
    is its own hold: even if all four literal cells verified, this value would stay
    ``unverified`` rather than become an accepted facility-level number.
    """
    inputs = _inputs("lotte")
    report = numeric_input_report(inputs, _review("lotte"))
    candidate = report["candidates"][0]

    assert candidate["unbound_reviewed_context"] == {"row_labels": ["r1c1"], "notes": ["r1c6"]}
    assert "reviewed_context_unbound:row_labels" in candidate["numeric_holds"]
    assert "reviewed_context_unbound:notes" in candidate["numeric_holds"]
    # The holds survive removing every source-verification hold, so they alone keep
    # the observation out of the numeric service.
    remaining = [
        hold
        for hold in candidate["numeric_holds"]
        if not hold.startswith(
            ("source_unverified:", "source_hold:", "cell_not_natively_promoted:")
        )
    ]
    assert remaining == [
        "reviewed_context_unbound:notes",
        "reviewed_context_unbound:row_labels",
    ]
    assert candidate["quality"] == "unverified"
    # The domain's own note diagnostics stay empty: nothing invents note ownership.
    assert candidate["source_holds"]["note_source_ids"] == []

    # Kia candidate 0 pins neither, and an empty list is not a hold.
    kia = numeric_input_report(_inputs("kia"), _review("kia"))["candidates"][0]
    assert kia["unbound_reviewed_context"] == {"row_labels": [], "notes": []}
    assert not [h for h in kia["numeric_holds"] if h.startswith("reviewed_context_unbound:")]


@local_inputs
def test_a_foreign_source_is_refused_before_any_numeric_input_exists():
    with pytest.raises(ValueError, match="reviewed layout source mismatch"):
        _inputs("lotte", pdf="kia")


@local_inputs
def test_a_role_pinned_outside_the_value_row_holds_instead_of_being_converted():
    """A unit taken from another row is refused, not silently applied to the value."""
    roles = {"metric": "r2c1", "unit": "r5c2", "year": "r0c3", "value": "r2c3"}
    report = numeric_input_report(_inputs("lotte", review=_trimmed(roles)), _trimmed(roles))

    (candidate,) = report["candidates"]
    assert candidate["normalization_status"] == "held"
    assert candidate["hold_reason"] == "binding crosses value row/column"
    assert candidate["numeric_usability"] == "not_normalized"
    assert report["observation_count"] == 0
    assert report["usable_numeric_source_count"] == 0
    assert report["numeric_service_call"]["observations_supplied"] == 0


@local_inputs
def test_a_year_header_from_another_column_is_refused_not_converted():
    """The 2024 header cannot be bound to a 2025 value: the role is refused."""
    roles = {"metric": "r2c1", "unit": "r2c2", "year": "r0c4", "value": "r2c3"}
    review = _trimmed(roles)
    inputs = _inputs("lotte", review=review)
    report = numeric_input_report(inputs, review)

    # No observation is produced, so no year is silently rewritten onto the value
    # and no unusable value is smuggled in with a neighbouring column's period.
    assert inputs.observations == ()
    (candidate,) = report["candidates"]
    assert candidate["role_cells"]["year"] == "r0c4"
    assert candidate["normalization_status"] == "held"
    assert candidate["hold_reason"] == "binding crosses value row/column"
    assert report["observation_count"] == 0
    assert report["usable_numeric_source_count"] == 0


@local_inputs
def test_an_unpromoted_value_cell_never_becomes_a_numeric_source():
    """Lotte r1c3 was held by the native verifier; source quality follows that."""
    inputs = _inputs("lotte")
    report = numeric_input_report(inputs, _review("lotte"))
    by_cell = {c["value_cell"]: c for c in report["candidates"]}

    held, promoted = by_cell["r1c3"], by_cell["r2c3"]
    assert held["value_raw"] == "1,840,806"
    assert held["refs"]["value_raw"]["natively_promoted"] is False
    assert held["refs"]["value_raw"]["verification_state"] == "rejected"
    assert "source_unverified:value_raw" in held["numeric_holds"]
    assert "source_hold:observation_source_unverified" in held["numeric_holds"]
    # The neighbouring promoted cell shows the difference is the receipt, not the row.
    assert promoted["refs"]["value_raw"]["natively_promoted"] is True
    assert promoted["refs"]["value_raw"]["verification_state"] == "verified"
    assert "source_unverified:value_raw" not in promoted["numeric_holds"]
    # Neither is usable, and usability is exactly "no holds" for every candidate.
    for candidate in report["candidates"]:
        if candidate["normalization_status"] != "normalized":
            continue
        assert (candidate["numeric_usability"] == "usable_numeric_source") == (
            candidate["numeric_holds"] == []
        )
        assert (candidate["quality"] == "verified") == (candidate["numeric_holds"] == [])


@local_inputs
def test_context_literals_reach_observation_refs_without_dropping_reviewed_notes():
    roles = {"metric": "r2c1", "unit": "r2c2", "year": "r0c3", "value": "r2c3"}
    review = _trimmed(roles)
    # Contrast fixture: attach declared note/facility references to existing cells.
    # These declarations are not source facts and must remain unresolved.
    review["candidates"][0]["source_cells"].update(notes=["r2c1"], row_labels=["r2c1"])
    source = (FIXTURES / "lotte.pdf").read_bytes()
    inputs = reviewed_numeric_inputs(review, source, verify_context=True)
    report = numeric_input_report(inputs, review)
    candidate = report["candidates"][0]
    assert inputs.context_receipts
    assert report["context_cell_verification"]["verified"] == 3
    assert all(
        candidate["refs"][role]["verification_state"] == "verified"
        for role in ("metric_raw", "unit_raw", "reporting_period")
    )
    assert not DIMENSION_HOLDS.intersection(candidate["numeric_holds"])
    assert "reviewed_context_unbound:notes" in candidate["numeric_holds"]
    assert "reviewed_context_unbound:row_labels" in candidate["numeric_holds"]
    assert candidate["quality"] == "unverified"
    assert report["numeric_service_call"]["outcome_count"] == 0

    # Malformed nonempty context cannot be silently filtered into an empty list.
    review["candidates"][0]["source_cells"]["notes"] = [123]
    with pytest.raises(ValueError, match="reviewed context must reference known cells"):
        reviewed_numeric_inputs(review, source, verify_context=True)

    review["candidates"][0]["source_cells"]["notes"] = []
    review["candidates"][0]["source_cells"]["scope"] = "r2c1"
    with pytest.raises(ValueError, match="unsupported reviewed cell role"):
        reviewed_numeric_inputs(review, source, verify_context=True)
