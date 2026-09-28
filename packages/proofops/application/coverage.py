"""Pure coverage projection for run progress and partial completion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

RunStatus = Literal["queued", "running", "partial", "completed", "failed", "cancelled"]
_STATUSES = frozenset({"queued", "running", "partial", "completed", "failed", "cancelled"})


@dataclass(frozen=True, slots=True)
class CoverageUpdate:
    status: RunStatus
    coverage: dict[str, int | bool]
    complete_badge: bool


def update_coverage(
    *,
    run_status: RunStatus,
    full_scope: bool,
    pages_total: int,
    pages_processed: int,
    pages_unreadable: int,
    pages_unprocessed: int,
    chunks_discovered: int,
    chunks_processed: int,
    claims_discovered: int,
    claims_decided: int,
    claims_needs_review: int,
    stop_reason: str | None = None,
) -> CoverageUpdate:
    """Validate counters and derive API coverage without hiding unfinished work."""
    counters = {
        "pages_total": pages_total,
        "pages_processed": pages_processed,
        "pages_unreadable": pages_unreadable,
        "pages_unprocessed": pages_unprocessed,
        "chunks_discovered": chunks_discovered,
        "chunks_processed": chunks_processed,
        "claims_discovered": claims_discovered,
        "claims_decided": claims_decided,
        "claims_needs_review": claims_needs_review,
    }
    if run_status not in _STATUSES or type(full_scope) is not bool:
        raise ValueError("invalid run status or scope")
    if any(type(value) is not int or value < 0 for value in counters.values()):
        raise ValueError("coverage counters must be non-negative integers")
    if pages_processed + pages_unreadable + pages_unprocessed != pages_total:
        raise ValueError("page counters must partition pages_total")
    if chunks_processed > chunks_discovered:
        raise ValueError("processed chunks exceed discovered chunks")
    if claims_decided + claims_needs_review > claims_discovered:
        raise ValueError("handled claims exceed discovered claims")
    if stop_reason is not None and (not isinstance(stop_reason, str) or not stop_reason.strip()):
        raise ValueError("stop_reason must be non-empty text or null")

    counters_complete = (
        pages_unreadable == pages_unprocessed == 0
        and chunks_processed == chunks_discovered
        and claims_decided + claims_needs_review == claims_discovered
    )
    status: RunStatus = run_status
    if stop_reason == "BUDGET_EXHAUSTED" or (
        run_status == "completed" and full_scope and not counters_complete
    ):
        status = "partial"
    complete = status == "completed" and full_scope and counters_complete
    coverage = counters | {"full_scope": full_scope, "complete": complete}
    return CoverageUpdate(status, coverage, status == "completed" and complete)
