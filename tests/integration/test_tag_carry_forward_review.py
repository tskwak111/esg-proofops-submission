"""A checkpoint cannot promote an unreviewed partial decision during carry-forward."""

from types import SimpleNamespace

import pytest
from proofops.application.tagging.consensus import reviewable_checkpoint
from proofops_worker.tag_recovery import TagRecovery
from proofops_worker.tag_reprocess import TagReprocess


@pytest.mark.parametrize("carrier", [TagRecovery.carry_forward, TagReprocess.carry_forward])
def test_unreviewed_partial_decision_stays_reviewable(carrier):
    item = {
        "claim_id": "claim",
        "status": "completed",
        "decision": {"decision_status": "decided", "evidence_grade": "E3"},
        "review_inputs": {
            "fact_assembly": {"profile": "partial-facts-v1"},
            "consensus": {"review_status": "needs_review"},
        },
    }
    carried = carrier(SimpleNamespace(_items={"claim": item}, _profile="strict-v1"), "claim")
    assert carried["status"] == "needs_review"
    assert carried["decision"] is None
    assert carried["candidate_grade"] == "E3"
    assert item["status"] == "completed"  # immutable lineage stays untouched


@pytest.mark.parametrize("carrier", [TagRecovery.carry_forward, TagReprocess.carry_forward])
def test_reviewed_decision_can_stay_completed(carrier):
    item = {
        "claim_id": "claim",
        "status": "completed",
        "decision": {"decision_status": "decided", "evidence_grade": "E3"},
        "review_inputs": {
            "fact_assembly": {"profile": "partial-facts-v1"},
            "consensus": {"review_status": "human_confirmed"},
            "review_origin": "human",
        },
    }
    carrier_state = SimpleNamespace(_items={"claim": item}, _profile="strict-v1")
    assert carrier(carrier_state, "claim")["status"] == "completed"


def test_run_pin_catches_missing_checkpoint_profile():
    item = {
        "status": "completed",
        "decision": {"decision_status": "decided", "evidence_grade": "E3"},
        "review_inputs": {"consensus": {"review_status": "auto_confirmed"}},
    }
    carried = reviewable_checkpoint(item, pinned_profile="partial-facts-v1")
    assert carried["status"] == "needs_review"


@pytest.mark.parametrize("carrier", [TagRecovery.carry_forward, TagReprocess.carry_forward])
def test_carrier_uses_its_pinned_profile(carrier):
    item = {
        "claim_id": "claim",
        "status": "completed",
        "decision": {"decision_status": "decided", "evidence_grade": "E3"},
        "review_inputs": {"consensus": {"review_status": "auto_confirmed"}},
    }
    carrier_state = SimpleNamespace(_items={"claim": item}, _profile="partial-facts-v1")
    assert carrier(carrier_state, "claim")["status"] == "needs_review"


@pytest.mark.parametrize("carrier", [TagRecovery.carry_forward, TagReprocess.carry_forward])
def test_partial_completed_without_decision_still_needs_review(carrier):
    item = {
        "claim_id": "claim",
        "status": "completed",
        "decision": None,
        "review_inputs": {
            "fact_assembly": {"profile": "partial-facts-v1"},
            "consensus": {"review_status": "needs_review"},
        },
    }
    carried = carrier(SimpleNamespace(_items={"claim": item}, _profile="strict-v1"), "claim")
    assert carried["status"] == "needs_review"
