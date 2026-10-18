"""GA1 public trial contracts; local PDFs and synthetic publications, no paid transport."""

import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from proofops_api.hosted import create_hosted_app
from proofops_api.hosted_store import HostedSettings

from tests.test_hosted_public import ORIGIN
from tests.test_hosted_public import settings as base_settings


@pytest.fixture
def settings(tmp_path):
    return replace(
        base_settings.__wrapped__(tmp_path),
        guest_access_until=datetime.now(UTC) + timedelta(days=2),
    )


def guest(app):
    client = TestClient(app, base_url="https://api.example.com")
    response = client.post("/v1/guest/session", headers={"Origin": ORIGIN})
    assert response.status_code == 200, response.text
    body = response.json()
    return client, {"Origin": ORIGIN, "X-CSRF-Token": body["csrf_token"]}, body


def test_guest_discovery_session_isolation_and_cookie(settings):
    app = create_hosted_app(settings)
    client, headers, body = guest(app)
    discovery = client.get("/v1/guest").json()
    assert discovery == dict(
        available=True,
        until=settings.guest_access_until.isoformat(),
        fresh_runs_per_day=2,
        showcase=False,
    )
    assert body["role"] == "guest"
    assert body["guest"] == dict(until=discovery["until"], fresh_runs_today=0, fresh_runs_limit=2)
    assert client.get("/v1/session").json() == body
    assert datetime.fromisoformat(body["expires_at"]).timestamp() <= time.time() + 28800
    response = client.post("/v1/guest/session", headers={"Origin": ORIGIN})
    second = response.json()
    assert second["tenant_id"] != body["tenant_id"] and second["user_id"] != body["user_id"]
    cookie = response.headers["set-cookie"].lower()
    assert all(item in cookie for item in ("secure", "httponly", "samesite=lax", "path=/"))
    assert response.headers["cache-control"] == "no-store"
    assert client.post("/v1/auth/logout", headers=headers).status_code == 403


@pytest.mark.parametrize("until", [None, datetime(2020, 1, 1, tzinfo=UTC)])
def test_guest_closed(settings, until):
    client = TestClient(create_hosted_app(replace(settings, guest_access_until=until)))
    assert client.get("/v1/guest").json()["available"] is False
    response = client.post("/v1/guest/session", headers={"Origin": ORIGIN})
    assert response.status_code == 403
    assert response.json()["code"] == "GUEST_ACCESS_CLOSED"


def test_guest_settings_env_validation(tmp_path):
    env = dict(
        APP_ENV="hosted",
        HOSTED_DATA_DIR=str(tmp_path),
        APP_ORIGIN=ORIGIN,
        HOSTED_SESSION_SECRET="x" * 40,
        HOSTED_EVENT_ID="guest-test",
    )
    defaults = HostedSettings.from_env(env)
    assert defaults.guest_access_until is None
    assert defaults.guest_fresh_runs_per_day == 2
    assert defaults.guest_sessions_per_ip_hour == 30
    assert defaults.guest_uploads_per_day == 60
    assert defaults.showcase_tenant == ""
    configured = HostedSettings.from_env(
        env
        | {
            "HOSTED_GUEST_ACCESS_UNTIL": "2026-10-20T23:59:00+09:00",
            "HOSTED_GUEST_FRESH_RUNS_PER_DAY": "5",
            "HOSTED_GUEST_SESSIONS_PER_IP_HOUR": "40",
            "HOSTED_GUEST_UPLOADS_PER_DAY": "70",
            "HOSTED_SHOWCASE_TENANT": "5f0c2a7e-1b3d-4c8e-9a6f-2d4b8c1e7a90",
        }
    )
    assert configured.guest_access_until.utcoffset() == timedelta(hours=9)
    assert configured.guest_fresh_runs_per_day == 5
    assert configured.guest_sessions_per_ip_hour == 40
    assert configured.guest_uploads_per_day == 70
    for name, value in (
        ("HOSTED_GUEST_ACCESS_UNTIL", "bad"),
        ("HOSTED_GUEST_ACCESS_UNTIL", "2026-10-20T23:59:00"),
        ("HOSTED_GUEST_FRESH_RUNS_PER_DAY", "0"),
        ("HOSTED_GUEST_FRESH_RUNS_PER_DAY", "6"),
        ("HOSTED_GUEST_SESSIONS_PER_IP_HOUR", "0"),
        ("HOSTED_GUEST_UPLOADS_PER_DAY", "0"),
        ("HOSTED_GUEST_UPLOADS_PER_DAY", "-1"),
        ("HOSTED_GUEST_UPLOADS_PER_DAY", "bad"),
        ("HOSTED_SHOWCASE_TENANT", "../private"),
    ):
        with pytest.raises(ValueError):
            HostedSettings.from_env(env | {name: value})


