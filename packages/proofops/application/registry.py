"""Tenant-scoped company and approved execution-option registry.

The optional SQLite store is local-only. Production must supply its approved
durable adapters before composition can serve traffic.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from types import MappingProxyType
from typing import Any
from uuid import UUID, uuid4

_OPTION_KINDS = ("rights", "consent", "runtime")
_OPTION_STATUSES = ("approved", "unverified", "disabled")
_MODES = ("disclosure", "advertising")
_RULEPACK_STATUSES = ("draft", "validated", "active", "retired")
_PROFILE_ID_FIELDS = {
    "rights": "rights_profile_id",
    "consent": "consent_profile_id",
    "runtime": "runtime_binding_id",
}


class RegistryNotFound(LookupError):
    """Opaque tenant-scoped not-found; never distinguish a foreign identifier."""


class IdempotencyConflict(ValueError):
    """One idempotency key was reused for a different company create request."""


@dataclass(frozen=True, slots=True)
class Company:
    company_id: str
    tenant_id: str
    legal_name: str
    registration_identifier: str | None
    aliases: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class CompanyPage:
    items: tuple[Company, ...]
    next_cursor: str | None
    snapshot_epoch: int


@dataclass(frozen=True, slots=True)
class RuntimeOption:
    id: str
    name: str
    status: str
    reason: str | None
    tenant_id: str
    kind: str
    version: str | None
    sha256: str | None
    artifact: dict[str, object] | None
    approved_by: str | None
    approved_at: str | None
    local_synthetic: bool


@dataclass(frozen=True, slots=True)
class RulePackChoice:
    rule_pack_id: str
    tenant_id: str
    version: str
    sha256: str
    status: str
    mode: str
    effective_date: str
    unresolved_gap_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RuntimeOptions:
    rights_profiles: tuple[RuntimeOption, ...]
    consent_profiles: tuple[RuntimeOption, ...]
    runtime_bindings: tuple[RuntimeOption, ...]
    rule_packs: tuple[RulePackChoice, ...]
    enabled_modes: tuple[str, ...]


class Registry:
    """Small tenant-only registry; ``sqlite`` is the explicit local durable store."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        *,
        active_rule_packs: Callable[[str], tuple[RulePackChoice, ...]] | None = None,
    ) -> None:
        self._active_rule_packs = active_rule_packs
        self._companies: dict[tuple[str, str], Company] = {}
        self._options: dict[tuple[str, str, str], RuntimeOption] = {}
        self._rule_packs: dict[tuple[str, str], RulePackChoice] = {}
        self._enabled_modes: dict[str, set[str]] = {}
        self._idempotency: dict[
            tuple[str, str, str], tuple[tuple[str, tuple[str, ...], str | None], Company]
        ] = {}
        self._snapshots: dict[tuple[str, int], tuple[str, ...]] = {}
        self._epoch = 0
        self._lock = RLock()
        self._database: sqlite3.Connection | None = None
        if database_path is not None:
            self._database = sqlite3.connect(str(database_path), check_same_thread=False)
            self._database.execute(
                "CREATE TABLE IF NOT EXISTS registry_metadata "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            version = self._database.execute(
                "SELECT value FROM registry_metadata WHERE key='schema_version'"
            ).fetchone()
            if version is not None and version[0] != "1":
                self.close()
                raise ValueError("unsupported registry schema version")
            self._database.execute(
                "CREATE TABLE IF NOT EXISTS registry_state "
                "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._database.execute(
                "INSERT OR IGNORE INTO registry_metadata(key, value) VALUES ('schema_version', '1')"
            )
            self._database.commit()
            self._load()

    @classmethod
    def empty(cls) -> Registry:
        return cls()

    @classmethod
    def sqlite(
        cls,
        database_path: str | Path,
        *,
        active_rule_packs: Callable[[str], tuple[RulePackChoice, ...]] | None = None,
    ) -> Registry:
        """Open the local durable adapter; no production/cloud adapter is implied."""
        return cls(database_path, active_rule_packs=active_rule_packs)

    def close(self) -> None:
        """Release the local SQLite connection; safe to call after each test/app shutdown."""
        with self._lock:
            if self._database is not None:
                self._database.close()
                self._database = None

    def with_option(
        self,
        tenant_id: str,
        kind: str,
        option_id: str,
        name: str,
        *,
        status: str = "unverified",
        reason: str | None = None,
        version: str | None = None,
        sha256: str | None = None,
        artifact: Mapping[str, object] | None = None,
        approved_by: str | None = None,
        approved_at: str | None = None,
        local_synthetic: bool = False,
    ) -> Registry:
        _uuid(tenant_id, "tenant_id")
        _uuid(option_id, "option_id")
        if kind not in _OPTION_KINDS:
            raise ValueError(f"unsupported option kind: {kind}")
        if status not in _OPTION_STATUSES:
            raise ValueError(f"unsupported option status: {status}")
        if not name:
            raise ValueError("option name is required")
        stored_artifact = _detached_artifact(artifact) if artifact is not None else None
        if status == "approved":
            if (
                not version
                or stored_artifact is None
                or sha256 != artifact_sha256(stored_artifact)
                or not approved_by
                or not _rfc3339(approved_at)
            ):
                raise ValueError(
                    "approved option requires verified artifact, hash, version, and approval"
                )
            identifier = _PROFILE_ID_FIELDS[kind]
            if (
                stored_artifact.get("tenant_id") != tenant_id
                or stored_artifact.get(identifier) != option_id
                or stored_artifact.get("status") != "approved"
                or stored_artifact.get("version") != version
            ):
                raise ValueError("approved artifact identity does not match registry metadata")
        elif any(
            value is not None
            for value in (version, sha256, stored_artifact, approved_by, approved_at)
        ):
            raise ValueError("only approved options may carry an artifact")
        option = RuntimeOption(
            option_id,
            name,
            status,
            reason,
            tenant_id,
            kind,
            version,
            sha256,
            stored_artifact,
            approved_by,
            approved_at,
            local_synthetic,
        )
        key = (tenant_id, kind, option_id)
        with self._write_operation():
            existing = self._options.get(key)
            if existing is not None:
                if existing == option:
                    return self
                raise ValueError("immutable option revision cannot be overwritten")
            self._options[key] = option
            self._advance()
        return self

    def with_rule_pack(
        self,
        tenant_id: str,
        rule_pack_id: str,
        *,
        version: str,
        sha256: str,
        status: str,
        mode: str,
        effective_date: str,
        unresolved_gap_ids: tuple[str, ...] = (),
    ) -> Registry:
        _uuid(tenant_id, "tenant_id")
        _uuid(rule_pack_id, "rule_pack_id")
        if (
            not version
            or not _sha256(sha256)
            or status not in _RULEPACK_STATUSES
            or mode not in _MODES
        ):
            raise ValueError("invalid rule pack choice")
        try:
            datetime.fromisoformat(effective_date)
        except ValueError as exc:
            raise ValueError("effective_date must be ISO date") from exc
        pack = RulePackChoice(
            rule_pack_id,
            tenant_id,
            version,
            sha256,
            status,
            mode,
            effective_date,
            tuple(unresolved_gap_ids),
        )
        key = (tenant_id, rule_pack_id)
        with self._write_operation():
            existing = self._rule_packs.get(key)
            if existing is not None:
                if existing == pack:
                    return self
                raise ValueError("immutable rule pack revision cannot be overwritten")
            self._rule_packs[key] = pack
            self._advance()
        return self

    def with_enabled_mode(self, tenant_id: str, mode: str) -> Registry:
        _uuid(tenant_id, "tenant_id")
        if mode not in _MODES:
            raise ValueError(f"unsupported mode: {mode}")
        with self._write_operation():
            modes = self._enabled_modes.setdefault(tenant_id, set())
            if mode not in modes:
                modes.add(mode)
                self._advance()
        return self

    def create_company(
        self,
        *,
        actor: str,
        tenant_id: str,
        legal_name: str,
        aliases: tuple[str, ...] = (),
        registration_identifier: str | None = None,
        idempotency_key: str | None = None,
        now: datetime | None = None,
    ) -> Company:
        _uuid(tenant_id, "tenant_id")
        if not actor or not 1 <= len(legal_name) <= 200:
            raise ValueError("actor and legal_name (1..200 chars) are required")
        if registration_identifier is not None and len(registration_identifier) > 100:
            raise ValueError("registration_identifier must be at most 100 chars")
        if any(not isinstance(alias, str) or len(alias) > 200 for alias in aliases):
            raise ValueError("aliases must be strings of at most 200 chars")
        timestamp = now if now is not None else datetime.now(UTC)
        if timestamp.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        fingerprint = (legal_name, tuple(aliases), registration_identifier)
        # Keep the stored v1 key shape; authentication is not part of request identity.
        idempotency = (tenant_id, "", idempotency_key) if idempotency_key else None
        with self._write_operation():
            if idempotency is not None:
                matches = [
                    (previous, company)
                    for (tenant, _actor, key), (previous, company) in self._idempotency.items()
                    if tenant == tenant_id
                    and key == idempotency_key
                    and timestamp < datetime.fromisoformat(company.created_at) + timedelta(hours=24)
                ]
                if matches:
                    if (
                        any(previous != fingerprint for previous, _ in matches)
                        or len({company.company_id for _, company in matches}) != 1
                    ):
                        raise IdempotencyConflict("idempotency key conflicts with prior request")
                    return matches[0][1]
            company = Company(
                company_id=str(uuid4()),
                tenant_id=tenant_id,
                legal_name=legal_name,
                registration_identifier=registration_identifier,
                aliases=tuple(aliases),
                created_at=timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z"),
            )
            self._companies[(tenant_id, company.company_id)] = company
            if idempotency is not None:
                self._idempotency[idempotency] = (fingerprint, company)
            self._advance()
            return company

    def get_company(self, *, tenant_id: str, company_id: str) -> Company:
        with self._read_operation():
            company = self._companies.get((tenant_id, company_id))
            if company is None:
                raise RegistryNotFound("company not found")
            return company

    def list_companies(self, *, tenant_id: str, cursor: str | None, limit: int) -> CompanyPage:
        _uuid(tenant_id, "tenant_id")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        operation = self._write_operation if cursor is None else self._read_operation
        with operation():
            if cursor is None:
                epoch = self._epoch
                ordered = sorted(
                    (
                        company
                        for (tenant, _), company in self._companies.items()
                        if tenant == tenant_id
                    ),
                    key=lambda company: (company.legal_name, company.company_id),
                )
                self._snapshots[(tenant_id, epoch)] = tuple(
                    company.company_id for company in ordered
                )
                offset = 0
            else:
                try:
                    epoch_text, offset_text = cursor.split(":", 1)
                    epoch, offset = int(epoch_text), int(offset_text)
                    if epoch < 0 or offset < 0:
                        raise ValueError("negative cursor")
                    company_ids = self._snapshots[(tenant_id, epoch)]
                except (KeyError, ValueError) as exc:
                    raise ValueError("invalid cursor") from exc
                ordered = [self._companies[(tenant_id, company_id)] for company_id in company_ids]
            page = tuple(ordered[offset : offset + limit])
            next_cursor = f"{epoch}:{offset + limit}" if offset + limit < len(ordered) else None
            return CompanyPage(page, next_cursor, epoch)

    def runtime_options(self, *, tenant_id: str) -> RuntimeOptions:
        _uuid(tenant_id, "tenant_id")
        with self._read_operation():
            approved = sorted(
                (
                    option
                    for (tenant, _, _), option in self._options.items()
                    if tenant == tenant_id and option.status == "approved"
                ),
                key=lambda option: (option.name, option.id),
            )
            choices = {
                kind: tuple(option for option in approved if option.kind == kind)
                for kind in _OPTION_KINDS
            }
            packs = tuple(
                sorted(
                    self._current_packs(tenant_id),
                    key=lambda pack: (pack.mode, pack.version, pack.rule_pack_id),
                )
            )
            return RuntimeOptions(
                choices["rights"],
                choices["consent"],
                choices["runtime"],
                packs,
                tuple(sorted(self._enabled_modes.get(tenant_id, set()))),
            )

    def validate_document(self, *, tenant_id: str, company_id: str, rights_profile_id: str) -> None:
        with self._read_operation():
            if (tenant_id, company_id) not in self._companies:
                raise RegistryNotFound("company not found")
            self._approved_option(tenant_id, "rights", rights_profile_id)

    def validate_run(
        self,
        *,
        tenant_id: str,
        consent_profile_id: str,
        runtime_binding_id: str,
        rule_pack_id: str,
        mode: str,
    ) -> None:
        with self._read_operation():
            self._approved_option(tenant_id, "consent", consent_profile_id)
            self._approved_option(tenant_id, "runtime", runtime_binding_id)
            pack = next(
                (
                    pack
                    for pack in self._current_packs(tenant_id)
                    if pack.rule_pack_id == rule_pack_id
                ),
                None,
            )
            if (
                pack is None
                or pack.status != "active"
                or pack.mode != mode
                or mode not in self._enabled_modes.get(tenant_id, set())
            ):
                raise RegistryNotFound("approved runtime option not found")

    def _current_packs(self, tenant_id: str) -> tuple[RulePackChoice, ...]:
        packs = (
            self._active_rule_packs(tenant_id)
            if self._active_rule_packs is not None
            else self._rule_packs.values()
        )
        return tuple(
            pack for pack in packs if pack.tenant_id == tenant_id and pack.status == "active"
        )

    def resolve_profile(self, auth: object, kind: str, option_id: str) -> Mapping[str, object]:
        """Return immutable metadata for a profile usable by a trusted adapter.

        The list endpoint intentionally omits artifact identity. Consumers that
        need it must pass an authenticated tenant context and get an approved,
        fully identified profile rather than accepting a client-supplied URI.
        """
        tenant_id = getattr(auth, "tenant_id", None)
        if not isinstance(tenant_id, str):
            raise RegistryNotFound("approved runtime option not found")
        with self._read_operation():
            option = self._options.get((tenant_id, kind, option_id))
            if (
                option is None
                or option.status != "approved"
                or not option.version
                or not option.sha256
                or option.artifact is None
                or not option.approved_by
                or not _rfc3339(option.approved_at)
            ):
                raise RegistryNotFound("approved runtime option not found")
            if artifact_sha256(option.artifact) != option.sha256:
                raise RegistryNotFound("approved runtime option not found")
            identifier = _PROFILE_ID_FIELDS[option.kind]
            if (
                option.artifact.get("tenant_id") != option.tenant_id
                or option.artifact.get(identifier) != option.id
                or option.artifact.get("status") != "approved"
                or option.artifact.get("version") != option.version
            ):
                raise RegistryNotFound("approved runtime option not found")
            return _deep_freeze(option.artifact)

    def _approved_option(self, tenant_id: str, kind: str, option_id: str) -> None:
        option = self._options.get((tenant_id, kind, option_id))
        if option is None or option.status != "approved":
            raise RegistryNotFound("approved runtime option not found")

    def _advance(self) -> None:
        self._epoch += 1

    @contextmanager
    def _write_operation(self):
        """Reload under a SQLite write lock and commit one complete registry state.

        # ponytail: one local state row and BEGIN IMMEDIATE serialize all local
        # writers; replace with per-record transactions only when contention is measured.
        """
        with self._lock:
            if self._database is None:
                yield
                return
            begun = False
            try:
                self._database.execute("BEGIN IMMEDIATE")
                begun = True
                self._load()
                yield
                self._persist()
                self._database.execute("COMMIT")
            except BaseException:
                if begun:
                    self._database.execute("ROLLBACK")
                self._load()
                raise

    @contextmanager
    def _read_operation(self):
        """Refresh from SQLite so a long-lived registry instance never serves stale state."""
        with self._lock:
            if self._database is not None:
                self._load()
            yield

    def _load(self) -> None:
        assert self._database is not None
        row = self._database.execute(
            "SELECT value FROM registry_state WHERE key = 'state'"
        ).fetchone()
        data: dict[str, Any] = json.loads(row[0]) if row is not None else _empty_state()
        self._epoch = int(data["epoch"])
        self._companies = {
            (item["tenant_id"], item["company_id"]): Company(
                item["company_id"],
                item["tenant_id"],
                item["legal_name"],
                item["registration_identifier"],
                tuple(item["aliases"]),
                item["created_at"],
            )
            for item in data["companies"]
        }
        self._options = {
            (item["tenant_id"], item["kind"], item["id"]): RuntimeOption(
                item["id"],
                item["name"],
                item["status"],
                item["reason"],
                item["tenant_id"],
                item["kind"],
                item["version"],
                item["sha256"],
                item["artifact"],
                item["approved_by"],
                item["approved_at"],
                bool(item["local_synthetic"]),
            )
            for item in data["options"]
        }
        self._rule_packs = {
            (item["tenant_id"], item["rule_pack_id"]): RulePackChoice(
                item["rule_pack_id"],
                item["tenant_id"],
                item["version"],
                item["sha256"],
                item["status"],
                item["mode"],
                item["effective_date"],
                tuple(item["unresolved_gap_ids"]),
            )
            for item in data["rule_packs"]
        }
        self._enabled_modes = {tenant: set(modes) for tenant, modes in data["enabled_modes"]}
        self._snapshots = {
            (tenant, int(epoch)): tuple(company_ids)
            for tenant, epoch, company_ids in data["snapshots"]
        }
        self._idempotency = {
            (item["tenant_id"], item["actor"], item["key"]): (
                (item["legal_name"], tuple(item["aliases"]), item["registration_identifier"]),
                self._companies[(item["tenant_id"], item["company_id"])],
            )
            for item in data["idempotency"]
        }

    def _persist(self) -> None:
        if self._database is None:
            return
        idempotency = [
            {
                "tenant_id": tenant,
                "actor": actor,
                "key": key,
                "legal_name": fingerprint[0],
                "aliases": list(fingerprint[1]),
                "registration_identifier": fingerprint[2],
                "company_id": company.company_id,
            }
            for (tenant, actor, key), (fingerprint, company) in self._idempotency.items()
        ]
        data = {
            "epoch": self._epoch,
            "companies": [
                {
                    "company_id": company.company_id,
                    "tenant_id": company.tenant_id,
                    "legal_name": company.legal_name,
                    "registration_identifier": company.registration_identifier,
                    "aliases": list(company.aliases),
                    "created_at": company.created_at,
                }
                for company in self._companies.values()
            ],
            "options": [
                {
                    "id": option.id,
                    "name": option.name,
                    "status": option.status,
                    "reason": option.reason,
                    "tenant_id": option.tenant_id,
                    "kind": option.kind,
                    "version": option.version,
                    "sha256": option.sha256,
                    "artifact": option.artifact,
                    "approved_by": option.approved_by,
                    "approved_at": option.approved_at,
                    "local_synthetic": option.local_synthetic,
                }
                for option in self._options.values()
            ],
            "rule_packs": [
                {
                    "rule_pack_id": pack.rule_pack_id,
                    "tenant_id": pack.tenant_id,
                    "version": pack.version,
                    "sha256": pack.sha256,
                    "status": pack.status,
                    "mode": pack.mode,
                    "effective_date": pack.effective_date,
                    "unresolved_gap_ids": list(pack.unresolved_gap_ids),
                }
                for pack in self._rule_packs.values()
            ],
            "enabled_modes": [
                (tenant, sorted(modes)) for tenant, modes in self._enabled_modes.items()
            ],
            "snapshots": [
                (tenant, epoch, list(ids)) for (tenant, epoch), ids in self._snapshots.items()
            ],
            "idempotency": idempotency,
        }
        self._database.execute(
            "INSERT OR REPLACE INTO registry_state(key, value) VALUES ('state', ?)",
            (json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False),),
        )


