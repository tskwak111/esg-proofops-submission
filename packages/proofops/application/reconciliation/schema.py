"""Small stdlib validator for the frozen reconciliation JSON Schemas.

The wheel has no mandatory third-party runtime dependency. This module
implements only the draft-2020-12 keywords used by the bundled, unchanged
1.1 schemas and deliberately rejects unsupported schema keywords.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import date, datetime
from importlib import resources
from typing import Any


class SchemaValidationError(ValueError):
    pass


def _load(name: str) -> dict[str, Any]:
    if name not in {"input", "policy", "output"}:
        raise ValueError("unknown_reconciliation_schema")
    target = resources.files(__package__).joinpath("schemas", f"{name}.schema.json")
    return json.loads(target.read_text(encoding="utf-8"))


def validate_schema(name: str, value: Any) -> None:
    _validate(value, _load(name), "$")


def _fail(path: str, code: str) -> None:
    raise SchemaValidationError(f"{name_for(path)}_schema_invalid:{path}:{code}")


def name_for(path: str) -> str:
    return "reconciliation"


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "null":
        return value is None
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    raise SchemaValidationError(f"unsupported_schema_type:{expected}")


def _try(value: Any, schema: Mapping[str, Any], path: str) -> bool:
    try:
        _validate(value, schema, path)
    except SchemaValidationError:
        return False
    return True


def _validate(value: Any, schema: Mapping[str, Any], path: str) -> None:
    supported = {
        "$schema",
        "$id",
        "$comment",
        "title",
        "description",
        "type",
        "const",
        "enum",
        "properties",
        "additionalProperties",
        "required",
        "minLength",
        "pattern",
        "format",
        "minItems",
        "uniqueItems",
        "items",
        "minimum",
        "maximum",
        "anyOf",
        "allOf",
        "if",
        "then",
        "else",
    }
    if set(schema) - supported:
        raise SchemaValidationError("unsupported_schema_keyword")
    if "anyOf" in schema:
        if not any(_try(value, branch, path) for branch in schema["anyOf"]):
            _fail(path, "anyOf")
    expected = schema.get("type")
    if expected is not None:
        choices = [expected] if isinstance(expected, str) else expected
        if not any(_matches_type(value, choice) for choice in choices):
            _fail(path, "type")
    if "const" in schema and value != schema["const"]:
        _fail(path, "const")
    if "enum" in schema and value not in schema["enum"]:
        _fail(path, "enum")

    if isinstance(value, dict):
        required = schema.get("required", [])
        if any(key not in value for key in required):
            _fail(path, "required")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            _fail(path, "additionalProperties")
        for key, child in properties.items():
            if key in value:
                _validate(value[key], child, f"{path}.{key}")
    elif isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            _fail(path, "minItems")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True, separators=(",", ":")) for item in value]
            if len(encoded) != len(set(encoded)):
                _fail(path, "uniqueItems")
        if "items" in schema:
            for index, item in enumerate(value):
                _validate(item, schema["items"], f"{path}[{index}]")
    elif isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            _fail(path, "minLength")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            _fail(path, "pattern")
        format_name = schema.get("format")
        try:
            if format_name == "date":
                date.fromisoformat(value)
            elif format_name == "date-time":
                datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            _fail(path, "format")
    elif isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            _fail(path, "minimum")
        if "maximum" in schema and value > schema["maximum"]:
            _fail(path, "maximum")

    for branch in schema.get("allOf", []):
        condition = branch.get("if")
        if condition is None:
            _validate(value, branch, path)
        elif _try(value, condition, path):
            if "then" in branch:
                _validate(value, branch["then"], path)
        elif "else" in branch:
            _validate(value, branch["else"], path)
