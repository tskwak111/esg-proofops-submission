"""Supported literal period syntax, never fiscal/calendar equivalence or approval."""

import re
from datetime import date


def is_supported_period(value: str | None) -> bool:
    """Unknown formats remain unresolved; preserve the caller's original text."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if match := re.fullmatch(r"(?:FY\s*)?([0-9]{4})(?:년)?(?:\s+Q[1-4])?", text, re.I):
        return int(match[1]) > 0
    if match := re.fullmatch(r"([0-9]{4})년?\s*[-–—~]\s*([0-9]{4})년?", text):
        return 0 < int(match[1]) <= int(match[2])
    if match := re.fullmatch(
        r"([0-9]{4}-[0-9]{2}-[0-9]{2})(?:\s*(?:~|–|—|to| - )\s*" r"([0-9]{4}-[0-9]{2}-[0-9]{2}))?",
        text,
    ):
        try:
            start = date.fromisoformat(match[1])
            end = date.fromisoformat(match[2]) if match[2] else start
        except ValueError:
            return False
        return start <= end
    return False