def guest_actor(store, client):
    from proofops.application.authorization import authorize
    from proofops_api.auth import SESSION_COOKIE_NAME

    return authorize(
        session_id=client.cookies.get(SESSION_COOKIE_NAME),
        now=time.time(),
        session_port=store.sessions,
        membership_port=store.memberships,
        required_capability="editor",
    )


def upload(client, headers, key="ga1-http-upload-001"):
    from tests.test_hosted_public import pdf

    response = client.post(
        "/v1/documents",
        content=pdf(),
        headers=headers | {"Content-Type": "application/pdf", "Idempotency-Key": key},
    )
    assert response.status_code == 201, response.text
    return response.json()


def run_request(client, headers, doc, key="ga1-http-run-0001", **extra):
    return client.post(
        "/v1/runs",
        json=dict(document_id=doc["document_id"], selected_pages=[1]) | extra,
        headers=headers | {"Idempotency-Key": key},
    )


def test_guest_origin_csrf_roles_and_session_expiry(settings):
    from proofops_api.auth import SESSION_COOKIE_NAME

    settings = replace(settings, guest_access_until=datetime.now(UTC) + timedelta(minutes=5))
    app = create_hosted_app(settings)
    anonymous = TestClient(app, base_url="https://api.example.com")
    for origin in (None, "https://untrusted.example", "null"):
        response = anonymous.post("/v1/guest/session", headers={"Origin": origin} if origin else {})
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "CSRF_INVALID"
        assert "set-cookie" not in response.headers
    client, headers, body = guest(app)
    store = app.state.composition
    auth = guest_actor(store, client)
    assert not auth.has_capability("reviewer") and not auth.has_capability("admin")
    assert datetime.fromisoformat(body["expires_at"]).timestamp() <= (
        settings.guest_access_until.timestamp()
    )
    for bad in ({"Origin": ORIGIN}, headers | {"Origin": "https://untrusted.example"}):
        response = client.post("/v1/documents", content=b"pdf", headers=bad)
        assert response.status_code == 403 and response.json()["error"]["code"] == "CSRF_INVALID"
    response = client.post(
        "/v1/session/tenant", json={"tenant_id": body["tenant_id"]}, headers=headers
    )
    assert response.status_code == 403
    with pytest.raises(ValueError):
        store.invite(body["tenant_id"], body["user_id"], "admin", now=time.time())
    disabled = TestClient(
        create_hosted_app(replace(settings, guest_access_until=None)),
        base_url="https://api.example.com",
    )
    disabled.cookies.update(client.cookies)
    assert disabled.get("/v1/session").status_code == 401
    with store.transaction() as db:
        db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
    assert client.get("/v1/session").status_code == 401
    assert store.sessions.get(client.cookies.get(SESSION_COOKIE_NAME))


def test_guest_a_cannot_read_or_write_b_and_can_manage_own(settings):
    from uuid import uuid4

    from tests.test_hosted_rc1 import seal

    app = create_hosted_app(settings)
    store = app.state.composition
    first, first_headers, _ = guest(app)
    second, second_headers, _ = guest(app)
    own = upload(first, first_headers)
    other = upload(second, second_headers)
    response = run_request(second, second_headers, other)
    assert response.status_code == 202, response.text
    run = response.json()
    seal(store, guest_actor(store, second), run)
    doc_path, run_path = f'/v1/documents/{other["document_id"]}', run["status_url"]
    for route in (
        doc_path,
        doc_path + "/original",
        doc_path + "/scope-plan",
        run_path,
        run_path + "/result",
        run_path + "/source-pages/1/manifest",
        run_path + "/source-pages/1/image",
        f'/v1/runs?document_id={other["document_id"]}',
    ):
        assert first.get(route).status_code == 404, route
    assert [d["document_id"] for d in first.get("/v1/documents").json()["items"]] == [
        own["document_id"]
    ]
    assert first.get("/v1/runs").json()["items"] == []
    assert run_request(first, first_headers, other).status_code == 404
    for route, body in (
        (doc_path + "/scope-plan", None),
        (doc_path + "/deletion-requests", {"reason": "delete other document"}),
        (run_path + "/cancel", None),
    ):
        response = first.post(
            route, json=body, headers=first_headers | {"Idempotency-Key": "ga1-denied-key-001"}
        )
        assert response.status_code in (403, 404), response.text
    # A fresh client avoids conflating per-minute request limits with permissions.
    assert run_request(second, second_headers, other, force_fresh=True).status_code == 403
    assert (
        second.post(
            run_path + f"/reviews/{uuid4()}/resolve", json={}, headers=second_headers
        ).status_code
        == 403
    )
    assert second.get(run_path + "/result").status_code == 200
    assert second.post(run_path + "/cancel", headers=second_headers).status_code == 200
    response = second.post(
        doc_path + "/deletion-requests",
        json={"reason": "remove own document"},
        headers=second_headers | {"Idempotency-Key": "ga1-delete-own-001"},
    )
    assert response.status_code == 202 and response.json()["status"] == "completed"
    assert second.get(doc_path).status_code == 404


