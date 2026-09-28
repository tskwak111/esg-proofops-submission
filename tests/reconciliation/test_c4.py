"""C4 classification basis: presence of a disclosed definition and calculation basis."""

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


def packet(
    *, definition=(), calculation=(), search="not_run", explained=False, **overrides
) -> dict:
    value = json.loads((CONTRACT_DIR / "example-input.json").read_text(encoding="utf-8"))
    value["item"] = "C4"
    value["claim"]["trigger_elements"] = ["revenue_share"]
    value["sustainability"].update(
        {
            "kind": "classification",
            "unit": "percent",
            "normalized": "green-products",
            "raw": "친환경 제품 32%",
        }
    )
    value["financial"].update(
        {
            "kind": "classification",
            "unit": "KRW",
            "normalized": "product-segment",
            "raw": "제품 부문 매출",
        }
    )
    declared = {
        "def": "fixture-note/definition",
        "calc": "fixture-note/calculation",
        "expl": "fixture-note/other",
    }
    for index, (source_id, locator) in enumerate(declared.items()):
        if source_id in set(definition) | set(calculation) or (source_id == "expl" and explained):
            value["sources"].append(
                {
                    "source_id": source_id,
                    "document_id": "fs-v1",
                    "artifact_sha256": str(index + 7) * 64,
                    "locator": locator,
                    "quote": f"{source_id} 기준 설명",
                }
            )
    if explained:
        value["explanation"]["source_id"] = "expl"
    value["c4_context"] = {
        "classification_name": "친환경 제품",
        "definition_source_ids": list(definition),
        "calculation_source_ids": list(calculation),
    }
    if search == "complete":
        value["search"] = {
            "state": "complete",
            "coverage_policy_id": "synthetic-coverage-only",
            "required_document_ids": ["sr-v1", "fs-v1"],
            "reviewed_source_ids": ["sr-scope", "fs-scope"],
            "failed_document_ids": [],
            "receipt_id": "synthetic-receipt-4",
        }
        value["explanation"]["search_complete"] = True
    value.update(overrides)
    return value


def run(**kwargs) -> dict:
    return engine.evaluate(packet(**kwargs), policy())


# --------------------------------------------------------------------------- #
# positive
# --------------------------------------------------------------------------- #


def test_both_required_explanations_present_matches():
    result = run(definition=["def"], calculation=["calc"])
    assert (result["execution_state"], result["status"]) == ("completed", "matched")
    assert result["reason_codes"] == ["classification_basis_present"]


def test_matched_result_reports_the_definition_and_calculation_sources():
    result = run(definition=["def"], calculation=["calc"])
    assert result["source_ids"] == ["sr-scope", "fs-scope", "def", "calc"]


def test_presence_is_decided_without_completed_search_when_both_bases_exist():
    result = run(definition=["def"], calculation=["calc"], search="not_run")
    assert result["status"] == "matched"


def test_a_single_source_may_carry_both_required_explanations():
    result = run(definition=["def"], calculation=["def"])
    assert result["status"] == "matched"
    assert result["source_ids"] == ["sr-scope", "fs-scope", "def"]


# --------------------------------------------------------------------------- #
# missing explanations
# --------------------------------------------------------------------------- #


def test_a_missing_definition_with_proven_coverage_needs_explanation():
    result = run(definition=[], calculation=["calc"], search="complete")
    assert (result["execution_state"], result["status"]) == ("completed", "needs_explanation")
    assert result["reason_codes"] == ["definition_not_found"]


def test_a_missing_calculation_basis_with_proven_coverage_needs_explanation():
    result = run(definition=["def"], calculation=[], search="complete")
    assert result["status"] == "needs_explanation"
    assert result["reason_codes"] == ["calculation_basis_not_found"]


def test_both_missing_explanations_are_reported_separately_and_in_a_stable_order():
    result = run(definition=[], calculation=[], search="complete")
    assert result["status"] == "needs_explanation"
    assert result["reason_codes"] == ["definition_not_found", "calculation_basis_not_found"]


def test_missing_explanations_without_completed_search_block_instead_of_alleging_absence():
    result = run(definition=[], calculation=[], search="not_run")
    assert (result["execution_state"], result["status"]) == ("blocked", None)
    assert result["reason_codes"] == ["search_incomplete"]
    assert result["review_required"] is True


def test_partial_bases_without_completed_search_also_block():
    result = run(definition=["def"], calculation=[], search="not_run")
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["search_incomplete"]


def test_an_unrelated_explanation_source_does_not_substitute_for_the_required_bases():
    result = run(definition=[], calculation=[], explained=True, search="complete")
    assert result["status"] == "needs_explanation"
    assert result["reason_codes"] == ["definition_not_found", "calculation_basis_not_found"]


# --------------------------------------------------------------------------- #
# guards
# --------------------------------------------------------------------------- #


def test_required_explanation_set_is_taken_from_the_approved_policy():
    value = packet(definition=["def"], calculation=["calc"])
    weakened = policy()
    weakened["c4_required_explanations"] = ["definition"]
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, weakened)


def test_a_dangling_definition_reference_is_malformed():
    value = packet(definition=["def"], calculation=["calc"])
    value["c4_context"]["definition_source_ids"] = ["ghost"]
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, policy())


def test_duplicate_definition_references_are_malformed():
    value = packet(definition=["def"], calculation=["calc"])
    value["c4_context"]["definition_source_ids"] = ["def", "def"]
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, policy())


def test_a_missing_classification_name_is_malformed():
    value = packet(definition=["def"], calculation=["calc"])
    value["c4_context"]["classification_name"] = ""
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, policy())


def test_a_non_classification_kind_blocks_on_kind():
    value = packet(definition=["def"], calculation=["calc"])
    value["financial"].update(kind="currency_amount", unit="KRW", normalized="1000", raw="1000")
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]


def test_a_missing_classification_value_blocks_as_unresolved():
    value = packet(definition=["def"], calculation=["calc"])
    value["sustainability"]["normalized"] = None
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["value_unresolved"]


def test_c4_does_not_compare_the_two_classification_values_for_equality():
    """The item checks that a basis is disclosed, not that two labels are identical."""
    result = run(definition=["def"], calculation=["calc"])
    assert result["status"] == "matched"
    assert result["sustainability_value"] != result["financial_value"]


def test_c4_is_deterministic_across_repeated_evaluation():
    value, pol = packet(definition=[], calculation=["calc"], search="complete"), policy()
    assert engine.evaluate(value, pol) == engine.evaluate(value, pol)


def test_a_c4_packet_whose_two_sides_agree_on_a_non_classification_kind_blocks():
    value = packet(definition=["def"], calculation=["calc"])
    for side in ("sustainability", "financial"):
        value[side].update(
            kind="period", unit="period", normalized="2024-01-01/2024-12-31", raw="2024"
        )
    result = engine.evaluate(value, policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]
