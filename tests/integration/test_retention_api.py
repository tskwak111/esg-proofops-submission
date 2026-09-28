"""Real local HTTP request and durable tombstones; physical customer deletion stays gated."""

import importlib
import json
import sqlite3
from uuid import uuid4

import pytest
from fastapi import APIRouter

from tests.integration.test_catalog_lists import (
    FOREIGN,
    NOW,
    ORIGIN,
    TENANT,
    _client,
    _document,
    _uploads,
    _version,
)
from tests.integration.test_run_lifecycle import validate


def workspace(tmp_path, *, role="admin"):
    try:
        module = importlib.import_module("proofops.adapters.local.retention_store")
        router = importlib.import_module("proofops_api.routers.deletion")
    except ImportError as exc:
        pytest.fail(f"Deletion API implementation missing: {exc}")
    from proofops_api.routers.documents import build_documents_router

    uploads = _uploads(tmp_path, lambda: NOW)
    doc = _document(uploads)["document_id"]
    foreign = _document(uploads, tenant=FOREIGN, foreign=True)["document_id"]
    version = _version(uploads, doc)
    store = module.LocalRetentionStore(uploads)

    def routes(auth):
        result = APIRouter()
        result.include_router(
            build_documents_router(
                uploads,
                auth,
                allowed_origin=ORIGIN,
                app_env="local",
                model_adapter="synthetic",
                clock=lambda: NOW,
            )
        )
        result.include_router(
            router.build_deletion_router(store, auth, allowed_origin=ORIGIN, clock=lambda: NOW)
        )
        return result

    return _client(routes, role=role), store, uploads, doc, foreign, version


