"""AT-016: safe-harbor records preserve evidence without inventing a grade.

All records are explicitly synthetic and local-only.  They do not determine
legal effect or represent an approved safe-harbor mapping.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace

import pytest
from jsonschema import Draft202012Validator
from proofops.domain.rules.safe_harbor import record_safe_harbor

from .test_rules import DOCUMENT, ROOT, SOURCE, fact, inputs, pack


def _inputs(category: str | None, **states: str):
    tags, context = inputs("performance")
    facts = {item.name: item for item in tags.facts}
    facts.update({name: fact(name, state) for name, state in states.items()})
    return replace(tags, safe_harbor_category=category, facts=tuple(facts.values())), context


def test_missing_mapping_records_gap_without_assigning_e0() -> None:
    tags, context = _inputs(
        "emissions_estimate",
        identified_as_estimate="present",
        estimation_method="present",
        uncertainty="absent",
        quantitative_or_qualified_ordinal="absent",
        unit_or_qualified_ordinal="absent",
    )

    record = record_safe_harbor(tags, context, pack())
    payload = record.to_api_dict()

    assert record.mapping_status == "unresolved"
    assert record.reasonable_basis_documented is None
    assert record.gap_ids == ("GAP-001",)
    assert "evidence_grade" not in payload and "label" not in payload
    assert {item.element_id: item.state for item in record.checklist} == {
        "identified_as_estimate": "present",
        "estimation_method": "present",
        "uncertainty": "absent",
    }


@pytest.mark.parametrize(
    "category,expected",
    [
        ("forward_looking", ("assumptions", "scenario_or_premises")),
        (
            "emissions_estimate",
            ("identified_as_estimate", "estimation_method", "uncertainty"),
        ),
        (
            "third_party_information",
            ("source_named", "acquisition_context", "limitations"),
        ),
    ],
)
def test_each_category_uses_only_its_version_pinned_checklist(category, expected) -> None:
    tags, context = _inputs(category, **{name: "absent" for name in expected}, unrelated="present")

    record = record_safe_harbor(tags, context, pack())

    assert record.applicable is True
    assert record.category == category
    assert tuple(item.element_id for item in record.checklist) == expected


@pytest.mark.parametrize("state", ["unknown", "conflict"])
def test_unresolved_evidence_is_not_changed_to_absent(state: str) -> None:
    tags, context = _inputs("forward_looking", assumptions=state, scenario_or_premises="present")

    record = record_safe_harbor(tags, context, pack())

    assert record.checklist[0].state == state
    assert record.reasonable_basis_documented is None


def test_unreported_checklist_item_defaults_to_unknown_not_absent() -> None:
    tags, context = _inputs("forward_looking", assumptions="present")

    record = record_safe_harbor(tags, context, pack())

    assert {item.element_id: item.state for item in record.checklist} == {
        "assumptions": "present",
        "scenario_or_premises": "unknown",
    }


def test_present_requires_verified_same_tenant_document_evidence() -> None:
    tags, context = _inputs("forward_looking", scenario_or_premises="absent")
    bad = fact(
        "assumptions",
        evidence_refs=(replace(SOURCE, document_version_id=SOURCE.source_id),),
    )
    tags = replace(tags, facts=tags.facts + (bad,))

    with pytest.raises(ValueError, match="source"):
        record_safe_harbor(tags, context, pack())

    good = replace(bad, evidence_refs=(SOURCE,), source_tenant_id=DOCUMENT)
    tags = replace(
        tags,
        facts=tuple(good if item.name == "assumptions" else item for item in tags.facts),
    )
    with pytest.raises(ValueError, match="source"):
        record_safe_harbor(tags, context, pack())


def test_no_category_is_not_routed_into_safe_harbor() -> None:
    tags, context = _inputs(None)

    record = record_safe_harbor(tags, context, pack())

    assert record.applicable is False
    assert record.category is None
    assert record.checklist == ()
    assert record.gap_ids == ()


def test_record_is_immutable_and_matches_fixed_api_schema() -> None:
    tags, context = _inputs(
        "third_party_information",
        source_named="present",
        acquisition_context="absent",
        limitations="unknown",
    )
    record = record_safe_harbor(tags, context, pack())
    schema = json.loads((ROOT / "contracts/jsonschema/api_models.schema.json").read_text())

    Draft202012Validator({"$ref": "#/$defs/SafeHarborRecord", "$defs": schema["$defs"]}).validate(
        record.to_api_dict()
    )
    with pytest.raises(FrozenInstanceError):
        record.mapping_status = "approved"