def test_guest_own_scope_plan_and_selection(settings):
    app = create_hosted_app(settings)
    client, headers, _ = guest(app)
    doc = upload(client, headers)
    route = f'/v1/documents/{doc["document_id"]}/scope-plan'
    created = client.post(route, headers=headers)
    assert created.status_code == 200, created.text
    assert client.get(route).json() == created.json()
    edited = client.post(
        route + "/selection",
        headers=headers | {"If-Match": created.headers["etag"]},
        json={
            "selected_claim_pages": [1],
            "selected_evidence_pages": [1],
            "manual_selection": True,
        },
    )
    assert edited.status_code == 200, edited.text


def test_guest_daily_limit_replay_restart_delete_and_next_day(settings):
    from proofops_api.hosted_store import HostedError, HostedStore

    app = create_hosted_app(settings)
    client, headers, _ = guest(app)
    store = app.state.composition
    auth = guest_actor(store, client)
    doc = upload(client, headers)
    first = run_request(client, headers, doc)
    second = run_request(client, headers, doc, "ga1-http-run-0002")
    assert first.status_code == second.status_code == 202
    assert client.get("/v1/session").json()["guest"]["fresh_runs_today"] == 2
    replay = run_request(client, headers, doc)
    assert replay.status_code == 202 and replay.json()["run_id"] == first.json()["run_id"]
    denied = run_request(client, headers, doc, "ga1-http-run-0003")
    assert denied.status_code == 429 and denied.json()["code"] == "GUEST_DAILY_LIMIT"
    restarted = HostedStore(settings)
    restarted.delete_document(auth, doc["document_id"], "ga1-delete-limit-001", now=time.time())
    from tests.test_hosted_public import pdf

    new = restarted.upload(auth, pdf(), "ga1-new-doc-000001", now=time.time())
    with pytest.raises(HostedError, match="GUEST_DAILY_LIMIT"):
        restarted.create_run(auth, new["document_id"], [1], "ga1-new-run-000001", now=time.time())
    assert (
        restarted.create_run(
            auth, new["document_id"], [1], "ga1-next-day-000001", now=time.time() + 86400
        )["status"]
        == "queued"
    )


def test_guest_daily_limit_does_not_charge_reuse(settings, tmp_path):
    import json

    from proofops_api.hosted_analysis import default_analysis_profile

    from tests.test_hosted_public import actor
    from tests.test_hosted_rc1 import create, seal

    config = tmp_path / "operator.json"
    config.write_text(
        json.dumps(dict(report_year=2025, period_start="2025-01-01", period_end="2025-12-31"))
    )
    app = create_hosted_app(
        replace(settings, rc1_reuse_enabled=True, live_config=config, guest_fresh_runs_per_day=1)
    )
    store = app.state.composition
    owner = actor(store)
    source = create(store, owner)
    source_path = seal(store, owner, source)
    before_source = (source_path / "result-1.json").read_bytes()
    client, headers, _ = guest(app)
    doc = upload(client, headers)
    fresh = run_request(client, headers | {"X-Pipeline-Profile": "unattended-v1"}, doc)
    assert fresh.status_code == 202 and fresh.json()["status"] == "queued"
    before_budget = store.budget_totals()
    reused = run_request(
        client,
        headers | {"X-Pipeline-Profile": default_analysis_profile()},
        doc,
        "ga1-reuse-run-0001",
    )
    assert reused.status_code == 202, reused.text
    run = reused.json()
    assert run["status"] == "completed" and run["result"]["reuse"]["reused"]
    assert run["result"]["reuse"]["provider_calls"] == 0
    assert store.budget_totals() == before_budget
    assert client.get("/v1/session").json()["guest"]["fresh_runs_today"] == 1
    assert client.get(run["result_url"]).status_code == 200
    assert client.get(source["status_url"]).status_code == 404
    assert (source_path / "result-1.json").read_bytes() == before_source
    with store.transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM deletion.costs").fetchone()[0] == 0
        assert {r[0] for r in db.execute("SELECT event FROM guest_audit")} >= {
            "session",
            "upload",
            "run",
        }


