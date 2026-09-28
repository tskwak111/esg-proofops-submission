"""Typed immutable domain values.

Pure module: stdlib only (dataclasses/typing/math/uuid). No AWS SDK, no
network, no file access, no environment variables, no scripts/legacy/sources
imports, no pydantic (the API DTO boundary owns that dependency).

Covers the minimum needed by later tasks:
- ElementState / Track / grade-label map (docs/28, contracts/jsonschema)
- SourceRef / LlmElement / LlmTags with the source-less-present guard and the
  LLM grade/label rejection boundary (grading is rules-engine only).

Trust boundary: `llm_tags_from_dict` enforces the fixed contract exactly --
unknown keys rejected (schema additionalProperties=false), required fields
enforced, UUIDs validated, bools rejected as ints, non-finite bbox rejected,
and every sequence defensively copied to an immutable tuple so frozen
dataclasses never alias caller-mutable lists.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Final, Literal
from uuid import UUID

from proofops.domain.errors import DomainValidationError

ElementState = Literal["present", "absent", "unknown", "conflict", "not_applicable"]
Track = Literal["goal", "performance", "management"]
LocationQuality = Literal["located", "unlocated", "unreadable"]
VerificationState = Literal["verified", "candidate", "rejected"]

ELEMENT_STATES: Final = ("present", "absent", "unknown", "conflict", "not_applicable")
TRACKS: Final = ("goal", "performance", "management")

GRADE_LABEL_MAP: Final = {
    "E0": "UNSUBSTANTIATED",
    "E1": "INCOMPLETE",
    "E2": "INCOMPLETE",
    "E3": "SUBSTANTIATED",
}

SAFE_HARBOR_CATEGORIES: Final = (
    "forward_looking",
    "emissions_estimate",
    "third_party_information",
)

# Fields the LLM boundary must never accept: grades/labels are computed only
# by the pure Python rules engine (docs/28) or a human-confirmed rescore.
_FORBIDDEN_LLM_FIELDS: Final = frozenset({"evidence_grade", "label", "sublabel", "decision_status"})

_ROOT_FIELDS: Final = frozenset(
    {
        "claim_id",
        "packet_sha256",
        "replicate_id",
        "track",
        "safe_harbor_category",
        "elements",
        "superlative_quote",
        "warnings",
    }
)
_ELEMENT_FIELDS: Final = frozenset(
    {
        "element_id",
        "state",
        "evidence_refs",
        "normalized_value",
        "credited_from",
        "reason_code",
    }
)
_SOURCE_FIELDS: Final = frozenset(
    {
        "source_id",
        "document_version_id",
        "parse_manifest_id",
        "page_num",
        "printed_page_label",
        "bbox",
        "raw_text_sha256",
        "quote",
        "char_start",
        "char_end",
        "location_quality",
        "verification_state",
    }
)


def _require_uuid(name: str, value: object) -> str:
    if not isinstance(value, str):
        raise DomainValidationError(f"{name} must be a UUID string")
    try:
        parsed = UUID(value)
    except ValueError:
        raise DomainValidationError(f"{name} must be a valid UUID") from None
    if str(parsed) != value.lower():
        raise DomainValidationError(f"{name} must be a canonical UUID string")
    return value


def _require_strict_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainValidationError(f"{name} must be an int (bool not accepted)")
    return value


def _require_sha256(name: str, value: object) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise DomainValidationError(f"{name} must be a 64-char lowercase hex sha256")
    if any(c not in "0123456789abcdef" for c in value):
        raise DomainValidationError(f"{name} must be a 64-char lowercase hex sha256")
    return value


def _require_bbox(value: object) -> tuple[float, float, float, float] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple) or len(value) != 4:
        raise DomainValidationError("bbox must have exactly 4 coordinates")
    coords: list[float] = []
    for coord in value:
        if isinstance(coord, bool) or not isinstance(coord, int | float):
            raise DomainValidationError("bbox coordinates must be numbers")
        number = float(coord)
        if not math.isfinite(number):
            raise DomainValidationError("bbox coordinates must be finite")
        coords.append(number)
    x0, y0, x1, y1 = coords
    if not (x0 < x1 and y0 < y1):
        raise DomainValidationError("bbox must satisfy x0 < x1 and y0 < y1")
    return (x0, y0, x1, y1)


@dataclass(frozen=True, slots=True)
class SourceRef:
    """Immutable provenance pointer. Missing geometry is None, never zero-bbox."""

    source_id: str
    document_version_id: str
    parse_manifest_id: str
    page_num: int
    printed_page_label: str | None
    bbox: tuple[float, float, float, float] | None
    raw_text_sha256: str
    quote: str
    char_start: int
    char_end: int
    location_quality: LocationQuality
    verification_state: VerificationState

    def __post_init__(self) -> None:
        _require_uuid("source_id", self.source_id)
        _require_uuid("document_version_id", self.document_version_id)
        _require_uuid("parse_manifest_id", self.parse_manifest_id)
        _require_strict_int("page_num", self.page_num)
        if self.page_num < 1:
            raise DomainValidationError("page_num must be >= 1")
        _require_strict_int("char_start", self.char_start)
        _require_strict_int("char_end", self.char_end)
        if self.char_start < 0 or self.char_end < self.char_start:
            raise DomainValidationError("char offsets must satisfy 0 <= start <= end")
        if self.location_quality not in ("located", "unlocated", "unreadable"):
            raise DomainValidationError(f"unknown location_quality: {self.location_quality}")
        if self.verification_state not in ("verified", "candidate", "rejected"):
            raise DomainValidationError(f"unknown verification_state: {self.verification_state}")
        object.__setattr__(self, "bbox", _require_bbox(self.bbox))
        _require_sha256("raw_text_sha256", self.raw_text_sha256)
        if not isinstance(self.quote, str):
            raise DomainValidationError("quote must be a string")


@dataclass(frozen=True, slots=True)
class LlmElement:
    """Single tagged element. present requires >= 1 evidence ref (contract)."""

    element_id: str
    state: ElementState
    evidence_refs: tuple[SourceRef, ...]
    normalized_value: str | None
    credited_from: str | None
    reason_code: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.element_id, str) or not self.element_id:
            raise DomainValidationError("element_id must be a non-empty string")
        if self.state not in ELEMENT_STATES:
            raise DomainValidationError(f"unknown element state: {self.state}")
        refs = tuple(self.evidence_refs)
        if any(not isinstance(ref, SourceRef) for ref in refs):
            raise DomainValidationError("evidence_refs must all be SourceRef")
        object.__setattr__(self, "evidence_refs", refs)
        if self.state == "present" and len(refs) == 0:
            raise DomainValidationError(
                "state=present requires at least one evidence_ref "
                "(source-less present is never accepted)"
            )


@dataclass(frozen=True, slots=True)
class LlmTags:
    """One replicate of tagging output. Carries no grade/label by construction."""

    claim_id: str
    packet_sha256: str
    replicate_id: int
    track: Track
    safe_harbor_category: str | None
    elements: tuple[LlmElement, ...] = field(default_factory=tuple)
    superlative_quote: str | None = None
    warnings: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        _require_uuid("claim_id", self.claim_id)
        _require_sha256("packet_sha256", self.packet_sha256)
        _require_strict_int("replicate_id", self.replicate_id)
        if self.replicate_id not in (1, 2, 3):
            raise DomainValidationError("replicate_id must be 1, 2, or 3")
        if self.track not in TRACKS:
            raise DomainValidationError(f"unknown track: {self.track}")
        if (
            self.safe_harbor_category is not None
            and self.safe_harbor_category not in SAFE_HARBOR_CATEGORIES
        ):
            raise DomainValidationError(
                f"unknown safe_harbor_category: {self.safe_harbor_category}"
            )
        elements = tuple(self.elements)
        if any(not isinstance(element, LlmElement) for element in elements):
            raise DomainValidationError("elements must all be LlmElement")
        object.__setattr__(self, "elements", elements)
        warnings = tuple(self.warnings)
        if any(not isinstance(warning, str) for warning in warnings):
            raise DomainValidationError("warnings must all be strings")
        object.__setattr__(self, "warnings", warnings)


def _reject_unknown(what: str, data: dict[str, Any], allowed: frozenset[str]) -> None:
    unknown = set(data.keys()) - allowed
    if unknown:
        raise DomainValidationError(f"{what} carries unknown fields: {sorted(unknown)}")


def _require_keys(what: str, data: dict[str, Any], required: frozenset[str]) -> None:
    missing = required - set(data.keys())
    if missing:
        raise DomainValidationError(f"{what} is missing required fields: {sorted(missing)}")


def _source_ref_from_dict(data: Any) -> SourceRef:
    if not isinstance(data, dict):
        raise DomainValidationError("source ref must be an object")
    _reject_unknown("source ref", data, _SOURCE_FIELDS)
    _require_keys("source ref", data, _SOURCE_FIELDS)
    printed = data["printed_page_label"]
    if printed is not None and not isinstance(printed, str):
        raise DomainValidationError("printed_page_label must be a string or null")
    return SourceRef(
        source_id=data["source_id"],
        document_version_id=data["document_version_id"],
        parse_manifest_id=data["parse_manifest_id"],
        page_num=data["page_num"],
        printed_page_label=printed,
        bbox=data["bbox"],
        raw_text_sha256=data["raw_text_sha256"],
        quote=data["quote"],
        char_start=data["char_start"],
        char_end=data["char_end"],
        location_quality=data["location_quality"],
        verification_state=data["verification_state"],
    )


def _element_from_dict(data: Any) -> LlmElement:
    if not isinstance(data, dict):
        raise DomainValidationError("element must be an object")
    forbidden = _FORBIDDEN_LLM_FIELDS.intersection(data.keys())
    if forbidden:
        raise DomainValidationError(
            f"LLM element must not carry grading fields: {sorted(forbidden)}"
        )
    _reject_unknown("element", data, _ELEMENT_FIELDS)
    _require_keys("element", data, _ELEMENT_FIELDS)
    refs = data["evidence_refs"]
    if not isinstance(refs, list | tuple):
        raise DomainValidationError("evidence_refs must be an array")
    for key in ("normalized_value", "credited_from", "reason_code"):
        if data[key] is not None and not isinstance(data[key], str):
            raise DomainValidationError(f"{key} must be a string or null")
    credited = data["credited_from"]
    if credited is not None:
        _require_uuid("credited_from", credited)
    return LlmElement(
        element_id=data["element_id"],
        state=data["state"],
        evidence_refs=tuple(_source_ref_from_dict(ref) for ref in refs),
        normalized_value=data["normalized_value"],
        credited_from=credited,
        reason_code=data["reason_code"],
    )


def llm_tags_from_dict(data: Any) -> LlmTags:
    """Parse untrusted tagger output. Rejects grade/label fields outright."""
    if not isinstance(data, dict):
        raise DomainValidationError("LLM tag payload must be an object")
    forbidden = _FORBIDDEN_LLM_FIELDS.intersection(data.keys())
    if forbidden:
        raise DomainValidationError(
            f"LLM tag payload must not carry grading fields: {sorted(forbidden)} "
            "(grades/labels are computed by the rules engine only)"
        )
    _reject_unknown("LLM tag payload", data, _ROOT_FIELDS)
    _require_keys("LLM tag payload", data, _ROOT_FIELDS)
    elements = data["elements"]
    if not isinstance(elements, list | tuple):
        raise DomainValidationError("elements must be an array")
    warnings = data["warnings"]
    if not isinstance(warnings, list | tuple):
        raise DomainValidationError("warnings must be an array")
    category = data["safe_harbor_category"]
    if category is not None and not isinstance(category, str):
        raise DomainValidationError("safe_harbor_category must be a string or null")
    quote = data["superlative_quote"]
    if quote is not None and not isinstance(quote, str):
        raise DomainValidationError("superlative_quote must be a string or null")
    return LlmTags(
        claim_id=data["claim_id"],
        packet_sha256=data["packet_sha256"],
        replicate_id=data["replicate_id"],
        track=data["track"],
        safe_harbor_category=category,
        elements=tuple(_element_from_dict(element) for element in elements),
        superlative_quote=quote,
        warnings=tuple(warnings),
    )
