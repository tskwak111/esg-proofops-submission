"""Real signed-token/HTTP boundary tests; every identity and issuer is synthetic."""

import base64
import hashlib
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.application.authorization import MembershipRecord, SessionRecord
from proofops_api.auth import SESSION_COOKIE_NAME, AuthStore, build_auth_router

ORIGIN = "https://proofops.example.test"
ISSUER = "https://issuer.example.test/pool-fixture"
SUBJECT = "synthetic-oidc-user-never-real"
TENANT = "00000000-0000-4000-8000-000000000037"


@pytest.fixture
def flow():
    import jwt
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives.asymmetric import rsa
    from proofops.adapters.aws.oidc import OIDCConfig, OIDCProvider
    from proofops.adapters.local.oidc_store import InMemoryOIDCStore
    from proofops_api.oidc import build_oidc_router

    now = [time.time()]
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cipher = Fernet(Fernet.generate_key())
    encrypted = {}
    data = {"claims": {}, "headers": {}, "requests": [], "jwks_calls": 0, "key": key}
    config = OIDCConfig(
        issuer=ISSUER,
        client_id="synthetic-client",
        app_origin=ORIGIN,
        authorization_endpoint="https://issuer.example.test/oauth2/authorize",
        token_endpoint="https://issuer.example.test/oauth2/token",
        jwks_uri=ISSUER + "/.well-known/jwks.json",
        redirect_uri=ORIGIN + "/auth/callback",
        return_to_allowlist=("/", "/review"),
        jwks_ttl_seconds=60,
    )

    def transport(request):
        data["requests"].append(request)
        if str(request.url) == config.jwks_uri:
            data["jwks_calls"] += 1
            jwk = jwt.algorithms.RSAAlgorithm.to_jwk(data["key"].public_key(), as_dict=True)
            jwk.update(kid=data.get("kid", "key-1"), use="sig", alg="RS256")
            return httpx.Response(200, json={"keys": [jwk]})
        assert str(request.url) == config.token_endpoint
        assert request.method == "POST"
        fields = parse_qs(request.content.decode())
        assert fields["grant_type"] == ["authorization_code"]
        assert fields["client_id"] == [config.client_id]
        assert fields["redirect_uri"] == [config.redirect_uri]
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(fields["code_verifier"][0].encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == data["authorization"]["code_challenge"][0]
        claims = {
            "sub": SUBJECT,
            "iss": ISSUER,
            "aud": config.client_id,
            "iat": int(now[0]) - 1,
            "exp": int(now[0]) + 300,
            "nonce": data["authorization"]["nonce"][0],
            "token_use": "id",
            "role": "admin",
            "tenant_id": TENANT,
        } | data["claims"]
        token = jwt.encode(
            claims,
            data["key"],
            algorithm="RS256",
            headers={"kid": data.get("kid", "key-1")} | data["headers"],
        )
        if data.get("tamper"):
            pieces = token.split(".")
            pieces[2] = ("A" if pieces[2][0] != "A" else "B") + pieces[2][1:]
            token = ".".join(pieces)
        return httpx.Response(
            200,
            json={
                "id_token": token,
                "refresh_token": "synthetic-refresh-never-real",
                "access_token": "synthetic-unused-access",
                "token_type": "Bearer",
            },
        )

    def save_refresh(session_hash, user_sub, token):
        assert user_sub == SUBJECT
        if data.get("encryption_failure"):
            raise RuntimeError("synthetic-encryption-failure")
        encrypted[session_hash] = cipher.encrypt(token.encode())

    provider = OIDCProvider(
        config,
        transport=httpx.MockTransport(transport),
        save_refresh=save_refresh,
        clock=lambda: now[0],
    )
    store = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    transactions = InMemoryOIDCStore()
    app = FastAPI()
    app.include_router(
        build_oidc_router(
            store, config=config, provider=provider, transactions=transactions, clock=lambda: now[0]
        )
    )
    app.include_router(build_auth_router(store, clock=lambda: now[0], allowed_origin=ORIGIN))
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client, config, data, now, store, encrypted, provider, transactions
    provider.close()


def begin(flow, path="/review"):
    client, _, data, *_ = flow
    response = client.get("/auth/login", params={"return_to": path})
    assert response.status_code == 302
    data["authorization"] = parse_qs(urlsplit(response.headers["location"]).query)
    return response


def callback(flow, code="synthetic-one-use-code"):
    client, _, data, *_ = flow
    return client.get(
        "/auth/callback",
        params={
            "state": data["authorization"]["state"][0],
            "code": code,
        },
    )


def test_success_rotates_session_and_uses_authoritative_membership(flow):
    client, config, data, now, store, encrypted, *_ = flow
    old = SessionRecord(
        "old-synthetic-session", SUBJECT, TENANT, "ignored", now[0] + 100, now[0] + 100, False
    )
    store.sessions.put(old)
    client.cookies.set(
        SESSION_COOKIE_NAME, old.session_id, domain="proofops.example.test", path="/"
    )
    start = begin(flow)
    auth = data["authorization"]
    assert auth["response_type"] == ["code"]
    assert auth["code_challenge_method"] == ["S256"]
    assert auth["scope"] == ["openid"]
    assert auth["redirect_uri"] == [config.redirect_uri]
    assert "code_verifier" not in auth
    response = callback(flow)
    assert response.status_code == 302
    assert response.headers["location"] == "/review"
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(value for value in cookies if value.startswith(SESSION_COOKIE_NAME + "="))
    for attribute in ("Secure", "HttpOnly", "Path=/", "SameSite=lax"):
        assert attribute in session_cookie
        assert attribute in start.headers["set-cookie"]
    assert "Domain=" not in session_cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    sid = client.cookies.get(SESSION_COOKIE_NAME)
    assert sid != old.session_id and len(sid) >= 43
    assert store.sessions.get(old.session_id).revoked
    assert old.revoked is False
    record = store.sessions.get(sid)
    assert record.expires_at == now[0] + 8 * 3600
    assert record.idle_deadline == now[0] + 1800
    session = client.get("/v1/session")
    assert session.status_code == 200
    assert session.json()["user_id"] == SUBJECT
    assert session.json()["tenant_id"] is None and session.json()["role"] is None
    headers = {"Origin": ORIGIN, "X-CSRF-Token": session.json()["csrf_token"]}
    assert (
        client.post("/v1/session/tenant", json={"tenant_id": TENANT}, headers=headers).status_code
        == 404
    )
    store.memberships.put(MembershipRecord(TENANT, SUBJECT, "viewer", "active"))
    selected = client.post("/v1/session/tenant", json={"tenant_id": TENANT}, headers=headers)
    assert selected.status_code == 200 and selected.json()["role"] == "viewer"
    assert encrypted and all(b"synthetic-refresh" not in value for value in encrypted.values())
    assert "synthetic-refresh" not in response.text + str(response.headers)


@pytest.mark.parametrize(
    "claims",
    [
        {"nonce": "wrong"},
        {"aud": "wrong"},
        {"aud": ["synthetic-client", "wrong"]},
        {"iss": ISSUER + "/wrong"},
        {"exp": 1},
        {"token_use": "access"},
        {"sub": ""},
        {"nonce": None},
        {"exp": "9999999999"},
        {"exp": True},
    ],
)
def test_claim_rejections_create_no_session(flow, claims):
    flow[2]["claims"] = claims
    begin(flow)
    response = callback(flow)
    assert response.status_code == 401
    assert flow[0].get("/v1/session").status_code == 401
    assert not flow[5]


def test_signature_tampering_rejected(flow):
    flow[2]["tamper"] = True
    begin(flow)
    assert callback(flow).status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "//evil.test",
        "https://evil.test",
        "https://proofops.example.test/review",
        "/\\evil.test",
        "/%2fevil.test",
        "/review?next=//evil.test",
        "/unknown",
    ],
)
def test_open_redirect_rejected_before_issuer_contact(flow, path):
    response = flow[0].get("/auth/login", params={"return_to": path})
    assert response.status_code == 400
    assert not flow[2]["requests"]


