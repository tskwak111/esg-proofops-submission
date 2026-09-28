"""Pure numeric checks over a trusted claim registry and pinned original snapshot.

Structural protocols accept internal application records without importing them.
The caller loads those records in an authorized run; API projections and model
assertions are not trusted snapshots or accepted semantic bindings.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Context, Decimal, InvalidOperation, localcontext
from fractions import Fraction
from hashlib import sha256
from typing import Literal, Protocol

from proofops.domain.documents import NativeSource
from proofops.domain.periods import is_supported_period
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef

CheckKind = Literal["comparison", "sum", "reduction", "growth", "product_reduction"]
CheckStatus = Literal["consistent", "inconsistent", "not_comparable", "not_computable"]
Interval = tuple[Fraction, Fraction]
_NUMBER = re.compile(r"([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?:\s+(.+))?")


def unit_note_literal(text: str) -> str | None:
    """Bounded unit-note syntax; prose is not an accepted scope interpretation."""
    match = re.fullmatch(
        r"(?i:단위|unit)\s*:\s*((?:천 )?(?:tCO2e|tCO₂e|kgCO2e|t|kg|g|GJ|TJ|MJ|"
        r"kWh|MWh|GWh|m3|m³|㎥|L|%p?|톤|원|백만원|억원|개|명|건|대))",
        text.strip(),
    )
    return match[1] if match else None


def scope_note_literal(text: str) -> str | None:
    """Only a complete explicit GHG scope literal, never prose or geography."""
    match = re.fullmatch(r"(?:Scope:\s*)?(Scope [123])", text.strip())
    return match[1] if match else None


class NumericEdge(Protocol):
    source_id: str
    target_id: str
    relation: str


class NumericCandidate(Protocol):
    source: NativeSource
    row_number: int | None
    column_number: int | None
    column_span: int | None

    @property
    def bbox(self) -> tuple[float, float, float, float] | None: ...


class NumericBlock(Protocol):
    kind: str
    source_id: str
    quality: str
    winner: int | None
    candidates: tuple[NumericCandidate, ...]


class NumericBatch(Protocol):
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    parser_run_id: str
    blocks: tuple[NumericCandidate, ...]


class NumericIssue(Protocol):
    issue_id: str
    source_ids: tuple[str, ...]
    state: str


class NumericSnapshot(Protocol):
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    blocks: tuple[NumericBlock, ...]
    candidates: tuple[NumericBatch, ...]
    edges: tuple[NumericEdge, ...]
    issues: tuple[NumericIssue, ...]


class NumericClaim(Protocol):
    claim_id: str
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    quote: str
    source_quality: str
    source_refs: tuple[SourceRef, ...]


class NumericObservation(Protocol):
    observation_id: str
    table_id: str
    row: int
    column: int
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    metric_raw: str
    scope: str | None
    subject: str | None
    reporting_period: str
    scope2_basis: str | None
    organizational_boundary: str | None
    unit_raw: str | None
    unit_canonical: str | None
    scale_multiplier: str
    denominator: str | None
    value_raw: str
    value_decimal: str | None
    value_state: str
    quality: str
    source_refs: tuple[SourceRef, ...]
    source_blocks: tuple[NumericBlock, ...]
    parent_relations: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True, slots=True)
class AggregationRelation:
    """Explicit upstream semantic acceptance, never inferred from matching rows.

    disjoint_complete_components asserts every pinned member is a distinct leaf
    component of this target, with no overlapping subtotal/total also included.
    binding_sha256 pins the whole binding with aggregation=None, including its
    period, dimensions, ordered members, claim and reported-value source span.
    """

    target_claim_id: str
    observation_ids: tuple[str, ...]
    binding_sha256: str
    source_refs: tuple[SourceRef, ...]
    relation: str
    acceptance_state: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_ids", tuple(self.observation_ids))
        object.__setattr__(self, "source_refs", tuple(self.source_refs))


@dataclass(frozen=True, slots=True)
class NumericCondition:
    """Explicit source-backed review that one note applies equally to both operands."""

    source_ref: SourceRef
    observation_ids: tuple[str, ...]
    binding_sha256: str
    relation: str
    acceptance_state: str
    reviewed_by: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_ids", tuple(self.observation_ids))
        if not isinstance(self.source_ref, SourceRef) or not isinstance(self.reviewed_by, str):
            raise ValueError("invalid numeric condition")


@dataclass(frozen=True, slots=True)
class ClaimBinding:
    claim_id: str
    tenant_id: str
    document_version_id: str
    kind: CheckKind
    observation_ids: tuple[str, ...]
    reported_value: str | None
    metric_raw: str
    scope: str | None
    subject: str | None
    scope2_basis: str | None
    organizational_boundary: str | None
    unit: str | None
    denominator: str | None
    source_refs: tuple[SourceRef, ...]
    # Legacy callers remain callable, but cannot compute without explicit scope
    # and upstream accepted binding plus original-source/claim validation.
    parse_manifest_id: str | None = None
    reporting_period: str | None = None
    quantity_kind: Literal["absolute", "intensity"] | None = None
    baseline_period: str | None = None
    reported_value_ref: SourceRef | None = None
    binding_accepted: bool = False
    aggregation: AggregationRelation | None = None
    # same-period different-product relative reduction needs an explicitly
    # accepted baseline product dimension plus verified source spans for both the
    # current and baseline product literals. These are never inferred from numbers.
    baseline_subject: str | None = None
    subject_ref: SourceRef | None = None
    baseline_subject_ref: SourceRef | None = None
    product_comparison_accepted: bool = False
    conditions: tuple[NumericCondition, ...] = ()

    def __post_init__(self) -> None:
        if (
            not self.claim_id
            or not self.tenant_id
            or not self.document_version_id
            or self.kind not in ("comparison", "sum", "reduction", "growth", "product_reduction")
            or not self.observation_ids
            or len(set(self.observation_ids)) != len(self.observation_ids)
            or type(self.binding_accepted) is not bool
            or type(self.product_comparison_accepted) is not bool
        ):
            raise ValueError("invalid numeric claim binding")
        if self.kind == "comparison" and len(self.observation_ids) != 1:
            raise ValueError("comparison requires exactly one observation")
        if self.kind in ("reduction", "growth") and len(self.observation_ids) != 2:
            raise ValueError(f"{self.kind} requires baseline and current observations")
        if self.kind == "product_reduction" and len(self.observation_ids) != 2:
            raise ValueError("product_reduction requires baseline and current product observations")
        refs = tuple(self.source_refs)
        if not refs or any(not isinstance(ref, SourceRef) for ref in refs):
            raise ValueError("numeric binding requires source_refs")
        if any(ref.document_version_id != self.document_version_id for ref in refs):
            raise ValueError("numeric binding source version mismatch")
        object.__setattr__(self, "source_refs", refs)
        object.__setattr__(self, "observation_ids", tuple(self.observation_ids))
        object.__setattr__(self, "conditions", tuple(self.conditions))
        if any(not isinstance(c, NumericCondition) for c in self.conditions) or (
            self.conditions and self.kind not in ("growth", "product_reduction")
        ):
            raise ValueError("numeric conditions require a supported rate binding")


@dataclass(frozen=True, slots=True)
class CheckResult:
    claim_id: str
    kind: CheckKind
    status: CheckStatus
    reported_value: str | None
    computed_value: str | None
    observation_ids: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    reason: str | None = None
    exact_ratio: tuple[str, str] | None = None


_DIMENSIONS = (
    "metric_raw",
    "scope",
    "subject",
    "scope2_basis",
    "organizational_boundary",
    "unit_canonical",
    "denominator",
)


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _display(raw: str, scale: Fraction = Fraction(1)) -> tuple[Fraction, Interval] | None:
    match = _NUMBER.fullmatch(raw.strip())
    if not match:
        return None
    text = match[1].replace(",", "")
    value = Fraction(Decimal(text)) * scale
    half_step = Fraction(1, 2 * 10 ** len(text.partition(".")[2])) * scale
    return value, (value - half_step, value + half_step)


def _exact_decimal(value: Fraction) -> str | None:
    denominator = value.denominator
    for factor in (2, 5):
        while denominator % factor == 0:
            denominator //= factor
    if denominator != 1:
        return None  # Never invent display precision for a recurring quotient.
    # A fresh context isolates precision, rounding and traps from the caller.
    precision = len(str(abs(value.numerator))) + len(str(value.denominator)) * 4 + 1
    with localcontext(Context(prec=precision)):
        return format(Decimal(value.numerator) / Decimal(value.denominator), "f")


def _identity(
    value: NumericSnapshot | NumericClaim | NumericObservation | NumericBatch,
) -> tuple[str, ...]:
    return (
        value.tenant_id,
        value.document_version_id,
        value.parse_manifest_id,
        value.source_sha256,
    )


def _verified(ref: SourceRef | None, original: NumericSnapshot) -> bool:
    """Strict raw-span subset of AT-012; no fuzzy search or offset repair."""
    if (
        not isinstance(ref, SourceRef)
        or ref.verification_state != "verified"
        or ref.location_quality != "located"
        or ref.bbox is None
        or (ref.document_version_id, ref.parse_manifest_id)
        != (original.document_version_id, original.parse_manifest_id)
    ):
        return False
    blocks = {b.source_id: b for b in original.blocks}
    block = blocks.get(ref.source_id)
    if (
        len(blocks) != len(original.blocks)
        or block is None
        or block.quality != "verified"
        or type(block.winner) is not int
        or not 0 <= block.winner < len(block.candidates)
    ):
        return False
    candidate = block.candidates[block.winner]
    source = candidate.source
    return (
        any(
            _identity(batch) == _identity(original)
            and batch.parser_run_id == source.parser_run_id
            and candidate in batch.blocks
            for batch in original.candidates
        )
        and (source.document_version_id, source.parse_manifest_id)
        == (original.document_version_id, original.parse_manifest_id)
        and source.physical_page == ref.page_num
        and source.printed_page_label == ref.printed_page_label
        and candidate.bbox == ref.bbox
        and sha256(source.raw_text.encode("utf-8")).hexdigest() == ref.raw_text_sha256
        and 0 <= ref.char_start < ref.char_end <= len(source.raw_text)
        and source.raw_text[ref.char_start : ref.char_end] == ref.quote
        and bool(ref.quote.strip())
    )


def _claim_verified(
    binding: ClaimBinding, claim: NumericClaim | None, original: NumericSnapshot
) -> bool:
    number = binding.reported_value_ref
    return (
        binding.binding_accepted
        and claim is not None
        and _identity(claim) == _identity(original)
        and binding.parse_manifest_id == original.parse_manifest_id
        and claim.source_quality == "verified"
        and binding.source_refs == claim.source_refs
        and bool(claim.source_refs)
        and all(_verified(ref, original) for ref in claim.source_refs)
        and claim.quote == " ".join(ref.quote for ref in claim.source_refs)
        and _verified(number, original)
        and number is not None
        and number.quote == binding.reported_value
        and any(
            number.source_id == ref.source_id
            and ref.char_start <= number.char_start < number.char_end <= ref.char_end
            and _whole_number(number, original)
            for ref in claim.source_refs
        )
    )


def _whole_number(number: SourceRef, original: NumericSnapshot) -> bool:
    block = next(b for b in original.blocks if b.source_id == number.source_id)
    assert block.winner is not None  # _verified checked the selected candidate.
    raw = block.candidates[block.winner].source.raw_text
    # Match the entire original token, even when a claim ref itself ends inside
    # a larger number. Sentence punctuation is not fractional display precision.
    tokens = re.finditer(
        r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?",
        raw,
    )
    return any(token.span() == (number.char_start, number.char_end) for token in tokens)


def _observation_display(
    item: NumericObservation, original: NumericSnapshot
) -> tuple[Fraction, Interval] | None:
    if (
        _identity(item) != _identity(original)
        or item.quality != "verified"
        or item.value_state != "value"
        or not item.source_refs
        or not all(_verified(ref, original) for ref in item.source_refs)
        or not item.source_blocks
        or any(b not in original.blocks for b in item.source_blocks)
        or not any(ref.quote == item.value_raw for ref in item.source_refs)
    ):
        return None
    # Only the accepted normalizer's explicit scale conversion is supported.
    unit = item.unit_raw
    scale = "1000" if unit and unit.startswith("천 ") else "1"
    canonical = unit[2:].strip() if scale == "1000" and unit else unit
    if item.scale_multiplier != scale or item.unit_canonical != canonical:
        return None
    refs: dict[str, list[SourceRef]] = {}
    for ref in item.source_refs:
        refs.setdefault(ref.source_id, []).append(ref)
    raw_match = _NUMBER.fullmatch(item.value_raw.strip())
    unit_bound = any(
        name == "unit_raw" and any(ref.quote.strip() == unit for ref in refs.get(target, ()))
        for _, target, name in item.parent_relations
    ) or any(unit is not None and unit_note_literal(ref.quote) == unit for ref in item.source_refs)
    if not unit_bound and not (raw_match and raw_match[2] == unit):
        return None
    display = _display(item.value_raw, Fraction(scale))
    parsed = _decimal(item.value_decimal)
    if display is None or parsed is None or display[0] != Fraction(parsed):
        return None
    # Preserve explicit field-to-source associations, not merely bag-of-quotes.
    targets = _note_targets(item, original)
    note_ids = {block.source_id for block in original.blocks if block.kind == "footnote"}
    scope_notes = {
        edge.source_id
        for edge in original.edges
        if edge.relation == "footnote_of" and edge.target_id in targets
    } & note_ids
    for field in (*_DIMENSIONS[:-2], "reporting_period", "denominator"):
        expected = getattr(item, field)
        if expected is not None and not any(
            name == field
            and target in refs
            and (
                target in scope_notes
                and any(scope_note_literal(ref.quote) == expected for ref in refs[target])
                if field == "scope" and target in note_ids
                else any(ref.quote.strip() == expected for ref in refs[target])
            )
            for _, target, name in item.parent_relations
        ):
            return None
    return display


def column_note_targets(original: NumericSnapshot, table_id: str, targets: set[str]) -> set[str]:
    """First-row column context only; never pull conditions from adjacent year columns."""
    children = {
        e.source_id
        for e in original.edges
        if e.relation == "table_parent" and e.target_id == table_id
    }
    cells = [b for b in original.blocks if b.source_id in children]
    positioned = [
        (b.source_id, c.row_number, c.column_number, c.column_number + (c.column_span or 1))
        for b in cells
        for c in b.candidates
        if c.row_number is not None and c.column_number is not None
    ]
    if not positioned:
        return set()
    first_row = min(row for _, row, _, _ in positioned)
    columns = [(start, end) for source, _, start, end in positioned if source in targets]
    # ponytail: first row is conservative context, not semantic header approval;
    # multi-tier arbitrary layouts need explicit source-bound header associations.
    return {
        source
        for source, row, start, end in positioned
        if row == first_row and any(start < right and left < end for left, right in columns)
    }


def unresolved_source_issue_ids(original: NumericSnapshot, source_ids: set[str]) -> frozenset[str]:
    """Follow explicit table ancestors so omitted parent refs cannot hide an issue."""
    related, pending = set(source_ids), list(source_ids)
    parents: dict[str, set[str]] = {}
    for edge in original.edges:
        if edge.relation == "table_parent":
            parents.setdefault(edge.source_id, set()).add(edge.target_id)
    while pending:
        child = pending.pop()
        for parent in parents.get(child, set()) - related:
            related.add(parent)
            pending.append(parent)
    return frozenset(
        issue.issue_id
        for issue in original.issues
        if issue.state in ("open", "unreadable") and related.intersection(issue.source_ids)
    )


def unassigned_note_ids(original: NumericSnapshot, source_id: str) -> frozenset[str]:
    """Unresolved notes around an explicit table lineage; never inferred ownership."""
    blocks = {b.source_id: b for b in original.blocks}
    block = blocks.get(source_id)
    if block is None or block.kind not in ("table", "table_row", "table_cell", "footnote"):
        return frozenset()
    # A detected note without a resolvable table owner is uncertainty, not absence.
    owned = {b.source_id for b in original.blocks if b.kind == "table"}
    known = {b.source_id for b in original.blocks if b.kind in ("table_cell", "table_row")}
    parents: dict[str, set[str]] = {}
    children: dict[str, set[str]] = {}
    for edge in original.edges:
        if edge.relation == "table_parent":
            parents.setdefault(edge.source_id, set()).add(edge.target_id)
            if edge.source_id in known:
                children.setdefault(edge.target_id, set()).add(edge.source_id)
    pending_owners = list(owned)
    while pending_owners:
        owner = pending_owners.pop()
        for child in children.get(owner, set()) - owned:
            owned.add(child)
            pending_owners.append(child)
    assigned = {
        e.source_id for e in original.edges if e.relation == "footnote_of" and e.target_id in owned
    }
    if block.kind == "footnote":
        return frozenset() if source_id in assigned else frozenset({source_id})
    related, pending = {source_id}, [source_id]
    while pending:
        child = pending.pop()
        for parent in parents.get(child, set()) & owned - related:
            related.add(parent)
            pending.append(parent)
    pages = {c.source.physical_page for sid in related for c in blocks[sid].candidates}
    return frozenset(
        b.source_id
        for b in original.blocks
        if b.kind == "footnote"
        and b.source_id not in assigned
        and any(c.source.physical_page in pages for c in b.candidates)
    )


def _note_targets(item: NumericObservation, original: NumericSnapshot) -> set[str]:
    targets = {source for source, _, _ in item.parent_relations}
    pending = list(targets)
    while pending:
        source_id = pending.pop()
        for edge in original.edges:
            if (
                edge.relation == "table_parent"
                and edge.source_id == source_id
                and edge.target_id not in targets
            ):
                targets.add(edge.target_id)
                pending.append(edge.target_id)
    if item.table_id not in targets:
        return set()
    note_ids = {block.source_id for block in original.blocks if block.kind == "footnote"}
    targets.update(target for _, target, _ in item.parent_relations if target not in note_ids)
    targets.update(column_note_targets(original, item.table_id, targets))
    return targets


def observation_source_holds(
    item: NumericObservation,
    original: NumericSnapshot,
    *,
    accepted_note_ids: frozenset[str] = frozenset(),
) -> dict:
    """Original-lineage diagnostics, not coverage approval or numeric permission."""
    if item.tenant_id != original.tenant_id or (
        item.document_version_id,
        item.parse_manifest_id,
        item.source_sha256,
    ) != (original.document_version_id, original.parse_manifest_id, original.source_sha256):
        raise ValueError("observation snapshot mismatch")
    source_ids = {ref.source_id for ref in item.source_refs} | {item.table_id}
    source_ids.update(source for source, _, _ in item.parent_relations)
    issues = unresolved_source_issue_ids(original, source_ids)
    notes, reasons = _footnote_holds(item, original, accepted_note_ids=accepted_note_ids)
    if issues:
        reasons.add("source_issue_unresolved")
    return dict(
        observation_id=item.observation_id,
        issue_ids=sorted(issues),
        note_source_ids=sorted(notes),
        reasons=sorted(reasons),
    )


def _unresolved_footnotes(
    item: NumericObservation, original: NumericSnapshot, accepted_note_ids: frozenset[str]
) -> bool:
    return bool(_footnote_holds(item, original, accepted_note_ids=accepted_note_ids)[1])


def _footnote_holds(
    item: NumericObservation,
    original: NumericSnapshot,
    *,
    accepted_note_ids: frozenset[str] = frozenset(),
) -> tuple[set[str], set[str]]:
    # Accumulate all holds; an unverified value must not hide its sibling note.
    held: set[str] = set()
    reasons: set[str] = set()
    roots_ids = {source for source, _, _ in item.parent_relations}
    roots = [b for b in item.source_blocks if b.source_id in roots_ids]
    if len(roots_ids) != 1 or len(roots) != 1:
        reasons.add("observation_root_unresolved")
    for root in roots:
        unassigned = unassigned_note_ids(original, root.source_id)
        held.update(unassigned)
        if unassigned:
            reasons.add("footnote_owner_unresolved")
        if type(root.winner) is not int or not 0 <= root.winner < len(root.candidates):
            reasons.add("observation_root_unresolved")
            continue
        candidate = root.candidates[root.winner]
        if (candidate.row_number, candidate.column_number) != (item.row, item.column) or not any(
            ref.source_id == root.source_id
            and ref.quote == item.value_raw
            and _verified(ref, original)
            for ref in item.source_refs
        ):
            reasons.add("observation_source_unverified")
    targets = _note_targets(item, original)
    if item.table_id not in targets:
        reasons.add("observation_table_lineage_unresolved")
    required = {
        e.source_id
        for e in original.edges
        if e.relation == "footnote_of" and e.target_id in targets
    }
    selected_notes = [b for b in original.blocks if b.source_id in required]
    notes = {b.source_id: b for b in selected_notes}
    if len(notes) != len(selected_notes):
        held.update(required)
        reasons.add("footnote_source_ambiguous")
    held.update(required - set(notes))
    if required - set(notes):
        reasons.add("footnote_source_missing")
    for note in notes.values():
        if type(note.winner) is not int or not 0 <= note.winner < len(note.candidates):
            held.add(note.source_id)
            reasons.add("footnote_source_unverified")
            continue
        raw = note.candidates[note.winner].source.raw_text
        unit, scope = unit_note_literal(raw), scope_note_literal(raw)
        if note.source_id not in accepted_note_ids and not (
            (unit and unit == item.unit_raw) or (scope and scope == item.scope)
        ):
            held.add(note.source_id)
            reasons.add("footnote_condition_unsupported_or_mismatched")
        if not any(
            ref.source_id == note.source_id and ref.quote == raw and _verified(ref, original)
            for ref in item.source_refs
        ):
            held.add(note.source_id)
            reasons.add("footnote_source_unverified")
    return held, reasons


def condition_binding_hash(binding: ClaimBinding) -> str:
    payload = asdict(replace(binding, conditions=()))
    payload.pop("conditions")
    return canonical_hash(payload)


def validated_condition_ids(
    binding: ClaimBinding, original: NumericSnapshot, items: Sequence[NumericObservation]
) -> frozenset[str]:
    """No automatic note interpretation: require pinned explicit review + real lineage."""
    if not binding.binding_accepted or binding.kind not in ("growth", "product_reduction"):
        return frozenset()
    if tuple(item.observation_id for item in items) != binding.observation_ids or not items:
        return frozenset()
    digest = condition_binding_hash(binding)
    blocks = {block.source_id: block for block in original.blocks}
    valid = set()
    for condition in binding.conditions:
        ref = condition.source_ref
        block = blocks.get(ref.source_id)
        if (
            condition.acceptance_state == "accepted"
            and condition.relation == "same_basis_for_selected_observations"
            and condition.reviewed_by.strip()
            and condition.observation_ids == binding.observation_ids
            and condition.binding_sha256 == digest
            and _verified(ref, original)
            and block is not None
            and block.kind == "footnote"
            and block.winner is not None
            and ref.quote == block.candidates[block.winner].source.raw_text
            and all(
                any(
                    edge.source_id == ref.source_id
                    and edge.relation == "footnote_of"
                    and edge.target_id in _note_targets(item, original)
                    for edge in original.edges
                )
                for item in items
            )
        ):
            valid.add(ref.source_id)
    return frozenset(valid)


def _matches_binding(
    item: NumericObservation,
    binding: ClaimBinding,
    period: str | None,
    *,
    subject_override: str | None = None,
) -> bool:
    # ``subject_override`` lets a product comparison require the baseline row to
    # carry the deliberately-different accepted product literal while every other
    # dimension still matches the binding exactly. It never relaxes any dimension.
    return (
        bool(period and binding.unit and binding.metric_raw)
        and (
            (binding.quantity_kind == "absolute" and binding.denominator is None)
            or (binding.quantity_kind == "intensity" and bool(binding.denominator))
        )
        and item.reporting_period == period
        and all(
            getattr(item, field)
            == (
                subject_override
                if field == "subject" and subject_override is not None
                else getattr(binding, "unit" if field == "unit_canonical" else field)
            )
            for field in _DIMENSIONS
        )
        and not (binding.unit and "/" in binding.unit and not binding.denominator)
    )


def _product_subjects_verified(
    binding: ClaimBinding, claim: NumericClaim | None, original: NumericSnapshot
) -> bool:
    """Both product literals must be verified spans inside the accepted claim quote.

    Acceptance is explicit and never inferred from the numbers. Each product name
    is a verified source span that lies inside one of the claim's own verified
    source refs, exactly like the reported-value span. The two products must be
    genuinely different subjects, so this is a real cross-product comparison.
    """
    current, baseline = binding.subject_ref, binding.baseline_subject_ref
    return (
        binding.product_comparison_accepted
        and claim is not None
        and bool(binding.subject)
        and bool(binding.baseline_subject)
        and binding.subject != binding.baseline_subject
        and current is not None
        and baseline is not None
        and current.quote == binding.subject
        and baseline.quote == binding.baseline_subject
        and _verified(current, original)
        and _verified(baseline, original)
        and all(
            any(
                span.source_id == ref.source_id
                and ref.char_start <= span.char_start < span.char_end <= ref.char_end
                for ref in claim.source_refs
            )
            for span in (current, baseline)
        )
    )


def _aggregation_verified(
    binding: ClaimBinding, original: NumericSnapshot, items: Sequence[NumericObservation]
) -> bool:
    relation = binding.aggregation
    payload = asdict(replace(binding, aggregation=None))
    binding_hashes = {canonical_hash(payload)}
    # Old immutable sum bindings predate the optional product fields. Accept
    # their original hash only when all those fields retain their old defaults.
    if (
        binding.baseline_subject is None
        and binding.subject_ref is None
        and binding.baseline_subject_ref is None
        and binding.product_comparison_accepted is False
        and not binding.conditions
    ):
        for name in (
            "baseline_subject",
            "subject_ref",
            "baseline_subject_ref",
            "product_comparison_accepted",
            "conditions",
        ):
            payload.pop(name)
        binding_hashes.add(canonical_hash(payload))
    # Renaming an observation must not count the same source cell twice.
    roots = tuple(frozenset(source for source, _, _ in item.parent_relations) for item in items)
    return (
        all(len(root) == 1 for root in roots)
        and len(set(roots)) == len(roots)
        and relation is not None
        and relation.acceptance_state == "accepted"
        and relation.relation == "disjoint_complete_components"
        and relation.target_claim_id == binding.claim_id
        and relation.observation_ids == binding.observation_ids
        and relation.binding_sha256 in binding_hashes
        and bool(relation.source_refs)
        and all(_verified(ref, original) for ref in relation.source_refs)
    )


def _result(
    binding: ClaimBinding,
    status: CheckStatus,
    computed: str | None = None,
    reason: str | None = None,
    items: Sequence[NumericObservation] = (),
    exact_ratio: tuple[str, str] | None = None,
) -> CheckResult:
    refs = (
        *binding.source_refs,
        *(ref for item in items for ref in item.source_refs),
        *(binding.aggregation.source_refs if binding.aggregation else ()),
    )
    return CheckResult(
        binding.claim_id,
        binding.kind,
        status,
        binding.reported_value,
        computed,
        binding.observation_ids,
        tuple(dict.fromkeys(refs)),
        reason,
        exact_ratio,
    )


def check_numeric_consistency(
    observations: Sequence[NumericObservation],
    bindings: Sequence[ClaimBinding],
    *,
    tenant_id: str,
    claims: Sequence[NumericClaim] = (),
    original: NumericSnapshot | None = None,
) -> tuple[CheckResult, ...]:
    """No grades/labels; uncertainty stays unresolved and never becomes absence."""
    indexed = {item.observation_id: item for item in observations}
    claim_index = {claim.claim_id: claim for claim in claims}
    if len(indexed) != len(observations) or len(claim_index) != len(claims):
        raise ValueError("duplicate observation or claim")
    if (
        any(item.tenant_id != tenant_id for item in observations)
        or any(binding.tenant_id != tenant_id for binding in bindings)
        or any(claim.tenant_id != tenant_id for claim in claims)
    ) or (original is not None and original.tenant_id != tenant_id):
        raise ValueError("numeric tenant mismatch")
    if len({b.claim_id for b in bindings}) != len(bindings):
        raise ValueError("duplicate claim binding")
    results = []
    for binding in bindings:
        selected = tuple(indexed[i] for i in binding.observation_ids if i in indexed)
        if any(item.document_version_id != binding.document_version_id for item in selected):
            raise ValueError("binding document version mismatch")
        if len(selected) != len(binding.observation_ids):
            results.append(_result(binding, "not_computable", reason="observation_missing"))
            continue
        if original is None or not _claim_verified(
            binding, claim_index.get(binding.claim_id), original
        ):
            results.append(_result(binding, "not_computable", reason="claim_source_unresolved"))
            continue
        source_ids = {ref.source_id for ref in binding.source_refs}
        source_ids.update(ref.source_id for item in selected for ref in item.source_refs)
        source_ids.update(item.table_id for item in selected)
        if unresolved_source_issue_ids(original, source_ids):
            results.append(
                _result(binding, "not_computable", reason="source_issue_unresolved", items=selected)
            )
            continue
        accepted_notes = validated_condition_ids(binding, original, selected)
        if any(_unresolved_footnotes(item, original, accepted_notes) for item in selected):
            results.append(
                _result(
                    binding,
                    "not_computable",
                    reason="footnote_conditions_unresolved",
                    items=selected,
                )
            )
            continue
        displays = tuple(_observation_display(item, original) for item in selected)
        reported = _display(binding.reported_value or "")
        if any(value is None for value in displays) or reported is None:
            results.append(_result(binding, "not_computable", reason="value_missing_or_unresolved"))
            continue
        periods = (
            (binding.baseline_period, binding.reporting_period)
            if binding.kind in ("reduction", "growth")
            else (binding.reporting_period,) * len(selected)
        )
        if not all(is_supported_period(period) for period in periods):
            results.append(_result(binding, "not_comparable", reason="reporting_period_unresolved"))
            continue
        # A temporal delta needs two distinct periods; a same-period product
        # comparison needs one shared period with a deliberately different subject.
        # comparison/sum keep their original period handling (no distinctness rule).
        if binding.kind in ("reduction", "growth"):
            period_ok = binding.baseline_period != binding.reporting_period
        elif binding.kind == "product_reduction":
            period_ok = binding.baseline_period == binding.reporting_period
        else:
            period_ok = True
        subject_overrides: tuple[str | None, ...] = (
            (binding.baseline_subject, binding.subject)
            if binding.kind == "product_reduction"
            else (None,) * len(selected)
        )
        product_ok = binding.kind != "product_reduction" or _product_subjects_verified(
            binding, claim_index.get(binding.claim_id), original
        )
        dimensions_ok = all(
            _matches_binding(item, binding, period, subject_override=override)
            for item, period, override in zip(selected, periods, subject_overrides, strict=True)
        )
        if not period_ok or not product_ok or not dimensions_ok:
            reason = (
                "product_comparison_unaccepted"
                if binding.kind == "product_reduction" and not product_ok
                else "dimension_mismatch"
            )
            results.append(_result(binding, "not_comparable", reason=reason))
            continue
        numbers = tuple(value for value in displays if value is not None)
        if binding.kind == "comparison":
            computed, interval = numbers[0]
        elif binding.kind == "sum":
            if not _aggregation_verified(binding, original, selected):
                results.append(
                    _result(binding, "not_comparable", reason="aggregation_relation_unresolved")
                )
                continue
            computed = sum((value for value, _ in numbers), Fraction())
            interval = (
                sum((bounds[0] for _, bounds in numbers), Fraction()),
                sum((bounds[1] for _, bounds in numbers), Fraction()),
            )
        else:
            (baseline, base_bounds), (current, current_bounds) = numbers
            if base_bounds[0] <= 0 <= base_bounds[1]:
                results.append(_result(binding, "not_computable", reason="zero_baseline_interval"))
                continue
            if binding.kind == "growth":
                # Temporal growth rate: (current / baseline - 1) * 100.
                computed = (current / baseline - 1) * 100
                corners = tuple((c / b - 1) * 100 for b in base_bounds for c in current_bounds)
            else:
                # reduction and product_reduction: (1 - current / baseline) * 100.
                computed = (baseline - current) / baseline * 100
                corners = tuple((1 - c / b) * 100 for b in base_bounds for c in current_bounds)
            interval = (min(corners), max(corners))
        decimal = _exact_decimal(computed)
        status: CheckStatus = (
            "consistent"
            if interval[0] <= reported[1][1] and reported[1][0] <= interval[1]
            else "inconsistent"
        )
        results.append(
            _result(
                binding,
                status,
                decimal,
                reason="non_terminating_decimal" if decimal is None else None,
                items=selected,
                exact_ratio=(str(computed.numerator), str(computed.denominator))
                if decimal is None
                else None,
            )
        )
    return tuple(results)
