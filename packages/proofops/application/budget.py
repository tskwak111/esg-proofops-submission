"""Immutable budget inputs and exact, price-snapshot-based cost projection.

Token counts are supplied by the model tokenizer/provider, never estimated from
characters here. input_tokens is total input including cache read/write tokens;
those cache buckets are disjoint subsets, not additional input tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any, Protocol

from proofops.domain.values import _require_sha256, _require_strict_int, _require_uuid


class BudgetExceeded(ValueError):
    """BUDGET_EXHAUSTED: stop new calls and expose partial processing, never sample silently."""


class BudgetConflict(ValueError):
    """Immutable policy, reservation or usage identity differs from the stored one."""


def count(name: str, value: object, minimum: int = 0) -> int:
    number = _require_strict_int(name, value)
    if not minimum <= number <= 2**63 - 1:
        raise ValueError(f"{name} is outside the supported token/count range")
    return number


def text_field(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{name} must be non-empty text of at most 512 characters")


@dataclass(frozen=True)
class RoleLimit:
    role: str
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int
    max_context_tokens: int

    def __post_init__(self) -> None:
        text_field("role", self.role)
        for field in ("max_calls", "max_input_tokens", "max_output_tokens", "max_context_tokens"):
            count(field, getattr(self, field), 1)


@dataclass(frozen=True)
class BudgetLimits:
    input_tokens: int
    output_tokens: int
    roles: tuple[RoleLimit, ...]
    max_attempts: int = 3

    def __post_init__(self) -> None:
        count("input_tokens", self.input_tokens, 1)
        count("output_tokens", self.output_tokens, 1)
        if not 1 <= count("max_attempts", self.max_attempts) <= 3:
            raise ValueError("max_attempts cannot exceed the three-attempt contract")
        roles = tuple(self.roles)
        if not roles or any(not isinstance(role, RoleLimit) for role in roles):
            raise ValueError("typed role limits required")
        if len({role.role for role in roles}) != len(roles):
            raise ValueError("duplicate role limits")
        object.__setattr__(self, "roles", roles)


@dataclass(frozen=True)
class BudgetCall:
    tenant_id: str
    run_id: str
    document_version_id: str
    request_id: str
    attempt: int
    role: str
    model_id: str
    region: str
    model_binding_hash: str
    request_signature: str
    replicate_id: int | None

    def __post_init__(self) -> None:
        for field in ("tenant_id", "run_id", "document_version_id"):
            _require_uuid(field, getattr(self, field))
        for field in ("request_id", "role", "model_id", "region"):
            text_field(field, getattr(self, field))
        for field in ("model_binding_hash", "request_signature"):
            _require_sha256(field, getattr(self, field))
        count("attempt", self.attempt, 1)
        if self.replicate_id is not None and count("replicate_id", self.replicate_id) not in (
            1,
            2,
            3,
        ):
            raise ValueError("replicate_id must be 1, 2, 3 or null")
        if self.role == "tagger" and self.replicate_id is None:
            raise ValueError("tagging calls require a replicate_id")


@dataclass(frozen=True)
class PricingSnapshot:
    snapshot_id: str
    model_id: str
    region: str
    captured_at: str
    input_per_million: Decimal
    output_per_million: Decimal
    cache_read_per_million: Decimal | None = None
    cache_write_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        for field in ("snapshot_id", "model_id", "region"):
            text_field(field, getattr(self, field))
        text_field("captured_at", self.captured_at)
        if datetime.fromisoformat(self.captured_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("pricing timestamp must be timezone-aware")
        for field in (
            "input_per_million",
            "output_per_million",
            "cache_read_per_million",
            "cache_write_per_million",
        ):
            value = getattr(self, field)
            if value is None and field.startswith("cache_"):
                continue
            if (
                not isinstance(value, Decimal)
                or not value.is_finite()
                or value < 0
                or len(str(value)) > 64
                or abs(int(value.as_tuple().exponent)) > 30
            ):
                raise ValueError("prices must be finite, non-negative Decimal values")

    def to_dict(self) -> dict[str, Any]:
        return {
            field: str(value) if isinstance(value, Decimal) else value
            for field, value in vars(self).items()
        }


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_write_tokens: int | None
    latency_ms: int
    status: str
    provider_request_id: str | None
    error_code: str | None = None

    def __post_init__(self) -> None:
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            value = getattr(self, field)
            if value is not None:
                count(field, value)
        count("latency_ms", self.latency_ms)
        if self.status not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("unsupported provider usage status")
        if self.provider_request_id is not None:
            text_field("provider_request_id", self.provider_request_id)
        if self.error_code is not None:
            text_field("error_code", self.error_code)
        if self.status == "failed" and self.error_code is None:
            raise ValueError("failed usage requires error_code")
        if self.input_tokens is not None:
            if (self.cache_read_tokens or 0) + (self.cache_write_tokens or 0) > self.input_tokens:
                raise ValueError("cache buckets cannot exceed total input")


def usage_cost(usage: TokenUsage, pricing: dict[str, Any] | None) -> str | None:
    if pricing is None or any(
        getattr(usage, field) is None
        for field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    ):
        return None
    incoming = usage.input_tokens - usage.cache_read_tokens - usage.cache_write_tokens  # type: ignore[operator]
    buckets = (
        (incoming, "input_per_million"),
        (usage.output_tokens, "output_per_million"),
        (usage.cache_read_tokens, "cache_read_per_million"),
        (usage.cache_write_tokens, "cache_write_per_million"),
    )
    with localcontext() as context:
        context.prec = 100
        amount = Decimal(0)
        for tokens, rate in buckets:
            if tokens:
                if pricing.get(rate) is None:
                    return None
                amount += tokens * Decimal(pricing[rate]) / Decimal(1_000_000)
        return format(amount, "f")


class UsageRepository(Protocol):
    def reserve_budget(
        self,
        call: BudgetCall,
        *,
        input_tokens: int,
        max_output_tokens: int,
        pricing: PricingSnapshot | None,
        now: int,
    ) -> bool: ...
    def mark_dispatched(self, call: BudgetCall) -> bool: ...
    def release_budget(
        self, call: BudgetCall, *, reason: str, now: int, response_cache_hit: bool = False
    ) -> None: ...
    def record_usage(self, call: BudgetCall, usage: TokenUsage, *, now: int) -> dict[str, Any]: ...
    def cost_data(self, tenant_id: str, run_id: str) -> list[dict[str, Any]]: ...


def reserve_budget(
    store: UsageRepository,
    call: BudgetCall,
    *,
    input_tokens: int,
    max_output_tokens: int,
    pricing: PricingSnapshot | None,
    now: int,
) -> bool:
    """True only for a new reservation; duplicate deliveries must not invoke again."""
    return store.reserve_budget(
        call,
        input_tokens=input_tokens,
        max_output_tokens=max_output_tokens,
        pricing=pricing,
        now=now,
    )


def record_usage(
    store: UsageRepository, call: BudgetCall, usage: TokenUsage, *, now: int
) -> dict[str, Any]:
    return store.record_usage(call, usage, now=now)


def cost_summary(store: UsageRepository, tenant_id: str, run_id: str) -> dict[str, Any]:
    all_records = store.cost_data(tenant_id, run_id)
    records = [row for row in all_records if row["state"] != "released"]
    settled = [row["ledger"] for row in records if row["ledger"] is not None]
    costs = [row["cost_decimal"] for row in settled if row["cost_decimal"] is not None]
    all_known = bool(records) and len(costs) == len(records)
    with localcontext() as context:
        context.prec = 100
        amount = (
            format(sum((Decimal(value) for value in costs), Decimal(0)), "f") if all_known else None
        )
    snapshots = {row["pricing_snapshot_id"] for row in settled}
    return {
        "run_id": run_id,
        "input_tokens": sum(row["usage"]["input_tokens"] or 0 for row in settled),
        "output_tokens": sum(row["usage"]["output_tokens"] or 0 for row in settled),
        "attempt_count": sum(row["state"] in {"dispatched", "settled"} for row in records),
        "cache_hit_count": sum((row["usage"]["cache_read_tokens"] or 0) > 0 for row in settled)
        + sum(bool((row["release"] or {}).get("response_cache_hit")) for row in all_records),
        "amount": amount,
        "currency": "USD",
        "pricing_snapshot_id": next(iter(snapshots)) if len(snapshots) == 1 else None,
        "cost_status": "known" if all_known else "partial" if costs else "unknown_cost",
    }