def test_guest_fresh_limit_is_atomic(settings):
    from concurrent.futures import ThreadPoolExecutor

    from proofops_api.hosted_store import HostedError

    from tests.test_hosted_public import pdf

    app = create_hosted_app(replace(settings, guest_fresh_runs_per_day=1))
    client, _, _ = guest(app)
    store = app.state.composition
    auth = guest_actor(store, client)
    doc = store.upload(auth, pdf(), "ga1-race-upload-001", now=time.time())

    def create(i):
        try:
            return store.create_run(
                auth, doc["document_id"], [1], f"ga1-race-run-{i:06}", now=time.time()
            )["status"]
        except HostedError as exc:
            return exc.code

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(create, range(4)))
    assert results.count("queued") == 1 and results.count("GUEST_DAILY_LIMIT") == 3


def test_guest_upload_limit_atomic_replay_and_deletion(settings):
    from concurrent.futures import ThreadPoolExecutor

    from proofops_api.hosted_store import HostedError, HostedStore

    from tests.test_hosted_public import pdf

    app = create_hosted_app(settings)
    client, _, _ = guest(app)
    store = app.state.composition
    auth = guest_actor(store, client)
    content = pdf()
    first = store.upload(auth, content, "ga1-upload-limit-000", now=time.time())

    def create(i):
        try:
            return store.upload(auth, content, f"ga1-upload-limit-{i:03}", now=time.time())
        except HostedError as exc:
            return exc.code

    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(create, range(1, 7)))
    assert sum(isinstance(r, dict) for r in results) == 4
    assert results.count("GUEST_UPLOAD_LIMIT") == 2
    restarted = HostedStore(settings)
    assert restarted.upload(auth, content, "ga1-upload-limit-000", now=time.time()) == first
    restarted.delete_document(auth, first["document_id"], "ga1-upload-delete-001", now=time.time())
    with pytest.raises(HostedError, match="GUEST_UPLOAD_LIMIT"):
        restarted.upload(auth, content, "ga1-upload-after-delete", now=time.time())
    assert restarted.upload(auth, content, "ga1-upload-tomorrow", now=time.time() + 86400)


def test_guest_ip_limit_is_sliding_persistent_atomic_and_private(settings):
    from concurrent.futures import ThreadPoolExecutor

    from proofops_api.hosted_store import HostedError, HostedStore

    store = HostedStore(replace(settings, guest_sessions_per_ip_hour=2))
    now = time.time()

    def create(_):
        try:
            return store.guest.login(ip="192.0.2.12", now=now)
        except HostedError as exc:
            return exc.code

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(create, range(4)))
    assert results.count("GUEST_SESSION_RATE_LIMIT") == 2
    cold = HostedStore(store.settings)
    with pytest.raises(HostedError, match="GUEST_SESSION_RATE_LIMIT"):
        cold.guest.login(ip="192.0.2.12", now=now + 3599)
    assert cold.guest.login(ip="192.0.2.12", now=now + 3600)
    assert cold.guest.login(ip="192.0.2.13", now=now)
    with cold.transaction(readonly=True) as db:
        serialized = str([tuple(row) for row in db.execute("SELECT * FROM guest_session_starts")])
        assert "192.0.2." not in serialized
        assert all(row[0] == "guest" for row in db.execute("SELECT role FROM guest_audit"))


@pytest.mark.parametrize("trusted", [False, True])
def test_guest_http_ip_limit_obeys_proxy_boundary(settings, trusted):
    app = create_hosted_app(
        replace(
            settings,
            guest_sessions_per_ip_hour=1,
            auth_trusted_proxies=("127.0.0.1",) if trusted else (),
        )
    )
    client = TestClient(app, base_url="https://api.example.com", client=("127.0.0.1", 12345))

    def start(forwarded):
        return client.post(
            "/v1/guest/session", headers={"Origin": ORIGIN, "X-Forwarded-For": forwarded}
        )

    assert start("192.0.2.1").status_code == 200
    # Spoofing the leftmost hop never changes the trusted rightmost peer.
    limited = start("192.0.2.200, 192.0.2.1")
    assert limited.status_code == 429
    assert limited.json()["code"] == "GUEST_SESSION_RATE_LIMIT"
    assert int(limited.headers["retry-after"]) > 0
    assert start("192.0.2.2").status_code == (200 if trusted else 429)


