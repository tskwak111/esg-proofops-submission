"""Bounded manual preliminary classification: reviewer input, never a grade.

Pure validator. An authorized reviewer (or an explicitly delegated local AI method)
supplies the preliminary track/category/dimension source spans for a source-verified
claim stopped at PRELIMINARY_TAGS_UNRESOLVED. Reuses the exact literal-source guards of
``validate_preliminary`` (``_literal_dimension_ref`` + ``verify_source_ref``) and the
strict track boundary (``validate_track_candidates``). Never a grade/label/decision and
never a fabricated model confidence (``track_confidence`` is always ``None``); a null
axis stays unknown; a source-unverified or ambiguous span is rejected; unknown axes are
rejected and the required roles cannot be omitted.

The record stores the reviewer's original ``{source_index, quote[, start, end]}``
selection (not a resolved SourceRef), so replay re-runs the identical literal guard
against the pinned graph -- a stored selection can never widen its index space or borrow
another table row's cell. The record is content-addressed (``record_sha256``) and pins
tenant/run/document/claim/parse-manifest/source/graph/claim hashes and the run's
committed tag checkpoint.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict

from proofops.application.claims import Claim
from proofops.application.evidence.binding import ClaimContext
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.application.tagging.preliminary import (
    PreliminaryClassification,
    _literal_dimension_ref,
    _sources,
)
from proofops.application.tagging.tracks import validate_track_candidates
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SAFE_HARBOR_CATEGORIES, TRACKS, _require_uuid

SCHEMA = "manual-preliminary-classification-v1"
HUMAN_ORIGIN = "human_classification"
AI_DELEGATED_ORIGIN = "ai_delegated_classification"
AI_DELEGATED_REVIEWER_PREFIX = "ai-delegated-classification:"
AI_DELEGATED_REVIEW_ORIGIN = "ai_project_interpretation"
_ORIGINS = frozenset({HUMAN_ORIGIN, AI_DELEGATED_ORIGIN})

REQUIRED_AXES = ("entity", "metric", "reporting_period")
OPTIONAL_AXES = ("facility", "scope", "product", "material", "boundary")
ALLOWED_AXES = frozenset(REQUIRED_AXES) | frozenset(OPTIONAL_AXES)
_SPAN_KEYS = ({"source_index", "quote"}, {"source_index", "start", "end", "quote"})
_BODY_KEYS = frozenset({"track", "safe_harbor_category", "dimensions", "reason"})


class ClassificationRejected(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code, self.status = code, status


def _validate_body_shape(body: Mapping) -> None:
    if not isinstance(body, Mapping) or set(body) != _BODY_KEYS:
        raise ClassificationRejected("VALIDATION_ERROR")
    track, category, dims, reason = (
        body["track"],
        body["safe_harbor_category"],
        body["dimensions"],
        body["reason"],
    )
    if (
        not isinstance(track, str)
        or track not in TRACKS
        or (
            category is not None
            and (not isinstance(category, str) or category not in SAFE_HARBOR_CATEGORIES)
        )
        or not isinstance(reason, str)
        or not 5 <= len(reason.strip()) <= 1000
        or not isinstance(dims, Mapping)
    ):
        raise ClassificationRejected("VALIDATION_ERROR")
    axes = set(dims)
    if axes - ALLOWED_AXES or not set(REQUIRED_AXES) <= axes:
        raise ClassificationRejected("VALIDATION_ERROR")
    for span in dims.values():
        if span is None:
            continue
        if not isinstance(span, Mapping) or set(span) not in _SPAN_KEYS:
            raise ClassificationRejected("VALIDATION_ERROR")


def validate_manual_classification(
    claim: Claim, graph: CanonicalDocumentGraph, body: Mapping, *, tenant_id: str
) -> PreliminaryClassification:
    """Validate a reviewer's manual classification into a PreliminaryClassification."""
    _require_uuid("tenant_id", tenant_id)
    _validate_body_shape(body)
    try:
        sources = _sources(claim, graph, tenant_id)
    except DomainValidationError as error:
        raise ClassificationRejected("CLASSIFICATION_SOURCE_REJECTED") from error
    try:
        track = validate_track_candidates(
            (claim,),
            [
                {
                    "claim_id": claim.claim_id,
                    "track": body["track"],
                    "safe_harbor_category": body["safe_harbor_category"],
                }
            ],
        )[0]
    except DomainValidationError as error:
        raise ClassificationRejected("VALIDATION_ERROR") from error
    dimensions: dict[str, object] = {}
    for name, span in body["dimensions"].items():
        if span is None:
            dimensions[name] = None
            continue
        try:
            dimensions[name] = _literal_dimension_ref(
                span, sources, graph, tenant_id=tenant_id, allow_offsets=True
            )
        except DomainValidationError as error:
            raise ClassificationRejected("CLASSIFICATION_SOURCE_REJECTED") from error
    return PreliminaryClassification(
        track, ClaimContext(claim, dimensions), None, body["safe_harbor_category"]
    )


def _record_body(record: Mapping) -> dict:
    """The reviewer's original selection body, reconstructed from the stored record."""
    return {
        "track": record["track"],
        "safe_harbor_category": record["safe_harbor_category"],
        "dimensions": {name: value for name, value in record["dimensions"].items()},
        "reason": record["reason"],
    }


