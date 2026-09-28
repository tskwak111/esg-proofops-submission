"""Configured Cognito OIDC HTTP/JWT boundary; no account discovery or SDK calls on import."""

from __future__ import annotations

import json
import math
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt


class OIDCRejected(ValueError):
    """Untrusted authorization response rejected; never expose its contents."""


class OIDCUnavailable(RuntimeError):
    """Configuration, issuer transport or encrypted persistence is unavailable."""


def _https_url(value: str) -> None:
    parsed = urlsplit(value)
    if (
        not value
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or any(c.isspace() or ord(c) < 32 for c in value)
        or "\\" in value
    ):
        raise ValueError("OIDC requires explicit HTTPS URLs without credentials/query/fragment")


@dataclass(frozen=True, slots=True)
class OIDCConfig:
    issuer: str
    client_id: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    redirect_uri: str
    app_origin: str
    return_to_allowlist: tuple[str, ...] = ("/",)
    state_ttl_seconds: int = 600
    jwks_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        object.__setattr__(self, "return_to_allowlist", tuple(self.return_to_allowlist))
        for value in (
            self.issuer,
            self.authorization_endpoint,
            self.token_endpoint,
            self.jwks_uri,
            self.redirect_uri,
            self.app_origin,
        ):
            _https_url(value)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", self.client_id):
            raise ValueError("OIDC client_id is required")
        if (
            urlsplit(self.app_origin).path
            or self.redirect_uri != self.app_origin + "/auth/callback"
        ):
            raise ValueError("OIDC callback must exactly match configured application origin")
        if self.issuer.endswith("/") or self.jwks_uri != self.issuer + "/.well-known/jwks.json":
            raise ValueError("JWKS must belong to configured issuer")
        if urlsplit(self.authorization_endpoint).netloc != urlsplit(self.token_endpoint).netloc:
            raise ValueError("authorization and token endpoints must share their configured origin")
        if (
            type(self.state_ttl_seconds) is not int
            or not 1 <= self.state_ttl_seconds <= 600
            or type(self.jwks_ttl_seconds) is not int
            or not 1 <= self.jwks_ttl_seconds <= 300
        ):
            raise ValueError("OIDC state/JWKS cache TTL exceeds security bounds")
        if not self.return_to_allowlist or any(
            not re.fullmatch(r"/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*", path)
            for path in self.return_to_allowlist
        ):
            raise ValueError("return_to allowlist must contain plain same-origin paths")


class KMSRefreshTokenSink:
    """Injected KMS encrypt + ciphertext writer; calls only the explicitly injected client.

    The writer stores only ciphertext, keyed by hashed SID, and must raise on failure.
    Root must configure an authorized real key/client and durable writer before live use.
    """

    def __init__(
        self, *, kms_client: Any, key_id: str, write_ciphertext: Callable[[str, str, bytes], None]
    ) -> None:
        if not key_id or kms_client is None or not callable(write_ciphertext):
            raise ValueError("KMS client, key and ciphertext writer are required")
        self._client = kms_client
        self._key_id = key_id
        self._write = write_ciphertext

    def __call__(self, session_hash: str, user_sub: str, token: str) -> None:
        result = self._client.encrypt(
            KeyId=self._key_id,
            Plaintext=token.encode(),
            EncryptionContext={"session_hash": session_hash, "user_sub": user_sub},
        )
        ciphertext = result.get("CiphertextBlob")
        if not isinstance(ciphertext, bytes) or not ciphertext or ciphertext == token.encode():
            raise OIDCUnavailable("encrypted refresh persistence unavailable")
        self._write(session_hash, user_sub, ciphertext)


