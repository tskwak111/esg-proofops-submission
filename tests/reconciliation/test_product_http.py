"""HTTP surface for the reconciliation product router.

Real FastAPI routing, real cookie session/CSRF/rate helpers, real SQLite store.
No model call, no network, no customer data.
"""

from __future__ import annotations

import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.adapters.local.reconciliation_store import LocalReconciliationStore
from proofops.application.authorization import AuthContext, MembershipRecord, SessionRecord
from proofops_api.auth import AuthStore
from proofops_api.routers.reconciliation import build_reconciliation_router

from tests.acceptance.test_upload import FOREIGN, TENANT
from tests.reconciliation.test_product_store import prepare_bundle, verified  # noqa: F401

ORIGIN = "http://testserver"
COOKIE = "__Host-proofops_session"
ROLES = ("viewer", "editor", "reviewer", "admin")

REVIEW = {
    "reason": "confirmed against the original page",
    "confirm_source_bindings": True,
    "confirm_decision_bindings": True,
    "confirm_search_coverage": False,
}


def seed_sessions(auth_store: AuthStore) -> dict[str, str]:
    """One session per role, plus one admin session in a foreign tenant."""
    far_future = time.time() + 10_000
    tokens: dict[str, str] = {}
    seats = [(role, role, TENANT) for role in ROLES] + [("foreign", "admin", FOREIGN)]
    for name, role, tenant in seats:
        user = f"{name}-user"
        auth_store.sessions.put_with_token(
            SessionRecord(
                f"{name}-session",
                user,
                tenant,
                auth_store.hash_csrf(f"csrf-{name}"),
                far_future,
                far_future,
                False,
            ),
            f"csrf-{name}",
        )
        auth_store.memberships.put(MembershipRecord(tenant, user, role, "active"))
        token = auth_store.sessions.csrf_token_for(f"{name}-session")
        assert token is not None
        tokens[name] = token
    return tokens


def headers(
    tokens: dict[str, str],
    name: str,
    *,
    if_match: str | None = '"1"',
    key: str | None = None,
) -> dict[str, str]:
    built = {"X-CSRF-Token": tokens[name], "Origin": ORIGIN}
    if if_match is not None:
        built["If-Match"] = if_match
    built["Idempotency-Key"] = key or str(uuid4())
    return built


@pytest.fixture
def api(verified, tmp_path: Path):  # noqa: F811
    """One router over the real verified run, with a session per role."""
    auth_store = AuthStore(sessions=InMemorySessionStore(), memberships=InMemoryMembershipStore())
    tokens = seed_sessions(auth_store)
    service = verified["service"]
    store = LocalReconciliationStore(
        service.store.path,
        tmp_path / "managed",
        run_store=service.store,
        claims=verified["claims"],
        tags=verified["tags"],
    )
    app = FastAPI()
    app.include_router(build_reconciliation_router(store, auth_store, allowed_origin=ORIGIN))

    run_id, claim_id = verified["run_id"], verified["claim_id"]
    bundle, artifacts_root = prepare_bundle(tmp_path, verified)
    operator = AuthContext(
        user_sub="operator",
        tenant_id=TENANT,
        role="admin",
        capabilities=frozenset({"viewer", "editor", "reviewer", "admin"}),
        session_id=str(uuid4()),
    )
    detail = store.register_case(operator, run_id, claim_id, bundle, artifacts_root)
    return TestClient(app), tokens, store, detail, run_id, claim_id


def source(detail) -> str:
    return detail["sources"][0]["source_id"]


def as_role(client: TestClient, name: str) -> TestClient:
    client.cookies.set(COOKIE, f"{name}-session")
    return client


# --------------------------------------------------------------------------- #
# reads
# --------------------------------------------------------------------------- #


