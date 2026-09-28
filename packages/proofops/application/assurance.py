"""Source-backed assurance extraction and deterministic scope matching.

`tagged_fields` is an extraction/tagging result, never an LLM coverage decision.
The trusted internal graph comes from the tenant-scoped source store; a v1
SourceRef's verification flag alone cannot approve evidence. This module does
no I/O or model invocation. Synthetic model/parser provenance remains explicit.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from hashlib import sha256
from typing import Any

from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.application.ports.models import ModelBinding
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef, _require_sha256, _require_uuid

SCALARS = ("provider", "standard_raw", "level", "reporting_period")
LISTS = (
    "entities",
    "facilities",
    "covered_metrics",
    "excluded_entities",
    "excluded_facilities",
    "excluded_metrics",
    "excluded_periods",
    "explicit_exclusions",
)
LEVELS = {
    "limited": "limited",
    "reasonable": "reasonable",
    "none": "none",
    "제한적 보증": "limited",
    "합리적 보증": "reasonable",
}
MATCH_RULES = {
    "version": "task-007-v1",
    "scope": "exact-nfc-subset",
    "period": "exact-year-literal-yyyy-yyyy년-yyyy년도",
    "unknown": "undetermined",
    "exclusion": "explicit-dimension-first-exact-year-or-unresolved",
    "multiple_scope_axes": "undetermined-without-scoped-extraction",
    "levels": LEVELS,
}


def _text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


@dataclass(frozen=True, slots=True)
class ClaimContext:
    tenant_id: str
    document_version_id: str
    claim_id: str
    metric: str | None
    reporting_period: str | None
    entities: tuple[str, ...]
    facilities: tuple[str, ...]

    def __post_init__(self):
        for name in ("tenant_id", "document_version_id", "claim_id"):
            _require_uuid(name, getattr(self, name))
        for name in ("metric", "reporting_period"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not _text(value)):
                raise DomainValidationError("claim dimensions must be nonempty strings or null")
        for name in ("entities", "facilities"):
            values = getattr(self, name)
            if not isinstance(values, tuple | list) or any(
                not isinstance(v, str) or not _text(v) for v in values
            ):
                raise DomainValidationError("claim boundary must contain nonempty strings")
            object.__setattr__(self, name, tuple(values))


@dataclass(frozen=True, slots=True)
class AssuranceStatement:
    """Immutable extraction revision; retain this envelope beside the v1 projection."""

    statement_id: str
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    graph_sha256: str
    binding: ModelBinding
    model_sha256: str
    prompt_sha256: str
    replicate_id: int
    synthetic: bool
    provider: str | None
    standard_raw: str | None
    level: str | None
    reporting_period: str | None
    entities: tuple[str, ...]
    facilities: tuple[str, ...]
    covered_metrics: tuple[str, ...]
    excluded_entities: tuple[str, ...]
    excluded_facilities: tuple[str, ...]
    excluded_metrics: tuple[str, ...]
    excluded_periods: tuple[str, ...]
    explicit_exclusions: tuple[tuple[str, str], ...]
    source_refs: tuple[SourceRef, ...]
    tagged_fields: tuple[tuple[str, tuple[SourceRef, ...]], ...]
    unresolved_fields: tuple[str, ...]

    def __post_init__(self):
        refs = tuple(self.source_refs)
        if not refs or any(not isinstance(ref, SourceRef) for ref in refs):
            raise DomainValidationError("assurance requires original source_refs")
        if any(
            (ref.document_version_id, ref.parse_manifest_id)
            != (self.document_version_id, self.parse_manifest_id)
            for ref in refs
        ):
            raise DomainValidationError("statement source identity mismatch")
        object.__setattr__(self, "source_refs", refs)
        for name in LISTS:
            if name != "explicit_exclusions":
                object.__setattr__(self, name, tuple(getattr(self, name)))
        object.__setattr__(
            self, "tagged_fields", tuple((name, tuple(items)) for name, items in self.tagged_fields)
        )
        object.__setattr__(
            self,
            "explicit_exclusions",
            tuple((name, value) for name, value in self.explicit_exclusions),
        )
        object.__setattr__(self, "unresolved_fields", tuple(self.unresolved_fields))

    @property
    def semantic_hash(self) -> str:
        return canonical_hash(asdict(self))


def extract_assurance(
    graph: CanonicalDocumentGraph,
    source_refs: Sequence[SourceRef],
    binding: ModelBinding,
    *,
    tagged_fields: Mapping[str, Sequence[SourceRef]],
    tenant_id: str,
    statement_id: str,
    model_sha256: str,
    prompt_sha256: str,
    replicate_id: int,
) -> AssuranceStatement:
    """Extract literal field spans and validate them against a bounded statement.

    Each named field contains only SourceRefs: values are read from the original
    quotes, never accepted as uncited model strings. `source_refs` is the selected
    opinion's span boundary, preventing tags from borrowing another opinion's
    period/metric/provider. Tag-role interpretation still requires approved model
    evaluation or human tagging; substring presence is not a semantic guarantee.
    """
    _require_uuid("tenant_id", tenant_id)
    _require_uuid("statement_id", statement_id)
    for name, value in (("model_sha256", model_sha256), ("prompt_sha256", prompt_sha256)):
        _require_sha256(name, value)
    if type(replicate_id) is not int or replicate_id not in (1, 2, 3):
        raise DomainValidationError("invalid replicate_id")
    if not isinstance(binding, ModelBinding) or not binding.binding_id or not binding.role:
        raise DomainValidationError("model binding required")
    if type(binding.synthetic) is not bool:
        raise DomainValidationError("binding synthetic marker must be boolean")
    if graph.tenant_id != tenant_id:
        raise DomainValidationError("tenant mismatch")
    if not isinstance(tagged_fields, Mapping) or set(tagged_fields) - set(SCALARS + LISTS):
        raise DomainValidationError("unknown assurance fields; model grades/status forbidden")
    selected = tuple(source_refs)
    if not selected:
        raise DomainValidationError("assurance requires original source_refs")
    blocks = {b.source_id: b for b in graph.blocks}
    if len(blocks) != len(graph.blocks):
        raise DomainValidationError("duplicate canonical source_id")

    def verified(ref: SourceRef) -> bool:
        if not isinstance(ref, SourceRef):
            raise DomainValidationError("SourceRef required")
        if (ref.document_version_id, ref.parse_manifest_id) != (
            graph.document_version_id,
            graph.parse_manifest_id,
        ) or ref.source_id not in blocks:
            raise DomainValidationError("source identity mismatch")
        block = blocks[ref.source_id]
        # Even unresolved text must reference an actual preserved parser candidate.
        candidates = [
            c
            for c in block.candidates
            if (
                c.source.document_version_id == ref.document_version_id
                and c.source.parse_manifest_id == ref.parse_manifest_id
                and c.source.physical_page == ref.page_num
                and c.source.printed_page_label == ref.printed_page_label
                and c.bbox == ref.bbox
                and sha256(c.source.raw_text.encode()).hexdigest() == ref.raw_text_sha256
                and 0 <= ref.char_start < ref.char_end <= len(c.source.raw_text)
                and c.source.raw_text[ref.char_start : ref.char_end] == ref.quote
            )
        ]
        if not candidates:
            raise DomainValidationError("citation differs from original source")
        return (
            block.quality == "verified"
            and block.winner is not None
            and block.candidates[block.winner] in candidates
            and ref.location_quality == "located"
            and ref.bbox is not None
            and ref.verification_state != "rejected"
        )

    selected_quality = [verified(ref) for ref in selected]
    fields = []
    values: dict[str, tuple[str, ...]] = {}
    unresolved = set()
    for name in SCALARS + LISTS:
        refs = tuple(tagged_fields.get(name, ()))
        ok = True
        for ref in refs:
            valid = verified(ref)
            if not any(
                ref.source_id == span.source_id
                and ref.raw_text_sha256 == span.raw_text_sha256
                and span.char_start <= ref.char_start < ref.char_end <= span.char_end
                for span in selected
            ):
                raise DomainValidationError("field is outside selected assurance statement")
            ok = ok and valid
        fields.append((name, refs))
        texts = tuple(dict.fromkeys(_text(ref.quote) for ref in refs))
        if not ok or (name in SCALARS and len(texts) > 1):
            unresolved.add(name)
            texts = ()
        values[name] = texts
    if not all(selected_quality):
        # An unresolved part of the opinion might contain a qualification/exclusion.
        unresolved.add("statement_source")
    if sum(len(values[name]) > 1 for name in ("covered_metrics", "entities", "facilities")) > 1:
        # Independent lists do not prove their Cartesian product is assured.
        unresolved.add("scope_group")
    scalar = {name: values[name][0] if values[name] else None for name in SCALARS}
    if scalar["level"] is not None:
        scalar["level"] = LEVELS.get(scalar["level"])
        if scalar["level"] is None:
            unresolved.add("level")
    exclusions = tuple(
        (name, ref.quote)
        for name, refs in fields
        if name.startswith("excluded_") or name == "explicit_exclusions"
        for ref in refs
    )
    return AssuranceStatement(
        statement_id=statement_id,
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        binding=binding,
        model_sha256=model_sha256,
        prompt_sha256=prompt_sha256,
        replicate_id=replicate_id,
        synthetic=binding.synthetic or any(batch.synthetic for batch in graph.candidates),
        provider=scalar["provider"],
        standard_raw=scalar["standard_raw"],
        level=scalar["level"],
        reporting_period=scalar["reporting_period"],
        entities=values["entities"],
        facilities=values["facilities"],
        covered_metrics=values["covered_metrics"],
        excluded_entities=values["excluded_entities"],
        excluded_facilities=values["excluded_facilities"],
        excluded_metrics=values["excluded_metrics"],
        excluded_periods=values["excluded_periods"],
        explicit_exclusions=exclusions,
        source_refs=selected,
        tagged_fields=tuple(fields),
        unresolved_fields=tuple(sorted(unresolved)),
    )


@dataclass(frozen=True, slots=True)
class AssuranceMatch:
    status: str
    level: str | None
    provider: str | None
    statement_id: str | None
    metric_match: str
    period_match: str
    boundary_match: str
    evidence_refs: tuple[SourceRef, ...]
    claim_id: str
    statement_sha256: str | None
    rule_sha256: str
    synthetic: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Explicit fixed-v1 projection; hashes/quality stay in the internal envelope."""
        data = {
            name: getattr(self, name)
            for name in (
                "status",
                "level",
                "provider",
                "statement_id",
                "metric_match",
                "period_match",
                "boundary_match",
            )
        }
        data["evidence_refs"] = [
            asdict(ref) | {"bbox": list(ref.bbox) if ref.bbox else None}
            for ref in self.evidence_refs
        ]
        return data


