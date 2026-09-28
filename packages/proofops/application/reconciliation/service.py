"""Provenance and authorization boundary for reconciliation 1.1."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from copy import deepcopy
from datetime import date
from typing import Any

from .schema import validate_schema
from .sources import validate_source_bytes

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def canonical_sha256(value: Mapping[str, Any]) -> str:
    """Return the handoff-1.1 canonical hash (ASCII-escaped JSON)."""
    raw = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _domain_evaluate(packet: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    from proofops.domain.reconciliation.engine import evaluate

    return evaluate(packet, policy)


def _blocked(packet: Mapping[str, Any], policy: Mapping[str, Any], *reasons: str) -> dict[str, Any]:
    sources = packet.get("sources", [])
    source_ids: set[str] = set()
    for source in sources:
        if isinstance(source, Mapping):
            source_id = source.get("source_id")
            if isinstance(source_id, str):
                source_ids.add(source_id)
    sustainability = packet.get("sustainability", {})
    financial = packet.get("financial", {})
    identity = packet.get("identity", {})
    return {
        "schema_version": "1.1",
        "claim_id": identity.get("claim_id", "invalid"),
        "item": packet.get("item", "C1"),
        "execution_state": "blocked",
        "status": None,
        "review_required": True,
        "reason_codes": sorted(set(reasons)) or ["unverifiable"],
        "source_ids": sorted(source_ids),
        "explanation_source_id": None,
        "sustainability_value": sustainability.get("raw"),
        "financial_value": financial.get("raw"),
        "packet_sha256": canonical_sha256(packet),
        "policy_sha256": canonical_sha256(policy),
        "synthetic": packet.get("synthetic") is True,
        "engine_version": "reconciliation-application-provenance-1",
    }


def _date(value: Any, code: str) -> date:
    if not isinstance(value, str):
        raise ValueError(code)
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(code) from exc


def _validate_semantics(packet: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    sources = packet["sources"]
    by_id: dict[str, dict[str, Any]] = {}
    for source in sources:
        source_id = source["source_id"]
        if source_id in by_id:
            raise ValueError("duplicate_source_id")
        by_id[source_id] = source
    refs: list[Any] = [
        packet["sustainability"]["source_id"],
        packet["financial"]["source_id"],
        packet["claim"]["source_id"],
        packet["explanation"]["source_id"],
        *packet["search"]["reviewed_source_ids"],
    ]
    c3 = packet["c3_context"]
    if c3 is not None:
        refs.extend([c3["commitment_source_id"], c3["funding_plan_source_id"]])
    c4 = packet["c4_context"]
    if c4 is not None:
        refs.extend(c4["definition_source_ids"])
        refs.extend(c4["calculation_source_ids"])
    if any(ref is not None and ref not in by_id for ref in refs):
        raise ValueError("source_reference_missing")
    identity = packet["identity"]
    if packet["claim"]["fiscal_year"] != identity["financial_fiscal_year"]:
        raise ValueError("claim_identity_mismatch")
    for start_key, end_key in (
        ("period_start", "period_end"),
        ("financial_period_start", "financial_period_end"),
    ):
        start, end = identity[start_key], identity[end_key]
        if (start is None) != (end is None):
            raise ValueError("period_incomplete")
        if start is not None and _date(start, "date_invalid") > _date(end, "date_invalid"):
            raise ValueError("date_order_invalid")
    if packet["claim"]["quote"] not in by_id[packet["claim"]["source_id"]]["quote"]:
        raise ValueError("claim_quote_source_mismatch")
    return by_id


def _trusted_policy(
    policy: dict[str, Any], registry: Mapping[str, Any] | None
) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(registry, Mapping):
        return None, "policy_unapproved"
    entry = registry.get(canonical_sha256(policy))
    if not isinstance(entry, Mapping) or entry.get("approved") is not True:
        return None, "policy_unapproved"
    for key in ("version", "source_policy_sha256"):
        if entry.get(key) != policy.get(key):
            return None, "policy_registry_mismatch"
    if (
        type(entry.get("synthetic_only")) is not bool
        or entry["synthetic_only"] != policy["synthetic_only"]
    ):
        return None, "policy_registry_mismatch"
    if not isinstance(entry.get("approved_by"), str) or not entry["approved_by"]:
        return None, "policy_registry_mismatch"
    try:
        _date(entry.get("approved_on"), "policy_registry_mismatch")
    except ValueError:
        return None, "policy_registry_mismatch"
    trusted = deepcopy(policy)
    trusted["approved"] = True
    trusted["approved_by"] = entry["approved_by"]
    trusted["approved_on"] = entry["approved_on"]
    trusted["synthetic_only"] = entry.get("synthetic_only") is True
    return trusted, None


def _document_reason(
    source: Mapping[str, Any], packet: Mapping[str, Any], registry: Mapping[str, Any] | None
) -> str | None:
    if not isinstance(registry, Mapping):
        return "document_registry_missing"
    entry = registry.get(source["document_id"])
    if not isinstance(entry, Mapping):
        return "document_registry_missing"
    if type(entry.get("synthetic")) is not bool or entry["synthetic"] != packet["synthetic"]:
        return "document_synthetic_mismatch"
    identity = packet["identity"]
    for field in ("tenant_id", "company_id", "package_id"):
        if entry.get(field) != identity[field]:
            return "document_identity_mismatch"
    if (
        entry.get("corp_code") != identity["dart_corp_code"]
        or entry.get("fiscal_year") != identity["financial_fiscal_year"]
    ):
        return "document_identity_mismatch"
    if entry.get("consolidation") != identity["consolidation"]:
        return "document_identity_mismatch"
    role = entry.get("document_role")
    if not isinstance(role, str):
        return "document_identity_mismatch"
    version = {
        "sustainability": identity["sustainability_document_version"],
        "financial": identity["financial_document_version"],
    }.get(role)
    if version is None or entry.get("document_version_id") != version:
        return "document_identity_mismatch"
    if role == "financial" and entry.get("rcept_no") != identity["rcept_no"]:
        return "document_identity_mismatch"
    published_key = {
        "sustainability": "sr_published_at",
        "financial": "financial_published_at",
    }[role]
    if entry.get("published_at") != identity[published_key]:
        return "document_publication_mismatch"
    if entry.get("as_of_date") != identity["as_of_date"]:
        return "document_as_of_mismatch"
    period_keys = {
        "sustainability": ("period_start", "period_end"),
        "financial": ("financial_period_start", "financial_period_end"),
    }[role]
    if entry.get("period_start") != identity[period_keys[0]]:
        return "document_period_mismatch"
    if entry.get("period_end") != identity[period_keys[1]]:
        return "document_period_mismatch"
    if entry.get("artifact_sha256") != source["artifact_sha256"]:
        return "document_artifact_mismatch"
    if packet["item"] not in entry.get("relevant_items", []):
        return "document_not_relevant"
    available = entry.get("available_on", entry.get("published_at"))
    try:
        if _date(available, "document_date_invalid") > _date(
            entry["as_of_date"], "document_date_invalid"
        ):
            return "document_not_available_as_of"
    except ValueError:
        return "document_date_invalid"
    decision = entry.get("decision_binding")
    if not isinstance(decision, Mapping):
        return "decision_binding_missing"
    expected_decision = {
        "item": packet["item"],
        "comparability": packet["comparability"],
        "claim": packet["claim"],
        "c3_context": packet["c3_context"],
        "c4_context": packet["c4_context"],
        "claim_id": identity["claim_id"],
    }
    if dict(decision) != expected_decision:
        return "decision_binding_mismatch"
    bindings = entry.get("source_bindings")
    binding = bindings.get(source["source_id"]) if isinstance(bindings, Mapping) else None
    if not isinstance(binding, Mapping):
        return "source_binding_missing"
    if binding.get("locator") != source["locator"] or binding.get("quote") != source["quote"]:
        return "source_binding_mismatch"
    return None


def _has_source_role(source: Mapping[str, Any], documents: Mapping[str, Any], role: str) -> bool:
    document = documents.get(source["document_id"])
    bindings = document.get("source_bindings") if isinstance(document, Mapping) else None
    binding = bindings.get(source["source_id"]) if isinstance(bindings, Mapping) else None
    roles = binding.get("roles") if isinstance(binding, Mapping) else None
    return isinstance(roles, list) and role in roles


def _verify_source(
    source: Mapping[str, Any],
    packet: Mapping[str, Any],
    source_reader: Any,
    document_registry: Mapping[str, Any] | None,
) -> str | None:
    reason = _document_reason(source, packet, document_registry)
    if reason is not None:
        return reason
    try:
        payload = source_reader(source)
        if not isinstance(payload, bytes):
            return "source_unverified"
        if hashlib.sha256(payload).hexdigest() != source["artifact_sha256"]:
            return "source_hash_mismatch"
        validator = getattr(source_reader, "validate", None)
        if callable(validator):
            if validator(source, payload) is not True:
                return "source_unverified"
        else:
            validate_source_bytes(payload, source)
    except Exception:
        return "source_unverified"
    return None


def _bind_facts(packet: dict[str, Any], documents: Mapping[str, Any]) -> str | None:
    sources = {source["source_id"]: source for source in packet["sources"]}
    for role in ("sustainability", "financial"):
        fact = packet[role]
        source = sources[fact["source_id"]]
        if not _has_source_role(source, documents, f"{role}_fact"):
            return "source_role_mismatch"
        document = documents.get(source["document_id"])
        if not isinstance(document, Mapping) or document.get("document_role") != role:
            return "source_role_mismatch"
        bindings = document.get("fact_bindings") if isinstance(document, Mapping) else None
        binding = bindings.get(source["source_id"]) if isinstance(bindings, Mapping) else None
        if not isinstance(binding, Mapping):
            return "fact_binding_missing"
        expected = {key: fact[key] for key in ("raw", "normalized", "kind", "unit")}
        trusted = {key: binding.get(key) for key in ("raw", "normalized", "kind", "unit")}
        if expected != trusted:
            return "fact_binding_mismatch"
        packet[role].update(trusted)
    return None


def _apply_coverage(
    packet: dict[str, Any],
    policy: Mapping[str, Any],
    registry: Mapping[str, Any] | None,
    documents: Mapping[str, Any],
) -> tuple[set[str] | None, str | None]:
    receipt_id = packet["search"]["receipt_id"]
    entry = (
        registry.get(receipt_id)
        if isinstance(registry, Mapping) and isinstance(receipt_id, str)
        else None
    )
    if not isinstance(entry, Mapping):
        if packet["search"]["state"] == "complete":
            return None, "coverage_unverified"
        packet["search"] = {
            "state": "not_run",
            "coverage_policy_id": None,
            "required_document_ids": [],
            "reviewed_source_ids": [],
            "failed_document_ids": [],
            "receipt_id": None,
        }
        packet["explanation"]["search_complete"] = False
        return set(), None
    identity = packet["identity"]
    for key in ("tenant_id", "company_id", "package_id"):
        if entry.get(key) != identity[key]:
            return None, "coverage_unverified"
    if entry.get("coverage_policy_id") != policy["coverage_policy_id"]:
        return None, "coverage_unverified"
    state = entry.get("state")
    required = entry.get("required_document_ids")
    reviewed = entry.get("reviewed_source_ids")
    failed = entry.get("failed_document_ids")
    if state not in {"complete", "incomplete", "not_run"}:
        return None, "coverage_unverified"
    if not isinstance(required, list):
        return None, "coverage_unverified"
    if not isinstance(reviewed, list):
        return None, "coverage_unverified"
    if not isinstance(failed, list):
        return None, "coverage_unverified"
    if any(
        not isinstance(value, str) or not value
        for values in (required, reviewed, failed)
        for value in values
    ):
        return None, "coverage_unverified"
    required_ids = [value for value in required if isinstance(value, str)]
    reviewed_ids = [value for value in reviewed if isinstance(value, str)]
    failed_ids = [value for value in failed if isinstance(value, str)]
    if any(document_id not in documents for document_id in required_ids):
        return None, "coverage_unverified"
    if state == "complete" and (not required_ids or failed_ids):
        return None, "coverage_unverified"
    packet["search"] = {
        "state": state,
        "coverage_policy_id": entry["coverage_policy_id"],
        "required_document_ids": required_ids,
        "reviewed_source_ids": reviewed_ids,
        "failed_document_ids": failed_ids,
        "receipt_id": receipt_id,
    }
    packet["explanation"]["search_complete"] = state == "complete"
    return set(reviewed_ids), None


def _candidate_shape(source: Any) -> bool:
    if not isinstance(source, dict) or set(source) != {
        "source_id",
        "document_id",
        "artifact_sha256",
        "locator",
        "quote",
    }:
        return False
    return (
        all(
            isinstance(source[key], str) and source[key]
            for key in ("source_id", "document_id", "locator", "quote")
        )
        and isinstance(source["artifact_sha256"], str)
        and _SHA256.fullmatch(source["artifact_sha256"]) is not None
    )


def reconcile(
    packet: dict[str, Any],
    policy: dict[str, Any],
    *,
    source_reader: Any,
    explanation_search: Any,
    policy_registry: Mapping[str, Any] | None = None,
    coverage_registry: Mapping[str, Any] | None = None,
    document_registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if isinstance(packet, Mapping) and packet.get("item") == "C5":
        raise NotImplementedError("stage_disabled")
    validate_schema("input", packet)
    validate_schema("policy", policy)
    trusted_packet = deepcopy(packet)
    trusted_policy, reason = _trusted_policy(deepcopy(policy), policy_registry)
    if reason is not None or trusted_policy is None:
        return _blocked(packet, policy, reason or "policy_unapproved")
    if trusted_policy["synthetic_only"] and not trusted_packet["synthetic"]:
        return _blocked(packet, policy, "policy_scope_mismatch")

    sources = _validate_semantics(trusted_packet)
    documents = document_registry if isinstance(document_registry, Mapping) else {}
    for source in sources.values():
        reason = _verify_source(source, trusted_packet, source_reader, document_registry)
        if reason is not None:
            return _blocked(packet, policy, reason)
    claim_source = sources[trusted_packet["claim"]["source_id"]]
    if not _has_source_role(claim_source, documents, "claim"):
        return _blocked(packet, policy, "source_role_mismatch")
    c3 = trusted_packet["c3_context"]
    if c3 is not None:
        for key, role in (
            ("commitment_source_id", "c3_commitment"),
            ("funding_plan_source_id", "c3_funding"),
        ):
            source_id = c3[key]
            if source_id is not None and not _has_source_role(sources[source_id], documents, role):
                return _blocked(packet, policy, "source_role_mismatch")
    c4 = trusted_packet["c4_context"]
    if c4 is not None:
        for key, role in (
            ("definition_source_ids", "c4_definition"),
            ("calculation_source_ids", "c4_calculation"),
        ):
            if any(
                not _has_source_role(sources[source_id], documents, role) for source_id in c4[key]
            ):
                return _blocked(packet, policy, "source_role_mismatch")
    reason = _bind_facts(trusted_packet, documents)
    if reason is not None:
        return _blocked(packet, policy, reason)
    reviewed, reason = _apply_coverage(trusted_packet, trusted_policy, coverage_registry, documents)
    if reason is not None or reviewed is None:
        return _blocked(packet, policy, reason or "coverage_unverified")

    explanation_id = trusted_packet["explanation"]["source_id"]
    if explanation_id is None:
        try:
            candidates = explanation_search(deepcopy(trusted_packet))
        except Exception:
            return _blocked(packet, policy, "explanation_search_failed")
        if not isinstance(candidates, list):
            return _blocked(packet, policy, "explanation_search_failed")
        verified: list[dict[str, Any]] = []
        for candidate in candidates:
            if not _candidate_shape(candidate) or candidate["source_id"] in sources:
                return _blocked(packet, policy, "explanation_source_unverified")
            if candidate["source_id"] not in reviewed:
                return _blocked(packet, policy, "coverage_unverified")
            reason = _verify_source(candidate, trusted_packet, source_reader, document_registry)
            if reason is not None:
                return _blocked(packet, policy, "explanation_source_unverified")
            if not _has_source_role(candidate, documents, "explanation"):
                return _blocked(packet, policy, "source_role_mismatch")
            verified.append(candidate)
        if verified:
            selected = min(verified, key=lambda value: value["source_id"])
            trusted_packet["sources"].append(deepcopy(selected))
            sources[selected["source_id"]] = selected
            trusted_packet["explanation"]["source_id"] = selected["source_id"]
    elif explanation_id not in reviewed:
        return _blocked(packet, policy, "coverage_unverified")

    if trusted_packet["search"]["state"] == "complete" and not reviewed.issubset(sources):
        return _blocked(packet, policy, "coverage_unverified")
    result = _domain_evaluate(trusted_packet, trusted_policy)
    # The submitted packet/policy identify this immutable invocation. Registry-derived
    # decisions and selected candidates are verification inputs, not new client revisions.
    result["packet_sha256"] = canonical_sha256(packet)
    result["policy_sha256"] = canonical_sha256(policy)
    return result
