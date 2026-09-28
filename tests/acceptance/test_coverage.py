"""AT-027: deterministic coverage; all counters are local synthetic fixtures."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from proofops.application.coverage import update_coverage


def counters(**changes):
    values = dict(
        pages_total=4,
        pages_processed=4,
        pages_unreadable=0,
        pages_unprocessed=0,
        chunks_discovered=6,
        chunks_processed=6,
        claims_discovered=3,
        claims_decided=2,
        claims_needs_review=1,
    )
    return values | changes


def test_full_scope_completion_keeps_review_pending_separate():
    result = update_coverage(run_status="completed", full_scope=True, **counters())

    assert result.status == "completed"
    assert result.coverage == counters() | {"full_scope": True, "complete": True}
    assert result.complete_badge is True


def test_budget_exhaustion_is_partial_and_never_gets_complete_badge():
    partial = counters(
        pages_processed=2,
        pages_unprocessed=2,
        chunks_processed=4,
        claims_decided=1,
    )
    result = update_coverage(
        run_status="completed",
        stop_reason="BUDGET_EXHAUSTED",
        full_scope=True,
        **partial,
    )

    assert result.status == "partial"
    assert result.coverage == partial | {"full_scope": True, "complete": False}
    assert result.complete_badge is False
    assert result.coverage["claims_discovered"] == 3
    assert result.coverage["claims_decided"] == 1
    assert result.coverage["claims_needs_review"] == 1


def test_declared_subset_can_finish_without_claiming_full_review():
    result = update_coverage(
        run_status="completed",
        full_scope=False,
        **counters(pages_processed=2, pages_unprocessed=2),
    )

    assert result.status == "completed"
    assert result.coverage["complete"] is False
    assert result.complete_badge is False


@pytest.mark.parametrize("status", ["queued", "running", "partial", "failed", "cancelled"])
def test_non_completed_run_never_claims_complete_even_when_counters_are_exhausted(status):
    result = update_coverage(run_status=status, full_scope=True, **counters())

    assert result.status == status
    assert result.coverage["complete"] is False
    assert result.complete_badge is False


@pytest.mark.parametrize(
    "invalid",
    [
        {"pages_processed": 3},
        {"pages_unreadable": 1},
        {"chunks_processed": 7},
        {"claims_decided": 3, "claims_needs_review": 1},
        {"pages_total": True},
        {"claims_discovered": -1},
    ],
)
def test_inconsistent_or_non_integer_counters_are_rejected(invalid):
    with pytest.raises(ValueError):
        update_coverage(run_status="running", full_scope=True, **counters(**invalid))


def test_partial_page_failure_cannot_be_reported_as_completed():
    result = update_coverage(
        run_status="completed",
        full_scope=True,
        **counters(pages_processed=3, pages_unreadable=1),
    )

    assert result.status == "partial"
    assert result.coverage["pages_unreadable"] == 1
    assert result.coverage["complete"] is False


def test_concurrent_projection_is_stateless_and_deterministic():
    inputs = dict(run_status="completed", full_scope=True, **counters())
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: update_coverage(**inputs), range(32)))

    assert all(result == results[0] for result in results)
