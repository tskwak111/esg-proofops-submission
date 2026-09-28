"""AT-030: synthetic prices/tokens, real durable reservation and settlement."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from proofops.adapters.aws.usage import LocalSQLiteUsageStore
from proofops.application.budget import (
    BudgetCall,
    BudgetConflict,
    BudgetExceeded,
    BudgetLimits,
    PricingSnapshot,
    RoleLimit,
    TokenUsage,
    cost_summary,
    record_usage,
    reserve_budget,
)

TENANT, OTHER, RUN, VERSION = (str(UUID(int=i)) for i in range(1, 5))
CALL = BudgetCall(
    TENANT,
    RUN,
    VERSION,
    "request-1",
    1,
    "tagger",
    "synthetic-model",
    "synthetic-region",
    "a" * 64,
    "b" * 64,
    1,
)
LIMITS = BudgetLimits(1000, 1000, (RoleLimit("tagger", 10, 1000, 1000, 2000),), 3)
PRICE = PricingSnapshot(
    "synthetic-price-v1",
    "synthetic-model",
    "synthetic-region",
    "2026-09-09T00:00:00Z",
    Decimal("1"),
    Decimal("2"),
    Decimal("0.1"),
    Decimal("1.25"),
)
USAGE = TokenUsage(100, 20, 10, 0, 25, "succeeded", "synthetic-response-1")


def seeded(path: Path, limits: BudgetLimits = LIMITS) -> LocalSQLiteUsageStore:
    store = LocalSQLiteUsageStore(path)
    store.create_budget(TENANT, RUN, VERSION, limits)
    return store


def reserve(store, call=CALL, *, price=PRICE, input_tokens=100, max_output_tokens=100):
    created = reserve_budget(
        store,
        call,
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        pricing=price,
        now=1,
    )
    if created:
        assert store.mark_dispatched(call)
    return created


def test_unset_price_is_unknown_cost_not_zero_and_matches_api_contract(tmp_path):
    import json

    from jsonschema import Draft202012Validator, FormatChecker

    store = seeded(tmp_path / "usage.sqlite")
    assert reserve(store, price=None)
    record_usage(store, CALL, USAGE, now=2)
    summary = cost_summary(store, TENANT, RUN)
    assert summary["amount"] is None
    assert summary["pricing_snapshot_id"] is None
    assert summary["cost_status"] == "unknown_cost"
    assert summary["input_tokens"] == 100 and summary["output_tokens"] == 20
    schema = json.loads(Path("contracts/jsonschema/api_models.schema.json").read_text())
    Draft202012Validator(
        {"$ref": "#/$defs/Cost", "$defs": schema["$defs"]}, format_checker=FormatChecker()
    ).validate(summary)


def test_decimal_cache_pricing_and_reopen_preserve_snapshot(tmp_path):
    path = tmp_path / "usage.sqlite"
    store = seeded(path)
    reserve(store)
    row = record_usage(store, CALL, USAGE, now=2)
    assert row["cost_decimal"] == "0.000131"
    assert row["pricing_snapshot_id"] == PRICE.snapshot_id
    reopened = LocalSQLiteUsageStore(path)
    assert cost_summary(reopened, TENANT, RUN) == {
        "run_id": RUN,
        "input_tokens": 100,
        "output_tokens": 20,
        "attempt_count": 1,
        "cache_hit_count": 1,
        "amount": "0.000131",
        "currency": "USD",
        "pricing_snapshot_id": PRICE.snapshot_id,
        "cost_status": "known",
    }
    assert record_usage(reopened, CALL, USAGE, now=3) == row
    with pytest.raises(BudgetConflict):
        record_usage(reopened, CALL, replace(USAGE, output_tokens=21), now=4)
    with pytest.raises(BudgetConflict):
        reserve(reopened, price=replace(PRICE, input_per_million=Decimal("999")))


def test_concurrent_reservations_cannot_overbook_and_duplicates_cannot_reinvoke(tmp_path):
    path = tmp_path / "usage.sqlite"
    seeded(path, replace(LIMITS, input_tokens=100, output_tokens=100))

    def attempt(i):
        try:
            return reserve(LocalSQLiteUsageStore(path), replace(CALL, request_id=f"request-{i}"))
        except BudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=8) as workers:
        assert sum(workers.map(attempt, range(8))) == 1
    another = tmp_path / "duplicate.sqlite"
    store = seeded(another)
    assert reserve(store)
    assert not reserve(LocalSQLiteUsageStore(another))
    assert cost_summary(store, TENANT, RUN)["amount"] is None


def test_failed_unknown_usage_keeps_reservation_and_never_counts_as_free(tmp_path):
    store = seeded(tmp_path / "usage.sqlite", replace(LIMITS, input_tokens=100, output_tokens=100))
    reserve(store)
    unknown = TokenUsage(None, None, None, None, 120000, "failed", None, "MODEL_TIMEOUT")
    record_usage(store, CALL, unknown, now=2)
    assert cost_summary(store, TENANT, RUN)["cost_status"] == "unknown_cost"
    with pytest.raises(BudgetExceeded):
        reserve(store, replace(CALL, attempt=2))
    assert len(store.ledger(TENANT, RUN)) == 1
    assert store.ledger(TENANT, RUN)[0]["usage"]["input_tokens"] is None


def test_actual_usage_releases_only_unused_tokens_and_overrun_blocks_new_calls(tmp_path):
    store = seeded(tmp_path / "usage.sqlite", replace(LIMITS, input_tokens=200, output_tokens=200))
    reserve(store)
    record_usage(store, CALL, replace(USAGE, input_tokens=20, output_tokens=10), now=2)
    assert reserve(
        store, replace(CALL, request_id="request-2"), input_tokens=180, max_output_tokens=190
    )
    record_usage(
        store,
        replace(CALL, request_id="request-2"),
        replace(USAGE, input_tokens=250, output_tokens=200),
        now=3,
    )
    with pytest.raises(BudgetExceeded):
        reserve(store, replace(CALL, request_id="request-3"), input_tokens=1, max_output_tokens=1)
    assert cost_summary(store, TENANT, RUN)["input_tokens"] == 270


def test_attempt_role_output_and_context_limits_apply_before_reserving(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    for attempt in range(1, 4):
        call = replace(CALL, attempt=attempt)
        assert reserve(store, call)
        record_usage(store, call, replace(USAGE, status="failed", error_code="PROVIDER_5XX"), now=2)
    with pytest.raises(BudgetExceeded):
        reserve(store, replace(CALL, attempt=4))
    with pytest.raises(BudgetExceeded):
        reserve(store, replace(CALL, role="vision"))
    with pytest.raises(BudgetExceeded):
        reserve(store, replace(CALL, request_id="too-large"), max_output_tokens=1001)
    assert cost_summary(store, TENANT, RUN)["attempt_count"] == 3
    calls = seeded(
        tmp_path / "calls.sqlite", replace(LIMITS, roles=(RoleLimit("tagger", 1, 1000, 1000, 150),))
    )
    with pytest.raises(BudgetExceeded):
        reserve(calls)
    assert reserve(calls, input_tokens=50, max_output_tokens=50)
    record_usage(calls, CALL, replace(USAGE, input_tokens=50), now=2)
    with pytest.raises(BudgetExceeded):
        reserve(calls, replace(CALL, attempt=2), input_tokens=1, max_output_tokens=1)


@pytest.mark.parametrize("field", ["input_tokens", "output_tokens"])
def test_provider_overrun_fences_new_and_already_reserved_calls_after_restart(tmp_path, field):
    path = tmp_path / "usage.sqlite"
    store = seeded(path)
    assert reserve(store)
    queued = replace(CALL, request_id="already-reserved")
    assert reserve_budget(
        store, queued, input_tokens=100, max_output_tokens=100, pricing=PRICE, now=1
    )
    actual = replace(USAGE, **{field: 101})
    ledger = record_usage(store, CALL, actual, now=2)
    reopened = LocalSQLiteUsageStore(path)
    # Preserve actual paid usage, including the part that exceeded reservation.
    assert record_usage(reopened, CALL, actual, now=3) == ledger
    assert cost_summary(reopened, TENANT, RUN)[field] == 101
    assert not reserve(reopened)  # Exact duplicate remains idempotent.
    with pytest.raises(BudgetExceeded, match="provider usage exceeded reservation"):
        reserve(reopened, replace(CALL, request_id="new-call"))
    with pytest.raises(BudgetExceeded, match="provider usage exceeded reservation"):
        reopened.mark_dispatched(queued)
    queued_row = next(
        row
        for row in reopened.cost_data(TENANT, RUN)
        if row["reservation"]["call"]["request_id"] == queued.request_id
    )
    assert queued_row["state"] == "reserved"
    # Other tenants are unaffected; no process-global stop flag.
    reopened.create_budget(OTHER, RUN, VERSION, LIMITS)
    assert reserve(reopened, replace(CALL, tenant_id=OTHER))


def test_tenant_document_replica_and_request_hash_isolation(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    store.create_budget(OTHER, RUN, VERSION, LIMITS)
    reserve(store)
    other_call = replace(CALL, tenant_id=OTHER)
    assert reserve(store, other_call)
    record_usage(store, other_call, replace(USAGE, input_tokens=70), now=2)
    assert cost_summary(store, TENANT, RUN)["input_tokens"] == 0
    assert cost_summary(store, OTHER, RUN)["input_tokens"] == 70
    with pytest.raises(KeyError):
        reserve(store, replace(CALL, document_version_id=str(UUID(int=6))))
    with pytest.raises(BudgetConflict):
        reserve(store, replace(CALL, request_signature="c" * 64, attempt=2))
    with pytest.raises(BudgetConflict):
        reserve(store, replace(CALL, replicate_id=2, attempt=2))


def test_partial_pricing_and_missing_cache_rate_do_not_report_subtotal_as_total(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    reserve(store)
    record_usage(store, CALL, USAGE, now=2)
    second = replace(CALL, request_id="request-2")
    reserve(store, second, price=None)
    record_usage(store, second, USAGE, now=2)
    summary = cost_summary(store, TENANT, RUN)
    assert summary["cost_status"] == "partial" and summary["amount"] is None
    third = replace(CALL, request_id="request-3")
    reserve(
        store, third, price=replace(PRICE, snapshot_id="missing-cache", cache_read_per_million=None)
    )
    assert record_usage(store, third, USAGE, now=2)["cost_decimal"] is None


def test_budget_policy_cannot_be_silently_raised_and_unreserved_usage_rejected(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    store.create_budget(TENANT, RUN, VERSION, LIMITS)
    with pytest.raises(BudgetConflict):
        store.create_budget(TENANT, RUN, VERSION, replace(LIMITS, input_tokens=99999))
    with pytest.raises(KeyError):
        record_usage(store, CALL, USAGE, now=2)
    with pytest.raises(ValueError):
        reserve(store, price=replace(PRICE, model_id="different-model"))


@pytest.mark.parametrize(
    "changes",
    [
        {"input_tokens": -1},
        {"output_tokens": True},
        {"cache_read_tokens": 101},
        {"cache_write_tokens": -1},
        {"latency_ms": -1},
    ],
)
def test_usage_rejects_invalid_counts(changes):
    with pytest.raises(ValueError):
        replace(USAGE, **changes)


def test_compensate_only_before_dispatch_and_never_bill_cache_lookup_as_provider(tmp_path):
    store = seeded(tmp_path / "usage.sqlite", replace(LIMITS, input_tokens=100, output_tokens=100))
    assert reserve_budget(
        store, CALL, input_tokens=100, max_output_tokens=100, pricing=PRICE, now=1
    )
    store.release_budget(
        CALL,
        reason="local response cache hit before provider dispatch",
        now=2,
        response_cache_hit=True,
    )
    store.release_budget(
        CALL,
        reason="local response cache hit before provider dispatch",
        now=3,
        response_cache_hit=True,
    )
    assert not store.mark_dispatched(CALL)
    assert store.ledger(TENANT, RUN) == []
    assert cost_summary(store, TENANT, RUN)["attempt_count"] == 0
    assert cost_summary(store, TENANT, RUN)["cache_hit_count"] == 1
    with pytest.raises(BudgetConflict):
        record_usage(store, CALL, USAGE, now=3)
    another = replace(CALL, request_id="provider-request")
    assert reserve(store, another)
    assert not store.mark_dispatched(another)
    with pytest.raises(BudgetConflict):
        store.release_budget(another, reason="timeout is not a refund", now=4)
    record_usage(store, another, replace(USAGE, status="cancelled"), now=5)
    assert cost_summary(store, TENANT, RUN)["attempt_count"] == 1


def test_shared_schema_and_separate_process_usage_reopen(tmp_path):
    import sqlite3
    import subprocess
    import sys

    path = tmp_path / "usage.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE unrelated (value TEXT)")
        db.execute("INSERT INTO unrelated VALUES ('preserved')")
        db.execute("PRAGMA user_version=88")
    store = seeded(path)
    reserve(store)
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import json, sys
from proofops.adapters.aws.usage import LocalSQLiteUsageStore
from proofops.application.budget import BudgetCall, TokenUsage
store = LocalSQLiteUsageStore(sys.argv[1])
store.record_usage(BudgetCall(**json.loads(sys.argv[2])),
                   TokenUsage(**json.loads(sys.argv[3])), now=2)
""",
            str(path),
            __import__("json").dumps(vars(CALL)),
            __import__("json").dumps(vars(USAGE)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert process.returncode == 0, process.stderr
    assert cost_summary(LocalSQLiteUsageStore(path), TENANT, RUN)["amount"] == "0.000131"
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT value FROM unrelated").fetchone() == ("preserved",)
        assert db.execute("PRAGMA user_version").fetchone() == (88,)
        db.execute("UPDATE usage_schema SET version=2")
    with pytest.raises(BudgetConflict, match="schema version"):
        LocalSQLiteUsageStore(path)


@pytest.mark.parametrize("rate", [1.5, Decimal("NaN"), Decimal("Infinity"), Decimal("-1")])
def test_reject_float_nonfinite_and_negative_money(rate):
    with pytest.raises(ValueError):
        replace(PRICE, input_per_million=rate)


def test_exhausted_input_budget_blocks_even_a_zero_input_new_call(tmp_path):
    store = seeded(tmp_path / "usage.sqlite", replace(LIMITS, input_tokens=100))
    reserve(store)
    record_usage(store, CALL, USAGE, now=2)
    with pytest.raises(BudgetExceeded):
        reserve(store, replace(CALL, request_id="zero-input"), input_tokens=0, max_output_tokens=1)


def test_retry_requires_failed_settlement_or_pre_dispatch_release(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    assert reserve_budget(
        store, CALL, input_tokens=100, max_output_tokens=100, pricing=PRICE, now=1
    )
    retry = replace(CALL, attempt=2)
    with pytest.raises(BudgetConflict):
        reserve(store, retry)
    assert store.mark_dispatched(CALL)
    with pytest.raises(BudgetConflict):
        reserve(store, retry)
    record_usage(store, CALL, replace(USAGE, status="failed", error_code="PROVIDER_5XX"), now=2)
    assert reserve(store, retry)
    record_usage(store, retry, USAGE, now=3)
    with pytest.raises(BudgetConflict):
        reserve(store, replace(CALL, attempt=3))
    released = replace(CALL, request_id="never-sent")
    assert reserve_budget(
        store, released, input_tokens=100, max_output_tokens=100, pricing=PRICE, now=4
    )
    store.release_budget(released, reason="preflight stopped before dispatch", now=5)
    assert reserve(store, replace(released, attempt=2))


def test_price_snapshot_id_cannot_alias_across_runs_or_tenants(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    reserve(store)
    store.create_budget(OTHER, RUN, VERSION, LIMITS)
    with pytest.raises(BudgetConflict):
        reserve(
            store,
            replace(CALL, tenant_id=OTHER),
            price=replace(PRICE, input_per_million=Decimal("999")),
        )
    other_run = str(UUID(int=99))
    store.create_budget(TENANT, other_run, VERSION, LIMITS)
    with pytest.raises(BudgetConflict):
        reserve(
            store,
            replace(CALL, run_id=other_run),
            price=replace(PRICE, output_per_million=Decimal("999")),
        )
    assert store.cost_data(OTHER, RUN) == []
    assert store.cost_data(TENANT, other_run) == []
    assert reserve(store, replace(CALL, tenant_id=OTHER))


def test_citation_failure_is_not_a_network_retry_and_schema_repair_is_once(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    reserve(store)
    record_usage(store, CALL, replace(USAGE, status="failed", error_code="CITATION_INVALID"), now=2)
    with pytest.raises(BudgetConflict):
        reserve(store, replace(CALL, attempt=2))
    schema_call = replace(CALL, request_id="schema-repair")
    reserve(store, schema_call)
    record_usage(
        store, schema_call, replace(USAGE, status="failed", error_code="LLM_SCHEMA_INVALID"), now=2
    )
    reserve(store, replace(schema_call, attempt=2))
    record_usage(
        store,
        replace(schema_call, attempt=2),
        replace(USAGE, status="failed", error_code="LLM_SCHEMA_INVALID"),
        now=3,
    )
    with pytest.raises(BudgetConflict):
        reserve(store, replace(schema_call, attempt=3))


def test_schema_repair_preserves_distinct_actual_request_signature(tmp_path):
    store = seeded(tmp_path / "usage.sqlite")
    reserve(store)
    record_usage(
        store, CALL, replace(USAGE, status="failed", error_code="LLM_SCHEMA_INVALID"), now=2
    )
    repaired = replace(CALL, attempt=2, request_signature="c" * 64)
    assert reserve(store, repaired)
    record_usage(store, repaired, USAGE, now=3)
    assert [row["call"]["request_signature"] for row in store.ledger(TENANT, RUN)] == [
        "b" * 64,
        "c" * 64,
    ]
