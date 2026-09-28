"""Session/tenant authorization.

Pure application-layer module: stdlib only, no AWS SDK, no network, no file
access, no environment variables. Adapters implement the ports declared
here; this module never imports proofops.adapters.

Authorization contract:
- role capability is a *set*, not a ladder: viewer is included by every
  role; editor and reviewer are distinct siblings; admin includes both.
  `authorize` checks capability membership, not a rank comparison.
- A live session with no active tenant selected, or with a tenant but
  lacking the required capability, is a `CapabilityDeniedError`
  (API maps this family to 403 FORBIDDEN) -- it is never conflated with
  "no session at all" (401) or "resource not found" (404).
- A missing/expired/revoked session is `SessionExpiredError` (-> 401).
- A tenant-scoped id the caller cannot reach -- either because the tenant
  does not exist, or because the caller has no *active* membership there
  -- is `TenantNotFoundError` (-> 404). Both cases must be indistinguishable
  from the caller's point of view (AT-037): existence is never leaked to a
  non-member.
- query/body tenant_id is never trusted as authorization evidence; only the
  session's own active_tenant_id and a strong (uncached) membership read
  decide access.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal, Protocol

Role = Literal["viewer", "editor", "reviewer", "admin"]
Capability = Literal["viewer", "editor", "reviewer", "admin"]

# viewer ⊂ editor, viewer ⊂ reviewer, viewer ⊂ admin, editor ⊂ admin,
# reviewer ⊂ admin. editor and reviewer are NOT related to each other
# (docs/07 §1: "editor 와 reviewer 는 상하 관계가 아니라 서로 다른 권한").
_ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    "viewer": frozenset({"viewer"}),
    "editor": frozenset({"viewer", "editor"}),
    "reviewer": frozenset({"viewer", "reviewer"}),
    "admin": frozenset({"viewer", "editor", "reviewer", "admin"}),
}


class AuthorizationError(Exception):
    """Base error for the authorization module."""


class SessionExpiredError(AuthorizationError):
    """Session is missing, past its absolute/idle deadline, or revoked."""


class CapabilityDeniedError(AuthorizationError):
    """Live session, but no active tenant or missing required capability."""


class TenantNotFoundError(AuthorizationError):
    """Requested tenant does not exist OR caller has no live membership.

    These two situations are deliberately not distinguished by this error:
    the API boundary must return the same 404 either way so tenant
    existence is never leaked to a non-member (AT-037).
    """


@dataclass(frozen=True, slots=True)
class SessionRecord:
    """Server-side session state (mirrors the Session storage entity)."""

    session_id: str
    user_sub: str
    active_tenant_id: str | None
    csrf_hash: str
    expires_at: float
    idle_deadline: float
    revoked: bool

    def __post_init__(self) -> None:
        for name, value in (
            ("expires_at", self.expires_at),
            ("idle_deadline", self.idle_deadline),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int | float)
                or not math.isfinite(value)
            ):
                raise ValueError(f"{name} must be a finite timestamp")
        if not isinstance(self.revoked, bool):
            raise ValueError("revoked must be a bool")


@dataclass(frozen=True, slots=True)
class MembershipRecord:
    """Strong-read tenant membership (mirrors the Membership storage entity)."""

    tenant_id: str
    user_sub: str
    role: Role
    status: Literal["active", "revoked"]

    def __post_init__(self) -> None:
        if self.role not in _ROLE_CAPABILITIES:
            raise ValueError(f"unknown role: {self.role!r}")
        if self.status not in {"active", "revoked"}:
            raise ValueError(f"unknown membership status: {self.status!r}")


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Result of a successful authorization: what the caller may do, where."""

    user_sub: str
    tenant_id: str
    role: Role
    capabilities: frozenset[Capability]
    session_id: str

    def has_capability(self, capability: Capability) -> bool:
        return capability in self.capabilities


class SessionPort(Protocol):
    """Boundary every session-store adapter must implement."""

    def get(self, session_id: str) -> SessionRecord | None: ...

    def csrf_token_for(self, session_id: str) -> str | None: ...

    def set_active_tenant(
        self, session_id: str, tenant_id: str, *, now: float
    ) -> SessionRecord: ...


class MembershipPort(Protocol):
    """Boundary every membership-store adapter must implement.

    `get` must always be a strong (uncached) read: docs/11 requires
    revoked-membership checks to bypass any short-lived cache.
    """

    def get(self, tenant_id: str, user_sub: str) -> MembershipRecord | None: ...

    def tenant_exists(self, tenant_id: str) -> bool: ...