def test_a_viewer_reads_the_case_and_gets_an_etag(api):
    client, _tokens, _store, detail, *_ = api
    response = as_role(client, "viewer").get(f"/v1/reconciliation/cases/{detail['case_id']}")
    assert response.status_code == 200, response.text
    assert response.headers["ETag"] == '"1"'
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["review_state"] == "pending"
    assert body["policy_approved"] is False
    assert body["latest_result"] is None
    assert set(body) >= {
        "case_id",
        "run_id",
        "claim_id",
        "item",
        "revision",
        "synthetic",
        "review_state",
        "policy_approved",
        "packet",
        "policy",
        "sources",
        "latest_result",
    }


def test_the_claim_listing_returns_the_registered_case(api):
    client, _tokens, _store, detail, run_id, claim_id = api
    response = as_role(client, "viewer").get(f"/v1/runs/{run_id}/claims/{claim_id}/reconciliation")
    assert response.status_code == 200, response.text
    assert detail["case_id"] in {item["case_id"] for item in response.json()["items"]}


def test_an_anonymous_request_is_rejected(api):
    client, _tokens, _store, detail, *_ = api
    client.cookies.clear()
    assert client.get(f"/v1/reconciliation/cases/{detail['case_id']}").status_code == 401


def test_a_foreign_tenant_sees_not_found_not_forbidden(api):
    client, _tokens, _store, detail, run_id, claim_id = api
    read = as_role(client, "foreign").get(f"/v1/reconciliation/cases/{detail['case_id']}")
    assert read.status_code == 404
    listed = as_role(client, "foreign").get(f"/v1/runs/{run_id}/claims/{claim_id}/reconciliation")
    assert listed.status_code in {200, 404}
    if listed.status_code == 200:
        assert listed.json()["items"] == []


def test_an_unknown_case_is_not_found(api):
    client, *_ = api
    assert as_role(client, "viewer").get(f"/v1/reconciliation/cases/{uuid4()}").status_code == 404


def test_a_local_filesystem_path_is_never_accepted_as_a_case_id(api):
    client, *_ = api
    response = as_role(client, "viewer").get("/v1/reconciliation/cases/..%2F..%2Fetc%2Fpasswd")
    assert response.status_code in {400, 404, 422}


# --------------------------------------------------------------------------- #
# role protection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("path", "body", "denied"),
    [
        ("review", REVIEW, ("viewer", "editor")),
        (
            "policy-approval",
            {"approved": True, "reason": "owner approval"},
            ("viewer", "editor", "reviewer"),
        ),
        ("evaluate", {}, ("viewer",)),
    ],
)
def test_mutations_refuse_insufficient_roles(api, path, body, denied):
    client, tokens, _store, detail, *_ = api
    for role in denied:
        response = as_role(client, role).post(
            f"/v1/reconciliation/cases/{detail['case_id']}/{path}",
            json=body,
            headers=headers(tokens, role),
        )
        assert response.status_code == 403, (role, path, response.text)


def test_a_foreign_admin_cannot_approve_another_tenants_policy(api):
    client, tokens, _store, detail, *_ = api
    response = as_role(client, "foreign").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/policy-approval",
        json={"approved": True, "reason": "not my tenant"},
        headers=headers(tokens, "foreign"),
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# CSRF, If-Match, idempotency
# --------------------------------------------------------------------------- #


def test_a_mutation_without_csrf_or_origin_is_rejected(api):
    client, tokens, _store, detail, *_ = api
    sent = headers(tokens, "reviewer")
    for drop in ("X-CSRF-Token", "Origin"):
        broken = {key: value for key, value in sent.items() if key != drop}
        broken["Idempotency-Key"] = str(uuid4())
        response = as_role(client, "reviewer").post(
            f"/v1/reconciliation/cases/{detail['case_id']}/review", json=REVIEW, headers=broken
        )
        assert response.status_code == 403, (drop, response.text)


def test_a_mutation_without_an_idempotency_key_is_rejected(api):
    client, tokens, _store, detail, *_ = api
    sent = headers(tokens, "reviewer")
    sent.pop("Idempotency-Key")
    response = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review", json=REVIEW, headers=sent
    )
    assert response.status_code in {400, 422}


