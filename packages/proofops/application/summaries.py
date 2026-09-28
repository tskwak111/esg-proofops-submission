"""Pure run summary projection; grades are consumed, never calculated here."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, cast

from proofops.domain.values import _require_strict_int, _require_uuid

Grade = Literal["E0", "E1", "E2", "E3"]
Applicability = Literal["applicable", "N_A", "undetermined"]
_GRADES = ("E0", "E1", "E2", "E3")
_LABELS = {
    "E0": "UNSUBSTANTIATED",
    "E1": "INCOMPLETE",
    "E2": "INCOMPLETE",
    "E3": "SUBSTANTIATED",
}
_DECISION_STATUSES = frozenset(
    {"decided", "blocked_evidence", "blocked_rule_gap", "not_applicable", "not_run"}
)
_COVERAGE_FIELDS = frozenset(
    {
        "pages_total",
        "pages_processed",
        "pages_unreadable",
        "pages_unprocessed",
        "chunks_discovered",
        "chunks_processed",
        "claims_discovered",
        "claims_decided",
        "claims_needs_review",
        "full_scope",
        "complete",
    }
)


@dataclass(frozen=True, slots=True)
class RequirementStatus:
    """One approved requirement-instance result for denominator accounting."""

    element_id: str
    applicability: Applicability
    satisfied: bool | None
    deferred: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.element_id, str) or not self.element_id.strip():
            raise ValueError("element_id must be non-empty text")
        if self.applicability not in ("applicable", "N_A", "undetermined"):
            raise ValueError("invalid applicability")
        if type(self.deferred) is not bool:
            raise ValueError("deferred must be boolean")
        if self.deferred:
            if self.applicability != "applicable" or self.satisfied is not None:
                raise ValueError("deferred requirements are excluded from the known denominator")
        elif self.applicability == "applicable":
            if type(self.satisfied) is not bool:
                raise ValueError("known applicable requirements need a satisfaction result")
        elif self.satisfied is not None:
            raise ValueError("excluded or undetermined requirements cannot be satisfied")


@dataclass(frozen=True, slots=True)
class Summary:
    run_id: str
    snapshot_epoch: int
    coverage: dict[str, int | bool]
    grade_counts: dict[str, int]
    undecided_count: int
    not_applicable_count: int
    deferred_count: int
    unverified_basis_count: int
    missing_by_element: tuple[tuple[str, int], ...]
    applicable_count: int
    satisfied_count: int
    fulfillment_rate: float | None
    undetermined_applicability_count: int | None

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "snapshot_epoch": self.snapshot_epoch,
            "coverage": dict(self.coverage),
            "grade_counts": dict(self.grade_counts),
            "undecided_count": self.undecided_count,
            "not_applicable_count": self.not_applicable_count,
            "deferred_count": self.deferred_count,
            "unverified_basis_count": self.unverified_basis_count,
            "missing_by_element": [
                {"element_id": element_id, "count": count}
                for element_id, count in self.missing_by_element
            ],
            "applicable_count": self.applicable_count,
            "satisfied_count": self.satisfied_count,
            "fulfillment_rate": self.fulfillment_rate,
            "undetermined_applicability_count": self.undetermined_applicability_count,
        }


def _validated_coverage(value: Mapping[str, object]) -> dict[str, int | bool]:
    if not isinstance(value, Mapping) or set(value) != _COVERAGE_FIELDS:
        raise ValueError("coverage must match the fixed Coverage contract")
    raw_counts = {key: value[key] for key in _COVERAGE_FIELDS - {"full_scope", "complete"}}
    if any(type(count) is not int or count < 0 for count in raw_counts.values()):
        raise ValueError("coverage counts must be non-negative integers")
    counts = cast(dict[str, int], raw_counts)
    if type(value["full_scope"]) is not bool or type(value["complete"]) is not bool:
        raise ValueError("coverage scope and completeness must be boolean")
    if counts["pages_total"] != (
        counts["pages_processed"] + counts["pages_unreadable"] + counts["pages_unprocessed"]
    ):
        raise ValueError("page coverage must partition pages_total")
    if counts["chunks_processed"] > counts["chunks_discovered"] or (
        counts["claims_decided"] + counts["claims_needs_review"] > counts["claims_discovered"]
    ):
        raise ValueError("processed coverage cannot exceed discovered coverage")
    if value["complete"] and (
        not value["full_scope"]
        or counts["pages_unreadable"]
        or counts["pages_unprocessed"]
        or counts["chunks_processed"] != counts["chunks_discovered"]
        or counts["claims_decided"] + counts["claims_needs_review"] != counts["claims_discovered"]
    ):
        raise ValueError("incomplete coverage cannot carry a complete flag")
    return cast(dict[str, int | bool], dict(value))


def _basis_statuses(decision: Mapping[str, object]) -> Iterable[str]:
    refs = decision.get("basis_refs", ())
    if not isinstance(refs, list | tuple):
        raise ValueError("basis_refs must be an array")
    for ref in refs:
        if isinstance(ref, str):
            try:
                ref = json.loads(ref)
            except json.JSONDecodeError as exc:
                raise ValueError("basis_refs must contain valid JSON") from exc
        if not isinstance(ref, Mapping):
            raise ValueError("basis_refs must contain objects")
        basis = ref.get("basis", ref)
        if not isinstance(basis, Mapping):
            raise ValueError("basis reference is malformed")
        status = basis.get("verification_status")
        if status is not None:
            if status not in ("verified", "unverified", "unlicensed"):
                raise ValueError("invalid basis verification status")
            yield status


def summarize_snapshot(
    *,
    run_id: str,
    snapshot_epoch: int,
    coverage: Mapping[str, object],
    decisions: Iterable[Mapping[str, object] | None],
    applicability: Iterable[RequirementStatus] | None,
) -> Summary:
    """Aggregate one frozen read without treating unresolved work as an E grade."""
    _require_uuid("run_id", run_id)
    if _require_strict_int("snapshot_epoch", snapshot_epoch) < 0:
        raise ValueError("snapshot_epoch must be non-negative")
    coverage = _validated_coverage(coverage)
    decisions = tuple(decisions)
    if len(decisions) != coverage["claims_discovered"]:
        raise ValueError("every discovered claim must be represented in the summary snapshot")

    grades = Counter({grade: 0 for grade in _GRADES})
    missing: Counter[str] = Counter()
    undecided = unverified = 0
    for decision in decisions:
        if decision is None:
            undecided += 1
            continue
        if not isinstance(decision, Mapping):
            raise ValueError("decisions must be mappings or null")
        status, grade, label = (
            decision.get("decision_status"),
            decision.get("evidence_grade"),
            decision.get("label"),
        )
        if status not in _DECISION_STATUSES:
            raise ValueError("invalid decision status")
        if status == "decided":
            if grade not in _GRADES or label != _LABELS[grade]:
                raise ValueError("decided claims require the fixed grade/label mapping")
            grades[grade] += 1
        else:
            if grade is not None or label is not None:
                raise ValueError("undecided claims cannot carry a grade or label")
            undecided += 1
        elements = decision.get("missing_elements", ())
        if not isinstance(elements, list | tuple) or any(
            not isinstance(element, str) or not element for element in elements
        ):
            raise ValueError("missing_elements must contain non-empty strings")
        missing.update(set(elements))
        unverified += sum(status != "verified" for status in _basis_statuses(decision))

    if applicability is None:
        applicable = satisfied = excluded = deferred = 0
        undetermined: int | None = None
    else:
        requirements = tuple(applicability)
        if any(not isinstance(item, RequirementStatus) for item in requirements):
            raise ValueError("applicability must contain RequirementStatus values")
        applicable = sum(
            item.applicability == "applicable" and not item.deferred for item in requirements
        )
        satisfied = sum(item.satisfied is True for item in requirements)
        excluded = sum(item.applicability == "N_A" for item in requirements)
        deferred = sum(item.deferred for item in requirements)
        undetermined = sum(item.applicability == "undetermined" for item in requirements)

    return Summary(
        run_id,
        snapshot_epoch,
        coverage,
        {grade: grades[grade] for grade in _GRADES},
        undecided,
        excluded,
        deferred,
        unverified,
        tuple(sorted(missing.items())),
        applicable,
        satisfied,
        satisfied / applicable if applicable else None,
        undetermined,
    )
