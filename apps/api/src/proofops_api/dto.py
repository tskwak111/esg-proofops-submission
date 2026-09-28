"""API DTO boundary.

Pydantic v2 strict, frozen, extra-forbidding models mirroring the fixed
machine-readable contract (contracts/jsonschema/api_models.schema.json).
The JSONSchema files remain authoritative; these models enforce the same
critical guards in Python so the API layer cannot bypass them:

- RunCreate scope rules: full forbids selected_pages, declared_subset requires
  a non-empty unique sorted 1-based page list.
- Decision grade/label map, blocked-decision null-grade rule, hex hashes,
  PERF/IMPL-only sublabel.
- CompanyCreate legal_name length; Health shape.

Strict mode still rejects bool-as-int and malformed values: the only
pre-validation accepted is UUID-string -> UUID and JSON-array -> tuple, i.e.
exactly what FastAPI decodes from real HTTP JSON bodies. Frozen models are
immutable at the boundary. Grading stays rules-engine-only: no endpoint
accepts caller-supplied grades for tags (see proofops.domain.values).
"""

from __future__ import annotations

import re
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

DtoValidationError = ValidationError

__all__ = [
    "CompanyCreate",
    "Decision",
    "DtoValidationError",
    "Health",
    "RunCreate",
]


def _coerce_uuid(value: Any) -> Any:
    """Accept canonical UUID strings from JSON; strict core validates the rest."""
    if isinstance(value, str):
        return UUID(value)
    return value


def _coerce_tuple(value: Any) -> Any:
    """Accept JSON arrays as tuples; strict core validates elements (bool rejected)."""
    if isinstance(value, list):
        return tuple(value)
    return value


class _StrictDTO(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class RunCreate(_StrictDTO):
    document_version_id: UUID
    mode: Literal["disclosure", "advertising"]
    scope: Literal["full", "declared_subset"]
    selected_pages: tuple[int, ...] | None = None
    rule_pack_id: UUID
    consent_profile_id: UUID
    runtime_binding_id: UUID

    @field_validator(
        "document_version_id",
        "rule_pack_id",
        "consent_profile_id",
        "runtime_binding_id",
        mode="before",
    )
    @classmethod
    def _coerce_uuids(cls, value: Any) -> Any:
        return _coerce_uuid(value)

    @field_validator("selected_pages", mode="before")
    @classmethod
    def _coerce_pages(cls, value: Any) -> Any:
        return _coerce_tuple(value)

    @field_validator("selected_pages")
    @classmethod
    def _check_pages(cls, value: tuple[int, ...] | None) -> tuple[int, ...] | None:
        if value is None:
            return None
        if len(value) == 0:
            raise ValueError("selected_pages must be non-empty")
        if any(page < 1 for page in value):
            raise ValueError("selected_pages must be 1-based page numbers")
        if len(set(value)) != len(value):
            raise ValueError("selected_pages must be unique")
        if tuple(sorted(value)) != value:
            raise ValueError("selected_pages must be sorted")
        return value

    @model_validator(mode="after")
    def _check_scope(self) -> RunCreate:
        if self.scope == "full" and self.selected_pages is not None:
            raise ValueError("scope=full must not carry selected_pages")
        if self.scope == "declared_subset" and self.selected_pages is None:
            raise ValueError("scope=declared_subset requires selected_pages")
        return self


class GradeRange(_StrictDTO):
    """Reachable ladder grades while evidence is unresolved; never a grade (engine v3+)."""

    floor: Literal["E0", "E1", "E2", "E3"]
    ceiling: Literal["E0", "E1", "E2", "E3"]
    open_elements: tuple[str, ...]

    @field_validator("open_elements", mode="before")
    @classmethod
    def _coerce_lists(cls, value: Any) -> Any:
        return _coerce_tuple(value)

    @model_validator(mode="after")
    def _check_order(self) -> GradeRange:
        if self.floor > self.ceiling or not self.open_elements:
            raise ValueError("grade_range needs floor <= ceiling and open elements")
        return self


class Decision(_StrictDTO):
    decision_revision: int
    tag_revision: int
    decision_status: Literal[
        "decided", "blocked_evidence", "blocked_rule_gap", "not_applicable", "not_run"
    ]
    evidence_grade: Literal["E0", "E1", "E2", "E3"] | None
    label: Literal["SUBSTANTIATED", "INCOMPLETE", "UNSUBSTANTIATED"] | None
    sublabel: Literal["PERF", "IMPL"] | None = None
    review_status: Literal[
        "auto_confirmed", "needs_review", "human_confirmed", "ai_delegated_confirmed"
    ]
    missing_elements: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    rule_pack_sha256: str
    semantic_hash: str
    gap_ids: tuple[str, ...] = ()
    grade_range: GradeRange | None = None

    @field_validator("missing_elements", "rule_ids", "gap_ids", mode="before")
    @classmethod
    def _coerce_lists(cls, value: Any) -> Any:
        return _coerce_tuple(value)

    @field_validator("decision_revision", "tag_revision")
    @classmethod
    def _check_revision(cls, value: int) -> int:
        if value < 1:
            raise ValueError("revisions must be >= 1")
        return value

    @field_validator("rule_pack_sha256", "semantic_hash")
    @classmethod
    def _check_hex64(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("must be a 64-char lowercase hex sha256")
        return value

    @model_validator(mode="after")
    def _check_grade_label(self) -> Decision:
        grade_to_label = {
            "E0": "UNSUBSTANTIATED",
            "E1": "INCOMPLETE",
            "E2": "INCOMPLETE",
            "E3": "SUBSTANTIATED",
        }
        if self.decision_status == "decided":
            if self.grade_range is not None:
                raise ValueError("decided decisions carry a grade, not a grade_range")
            if self.evidence_grade is None or self.label is None:
                raise ValueError("decided requires evidence_grade and label")
            if grade_to_label[self.evidence_grade] != self.label:
                raise ValueError(
                    f"grade/label mismatch: {self.evidence_grade} "
                    f"maps to {grade_to_label[self.evidence_grade]}"
                )
        else:
            if (
                self.evidence_grade is not None
                or self.label is not None
                or self.sublabel is not None
            ):
                raise ValueError(f"{self.decision_status} must carry null grade/label/sublabel")
        return self


class CompanyCreate(_StrictDTO):
    legal_name: str
    registration_identifier: str | None = None
    aliases: tuple[str, ...]

    @field_validator("aliases", mode="before")
    @classmethod
    def _coerce_aliases(cls, value: Any) -> Any:
        return _coerce_tuple(value)

    @field_validator("legal_name")
    @classmethod
    def _check_legal_name(cls, value: str) -> str:
        if not 1 <= len(value) <= 200:
            raise ValueError("legal_name must be 1..200 chars")
        return value

    @field_validator("registration_identifier")
    @classmethod
    def _check_registration(cls, value: str | None) -> str | None:
        if value is not None and len(value) > 100:
            raise ValueError("registration_identifier must be at most 100 chars")
        return value


class Health(_StrictDTO):
    status: Literal["ok", "degraded", "not_ready"]
    version: str
    checks: tuple[str, ...] = ()

    @field_validator("checks", mode="before")
    @classmethod
    def _coerce_checks(cls, value: Any) -> Any:
        return _coerce_tuple(value)

    @field_validator("version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not value:
            raise ValueError("version must be non-empty")
        return value
