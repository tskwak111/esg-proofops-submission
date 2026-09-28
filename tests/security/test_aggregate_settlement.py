import csv
import json
import os
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from proofops.adapters.local.upstage import UpstageProbe, request_usage

from scripts import settle_unsettled_reservations as settlement

HEADER = [
    "date",
    "api_key_id",
    "api_key_name",
    "owner_email",
    "usage_type",
    "billing_source",
    "agent_id",
    "agent_name",
    "product",
    "pricing_options",
    "quantity",
    "unit",
    "item_cost",
    "used_credit",
    "subtotal",
]


def fixture(tmp_path):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", ledger)
    probe.authorize_additional_budget("10", reason="approved")
    for request_id in ("one", "two"):
        probe._reserve(request_id, {})
    csv_path = tmp_path / "usage.csv"
    with csv_path.open("w", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(HEADER)
        writer.writerow(
            [
                "2026-09-09",
                "key-a",
                "ESG",
                "private-operator",
                "api",
                "payg",
                "",
                "",
                "solar-pro3",
                "Input",
                "1",
                "",
                "1.20",
                "1.20",
                "0",
            ]
        )
        writer.writerow(
            [
                "2026-09-25",
                "key-b",
                "ESG",
                "private-operator",
                "api",
                "payg",
                "",
                "",
                "solar-pro4",
                "Output",
                "1",
                "",
                "2.17",
                "2.17",
                "0",
            ]
        )
    args = [
        "--ledger",
        str(ledger),
        "--aggregate-bound",
        str(csv_path),
        "--window-start",
        "2026-09-01",
        "--window-end",
        datetime.now(UTC).date().isoformat(),
        "--key-scope",
        "all",
    ]
    return probe, csv_path, args


def test_aggregate_dry_run_apply_idempotent_and_budget(tmp_path, capsys):
    probe, _, args = fixture(tmp_path)
    before = probe.ledger.read_bytes()
    assert settlement.main(args) == 0
    assert probe.ledger.read_bytes() == before
    assert "3.37" in capsys.readouterr().out
    assert settlement.main([*args, "--apply"]) == 0
    assert probe.summary()["committed_usd"] == "3.37"
    assert request_usage(probe.ledger, ["one"])["cost_with_vat_reserve_usd"] == "unknown"
    with sqlite3.connect(probe.ledger) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM probe_calls WHERE committed='0' AND receipt IS NOT NULL"
            ).fetchone()[0]
            == 2
        )
        audit_raw = db.execute("SELECT audit FROM probe_aggregate_bounds").fetchone()[0]
        assert "private-operator" not in audit_raw
        audit = json.loads(audit_raw)
        assert audit["affected_request_ids"] == ["one", "two"]
        assert audit["key_scope"] == "all"
        assert audit["earliest_evidence_at"] == "2026-09-09T00:00:00Z"
        assert db.execute("SELECT count(*) FROM probe_aggregate_bounds").fetchone()[0] == 1
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE probe_aggregate_bounds SET committed='0'")
    assert settlement.main([*args, "--apply"]) == 0
    assert probe.summary()["committed_usd"] == "3.37"
    assert "already applied" in capsys.readouterr().out
    future = datetime(2026, 9, 27, tzinfo=UTC).timestamp()
    os.utime(probe.ledger, (future, future))
    assert settlement.main([*args, "--apply"]) == 0
    for index in range(16):
        probe._reserve(f"new-{index}", {})
    with pytest.raises(ValueError, match="already used"):
        settlement.main([*args, "--apply"])
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        probe._reserve("too-many", {})


def test_aggregate_rejects_bound_over_authorized_cap_without_writes(tmp_path):
    probe, path, args = fixture(tmp_path)
    probe._reserve("already-settled", {})
    probe._settle(
        "already-settled",
        Decimal("0.50"),
        {"cost_with_vat_reserve_usd": "0.50"},
    )
    data = path.read_text()
    assert "1.20,1.20,0" in data
    path.write_text(data.replace("1.20,1.20,0", "18.83,18.83,0", 1))
    before = probe.ledger.read_bytes()

    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        settlement.main([*args, "--apply"])

    assert probe.ledger.read_bytes() == before
    assert probe.summary()["committed_usd"] == "2.50"
    assert probe.summary()["unsettled_calls"] == 2


def test_aggregate_rejects_unverified_narrow_key_scope(tmp_path):
    probe, _, args = fixture(tmp_path)
    trial = args.copy()
    trial[trial.index("--key-scope") + 1] = "key-a"

    with pytest.raises(ValueError, match="key scope must be 'all'"):
        settlement.main([*trial, "--apply"])

    assert probe.summary()["unsettled_calls"] == 2


