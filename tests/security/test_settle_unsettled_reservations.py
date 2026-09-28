import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from proofops.adapters.local.upstage import UpstageProbe

from scripts import settle_unsettled_reservations as settlement
from scripts.settle_unsettled_reservations import main


def test_provider_evidence_dry_run_apply_and_idempotence(tmp_path, capsys):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", ledger)
    body = probe.request_body("JSON", "{}", request_id="one")
    probe._reserve("one", body)
    evidence = tmp_path / "usage.json"
    record = {
        "ledger_request_id": "one",
        "request_body": body,
        "request_timestamp_utc": "2026-09-25T12:00:00Z",
        "provider_timestamp_utc": "2026-09-25T12:00:40Z",
        "provider_request_id": " provider-one ",
        "provider_model": "solar-pro3-260323",
        "billing_status": "billed",
        "input_tokens": 100,
        "output_tokens": 20,
    }
    evidence.write_text(json.dumps([record]))
    before = ledger.read_bytes()
    assert main(["--ledger", str(ledger), "--evidence", str(evidence)]) == 0
    assert ledger.read_bytes() == before
    assert "match (dry-run)" in capsys.readouterr().out
    assert main(["--ledger", str(ledger), "--evidence", str(evidence), "--apply"]) == 0
    with sqlite3.connect(ledger) as db:
        committed, receipt = db.execute("SELECT committed,receipt FROM probe_calls").fetchone()
        assert 0 < float(committed) < 1
        assert json.loads(receipt)["provider_request_id"] == "provider-one"
        assert db.execute("SELECT COUNT(*) FROM probe_settlement_audit").fetchone()[0] == 1
    assert main(["--ledger", str(ledger), "--evidence", str(evidence), "--apply"]) == 0
    assert "settled=0 kept=0" in capsys.readouterr().out


def test_mismatched_evidence_and_explicit_no_charge(tmp_path, capsys):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", ledger)
    body = probe.request_body("JSON", "{}", request_id="one")
    probe._reserve("one", body)
    evidence = tmp_path / "usage.json"
    record = {
        "ledger_request_id": "one",
        "request_body": body,
        "request_timestamp_utc": "2026-09-25T12:00:00Z",
        "provider_timestamp_utc": "2026-09-25T12:00:30Z",
        "provider_request_id": "provider-one",
        "provider_model": "solar-pro3",
        "billing_status": "not_billed",
    }
    for field, value in (
        ("request_body", {**body, "max_tokens": 1}),
        ("provider_model", "solar-pro4"),
        ("provider_timestamp_utc", "2026-09-25T12:10:00Z"),
    ):
        evidence.write_text(json.dumps([{**record, field: value}]))
        assert main(["--ledger", str(ledger), "--evidence", str(evidence), "--apply"]) == 0
        assert "settled=0 kept=1" in capsys.readouterr().out
    evidence.write_text(json.dumps([record]))
    assert main(["--ledger", str(ledger), "--evidence", str(evidence), "--apply"]) == 0
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT committed FROM probe_calls").fetchone()[0] == "0"
        assert db.execute("SELECT COUNT(*) FROM probe_settlement_audit").fetchone()[0] == 1


def test_one_provider_record_cannot_settle_two_reservations(tmp_path, capsys):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", ledger)
    evidence = tmp_path / "usage.json"
    rows = []
    for request_id in ("one", "two"):
        body = probe.request_body("JSON", request_id, request_id=request_id)
        probe._reserve(request_id, body)
        rows.append(
            {
                "ledger_request_id": request_id,
                "request_body": body,
                "request_timestamp_utc": "2026-09-25T12:00:00Z",
                "provider_timestamp_utc": "2026-09-25T12:00:30Z",
                "provider_request_id": "same-provider-id",
                "provider_model": "solar-pro3",
                "billing_status": "not_billed",
            }
        )
    evidence.write_text(json.dumps(rows))
    main(["--ledger", str(ledger), "--evidence", str(evidence), "--apply"])
    assert "settled=0 kept=2" in capsys.readouterr().out


