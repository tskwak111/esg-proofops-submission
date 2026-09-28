"""R24 warm-detail replay reuse — call-count guards + behaviour equivalence.

Root cause fix (measured in tests/fixtures/pipeline/current-read-fix):

* ``LocalTagStore.load_inputs`` replayed ``LocalClaimStore.load_evidence`` twice
  per call — once inside ``load_snapshot`` (envelope only) and once again for the
  discovery/graph — on one immutable published snapshot. It must now replay the
  mutually verified evidence exactly once and reuse it for both.
* ``claims.py`` ``detail`` called ``tags.load_inputs`` twice with identical
  arguments (best-effort assurance context + strict tagged body). It must call it
  once per request and share the single result.

These guards fail on the pre-fix code (2 replays / 2 load_inputs) and pass after.
They also pin the invariants that must NOT change: load_snapshot still verifies
every hash/pin, load_inputs stays deterministic and self-verifying (the fixed
result equals a reference load_inputs), a tampered snapshot is still rejected on
the next request, untagged detail stays 200 with no tag artifacts, tagged detail
is stable across two warm requests, and a genuine load_inputs failure on a tagged
claim still propagates as an error. A full baseline-vs-final DTO byte-equivalence
comparison is NOT established here (see current-read-fix RESULT).
"""

from __future__ import annotations

from tests.acceptance.test_upload import TENANT
from tests.integration.test_local_tag_runner import verified_setup
from tests.integration.test_run_lifecycle import client, validate


