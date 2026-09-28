"""C1 organizational boundary: exact verified entity/facility set comparison."""

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


def packet(sus, fin, *, kind="entity_set", explained=False, search="not_run", **overrides) -> dict:
    value = json.loads((CONTRACT_DIR / "example-input.json").read_text(encoding="utf-8"))
    value["item"] = "C1"
    value["sustainability"]["normalized"] = sus
    value["financial"]["normalized"] = fin
    value["sustainability"]["kind"] = kind
    value["financial"]["kind"] = kind
    if explained:
        value["sources"].append(
            {
                "source_id": "expl",
                "document_id": "fs-v1",
                "artifact_sha256": "4" * 64,
                "locator": "fixture-note/difference",
                "quote": "해외 종속기업은 연결 범위에서 제외되었습니다.",
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
            "receipt_id": "synthetic-receipt-1",
        }
        value["explanation"]["search_complete"] = True
    elif search == "incomplete":
        value["search"]["state"] = "incomplete"
    value.update(overrides)
    return value


def run(*args, **kwargs) -> dict:
    return engine.evaluate(packet(*args, **kwargs), policy())


def setify(value: list[str]) -> str:
    return json.dumps(value)


# --------------------------------------------------------------------------- #
# positive
# --------------------------------------------------------------------------- #


def test_identical_entity_sets_match():
    result = run(setify(["A", "B"]), setify(["A", "B"]))
    assert (result["execution_state"], result["status"]) == ("completed", "matched")
    assert result["reason_codes"] == ["same_verified_entity_set"]
    assert result["explanation_source_id"] is None


def test_set_equality_ignores_member_order():
    result = run(setify(["B", "A"]), setify(["A", "B"]))
    assert result["status"] == "matched"


def test_identical_facility_sets_match_on_the_same_rule():
    result = run(setify(["F1", "F2"]), setify(["F2", "F1"]), kind="facility_set")
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["same_verified_entity_set"]


def test_matched_sets_do_not_require_a_completed_search():
    """Nothing is missing when the sets agree, so search coverage is irrelevant."""
    result = run(setify(["A", "B"]), setify(["A", "B"]), search="not_run")
    assert result["status"] == "matched"


def test_a_verified_difference_explanation_produces_matched():
    result = run(setify(["A", "B"]), setify(["A", "C"]), explained=True, search="complete")
    assert (result["execution_state"], result["status"]) == ("completed", "matched")
    assert result["reason_codes"] == ["difference_explained"]
    assert result["explanation_source_id"] == "expl"
    assert "expl" in result["source_ids"]


def test_an_explanation_is_accepted_even_when_search_is_not_complete():
    """A found explanation already answers the difference; coverage only matters for absence."""
    result = run(setify(["A", "B"]), setify(["A", "C"]), explained=True, search="not_run")
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["difference_explained"]


# --------------------------------------------------------------------------- #
# negative: counting must never substitute for set identity
# --------------------------------------------------------------------------- #


def test_same_cardinality_with_different_members_is_never_matched():
    result = run(setify(["A", "B"]), setify(["A", "C"]), search="complete")
    assert result["status"] == "needs_explanation"
    assert result["reason_codes"] == ["explanation_not_found"]


def test_subset_relationship_is_a_difference_not_a_match():
    result = run(setify(["A", "B", "C"]), setify(["A", "B"]), search="complete")
    assert result["status"] == "needs_explanation"


def test_case_differences_in_entity_ids_are_a_difference_not_a_match():
    result = run(setify(["a"]), setify(["A"]), search="complete")
    assert result["status"] == "needs_explanation"


def test_whitespace_differences_in_entity_ids_are_a_difference_not_a_match():
    result = run(setify(["A "]), setify(["A"]), search="complete")
    assert result["status"] == "needs_explanation"


def test_duplicate_members_do_not_silently_change_the_compared_set():
    with pytest.raises(DomainValidationError):
        run(setify(["A", "A", "B"]), setify(["A", "B"]))


def test_allowed_difference_types_are_a_search_taxonomy_not_an_automatic_pass():
    unexplained = packet(setify(["A", "B"]), setify(["A", "C"]), search="complete")
    permissive = policy(
        allowed_difference_types=[
            "operational_control_vs_control",
            "overseas_subsidiary_excluded",
            "equity_method_excluded",
            "acquisition_disposal_proration",
        ]
    )
    result = engine.evaluate(unexplained, permissive)
    assert result["status"] == "needs_explanation"
    assert engine.evaluate(unexplained, policy())["status"] == "needs_explanation"


# --------------------------------------------------------------------------- #
# absence requires proven coverage
# --------------------------------------------------------------------------- #


def test_difference_without_completed_search_blocks_instead_of_alleging_absence():
    result = run(setify(["A", "B"]), setify(["A", "C"]), search="not_run")
    assert (result["execution_state"], result["status"]) == ("blocked", None)
    assert result["reason_codes"] == ["search_incomplete"]
    assert result["review_required"] is True


def test_explicitly_incomplete_search_also_blocks():
    result = run(setify(["A", "B"]), setify(["A", "C"]), search="incomplete")
    assert result["reason_codes"] == ["search_incomplete"]


def test_a_complete_search_with_a_failed_document_is_not_a_complete_search():
    value = packet(setify(["A", "B"]), setify(["A", "C"]), search="complete")
    value["search"]["failed_document_ids"] = ["fs-v1"]
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, policy())


def test_a_complete_search_without_a_receipt_is_malformed():
    value = packet(setify(["A", "B"]), setify(["A", "C"]), search="complete")
    value["search"]["receipt_id"] = None
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, policy())


def test_a_complete_search_without_a_coverage_policy_is_malformed():
    value = packet(setify(["A", "B"]), setify(["A", "C"]), search="complete")
    value["search"]["coverage_policy_id"] = None
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, policy())


# --------------------------------------------------------------------------- #
# boundary and malformed values
# --------------------------------------------------------------------------- #


def test_an_empty_set_is_never_normalised_into_a_match():
    result = run(setify([]), setify([]))
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["entity_set_empty"]


def test_one_empty_side_blocks_rather_than_reporting_a_difference():
    result = run(setify([]), setify(["A"]), search="complete")
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["entity_set_empty"]


def test_missing_normalized_value_blocks_as_unresolved():
    value = packet(None, setify(["A"]))
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["value_unresolved"]


@pytest.mark.parametrize(
    "bad",
    ['{"a": 1}', "[1, 2]", "A,B", '["A"', "null", '[["A"]]', '[""]', '["A", null]'],
)
def test_entity_sets_that_do_not_honour_the_declared_format_are_malformed(bad):
    with pytest.raises(DomainValidationError):
        run(bad, setify(["A"]))


def test_entity_set_versus_facility_set_across_the_two_sides_blocks_on_kind():
    value = packet(setify(["A"]), setify(["A"]))
    value["financial"]["kind"] = "facility_set"
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]


def test_a_currency_kind_cannot_be_compared_as_an_entity_boundary():
    value = packet("1000", "1000", kind="currency_amount")
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]


def test_c1_result_reports_both_compared_sources():
    result = run(setify(["A", "B"]), setify(["A", "B"]))
    assert result["source_ids"] == ["sr-scope", "fs-scope"]


def test_c1_is_deterministic_across_repeated_evaluation():
    value, pol = packet(setify(["A", "B"]), setify(["A", "C"]), search="complete"), policy()
    assert engine.evaluate(value, pol) == engine.evaluate(value, pol)