def create_company(registry: Registry, **kwargs: object) -> tuple[Registry, Company]:
    return registry, registry.create_company(**kwargs)  # type: ignore[arg-type]


def list_companies(
    registry: Registry, *, actor: str, tenant_id: str, cursor: str | None = None, limit: int = 50
) -> CompanyPage:
    if not actor:
        raise ValueError("actor is required")
    return registry.list_companies(tenant_id=tenant_id, cursor=cursor, limit=limit)


def list_runtime_options(registry: Registry, *, actor: str, tenant_id: str) -> RuntimeOptions:
    if not actor:
        raise ValueError("actor is required")
    return registry.runtime_options(tenant_id=tenant_id)


def validate_document(registry: Registry, **kwargs: str) -> None:
    registry.validate_document(**kwargs)


def validate_run(registry: Registry, **kwargs: str) -> None:
    registry.validate_run(**kwargs)


def resolve_profile(
    registry: Registry, auth: object, kind: str, option_id: str
) -> Mapping[str, object]:
    return registry.resolve_profile(auth, kind, option_id)


def _uuid(value: str, name: str) -> None:
    try:
        UUID(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a UUID") from exc


def _sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def artifact_sha256(artifact: Mapping[str, object]) -> str:
    """Hash JSON content exactly as stored by the local registry."""
    return hashlib.sha256(_canonical_json(artifact)).hexdigest()


def _detached_artifact(artifact: Mapping[str, object]) -> dict[str, object]:
    loaded = json.loads(_canonical_json(artifact))
    if not isinstance(loaded, dict):  # defensive: Mapping input should always encode as object
        raise ValueError("artifact must be a JSON object")
    return loaded


def _json_mapping(value: object) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("unsupported JSON value")


def _canonical_json(value: Mapping[str, object]) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=_json_mapping,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("artifact must be finite JSON data") from exc


def _deep_freeze(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise RegistryNotFound("approved runtime option not found")
    return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def _rfc3339(value: str | None) -> bool:
    if not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _empty_state() -> dict[str, Any]:
    return {
        "epoch": 0,
        "companies": [],
        "options": [],
        "rule_packs": [],
        "enabled_modes": [],
        "snapshots": [],
        "idempotency": [],
    }
