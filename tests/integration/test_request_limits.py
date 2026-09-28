"""Local-only HTTP request control regression tests with synthetic identities."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

ORIGIN = "https://testserver"
TENANT = "11111111-1111-4111-8111-111111111111"
SESSION_COOKIE = "__Host-proofops_session"


@dataclass
class Clock:
    value: float = 1_800_000_000.0

    def __call__(self) -> float:
        return self.value


def _fixture() -> tuple[FastAPI, Any, Any, Clock]:
    from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
    from proofops.application.authorization import MembershipRecord, SessionRecord
    from proofops.application.registry import Registry
    from proofops_api.auth import AuthStore, build_auth_router
    from proofops_api.preflight import build_preflight_router
    from proofops_api.request_limits import LocalRequestLimiter
    from proofops_api.routers.registry import build_registry_router

    clock = Clock()
    sessions = InMemorySessionStore()
    memberships = InMemoryMembershipStore()
    auth = AuthStore(sessions, memberships, LocalRequestLimiter())
    for session_id, user_sub, role in (
        ("session-a", "synthetic-user-a", "editor"),
        ("session-a-2", "synthetic-user-a", "editor"),
        ("session-b", "synthetic-user-b", "editor"),
        ("session-viewer", "synthetic-viewer", "viewer"),
    ):
        sessions.put(
            SessionRecord(
                session_id,
                user_sub,
                TENANT,
                "adapter-replaces-this-hash",
                clock.value + 10_000,
                clock.value + 10_000,
                False,
            )
        )
        memberships.put(MembershipRecord(TENANT, user_sub, role, "active"))

    registry = Registry.empty()
    app = FastAPI()
    app.include_router(build_auth_router(auth, clock=clock, allowed_origin=ORIGIN))
    app.include_router(build_registry_router(registry, auth, allowed_origin=ORIGIN, clock=clock))

    def missing_profile(*_args: object) -> dict[str, object]:
        raise LookupError("synthetic profile is absent")

    app.include_router(
        build_preflight_router(
            auth,
            resolve_profile=missing_profile,
            allowed_regions=(),
            allowed_origin=ORIGIN,
            clock=clock,
        )
    )
    return app, auth, registry, clock


def _client(app: FastAPI, auth: Any, session_id: str) -> TestClient:
    client = TestClient(app, base_url=ORIGIN)
    client.cookies.set(SESSION_COOKIE, session_id)
    csrf = auth.sessions.csrf_token_for(session_id)
    assert csrf is not None
    client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": csrf})
    return client


def _create(client: TestClient, number: int, **headers: str):
    return client.post(
        "/v1/companies",
        json={"legal_name": f"Synthetic company {number}", "aliases": []},
        headers={"Idempotency-Key": f"synthetic-company-{number:04d}", **headers},
    )


def test_concurrent_write_limit_allows_ten_mutations_and_rejects_the_next() -> None:
    app, auth, _registry, _clock = _fixture()

    def create(number: int):
        return _create(_client(app, auth, "session-a"), number)

    with ThreadPoolExecutor(max_workers=11) as pool:
        responses = list(pool.map(create, range(11)))

    assert sorted(response.status_code for response in responses) == [201] * 10 + [429]
    limited = next(response for response in responses if response.status_code == 429)
    assert limited.json()["error"]["code"] == "RATE_LIMITED"
    assert limited.headers["Retry-After"] == "60"
    listing = _client(app, auth, "session-a").get("/v1/companies")
    assert listing.status_code == 200
    assert len(listing.json()["items"]) == 10


def test_window_expiry_and_operation_user_session_isolation() -> None:
    from proofops.application.authorization import MembershipRecord

    app, auth, _registry, clock = _fixture()
    user_a = _client(app, auth, "session-a")
    for number in range(10):
        assert _create(user_a, number).status_code == 201

    assert _create(user_a, 10).status_code == 429
    assert _create(user_a, 10, **{"X-CSRF-Token": "wrong"}).status_code == 403
    auth.memberships.put(MembershipRecord(TENANT, "synthetic-user-a", "viewer", "active"))
    assert _create(user_a, 10).status_code == 403
    auth.memberships.put(MembershipRecord(TENANT, "synthetic-user-a", "admin", "active"))
    unrelated_write = user_a.post(
        "/v1/preflight",
        json={
            "runtime_binding_id": "00000000-0000-4000-8000-000000000001",
            "consent_profile_id": "00000000-0000-4000-8000-000000000002",
            "include_live_model_probe": False,
        },
        headers={"Idempotency-Key": "synthetic-preflight-write"},
    )
    assert unrelated_write.status_code == 404
    assert user_a.get("/v1/runtime-options").status_code == 200

    rotated = user_a.post("/v1/session/tenant", json={"tenant_id": TENANT})
    assert rotated.status_code == 200
    new_session_id = rotated.cookies.get(SESSION_COOKIE)
    assert new_session_id and new_session_id != "session-a"
    user_a.cookies.set(SESSION_COOKIE, new_session_id)
    user_a.headers["X-CSRF-Token"] = rotated.json()["csrf_token"]
    assert _create(user_a, 11).status_code == 429

    user_b = _client(app, auth, "session-b")
    spoofed = _create(
        user_b,
        12,
        **{"X-User-Id": "synthetic-user-a", "X-Forwarded-For": "127.0.0.1"},
    )
    assert spoofed.status_code == 201

    clock.value += 59
    almost = _create(user_a, 13)
    assert almost.status_code == 429
    assert almost.headers["Retry-After"] == "1"
    clock.value += 1
    assert _create(user_a, 14).status_code == 201


def test_auth_csrf_and_role_denials_precede_and_do_not_consume_limits() -> None:
    app, auth, _registry, _clock = _fixture()
    anonymous = TestClient(app, base_url=ORIGIN)
    for _ in range(12):
        response = anonymous.post(
            "/v1/companies",
            json={"legal_name": "Attack", "aliases": []},
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": "forged",
                "X-User-Id": "synthetic-user-a",
                "X-Forwarded-For": "127.0.0.1",
                "Idempotency-Key": "anonymous-attack-key",
            },
        )
        assert response.status_code == 401

    user_a = _client(app, auth, "session-a")
    for number in range(12):
        assert _create(user_a, number, **{"X-CSRF-Token": "wrong"}).status_code == 403
    assert _create(user_a, 100).status_code == 201

    viewer = _client(app, auth, "session-viewer")
    for number in range(12):
        assert _create(viewer, 200 + number).status_code == 403


@pytest.mark.parametrize("path", ["/v1/companies", "/v1/preflight", "/v1/session/tenant"])
def test_json_body_is_bounded_before_parsing_or_mutation(path: str) -> None:
    from proofops.application.authorization import MembershipRecord

    app, auth, _registry, _clock = _fixture()
    auth.memberships.put(MembershipRecord(TENANT, "synthetic-user-a", "admin", "active"))
    client = _client(app, auth, "session-a")
    headers = {"Content-Type": "application/json", "Idempotency-Key": "bounded-body-test"}

    oversized = client.post(
        path,
        content=b'{"legal_name":"' + b"x" * 65_536 + b'","aliases":[]}',
        headers=headers,
    )
    streamed = client.post(
        path,
        content=(chunk for chunk in (b'{"legal_name":"', b"x" * 65_536, b'","aliases":[]}')),
        headers=headers,
    )
    malformed = client.post(path, content=b"{", headers=headers)

    assert oversized.status_code == 413
    assert oversized.json()["error"]["code"] == "PAYLOAD_TOO_LARGE"
    assert streamed.status_code == 413
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _create(client, 1).status_code == 201
    assert len(client.get("/v1/companies").json()["items"]) == 1


def test_session_routes_use_contract_limits_and_rotation_does_not_reset_budget() -> None:
    app, auth, _registry, clock = _fixture()
    client = _client(app, auth, "session-a-2")

    for _ in range(120):
        assert client.get("/v1/session").status_code == 200
    assert client.get("/v1/session").status_code == 429
    clock.value += 60
    assert client.get("/v1/session").status_code == 200

    for _ in range(10):
        switched = client.post("/v1/session/tenant", json={"tenant_id": TENANT})
        assert switched.status_code == 200
        session_id = switched.cookies.get(SESSION_COOKIE)
        assert session_id is not None
        client.headers["X-CSRF-Token"] = switched.json()["csrf_token"]
    current_session_id = session_id
    limited = client.post("/v1/session/tenant", json={"tenant_id": TENANT})
    assert limited.status_code == 429
    assert auth.sessions.get(current_session_id) is not None


def test_preflight_keeps_separate_atomic_tenant_live_probe_ceiling() -> None:
    app, auth, _registry, _clock = _fixture()
    payload = {
        "runtime_binding_id": "00000000-0000-4000-8000-000000000001",
        "consent_profile_id": "00000000-0000-4000-8000-000000000002",
        "include_live_model_probe": True,
    }

    def preflight(session_id: str, key: str, body: dict[str, object] = payload):
        client = _client(app, auth, session_id)
        return client.post("/v1/preflight", json=body, headers={"Idempotency-Key": key})

    assert preflight("session-a", "synthetic-preflight-0001").status_code == 403

    from proofops.application.authorization import MembershipRecord

    auth.memberships.put(MembershipRecord(TENANT, "synthetic-user-a", "admin", "active"))
    auth.memberships.put(MembershipRecord(TENANT, "synthetic-user-b", "admin", "active"))
    assert preflight("session-a", "synthetic-preflight-0002").status_code == 404
    assert preflight("session-a", "synthetic-preflight-0003").status_code == 404
    limited = preflight("session-b", "synthetic-preflight-0004")
    assert limited.status_code == 429
    assert limited.headers["Retry-After"] == "60"
    assert (
        preflight(
            "session-b",
            "synthetic-preflight-0005",
            {**payload, "include_live_model_probe": False},
        ).status_code
        == 404
    )


def test_limiter_fails_closed_at_its_bounded_bucket_capacity() -> None:
    from proofops_api.request_limits import LocalRequestLimiter, RequestLimit

    limiter = LocalRequestLimiter(max_buckets=2)

    def limits(user: str) -> tuple[RequestLimit, ...]:
        return (RequestLimit("user", user, "companies_list", 120),)

    assert limiter.consume(limits("a"), now=0.0) is None
    assert limiter.consume(limits("b"), now=0.0) is None
    assert limiter.consume(limits("c"), now=0.0) == 60
    assert limiter.consume(limits("c"), now=60.0) is None


def test_backward_clock_drift_keeps_retry_after_bounded() -> None:
    from proofops_api.request_limits import LocalRequestLimiter, RequestLimit

    limiter = LocalRequestLimiter()
    limit = (RequestLimit("user", "clock-user", "clock-operation", 1),)

    assert limiter.consume(limit, now=100.0) is None
    assert limiter.consume(limit, now=-10_000.0) == 60


def test_bounded_json_rejects_an_oversized_chunk_before_copying(monkeypatch) -> None:
    import asyncio

    from proofops_api import middleware

    class GuardedBytearray(bytearray):
        def extend(self, chunk: bytes) -> None:
            assert len(self) + len(chunk) <= 4, "oversized chunk was copied before rejection"
            super().extend(chunk)

    class Request:
        headers: dict[str, str] = {}

        async def stream(self):
            yield b"12345"

    monkeypatch.setattr(middleware, "bytearray", GuardedBytearray, raising=False)
    with pytest.raises(middleware.RequestBodyTooLarge):
        asyncio.run(middleware.read_bounded_json(Request(), max_bytes=4))


def test_document_routes_apply_independent_read_and_write_limits(tmp_path, monkeypatch) -> None:
    from proofops_api.routers import documents

    from tests.acceptance.test_upload import http_client, setup_upload

    monkeypatch.setattr(documents, "READ_REQUESTS_PER_MINUTE", 1, raising=False)
    monkeypatch.setattr(documents, "WRITE_REQUESTS_PER_MINUTE", 1, raising=False)
    service, seeded, data, version_body = setup_upload(tmp_path)

    with http_client(service) as client:
        bad_csrf = client.post(
            "/v1/documents",
            json={key: seeded[key] for key in ("company_id", "title", "document_type")},
            headers={"X-CSRF-Token": "wrong", "Idempotency-Key": "bad-csrf-document"},
        )
        assert bad_csrf.status_code == 403

        created = client.post(
            "/v1/documents",
            json={key: seeded[key] for key in ("company_id", "title", "document_type")},
            headers={"Idempotency-Key": "limited-document-0001"},
        )
        limited_create = client.post(
            "/v1/documents",
            json={key: seeded[key] for key in ("company_id", "title", "document_type")},
            headers={"Idempotency-Key": "limited-document-0002"},
        )
        assert created.status_code == 201
        assert limited_create.status_code == 429
        assert (
            service._db.execute(
                "SELECT count(*) FROM upload_records WHERE kind='document'"
            ).fetchone()[0]
            == 2
        )

        document_url = f"/v1/documents/{created.json()['document_id']}"
        assert client.get(document_url).status_code == 200
        assert client.get(document_url).status_code == 429

        ticket = client.post(
            document_url + "/versions",
            json=version_body,
            headers={"Idempotency-Key": "limited-version-0001"},
        )
        assert ticket.status_code == 201
        assert (
            client.post(
                document_url + "/versions",
                json=version_body,
                headers={"Idempotency-Key": "limited-version-0002"},
            ).status_code
            == 429
        )
        assert (
            client.post(
                ticket.json()["post_url"],
                data=ticket.json()["post_fields"],
                files={"file": ("source.pdf", data, "application/pdf")},
            ).status_code
            == 204
        )
        complete_body = {"sha256": version_body["sha256"], "size_bytes": len(data)}
        completed = client.post(
            f"/v1/uploads/{ticket.json()['upload_id']}/complete",
            json=complete_body,
            headers={"Idempotency-Key": "limited-complete-0001"},
        )
        assert completed.status_code == 202
        assert (
            client.post(
                f"/v1/uploads/{ticket.json()['upload_id']}/complete",
                json=complete_body,
                headers={"Idempotency-Key": "limited-complete-0002"},
            ).status_code
            == 429
        )
        version_url = f"/v1/versions/{completed.json()['resource_id']}"
        assert client.get(version_url).status_code == 200
        assert client.get(version_url).status_code == 429
    service.close()


def test_run_routes_apply_independent_operation_limits_after_csrf(tmp_path, monkeypatch) -> None:
    from proofops_api.routers import runs

    from tests.integration.test_run_lifecycle import client, setup

    monkeypatch.setattr(runs, "READ_REQUESTS_PER_MINUTE", 1, raising=False)
    monkeypatch.setattr(runs, "WRITE_REQUESTS_PER_MINUTE", 1, raising=False)
    service, body = setup(tmp_path)
    http, _auth = client(service)

    assert http.post("/v1/runs", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    created = http.post("/v1/runs", json=body)
    assert created.status_code == 202
    assert http.post("/v1/runs", json=body).status_code == 429
    run_url = f"/v1/runs/{created.json()['run_id']}"

    for suffix in ("", "/cost", "/audit"):
        assert http.get(run_url + suffix).status_code == 200
        assert http.get(run_url + suffix).status_code == 429

    action_headers = {"If-Match": '"1"', "Idempotency-Key": "limited-cancel-key"}
    cancelled = http.post(
        run_url + "/cancel", json={"reason": "Cancel limited run"}, headers=action_headers
    )
    assert cancelled.status_code == 202
    assert (
        http.post(
            run_url + "/cancel", json={"reason": "Cancel limited run"}, headers=action_headers
        ).status_code
        == 429
    )
    retry_headers = {"If-Match": '"2"', "Idempotency-Key": "limited-retry-key"}
    assert (
        http.post(
            run_url + "/retry", json={"reason": "Retry cancelled run"}, headers=retry_headers
        ).status_code
        == 409
    )
    assert (
        http.post(
            run_url + "/retry", json={"reason": "Retry cancelled run"}, headers=retry_headers
        ).status_code
        == 429
    )
    assert len(service.audit(TENANT, created.json()["run_id"])["items"]) == 2


def test_source_and_quality_reads_are_limited_and_never_cached(tmp_path, monkeypatch) -> None:
    from proofops_api.middleware import BrowserSecurityHeadersMiddleware
    from proofops_api.routers import sources
    from proofops_api.routers.sources import build_sources_router

    from tests.integration.test_local_parser_runner import runner_setup
    from tests.integration.test_run_lifecycle import client

    monkeypatch.setattr(sources, "READ_REQUESTS_PER_MINUTE", 1, raising=False)
    service, run_id, runner, now, _stream = runner_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    graph = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    source_id = graph.blocks[0].source_id
    http, auth = client(service)
    http.app.include_router(
        build_sources_router(
            service.store, service.uploads, runner.parser, auth, clock=lambda: now[0]
        )
    )
    http.app.add_middleware(BrowserSecurityHeadersMiddleware)

    anonymous = TestClient(http.app, base_url="https://testserver")
    assert anonymous.get(f"/v1/runs/{run_id}/sources/{source_id}").status_code == 401
    source = http.get(f"/v1/runs/{run_id}/sources/{source_id}")
    assert source.status_code == 200
    assert source.headers["Cache-Control"] == "no-store"
    assert http.get(f"/v1/runs/{run_id}/sources/{source_id}").status_code == 429
    quality = http.get(f"/v1/runs/{run_id}/quality")
    assert quality.status_code == 200
    assert quality.headers["Cache-Control"] == "no-store"
    assert http.get(f"/v1/runs/{run_id}/quality").status_code == 429


def test_concurrent_logout_limit_leaves_the_rejected_session_live() -> None:
    from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
    from proofops.application.authorization import SessionRecord
    from proofops_api.auth import AuthStore
    from proofops_api.session import build_session_router

    clock = Clock()
    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    for number in range(11):
        session_id = f"logout-{number}"
        token = f"csrf-{number}"
        auth.sessions.put_with_token(
            SessionRecord(
                session_id,
                "shared-logout-user",
                TENANT,
                auth.hash_csrf(token),
                clock.value + 1000,
                clock.value + 1000,
                False,
            ),
            token,
        )
    app = FastAPI()
    app.include_router(build_session_router(auth, allowed_origin=ORIGIN, clock=clock))

    attacked = _client(app, auth, "logout-0")
    for _ in range(12):
        assert (
            attacked.post("/v1/auth/logout", headers={"X-CSRF-Token": "wrong"}).status_code == 403
        )

    def sign_out(number: int):
        return _client(app, auth, f"logout-{number}").post("/v1/auth/logout")

    with ThreadPoolExecutor(max_workers=11) as pool:
        responses = list(pool.map(sign_out, range(11)))

    assert sorted(response.status_code for response in responses) == [204] * 10 + [429]
    assert sum(not auth.sessions.get(f"logout-{number}").revoked for number in range(11)) == 1


def test_rulepack_limit_follows_admin_and_csrf_and_bounds_json(tmp_path, monkeypatch) -> None:
    from proofops_api import rulepacks

    from tests.acceptance.test_rulepack_api import PACK_A, _client, _files, _headers, _pack

    monkeypatch.setattr(rulepacks, "WRITE_REQUESTS_PER_MINUTE", 2, raising=False)
    client, store, csrf = _client(tmp_path)
    store.add_pack(_pack(), _files())
    url = f"/v1/rule-packs/{PACK_A}/activate"

    oversized = client.post(
        url,
        content=b'{"reason":"' + b"x" * 65_536 + b'"}',
        headers={**_headers(csrf, key="oversized-rulepack"), "Content-Type": "application/json"},
    )
    assert oversized.status_code == 422
    for _ in range(3):
        assert (
            client.post(
                url,
                json={"reason": "invalid csrf is rejected"},
                headers=_headers("wrong", key="invalid-csrf-rulepack"),
            ).status_code
            == 403
        )
    activated = client.post(
        url,
        json={"reason": "activate bounded rulepack"},
        headers=_headers(csrf, key="limited-rulepack-key"),
    )
    assert activated.status_code == 200
    assert (
        client.post(
            url,
            json={"reason": "activate bounded rulepack"},
            headers=_headers(csrf, key="limited-rulepack-key"),
        ).status_code
        == 429
    )
    with sqlite3.connect(store.path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM rulepack_activation_events").fetchone()[0] == 1
        )
