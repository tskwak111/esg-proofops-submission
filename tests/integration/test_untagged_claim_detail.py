"""Untagged extracted-claim detail: extraction-only read, refusal, tagged stability."""

import re
from dataclasses import asdict, replace
from uuid import uuid4

from proofops.application.authorization import MembershipRecord

from tests.acceptance.test_upload import FOREIGN, TENANT
from tests.integration.test_local_extract_runner import extraction_setup
from tests.integration.test_run_lifecycle import client, validate


def test_untagged_detail_returns_extraction_only_with_original_source_refs(tmp_path, monkeypatch):
    from proofops_api.routers.claims import build_claims_router

    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    http.app.include_router(build_claims_router(runner.claims, auth, clock=lambda: now[0]))
    url = f"/v1/runs/{run_id}/claims"
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    actual = runner.claims.list(TENANT, run_id)
    assert actual
    claim = actual[0]

    detail = http.get(url + "/" + claim.claim_id)
    assert detail.status_code == 200, detail.text
    assert detail.headers["cache-control"] == "no-store"
    # No tag revision exists, so no ETag may condition future tag edits.
    assert "etag" not in {key.lower() for key in detail.headers}
    validate("ClaimDetail", detail.json())

    body = detail.json()
    assert body["claim"]["claim_id"] == claim.claim_id
    assert body["claim"]["track"] is None
    assert body["claim"]["decision"] is None
    assert body["claim"]["quote"] == claim.quote
    # Original extraction source refs are preserved for the SourceViewer.
    assert len(body["source_refs"]) == len(claim.source_refs)
    expected_refs = [asdict(ref) for ref in claim.source_refs]
    for expected, received in zip(expected_refs, body["source_refs"]):
        assert received["source_id"] == expected["source_id"]
        assert received["page_num"] == expected["page_num"]
        assert received["quote"] == expected["quote"]
        assert received["bbox"] == (
            list(expected["bbox"]) if expected["bbox"] is not None else None
        )
    assert body["elements"] == []
    assert body["replicate_request_ids"] == []
    # No tag packet exists; a null is returned instead of an invented hash.
    assert body["packet_sha256"] is None
    assert body["tag_status"] == "untagged"
    assert body["assurance"]["status"] == "undetermined"
    assert body["assurance"]["evidence_refs"] == []
    assert body["suggestion"] is None
    assert body["basis_refs"] == []
    assert "raw_response" not in detail.text and "reviewer_sub" not in detail.text

    # Unknown claims still 404; list projection is unchanged.
    assert http.get(url + "/" + str(uuid4())).status_code == 404
    listed = http.get(url)
    assert listed.status_code == 200
    assert all(item["decision"] is None for item in listed.json()["items"])


def test_untagged_detail_refuses_foreign_tenant(tmp_path, monkeypatch):
    from proofops_api.routers.claims import build_claims_router

    service, run_id, runner, now, _ = extraction_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    http.app.include_router(build_claims_router(runner.claims, auth, clock=lambda: now[0]))
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    claim_id = runner.claims.list(TENANT, run_id)[0].claim_id

    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=FOREIGN))
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "admin", "active"))
    assert http.get(f"/v1/runs/{run_id}/claims").status_code == 404
    assert http.get(f"/v1/runs/{run_id}/claims/{claim_id}").status_code == 404


def test_tagged_detail_behavior_unchanged(tmp_path, monkeypatch):
    from proofops_api.routers.claims import build_claims_router

    from tests.integration.test_local_tag_runner import verified_setup

    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    http, auth = client(service)
    http.app.include_router(
        build_claims_router(runner.claims, auth, tags=runner.tags, clock=lambda: now[0])
    )
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    claim_id = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]["claim_id"]

    detail = http.get(f"/v1/runs/{run_id}/claims/{claim_id}")
    assert detail.status_code == 200, detail.text
    validate("ClaimDetail", detail.json())
    body = detail.json()
    assert body["claim"]["track"] == "performance"
    assert body["claim"]["decision"] is None
    assert body["tag_status"] == "tagged"
    assert re.fullmatch(r"[0-9a-f]{64}", body["packet_sha256"])
    assert len(set(body["replicate_request_ids"])) == 3
    assert len(body["elements"]) == 6
    assert all(element["state"] == "unknown" for element in body["elements"])
    assert body["assurance"]["status"] == "undetermined"
    assert detail.headers["etag"] == '"1"'
