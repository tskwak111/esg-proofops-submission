"""R22 end-to-end: manual classification of a PRELIMINARY_TAGS_UNRESOLVED claim then a
bounded reprocess that publishes an ordinary review through the existing path.

Uses the real local stores and LocalTagRunner with the explicit synthetic verified
corpus (no paid calls, no network). The reprocess supplies the reviewer's recorded
classification as the preliminary override; the element stage still runs its real
synthetic replicas and publishes through publish_transaction, so the claim reaches the
existing review queue and a rule-engine decision -- never a fabricated grade.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from proofops.adapters.local.classification_store import (
    ClassificationStoreError,
    LocalSQLiteClassificationStore,
)
from proofops.application.authorization import AuthContext
from proofops.application.tagging.manual_classification import ClassificationRejected

from tests.integration.test_local_tag_runner import TENANT, verified_setup


def _actor(role="reviewer"):
    caps = {"viewer"} | ({"reviewer"} if role == "reviewer" else set())
    return AuthContext("reviewer-1", TENANT, role, frozenset(caps), str(UUID(int=9)))


def _blocked_setup(tmp_path, monkeypatch):
    """A synthetic run whose single claim stops at PRELIMINARY_TAGS_UNRESOLVED."""
    service, run_id, runner, now, stream = verified_setup(tmp_path, monkeypatch)
    # Override the fixture supplier so preliminary returns unresolved (track=None),
    # exactly the R19 blocked state this feature targets.
    runner.preliminary = lambda claim, graph: None
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "blocked"
    envelope = runner.tags.load_snapshot(TENANT, run_id)
    (item,) = envelope["claims"]
    assert item["status"] == "blocked" and item["reason"] == "PRELIMINARY_TAGS_UNRESOLVED"
    claim_id = item["claim_id"]
    store = LocalSQLiteClassificationStore(
        service.store, service.uploads, runner.parser, runner.tags, runner.claims
    )
    return service, run_id, runner, now, store, claim_id


def _body(store, run_id, claim_id, actor):
    view = store.view(actor, run_id, claim_id)
    assert view["eligible"] is True and view["blocked_reason"] == "PRELIMINARY_TAGS_UNRESOLVED"
    assert view["etag"] is not None
    # Pick a literal quote present in the claim's own numbered source 0.
    quote = view["sources"][0]["quote"].split(" ")[0]
    return view, {
        "track": "performance",
        "safe_harbor_category": None,
        "dimensions": {
            "entity": {"source_index": 0, "quote": quote},
            "metric": None,
            "reporting_period": None,
        },
        "reason": "reviewer classified this as a reported performance result",
    }


def test_classify_then_reprocess_publishes_ordinary_review(tmp_path, monkeypatch):
    service, run_id, runner, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(store, run_id, claim_id, actor)

    result = store.record_and_enqueue(
        actor,
        run_id,
        claim_id,
        body,
        view["etag"],
        "idem-classification-000001",
        origin="human_classification",
        classified_by=actor.user_sub,
        delegation_authority=None,
        now=int(now[0]),
    )
    assert result["classification"]["track"] == "performance"
    assert result["classification"]["origin"] == "human_classification"
    assert result["reprocess_job"]["claim_ids"] == [claim_id]

    # No published review yet: only the bounded reprocess job is enqueued.
    assert runner.claims.current_tag(TENANT, run_id, claim_id) is None

    # The worker consumes the reprocess job: real synthetic element replicas run and
    # publish through the existing path, reaching the ordinary review queue.
    status = runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert status in ("needs_review", "completed")

    published = runner.claims.current_tag(TENANT, run_id, claim_id)
    assert published is not None
    tag = published["tag"]
    assert tag["tag_revision"] == 1  # first revision only; never a fabricated revision 2
    # A real tagging receipt set backed the publication (not the manual classification).
    inputs = runner.tags.load_inputs(TENANT, run_id, claim_id)
    assert inputs.tag_runs  # actual element replicas
    # The reviewed classification provenance is recorded on the checkpoint item.
    envelope = runner.tags.load_snapshot(TENANT, run_id)
    (item,) = envelope["claims"]
    assert item["reviewed_classification"]["origin"] == "human_classification"
    assert item["reviewed_classification"]["track"] == "performance"


def test_idempotent_replay_returns_same_job_even_after_ineligible(tmp_path, monkeypatch):
    service, run_id, runner, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(store, run_id, claim_id, actor)
    key = "idem-classification-abc123"
    first = store.record_and_enqueue(
        actor,
        run_id,
        claim_id,
        body,
        view["etag"],
        key,
        origin="human_classification",
        classified_by=actor.user_sub,
        delegation_authority=None,
        now=int(now[0]),
    )
    # Replay with identical bytes returns the same record + job (even though the run now
    # has an outstanding reprocess job that would otherwise make it ineligible).
    second = store.record_and_enqueue(
        actor,
        run_id,
        claim_id,
        body,
        view["etag"],
        key,
        origin="human_classification",
        classified_by=actor.user_sub,
        delegation_authority=None,
        now=int(now[0]),
    )
    assert first == second


def test_stale_lineage_if_match_is_rejected(tmp_path, monkeypatch):
    service, run_id, runner, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    _, body = _body(store, run_id, claim_id, actor)
    with pytest.raises(ClassificationStoreError) as exc:
        store.record_and_enqueue(
            actor,
            run_id,
            claim_id,
            body,
            '"' + "f" * 64 + '"',
            "idem-classification-stale1",
            origin="human_classification",
            classified_by=actor.user_sub,
            delegation_authority=None,
            now=int(now[0]),
        )
    assert exc.value.status == 412


def test_source_unverified_dimension_is_rejected(tmp_path, monkeypatch):
    service, run_id, runner, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(store, run_id, claim_id, actor)
    body["dimensions"]["entity"] = {"source_index": 0, "quote": "존재하지않는문구ZZZ"}
    with pytest.raises(ClassificationRejected) as exc:
        store.record_and_enqueue(
            actor,
            run_id,
            claim_id,
            body,
            view["etag"],
            "idem-classification-bad001",
            origin="human_classification",
            classified_by=actor.user_sub,
            delegation_authority=None,
            now=int(now[0]),
        )
    assert exc.value.code == "CLASSIFICATION_SOURCE_REJECTED"


def test_second_reprocess_blocked_after_publication(tmp_path, monkeypatch):
    service, run_id, runner, now, store, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(store, run_id, claim_id, actor)
    store.record_and_enqueue(
        actor,
        run_id,
        claim_id,
        body,
        view["etag"],
        "idem-classification-once01",
        origin="human_classification",
        classified_by=actor.user_sub,
        delegation_authority=None,
        now=int(now[0]),
    )
    runner.run_once(tenant_id=TENANT, run_id=run_id)
    assert runner.claims.current_tag(TENANT, run_id, claim_id) is not None
    # After publication the claim holds an immutable head: it is no longer eligible and
    # a fresh classification attempt is refused.
    after = store.view(actor, run_id, claim_id)
    assert after["eligible"] is False and after["ineligible_reason"] == "ALREADY_TAGGED"
    with pytest.raises(ClassificationStoreError) as exc:
        store.record_and_enqueue(
            actor,
            run_id,
            claim_id,
            body,
            '"' + "f" * 64 + '"',
            "idem-classification-twice1",
            origin="human_classification",
            classified_by=actor.user_sub,
            delegation_authority=None,
            now=int(now[0]),
        )
    assert exc.value.status in (409, 412)
