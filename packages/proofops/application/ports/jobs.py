"""Typed durable-job boundary; no storage, environment, SDK or model dependency."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Protocol

from proofops.domain.values import _require_sha256, _require_strict_int, _require_uuid


class JobConflict(ValueError):
    """Revision/idempotency/immutable input conflict; callers must not merge it."""


class LeaseLost(ValueError):
    """Expired, cancelled or superseded worker ownership."""


@dataclass(frozen=True, slots=True)
class JobMessage:
    tenant_id: str
    run_id: str
    document_version_id: str
    job_id: str
    stage: str
    shard: str
    input_hash: str

    def __post_init__(self) -> None:
        for name in ("tenant_id", "run_id", "document_version_id", "job_id"):
            _require_uuid(name, getattr(self, name))
        _require_sha256("input_hash", self.input_hash)
        for name in ("stage", "shard"):
            value = getattr(self, name)
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                raise ValueError(f"invalid {name}")


@dataclass(frozen=True, slots=True)
class JobLease:
    message: JobMessage
    owner: str
    fencing_token: int
    attempt: int
    lease_until: int

    def __post_init__(self) -> None:
        if not isinstance(self.message, JobMessage):
            raise ValueError("message must be JobMessage")
        if not isinstance(self.owner, str) or not self.owner.strip():
            raise ValueError("owner must be non-empty")
        for name in ("fencing_token", "attempt", "lease_until"):
            if _require_strict_int(name, getattr(self, name)) < 1:
                raise ValueError(f"{name} must be positive")


class JobRepository(Protocol):
    def delivery_status(self, message: JobMessage) -> str: ...
    def claim_job(
        self, message: JobMessage, *, owner: str, now: int, lease_seconds: int
    ) -> JobLease | None: ...
    def can_call(self, lease: JobLease, *, now: int) -> bool: ...
    def commit_job(
        self, lease: JobLease, *, payload: bytes, now: int, next_job: JobMessage | None = None
    ) -> bool: ...
    def record_usage(self, lease: JobLease, usage: dict[str, Any]) -> None: ...
    def fail_job(
        self,
        lease: JobLease,
        *,
        error_code: str,
        now: int,
        jitter: float = 0.5,
        retry_after: int | None = None,
    ) -> dict[str, Any]: ...
    def pending_outbox(self, tenant_id: str, run_id: str, *, now: int) -> list[dict[str, Any]]: ...
    def mark_outbox(
        self,
        tenant_id: str,
        run_id: str,
        event_id: str,
        *,
        now: int,
        sent: bool,
        expected_attempts: int,
    ) -> bool: ...
