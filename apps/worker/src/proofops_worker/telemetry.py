"""Observe a real leased operation without changing publication or billing semantics."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from time import perf_counter
from typing import Any

from proofops.application.ports.jobs import JobLease, LeaseLost
from proofops.application.telemetry import Telemetry, TraceContext

from proofops_worker.consumer import StageFailure, TagHeartbeatFailed


def worker_context(lease: JobLease, propagated: Mapping[str, str]) -> TraceContext:
    if propagated.get("run_id") != lease.message.run_id:
        raise ValueError("trace run identity mismatch")
    value = propagated.get("traceparent")
    match = re.fullmatch(r"00-([0-9a-f]{32})-([0-9a-f]{16})-0[01]", value or "")
    if match is None:
        raise ValueError("invalid queue traceparent")
    return TraceContext.new(
        trace_id=match[1],
        parent_span_id=match[2],
        tenant_id=lease.message.tenant_id,
        run_id=lease.message.run_id,
        job_id=lease.message.job_id,
    )


def observe_job(
    telemetry: Telemetry,
    lease: JobLease,
    operation: Callable[[JobLease], tuple[bytes, dict[str, Any]]],
    *,
    context: TraceContext,
    heartbeat_state=None,
) -> tuple[bytes, dict[str, Any]]:
    if (context.tenant_id, context.run_id, context.job_id) != (
        lease.message.tenant_id,
        lease.message.run_id,
        lease.message.job_id,
    ):
        raise ValueError("telemetry lease identity mismatch")
    started = perf_counter()
    event: dict[str, Any] = {}
    try:
        result = operation(lease)
        event.update(
            {
                key: result[1].get(key)
                for key in ("input_tokens", "output_tokens", "cached_tokens", "provider_request_id")
            }
        )
        event.update(event="operation_completed", code="OK")
        return result
    except BaseException as error:
        event.update(event="operation_failed", code="UNEXPECTED_ERROR")
        error_type = type(error).__name__
        event["exception_type"] = (
            error_type if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", error_type) else "Exception"
        )
        if isinstance(error, StageFailure):
            event.update(
                {
                    key: error.usage.get(key)
                    for key in (
                        "input_tokens",
                        "output_tokens",
                        "cached_tokens",
                        "provider_request_id",
                    )
                }
            )
            event["code"] = error.error_code
        elif isinstance(error, LeaseLost) or error_type == "_TagFenceLost":
            event["code"] = "LEASE_LOST"
        elif isinstance(error, TagHeartbeatFailed):
            event["code"] = "LEASE_HEARTBEAT_FAILED"
        raise
    finally:
        event.update(
            stage=lease.message.stage.upper(),
            attempt=lease.attempt,
            fencing_token=lease.fencing_token,
            latency_ms=(perf_counter() - started) * 1000,
            last_successful_heartbeat_at=(
                heartbeat_state.last_successful_at if heartbeat_state is not None else None
            ),
        )
        telemetry.emit(event, context=context)
