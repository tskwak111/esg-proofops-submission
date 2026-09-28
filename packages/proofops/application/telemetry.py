"""Content-free JSON observations; canonical audit and billing remain separate.

Context must originate from server-owned IDs, never request headers or event text.
Local streams are real sinks; deploying a cloud collector is a separate concern.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TextIO
from uuid import UUID, uuid4

_EVENTS = frozenset({"api_request", "model_call", "operation_completed", "operation_failed"})
_STAGES = frozenset(
    "API RUNTIME UPLOAD PARSE EXTRACT RETRIEVE TAG AGGREGATE RULE REVIEW EXPORT".split()
)
_ROLES = frozenset("extractor tagger vision writer".split())
_CODES = frozenset(
    """OK UNKNOWN INTERNAL_ERROR AUTH_REQUIRED SESSION_EXPIRED FORBIDDEN CSRF_INVALID
RESOURCE_NOT_FOUND IDEMPOTENCY_CONFLICT STALE_REVIEW_REVISION VALIDATION_ERROR PDF_INVALID
PDF_PASSWORD_REQUIRED UPLOAD_LIMIT_EXCEEDED PARSE_TIMEOUT PARSE_RESOURCE_LIMIT PARSE_CONFLICT
SOURCE_UNREADABLE CITATION_INVALID BINDING_UNCERTAIN LLM_SCHEMA_INVALID LLM_TRUNCATED
MODEL_THROTTLED PROVIDER_5XX MODEL_UNAVAILABLE REGION_DENIED BUDGET_EXHAUSTED RULE_GAP
BASIS_UNVERIFIED RETAG_REQUIRED EXPORT_SNAPSHOT_BUSY REPORT_NOT_FINALIZABLE LEASE_LOST
DEPENDENCY_UNAVAILABLE SQS_RECEIVE_LIMIT LEASE_HEARTBEAT_FAILED UNEXPECTED_ERROR""".split()
)
_METRICS = frozenset(
    """queue_oldest_age job_retry_count dlq_messages parser_page_count
