"""TASK-041 acceptance tests for browser session protections."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.usefixtures("_local_runtime")

SESSION_COOKIE = "__Host-proofops_session"
ORIGIN = "http://testserver"
TENANT_ID = "11111111-1111-4111-8111-111111111111"
USER_SUB = "session-security-user"


@pytest.fixture()
def _local_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")
    monkeypatch.setenv("APP_ORIGIN", ORIGIN)


@pytest.fixture()
def client() -> TestClient:
    from proofops_api.main import create_app

    return TestClient(create_app())


def _seed_live_session(client: TestClient, *, session_id: str = "logout-session") -> None:
    from proofops.application.authorization import MembershipRecord, SessionRecord

    store = client.app.state.composition.auth_store
    future = time.time() + 10_000
    store.sessions.put(
        SessionRecord(
            session_id=session_id,
            user_sub=USER_SUB,
            active_tenant_id=TENANT_ID,
            csrf_hash="ignored-by-hash-only-local-store",
            expires_at=future,
            idle_deadline=future,
            revoked=False,
        )
    )
    store.memberships.put(MembershipRecord(TENANT_ID, USER_SUB, "viewer", "active"))
    client.cookies.set(SESSION_COOKIE, session_id)


def _csrf_from_live_session(client: TestClient) -> str:
    response = client.get("/v1/session")
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_logout_without_csrf_is_403_and_does_not_revoke(client: TestClient) -> None:
    _seed_live_session(client)

    response = client.post("/v1/auth/logout", headers={"Origin": ORIGIN})

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"
    assert client.get("/v1/session").status_code == 200


def test_logout_rejects_wrong_origin(client: TestClient) -> None:
    _seed_live_session(client)
    csrf = _csrf_from_live_session(client)

    response = client.post(
        "/v1/auth/logout",
        headers={"Origin": "http://evil.example", "X-CSRF-Token": csrf},
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"


def test_logout_accepts_configured_browser_origin_when_api_host_differs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from proofops_api.main import create_app

    browser_origin = "https://app.example"
    monkeypatch.setenv("APP_ORIGIN", browser_origin)
    client = TestClient(create_app(), base_url="https://api.example")
    _seed_live_session(client)
    csrf = _csrf_from_live_session(client)

    response = client.post(
        "/v1/auth/logout",
        headers={"Origin": browser_origin, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 204


@pytest.mark.parametrize("route", ["/v1/auth/logout", "/v1/session/tenant"])
def test_missing_configured_origin_cannot_be_replaced_by_attacker_host(monkeypatch, route):
    from proofops_api.main import create_app

    monkeypatch.delenv("APP_ORIGIN", raising=False)
    client = TestClient(create_app(), base_url="http://attacker.example")
    _seed_live_session(client)
    csrf = _csrf_from_live_session(client)
    response = client.post(
        route,
        json={"tenant_id": TENANT_ID},
        headers={"Origin": "http://attacker.example", "X-CSRF-Token": csrf},
    )
    assert response.status_code == 403
    assert client.get("/v1/session").status_code == 200


def test_cors_allows_credentials_only_for_configured_browser_origin(client):
    headers = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "X-CSRF-Token",
    }
    allowed = client.options("/v1/auth/logout", headers={**headers, "Origin": ORIGIN})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert allowed.headers["access-control-allow-credentials"] == "true"
    denied = client.options(
        "/v1/auth/logout", headers={**headers, "Origin": "https://attacker.example"}
    )
    assert "access-control-allow-origin" not in denied.headers


def test_real_http_get_csrf_then_logout_revokes_session_and_clears_cookie(
    client: TestClient,
) -> None:
    raw_session_id = "logout-session"
    _seed_live_session(client, session_id=raw_session_id)
    csrf = _csrf_from_live_session(client)

    response = client.post(
        "/v1/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert not response.url.query
    assert "location" not in response.headers
    assert csrf not in str(response.headers)
    set_cookie = response.headers["set-cookie"]
    assert f'{SESSION_COOKIE}=""' in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie

    store = client.app.state.composition.auth_store.sessions
    revoked = store.get(raw_session_id)
    assert revoked is not None and revoked.revoked is True
    client.cookies.set(SESSION_COOKIE, raw_session_id)
    assert client.get("/v1/session").status_code == 401


def test_expired_logout_is_401_before_csrf_validation(client: TestClient) -> None:
    from proofops.application.authorization import SessionRecord

    store = client.app.state.composition.auth_store
    past = time.time() - 1
    store.sessions.put(
        SessionRecord(
            session_id="expired-session",
            user_sub=USER_SUB,
            active_tenant_id=None,
            csrf_hash="ignored-by-hash-only-local-store",
            expires_at=past,
            idle_deadline=past,
            revoked=False,
        )
    )
    client.cookies.set(SESSION_COOKIE, "expired-session")

    response = client.post("/v1/auth/logout")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_logout_without_cookie_is_401_even_with_csrf_headers(client: TestClient) -> None:
    response = client.post(
        "/v1/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": "attacker-token"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTH_REQUIRED"


def test_csrf_check_uses_exact_configured_origin_and_hash() -> None:
    from proofops.adapters.local.auth_store import hash_token
    from proofops_api.middleware import verify_csrf

    token = "opaque-csrf-token"
    digest = hash_token(token)
    assert verify_csrf(
        origin=ORIGIN,
        csrf_token=token,
        csrf_hash=digest,
        allowed_origin=ORIGIN,
    )
    assert not verify_csrf(
        origin=f"{ORIGIN}.evil.example",
        csrf_token=token,
        csrf_hash=digest,
        allowed_origin=ORIGIN,
    )
    assert not verify_csrf(
        origin=ORIGIN,
        csrf_token="wrong-token",
        csrf_hash=digest,
        allowed_origin=ORIGIN,
    )


def test_responses_apply_self_restricted_csp(client: TestClient) -> None:
    response = client.get("/v1/health/live")

    assert response.status_code == 200
    csp = response.headers["content-security-policy"]
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-src 'self' blob:" in csp
    assert "object-src 'none'" in csp


def test_web_source_does_not_persist_tokens_in_browser_storage() -> None:
    web_source = Path(__file__).parents[2] / "apps" / "web" / "src"
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in web_source.rglob("*")
        if path.suffix in {".ts", ".tsx"}
    )

    assert "localStorage" not in source
    assert "sessionStorage" not in source


def test_rotate_session_delegates_to_atomic_store_rotation() -> None:
    from proofops.adapters.local.auth_store import InMemorySessionStore
    from proofops.application.authorization import SessionRecord
    from proofops_api.session import rotate_session

    store = InMemorySessionStore()
    store.put(SessionRecord("old-sid", USER_SUB, TENANT_ID, "ignored", 100.0, 100.0, False))

    rotated = rotate_session(
        session_id="old-sid",
        tenant_id=TENANT_ID,
        now=0.0,
        session_store=store,
    )

    assert rotated.session_id != "old-sid"
    old = store.get("old-sid")
    assert old is not None and old.revoked is True