def test_guest_runtime_cost_and_retention_caps_unchanged(settings):
    from decimal import Decimal

    from proofops_api.hosted_store import HostedError

    configured = replace(
        settings,
        run_usd=Decimal("1"),
        day_usd=Decimal("15"),
        event_usd=Decimal("30"),
        event_usd_ceiling=Decimal("30"),
    )
    client = TestClient(create_hosted_app(configured))
    limits = client.get("/v1/runtime").json()["limits"]
    assert (limits["run_usd"], limits["day_usd"], limits["event_usd"], limits["original_days"]) == (
        "1",
        "15",
        "30",
        7,
    )
    for change in (
        dict(run_usd=Decimal("1.01")),
        dict(day_usd=Decimal("15.01")),
        dict(event_usd=Decimal("30.01")),
        dict(active_days=8),
    ):
        with pytest.raises(ValueError):
            replace(configured, **change)
    app = create_hosted_app(replace(settings, run_usd=Decimal("1"), day_usd=Decimal("1")))
    first, h1, _ = guest(app)
    second, h2, _ = guest(app)
    assert run_request(first, h1, upload(first, h1)).status_code == 202
    blocked = run_request(second, h2, upload(second, h2))
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "BUDGET_EXCEEDED"
    store = app.state.composition
    job = store.claim(now=time.time())
    with pytest.raises(HostedError, match="BUDGET_EXCEEDED"):
        store.dispatch(
            job,
            "openai",
            "ga1-over-budget",
            "a" * 64,
            Decimal("1.01"),
            lambda _: pytest.fail("paid dispatch forbidden"),
            now=time.time(),
        )


def test_guest_original_expires_and_audit_backup_is_private(settings, tmp_path):
    import sqlite3

    app = create_hosted_app(settings)
    client, headers, _ = guest(app)
    store = app.state.composition
    auth = guest_actor(store, client)
    doc = upload(client, headers)
    assert doc["expires"] - doc["created"] == 7 * 86400
    backup = store.backup(tmp_path / "backup", now=time.time())
    with sqlite3.connect(backup / "control.sqlite3") as db:
        for table in ("sessions", "memberships", "guest_audit", "guest_session_starts"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    store.maintain(now=doc["expires"] + 1)
    assert not store.document_dir(auth.tenant_id, doc["document_id"]).exists()
    assert client.get("/v1/session").status_code == 401


@pytest.fixture(scope="module")
def publication(tmp_path_factory):
    import json
    import os
    import socket
    import sys
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from types import SimpleNamespace

    from proofops_api import hosted_analysis
    from proofops_api.hosted_analysis import default_analysis_profile

    from deploy.public.offline_fixture import (
        async_parse_replies,
        fake_llm,
        fake_page_ocr,
        source_pdf,
    )
    from evaluation.hosted_r108 import drive
    from tests.test_hosted_public import actor

    settings = base_settings.__wrapped__(tmp_path_factory.mktemp("ga1-publication"))
    app = create_hosted_app(settings)
    store = app.state.composition
    owner = actor(store)
    doc = store.upload(owner, source_pdf(), "ga1-source-upload-001", now=time.time())
    store.create_run(
        owner,
        doc["document_id"],
        [2],
        "ga1-source-run-000001",
        now=time.time(),
        auto_scope=True,
        pipeline_profile=default_analysis_profile(),
    )

    def analyze(store, job, prepared):
        with TemporaryDirectory() as scratch, async_parse_replies() as parse_post:
            workspace = Path(scratch)
            (workspace / "selected.pdf").write_bytes(prepared.read_bytes())
            state, outcome = drive(
                store,
                job,
                workspace,
                dict(report_year=2025, period_start="2025-01-01", period_end="2025-12-31"),
                llm_post=fake_llm,
                parse_post=parse_post,
                ocr_post=fake_page_ocr,
            )
            store.publish_analysis(job, state, outcome["status"], outcome["reason"])

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "environ", os.environ.copy())
        patch.setenv("PROOFOPS_RB3_HARD_DEADLINE", str(time.monotonic() + 1800))
        patch.setattr(
            hosted_analysis, "sys", SimpleNamespace(platform="linux", executable=sys.executable)
        )
        patch.setattr(socket, "create_connection", lambda *a, **k: pytest.fail("no network"))
        done = store.tick(now=time.time(), analysis=analyze)
    assert done["status"] == "completed", done
    path = store.document_dir(owner.tenant_id, doc["document_id"]) / "analysis" / done["run_id"]
    assert json.loads((path / "pilot.json").read_text())["analysis_policy_sha256"] == (
        "f29385d0c74d1520350deb06eaa9e11b72c5829dfaefeb2417c4e8ac0fed4ebd"
    )
    return settings, owner, done, store.get_result(owner.tenant_id, done["run_id"])