def test_state_and_code_replay_and_browser_binding(flow):
    client = flow[0]
    begin(flow)
    assert callback(flow).status_code == 302
    calls = len(flow[2]["requests"])
    assert callback(flow).status_code == 401
    begin(flow)
    assert callback(flow).status_code == 401  # code under a fresh valid state
    assert len(flow[2]["requests"]) == calls
    begin(flow)
    client.cookies.delete("__Host-proofops_oidc", domain="proofops.example.test", path="/")
    assert callback(flow, "new-code").status_code == 401
    assert len(flow[2]["requests"]) == calls


def test_expired_state_and_exact_callback_uri(flow):
    begin(flow)
    flow[3][0] += 600
    assert callback(flow).status_code == 401
    assert not flow[2]["requests"]
    begin(flow)
    state = flow[2]["authorization"]["state"][0]
    result = flow[0].get(
        "/auth/callback", params={"state": state, "code": "new"}, headers={"Host": "evil.test"}
    )
    assert result.status_code == 400
    assert not flow[2]["requests"]


def test_missing_configuration_and_encryption_fail_closed(flow):
    from proofops.adapters.aws.oidc import OIDCProvider
    from proofops_api.oidc import build_oidc_router

    app = FastAPI()
    app.include_router(build_oidc_router(flow[4]))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/auth/login").status_code == 503
        assert client.get("/auth/callback").status_code == 503
    with pytest.raises(ValueError):
        replace(flow[1], client_id="")
    provider = OIDCProvider(flow[1])
    app = FastAPI()
    app.include_router(build_oidc_router(flow[4], config=flow[1], provider=provider))
    with TestClient(app, base_url=ORIGIN) as client:
        assert client.get("/auth/login").status_code == 503
    provider.close()
    flow[2]["encryption_failure"] = True
    begin(flow)
    assert callback(flow).status_code == 503
    assert flow[0].get("/v1/session").status_code == 401


