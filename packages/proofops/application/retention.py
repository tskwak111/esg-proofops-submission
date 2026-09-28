"""SEC-004: explicit retention gates, verified deletion and durable tombstone replay."""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from proofops.domain.values import _require_strict_int, _require_uuid

RESOURCE_KINDS = frozenset(
    {"original", "derived", "search", "cache", "review", "memory", "export", "backup"}
)
DeletionStatus = Literal["requested", "running", "blocked_retention", "completed", "failed"]


def valid_time(now: int) -> None:
    if _require_strict_int("now", now) < 0:
        raise ValueError("now must be non-negative")


@dataclass(frozen=True)
class RetentionPolicy:
    tenant_id: str
    document_id: str
    policy_id: str
    approved: bool = False
    legal_hold: bool = False
    retain_until: int | None = None

    def __post_init__(self):
        _require_uuid("tenant_id", self.tenant_id)
        _require_uuid("document_id", self.document_id)
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise ValueError("policy_id required")
        if type(self.approved) is not bool or type(self.legal_hold) is not bool:
            raise ValueError("policy flags must be booleans")
        if self.retain_until is not None:
            valid_time(self.retain_until)

    def blocks(self, now: int) -> bool:
        return (
            not self.approved
            or self.legal_hold
            or (self.retain_until is not None and now < self.retain_until)
        )


@dataclass(frozen=True, order=True)
class DeletionTarget:
    document_version: str
    kind: str
    resource_id: str
    sha256: str

    def __post_init__(self):
        if self.kind not in RESOURCE_KINDS:
            raise ValueError("untracked resource kind")
        if not self.document_version or not self.resource_id:
            raise ValueError("resource identity required")
        if len(self.sha256) != 64 or any(c not in "0123456789abcdef" for c in self.sha256):
            raise ValueError("resource hash required")


@dataclass(frozen=True)
class DeletionManifest:
    tenant_id: str
    document_id: str
    deletion_id: str
    requested_by: str
    requested_at: int
    targets: tuple[DeletionTarget, ...]

    def __post_init__(self):
        for name in ("tenant_id", "document_id", "deletion_id"):
            _require_uuid(name, getattr(self, name))
        valid_time(self.requested_at)
        if not self.requested_by:
            raise ValueError("requested_by required")
        if type(self.targets) is not tuple or len(set(self.targets)) != len(self.targets):
            raise ValueError("unique immutable targets required")


@dataclass(frozen=True)
class DeletionResult:
    status: DeletionStatus
    completed_at: int | None
    deleted: tuple[DeletionTarget, ...]
    remaining: tuple[DeletionTarget, ...]
    policy_id: str
    error_codes: tuple[str, ...] = ()


class DeletionSession(Protocol):
    """A fenced scope: inventory includes every version and kind, errors never mean empty."""

    def inventory(self) -> tuple[DeletionTarget, ...]: ...
    def delete(self, target: DeletionTarget) -> None: ...
    def record(self, result: DeletionResult) -> None: ...


class DeletionPort(Protocol):
    """Durable tombstone precedes work; writes and reads are fenced until after restore."""

    def scope(self, manifest: DeletionManifest) -> AbstractContextManager[DeletionSession]: ...
    def tombstones(self) -> tuple[DeletionManifest, ...]: ...


def delete_document_tree(
    manifest: DeletionManifest, policy: RetentionPolicy, store: DeletionPort, *, now: int
) -> DeletionResult:
    valid_time(now)
    if (manifest.tenant_id, manifest.document_id) != (policy.tenant_id, policy.document_id):
        raise ValueError("retention policy scope mismatch")
    with store.scope(manifest) as session:
        targets = (
            session.inventory()
        )  # Re-enumerate after restore; the old manifest may be partial.
        errors = []
        blocked = policy.blocks(now)
        if not blocked:
            for target in targets:
                try:
                    session.delete(target)
                except (OSError, RuntimeError):
                    errors.append(
                        "DELETE_FAILED"
                    )  # No backend exception or document body in audit.
        remaining = session.inventory()  # A successful delete acknowledgment is insufficient.
        deleted = tuple(t for t in targets if t not in remaining)
        status: DeletionStatus = (
            "blocked_retention"
            if blocked
            else "failed"
            if errors
            else "running"
            if remaining
            else "completed"
        )
        result = DeletionResult(
            status,
            now if status == "completed" else None,
            deleted,
            remaining,
            policy.policy_id,
            tuple(sorted(set(errors))),
        )
        session.record(result)
        return result


def reapply_tombstones(
    store: DeletionPort, policies: Mapping[tuple[str, str], RetentionPolicy], *, now: int
) -> tuple[DeletionResult, ...]:
    """Restore is offline; preserved tombstones hide data even if policy is unavailable."""
    valid_time(now)
    return tuple(
        delete_document_tree(
            manifest,
            policies.get((manifest.tenant_id, manifest.document_id))
            or RetentionPolicy(manifest.tenant_id, manifest.document_id, "unapproved"),
            store,
            now=now,
        )
        for manifest in store.tombstones()
    )
