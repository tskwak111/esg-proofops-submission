"""Pure Developer B reconciliation engine (contract 1.1).

``evaluate(packet, policy)`` applies an approved policy to an already verified
packet and returns the 1.1 output object. It computes no grade and no label:
the existing G/P/M ladder is a separate axis and is untouched by this module.

Purity: standard library plus other ``proofops.domain`` modules only. No file,
network, environment or adapter access, and the caller's dictionaries are never
mutated.

Canonical hashing is ``json.dumps(value, ensure_ascii=True, sort_keys=True,
separators=(",", ":"))`` over UTF-8, matching the published handoff fixtures.
``canonical_json`` and ``canonical_sha256`` are exported so the application layer
and the CLI hash exactly the same bytes.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy

from proofops.domain.errors import DomainValidationError

from . import c1, c2, c3, c4
from .common import (
    C5_ITEM,
    SCHEMA_VERSION,
    Context,
    Outcome,
    blocked,
    completed,
    validate_packet,
    validate_policy,
)

ENGINE_VERSION = "reconciliation-engine-1.1.0"

_ITEM_EVALUATORS = {
    "C1": c1.evaluate,
    "C2": c2.evaluate,
    "C3": c3.evaluate,
    "C4": c4.evaluate,
}

_C5_MESSAGE = (
    "stage 1 does not implement accounting-judgement reconciliation; "
    "the dispatcher must record not_run with stage_disabled instead of calling the engine"
)


def canonical_json(value: object) -> str:
    """Serialise ``value`` exactly as the 1.1 hash contract prescribes.

    ``allow_nan=False`` so a non-finite float is refused rather than emitted as
    the non-standard ``NaN`` / ``Infinity`` tokens, which would make two callers
    disagree about the hash of the same logical packet.
    """
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as error:
        raise DomainValidationError("value is not canonical JSON serialisable") from error


def canonical_sha256(value: object) -> str:
    """SHA-256 of :func:`canonical_json` encoded as UTF-8."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _policy_gate(packet: dict, policy: dict) -> Outcome | None:
    if policy["approved"] is not True:
        return blocked("policy_unapproved")
    if policy["current_stage"] != 1:
        return blocked("stage_not_supported")
    if policy["synthetic_only"] and not packet["synthetic"]:
        return blocked("synthetic_policy_real_packet")
    if packet["item"] not in policy["enabled_items"]:
        return blocked("item_not_enabled")
    return None


def _decide(packet: dict, policy: dict) -> Outcome:
    gate = _policy_gate(packet, policy)
    if gate is not None:
        return gate

    comparability = packet["comparability"]
    if comparability == "not_comparable":
        # A verified non-comparison is a fact about the pair, not a processing
        # failure, so it completes as not_applicable.
        return completed("not_applicable", "not_comparable")
    if comparability == "unknown":
        return blocked("comparability_unknown")

    if packet["sustainability"]["kind"] != packet["financial"]["kind"]:
        return blocked("kind_mismatch")
    if packet["sustainability"]["kind"] == "unknown":
        return blocked("value_unresolved")

    return _ITEM_EVALUATORS[packet["item"]](Context(packet=packet, policy=policy))


def _source_ids(packet: dict, outcome: Outcome) -> list[str]:
    ordered: list[str] = []
    candidates = [packet["sustainability"]["source_id"], packet["financial"]["source_id"]]
    candidates.extend(outcome.extra_source_ids)
    for source_id in candidates:
        if source_id is not None and source_id not in ordered:
            ordered.append(source_id)
    return ordered


def _project(packet: dict, policy: dict, outcome: Outcome) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "claim_id": packet["identity"]["claim_id"],
        "item": packet["item"],
        "execution_state": outcome.execution_state,
        "status": outcome.status,
        "review_required": outcome.execution_state != "completed",
        "reason_codes": list(outcome.reason_codes),
        "source_ids": _source_ids(packet, outcome),
        "explanation_source_id": outcome.explanation_source_id,
        "sustainability_value": packet["sustainability"]["raw"],
        "financial_value": packet["financial"]["raw"],
        "packet_sha256": canonical_sha256(packet),
        "policy_sha256": canonical_sha256(policy),
        "synthetic": packet["synthetic"],
        "engine_version": ENGINE_VERSION,
    }


def evaluate(packet: dict, policy: dict) -> dict:
    """Evaluate one reconciliation item.

    Raises ``DomainValidationError`` (a ``ValueError``) for malformed input and
    ``NotImplementedError`` for any C5 request. Unresolved policy, source or
    search state never raises: it returns ``execution_state="blocked"`` with
    ``status=None``.
    """
    if type(packet) is not dict:
        raise DomainValidationError("packet must be a JSON object")
    if type(policy) is not dict:
        raise DomainValidationError("policy must be a JSON object")
    if packet.get("item") == C5_ITEM:
        raise NotImplementedError(_C5_MESSAGE)

    packet = deepcopy(packet)
    policy = deepcopy(policy)
    validate_policy(policy)
    validate_packet(packet)
    return _project(packet, policy, _decide(packet, policy))