def _subset(claim_values: Sequence[str], covered: Sequence[str]) -> str:
    if not claim_values or not covered:
        return "unknown"
    return "yes" if {_text(v) for v in claim_values} <= {_text(v) for v in covered} else "no"


def _normalize_year(value: str | None) -> str | None:
    """Match-time exact single-year literal: YYYY, YYYY년, or YYYY 년도/년도.

    Returns the four-digit year for exactly those three literal shapes and
    None otherwise (ranges, extra prose, fiscal labels stay unknown). Stored
    statement/claim fields are never rewritten; this applies only to the
    comparison comparands.
    """
    if not isinstance(value, str):
        return None
    text = _text(value)
    if re.fullmatch(r"[0-9]{4}", text):
        return text
    match = re.fullmatch(r"([0-9]{4})년", text)
    if match:
        return match.group(1)
    match = re.fullmatch(r"([0-9]{4})\s*년도", text)
    if match:
        return match.group(1)
    return None


def _validated_dimension_text(ref, *, claim, original, tenant_id: str) -> str | None:
    """Return the literal dimension quote when it is source-bound to this claim.

    Accepts only a SourceRef that (a) carries this claim's document/manifest
    identity, (b) sits inside one of the claim's own atomic source spans
    (same source_id and char containment), and (c) re-verifies against the
    trusted original graph (page/bbox/quote/hash/offsets). Anything else --
    foreign source_id, out-of-claim span, unverified/unlocated citation --
    returns None so the matcher stays unknown instead of guessing.
    """
    try:
        if ref is None:
            return None
        if not isinstance(ref, SourceRef):
            return None
        if (ref.document_version_id, ref.parse_manifest_id) != (
            claim.document_version_id,
            claim.parse_manifest_id,
        ):
            return None
        text = _text(ref.quote) if isinstance(ref.quote, str) else ""
        if not text:
            return None
        contained = False
        for source in claim.source_refs:
            if (
                source.source_id == ref.source_id
                and source.document_version_id == ref.document_version_id
                and source.parse_manifest_id == ref.parse_manifest_id
                and source.char_start <= ref.char_start < ref.char_end <= source.char_end
            ):
                contained = True
                break
        if not contained:
            return None
        from proofops.application.evidence.span_citations import verify_source_ref

        checked = verify_source_ref(ref, original, tenant_id=tenant_id)
        if checked.verification_state != "verified" or checked.quote != ref.quote:
            return None
        return text
    except Exception:
        return None