def test_whitespace_variants_of_provider_id_cannot_settle_two_reservations(tmp_path, capsys):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", ledger)
    evidence = tmp_path / "usage.json"
    rows = []
    for request_id, provider_id in (
        ("one", "same-provider-id"),
        ("two", " same-provider-id "),
    ):
        body = probe.request_body("JSON", request_id, request_id=request_id)
        probe._reserve(request_id, body)
        rows.append(
            {
                "ledger_request_id": request_id,
                "request_body": body,
                "request_timestamp_utc": "2026-09-25T12:00:00Z",
                "provider_timestamp_utc": "2026-09-25T12:00:30Z",
                "provider_request_id": provider_id,
                "provider_model": "solar-pro3",
                "billing_status": "not_billed",
            }
        )
    evidence.write_text(json.dumps(rows))

    assert main(["--ledger", str(ledger), "--evidence", str(evidence), "--apply"]) == 0

    assert "settled=0 kept=2" in capsys.readouterr().out
    with sqlite3.connect(ledger) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM probe_calls WHERE receipt IS NOT NULL").fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    "provider_id",
    ["", " \t ", "provider id", "provider\tid", "provider\x00id", "prøvider", "x" * 257],
)
def test_malformed_provider_id_keeps_reservation(tmp_path, capsys, provider_id):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", ledger)
    body = probe.request_body("JSON", "{}", request_id="one")
    probe._reserve("one", body)
    evidence = tmp_path / "usage.json"
    evidence.write_text(
        json.dumps(
            [
                {
                    "ledger_request_id": "one",
                    "request_body": body,
                    "request_timestamp_utc": "2026-09-25T12:00:00Z",
                    "provider_timestamp_utc": "2026-09-25T12:00:30Z",
                    "provider_request_id": provider_id,
                    "provider_model": "solar-pro3",
                    "billing_status": "not_billed",
                }
            ]
        )
    )

    assert main(["--ledger", str(ledger), "--evidence", str(evidence), "--apply"]) == 0

    assert "settled=0 kept=1" in capsys.readouterr().out
    with sqlite3.connect(ledger) as db:
        assert db.execute("SELECT receipt FROM probe_calls").fetchone()[0] is None


@pytest.mark.parametrize("second_provider_id", ["same-provider-id", " same-provider-id "])
def test_concurrent_applies_cannot_reuse_provider_request_id(
    tmp_path, monkeypatch, second_provider_id
):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("offline-key", ledger)
    evidence_files = []
    for request_id, provider_id in (
        ("one", "same-provider-id"),
        ("two", second_provider_id),
    ):
        body = probe.request_body("JSON", request_id, request_id=request_id)
        probe._reserve(request_id, body)
        evidence = tmp_path / f"{request_id}.json"
        evidence.write_text(
            json.dumps(
                [
                    {
                        "ledger_request_id": request_id,
                        "request_body": body,
                        "request_timestamp_utc": "2026-09-25T12:00:00Z",
                        "provider_timestamp_utc": "2026-09-25T12:00:30Z",
                        "provider_request_id": provider_id,
                        "provider_model": "solar-pro3",
                        "billing_status": "not_billed",
                    }
                ]
            )
        )
        evidence_files.append(evidence)
    barrier = Barrier(2)
    original_decide = settlement.decide

    def synchronized_decide(*args):
        result = original_decide(*args)
        barrier.wait(timeout=5)
        return result

    monkeypatch.setattr(settlement, "decide", synchronized_decide)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda evidence: main(
                    ["--ledger", str(ledger), "--evidence", str(evidence), "--apply"]
                ),
                evidence_files,
            )
        )
    assert results == [0, 0]
    with sqlite3.connect(ledger) as db:
        assert (
            db.execute("SELECT COUNT(*) FROM probe_calls WHERE receipt IS NOT NULL").fetchone()[0]
            == 1
        )
        assert db.execute("SELECT COUNT(*) FROM probe_settlement_audit").fetchone()[0] == 1
        existing = db.execute("SELECT receipt FROM probe_settlement_audit").fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO probe_settlement_audit VALUES (?,?,?,?)",
                ("duplicate", "2026-09-25T12:00:00Z", "hash", existing),
            )
        duplicate_variant = json.loads(existing)
        duplicate_variant["provider_request_id"] = " same-provider-id "
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO probe_settlement_audit VALUES (?,?,?,?)",
                (
                    "duplicate-variant",
                    "2026-09-25T12:00:00Z",
                    "hash",
                    json.dumps(duplicate_variant),
                ),
            )
