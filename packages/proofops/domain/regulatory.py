"""Pure regulatory deferral applicability resolution.

The resolver selects only an explicit entry from a human-approved timeline.
It contains no regulatory dates, asset thresholds, violation rules, or safe-
harbor conclusions of its own.  Missing approval, disabled automatic legal
applicability, no exact match, or overlapping matches all fail closed to
``undetermined`` (GAP-009).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Final, Literal, cast
from uuid import UUID

from proofops.domain.errors import DomainValidationError
from proofops.domain.rulepacks import RulePackSnapshot

DeferralStatus = Literal["required", "advisory", "undetermined"]
_ENTRY_OUTCOMES: Final = frozenset({"required", "advisory"})


def _condition_ids(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, str | bytes):
        raise DomainValidationError("condition_ids must be an iterable of strings")
    try:
        frozen = frozenset(values)
    except TypeError as exc:
        raise DomainValidationError("condition_ids must be an iterable of strings") from exc
    if any(not isinstance(value, str) or not value for value in frozen):
        raise DomainValidationError("condition_ids must contain only non-empty strings")
    return frozen


def _is_utc_timestamp(value: object) -> bool:
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(f"{value[:-1]}+00:00")
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0)


def _uuid(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(f"{name} must be a UUID string")
    try:
        parsed = UUID(value)
    except ValueError:
        raise DomainValidationError(f"{name} must be a valid UUID") from None
    if str(parsed) != value.lower():
        raise DomainValidationError(f"{name} must be a canonical UUID string")
    return value


@dataclass(frozen=True, slots=True)
class CompanyContext:
    """Opaque approved company conditions at one reporting cutoff date."""

    tenant_id: str
    company_id: str
    as_of: date
    condition_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _uuid("tenant_id", self.tenant_id)
        _uuid("company_id", self.company_id)
        if type(self.as_of) is not date:
            raise DomainValidationError("as_of must be a date")
        object.__setattr__(self, "condition_ids", _condition_ids(self.condition_ids))


@dataclass(frozen=True, slots=True)
class _TimelineEntry:
    """One explicit half-open period and company-condition outcome."""

    entry_id: str
    outcome: Literal["required", "advisory"]
    condition_ids: frozenset[str]
    starts_on: date
    ends_before: date | None

    def __post_init__(self) -> None:
        if not isinstance(self.entry_id, str) or not self.entry_id:
            raise DomainValidationError("entry_id must be a non-empty string")
        if not isinstance(self.outcome, str) or self.outcome not in _ENTRY_OUTCOMES:
            raise DomainValidationError("timeline outcome must be required or advisory")
        object.__setattr__(self, "condition_ids", _condition_ids(self.condition_ids))
        if type(self.starts_on) is not date:
            raise DomainValidationError("starts_on must be a date")
        if self.ends_before is not None:
            if type(self.ends_before) is not date:
                raise DomainValidationError("ends_before must be a date or None")
            if self.ends_before <= self.starts_on:
                raise DomainValidationError("ends_before must be later than starts_on")

    def matches(self, context: CompanyContext) -> bool:
        return (
            self.condition_ids <= context.condition_ids
            and self.starts_on <= context.as_of
            and (self.ends_before is None or context.as_of < self.ends_before)
        )


def _entry_from_dict(value: object) -> _TimelineEntry:
    if not isinstance(value, Mapping):
        raise DomainValidationError("timeline entry must be a mapping")
    try:
        entry_id = value["entry_id"]
        outcome = value["outcome"]
        condition_ids = value["condition_ids"]
        starts_on = value["starts_on"]
        ends_before = value["ends_before"]
    except KeyError as exc:
        raise DomainValidationError(f"timeline entry missing field: {exc.args[0]}") from exc
    if not isinstance(starts_on, str):
        raise DomainValidationError("starts_on must be an ISO date string")
    if ends_before is not None and not isinstance(ends_before, str):
        raise DomainValidationError("ends_before must be an ISO date string or None")
    try:
        start_date = date.fromisoformat(starts_on)
        end_date = date.fromisoformat(ends_before) if ends_before is not None else None
    except ValueError as exc:
        raise DomainValidationError("timeline dates must be ISO dates") from exc
    if not isinstance(condition_ids, list | tuple | frozenset):
        raise DomainValidationError("condition_ids must be an iterable of strings")
    return _TimelineEntry(
        entry_id=entry_id,  # type: ignore[arg-type]
        outcome=outcome,  # type: ignore[arg-type]
        condition_ids=_condition_ids(cast(Iterable[str], condition_ids)),
        starts_on=start_date,
        ends_before=end_date,
    )


def _approved_entries(snapshot: RulePackSnapshot) -> tuple[_TimelineEntry, ...] | None:
    try:
        timeline = snapshot.file_content("regulatory/timeline.yaml")
    except KeyError:
        return None
    if (
        snapshot.status not in {"validated", "active"}
        or not isinstance(snapshot.approved_by, str)
        or not snapshot.approved_by.strip()
        or not _is_utc_timestamp(snapshot.approved_at)
        or timeline.get("verification_status") != "approved"
        or timeline.get("automatic_legal_applicability_enabled") is not True
    ):
        return None
    raw_entries = timeline.get("entries")
    if not isinstance(raw_entries, list):
        return None
    try:
        entries = tuple(_entry_from_dict(entry) for entry in raw_entries)
    except DomainValidationError:
        return None
    if len({entry.entry_id for entry in entries}) != len(entries):
        return None
    return entries


def resolve_deferral(
    context: CompanyContext,
    timeline: RulePackSnapshot,
) -> DeferralStatus:
    """Return the sole approved matching outcome, otherwise ``undetermined``."""
    if context.tenant_id != timeline.tenant_id:
        return "undetermined"
    entries = _approved_entries(timeline)
    if entries is None:
        return "undetermined"
    matches = [entry for entry in entries if entry.matches(context)]
    return matches[0].outcome if len(matches) == 1 else "undetermined"
