"""C2 reporting period: real dates, collection lag explanations, out-of-scope activity."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from proofops.domain.errors import DomainValidationError
from proofops.domain.reconciliation import engine

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "reconciliation"


def policy(**overrides) -> dict:
    value = json.loads((CONTRACT_DIR / "example-policy.json").read_text(encoding="utf-8"))
    value.update(overrides)
    return value


def packet(sus, fin, *, explained=False, search="not_run", **overrides) -> dict:
    value = json.loads((CONTRACT_DIR / "example-input.json").read_text(encoding="utf-8"))
    value["item"] = "C2"
    value["claim"]["trigger_elements"] = ["quantitative_value"]
    for side, normalized in (("sustainability", sus), ("financial", fin)):
        value[side]["kind"] = "period"
        value[side]["unit"] = "period"
        value[side]["normalized"] = normalized
        value[side]["raw"] = normalized.replace("/", " ~ ") if normalized else None
    if explained:
        value["sources"].append(
            {
                "source_id": "expl",
                "document_id": "fs-v1",
                "artifact_sha256": "5" * 64,
                "locator": "fixture-note/timing",
                "quote": "집계 시차로 재무 결산기간과 측정기간이 다릅니다.",
            }
        )
        value["explanation"]["source_id"] = "expl"
    if search == "complete":
        value["search"] = {
            "state": "complete",
            "coverage_policy_id": "synthetic-coverage-only",
            "required_document_ids": ["sr-v1", "fs-v1"],
            "reviewed_source_ids": ["sr-scope", "fs-scope"],
            "failed_document_ids": [],
            "receipt_id": "synthetic-receipt-2",
        }
        value["explanation"]["search_complete"] = True
    value.update(overrides)
    return value


def run(*args, **kwargs) -> dict:
    return engine.evaluate(packet(*args, **kwargs), policy())


YEAR = "2024-01-01/2024-12-31"


# --------------------------------------------------------------------------- #
# positive
# --------------------------------------------------------------------------- #


def test_identical_periods_match():
    result = run(YEAR, YEAR)
    assert (result["execution_state"], result["status"]) == ("completed", "matched")
    assert result["reason_codes"] == ["same_period"]


def test_a_verified_collection_lag_explanation_turns_a_difference_into_matched():
    result = run(YEAR, "2023-10-01/2024-09-30", explained=True, search="complete")
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["period_difference_explained"]
    assert result["explanation_source_id"] == "expl"


def test_a_non_december_fiscal_year_end_is_compared_on_real_dates_not_on_the_year_label():
    value = packet("2024-04-01/2025-03-31", "2024-04-01/2025-03-31")
    value["identity"]["period_start"] = "2024-04-01"
    value["identity"]["period_end"] = "2025-03-31"
    value["identity"]["financial_period_start"] = "2024-04-01"
    value["identity"]["financial_period_end"] = "2025-03-31"
    result = engine.evaluate(value, policy())
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["same_period"]


def test_same_calendar_year_but_different_measurement_windows_is_not_a_match():
    result = run(YEAR, "2024-04-01/2024-12-31", search="complete")
    assert result["status"] == "needs_explanation"
    assert result["reason_codes"] == ["explanation_not_found"]


def test_a_single_day_shift_is_a_real_difference():
    result = run(YEAR, "2024-01-02/2024-12-31", search="complete")
    assert result["status"] == "needs_explanation"


# --------------------------------------------------------------------------- #
# absence requires proven coverage
# --------------------------------------------------------------------------- #


def test_period_difference_without_completed_search_blocks():
    result = run(YEAR, "2023-01-01/2023-12-31", search="not_run")
    assert (result["execution_state"], result["status"]) == ("blocked", None)
    assert result["reason_codes"] == ["search_incomplete"]


def test_period_difference_with_completed_search_and_no_explanation_needs_explanation():
    result = run(YEAR, "2023-01-01/2023-12-31", search="complete")
    assert result["status"] == "needs_explanation"
    assert result["review_required"] is False


# --------------------------------------------------------------------------- #
# out of scope
# --------------------------------------------------------------------------- #


def test_activity_entirely_outside_the_declared_reporting_period_is_not_applicable():
    result = run("2019-01-01/2019-12-31", "2019-01-01/2019-12-31")
    assert (result["execution_state"], result["status"]) == ("completed", "not_applicable")
    assert result["reason_codes"] == ["period_out_of_scope"]


def test_partial_overlap_with_the_reporting_period_is_still_in_scope():
    result = run("2023-07-01/2024-06-30", "2023-07-01/2024-06-30")
    assert result["status"] == "matched"


def test_out_of_scope_is_not_claimed_when_the_reporting_period_itself_is_unknown():
    value = packet("2019-01-01/2019-12-31", "2019-01-01/2019-12-31")
    value["identity"]["period_start"] = None
    value["identity"]["period_end"] = None
    result = engine.evaluate(value, policy())
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["same_period"]


# --------------------------------------------------------------------------- #
# boundary and malformed values
# --------------------------------------------------------------------------- #


def test_missing_period_value_blocks_as_unresolved():
    result = run(None, YEAR)
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["value_unresolved"]


@pytest.mark.parametrize(
    "bad",
    [
        "2024-01-01",
        "2024-01-01/2024-12-31/2025-01-01",
        "2024-01-01 ~ 2024-12-31",
        "2024-13-01/2024-12-31",
        "2024-02-30/2024-12-31",
        "2024/01/01-2024/12/31",
        "2024-12-31/2024-01-01",
        "",
    ],
)
def test_periods_that_do_not_honour_the_declared_format_are_malformed(bad):
    with pytest.raises(DomainValidationError):
        run(bad, YEAR)


def test_a_period_compared_against_a_currency_amount_blocks_on_kind():
    value = packet(YEAR, YEAR)
    value["financial"].update(kind="currency_amount", unit="KRW", normalized="1000", raw="1000")
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]


def test_instant_measures_are_representable_as_a_zero_length_period():
    result = run("2024-12-31/2024-12-31", "2024-12-31/2024-12-31")
    assert result["status"] == "matched"


def test_a_period_balance_against_an_annual_window_is_a_difference_not_an_equality():
    result = run("2024-12-31/2024-12-31", YEAR, search="complete")
    assert result["status"] == "needs_explanation"


def test_c2_is_deterministic_across_repeated_evaluation():
    value, pol = packet(YEAR, "2023-01-01/2023-12-31", search="complete"), policy()
    assert engine.evaluate(value, pol) == engine.evaluate(value, pol)


def test_a_c2_packet_whose_two_sides_agree_on_a_non_period_kind_blocks():
    """Both sides well formed and identical in kind, but not the kind C2 compares."""
    value = packet(YEAR, YEAR)
    for side in ("sustainability", "financial"):
        value[side].update(kind="classification", unit="label", normalized="green", raw="green")
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]
