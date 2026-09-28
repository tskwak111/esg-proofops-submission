"""Original v2 §4.4 exceptions on confirmed, citation/binding-verified facts.

`comparison_basis` attests the comparison group, measurement unit and source;
`direct_product_material_ratio_or_target` attests a number directly bound to the
claimed product/material. These are upstream binding results, not text searches.
A `categorical_ordinal` fact selects ordinal P1 qualification; raw numeric claims
continue to use the existing P1 primitives. Raw tags and their sources are retained.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from proofops.domain.errors import DomainValidationError

if TYPE_CHECKING:
    from proofops.domain.rulepacks import RulePackSnapshot
    from proofops.domain.rules.engine import ConfirmedTags, RuleContext


@dataclass(frozen=True, slots=True)
class ExceptionEffect:
    override_grade: str | None = None
    cap_grade: str | None = None
    gap_ids: tuple[str, ...] = ()
    unresolved_elements: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    ordinal_state: str | None = None


def apply_source_exceptions(
    tags: ConfirmedTags, context: RuleContext, pack: RulePackSnapshot
) -> ExceptionEffect:
    """Return an override/cap/gap without assigning a label or mutating inputs."""
    from proofops.domain.rules.engine import _validate_inputs

    _validate_inputs(tags, context, pack)
    facts = {fact.name: fact for fact in tags.facts}

    def state(name: str) -> str:
        return facts[name].state if name in facts else "unknown"

    special = pack.file_content("rubric/exceptions.yaml")["superlative"]
    has_superlative = bool(tags.superlative_quote) or (
        "has_superlative" in facts and state("has_superlative") != "absent"
    )
    if tags.safe_harbor_category:
        return ExceptionEffect(
            gap_ids=("GAP-001", special["with_safe_harbor"]) if has_superlative else ("GAP-001",)
        )

    unresolved: list[str] = []
    if has_superlative and special["enabled"]:
        original = facts.get("has_superlative")
        if not (
            original
            and original.state == "present"
            and tags.superlative_quote
            and tags.superlative_quote.strip()
            and any(tags.superlative_quote in ref.quote for ref in original.evidence_refs)
        ):
            unresolved.append("has_superlative")
        else:
            operands = ("comparison_basis", "external_verification")
            if all(state(name) == "absent" for name in operands):
                return ExceptionEffect(override_grade="E0", rule_ids=("SUPERLATIVE_E0",))
            if not any(state(name) == "present" for name in operands):
                unresolved.extend(name for name in operands if state(name) != "absent")

    ordinal = None
    rules = []
    if tags.track == "performance" and "categorical_ordinal" in facts:
        names = ("categorical_ordinal", "certification_provider")
        for name in names:
            if state(name) == "present" and not (facts[name].normalized_value or "").strip():
                raise DomainValidationError(
                    "categorical ordinal requires a grade and named provider"
                )
        states = [state(name) for name in names]
        # Derive qualification, never rewrite either observed primitive as absent.
        ordinal = (
            "present"
            if all(s == "present" for s in states)
            else "absent"
            if "absent" in states
            else "conflict"
            if "conflict" in states
            else "unknown"
        )
        rules.append("CATEGORICAL_ORDINAL")

    cap = None
    gaps = []
    if tags.product_variant:
        if tags.track != "management":
            gaps.append("GAP-007")
        else:
            product = pack.file_content("rubric/management.yaml")["product_variant"]
            direct = product["e2_plus_requires"]
            if state(direct) == "absent":
                cap = product["specific_names_only_cap"]
                rules.append("PRODUCT_NAMES_CAP")
            elif state(direct) == "present":
                gaps.append(product["generic_boundary_interaction"])
            else:
                unresolved.append(direct)
    return ExceptionEffect(
        cap_grade=cap,
        gap_ids=tuple(gaps),
        unresolved_elements=tuple(unresolved),
        rule_ids=tuple(rules),
        ordinal_state=ordinal,
    )
