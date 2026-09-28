"""Capacity-policy reservation tests: exact shape, detachment, fail-closed validation."""

import ast
from datetime import UTC, datetime, timedelta
from pathlib import Path

import proofops.application.input_reservation as reservation
import pytest
from proofops.application.input_reservation import (
    solar_pro4_capacity_policy,
    validate_capacity_policy,
)

CAPTURED = datetime(2026, 9, 18, 16, 53, tzinfo=UTC)
EXPIRES = datetime(2026, 9, 25, tzinfo=UTC)

EXPECTED_POLICY = {
    "schema": "upstage-context-capacity-reservation-v1",
    "model_id": "solar-pro4",
    "reservation_kind": "conservative-input-ceiling",
    "reservation_input_tokens": 1048576,
    "source_url": "https://www.upstage.ai/blog/en/solar-pro-4",
    "source_context_label": "512K",
    "source_quote": "Solar Pro 4 supports a 512K context with up to 128K output tokens.",
    "captured_at": "2026-09-18T16:53:00Z",
    "expires_at": "2026-09-25T00:00:00Z",
    "note": (
        "Conservative input reservation ceiling above both decimal and binary "
        "readings of the published 512K bound; not the actual model window and "
        "not experimentally proven tokenizer parity. Actual provider usage must "
        "settle separately."
    ),
}


def test_valid_policy_validates_to_fixed_reservation_bound():
    policy = solar_pro4_capacity_policy()
    assert policy == EXPECTED_POLICY
    assert policy["reservation_input_tokens"] == 2**20 == 1048576
    assert len(policy["source_quote"].split()) < 25
    assert validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED) == 1048576
    mid = datetime(2026, 9, 20, tzinfo=UTC)
    assert validate_capacity_policy(policy, model_id="solar-pro4", checked_at=mid) == 1048576
    just_before_expiry = EXPIRES - timedelta(microseconds=1)
    assert (
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=just_before_expiry)
        == 1048576
    )


def test_factory_returns_detached_copy():
    first = solar_pro4_capacity_policy()
    second = solar_pro4_capacity_policy()
    assert first == second and first is not second
    first["reservation_input_tokens"] = 0
    first["note"] = "mutated"
    assert solar_pro4_capacity_policy() == EXPECTED_POLICY


def test_wrong_model_rejected():
    policy = solar_pro4_capacity_policy()
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro3", checked_at=CAPTURED)
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4-latest", checked_at=CAPTURED)
    tampered = dict(policy, model_id="solar-pro3")
    with pytest.raises(ValueError):
        validate_capacity_policy(tampered, model_id="solar-pro3", checked_at=CAPTURED)
    with pytest.raises(ValueError):
        validate_capacity_policy(tampered, model_id="solar-pro4", checked_at=CAPTURED)


@pytest.mark.parametrize("key", sorted(EXPECTED_POLICY))
def test_missing_key_rejected(key):
    policy = {k: v for k, v in solar_pro4_capacity_policy().items() if k != key}
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


def test_extra_key_rejected():
    policy = dict(solar_pro4_capacity_policy(), fallback_model="solar-pro3")
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


@pytest.mark.parametrize("policy", [[], "policy", None, 1048576, True])
def test_non_object_policy_rejected(policy):
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema", "other-schema"),
        ("reservation_kind", "actual-model-window"),
        ("note", "something else"),
    ],
)
def test_wrong_descriptor_value_rejected(key, value):
    policy = dict(solar_pro4_capacity_policy(), **{key: value})
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


@pytest.mark.parametrize("count", [512000, 524288, 0, -1, 1048577, "1048576", 1048576.0, None])
def test_wrong_count_rejected(count):
    policy = dict(solar_pro4_capacity_policy(), reservation_input_tokens=count)
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


@pytest.mark.parametrize("count", [True, False])
def test_bool_never_counts_as_reservation(count):
    policy = dict(solar_pro4_capacity_policy(), reservation_input_tokens=count)
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("source_url", "https://example.com/other-model"),
        ("source_context_label", "128K"),
        ("source_quote", "Some other marketing sentence about unlimited tokens."),
        ("source_quote", "word " * 25),
        ("source_quote", ""),
        ("source_quote", None),
    ],
)
def test_wrong_source_evidence_rejected(key, value):
    policy = dict(solar_pro4_capacity_policy(), **{key: value})
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


def test_tampered_policy_timestamps_rejected():
    for key, value in [
        ("captured_at", "2026-09-17T00:00:00Z"),
        ("expires_at", "2026-10-01T00:00:00Z"),
        ("captured_at", "not-a-timestamp"),
        ("captured_at", "2026-09-18T16:53:00"),
    ]:
        policy = dict(solar_pro4_capacity_policy(), **{key: value})
        with pytest.raises(ValueError):
            validate_capacity_policy(policy, model_id="solar-pro4", checked_at=CAPTURED)


def test_expired_or_not_yet_captured_rejected():
    policy = solar_pro4_capacity_policy()
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=EXPIRES)
    after_expiry = EXPIRES + timedelta(days=1)
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=after_expiry)
    with pytest.raises(ValueError):
        validate_capacity_policy(
            policy, model_id="solar-pro4", checked_at=CAPTURED - timedelta(microseconds=1)
        )


