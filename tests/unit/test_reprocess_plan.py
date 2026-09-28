"""R22 reprocess plan: durable baseline pins the allowance across crash/restart."""

from types import SimpleNamespace
from uuid import UUID

import pytest
from proofops.adapters.local.classification_reprocess import (
    CLAIM_REQUEST_BUDGET,
    ClassificationReprocessPlan,
    ReprocessRejected,
    issued_since_baseline,
)


def _plan(baseline, receipts_root, *, job=1):
    job_id = str(UUID(int=job))
    return ClassificationReprocessPlan(
        "tag_reprocess_plan_v1",
        str(UUID(int=100)),
        str(UUID(int=101)),
        str(UUID(int=200)),
        "a" * 64,
        job_id,
        "tag-reprocess-" + job_id,
        str(receipts_root),
        str(UUID(int=300)),
        str(UUID(int=400)),
        "b" * 64,
        tuple(baseline),
        CLAIM_REQUEST_BUDGET,
        "reviewer",
        123,
    )


def _store(rows):
    usage = SimpleNamespace(
        cost_data=lambda t, r: [{"reservation": {"call": {"request_id": rid}}} for rid in rows]
    )
    return SimpleNamespace(usage=usage)


def test_baseline_must_be_sorted_deduplicated_uuids(tmp_path):
    root = tmp_path / str(UUID(int=101))
    with pytest.raises(ReprocessRejected):
        _plan([str(UUID(int=2)), str(UUID(int=1))], root)  # unsorted
    with pytest.raises(ReprocessRejected):
        _plan([str(UUID(int=1)), str(UUID(int=1))], root)  # duplicate


def test_issued_since_baseline_counts_only_new_request_ids(tmp_path):
    root = tmp_path / str(UUID(int=101))
    (root / "elements").mkdir(parents=True)
    baseline = [str(UUID(int=1))]
    # One brand-new element receipt directory exists after authorization.
    new_id = str(UUID(int=2))
    (root / "elements" / new_id).mkdir()
    store = _store([str(UUID(int=1)), new_id])
    issued = issued_since_baseline(store, "t", "r", root, baseline)
    assert issued == [new_id]


def test_pinned_baseline_survives_recompute_would_be_zero(tmp_path):
    """The regression: recomputing the baseline on load would always yield zero.

    With the baseline pinned at authorization, a request issued after authorization is
    still counted even though it now also appears in current durable state -- so a
    restart cannot hand out a fresh allowance.
    """
    root = tmp_path / str(UUID(int=101))
    (root / "elements").mkdir(parents=True)
    pinned_baseline = [str(UUID(int=1))]
    issued_id = str(UUID(int=2))
    (root / "elements" / issued_id).mkdir()
    store = _store([str(UUID(int=1)), issued_id])
    # Pinned baseline -> counts the issued request.
    assert issued_since_baseline(store, "t", "r", root, pinned_baseline) == [issued_id]
    # A (buggy) recomputed baseline from current state would include issued_id and
    # therefore count zero -- which is exactly what pinning prevents.
    recomputed = sorted({str(UUID(int=1)), issued_id})
    assert issued_since_baseline(store, "t", "r", root, recomputed) == []
