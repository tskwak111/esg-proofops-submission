"""Single-host durable P1 control plane; no research DB or model adapter reuse.

Payload isolation makes authorized purge possible without disabling immutable
research triggers. All dispatches require a durable, fenced reservation first.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from proofops.adapters.local.provider_errors import MESSAGES, provider_error_code
from proofops.adapters.local.source_hx3 import failure_location
from proofops.application.authorization import MembershipRecord, SessionExpiredError, SessionRecord
from proofops.application.pipeline_failures import MESSAGES as PIPELINE_MESSAGES
from proofops.application.pipeline_failures import PUBLIC_CODES
from proofops.application.uploads_security import (
    HOSTED_NULL_NAVIGATION_PROFILE,
    PdfLimits,
    UploadRejected,
    verify_pdf_file,
)

from proofops_api.hosted_sqlite import BUSY_TIMEOUT_MS, retry_busy

MICRO = Decimal(1_000_000)
RUN_LEASE_SECONDS = 1900


TERMINAL = ("partial_blocked", "completed", "cancelled", "failed")
OPENROUTER_LIMIT_CODES = frozenset({"OPENROUTER_HTTP_402", "OPENROUTER_HTTP_403"})
OPENAI_LIMIT_CODES = frozenset({"OPENAI_HTTP_401", "OPENAI_HTTP_429", "OPENAI_INSUFFICIENT_QUOTA"})
PROVIDER_LIMIT_CODES = {
    "openrouter": OPENROUTER_LIMIT_CODES,
    "openai": OPENAI_LIMIT_CODES,
}
ALL_PROVIDER_LIMIT_CODES = OPENROUTER_LIMIT_CODES | OPENAI_LIMIT_CODES
PROVIDER_LIMIT_MESSAGE = "분석 서비스 한도 초과 — 운영자 확인 필요"
CONTROL_MESSAGES = {
    "MAINTENANCE": "서버 점검 중입니다. 진행 중 분석은 계속 처리하며 새 분석은 점검 후 접수합니다.",
    "LEASE_EXPIRED": (
        "서버 재시작 또는 작업 응답 중단으로 분석이 중단되었습니다. "
        "전송 기록이 있어 비용 정산 확인 전까지 보류하며 자동 재실행하지 않습니다."
    ),
    "LEASE_EXPIRED_NO_DISPATCH": (
        "서버 재시작 또는 작업 응답 중단으로 분석이 중단되었습니다. "
        "모델 전송이 없어 비용 예약이 해제되었습니다. 다시 접수할 수 있습니다."
    ),
}


class HostedError(ValueError):
    def __init__(self, code: str, status: int = 409, *, retry_after: int | None = None):
        super().__init__(code)
        self.code, self.status = code, status
        self.retry_after = retry_after


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def file_sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def copy_verified_file(source: Path, target: Path, expected_sha: str) -> None:
    """Create-only disk publication; hash the copied bytes before making them visible."""
    with NamedTemporaryFile(dir=target.parent) as staged, source.open("rb") as stream:
        shutil.copyfileobj(stream, staged, length=1024 * 1024)
        staged.flush()
        os.fsync(staged.fileno())
        if file_sha256(Path(staged.name)) != expected_sha:
            raise HostedError("SOURCE_HASH_MISMATCH")
        os.chmod(staged.name, 0o400)
        os.link(staged.name, target)


def micros(value: Decimal | str) -> int:
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or amount * MICRO != (amount * MICRO).to_integral():
        raise ValueError("invalid micro-USD amount")
    return int(amount * MICRO)


def usd(value: int) -> str:
    return f"{Decimal(value) / MICRO:.6f}"


def identifier(value: str) -> str:
    try:
        if str(UUID(value)) != value:
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError):
        raise HostedError("RESOURCE_NOT_FOUND", 404) from None


def day(now: float) -> str:
    return datetime.fromtimestamp(now, ZoneInfo("Asia/Seoul")).date().isoformat()


@dataclass(frozen=True)
class HostedSettings:
    root: Path
    origin: str
    secret: str
    event_id: str
    accept_uploads: bool = False
    consent_version: str = ""
    backend: str = "static"
    accept_runs: bool = False
    live_disabled: bool = True
    live_config: Path | None = None
    max_upload_bytes: int = 150 * 1024 * 1024
    active_days: int = 7
    backup_days: int = 14
    audit_days: int = 30
    run_usd: Decimal = Decimal("1.0")
    day_usd: Decimal = Decimal("7")
    event_usd: Decimal = Decimal("7")
    event_usd_ceiling: Decimal = Decimal("7")
    user_daily_runs: int = 6
    max_active_runs: int = 1
    user_active_runs: int = 1
    daily_runs: int = 10
    access_config: Path | None = None
    auth_trusted_proxies: tuple[str, ...] = ()
    guest_access_until: datetime | None = None
    guest_fresh_runs_per_day: int = 2
    guest_sessions_per_ip_hour: int = 30
    guest_uploads_per_day: int = 60
    showcase_tenant: str = ""
    rc1_reuse_enabled: bool = False
    retain_failed_analysis: bool = True
    failure_diagnostics_max_bytes: int = 512 * 1024 * 1024

    def __post_init__(self) -> None:
        from urllib.parse import urlsplit

        parsed = urlsplit(self.origin)
        if self.guest_access_until is not None and (
            not isinstance(self.guest_access_until, datetime)
            or self.guest_access_until.utcoffset() is None
        ):
            raise ValueError("HOSTED_GUEST_ACCESS_UNTIL must include a timezone")
        if (
            type(self.guest_fresh_runs_per_day) is not int
            or not 1 <= self.guest_fresh_runs_per_day <= 5
            or type(self.guest_sessions_per_ip_hour) is not int
            or self.guest_sessions_per_ip_hour < 1
            or type(self.guest_uploads_per_day) is not int
            or self.guest_uploads_per_day < 1
        ):
            raise ValueError("HOSTED_GUEST_LIMIT_INVALID")
        if not isinstance(self.showcase_tenant, str):
            raise ValueError("HOSTED_SHOWCASE_TENANT must be a UUID or empty")
        if self.showcase_tenant:
            identifier(self.showcase_tenant)
        if type(self.retain_failed_analysis) is not bool or (
            type(self.failure_diagnostics_max_bytes) is not int
            or not 16384 <= self.failure_diagnostics_max_bytes <= 512 * 1024 * 1024
        ):
            raise ValueError("HOSTED_FAILURE_DIAGNOSTICS_INVALID")
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or "*" in self.origin
        ):
            raise ValueError("APP_ORIGIN must be one exact HTTPS origin")
        if not self.root.is_absolute() or len(self.secret.encode()) < 32 or not self.event_id:
            raise ValueError(
                "absolute private data directory, session secret and event ID required"
            )
        if self.backend not in {"r108", "legacy", "static"}:
            raise ValueError("invalid analysis backend")
        if self.access_config is not None and not self.access_config.is_absolute():
            raise ValueError("absolute private access config required")
        from ipaddress import ip_address

        for proxy in self.auth_trusted_proxies:
            ip_address(proxy)
        if type(self.max_active_runs) is not int or self.max_active_runs not in (1, 2):
            raise ValueError("HOSTED_RUN_CONCURRENCY_INVALID")
        if not self.live_disabled:
            from proofops_api.hosted_analysis import read_operator_config

            read_operator_config(self.live_config)
        if self.accept_uploads and not self.consent_version:
            raise ValueError("upload consent version required")
        for value in (
            self.max_upload_bytes,
            self.active_days,
            self.backup_days,
            self.audit_days,
            self.user_daily_runs,
            self.user_active_runs,
            self.daily_runs,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("positive limits required")
        if (
            self.max_upload_bytes > 150 * 1024 * 1024
            or self.active_days > 7
            or self.backup_days > 14
            or self.audit_days > 30
        ):
            raise ValueError("limits exceed P1 ceilings")
        if not self.active_days <= self.backup_days <= self.audit_days:
            raise ValueError("retention order invalid")
        # DEC-191: the operator-approved daily ceiling is USD 15; run and event caps are unchanged.
        for amount, ceiling in ((self.run_usd, "1.0"), (self.day_usd, "15")):
            if not 0 < micros(amount) <= micros(ceiling):
                raise ValueError("budget exceeds DEC-22 ceilings")
        if not 0 < micros(self.event_usd) <= micros(self.event_usd_ceiling):
            raise ValueError("budget exceeds DEC-22 ceilings")

    @classmethod
    def from_env(cls, env: Any) -> HostedSettings:
        def flag(name: str, default: str) -> bool:
            value = env.get(name, default)
            if value not in {"true", "false"}:
                raise ValueError(f"invalid {name}")
            return value == "true"

        if env.get("APP_ENV") != "hosted" or env.get("MODEL_ADAPTER", "disabled") != "disabled":
            raise ValueError("explicit hosted/disabled composition required")
        return cls(
            root=Path(env.get("HOSTED_DATA_DIR", "")),
            origin=env.get("APP_ORIGIN", ""),
            secret=env.get("HOSTED_SESSION_SECRET", ""),
            event_id=env.get("HOSTED_EVENT_ID", ""),
            accept_uploads=flag("HOSTED_ACCEPT_UPLOADS", "false"),
            consent_version=env.get("HOSTED_CONSENT_VERSION", ""),
            backend=env.get("ANALYSIS_BACKEND", "static"),
            accept_runs=flag("R108_ACCEPT_NEW_RUNS", "false"),
            rc1_reuse_enabled=flag("RC1_RESULT_REUSE_ENABLED", "false"),
            live_disabled=flag("LIVE_DISABLED", "true"),
            live_config=Path(env["HOSTED_LIVE_CONFIG"]) if env.get("HOSTED_LIVE_CONFIG") else None,
            max_upload_bytes=int(env.get("HOSTED_MAX_UPLOAD_BYTES", "157286400")),
            active_days=int(env.get("HOSTED_ACTIVE_DAYS", "7")),
            backup_days=int(env.get("HOSTED_BACKUP_DAYS", "14")),
            audit_days=int(env.get("HOSTED_AUDIT_DAYS", "30")),
            run_usd=Decimal(env.get("HOSTED_RUN_USD", "1.0")),
            day_usd=Decimal(env.get("HOSTED_DAY_USD", "7")),
            event_usd=Decimal(env.get("HOSTED_EVENT_USD", "7")),
            event_usd_ceiling=Decimal(env.get("HOSTED_EVENT_USD_CEILING", "7")),
            user_daily_runs=int(env.get("HOSTED_USER_DAILY_RUNS", "6")),
            max_active_runs=int(env.get("HOSTED_MAX_ACTIVE_RUNS", "1")),
            user_active_runs=int(env.get("HOSTED_USER_ACTIVE_RUNS", "1")),
            daily_runs=int(env.get("HOSTED_DAILY_RUNS", "10")),
            retain_failed_analysis=flag("HOSTED_RETAIN_FAILED_ANALYSIS", "true"),
            failure_diagnostics_max_bytes=int(
                env.get("HOSTED_FAILURE_DIAGNOSTICS_MAX_BYTES", "536870912")
            ),
            access_config=Path(env.get("HOSTED_ACCESS_CONFIG", "/run/private/access.json")),
            auth_trusted_proxies=tuple(
                value.strip()
                for value in env.get("HOSTED_AUTH_TRUSTED_PROXIES", "").split(",")
                if value.strip()
            ),
            guest_access_until=datetime.fromisoformat(env["HOSTED_GUEST_ACCESS_UNTIL"])
            if env.get("HOSTED_GUEST_ACCESS_UNTIL")
            else None,
            guest_fresh_runs_per_day=int(env.get("HOSTED_GUEST_FRESH_RUNS_PER_DAY", "2")),
            guest_sessions_per_ip_hour=int(env.get("HOSTED_GUEST_SESSIONS_PER_IP_HOUR", "30")),
            guest_uploads_per_day=int(env.get("HOSTED_GUEST_UPLOADS_PER_DAY", "60")),
            showcase_tenant=env.get("HOSTED_SHOWCASE_TENANT", ""),
        )


class SQLiteSessions:
    def __init__(self, store: HostedStore):
        self.store = store

    def csrf_token_for(self, sid: str) -> str:
        return hmac.new(
            self.store.settings.secret.encode(), sid.encode(), hashlib.sha256
        ).hexdigest()

    def get(self, sid: str) -> SessionRecord | None:
        with self.store.transaction(readonly=True) as db:
            row = db.execute("SELECT * FROM sessions WHERE sid_hash=?", (digest(sid),)).fetchone()
            valid = row is not None and self.store.access.session_valid(db, row, time.time())
            if row is not None and row["user_id"].startswith("guest:"):
                valid = valid and self.store.guest.available(time.time())
        if row is None:
            return None
        return SessionRecord(
            sid,
            row["user_id"],
            row["tenant_id"],
            row["csrf_hash"],
            row["expires"],
            row["idle"],
            bool(row["revoked"]) or not valid,
        )

    def revoke(self, sid: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE sessions SET revoked=1 WHERE sid_hash=?", (digest(sid),))

    def touch(self, sid: str, now: float) -> None:
        with self.store.transaction() as db:
            db.execute(
                "UPDATE sessions SET idle=min(expires,?) WHERE sid_hash=? AND revoked=0 "
                "AND expires>? AND idle>?",
                (now + 1800, digest(sid), now, now),
            )

    def set_active_tenant(self, sid: str, tenant_id: str, *, now: float) -> SessionRecord:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM sessions WHERE sid_hash=?", (digest(sid),)).fetchone()
            if row is None or row["revoked"] or now >= min(row["expires"], row["idle"]):
                raise SessionExpiredError()
            new_id = secrets.token_urlsafe(32)
            db.execute("UPDATE sessions SET revoked=1 WHERE sid_hash=?", (digest(sid),))
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,0)",
                (
                    digest(new_id),
                    row["user_id"],
                    tenant_id,
                    digest(self.csrf_token_for(new_id)),
                    row["expires"],
                    min(row["expires"], now + 1800),
                ),
            )
            db.execute(
                "INSERT INTO access_sessions SELECT ?,role,session_epoch FROM access_sessions "
                "WHERE sid_hash=?",
                (digest(new_id), digest(sid)),
            )
        record = self.get(new_id)
        assert record is not None
        return record


class SQLiteMemberships:
    def __init__(self, store: HostedStore):
        self.store = store

    def get(self, tenant_id: str, user_sub: str) -> MembershipRecord | None:
        with self.store.transaction(readonly=True) as db:
            row = db.execute(
                "SELECT role,status FROM memberships WHERE tenant_id=? AND user_id=?",
                (tenant_id, user_sub),
            ).fetchone()
        return MembershipRecord(tenant_id, user_sub, row[0], row[1]) if row else None

    def tenant_exists(self, tenant_id: str) -> bool:
        with self.store.transaction(readonly=True) as db:
            return (
                db.execute("SELECT 1 FROM memberships WHERE tenant_id=?", (tenant_id,)).fetchone()
                is not None
            )


class HostedStore:
    app_env = "hosted"

    def __init__(self, settings: HostedSettings):
        self.settings = settings
        self.root = settings.root
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / "control.sqlite3"
        self.ledger = self.root / "deletions.sqlite3"
        self.sessions = SQLiteSessions(self)
        self.memberships = SQLiteMemberships(self)
        from proofops_api.hosted_access import AccessPasswords
        from proofops_api.hosted_guest import GuestAccess

        self.access = AccessPasswords(self)
        self.guest = GuestAccess(self)
        with self.transaction() as db:
            self.access.migrate(db)
            self.guest.migrate(db)
            db.execute("CREATE TABLE IF NOT EXISTS hosted_schema (version INTEGER PRIMARY KEY)")
            versions = db.execute("SELECT version FROM hosted_schema").fetchall()
            if versions and [row[0] for row in versions] != [1]:
                raise ValueError("unsupported hosted schema")
            db.execute("INSERT OR IGNORE INTO hosted_schema VALUES (1)")
            db.execute("""CREATE TABLE IF NOT EXISTS ops2_provider_observations (
                provider TEXT PRIMARY KEY, observed REAL NOT NULL, error_code TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS admission_control (
                id INTEGER PRIMARY KEY CHECK(id=1),
                draining INTEGER NOT NULL CHECK(draining IN (0,1)), updated REAL NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO admission_control VALUES (1,0,0)")
            db.execute("""CREATE TABLE IF NOT EXISTS sessions (
                sid_hash TEXT PRIMARY KEY, user_id TEXT, tenant_id TEXT, csrf_hash TEXT,
                expires REAL, idle REAL, revoked INTEGER)""")
            db.execute("""CREATE TABLE IF NOT EXISTS memberships (
                tenant_id TEXT, user_id TEXT, role TEXT, status TEXT,
                PRIMARY KEY(tenant_id,user_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS invitations (
                code_hash TEXT PRIMARY KEY, tenant_id TEXT, user_id TEXT, role TEXT,
                expires REAL, used INTEGER DEFAULT 0)""")
            db.execute("""CREATE TABLE IF NOT EXISTS documents (
                tenant_id TEXT, document_id TEXT, sha256 TEXT, page_count INTEGER,
                size_bytes INTEGER, created REAL, expires REAL, consent_version TEXT,
                PRIMARY KEY(tenant_id,document_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS document_owners (
                tenant_id TEXT, document_id TEXT, user_hash TEXT,
                PRIMARY KEY(tenant_id,document_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS run_progress (
                run_id TEXT PRIMARY KEY, stage TEXT, progress INTEGER, result_ready INTEGER)""")
            db.execute("""CREATE TABLE IF NOT EXISTS run_claim_progress (
                run_id TEXT PRIMARY KEY, extracted INTEGER NOT NULL, processed INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS run_claim_stage_progress (
                run_id TEXT NOT NULL, stage TEXT NOT NULL, completed INTEGER NOT NULL,
                PRIMARY KEY(run_id, stage))""")
            db.execute("""CREATE TABLE IF NOT EXISTS result_snapshots (
                run_id TEXT, revision INTEGER, sha256 TEXT NOT NULL,
                PRIMARY KEY(run_id,revision))""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS result_snapshot_update
                BEFORE UPDATE ON result_snapshots
                BEGIN SELECT RAISE(ABORT,'immutable result snapshot'); END""")
            db.execute("""CREATE TABLE IF NOT EXISTS runs (
                tenant_id TEXT, run_id TEXT PRIMARY KEY, document_id TEXT, user_hash TEXT,
                pages TEXT, status TEXT, created REAL, day TEXT, event_id TEXT,
                envelope INTEGER, fence INTEGER DEFAULT 0, lease_until REAL DEFAULT 0,
                error_code TEXT, prepared_sha256 TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS idempotency (
                tenant_id TEXT, user_hash TEXT, route TEXT, key_hash TEXT, request_hash TEXT,
                resource_id TEXT, document_id TEXT, created REAL,
                PRIMARY KEY(tenant_id,user_hash,route,key_hash))""")
            db.execute("""CREATE TABLE IF NOT EXISTS deletion.costs (
                run_id TEXT, call_id TEXT, request_id TEXT, provider TEXT, input_sha256 TEXT,
                bound INTEGER, actual INTEGER, status TEXT, created REAL,
                PRIMARY KEY(run_id,call_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS deletion.spend (
                run_id TEXT PRIMARY KEY, user_hash TEXT, day TEXT, event_id TEXT,
                amount INTEGER, created REAL, status TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS deletion.source_retention (
                tenant_id TEXT NOT NULL, run_id TEXT NOT NULL, document_id TEXT NOT NULL,
                expires REAL NOT NULL, created REAL NOT NULL,
                PRIMARY KEY(tenant_id,run_id))""")
            db.execute(
                "INSERT OR IGNORE INTO deletion.source_retention "
                "SELECT r.tenant_id,r.run_id,r.document_id,d.expires,r.created "
                "FROM runs r JOIN documents d USING(tenant_id,document_id)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS deletion.event_totals (
                event_id TEXT PRIMARY KEY, amount INTEGER NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS rate_limits (
                subject TEXT, bucket INTEGER, count INTEGER, PRIMARY KEY(subject,bucket))""")
            db.execute("""CREATE TABLE IF NOT EXISTS backups (
                path TEXT PRIMARY KEY, created REAL, expires REAL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS deletion.identity (
                id INTEGER PRIMARY KEY CHECK(id=1), ledger_id TEXT NOT NULL)""")
            db.execute("INSERT OR IGNORE INTO deletion.identity VALUES (1,?)", (str(uuid4()),))
            db.execute("""CREATE TABLE IF NOT EXISTS deletion.tombstones (
                tenant_id TEXT, document_id TEXT, deletion_id TEXT, created REAL,
                key_hash TEXT, PRIMARY KEY(tenant_id,document_id))""")
            db.execute("""CREATE TABLE IF NOT EXISTS deletion.attempts (
                tenant_id TEXT, document_id TEXT, status TEXT, updated REAL,
                PRIMARY KEY(tenant_id,document_id))""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS deletion.tombstone_update
                BEFORE UPDATE ON tombstones
                BEGIN SELECT RAISE(ABORT,'immutable tombstone'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS deletion.tombstone_delete
                BEFORE DELETE ON tombstones WHEN retention_expired(OLD.created)=0
                BEGIN SELECT RAISE(ABORT,'immutable tombstone'); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS deletion.tombstone_insert
                BEFORE INSERT ON tombstones WHEN EXISTS(SELECT 1 FROM tombstones
                    WHERE tenant_id=NEW.tenant_id AND document_id=NEW.document_id)
                BEGIN SELECT RAISE(ABORT,'immutable tombstone'); END""")
            from proofops_api.hosted_reuse import migrate

            migrate(db)
        for path in (self.path, self.ledger):
            os.chmod(path, 0o600)
        with self.transaction() as db:
            # Lost control state never makes admitted/unknown dispatch safe to retry.
            db.execute(
                "UPDATE deletion.spend SET status='accounting_pending' WHERE status='reserved' "
                "AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.run_id=deletion.spend.run_id)"
            )
        self.replay_deletions(now=time.time())

    @contextmanager
    def transaction(
        self,
        *,
        expiry_cutoff: float | None = None,
        readonly: bool = False,
        lock_wait_seconds: float | None = None,
        operation: str = "transaction",
    ) -> Iterator[sqlite3.Connection]:
        # Keep rollback journals: WAL loses cross-file atomicity for the attached
        # money/tombstone ledger. Reads need a snapshot, not a reserved writer.
        def acquire():
            db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.create_function(
                "retention_expired",
                1,
                lambda created: int(expiry_cutoff is not None and created <= expiry_cutoff),
            )
            try:
                # ATTACH itself can encounter a pending writer. Recreate the
                # connection on BUSY, before any caller side effects can execute.
                db.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
                db.execute("ATTACH DATABASE ? AS deletion", (str(self.ledger),))
                if readonly:
                    db.execute("PRAGMA query_only=ON")
                    db.execute("BEGIN")
                    # Take both read locks before yielding to avoid a reader /
                    # attached-ledger commit lock cycle.
                    for schema in ("main", "deletion"):
                        db.execute(
                            f"SELECT rootpage FROM {schema}.sqlite_master LIMIT 1"
                        ).fetchall()
                else:
                    db.execute("PRAGMA secure_delete=ON")
                    db.execute("BEGIN IMMEDIATE")
                return db
            except BaseException:
                db.rollback()
                db.close()
                raise

        db = retry_busy(acquire, phase=f"{operation}:acquire", wait_seconds=lock_wait_seconds)
        try:
            started = time.monotonic()
            yield db
            # A busy COMMIT keeps this exact transaction open. Retry COMMIT only;
            # yielding the body again would duplicate files or provider effects.
            retry_busy(db.commit, phase=f"{operation}:commit", wait_seconds=lock_wait_seconds)
            elapsed = time.monotonic() - started
            if not readonly and elapsed >= 1:
                logging.getLogger(__name__).warning(
                    "hosted sqlite writer operation=%s elapsed_ms=%d",
                    operation,
                    int(elapsed * 1000),
                )
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def user_hash(self, user: str) -> str:
        return hmac.new(self.settings.secret.encode(), user.encode(), hashlib.sha256).hexdigest()

    def invite(self, tenant: str, user: str, role: str, *, now: float) -> str:
        identifier(tenant)
        MembershipRecord(tenant, user, role, "active")  # type: ignore[arg-type]
        if not user or len(user) > 200 or user.startswith(("access-password:", "guest:")):
            raise ValueError("invalid user")
        code = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute(
                "INSERT INTO invitations VALUES (?,?,?,?,?,0)",
                (digest(code), tenant, user, role, now + 86400),
            )
        return code

    def redeem(self, code: str, *, now: float) -> str:
        sid = secrets.token_urlsafe(32)
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM invitations WHERE code_hash=?", (digest(code),)
            ).fetchone()
            if row is None or row["used"] or now >= row["expires"]:
                raise HostedError("INVITATION_INVALID", 401)
            db.execute("UPDATE invitations SET used=1 WHERE code_hash=?", (digest(code),))
            # Invitations cannot silently restore a revoked membership or change a role.
            existing = db.execute(
                "SELECT role,status FROM memberships WHERE tenant_id=? AND user_id=?",
                (row["tenant_id"], row["user_id"]),
            ).fetchone()
            if existing and (existing["status"] != "active" or existing["role"] != row["role"]):
                raise HostedError("INVITATION_INVALID", 401)
            db.execute(
                "INSERT OR IGNORE INTO memberships VALUES (?,?,?,'active')",
                (row["tenant_id"], row["user_id"], row["role"]),
            )
            db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?,?,?,0)",
                (
                    digest(sid),
                    row["user_id"],
                    row["tenant_id"],
                    digest(self.sessions.csrf_token_for(sid)),
                    now + 28800,
                    now + 1800,
                ),
            )
        return sid

    def consume(self, subject: str, limit: int, now: float) -> None:
        with self.transaction() as db:
            bucket = int(now // 60)
            hashed = self.user_hash(subject)
            row = db.execute(
                "SELECT count FROM rate_limits WHERE subject=? AND bucket=?", (hashed, bucket)
            ).fetchone()
            if row and row[0] >= limit:
                raise HostedError("RATE_LIMITED", 429)
            db.execute(
                "INSERT INTO rate_limits VALUES (?,?,1) ON CONFLICT(subject,bucket) "
                "DO UPDATE SET count=count+1",
                (hashed, bucket),
            )

    def document_dir(self, tenant: str, document: str) -> Path:
        return self.root / "payloads" / identifier(tenant) / identifier(document)

    @staticmethod
    def _deleted(db: sqlite3.Connection, tenant: str, document: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM deletion.tombstones WHERE tenant_id=? AND document_id=?",
                (tenant, document),
            ).fetchone()
            is not None
        )

    def _document(self, db: sqlite3.Connection, tenant: str, document: str) -> sqlite3.Row:
        identifier(tenant)
        identifier(document)
        row = db.execute(
            "SELECT * FROM documents WHERE tenant_id=? AND document_id=?", (tenant, document)
        ).fetchone()
        if row is None or self._deleted(db, tenant, document):
            raise HostedError("RESOURCE_NOT_FOUND", 404)
        return row

    def get_document(self, tenant: str, document: str) -> dict:
        with self.transaction(readonly=True) as db:
            return dict(self._document(db, tenant, document)) | self.ux1_document_metadata(
                tenant, document
            )

    def ux1_document_metadata(self, tenant: str, document: str) -> dict:
        # Caller must first enforce document visibility/tombstones using _document or collection.
        path = self.document_dir(tenant, document) / "ux1-metadata.json"
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        return {"ux1_metadata": value} if isinstance(value, dict) else {}

    def require_owned_resource(self, auth: Any, kind: str, resource: str) -> None:
        """Opaque denial for organizer resources, including nested source/result routes."""
        from proofops_api.hosted_access import is_organizer

        if not is_organizer(auth):
            return
        with self.transaction(readonly=True) as db:
            if kind == "runs":
                row = self._run(db, auth.tenant_id, resource)
                if row["user_hash"] != self.user_hash(auth.user_sub):
                    raise HostedError("RESOURCE_NOT_FOUND", 404)
                resource = row["document_id"]
            self._require_document_owner(db, auth, resource)

    def _require_document_owner(self, db: Any, auth: Any, document: str) -> None:
        from proofops_api.hosted_access import is_organizer
        from proofops_api.hosted_guest import ShowcaseReader

        if is_organizer(auth) and not isinstance(auth, ShowcaseReader):
            owner = db.execute(
                "SELECT user_hash FROM document_owners WHERE tenant_id=? AND document_id=?",
                (auth.tenant_id, document),
            ).fetchone()
            if owner is None or owner[0] != self.user_hash(auth.user_sub):
                raise HostedError("RESOURCE_NOT_FOUND", 404)

    def _replay(
        self, db: sqlite3.Connection, auth: Any, route: str, key: str, body_hash: str
    ) -> str | None:
        if not 16 <= len(key) <= 128:
            raise HostedError("IDEMPOTENCY_KEY_INVALID", 400)
        row = db.execute(
            "SELECT * FROM idempotency WHERE tenant_id=? AND user_hash=? AND route=? AND "
            "key_hash=?",
            (auth.tenant_id, self.user_hash(auth.user_sub), route, digest(key)),
        ).fetchone()
        if row is None:
            return None
        if row["request_hash"] != body_hash:
            raise HostedError("IDEMPOTENCY_CONFLICT")
        self._document(db, auth.tenant_id, row["document_id"])
        return row["resource_id"]

    def _remember(
        self,
        db: sqlite3.Connection,
        auth: Any,
        route: str,
        key: str,
        body_hash: str,
        resource: str,
        document: str,
        now: float,
    ) -> None:
        db.execute(
            "INSERT INTO idempotency VALUES (?,?,?,?,?,?,?,?)",
            (
                auth.tenant_id,
                self.user_hash(auth.user_sub),
                route,
                digest(key),
                body_hash,
                resource,
                document,
                now,
            ),
        )
        self.guest.audit(db, auth, route, resource, now)

    def upload(self, auth: Any, content: bytes, key: str, *, now: float) -> dict:
        """Compatibility for bounded local callers; HTTP uses upload_file directly."""
        with NamedTemporaryFile(dir=self.settings.root) as staged:
            staged.write(content)
            staged.flush()
            return self.upload_file(auth, Path(staged.name), key, now=now)

    def upload_file(
        self, auth: Any, source: Path, key: str, *, now: float, ux1_metadata: dict | None = None
    ) -> dict:
        if not auth.has_capability("editor"):
            raise HostedError("FORBIDDEN", 403)
        if not self.settings.accept_uploads or not self.settings.consent_version:
            raise HostedError("UPLOAD_DISABLED", 503)
        size = source.stat().st_size
        if size > self.settings.max_upload_bytes:
            raise HostedError("PAYLOAD_TOO_LARGE", 413)
        sha = file_sha256(source)
        with self.transaction() as db:
            replay = self._replay(db, auth, "upload", key, sha)
            if replay:
                return dict(
                    self._document(db, auth.tenant_id, replay)
                ) | self.ux1_document_metadata(auth.tenant_id, replay)
            self.guest.check_upload(db, auth, now)
        doc = str(uuid4())
        with TemporaryDirectory(prefix="proofops-upload-", dir=self.settings.root) as scratch:
            security: dict[str, Any] = {}
            derived = Path(scratch) / "decrypted.pdf"
            try:
                pages = verify_pdf_file(
                    source,
                    PdfLimits(
                        policy_profile=HOSTED_NULL_NAVIGATION_PROFILE,
                        max_bytes=self.settings.max_upload_bytes,
                        max_decoded_bytes=512 * 1024 * 1024,
                    ),
                    expected_size=size,
                    expected_sha256=sha,
                    derived_path=derived,
                    security_metadata=security,
                )
            except UploadRejected as exc:
                raise HostedError(str(exc), 422) from None
            with self.transaction() as db:
                replay = self._replay(db, auth, "upload", key, sha)
                if replay:
                    return dict(
                        self._document(db, auth.tenant_id, replay)
                    ) | self.ux1_document_metadata(auth.tenant_id, replay)
                self.guest.check_upload(db, auth, now)
                path = self.document_dir(auth.tenant_id, doc)
                path.mkdir(parents=True, mode=0o700)
                copy_verified_file(source, path / "original.pdf", sha)
                if security.get("encrypted"):
                    copy_verified_file(derived, path / "decrypted.pdf", security["analysis_sha256"])
                self._write(path / "upload-security.json", json.dumps(security).encode())
                if ux1_metadata is not None:
                    self._write(path / "ux1-metadata.json", json.dumps(ux1_metadata).encode())
                db.execute(
                    "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                    (
                        auth.tenant_id,
                        doc,
                        sha,
                        pages,
                        size,
                        now,
                        now + self.settings.active_days * 86400,
                        self.settings.consent_version,
                    ),
                )
                db.execute(
                    "INSERT INTO document_owners VALUES (?,?,?)",
                    (auth.tenant_id, doc, self.user_hash(auth.user_sub)),
                )
                self._remember(db, auth, "upload", key, sha, doc, doc, now)
                return dict(self._document(db, auth.tenant_id, doc)) | self.ux1_document_metadata(
                    auth.tenant_id, doc
                )

    def analysis_source(self, directory: Path, document: dict) -> Path:
        """Tenant-owned immutable original plus verified, byte-bound plaintext derivative."""
        original = directory / "original.pdf"
        if original.is_symlink() or file_sha256(original) != document["sha256"]:
            raise HostedError("UPLOAD_INTEGRITY_MISMATCH", 422)
        sidecar = directory / "upload-security.json"
        if not sidecar.exists():
            return original  # Pre-FX6b documents have no derivative metadata.
        if sidecar.is_symlink():
            raise HostedError("UPLOAD_INTEGRITY_MISMATCH", 422)
        metadata = json.loads(sidecar.read_text())
        if (
            metadata.get("source_sha256") != document["sha256"]
            or metadata.get("original_inspection") != "complete"
        ):
            raise HostedError("UPLOAD_INTEGRITY_MISMATCH", 422)
        if not metadata.get("encrypted"):
            return original
        derived = directory / "decrypted.pdf"
        if (
            not derived.is_file()
            or derived.is_symlink()
            or file_sha256(derived) != metadata.get("analysis_sha256")
        ):
            raise HostedError("UPLOAD_INTEGRITY_MISMATCH", 422)
        return derived

    @staticmethod
    def _write(path: Path, content: bytes) -> None:
        temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("xb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def _project_run(
        self, row: sqlite3.Row, db: sqlite3.Connection, *, inline_result: bool = True
    ) -> dict:
        progress = db.execute(
            "SELECT * FROM run_progress WHERE run_id=?", (row["run_id"],)
        ).fetchone()
        counts = db.execute(
            "SELECT * FROM run_claim_progress WHERE run_id=?", (row["run_id"],)
        ).fetchone()
        queue = db.execute(
            "SELECT count(*) FROM runs WHERE status='queued' "
            "AND (created<? OR (created=? AND run_id<=?))",
            (row["created"], row["created"], row["run_id"]),
        ).fetchone()[0]
        ready = bool(
            progress
            and progress["result_ready"]
            and row["status"] in {"completed", "partial_blocked"}
        )
        result = self._result(db, row) if ready and inline_result else None
        raw_error = row["error_code"] or ""
        provider = "openai" if raw_error.startswith("OPENAI_") else "openrouter"
        cause = provider_error_code(provider, raw_error)
        user_counts = db.execute(
            "SELECT count(*),coalesce(sum(status='running'),0) FROM runs WHERE user_hash=? "
            "AND status IN ('queued','running','accounting_pending')",
            (row["user_hash"],),
        ).fetchone()
        return {
            "run_id": row["run_id"],
            "document_id": row["document_id"],
            "status": row["status"],
            "selected_pages": json.loads(row["pages"]),
            "claim_pages": self.run_options(row).get("claim_pages", json.loads(row["pages"])),
            "pipeline_profile": self.run_options(row).get("pipeline_profile", "strict-v1"),
            "status_url": f'/v1/runs/{row["run_id"]}',
            "profile": "r108-hosted-sprint5-v1",
            "stage": progress["stage"] if progress else row["status"],
            "progress": progress["progress"] / 100 if progress else 0,
            "claims_extracted": counts["extracted"] if counts else 0,
            "claims_processed": counts["processed"] if counts else 0,
            "claim_stage_counts": {"preliminary": 0, "relation": 0, "elements": 0}
            | {
                stage: completed
                for stage, completed in db.execute(
                    "SELECT stage,completed FROM run_claim_stage_progress WHERE run_id=?",
                    (row["run_id"],),
                )
            },
            "queue_position": queue if row["status"] == "queued" else None,
            "user_active_count": user_counts[0],
            "user_processing_count": user_counts[1],
            "result_url": f'/v1/runs/{row["run_id"]}/result' if ready else None,
            "result": result,
            "error_code": row["error_code"],
            "error_message": CONTROL_MESSAGES.get(raw_error)
            or MESSAGES.get(raw_error)
            or PIPELINE_MESSAGES.get(raw_error)
            or (PROVIDER_LIMIT_MESSAGE if row["error_code"] in ALL_PROVIDER_LIMIT_CODES else None),
            "provider_error_code": cause,
            "provider_error_message": MESSAGES.get(cause),
            "retryable": False,
            "prepared_sha256": row["prepared_sha256"],
        }

    def provider_limits(self) -> dict:
        """Observed service availability only; never probe providers on public GETs."""
        limits = {}
        with self.transaction(readonly=True) as db:
            for provider, codes in PROVIDER_LIMIT_CODES.items():
                parameters = tuple(sorted(codes))
                placeholders = ",".join("?" for _ in parameters)
                row = db.execute(
                    f"SELECT error_code,created FROM runs WHERE error_code IN ({placeholders}) "
                    "ORDER BY created DESC,run_id DESC LIMIT 1",
                    parameters,
                ).fetchone()
                observed = db.execute(
                    "SELECT error_code,observed FROM ops2_provider_observations WHERE provider=?",
                    (provider,),
                ).fetchone()
                recovered = bool(
                    observed and observed[0] is None and (row is None or observed[1] >= row[1])
                )
                if observed and observed[0] in codes and (row is None or observed[1] >= row[1]):
                    row = observed
                if recovered:
                    row = None
                limits[provider] = {
                    "status": "blocked" if row else "available" if recovered else "unknown",
                    "error_code": row[0] if row else None,
                    "remaining_usd": None,
                    "source": "observed_http_failure"
                    if row
                    else "observed_http_success"
                    if recovered
                    else "not_queried",
                    "retryable": False,
                    "message": PROVIDER_LIMIT_MESSAGE if row else None,
                }
        return limits

    def _run(self, db: sqlite3.Connection, tenant: str, run: str) -> sqlite3.Row:
        identifier(run)
        row = db.execute(
            "SELECT * FROM runs WHERE tenant_id=? AND run_id=?", (tenant, run)
        ).fetchone()
        if row is None:
            raise HostedError("RESOURCE_NOT_FOUND", 404)
        self._document(db, tenant, row["document_id"])
        return row

    def get_run(self, tenant: str, run: str) -> dict:
        lock = None
        try:
            with self.transaction(readonly=True) as db:
                row = self._run(db, tenant, run)
                result = self._project_run(row, db, inline_result=False)
                if result["result_url"]:
                    # Keep payloads leased without holding the control writer.
                    path = self.document_dir(tenant, row["document_id"]) / "analysis.lock"
                    lock = path.open("a+b")
                    os.fchmod(lock.fileno(), 0o600)
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
            if lock is not None:
                result["result"] = self._result(None, row)
                # A deletion may fence the leased payload while hashes are read.
                # Never expose bytes after its tombstone commits.
                with self.transaction(readonly=True) as db:
                    self._run(db, tenant, run)
            return result
        finally:
            if lock is not None:
                lock.close()

    def _scope(self, db, tenant: str, document: str) -> dict:
        doc = self._document(db, tenant, document)
        if time.time() >= doc["expires"]:
            raise HostedError("SOURCE_EXPIRED")
        paths = sorted(self.document_dir(tenant, document).glob("scope-*.json"))
        if not paths:
            raise HostedError("SCOPE_PLAN_REQUIRED")
        scope = json.loads(paths[-1].read_text())
        from proofops_api.hosted_scope import scope_selection

        if scope["plan"]["source_sha256"] != doc["sha256"]:
            raise HostedError("SOURCE_HASH_MISMATCH")
        if scope["plan"]["status"] != "failed" or scope["selection"] is not None:
            try:
                selection = scope_selection(
                    scope["plan"],
                    doc["sha256"],
                    doc["page_count"],
                    reporting_scope=scope.get("reporting_scope"),
                    manual_override=scope["plan"]["status"] == "failed",
                    manual_selection=(scope["selection"] or {}).get("manual_selection", False),
                )
            except (ValueError, KeyError, TypeError):
                raise HostedError("SCOPE_INTEGRITY_INVALID") from None
            if selection != scope["selection"]:
                raise HostedError("SCOPE_INTEGRITY_INVALID")
        return scope

    def get_scope(self, tenant: str, document: str) -> dict:
        with self.transaction(readonly=True) as db:
            return self._scope(db, tenant, document)

    def generate_scope(self, auth, document: str) -> dict:
        if not auth.has_capability("editor"):
            raise HostedError("FORBIDDEN", 403)
        with self.transaction() as db:
            doc = dict(self._document(db, auth.tenant_id, document))
            path = self.document_dir(auth.tenant_id, document)
            if list(path.glob("scope-*.json")):
                return self._scope(db, auth.tenant_id, document)
            if time.time() >= doc["expires"]:
                raise HostedError("SOURCE_EXPIRED")
        # Share the cross-process upload slot with validation on the 4GiB host.
        with (self.settings.root / "upload.lock").open("a+b") as lease:
            os.fchmod(lease.fileno(), 0o600)
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise HostedError("UPLOAD_BUSY", 429) from None
            with TemporaryDirectory(prefix="proofops-scope-") as scratch:
                output = Path(scratch) / "plan.json"
                try:
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "proofops_api.hosted_scope",
                            str(self.analysis_source(path, doc)),
                            str(output),
                            doc["sha256"],
                            auth.tenant_id,
                        ],
                        timeout=90,
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    plan = json.loads(output.read_text())
                except (subprocess.SubprocessError, OSError, ValueError):
                    from evaluation.hosted_scope_plan import failed_plan

                    plan = failed_plan(doc["sha256"], doc["page_count"], "SCOPE_INSPECTION_FAILED")
        from proofops.domain.provenance import canonical_hash

        from evaluation.reporting_period import (
            detect_reporting_period,
            resolve_reporting_scope,
            validate_detection,
        )
        from proofops_api.hosted_scope import scope_selection

        if "reporting_period_detection" not in plan:
            plan["reporting_period_detection"] = detect_reporting_period(
                {}, doc["page_count"], doc["sha256"]
            )

        detection = plan["reporting_period_detection"]
        validate_detection(detection, doc["sha256"])
        for item in detection["candidates"] + detection["exceptions"]:
            item["source_ref"].update(
                tenant_id=auth.tenant_id, document_id=document, document_version_id=document
            )
        plan["plan_sha256"] = canonical_hash({k: v for k, v in plan.items() if k != "plan_sha256"})
        defaults = None
        if self.settings.live_config is not None:
            try:
                defaults = json.loads(self.settings.live_config.read_text())
            except (OSError, ValueError):
                pass  # No invented default when operator configuration is unavailable.
        period = resolve_reporting_scope(plan["reporting_period_detection"], defaults)
        selection = (
            None
            if plan["status"] == "failed"
            else scope_selection(plan, doc["sha256"], doc["page_count"], reporting_scope=period)
        )
        with self.transaction() as db:
            current = self._document(db, auth.tenant_id, document)
            if time.time() >= current["expires"]:
                raise HostedError("SOURCE_EXPIRED")
            if plan["source_sha256"] != current["sha256"]:
                raise HostedError("SOURCE_HASH_MISMATCH")
            if list(path.glob("scope-*.json")):
                return self._scope(db, auth.tenant_id, document)
            scope = dict(revision=1, plan=plan, selection=selection, reporting_scope=period)
            self._write(path / "scope-000001.json", json.dumps(scope).encode())
            return scope

    def edit_scope(
        self,
        auth,
        document,
        claims,
        evidence,
        if_match,
        *,
        manual_selection=False,
        reporting_scope=None,
    ):
        if not auth.has_capability("editor"):
            raise HostedError("FORBIDDEN", 403)
        with self.transaction() as db:
            scope = self._scope(db, auth.tenant_id, document)
            if if_match is None:
                raise HostedError("IF_MATCH_REQUIRED", 428)
            if if_match != f'"{scope["revision"]}"':
                raise HostedError("SCOPE_CONFLICT", 412)
            doc = self._document(db, auth.tenant_id, document)
            plan = scope["plan"] | dict(
                selected_claim_pages=claims, selected_evidence_pages=evidence
            )
            from evaluation.reporting_period import validate_period
            from proofops_api.hosted_scope import scope_selection

            period = scope.get("reporting_scope")
            if reporting_scope is not None:
                try:
                    value = validate_period(reporting_scope)
                except ValueError:
                    raise HostedError("REPORTING_SCOPE_INVALID", 422) from None
                period = (period or {}) | value | dict(origin="manual", warning=None)
            try:
                selection = scope_selection(
                    plan,
                    doc["sha256"],
                    doc["page_count"],
                    reporting_scope=period,
                    manual_override=plan["status"] == "failed",
                    manual_selection=manual_selection,
                )
            except (ValueError, KeyError, TypeError):
                raise HostedError("PAGE_SELECTION_INVALID", 422) from None
            revision = scope["revision"] + 1
            updated = dict(revision=revision, plan=plan, selection=selection)
            if period is not None:
                updated["reporting_scope"] = period
            self._write(
                self.document_dir(auth.tenant_id, document) / f"scope-{revision:06}.json",
                json.dumps(updated).encode(),
            )
            return updated

    def run_options(self, job) -> dict:
        path = (
            self.document_dir(job["tenant_id"], job["document_id"])
            / f'{job["run_id"]}.options.json'
        )
        if not path.is_file():
            return {}  # Previous <=2-page runs retain their frozen profile.
        options = json.loads(path.read_text())
        from proofops.domain.provenance import canonical_hash

        expected = options.pop("sha256")
        if canonical_hash(options) != expected or options["pages"] != json.loads(job["pages"]):
            raise HostedError("SCOPE_INTEGRITY_INVALID")
        return options

    def create_run(
        self,
        auth: Any,
        document: str,
        pages: list[int] | None,
        key: str,
        *,
        now: float,
        auto_scope: bool = False,
        scope_selection_sha256: str | None = None,
        pipeline_profile: str = "unattended-v1",
        force_fresh: bool = False,
    ) -> dict:
        if not auth.has_capability("editor"):
            raise HostedError("FORBIDDEN", 403)
        if force_fresh and not auth.has_capability("admin"):
            raise HostedError("FORBIDDEN", 403)
        if not self.settings.accept_runs or self.settings.backend != "r108":
            raise HostedError("ANALYSIS_DISABLED", 503)
        with self.transaction() as db:
            doc = self._document(db, auth.tenant_id, document)
            self._require_document_owner(db, auth, document)
            if now >= doc["expires"]:
                raise HostedError("SOURCE_EXPIRED", 409)
            options = None
            if auto_scope and pages is None:
                scope = self._scope(db, auth.tenant_id, document)
                if scope["selection"] is None:
                    raise HostedError("SCOPE_PLAN_FAILED", 422)
                selection = scope["selection"]
                if (
                    scope_selection_sha256
                    and scope_selection_sha256 != selection["selection_sha256"]
                ):
                    raise HostedError("SCOPE_CONFLICT", 412)
                if "reporting_scope" in selection:
                    from evaluation.reporting_period import validate_period

                    try:
                        validate_period(selection["reporting_scope"])
                    except ValueError:
                        raise HostedError("REPORTING_SCOPE_REQUIRED", 422) from None
                selected = selection["pages"]
                options = selection | dict(
                    pipeline_profile=pipeline_profile,
                    source_sha256=doc["sha256"],
                    scope_policy_sha256=scope["plan"]["policy_sha256"],
                )
            else:
                selected = list(range(1, min(2, doc["page_count"]) + 1)) if pages is None else pages
                if auto_scope:
                    options = dict(
                        pages=selected,
                        claim_pages=selected,
                        pipeline_profile=pipeline_profile,
                        source_sha256=doc["sha256"],
                        declared_subset=True,
                    )
            if (
                not isinstance(selected, list)
                or not 1 <= len(selected) <= (30 if options else 2)
                or any(
                    type(page) is not int or not 1 <= page <= doc["page_count"] for page in selected
                )
                or len(set(selected)) != len(selected)
            ):
                raise HostedError("PAGE_SELECTION_INVALID", 422)
            from proofops_api.hosted_analysis import FINALS_PROFILES

            if pipeline_profile not in {"unattended-v1", "strict-v1", *FINALS_PROFILES}:
                raise HostedError("PIPELINE_PROFILE_INVALID", 422)
            if pipeline_profile in FINALS_PROFILES:
                from proofops.application.analysis_profile import publication_policy_for
                from proofops.domain.provenance import canonical_hash

                if options is None:
                    options = dict(
                        pages=selected, claim_pages=selected, source_sha256=doc["sha256"]
                    )
                if len(options["claim_pages"]) > 10:
                    raise HostedError("PAGE_SELECTION_INVALID", 422)
                operator_pin = None
                config = {}
                if self.settings.live_config is not None:
                    config = json.loads(self.settings.live_config.read_text())
                    operator_pin = config.get("analysis_policy_sha256")
                options.update(
                    pipeline_profile=pipeline_profile,
                    analysis_profile=pipeline_profile,
                    analysis_profile_sha256=canonical_hash(
                        publication_policy_for(pipeline_profile, pinned_hash=operator_pin)
                    ),
                )
                from proofops_api.hosted_analysis import operator_profiles

                execution = publication_policy_for(pipeline_profile, pinned_hash=operator_pin)
                if execution.get("provider_scheduler"):
                    profiles = operator_profiles(config | dict(analysis_profile=pipeline_profile))
                    frozen = {
                        k: profiles[k] for k in ("llm_max_workers", "llm_requests_per_second")
                    }
                    options.update(
                        provider_execution=frozen, provider_execution_sha256=canonical_hash(frozen)
                    )
                if self.settings.rc1_reuse_enabled and "reporting_scope" not in options:
                    from evaluation.reporting_period import validate_period

                    try:
                        options["reporting_scope"] = validate_period(config)
                    except ValueError:
                        pass  # Unknown period cannot match a reusable publication.
            body_hash = digest(json.dumps([document, selected, options]))
            if force_fresh:
                body_hash = digest(body_hash + ":force_fresh")
            replay = self._replay(db, auth, "run", key, body_hash)
            if replay:
                return self._project_run(self._run(db, auth.tenant_id, replay), db)
            if self._maintenance(db):
                raise HostedError("MAINTENANCE", 503)
            if not (self.document_dir(auth.tenant_id, document) / "original.pdf").is_file():
                raise HostedError("SOURCE_UNAVAILABLE")
            if self.settings.rc1_reuse_enabled:
                from proofops_api.hosted_reuse import try_reuse

                self.analysis_source(self.document_dir(auth.tenant_id, document), dict(doc))
                if not force_fresh:
                    reused = try_reuse(
                        self, db, auth, doc, options, key=key, body_hash=body_hash, now=now
                    )
                    if reused is not None:
                        return reused
            self.guest.check_fresh_run(db, auth, now)
            user = self.user_hash(auth.user_sub)
            # Per-user slot pressure queues work; admission still reserves the
            # complete envelope and checks the global queue, count and USD caps.
            if db.execute("SELECT count(*) FROM runs WHERE status='queued'").fetchone()[0] >= 8:
                raise HostedError("QUEUE_FULL", 429)
            today = day(now)
            for subject, maximum in (
                (user, self.settings.user_daily_runs),
                (None, self.settings.daily_runs),
            ):
                query = "SELECT count(*) FROM deletion.spend WHERE day=?"
                params: tuple = (today,)
                if subject is not None:
                    query += " AND user_hash=?"
                    params += (subject,)
                if db.execute(query, params).fetchone()[0] >= maximum:
                    raise HostedError("DAILY_RUN_LIMIT", 429)
            bound = micros(self.settings.run_usd)
            for field, value, cap in (
                ("day", today, self.settings.day_usd),
                ("event_id", self.settings.event_id, self.settings.event_usd),
            ):
                total = db.execute(
                    f"SELECT coalesce(sum(amount),0) FROM deletion.spend WHERE {field}=?", (value,)
                ).fetchone()[0]
                if field == "event_id":
                    archived = db.execute(
                        "SELECT amount FROM deletion.event_totals WHERE event_id=?", (value,)
                    ).fetchone()
                    total += archived[0] if archived else 0
                if total + bound > micros(cap):
                    raise HostedError("BUDGET_EXCEEDED", 429)
            run = str(uuid4())
            if options:
                from proofops.domain.provenance import canonical_hash

                options["sha256"] = canonical_hash(options)
                self._write(
                    self.document_dir(auth.tenant_id, document) / f"{run}.options.json",
                    json.dumps(options).encode(),
                )
            db.execute(
                "INSERT INTO runs VALUES (?,?,?,?,?,'queued',?,?,?,?,0,0,NULL,NULL)",
                (
                    auth.tenant_id,
                    run,
                    document,
                    user,
                    json.dumps(selected),
                    now,
                    today,
                    self.settings.event_id,
                    bound,
                ),
            )
            db.execute(
                "INSERT INTO deletion.spend VALUES (?,?,?,?,?,?,'reserved')",
                (run, user, today, self.settings.event_id, bound, now),
            )
            db.execute(
                "INSERT INTO deletion.source_retention VALUES (?,?,?,?,?)",
                (auth.tenant_id, run, document, doc["expires"], now),
            )
            self._remember(db, auth, "run", key, body_hash, run, document, now)
            return self._project_run(self._run(db, auth.tenant_id, run), db)

    @staticmethod
    def _maintenance(db: sqlite3.Connection) -> bool:
        return bool(db.execute("SELECT draining FROM admission_control WHERE id=1").fetchone()[0])

    def _control_state(self, db: sqlite3.Connection) -> dict:
        maintenance = self._maintenance(db)
        counts = db.execute(
            "SELECT coalesce(sum(status='running'),0),coalesce(sum(status='queued'),0) FROM runs"
        ).fetchone()
        return dict(
            maintenance=maintenance,
            message=CONTROL_MESSAGES["MAINTENANCE"] if maintenance else None,
            running=counts[0],
            queued=counts[1],
        )

    def drain(self, *, now: float) -> dict:
        with self.transaction() as db:
            db.execute("UPDATE admission_control SET draining=1,updated=? WHERE id=1", (now,))
            return self._control_state(db)

    def resume(self, *, now: float) -> dict:
        with self.transaction() as db:
            db.execute("UPDATE admission_control SET draining=0,updated=? WHERE id=1", (now,))
            return self._control_state(db)

    def control_state(self, *, now: float) -> dict:
        with self.transaction() as db:
            self._expire_running(db, now)
            return self._control_state(db)

    def _expire_running(self, db: sqlite3.Connection, now: float) -> None:
        expired = db.execute(
            "SELECT run_id FROM runs WHERE (status='running' AND lease_until<=?) "
            "OR (status='accounting_pending' AND error_code='LEASE_EXPIRED')",
            (now,),
        ).fetchall()
        for row in expired:
            # Broker inserts a cost reservation before transport. No rows in
            # the surviving independent ledger proves no provider was sent.
            no_dispatch = db.execute(
                "SELECT 1 FROM deletion.spend WHERE run_id=? AND NOT EXISTS "
                "(SELECT 1 FROM deletion.costs WHERE run_id=?)",
                (row[0], row[0]),
            ).fetchone()
            if no_dispatch:
                db.execute(
                    "UPDATE runs SET status='failed',fence=fence+1,lease_until=0,"
                    "error_code='LEASE_EXPIRED_NO_DISPATCH' WHERE run_id=?",
                    (row[0],),
                )
                self._settle_envelope(db, row[0])
                continue
            db.execute(
                "UPDATE runs SET "
                "status='accounting_pending',fence=fence+1,error_code='LEASE_EXPIRED' WHERE "
                "run_id=?",
                (row[0],),
            )
            db.execute(
                "UPDATE deletion.spend SET status='accounting_pending' WHERE run_id=?",
                (row[0],),
            )

    def claim(self, *, now: float) -> dict | None:
        with self.transaction() as db:
            self._expire_running(db, now)
            if self._maintenance(db):
                return None
            # Only an active fenced worker occupies the slot. Pending accounting
            # remains charged to its envelope/day/event; it cannot dispatch again.
            if db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0] >= (
                self.settings.max_active_runs
            ):
                return None
            row = db.execute(
                "SELECT q.* FROM runs q WHERE q.status='queued' AND NOT EXISTS "
                "(SELECT 1 FROM runs r WHERE r.status='running' AND "
                "r.tenant_id=q.tenant_id AND r.document_id=q.document_id) "
                "AND (SELECT count(*) FROM runs u WHERE u.user_hash=q.user_hash "
                "AND u.status='running')<? ORDER BY q.created,q.run_id LIMIT 1",
                (self.settings.user_active_runs,),
            ).fetchone()
            if row is None:
                return None
            self._document(db, row["tenant_id"], row["document_id"])
            db.execute(
                "UPDATE runs SET status='running',fence=fence+1,lease_until=? WHERE run_id=?",
                (now + RUN_LEASE_SECONDS, row["run_id"]),
            )
            return dict(
                db.execute("SELECT * FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            )

    def _fenced(self, db: sqlite3.Connection, job: dict, now: float) -> sqlite3.Row:
        row = self._run(db, job["tenant_id"], job["run_id"])
        if row["status"] != "running" or row["fence"] != job["fence"] or row["lease_until"] <= now:
            raise HostedError("LEASE_LOST")
        return row

    def renew_lease(self, job: dict, *, now: float) -> None:
        started = time.monotonic()
        # Like the inner job heartbeat, only renewal gets a longer BUSY budget.
        # Acquisition/COMMIT may retry; the fenced body is never replayed.
        with self.transaction(lock_wait_seconds=30, operation="renew_lease") as db:
            current = now + time.monotonic() - started
            row = self._fenced(db, job, current)
            lease_until = max(row["lease_until"], current + RUN_LEASE_SECONDS)
            db.execute(
                "UPDATE runs SET lease_until=? WHERE run_id=?",
                (lease_until, job["run_id"]),
            )
        job["lease_until"] = lease_until

    def prepare(self, job: dict, *, now: float) -> Path:
        # Lease source bytes separately from the DB snapshot; deletion remains
        # fenced throughout copying and the actual prepare subprocess.
        with (
            TemporaryDirectory(prefix="proofops-prepare-", dir=self.settings.root) as scratch,
            ExitStack() as leases,
        ):
            source, output = Path(scratch) / "source.pdf", Path(scratch) / "selected.pdf"
            with self.transaction(readonly=True) as db:
                row = self._fenced(db, job, now)
                doc = self._document(db, job["tenant_id"], job["document_id"])
                if now >= doc["expires"]:
                    raise HostedError("SOURCE_EXPIRED")
                path = self.document_dir(job["tenant_id"], job["document_id"])
                lock = leases.enter_context((path / "analysis.lock").open("a+b"))
                fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                pages = row["pages"]
            analysis = self.analysis_source(path, dict(doc))
            copy_verified_file(analysis, source, file_sha256(analysis))
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "proofops_api.hosted_prepare",
                        str(source),
                        str(output),
                        pages,
                    ],
                    timeout=30,
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except (subprocess.SubprocessError, OSError):
                raise HostedError("PREPARE_FAILED") from None
            if output.stat().st_size > 50 * 1024 * 1024:
                raise HostedError("PREPARE_TOO_LARGE")
            prepared_sha = file_sha256(output)
            prepared = path / f'{job["run_id"]}.prepared.pdf'
            copy_verified_file(output, prepared, prepared_sha)
            with self.transaction() as db:
                self._fenced(db, job, time.time())
                db.execute(
                    "UPDATE runs SET prepared_sha256=? WHERE run_id=?",
                    (prepared_sha, job["run_id"]),
                )
                return prepared

    def dispatch(
        self,
        job: dict,
        provider: str,
        call_id: str,
        input_sha: str,
        bound: Decimal,
        transport: Callable[[dict], dict],
        *,
        now: float,
        allow_transient_403: bool = False,
        allow_model_partial: bool = False,
        allow_response_uncertainty: bool = False,
        allow_connect_not_sent: bool = False,
        allow_ops2_fallback: bool = False,
        diagnostic_context: dict | None = None,
    ) -> dict:
        """Broker boundary for a future approved adapter. No HTTP/provider implementation here."""
        if (
            not provider
            or not call_id
            or len(input_sha) != 64
            or any(c not in "0123456789abcdef" for c in input_sha)
        ):
            raise HostedError("DISPATCH_INVALID", 422)
        reserved = micros(bound)
        if reserved <= 0:
            raise HostedError("DISPATCH_INVALID", 422)
        phase = "reservation"
        try:
            started = time.monotonic()
            with self.transaction(lock_wait_seconds=30, operation="dispatch_reserve") as db:
                row = self._fenced(db, job, now + time.monotonic() - started)
                if db.execute(
                    "SELECT 1 FROM deletion.costs WHERE run_id=? AND call_id=?",
                    (job["run_id"], call_id),
                ).fetchone():
                    raise HostedError("DISPATCH_ALREADY_RECORDED")
                total = db.execute(
                    "SELECT coalesce(sum(CASE WHEN status='settled' THEN actual ELSE bound END),0) "
                    "FROM deletion.costs WHERE run_id=?",
                    (job["run_id"],),
                ).fetchone()[0]
                if total + reserved > row["envelope"]:
                    raise HostedError("BUDGET_EXCEEDED", 429)
                request = {
                    "request_id": str(uuid4()),
                    "provider": provider,
                    "input_sha256": input_sha,
                }
                db.execute(
                    "INSERT INTO deletion.costs VALUES (?,?,?,?,?,?,NULL,'reserved',?)",
                    (
                        job["run_id"],
                        call_id,
                        request["request_id"],
                        provider,
                        input_sha,
                        reserved,
                        now,
                    ),
                )
            phase = "transport"
            response = transport(request)
            phase = "receipt_validation"
            if any(response.get(k) != v for k, v in request.items()):
                raise HostedError("RECEIPT_MISMATCH")
            actual = micros(response["actual_usd"])
            if actual > reserved:
                raise HostedError("RECEIPT_BOUND_EXCEEDED")
            phase = "settlement"
            # A response and its cost already exist; tolerate the same bounded
            # writer contention as lease renewal without replaying transport.
            with self.transaction(lock_wait_seconds=30, operation="dispatch_settle") as db:
                db.execute(
                    "INSERT INTO ops2_provider_observations VALUES (?,?,NULL) "
                    "ON CONFLICT(provider) DO UPDATE SET observed=excluded.observed,"
                    "error_code=NULL "
                    "WHERE excluded.observed>=ops2_provider_observations.observed",
                    (provider, time.time()),
                )
                # A late cancelled/deleted result may settle money, but cannot publish payload.
                db.execute(
                    "UPDATE deletion.costs SET actual=?,status='settled' "
                    "WHERE run_id=? AND call_id=? "
                    "AND status='reserved'",
                    (actual, job["run_id"], call_id),
                )
                state = db.execute(
                    "SELECT status FROM runs WHERE run_id=?", (job["run_id"],)
                ).fetchone()
                if state is None or state[0] in TERMINAL:
                    self._settle_envelope(db, job["run_id"])
            return response
        except BaseException as exc:
            from proofops_api.hosted_diagnostics import record_dispatch_failure

            record_dispatch_failure(
                self, job, call_id, provider, input_sha, phase, exc, diagnostic_context
            )
            if phase == "reservation":
                # Admission failed before transport. Preserve its original error
                # and all money/ownership state; diagnostics never authorize retry.
                raise
            # A rejected quota call is terminal, but its unknown cost still needs
            # operator reconciliation. Never release reservations or redeliver it.
            limit_code = str(exc)
            observation_code = (
                "OPENAI_INSUFFICIENT_QUOTA"
                if provider == "openai"
                and getattr(exc, "provider_error_code", None) == "billing_not_active"
                else limit_code
            )
            quota_blocked = limit_code in PROVIDER_LIMIT_CODES.get(provider, ())
            from proofops_api.hosted_ops2 import fallback_trigger

            ops2_failure = (
                allow_ops2_fallback is True and provider == "openai" and bool(fallback_trigger(exc))
            )
            cause = provider_error_code(provider, limit_code)
            if cause:
                logging.getLogger(__name__).warning(
                    "hosted provider failure run=%s provider=%s cause=%s raw=%s",
                    job["run_id"],
                    provider,
                    cause,
                    limit_code
                    if limit_code.startswith(("UPSTAGE_", "OPENAI_", "OPENROUTER_"))
                    else cause,
                )
            # An uncertain model transport is a per-claim hold, not a lost run fence.
            # Unknown receipts and non-model failures retain the existing terminal guard.
            from proofops.adapters.local.openai import OpenAITransient403
            from proofops.adapters.local.openai_connections import (
                OpenAIConnectNotSent,
                OpenAIControlNotSent,
            )

            if (
                phase == "transport"
                and provider == "openai"
                and (
                    isinstance(exc, OpenAIControlNotSent)
                    or (allow_connect_not_sent is True and isinstance(exc, OpenAIConnectNotSent))
                )
            ):
                # This typed proof originates exclusively before HTTP request().
                # Settle only this reservation. Control stops propagate without retry;
                # a connect retry still needs its existing policy and a new call_id.
                with self.transaction(lock_wait_seconds=30) as db:
                    db.execute(
                        "UPDATE deletion.costs SET actual=0,status='settled' "
                        "WHERE run_id=? AND call_id=? AND status='reserved'",
                        (job["run_id"], call_id),
                    )
                raise
            transient_denial = (
                allow_transient_403 is True
                and provider == "openai"
                and isinstance(exc, OpenAITransient403)
            )
            model_network_failure = provider in {"openai", "openrouter"} and (
                isinstance(exc, OSError) or limit_code == "PROCESSING_UNCERTAIN"
            )
            isolated_model_failure = (
                allow_model_partial is True
                and provider == "openai"
                and (
                    cause in {"PROVIDER_ACCESS_DENIED", "PROVIDER_UNAVAILABLE"}
                    or model_network_failure
                    or (
                        allow_response_uncertainty is True
                        and isinstance(exc, json.JSONDecodeError | UnicodeError | HTTPException)
                    )
                )
            )
            with self.transaction(lock_wait_seconds=30) as db:
                if observation_code in PROVIDER_LIMIT_CODES.get(provider, ()):
                    db.execute(
                        "INSERT INTO ops2_provider_observations VALUES (?,?,?) "
                        "ON CONFLICT(provider) DO UPDATE SET observed=excluded.observed,"
                        "error_code=excluded.error_code "
                        "WHERE excluded.observed>=ops2_provider_observations.observed",
                        (provider, time.time(), observation_code),
                    )
                db.execute(
                    "UPDATE deletion.costs SET status='accounting_pending' "
                    "WHERE run_id=? AND call_id=? "
                    "AND status='reserved'",
                    (job["run_id"], call_id),
                )
                if (
                    not ops2_failure
                    and not model_network_failure
                    and not transient_denial
                    and not isolated_model_failure
                ):
                    db.execute(
                        "UPDATE runs SET "
                        "status=?,fence=fence+1,error_code=?,lease_until=0 "
                        "WHERE run_id=? AND status='running' AND fence=?",
                        (
                            "failed" if quota_blocked else "accounting_pending",
                            limit_code if quota_blocked else cause or "ACCOUNTING_PENDING",
                            job["run_id"],
                            job["fence"],
                        ),
                    )
                db.execute(
                    "UPDATE deletion.spend SET status='accounting_pending' WHERE run_id=?",
                    (job["run_id"],),
                )
            if model_network_failure:
                raise HostedError("PROCESSING_UNCERTAIN") from None
            raise

    def _settle_envelope(self, db: sqlite3.Connection, run: str) -> None:
        unknown = db.execute(
            "SELECT 1 FROM deletion.costs WHERE run_id=? AND status!='settled'", (run,)
        ).fetchone()
        if unknown:
            db.execute(
                "UPDATE deletion.spend SET status='accounting_pending' WHERE run_id=?", (run,)
            )
        else:
            actual = db.execute(
                "SELECT coalesce(sum(actual),0) FROM deletion.costs WHERE run_id=?", (run,)
            ).fetchone()[0]
            db.execute(
                "UPDATE deletion.spend SET amount=?,status='settled' WHERE run_id=?", (actual, run)
            )

    def finish(self, job: dict, status: str, *, now: float) -> None:
        if status not in TERMINAL or status == "completed":
            # Completion is available only through fenced analysis artifact publication.
            raise HostedError("ANALYSIS_RESULT_UNAVAILABLE")
        with self.transaction() as db:
            self._fenced(db, job, now)
            db.execute(
                "UPDATE runs SET status=?,lease_until=0,error_code=? WHERE run_id=?",
                (
                    status,
                    "LIVE_BINDING_UNAVAILABLE" if status == "partial_blocked" else None,
                    job["run_id"],
                ),
            )
            self._settle_envelope(db, job["run_id"])

    def tick(self, *, now: float, analysis=None) -> dict | None:
        job = self.claim(now=now)
        if job is None:
            return None
        prepared = None
        try:
            prepared = self.prepare(job, now=now)
            if analysis is not None or not self.settings.live_disabled:
                from proofops_api.hosted_analysis import execute_analysis

                (analysis or execute_analysis)(self, job, prepared)
            else:
                with TemporaryDirectory(prefix="blocked-", dir=prepared.parent) as scratch:
                    state = Path(scratch)
                    self._write(
                        state / "outcome.json",
                        json.dumps(
                            dict(
                                status="partial_blocked",
                                reason="LIVE_BINDING_UNAVAILABLE",
                                stages=[],
                                provider_calls=0,
                                scope=self.run_options(job),
                                reporting_scope=self.run_options(job).get("reporting_scope"),
                            )
                        ).encode(),
                    )
                    self.publish_analysis(job, state, "partial_blocked", "LIVE_BINDING_UNAVAILABLE")
        except (HostedError, OSError, ValueError, subprocess.SubprocessError) as exc:
            code = (
                exc.code
                if isinstance(exc, HostedError)
                else str(exc)
                if str(exc) in ALL_PROVIDER_LIMIT_CODES | PUBLIC_CODES
                else "ANALYSIS_WORKER_FAILED"
                if prepared is not None
                else "PREPARE_FAILED"
            )
            with self.transaction() as db:
                progress = db.execute(
                    "SELECT stage FROM run_progress WHERE run_id=?", (job["run_id"],)
                ).fetchone()
                stage = (
                    progress[0]
                    if progress and progress[0] in {"parse", "extract", "tag", "result"}
                    else "prepare"
                    if prepared is None
                    else "analysis"
                )
                row = db.execute(
                    "SELECT status,fence,error_code FROM runs WHERE run_id=?", (job["run_id"],)
                ).fetchone()
                if row and (row[2] in MESSAGES or row[2] in ALL_PROVIDER_LIMIT_CODES):
                    code = row[2]
                if row and row[0] == "running" and row[1] == job["fence"]:
                    db.execute(
                        "UPDATE runs SET status='failed',error_code=?,lease_until=0 WHERE "
                        "run_id=?",
                        (code, job["run_id"]),
                    )
                    self._settle_envelope(db, job["run_id"])
            logging.getLogger(__name__).warning(
                "hosted run %s failed: %s (%s) stage=%s location=%s",
                job["run_id"],
                code,
                type(exc).__name__,
                stage,
                failure_location(exc),
            )
        try:
            return self.get_run(job["tenant_id"], job["run_id"])
        except HostedError as exc:
            if exc.status != 404:
                raise
            return None

    def update_progress(
        self,
        job: dict,
        stage: str,
        progress: int,
        *,
        claims_extracted: int | None = None,
        claims_processed: int | None = None,
        claim_stage_counts: dict[str, int] | None = None,
    ) -> None:
        if claim_stage_counts is not None and (
            not isinstance(claim_stage_counts, dict)
            or set(claim_stage_counts) - {"preliminary", "relation", "elements"}
            or any(type(count) is not int or count < 0 for count in claim_stage_counts.values())
        ):
            raise ValueError("invalid claim stage progress")
        for count in (claims_extracted, claims_processed):
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("invalid claim progress")
        with self.transaction(operation="progress") as db:
            now = time.time()
            self._fenced(db, job, now)
            db.execute(
                "UPDATE runs SET lease_until=? WHERE run_id=?",
                (now + RUN_LEASE_SECONDS, job["run_id"]),
            )
            db.execute(
                "INSERT INTO run_progress VALUES (?,?,?,0) ON CONFLICT(run_id) "
                "DO UPDATE SET stage=excluded.stage,progress=excluded.progress",
                (job["run_id"], stage, progress),
            )
            if claims_extracted is not None or claims_processed is not None:
                db.execute(
                    "INSERT INTO run_claim_progress VALUES (?,?,?) ON CONFLICT(run_id) "
                    "DO UPDATE SET extracted=max(extracted,excluded.extracted),"
                    "processed=max(processed,excluded.processed)",
                    (job["run_id"], claims_extracted or 0, claims_processed or 0),
                )
            for claim_stage, completed in (claim_stage_counts or {}).items():
                db.execute(
                    "INSERT INTO run_claim_stage_progress VALUES (?,?,?) ON CONFLICT(run_id,stage) "
                    "DO UPDATE SET completed=max(completed,excluded.completed)",
                    (job["run_id"], claim_stage, completed),
                )

    def publish_analysis(
        self, job: dict, source: Path, status: str, error: str | None, *, publication_handoff=None
    ) -> None:
        from proofops_api.hosted_results import result_snapshot

        if status not in {"completed", "partial_blocked"}:
            raise HostedError("ANALYSIS_RESULT_UNAVAILABLE")
        self.update_progress(job, "result", 95)
        # The worker owns analysis.lock while validating its private workspace.
        # Replay must never hold the control writer or run on a GET request.
        from proofops_api.hosted_publication import reuse_handoff

        pages = json.loads(job["pages"])
        content = reuse_handoff(publication_handoff, source, job, pages)
        if content is None:
            content = result_snapshot(source, job["tenant_id"], pages)
        # Stage potentially large receipts outside the writer; the final fence
        # and same-filesystem rename publish them together with the DB anchor.
        with TemporaryDirectory(prefix="proofops-publication-", dir=self.root) as scratch:
            staged = Path(scratch) / "artifacts"
            shutil.copytree(source, staged)
            # Initial publication is create-only. Flush the snapshot with the
            # receipts before acquiring either DB writer; install its anchor
            # only after rechecking the current run/document/fence.
            snapshot = staged / "result-1.json"
            self._write(snapshot, content)
            snapshot.chmod(0o400)
            snapshot_hash = digest(content)
            with self.transaction(operation="publish") as db:
                self._fenced(db, job, time.time())
                if db.execute(
                    "SELECT 1 FROM result_snapshots WHERE run_id=?", (job["run_id"],)
                ).fetchone():
                    raise HostedError("ANALYSIS_RESULT_UNAVAILABLE")
                target = (
                    self.document_dir(job["tenant_id"], job["document_id"])
                    / "analysis"
                    / job["run_id"]
                )
                target.parent.mkdir(mode=0o700, exist_ok=True)
                os.rename(staged, target)
                db.execute(
                    "INSERT INTO result_snapshots VALUES (?,1,?)", (job["run_id"], snapshot_hash)
                )
                db.execute(
                    "INSERT OR REPLACE INTO run_progress VALUES (?, 'result',100,1)",
                    (job["run_id"],),
                )
                db.execute(
                    "UPDATE runs SET status=?,error_code=?,lease_until=0 WHERE run_id=?",
                    (status, error, job["run_id"]),
                )
                self._settle_envelope(db, job["run_id"])

    def _publish_snapshot(self, db: sqlite3.Connection, run: str, path: Path, content: bytes):
        revision = db.execute(
            "SELECT coalesce(max(revision),0)+1 FROM result_snapshots WHERE run_id=?", (run,)
        ).fetchone()[0]
        target = path / f"result-{revision}.json"
        self._write(target, content)
        target.chmod(0o400)
        db.execute("INSERT INTO result_snapshots VALUES (?,?,?)", (run, revision, digest(content)))

    def _result(self, db: sqlite3.Connection | None, row: sqlite3.Row) -> dict:
        path = self.document_dir(row["tenant_id"], row["document_id"]) / "analysis" / row["run_id"]
        if db is None:
            with self.transaction(readonly=True) as connection:
                anchor = connection.execute(
                    "SELECT revision,sha256 FROM result_snapshots WHERE run_id=? "
                    "ORDER BY revision DESC LIMIT 1",
                    (row["run_id"],),
                ).fetchone()
        else:
            anchor = db.execute(
                "SELECT revision,sha256 FROM result_snapshots WHERE run_id=? "
                "ORDER BY revision DESC LIMIT 1",
                (row["run_id"],),
            ).fetchone()
        try:
            if anchor is None:
                raise ValueError("snapshot not published")
            content = (path / f"result-{anchor['revision']}.json").read_bytes()
            if digest(content) != anchor["sha256"]:
                raise ValueError("snapshot hash mismatch")
            snapshot = json.loads(content)
            if snapshot["tenant_id"] != row["tenant_id"] or snapshot["page_map"] != json.loads(
                row["pages"]
            ):
                raise ValueError("snapshot scope mismatch")
            for relative, expected in snapshot["source_hashes"].items():
                if digest((path / relative).read_bytes()) != expected:
                    raise ValueError("source hash mismatch")
            from proofops.application.pipeline_failures import public_reason_details

            result = snapshot["result"]
            if result.get("reuse", {}).get("reused"):
                from proofops_api.hosted_reuse import verify_copy

                verify_copy(self, row, result)
            if result.get("hold_reasons"):
                return result | dict(
                    hold_reason_details=public_reason_details(result["hold_reasons"])
                )
            return result
        except (OSError, ValueError, KeyError, TypeError):
            raise HostedError("RESULT_INTEGRITY_UNAVAILABLE", 409) from None

    def get_result(self, tenant: str, run: str) -> dict:
        result = self.get_run(tenant, run)
        if result["result_url"] is None:
            raise HostedError("ANALYSIS_RESULT_UNAVAILABLE")
        return result["result"]

    def resolve_review(
        self, auth: Any, run: str, review: str, body: dict, if_match: str | None, key: str
    ) -> dict:
        from proofops_api.hosted_results import resolve_review, result_snapshot

        if not auth.has_capability("reviewer"):
            raise HostedError("FORBIDDEN", 403)
        with self.transaction() as db:
            row = self._run(db, auth.tenant_id, run)
            if row["status"] not in {"completed", "partial_blocked"}:
                raise HostedError("ANALYSIS_RESULT_UNAVAILABLE")
            if db.execute("SELECT 1 FROM rc1_reuse_audit WHERE run_id=?", (run,)).fetchone():
                raise HostedError("REUSED_RESULT_READ_ONLY", 409)
            path = self.document_dir(auth.tenant_id, row["document_id"]) / "analysis" / run
            # Shared payload lease lets GETs continue reading the prior immutable
            # snapshot while one review at a time publishes a validated revision.
            lock = (path.parent.parent / "analysis.lock").open("a+b")
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        try:
            with (path / "review.lock").open("a+b") as review_lock:
                os.fchmod(review_lock.fileno(), 0o600)
                fcntl.flock(review_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                resolved = resolve_review(path, auth, review, body, if_match, key)
                content = result_snapshot(path, auth.tenant_id, json.loads(row["pages"]))
                with self.transaction() as db:
                    self._run(db, auth.tenant_id, run)
                    self._publish_snapshot(db, run, path, content)
                return resolved
        finally:
            lock.close()

    def cancel(self, auth: Any, run: str, *, now: float) -> dict:
        with self.transaction() as db:
            row = self._run(db, auth.tenant_id, run)
            if row["user_hash"] != self.user_hash(auth.user_sub) and not auth.has_capability(
                "admin"
            ):
                raise HostedError("FORBIDDEN", 403)
            db.execute(
                "UPDATE runs SET status='cancelled',fence=fence+1,lease_until=0 WHERE run_id=?",
                (run,),
            )
            self._settle_envelope(db, run)
            self.guest.audit(db, auth, "cancel", run, now)
            return self._project_run(self._run(db, auth.tenant_id, run), db)

    def budget_totals(self) -> dict:
        with self.transaction(readonly=True) as db:
            total = db.execute(
                "SELECT coalesce(sum(amount),0) FROM deletion.spend WHERE event_id=?",
                (self.settings.event_id,),
            ).fetchone()[0]
            archived = db.execute(
                "SELECT amount FROM deletion.event_totals WHERE event_id=?",
                (self.settings.event_id,),
            ).fetchone()
            total += archived[0] if archived else 0
        return {"event_reserved_usd": usd(total)}

    def delete_document(self, auth: Any, document: str, key: str, *, now: float) -> dict:
        if not isinstance(key, str) or not 16 <= len(key) <= 128:
            raise HostedError("IDEMPOTENCY_KEY_INVALID", 400)
        with self.transaction() as db:
            owner = db.execute(
                "SELECT user_hash FROM document_owners WHERE tenant_id=? " "AND document_id=?",
                (auth.tenant_id, document),
            ).fetchone()
            if not auth.has_capability("admin") and (
                owner is None or owner[0] != self.user_hash(auth.user_sub)
            ):
                self._document(db, auth.tenant_id, document)
                raise HostedError("FORBIDDEN", 403)
            row = db.execute(
                "SELECT * FROM deletion.tombstones WHERE tenant_id=? AND document_id=?",
                (auth.tenant_id, document),
            ).fetchone()
            if row is None:
                self._document(db, auth.tenant_id, document)
                db.execute(
                    "INSERT INTO deletion.tombstones VALUES (?,?,?,?,?)",
                    (auth.tenant_id, document, str(uuid4()), now, digest(key)),
                )
                db.execute(
                    "UPDATE runs SET status='cancelled',fence=fence+1,lease_until=0 WHERE "
                    "tenant_id=? AND document_id=?",
                    (auth.tenant_id, document),
                )
                self.guest.audit(db, auth, "delete", document, now)
        # Tombstone is durable before any filesystem deletion.
        return self._purge(auth.tenant_id, document, now)

    def _purge(self, tenant: str, document: str, now: float) -> dict:
        status = "completed"
        try:
            with self.transaction(readonly=True) as db:
                if not self._deleted(db, tenant, document):
                    raise HostedError("TOMBSTONE_REQUIRED")
                backups = db.execute("SELECT path FROM backups").fetchall()
            path = self.document_dir(tenant, document)
            if path.exists():
                lock_path = path / "analysis.lock"
                if lock_path.exists():
                    # The committed tombstone fences new users; keep the existing
                    # document lease while removing bytes, without a DB writer.
                    with lock_path.open("a+b") as lock:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        shutil.rmtree(path)
                else:
                    shutil.rmtree(path)
            for backup in backups:
                target = Path(backup[0]) / "artifacts" / tenant / document
                if target.exists():
                    shutil.rmtree(target)
            with self.transaction() as db:
                if not self._deleted(db, tenant, document):
                    raise HostedError("TOMBSTONE_REQUIRED")
                runs = db.execute(
                    "SELECT run_id FROM runs WHERE tenant_id=? AND document_id=?",
                    (tenant, document),
                ).fetchall()
                for run in runs:
                    self._settle_envelope(db, run[0])
                    db.execute("DELETE FROM run_progress WHERE run_id=?", (run[0],))
                    db.execute("DELETE FROM run_claim_progress WHERE run_id=?", (run[0],))
                    db.execute("DELETE FROM run_claim_stage_progress WHERE run_id=?", (run[0],))
                    db.execute("DELETE FROM result_snapshots WHERE run_id=?", (run[0],))
                db.execute(
                    "DELETE FROM document_owners WHERE tenant_id=? AND document_id=?",
                    (tenant, document),
                )
                db.execute(
                    "DELETE FROM runs WHERE tenant_id=? AND document_id=?", (tenant, document)
                )
                db.execute(
                    "DELETE FROM documents WHERE tenant_id=? AND document_id=?", (tenant, document)
                )
                db.execute(
                    "DELETE FROM idempotency WHERE tenant_id=? AND document_id=?",
                    (tenant, document),
                )
                from proofops_api.hosted_results import clear_receipt_caches

                clear_receipt_caches()
        except (OSError, sqlite3.DatabaseError):
            status = "failed"
        with self.transaction() as db:
            db.execute(
                "INSERT INTO deletion.attempts VALUES (?,?,?,?) ON "
                "CONFLICT(tenant_id,document_id) DO UPDATE SET "
                "status=excluded.status,updated=excluded.updated",
                (tenant, document, status, now),
            )
            row = db.execute(
                "SELECT deletion_id,created FROM deletion.tombstones WHERE tenant_id=? AND "
                "document_id=?",
                (tenant, document),
            ).fetchone()
        return {"deletion_id": row[0], "status": status, "requested_at": row[1]}

    def replay_deletions(self, *, now: float) -> None:
        with self.transaction(readonly=True) as db:
            rows = db.execute("SELECT tenant_id,document_id FROM deletion.tombstones").fetchall()
        for row in rows:
            result = self._purge(row[0], row[1], now)
            if result["status"] != "completed":
                raise HostedError("PURGE_INCOMPLETE", 503)

    def maintain(self, *, now: float) -> None:
        # API and worker both maintain this volume. Serialize filesystem GC
        # separately so slow unlink never reserves the control/ledger writer.
        with (self.root / "maintenance.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            self._maintain(now=now)

    def _maintain(self, *, now: float) -> None:
        with self.transaction() as db:
            self._expire_running(db, now)
            expired = db.execute(
                "SELECT tenant_id,document_id FROM documents WHERE expires<=?", (now,)
            ).fetchall()
            for row in expired:
                if not self._deleted(db, row[0], row[1]):
                    db.execute(
                        "INSERT INTO deletion.tombstones VALUES (?,?,?,?,?)",
                        (row[0], row[1], str(uuid4()), now, digest("expiry")),
                    )
            backups = db.execute("SELECT path FROM backups WHERE expires<=?", (now,)).fetchall()
            db.execute("DELETE FROM sessions WHERE expires<=? OR idle<=? OR revoked=1", (now, now))
            self.access.maintain(db, now, self.settings.audit_days)
            self.guest.maintain(db, now)
            db.execute("DELETE FROM invitations WHERE expires<=? OR used=1", (now,))
            db.execute("DELETE FROM rate_limits WHERE bucket<?", (int(now // 60) - 2,))
            cutoff = now - self.settings.audit_days * 86400
            db.execute("DELETE FROM rc1_reuse_audit WHERE created<?", (cutoff,))
            db.execute("DELETE FROM deletion.source_retention WHERE created<?", (cutoff,))
            # Remove per-run audit after30d, retaining only a subject-free event aggregate.
            # Unreconciled money cannot be discarded to free budget.
            old = db.execute(
                "SELECT run_id,event_id,amount FROM deletion.spend WHERE created<? AND "
                "status='settled'",
                (cutoff,),
            ).fetchall()
            for run in old:
                db.execute(
                    "INSERT INTO deletion.event_totals VALUES (?,?) ON CONFLICT(event_id) "
                    "DO UPDATE SET amount=amount+excluded.amount",
                    (run[1], run[2]),
                )
                db.execute("DELETE FROM deletion.costs WHERE run_id=?", (run[0],))
                db.execute("DELETE FROM deletion.spend WHERE run_id=?", (run[0],))
        for backup in backups:
            target = Path(backup[0])
            if target.exists():
                shutil.rmtree(target)
            with self.transaction() as db:
                db.execute("DELETE FROM backups WHERE path=? AND expires<=?", (backup[0], now))
        self.replay_deletions(now=now)
        with self.transaction(expiry_cutoff=now - self.settings.audit_days * 86400) as db:
            expired = db.execute(
                "SELECT t.tenant_id,t.document_id FROM deletion.tombstones t "
                "JOIN deletion.attempts a USING(tenant_id,document_id) "
                "WHERE t.created<=? AND a.status='completed'",
                (now - self.settings.audit_days * 86400,),
            ).fetchall()
            for row in expired:
                db.execute(
                    "DELETE FROM deletion.tombstones WHERE tenant_id=? AND document_id=?",
                    tuple(row),
                )
                db.execute(
                    "DELETE FROM deletion.attempts WHERE tenant_id=? AND document_id=?", tuple(row)
                )
        # Remove unreferenced files left by a crashed upload; no content enters logs.
        trash = self.root / ".maintenance-trash"
        trash.mkdir(mode=0o700, exist_ok=True)
        with self.transaction() as db:
            known = {
                (row[0], row[1])
                for row in db.execute("SELECT tenant_id,document_id FROM documents")
            }
            payloads = self.root / "payloads"
            if payloads.exists():
                for tenant in payloads.iterdir():
                    for doc in tenant.iterdir():
                        if (tenant.name, doc.name) not in known:
                            # Atomic detachment keeps the upload race fenced.
                            os.rename(doc, trash / str(uuid4()))
        for orphan in trash.iterdir():
            shutil.rmtree(orphan)

    def backup(self, target: Path, *, now: float) -> Path:
        """Operator-only stopped-service backup, allowlist excludes raw bytes and sessions."""
        target = target.resolve()
        if target.exists() or target == self.root or self.root.is_relative_to(target):
            raise ValueError("new backup directory required")
        target.mkdir(parents=True, mode=0o700)
        with self.transaction() as db:
            # SQLite online backup from a separate read connection, while writer fence is held.
            source = sqlite3.connect(self.path)
            destination = sqlite3.connect(target / "control.sqlite3")
            try:
                source.backup(destination)
                destination.execute("PRAGMA secure_delete=ON")
                for table in (
                    "sessions",
                    "invitations",
                    "memberships",
                    "rate_limits",
                    "backups",
                    "access_sessions",
                    "access_login_failures",
                    "access_login_locks",
                    "access_auth_audit",
                    "guest_session_starts",
                    "guest_audit",
                ):
                    destination.execute(f"DELETE FROM {table}")
                destination.commit()
                destination.execute("VACUUM")
            finally:
                source.close()
                destination.close()
            for doc in db.execute("SELECT tenant_id,document_id,created FROM documents"):
                if self._deleted(db, doc[0], doc[1]):
                    continue
                path = self.document_dir(doc[0], doc[1])
                for kind in ("receipt", "graph", "export"):
                    source_dir = path / kind
                    if source_dir.exists():
                        dest = target / "artifacts" / doc[0] / doc[1] / kind
                        shutil.copytree(source_dir, dest)
            oldest = db.execute("SELECT min(created) FROM documents").fetchone()[0]
            expires = (
                min(now, oldest if oldest is not None else now) + self.settings.backup_days * 86400
            )
            db.execute("INSERT INTO backups VALUES (?,?,?)", (str(target), now, expires))
            self._write(
                target / "manifest.json",
                json.dumps(
                    {
                        "schema": 1,
                        "created": now,
                        "expires": expires,
                        "raw_source": "excluded",
                        "ledger_id": db.execute(
                            "SELECT ledger_id FROM deletion.identity WHERE id=1"
                        ).fetchone()[0],
                    }
                ).encode(),
            )
        return target

    def restore(self, backup: Path, *, now: float) -> None:
        """Restore payload/control rows only; never roll back deletion or money ledgers."""
        manifest = json.loads((backup / "manifest.json").read_text())
        if manifest["schema"] != 1 or manifest["expires"] <= now:
            raise HostedError("BACKUP_EXPIRED")
        with self.transaction() as db:
            identity = db.execute("SELECT ledger_id FROM deletion.identity WHERE id=1").fetchone()[
                0
            ]
            if manifest.get("ledger_id") != identity:
                raise HostedError("LEDGER_RECOVERY_REQUIRED", 503)
            db.execute("ATTACH DATABASE ? AS recovered", (str(backup / "control.sqlite3"),))
            # Only still-active document metadata; original deliberately unavailable on restore.
            for row in db.execute(
                "SELECT * FROM recovered.documents WHERE expires>?", (now,)
            ).fetchall():
                if self._deleted(db, row["tenant_id"], row["document_id"]):
                    continue
                db.execute("INSERT OR IGNORE INTO documents VALUES (?,?,?,?,?,?,?,?)", tuple(row))
                source = backup / "artifacts" / row["tenant_id"] / row["document_id"]
                if source.exists():
                    dest = self.document_dir(row["tenant_id"], row["document_id"])
                    shutil.copytree(source, dest, dirs_exist_ok=True)
        self.replay_deletions(now=now)
