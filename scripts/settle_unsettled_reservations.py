"""Reconcile reserved Upstage calls from a user-supplied provider usage/billing export.

No network calls; dry-run is the default and opens the ledger read-only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from proofops.adapters.local.upstage import (
    MODEL,
    MODEL_PRO4,
    POLICY,
    PRICE,
    PRICE_PRO4,
    UpstageProbe,
)
from proofops.application.budget import TokenUsage, usage_cost
from proofops.domain.provenance import canonical_hash

PROVIDER_ID_SQL_KEY = (
    "trim(json_extract(receipt, '$.provider_request_id'), "
    "char(9,10,11,12,13,28,29,30,31,32,133,160,5760,8192,8193,8194,8195,8196,8197,"
    "8198,8199,8200,8201,8202,8232,8233,8239,8287,12288))"
)
AGGREGATE_COLUMNS = [
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


def _money(value: str) -> Decimal:
    try:
        amount = Decimal(value.strip().removeprefix("$").replace(",", ""))
    except (AttributeError, InvalidOperation):
        raise ValueError("aggregate CSV amount invalid") from None
    if not amount.is_finite() or amount < 0:
        raise ValueError("aggregate CSV amount invalid")
    return amount


def parse_aggregate_csv(
    evidence: bytes, start: date, end: date, key_scope: str
) -> tuple[Decimal, date]:
    if key_scope != "all":
        raise ValueError(
            "aggregate key scope must be 'all'; " "ledger API key identity is unavailable"
        )
    reader = csv.DictReader(io.StringIO(evidence.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != AGGREGATE_COLUMNS:
        raise ValueError("aggregate CSV columns invalid")
    rows = list(reader)
    if not rows or any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("aggregate CSV shape invalid")
    selected = Decimal(0)
    first_usage_date: date | None = None
    for row in rows:
        if not all(
            row[column].strip()
            for column in (
                "date",
                "api_key_id",
                "api_key_name",
                "usage_type",
                "billing_source",
                "product",
                "pricing_options",
                "quantity",
                "item_cost",
                "used_credit",
                "subtotal",
            )
        ):
            raise ValueError("aggregate CSV usage row incomplete")
        try:
            day = date.fromisoformat(row["date"])
            quantity = Decimal(row["quantity"].replace(",", ""))
        except (InvalidOperation, ValueError):
            raise ValueError("aggregate CSV usage row invalid") from None
        if not quantity.is_finite() or quantity < 0 or not start <= day <= end:
            raise ValueError("aggregate CSV usage outside window")
        first_usage_date = day if first_usage_date is None else min(first_usage_date, day)
        if row["usage_type"] != "api" or row["billing_source"] != "payg":
            raise ValueError("aggregate CSV usage type invalid")
        cost = _money(row["item_cost"])
        _money(row["used_credit"])
        _money(row["subtotal"])
        selected += cost
    if first_usage_date is None:
        raise ValueError("aggregate CSV has no usage date")
    return selected, first_usage_date


def earliest_evidence_time(db: sqlite3.Connection, ledger: Path) -> datetime:
    evidence: list[datetime] = []

    def add(raw: object) -> None:
        if not isinstance(raw, str):
            raise ValueError("ledger evidence timestamp invalid")
        try:
            evidence.append(utc(raw))
        except ValueError:
            raise ValueError("ledger evidence timestamp invalid") from None

    policy_row = db.execute("SELECT body FROM probe_policy WHERE id=1").fetchone()
    try:
        policy = json.loads(policy_row[0])
        captured_at = policy["price"]["captured_at"]
    except (IndexError, KeyError, TypeError, ValueError):
        raise ValueError("ledger earliest evidence cannot be established") from None
    add(captured_at)

    for (authorized_at,) in db.execute("SELECT authorized_at FROM probe_extensions"):
        add(authorized_at)

    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('probe_calls', 'probe_settlement_audit', 'probe_aggregate_bounds')"
        )
    }
    timestamped_requests: set[str] = set()
    request_ids: set[str] = {row[0] for row in db.execute("SELECT request_id FROM probe_calls")}
    for table in ("probe_calls", "probe_settlement_audit"):
        if table not in tables:
            continue
        columns = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
        if "receipt" in columns:
            for request_id, raw in db.execute(
                f"SELECT request_id, receipt FROM {table} WHERE receipt IS NOT NULL"
            ):
                try:
                    receipt = json.loads(raw)
                except (TypeError, ValueError):
                    raise ValueError("ledger receipt timestamp invalid") from None
                if not isinstance(receipt, dict):
                    raise ValueError("ledger receipt timestamp invalid")
                for key in ("request_timestamp_utc", "provider_timestamp_utc"):
                    if key in receipt:
                        add(receipt[key])
                        timestamped_requests.add(request_id)
                price = receipt.get("price_snapshot")
                if isinstance(price, dict) and "captured_at" in price:
                    add(price["captured_at"])
        if "settled_at" in columns:
            for (settled_at,) in db.execute(
                f"SELECT settled_at FROM {table} WHERE settled_at IS NOT NULL"
            ):
                add(settled_at)

    if "probe_aggregate_bounds" in tables:
        for (raw,) in db.execute("SELECT audit FROM probe_aggregate_bounds"):
            try:
                audit = json.loads(raw)
            except (TypeError, ValueError):
                raise ValueError("ledger aggregate audit timestamp invalid") from None
            if not isinstance(audit, dict):
                raise ValueError("ledger aggregate audit timestamp invalid")
            if "earliest_evidence_at" in audit:
                add(audit["earliest_evidence_at"])
            if "window_start" in audit:
                try:
                    window_start = date.fromisoformat(audit["window_start"])
                except (TypeError, ValueError):
                    raise ValueError("ledger aggregate audit timestamp invalid") from None
                evidence.append(datetime.combine(window_start, datetime.min.time(), UTC))

    response_dir = ledger.with_name(ledger.name + ".responses")
    if response_dir.is_dir():
        response_ids = {canonical_hash(request_id): request_id for request_id in request_ids}
        for path in response_dir.glob("*.json"):
            request_id = response_ids.get(path.stem)
            if request_id is None:
                continue
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                if request_id not in timestamped_requests:
                    raise ValueError("ledger response timestamp cannot be established") from None
                continue
            if not isinstance(saved, dict) or saved.get("request_id") != request_id:
                if request_id not in timestamped_requests:
                    raise ValueError("ledger response timestamp cannot be established")
                continue
            response = saved.get("provider_response")
            if not isinstance(response, dict):
                if request_id not in timestamped_requests:
                    raise ValueError("ledger response timestamp cannot be established")
                continue
            created = response.get("created")
            if type(created) is int and created >= 0:
                try:
                    evidence.append(datetime.fromtimestamp(created, UTC))
                except (OverflowError, OSError, ValueError):
                    raise ValueError("ledger response timestamp invalid") from None
            elif request_id not in timestamped_requests:
                raise ValueError("ledger response timestamp cannot be established")

    try:
        stat = ledger.stat()
        birthtime = getattr(stat, "st_birthtime", None)
        if isinstance(birthtime, int | float) and not isinstance(birthtime, bool):
            evidence.append(datetime.fromtimestamp(birthtime, UTC))
    except (OSError, OverflowError, ValueError):
        raise ValueError("ledger creation time cannot be established") from None

    if not evidence:
        raise ValueError("ledger earliest evidence cannot be established")
    return min(evidence)


def aggregate_bound(args: argparse.Namespace) -> int:
    if not args.window_start or not args.window_end or not args.key_scope:
        raise ValueError("aggregate mode requires window start/end and key scope")
    try:
        start, end = date.fromisoformat(args.window_start), date.fromisoformat(args.window_end)
    except ValueError:
        raise ValueError("window dates must be UTC YYYY-MM-DD") from None
    if end < start:
        raise ValueError("window end precedes start")
    if args.key_scope != "all":
        raise ValueError(
            "aggregate key scope must be 'all'; " "ledger API key identity is unavailable"
        )
    evidence = args.aggregate_bound.read_bytes()
    evidence_hash = hashlib.sha256(evidence).hexdigest()
    address = args.ledger.resolve().as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
    with sqlite3.connect(address, uri=True, timeout=10) as db:
        limit = UpstageProbe._authorized_limit(db)
        before = UpstageProbe._call_total(db)
        rows = db.execute(
            "SELECT request_id,signature,committed FROM probe_calls "
            "WHERE receipt IS NULL ORDER BY request_id"
        ).fetchall()
        for _, _, committed in rows:
            if Decimal(committed) != Decimal(POLICY["reservation_usd"]):
                raise ValueError("reservation amount changed")
        amount, csv_first_usage_date = parse_aggregate_csv(evidence, start, end, args.key_scope)
        earliest = earliest_evidence_time(db, args.ledger)
        if start > earliest.date():
            raise ValueError("window does not cover ledger earliest evidence")
        if not start <= csv_first_usage_date <= end:
            raise ValueError("aggregate CSV earliest usage outside window")
        prior = (
            db.execute("SELECT audit FROM probe_aggregate_bounds").fetchall()
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='probe_aggregate_bounds'"
            ).fetchone()
            else []
        )
        for (raw,) in prior:
            audit = json.loads(raw)
            if (
                audit["evidence_sha256"],
                audit["window_start"],
                audit["window_end"],
                audit["key_scope"],
                audit["total_usd"],
            ) == (evidence_hash, start.isoformat(), end.isoformat(), args.key_scope, str(amount)):
                if not rows:
                    print("aggregate bound already applied")
                    return 0
                raise ValueError("aggregate evidence already used for earlier reservations")
        if not rows:
            print("no unsettled reservations")
            return 0
        if end < datetime.fromtimestamp(args.ledger.stat().st_mtime, UTC).date():
            raise ValueError("window does not cover ledger last modification")
        ids = [row[0] for row in rows]
        audit = {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "key_scope": args.key_scope,
            "evidence_sha256": evidence_hash,
            "total_usd": str(amount),
            "earliest_evidence_at": earliest.isoformat().replace("+00:00", "Z"),
            "csv_first_usage_date": csv_first_usage_date.isoformat(),
            "affected_request_ids": ids,
        }
        bound_id = "aggregate:" + canonical_hash(audit)
        after = before - sum((Decimal(row[2]) for row in rows), Decimal(0)) + amount
        print(f"window={start}..{end} UTC key_scope={args.key_scope} affected={len(rows)}")
        print(
            f"before={before} headroom={limit - before} bound={amount} "
            f"after={after} headroom_after={limit - after}"
        )
        if not args.apply:
            print("dry-run; ledger unchanged")
            return 0
        db.execute("BEGIN IMMEDIATE")
        locked_limit = UpstageProbe._authorized_limit(db)
        current = db.execute(
            "SELECT request_id,signature,committed FROM probe_calls "
            "WHERE receipt IS NULL ORDER BY request_id"
        ).fetchall()
        if current != rows:
            raise ValueError("affected reservations changed")
        locked_before = UpstageProbe._call_total(db)
        locked_after = (
            locked_before - sum((Decimal(row[2]) for row in current), Decimal(0)) + amount
        )
        if locked_after > locked_limit:
            raise ValueError("BUDGET_EXHAUSTED")
        locked_earliest = earliest_evidence_time(db, args.ledger)
        if locked_earliest != earliest:
            raise ValueError("ledger evidence changed during settlement")
        db.execute(
            "CREATE TABLE IF NOT EXISTS probe_aggregate_bounds ("
            "id TEXT PRIMARY KEY, committed TEXT NOT NULL, audit TEXT NOT NULL)"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS probe_aggregate_bounds_no_update "
            "BEFORE UPDATE ON probe_aggregate_bounds BEGIN SELECT RAISE(ABORT,'immutable'); END"
        )
        db.execute(
            "CREATE TRIGGER IF NOT EXISTS probe_aggregate_bounds_no_delete "
            "BEFORE DELETE ON probe_aggregate_bounds BEGIN SELECT RAISE(ABORT,'immutable'); END"
        )
        for request_id, signature, committed in rows:
            receipt = json.dumps(
                {
                    "settlement_origin": "provider_aggregate_upper_bound",
                    "aggregate_bound_id": bound_id,
                    "cost_with_vat_reserve_usd": "0",
                },
                sort_keys=True,
            )
            changed = db.execute(
                "UPDATE probe_calls SET committed='0',receipt=? "
                "WHERE request_id=? AND signature=? AND committed=? AND receipt IS NULL",
                (receipt, request_id, signature, committed),
            ).rowcount
            if changed != 1:
                raise ValueError("affected reservations changed")
        db.execute(
            "INSERT INTO probe_aggregate_bounds VALUES (?,?,?)",
            (bound_id, str(amount), json.dumps(audit, sort_keys=True)),
        )
        db.commit()
        print("aggregate bound applied")
    return 0


def canonical_provider_id(value: object) -> str:
    """Trim ends; keep case; require 1-256 printable ASCII non-whitespace chars."""
    if not isinstance(value, str):
        raise ValueError("provider request ID malformed")
    value = value.strip()
    if (
        not value
        or len(value) > 256
        or any(not char.isascii() or not char.isprintable() or char.isspace() for char in value)
    ):
        raise ValueError("provider request ID malformed")
    return value


def stored_provider_ids(db: sqlite3.Connection) -> set[str]:
    ids = set()
    tables = {
        row[0]
        for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('probe_calls', 'probe_settlement_audit')"
        )
    }
    for table in tables:
        for (raw,) in db.execute(f"SELECT receipt FROM {table} WHERE receipt IS NOT NULL"):
            receipt = json.loads(raw)
            if not isinstance(receipt, dict):
                raise ValueError("stored receipt malformed")
            provider_id = receipt.get("provider_request_id")
            if provider_id is not None:
                ids.add(canonical_provider_id(provider_id))
    return ids


def provider_id_is_used(db: sqlite3.Connection, provider_id: str) -> bool:
    return any(
        db.execute(
            f"SELECT 1 FROM {table} WHERE receipt IS NOT NULL AND {PROVIDER_ID_SQL_KEY}=? LIMIT 1",
            (provider_id,),
        ).fetchone()
        for table in ("probe_calls", "probe_settlement_audit")
    )


def records(path: Path) -> list[dict]:
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as stream:
            return list(csv.DictReader(stream))
    data = json.loads(path.read_text(encoding="utf-8"))
    result = data["records"] if isinstance(data, dict) else data
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise ValueError("EVIDENCE_FORMAT_INVALID")
    return result


def utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError("timestamp must be UTC")
    return parsed.astimezone(UTC)


def decide(request_id: str, signature: str, entry: dict) -> tuple[Decimal, dict]:
    """Require an original body hash, model, provider identity and close UTC times."""
    if entry.get("ledger_request_id") != request_id:
        raise ValueError("request ID mismatch")
    body = entry.get("request_body")
    if isinstance(body, str):  # CSV cell containing JSON
        body = json.loads(body)
    if not isinstance(body, dict) or canonical_hash(body) != signature:
        raise ValueError("request signature mismatch")
    model = body.get("model")
    aliases = {
        MODEL: {MODEL, "solar-pro3-260323"},
        MODEL_PRO4: {MODEL_PRO4, "solar-pro4-260806"},
    }
    if model not in aliases or entry.get("provider_model") not in aliases[model]:
        raise ValueError("provider model mismatch")
    seconds = (
        utc(entry.get("provider_timestamp_utc")) - utc(entry.get("request_timestamp_utc"))
    ).total_seconds()
    if abs(seconds) > 300:
        raise ValueError("timestamp outside five-minute window")
    provider_id = canonical_provider_id(entry.get("provider_request_id"))
    status = entry.get("billing_status")
    if status == "not_billed":
        cost = Decimal(0)
        usage = {}
    elif status == "billed":
        try:
            input_tokens = int(entry["input_tokens"])
            output_tokens = int(entry["output_tokens"])
            if (
                str(input_tokens) != str(entry["input_tokens"])
                or str(output_tokens) != str(entry["output_tokens"])
                or input_tokens < 0
                or output_tokens < 0
            ):
                raise ValueError
            price = PRICE if model == MODEL else PRICE_PRO4
            tokens = TokenUsage(input_tokens, output_tokens, 0, 0, 0, "succeeded", provider_id)
            cost = Decimal(usage_cost(tokens, price.to_dict())) * Decimal(POLICY["vat_allowance"])
            usage = {"input_tokens": input_tokens, "output_tokens": output_tokens}
        except (KeyError, TypeError, InvalidOperation, ValueError):
            raise ValueError("usage tokens invalid") from None
        if cost > Decimal(POLICY["reservation_usd"]):
            raise ValueError("cost exceeds reservation")
    else:
        raise ValueError("billing status must be billed or not_billed")
    receipt = {
        "model": model,
        "provider_model": entry["provider_model"],
        "provider_request_id": provider_id,
        "request_signature": signature,
        "request_timestamp_utc": entry["request_timestamp_utc"],
        "provider_timestamp_utc": entry["provider_timestamp_utc"],
        "billing_status": status,
        "price_snapshot": (PRICE if model == MODEL else PRICE_PRO4).to_dict(),
        "cost_with_vat_reserve_usd": str(cost),
        "settlement_origin": "provider_export_reconciliation",
        **usage,
    }
    return cost, receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", required=True, type=Path)
    evidence = parser.add_mutually_exclusive_group(required=True)
    evidence.add_argument("--evidence", type=Path, help="per-request provider CSV or JSON export")
    evidence.add_argument("--aggregate-bound", type=Path, help="daily provider usage CSV")
    parser.add_argument("--window-start", help="aggregate UTC first date, inclusive")
    parser.add_argument("--window-end", help="aggregate UTC last date, inclusive")
    parser.add_argument("--key-scope", help="aggregate CSV key label, or all")
    parser.add_argument(
        "--apply", action="store_true", help="settle matched rows with audit entries"
    )
    args = parser.parse_args(argv)
    if args.aggregate_bound:
        return aggregate_bound(args)
    evidence_bytes = args.evidence.read_bytes()
    evidence_hash = hashlib.sha256(evidence_bytes).hexdigest()
    entries = records(args.evidence)
    by_id: dict[str, list[dict]] = {}
    provider_ids: dict[str, int] = {}
    for entry in entries:
        by_id.setdefault(str(entry.get("ledger_request_id", "")), []).append(entry)
        try:
            provider_id = canonical_provider_id(entry.get("provider_request_id"))
        except ValueError:
            continue
        else:
            provider_ids[provider_id] = provider_ids.get(provider_id, 0) + 1
    address = args.ledger.resolve().as_uri() + ("?mode=rw" if args.apply else "?mode=ro")
    settled = 0
    kept = 0
    with sqlite3.connect(address, uri=True, timeout=10) as db:
        UpstageProbe._authorized_limit(db)
        rows = db.execute(
            "SELECT request_id,signature,committed FROM probe_calls "
            "WHERE receipt IS NULL ORDER BY request_id"
        ).fetchall()
        prior_provider_ids = stored_provider_ids(db)
        print("request_id | decision | cost_usd | reason")
        for request_id, signature, committed in rows:
            candidates = by_id.get(request_id, [])
            if len(candidates) != 1:
                reason = "missing" if not candidates else "ambiguous"
                print(f"{request_id} | keep | - | {reason} evidence")
                kept += 1
                continue
            entry = candidates[0]
            try:
                if Decimal(committed) != Decimal(POLICY["reservation_usd"]):
                    raise ValueError("reservation amount mismatch")
                cost, receipt = decide(request_id, signature, entry)
                provider_id = receipt["provider_request_id"]
                if provider_ids.get(provider_id) != 1 or provider_id in prior_provider_ids:
                    raise ValueError("provider request ID reused")
                saved = args.ledger.with_name(args.ledger.name + ".responses") / (
                    canonical_hash(request_id) + ".json"
                )
                if saved.exists():
                    provider = json.loads(saved.read_text())["provider_response"]
                    if (
                        canonical_provider_id(provider["id"]) != provider_id
                        or provider["model"] != receipt["provider_model"]
                    ):
                        raise ValueError("saved provider response mismatch")
                receipt["evidence_sha256"] = evidence_hash
                receipt["evidence_record_sha256"] = canonical_hash(entry)
            except (KeyError, TypeError, InvalidOperation, ValueError) as exc:
                print(f"{request_id} | keep | - | {exc}")
                kept += 1
                continue
            if args.apply:
                db.execute("BEGIN IMMEDIATE")
                UpstageProbe._authorized_limit(db)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS probe_settlement_audit ("
                    "request_id TEXT PRIMARY KEY, settled_at TEXT NOT NULL, "
                    "evidence_sha256 TEXT NOT NULL, receipt TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS settlement_provider_request_id_unique "
                    "ON probe_settlement_audit(json_extract(receipt, '$.provider_request_id'))"
                )
                db.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS settlement_provider_id_canonical_unique "
                    f"ON probe_settlement_audit({PROVIDER_ID_SQL_KEY})"
                )
                if provider_id_is_used(db, provider_id):
                    db.rollback()
                    print(f"{request_id} | keep | - | provider request ID reused")
                    kept += 1
                    continue
                payload = json.dumps(receipt, sort_keys=True)
                changed = db.execute(
                    "UPDATE probe_calls SET committed=?,receipt=? "
                    "WHERE request_id=? AND signature=? AND receipt IS NULL AND committed=?",
                    (str(cost), payload, request_id, signature, committed),
                ).rowcount
                if changed != 1:
                    db.rollback()
                    print(f"{request_id} | keep | - | reservation changed concurrently")
                    kept += 1
                    continue
                db.execute(
                    "INSERT INTO probe_settlement_audit VALUES (?,?,?,?)",
                    (request_id, datetime.now(UTC).isoformat(), evidence_hash, payload),
                )
                db.commit()
                prior_provider_ids.add(provider_id)
                settled += 1
            else:
                kept += 1
            decision = "settled" if args.apply else "match (dry-run)"
            print(f"{request_id} | {decision} | {cost} | {receipt['billing_status']}")
    print(f"settled={settled} kept={kept}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
