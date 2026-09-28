"""Local-synthetic-only TASK-030 usage persistence, in the catalog adapter path.

No AWS SDK/client is configured here. Durable SQLite behavior is tested locally;
DynamoDB transactions, actual provider usage and price retrieval remain not_run.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from proofops.application.budget import (
    BudgetCall,
    BudgetConflict,
    BudgetExceeded,
    BudgetLimits,
    PricingSnapshot,
    TokenUsage,
    count,
    text_field,
    usage_cost,
)
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_uuid


class LocalSQLiteUsageStore:
    kind = "local-synthetic-only"

    def __init__(self, path: str | Path) -> None:
        if str(path) == ":memory:":
            raise ValueError("durable reservations require a file-backed database")
        self.path = str(path)
        with self._transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS usage_schema (version INTEGER PRIMARY KEY)")
            versions = db.execute("SELECT version FROM usage_schema").fetchall()
            if versions and versions != [(1,)]:
                raise BudgetConflict("unsupported usage schema version")
            db.execute("INSERT OR IGNORE INTO usage_schema VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS usage_prices (
                snapshot_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS usage_runs (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, document_version_id TEXT NOT NULL,
                limits_json TEXT NOT NULL, PRIMARY KEY (tenant_id, run_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS usage_attempts (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, request_id TEXT NOT NULL,
                attempt INTEGER NOT NULL, reservation TEXT NOT NULL, state TEXT NOT NULL,
                ledger TEXT, release_json TEXT,
                PRIMARY KEY (tenant_id, run_id, request_id, attempt))""")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # ponytail: SQLite serializes writers; use DynamoDB transactions for distributed usage.
        db = sqlite3.connect(self.path, isolation_level=None, timeout=10)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _json(value) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _scope(tenant, run) -> None:
        _require_uuid("tenant_id", tenant)
        _require_uuid("run_id", run)

    def _run(self, db, tenant, run):
        self._scope(tenant, run)
        row = db.execute(
            "SELECT document_version_id, limits_json FROM usage_runs "
            "WHERE tenant_id=? AND run_id=?",
            (tenant, run),
        ).fetchone()
        if row is None:
            raise KeyError("run not found")
        return row[0], json.loads(row[1])

    def create_budget(
        self, tenant_id: str, run_id: str, document_version_id: str, limits: BudgetLimits
    ) -> None:
        with self._transaction() as db:
            self.create_budget_transaction(db, tenant_id, run_id, document_version_id, limits)

    def create_budget_transaction(self, db, tenant_id, run_id, document_version_id, limits):
        """Join atomic run creation; never begin or commit a nested transaction."""
        if not db.in_transaction:
            raise ValueError("active transaction required")
        self._scope(tenant_id, run_id)
        _require_uuid("document_version_id", document_version_id)
        if not isinstance(limits, BudgetLimits):
            raise ValueError("typed immutable limits required")
        policy = self._json(asdict(limits))
        previous = db.execute(
            "SELECT document_version_id, limits_json FROM usage_runs "
            "WHERE tenant_id=? AND run_id=?",
            (tenant_id, run_id),
        ).fetchone()
        if previous is not None:
            if tuple(previous) != (document_version_id, policy):
                raise BudgetConflict("immutable run budget differs; automatic increases forbidden")
            return
        db.execute(
            "INSERT INTO usage_runs VALUES (?, ?, ?, ?)",
            (tenant_id, run_id, document_version_id, policy),
        )

    @staticmethod
    def _key(call) -> tuple:
        return call.tenant_id, call.run_id, call.request_id, call.attempt

    def _attempt(self, db, call):
        row = db.execute(
            "SELECT reservation, state, ledger FROM usage_attempts "
            "WHERE tenant_id=? AND run_id=? AND request_id=? AND attempt=?",
            self._key(call),
        ).fetchone()
        if row is None:
            raise KeyError("reservation not found")
        reservation = json.loads(row[0])
        if reservation["call"] != asdict(call):
            raise BudgetConflict("reservation identity differs")
        return reservation, row[1], None if row[2] is None else json.loads(row[2])

    @staticmethod
    def _records(db, tenant, run) -> list[dict[str, Any]]:
        return [
            {
                "reservation": json.loads(row[0]),
                "state": row[1],
                "ledger": None if row[2] is None else json.loads(row[2]),
                "release": None if row[3] is None else json.loads(row[3]),
            }
            for row in db.execute(
                "SELECT reservation, state, ledger, release_json FROM usage_attempts "
                "WHERE tenant_id=? AND run_id=? ORDER BY request_id, attempt",
                (tenant, run),
            )
        ]

    def reserve_budget(
        self,
        call: BudgetCall,
        *,
        input_tokens: int,
        max_output_tokens: int,
        pricing: PricingSnapshot | None,
        now: int,
    ) -> bool:
        count("input_tokens", input_tokens)
        count("max_output_tokens", max_output_tokens, 1)
        count("now", now)
        if pricing is not None and (pricing.model_id, pricing.region) != (
            call.model_id,
            call.region,
        ):
            raise ValueError("pricing snapshot must match the bound model and region")
        price = None if pricing is None else pricing.to_dict()
        candidate = {
            "call": asdict(call),
            "input_tokens": input_tokens,
            "max_output_tokens": max_output_tokens,
            "pricing": price,
        }
        with self._transaction() as db:
            version, limits = self._run(db, call.tenant_id, call.run_id)
            if version != call.document_version_id:
                raise KeyError("run not found")
            role = next((role for role in limits["roles"] if role["role"] == call.role), None)
            if (
                role is None
                or input_tokens > role["max_input_tokens"]
                or (
                    max_output_tokens > role["max_output_tokens"]
                    or input_tokens + max_output_tokens > role["max_context_tokens"]
                )
            ):
                raise BudgetExceeded("BUDGET_EXHAUSTED: role/context/output limit")
            rows = self._records(db, call.tenant_id, call.run_id)
            for row in rows:
                existing = row["reservation"]
                if existing["call"]["request_id"] == call.request_id:
                    identity = dict(
                        existing["call"],
                        attempt=call.attempt,
                        request_signature=call.request_signature,
                    )
                    if identity != asdict(call):
                        raise BudgetConflict("request identity cannot change between attempts")
                    if existing["call"]["attempt"] == call.attempt:
                        original = {k: v for k, v in existing.items() if k != "created_at"}
                        if original != candidate:
                            raise BudgetConflict("immutable reservation differs")
                        return False
            self._check_provider_overrun(rows)
            prior_attempts = [
                row["reservation"]["call"]["attempt"]
                for row in rows
                if row["reservation"]["call"]["request_id"] == call.request_id
            ]
            if call.attempt > limits["max_attempts"]:
                raise BudgetExceeded("BUDGET_EXHAUSTED: request attempt limit")
            if call.attempt != max(prior_attempts, default=0) + 1:
                raise BudgetConflict("attempts must be consecutive")
            active = [row for row in rows if row["state"] != "released"]
            if (
                sum(row["reservation"]["call"]["role"] == call.role for row in active)
                >= role["max_calls"]
            ):
                raise BudgetExceeded("BUDGET_EXHAUSTED: role call limit")
            if prior_attempts:
                previous = next(
                    row
                    for row in rows
                    if row["reservation"]["call"]["request_id"] == call.request_id
                    and row["reservation"]["call"]["attempt"] == call.attempt - 1
                )
                signature_changed = (
                    previous["reservation"]["call"]["request_signature"] != call.request_signature
                )
                schema_repair = (
                    previous["ledger"] is not None
                    and previous["ledger"]["usage"]["error_code"] == "LLM_SCHEMA_INVALID"
                )
                if signature_changed and not schema_repair:
                    raise BudgetConflict(
                        "only schema repair may change the retry request signature"
                    )
                if previous["state"] != "released":
                    prior_usage = (
                        None if previous["ledger"] is None else previous["ledger"]["usage"]
                    )
                    if (
                        previous["state"] != "settled"
                        or prior_usage is None
                        or prior_usage["status"] == "succeeded"
                        or prior_usage["error_code"]
                        not in {
                            "MODEL_THROTTLED",
                            "PROVIDER_5XX",
                            "MODEL_TIMEOUT",
                            "LLM_SCHEMA_INVALID",
                        }
                    ):
                        raise BudgetConflict("previous attempt is not settled and retryable")
                    if (
                        prior_usage["error_code"] == "LLM_SCHEMA_INVALID"
                        and sum(
                            row["ledger"] is not None
                            and row["reservation"]["call"]["request_id"] == call.request_id
                            and row["ledger"]["usage"]["error_code"] == "LLM_SCHEMA_INVALID"
                            for row in rows
                        )
                        > 1
                    ):
                        raise BudgetConflict("schema repair is limited to one attempt")
            for name, requested in (
                ("input_tokens", input_tokens),
                ("output_tokens", max_output_tokens),
            ):
                used = 0
                for row in active:
                    actual = None if row["ledger"] is None else row["ledger"]["usage"][name]
                    reserved = row["reservation"][
                        "input_tokens" if name == "input_tokens" else "max_output_tokens"
                    ]
                    used += reserved if actual is None else actual
                if used >= limits[name] or used + requested > limits[name]:
                    raise BudgetExceeded(f"BUDGET_EXHAUSTED: {name}; partial_no_silent_sampling")
            candidate["created_at"] = now
            if price is not None:
                encoded_price = self._json(price)
                old_price = db.execute(
                    "SELECT payload_json FROM usage_prices WHERE snapshot_id=?",
                    (price["snapshot_id"],),
                ).fetchone()
                if old_price is not None and old_price[0] != encoded_price:
                    raise BudgetConflict("pricing snapshot ID has different immutable content")
                db.execute(
                    "INSERT OR IGNORE INTO usage_prices VALUES (?, ?)",
                    (price["snapshot_id"], encoded_price),
                )
            db.execute(
                "INSERT INTO usage_attempts VALUES (?, ?, ?, ?, ?, 'reserved', NULL, NULL)",
                (*self._key(call), self._json(candidate)),
            )
            return True

    @staticmethod
    def _check_provider_overrun(rows) -> None:
        # Retain actual usage; a broken count/output bound stops further spend.
        for row in rows:
            if row["ledger"] is None:
                continue
            for actual_key, reserved_key in (
                ("input_tokens", "input_tokens"),
                ("output_tokens", "max_output_tokens"),
            ):
                actual = row["ledger"]["usage"][actual_key]
                if actual is not None and actual > row["reservation"][reserved_key]:
                    raise BudgetExceeded("BUDGET_EXHAUSTED: provider usage exceeded reservation")

    def mark_dispatched(self, call: BudgetCall) -> bool:
        """CAS once immediately before provider invocation; never replay a dispatched attempt."""
        with self._transaction() as db:
            _, state, _ = self._attempt(db, call)
            if state != "reserved":
                return False
            self._check_provider_overrun(self._records(db, call.tenant_id, call.run_id))
            db.execute(
                "UPDATE usage_attempts SET state='dispatched' "
                "WHERE tenant_id=? AND run_id=? AND request_id=? AND attempt=?",
                self._key(call),
            )
            return True

    def release_budget(
        self, call: BudgetCall, *, reason: str, now: int, response_cache_hit: bool = False
    ) -> None:
        """Compensate only a reservation proven not dispatched; timeouts cannot refund usage."""
        text_field("reason", reason)
        count("now", now)
        if not isinstance(response_cache_hit, bool):
            raise ValueError("response_cache_hit must be a bool")
        with self._transaction() as db:
            _, state, _ = self._attempt(db, call)
            if state == "released":
                previous = json.loads(
                    db.execute(
                        "SELECT release_json FROM usage_attempts WHERE tenant_id=? AND run_id=? "
                        "AND request_id=? AND attempt=?",
                        self._key(call),
                    ).fetchone()[0]
                )
                if previous["reason"] != reason or (
                    previous.get("response_cache_hit", False) != response_cache_hit
                ):
                    raise BudgetConflict("immutable release metadata differs")
                return
            if state != "reserved":
                raise BudgetConflict("dispatched usage is unknown; record it without refunding")
            db.execute(
                "UPDATE usage_attempts SET state='released', release_json=? "
                "WHERE tenant_id=? AND run_id=? AND request_id=? AND attempt=?",
                (
                    self._json(
                        {
                            "reason": reason,
                            "released_at": now,
                            "response_cache_hit": response_cache_hit,
                        }
                    ),
                    *self._key(call),
                ),
            )

    def record_usage(self, call: BudgetCall, usage: TokenUsage, *, now: int) -> dict[str, Any]:
        count("now", now)
        if not isinstance(usage, TokenUsage):
            raise ValueError("typed provider usage required")
        with self._transaction() as db:
            reservation, state, previous = self._attempt(db, call)
            if previous is not None:
                if previous["usage"] != asdict(usage):
                    raise BudgetConflict("immutable usage differs")
                return previous
            if state != "dispatched":
                raise BudgetConflict("usage requires a dispatched reservation")
            price = reservation["pricing"]
            ledger = {
                "call": asdict(call),
                "usage": asdict(usage),
                "recorded_at": now,
                "cost_decimal": usage_cost(usage, price),
                "pricing_snapshot_id": None if price is None else price["snapshot_id"],
                "pricing_snapshot_hash": None if price is None else canonical_hash(price),
                "execution_profile": self.kind,
            }
            db.execute(
                "UPDATE usage_attempts SET state='settled', ledger=? "
                "WHERE tenant_id=? AND run_id=? AND request_id=? AND attempt=?",
                (self._json(ledger), *self._key(call)),
            )
            return ledger

    def cost_data(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        with self._transaction() as db:
            self._run(db, tenant_id, run_id)
            return self._records(db, tenant_id, run_id)

    def ledger(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]:
        return [
            row["ledger"] for row in self.cost_data(tenant_id, run_id) if row["ledger"] is not None
        ]
