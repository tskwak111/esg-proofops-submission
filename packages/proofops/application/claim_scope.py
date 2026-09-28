"""Optional vertical claim-page scope for the frozen ``extraction_limits``.

This lives outside ``application.claims`` on purpose: ``claims.py`` is hashed
byte-for-byte as ``claim_validator_sha256`` in ``claim_source_policy_v2``, so any
edit there would break every existing real claim-source-policy replay. The
claim-page subset is a trust-boundary concern layered on top of the unchanged
validator, not part of it.

``claim_pages`` is optional. When present it must be a nonempty canonical
(sorted, de-duplicated, 1-based ``int``, no ``bool``) subset of the run's broad
``selected_pages``; it narrows only the claim-discovery scope and never widens
it or the parsed/evidence graph. Absent, the frozen value is exactly the legacy
``{max_calls, max_output_tokens}`` and callers keep old behaviour and hashes.
"""

from __future__ import annotations

_LEGACY_KEYS = {"max_calls", "max_output_tokens"}
_SCOPED_KEYS = {"max_calls", "max_output_tokens", "claim_pages"}


def validate_extraction_limits(limits: object, selected_pages: list[int]) -> None:
    """Reject any frozen ``extraction_limits`` that is not a trusted shape.

    Raises ``ValueError('EXTRACTION_LIMITS_INVALID')`` on any violation; a valid
    legacy ``{max_calls, max_output_tokens}`` mapping passes unchanged.
    """
    if (
        not isinstance(limits, dict)
        or set(limits) not in (_LEGACY_KEYS, _SCOPED_KEYS)
        or type(limits["max_calls"]) is not int
        or not 1 <= limits["max_calls"] <= 20
        or type(limits["max_output_tokens"]) is not int
        or not 1 <= limits["max_output_tokens"] <= 1024
    ):
        raise ValueError("EXTRACTION_LIMITS_INVALID")
    if "claim_pages" in limits:
        pages = limits["claim_pages"]
        if (
            not isinstance(pages, list)
            or not pages
            or any(type(page) is not int or page < 1 for page in pages)
            or sorted(set(pages)) != pages
            or not set(pages) <= set(selected_pages)
        ):
            raise ValueError("EXTRACTION_LIMITS_INVALID")


def claim_pages_for(limits: object, selected_pages: list[int]) -> list[int]:
    """Effective claim-discovery pages, validating the frozen limits first.

    Absent ``extraction_limits`` (the local-synthetic legacy path) preserves the
    broad ``selected_pages``. A present mapping is validated on every read so a
    tampered frozen scope fails closed at the replay/worker trust boundary before
    any model call, rather than silently narrowing or widening. Never widens
    beyond ``selected_pages``.
    """
    if limits is None:
        return list(selected_pages)
    validate_extraction_limits(limits, selected_pages)
    if isinstance(limits, dict) and "claim_pages" in limits:
        return list(limits["claim_pages"])
    return list(selected_pages)
