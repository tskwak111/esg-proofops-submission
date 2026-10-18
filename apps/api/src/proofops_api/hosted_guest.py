"""Bounded public trial admission; no analysis/provider policy changes."""

import math
import secrets
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from proofops.application.authorization import AuthContext, SessionRecord
from starlette.requests import Request

from proofops_api.hosted_store import HostedError, HostedSettings, HostedStore, day, digest


def is_guest(auth: AuthContext | SessionRecord) -> bool:
    return auth.user_sub.startswith("guest:")


def peer_ip(request: Request, settings: HostedSettings) -> str:
    """Use the same trusted immediate-proxy boundary for both login methods."""
    from ipaddress import ip_address

    peer = request.client.host if request.client else "unknown"
    if peer in settings.auth_trusted_proxies:
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        try:
            peer = str(ip_address(forwarded))
        except ValueError:
            pass
    return peer


class GuestAccess:
    def __init__(self, store: HostedStore):
        self.store = store

    @staticmethod
    def migrate(db: sqlite3.Connection) -> None:
        db.execute("""CREATE TABLE IF NOT EXISTS guest_session_starts (
            ip_hash TEXT NOT NULL, created REAL NOT NULL)""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS guest_starts_ip ON guest_session_starts "
            "(ip_hash,created)"
        )
        db.execute("""CREATE TABLE IF NOT EXISTS guest_audit (
            created REAL NOT NULL, day TEXT NOT NULL, role TEXT NOT NULL,
            tenant_id TEXT NOT NULL, user_hash TEXT NOT NULL,
            event TEXT NOT NULL, resource_id TEXT)""")
        db.execute(
            "CREATE INDEX IF NOT EXISTS guest_audit_quota ON guest_audit " "(tenant_id,day,event)"
        )

    def available(self, now: float) -> bool:
        until = self.store.settings.guest_access_until
        return until is not None and now < until.timestamp()

    def discovery(self, now: float) -> dict:
        settings = self.store.settings
        return dict(
            available=self.available(now),
            until=settings.guest_access_until.isoformat() if settings.guest_access_until else None,
            fresh_runs_per_day=settings.guest_fresh_runs_per_day,
            showcase=bool(settings.showcase_tenant),
        )

    def login(self, *, ip: str, now: float) -> str:
        settings = self.store.settings
        until = settings.guest_access_until
        if until is None or now >= until.timestamp():
            raise HostedError("GUEST_ACCESS_CLOSED", 403)
        with self.store.transaction() as db:
            hashed = self.store.user_hash("guest-ip:" + ip)
            db.execute("DELETE FROM guest_session_starts WHERE created<=?", (now - 3600,))
            count, oldest = db.execute(
                "SELECT count(*),min(created) FROM guest_session_starts WHERE ip_hash=?",
                (hashed,),
            ).fetchone()
            if count >= settings.guest_sessions_per_ip_hour:
                raise HostedError(
                    "GUEST_SESSION_RATE_LIMIT",
                    429,
                    retry_after=max(1, math.ceil(oldest + 3600 - now)),
                )
            sid, user, tenant = secrets.token_urlsafe(32), "guest:" + str(uuid4()), str(uuid4())
            expires = min(now + 28800, until.timestamp())
            db.execute("INSERT INTO memberships VALUES (?,?,'editor','active')", (tenant, user))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,0)",
                (
                    digest(sid),
                    user,
                    tenant,
                    digest(self.store.sessions.csrf_token_for(sid)),
                    expires,
                    min(expires, now + 1800),
                ),
            )
            db.execute("INSERT INTO guest_session_starts VALUES (?,?)", (hashed, now))
            db.execute(
                "INSERT INTO guest_audit VALUES (?,?,'guest',?,?,'session',NULL)",
                (now, day(now), tenant, self.store.user_hash(user)),
            )
        return sid

    def fresh_runs(self, db: sqlite3.Connection, user: str, now: float) -> int:
        # Reuse has no spend reservation; purge/cancellation cannot reset admission counts.
        return db.execute(
            "SELECT count(*) FROM deletion.spend WHERE user_hash=? AND day=?",
            (self.store.user_hash(user), day(now)),
        ).fetchone()[0]

    def session_info(self, user: str, now: float) -> dict:
        with self.store.transaction(readonly=True) as db:
            return dict(
                until=self.discovery(now)["until"],
                fresh_runs_today=self.fresh_runs(db, user, now),
                fresh_runs_limit=self.store.settings.guest_fresh_runs_per_day,
            )

    def check_fresh_run(self, db: sqlite3.Connection, auth: AuthContext, now: float) -> None:
        if is_guest(auth) and self.fresh_runs(db, auth.user_sub, now) >= (
            self.store.settings.guest_fresh_runs_per_day
        ):
            raise HostedError("GUEST_DAILY_LIMIT", 429)

    def check_upload(self, db: sqlite3.Connection, auth: AuthContext, now: float) -> None:
        if not is_guest(auth):
            return
        tenant_count, total_count = db.execute(
            "SELECT coalesce(sum(tenant_id=?),0),count(*) FROM guest_audit "
            "WHERE day=? AND event='upload' AND role='guest'",
            (auth.tenant_id, day(now)),
        ).fetchone()
        if tenant_count >= 5 or total_count >= self.store.settings.guest_uploads_per_day:
            raise HostedError("GUEST_UPLOAD_LIMIT", 429)

    def audit(
        self, db: sqlite3.Connection, auth: AuthContext, event: str, resource: str, now: float
    ) -> None:
        if is_guest(auth):
            db.execute(
                "INSERT INTO guest_audit VALUES (?,?,'guest',?,?,?,?)",
                (
                    now,
                    day(now),
                    auth.tenant_id,
                    self.store.user_hash(auth.user_sub),
                    event,
                    resource,
                ),
            )

    def maintain(self, db: sqlite3.Connection, now: float) -> None:
        db.execute("DELETE FROM guest_session_starts WHERE created<=?", (now - 3600,))
        db.execute(
            "DELETE FROM guest_audit WHERE created<?",
            (now - self.store.settings.audit_days * 86400,),
        )
        db.execute(
            "DELETE FROM memberships WHERE user_id LIKE 'guest:%' "
            "AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.user_id=memberships.user_id)"
        )


# These are the only routes allowed to borrow showcase read visibility. A new
# endpoint must opt in, and this viewer context never reaches a write handler.
SHOWCASE_READ_ROUTES = frozenset(
    {
        "/v1/documents/{document_id}",
        "/v1/documents/{document_id}/original",
        "/v1/documents/{document_id}/scope-plan",
        "/v1/runs/{run_id}",
        "/v1/runs/{run_id}/result",
        "/v1/runs/{run_id}/source-pages/{physical_page}/manifest",
        "/v1/runs/{run_id}/source-pages/{physical_page}/image",
    }
)


@dataclass(frozen=True, slots=True)
class ShowcaseReader(AuthContext):
    caller_tenant_id: str


def showcase_reader(store: HostedStore, auth: AuthContext) -> ShowcaseReader:
    return ShowcaseReader(
        user_sub=auth.user_sub,
        tenant_id=store.settings.showcase_tenant,
        role="viewer",
        capabilities=frozenset({"viewer"}),
        session_id=auth.session_id,
        caller_tenant_id=auth.tenant_id,
    )


def recheck_read_auth(
    store: HostedStore, auth: AuthContext, fresh: AuthContext, kind: str, resource: str, now: float
) -> AuthContext:
    """Preserve post-IO session/membership revocation and showcase visibility checks."""
    tenant = auth.caller_tenant_id if isinstance(auth, ShowcaseReader) else auth.tenant_id
    if (fresh.tenant_id, fresh.user_sub) != (tenant, auth.user_sub):
        raise HostedError("RESOURCE_NOT_FOUND", 404)
    if isinstance(auth, ShowcaseReader):
        if auth.tenant_id != store.settings.showcase_tenant or not showcase_resource(
            store, kind, resource, now
        ):
            raise HostedError("RESOURCE_NOT_FOUND", 404)
        return showcase_reader(store, fresh)
    return fresh


def showcase_resource(store: HostedStore, kind: str, resource: str, now: float) -> bool:
    tenant = store.settings.showcase_tenant
    if not tenant:
        return False
    with store.transaction(readonly=True) as db:
        if kind == "runs":
            row = db.execute(
                "SELECT document_id FROM runs WHERE tenant_id=? AND run_id=? "
                "AND status='completed'",
                (tenant, resource),
            ).fetchone()
            if row is None:
                return False
            resource = row[0]
        return (
            db.execute(
                "SELECT 1 FROM documents d WHERE tenant_id=? AND document_id=? AND expires>? "
                "AND NOT EXISTS (SELECT 1 FROM deletion.tombstones t WHERE "
                "t.tenant_id=d.tenant_id AND t.document_id=d.document_id)",
                (tenant, resource, now),
            ).fetchone()
            is not None
        )
