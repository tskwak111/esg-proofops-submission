"""Budget authorization cannot mint more than the user's cumulative USD30."""

import sqlite3

import pytest
from proofops.adapters.local.upstage import R32_REASON, UpstageProbe


def test_scoped_history_never_raises_general_cap(tmp_path):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    probe.authorize_additional_budget("10.00", reason="prior authorization")
    for index in range(20):
        probe._reserve(str(index), {})
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "INSERT INTO probe_extensions(additional_usd,reason,authorized_at) VALUES (?,?,?)",
            (
                "2.00",
                R32_REASON,
                "2026-09-23T15:33:05.574485+00:00",
            ),
        )
    assert probe.summary()["authorized_limit_usd"] == "20.00"
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        probe._reserve("outside-evaluation", {})


def test_unknown_scoped_row_fails_closed(tmp_path):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    probe.authorize_additional_budget("10.00", reason="prior authorization")
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "INSERT INTO probe_extensions(additional_usd,reason,authorized_at) VALUES (?,?,?)",
            ("2.00", "R32 wrong scope", "2026-09-23T15:33:05+00:00"),
        )
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        probe._reserve("blocked", {})


def test_second_extension_reaches_30_but_no_more(tmp_path):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    probe.authorize_additional_budget("10.00", reason="user approved additional USD10")
    probe.authorize_additional_budget("10.00", reason="user approved cumulative USD30")
    with pytest.raises(ValueError, match="AUTHORIZATION_EXCEEDS_CEILING"):
        probe.authorize_additional_budget("0.01", reason="over ceiling")
    assert probe.summary()["authorized_limit_usd"] == "30.00"


def test_r32_then_one_general_grant_reaches_30(tmp_path):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    probe.authorize_additional_budget("10.00", reason="prior authorization")
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "INSERT INTO probe_extensions(additional_usd,reason,authorized_at) VALUES (?,?,?)",
            ("2.00", R32_REASON, "2026-09-23T15:33:05+00:00"),
        )
    assert probe.summary()["authorized_limit_usd"] == "20.00"
    with pytest.raises(ValueError, match="AUTHORIZATION_EXCEEDS_CEILING"):
        probe.authorize_additional_budget("10.01", reason="over ceiling")
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        probe.authorize_additional_budget("5.00", reason="noncanonical post-R32 amount")
    probe.authorize_additional_budget("10.00", reason="2026-09-28 user cumulative USD30")
    assert probe.summary()["authorized_limit_usd"] == "30.00"
    with pytest.raises(ValueError, match="AUTHORIZATION_EXCEEDS_CEILING"):
        probe.authorize_additional_budget("0.01", reason="second grant")
    with sqlite3.connect(probe.ledger) as db:
        assert db.execute(
            "SELECT id,additional_usd FROM probe_extensions ORDER BY id"
        ).fetchall() == [(1, "10.00"), (2, "2.00"), (3, "10.00")]


@pytest.mark.parametrize(
    "amount,reason",
    [
        ("10.00", "unscoped"),
        ("5.00", '{"reason":"too small","scope":"general"}'),
        ("10.00", '{"scope":"general","reason":"noncanonical order"}'),
    ],
)
def test_post_r32_malformed_grant_fails_closed(tmp_path, amount, reason):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    probe.authorize_additional_budget("10.00", reason="prior authorization")
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "INSERT INTO probe_extensions(additional_usd,reason,authorized_at) VALUES (?,?,?)",
            ("2.00", R32_REASON, "2026-09-23T15:33:05+00:00"),
        )
        db.execute(
            "INSERT INTO probe_extensions(additional_usd,reason,authorized_at) VALUES (?,?,?)",
            (amount, reason, "2026-09-28T00:00:00+00:00"),
        )
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        probe.summary()


@pytest.mark.parametrize("amount", ["NaN", "-10.00", "10.01", "Infinity"])
def test_reservation_revalidates_durable_authorization(tmp_path, amount):
    path = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", path)
    with sqlite3.connect(path) as db:
        db.execute(
            "INSERT INTO probe_extensions(additional_usd,reason,authorized_at) VALUES (?,?,?)",
            (amount, "tampered", "2026-09-18T00:00:00Z"),
        )
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        probe._reserve("no-network", {})
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM probe_calls").fetchone()[0] == 0


def test_reservation_revalidates_base_policy_after_construction(tmp_path):
    path = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", path)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE probe_policy SET body='{}' WHERE id=1")
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        probe._reserve("no-network", {})


@pytest.mark.parametrize(
    "amount,receipt",
    [
        ("-100", None),
        ("NaN", None),
        ("Infinity", None),
        ("0", None),
        ("0.99", None),
        ("-1", "{}"),
        ("NaN", "{}"),
        ("2", "{}"),
        ("0.5", "not-json"),
        ("0.5", '{"cost_with_vat_reserve_usd":"0.1"}'),
    ],
)
def test_malformed_call_balance_blocks_reservation(tmp_path, amount, receipt):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "INSERT INTO probe_calls VALUES (?,?,?,?)", ("bad", "signature", amount, receipt)
        )
    with pytest.raises(ValueError, match="BUDGET_LEDGER_INVALID"):
        probe._reserve("new", {})
    with sqlite3.connect(probe.ledger) as db:
        assert db.execute("SELECT COUNT(*) FROM probe_calls").fetchone()[0] == 1


@pytest.mark.parametrize("reason", ["r32 evaluation", "Only unrelated runs", "unknown grant"])
def test_unknown_extension_reason_fails_closed_even_without_scope_keywords(tmp_path, reason):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "INSERT INTO probe_extensions(additional_usd,reason,authorized_at) VALUES (?,?,?)",
            ("2.00", reason, "2026-09-23T15:33:05+00:00"),
        )
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        probe._reserve("new", {})


def test_r32_recorded_form_requires_exact_id(tmp_path):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "INSERT INTO probe_extensions(id,additional_usd,reason,authorized_at) VALUES (?,?,?,?)",
            (3, "2.00", R32_REASON, "2026-09-23T15:33:05+00:00"),
        )
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        probe._reserve("new", {})


def test_canonical_general_reason_must_fit_stored_limit(tmp_path):
    probe = UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    with pytest.raises(ValueError, match="AUTHORIZATION_REASON_REQUIRED"):
        probe.authorize_additional_budget("1", reason="x" * 500)
    with sqlite3.connect(probe.ledger) as db:
        assert db.execute("SELECT COUNT(*) FROM probe_extensions").fetchone()[0] == 0