def _check_live_session(*, session_id: str, now: float, session_port: SessionPort) -> SessionRecord:
    if isinstance(now, bool) or not isinstance(now, int | float) or not math.isfinite(now):
        raise ValueError("now must be a finite timestamp")
    session = session_port.get(session_id)
    if session is None or session.revoked:
        raise SessionExpiredError(session_id)
    if now >= session.expires_at or now >= session.idle_deadline:
        raise SessionExpiredError(session_id)
    return session


def get_live_session(*, session_id: str, now: float, session_port: SessionPort) -> SessionRecord:
    """Authenticate a session without requiring a tenant selection."""
    return _check_live_session(session_id=session_id, now=now, session_port=session_port)


def _live_membership(
    *, membership_port: MembershipPort, tenant_id: str, user_sub: str
) -> MembershipRecord:
    """Return an active membership or raise the tenant-hiding 404 error.

    Deliberately does not branch on `tenant_exists` before raising: any
    caller-observable difference between "tenant absent" and "tenant present
    but you are not a member" would leak existence, which AT-037 forbids.
    """
    record = membership_port.get(tenant_id, user_sub)
    if record is None or record.status != "active":
        raise TenantNotFoundError(tenant_id)
    return record


def authorize(
    *,
    session_id: str,
    now: float,
    session_port: SessionPort,
    membership_port: MembershipPort,
    required_capability: Capability = "viewer",
    requested_tenant_id: str | None = None,
) -> AuthContext:
    """Resolve session + membership + capability into an AuthContext.

    Failure families (see module docstring):
    - no/expired/revoked session -> SessionExpiredError (401)
    - live session, no active tenant, or capability missing -> CapabilityDeniedError (403)
    - requested_tenant_id names a tenant the caller cannot reach -> TenantNotFoundError (404)

    `requested_tenant_id`, when given, identifies a *specific resource's*
    tenant scope (e.g. a path parameter for a tenant-scoped object): it is
    checked against a strong membership read, never against the session's
    active tenant alone, and never accepted as authorization evidence by
    itself.
    """
    session = _check_live_session(session_id=session_id, now=now, session_port=session_port)

    active_tenant_id = session.active_tenant_id
    if active_tenant_id is None:
        raise CapabilityDeniedError("session has no active tenant selected")

    scope_tenant_id = requested_tenant_id if requested_tenant_id is not None else active_tenant_id

    if requested_tenant_id is not None and requested_tenant_id != active_tenant_id:
        # A different tenant than the session's active one is a resource
        # reachability question, not a capability question: 404, and it
        # must not distinguish "tenant exists but you're not a member"
        # from "tenant does not exist".
        _live_membership(
            membership_port=membership_port,
            tenant_id=scope_tenant_id,
            user_sub=session.user_sub,
        )
        # Reaching here without the caller matching their *active* tenant
        # would still not be a valid AuthContext for this session; scoped
        # cross-tenant reads are resolved by the resource lookup layer,
        # not by minting an AuthContext in a tenant the session isn't
        # switched into. Treat as not-found for the requested id.
        raise TenantNotFoundError(scope_tenant_id)

    membership = _live_membership(
        membership_port=membership_port,
        tenant_id=active_tenant_id,
        user_sub=session.user_sub,
    )

    capabilities = _ROLE_CAPABILITIES[membership.role]
    if required_capability not in capabilities:
        raise CapabilityDeniedError(
            f"role {membership.role!r} lacks capability {required_capability!r}"
        )

    return AuthContext(
        user_sub=session.user_sub,
        tenant_id=active_tenant_id,
        role=membership.role,
        capabilities=capabilities,
        session_id=session.session_id,
    )


def select_tenant(
    *,
    session_id: str,
    tenant_id: str,
    now: float,
    session_port: SessionPort,
    membership_port: MembershipPort,
) -> AuthContext:
    """Switch the session's active tenant after a strong membership check.

    Raises `TenantNotFoundError` (-> opaque 404) for both a nonexistent
    tenant and a tenant the caller is not an *active* member of (a revoked
    membership is rejected identically). Never switches to a tenant the
    caller is unauthorized for, even transiently.
    """
    session = _check_live_session(session_id=session_id, now=now, session_port=session_port)

    membership = _live_membership(
        membership_port=membership_port,
        tenant_id=tenant_id,
        user_sub=session.user_sub,
    )

    updated = session_port.set_active_tenant(session_id, tenant_id, now=now)
    capabilities = _ROLE_CAPABILITIES[membership.role]
    return AuthContext(
        user_sub=updated.user_sub,
        tenant_id=tenant_id,
        role=membership.role,
        capabilities=capabilities,
        session_id=updated.session_id,
    )
