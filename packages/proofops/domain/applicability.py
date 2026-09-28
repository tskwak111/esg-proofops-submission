"""Pure industry-topic applicability resolution.

GAP-010 deliberately supplies no real GICS-to-SASB mapping.  This module only
applies a caller-supplied, versioned mapping marked verified; all absent or
unverified coverage remains ``undetermined`` rather than becoming ``N_A``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from proofops.domain.errors import DomainValidationError

Applicability = Literal["applicable", "N_A", "undetermined"]
VerificationStatus = Literal["verified", "unverified"]


def _non_empty(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainValidationError(f"{name} must be a non-empty string")
    return value


@dataclass(frozen=True, slots=True)
class IndustryIdentity:
    """An industry classification supplied with the report."""

    system: str
    code: str | None

    def __post_init__(self) -> None:
        _non_empty("industry system", self.system)
        if self.code is not None:
            _non_empty("industry code", self.code)


@dataclass(frozen=True, slots=True)
class IndustryMappingEntry:
    """One version-pinned industry/topic applicability decision."""

    industry_system: str
    industry_code: str
    topic_id: str
    applicability: Applicability
    mandatory: bool | None

    def __post_init__(self) -> None:
        _non_empty("industry system", self.industry_system)
        _non_empty("industry code", self.industry_code)
        _non_empty("topic_id", self.topic_id)
        if self.applicability not in ("applicable", "N_A"):
            raise DomainValidationError("mapping applicability must be applicable or N_A")
        if self.applicability == "applicable" and not isinstance(self.mandatory, bool):
            raise DomainValidationError(
                "applicable mapping entries require mandatory=True or False"
            )
        if self.applicability == "N_A" and self.mandatory is not None:
            raise DomainValidationError("N_A mapping entries must not set mandatory")


@dataclass(frozen=True, slots=True)
class IndustryMapping:
    """A frozen mapping snapshot; verification is a prior human/domain gate."""

    version: str
    verification_status: VerificationStatus
    entries: tuple[IndustryMappingEntry, ...]

    def __post_init__(self) -> None:
        _non_empty("mapping version", self.version)
        if self.verification_status not in ("verified", "unverified"):
            raise DomainValidationError("unknown mapping verification_status")
        entries = tuple(self.entries)
        if any(not isinstance(entry, IndustryMappingEntry) for entry in entries):
            raise DomainValidationError("mapping entries must all be IndustryMappingEntry")
        keys = {(entry.industry_system, entry.industry_code, entry.topic_id) for entry in entries}
        if len(keys) != len(entries):
            raise DomainValidationError("mapping cannot contain duplicate industry/topic entries")
        object.__setattr__(self, "entries", entries)


@dataclass(frozen=True, slots=True)
class IndustryApplicability:
    """Applicability and mandatory status stay separate for denominator logic."""

    applicability: Applicability
    mandatory: bool | None
    mapping_version: str | None

    def __post_init__(self) -> None:
        if self.applicability not in ("applicable", "N_A", "undetermined"):
            raise DomainValidationError("unknown applicability")
        if self.applicability == "applicable" and not isinstance(self.mandatory, bool):
            raise DomainValidationError("applicable result requires mandatory=True or False")
        if self.applicability != "applicable" and self.mandatory is not None:
            raise DomainValidationError("only applicable results may set mandatory")
        if self.mapping_version is not None:
            _non_empty("mapping version", self.mapping_version)


def resolve_industry_applicability(
    industry: IndustryIdentity,
    topic_id: str,
    mapping: IndustryMapping | None,
) -> IndustryApplicability:
    """Resolve only explicit verified coverage; missing coverage is undetermined."""
    if not isinstance(industry, IndustryIdentity):
        raise DomainValidationError("industry must be IndustryIdentity")
    _non_empty("topic_id", topic_id)
    if mapping is None:
        return IndustryApplicability("undetermined", None, None)
    if not isinstance(mapping, IndustryMapping):
        raise DomainValidationError("mapping must be IndustryMapping or None")
    if (
        industry.code is None
        or industry.system.casefold() == "unknown"
        or mapping.verification_status != "verified"
    ):
        return IndustryApplicability("undetermined", None, mapping.version)
    for entry in mapping.entries:
        if (
            entry.industry_system == industry.system
            and entry.industry_code == industry.code
            and entry.topic_id == topic_id
        ):
            return IndustryApplicability(entry.applicability, entry.mandatory, mapping.version)
    return IndustryApplicability("undetermined", None, mapping.version)
