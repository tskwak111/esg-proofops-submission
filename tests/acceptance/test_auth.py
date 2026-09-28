"""TASK-037 acceptance tests: authentication, role, tenant isolation.

Contract under test (docs/20 TASK-037, AT-037, SEC-001, docs/03 §5,
docs/11, evidence/contract_review_resolution.md finding 4):

- No/expired/revoked session -> 401 (AUTH_REQUIRED / SESSION_EXPIRED).
- Valid session but no active tenant selected, or valid session lacking the
  required capability in the active tenant -> 403 (FORBIDDEN).
- A tenant-scoped resource the caller cannot access (wrong tenant, or a
  tenant that does not exist at all) -> 404, and the two cases must be
  indistinguishable from the response (existence is never leaked).
- `POST /v1/session/tenant` only switches to a tenant where the caller has
  a live (status=active) membership; revoked membership is rejected the
  same way as a nonexistent tenant.
- Session rotation happens on tenant switch (session_id/csrf change), and a
  revoked session is rejected immediately (no stale-session window).

These tests exercise both the pure application-layer functions
(`proofops.application.authorization`) and the FastAPI wiring
(`proofops_api.auth` + `proofops_api.main`) with an explicit local-only
in-memory adapter. No network, no real Cognito call: those remain not_run
per AGENTS.md / docs/17 until account values exist.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

pytestmark = pytest.mark.usefixtures("_no_env_leak")


TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
USER_A = "user-a-sub"
USER_B = "user-b-sub"


@pytest.fixture()
def _no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")


# ---------------------------------------------------------------------------
# Pure application-layer tests (no HTTP, no FastAPI)
# ---------------------------------------------------------------------------


def _build_stores():
    from proofops.adapters.local.auth_store import (
        InMemoryMembershipStore,
        InMemorySessionStore,
    )
    from proofops.application.authorization import MembershipRecord, SessionRecord

    sessions = InMemorySessionStore()
    memberships = InMemoryMembershipStore()

    memberships.put(MembershipRecord(TENANT_A, USER_A, "editor", "active"))
    memberships.put(MembershipRecord(TENANT_B, USER_B, "admin", "active"))

    sid_a = "session-a"
    sessions.put(
        SessionRecord(
            session_id=sid_a,
            user_sub=USER_A,
            active_tenant_id=TENANT_A,
            csrf_hash="csrf-a",
            expires_at=10_000.0,
            idle_deadline=10_000.0,
            revoked=False,
        )
    )
    return sessions, memberships, sid_a


def test_local_session_store_hashes_sid_and_retains_no_raw_csrf() -> None:
    import secrets

    from proofops.adapters.local.auth_store import InMemorySessionStore, hash_token
    from proofops.application.authorization import SessionRecord

    sessions = InMemorySessionStore()
    sid = "raw-session-cookie-value"
    csrf = "raw-csrf-token-value"
    sessions.put_with_token(
        SessionRecord(sid, USER_A, None, hash_token(csrf), 10_000.0, 10_000.0, False),
        csrf,
    )

    assert sid not in sessions._sessions  # noqa: SLF001 - storage security contract
    assert sid not in repr(sessions._sessions)  # noqa: SLF001
    assert csrf not in repr(sessions.__dict__)
    issued = sessions.csrf_token_for(sid)
    loaded = sessions.get(sid)
    assert issued is not None and loaded is not None
    assert secrets.compare_digest(hash_token(issued), loaded.csrf_hash)


def test_local_session_seed_rejects_csrf_hash_token_mismatch() -> None:
    from proofops.adapters.local.auth_store import InMemorySessionStore, hash_token
    from proofops.application.authorization import SessionRecord

    sessions = InMemorySessionStore()
    with pytest.raises(ValueError, match="CSRF hash/token mismatch"):
        sessions.put_with_token(
            SessionRecord(
                "mismatched-seed",
                USER_A,
                None,
                hash_token("expected-token"),
                10_000.0,
                10_000.0,
                False,
            ),
            "different-token",
        )


@pytest.mark.parametrize("deadline", [float("nan"), float("inf")])
def test_session_record_rejects_nonfinite_deadlines(deadline: float) -> None:
    from proofops.application.authorization import SessionRecord

    with pytest.raises(ValueError, match="finite timestamp"):
        SessionRecord("sid", USER_A, None, "csrf-hash", deadline, 10_000.0, False)


def test_authorize_rejects_nonfinite_explicit_now() -> None:
    from proofops.application.authorization import authorize

    sessions, memberships, sid_a = _build_stores()
    with pytest.raises(ValueError, match="now must be a finite timestamp"):
        authorize(
            session_id=sid_a,
            now=float("nan"),
            session_port=sessions,
            membership_port=memberships,
        )


def test_membership_record_rejects_unknown_role() -> None:
    from proofops.application.authorization import MembershipRecord

    with pytest.raises(ValueError, match="unknown role"):
        MembershipRecord(TENANT_A, USER_A, "owner", "active")  # type: ignore[arg-type]


def test_authorize_returns_context_for_live_membership() -> None:
    from proofops.application.authorization import authorize

    sessions, memberships, sid_a = _build_stores()
    ctx = authorize(
        session_id=sid_a,
        now=0.0,
        session_port=sessions,
        membership_port=memberships,
        required_capability="editor",
    )
    assert ctx.user_sub == USER_A
    assert ctx.tenant_id == TENANT_A
    assert ctx.role == "editor"
    assert "editor" in ctx.capabilities
    assert "viewer" in ctx.capabilities
    assert "reviewer" not in ctx.capabilities  # editor/reviewer are siblings


def test_authorize_rejects_expired_session() -> None:
    from proofops.application.authorization import SessionExpiredError, authorize

    sessions, memberships, sid_a = _build_stores()
    with pytest.raises(SessionExpiredError):
        authorize(
            session_id=sid_a,
            now=999_999.0,  # past expires_at
            session_port=sessions,
            membership_port=memberships,
        )


def test_authorize_rejects_missing_session() -> None:
    from proofops.application.authorization import SessionExpiredError, authorize

    sessions, memberships, _sid_a = _build_stores()
    with pytest.raises(SessionExpiredError):
        authorize(
            session_id="does-not-exist",
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
        )


def test_authorize_rejects_revoked_session_immediately() -> None:
    from proofops.adapters.local.auth_store import InMemorySessionStore
    from proofops.application.authorization import SessionExpiredError, authorize

    sessions, memberships, sid_a = _build_stores()
    assert isinstance(sessions, InMemorySessionStore)
    sessions.revoke(sid_a)
    with pytest.raises(SessionExpiredError):
        authorize(
            session_id=sid_a,
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
        )


def test_authorize_no_active_tenant_is_capability_denied() -> None:
    """A valid session without an active tenant cannot pass a tenant-scoped
    capability check: this must be the same failure family the API maps to
    403, never the same shape as an unauthenticated request."""
    from proofops.adapters.local.auth_store import InMemorySessionStore
    from proofops.application.authorization import (
        CapabilityDeniedError,
        SessionRecord,
        authorize,
    )

    sessions, memberships, _sid_a = _build_stores()
    sid_no_tenant = "session-no-tenant"
    assert isinstance(sessions, InMemorySessionStore)
    sessions.put(
        SessionRecord(
            session_id=sid_no_tenant,
            user_sub=USER_A,
            active_tenant_id=None,
            csrf_hash="csrf-x",
            expires_at=10_000.0,
            idle_deadline=10_000.0,
            revoked=False,
        )
    )
    with pytest.raises(CapabilityDeniedError):
        authorize(
            session_id=sid_no_tenant,
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
            required_capability="viewer",
        )


def test_authorize_cross_tenant_request_is_tenant_not_found() -> None:
    """User A's session is active in tenant A; requesting tenant B by id
    must raise the SAME error as a nonexistent tenant (AT-037)."""
    from proofops.application.authorization import TenantNotFoundError, authorize

    sessions, memberships, sid_a = _build_stores()
    with pytest.raises(TenantNotFoundError):
        authorize(
            session_id=sid_a,
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
            requested_tenant_id=TENANT_B,
        )


def test_authorize_nonexistent_tenant_raises_identical_error_type() -> None:
    from proofops.application.authorization import TenantNotFoundError, authorize

    sessions, memberships, sid_a = _build_stores()
    with pytest.raises(TenantNotFoundError):
        authorize(
            session_id=sid_a,
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
            requested_tenant_id="99999999-9999-4999-8999-999999999999",
        )


def test_authorize_rejects_capability_not_in_role() -> None:
    from proofops.application.authorization import CapabilityDeniedError, authorize

    sessions, memberships, sid_a = _build_stores()  # USER_A is "editor" in tenant A
    with pytest.raises(CapabilityDeniedError):
        authorize(
            session_id=sid_a,
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
            required_capability="reviewer",  # editor does not imply reviewer
        )


def test_select_tenant_switches_active_tenant_on_live_membership() -> None:
    from proofops.adapters.local.auth_store import InMemoryMembershipStore
    from proofops.application.authorization import MembershipRecord, select_tenant

    sessions, memberships, sid_a = _build_stores()
    assert isinstance(memberships, InMemoryMembershipStore)
    memberships.put(MembershipRecord(TENANT_B, USER_A, "viewer", "active"))

    ctx = select_tenant(
        session_id=sid_a,
        tenant_id=TENANT_B,
        now=0.0,
        session_port=sessions,
        membership_port=memberships,
    )
    assert ctx.tenant_id == TENANT_B
    assert ctx.role == "viewer"


def test_session_rotation_checks_liveness_atomically_under_store_lock() -> None:
    from proofops.application.authorization import SessionExpiredError

    sessions, _memberships, sid_a = _build_stores()
    barrier = Barrier(2)

    def rotate():
        barrier.wait()
        return sessions.set_active_tenant(sid_a, TENANT_B, now=0.0)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(rotate) for _ in range(2)]
    successes = [future.result() for future in futures if future.exception() is None]
    failures = [future.exception() for future in futures if future.exception() is not None]

    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], SessionExpiredError)
    old = sessions.get(sid_a)
    assert old is not None and old.revoked is True


def test_select_tenant_rejects_revoked_membership_same_as_missing_tenant() -> None:
    from proofops.adapters.local.auth_store import InMemoryMembershipStore
    from proofops.application.authorization import (
        MembershipRecord,
        TenantNotFoundError,
        select_tenant,
    )

    sessions, memberships, sid_a = _build_stores()
    assert isinstance(memberships, InMemoryMembershipStore)
    memberships.put(MembershipRecord(TENANT_B, USER_A, "viewer", "revoked"))

    with pytest.raises(TenantNotFoundError):
        select_tenant(
            session_id=sid_a,
            tenant_id=TENANT_B,
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
        )

    with pytest.raises(TenantNotFoundError):
        select_tenant(
            session_id=sid_a,
            tenant_id="00000000-0000-4000-8000-000000000000",
            now=0.0,
            session_port=sessions,
            membership_port=memberships,
        )


# ---------------------------------------------------------------------------
# HTTP-level tests via FastAPI TestClient (local composition, no network)
# ---------------------------------------------------------------------------


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch):
    from fastapi.testclient import TestClient
    from proofops_api.main import create_app

    monkeypatch.setenv("APP_ORIGIN", "http://testserver")
    app = create_app()
    return TestClient(app)


def _seed(app_state) -> None:
    import time

    from proofops.application.authorization import MembershipRecord, SessionRecord

    far_future = time.time() + 10_000.0
    store = app_state.auth_store
    store.sessions.put_with_token(
        SessionRecord(
            session_id="sid-a",
            user_sub=USER_A,
            active_tenant_id=TENANT_A,
            csrf_hash=store.hash_csrf("csrf-a-token"),
            expires_at=far_future,
            idle_deadline=far_future,
            revoked=False,
        ),
        "csrf-a-token",
    )
    store.sessions.put_with_token(
        SessionRecord(
            session_id="sid-no-tenant",
            user_sub=USER_A,
            active_tenant_id=None,
            csrf_hash=store.hash_csrf("csrf-x-token"),
            expires_at=far_future,
            idle_deadline=far_future,
            revoked=False,
        ),
        "csrf-x-token",
    )
    store.memberships.put(MembershipRecord(TENANT_A, USER_A, "editor", "active"))
    store.memberships.put(MembershipRecord(TENANT_B, USER_B, "admin", "active"))


def test_get_session_requires_cookie(client) -> None:
    resp = client.get("/v1/session")
    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] in ("AUTH_REQUIRED", "SESSION_EXPIRED")


def test_get_session_returns_role_and_tenant(client) -> None:
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "sid-a")
    resp = client.get("/v1/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == TENANT_A
    assert body["role"] == "editor"
    assert body["user_id"] == USER_A
    assert body["csrf_token"]


def test_get_session_without_active_tenant_is_200_with_csrf_for_first_selection(client) -> None:
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "sid-no-tenant")
    resp = client.get("/v1/session")
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] is None
    assert body["role"] is None
    assert body["user_id"] == USER_A
    assert body["csrf_token"]


def test_get_session_expired_cookie_is_401(client) -> None:
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "not-a-real-session")
    resp = client.get("/v1/session")
    assert resp.status_code == 401


def test_tenant_switch_to_inaccessible_tenant_is_404_without_leaking(client) -> None:
    """AT-037: tenant A user requesting tenant B by id gets a 404 that is
    identical (same code/body shape) whether B exists or not."""
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "sid-a")
    csrf = client.get("/v1/session").json()["csrf_token"]

    resp_existing_other_tenant = client.post(
        "/v1/session/tenant",
        json={"tenant_id": TENANT_B},
        headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
    )
    resp_nonexistent_tenant = client.post(
        "/v1/session/tenant",
        json={"tenant_id": "99999999-9999-4999-8999-999999999999"},
        headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
    )

    assert resp_existing_other_tenant.status_code == 404
    assert resp_nonexistent_tenant.status_code == 404
    assert resp_existing_other_tenant.json() == resp_nonexistent_tenant.json() | {
        "error": {
            **resp_nonexistent_tenant.json()["error"],
            "request_id": resp_existing_other_tenant.json()["error"]["request_id"],
        }
    }
    assert resp_existing_other_tenant.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_tenant_switch_success_rotates_session(client) -> None:
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "sid-a")
    csrf = client.get("/v1/session").json()["csrf_token"]

    store = client.app.state.composition.auth_store
    store.memberships.put(
        __import__(
            "proofops.application.authorization", fromlist=["MembershipRecord"]
        ).MembershipRecord(TENANT_B, USER_A, "viewer", "active")
    )

    resp = client.post(
        "/v1/session/tenant",
        json={"tenant_id": TENANT_B},
        headers={"X-CSRF-Token": csrf, "Origin": "http://testserver"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant_id"] == TENANT_B
    assert body["role"] == "viewer"
    # rotation: a new session id must be issued, old cookie value invalidated.
    new_sid = resp.cookies.get("__Host-proofops_session")
    assert new_sid is not None
    assert new_sid != "sid-a"


def test_http_first_tenant_selection_then_reuses_new_cookie_and_csrf(client) -> None:
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "sid-no-tenant")

    first_session = client.get("/v1/session")
    assert first_session.status_code == 200
    first_csrf = first_session.json()["csrf_token"]
    first_switch = client.post(
        "/v1/session/tenant",
        json={"tenant_id": TENANT_A},
        headers={"X-CSRF-Token": first_csrf, "Origin": "http://testserver"},
    )
    assert first_switch.status_code == 200
    new_sid = first_switch.cookies.get("__Host-proofops_session")
    new_csrf = first_switch.json()["csrf_token"]
    assert new_sid and new_sid != "sid-no-tenant"
    assert new_csrf and new_csrf != first_csrf

    client.cookies.set("__Host-proofops_session", new_sid)
    second_switch = client.post(
        "/v1/session/tenant",
        json={"tenant_id": TENANT_A},
        headers={"X-CSRF-Token": new_csrf, "Origin": "http://testserver"},
    )
    assert second_switch.status_code == 200

    client.cookies.set("__Host-proofops_session", "sid-no-tenant")
    assert client.get("/v1/session").status_code == 401


def test_expired_tenant_switch_is_401_before_csrf_check(client) -> None:
    import time

    from proofops.adapters.local.auth_store import hash_token
    from proofops.application.authorization import SessionRecord

    store = client.app.state.composition.auth_store
    expired_at = time.time() - 1.0
    store.sessions.put_with_token(
        SessionRecord(
            "sid-expired",
            USER_A,
            None,
            hash_token("expired-csrf"),
            expired_at,
            expired_at,
            False,
        ),
        "expired-csrf",
    )
    client.cookies.set("__Host-proofops_session", "sid-expired")

    response = client.post(
        "/v1/session/tenant",
        json={"tenant_id": TENANT_A},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_tenant_switch_without_csrf_is_403(client) -> None:
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "sid-a")
    resp = client.post(
        "/v1/session/tenant",
        json={"tenant_id": TENANT_A},
        headers={"Origin": "http://testserver"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_INVALID"


def test_tenant_switch_wrong_origin_is_403(client) -> None:
    _seed(client.app.state.composition)
    client.cookies.set("__Host-proofops_session", "sid-a")
    csrf = client.get("/v1/session").json()["csrf_token"]
    resp = client.post(
        "/v1/session/tenant",
        json={"tenant_id": TENANT_A},
        headers={"X-CSRF-Token": csrf, "Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_INVALID"
