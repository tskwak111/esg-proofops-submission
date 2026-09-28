"""Shared validation and decision primitives for the reconciliation engine.

Pure domain code: standard library only, no I/O, no environment access, and no
imports from the application or adapter layers. Input is treated as already
verified by the application layer; this module still refuses anything that does
not honour the published 1.1 contract shape.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import NoReturn

from proofops.domain.errors import DomainValidationError

SCHEMA_VERSION = "1.1"
C5_ITEM = "C5"

ITEMS = ("C1", "C2", "C3", "C4")
KINDS = ("entity_set", "facility_set", "currency_amount", "period", "classification", "unknown")
TRACKS = ("goal", "performance", "management", "unknown")
CONSOLIDATION = ("consolidated", "separate", "unknown")
COMPARABILITY = ("comparable", "not_comparable", "unknown")
SEARCH_STATES = ("complete", "incomplete", "not_run")
DIFFERENCE_TYPES = (
    "operational_control_vs_control",
    "overseas_subsidiary_excluded",
    "equity_method_excluded",
    "acquisition_disposal_proration",
)
SET_KINDS = ("entity_set", "facility_set")

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_PERIOD_RE = re.compile(r"^([0-9]{4}-[0-9]{2}-[0-9]{2})/([0-9]{4}-[0-9]{2}-[0-9]{2})$")
_DECIMAL_RE = re.compile(r"^-?[0-9]+(?:\.[0-9]+)?$")
_RATIO_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_CORP_RE = re.compile(r"^[0-9]{8}$")

IDENTITY_KEYS = (
    "tenant_id",
    "company_id",
    "claim_id",
    "package_id",
    "period_start",
    "period_end",
    "sustainability_document_version",
    "financial_document_version",
    "dart_corp_code",
    "financial_fiscal_year",
    "consolidation",
    "financial_period_start",
    "financial_period_end",
    "sr_published_at",
    "financial_published_at",
    "rcept_no",
    "as_of_date",
)
PACKET_KEYS = (
    "schema_version",
    "synthetic",
    "identity",
    "item",
    "sources",
    "sustainability",
    "financial",
    "comparability",
    "explanation",
    "c3_context",
    "claim",
    "search",
    "c4_context",
)
POLICY_KEYS = (
    "schema_version",
    "version",
    "approved",
    "synthetic_only",
    "current_stage",
    "enabled_items",
    "c1_identity_rule",
    "c3_threshold",
    "c3_account_mapping_approved",
    "approved_by",
    "approved_on",
    "source_policy_sha256",
    "allowed_capex_account_ids",
    "coverage_policy_id",
    "allowed_difference_types",
    "c2_timing_rule",
    "c4_required_explanations",
)
SOURCE_KEYS = ("source_id", "document_id", "artifact_sha256", "locator", "quote")
FACT_KEYS = ("raw", "normalized", "kind", "unit", "source_id")
SEARCH_KEYS = (
    "state",
    "coverage_policy_id",
    "required_document_ids",
    "reviewed_source_ids",
    "failed_document_ids",
    "receipt_id",
)
CLAIM_KEYS = ("track", "quote", "source_id", "trigger_elements", "fiscal_year")
C3_KEYS = (
    "currency",
    "target_period_start",
    "target_period_end",
    "capex_period_start",
    "capex_period_end",
    "capex_account_ids",
    "commitment_source_id",
    "funding_plan_source_id",
)
C4_KEYS = ("classification_name", "definition_source_ids", "calculation_source_ids")

C1_IDENTITY_RULE = "exact_verified_entity_set"
C2_TIMING_RULE = "same_period_or_verified_explanation"
C4_REQUIRED_EXPLANATIONS = ["definition", "calculation_basis"]


# --------------------------------------------------------------------------- #
# outcome
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Outcome:
    """One item decision, before it is projected onto the 1.1 output shape."""

    execution_state: str
    status: str | None
    reason_codes: tuple[str, ...]
    extra_source_ids: tuple[str, ...] = ()
    explanation_source_id: str | None = None


def completed(
    status: str, *reasons: str, sources: tuple[str, ...] = (), explanation: str | None = None
) -> Outcome:
    return Outcome("completed", status, tuple(reasons), tuple(sources), explanation)


def blocked(*reasons: str, sources: tuple[str, ...] = ()) -> Outcome:
    return Outcome("blocked", None, tuple(reasons), tuple(sources), None)


# --------------------------------------------------------------------------- #
# low level guards
# --------------------------------------------------------------------------- #


def fail(message: str) -> NoReturn:
    raise DomainValidationError(message)


def require_mapping(value: object, name: str) -> dict:
    if type(value) is not dict:
        fail(f"{name} must be a JSON object")
    return value


def require_exact_keys(mapping: dict, keys: tuple[str, ...], name: str) -> None:
    present = set(mapping)
    expected = set(keys)
    missing = sorted(expected - present)
    unknown = sorted(present - expected)
    if missing:
        fail(f"{name} is missing required fields: {', '.join(missing)}")
    if unknown:
        fail(f"{name} has unknown fields: {', '.join(unknown)}")


def require_bool(mapping: dict, key: str, name: str) -> bool:
    value = mapping[key]
    if type(value) is not bool:
        fail(f"{name}.{key} must be a boolean")
    return value


def require_int(mapping: dict, key: str, name: str, *, low: int, high: int) -> int:
    value = mapping[key]
    if type(value) is not int:
        fail(f"{name}.{key} must be an integer")
    if not low <= value <= high:
        fail(f"{name}.{key} is out of range")
    return value


def require_text(mapping: dict, key: str, name: str) -> str:
    value = mapping[key]
    if type(value) is not str or not value:
        fail(f"{name}.{key} must be a non-empty string")
    return value


def require_optional_text(mapping: dict, key: str, name: str) -> str | None:
    value = mapping[key]
    if value is None:
        return None
    if type(value) is not str or not value:
        fail(f"{name}.{key} must be a non-empty string or null")
    return value


def require_enum(mapping: dict, key: str, name: str, allowed: tuple[str, ...]) -> str:
    value = mapping[key]
    if value not in allowed or type(value) is not str:
        fail(f"{name}.{key} must be one of: {', '.join(allowed)}")
    return value


def require_unique_text_list(mapping: dict, key: str, name: str) -> list[str]:
    value = mapping[key]
    if type(value) is not list:
        fail(f"{name}.{key} must be an array")
    for entry in value:
        if type(entry) is not str or not entry:
            fail(f"{name}.{key} entries must be non-empty strings")
    if len(set(value)) != len(value):
        fail(f"{name}.{key} entries must be unique")
    return value


def parse_date(value: str, name: str) -> date:
    if type(value) is not str or not _DATE_RE.match(value):
        fail(f"{name} must be a YYYY-MM-DD date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        fail(f"{name} is not a real calendar date")


def require_optional_date(mapping: dict, key: str, name: str) -> date | None:
    value = mapping[key]
    if value is None:
        return None
    return parse_date(value, f"{name}.{key}")


def require_ordered_period(mapping: dict, start_key: str, end_key: str, name: str) -> None:
    start = require_optional_date(mapping, start_key, name)
    end = require_optional_date(mapping, end_key, name)
    if start is not None and end is not None and start > end:
        fail(f"{name}.{start_key} must not be after {name}.{end_key}")


def parse_decimal(value: str, name: str) -> Decimal:
    if type(value) is not str or not _DECIMAL_RE.match(value):
        fail(f"{name} must be a plain decimal string")
    return Decimal(value)


def parse_period(value: str, name: str) -> tuple[date, date]:
    match = _PERIOD_RE.match(value)
    if not match:
        fail(f"{name} must be YYYY-MM-DD/YYYY-MM-DD")
    start = parse_date(match.group(1), f"{name} start")
    end = parse_date(match.group(2), f"{name} end")
    if start > end:
        fail(f"{name} start must not be after end")
    return start, end


def parse_entity_set(value: str, name: str) -> frozenset[str]:
    try:
        decoded = json.loads(value)
    except ValueError:
        fail(f"{name} must be a JSON array of identifiers")
    if type(decoded) is not list:
        fail(f"{name} must be a JSON array of identifiers")
    for entry in decoded:
        if type(entry) is not str or not entry:
            fail(f"{name} entries must be non-empty strings")
    if len(set(decoded)) != len(decoded):
        fail(f"{name} entries must be unique")
    return frozenset(decoded)


# --------------------------------------------------------------------------- #
# policy
# --------------------------------------------------------------------------- #


def validate_policy(policy: dict) -> None:
    require_exact_keys(policy, POLICY_KEYS, "policy")
    if policy["schema_version"] != SCHEMA_VERSION:
        fail("policy.schema_version must be 1.1")
    require_text(policy, "version", "policy")
    require_bool(policy, "approved", "policy")
    require_bool(policy, "synthetic_only", "policy")
    require_bool(policy, "c3_account_mapping_approved", "policy")
    if type(policy["current_stage"]) is not int:
        fail("policy.current_stage must be an integer")
    enabled = require_unique_text_list(policy, "enabled_items", "policy")
    for entry in enabled:
        if entry not in ITEMS:
            fail("policy.enabled_items may only contain C1, C2, C3 or C4")
    if policy["c1_identity_rule"] != C1_IDENTITY_RULE:
        fail(f"policy.c1_identity_rule must be {C1_IDENTITY_RULE}")
    if policy["c2_timing_rule"] != C2_TIMING_RULE:
        fail(f"policy.c2_timing_rule must be {C2_TIMING_RULE}")
    if policy["c4_required_explanations"] != C4_REQUIRED_EXPLANATIONS:
        fail("policy.c4_required_explanations must be [definition, calculation_basis]")
    threshold = policy["c3_threshold"]
    if threshold is not None and (type(threshold) is not str or not _RATIO_RE.match(threshold)):
        fail("policy.c3_threshold must be a non-negative decimal string or null")
    require_optional_text(policy, "approved_by", "policy")
    require_optional_date(policy, "approved_on", "policy")
    source_hash = policy["source_policy_sha256"]
    if type(source_hash) is not str or not _HEX64_RE.match(source_hash):
        fail("policy.source_policy_sha256 must be 64 lowercase hex characters")
    require_unique_text_list(policy, "allowed_capex_account_ids", "policy")
    require_text(policy, "coverage_policy_id", "policy")
    for entry in require_unique_text_list(policy, "allowed_difference_types", "policy"):
        if entry not in DIFFERENCE_TYPES:
            fail("policy.allowed_difference_types contains an unknown difference type")


# --------------------------------------------------------------------------- #
# packet
# --------------------------------------------------------------------------- #


def _validate_identity(identity: dict) -> None:
    require_exact_keys(identity, IDENTITY_KEYS, "identity")
    for key in (
        "tenant_id",
        "company_id",
        "claim_id",
        "package_id",
        "sustainability_document_version",
        "financial_document_version",
    ):
        require_text(identity, key, "identity")
    corp = identity["dart_corp_code"]
    if type(corp) is not str or not _CORP_RE.match(corp):
        fail("identity.dart_corp_code must be 8 digits")
    require_int(identity, "financial_fiscal_year", "identity", low=1900, high=2200)
    require_enum(identity, "consolidation", "identity", CONSOLIDATION)
    require_optional_text(identity, "rcept_no", "identity")
    require_ordered_period(identity, "period_start", "period_end", "identity")
    require_ordered_period(identity, "financial_period_start", "financial_period_end", "identity")
    require_optional_date(identity, "sr_published_at", "identity")
    require_optional_date(identity, "financial_published_at", "identity")
    parse_date(identity["as_of_date"], "identity.as_of_date")


def _validate_fact(fact: dict, name: str) -> None:
    require_exact_keys(fact, FACT_KEYS, name)
    require_optional_text(fact, "raw", name)
    require_optional_text(fact, "unit", name)
    require_optional_text(fact, "source_id", name)
    kind = require_enum(fact, "kind", name, KINDS)
    normalized = require_optional_text(fact, "normalized", name)
    if normalized is None:
        return
    if kind in SET_KINDS:
        parse_entity_set(normalized, f"{name}.normalized")
    elif kind == "period":
        parse_period(normalized, f"{name}.normalized")
    elif kind == "currency_amount":
        parse_decimal(normalized, f"{name}.normalized")


def _validate_sources(sources: object) -> list[str]:
    if type(sources) is not list:
        fail("sources must be an array")
    ids: list[str] = []
    for index, source in enumerate(sources):
        name = f"sources[{index}]"
        require_mapping(source, name)
        require_exact_keys(source, SOURCE_KEYS, name)
        for key in ("source_id", "document_id", "locator", "quote"):
            require_text(source, key, name)
        digest = source["artifact_sha256"]
        if type(digest) is not str or not _HEX64_RE.match(digest):
            fail(f"{name}.artifact_sha256 must be 64 lowercase hex characters")
        ids.append(source["source_id"])
    if len(set(ids)) != len(ids):
        fail("sources contain a duplicate source_id")
    return ids


def _validate_search(search: dict) -> None:
    require_exact_keys(search, SEARCH_KEYS, "search")
    state = require_enum(search, "state", "search", SEARCH_STATES)
    require_optional_text(search, "coverage_policy_id", "search")
    require_optional_text(search, "receipt_id", "search")
    for key in ("required_document_ids", "reviewed_source_ids", "failed_document_ids"):
        require_unique_text_list(search, key, "search")
    if state != "complete":
        return
    if search["failed_document_ids"]:
        fail("a complete search cannot have failed documents")
    if not search["coverage_policy_id"]:
        fail("a complete search requires a coverage policy")
    if not search["receipt_id"]:
        fail("a complete search requires a receipt")
    if not search["required_document_ids"]:
        fail("a complete search requires the list of documents it had to read")


def _validate_c3_context(context: dict) -> None:
    require_exact_keys(context, C3_KEYS, "c3_context")
    require_text(context, "currency", "c3_context")
    require_unique_text_list(context, "capex_account_ids", "c3_context")
    require_optional_text(context, "commitment_source_id", "c3_context")
    require_optional_text(context, "funding_plan_source_id", "c3_context")
    require_ordered_period(context, "target_period_start", "target_period_end", "c3_context")
    require_ordered_period(context, "capex_period_start", "capex_period_end", "c3_context")


def _validate_c4_context(context: dict) -> None:
    require_exact_keys(context, C4_KEYS, "c4_context")
    require_text(context, "classification_name", "c4_context")
    require_unique_text_list(context, "definition_source_ids", "c4_context")
    require_unique_text_list(context, "calculation_source_ids", "c4_context")


def validate_packet(packet: dict) -> None:
    require_exact_keys(packet, PACKET_KEYS, "packet")
    if packet["schema_version"] != SCHEMA_VERSION:
        fail("packet.schema_version must be 1.1")
    require_bool(packet, "synthetic", "packet")
    require_enum(packet, "item", "packet", ITEMS)
    require_enum(packet, "comparability", "packet", COMPARABILITY)

    identity = require_mapping(packet["identity"], "identity")
    _validate_identity(identity)

    source_ids = _validate_sources(packet["sources"])

    for side in ("sustainability", "financial"):
        _validate_fact(require_mapping(packet[side], side), side)

    explanation = require_mapping(packet["explanation"], "explanation")
    require_exact_keys(explanation, ("source_id", "search_complete"), "explanation")
    require_optional_text(explanation, "source_id", "explanation")
    require_bool(explanation, "search_complete", "explanation")

    claim = require_mapping(packet["claim"], "claim")
    require_exact_keys(claim, CLAIM_KEYS, "claim")
    require_enum(claim, "track", "claim", TRACKS)
    require_text(claim, "quote", "claim")
    require_text(claim, "source_id", "claim")
    require_unique_text_list(claim, "trigger_elements", "claim")
    require_int(claim, "fiscal_year", "claim", low=1900, high=2200)

    search = require_mapping(packet["search"], "search")
    _validate_search(search)

    item = packet["item"]
    for key, item_name, validator in (
        ("c3_context", "C3", _validate_c3_context),
        ("c4_context", "C4", _validate_c4_context),
    ):
        context = packet[key]
        if context is None:
            if item == item_name:
                fail(f"{key} is required for item {item_name}")
            continue
        validator(require_mapping(context, key))

    if explanation["search_complete"] != (search["state"] == "complete"):
        fail("explanation.search_complete must agree with search.state")

    known = set(source_ids)
    referenced: list[str | None] = [
        claim["source_id"],
        explanation["source_id"],
        packet["sustainability"]["source_id"],
        packet["financial"]["source_id"],
    ]
    referenced += search["reviewed_source_ids"]
    if packet["c3_context"]:
        referenced += [
            packet["c3_context"]["commitment_source_id"],
            packet["c3_context"]["funding_plan_source_id"],
        ]
    if packet["c4_context"]:
        referenced += packet["c4_context"]["definition_source_ids"]
        referenced += packet["c4_context"]["calculation_source_ids"]
    for reference in referenced:
        if reference is not None and reference not in known:
            fail(f"unknown source reference: {reference}")


# --------------------------------------------------------------------------- #
# shared item helpers
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Context:
    """Everything an item module needs, already validated."""

    packet: dict
    policy: dict

    @property
    def sustainability(self) -> dict:
        return self.packet["sustainability"]

    @property
    def financial(self) -> dict:
        return self.packet["financial"]

    @property
    def kind(self) -> str:
        return self.packet["sustainability"]["kind"]

    @property
    def explanation_source_id(self) -> str | None:
        return self.packet["explanation"]["source_id"]

    @property
    def search_complete(self) -> bool:
        return self.packet["search"]["state"] == "complete"

    def require_kind(self, *allowed: str) -> Outcome | None:
        if self.kind not in allowed:
            return blocked("kind_mismatch")
        return None

    def unresolved_values(self) -> Outcome | None:
        if self.sustainability["normalized"] is None or self.financial["normalized"] is None:
            return blocked("value_unresolved")
        return None

    def resolve_difference(self, *, explained_reason: str) -> Outcome:
        """Shared ladder for 'the two sides differ' across C1, C2 and C3."""
        if self.explanation_source_id is not None:
            return completed(
                "matched",
                explained_reason,
                sources=(self.explanation_source_id,),
                explanation=self.explanation_source_id,
            )
        if self.search_complete:
            return completed("needs_explanation", "explanation_not_found")
        return blocked("search_incomplete")
