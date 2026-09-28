"""Local in-memory session/membership adapters (TASK-037).

Explicit local-only adapters implementing the SessionPort/MembershipPort
protocols from proofops.application.authorization. These back local dev and
tests only: no DynamoDB, no network. Non-local composition must not use
them (see packages/proofops/composition.py fail-closed behavior).

Session-id generation and CSRF derivation use stdlib `secrets`/`hashlib`/`hmac` only.
Real Cognito/OIDC token verification, KMS-encrypted refresh-token storage,
and DynamoDB-backed session/membership tables remain not_run until the
account values in docs/17_ENV_CONFIG.md exist (COGNITO_*, SESSION_SECRET_ARN).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, replace

from proofops.application.authorization import (
    MembershipPort,
    MembershipRecord,
    SessionExpiredError,
    SessionPort,
    SessionRecord,
)

_ABSOLUTE_SESSION_SECONDS = 8 * 60 * 60
_IDLE_SESSION_SECONDS = 30 * 60


def new_session_id() -> str:
    """32+ bytes CSPRNG session id (docs/11: 'SID 는 32bytes 이상 CSPRNG')."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Server stores only a hash, never the raw CSRF/session token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _StoredSession:
    """Hash-keyed server record; intentionally has no raw session id."""

    user_sub: str
    active_tenant_id: str | None
    csrf_hash: str
    expires_at: float
    idle_deadline: float
    revoked: bool

    @classmethod
    def from_record(cls, record: SessionRecord, *, csrf_hash: str) -> _StoredSession:
        return cls(
            user_sub=record.user_sub,
            active_tenant_id=record.active_tenant_id,
            csrf_hash=csrf_hash,
            expires_at=record.expires_at,
            idle_deadline=record.idle_deadline,
            revoked=record.revoked,
        )

    def project(self, session_id: str) -> SessionRecord:
        return SessionRecord(
            session_id=session_id,
            user_sub=self.user_sub,
            active_tenant_id=self.active_tenant_id,
            csrf_hash=self.csrf_hash,
            expires_at=self.expires_at,
            idle_deadline=self.idle_deadline,
            revoked=self.revoked,
        )


class InMemorySessionStore:
    """Local-only session store. Not safe across processes; tests/dev only.

    Keys are SHA-256 hashes of cookie SIDs and stored values omit raw SIDs.
    CSRF tokens are deterministic per local store/SID via HMAC, so GET can
    issue the token while the record retains only its hash. The random HMAC
    key is process-local; this adapter is intentionally non-durable.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, _StoredSession] = {}
        self._csrf_secret = secrets.token_bytes(32)
        self._lock = threading.Lock()

    def _key(self, session_id: str) -> str:
        return hash_token(session_id)

    def _csrf_token(self, session_id: str) -> str:
        return hmac.new(
            self._csrf_secret,
            session_id.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _stored(self, record: SessionRecord) -> _StoredSession:
        return _StoredSession.from_record(
            record,
            csrf_hash=hash_token(self._csrf_token(record.session_id)),
        )

    def put(self, record: SessionRecord) -> None:
        with self._lock:
            self._sessions[self._key(record.session_id)] = self._stored(record)

    def put_with_token(self, record: SessionRecord, csrf_token: str) -> None:
        """Validate a local seed token, then discard it and store hash-only."""
        if not secrets.compare_digest(hash_token(csrf_token), record.csrf_hash):
            raise ValueError("CSRF hash/token mismatch")
        with self._lock:
            self._sessions[self._key(record.session_id)] = self._stored(record)

    def csrf_token_for(self, session_id: str) -> str | None:
        with self._lock:
            if self._key(session_id) not in self._sessions:
                return None
            return self._csrf_token(session_id)

    def get(self, session_id: str) -> SessionRecord | None:
        with self._lock:
            stored = self._sessions.get(self._key(session_id))
            return stored.project(session_id) if stored else None

    def revoke(self, session_id: str) -> None:
        with self._lock:
            key = self._key(session_id)
            existing = self._sessions.get(key)
            if existing is not None:
                self._sessions[key] = replace(existing, revoked=True)

    def set_active_tenant(self, session_id: str, tenant_id: str, *, now: float) -> SessionRecord:
        """Rotate to a fresh session id/CSRF on tenant switch (docs/11:
        'rotation 은 로그인·권한승격·tenant 전환시'). The old session id is
        revoked so it cannot be replayed after rotation."""
        with self._lock:
            old_key = self._key(session_id)
            existing = self._sessions.get(old_key)
            if (
                existing is None
                or existing.revoked
                or now >= existing.expires_at
                or now >= existing.idle_deadline
            ):
                raise SessionExpiredError(session_id)
            new_id = new_session_id()
            new_token = self._csrf_token(new_id)
            rotated = SessionRecord(
                session_id=new_id,
                user_sub=existing.user_sub,
                active_tenant_id=tenant_id,
                csrf_hash=hash_token(new_token),
                expires_at=existing.expires_at,
                idle_deadline=existing.idle_deadline,
                revoked=False,
            )
            self._sessions[self._key(new_id)] = _StoredSession.from_record(
                rotated,
                csrf_hash=hash_token(new_token),
            )
            self._sessions[old_key] = replace(existing, revoked=True)
            return rotated


class InMemoryMembershipStore:
    """Local-only membership store. Every read here is a strong read."""

    def __init__(self) -> None:
        self._memberships: dict[tuple[str, str], MembershipRecord] = {}
        self._tenants: set[str] = set()
        self._lock = threading.Lock()

    def put(self, record: MembershipRecord) -> None:
        with self._lock:
            self._memberships[(record.tenant_id, record.user_sub)] = record
            self._tenants.add(record.tenant_id)

    def get(self, tenant_id: str, user_sub: str) -> MembershipRecord | None:
        with self._lock:
            return self._memberships.get((tenant_id, user_sub))

    def tenant_exists(self, tenant_id: str) -> bool:
        with self._lock:
            return tenant_id in self._tenants


_: SessionPort = InMemorySessionStore()  # protocol-conformance smoke check
_m: MembershipPort = InMemoryMembershipStore()  # protocol-conformance smoke check
del _, _m