def test_window_must_cover_earliest_ledger_evidence(tmp_path):
    probe, path, args = fixture(tmp_path)
    data = path.read_text()
    assert "2026-09-09" in data
    path.write_text(data.replace("2026-09-09", "2026-09-10", 1))
    trial = args.copy()
    trial[trial.index("--window-start") + 1] = "2026-09-10"

    with pytest.raises(ValueError, match="window does not cover ledger earliest evidence"):
        settlement.main(trial)

    assert probe.summary()["unsettled_calls"] == 2


def test_window_uses_verified_response_timestamp(tmp_path):
    probe, _, args = fixture(tmp_path)
    response_dir = probe.ledger.with_name(probe.ledger.name + ".responses")
    response_path = response_dir / f"{settlement.canonical_hash('one')}.json"
    response_path.write_text(
        json.dumps(
            {
                "request_id": "one",
                "provider_response": {
                    "created": int(datetime(2026, 8, 31, 12, tzinfo=UTC).timestamp())
                },
            }
        )
    )

    with pytest.raises(ValueError, match="window does not cover ledger earliest evidence"):
        settlement.main(args)

    assert probe.summary()["unsettled_calls"] == 2


@pytest.mark.parametrize(
    "mutation", ["unknown_header", "bad_date", "negative_cost", "missing_column", "bad_type"]
)
def test_csv_shape_rejected(tmp_path, mutation):
    probe, path, args = fixture(tmp_path)
    data = path.read_text()
    if mutation == "unknown_header":
        data = data.replace("item_cost", "cost")
    elif mutation == "bad_date":
        data = data.replace("2026-09-09", "yesterday")
    elif mutation == "negative_cost":
        data = data.replace("1.20,1.20,0", "-1.20,1.20,0")
    elif mutation == "bad_type":
        data = data.replace(",api,payg,", ",web,payg,")
    else:
        data = data.replace("item_cost,", "")
    path.write_text(data)
    with pytest.raises(ValueError):
        settlement.main([*args, "--apply"])
    assert probe.summary()["unsettled_calls"] == 2


def test_window_and_scope_rejected(tmp_path):
    probe, _, args = fixture(tmp_path)
    for start, end, scope in [
        ("2026-09-10", "2026-09-26", "all"),
        ("2026-09-01", "2026-09-25", "all"),
        ("2026-09-01", "2026-09-26", "missing"),
    ]:
        trial = args.copy()
        trial[trial.index("--window-start") + 1] = start
        trial[trial.index("--window-end") + 1] = end
        trial[trial.index("--key-scope") + 1] = scope
        with pytest.raises(ValueError):
            settlement.main([*trial, "--apply"])
    assert probe.summary()["unsettled_calls"] == 2


def test_aggregate_atomic_refuses_changed_row(tmp_path, monkeypatch):
    probe, _, args = fixture(tmp_path)
    original = settlement.parse_aggregate_csv

    def changed(*values):
        result = original(*values)
        with sqlite3.connect(probe.ledger) as db:
            db.execute("UPDATE probe_calls SET signature='changed' WHERE request_id='one'")
        return result

    monkeypatch.setattr(settlement, "parse_aggregate_csv", changed)
    with pytest.raises(ValueError, match="changed"):
        settlement.main([*args, "--apply"])
    with sqlite3.connect(probe.ledger) as db:
        assert (
            db.execute("SELECT count(*) FROM probe_calls WHERE receipt IS NOT NULL").fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT name FROM sqlite_master WHERE name='probe_aggregate_bounds'"
            ).fetchone()
            is None
        )


def test_aggregate_insert_failure_rolls_back_all_rows(tmp_path):
    probe, _, args = fixture(tmp_path)
    with sqlite3.connect(probe.ledger) as db:
        db.execute(
            "CREATE TABLE probe_aggregate_bounds (id TEXT PRIMARY KEY, committed TEXT, audit TEXT)"
        )
        db.execute(
            "CREATE TRIGGER reject_bound BEFORE INSERT ON probe_aggregate_bounds "
            "BEGIN SELECT RAISE(ABORT,'rejected'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="rejected"):
        settlement.main([*args, "--apply"])
    with sqlite3.connect(probe.ledger) as db:
        assert (
            db.execute("SELECT count(*) FROM probe_calls WHERE receipt IS NOT NULL").fetchone()[0]
            == 0
        )