@pytest.fixture
def showcase(publication, tmp_path):
    import shutil

    from proofops_api.hosted_store import HostedStore

    from tests.test_hosted_public import pdf

    original_settings, owner, done, result = publication
    root = tmp_path / "showcase-copy"
    shutil.copytree(original_settings.root, root)
    settings = replace(
        original_settings,
        root=root,
        access_config=root / "access.json",
        guest_access_until=datetime.now(UTC) + timedelta(days=2),
    )
    store = HostedStore(settings)
    extra = store.upload(owner, pdf(), "ga1-showcase-extra-doc", now=time.time())
    queued = store.create_run(
        owner, extra["document_id"], [1], "ga1-showcase-queued", now=time.time()
    )
    expired = store.upload(owner, pdf(), "ga1-showcase-expired", now=time.time() - 8 * 86400)
    assert done["status"] == "completed", done["status"]
    app = create_hosted_app(replace(settings, showcase_tenant=owner.tenant_id))
    return app, app.state.composition, owner, done, result, queued, expired


def showcase_client(app, store, role, *, same_tenant=False):
    from uuid import uuid4

    if role == "guest":
        client, headers, _ = guest(app)
        return client, headers
    tenant = store.settings.showcase_tenant if same_tenant else str(uuid4())
    password = "ga1-disposable-password"
    store.access.set_password(
        role, password, tenant=tenant, expires=time.time() + 3600, now=time.time()
    )
    client = TestClient(app, base_url="https://api.example.com")
    response = client.post(
        "/v1/auth/access-password",
        json={"role": role, "password": password},
        headers={"Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.json()["role"] == role
    return client, {"Origin": ORIGIN, "X-CSRF-Token": response.json()["csrf_token"]}


@pytest.mark.parametrize("role", ["guest", "organizer", "admin"])
def test_showcase_all_roles_collection_result_original_and_page_links(showcase, role):
    from tests.test_pa6_reads import route

    app, store, owner, done, result, queued, expired = showcase
    client, _ = showcase_client(app, store, role)
    assert TestClient(app).get("/v1/showcase/runs").status_code == 401
    assert TestClient(app).get(done["result_url"]).status_code == 401
    assert client.get("/v1/guest").json()["showcase"] is True
    budget = store.budget_totals()
    first = client.get("/v1/showcase/documents?limit=1").json()
    assert first["next_cursor"]
    second = client.get("/v1/showcase/documents", params={"cursor": first["next_cursor"]}).json()
    ids = {row["document_id"] for row in first["items"] + second["items"]}
    assert ids == {done["document_id"], queued["document_id"]}
    assert expired["document_id"] not in ids
    assert client.get("/v1/documents", params={"cursor": first["next_cursor"]}).status_code == 400
    listed = client.get("/v1/showcase/runs").json()
    assert [r["run_id"] for r in listed["items"]] == [done["run_id"]]
    assert listed["items"][0]["result_url"] == done["result_url"]
    assert client.get(queued["status_url"]).status_code == 404
    assert client.get(queued["status_url"] + "/result").status_code == 404
    viewed = client.get(done["result_url"] + "?w18=true")
    assert viewed.status_code == 200, viewed.text
    assert viewed.json()["claims"]
    assert client.get(done["status_url"]).status_code == 200
    doc = client.get(f'/v1/documents/{done["document_id"]}')
    assert doc.status_code == 200
    pdf_route = viewed.json()["w16_source_document"]["url"]
    original = store.document_dir(owner.tenant_id, done["document_id"]) / "original.pdf"
    response = client.get(pdf_route)
    assert response.status_code == 200 and response.content == original.read_bytes()
    assert client.get(pdf_route, headers={"Range": "bytes=0-4"}).content == b"%PDF-"
    manifest = client.get(route(done))
    assert manifest.status_code == 200, manifest.text
    png = client.get(manifest.json()["image_url"])
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
    assert client.get(route(done, "image") + "?image_sha256=" + "0" * 64).status_code == 409
    assert store.budget_totals() == budget
    # Normal inventory stays personal even with showcase configured.
    assert client.get("/v1/documents").json()["items"] == []
    assert client.get("/v1/runs").json()["items"] == []


@pytest.mark.parametrize(
    "role,same_tenant",
    [
        ("guest", False),
        ("organizer", False),
        ("admin", False),
    ],
)
def test_showcase_writes_forbidden_for_other_tenants(showcase, role, same_tenant):
    from uuid import uuid4

    app, store, _, done, _, _, _ = showcase
    client, headers = showcase_client(app, store, role, same_tenant=same_tenant)
    doc_path, run_path = f'/v1/documents/{done["document_id"]}', done["status_url"]
    for route, body in (
        (doc_path + "/scope-plan", None),
        (
            doc_path + "/scope-plan/selection",
            {"selected_claim_pages": [1], "selected_evidence_pages": [1]},
        ),
        (doc_path + "/deletion-requests", {"reason": "attempt showcase deletion"}),
        (run_path + "/cancel", None),
        (run_path + f"/reviews/{uuid4()}/resolve", {}),
        ("/v1/runs", {"document_id": done["document_id"], "selected_pages": [1]}),
    ):
        response = client.post(
            route,
            json=body,
            headers=headers | {"Idempotency-Key": "ga1-showcase-write", "If-Match": '"1"'},
        )
        assert response.status_code in (403, 404), (route, response.text)
    assert client.get(run_path).json()["status"] == "completed"
    assert client.get(doc_path + "/original").status_code == 200


@pytest.mark.parametrize("role", ["organizer", "admin"])
def test_showcase_source_tenant_members_keep_normal_write_paths(showcase, role):
    """The showcase tenant is the operators' own workspace; only other tenants are read-only."""
    app, store, _, done, _, _, _ = showcase
    client, headers = showcase_client(app, store, role, same_tenant=True)
    doc_path = f'/v1/documents/{done["document_id"]}'
    for route, body in (
        (doc_path + "/scope-plan", None),
        ("/v1/runs", {"document_id": done["document_id"], "selected_pages": [1]}),
    ):
        response = client.post(
            route,
            json=body,
            headers=headers | {"Idempotency-Key": "ga1-showcase-owner-write", "If-Match": '"1"'},
        )
        assert "SHOWCASE_READ_ONLY" not in response.text, (route, response.text)


def test_showcase_disabled_and_foreign_document_filter(settings):
    from uuid import uuid4

    app = create_hosted_app(settings)
    client, _, _ = guest(app)
    for path in ("/v1/showcase/documents", "/v1/showcase/runs"):
        assert client.get(path).json() == {"items": [], "next_cursor": None}
    configured = create_hosted_app(replace(settings, showcase_tenant=str(uuid4())))
    other, headers, _ = guest(configured)
    own = upload(other, headers)
    response = other.get("/v1/showcase/runs", params={"document_id": own["document_id"]})
    assert response.status_code == 404


@pytest.mark.parametrize("route_kind", ["original", "image"])
def test_showcase_post_io_revocation_returns_no_source(showcase, monkeypatch, route_kind):
    from proofops_api import hosted_reads, hosted_source_view

    from tests.test_pa6_reads import route

    app, store, _, done, _, _, _ = showcase
    client, _, _ = guest(app)
    auth = guest_actor(store, client)
    module = hosted_source_view if route_kind == "original" else hosted_reads
    name = "copy_verified_file" if route_kind == "original" else "render"
    original = getattr(module, name)

    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        store.sessions.revoke(auth.session_id)
        return result

    monkeypatch.setattr(module, name, revoke)
    target = (
        f'/v1/documents/{done["document_id"]}/original'
        if route_kind == "original"
        else route(done, "image")
    )
    response = client.get(target)
    assert response.status_code == 401, response.text
    assert not response.content.startswith((b"%PDF", b"\x89PNG"))


def test_showcase_tamper_and_expiration_keep_source_checks(showcase):
    app, store, owner, done, _, _, _ = showcase
    client, _, _ = guest(app)
    original = store.document_dir(owner.tenant_id, done["document_id"]) / "original.pdf"
    original.chmod(0o600)
    original.write_bytes(b"tampered")
    response = client.get(f'/v1/documents/{done["document_id"]}/original')
    assert response.status_code == 409
    with store.transaction() as db:
        db.execute("UPDATE documents SET expires=0 WHERE document_id=?", (done["document_id"],))
    assert client.get(done["status_url"]).status_code == 404
    assert client.get("/v1/showcase/runs").json()["items"] == []


@pytest.mark.parametrize("status", ["queued", "running", "partial_blocked", "failed", "cancelled"])
def test_showcase_noncompleted_runs_stay_private(settings, status):
    from tests.test_hosted_public import actor, pdf

    app = create_hosted_app(settings)
    store = app.state.composition
    owner = actor(store)
    doc = store.upload(owner, pdf(), "ga1-not-public-upload", now=time.time())
    run = store.create_run(owner, doc["document_id"], [1], "ga1-not-public-run", now=time.time())
    with store.transaction() as db:
        db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run["run_id"]))
    public = create_hosted_app(replace(settings, showcase_tenant=owner.tenant_id))
    client, _, _ = guest(public)
    assert client.get("/v1/showcase/runs").json()["items"] == []
    for suffix in ("", "/result", "/source-pages/1/manifest", "/source-pages/1/image"):
        assert client.get(run["status_url"] + suffix).status_code == 404