def test_http_tombstone_is_durable_idempotent_and_never_claims_approved_retention(tmp_path):
    http, store, uploads, doc, foreign, version = workspace(tmp_path)
    url = f"/v1/documents/{doc}/deletion-requests"
    page = http.get("/v1/documents", params={"limit": 1}).json()
    before_original = uploads.read_original(TENANT, version["version_id"])
    before = list(uploads._db.execute("SELECT * FROM upload_records"))
    response = http.post(url, json={"reason": "Synthetic requested deletion"})
    assert response.status_code == 202, response.text
    validate("DeletionRequest", response.json())
    assert response.json()["status"] == "blocked_retention"
    assert response.headers["cache-control"] == "no-store"
    assert http.post(url, json={"reason": "Synthetic requested deletion"}).json() == response.json()
    assert http.post(url, json={"reason": "Changed request reason"}).status_code == 409
    assert list(uploads._db.execute("SELECT * FROM upload_records")) == before
    assert before_original  # original physically retained under unapproved customer policy
    assert http.get(f"/v1/documents/{doc}").status_code == 404
    assert http.get(f'/v1/versions/{version["version_id"]}').status_code == 404
    assert http.get(f"/v1/documents/{doc}/versions").status_code == 404
    assert http.get("/v1/documents").json()["items"] == []
    if page.get("next_cursor"):
        assert (
            http.get("/v1/documents", params={"cursor": page["next_cursor"], "limit": 1}).json()[
                "items"
            ]
            == []
        )
    with pytest.raises(ValueError):
        uploads.read_original(TENANT, version["version_id"])
    reopened = type(store)(uploads)
    assert reopened.is_deleted(TENANT, doc) and not reopened.is_deleted(FOREIGN, foreign)
    with uploads._lock:
        row = uploads._db.execute("SELECT manifest_json FROM retention_tombstones").fetchone()
        manifest = json.loads(row[0])
        assert manifest["policy_status"] == "requires_customer_approval"
        assert manifest["physical_deletion_status"] == "not_run"
        assert set(manifest["categories"]) >= {
            "original",
            "derived",
            "search",
            "cache",
            "review",
            "memory",
        }
        assert version["version_id"] in manifest["document_version_ids"]
        for action in (
            "UPDATE retention_tombstones SET requested_by='other'",
            "DELETE FROM retention_tombstones",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                uploads._db.execute(action)
        with pytest.raises(sqlite3.IntegrityError):
            uploads._db.execute(
                "INSERT OR REPLACE INTO retention_tombstones SELECT * FROM retention_tombstones"
            )
    http.close()
    uploads.close()


@pytest.mark.parametrize("role", ["viewer", "editor", "reviewer"])
def test_deletion_requires_admin(tmp_path, role):
    http, store, uploads, doc, _, _ = workspace(tmp_path, role=role)
    assert (
        http.post(
            f"/v1/documents/{doc}/deletion-requests", json={"reason": "Delete fixture"}
        ).status_code
        == 403
    )
    assert not store.is_deleted(TENANT, doc)
    http.close()
    uploads.close()


def test_deletion_auth_csrf_foreign_scope_and_body_validation(tmp_path):
    http, store, uploads, doc, foreign, _ = workspace(tmp_path)
    assert (
        http.post(
            f"/v1/documents/{foreign}/deletion-requests", json={"reason": "Delete fixture"}
        ).status_code
        == 404
    )
    url = f"/v1/documents/{doc}/deletion-requests"
    for body in (
        {"reason": "no"},
        {"reason": "Delete fixture", "approved": True},
        {"reason": 12345},
    ):
        assert http.post(url, json=body).status_code == 422
    assert (
        http.post(
            url, json={"reason": "Delete fixture"}, headers={"Idempotency-Key": "short"}
        ).status_code
        == 400
    )
    assert (
        http.post(
            url, json={"reason": "Delete fixture"}, headers={"X-CSRF-Token": "bad"}
        ).status_code
        == 403
    )
    assert (
        http.post(
            url, json={"reason": "Delete fixture"}, headers={"Origin": "https://evil.invalid"}
        ).status_code
        == 403
    )
    assert not store.is_deleted(TENANT, doc)
    http.cookies.clear()
    assert http.post(url, json={"reason": "Delete fixture"}).status_code == 401
    http.close()
    uploads.close()


def test_repeated_request_with_new_key_does_not_duplicate_audit(tmp_path):
    http, store, uploads, doc, _, _ = workspace(tmp_path)
    url = f"/v1/documents/{doc}/deletion-requests"
    first = http.post(url, json={"reason": "Delete fixture"})
    again = http.post(
        url, json={"reason": "Delete fixture"}, headers={"Idempotency-Key": str(uuid4())}
    )
    assert first.status_code == again.status_code == 202 and first.json() == again.json()
    with uploads._lock:
        rows = uploads._db.execute(
            "SELECT data FROM upload_audit WHERE tenant=? AND document_id=?", (TENANT, doc)
        ).fetchall()
    assert (
        len([r for r in rows if json.loads(r[0])["action"] == "document.deletion_requested"]) == 1
    )
    http.close()
    uploads.close()


def test_deletion_rejects_oversized_reason_even_if_padding_would_trim_it(tmp_path):
    http, store, uploads, doc, _, _ = workspace(tmp_path)
    response = http.post(
        f"/v1/documents/{doc}/deletion-requests", json={"reason": "Delete fixture" + " " * 1000}
    )
    assert response.status_code == 422
    assert not store.is_deleted(TENANT, doc)
    http.close()
    uploads.close()


def test_reused_later_idempotency_key_rejects_changed_body(tmp_path):
    http, _, uploads, doc, _, _ = workspace(tmp_path)
    url = f"/v1/documents/{doc}/deletion-requests"
    assert http.post(url, json={"reason": "Delete fixture"}).status_code == 202
    key = str(uuid4())
    assert (
        http.post(
            url, json={"reason": "Delete fixture"}, headers={"Idempotency-Key": key}
        ).status_code
        == 202
    )
    assert (
        http.post(
            url, json={"reason": "Changed reason"}, headers={"Idempotency-Key": key}
        ).status_code
        == 409
    )
    http.close()
    uploads.close()


def test_audit_failure_rolls_back_tombstone_and_concurrent_requests_create_one_event(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from proofops.application.authorization import AuthContext

    http, store, uploads, doc, _, _ = workspace(tmp_path)
    actor = AuthContext("fixture", TENANT, "admin", frozenset({"admin"}), "fixture")
    with uploads._lock:
        uploads._db.execute("""CREATE TRIGGER fail_audit BEFORE INSERT ON upload_audit
            BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END""")
    assert (
        http.post(
            f"/v1/documents/{doc}/deletion-requests", json={"reason": "Delete fixture"}
        ).status_code
        == 503
    )
    assert not store.is_deleted(TENANT, doc)
    with uploads._lock:
        uploads._db.execute("DROP TRIGGER fail_audit")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                store.request, actor, doc, {"reason": "Delete fixture"}, str(uuid4()), now=NOW
            )
            for _ in range(2)
        ]
        responses = [f.result() for f in futures]
    assert responses[0] == responses[1] and responses[0]["status"] == "blocked_retention"
    with uploads._lock:
        count = uploads._db.execute("SELECT count(*) FROM retention_tombstones").fetchone()[0]
    assert count == 1
    http.close()
    uploads.close()
