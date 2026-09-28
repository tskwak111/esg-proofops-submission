"""Deterministic target-change candidates over two approved document versions.

The caller supplies an approved stable comparison key and content hash per target.
This module does no semantic matching, evidence borrowing, grading, storage, or I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from proofops.domain.values import _require_sha256, _require_strict_int, _require_uuid

Grade = Literal["E0", "E1", "E2", "E3"]
ChangeType = Literal["modified", "removed_candidate", "new", "ambiguous"]
ComparisonStatus = Literal["queued", "completed", "not_run", "failed"]


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    """One approved target projection; the grade is carried but never compared."""

    claim_id: str
    comparison_key: str
    content_sha256: str
    decision_revision: int
    evidence_grade: Grade | None

    def __post_init__(self) -> None:
        _require_uuid("claim_id", self.claim_id)
        if not isinstance(self.comparison_key, str) or not self.comparison_key.strip():
            raise ValueError("comparison_key must be non-empty text")
        _require_sha256("content_sha256", self.content_sha256)
        if _require_strict_int("decision_revision", self.decision_revision) < 1:
            raise ValueError("decision_revision must be positive")
        if self.evidence_grade not in (None, "E0", "E1", "E2", "E3"):
            raise ValueError("invalid evidence_grade")


@dataclass(frozen=True, slots=True)
class ApprovedVersion:
    tenant_id: str
    company_id: str
    document_version_id: str
    report_year: int
    approved: bool
    targets: tuple[TargetSnapshot, ...] = ()

    def __post_init__(self) -> None:
        _require_uuid("tenant_id", self.tenant_id)
        _require_uuid("company_id", self.company_id)
        _require_uuid("document_version_id", self.document_version_id)
        if not 1900 <= _require_strict_int("report_year", self.report_year) <= 2200:
            raise ValueError("report_year is outside the supported range")
        if type(self.approved) is not bool:
            raise ValueError("approved must be boolean")
        targets = tuple(self.targets)
        if any(not isinstance(item, TargetSnapshot) for item in targets):
            raise ValueError("targets must contain TargetSnapshot values")
        if len({item.claim_id for item in targets}) != len(targets):
            raise ValueError("claim_id must be unique within a version")
        object.__setattr__(self, "targets", targets)


@dataclass(frozen=True, slots=True)
class ComparisonChange:
    current_claim_id: str | None
    prior_claim_id: str | None
    type: ChangeType
    reason: str

    def to_api_dict(self) -> dict[str, str | None]:
        return {
            "current_claim_id": self.current_claim_id,
            "prior_claim_id": self.prior_claim_id,
            "type": self.type,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class Comparison:
    comparison_id: str
    status: ComparisonStatus
    reason: str | None
    changes: tuple[ComparisonChange, ...]

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "comparison_id": self.comparison_id,
            "status": self.status,
            "reason": self.reason,
            "changes": [item.to_api_dict() for item in self.changes],
        }


def _by_key(targets: tuple[TargetSnapshot, ...]) -> dict[str, tuple[TargetSnapshot, ...]]:
    grouped: dict[str, list[TargetSnapshot]] = {}
    for target in targets:
        grouped.setdefault(target.comparison_key, []).append(target)
    return {key: tuple(values) for key, values in grouped.items()}


def compare_years(
    current: ApprovedVersion,
    prior: ApprovedVersion | None,
    *,
    comparison_id: str,
) -> Comparison:
    """Return candidates only; prior evidence and grades never enter current decisions."""
    _require_uuid("comparison_id", comparison_id)
    if not isinstance(current, ApprovedVersion) or (
        prior is not None and not isinstance(prior, ApprovedVersion)
    ):
        raise ValueError("approved version snapshots are required")
    if not current.approved or (prior is not None and not prior.approved):
        raise ValueError("approved versions are required")
    if prior is None:
        return Comparison(comparison_id, "not_run", "prior_document_version_missing", ())
    if (current.tenant_id, current.company_id) != (prior.tenant_id, prior.company_id):
        raise ValueError("version identity mismatch")
    if current.report_year != prior.report_year + 1:
        return Comparison(
            comparison_id,
            "not_run",
            "prior_document_version_not_previous_year",
            (),
        )

    current_by_key = _by_key(current.targets)
    prior_by_key = _by_key(prior.targets)
    changes: list[ComparisonChange] = []
    for key in sorted(current_by_key):
        current_targets = current_by_key[key]
        prior_targets = prior_by_key.get(key, ())
        if len(current_targets) > 1 or len(prior_targets) > 1:
            changes.extend(
                ComparisonChange(
                    item.claim_id,
                    None,
                    "ambiguous",
                    "comparison_key_not_unique",
                )
                for item in current_targets
            )
            changes.extend(
                ComparisonChange(
                    None,
                    item.claim_id,
                    "ambiguous",
                    "comparison_key_not_unique",
                )
                for item in prior_targets
            )
        elif not prior_targets:
            changes.append(
                ComparisonChange(
                    current_targets[0].claim_id,
                    None,
                    "new",
                    "target_new_in_current_version",
                )
            )
        elif current_targets[0].content_sha256 != prior_targets[0].content_sha256:
            changes.append(
                ComparisonChange(
                    current_targets[0].claim_id,
                    prior_targets[0].claim_id,
                    "modified",
                    "target_content_changed",
                )
            )
    for key in sorted(prior_by_key.keys() - current_by_key.keys()):
        prior_targets = prior_by_key[key]
        if len(prior_targets) > 1:
            changes.extend(
                ComparisonChange(
                    None,
                    item.claim_id,
                    "ambiguous",
                    "comparison_key_not_unique",
                )
                for item in prior_targets
            )
        else:
            changes.append(
                ComparisonChange(
                    None,
                    prior_targets[0].claim_id,
                    "removed_candidate",
                    "target_missing_from_current_version",
                )
            )
    return Comparison(comparison_id, "completed", None, tuple(changes))