def _count_calls(obj, name, monkeypatch):
    """Wrap obj.name to count invocations; returns a mutable {'n': int}."""
    counter = {"n": 0}
    original = getattr(obj, name)

    def wrapped(*args, **kwargs):
        counter["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(obj, name, wrapped)
    return counter


def test_load_inputs_replays_claim_evidence_once(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    claim_id = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]["claim_id"]

    # Repeated reads must be deterministic, independent of instrumentation.
    expected = runner.tags.load_inputs(TENANT, run_id, claim_id)

    evidence_calls = _count_calls(runner.tags.claims, "load_evidence", monkeypatch)
    inputs = runner.tags.load_inputs(TENANT, run_id, claim_id)

    # RED before the fix: this was 2 (load_snapshot + explicit re-replay).
    assert evidence_calls["n"] == 1
    assert inputs == expected


def test_load_snapshot_still_verifies_and_matches_evidence_once(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"

    evidence_calls = _count_calls(runner.tags.claims, "load_evidence", monkeypatch)
    envelope = runner.tags.load_snapshot(TENANT, run_id)

    # load_snapshot performs exactly one evidence replay for its pin check and
    # returns the tag envelope unchanged.
    assert evidence_calls["n"] == 1
    assert envelope["claims"]
    assert "rulepack_use" in envelope or "coverage" in envelope


def test_tampered_tag_checkpoint_still_rejected_on_next_request(tmp_path, monkeypatch):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    claim_id = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]["claim_id"]

    # A clean request succeeds.
    assert runner.tags.load_inputs(TENANT, run_id, claim_id) is not None

    # Corrupt the recorded tag_snapshot hash so the checkpoint no longer matches.
    run = service.store.jobs.get_run(TENANT, run_id)
    tampered = run["tag_snapshot_sha256"]
    tampered = ("0" if tampered[0] != "0" else "1") + tampered[1:]

    original_get_run = service.store.jobs.get_run

    def poisoned_get_run(tenant_id, rid):
        record = dict(original_get_run(tenant_id, rid))
        if rid == run_id:
            record["tag_snapshot_sha256"] = tampered
        return record

    monkeypatch.setattr(runner.tags.store.jobs, "get_run", poisoned_get_run)

    # Revalidation on the subsequent request must reject the tamper, not serve a
    # cached/reused envelope.
    import pytest

    with pytest.raises(ValueError, match="TAG_CHECKPOINT_HASH_MISMATCH"):
        runner.tags.load_snapshot(TENANT, run_id)
    with pytest.raises(ValueError, match="TAG_CHECKPOINT_HASH_MISMATCH"):
        runner.tags.load_inputs(TENANT, run_id, claim_id)


def test_detail_calls_load_inputs_once_per_request_tagged(tmp_path, monkeypatch):
    from proofops_api.routers.claims import build_claims_router

    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    http.app.include_router(
        build_claims_router(runner.claims, auth, tags=runner.tags, clock=lambda: now[0])
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    claim_id = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]["claim_id"]
    url = f"/v1/runs/{run_id}/claims/{claim_id}"

    inputs_calls = _count_calls(runner.tags, "load_inputs", monkeypatch)
    detail = http.get(url)
    assert detail.status_code == 200, detail.text

    # RED before the fix: this was 2 (assurance context + tagged body).
    assert inputs_calls["n"] == 1
    validate("ClaimDetail", detail.json())
    body = detail.json()
    assert body["tag_status"] == "tagged"
    assert len(set(body["replicate_request_ids"])) == 3
    assert detail.headers["etag"] == '"1"'


def test_detail_tagged_body_and_headers_stable_across_two_warm_requests(tmp_path, monkeypatch):
    """Two identical warm requests return byte-identical body + headers.

    This is after-vs-after stability of the fixed code, NOT a before/after
    equivalence proof across the fix boundary (see current-read-fix RESULT).
    """
    from proofops_api.routers.claims import build_claims_router

    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    http.app.include_router(
        build_claims_router(runner.claims, auth, tags=runner.tags, clock=lambda: now[0])
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    claim_id = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]["claim_id"]
    url = f"/v1/runs/{run_id}/claims/{claim_id}"

    first = http.get(url)
    second = http.get(url)
    assert first.status_code == second.status_code == 200
    assert first.content == second.content
    assert first.headers.get("etag") == second.headers.get("etag")
    assert first.headers.get("cache-control") == second.headers.get("cache-control") == "no-store"


def test_detail_untagged_stays_extraction_only(tmp_path, monkeypatch):
    """An unpublished tag checkpoint leaves context undetermined, without artifacts."""
    from proofops.adapters.local.tag_store import LocalTagStore
    from proofops_api.routers.claims import build_claims_router

    from tests.integration.test_local_extract_runner import extraction_setup

    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    tags = LocalTagStore(service.store, service.uploads, runner.parser)
    calls = _count_calls(tags, "load_inputs", monkeypatch)
    http.app.include_router(
        build_claims_router(runner.claims, auth, tags=tags, clock=lambda: now[0])
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    claim = runner.claims.list(TENANT, run_id)[0]

    detail = http.get(f"/v1/runs/{run_id}/claims/{claim.claim_id}")
    assert calls["n"] == 1
    assert detail.status_code == 200, detail.text
    validate("ClaimDetail", detail.json())
    body = detail.json()
    assert body["tag_status"] == "untagged"
    assert body["elements"] == []
    assert body["replicate_request_ids"] == []
    assert body["packet_sha256"] is None
    assert body["assurance"]["status"] == "undetermined"
    assert "etag" not in {key.lower() for key in detail.headers}


def test_detail_tagged_propagates_genuine_load_inputs_failure(tmp_path, monkeypatch):
    """A real load_inputs integrity failure on a tagged claim must surface as an
    error (409 ARTIFACT_UNAVAILABLE), not be masked by the best-effort assurance
    context. The shared single call must still be strict for the tagged body."""
    from proofops_api.routers.claims import build_claims_router

    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    http.app.include_router(
        build_claims_router(runner.claims, auth, tags=runner.tags, clock=lambda: now[0])
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    claim_id = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]["claim_id"]
    url = f"/v1/runs/{run_id}/claims/{claim_id}"

    # Sanity: tagged claim currently resolves.
    assert http.get(url).status_code == 200

    def boom(tenant_id, rid, cid):
        raise ValueError("TAG_REPLAY_MISMATCH")

    monkeypatch.setattr(runner.tags, "load_inputs", boom)
    failed = http.get(url)
    assert failed.status_code == 409, failed.text
    assert failed.json()["error"]["code"] == "ARTIFACT_UNAVAILABLE"
