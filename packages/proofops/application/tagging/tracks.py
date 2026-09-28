"""Strict track/category candidate boundary; no model call or grading."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from proofops.application.claims import Claim
from proofops.domain.errors import DomainValidationError
from proofops.domain.values import SAFE_HARBOR_CATEGORIES, TRACKS, SourceRef, Track

_FIELDS = frozenset({"claim_id", "track", "safe_harbor_category"})


@dataclass(frozen=True, slots=True)
class TrackCandidate:
    """A validated classification that retains its immutable source claim."""

    claim: Claim
    track: Track
    safe_harbor_category: str | None


def validate_track_candidates(
    claims: Sequence[Claim], structured_candidates: Sequence[Mapping[str, Any]]
) -> tuple[TrackCandidate, ...]:
    """Validate one structured candidate per claim without deriving track from topic."""
    if not isinstance(claims, Sequence) or isinstance(claims, str | bytes):
        raise DomainValidationError("claims must be an array")
    if not isinstance(structured_candidates, Sequence) or isinstance(
        structured_candidates, str | bytes
    ):
        raise DomainValidationError("structured candidates must be an array")

    claim_by_id: dict[str, Claim] = {}
    identity: tuple[str, str, str, str] | None = None
    for claim in claims:
        if not isinstance(claim, Claim):
            raise DomainValidationError("claims must contain Claim values")
        current = (
            claim.tenant_id,
            claim.document_version_id,
            claim.parse_manifest_id,
            claim.source_sha256,
        )
        if identity is None:
            identity = current
        elif current != identity:
            raise DomainValidationError("claims must share tenant and source identity")
        if claim.claim_id in claim_by_id:
            raise DomainValidationError("duplicate claim_id")
        if not claim.source_refs or any(
            not isinstance(ref, SourceRef)
            or (ref.document_version_id, ref.parse_manifest_id)
            != (claim.document_version_id, claim.parse_manifest_id)
            for ref in claim.source_refs
        ):
            raise DomainValidationError("claim source provenance is missing or mismatched")
        claim_by_id[claim.claim_id] = claim

    by_id: dict[str, TrackCandidate] = {}
    for item in structured_candidates:
        if not isinstance(item, Mapping) or set(item) != _FIELDS:
            raise DomainValidationError("invalid track candidate fields; grades are prohibited")
        claim_id, track, category = (
            item["claim_id"],
            item["track"],
            item["safe_harbor_category"],
        )
        if not isinstance(claim_id, str) or claim_id not in claim_by_id:
            raise DomainValidationError("track candidate references an unknown claim")
        if claim_id in by_id:
            raise DomainValidationError("duplicate track candidate")
        if not isinstance(track, str) or track not in TRACKS:
            raise DomainValidationError("invalid track")
        if category is not None and (
            not isinstance(category, str) or category not in SAFE_HARBOR_CATEGORIES
        ):
            raise DomainValidationError("invalid safe harbor category")
        by_id[claim_id] = TrackCandidate(claim_by_id[claim_id], cast(Track, track), category)

    if by_id.keys() != claim_by_id.keys():
        raise DomainValidationError("every claim requires exactly one track candidate")
    return tuple(by_id[claim.claim_id] for claim in claims)
