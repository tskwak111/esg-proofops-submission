"""Submission contract checks; never certify source truth or human approval."""

import importlib.util
from pathlib import Path

import pytest

DIRECTORY = Path(__file__).resolve().parents[2] / "tests/fixtures/data-submission"
SPEC = importlib.util.spec_from_file_location(
    "submission_validator", DIRECTORY / "validate_data.py"
)
assert SPEC and SPEC.loader
validator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validator)


@pytest.mark.parametrize(
    ("table", "field", "value"),
    [
        ("numeric", "expected_check", "1-35.40/48.73=27.36%"),
        ("numeric", "scope2_basis", "market-based (inferred)"),
        ("numeric", "value_decimal", "0.2736"),
        ("numeric", "value_decimal", "NaN"),
        ("numeric", "value_raw", "35.40;48.73"),
        ("assurance", "level", "limited(Moderate)"),
        ("assurance", "expected_status", "verified"),
        ("claims", "annotation_status", "approved"),
        ("rules", "decision_status", "approved"),
        ("acceptance", "observed_value", "0.99"),
    ],
)
def test_rejects_semantic_contract_errors(table, field, value):
    tables = validator.load(DIRECTORY / "examples")
    tables[table][0][field] = value
    with pytest.raises(ValueError):
        validator.validate(tables)


def test_final_rejects_draft_even_after_rights_approval():
    tables = validator.load(DIRECTORY / "examples")
    for document in tables["corpus"]:
        document["rights_status"] = "approved"
    with pytest.raises(ValueError, match="adjudicat"):
        validator.validate(tables, final=True)


def test_pending_examples_and_undetermined_assurance_remain_reviewable():
    tables = validator.load(DIRECTORY / "examples")
    tables["assurance"][0].update(expected_status="undetermined", quote="")
    tables["numeric"][0].update(value_raw="1,234.50", value_decimal="1234.50")
    assert validator.validate(tables)["numeric"] == 1


def test_cross_document_numeric_reference_rejected():
    tables = validator.load(DIRECTORY / "examples")
    tables["numeric"][0]["document_id"] = "not-in-corpus"
    with pytest.raises(ValueError):
        validator.validate(tables)


@pytest.mark.parametrize(("raw", "decimal"), [("(1,234.50)", "-1234.50"), ("판독불가", "")])
def test_parenthesized_negative_and_unreadable_source_are_preserved(raw, decimal):
    tables = validator.load(DIRECTORY / "examples")
    tables["numeric"][0].update(value_raw=raw, value_decimal=decimal, expected_check="unresolved")
    assert validator.validate(tables)["numeric"] == 1
