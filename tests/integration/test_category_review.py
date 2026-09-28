"""R24: bounded removal of a demonstrably wrong safe-harbor category.

Real store, real loader guards, real engine. No model call, no network. The
correction only removes a wrong non-null category; it never adds or replaces
one, never fabricates a tag-run header or packet value, and never creates a
``ConfirmedFact``.
"""

import copy
import json
from dataclasses import asdict
from uuid import uuid4

import pytest
from proofops.application.evidence.retrieval import freeze_track_packet, retrieve_evidence
from proofops.application.reviews import CATEGORY_POLICY_V1, ReviewRejected
from proofops.application.tagging.tracks import TrackCandidate
from proofops.domain.provenance import canonical_hash

from tests.acceptance import test_reviews, test_tagging
from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_retrieval import SyntheticSearch
from tests.acceptance.test_reviews import post, workspace
from tests.integration.test_ai_delegated_review import _actor
from tests.integration.test_safe_harbor_review import safe_record

OBSERVED = "emissions_estimate"
CHECKLIST = ("identified_as_estimate", "estimation_method", "uncertainty")


def category_workspace(tmp_path, monkeypatch, *, observed=OBSERVED):
    """Same synthetic claim as the review acceptance fixture, tagged with a category.

    Mirrors the real defect shape: the packet AND all three guarded tag-run
    headers agree on a category the sentence does not support.
    """
    original_setup = test_tagging.setup

    def setup(path):
        inputs = original_setup(path)
        claim = inputs["context"].claim
        packet = retrieve_evidence(
            claim,
            inputs["original"],
            SyntheticSearch(inputs["original"]),
            tenant_id=TENANT,
            run_id=RUN,
            index_generation="synthetic-v1",
            rulepack=inputs["rulepack"],
            document_context={},
            token_counter=lambda text: len(text) // 4,
        )
        track = TrackCandidate(claim, "performance", observed)
        inputs.update(
            track=track,
            packet=freeze_track_packet(packet, track=track, rulepack=inputs["rulepack"]),
        )
        inputs["invoke"].changes = {
            replica: {"root": {"safe_harbor_category": observed}} for replica in (1, 2, 3)
        }
        return inputs

    monkeypatch.setattr(test_reviews, "setup", setup)
    return workspace(tmp_path)


def category_review(ws, *, observed=OBSERVED, corrected=None):
    claim_refs = json.loads(json.dumps([asdict(r) for r in ws[2].context.claim.source_refs]))
    return {
        "policy": CATEGORY_POLICY_V1,
        "input_snapshot_sha256": canonical_hash(ws[2].snapshot()),
        "track": ws[4]["track"],
        "observed_category": observed,
        "corrected_category": corrected,
        "claim_source_refs": claim_refs,
        "basis_refs": copy.deepcopy(ws[2].packet.to_dict()["claim_source_refs"]),
        "source_authority": "원문 v2.0 §4.6 온실가스 배출량 추정치 범주 판정 대상",
        "reason": "문장은 실적 수치이며 추정 표시·추정 방법·불확실성 서술이 없다",
    }


def resolve(ws, review, *, key=None, if_match='"1"', reopen=False, body=None):
    return ws[1].resolve_ai_delegated_review(
        _actor(),
        ws[3]["review_id"],
        body or ws[4],
        if_match,
        key or str(uuid4()),
        delegated_reviewer="review-operator",
        delegation_authority="user authorization 2026-09-22 R24 category re-review",
        category_review=review,
        reopen=reopen,
    )


def history(ws):
    return ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])