@pytest.mark.parametrize("if_match", [None, "1", '"0"', '"abc"', "*"])
def test_a_missing_or_malformed_if_match_is_rejected(api, if_match):
    client, tokens, _store, detail, *_ = api
    response = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=REVIEW,
        headers=headers(tokens, "reviewer", if_match=if_match),
    )
    assert response.status_code in {400, 422, 428}, response.text


def test_a_stale_if_match_conflicts(api):
    client, tokens, _store, detail, *_ = api
    first = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=REVIEW,
        headers=headers(tokens, "reviewer"),
    )
    assert first.status_code == 200, first.text
    assert first.headers["ETag"] == '"2"'
    second = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=REVIEW,
        headers=headers(tokens, "reviewer"),
    )
    assert second.status_code == 409, second.text


def test_repeating_an_idempotency_key_replays_without_advancing(api):
    client, tokens, _store, detail, *_ = api
    key = str(uuid4())
    first = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=REVIEW,
        headers=headers(tokens, "reviewer", key=key),
    )
    second = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=REVIEW,
        headers=headers(tokens, "reviewer", key=key),
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    current = as_role(client, "viewer").get(f"/v1/reconciliation/cases/{detail['case_id']}")
    assert current.json()["revision"] == 2


def test_the_same_key_with_a_different_body_conflicts(api):
    client, tokens, _store, detail, *_ = api
    key = str(uuid4())
    as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=REVIEW,
        headers=headers(tokens, "reviewer", key=key),
    )
    changed = dict(REVIEW, confirm_search_coverage=True)
    response = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=changed,
        headers=headers(tokens, "reviewer", key=key),
    )
    assert response.status_code == 409, response.text


# --------------------------------------------------------------------------- #
# request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": "ok reason", "confirm_source_bindings": True},
        dict(REVIEW, confirm_source_bindings=False),
        dict(REVIEW, confirm_decision_bindings="yes"),
        dict(REVIEW, reason=""),
        dict(REVIEW, status="matched"),
    ],
)
def test_a_review_body_that_is_not_a_full_confirmation_is_refused(api, body):
    client, tokens, _store, detail, *_ = api
    response = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=body,
        headers=headers(tokens, "reviewer"),
    )
    assert response.status_code in {400, 422}, response.text


def test_a_reviewer_cannot_dictate_a_result_status(api):
    client, tokens, _store, detail, *_ = api
    as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/review",
        json=REVIEW,
        headers=headers(tokens, "reviewer"),
    )
    current = as_role(client, "viewer").get(f"/v1/reconciliation/cases/{detail['case_id']}").json()
    assert current["latest_result"] is None


def test_evaluate_rejects_an_unexpected_payload(api):
    client, tokens, _store, detail, *_ = api
    response = as_role(client, "editor").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/evaluate",
        json={"policy": {"approved": True}},
        headers=headers(tokens, "editor"),
    )
    assert response.status_code in {400, 422}, response.text


# --------------------------------------------------------------------------- #
# evaluation and snapshots
# --------------------------------------------------------------------------- #


def test_evaluating_a_pending_case_succeeds_at_http_level_with_a_blocked_result(api):
    client, tokens, _store, detail, *_ = api
    response = as_role(client, "editor").post(
        f"/v1/reconciliation/cases/{detail['case_id']}/evaluate",
        json={},
        headers=headers(tokens, "editor"),
    )
    assert response.status_code == 200, response.text
    result = response.json()["latest_result"]["result"]
    assert result["execution_state"] == "blocked"
    assert result["status"] is None


