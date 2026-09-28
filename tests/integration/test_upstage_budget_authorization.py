"""Budget authorization extension tests – no network, no live API keys.

Verifies the cumulative USD 20 authorization (old 10 + additional 10) and the
fail-closed semantics around the explicit ledger extension.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from proofops.adapters.local import upstage

# ---------------------------------------------------------------------------
# Shared fixture: pin datetime so the Sep-25 expiry guard does not fire.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def fixed_pricing_date(monkeypatch):
    from datetime import UTC, datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 18, tzinfo=UTC)

    monkeypatch.setattr(upstage, "datetime", FixedDateTime)


def _response(model="solar-pro3"):
    return {
        "id": "test-provider-id",
        "model": model,
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


# ---------------------------------------------------------------------------
# 1. Cumulative cap: 10 + 10 = 20
# ---------------------------------------------------------------------------


def test_cumulative_cap_extends_from_10_to_20(tmp_path, monkeypatch):
    """Explicit extension doubles the cap; POLICY limit_usd stays at '10.00'."""
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    result = client.authorize_additional_budget("10.00", reason="User approved USD 20 total")
    assert result["previous_limit_usd"] == "10.00"
    assert result["additional_usd"] == "10.00"
    assert result["reason"] == "User approved USD 20 total"
    assert "authorized_at" in result

    s = client.summary()
    assert s["limit_usd"] == "10.00"  # POLICY static field unchanged
    assert s["authorized_limit_usd"] == "20.00"  # base + extension


def test_base_policy_limit_unchanged_after_extension(tmp_path):
    """POLICY["limit_usd"] must still read '10.00' to avoid breaking callers."""
    assert upstage.POLICY["limit_usd"] == "10.00"
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    client.authorize_additional_budget("10.00", reason="ext")
    # POLICY module-level dict is unchanged
    assert upstage.POLICY["limit_usd"] == "10.00"


def test_calls_within_base_budget_succeed_without_extension(tmp_path, monkeypatch):
    """New clients without extension still cap at $10 (10 × $1 reservation).

    We use a failing _post so each call keeps the full $1 reservation; successful
    calls settle at ~$0.00003, which would require thousands of calls to exhaust $10.
    """
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    monkeypatch.setattr(client, "_post", lambda body: (_ for _ in ()).throw(OSError()))
    for i in range(10):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            client.complete("JSON", "{}", request_id=str(i))
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        client.complete("JSON", "{}", request_id="over")


def test_calls_up_to_20_allowed_after_extension(tmp_path, monkeypatch):
    """After extending to $20, up to 20 × $1-reservation calls are allowed.

    We use a failing _post so each call keeps the full $1 reservation.
    """
    path = tmp_path / "budget.sqlite3"
    client = upstage.UpstageProbe("test-secret", path)
    client.authorize_additional_budget("10.00", reason="USD 20 auth")
    monkeypatch.setattr(client, "_post", lambda body: (_ for _ in ()).throw(OSError()))
    for i in range(20):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            client.complete("JSON", "{}", request_id=str(i))
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        client.complete("JSON", "{}", request_id="over")
    assert Decimal(client.summary()["authorized_limit_usd"]) == 20
    assert client.summary()["calls"] == 20


# ---------------------------------------------------------------------------
# 2. Concurrent reservation respects extended cap
# ---------------------------------------------------------------------------


def test_concurrent_reservations_respect_extended_cap(tmp_path, monkeypatch):
    """With $20 limit, concurrent threads must not collectively exceed 20 slots."""
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    client.authorize_additional_budget("10.00", reason="concurrent test")
    monkeypatch.setattr(client, "_post", lambda body: (_ for _ in ()).throw(OSError()))

    def call(i):
        try:
            client.complete("JSON", "{}", request_id=str(i))
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(call, range(22)))

    exhausted = outcomes.count("BUDGET_EXHAUSTED")
    assert exhausted == 2, f"Expected 2 BUDGET_EXHAUSTED, got {exhausted}: {outcomes}"
    assert Decimal(client.summary()["committed_usd"]) == 20


# ---------------------------------------------------------------------------
# 3. Restart: authorization persists across client reconstruction
# ---------------------------------------------------------------------------


def test_authorization_survives_restart(tmp_path, monkeypatch):
    """Extension rows are durable; a fresh UpstageProbe on the same ledger sees them."""
    path = tmp_path / "budget.sqlite3"
    first = upstage.UpstageProbe("test-secret", path)
    first.authorize_additional_budget("10.00", reason="durable ext")

    # Fill all 10 base slots with failed (unsettled) calls ($1 each reserved).
    monkeypatch.setattr(first, "_post", lambda body: (_ for _ in ()).throw(OSError()))
    for i in range(10):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            first.complete("JSON", "{}", request_id=f"pre-{i}")

    # Reconstruct client; it should see the extension and allow 10 more calls.
    # Use failing _post so each also keeps its $1 reservation → 20 slots total.
    second = upstage.UpstageProbe("test-secret", path)
    monkeypatch.setattr(second, "_post", lambda body: (_ for _ in ()).throw(OSError()))
    for i in range(10):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            second.complete("JSON", "{}", request_id=f"post-{i}")

    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        second.complete("JSON", "{}", request_id="over-restart")
    assert second.summary()["authorized_limit_usd"] == "20.00"


# ---------------------------------------------------------------------------
# 4. Unchanged unsettled rows after extension
# ---------------------------------------------------------------------------


def test_existing_unsettled_rows_unchanged_after_extension(tmp_path, monkeypatch):
    """authorize_additional_budget must not modify any existing probe_calls rows."""
    path = tmp_path / "budget.sqlite3"
    client = upstage.UpstageProbe("test-secret", path)
    monkeypatch.setattr(client, "_post", lambda body: (_ for _ in ()).throw(OSError()))

    # Create 3 unsettled reservations.
    for i in range(3):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            client.complete("JSON", "{}", request_id=str(i))

    import sqlite3

    with sqlite3.connect(path) as db:
        before = db.execute(
            "SELECT request_id, committed, receipt FROM probe_calls ORDER BY request_id"
        ).fetchall()

    client.authorize_additional_budget("10.00", reason="unsettled test")

    with sqlite3.connect(path) as db:
        after = db.execute(
            "SELECT request_id, committed, receipt FROM probe_calls ORDER BY request_id"
        ).fetchall()

    assert before == after, "authorize_additional_budget must not touch probe_calls rows"
    assert client.summary()["unsettled_calls"] == 3
    assert Decimal(client.summary()["committed_usd"]) == 3


# ---------------------------------------------------------------------------
# 5. Invalid policy inputs rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "amount,reason",
    [
        ("nan", "NaN must be rejected"),
        ("inf", "Infinity must be rejected"),
        ("-10.00", "Negative must be rejected"),
        ("0", "Zero must be rejected"),
        ("0.00", "Zero must be rejected"),
        ("", "Empty string must be rejected"),
        ("abc", "Non-numeric must be rejected"),
    ],
)
def test_invalid_amount_is_rejected(tmp_path, amount, reason):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    with pytest.raises(ValueError, match="AUTHORIZATION_AMOUNT_INVALID"):
        client.authorize_additional_budget(amount, reason="valid reason")
    # Ledger must not have any extension row.
    import sqlite3

    with sqlite3.connect(tmp_path / "budget.sqlite3") as db:
        count = db.execute("SELECT COUNT(*) FROM probe_extensions").fetchone()[0]
    assert count == 0, f"Extension row was written for invalid amount '{amount}': {reason}"


@pytest.mark.parametrize(
    "reason",
    [
        "",
        "   ",
        "x" * 513,
    ],
)
def test_invalid_reason_is_rejected(tmp_path, reason):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    with pytest.raises(ValueError, match="AUTHORIZATION_REASON_REQUIRED"):
        client.authorize_additional_budget("10.00", reason=reason)


# ---------------------------------------------------------------------------
# 6. Expiry guard is retained (the guard fires before any budget check)
# ---------------------------------------------------------------------------


def test_expiry_guard_blocks_complete_at_oct2(tmp_path, monkeypatch):
    """PRICE_RECHECK_REQUIRED must fire when datetime.now() >= 2026-10-02."""
    from datetime import UTC, datetime

    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    client.authorize_additional_budget("10.00", reason="expiry guard test")

    class ExpiredDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, tzinfo=UTC)

    monkeypatch.setattr(upstage, "datetime", ExpiredDateTime)
    with pytest.raises(ValueError, match="PRICE_RECHECK_REQUIRED"):
        client.complete("JSON", "{}", request_id="expired")
    # No budget consumed by the blocked call.
    assert client.summary()["calls"] == 0


def test_expiry_guard_does_not_block_authorize_additional_budget(tmp_path, monkeypatch):
    """authorize_additional_budget has no expiry guard; only complete() checks the date."""
    from datetime import UTC, datetime

    class ExpiredDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 2, tzinfo=UTC)

    monkeypatch.setattr(upstage, "datetime", ExpiredDateTime)
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    # Should not raise – the ledger extension itself is time-independent.
    result = client.authorize_additional_budget("10.00", reason="post-expiry admin")
    assert result["additional_usd"] == "10.00"


# ---------------------------------------------------------------------------
# 7. Policy mismatch is fail-closed (tampered ledger refuses extension)
# ---------------------------------------------------------------------------


def test_policy_mismatch_blocks_extension(tmp_path, monkeypatch):
    """If probe_policy row 1 has been tampered, extension must raise BUDGET_POLICY_MISMATCH."""
    import sqlite3

    path = tmp_path / "budget.sqlite3"
    upstage.UpstageProbe("test-secret", path)  # initialise ledger

    # Tamper the stored policy.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE probe_policy SET body='{}' WHERE id=1")

    client = upstage.UpstageProbe.__new__(upstage.UpstageProbe)
    client._model = "solar-pro3"
    client._price = upstage.PRICE
    client._api_key = "test-secret"
    client.ledger = path

    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        client.authorize_additional_budget("10.00", reason="should fail")


# ---------------------------------------------------------------------------
# 8. summary() authorized_limit_usd without any extension equals base $10
# ---------------------------------------------------------------------------


def test_summary_authorized_limit_equals_base_without_extension(tmp_path):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    s = client.summary()
    assert s["limit_usd"] == "10.00"
    assert s["authorized_limit_usd"] == "10.00"
    assert s["calls"] == 0


# ---------------------------------------------------------------------------
# 9. Ceiling enforcement: cumulative total cannot exceed USD 30
# ---------------------------------------------------------------------------


def test_third_extension_rejected_if_exceeds_30_ceiling(tmp_path):
    """A third extension would exceed the user's $30 cumulative limit."""
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    client.authorize_additional_budget("10.00", reason="first ext")
    assert client.summary()["authorized_limit_usd"] == "20.00"
    client.authorize_additional_budget("10.00", reason="second ext")

    with pytest.raises(ValueError, match="AUTHORIZATION_EXCEEDS_CEILING"):
        client.authorize_additional_budget("0.01", reason="third ext - must fail")
    # Ledger must not change.
    assert client.summary()["authorized_limit_usd"] == "30.00"