def test_observed_category_blocks_grading_and_removal_releases_the_ladder(tmp_path, monkeypatch):
    ws = category_workspace(tmp_path, monkeypatch)
    before = history(ws)
    review = category_review(ws)

    result = resolve(ws, review)

    # 1. The correction is the only semantic difference and it grades by rules only.
    assert result["decision"]["review_status"] == "ai_delegated_confirmed"
    assert "GAP-001" not in result["decision"]["gap_ids"]
    after = history(ws)
    assert after["tags"][0] == before["tags"][0]
    tag = after["tags"][-1]
    assert tag["confirmed_tags"]["safe_harbor_category"] is None
    # 2. Observed model headers and packet pins keep the ORIGINAL value forever.
    assert tag["category_review"]["observed_tag_run_headers"] == [OBSERVED] * 3
    assert tag["category_review"]["observed_packet_safe_harbor_category"] == OBSERVED
    assert tag["category_review"]["corrected_category"] is None
    assert tag["category_review"]["request"] == review
    assert tag["category_review"]["superseded_checklist_items"] == list(CHECKLIST)
    assert {r.guarded.safe_harbor_category for r in ws[2].tag_runs if r.guarded} == {OBSERVED}
    assert ws[2].packet.to_dict()["safe_harbor_category"] == OBSERVED
    assert tag["confirmed_tags"]["packet_sha256"] == ws[2].packet.packet_sha256
    assert tag["confirmed_tags"]["prompt_sha256"] == ws[2].tag_runs[0].prompt_sha256
    assert tag["confirmed_tags"]["model_sha256"] == ws[2].tag_runs[0].model_sha256
    # 3. No fact is invented by the correction; the superseded checklist never appears.
    names = {f["name"] for f in tag["confirmed_tags"]["facts"]}
    assert names.isdisjoint(CHECKLIST)
    # 4. The safe-harbor reader stops showing the wrong GHG-estimate checklist.
    record = safe_record(ws)
    assert (record.applicable, record.category, record.checklist) == (False, None, ())


def test_unchanged_and_human_re_review_carry_the_correction_and_ai_ancestry(tmp_path, monkeypatch):
    ws = category_workspace(tmp_path, monkeypatch)
    review = category_review(ws)
    resolve(ws, review)
    baseline = history(ws)

    for revision in (2, 3):
        prior = history(ws)
        ws[1].resolve_review(
            _actor(),
            ws[3]["review_id"],
            ws[4] | {"base_tag_revision": revision},
            f'"{revision}"',
            str(uuid4()),
            reopen=True,
        )
        current = history(ws)
        assert current["tags"][: len(prior["tags"])] == prior["tags"]
        head = current["tags"][-1]
        assert head["origin"] == "human"
        # Carried, re-validated, and still honest about who first attested it.
        assert head["category_review"]["request"] == review
        assert head["category_review"]["carried_from"]["origin"] == "ai_delegated"
        assert head["confirmed_tags"]["safe_harbor_category"] is None
        assert head["category_review"]["observed_packet_safe_harbor_category"] == OBSERVED
        assert safe_record(ws).category is None
    assert history(ws)["tags"][: len(baseline["tags"])] == baseline["tags"]


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("unknown_policy", "CATEGORY_REVIEW_INVALID"),
        ("missing_key", "CATEGORY_REVIEW_INVALID"),
        ("forged_snapshot_hash", "CATEGORY_REVIEW_INVALID"),
        ("track_mismatch", "CATEGORY_REVIEW_INVALID"),
        ("short_authority", "CATEGORY_REVIEW_INVALID"),
        ("non_null_corrected", "CATEGORY_CORRECTION_UNSUPPORTED"),
        ("replacement_category", "CATEGORY_CORRECTION_UNSUPPORTED"),
        ("observed_mismatch", "CATEGORY_REVIEW_OBSERVED_MISMATCH"),
        ("partial_claim_refs", "WHOLE_CLAIM_REQUIRED"),
        ("forged_claim_ref", "WHOLE_CLAIM_REQUIRED"),
        ("forged_basis_ref", "CATEGORY_SOURCE_REJECTED"),
        ("basis_ref_outside_packet", "CATEGORY_SOURCE_REJECTED"),
        ("empty_basis", "CATEGORY_REVIEW_INVALID"),
    ],
)
def test_rejected_correction_never_writes(tmp_path, monkeypatch, mutation, code):
    ws = category_workspace(tmp_path, monkeypatch)
    review = category_review(ws)
    if mutation == "unknown_policy":
        review["policy"] = "claim_category_reclassification_v9"
    elif mutation == "missing_key":
        del review["basis_refs"]
    elif mutation == "forged_snapshot_hash":
        review["input_snapshot_sha256"] = "0" * 64
    elif mutation == "track_mismatch":
        review["track"] = "management"
    elif mutation == "short_authority":
        review["source_authority"] = "§4.6"
    elif mutation == "non_null_corrected":
        review["corrected_category"] = OBSERVED
    elif mutation == "replacement_category":
        review["corrected_category"] = "third_party_information"
    elif mutation == "observed_mismatch":
        review["observed_category"] = "forward_looking"
    elif mutation == "partial_claim_refs":
        review["claim_source_refs"] = []
    elif mutation == "forged_claim_ref":
        review["claim_source_refs"][0]["quote"] += "그리고 추정 방법을 제시했다"
    elif mutation == "forged_basis_ref":
        review["basis_refs"][0]["quote"] = "injected text"
    elif mutation == "basis_ref_outside_packet":
        review["basis_refs"][0]["char_end"] = review["basis_refs"][0]["char_start"] + 1
    else:
        review["basis_refs"] = []
    before = history(ws)

    with pytest.raises(ReviewRejected, match=code):
        resolve(ws, review)

    assert history(ws) == before


