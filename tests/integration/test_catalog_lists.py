"""Fixed-contract local catalog lists over real HTTP and SQLite state."""

from __future__ import annotations

import base64
import hmac
import json
import sqlite3
from hashlib import sha256
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.adapters.local.rulepack_store import RulePackSqliteStore
from proofops.application.authorization import MembershipRecord, SessionRecord
from proofops.application.registry import Registry, artifact_sha256
from proofops.application.uploads import UploadService
from proofops_api.auth import AuthStore
from proofops_api.routers.documents import build_documents_router
from proofops_api.rulepacks import build_rulepack_router

from tests.acceptance.test_rulepack_api import GAP_IDS, _files, _pack
from tests.acceptance.test_upload import RIGHTS, TENANT, pdf
from tests.integration.test_run_lifecycle import setup as setup_runs

FOREIGN = "22222222-2222-4222-8222-222222222222"
ORIGIN = "https://testserver"
NOW = 1_789_000_000


def _client(router, *, tenant: str = TENANT, role: str = "admin") -> TestClient:
    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    auth.sessions.put_with_token(
        SessionRecord(
            "catalog-session",
            "catalog-user",
            tenant,
            auth.hash_csrf("csrf-token"),
            10**12,
            10**12,
            False,
        ),
        "csrf-token",
    )
    auth.memberships.put(MembershipRecord(tenant, "catalog-user", role, "active"))
    csrf_token = auth.sessions.csrf_token_for("catalog-session")
    assert csrf_token is not None
    app = FastAPI()
    app.include_router(router(auth))
    http = TestClient(app, base_url=ORIGIN)
    http.cookies.set("__Host-proofops_session", "catalog-session")
    http.headers.update(
        {
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf_token,
            "Idempotency-Key": str(uuid4()),
        }
    )
    return http


def _uploads(tmp_path, clock) -> UploadService:
    registry = Registry.empty()
    company = registry.create_company(actor="fixture", tenant_id=TENANT, legal_name="Catalog Co")
    foreign = registry.create_company(
        actor="fixture", tenant_id=FOREIGN, legal_name="Foreign Catalog Co"
    )
    for tenant in (TENANT, FOREIGN):
        artifact = {
            "tenant_id": tenant,
            "rights_profile_id": RIGHTS,
            "status": "approved",
            "version": "v1",
        }
        registry.with_option(
            tenant,
            "rights",
            RIGHTS,
            "Synthetic fixture only",
            status="approved",
            version="v1",
            artifact=artifact,
            sha256=artifact_sha256(artifact),
            approved_by="synthetic-fixture",
            approved_at="2026-09-09T00:00:00Z",
            local_synthetic=True,
        )
    service = UploadService(
        tmp_path / "catalog.sqlite", tmp_path / "objects", registry, clock=clock
    )
    service._catalog_companies = (company, foreign)  # test fixture ownership only
    return service


def _document(service: UploadService, *, tenant: str = TENANT, foreign: bool = False) -> dict:
    company = service._catalog_companies[1 if foreign else 0]
    return service.create_document(
        tenant,
        {
            "company_id": company.company_id,
            "title": f"Report {uuid4()}",
            "document_type": "sustainability_report",
        },
        str(uuid4()),
    )


def _version(service: UploadService, document_id: str, *, year: int = 2025) -> dict:
    content = pdf()
    body = {
        "filename": "source.pdf",
        "size_bytes": len(content),
        "sha256": sha256(content).hexdigest(),
        "report_year": year,
        "industry_system": "unknown",
        "period_start": f"{year}-01-01",
        "period_end": f"{year}-12-31",
        "rights_profile_id": RIGHTS,
    }
    ticket = service.initiate_upload(TENANT, document_id, body, str(uuid4()))
    service.receive_content(TENANT, ticket["upload_id"], content, "application/pdf")
    return service.complete_upload(
        TENANT,
        ticket["upload_id"],
        {"sha256": body["sha256"], "size_bytes": len(content)},
        str(uuid4()),
    )


