"""AT-009: track and safe-harbor category stay independent of claim topics.

All inputs are explicitly synthetic, local, human-authored tagging candidates.
No model call, grade, or production evidence is represented by these fixtures.
"""

from dataclasses import FrozenInstanceError, replace
from uuid import UUID, uuid5

import pytest
from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
from proofops.domain.values import SourceRef

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "22222222-2222-4222-8222-222222222222"
MANIFEST = "33333333-3333-4333-8333-333333333333"
PROFILE = ExtractionProfile("a" * 64, "b" * 64, "c" * 64, synthetic=True)


def claim(name: str, quote: str, *topic_ids: str, tenant_id: str = TENANT) -> Claim:
    claim_id = str(uuid5(UUID(MANIFEST), f"claim:{name}"))
    source_id = str(uuid5(UUID(MANIFEST), f"source:{name}"))
    ref = SourceRef(
        source_id,
        VERSION,
        MANIFEST,
        1,
        None,
        (10, 10, 590, 40),
        "d" * 64,
        quote,
        0,
        len(quote),
        "located",
        "candidate",
    )
    receipt = ExtractionReceipt(source_id, "e" * 64, "f" * 64, "{}", PROFILE, "processed")
    return Claim(
        claim_id,
        tenant_id,
        VERSION,
        MANIFEST,
        "0" * 64,
        quote,
        (ref,),
        "unverified",
        tuple(topic_ids),
        receipt,
    )


def test_supply_chain_future_target_remains_goal_not_management():
    from proofops.application.tagging.tracks import validate_track_candidates

    target = claim("target", "2030년까지 공급망 배출량을 30% 감축하겠습니다.", "supply_chain")
    process = claim("process", "공급망 환경평가 절차를 운영하고 있습니다.", "supply_chain")
    result = validate_track_candidates(
        (target, process),
        (
            {"claim_id": target.claim_id, "track": "goal", "safe_harbor_category": None},
            {"claim_id": process.claim_id, "track": "management", "safe_harbor_category": None},
        ),
    )

    assert [item.track for item in result] == ["goal", "management"]
    assert result[0].claim.topic_ids == result[1].claim.topic_ids == ("supply_chain",)
    assert result[0].claim.quote.startswith("2030년")


def test_track_and_safe_harbor_category_are_independent():
    from proofops.application.tagging.tracks import validate_track_candidates

    plain = claim("plain", "2030년까지 용수 사용량을 줄이겠습니다.", "water")
    sourced = claim("sourced", "2030년까지 협력사 자료를 활용하겠습니다.", "supply_chain")
    result = validate_track_candidates(
        (plain, sourced),
        (
            {"claim_id": plain.claim_id, "track": "goal", "safe_harbor_category": None},
            {
                "claim_id": sourced.claim_id,
                "track": "goal",
                "safe_harbor_category": "third_party_information",
            },
        ),
    )

    assert [(item.track, item.safe_harbor_category) for item in result] == [
        ("goal", None),
        ("goal", "third_party_information"),
    ]
    with pytest.raises(FrozenInstanceError):
        result[0].track = "management"


@pytest.mark.parametrize("mutation", ["foreign_id", "unknown_track", "unknown_category", "grade"])
def test_structured_boundary_rejects_foreign_ids_unknown_values_and_grades(mutation):
    from proofops.application.tagging.tracks import validate_track_candidates

    item = claim("strict", "2030년까지 배출량을 줄이겠습니다.", "climate")
    candidate = {
        "claim_id": item.claim_id,
        "track": "goal",
        "safe_harbor_category": None,
    }
    if mutation == "foreign_id":
        candidate["claim_id"] = str(uuid5(UUID(MANIFEST), "foreign-claim"))
    elif mutation == "unknown_track":
        candidate["track"] = "strategy"
    elif mutation == "unknown_category":
        candidate["safe_harbor_category"] = "future_goal"
    else:
        candidate["evidence_grade"] = "E3"
    with pytest.raises(ValueError):
        validate_track_candidates((item,), (candidate,))


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "mixed_tenant", "source_less"])
def test_batch_is_complete_unique_tenant_scoped_and_source_backed(mutation):
    from proofops.application.tagging.tracks import validate_track_candidates

    first = claim("first", "2030년까지 배출량을 줄이겠습니다.", "climate")
    second = claim("second", "환경경영시스템을 운영합니다.", "management")
    candidates = (
        {"claim_id": first.claim_id, "track": "goal", "safe_harbor_category": None},
        {"claim_id": second.claim_id, "track": "management", "safe_harbor_category": None},
    )
    if mutation == "missing":
        candidates = candidates[:1]
    elif mutation == "duplicate":
        candidates = (candidates[0], candidates[0])
    elif mutation == "mixed_tenant":
        second = replace(second, tenant_id="99999999-9999-4999-8999-999999999999")
    else:
        first = replace(first, source_refs=())

    with pytest.raises(ValueError):
        validate_track_candidates((first, second), candidates)
