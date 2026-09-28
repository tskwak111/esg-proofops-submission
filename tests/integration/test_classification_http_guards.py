"""HTTP classification permissions, CSRF and conditional-write forwarding."""

from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops_api.auth import AuthStore
from proofops_api.routers.classification import build_classification_router

from tests.acceptance.test_rulepack_api import _seed_auth


def test_classification_http_guards_and_conditional_headers():
    class Service:
        calls = []

        def view(self, actor, run, claim):
            self.calls.append((actor.tenant_id, run, claim))
            return {"etag": '"' + "a" * 64 + '"', "eligible": True}

        def record_and_enqueue(self, actor, run, claim, body, if_match, key, **provenance):
            assert provenance["origin"] == "human_classification"
            assert provenance["classified_by"] == actor.user_sub
            assert provenance["delegation_authority"] is None
            self.calls.append((actor, run, claim, body, if_match, key))
            return {"classification": {"revision": 1}, "reprocess_job": {"status": "pending"}}

    service = Service()
    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    token = _seed_auth(auth, role="reviewer")
    app = FastAPI()
    app.include_router(
        build_classification_router(service, auth, allowed_origin="https://testserver")
    )
    http = TestClient(app, base_url="https://testserver")
    url = f"/v1/runs/{uuid4()}/claims/{uuid4()}/classification"
    assert http.get(url).status_code == 401 and not service.calls
    http.cookies.set("__Host-proofops_session", "admin-session")
    response = http.get(url)
    assert response.status_code == 200 and response.headers["etag"] == response.json()["etag"]
    body = dict(
        track="management",
        safe_harbor_category=None,
        dimensions=dict(entity=None, metric=None, reporting_period=None),
        reason="verified original",
    )
    before = len(service.calls)
    assert http.post(url, json=body).status_code == 403
    assert len(service.calls) == before
    headers = {
        "Origin": "https://testserver",
        "X-CSRF-Token": token,
        "If-Match": response.headers["etag"],
        "Idempotency-Key": str(uuid4()),
    }
    assert (
        http.post(url, json=body | {"origin": "ai_delegated"}, headers=headers).status_code == 422
    )
    assert len(service.calls) == before
    assert http.post(url, json=body, headers=headers | {"If-Match": "a" * 64}).status_code == 400
    assert len(service.calls) == before
    accepted = http.post(url, json=body, headers=headers)
    assert accepted.status_code == 202 and accepted.headers["cache-control"] == "no-store"
    assert service.calls[-1][3:] == (body, headers["If-Match"], headers["Idempotency-Key"])
    _seed_auth(auth, role="viewer")
    before = len(service.calls)
    assert http.post(url, json=body, headers=headers).status_code == 403
    assert len(service.calls) == before