def _normalize_reporting_period(text: str | None) -> str | None:
    """Retain the literal period; map only an exact explicit single-year token.

    Exact `YYYY`, `YYYY년`, or `YYYY 년도`/`YYYY년도` maps to `YYYY` at the
    claim-context build step (the stored SourceRef keeps the original quote).
    Ranges, multi-year phrases, or inferred years return literally so
    `match_assurance` keeps them `unknown`.
    """
    if text is None:
        return None
    year = _normalize_year(text)
    return year if year is not None else text


def claim_context_from_review_inputs(review_inputs, *, tenant_id, document_version_id, claim_id):
    """Build an assurance ClaimContext from validated preliminary dimensions.

    Reads only `review_inputs.context.dimensions` (entity/metric/
    reporting_period/facility) already loaded via LocalTagStore.load_inputs;
    never regex-guesses entity/metric names or run-global dates from text.
    Each ref must re-verify against `review_inputs.original` and sit inside
    the claim's own atomic span; invalid/missing refs degrade to None/() so
    matching stays undetermined. An empty facility tuple is preserved as-is
    (never universal coverage). On any absent or mismatched inputs the empty
    context is returned.
    """
    try:
        empty = ClaimContext(tenant_id, document_version_id, claim_id, None, None, (), ())
    except Exception:
        return None
    if review_inputs is None:
        return empty
    try:
        context = review_inputs.context
        claim = context.claim
        dimensions = context.dimensions
        original = review_inputs.original
        if claim.claim_id != claim_id:
            return empty
        if (claim.tenant_id, claim.document_version_id) != (tenant_id, document_version_id):
            return empty
        if not isinstance(dimensions, Mapping):
            return empty
        tid = claim.tenant_id
        metric = _validated_dimension_text(
            dimensions.get("metric"), claim=claim, original=original, tenant_id=tid
        )
        period_raw = _validated_dimension_text(
            dimensions.get("reporting_period"), claim=claim, original=original, tenant_id=tid
        )
        entity = _validated_dimension_text(
            dimensions.get("entity"), claim=claim, original=original, tenant_id=tid
        )
        facility = _validated_dimension_text(
            dimensions.get("facility"), claim=claim, original=original, tenant_id=tid
        )
        period = _normalize_reporting_period(period_raw)
        entities = (entity,) if entity else ()
        facilities = (facility,) if facility else ()
        return ClaimContext(
            tenant_id, document_version_id, claim_id, metric, period, entities, facilities
        )
    except Exception:
        return empty


