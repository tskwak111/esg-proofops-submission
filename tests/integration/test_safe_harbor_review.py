"""R07d: explicit checklist facts use the immutable delegated-review path."""

import copy
from uuid import uuid4

import pytest
from proofops.application.evidence.retrieval import freeze_track_packet, retrieve_evidence
from proofops.application.reviews import ReviewRejected
from proofops.application.tagging.tracks import TrackCandidate
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import snapshot_from_validated
from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags
from proofops.domain.rules.safe_harbor import CHECKLIST_POLICY_V1, record_safe_harbor
from proofops.domain.values import _source_ref_from_dict

from tests.acceptance import test_reviews, test_tagging
from tests.acceptance.test_checklist_policy import derived
from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_retrieval import SyntheticSearch
from tests.acceptance.test_reviews import post, workspace
from tests.integration.test_ai_delegated_review import _actor

CATEGORY = "forward_looking"
ITEMS = ("assumptions", "scenario_or_premises")


def safe_harbor_workspace(tmp_path, monkeypatch):
    original_setup = test_tagging.setup

    def setup(path):
        inputs = original_setup(path)
        _, manifest, files = derived()
        rulepack = snapshot_from_validated(manifest.to_dict(), files)
        claim = inputs["context"].claim
        packet = retrieve_evidence(
            claim,
            inputs["original"],
            SyntheticSearch(inputs["original"]),
            tenant_id=TENANT,
            run_id=RUN,
            index_generation="synthetic-v1",
            rulepack=rulepack,
            document_context={},
            token_counter=lambda text: len(text) // 4,
        )
        track = TrackCandidate(claim, "performance", CATEGORY)
        inputs.update(
            rulepack=rulepack,
            track=track,
            packet=freeze_track_packet(packet, track=track, rulepack=rulepack),
        )
        inputs["invoke"].changes = {
            replica: {"root": {"safe_harbor_category": CATEGORY}} for replica in (1, 2, 3)
        }
        return inputs

    monkeypatch.setattr(test_reviews, "setup", setup)
    return workspace(tmp_path)


def checklist_review(ws, states):
    ref = ws[2].packet.to_dict()["claim_source_refs"][0]
    return {
        "policy": CHECKLIST_POLICY_V1,
        "input_snapshot_sha256": canonical_hash(ws[2].snapshot()),
        "category": CATEGORY,
        "source_authority": "coordinator review of the pinned evidence packet",
        "facts": [
            {
                "name": name,
                "state": state,
                "evidence_refs": [copy.deepcopy(ref)] if state != "unknown" else [],
                "search_coverage_verified": state == "absent",
                "reason": f"Explicit packet review for {name}.",
            }
            for name, state in zip(ITEMS, states, strict=True)
        ],
    }


def resolve(ws, review):
    return ws[1].resolve_ai_delegated_review(
        _actor(),
        ws[3]["review_id"],
        ws[4],
        '"1"',
        str(uuid4()),
        delegated_reviewer="review-operator",
        delegation_authority="user delegation 2026-09-20",
        safe_harbor_review=review,
    )


def safe_record(ws):
    raw = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])["tags"][-1]["confirmed_tags"]
    tags = ConfirmedTags(
        **(
            raw
            | {
                "facts": tuple(
                    ConfirmedFact(
                        **(
                            fact
                            | {
                                "evidence_refs": tuple(
                                    _source_ref_from_dict(ref) for ref in fact["evidence_refs"]
                                )
                            }
                        )
                    )
                    for fact in raw["facts"]
                )
            }
        )
    )
    return record_safe_harbor(tags, ws[2].rule_context, ws[2].rulepack)


@pytest.mark.parametrize(
    "states,expected",
    [
        (("present", "present"), True),
        (("absent", "present"), False),
        (("unknown", "present"), None),
    ],
)
def test_checklist_review_records_true_false_or_unknown(tmp_path, monkeypatch, states, expected):
    ws = safe_harbor_workspace(tmp_path, monkeypatch)
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    review = checklist_review(ws, states)

    result = resolve(ws, review)

    assert result["decision"]["review_status"] == "ai_delegated_confirmed"
    after = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    assert after["tags"][0] == before["tags"][0]
    assert after["tags"][-1]["safe_harbor_review"]["request"] == review
    assert safe_record(ws).reasonable_basis_documented is expected

    for revision in (2, 3):
        ws[1].resolve_review(
            _actor(),
            ws[3]["review_id"],
            ws[4] | {"base_tag_revision": revision},
            f'"{revision}"',
            str(uuid4()),
            reopen=True,
        )
        current = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])["tags"][-1]
        assert current["origin"] == "human"
        assert current["safe_harbor_review"]["request"] == review
        assert current["safe_harbor_review"]["carried_from"]["origin"] == "ai_delegated"
        assert safe_record(ws).reasonable_basis_documented is expected


@pytest.mark.parametrize("mutation", ["injected_ref", "unattested_absence"])
def test_unverified_or_unattested_checklist_fact_never_writes(tmp_path, monkeypatch, mutation):
    ws = safe_harbor_workspace(tmp_path, monkeypatch)
    review = checklist_review(ws, ("unknown", "present"))
    if mutation == "injected_ref":
        review["facts"][0]["evidence_refs"] = copy.deepcopy(review["facts"][1]["evidence_refs"])
        review["facts"][0]["evidence_refs"][0]["quote"] = "injected text"
    else:
        review["facts"][0].update(
            state="absent",
            evidence_refs=copy.deepcopy(review["facts"][1]["evidence_refs"]),
            search_coverage_verified=False,
        )
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])

    with pytest.raises(ReviewRejected):
        resolve(ws, review)

    assert ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]) == before


def test_legacy_pack_does_not_auto_enable_checklist_input(tmp_path):
    ws = workspace(tmp_path)
    ref = ws[2].packet.to_dict()["claim_source_refs"][0]
    review = {
        "policy": CHECKLIST_POLICY_V1,
        "input_snapshot_sha256": canonical_hash(ws[2].snapshot()),
        "category": CATEGORY,
        "source_authority": "coordinator review of the pinned evidence packet",
        "facts": [
            {
                "name": name,
                "state": "present",
                "evidence_refs": [copy.deepcopy(ref)],
                "search_coverage_verified": False,
                "reason": f"Explicit packet review for {name}.",
            }
            for name in ITEMS
        ],
    }
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    with pytest.raises(ReviewRejected, match="SAFE_HARBOR_REVIEW_INVALID"):
        resolve(ws, review)
    assert ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]) == before


def test_existing_human_review_path_stays_compatible(tmp_path, monkeypatch):
    ws = safe_harbor_workspace(tmp_path, monkeypatch)
    response = post(ws)
    assert response.status_code == 200
    assert response.json()["decision"]["review_status"] == "human_confirmed"
    assert safe_record(ws).reasonable_basis_documented is None
