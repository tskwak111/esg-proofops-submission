"""Pure immutable audit events and hash-chain verification."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Final

from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_sha256, _require_strict_int, _require_uuid


class AuditConflict(DomainValidationError):
    """The supplied audit HEAD is stale or belongs to another stream."""


class AuditIntegrityError(DomainValidationError):
    """An immutable audit event chain is missing, reordered, or altered."""


def _required_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise DomainValidationError(f"{name} must be a non-empty string")
    return value


def _optional_hash(name: str, value: str | None) -> str | None:
    if value is not None:
        _require_sha256(name, value)
    return value


def _utc_timestamp(value: object) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise DomainValidationError("timestamp must be UTC RFC3339 ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise DomainValidationError("timestamp must be UTC RFC3339 ending in Z") from None
    if parsed.tzinfo != UTC:
        raise DomainValidationError("timestamp must be UTC RFC3339 ending in Z")
    return value


@dataclass(frozen=True, slots=True)
class AuditHead:
    tenant_id: str
    run_id: str
    sequence: int
    event_hash: str | None

    def __post_init__(self) -> None:
        _require_uuid("tenant_id", self.tenant_id)
        _require_uuid("run_id", self.run_id)
        if _require_strict_int("sequence", self.sequence) < 0:
            raise DomainValidationError("sequence must be non-negative")
        _optional_hash("event_hash", self.event_hash)
        if (self.sequence == 0) != (self.event_hash is None):
            raise DomainValidationError("only an empty audit HEAD has no event_hash")

    @classmethod
    def empty(cls, tenant_id: str, run_id: str) -> AuditHead:
        return cls(tenant_id, run_id, 0, None)


@dataclass(frozen=True, slots=True)
class ChangeSet:
    tenant_id: str
    run_id: str
    actor_sub: str
    action: str
    target_id: str
    before_hash: str | None
    after_hash: str
    revision: int
    reason: str | None

    def __post_init__(self) -> None:
        _require_uuid("tenant_id", self.tenant_id)
        _require_uuid("run_id", self.run_id)
        _require_uuid("target_id", self.target_id)
        _required_text("actor_sub", self.actor_sub)
        _required_text("action", self.action)
        _optional_hash("before_hash", self.before_hash)
        _require_sha256("after_hash", self.after_hash)
        if _require_strict_int("revision", self.revision) < 1:
            raise DomainValidationError("revision must be positive")
        if self.reason is not None:
            _required_text("reason", self.reason)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    tenant_id: str
    run_id: str
    event_id: str
    sequence: int
    actor_sub: str
    action: str
    target_id: str
    before_hash: str | None
    after_hash: str
    revision: int
    previous_event_hash: str | None
    event_hash: str
    timestamp: str
    reason: str | None

    def __post_init__(self) -> None:
        _require_uuid("tenant_id", self.tenant_id)
        _require_uuid("run_id", self.run_id)
        _require_uuid("event_id", self.event_id)
        if _require_strict_int("sequence", self.sequence) < 1:
            raise DomainValidationError("sequence must be positive")
        _required_text("actor_sub", self.actor_sub)
        _required_text("action", self.action)
        _require_uuid("target_id", self.target_id)
        _optional_hash("before_hash", self.before_hash)
        _require_sha256("after_hash", self.after_hash)
        if _require_strict_int("revision", self.revision) < 1:
            raise DomainValidationError("revision must be positive")
        _optional_hash("previous_event_hash", self.previous_event_hash)
        _require_sha256("event_hash", self.event_hash)
        _utc_timestamp(self.timestamp)
        if self.reason is not None:
            _required_text("reason", self.reason)


_EVENT_HASH_FIELDS: Final = (
    "tenant_id",
    "run_id",
    "event_id",
    "sequence",
    "actor_sub",
    "action",
    "target_id",
    "before_hash",
    "after_hash",
    "revision",
    "previous_event_hash",
    "timestamp",
    "reason",
)


def _hash_event(event: AuditEvent) -> str:
    return canonical_hash({name: getattr(event, name) for name in _EVENT_HASH_FIELDS})


def new_audit_event(
    change: ChangeSet,
    expected_head: AuditHead,
    *,
    event_id: str,
    timestamp: str,
) -> tuple[AuditEvent, AuditHead]:
    """Materialize the next event without I/O; the adapter commits it with HEAD CAS."""
    if not isinstance(change, ChangeSet) or not isinstance(expected_head, AuditHead):
        raise DomainValidationError("change and expected_head must be audit domain values")
    if (change.tenant_id, change.run_id) != (expected_head.tenant_id, expected_head.run_id):
        raise AuditConflict("expected audit HEAD belongs to another tenant or run")
    candidate = AuditEvent(
        tenant_id=change.tenant_id,
        run_id=change.run_id,
        event_id=event_id,
        sequence=expected_head.sequence + 1,
        actor_sub=change.actor_sub,
        action=change.action,
        target_id=change.target_id,
        before_hash=change.before_hash,
        after_hash=change.after_hash,
        revision=change.revision,
        previous_event_hash=expected_head.event_hash,
        event_hash="0" * 64,
        timestamp=timestamp,
        reason=change.reason,
    )
    event = replace(candidate, event_hash=_hash_event(candidate))
    return event, AuditHead(change.tenant_id, change.run_id, event.sequence, event.event_hash)


def verify_audit_chain(events: tuple[AuditEvent, ...], *, expected_head: AuditHead) -> None:
    """Verify a complete stream from sequence one through its expected HEAD."""
    if not isinstance(events, tuple) or any(not isinstance(event, AuditEvent) for event in events):
        raise AuditIntegrityError("audit events must be an immutable tuple")
    previous: str | None = None
    for sequence, event in enumerate(events, 1):
        if (event.tenant_id, event.run_id) != (expected_head.tenant_id, expected_head.run_id):
            raise AuditIntegrityError("audit event belongs to another tenant or run")
        if event.sequence != sequence or event.previous_event_hash != previous:
            raise AuditIntegrityError("audit event sequence or previous hash is invalid")
        if event.event_hash != _hash_event(event):
            raise AuditIntegrityError("audit event hash is invalid")
        previous = event.event_hash
    if expected_head.sequence != len(events) or expected_head.event_hash != previous:
        raise AuditIntegrityError("audit events do not reach the expected HEAD")
