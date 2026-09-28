"""C3 investment commitment: Decimal amounts, approved accounts, blocked unresolved policy."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from proofops.domain.errors import DomainValidationError
from proofops.domain.reconciliation import engine

CONTRACT_DIR = Path(__file__).resolve().parents[2] / "contracts" / "reconciliation"


def draft_policy(**overrides) -> dict:
    """The shipped example policy: threshold and account mapping deliberately unapproved."""
    value = json.loads((CONTRACT_DIR / "example-policy.json").read_text(encoding="utf-8"))
    value.update(overrides)
    return value


def approved_policy(**overrides) -> dict:
    approved = {
        "c3_threshold": "5.0",
        "c3_account_mapping_approved": True,
        "allowed_capex_account_ids": ["synthetic-PPE"],
    }
    approved.update(overrides)
    return draft_policy(**approved)


def packet(
    commitment,
    capex,
    *,
    currency="KRW",
    accounts=("synthetic-PPE",),
    commitment_source=None,
    funding_source=None,
    explained=False,
    search="not_run",
    **overrides,
) -> dict:
    value = json.loads((CONTRACT_DIR / "example-input.json").read_text(encoding="utf-8"))
    value["item"] = "C3"
    value["claim"]["track"] = "goal"
    value["claim"]["trigger_elements"] = ["currency_amount"]
    for side, normalized in (("sustainability", commitment), ("financial", capex)):
        value[side]["kind"] = "currency_amount"
        value[side]["unit"] = currency
        value[side]["normalized"] = normalized
        value[side]["raw"] = normalized
    extra_sources = []
    if commitment_source:
        extra_sources.append(("commit", "fixture-note/commitment", "약정 주석"))
    if funding_source:
        extra_sources.append(("funding", "fixture-note/funding", "조달 계획"))
    if explained:
        extra_sources.append(("expl", "fixture-note/difference", "투자 집행 시기 설명"))
    for index, (source_id, locator, quote) in enumerate(extra_sources):
        value["sources"].append(
            {
                "source_id": source_id,
                "document_id": "fs-v1",
                "artifact_sha256": str(index + 6) * 64,
                "locator": locator,
                "quote": quote,
            }
        )
    if explained:
        value["explanation"]["source_id"] = "expl"
    value["c3_context"] = {
        "currency": currency,
        "target_period_start": "2025-01-01",
        "target_period_end": "2030-12-31",
        "capex_period_start": "2024-01-01",
        "capex_period_end": "2024-12-31",
        "capex_account_ids": list(accounts),
        "commitment_source_id": "commit" if commitment_source else None,
        "funding_plan_source_id": "funding" if funding_source else None,
    }
    if search == "complete":
        value["search"] = {
            "state": "complete",
            "coverage_policy_id": "synthetic-coverage-only",
            "required_document_ids": ["sr-v1", "fs-v1"],
            "reviewed_source_ids": ["sr-scope", "fs-scope"],
            "failed_document_ids": [],
            "receipt_id": "synthetic-receipt-3",
        }
        value["explanation"]["search_complete"] = True
    value.update(overrides)
    return value


def run(*args, policy=None, **kwargs) -> dict:
    return engine.evaluate(packet(*args, **kwargs), policy or approved_policy())


# --------------------------------------------------------------------------- #
# unresolved policy always blocks
# --------------------------------------------------------------------------- #


def test_the_shipped_draft_policy_blocks_because_threshold_and_mapping_are_unapproved():
    result = run("10000000000", "1000000000", policy=draft_policy())
    assert (result["execution_state"], result["status"]) == ("blocked", None)
    assert result["reason_codes"] == ["c3_policy_unapproved"]
    assert result["review_required"] is True


def test_a_null_threshold_alone_blocks_even_with_approved_account_mapping():
    result = run(
        "1000",
        "1000",
        policy=draft_policy(
            c3_threshold=None,
            c3_account_mapping_approved=True,
            allowed_capex_account_ids=["synthetic-PPE"],
        ),
    )
    assert result["reason_codes"] == ["c3_policy_unapproved"]


def test_an_unapproved_account_mapping_alone_blocks_even_with_a_threshold():
    result = run(
        "1000",
        "1000",
        policy=draft_policy(
            c3_threshold="5.0",
            c3_account_mapping_approved=False,
            allowed_capex_account_ids=["synthetic-PPE"],
        ),
    )
    assert result["reason_codes"] == ["c3_policy_unapproved"]


def test_a_capex_account_outside_the_approved_allowlist_blocks():
    result = run("1000", "1000", accounts=("cash-flow-investing-total",))
    assert result["reason_codes"] == ["c3_policy_unapproved"]


def test_an_empty_capex_account_list_blocks_rather_than_defaulting_to_everything():
    result = run("1000", "1000", accounts=())
    assert result["reason_codes"] == ["c3_policy_unapproved"]


def test_the_documented_five_times_example_is_not_a_built_in_default():
    """Without an explicitly approved threshold the engine must refuse, not assume 5.0."""
    result = run(
        "10000000000",
        "1000000000",
        policy=draft_policy(
            c3_account_mapping_approved=True, allowed_capex_account_ids=["synthetic-PPE"]
        ),
    )
    assert result["execution_state"] == "blocked"


# --------------------------------------------------------------------------- #
# trigger scope
# --------------------------------------------------------------------------- #


def test_a_non_goal_track_claim_is_not_applicable_for_c3():
    value = packet("1000", "1000")
    value["claim"]["track"] = "performance"
    result = engine.evaluate(value, approved_policy())
    assert (result["execution_state"], result["status"]) == ("completed", "not_applicable")
    assert result["reason_codes"] == ["c3_trigger_absent"]


def test_a_goal_claim_without_a_currency_amount_trigger_is_not_applicable():
    value = packet("1000", "1000")
    value["claim"]["trigger_elements"] = ["quantitative_value"]
    result = engine.evaluate(value, approved_policy())
    assert result["status"] == "not_applicable"
    assert result["reason_codes"] == ["c3_trigger_absent"]


def test_trigger_scope_is_checked_before_the_policy_gate():
    value = packet("1000", "1000")
    value["claim"]["track"] = "management"
    result = engine.evaluate(value, draft_policy())
    assert result["reason_codes"] == ["c3_trigger_absent"]


# --------------------------------------------------------------------------- #
# amounts
# --------------------------------------------------------------------------- #


def test_commitment_within_the_approved_threshold_matches():
    result = run("5000000000", "1000000000")
    assert (result["execution_state"], result["status"]) == ("completed", "matched")
    assert result["reason_codes"] == ["capex_within_threshold"]


def test_the_threshold_boundary_is_inclusive_and_exact():
    inclusive = run("5000000000", "1000000000")
    assert inclusive["status"] == "matched"
    just_over = run("5000000001", "1000000000", search="complete")
    assert just_over["status"] == "needs_explanation"


def test_large_amounts_are_compared_without_floating_point_error():
    """5.0 * 20000000000000000001 must not be rounded into a match."""
    result = run("100000000000000000006", "20000000000000000001", search="complete")
    assert result["status"] == "needs_explanation"


def test_a_fractional_threshold_is_honoured_exactly():
    result = run("2500000000", "1000000000", policy=approved_policy(c3_threshold="2.5"))
    assert result["status"] == "matched"
    over = run(
        "2500000001", "1000000000", policy=approved_policy(c3_threshold="2.5"), search="complete"
    )
    assert over["status"] == "needs_explanation"


@pytest.mark.parametrize("capex", ["0", "-1", "0.00"])
def test_non_positive_capex_blocks_and_never_produces_an_infinite_ratio(capex):
    result = run("1000", capex)
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["c3_capex_not_positive"]


@pytest.mark.parametrize("commitment", ["0", "-5"])
def test_non_positive_commitment_blocks(commitment):
    result = run(commitment, "1000")
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["c3_commitment_not_positive"]


def test_missing_amount_blocks_as_unresolved():
    result = run(None, "1000")
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["value_unresolved"]


@pytest.mark.parametrize(
    "bad", ["1,000", "10억", "1e9", "NaN", "Infinity", "-Infinity", "", " 1000", "0x10"]
)
def test_amounts_that_are_not_plain_decimal_strings_are_malformed(bad):
    with pytest.raises(DomainValidationError):
        run(bad, "1000")


def test_mixed_currencies_block():
    value = packet("1000", "1000")
    value["financial"]["unit"] = "USD"
    result = engine.evaluate(value, approved_policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["c3_currency_mismatch"]


def test_a_context_currency_that_disagrees_with_the_facts_blocks():
    value = packet("1000", "1000")
    value["c3_context"]["currency"] = "USD"
    result = engine.evaluate(value, approved_policy())
    assert result["reason_codes"] == ["c3_currency_mismatch"]


# --------------------------------------------------------------------------- #
# periods and explanations
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "field",
    ["target_period_start", "target_period_end", "capex_period_start", "capex_period_end"],
)
def test_an_unresolved_context_period_blocks(field):
    value = packet("1000", "1000")
    value["c3_context"][field] = None
    result = engine.evaluate(value, approved_policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["c3_period_unresolved"]


def test_a_reversed_context_period_is_malformed():
    value = packet("1000", "1000")
    value["c3_context"]["capex_period_end"] = "2023-01-01"
    with pytest.raises(DomainValidationError):
        engine.evaluate(value, approved_policy())


def test_a_disclosed_commitment_note_matches_even_above_the_threshold():
    result = run("10000000000", "1000000000", commitment_source=True)
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["commitment_disclosed"]
    assert "commit" in result["source_ids"]


def test_a_verified_difference_explanation_matches_above_the_threshold():
    result = run("10000000000", "1000000000", explained=True, search="complete")
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["difference_explained"]
    assert result["explanation_source_id"] == "expl"


def test_absent_commitment_alone_is_not_immediately_a_finding():
    result = run("10000000000", "1000000000", search="not_run")
    assert (result["execution_state"], result["status"]) == ("blocked", None)
    assert result["reason_codes"] == ["search_incomplete"]


def test_absent_commitment_with_proven_coverage_needs_explanation():
    result = run("10000000000", "1000000000", search="complete")
    assert result["status"] == "needs_explanation"
    assert result["reason_codes"] == ["explanation_not_found"]


def test_a_funding_plan_source_is_reported_among_the_used_sources():
    result = run("5000000000", "1000000000", funding_source=True)
    assert "funding" in result["source_ids"]


def test_c3_is_deterministic_across_repeated_evaluation():
    value, pol = packet("10000000000", "1000000000", search="complete"), approved_policy()
    assert engine.evaluate(value, pol) == engine.evaluate(value, pol)


def test_a_c3_packet_whose_two_sides_agree_on_a_non_currency_kind_blocks():
    value = packet("1000", "1000")
    for side in ("sustainability", "financial"):
        value[side].update(
            kind="period", unit="KRW", normalized="2024-01-01/2024-12-31", raw="2024"
        )
    result = engine.evaluate(value, approved_policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]


def test_an_empty_approved_capex_allowlist_blocks_even_with_a_threshold_and_mapping():
    result = run(
        "1000",
        "1000",
        policy=draft_policy(
            c3_threshold="5.0",
            c3_account_mapping_approved=True,
            allowed_capex_account_ids=[],
        ),
    )
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["c3_policy_unapproved"]


def test_the_threshold_product_is_exact_beyond_the_default_decimal_precision():
    """29-digit KRW figures must not be rounded into the wrong side of the threshold."""
    capex = "12345678901234567890123456789"
    commitment = "61728394506172839450617283945"  # exactly 5 x capex
    result = run(commitment, capex, policy=approved_policy(c3_threshold="5"))
    assert result["status"] == "matched"
    assert result["reason_codes"] == ["capex_within_threshold"]

    over = run(commitment + "0", capex, policy=approved_policy(c3_threshold="5"), search="complete")
    assert over["status"] == "needs_explanation"


def test_a_multi_year_target_is_never_silently_annualised_against_annual_capex():
    """The approved threshold expresses the multi-year relationship; no division by span."""
    six_year_target = packet(
        "6000000000",
        "1000000000",
        search="complete",
        **{
            "c3_context": {
                "currency": "KRW",
                "target_period_start": "2025-01-01",
                "target_period_end": "2030-12-31",
                "capex_period_start": "2024-01-01",
                "capex_period_end": "2024-12-31",
                "capex_account_ids": ["synthetic-PPE"],
                "commitment_source_id": None,
                "funding_plan_source_id": None,
            }
        },
    )
    result = engine.evaluate(six_year_target, approved_policy(c3_threshold="5.0"))
    # 6e9 vs 5.0 x 1e9 exceeds the threshold. An annualised 6e9/6 = 1e9 would have matched.
    assert result["status"] == "needs_explanation"
    assert result["reason_codes"] == ["explanation_not_found"]


def test_target_and_capex_periods_are_both_validated_for_ordering():
    reversed_target = packet("1000", "1000")
    reversed_target["c3_context"]["target_period_end"] = "2024-01-01"
    with pytest.raises(DomainValidationError):
        engine.evaluate(reversed_target, approved_policy())