def test_jwks_cache_expires_and_rotates(flow):
    from cryptography.hazmat.primitives.asymmetric import rsa

    begin(flow)
    assert callback(flow).status_code == 302
    begin(flow)
    assert callback(flow, "second-code").status_code == 302
    assert flow[2]["jwks_calls"] == 1
    flow[2]["key"] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    flow[2]["kid"] = "key-2"
    begin(flow)
    assert callback(flow, "before-cache-expiry").status_code == 401
    assert flow[2]["jwks_calls"] == 1
    flow[3][0] += 60
    begin(flow)
    assert callback(flow, "after-cache-expiry").status_code == 302
    assert flow[2]["jwks_calls"] == 2


@pytest.mark.parametrize(
    "headers",
    [
        {"kid": "unknown"},
        {"jku": "https://evil.test/key"},
        {"crit": ["evil"]},
        {"jwk": {"kty": "oct"}},
    ],
)
def test_untrusted_key_headers_rejected(flow, headers):
    flow[2]["headers"] = headers
    begin(flow)
    assert callback(flow).status_code == 401
    assert all("evil.test" not in str(item.url) for item in flow[2]["requests"])


def test_parallel_state_consumption_and_bounded_store():
    from concurrent.futures import ThreadPoolExecutor

    from proofops.adapters.local.auth_store import hash_token
    from proofops.adapters.local.oidc_store import InMemoryOIDCStore, PendingLogin

    store = InMemoryOIDCStore(capacity=1)
    pending = PendingLogin("nonce", "verifier", hash_token("browser"), "/", 600)
    store.put("state", pending, now=0)
    with pytest.raises(RuntimeError):
        store.put("different", pending, now=0)
    with ThreadPoolExecutor(max_workers=8) as executor:
        consumed = list(executor.map(lambda _: store.consume("state", "browser", now=0), range(8)))
        reserved = list(executor.map(lambda _: store.reserve_code("code", now=0), range(8)))
    assert sum(item is not None for item in consumed) == 1
    assert sum(reserved) == 1
    assert not store.reserve_code("new-code", now=0)
    assert store.reserve_code("new-code", now=600)
    with pytest.raises(ValueError):
        store.put("too-long", replace(pending, expires_at=1201), now=600)


