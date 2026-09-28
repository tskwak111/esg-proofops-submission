"""R09: AI-delegated review records honest provenance with identical guards.

One integration test over the real service + local SQLite store + real
engine (synthetic fixtures, no paid calls):

1. honest label: ``resolve_ai_delegated_review`` writes
   ``origin=ai_delegated`` / ``review_status=ai_delegated_confirmed`` with
   an ``ai-delegated-review:`` reviewer, keeps the engine grade from Python
   only, and leaves the prior immutable consensus row untouched; the HTTP
   body still cannot self-assert provenance (extra ``origin`` key -> 422)
   and the default HTTP/human route still records ``human``.
2. source rejection: a forged quote is rejected with no new tag row.
3. conflict path: a second resolution at the same revision loses with the
   same STALE/CONFLICT codes as the human route.
"""

import json
from uuid import uuid4

from proofops.application.authorization import AuthContext
from proofops.application.reviews import ReviewRejected

from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_reviews import post, workspace


def _actor(index=0):
    return AuthContext(
        f"operator-{index}", TENANT, "reviewer", frozenset({"viewer", "reviewer"}), str(index)
    )


def test_cli_re_review_is_explicit_and_dry_by_default(tmp_path, monkeypatch, capsys):
    from scripts.review_ai_delegated import main

    ws = workspace(tmp_path)
    service, review = ws[1], ws[3]
    assert post(ws).status_code == 200
    before = service.store.history(TENANT, RUN, review["claim_id"])
    # Only replace input composition: retain real service, source guards, engine and SQLite.
    monkeypatch.setattr("proofops.application.reviews.ReviewService", lambda *a, **k: service)
    correction = tmp_path / "correction.json"
    correction.write_text(json.dumps(ws[4] | {"base_tag_revision": 2}))
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
        '"2"',
        "--idempotency-key",
        "cli-explicit-rereview-0001",
    ]
    assert main(argv + ["--re-review"]) == 0
    assert json.loads(capsys.readouterr().out)["re_review"] is True
    assert service.store.history(TENANT, RUN, review["claim_id"]) == before
    assert main(argv + ["--apply"]) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "STALE_REVIEW_REVISION"
    assert main(argv + ["--apply", "--re-review"]) == 0
    assert json.loads(capsys.readouterr().out)["new_tag_revision"] == 3
    after = service.store.history(TENANT, RUN, review["claim_id"])
    assert after["tags"][:2] == before["tags"]
    assert after["tags"][-1]["origin"] == "ai_delegated"


def test_ai_delegated_review_honest_label_source_rejection_and_conflict(tmp_path):
    ws = workspace(tmp_path)
    service, review, body = ws[1], ws[3], ws[4]

    # --- HTTP body cannot self-assert trusted provenance. ---
    assert post(ws, body=body | {"origin": "ai_delegated"}).status_code == 422

    # --- 1. honest AI-delegated label, same guards as human. ---
    before = service.store.history(TENANT, RUN, review["claim_id"])
    result = service.resolve_ai_delegated_review(
        _actor(),
        review["review_id"],
        json.loads(json.dumps(body)),
        '"1"',
        f"ai-delegated-key-{uuid4().hex[:8]}",
        delegated_reviewer="review-operator",
        delegation_authority="user delegation 2026-09-20",
    )
    assert result["decision"]["review_status"] == "ai_delegated_confirmed"
    assert result["decision"]["review_status"] != "human_confirmed"
    assert result["review"]["status"] == "resolved"
    after = service.store.history(TENANT, RUN, review["claim_id"])
    assert after["tags"][0] == before["tags"][0]  # prior consensus row immutable
    assert len(after["tags"]) == 2
    ai_tag = after["tags"][1]
    assert ai_tag["origin"] == "ai_delegated"
    assert ai_tag["reviewer_sub"] == "ai-delegated-review:review-operator"
    assert ai_tag["review_reason"] == body["reason"]
    assert ai_tag.get("review_origin") == "ai_project_interpretation"
    assert ai_tag.get("delegation_authority") == "user delegation 2026-09-20"
    import jsonschema

    schema = json.load(open("contracts/jsonschema/api_models.schema.json"))
    jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/ReviewResolution", "$defs": schema["$defs"]}
    ).validate(
        {
            "review": result["review"],
            "decision": result["decision"],
            "new_tag_revision": result["new_tag_revision"],
        }
    )

    # --- 2. source rejection writes nothing (fresh review workspace). ---
    forged_dir = tmp_path / "forged"
    forged_dir.mkdir(exist_ok=True)
    ws2 = workspace(forged_dir)
    service2, review2, body2 = ws2[1], ws2[3], ws2[4]
    forged = json.loads(json.dumps(body2))
    forged["elements"][0]["evidence_refs"][0]["quote"] = "999%"
    before2 = service2.store.history(TENANT, RUN, review2["claim_id"])
    try:
        service2.resolve_ai_delegated_review(
            _actor(1),
            review2["review_id"],
            forged,
            '"1"',
            f"ai-delegated-key-{uuid4().hex[:8]}",
            delegated_reviewer="review-operator",
            delegation_authority="user delegation 2026-09-20",
        )
        raise AssertionError("forged source must be rejected")
    except ReviewRejected as error:
        assert error.code == "SOURCE_REJECTED"
    assert service2.store.history(TENANT, RUN, review2["claim_id"]) == before2

    # --- 3. same revision conflict path as the human route. ---
    try:
        service.resolve_ai_delegated_review(
            _actor(2),
            review["review_id"],
            json.loads(json.dumps(body)),
            '"1"',
            f"ai-delegated-key-{uuid4().hex[:8]}",
            delegated_reviewer="review-operator",
            delegation_authority="user delegation 2026-09-20",
        )
        raise AssertionError("stale revision must conflict")
    except ReviewRejected as error:
        assert error.code in ("STALE_REVIEW_REVISION", "REVIEW_CONFLICT")
        assert error.status in (409, 412)
    assert len(service.store.history(TENANT, RUN, review["claim_id"])["tags"]) == 2

    # --- default human route unchanged: fresh workspace resolves as human. ---
    human_dir = tmp_path / "human"
    human_dir.mkdir(exist_ok=True)
    ws3 = workspace(human_dir)
    human = post(ws3)
    assert human.status_code == 200, human.text
    assert human.json()["decision"]["review_status"] == "human_confirmed"