def match_assurance(statement: AssuranceStatement | None, claim: ClaimContext) -> AssuranceMatch:
    """Match one opinion at a time; never merge providers or promote assurance level.

    Period compares exact single-year literals only (YYYY, YYYY년, YYYY 년도),
    normalized at match time on both comparands without rewriting stored
    fields; ranges and extra prose stay unknown. ponytail: exact names only;
    approved alias normalization can extend this when supplied, without fuzzy
    scope expansion.
    """
    metric = period = boundary = "unknown"
    status = "undetermined"
    reasons = []
    if statement is not None:
        if (statement.tenant_id, statement.document_version_id) != (
            claim.tenant_id,
            claim.document_version_id,
        ):
            raise DomainValidationError("assurance/claim tenant or document version mismatch")
        metric = _subset((claim.metric,) if claim.metric else (), statement.covered_metrics)
        claim_year = _normalize_year(claim.reporting_period) if claim.reporting_period else None
        statement_year = (
            _normalize_year(statement.reporting_period) if statement.reporting_period else None
        )
        if claim_year is not None and statement_year is not None:
            period = "yes" if claim_year == statement_year else "no"
        entity = _subset(claim.entities, statement.entities)
        facility = _subset(claim.facilities, statement.facilities)
        boundary = (
            "no"
            if "no" in (entity, facility)
            else ("yes" if entity == facility == "yes" else "unknown")
        )
        excluded = any(
            set(map(_text, claimed)) & set(covered)
            for claimed, covered in (
                ((claim.metric,) if claim.metric else (), statement.excluded_metrics),
                (
                    (claim.reporting_period,) if claim.reporting_period else (),
                    statement.excluded_periods,
                ),
                (claim.entities, statement.excluded_entities),
                (claim.facilities, statement.excluded_facilities),
            )
        )
        excluded_years = tuple(_normalize_year(value) for value in statement.excluded_periods)
        excluded = excluded or (claim_year is not None and claim_year in excluded_years)
        uncertain = (
            statement.unresolved_fields
            or any(year is None for year in excluded_years)
            or any(name == "explicit_exclusions" for name, _ in statement.explicit_exclusions)
        )
        if "scope_group" in statement.unresolved_fields:
            reasons.append("ambiguous_scope_group")
        elif excluded:
            status = "not_covered"
            reasons.append("explicit_exclusion")
        elif uncertain:
            reasons.append("unresolved_statement")
        elif statement.level == "none" or "no" in (metric, period, boundary):
            status = "not_covered"
            reasons.append("scope_mismatch_or_no_assurance")
        elif (
            metric == period == boundary == "yes"
            and statement.provider
            and statement.standard_raw
            and statement.level in ("limited", "reasonable")
        ):
            status = "covered"
        else:
            reasons.append("insufficient_scope_information")
    else:
        reasons.append("statement_unavailable")
    return AssuranceMatch(
        status,
        statement.level if statement else None,
        statement.provider if statement else None,
        statement.statement_id if statement else None,
        metric,
        period,
        boundary,
        statement.source_refs if statement else (),
        claim.claim_id,
        statement.semantic_hash if statement else None,
        canonical_hash(MATCH_RULES),
        statement.synthetic if statement else False,
        tuple(reasons),
    )
