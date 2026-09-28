"""Engine-level contract tests for the pure Developer B reconciliation engine.

Fixtures under developer-b-package-v3 are treated as published contract examples to
compare against, never as a lookup table the engine may consult at runtime.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from proofops.domain.errors import DomainValidationError
from proofops.domain.reconciliation import engine

REPO_ROOT = Path(__file__).resolve().parents[2]
# Master copied the eight handoff examples unchanged into contracts/reconciliation so the
# regression does not depend on the untracked developer-b-package-v3 folder.
CONTRACT_DIR = REPO_ROOT / "contracts" / "reconciliation"
EXAMPLES_DIR = CONTRACT_DIR / "examples"

OUTPUT_KEYS = {
    "schema_version",
    "claim_id",
    "item",
    "execution_state",
    "status",
    "review_required",
    "reason_codes",
    "source_ids",
    "explanation_source_id",
    "sustainability_value",
    "financial_value",
    "packet_sha256",
    "policy_sha256",
    "synthetic",
    "engine_version",
}


def load_case(name: str) -> dict:
    return json.loads((EXAMPLES_DIR / f"{name}.json").read_text(encoding="utf-8"))


def all_cases() -> list[tuple[str, dict]]:
    return [
        (p.stem, json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(EXAMPLES_DIR.glob("*.json"))
    ]


def base_policy(**overrides) -> dict:
    policy = json.loads((CONTRACT_DIR / "example-policy.json").read_text(encoding="utf-8"))
    policy.update(overrides)
    return policy


def base_packet(**overrides) -> dict:
    packet = json.loads((CONTRACT_DIR / "example-input.json").read_text(encoding="utf-8"))
    packet.update(overrides)
    return packet


# --------------------------------------------------------------------------- #
# frozen public surface
# --------------------------------------------------------------------------- #


def test_engine_exposes_only_the_frozen_callables():
    assert callable(engine.evaluate)
    assert callable(engine.canonical_json)
    assert callable(engine.canonical_sha256)
    assert isinstance(engine.ENGINE_VERSION, str) and engine.ENGINE_VERSION


def test_domain_module_imports_nothing_outside_the_domain_package():
    source = (
        REPO_ROOT / "packages" / "proofops" / "domain" / "reconciliation" / "engine.py"
    ).read_text(encoding="utf-8")
    for banned in (
        "boto3",
        "httpx",
        "requests",
        "os.environ",
        "open(",
        "pathlib",
        "proofops.adapters",
        "proofops.application",
    ):
        assert banned not in source, banned


def test_canonical_hash_uses_ascii_escaped_sorted_compact_json():
    value = {"b": 1, "a": "가"}
    expected = hashlib.sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert engine.canonical_sha256(value) == expected
    assert engine.canonical_json(value) == '{"a":"\\uac00","b":1}'


# --------------------------------------------------------------------------- #
# C5 containment
# --------------------------------------------------------------------------- #


def test_direct_c5_request_raises_not_implemented_and_never_a_value_error():
    with pytest.raises(NotImplementedError):
        engine.evaluate(base_packet(item="C5"), base_policy())


def test_c5_is_rejected_before_any_other_validation_so_a_broken_c5_packet_still_signals_stage():
    broken = base_packet(item="C5")
    del broken["sources"]
    with pytest.raises(NotImplementedError):
        engine.evaluate(broken, base_policy())


def test_c5_is_not_an_accepted_enum_value_anywhere_in_the_result():
    result = engine.evaluate(base_packet(), base_policy())
    assert "C5" not in json.dumps(result)


# --------------------------------------------------------------------------- #
# output shape
# --------------------------------------------------------------------------- #


def test_output_has_exactly_the_contract_keys_and_no_grade_or_label():
    result = engine.evaluate(base_packet(), base_policy())
    assert set(result) == OUTPUT_KEYS
    for forbidden in ("evidence_grade", "label", "grade", "sublabel", "confidence"):
        assert forbidden not in result


def test_reason_codes_is_always_a_non_empty_list_of_strings():
    for _name, case in all_cases():
        result = engine.evaluate(case["input"], case["policy"])
        assert isinstance(result["reason_codes"], list)
        assert result["reason_codes"]
        assert all(isinstance(code, str) and code for code in result["reason_codes"])


def test_blocked_and_not_run_always_carry_null_status():
    result = engine.evaluate(base_packet(), base_policy(approved=False))
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


def test_completed_always_carries_a_non_null_status():
    result = engine.evaluate(base_packet(), base_policy())
    assert result["execution_state"] == "completed"
    assert result["status"] in {"matched", "needs_explanation", "not_applicable"}


def test_source_ids_are_unique_and_drawn_only_from_the_packet_sources():
    for _name, case in all_cases():
        result = engine.evaluate(case["input"], case["policy"])
        available = {s["source_id"] for s in case["input"]["sources"]}
        assert len(result["source_ids"]) == len(set(result["source_ids"]))
        assert set(result["source_ids"]) <= available


def test_values_echo_the_raw_original_text_not_the_normalized_form():
    packet = base_packet()
    result = engine.evaluate(packet, base_policy())
    assert result["sustainability_value"] == packet["sustainability"]["raw"]
    assert result["financial_value"] == packet["financial"]["raw"]


def test_null_raw_is_reported_as_null_value_not_empty_string():
    packet = base_packet()
    packet["sustainability"]["raw"] = None
    packet["sustainability"]["normalized"] = None
    result = engine.evaluate(packet, base_policy())
    assert result["sustainability_value"] is None


# --------------------------------------------------------------------------- #
# hashes, replay, immutability
# --------------------------------------------------------------------------- #


def test_packet_and_policy_hashes_match_the_published_contract_fixtures():
    for name, case in all_cases():
        result = engine.evaluate(case["input"], case["policy"])
        assert result["packet_sha256"] == case["expected"]["packet_sha256"], name
        assert result["policy_sha256"] == case["expected"]["policy_sha256"], name


def test_replay_of_the_same_packet_and_policy_is_byte_identical():
    packet, policy = base_packet(), base_policy()
    first = engine.evaluate(packet, policy)
    second = engine.evaluate(packet, policy)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_evaluate_never_mutates_the_caller_packet_or_policy():
    packet, policy = base_packet(), base_policy()
    packet_before = copy.deepcopy(packet)
    policy_before = copy.deepcopy(policy)
    engine.evaluate(packet, policy)
    assert packet == packet_before
    assert policy == policy_before


def test_mutating_the_returned_result_cannot_reach_back_into_the_inputs():
    packet, policy = base_packet(), base_policy()
    result = engine.evaluate(packet, policy)
    result["source_ids"].append("injected")
    result["reason_codes"].append("injected")
    assert "injected" not in json.dumps(packet)
    assert engine.evaluate(packet, policy)["source_ids"] == ["sr-scope", "fs-scope"]


def test_two_packets_differing_only_in_a_nested_value_get_different_hashes():
    first = engine.evaluate(base_packet(), base_policy())
    other = base_packet()
    other["identity"]["tenant_id"] = "other-tenant"
    second = engine.evaluate(other, base_policy())
    assert first["packet_sha256"] != second["packet_sha256"]


# --------------------------------------------------------------------------- #
# published contract examples
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", [n for n, _ in all_cases()])
def test_engine_reproduces_every_published_contract_example(name):
    case = load_case(name)
    result = engine.evaluate(case["input"], case["policy"])
    expected = case["expected"]
    compared = (
        "schema_version",
        "claim_id",
        "item",
        "execution_state",
        "status",
        "review_required",
        "reason_codes",
        "source_ids",
        "explanation_source_id",
        "sustainability_value",
        "financial_value",
        "packet_sha256",
        "policy_sha256",
        "synthetic",
    )
    assert {key: result[key] for key in compared} == {key: expected[key] for key in compared}


def test_review_required_semantics_agree_with_every_published_example():
    """The fixtures are the authority on review_required, not an assumed formula."""
    observed = set()
    for _name, case in all_cases():
        result = engine.evaluate(case["input"], case["policy"])
        assert result["review_required"] == case["expected"]["review_required"]
        observed.add((result["execution_state"], result["status"], result["review_required"]))
    assert ("completed", "needs_explanation", False) in observed
    assert ("blocked", None, True) in observed
    assert ("completed", "matched", False) in observed
    assert ("completed", "not_applicable", False) in observed


# --------------------------------------------------------------------------- #
# policy gates
# --------------------------------------------------------------------------- #


def test_unapproved_policy_blocks_before_any_item_logic():
    result = engine.evaluate(base_packet(), base_policy(approved=False))
    assert (result["execution_state"], result["status"]) == ("blocked", None)
    assert result["reason_codes"] == ["policy_unapproved"]


def test_item_outside_enabled_items_blocks():
    result = engine.evaluate(base_packet(), base_policy(enabled_items=["C2", "C3"]))
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["item_not_enabled"]


def test_synthetic_only_policy_refuses_a_real_packet():
    result = engine.evaluate(base_packet(synthetic=False), base_policy(synthetic_only=True))
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["synthetic_policy_real_packet"]
    assert result["synthetic"] is False


def test_real_policy_accepts_a_real_packet():
    result = engine.evaluate(base_packet(synthetic=False), base_policy(synthetic_only=False))
    assert result["execution_state"] == "completed"


def test_unsupported_stage_blocks_rather_than_crashing():
    result = engine.evaluate(base_packet(), base_policy(current_stage=2))
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["stage_not_supported"]


def test_non_integer_stage_is_malformed():
    with pytest.raises(DomainValidationError):
        engine.evaluate(base_packet(), base_policy(current_stage="1"))


def test_boolean_is_not_accepted_where_an_integer_stage_is_required():
    with pytest.raises(DomainValidationError):
        engine.evaluate(base_packet(), base_policy(current_stage=True))


def test_policy_gate_order_is_deterministic_when_several_gates_fail():
    result = engine.evaluate(
        base_packet(synthetic=False), base_policy(approved=False, enabled_items=[])
    )
    assert result["reason_codes"] == ["policy_unapproved"]


# --------------------------------------------------------------------------- #
# comparability and kind gates
# --------------------------------------------------------------------------- #


def test_verified_not_comparable_is_a_completed_not_applicable_not_a_block():
    result = engine.evaluate(base_packet(comparability="not_comparable"), base_policy())
    assert (result["execution_state"], result["status"]) == ("completed", "not_applicable")
    assert result["reason_codes"] == ["not_comparable"]


def test_unknown_comparability_blocks_and_is_never_silently_treated_as_comparable():
    result = engine.evaluate(base_packet(comparability="unknown"), base_policy())
    assert (result["execution_state"], result["status"]) == ("blocked", None)
    assert result["reason_codes"] == ["comparability_unknown"]


def test_comparable_claim_whose_two_sides_declare_different_kinds_blocks():
    packet = base_packet()
    packet["financial"].update(kind="currency_amount", unit="KRW", normalized="1000", raw="1000")
    result = engine.evaluate(packet, base_policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["kind_mismatch"]


def test_a_value_that_contradicts_its_declared_kind_is_malformed_not_merely_blocked():
    """Declared kind and normalized form must agree before any semantics run."""
    packet = base_packet()
    packet["financial"]["kind"] = "currency_amount"  # value is still an entity set
    with pytest.raises(DomainValidationError):
        engine.evaluate(packet, base_policy())


def test_unknown_kind_blocks_as_unresolved_value():
    packet = base_packet()
    packet["sustainability"]["kind"] = "unknown"
    packet["financial"]["kind"] = "unknown"
    packet["sustainability"]["normalized"] = None
    packet["financial"]["normalized"] = None
    result = engine.evaluate(packet, base_policy())
    assert result["execution_state"] == "blocked"
    assert result["reason_codes"] == ["value_unresolved"]


# --------------------------------------------------------------------------- #
# malformed input
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("sources"), id="missing_sources"),
        pytest.param(lambda p: p.pop("identity"), id="missing_identity"),
        pytest.param(lambda p: p.update(schema_version="1.0"), id="wrong_schema_version"),
        pytest.param(lambda p: p.update(item="C9"), id="unknown_item"),
        pytest.param(lambda p: p.update(synthetic="true"), id="synthetic_not_boolean"),
        pytest.param(lambda p: p.update(comparability="maybe"), id="bad_comparability"),
        pytest.param(lambda p: p["sources"].append(p["sources"][0]), id="duplicate_source_id"),
        pytest.param(lambda p: p["claim"].update(source_id="missing"), id="dangling_claim_ref"),
        pytest.param(
            lambda p: p["sustainability"].update(source_id="missing"), id="dangling_fact_ref"
        ),
        pytest.param(
            lambda p: p["search"].update(reviewed_source_ids=["nope"]), id="dangling_reviewed_ref"
        ),
        pytest.param(
            lambda p: p["identity"].update(period_start="2025-01-01"), id="reversed_sr_period"
        ),
        pytest.param(
            lambda p: p["identity"].update(financial_period_end="2023-12-31"),
            id="reversed_financial_period",
        ),
        pytest.param(lambda p: p["identity"].update(dart_corp_code="123"), id="short_corp_code"),
        pytest.param(lambda p: p["identity"].update(consolidation="mixed"), id="bad_consolidation"),
        pytest.param(lambda p: p["identity"].update(as_of_date="2025-13-01"), id="impossible_date"),
        pytest.param(
            lambda p: p["identity"].update(financial_fiscal_year="2024"), id="fy_not_integer"
        ),
        pytest.param(
            lambda p: p["sources"][0].update(artifact_sha256="nothex"), id="bad_artifact_hash"
        ),
        pytest.param(
            lambda p: p["sources"][0].update(artifact_sha256="A" * 64), id="uppercase_hash"
        ),
        pytest.param(lambda p: p["sources"][0].update(quote=""), id="empty_quote"),
        pytest.param(lambda p: p["claim"].update(track="financial"), id="bad_track"),
        pytest.param(
            lambda p: p["claim"].update(trigger_elements=["a", "a"]), id="duplicate_triggers"
        ),
        pytest.param(
            lambda p: p["explanation"].update(search_complete=True), id="search_states_disagree"
        ),
        pytest.param(lambda p: p.update(extra_field=1), id="unknown_top_level_field"),
        pytest.param(lambda p: p["identity"].update(extra=1), id="unknown_identity_field"),
    ],
)
def test_malformed_packets_raise_value_error(mutate):
    packet = base_packet()
    mutate(packet)
    with pytest.raises(DomainValidationError):
        engine.evaluate(packet, base_policy())


def test_domain_validation_error_is_a_value_error_for_contract_consumers():
    assert issubclass(DomainValidationError, ValueError)
    with pytest.raises(ValueError):
        engine.evaluate(base_packet(item="bogus"), base_policy())


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("enabled_items"), id="missing_enabled_items"),
        pytest.param(lambda p: p.update(schema_version="1.0"), id="wrong_policy_version"),
        pytest.param(lambda p: p.update(c1_identity_rule="count_only"), id="weakened_c1_rule"),
        pytest.param(lambda p: p.update(c2_timing_rule="anything"), id="weakened_c2_rule"),
        pytest.param(
            lambda p: p.update(c4_required_explanations=["definition"]), id="weakened_c4_rule"
        ),
        pytest.param(lambda p: p.update(enabled_items=["C5"]), id="c5_in_enabled_items"),
        pytest.param(lambda p: p.update(enabled_items=["C1", "C1"]), id="duplicate_enabled_items"),
        pytest.param(lambda p: p.update(approved="yes"), id="approved_not_boolean"),
        pytest.param(lambda p: p.update(c3_threshold="-1"), id="negative_threshold"),
        pytest.param(lambda p: p.update(c3_threshold="abc"), id="non_numeric_threshold"),
        pytest.param(lambda p: p.update(source_policy_sha256="short"), id="bad_policy_hash"),
        pytest.param(
            lambda p: p.update(allowed_difference_types=["invented"]), id="unknown_difference_type"
        ),
        pytest.param(lambda p: p.update(extra=1), id="unknown_policy_field"),
    ],
)
def test_malformed_policies_raise_value_error(mutate):
    policy = base_policy()
    mutate(policy)
    with pytest.raises(DomainValidationError):
        engine.evaluate(base_packet(), policy)


@pytest.mark.parametrize("bad", [None, [], "packet", 3, True])
def test_non_object_arguments_raise_value_error(bad):
    with pytest.raises(DomainValidationError):
        engine.evaluate(bad, base_policy())
    with pytest.raises(DomainValidationError):
        engine.evaluate(base_packet(), bad)


def test_c3_item_without_context_is_malformed():
    with pytest.raises(DomainValidationError):
        engine.evaluate(base_packet(item="C3"), base_policy())


def test_c4_item_without_context_is_malformed():
    with pytest.raises(DomainValidationError):
        engine.evaluate(base_packet(item="C4"), base_policy())


def test_unserialisable_packet_is_rejected_rather_than_crashing_the_hash():
    packet = base_packet()
    packet["identity"]["tenant_id"] = {"unhashable": {1, 2}}
    with pytest.raises(DomainValidationError):
        engine.evaluate(packet, base_policy())


# --------------------------------------------------------------------------- #
# remaining validation branches
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.update(identity="not-an-object"), id="identity_not_object"),
        pytest.param(lambda p: p.update(sources={}), id="sources_not_an_array"),
        pytest.param(lambda p: p["sources"].__setitem__(0, "text"), id="source_not_an_object"),
        pytest.param(lambda p: p.update(claim=[]), id="claim_not_an_object"),
        pytest.param(
            lambda p: p["identity"].update(financial_fiscal_year=1800), id="fy_out_of_range"
        ),
        pytest.param(lambda p: p["claim"].update(fiscal_year=9999), id="claim_fy_out_of_range"),
        pytest.param(
            lambda p: p["claim"].update(trigger_elements="boundary"), id="triggers_not_an_array"
        ),
        pytest.param(lambda p: p["claim"].update(trigger_elements=[1]), id="trigger_not_a_string"),
        pytest.param(
            lambda p: p["identity"].update(as_of_date="2025/06/30"), id="date_wrong_separator"
        ),
        pytest.param(
            lambda p: p["identity"].update(as_of_date="20250630"), id="date_without_separators"
        ),
        pytest.param(lambda p: p["identity"].update(as_of_date=20250630), id="date_not_a_string"),
    ],
)
def test_remaining_malformed_packet_shapes_raise_value_error(mutate):
    packet = base_packet()
    mutate(packet)
    with pytest.raises(DomainValidationError):
        engine.evaluate(packet, base_policy())


def test_a_complete_search_must_name_the_documents_it_had_to_read():
    packet = base_packet()
    packet["search"] = {
        "state": "complete",
        "coverage_policy_id": "synthetic-coverage-only",
        "required_document_ids": [],
        "reviewed_source_ids": [],
        "failed_document_ids": [],
        "receipt_id": "receipt-1",
    }
    packet["explanation"]["search_complete"] = True
    with pytest.raises(DomainValidationError):
        engine.evaluate(packet, base_policy())


def test_canonical_json_refuses_a_value_it_cannot_serialise():
    with pytest.raises(DomainValidationError):
        engine.canonical_json({"unserialisable": {1, 2}})
    with pytest.raises(DomainValidationError):
        engine.canonical_sha256(object())


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_refuses_non_finite_floats(bad):
    """NaN/Infinity are not JSON; emitting them would desynchronise caller hashes."""
    with pytest.raises(DomainValidationError):
        engine.canonical_json({"amount": bad})
    with pytest.raises(DomainValidationError):
        engine.canonical_sha256([bad])


def test_canonical_json_still_accepts_ordinary_finite_numbers():
    assert engine.canonical_json({"amount": 1.5, "count": 2}) == '{"amount":1.5,"count":2}'