unreadable_count table_conflicts citation_rejected binding_undetermined three_run_disagreement
grade_counts decision_blocked_rule_gap review_pending review_age export_snapshot_retry
cost_unknown""".split()
)
_COUNTS = ("attempt", "fencing_token", "input_tokens", "output_tokens", "cached_tokens")


def _choice(value: Any, allowed: frozenset[str], default: str = "UNKNOWN") -> str:
    return value if type(value) is str and value in allowed else default


def _hex(value: str, length: int) -> bool:
    return type(value) is str and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


@dataclass(frozen=True, slots=True)
class TraceContext:
    request_id: str = field(default_factory=lambda: str(uuid4()))
    trace_id: str = field(default_factory=lambda: uuid4().hex)
    span_id: str = field(default_factory=lambda: uuid4().hex[:16])
    parent_span_id: str | None = None
    tenant_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    model_binding_hash: str | None = None
    request_signature: str | None = None

    def __post_init__(self) -> None:
        if self.request_id is None or self.trace_id is None or self.span_id is None:
            raise ValueError("missing telemetry identity")
        for value in (self.request_id, self.tenant_id, self.run_id, self.job_id):
            if value is not None:
                try:
                    valid = type(value) is str and str(UUID(value)) == value
                except ValueError:
                    valid = False
                if not valid:
                    raise ValueError("invalid telemetry identity")
        for value, size in ((self.trace_id, 32), (self.span_id, 16), (self.parent_span_id, 16)):
            if value is not None and (not _hex(value, size) or int(value, 16) == 0):
                raise ValueError("invalid trace context")
        for value in (self.model_binding_hash, self.request_signature):
            if value is not None and not _hex(value, 64):
                raise ValueError("invalid telemetry hash")

    @classmethod
    def new(cls, **kwargs: Any) -> TraceContext:
        return cls(**kwargs)


def traceparent(context: TraceContext) -> str:
    return f"00-{context.trace_id}-{context.span_id}-01"


def queue_trace(context: TraceContext) -> dict[str, str]:
    if context.run_id is None:
        raise ValueError("queue trace requires run identity")
    return {"traceparent": traceparent(context), "run_id": context.run_id}


class _QuietHandler(logging.StreamHandler):
    """Default logging error handling prints exception text; never use it here."""

    dropped_events = 0

    def handleError(self, record: logging.LogRecord) -> None:
        self.dropped_events += 1


class Telemetry:
    def __init__(
        self,
        *,
        service: str,
        env: str,
        stream: TextIO,
        hash_key: bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.service, self.env = _configuration(service, env)
        if type(hash_key) is not bytes or len(hash_key) < 32:
            raise ValueError("telemetry requires a private hashing key of at least 32 bytes")
        self._key, self._clock = hash_key, clock
        self._handler = _QuietHandler(stream)
        self._logger = logging.Logger("proofops.sanitized", logging.INFO)
        self._logger.propagate = False
        self._logger.addHandler(self._handler)

    @property
    def dropped_events(self) -> int:
        return self._handler.dropped_events

    def _hash(self, value: Any) -> str | None:
        if type(value) is not str or not 1 <= len(value) <= 1024:
            return None
        return hmac.new(self._key, value.encode(errors="replace"), hashlib.sha256).hexdigest()

    def emit(self, event: Mapping[str, Any], *, context: TraceContext) -> dict[str, Any]:
        event_name = _choice(event.get("event"), _EVENTS, "unknown")
        row: dict[str, Any] = {
            "kind": "log",
            "timestamp": self._clock().astimezone(UTC).isoformat(),
            "level": "ERROR" if event_name == "operation_failed" else "INFO",
            "service": self.service,
            "env": self.env,
            "event": event_name,
            "stage": _choice(event.get("stage"), _STAGES),
            "code": _choice(event.get("code"), _CODES),
            "role": _choice(event.get("role"), _ROLES),
            "trace_id": context.trace_id,
            "span_id": context.span_id,
            "parent_span_id": context.parent_span_id,
            "request_id": context.request_id,
            "tenant_hash": self._hash(context.tenant_id),
            "run_id": context.run_id,
            "job_id": context.job_id,
            "model_binding_hash": context.model_binding_hash,
            "request_signature": context.request_signature,
            "provider_request_id": self._hash(event.get("provider_request_id")),
            "exception_type": (
                event["exception_type"]
                if type(event.get("exception_type")) is str
                and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", event["exception_type"])
                else None
            ),
            "last_successful_heartbeat_at": None,
        }
        heartbeat_at = event.get("last_successful_heartbeat_at")
        if type(heartbeat_at) is int and 0 <= heartbeat_at <= 253_402_300_799:
            row["last_successful_heartbeat_at"] = (
                datetime.fromtimestamp(heartbeat_at, UTC).isoformat().replace("+00:00", "Z")
            )
        for name in _COUNTS:
            value = event.get(name)
            row[name] = value if type(value) is int and 0 <= value <= 2**63 - 1 else None
        value = event.get("latency_ms")
        row["latency_ms"] = (
            value
            if isinstance(value, int | float)
            and type(value) in (int, float)
            and 0 <= value <= 2**53
            and math.isfinite(value)
            else None
        )
        status = event.get("http_status")
        row["http_status"] = status if type(status) is int and 100 <= status <= 599 else None
        self._write(row)
        self._write({**row, "kind": "trace"})
        metrics: dict[str, int | float] = {}
        for name in _METRICS:
            amount = event.get(name)
            if type(amount) is int and 0 <= amount <= 2**63 - 1:
                metrics[name] = amount
        if row["event"] == "api_request":
            metrics["api_requests_total"] = 1
            if row["latency_ms"] is not None:
                metrics["api_latency_ms"] = row["latency_ms"]
        if row["input_tokens"] is not None and row["output_tokens"] is not None:
            metrics["model_tokens"] = row["input_tokens"] + row["output_tokens"]
        if row["code"] == "LEASE_LOST":
            metrics["lease_lost_total"] = 1
        for name, amount in metrics.items():
            self._write(
                {
                    "kind": "metric",
                    "timestamp": row["timestamp"],
                    "name": name,
                    "value": amount,
                    "labels": {"env": self.env, "stage": row["stage"], "error_code": row["code"]},
                }
            )
        return row

    def _write(self, row: dict[str, Any]) -> None:
        self._logger.info(json.dumps(row, ensure_ascii=True, allow_nan=False))


def emit_sanitized_event(
    telemetry: Telemetry,
    event: Mapping[str, Any],
    *,
    context: TraceContext,
) -> dict[str, Any]:
    return telemetry.emit(event, context=context)


def _configuration(service: str, env: str) -> tuple[str, str]:
    if service not in ("api", "worker") or env not in ("local", "test", "staging", "production"):
        raise ValueError("invalid telemetry configuration")
    return service, env


class SafeRuntimeFormatter(logging.Formatter):
    """Discard external message/args/extra/stack entirely, including valid-looking secrets."""

    def __init__(self, *, service: str, env: str) -> None:
        super().__init__()
        self.service, self.env = _configuration(service, env)

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "kind": "log",
                "timestamp": datetime.now(UTC).isoformat(),
                "level": _choice(
                    record.levelname, frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
                ),
                "service": self.service,
                "env": self.env,
                "stage": "RUNTIME",
                "code": "INTERNAL_ERROR" if record.exc_info else "UNKNOWN",
                **dict.fromkeys(
                    (
                        "trace_id",
                        "request_id",
                        "tenant_hash",
                        "run_id",
                        "job_id",
                        "attempt",
                        "fencing_token",
                        "latency_ms",
                    )
                ),
            }
        )
