"""TASK-045 acceptance tests: tenant-scoped company and approved-option registry."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
RIGHTS_A = "33333333-3333-4333-8333-333333333333"
RIGHTS_B = "44444444-4444-4444-8444-444444444444"
CONSENT_A = "55555555-5555-4555-8555-555555555555"
RUNTIME_A = "66666666-6666-4666-8666-666666666666"
RULEPACK_A = "77777777-7777-4777-8777-777777777777"

pytestmark = pytest.mark.usefixtures("_no_env_leak")


def test_company_idempotency_is_tenant_scoped_and_expires_after_24_hours(tmp_path):
    from proofops.application.registry import IdempotencyConflict, Registry

    path = tmp_path / "registry.sqlite3"
    now = datetime(2026, 9, 9, tzinfo=UTC)
    args = dict(tenant_id=TENANT_A, legal_name="Synthetic", idempotency_key="tenant-scoped-create")
    first = Registry.sqlite(path)
    company = first.create_company(actor="editor-a", now=now, **args)
    first.close()
    second = Registry.sqlite(path)
    assert second.create_company(actor="editor-b", now=now + timedelta(hours=23), **args) == company
    with pytest.raises(IdempotencyConflict):
        second.create_company(
            actor="editor-b", now=now + timedelta(hours=23), **(args | {"legal_name": "changed"})
        )
    later = second.create_company(actor="editor-b", now=now + timedelta(hours=24), **args)
    assert later.company_id != company.company_id
    second.close()


def test_run_validation_uses_live_active_pointer_and_rejects_unknown_registry_schema(tmp_path):
    from proofops.application.registry import Registry, RegistryNotFound, RulePackChoice

    current = [
        RulePackChoice(
            RULEPACK_A, TENANT_A, "v1", "a" * 64, "active", "disclosure", "2026-09-09", ()
        )
    ]
    registry = Registry(active_rule_packs=lambda tenant: tuple(current))
    _approved(registry, TENANT_A, "consent", CONSENT_A, "Synthetic consent")
    _approved(registry, TENANT_A, "runtime", RUNTIME_A, "Synthetic runtime")
    registry.with_enabled_mode(TENANT_A, "disclosure")
    args = dict(
        tenant_id=TENANT_A,
        consent_profile_id=CONSENT_A,
        runtime_binding_id=RUNTIME_A,
        rule_pack_id=RULEPACK_A,
        mode="disclosure",
    )
    registry.validate_run(**args)
    current[0] = replace(current[0], rule_pack_id=RIGHTS_B)
    with pytest.raises(RegistryNotFound):
        registry.validate_run(**args)

    path = tmp_path / "unknown.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE registry_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO registry_metadata VALUES ('schema_version', '2')")
    with pytest.raises(ValueError, match="unsupported registry schema"):
        Registry.sqlite(path)


@pytest.fixture()
def _no_env_leak(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")


def _registry():
    from proofops.application.registry import Registry

    registry = Registry.empty()
    for kind, option_id, name in (
        ("rights", RIGHTS_A, "Verified public-report rights"),
        ("consent", CONSENT_A, "Approved processing consent"),
        ("runtime", RUNTIME_A, "Approved local synthetic runtime"),
    ):
        _approved(registry, TENANT_A, kind, option_id, name)
    _approved(registry, TENANT_B, "rights", RIGHTS_B, "Other tenant rights")
    return (
        registry.with_rule_pack(
            TENANT_A,
            RULEPACK_A,
            version="v1",
            sha256="a" * 64,
            status="active",
            mode="disclosure",
            effective_date="2026-09-09",
        )
        .with_option(
            TENANT_A,
            "runtime",
            "88888888-8888-4888-8888-888888888888",
            "Disabled",
            status="disabled",
        )
        .with_enabled_mode(TENANT_A, "disclosure")
    )


def _approved(registry, tenant_id: str, kind: str, option_id: str, name: str) -> None:
    from proofops.application.registry import artifact_sha256

    id_field = {
        "rights": "rights_profile_id",
        "consent": "consent_profile_id",
        "runtime": "runtime_binding_id",
    }[kind]
    artifact = {"tenant_id": tenant_id, id_field: option_id, "status": "approved", "version": "v1"}
    registry.with_option(
        tenant_id,
        kind,
        option_id,
        name,
        status="approved",
        version="v1",
        artifact=artifact,
        sha256=artifact_sha256(artifact),
        approved_by="local-synthetic-fixture",
        approved_at="2026-09-09T00:00:00Z",
        local_synthetic=True,
    )


def test_company_names_do_not_merge_and_document_run_options_are_tenant_scoped() -> None:
    from proofops.application.registry import (
        RegistryNotFound,
        create_company,
        validate_document,
        validate_run,
    )

    registry = _registry()
    registry, first = create_company(
        registry, actor="editor-a", tenant_id=TENANT_A, legal_name="동일 이름"
    )
    registry, second = create_company(
        registry, actor="editor-a", tenant_id=TENANT_A, legal_name="동일 이름"
    )

    assert first.company_id != second.company_id
    assert registry.get_company(tenant_id=TENANT_A, company_id=first.company_id) == first
    with pytest.raises(RegistryNotFound):
        registry.get_company(tenant_id=TENANT_B, company_id=first.company_id)
    validate_document(
        registry,
        tenant_id=TENANT_A,
        company_id=first.company_id,
        rights_profile_id=RIGHTS_A,
    )
    validate_run(
        registry,
        tenant_id=TENANT_A,
        consent_profile_id=CONSENT_A,
        runtime_binding_id=RUNTIME_A,
        rule_pack_id=RULEPACK_A,
        mode="disclosure",
    )

    with pytest.raises(RegistryNotFound):
        validate_document(
            registry,
            tenant_id=TENANT_A,
            company_id="00000000-0000-4000-8000-000000000000",
            rights_profile_id=RIGHTS_A,
        )
    with pytest.raises(RegistryNotFound):
        validate_document(
            registry,
            tenant_id=TENANT_A,
            company_id=first.company_id,
            rights_profile_id=RIGHTS_B,
        )


def test_runtime_options_only_return_current_tenant_approved_choices_and_never_secrets() -> None:
    from proofops.application.registry import list_runtime_options

    options = list_runtime_options(_registry(), actor="viewer-a", tenant_id=TENANT_A)

    assert [option.id for option in options.rights_profiles] == [RIGHTS_A]
    assert [option.id for option in options.runtime_bindings] == [RUNTIME_A]
    assert options.enabled_modes == ("disclosure",)
    assert "secret" not in repr(options).lower()


def test_registry_rejects_attempt_to_overwrite_existing_option_revision() -> None:
    from proofops.application.registry import Registry

    registry = Registry.empty().with_option(TENANT_A, "rights", RIGHTS_A, "original")
    with pytest.raises(ValueError, match="immutable"):
        registry.with_option(TENANT_A, "rights", RIGHTS_A, "changed")


def test_default_profile_is_not_approved_without_verified_artifact() -> None:
    from proofops.application.registry import Registry

    registry = Registry.empty().with_option(TENANT_A, "rights", RIGHTS_A, "unverified by default")
    assert registry.runtime_options(tenant_id=TENANT_A).rights_profiles == ()


def test_sqlite_registry_reopens_company_and_idempotency_record(tmp_path) -> None:
    from proofops.application.registry import Registry, create_company, list_companies

    path = tmp_path / "registry.sqlite3"
    registry = Registry.sqlite(path)
    registry, created = create_company(
        registry,
        actor="editor-a",
        tenant_id=TENANT_A,
        legal_name="durable company",
        idempotency_key="durable-idempotency-key",
    )
    reopened = Registry.sqlite(path)
    _, replay = create_company(
        reopened,
        actor="editor-a",
        tenant_id=TENANT_A,
        legal_name="durable company",
        idempotency_key="durable-idempotency-key",
    )

    assert replay.company_id == created.company_id
    assert [
        company.company_id
        for company in list_companies(reopened, actor="viewer-a", tenant_id=TENANT_A).items
    ] == [created.company_id]


def test_sqlite_instances_preserve_interleaved_tenants_profiles_and_idempotency(tmp_path) -> None:
    from proofops.application.registry import (
        IdempotencyConflict,
        Registry,
        create_company,
        list_companies,
    )

    path = tmp_path / "shared-registry.sqlite3"
    first = Registry.sqlite(path)
    second = Registry.sqlite(path)
    first, company_a = create_company(
        first,
        actor="editor-a",
        tenant_id=TENANT_A,
        legal_name="company from first instance",
        idempotency_key="interleaved-idempotency-key",
    )
    _approved(first, TENANT_A, "rights", RIGHTS_A, "rights from first instance")
    second, company_b = create_company(
        second,
        actor="editor-b",
        tenant_id=TENANT_B,
        legal_name="company from second instance",
    )
    _approved(second, TENANT_B, "rights", RIGHTS_B, "rights from second instance")
    _, replay = create_company(
        second,
        actor="editor-a",
        tenant_id=TENANT_A,
        legal_name="company from first instance",
        idempotency_key="interleaved-idempotency-key",
    )
    with pytest.raises(IdempotencyConflict):
        create_company(
            second,
            actor="editor-a",
            tenant_id=TENANT_A,
            legal_name="different body",
            idempotency_key="interleaved-idempotency-key",
        )

    reopened = Registry.sqlite(path)
    assert replay.company_id == company_a.company_id
    assert [
        company.company_id
        for company in list_companies(first, actor="viewer-a", tenant_id=TENANT_B).items
    ] == [company_b.company_id]
    assert [
        company.company_id
        for company in list_companies(reopened, actor="viewer-a", tenant_id=TENANT_A).items
    ] == [company_a.company_id]
    assert [
        option.id for option in reopened.runtime_options(tenant_id=TENANT_A).rights_profiles
    ] == [RIGHTS_A]
    assert [
        option.id for option in reopened.runtime_options(tenant_id=TENANT_B).rights_profiles
    ] == [RIGHTS_B]
    first.close()
    second.close()
    reopened.close()


def test_failed_sqlite_persistence_rolls_back_database_and_memory(tmp_path, monkeypatch) -> None:
    from proofops.application.registry import Registry, create_company, list_companies

    path = tmp_path / "rollback-registry.sqlite3"
    registry = Registry.sqlite(path)
    original_persist = registry._persist

    def fail_persist() -> None:
        raise sqlite3.OperationalError("synthetic write failure")

    import sqlite3

    monkeypatch.setattr(registry, "_persist", fail_persist)
    with pytest.raises(sqlite3.OperationalError, match="synthetic write failure"):
        create_company(
            registry,
            actor="editor-a",
            tenant_id=TENANT_A,
            legal_name="must not survive",
        )

    monkeypatch.setattr(registry, "_persist", original_persist)
    assert list_companies(registry, actor="viewer-a", tenant_id=TENANT_A).items == ()
    reopened = Registry.sqlite(path)
    assert list_companies(reopened, actor="viewer-a", tenant_id=TENANT_A).items == ()
    registry.close()
    reopened.close()


def test_company_create_requires_aliases_per_openapi_contract() -> None:
    from proofops_api.dto import CompanyCreate, DtoValidationError

    with pytest.raises(DtoValidationError):
        CompanyCreate.model_validate({"legal_name": "aliases are required"})


def test_profile_resolver_returns_only_a_tenant_approved_immutable_artifact() -> None:
    from proofops.application.registry import Registry, RegistryNotFound, resolve_profile

    class Auth:
        tenant_id = TENANT_A

    registry = Registry.empty()
    _approved(registry, TENANT_A, "rights", RIGHTS_A, "Verified public-report rights")
    profile = resolve_profile(registry, Auth(), "rights", RIGHTS_A)

    assert profile["rights_profile_id"] == RIGHTS_A
    with pytest.raises(TypeError):
        profile["sha256"] = "c" * 64  # type: ignore[index]
    with pytest.raises(RegistryNotFound):
        resolve_profile(registry, type("AuthB", (), {"tenant_id": TENANT_B})(), "rights", RIGHTS_A)


def test_companies_paginate_from_a_stable_snapshot() -> None:
    from proofops.application.registry import create_company, list_companies

    registry = _registry()
    for name in ("C", "A", "B"):
        registry, _ = create_company(
            registry, actor="editor-a", tenant_id=TENANT_A, legal_name=name
        )

    first = list_companies(registry, actor="viewer-a", tenant_id=TENANT_A, limit=2)
    second = list_companies(
        registry,
        actor="viewer-a",
        tenant_id=TENANT_A,
        cursor=first.next_cursor,
        limit=2,
    )

    assert [company.legal_name for company in first.items] == ["A", "B"]
    assert [company.legal_name for company in second.items] == ["C"]
    assert first.snapshot_epoch == second.snapshot_epoch
    with pytest.raises(ValueError, match="invalid cursor"):
        list_companies(registry, actor="viewer-a", tenant_id=TENANT_A, cursor="-1:-1")


def _client(registry):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
    from proofops.application.authorization import MembershipRecord, SessionRecord
    from proofops_api.auth import AuthStore
    from proofops_api.routers.registry import build_registry_router

    sessions = InMemorySessionStore()
    memberships = InMemoryMembershipStore()
    store = AuthStore(sessions=sessions, memberships=memberships)
    far_future = time.time() + 10_000
    sessions.put_with_token(
        SessionRecord(
            "editor-a",
            "user-a",
            TENANT_A,
            store.hash_csrf("csrf-a"),
            far_future,
            far_future,
            False,
        ),
        "csrf-a",
    )
    sessions.put_with_token(
        SessionRecord(
            "viewer-a",
            "viewer-a",
            TENANT_A,
            store.hash_csrf("csrf-v"),
            far_future,
            far_future,
            False,
        ),
        "csrf-v",
    )
    memberships.put(MembershipRecord(TENANT_A, "user-a", "editor", "active"))
    memberships.put(MembershipRecord(TENANT_A, "viewer-a", "viewer", "active"))
    app = FastAPI()
    app.include_router(build_registry_router(registry, store, allowed_origin="http://testserver"))
    return TestClient(app), sessions.csrf_token_for("editor-a"), sessions.csrf_token_for("viewer-a")


def test_http_company_create_is_csrf_and_idempotency_protected_without_name_merging() -> None:
    client, editor_csrf, _viewer_csrf = _client(_registry())
    client.cookies.set("__Host-proofops_session", "editor-a")
    headers = {
        "X-CSRF-Token": editor_csrf,
        "Origin": "http://testserver",
        "Idempotency-Key": "registry-create-key",
    }
    body = {"legal_name": "합성 기업", "aliases": ["합성"]}

    first = client.post("/v1/companies", json=body, headers=headers)
    replay = client.post("/v1/companies", json=body, headers=headers)
    changed = client.post("/v1/companies", json={**body, "aliases": []}, headers=headers)

    assert first.status_code == 201
    assert replay.status_code == 201
    assert first.json()["company_id"] == replay.json()["company_id"]
    assert changed.status_code == 409
    assert UUID(first.json()["company_id"])


def test_http_registry_authorizes_roles_and_returns_only_approved_options() -> None:
    client, _editor_csrf, viewer_csrf = _client(_registry())
    assert client.get("/v1/companies").status_code == 401

    client.cookies.set("__Host-proofops_session", "viewer-a")
    options = client.get("/v1/runtime-options")
    denied = client.post(
        "/v1/companies",
        json={"legal_name": "viewer cannot create", "aliases": []},
        headers={
            "X-CSRF-Token": viewer_csrf,
            "Origin": "http://testserver",
            "Idempotency-Key": "viewer-create-key",
        },
    )

    assert options.status_code == 200
    assert [item["id"] for item in options.json()["rights_profiles"]] == [RIGHTS_A]
    assert [item["id"] for item in options.json()["runtime_bindings"]] == [RUNTIME_A]
    assert denied.status_code == 403


def test_artifact_hash_accepts_nested_immutable_registry_metadata():
    from types import MappingProxyType

    from proofops.application.registry import artifact_sha256

    raw = {"name": "환경", "nested": [{"active": True}], "number": 2}
    frozen = MappingProxyType(
        {"name": "환경", "nested": (MappingProxyType({"active": True}),), "number": 2}
    )
    assert artifact_sha256(frozen) == artifact_sha256(raw)
