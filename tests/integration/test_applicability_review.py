"""R07c: explicit whole-claim applicability review, real guards/store/engine."""

import copy
import json
from dataclasses import asdict
from uuid import uuid4

import pytest
from proofops.application.reviews import ReviewRejected
from proofops.domain.provenance import canonical_hash

from tests.acceptance import test_tagging
from tests.acceptance.test_binding import DIMENSIONS, corpus, span, tags
from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_reviews import post, workspace
from tests.integration.test_ai_delegated_review import _actor


def management_workspace(tmp_path, monkeypatch):
    # Explicit synthetic management claim: means, boundary, verification and detail.
    monkeypatch.setattr(
        test_tagging,
        "corpus",
        lambda: corpus(
            claim_dimensions={
                "product": "사업장",
                "material": "운영관리",
                "metric": "함유비율 | 국내 전체 사업장은 ISO 14001을 운영하며 "
                "외부기관 검증을 받았고 "
                "매월 폐기물 점검을 시행한다",
            }
        ),
    )
    monkeypatch.setattr(
        test_tagging,
        "tags",
        lambda ref: tags(
            ref, {k: v for k, v in DIMENSIONS.items() if k not in {"product", "material"}}
        ),
    )
    ws = workspace(tmp_path)
    body = copy.deepcopy(ws[4])
    body["track"] = "management"
    ref = ws[2].context.claim.source_refs[0]
    quotes = [
        "ISO 14001",
        "국내 전체 사업장",
        "외부기관 검증을 받았고",
        "매월 폐기물 점검을 시행한다",
    ]
    body["elements"] = [
        dict(
            element_id=f"M{i}",
            state="present" if i <= 4 else "unknown",
            evidence_refs=[asdict(span(ref, quotes[i - 1]))] if i <= 4 else [],
            normalized_value=None,
            credited_from=None,
            reason_code=None,
        )
        for i in range(1, 7)
    ]
    return ws[:4] + (body,) + ws[5:]


def applicability(ws, value=False):
    return dict(
        policy="local_claim_applicability_v1",
        input_snapshot_sha256=canonical_hash(ws[2].snapshot()),
        track=ws[4]["track"],
        claim_source_refs=json.loads(
            json.dumps([asdict(r) for r in ws[2].context.claim.source_refs])
        ),
        source_authority="coordinator adoption: source v2 section 4.5, docs31 R07c",
        triggers=[
            dict(name=name, value=value, reason=reason)
            for name, reason in (
                (
                    "governance_claim",
                    "Whole claim describes operations and verification, no governance assertion.",
                ),
                (
                    "compensation_link_claim",
                    "Whole claim describes operations and verification, no remuneration assertion.",
                ),
            )
        ],
    )


def resolve(ws, review, *, key=None, if_match='"1"'):
    return ws[1].resolve_ai_delegated_review(
        _actor(),
        ws[3]["review_id"],
        ws[4],
        if_match,
        key or str(uuid4()),
        delegated_reviewer="review-operator",
        delegation_authority="user delegation 2026-09-20",
        applicability_review=review,
    )


def test_reviewed_false_excludes_and_engine_grades_immutable_management(tmp_path, monkeypatch):
    ws = management_workspace(tmp_path, monkeypatch)
    service = ws[1]
    before = service.store.history(TENANT, RUN, ws[3]["claim_id"])
    review = applicability(ws)
    key = str(uuid4())
    result = resolve(ws, review, key=key)
    assert result["decision"]["evidence_grade"] == "E3"
    assert result["decision"]["review_status"] == "ai_delegated_confirmed"
    after = service.store.history(TENANT, RUN, ws[3]["claim_id"])
    assert after["tags"][0] == before["tags"][0]
    assert set(after["decisions"][-1]["decision"]["excluded_elements"]) == {"M5", "M6"}
    tag = after["tags"][-1]
    assert tag["applicability_review"]["request"] == review
    assert tag["applicability_review"]["identity"]["claim_id"] == ws[3]["claim_id"]
    assert tag["review_origin"] == "ai_project_interpretation"
    facts = {f["name"]: f for f in tag["confirmed_tags"]["facts"]}
    assert facts["governance_claim"]["state"] == "absent"
    assert facts["governance_claim"]["source_scope"] == "local_claim"
    assert facts["responsible_organization"]["state"] == "unknown"
    assert "reduction_or_improvement_claim" not in facts
    assert resolve(ws, review, key=key) == result
    changed = copy.deepcopy(review)
    changed["triggers"][0]["value"] = True
    with pytest.raises(ReviewRejected, match="IDEMPOTENCY_CONFLICT"):
        resolve(ws, changed, key=key)
    with pytest.raises(ReviewRejected) as stale:
        resolve(ws, review)
    assert stale.value.status == 412

    # Chained human re-reviews retain the original AI applicability attestation.
    for revision in (2, 3):
        prior = service.store.history(TENANT, RUN, ws[3]["claim_id"])
        repeated = service.resolve_review(
            _actor(),
            ws[3]["review_id"],
            ws[4] | {"base_tag_revision": revision},
            f'"{revision}"',
            str(uuid4()),
            reopen=True,
        )
        current = service.store.history(TENANT, RUN, ws[3]["claim_id"])
        assert current["tags"][:-1] == prior["tags"]
        assert current["decisions"][:-1] == prior["decisions"]
        assert repeated["decision"]["evidence_grade"] == "E3"
        assert set(current["decisions"][-1]["decision"]["excluded_elements"]) == {"M5", "M6"}
        carried = current["tags"][-1]
        assert carried["origin"] == "human"
        assert carried["applicability_review"]["request"] == review
        assert carried["applicability_review"]["carried_from"]["origin"] == "ai_delegated"
        assert carried["applicability_review"]["carried_from"]["delegated_reviewer"] == (
            "review-operator"
        )