@pytest.mark.parametrize(
    "checked_at",
    [
        "2026-09-18T16:53:00Z",
        "2026-09-20",
        None,
        1726680000,
        True,
        datetime(2026, 9, 20, 12, 0, 0),
    ],
)
def test_wrong_checked_at_type_rejected(checked_at):
    policy = solar_pro4_capacity_policy()
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=checked_at)


def test_module_has_no_ambient_clock_network_file_or_env_imports():
    tree = ast.parse(Path(reservation.__file__).read_text())
    allowed = {"__future__", "datetime", "typing", "proofops.domain.errors"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= allowed | {"proofops"}, imported


REFRESHED_CAPTURED = datetime(2026, 9, 25, 10, 57, tzinfo=UTC)
REFRESHED_EXPIRES = datetime(2026, 10, 2, tzinfo=UTC)

EXPECTED_REFRESHED_POLICY = dict(
    EXPECTED_POLICY,
    captured_at="2026-09-25T10:57:00Z",
    expires_at="2026-10-02T00:00:00Z",
)


def test_old_policy_historical_valid_and_now_expired():
    legacy = solar_pro4_capacity_policy()
    assert legacy == EXPECTED_POLICY
    # Historically valid within its window
    historical = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    assert validate_capacity_policy(legacy, model_id="solar-pro4", checked_at=historical) == 1048576
    # Now expired on/after 2026-09-25T00:00:00Z
    now_expired = datetime(2026, 9, 25, 10, 55, tzinfo=UTC)
    with pytest.raises(ValueError):
        validate_capacity_policy(legacy, model_id="solar-pro4", checked_at=now_expired)


def test_refreshed_policy_opt_in_and_validity_window():
    refreshed = solar_pro4_capacity_policy(refreshed=True)
    assert refreshed == EXPECTED_REFRESHED_POLICY
    # Valid at exact captured_at
    assert (
        validate_capacity_policy(refreshed, model_id="solar-pro4", checked_at=REFRESHED_CAPTURED)
        == 1048576
    )
    # Valid mid-window
    mid = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
    assert validate_capacity_policy(refreshed, model_id="solar-pro4", checked_at=mid) == 1048576
    # Valid just before expiry
    just_before = REFRESHED_EXPIRES - timedelta(microseconds=1)
    assert (
        validate_capacity_policy(refreshed, model_id="solar-pro4", checked_at=just_before)
        == 1048576
    )


def test_refreshed_policy_expiry_and_pre_capture_rejected():
    refreshed = solar_pro4_capacity_policy(refreshed=True)
    # Rejected at exactly expires_at
    with pytest.raises(ValueError):
        validate_capacity_policy(refreshed, model_id="solar-pro4", checked_at=REFRESHED_EXPIRES)
    # Rejected after expires_at
    with pytest.raises(ValueError):
        validate_capacity_policy(
            refreshed, model_id="solar-pro4", checked_at=REFRESHED_EXPIRES + timedelta(days=1)
        )
    # Rejected before captured_at
    with pytest.raises(ValueError):
        validate_capacity_policy(
            refreshed,
            model_id="solar-pro4",
            checked_at=REFRESHED_CAPTURED - timedelta(microseconds=1),
        )


def test_validator_recognizes_only_the_two_exact_pinned_versions_and_rejects_malformed():
    # Mismatched/mixed timestamps between revisions
    mixed_1 = dict(
        EXPECTED_POLICY,
        captured_at="2026-09-18T16:53:00Z",
        expires_at="2026-10-02T00:00:00Z",
    )
    with pytest.raises(ValueError):
        validate_capacity_policy(
            mixed_1, model_id="solar-pro4", checked_at=datetime(2026, 9, 20, tzinfo=UTC)
        )

    mixed_2 = dict(
        EXPECTED_POLICY,
        captured_at="2026-09-25T10:57:00Z",
        expires_at="2026-09-25T00:00:00Z",
    )
    with pytest.raises(ValueError):
        validate_capacity_policy(
            mixed_2, model_id="solar-pro4", checked_at=datetime(2026, 9, 25, 11, 0, tzinfo=UTC)
        )

    # Extended expiry without capture refresh (bypass attempt)
    tampered_extension = dict(
        EXPECTED_POLICY,
        captured_at="2026-09-18T16:53:00Z",
        expires_at="2026-09-26T00:00:00Z",
    )
    with pytest.raises(ValueError):
        validate_capacity_policy(
            tampered_extension,
            model_id="solar-pro4",
            checked_at=datetime(2026, 9, 25, 12, 0, tzinfo=UTC),
        )

    # Invalid argument type to helper
    with pytest.raises(ValueError):
        solar_pro4_capacity_policy(refreshed="invalid")


@pytest.mark.parametrize("field", ["captured_at", "expires_at"])
@pytest.mark.parametrize("value", [[], {}, None, 2026])
def test_capacity_timestamp_non_text_is_validation_error(field, value):
    policy = dict(EXPECTED_REFRESHED_POLICY, **{field: value})
    with pytest.raises(ValueError):
        validate_capacity_policy(policy, model_id="solar-pro4", checked_at=REFRESHED_CAPTURED)