def test_supplied_safe_harbor_review_fails_closed(tmp_path, monkeypatch):
    ws = category_workspace(tmp_path, monkeypatch)
    before = history(ws)

    with pytest.raises(ReviewRejected, match="CATEGORY_REVIEW_CONFLICTS_SAFE_HARBOR") as failure:
        ws[1].resolve_ai_delegated_review(
            _actor(),
            ws[3]["review_id"],
            ws[4],
            '"1"',
            str(uuid4()),
            delegated_reviewer="review-operator",
            delegation_authority="user authorization 2026-09-22",
            safe_harbor_review={"policy": "unused"},
            category_review=category_review(ws),
        )

    assert failure.value.status == 409
    assert history(ws) == before


def test_carried_safe_harbor_review_fails_closed_without_silent_drop(tmp_path, monkeypatch):
    from tests.integration import test_safe_harbor_review as checklist

    ws = checklist.safe_harbor_workspace(tmp_path, monkeypatch)
    checklist.resolve(ws, checklist.checklist_review(ws, ("present", "present")))
    before = history(ws)
    assert before["tags"][-1]["safe_harbor_review"]["request"]["category"] == checklist.CATEGORY

    with pytest.raises(ReviewRejected, match="CATEGORY_REVIEW_CONFLICTS_SAFE_HARBOR"):
        resolve(
            ws,
            category_review(ws, observed=checklist.CATEGORY),
            if_match='"2"',
            reopen=True,
            body=ws[4] | {"base_tag_revision": 2},
        )

    # The carried checklist attestation is neither dropped nor superseded silently.
    after = history(ws)
    assert after == before
    assert safe_record(ws).category == checklist.CATEGORY


def test_stale_if_match_and_changed_receipt_under_one_key_are_refused(tmp_path, monkeypatch):
    ws = category_workspace(tmp_path, monkeypatch)
    review = category_review(ws)
    key = str(uuid4())

    result = resolve(ws, review, key=key)
    assert resolve(ws, review, key=key) == result

    changed = copy.deepcopy(review)
    changed["reason"] = review["reason"] + " (재진술)"
    with pytest.raises(ReviewRejected, match="IDEMPOTENCY_CONFLICT"):
        resolve(ws, changed, key=key)
    with pytest.raises(ReviewRejected, match="STALE_REVIEW_REVISION") as stale:
        resolve(ws, review)
    assert stale.value.status == 412


def test_human_surface_also_binds_the_receipt_to_retry_identity(tmp_path, monkeypatch):
    ws = category_workspace(tmp_path, monkeypatch)
    review = category_review(ws)
    key = str(uuid4())

    result = ws[1].resolve_review(
        _actor(), ws[3]["review_id"], ws[4], '"1"', key, category_review=review
    )
    assert result["decision"]["review_status"] == "human_confirmed"
    tag = history(ws)["tags"][-1]
    assert tag["origin"] == "human"
    assert tag["confirmed_tags"]["safe_harbor_category"] is None
    assert "carried_from" not in tag["category_review"]

    changed = copy.deepcopy(review)
    changed["reason"] = review["reason"] + " (재진술)"
    with pytest.raises(ReviewRejected, match="IDEMPOTENCY_CONFLICT"):
        ws[1].resolve_review(
            _actor(), ws[3]["review_id"], ws[4], '"1"', key, category_review=changed
        )


def test_ordinary_review_without_a_receipt_keeps_todays_behaviour(tmp_path, monkeypatch):
    ws = category_workspace(tmp_path, monkeypatch)

    response = post(ws)

    assert response.status_code == 200, response.text
    tag = history(ws)["tags"][-1]
    assert "category_review" not in tag
    assert tag["confirmed_tags"]["safe_harbor_category"] == OBSERVED
    assert "GAP-001" in response.json()["decision"]["gap_ids"]
    assert response.json()["decision"]["decision_status"] == "blocked_rule_gap"
    assert response.json()["decision"]["evidence_grade"] is None