def assert_conditional_held(history, elements):
    """GAP-003 (가): the ladder grade stands, but unminted triggers stay unresolved."""
    decision = history["decisions"][-1]["decision"]
    assert not set(elements) & set(decision["excluded_elements"])
    assert set(elements) <= set(decision["unresolved_elements"])
    assert decision["review_status"] == "needs_review"


@pytest.mark.parametrize("value", [None, True])
def test_unknown_or_true_trigger_with_unknown_element_remains_held(tmp_path, monkeypatch, value):
    ws = management_workspace(tmp_path, monkeypatch)
    result = resolve(ws, applicability(ws, value))
    assert result["decision"]["evidence_grade"] == "E3"
    history = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    assert history["decisions"][-1]["decision"]["excluded_elements"] == []
    assert_conditional_held(history, {"M5", "M6"})


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_claim",
        "cut_quote",
        "forged_quote",
        "hash",
        "name",
        "track",
        "bool",
        "duplicate",
        "absent_element",
        "source_unverified",
    ],
)
def test_bad_attestation_never_writes(tmp_path, monkeypatch, mutation):
    ws = management_workspace(tmp_path, monkeypatch)
    review = applicability(ws)
    if mutation == "foreign_claim":
        review["claim_source_refs"] = [
            asdict(
                next(
                    b
                    for b in ws[2].original.blocks
                    if b.source_id != ws[2].context.claim.source_refs[0].source_id
                ).source_ref()
            )
        ]
    elif mutation == "cut_quote":
        review["claim_source_refs"] = [
            asdict(span(ws[2].context.claim.source_refs[0], "ISO 14001"))
        ]
    elif mutation == "forged_quote":
        review["claim_source_refs"][0]["quote"] = "unrelated claim"
    elif mutation == "hash":
        review["input_snapshot_sha256"] = "0" * 64
    elif mutation == "name":
        review["triggers"][0]["name"] = "arbitrary_absence"
    elif mutation == "track":
        review["track"] = "goal"
    elif mutation == "bool":
        review["triggers"][0]["value"] = 0
    elif mutation == "duplicate":
        review["triggers"].append(review["triggers"][0])
    elif mutation == "absent_element":
        ws[4]["elements"][0].update(state="absent", evidence_refs=[])
    else:
        # Claim quality remains 'verified'; full source replay must still reject.
        from dataclasses import replace

        source_id = ws[2].context.claim.source_refs[0].source_id
        graph = replace(
            ws[2].original,
            blocks=tuple(
                replace(b, quality="unverified") if b.source_id == source_id else b
                for b in ws[2].original.blocks
            ),
        )
        monkeypatch.setattr(
            "proofops.application.reviews.verify_source_ref",
            lambda ref, original, **kw: __import__(
                "proofops.application.evidence.span_citations", fromlist=["verify_source_ref"]
            ).verify_source_ref(ref, graph, **kw),
        )
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    with pytest.raises(ReviewRejected):
        resolve(ws, review)
    assert ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]) == before


def test_http_cannot_mint_applicability(tmp_path, monkeypatch):
    ws = management_workspace(tmp_path, monkeypatch)
    assert post(ws, body=ws[4] | {"applicability_review": applicability(ws)}).status_code == 422
    result = post(ws)
    assert result.status_code == 200
    assert result.json()["decision"]["evidence_grade"] == "E3"
    assert_conditional_held(ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]), {"M5", "M6"})