def test_partial_extension_below_ceiling_is_allowed(tmp_path):
    """An extension that keeps cumulative total <= $30 is permitted."""
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    client.authorize_additional_budget("5.00", reason="first half")
    client.authorize_additional_budget("5.00", reason="second half")
    assert client.summary()["authorized_limit_usd"] == "20.00"
    client.authorize_additional_budget("10.00", reason="third extension")

    with pytest.raises(ValueError, match="AUTHORIZATION_EXCEEDS_CEILING"):
        client.authorize_additional_budget("0.01", reason="one cent too many")


# ---------------------------------------------------------------------------
# 10. _reserve validates persisted policy body inside BEGIN IMMEDIATE
# ---------------------------------------------------------------------------


def test_reserve_rejects_tampered_policy_body(tmp_path, monkeypatch):
    """_reserve must catch a tampered probe_policy row before making a reservation."""
    import sqlite3

    path = tmp_path / "budget.sqlite3"
    client = upstage.UpstageProbe("test-secret", path)
    # Tamper the persisted policy AFTER construction.
    with sqlite3.connect(path) as db:
        db.execute("UPDATE probe_policy SET body='{}' WHERE id=1")

    monkeypatch.setattr(client, "_post", lambda body: _response())
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        client.complete("JSON", "{}", request_id="tampered")
    # Both reservation and summary reject a tampered policy; inspect rows directly.
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        client.summary()
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM probe_calls").fetchone()[0] == 0


def test_rechecked_price_allows_sep25_request_without_spending(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    class RecheckedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 25, 12, tzinfo=UTC)

    monkeypatch.setattr(upstage, "datetime", RecheckedClock)
    client = upstage.UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    client.request_body("JSON", "{}", request_id="price-rechecked")
    assert client.summary()["calls"] == 0