def test_export_encoding_preserves_the_correction_receipt(tmp_path, monkeypatch):
    from proofops.application.exports import decode_revision_record, encode_revision_record

    ws = category_workspace(tmp_path, monkeypatch)
    resolve(ws, category_review(ws))
    stored = history(ws)
    tag, decision = stored["tags"][-1], stored["decisions"][-1]

    record = encode_revision_record(tag, decision, ws[2].snapshot())

    assert record["tag"]["category_review"] == tag["category_review"]
    assert decode_revision_record(record)["tag"] == tag


def test_cli_dry_run_validates_the_actual_request_before_apply(tmp_path, monkeypatch, capsys):
    from scripts.review_ai_delegated import main

    ws = category_workspace(tmp_path, monkeypatch)
    service, review = ws[1], ws[3]
    before = history(ws)
    monkeypatch.setattr("proofops.application.reviews.ReviewService", lambda *a, **k: service)
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps(ws[4]))
    valid = tmp_path / "category.json"
    valid.write_text(json.dumps(category_review(ws), ensure_ascii=False))
    forged = tmp_path / "category-forged.json"
    forged.write_text(json.dumps(category_review(ws) | {"observed_category": "forward_looking"}))
    argv = [
        "--state-db",
        str(tmp_path / "state.sqlite"),
        "--tenant-id",
        TENANT,
        "--review-id",
        review["review_id"],
        "--correction-json",
        str(correction),
        "--delegated-reviewer",
        "cli-test",
        "--if-match",
        '"1"',
        "--idempotency-key",
        "cli-category-removal-0001",
    ]

    # A request that the service would refuse is refused in the dry run too.
    assert main(argv + ["--category-review-json", str(forged)]) == 1
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"] == "CATEGORY_REVIEW_OBSERVED_MISMATCH"
    assert refused["stage"] == "category_review_validation"
    assert history(ws) == before

    # A valid request reports the pinned observation and still writes nothing.
    assert main(argv + ["--category-review-json", str(valid)]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["applied"] is False
    assert dry["category_review_validation"]["observed_category"] == OBSERVED
    assert dry["category_review_validation"]["corrected_category"] is None
    assert history(ws) == before

    # Mixing a checklist attestation into the same revision is refused up front.
    assert (
        main(argv + ["--category-review-json", str(valid), "--safe-harbor-review-json", str(valid)])
        == 1
    )
    assert json.loads(capsys.readouterr().out)["error"] == "CATEGORY_REVIEW_CONFLICTS_SAFE_HARBOR"
    assert history(ws) == before

    assert main(argv + ["--category-review-json", str(valid), "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True and applied["new_tag_revision"] == 2
    after = history(ws)
    assert after["tags"][0] == before["tags"][0]
    assert after["tags"][-1]["confirmed_tags"]["safe_harbor_category"] is None
    assert after["tags"][-1]["origin"] == "ai_delegated"


def test_cli_dry_run_refuses_a_carried_checklist_conflict_before_apply(
    tmp_path, monkeypatch, capsys
):
    from scripts.review_ai_delegated import main
    from tests.integration import test_safe_harbor_review as checklist

    ws = checklist.safe_harbor_workspace(tmp_path, monkeypatch)
    checklist.resolve(ws, checklist.checklist_review(ws, ("present", "present")))
    service, review = ws[1], ws[3]
    before = history(ws)
    monkeypatch.setattr("proofops.application.reviews.ReviewService", lambda *a, **k: service)
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps(ws[4] | {"base_tag_revision": 2}))
    category = tmp_path / "category.json"
    category.write_text(
        json.dumps(category_review(ws, observed=checklist.CATEGORY), ensure_ascii=False)
    )

    assert (
        main(
            [
                "--state-db",
                str(tmp_path / "state.sqlite"),
                "--tenant-id",
                TENANT,
                "--review-id",
                review["review_id"],
                "--correction-json",
                str(correction),
                "--delegated-reviewer",
                "cli-test",
                "--if-match",
                '"2"',
                "--idempotency-key",
                "cli-category-carried-conflict-1",
                "--category-review-json",
                str(category),
                "--re-review",
            ]
        )
        == 1
    )
    refused = json.loads(capsys.readouterr().out)
    assert refused["error"] == "CATEGORY_REVIEW_CONFLICTS_SAFE_HARBOR"
    assert refused["stage"] == "category_review_validation"
    assert history(ws) == before
