"""AT-015: real rules on explicitly synthetic, attested same-document facts."""

from dataclasses import FrozenInstanceError, replace
from itertools import product

import pytest

from .test_rules import DOCUMENT, SOURCE, evaluate, fact, inputs, pack


def with_facts(tags, *facts):
    merged = {f.name: f for f in tags.facts}
    merged.update({f.name: f for f in facts})
    return replace(tags, facts=tuple(merged.values()))


def superlative(track="management", comparison="absent", verification="absent"):
    tags, context = inputs(track)
    quote = "synthetic 업계 최고"
    source = replace(SOURCE, quote=quote, char_end=len(quote))
    tags = with_facts(
        tags,
        fact("has_superlative", evidence_refs=(source,)),
        fact("comparison_basis", comparison),
        fact("external_verification", verification),
    )
    return replace(tags, superlative_quote=quote), context


@pytest.mark.parametrize("track", ["goal", "performance", "management"])
@pytest.mark.parametrize("comparison,verification", list(product(["present", "absent"], repeat=2)))
def test_only_both_verified_absences_force_e0(track, comparison, verification):
    from proofops.domain.rules.exceptions import apply_source_exceptions

    tags, context = superlative(track, comparison, verification)
    effect = apply_source_exceptions(tags, context, pack())
    expected = "E0" if comparison == verification == "absent" else None
    assert effect.override_grade == expected
    result = evaluate(tags, context)
    assert result.decision_status == "decided"
    assert result.evidence_grade == (
        expected or ("E2" if track == "management" and verification == "absent" else "E3")
    )
    if expected:
        assert result.label == "UNSUBSTANTIATED"
        assert "SUPERLATIVE_E0" in result.rule_ids


@pytest.mark.parametrize("state", ["unknown", "conflict", "not_applicable"])
@pytest.mark.parametrize("name", ["comparison_basis", "external_verification"])
def test_unresolved_superlative_input_is_never_absent(name, state):
    tags, context = superlative()
    result = evaluate(with_facts(tags, fact(name, state)), context)
    assert result.evidence_grade is None
    assert result.decision_status == "blocked_evidence"
    assert name in result.unresolved_elements
    assert name not in result.missing_elements


def test_one_present_operand_prevents_override_despite_other_unknown():
    tags, context = superlative("goal", "present", "unknown")
    assert evaluate(tags, context).evidence_grade == "E3"


def test_superlative_requires_verified_original_quote():
    tags, context = superlative()
    for changed in (
        replace(tags, facts=tuple(f for f in tags.facts if f.name != "has_superlative")),
        replace(tags, superlative_quote="invented wording"),
    ):
        result = evaluate(changed, context)
        assert result.evidence_grade is None
        assert result.decision_status == "blocked_evidence"


def test_safe_harbor_collision_preserves_gap_and_no_grade():
    tags, context = superlative()
    result = evaluate(replace(tags, safe_harbor_category="forward_looking"), context)
    assert result.evidence_grade is None and result.label is None
    assert "GAP-006" in result.gap_ids
    assert result.decision_status == "blocked_rule_gap"


@pytest.mark.parametrize("boundary", ["present", "absent"])
def test_product_names_without_direct_ratio_are_capped_at_e1(boundary):
    tags, context = inputs("management", org_boundary=boundary)
    tags = with_facts(tags, fact("direct_product_material_ratio_or_target", "absent"))
    result = evaluate(replace(tags, product_variant=True), context)
    assert result.evidence_grade == "E1"
    assert "PRODUCT_NAMES_CAP" in result.rule_ids


@pytest.mark.parametrize(
    "state,status",
    [
        ("present", "blocked_rule_gap"),
        ("unknown", "blocked_evidence"),
        ("conflict", "blocked_evidence"),
    ],
)
def test_product_ratio_does_not_invent_missing_boundary_contract(state, status):
    tags, context = inputs("management")
    tags = with_facts(tags, fact("direct_product_material_ratio_or_target", state))
    result = evaluate(replace(tags, product_variant=True), context)
    assert result.evidence_grade is None
    assert result.decision_status == status
    if state == "present":
        assert result.ladder_candidate == "E3" and "GAP-007" in result.gap_ids