def test_a_reviewed_and_approved_case_evaluates_against_server_snapshots(api):
    client, tokens, _store, detail, *_ = api
    case = detail["case_id"]
    reviewed = as_role(client, "reviewer").post(
        f"/v1/reconciliation/cases/{case}/review", json=REVIEW, headers=headers(tokens, "reviewer")
    )
    approved = as_role(client, "admin").post(
        f"/v1/reconciliation/cases/{case}/policy-approval",
        json={"approved": True, "reason": "owner approval"},
        headers=headers(tokens, "admin", if_match=reviewed.headers["ETag"]),
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["policy_approved"] is True
    evaluated = as_role(client, "editor").post(
        f"/v1/reconciliation/cases/{case}/evaluate",
        json={},
        headers=headers(tokens, "editor", if_match=approved.headers["ETag"]),
    )
    assert evaluated.status_code == 200, evaluated.text
    result = evaluated.json()["latest_result"]["result"]
    assert result["schema_version"] == "1.1"
    assert "policy_unapproved" not in result["reason_codes"]


def test_a_revision_snapshot_is_readable_and_stable(api):
    client, tokens, _store, detail, *_ = api
    case = detail["case_id"]
    evaluated = as_role(client, "editor").post(
        f"/v1/reconciliation/cases/{case}/evaluate", json={}, headers=headers(tokens, "editor")
    )
    revision = evaluated.json()["latest_result"]["revision"]
    first = as_role(client, "viewer").get(f"/v1/reconciliation/cases/{case}/revisions/{revision}")
    assert first.status_code == 200, first.text
    assert first.headers["Cache-Control"] == "no-store"
    second = as_role(client, "viewer").get(f"/v1/reconciliation/cases/{case}/revisions/{revision}")
    assert first.json() == second.json()


def test_an_unknown_revision_is_not_found(api):
    client, _tokens, _store, detail, *_ = api
    response = as_role(client, "viewer").get(
        f"/v1/reconciliation/cases/{detail['case_id']}/revisions/9999"
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# source download
# --------------------------------------------------------------------------- #


def test_source_bytes_download_as_an_inert_attachment(api):
    client, _tokens, _store, detail, *_ = api
    response = as_role(client, "viewer").get(
        f"/v1/reconciliation/cases/{detail['case_id']}/sources/{source(detail)}/content"
    )
    assert response.status_code == 200, response.text
    assert response.headers["Content-Disposition"].startswith("attachment;")
    assert response.headers["Content-Type"].startswith("application/octet-stream")
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert sha256(response.content).hexdigest() == response.headers["X-Content-SHA256"]


def test_a_tampered_managed_source_is_refused_at_download(api):
    client, _tokens, store, detail, *_ = api
    store.managed_path(TENANT, detail["case_id"], source(detail)).write_bytes(b"tampered")
    response = as_role(client, "viewer").get(
        f"/v1/reconciliation/cases/{detail['case_id']}/sources/{source(detail)}/content"
    )
    assert response.status_code in {409, 422, 500}
    assert b"tampered" not in response.content


def test_unicode_document_filename_downloads_without_header_encoding_failure(api, monkeypatch):
    client, _tokens, store, detail, *_ = api
    original = store.source_content

    def named_content(*args):
        payload, _filename, digest = original(*args)
        return payload, "보고서-2024.pdf", digest

    monkeypatch.setattr(store, "source_content", named_content)
    response = as_role(client, "viewer").get(
        f"/v1/reconciliation/cases/{detail['case_id']}/sources/{source(detail)}/content"
    )
    assert response.status_code == 200, response.text
    assert response.headers["Content-Disposition"] == 'attachment; filename="-2024.pdf"'
    assert sha256(response.content).hexdigest() == response.headers["X-Content-SHA256"]


def test_an_unknown_source_is_not_found(api):
    client, _tokens, _store, detail, *_ = api
    response = as_role(client, "viewer").get(
        f"/v1/reconciliation/cases/{detail['case_id']}/sources/nope/content"
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# contract surface
# --------------------------------------------------------------------------- #


def test_every_reconciliation_route_declares_a_response_model_and_operation_id(api):
    client, *_ = api
    schema = client.app.openapi()
    paths = {path for path in schema["paths"] if "reconciliation" in path}
    assert len(paths) == 7, sorted(paths)
    for path, operations in schema["paths"].items():
        if "reconciliation" not in path:
            continue
        for method, operation in operations.items():
            assert operation.get("operationId"), (path, method)
            assert "200" in operation["responses"], (path, method)


def test_c5_is_not_reachable_through_the_product_api(api):
    client, *_ = api
    schema = client.app.openapi()
    assert "C5" not in str(schema["components"].get("schemas", {}))