class OIDCProvider:
    """RS256 ID-token verification; access tokens never grant application roles.

    `save_refresh` is a trusted encryption/persistence boundary. The default is disabled;
    real composition must use KMSRefreshTokenSink, synthetic tests may use local encryption.
    """

    def __init__(
        self,
        config: OIDCConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        save_refresh: Callable[[str, str, str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.save_refresh = save_refresh
        self._clock = clock
        self._http = httpx.Client(
            transport=transport, timeout=5.0, follow_redirects=False, trust_env=False
        )
        self._keys: dict[str, Any] = {}
        self._expires_at = 0.0
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return callable(self.save_refresh)

    def close(self) -> None:
        self._http.close()

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            with self._http.stream(method, url, **kwargs) as response:
                if response.status_code != 200:
                    raise OIDCRejected("issuer response rejected")
                body = bytearray()
                for chunk in response.iter_bytes(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > 65536:
                        raise OIDCRejected("issuer response exceeds limit")
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise OIDCRejected("issuer response is not an object")
                return result
        except httpx.HTTPError:
            raise OIDCUnavailable("issuer unavailable") from None
        except (ValueError, UnicodeError):
            raise OIDCRejected("issuer response rejected") from None

    def _key(self, kid: str) -> Any:
        now = self._clock()
        with self._lock:
            expired = now >= self._expires_at
        if expired:
            # No global lock over HTTP. Concurrent refreshes are bounded by HTTP timeouts;
            # unknown kids never force a refresh within the TTL (key-id spray resistance).
            data = self._json("GET", self.config.jwks_uri)
            raw_keys = data.get("keys")
            if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 16:
                raise OIDCRejected("JWKS rejected")
            keys = {}
            try:
                for item in raw_keys:
                    if (
                        not isinstance(item, dict)
                        or item.get("kty") != "RSA"
                        or item.get("use") != "sig"
                        or item.get("alg") != "RS256"
                        or not isinstance(item.get("kid"), str)
                        or not 1 <= len(item["kid"]) <= 256
                        or item["kid"] in keys
                        or ("key_ops" in item and item["key_ops"] != ["verify"])
                    ):
                        raise OIDCRejected("JWKS key rejected")
                    key = jwt.PyJWK.from_dict(item, algorithm="RS256").key
                    if key.key_size < 2048:
                        raise OIDCRejected("JWKS key too small")
                    keys[item["kid"]] = key
            except (ValueError, KeyError, TypeError, jwt.PyJWTError):
                raise OIDCRejected("JWKS rejected") from None
            with self._lock:
                self._keys = keys
                self._expires_at = now + self.config.jwks_ttl_seconds
        with self._lock:
            key = self._keys.get(kid)
        if key is None:
            raise OIDCRejected("unknown signing key")
        return key

    def exchange(self, *, code: str, verifier: str, nonce: str) -> tuple[str, str]:
        if not self.ready:
            raise OIDCUnavailable("encrypted refresh persistence unavailable")
        tokens = self._json(
            "POST",
            self.config.token_endpoint,
            data={
                "grant_type": "authorization_code",
                "client_id": self.config.client_id,
                "redirect_uri": self.config.redirect_uri,
                "code": code,
                "code_verifier": verifier,
            },
        )
        token = tokens.get("id_token")
        refresh = tokens.get("refresh_token")
        if (
            not isinstance(token, str)
            or not 1 <= len(token) <= 16384
            or not isinstance(refresh, str)
            or not 1 <= len(refresh) <= 16384
            or tokens.get("token_type") != "Bearer"
        ):
            raise OIDCRejected("token response rejected")
        try:
            header = jwt.get_unverified_header(token)
            if (
                header.get("alg") != "RS256"
                or not isinstance(header.get("kid"), str)
                or not 1 <= len(header["kid"]) <= 256
                or header.get("crit")
                or any(name in header for name in ("jku", "jwk", "x5u"))
            ):
                raise OIDCRejected("token header rejected")
            claims = jwt.decode(
                token,
                self._key(header["kid"]),
                algorithms=["RS256"],
                audience=self.config.client_id,
                issuer=self.config.issuer,
                options={
                    "require": ["sub", "iss", "aud", "exp", "nonce", "token_use"],
                    "strict_aud": True,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            now = self._clock()
            for name in ("exp", "iat", "nbf"):
                if name in claims:
                    value = claims[name]
                    if (
                        type(value) not in (int, float)
                        or not math.isfinite(value)
                        or (name == "exp" and now >= value)
                        or (name != "exp" and now < value)
                    ):
                        raise OIDCRejected("token time rejected")
            if (
                claims["token_use"] != "id"
                or not isinstance(claims["nonce"], str)
                or not secrets.compare_digest(claims["nonce"], nonce)
                or not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 256
            ):
                raise OIDCRejected("token identity rejected")
            return claims["sub"], refresh
        except (jwt.PyJWTError, ValueError, TypeError, KeyError):
            raise OIDCRejected("ID token rejected") from None

    def persist_refresh(self, session_hash: str, user_sub: str, token: str) -> None:
        try:
            if self.save_refresh is None:
                raise OIDCUnavailable("encrypted refresh persistence unavailable")
            self.save_refresh(session_hash, user_sub, token)
        except Exception:
            raise OIDCUnavailable("encrypted refresh persistence unavailable") from None