@pytest.mark.parametrize(
    "kind",
    [
        "redirect",
        "oversized",
        "invalid-json",
        "timeout",
        "none-alg",
        "hs256-alg",
        "wrong-signing-key",
    ],
)
def test_bad_issuer_http_and_algorithm_responses_fail_closed(flow, kind):
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa

    provider = flow[6]
    original = provider._http

    def response(request):
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://evil.test"})
        if kind == "oversized":
            return httpx.Response(200, content=b"x" * 65537)
        if kind == "invalid-json":
            return httpx.Response(200, content=b"[")
        if kind == "timeout":
            raise httpx.ReadTimeout("synthetic fixture", request=request)
        if request.method == "GET":
            return original.send(request)
        claims = {
            "sub": SUBJECT,
            "iss": ISSUER,
            "aud": flow[1].client_id,
            "exp": int(flow[3][0]) + 300,
            "nonce": flow[2]["authorization"]["nonce"][0],
            "token_use": "id",
        }
        alg = {"none-alg": "none", "hs256-alg": "HS256", "wrong-signing-key": "RS256"}[kind]
        key = (
            None
            if alg == "none"
            else "a" * 32
            if alg == "HS256"
            else rsa.generate_private_key(public_exponent=65537, key_size=2048)
        )
        token = jwt.encode(claims, key, algorithm=alg, headers={"kid": "key-1"})
        return httpx.Response(
            200, json={"id_token": token, "refresh_token": "synthetic", "token_type": "Bearer"}
        )

    with httpx.Client(transport=httpx.MockTransport(response)) as replacement:
        provider._http = replacement
        try:
            begin(flow)
            assert callback(flow).status_code == (503 if kind == "timeout" else 401)
            assert flow[0].get("/v1/session").status_code == 401
        finally:
            provider._http = original


def test_kms_sink_passes_only_real_encrypted_bytes_to_writer():
    from cryptography.fernet import Fernet
    from proofops.adapters.aws.oidc import KMSRefreshTokenSink

    cipher = Fernet(Fernet.generate_key())
    writes = []

    class SyntheticKMS:
        def encrypt(self, **kwargs):
            assert kwargs["KeyId"] == "synthetic-test-key-not-an-arn"
            assert kwargs["EncryptionContext"] == {"session_hash": "sid-hash", "user_sub": SUBJECT}
            return {"CiphertextBlob": cipher.encrypt(kwargs["Plaintext"])}

    sink = KMSRefreshTokenSink(
        kms_client=SyntheticKMS(),
        key_id="synthetic-test-key-not-an-arn",
        write_ciphertext=lambda *args: writes.append(args),
    )
    sink("sid-hash", SUBJECT, "synthetic-refresh")
    assert writes[0][:2] == ("sid-hash", SUBJECT)
    assert cipher.decrypt(writes[0][2]) == b"synthetic-refresh"
    assert writes[0][2] != b"synthetic-refresh"


def test_success_over_real_loopback_http_issuer(flow):
    """Real TCP issuer exchange; HTTPS config mapped to loopback only by test transport."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    provider = flow[6]
    original = provider._http
    received = []

    class IssuerHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def respond(self):
            self.connection.settimeout(2)
            length = int(self.headers.get("Content-Length", 0))
            assert length <= 8192
            body = self.rfile.read(length)
            received.append((self.command, self.path))
            response = original.request(
                self.command, "https://issuer.example.test" + self.path, content=body
            )
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), IssuerHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()

    class LoopbackTransport(httpx.BaseTransport):
        def __init__(self):
            self.inner = httpx.HTTPTransport(retries=0)

        def handle_request(self, request):
            assert request.url.host == "issuer.example.test"
            request.url = request.url.copy_with(
                scheme="http", host="127.0.0.1", port=server.server_port
            )
            return self.inner.handle_request(request)

        def close(self):
            self.inner.close()

    try:
        with httpx.Client(transport=LoopbackTransport(), timeout=2, trust_env=False) as local:
            provider._http = local
            begin(flow)
            assert callback(flow).status_code == 302
            assert flow[0].get("/v1/session").json()["user_id"] == SUBJECT
    finally:
        provider._http = original
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    assert received == [("POST", "/oauth2/token"), ("GET", "/pool-fixture/.well-known/jwks.json")]


def test_session_storage_failure_returns_no_live_successor(flow):
    sessions = flow[4].sessions
    original = sessions.put

    def fail_after_write(record):
        original(record)
        raise RuntimeError("synthetic store failure after write")

    sessions.put = fail_after_write
    begin(flow)
    result = callback(flow)
    assert result.status_code == 503
    assert all(record.revoked for record in sessions._sessions.values())


def test_configuration_copies_mutable_return_allowlist(flow):
    allowlist = ["/review"]
    config = replace(flow[1], return_to_allowlist=allowlist)
    allowlist.append("https://evil.test")
    assert config.return_to_allowlist == ("/review",)