def test_documents_and_versions_are_tenant_scoped_signed_stable_pages(tmp_path):
    now = [float(NOW)]
    service = _uploads(tmp_path, lambda: now[0])
    documents = [_document(service) for _ in range(3)]
    foreign = _document(service, tenant=FOREIGN, foreign=True)
    versions = [_version(service, documents[1]["document_id"], year=year) for year in (2024, 2025)]
    baseline = {
        item["document_id"]: service.get_document(TENANT, item["document_id"]) for item in documents
    }

    def router(auth):
        return build_documents_router(
            service,
            auth,
            allowed_origin=ORIGIN,
            app_env="local",
            model_adapter="synthetic",
            clock=lambda: now[0],
        )

    with _client(router) as http:
        first = http.get("/v1/documents", params={"limit": 1})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert set(first.json()) == {"items", "next_cursor", "snapshot_epoch"}
        assert set(first.json()["items"][0]) == {
            "document_id",
            "company_id",
            "title",
            "document_type",
            "latest_version_id",
            "revision",
            "created_at",
        }
        cursor = first.json()["next_cursor"]
        assert cursor

        _document(service)
        mutate_id = next(
            document_id
            for document_id in baseline
            if document_id
            not in {
                first.json()["items"][0]["document_id"],
                documents[1]["document_id"],
            }
        )
        _version(service, mutate_id, year=2026)

        replica = UploadService(
            tmp_path / "catalog.sqlite",
            tmp_path / "objects",
            service.registry,
            clock=lambda: now[0],
        )

        def replica_router(auth):
            return build_documents_router(
                replica,
                auth,
                allowed_origin=ORIGIN,
                app_env="local",
                model_adapter="synthetic",
                clock=lambda: now[0],
            )

        with _client(replica_router) as other_replica:
            assert (
                other_replica.get(
                    "/v1/documents", params={"limit": 1, "cursor": cursor}
                ).status_code
                == 200
            )
        replica.close()

        seen = list(first.json()["items"])
        while cursor:
            page = http.get("/v1/documents", params={"limit": 1, "cursor": cursor})
            assert page.status_code == 200
            assert page.json()["snapshot_epoch"] == first.json()["snapshot_epoch"]
            seen.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
        assert [item["document_id"] for item in seen] == [
            item["document_id"]
            for item in sorted(
                documents, key=lambda item: (item["created_at"], item["document_id"])
            )
        ]
        assert {item["document_id"] for item in seen}.isdisjoint({foreign["document_id"]})
        assert (
            next(item for item in seen if item["document_id"] == mutate_id) == baseline[mutate_id]
        )

        version_first = http.get(
            f"/v1/documents/{documents[1]['document_id']}/versions", params={"limit": 1}
        )
        assert version_first.status_code == 200
        assert set(version_first.json()["items"][0]) == {
            "version_id",
            "document_id",
            "sha256",
            "report_year",
            "page_count",
            "status",
            "created_at",
        }
        version_cursor = version_first.json()["next_cursor"]
        assert version_cursor
        assert (
            http.get("/v1/documents", params={"limit": 1, "cursor": version_cursor}).status_code
            == 400
        )
        _version(service, documents[1]["document_id"], year=2027)
        remaining = http.get(
            f"/v1/documents/{documents[1]['document_id']}/versions",
            params={"limit": 1, "cursor": version_cursor},
        )
        assert remaining.status_code == 200
        assert {
            item["version_id"] for item in version_first.json()["items"] + remaining.json()["items"]
        } == {version["version_id"] for version in versions}
        assert http.get(f"/v1/documents/{foreign['document_id']}/versions").status_code == 404

        tampered = first.json()["next_cursor"] + "!"
        assert http.get("/v1/documents", params={"limit": 1, "cursor": tampered}).status_code == 400
        assert (
            http.get(
                "/v1/documents", params={"limit": 2, "cursor": first.json()["next_cursor"]}
            ).status_code
            == 400
        )
        with _client(router, tenant=FOREIGN) as other:
            assert (
                other.get(
                    "/v1/documents",
                    params={"limit": 1, "cursor": first.json()["next_cursor"]},
                ).status_code
                == 400
            )
        now[0] += 901
        assert (
            http.get(
                "/v1/documents", params={"limit": 1, "cursor": first.json()["next_cursor"]}
            ).status_code
            == 400
        )

    with sqlite3.connect(service._db.execute("PRAGMA database_list").fetchone()[2]) as db:
        assert db.execute("SELECT count(*) FROM catalog_list_snapshots").fetchone()[0] >= 2
    service.close()


