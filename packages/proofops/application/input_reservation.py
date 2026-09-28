"""Explicit conservative INPUT reservation for the Solar Pro 4 context bound.

This is a reservation ceiling, not a tokenizer or a character-based count
approximation: no chat tokenizer or UTF-8-plus-constant estimate is used
anywhere here. ``reservation_input_tokens`` (2**20 = 1048576) is a
conservative ceiling chosen to sit above BOTH the decimal (512000) and the
binary (524288) readings of the published "512K" context bound; it must never
be represented as the model's actual context window.

Returning this reservation bound proves nothing about model accuracy, and
actual provider-reported usage must settle separately against the budget
store (the overrun fence stops dispatch when actual usage exceeds the
reservation). Source evidence below is honestly described as a published
context bound, not experimentally proven tokenizer parity.

Consumers must pin ``canonical_hash(policy)`` in their runtime
artifacts/receipts themselves; this helper never computes or stores hashes.

Pure application helper: stdlib ``datetime``/``typing`` for parsing and
annotations plus the existing domain error type only. No ambient clock,
network, file, or environment access. Supports only ``solar-pro4``; there is
no Pro 3 support and no fallback model.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from proofops.domain.errors import DomainValidationError

_MODEL_ID: Final = "solar-pro4"
_SCHEMA: Final = "upstage-context-capacity-reservation-v1"
_RESERVATION_KIND: Final = "conservative-input-ceiling"
_RESERVATION_INPUT_TOKENS: Final = 2**20
_SOURCE_URL: Final = "https://www.upstage.ai/blog/en/solar-pro-4"
_SOURCE_CONTEXT_LABEL: Final = "512K"
_SOURCE_QUOTE: Final = "Solar Pro 4 supports a 512K context with up to 128K output tokens."
_CAPTURED_AT: Final = "2026-09-18T16:53:00Z"
_EXPIRES_AT: Final = "2026-09-25T00:00:00Z"
_REFRESHED_CAPTURED_AT: Final = "2026-09-25T10:57:00Z"
_REFRESHED_EXPIRES_AT: Final = "2026-10-02T00:00:00Z"
_NOTE: Final = (
    "Conservative input reservation ceiling above both decimal and binary "
    "readings of the published 512K bound; not the actual model window and "
    "not experimentally proven tokenizer parity. Actual provider usage must "
    "settle separately."
)

_PINNED_TIMESTAMPS: Final = frozenset(
    {
        (_CAPTURED_AT, _EXPIRES_AT),
        (_REFRESHED_CAPTURED_AT, _REFRESHED_EXPIRES_AT),
    }
)

_POLICY_KEYS: Final = frozenset(
    {
        "schema",
        "model_id",
        "reservation_kind",
        "reservation_input_tokens",
        "source_url",
        "source_context_label",
        "source_quote",
        "captured_at",
        "expires_at",
        "note",
    }
)


def solar_pro4_capacity_policy(refreshed: bool = False) -> dict[str, Any]:
    """Return the legacy policy, or explicitly opt into the 2026-09-25 revision."""
    if type(refreshed) is not bool:
        raise DomainValidationError("refreshed must be a boolean")
    captured, expires = (
        (_REFRESHED_CAPTURED_AT, _REFRESHED_EXPIRES_AT)
        if refreshed
        else (_CAPTURED_AT, _EXPIRES_AT)
    )
    return {
        "schema": _SCHEMA,
        "model_id": _MODEL_ID,
        "reservation_kind": _RESERVATION_KIND,
        "reservation_input_tokens": _RESERVATION_INPUT_TOKENS,
        "source_url": _SOURCE_URL,
        "source_context_label": _SOURCE_CONTEXT_LABEL,
        "source_quote": _SOURCE_QUOTE,
        "captured_at": captured,
        "expires_at": expires,
        "note": _NOTE,
    }


def _require_exact(name: str, value: object, expected: str) -> None:
    if not isinstance(value, str) or value != expected:
        raise DomainValidationError(f"{name} does not match the pinned capacity policy")


def _parse_policy_instant(name: str, value: object) -> datetime:
    if not isinstance(value, str):
        raise DomainValidationError(f"{name} must be a timezone-aware timestamp string")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DomainValidationError(f"{name} must be a valid ISO-8601 timestamp") from None
    if instant.tzinfo is None or instant.tzinfo.utcoffset(instant) is None:
        raise DomainValidationError(f"{name} must be timezone-aware")
    return instant


def validate_capacity_policy(policy: dict[str, Any], *, model_id: str, checked_at: datetime) -> int:
    """Validate the exact capacity policy and return the reservation bound.

    Raises ``DomainValidationError`` (a ``ValueError``) for any wrong model,
    shape, count, source, expiry, or time-type deviation. Timestamps must be
    timezone-aware with ``captured_at <= checked_at < expires_at``. Bool
    values are never accepted as counts.
    """
    if not isinstance(policy, dict):
        raise DomainValidationError("capacity policy must be an object")
    if set(policy.keys()) != set(_POLICY_KEYS):
        raise DomainValidationError("capacity policy shape does not match the pinned policy")
    if not isinstance(model_id, str) or model_id != _MODEL_ID:
        raise DomainValidationError("capacity policy supports only solar-pro4")
    _require_exact("schema", policy["schema"], _SCHEMA)
    _require_exact("policy model_id", policy["model_id"], model_id)
    _require_exact("reservation_kind", policy["reservation_kind"], _RESERVATION_KIND)
    count = policy["reservation_input_tokens"]
    if isinstance(count, bool) or not isinstance(count, int):
        raise DomainValidationError("reservation_input_tokens must be an int (bool not accepted)")
    if count != _RESERVATION_INPUT_TOKENS:
        raise DomainValidationError("reservation_input_tokens does not match the pinned policy")
    _require_exact("source_url", policy["source_url"], _SOURCE_URL)
    _require_exact("source_context_label", policy["source_context_label"], _SOURCE_CONTEXT_LABEL)
    quote = policy["source_quote"]
    if not isinstance(quote, str) or not quote.strip():
        raise DomainValidationError("source_quote must be non-empty text")
    if len(quote.split()) >= 25:
        raise DomainValidationError("source_quote must stay under 25 words")
    _require_exact("source_quote", quote, _SOURCE_QUOTE)
    captured_raw = policy["captured_at"]
    expires_raw = policy["expires_at"]
    captured = _parse_policy_instant("captured_at", captured_raw)
    expires = _parse_policy_instant("expires_at", expires_raw)
    if (captured_raw, expires_raw) not in _PINNED_TIMESTAMPS:
        raise DomainValidationError("capacity policy timestamps do not match the pinned policy")
    _require_exact("note", policy["note"], _NOTE)
    if not isinstance(checked_at, datetime):
        raise DomainValidationError("checked_at must be a timezone-aware datetime")
    if checked_at.tzinfo is None or checked_at.tzinfo.utcoffset(checked_at) is None:
        raise DomainValidationError("checked_at must be timezone-aware")
    if not captured <= checked_at < expires:
        raise DomainValidationError("capacity policy is not valid at checked_at")
    return _RESERVATION_INPUT_TOKENS
