"""Runtime composition seam for the pure numeric consistency check.

The domain function ``proofops.domain.numeric.check_numeric_consistency`` is pure
and has no executable caller. This layer composes already-trusted, loaded records
(a frozen snapshot, its normalized observations and discovered claims) plus the
explicitly supplied, operator-tagged ``ClaimBinding`` objects and forwards them.

The runtime never manufactures an accepted binding or upgrades uncertainty to a
grade. Optional proposals retain source-linked dimensions for explicit review.
An unbound claim is ``needs_review`` with reason
``binding_absent``, and ``unknown``/``conflict``/``unreadable``/``not_computable``/
``not_comparable`` domain outcomes are preserved verbatim.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from proofops.domain.numeric import (
    CheckResult,
    ClaimBinding,
    NumericClaim,
    NumericObservation,
    NumericSnapshot,
    check_numeric_consistency,
    observation_source_holds,
    validated_condition_ids,
)

# A claim with no accepted, source-backed binding is not a numeric finding.
BINDING_ABSENT = "binding_absent"
BINDING_NOT_ACCEPTED = "binding_not_accepted"

# Domain statuses that represent a decided numeric comparison, not uncertainty.
_DECIDED = frozenset({"consistent", "inconsistent"})


@dataclass(frozen=True, slots=True)
class NumericCheckOutcome:
    """One claim's numeric result plus its retained lineage.

    ``result`` is present only when a typed binding was supplied for the claim;
    otherwise ``status`` is ``needs_review`` and ``reason`` explains the gap.
    ``holds`` carries the per-observation source diagnostics used as input.
    """

    claim_id: str
    status: str
    reason: str | None
    result: CheckResult | None
    holds: tuple[dict, ...] = ()


@dataclass(frozen=True, slots=True)
class NumericAnalysisReport:
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    outcomes: tuple[NumericCheckOutcome, ...] = field(default_factory=tuple)

    @property
    def has_findings(self) -> bool:
        """True only when a binding produced a decided comparison.

        A ``not_computable``/``not_comparable`` result is unresolved uncertainty,
        not a finding, so it does not count here.
        """
        return any(o.result is not None and o.result.status in _DECIDED for o in self.outcomes)


def _identity(
    value: NumericSnapshot | NumericClaim | NumericObservation,
) -> tuple[str, str, str, str]:
    return (
        value.tenant_id,
        value.document_version_id,
        value.parse_manifest_id,
        value.source_sha256,
    )


def analyze_numeric_consistency(
    *,
    tenant_id: str,
    original: NumericSnapshot,
    observations: Sequence[NumericObservation],
    bindings: Sequence[ClaimBinding],
    claims: Sequence[NumericClaim] = (),
) -> NumericAnalysisReport:
    """Run the domain numeric check over a frozen run and typed bindings.

    ``original`` is the pinned snapshot, ``observations`` the normalized
    observations, ``claims`` the discovered claims, and ``bindings`` the
    explicitly supplied, operator-tagged ``ClaimBinding`` objects. Supplying a
    binding is operator tagging, not automatic approval: the pure check still
    validates the accepted binding against verified original/claim provenance.
    """
    identity = _identity(original)
    if identity[0] != tenant_id:
        raise ValueError("numeric analysis tenant mismatch")

    for item in observations:
        if _identity(item) != identity:
            raise ValueError("observation snapshot mismatch")

    # Validate the whole binding set (accepted AND rejected) before partitioning:
    # tenant/version identity and globally unique claim ids. A rejected binding is
    # surfaced, not silently dropped, so it must also be well formed and in-scope.
    if len({b.claim_id for b in bindings}) != len(bindings):
        raise ValueError("duplicate claim binding")
    for binding in bindings:
        if binding.tenant_id != tenant_id:
            raise ValueError("binding tenant mismatch")
        if binding.document_version_id != identity[1]:
            raise ValueError("binding document version mismatch")
        if binding.parse_manifest_id not in (None, identity[2]):
            raise ValueError("binding manifest mismatch")

    # Discovered claims must belong to the same frozen snapshot.
    for claim in claims:
        if _identity(claim) != identity:
            raise ValueError("claim snapshot mismatch")

    indexed = {item.observation_id: item for item in observations}

    accepted = tuple(b for b in bindings if b.binding_accepted)
    rejected = tuple(b for b in bindings if not b.binding_accepted)

    # Only accepted bindings reach the pure check; the check itself decides
    # whether the accepted semantic declaration matches verified provenance.
    results = check_numeric_consistency(
        observations,
        accepted,
        tenant_id=tenant_id,
        claims=claims,
        original=original,
    )
    result_by_claim = {result.claim_id: result for result in results}
    binding_by_claim = {b.claim_id: b for b in accepted}

    outcomes: list[NumericCheckOutcome] = []
    for result in results:
        binding = binding_by_claim[result.claim_id]
        accepted_notes = validated_condition_ids(
            binding,
            original,
            tuple(indexed[oid] for oid in binding.observation_ids if oid in indexed),
        )
        holds = tuple(
            observation_source_holds(indexed[oid], original, accepted_note_ids=accepted_notes)
            for oid in binding.observation_ids
            if oid in indexed
        )
        outcomes.append(
            NumericCheckOutcome(
                claim_id=result.claim_id,
                status=result.status,
                reason=result.reason,
                result=result,
                holds=holds,
            )
        )

    # A rejected binding is explicit operator input; surface it, never compute it.
    for binding in rejected:
        if binding.claim_id in result_by_claim:
            continue
        outcomes.append(
            NumericCheckOutcome(
                claim_id=binding.claim_id,
                status="needs_review",
                reason=BINDING_NOT_ACCEPTED,
                result=None,
            )
        )

    # Claims with no supplied binding at all: explicit needs_review, never success.
    bound_claim_ids = {b.claim_id for b in bindings}
    for claim in claims:
        if claim.claim_id in bound_claim_ids:
            continue
        outcomes.append(
            NumericCheckOutcome(
                claim_id=claim.claim_id,
                status="needs_review",
                reason=BINDING_ABSENT,
                result=None,
            )
        )

    return NumericAnalysisReport(
        tenant_id=identity[0],
        document_version_id=identity[1],
        parse_manifest_id=identity[2],
        source_sha256=identity[3],
        outcomes=tuple(outcomes),
    )


@dataclass(frozen=True, slots=True)
class NumericBindingProposal:
    """A source-linked review suggestion, never an accepted numeric finding."""

    claim_id: str
    binding: ClaimBinding | None
    reasons: tuple[str, ...]
    observation_ids: tuple[str, ...]
    dimension_refs: dict
    source_refs: tuple
    holds: tuple[dict, ...] = ()


def propose_numeric_bindings(*, tenant_id, original, observations, claims, contexts=()):
    """Propose a narrow literal comparison, leaving acceptance to reviewed import.

    Reuse normalized table roles and validated ClaimContext spans. No value-equality
    search, global context, alias conversion, source promotion or aggregation.
    Missing roles and unsupported quantity semantics remain explicit review holds.
    """
    import re
    from dataclasses import replace

    from proofops.application.assurance import _validated_dimension_text
    from proofops.application.evidence.binding import _axis_cues
    from proofops.domain.numeric import _verified, _whole_number

    # Reuse the runtime's snapshot/tenant checks before inspecting candidate text.
    analyze_numeric_consistency(
        tenant_id=tenant_id,
        original=original,
        observations=observations,
        claims=claims,
        bindings=(),
    )
    by_claim = {c.claim.claim_id: c for c in contexts}
    if len(by_claim) != len(contexts) or set(by_claim) - {c.claim_id for c in claims}:
        raise ValueError("duplicate or foreign numeric context")
    roles = {
        "metric": "metric_raw",
        "entity": "subject",
        "reporting_period": "reporting_period",
        "scope": "scope",
        "boundary": "organizational_boundary",
    }
    # ponytail: only absolute comparisons; sums, targets and intensity require reviewed input.
    unsupported = re.compile(r"목표|계획|예정|원단위|집약도|\b(target|goal|intensity)\b", re.I)
    number = re.compile(r"[+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?")
    proposals = []
    for claim in claims:
        context = by_claim.get(claim.claim_id)
        reasons, matches, bindings = [], [], []
        if not observations:
            reasons.append("normalized_observations_absent")
        refs = dict(context.dimensions) if context else {}
        if context is not None and context.claim != claim:
            raise ValueError("numeric context claim mismatch")
        values = {
            role: _validated_dimension_text(
                ref, claim=claim, original=original, tenant_id=tenant_id
            )
            for role, ref in refs.items()
        }
        if context is None:
            reasons.append("claim_context_absent")
        for role in ("metric", "entity", "reporting_period"):
            if not values.get(role):
                reasons.append(f"dimension_unresolved:{role}")
        for role in refs:
            if values.get(role) is None:
                reasons.append(f"dimension_unresolved:{role}")
        if (
            claim.source_quality != "verified"
            or not claim.source_refs
            or not all(_verified(ref, original) for ref in claim.source_refs)
            or claim.quote != " ".join(ref.quote for ref in claim.source_refs)
        ):
            reasons.append("claim_source_unresolved")
        for axis in ("scope", "boundary"):
            if not _axis_cues(claim.quote, axis) <= _axis_cues(values.get(axis) or "", axis):
                reasons.append(f"dimension_unresolved:{axis}")
        if unsupported.search(claim.quote):
            reasons.append("target_or_intensity_requires_reviewed_input")
        # Product/material/facility cannot be silently squeezed into entity/boundary.
        if set(refs) - set(roles):
            reasons.append("additional_dimensions_require_reviewed_input")
        if not reasons:
            for item in observations:
                if not all(
                    getattr(item, field) == values.get(role) for role, field in roles.items()
                ):
                    continue
                matches.append(item)
                if item.value_state != "value" or item.value_decimal is None:
                    reasons.append(f"value_state:{item.value_state}")
                    continue
                if (
                    item.denominator
                    or "/" in (item.unit_canonical or "")
                    or unsupported.search(" ".join(ref.quote for ref in item.source_refs))
                ):
                    reasons.append("target_or_intensity_requires_reviewed_input")
                    continue
                unit = item.unit_canonical
                if not unit or item.scale_multiplier != "1":
                    reasons.append("unit_missing_or_scale_requires_reviewed_input")
                    continue
                # Basis is not a ClaimContext role: retain only a unique local literal.
                if item.scope2_basis:
                    basis_refs = [
                        source
                        for source in claim.source_refs
                        if source.quote.count(item.scope2_basis) == 1
                    ]
                    if len(basis_refs) != 1:
                        reasons.append("dimension_unresolved:scope2_basis")
                        continue
                    source = basis_refs[0]
                    start = source.char_start + source.quote.index(item.scope2_basis)
                    refs["scope2_basis"] = replace(
                        source,
                        quote=item.scope2_basis,
                        char_start=start,
                        char_end=start + len(item.scope2_basis),
                    )
                spans = []
                for source in claim.source_refs:
                    for token in number.finditer(source.quote):
                        tail = source.quote[token.end() :]
                        if not re.match(r"\s*" + re.escape(unit) + r"(?=$|\s|[.,;)])", tail):
                            continue
                        span = replace(
                            source,
                            quote=token.group(),
                            char_start=source.char_start + token.start(),
                            char_end=source.char_start + token.end(),
                        )
                        if _verified(span, original) and _whole_number(span, original):
                            spans.append(span)
                if len(spans) != 1:
                    reasons.append("number_unit_span_missing_or_ambiguous")
                    continue
                span = spans[0]
                # Unit gets its own exact source span, not a copied observation assertion.
                source = next(
                    r
                    for r in claim.source_refs
                    if r.source_id == span.source_id
                    and r.char_start <= span.char_start < span.char_end <= r.char_end
                )
                offset = span.char_end + len(
                    source.quote[span.char_end - source.char_start :].split(unit, 1)[0]
                )
                refs["unit"] = replace(
                    source, quote=unit, char_start=offset, char_end=offset + len(unit)
                )
                bindings.append(
                    ClaimBinding(
                        claim_id=claim.claim_id,
                        tenant_id=tenant_id,
                        document_version_id=claim.document_version_id,
                        parse_manifest_id=claim.parse_manifest_id,
                        kind="comparison",
                        observation_ids=(item.observation_id,),
                        reported_value=span.quote,
                        metric_raw=values["metric"],
                        subject=values["entity"],
                        reporting_period=values["reporting_period"],
                        scope=values.get("scope"),
                        organizational_boundary=values.get("boundary"),
                        scope2_basis=item.scope2_basis,
                        unit=unit,
                        denominator=None,
                        quantity_kind="absolute",
                        source_refs=claim.source_refs,
                        reported_value_ref=span,
                        binding_accepted=False,
                    )
                )
            if not matches:
                reasons.append("no_exact_metric_year_entity_scope_boundary_observation")
        holds = tuple(
            observation_source_holds(item, original)
            | {
                "quality": item.quality,
                "value_state": item.value_state,
                "source_refs": item.source_refs,
                "parent_relations": item.parent_relations,
            }
            for item in matches
        )
        for item in matches:
            if item.quality != "verified" or not all(
                _verified(r, original) for r in item.source_refs
            ):
                reasons.append("observation_source_unresolved")
        for hold in holds:
            reasons.extend(hold["reasons"])
        if len(matches) > 1:
            reasons.append("multiple_observations_require_reviewed_input")
        binding = bindings[0] if len(bindings) == 1 and len(matches) == 1 else None
        if binding is not None:
            reasons.append("explicit_review_required")
            reasons.append("quantity_role_not_automatically_accepted")
        proposals.append(
            NumericBindingProposal(
                claim.claim_id,
                binding,
                tuple(dict.fromkeys(reasons)),
                tuple(item.observation_id for item in matches),
                refs,
                claim.source_refs,
                holds,
            )
        )
    return tuple(proposals)