def test_runs_are_signed_stable_pages_across_insert_and_head_mutation(tmp_path):
    service, body = setup_runs(tmp_path)
    now = [float(NOW)]
    service.clock = lambda: now[0]

    def router(auth):
        from proofops_api.routers.runs import build_runs_router

        return build_runs_router(service, auth, allowed_origin=ORIGIN, clock=lambda: now[0])

    with _client(router) as http:
        created = [
            http.post(
                "/v1/runs",
                json=body,
                headers={
                    "Origin": ORIGIN,
                    "X-CSRF-Token": http.headers["X-CSRF-Token"],
                    "Idempotency-Key": str(uuid4()),
                },
            ).json()
            for _ in range(2)
        ]
        first = http.get("/v1/runs", params={"limit": 1})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert set(first.json()) == {"items", "next_cursor", "snapshot_epoch"}
        assert set(first.json()["items"][0]) == {
            "run_id",
            "document_version_id",
            "status",
            "current_stage",
            "revision",
            "mutation_epoch",
            "coverage",
            "rule_pack_sha256",
            "created_at",
        }
        cursor = first.json()["next_cursor"]
        assert cursor
        target = next(run for run in created if run["run_id"] != first.json()["items"][0]["run_id"])
        cancelled = http.post(
            f"/v1/runs/{target['run_id']}/cancel",
            json={"reason": "freeze catalog snapshot"},
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": http.headers["X-CSRF-Token"],
                "If-Match": f'"{target["revision"]}"',
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert cancelled.status_code == 202
        inserted = http.post(
            "/v1/runs",
            json=body,
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": http.headers["X-CSRF-Token"],
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert inserted.status_code == 202

        second = http.get("/v1/runs", params={"limit": 1, "cursor": cursor})
        assert second.status_code == 200
        seen = first.json()["items"] + second.json()["items"]
        assert [item["run_id"] for item in seen] == [
            item["run_id"]
            for item in sorted(created, key=lambda item: (item["created_at"], item["run_id"]))
        ]
        assert (
            next(item for item in seen if item["run_id"] == target["run_id"])["status"] == "queued"
        )
        assert inserted.json()["run_id"] not in {item["run_id"] for item in seen}
        assert second.json()["snapshot_epoch"] == first.json()["snapshot_epoch"]
        assert second.json()["next_cursor"] is None
        assert http.get("/v1/runs", params={"limit": 2, "cursor": cursor}).status_code == 400
        assert http.get("/v1/runs", params={"limit": 1, "cursor": cursor + "!"}).status_code == 400
        now[0] += 901
        assert http.get("/v1/runs", params={"limit": 1, "cursor": cursor}).status_code == 400


def test_rulepacks_are_tenant_scoped_signed_stable_pages(tmp_path):
    now = [float(NOW)]
    store = RulePackSqliteStore(tmp_path / "rulepacks.sqlite")
    pack_ids = [f"{number:08d}-aaaa-4aaa-8aaa-aaaaaaaaaaaa" for number in (3, 1, 2)]
    packs = [_pack(pack_id=pack_id) for pack_id in pack_ids]
    for pack in packs:
        store.add_pack(pack, _files())
    store.add_pack(
        _pack(tenant_id=FOREIGN, pack_id="99999999-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), _files()
    )

    def router(auth):
        return build_rulepack_router(
            store, auth, gap_ids=GAP_IDS, allowed_origin=ORIGIN, clock=lambda: now[0]
        )

    with _client(router) as http:
        first = http.get("/v1/rule-packs", params={"limit": 1})
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert set(first.json()) == {"items", "next_cursor", "snapshot_epoch"}
        assert set(first.json()["items"][0]) == {
            "rule_pack_id",
            "version",
            "sha256",
            "status",
            "mode",
            "effective_date",
            "unresolved_gap_ids",
        }
        cursor = first.json()["next_cursor"]
        assert cursor
        target = next(
            pack for pack in packs if pack.rule_pack_id != first.json()["items"][0]["rule_pack_id"]
        )
        activated = http.post(
            f"/v1/rule-packs/{target.rule_pack_id}/activate",
            json={"reason": "freeze catalog snapshot"},
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": http.headers["X-CSRF-Token"],
                "If-Match": '"1"',
                "Idempotency-Key": str(uuid4()),
            },
        )
        assert activated.status_code == 200
        inserted = _pack(pack_id="00000000-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        store.add_pack(inserted, _files())

        seen = list(first.json()["items"])
        while cursor:
            page = http.get("/v1/rule-packs", params={"limit": 1, "cursor": cursor})
            assert page.status_code == 200
            assert page.json()["snapshot_epoch"] == first.json()["snapshot_epoch"]
            seen.extend(page.json()["items"])
            cursor = page.json()["next_cursor"]
        assert [item["rule_pack_id"] for item in seen] == pack_ids
        assert (
            next(item for item in seen if item["rule_pack_id"] == target.rule_pack_id)["status"]
            == "validated"
        )
        assert inserted.rule_pack_id not in {item["rule_pack_id"] for item in seen}
        assert all("tenant_id" not in item and "approved_by" not in item for item in seen)
        assert (
            http.get(
                "/v1/rule-packs", params={"limit": 2, "cursor": first.json()["next_cursor"]}
            ).status_code
            == 400
        )
        assert (
            http.get(
                "/v1/rule-packs", params={"limit": 1, "cursor": first.json()["next_cursor"] + "!"}
            ).status_code
            == 400
        )
        now[0] += 901
        assert (
            http.get(
                "/v1/rule-packs", params={"limit": 1, "cursor": first.json()["next_cursor"]}
            ).status_code
            == 400
        )


def test_catalog_schema_refuses_unknown_version(tmp_path):
    from proofops.adapters.local.catalog_pages import CatalogSchemaUnsupported, initialize

    path = tmp_path / "catalog-schema.sqlite"
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        db.commit()
        db.execute("UPDATE catalog_schema SET version=2")
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(CatalogSchemaUnsupported, match="unsupported catalog schema"):
            initialize(db)
        db.rollback()


def test_catalog_page_fails_closed_at_hard_capacity(tmp_path, monkeypatch):
    from proofops.adapters.local import catalog_pages

    path = tmp_path / "catalog-capacity.sqlite"
    monkeypatch.setattr(catalog_pages, "MAX_ROWS_PER_SNAPSHOT", 2)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        catalog_pages.initialize(db)
        with pytest.raises(catalog_pages.CatalogCapacityExceeded):
            catalog_pages.page(
                db,
                tenant_id=TENANT,
                endpoint="claims",
                query={"run_id": str(uuid4())},
                cursor=None,
                limit=1,
                now=NOW,
                load_items=lambda: ({"claim_id": str(uuid4())} for _ in range(3)),
            )
        assert db.execute("SELECT count(*) FROM catalog_list_snapshots").fetchone()[0] == 0
        db.commit()

    monkeypatch.setattr(catalog_pages, "MAX_ROWS_PER_SNAPSHOT", 10)
    monkeypatch.setattr(catalog_pages, "MAX_SNAPSHOT_BYTES", 64)

    def oversized_stream():
        yield {"text": "x" * 128}
        pytest.fail("byte capacity must stop the loader immediately")

    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        with pytest.raises(catalog_pages.CatalogCapacityExceeded):
            catalog_pages.page(
                db,
                tenant_id=TENANT,
                endpoint="claims",
                query={"run_id": str(uuid4())},
                cursor=None,
                limit=1,
                now=NOW,
                load_items=oversized_stream,
            )
        assert db.execute("SELECT count(*) FROM catalog_list_snapshots").fetchone()[0] == 0
        db.rollback()

    monkeypatch.setattr(catalog_pages, "MAX_SNAPSHOT_BYTES", 1_000_000)
    monkeypatch.setattr(catalog_pages, "MAX_TENANT_SNAPSHOT_COUNT", 1)
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        first = catalog_pages.page(
            db,
            tenant_id=TENANT,
            endpoint="claims",
            query={"run_id": "first"},
            cursor=None,
            limit=1,
            now=NOW,
            load_items=lambda: [{"claim_id": "one"}],
        )
        assert first["items"] == [{"claim_id": "one"}]
        with pytest.raises(catalog_pages.CatalogCapacityExceeded):
            catalog_pages.page(
                db,
                tenant_id=TENANT,
                endpoint="reviews",
                query={"run_id": "second"},
                cursor=None,
                limit=1,
                now=NOW,
                load_items=lambda: [{"review_id": "two"}],
            )
        assert db.execute("SELECT count(*) FROM catalog_list_snapshots").fetchone()[0] == 1

        monkeypatch.setattr(catalog_pages, "MAX_TENANT_SNAPSHOT_COUNT", 100)
        monkeypatch.setattr(catalog_pages, "MAX_GLOBAL_SNAPSHOT_COUNT", 1)
        with pytest.raises(catalog_pages.CatalogCapacityExceeded):
            catalog_pages.page(
                db,
                tenant_id=FOREIGN,
                endpoint="claims",
                query={"run_id": "global-count"},
                cursor=None,
                limit=1,
                now=NOW,
                load_items=lambda: [{"claim_id": "two"}],
            )

        stored_bytes = db.execute(
            "SELECT length(CAST(payload AS BLOB)) FROM catalog_list_snapshots"
        ).fetchone()[0]
        monkeypatch.setattr(catalog_pages, "MAX_GLOBAL_SNAPSHOT_COUNT", 100)
        monkeypatch.setattr(catalog_pages, "MAX_TENANT_SNAPSHOT_BYTES", stored_bytes)
        with pytest.raises(catalog_pages.CatalogCapacityExceeded):
            catalog_pages.page(
                db,
                tenant_id=TENANT,
                endpoint="reviews",
                query={"run_id": "tenant-bytes"},
                cursor=None,
                limit=1,
                now=NOW,
                load_items=lambda: [{"review_id": "two"}],
            )

        monkeypatch.setattr(catalog_pages, "MAX_TENANT_SNAPSHOT_BYTES", 1_000_000)
        monkeypatch.setattr(catalog_pages, "MAX_GLOBAL_SNAPSHOT_BYTES", stored_bytes)
        with pytest.raises(catalog_pages.CatalogCapacityExceeded):
            catalog_pages.page(
                db,
                tenant_id=FOREIGN,
                endpoint="claims",
                query={"run_id": "global-bytes"},
                cursor=None,
                limit=1,
                now=NOW,
                load_items=lambda: [{"claim_id": "two"}],
            )
        assert db.execute("SELECT count(*) FROM catalog_list_snapshots").fetchone()[0] == 1
        db.rollback()


def test_catalog_page_is_reusable_for_claims_and_persists_original_epoch(tmp_path):
    from proofops.adapters.local.catalog_pages import initialize, page

    path = tmp_path / "catalog-claims.sqlite"
    run_id = str(uuid4())
    items = [{"claim_id": "a"}, {"claim_id": "b"}]
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        first = page(
            db,
            tenant_id=TENANT,
            endpoint="claims",
            query={"run_id": run_id, "filters": [None, None, None]},
            cursor=None,
            limit=1,
            now=NOW,
            load_items=lambda: {"items": iter(items), "snapshot_epoch": 7},
        )
        db.commit()

    assert first == {
        "items": [{"claim_id": "a"}],
        "next_cursor": first["next_cursor"],
        "snapshot_epoch": 7,
    }
    assert first["next_cursor"]

    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        repeated = page(
            db,
            tenant_id=TENANT,
            endpoint="claims",
            query={"run_id": run_id, "filters": [None, None, None]},
            cursor=None,
            limit=1,
            now=NOW + 1,
            load_items=lambda: {"items": items, "snapshot_epoch": 7},
        )
        assert db.execute("SELECT count(*) FROM catalog_list_snapshots").fetchone()[0] == 1
        db.commit()
    assert repeated["items"] == first["items"]
    assert repeated["snapshot_epoch"] == first["snapshot_epoch"]
    assert repeated["next_cursor"]

    items.append({"claim_id": "new-head"})

    with sqlite3.connect(path, isolation_level=None) as replica:
        replica.execute("BEGIN IMMEDIATE")
        initialize(replica)
        second = page(
            replica,
            tenant_id=TENANT,
            endpoint="claims",
            query={"run_id": run_id, "filters": [None, None, None]},
            cursor=first["next_cursor"],
            limit=1,
            now=NOW + 1,
            load_items=lambda: pytest.fail("continuation must use the persisted snapshot"),
        )
        replica.commit()

    assert second == {"items": [{"claim_id": "b"}], "next_cursor": None, "snapshot_epoch": 7}

    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        fresh = page(
            db,
            tenant_id=TENANT,
            endpoint="claims",
            query={"run_id": run_id, "filters": [None, None, None]},
            cursor=None,
            limit=3,
            now=NOW + 901,
            load_items=lambda: {"items": items, "snapshot_epoch": 8},
        )
        assert db.execute("SELECT count(*) FROM catalog_list_snapshots").fetchone()[0] == 1
        db.commit()
    assert fresh["items"][-1] == {"claim_id": "new-head"}
    assert fresh["snapshot_epoch"] == 8


def test_catalog_page_reads_existing_signed_snapshot_format(tmp_path):
    from proofops.adapters.local.catalog_pages import initialize, page

    path = tmp_path / "catalog-compatible.sqlite"
    query_hash = sha256(b'{"limit":1}').hexdigest()
    with sqlite3.connect(path, isolation_level=None) as db:
        db.execute("BEGIN IMMEDIATE")
        initialize(db)
        epoch = db.execute(
            "INSERT INTO catalog_list_snapshots "
            "(tenant_id,endpoint,query_hash,expires,payload) VALUES (?,?,?,?,?)",
            (TENANT, "claims", query_hash, NOW + 900, '[{"claim_id":"a"},{"claim_id":"b"}]'),
        ).lastrowid
        key = db.execute("SELECT secret FROM catalog_cursor_key WHERE id=1").fetchone()[0]
        payload = json.dumps(
            {
                "tenant": TENANT,
                "endpoint": "claims",
                "query_hash": query_hash,
                "expires": NOW + 900,
                "epoch": epoch,
                "after": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        cursor = base64.urlsafe_b64encode(payload + hmac.digest(key, payload, "sha256")).decode()
        result = page(
            db,
            tenant_id=TENANT,
            endpoint="claims",
            query={"limit": 1},
            cursor=cursor,
            limit=1,
            now=NOW,
            load_items=lambda: pytest.fail("existing snapshot must be reused"),
        )
        db.commit()
    assert result == {
        "items": [{"claim_id": "b"}],
        "next_cursor": None,
        "snapshot_epoch": epoch,
    }