def categorical(provider="present"):
    tags, context = inputs("performance", comparison_baseline="absent", assurance_covered="absent")
    tags = with_facts(
        tags,
        fact("categorical_ordinal", normalized_value="Synthetic certification Gold"),
        fact("certification_provider", provider, normalized_value="Synthetic Certifier"),
    )
    return tags, context


@pytest.mark.parametrize(
    "provider,grade,status",
    [
        ("present", "E1", "decided"),
        ("absent", "E0", "decided"),
        ("unknown", None, "blocked_evidence"),
        ("conflict", None, "blocked_evidence"),
    ],
)
def test_categorical_ordinal_requires_named_external_provider(provider, grade, status):
    tags, context = categorical(provider)
    result = evaluate(tags, context)
    assert result.evidence_grade == grade
    assert result.decision_status == status


def test_provider_name_does_not_grant_covered_assurance():
    tags, context = categorical()
    tags = with_facts(tags, fact("comparison_baseline"))
    result = evaluate(tags, context)
    assert result.evidence_grade == "E2"
    assert "P4" in result.missing_elements


@pytest.mark.parametrize(
    "name", ["has_superlative", "direct_product_material_ratio_or_target", "certification_provider"]
)
@pytest.mark.parametrize(
    "change",
    [
        {"source_tenant_id": DOCUMENT},
        {"evidence_refs": (replace(SOURCE, location_quality="unreadable"),)},
        {"evidence_refs": (replace(SOURCE, document_version_id=SOURCE.source_id),)},
    ],
)
def test_exception_sources_cannot_cross_identity_or_readability_boundary(name, change):
    tags, context = superlative()
    with pytest.raises(ValueError):
        evaluate(with_facts(tags, fact(name, **change)), context)


@pytest.mark.parametrize(
    "change", [{"binding_accepted": False}, {"evidence_refs": ()}, {"source_scope": "global_bound"}]
)
def test_other_product_or_global_ratio_never_qualifies(change):
    tags, context = inputs("management")
    with pytest.raises(ValueError):
        tags = with_facts(tags, fact("direct_product_material_ratio_or_target", **change))
        evaluate(replace(tags, product_variant=True), context)


def test_exception_hash_and_api_projection_preserve_revisions():
    import json

    from jsonschema import Draft202012Validator

    from .test_rules import ROOT

    tags, context = superlative()
    first = evaluate(tags, context)
    second = evaluate(replace(tags, tag_revision=2), replace(context, decision_revision=2))
    assert first == evaluate(tags, context)
    assert first.semantic_hash != second.semantic_hash
    assert first.decision_revision == 1 and second.decision_revision == 2
    assert first.evidence_grade == "E0"
    with pytest.raises(FrozenInstanceError):
        first.evidence_grade = "E3"
    schema = json.loads((ROOT / "contracts/jsonschema/api_models.schema.json").read_text())
    Draft202012Validator({"$ref": "#/$defs/Decision", "$defs": schema["$defs"]}).validate(
        first.to_api_dict()
    )


def test_product_cap_resolves_only_uncertainty_above_e1():
    tags, context = inputs("management", org_boundary="unknown", external_verification="unknown")
    tags = with_facts(tags, fact("direct_product_material_ratio_or_target", "absent"))
    result = evaluate(replace(tags, product_variant=True), context)
    assert result.evidence_grade == "E1"
    assert result.review_status == "needs_review"
    assert "M2" in result.unresolved_elements


def test_explicit_unknown_superlative_tag_cannot_silently_skip_override_check():
    tags, context = superlative()
    tags = with_facts(tags, fact("has_superlative", "unknown"))
    result = evaluate(replace(tags, superlative_quote=None), context)
    assert result.evidence_grade is None
    assert result.decision_status == "blocked_evidence"


def test_standalone_exception_boundary_rejects_wrong_tenant():
    from proofops.domain.rules.exceptions import apply_source_exceptions

    tags, context = superlative()
    with pytest.raises(ValueError):
        apply_source_exceptions(tags, replace(context, tenant_id=DOCUMENT), pack())