def test_guest_rejected_pdf_and_closed_window_cannot_bypass_guards(settings):
    app = create_hosted_app(settings)
    client, headers, _ = guest(app)
    response = client.post(
        "/v1/documents",
        content=b"not a PDF",
        headers=headers
        | {
            "Content-Type": "application/pdf",
            "Idempotency-Key": "ga1-invalid-upload",
        },
    )
    assert response.status_code == 422
    store = app.state.composition
    with store.transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 0
        assert (
            db.execute("SELECT count(*) FROM guest_audit WHERE event='upload'").fetchone()[0] == 0
        )
    expired = TestClient(
        create_hosted_app(
            replace(settings, guest_access_until=datetime.now(UTC) - timedelta(seconds=1))
        ),
        base_url="https://api.example.com",
    )
    expired.cookies.update(client.cookies)
    assert expired.get("/v1/session").status_code == 401
    assert expired.post("/v1/documents", content=b"pdf", headers=headers).status_code == 401


def test_guest_global_upload_limit_http_replay_restart_delete_and_kst(settings):
    from proofops_api.hosted_store import HostedError, HostedStore

    from tests.test_hosted_public import actor, pdf

    settings = replace(settings, guest_uploads_per_day=2)
    app = create_hosted_app(settings)
    first, headers, _ = guest(app)
    second, other_headers, _ = guest(app)
    own = upload(first, headers)
    upload(second, other_headers)
    assert upload(first, headers)["document_id"] == own["document_id"]
    denied = second.post(
        "/v1/documents",
        content=pdf(),
        headers=other_headers
        | {"Content-Type": "application/pdf", "Idempotency-Key": "i35-denied-upload"},
    )
    assert denied.status_code == 429 and denied.json()["code"] == "GUEST_UPLOAD_LIMIT"
    store = HostedStore(settings)
    auth = guest_actor(store, first)
    store.delete_document(auth, own["document_id"], "i35-delete-upload", now=time.time())
    with pytest.raises(HostedError, match="GUEST_UPLOAD_LIMIT"):
        store.upload(auth, pdf(), "i35-after-restart", now=time.time())
    # Non-guests remain outside this quota.
    store.upload(actor(store), pdf(), "i35-admin-upload", now=time.time())
    with store.transaction() as db:
        db.execute("DELETE FROM guest_audit WHERE event='upload'")
    before_midnight = datetime(2026, 10, 18, 14, 59, 59, tzinfo=UTC).timestamp()
    store.upload(auth, pdf(), "i35-before-midnight1", now=before_midnight)
    store.upload(guest_actor(store, second), pdf(), "i35-before-midnight2", now=before_midnight)
    with pytest.raises(HostedError, match="GUEST_UPLOAD_LIMIT"):
        store.upload(auth, pdf(), "i35-before-midnight3", now=before_midnight)
    assert store.upload(auth, pdf(), "i35-after-midnight", now=before_midnight + 1)


def test_guest_global_upload_limit_atomic_across_tenants(settings):
    from concurrent.futures import ThreadPoolExecutor

    from proofops_api.hosted_store import HostedError

    from tests.test_hosted_public import pdf

    app = create_hosted_app(replace(settings, guest_uploads_per_day=1))
    store = app.state.composition
    actors = [guest_actor(store, guest(app)[0]) for _ in range(4)]

    def create(i):
        try:
            store.upload(actors[i], pdf(), f"i35-global-race-{i:04}", now=time.time())
            return "uploaded"
        except HostedError as exc:
            return exc.code

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(create, range(4)))
    assert results.count("uploaded") == 1
    assert results.count("GUEST_UPLOAD_LIMIT") == 3
    with store.transaction(readonly=True) as db:
        assert db.execute("SELECT count(*) FROM documents").fetchone()[0] == 1
        assert (
            db.execute("SELECT count(*) FROM guest_audit WHERE event='upload'").fetchone()[0] == 1
        )