def _selection_from_ref(ref, sources) -> dict:
    """Invert a resolved dimension ref back to its ``{source_index, start, end, quote}``.

    The numbered source it belongs to is the claim source with the same ``source_id``;
    offsets are relative to that source's own quote span, exactly the space
    ``_literal_dimension_ref`` consumes on replay.
    """
    for index, source in enumerate(sources):
        if (
            source.source_id == ref.source_id
            and source.char_start <= ref.char_start
            and ref.char_end <= source.char_end
        ):
            start = ref.char_start - source.char_start
            end = ref.char_end - source.char_start
            if source.quote[start:end] == ref.quote:
                return {"source_index": index, "start": start, "end": end, "quote": ref.quote}
    raise ClassificationRejected("CLASSIFICATION_SOURCE_REJECTED")


def classification_snapshot(
    classification: PreliminaryClassification,
    *,
    tenant_id: str,
    run_id: str,
    claim: Claim,
    graph: CanonicalDocumentGraph,
    lineage_checkpoint_sha256: str,
    origin: str,
    classified_by: str,
    reason: str,
    delegation_authority: str | None = None,
) -> dict:
    """Content-addressable manual-classification record pinned to its full lineage.

    Each non-null dimension is stored as the reviewer's *selection*
    (source_index/start/end/quote) inverted from the resolved ref, not the resolved
    SourceRef, so replay re-runs the same literal guard against the pinned graph and a
    stored selection can never widen its index space or borrow another row.
    """
    if origin not in _ORIGINS:
        raise ClassificationRejected("VALIDATION_ERROR")
    if not isinstance(classified_by, str) or not classified_by.strip():
        raise ClassificationRejected("VALIDATION_ERROR")
    if not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 1000:
        raise ClassificationRejected("VALIDATION_ERROR")
    if classification.track_confidence is not None:
        raise ClassificationRejected("VALIDATION_ERROR")
    sources = _sources(claim, graph, tenant_id)
    dimensions = {
        name: (_selection_from_ref(ref, sources) if ref is not None else None)
        for name, ref in classification.context.dimensions.items()
    }
    record = {
        "schema": SCHEMA,
        "tenant_id": tenant_id,
        "run_id": run_id,
        "document_version_id": claim.document_version_id,
        "claim_id": claim.claim_id,
        "parse_manifest_id": claim.parse_manifest_id,
        "source_sha256": claim.source_sha256,
        "graph_sha256": canonical_hash(asdict(graph)),
        "claim_sha256": canonical_hash(asdict(claim)),
        "lineage_checkpoint_sha256": lineage_checkpoint_sha256,
        "track": classification.track.track,
        "safe_harbor_category": classification.safe_harbor_category,
        "track_confidence": None,
        "dimensions": dimensions,
        "origin": origin,
        "classified_by": classified_by,
        "review_origin": (AI_DELEGATED_REVIEW_ORIGIN if origin == AI_DELEGATED_ORIGIN else None),
        "delegation_authority": (
            delegation_authority.strip()
            if origin == AI_DELEGATED_ORIGIN and isinstance(delegation_authority, str)
            else None
        ),
        "reason": reason.strip(),
        "revision": 1,
    }
    if origin == AI_DELEGATED_ORIGIN and not record["delegation_authority"]:
        raise ClassificationRejected("VALIDATION_ERROR")
    record["record_sha256"] = canonical_hash(record)
    return record


def classification_override(
    record: Mapping, claim: Claim, graph: CanonicalDocumentGraph, *, tenant_id: str
):
    """Rebuild the reviewer's classification from a stored record, re-checking everything.

    Validates the content-address (``record_sha256``), the full lineage pins
    (tenant/document/claim/parse-manifest/source/graph/claim hashes and origin) against
    the live claim+graph, and re-runs the literal source guard against the recomputed
    numbered sources -- so a changed digest/pin or a dimension borrowed from another
    table row is rejected, and a stored selection can never widen its own index space.
    """
    if not isinstance(record, Mapping) or record.get("schema") != SCHEMA:
        raise ClassificationRejected("VALIDATION_ERROR", 409)
    stored_digest = record.get("record_sha256")
    if stored_digest != canonical_hash({k: v for k, v in record.items() if k != "record_sha256"}):
        raise ClassificationRejected("VALIDATION_ERROR", 409)
    if record.get("origin") not in _ORIGINS or record.get("track_confidence") is not None:
        raise ClassificationRejected("VALIDATION_ERROR", 409)
    if (
        record.get("tenant_id") != tenant_id
        or record.get("claim_id") != claim.claim_id
        or record.get("document_version_id") != claim.document_version_id
        or record.get("parse_manifest_id") != claim.parse_manifest_id
        or record.get("source_sha256") != claim.source_sha256
        or record.get("graph_sha256") != canonical_hash(asdict(graph))
        or record.get("claim_sha256") != canonical_hash(asdict(claim))
    ):
        raise ClassificationRejected("VALIDATION_ERROR", 409)
    # Re-run the identical literal guard the reviewer passed; membership in the claim's
    # own numbered sources is enforced by _literal_dimension_ref's source_index bound.
    try:
        return validate_manual_classification(
            claim, graph, _record_body(record), tenant_id=tenant_id
        )
    except ClassificationRejected as error:
        raise ClassificationRejected(error.code, 409) from error


__all__ = [
    "SCHEMA",
    "HUMAN_ORIGIN",
    "AI_DELEGATED_ORIGIN",
    "AI_DELEGATED_REVIEWER_PREFIX",
    "AI_DELEGATED_REVIEW_ORIGIN",
    "REQUIRED_AXES",
    "OPTIONAL_AXES",
    "ALLOWED_AXES",
    "ClassificationRejected",
    "validate_manual_classification",
    "classification_snapshot",
    "classification_override",
]