def test_reviewed_goal_and_omitted_trigger_stays_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(
        test_tagging,
        "corpus",
        lambda: corpus(
            claim_dimensions={
                "product": "사업장",
                "material": "운영관리",
                "metric": "함유비율 | 국내 전체 사업장은 2030년까지 배출량 40% 감축을 목표로 하며 "
                "기준은 2020년 100톤, 현재 진척 10%, 설비 교체로 이행한다",
            }
        ),
    )
    monkeypatch.setattr(
        test_tagging,
        "tags",
        lambda ref: tags(
            ref, {k: v for k, v in DIMENSIONS.items() if k not in {"product", "material"}}
        ),
    )
    ws = workspace(tmp_path)
    ws[4]["track"] = "goal"
    ref = ws[2].context.claim.source_refs[0]
    quotes = ["2030년", "배출량 40%", "2020년 100톤", "국내 전체 사업장", "진척 10%", "설비 교체"]
    ws[4]["elements"] = [
        dict(
            element_id=f"G{i}",
            state="present" if i <= 6 else "unknown",
            evidence_refs=[asdict(span(ref, quotes[i - 1]))] if i <= 6 else [],
            normalized_value=quotes[i - 1] if i <= 6 else None,
            credited_from=None,
            reason_code=None,
        )
        for i in range(1, 9)
    ]
    review = applicability(ws)
    review["triggers"] = [
        dict(
            name=name,
            value=False,
            reason="Whole atomic claim asserts reduction and equipment replacement only.",
        )
        for name in ("offset_or_carbon_neutral_claim", "science_based_claim")
    ]
    result = resolve(ws, review)
    assert result["decision"]["evidence_grade"] == "E3"
    history = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    assert set(history["decisions"][-1]["decision"]["excluded_elements"]) == {"G7", "G8"}

    # A separate review has no default false for omitted names.
    (tmp_path / "omitted").mkdir()
    other = workspace(tmp_path / "omitted")
    other[4].update(ws[4])
    partial = applicability(other)
    partial["triggers"] = review["triggers"][:1]
    assert resolve(other, partial)["decision"]["evidence_grade"] == "E3"
    assert_conditional_held(other[1].store.history(TENANT, RUN, other[3]["claim_id"]), {"G8"})


def test_track_change_drops_preexisting_incompatible_triggers(tmp_path, monkeypatch):
    from dataclasses import replace

    from proofops.application import reviews
    from proofops.domain.rules.engine import ConfirmedFact

    from tests.acceptance import test_reviews

    # Simulate a trusted prior producer with applicability facts: today's
    # consensus does not emit these. Keep its normal receipt/pin validation.
    def seed(result):
        if result.confirmed_tags is None:
            return result
        return replace(
            result,
            confirmed_tags=replace(
                result.confirmed_tags,
                facts=result.confirmed_tags.facts
                + tuple(
                    ConfirmedFact(
                        name,
                        "absent",
                        search_coverage_verified=True,
                    )
                    for name in (
                        "governance_claim",
                        "compensation_link_claim",
                        "reduction_or_improvement_claim",
                    )
                ),
            ),
        )

    real_consensus, real_form = test_reviews.consensus, reviews.form_consensus
    monkeypatch.setattr(test_reviews, "consensus", lambda *a, **kw: seed(real_consensus(*a, **kw)))
    monkeypatch.setattr(reviews, "form_consensus", lambda *a, **kw: seed(real_form(*a, **kw)))
    ws = management_workspace(tmp_path, monkeypatch)
    assert resolve(ws, None)["decision"]["evidence_grade"] == "E3"
    assert_conditional_held(ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]), {"M5", "M6"})
    tag = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])["tags"][-1]
    assert not any(f["name"].endswith("_claim") for f in tag["confirmed_tags"]["facts"])


@pytest.mark.parametrize("value,state", [(False, "absent"), (True, "present"), (None, "unknown")])
def test_management_review_records_willingness_only_from_the_whole_claim(
    tmp_path, monkeypatch, value, state
):
    # The §4.4 management E0 branch ("의지 표현만") is a claim-local judgment; the
    # review path is the only producer and it never infers report-wide absence.
    ws = management_workspace(tmp_path, monkeypatch)
    review = applicability(ws)
    review["triggers"].append(
        dict(
            name="willingness_only",
            value=value,
            reason="Whole claim reviewed for intent-only wording.",
        )
    )
    resolve(ws, review)
    tag = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])["tags"][-1]
    fact = {f["name"]: f for f in tag["confirmed_tags"]["facts"]}["willingness_only"]
    assert fact["state"] == state and fact["source_scope"] == "local_claim"
    assert fact["search_coverage_verified"] is (value is False)


def test_willingness_only_is_rejected_where_the_track_ladder_does_not_use_it(tmp_path, monkeypatch):
    from proofops.application.reviews import _review_applicability

    ws = management_workspace(tmp_path, monkeypatch)
    review = applicability(ws)
    willingness = dict(name="willingness_only", value=False, reason="Whole claim reviewed.")
    facts, _ = _review_applicability(ws[2], "management", review | {"triggers": [willingness]})
    assert [f.name for f in facts] == ["willingness_only"]
    goal = review | {"track": "goal", "triggers": [willingness]}
    with pytest.raises(ReviewRejected, match="APPLICABILITY_REVIEW_INVALID"):
        _review_applicability(ws[2], "goal", goal)
