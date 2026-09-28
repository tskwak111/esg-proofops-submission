"""AT-001: real PDFs, SQLite persistence and synthetic local upload transport."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
from uuid import uuid4

import pytest
from pypdf import PdfWriter

TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "22222222-2222-4222-8222-222222222222"
RIGHTS = "33333333-3333-4333-8333-333333333333"


def pdf(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=600, height=800)
    stream = BytesIO()
    writer.write(stream)
    return stream.getvalue()


def setup_upload(tmp_path, *, data=None, limits=None, service=None):
    from proofops.application.registry import Registry, artifact_sha256
    from proofops.application.uploads import UploadService

    registry = service.registry if service is not None else Registry.empty()
    company = registry.create_company(actor="fixture", tenant_id=TENANT, legal_name="Synthetic Co")
    artifact = dict(tenant_id=TENANT, rights_profile_id=RIGHTS, status="approved", version="v1")
    registry.with_option(
        TENANT,
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
    if service is None:
        service = UploadService(
            tmp_path / "uploads.sqlite",
            tmp_path / "objects",
            registry,
            **({"limits": limits} if limits else {}),
        )
    document = service.create_document(
        TENANT,
        dict(company_id=company.company_id, title="Report", document_type="sustainability_report"),
        str(uuid4()),
    )
    data = pdf() if data is None else data
    payload = dict(
        filename="../../source.pdf",
        size_bytes=len(data),
        sha256=sha256(data).hexdigest(),
        report_year=2025,
        industry_system="unknown",
        period_start="2025-01-01",
        period_end="2025-12-31",
        rights_profile_id=RIGHTS,
    )
    return service, document, data, payload


def test_complete_is_durable_idempotent_and_metadata_and_bytes_are_immutable(tmp_path):
    from proofops.application.uploads import UploadService
    from proofops.application.uploads_security import UploadRejected

    service, doc, data, body = setup_upload(tmp_path)
    key = str(uuid4())
    ticket = service.initiate_upload(TENANT, doc["document_id"], body, key)
    assert service.initiate_upload(TENANT, doc["document_id"], body, key) == ticket
    service.receive_content(TENANT, ticket["upload_id"], data, "application/pdf")
    complete = dict(sha256=body["sha256"], size_bytes=len(data))
    with ThreadPoolExecutor(max_workers=2) as pool:
        versions = list(
            pool.map(
                lambda _: service.complete_upload(
                    TENANT, ticket["upload_id"], complete, str(uuid4())
                ),
                range(2),
            )
        )
    assert versions[0] == versions[1]
    first = versions[0]
    assert first["page_count"] == 1 and first["status"] == "ready"
    snapshot = service.version_snapshot(TENANT, first["version_id"])
    assert snapshot["metadata"]["period_start"] == "2025-01-01"
    assert snapshot["company"]["legal_name"] == "Synthetic Co"
    assert snapshot["document_type"] == "sustainability_report"
    assert snapshot["local_synthetic"] is True and snapshot["s3_version_id"] is None
    assert service.read_original(TENANT, first["version_id"]) == data
    with pytest.raises(UploadRejected):
        service.receive_content(TENANT, ticket["upload_id"], pdf(2), "application/pdf")
    reopened = UploadService(tmp_path / "uploads.sqlite", tmp_path / "objects", service.registry)
    assert reopened.complete_upload(TENANT, ticket["upload_id"], complete, str(uuid4())) == first
    next_body = dict(body, report_year=2026, period_start="2026-01-01", period_end="2026-12-31")
    next_ticket = reopened.initiate_upload(TENANT, doc["document_id"], next_body, str(uuid4()))
    reopened.receive_content(TENANT, next_ticket["upload_id"], data, "application/pdf")
    second = reopened.complete_upload(TENANT, next_ticket["upload_id"], complete, str(uuid4()))
    assert second["version_id"] != first["version_id"]
    assert reopened.version_snapshot(TENANT, first["version_id"]) == snapshot
    assert reopened.get_document(TENANT, doc["document_id"])["revision"] == 3
    service.close()
    reopened.close()


@pytest.mark.parametrize(
    "data,mime,code",
    [(b"fake PDF", "application/pdf", "PDF_INVALID"), (pdf(), "image/png", "PDF_INVALID")],
)
def test_forged_mime_or_magic_never_publishes_version(tmp_path, data, mime, code):
    from proofops.application.uploads_security import UploadRejected

    service, doc, _, body = setup_upload(tmp_path, data=data)
    ticket = service.initiate_upload(TENANT, doc["document_id"], body, str(uuid4()))
    with pytest.raises(UploadRejected, match=code):
        service.receive_content(TENANT, ticket["upload_id"], data, mime)
        service.complete_upload(
            TENANT,
            ticket["upload_id"],
            dict(sha256=body["sha256"], size_bytes=len(data)),
            str(uuid4()),
        )
    assert service.get_document(TENANT, doc["document_id"])["latest_version_id"] is None
    assert not list((tmp_path / "objects" / "original").rglob("*.pdf"))
    service.close()


def test_size_hash_tenant_rights_expiry_and_idempotency_guards(tmp_path):
    from proofops.application.uploads_security import PdfLimits, UploadRejected

    service, doc, data, body = setup_upload(tmp_path, limits=PdfLimits(max_bytes=1000))
    with pytest.raises(UploadRejected, match="UPLOAD_LIMIT_EXCEEDED"):
        service.initiate_upload(
            TENANT, doc["document_id"], dict(body, size_bytes=1001), str(uuid4())
        )
    for tenant, payload in [(FOREIGN, body), (TENANT, dict(body, rights_profile_id=str(uuid4())))]:
        with pytest.raises(UploadRejected, match="NOT_FOUND"):
            service.initiate_upload(tenant, doc["document_id"], payload, str(uuid4()))
    key = str(uuid4())
    ticket = service.initiate_upload(TENANT, doc["document_id"], body, key)
    with pytest.raises(UploadRejected, match="IDEMPOTENCY_CONFLICT"):
        service.initiate_upload(TENANT, doc["document_id"], dict(body, report_year=2024), key)
    with pytest.raises(UploadRejected, match="NOT_FOUND"):
        service.receive_content(FOREIGN, ticket["upload_id"], data, "application/pdf")
    with pytest.raises(UploadRejected, match="UPLOAD_LIMIT_EXCEEDED"):
        service.receive_content(TENANT, ticket["upload_id"], b"x" * 1001, "application/pdf")
    service.receive_content(TENANT, ticket["upload_id"], data, "application/pdf")
    with pytest.raises(UploadRejected, match="UPLOAD_INTEGRITY_MISMATCH"):
        service.complete_upload(
            TENANT, ticket["upload_id"], dict(sha256="0" * 64, size_bytes=len(data)), str(uuid4())
        )
    with pytest.raises(UploadRejected, match="NOT_FOUND"):
        service.complete_upload(
            FOREIGN,
            ticket["upload_id"],
            dict(sha256=body["sha256"], size_bytes=len(data)),
            str(uuid4()),
        )
    service.clock = lambda: 10**12
    with pytest.raises(UploadRejected, match="UPLOAD_EXPIRED"):
        service.complete_upload(
            TENANT,
            ticket["upload_id"],
            dict(sha256=body["sha256"], size_bytes=len(data)),
            str(uuid4()),
        )
    service.close()


def http_client(service, *, tenant=TENANT, role="editor", app=None):
    import time

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
    from proofops.application.authorization import MembershipRecord, SessionRecord
    from proofops_api.auth import AuthStore
    from proofops_api.routers.documents import build_documents_router

    store = (
        app.state.composition.auth_store
        if app is not None
        else AuthStore(sessions=InMemorySessionStore(), memberships=InMemoryMembershipStore())
    )
    store.sessions.put_with_token(
        SessionRecord(
            "upload-session",
            "user",
            tenant,
            store.hash_csrf("csrf"),
            time.time() + 3600,
            time.time() + 3600,
            False,
        ),
        "csrf",
    )
    store.memberships.put(MembershipRecord(tenant, "user", role, "active"))
    if app is None:
        app = FastAPI()
        app.include_router(
            build_documents_router(
                service,
                store,
                allowed_origin="http://testserver",
                app_env="local",
                model_adapter="synthetic",
            )
        )
    client = TestClient(app)
    client.cookies.set("__Host-proofops_session", "upload-session")
    client.headers.update(
        {
            "Origin": "http://testserver",
            "X-CSRF-Token": store.sessions.csrf_token_for("upload-session"),
            "Idempotency-Key": str(uuid4()),
        }
    )
    return client


def assert_contract(name, payload):
    from pathlib import Path

    import yaml
    from jsonschema import Draft202012Validator, FormatChecker

    schemas = yaml.safe_load(Path("contracts/openapi.yaml").read_text())["components"]["schemas"]
    Draft202012Validator(
        dict(schemas[name], components={"schemas": schemas}), format_checker=FormatChecker()
    ).validate(payload)


def test_http_real_multipart_to_version_roundtrip_and_contracts(tmp_path):
    service, doc, data, body = setup_upload(tmp_path)
    with http_client(service) as client:
        created = client.post(
            "/v1/documents", json={k: doc[k] for k in ("company_id", "title", "document_type")}
        )
        assert created.status_code == 201, created.text
        assert_contract("Document", created.json())
        target = created.json()["document_id"]
        ticket_response = client.post(f"/v1/documents/{target}/versions", json=body)
        assert ticket_response.status_code == 201, ticket_response.text
        ticket = ticket_response.json()
        assert_contract("UploadTicket", ticket)
        receipt = client.post(
            ticket["post_url"],
            data=ticket["post_fields"],
            files={"file": ("source.pdf", data, "application/pdf")},
        )
        assert receipt.status_code == 204, receipt.text
        repeat_receipt = client.post(
            ticket["post_url"],
            data=ticket["post_fields"],
            files={"file": ("source.pdf", data, "application/pdf")},
        )
        assert repeat_receipt.status_code == 409
        completed = client.post(
            f"/v1/uploads/{ticket['upload_id']}/complete",
            json=dict(sha256=body["sha256"], size_bytes=len(data)),
        )
        assert completed.status_code == 202, completed.text
        job = completed.json()
        assert_contract("JobAccepted", job)
        version = client.get(job["status_url"])
        assert version.status_code == 200
        assert version.json()["status"] == "ready"
        assert_contract("DocumentVersion", version.json())
        retry = client.post(
            f"/v1/uploads/{ticket['upload_id']}/complete",
            json=dict(sha256=body["sha256"], size_bytes=len(data)),
        )
        assert retry.json() == job
        assert "/local/" not in str(client.get("/openapi.json").json()["paths"])
        with http_client(service, tenant=FOREIGN) as other:
            assert other.get(job["status_url"]).status_code == 404
            assert other.get(f"/v1/documents/{target}").status_code == 404
    service.close()


def test_http_write_guards_reject_unsafe_ticket_fields_and_payloads(tmp_path):
    service, doc, data, body = setup_upload(tmp_path)
    with http_client(service) as client:
        url = f"/v1/documents/{doc['document_id']}/versions"
        assert client.post(url, json=dict(body, label="SUBSTANTIATED")).status_code == 422
        assert client.post(url, json=dict(body, size_bytes=True)).status_code == 422
        assert client.post(url, json=dict(body, period_end="2024-01-01")).status_code == 422
        ticket = client.post(url, json=body).json()
        files = {"file": ("source.pdf", data, "application/pdf")}
        assert (
            client.post(ticket["post_url"], data={"ticket": "wrong"}, files=files).status_code
            == 403
        )
        assert (
            client.post(ticket["post_url"], data={"ticket": "위조"}, files=files).status_code == 403
        )

        assert (
            client.post(
                ticket["post_url"], data=dict(ticket["post_fields"], key="foreign"), files=files
            ).status_code
            == 422
        )
        csrf = client.headers.pop("X-CSRF-Token")
        assert (
            client.post(ticket["post_url"], data=ticket["post_fields"], files=files).status_code
            == 403
        )
        assert client.post(url, json=body).status_code == 403
        client.headers["X-CSRF-Token"] = csrf
        client.headers["Origin"] = "https://foreign.invalid"
        assert client.post(url, json=body).status_code == 403
        client.cookies.clear()
        assert client.get(f"/v1/documents/{doc['document_id']}").status_code == 401
    with http_client(service, role="viewer") as viewer:
        assert viewer.post(url, json=body).status_code == 403
    service.close()


def test_separate_database_connections_complete_once_and_detect_stored_byte_tampering(tmp_path):
    from proofops.application.uploads import UploadService
    from proofops.application.uploads_security import UploadRejected

    service, doc, data, body = setup_upload(tmp_path)
    other = UploadService(tmp_path / "uploads.sqlite", tmp_path / "objects", service.registry)
    ticket = service.initiate_upload(TENANT, doc["document_id"], body, str(uuid4()))
    service.receive_content(TENANT, ticket["upload_id"], data, "application/pdf")
    complete = dict(sha256=body["sha256"], size_bytes=len(data))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda store: store.complete_upload(
                    TENANT, ticket["upload_id"], complete, str(uuid4())
                ),
                (service, other),
            )
        )
    assert results[0] == results[1]
    assert other.get_document(TENANT, doc["document_id"])["revision"] == 2
    results[0]["status"] = "rejected"
    assert service.get_version(TENANT, results[0]["version_id"])["status"] == "ready"
    with pytest.raises(UploadRejected, match="NOT_FOUND"):
        other.read_original(FOREIGN, results[0]["version_id"])
    # Simulate actual storage corruption, not a mocked verifier/checksum response.
    path = tmp_path / "objects" / "original" / TENANT / f"{results[0]['version_id']}.pdf"
    path.chmod(0o600)
    path.write_bytes(b"corrupted")
    with pytest.raises(UploadRejected, match="UPLOAD_INTEGRITY_MISMATCH"):
        service.read_original(TENANT, results[0]["version_id"])
    ticket2 = service.initiate_upload(TENANT, doc["document_id"], body, str(uuid4()))
    service.receive_content(TENANT, ticket2["upload_id"], data, "application/pdf")
    path = tmp_path / "objects" / "quarantine" / TENANT / f"{ticket2['upload_id']}.pdf"
    path.chmod(0o600)
    path.write_bytes(b"corrupted")
    with pytest.raises(UploadRejected, match="UPLOAD_INTEGRITY_MISMATCH"):
        service.complete_upload(TENANT, ticket2["upload_id"], complete, str(uuid4()))
    assert other.get_document(TENANT, doc["document_id"])["revision"] == 2
    service.close()
    other.close()


def test_local_upload_mount_gate_and_stream_byte_limit(tmp_path):
    from proofops.application.uploads_security import PdfLimits
    from proofops_api.routers.documents import build_documents_router

    service, doc, data, body = setup_upload(tmp_path, limits=PdfLimits(max_bytes=1000))
    for env, adapter in [("production", "synthetic"), ("local", "bedrock")]:
        with pytest.raises(ValueError, match="local-synthetic"):
            build_documents_router(
                service,
                None,
                allowed_origin="http://testserver",
                app_env=env,
                model_adapter=adapter,
            )
    with http_client(service) as client:
        ticket = client.post(f"/v1/documents/{doc['document_id']}/versions", json=body).json()

        # Generator has no Content-Length: server must enforce actual received bytes.
        def oversized_stream():
            yield b'--boundary\r\nContent-Disposition: form-data; name="file"; filename="x.pdf"\r\n'
            yield b"Content-Type: application/pdf\r\n\r\n" + b"x" * 70000
            yield b"\r\n--boundary--\r\n"

        response = client.post(
            ticket["post_url"],
            content=oversized_stream(),
            headers={"Content-Type": "multipart/form-data; boundary=boundary"},
        )
        assert response.status_code == 422
        assert not list((tmp_path / "objects").rglob("*.pdf"))
        valid = client.post(
            ticket["post_url"],
            data=ticket["post_fields"],
            files={"file": ("source.pdf", data, "application/pdf")},
        )
        assert valid.status_code == 204
    service.close()


def test_validation_job_survives_restart_and_older_upload_cannot_replace_new_head(tmp_path):
    from proofops.application.uploads import UploadService

    service, doc, data, body = setup_upload(tmp_path)
    service.clock = lambda: 1800000000
    older = service.initiate_upload(TENANT, doc["document_id"], body, str(uuid4()))
    service.receive_content(TENANT, older["upload_id"], data, "application/pdf")
    complete = dict(sha256=body["sha256"], size_bytes=len(data))
    queued = service.enqueue_validation(TENANT, older["upload_id"], complete, str(uuid4()))
    assert queued["job_id"] != older["upload_id"] and queued["status"] == "queued"
    service.clock = lambda: 1800000000.5
    newer = service.initiate_upload(
        TENANT, doc["document_id"], dict(body, report_year=2026), str(uuid4())
    )
    service.receive_content(TENANT, newer["upload_id"], data, "application/pdf")
    latest = service.complete_upload(TENANT, newer["upload_id"], complete, str(uuid4()))
    service.close()
    reopened = UploadService(
        tmp_path / "uploads.sqlite",
        tmp_path / "objects",
        service.registry,
        clock=lambda: 1800000002,
    )
    earlier = reopened.complete_upload(TENANT, older["upload_id"], complete, str(uuid4()))
    assert (
        reopened.get_document(TENANT, doc["document_id"])["latest_version_id"]
        == latest["version_id"]
    )
    job = reopened.validation_job(TENANT, older["upload_id"])
    assert job["job_id"] == queued["job_id"] and job["status"] == "ready"
    assert job["result_version_id"] == earlier["version_id"] and job["attempt"] == 1
    assert reopened.get_version(TENANT, earlier["version_id"])["status"] == "ready"
    reopened.close()


def test_unknown_upload_component_schema_is_rejected(tmp_path):
    import sqlite3

    from proofops.application.uploads import UploadService

    database = tmp_path / "future.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE upload_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO upload_metadata VALUES ('schema_version','999')")
    with pytest.raises(ValueError, match="schema"):
        UploadService(database, tmp_path / "objects", None)


def test_upload_openapi_security_errors_and_etag_match_contract(tmp_path):
    service, doc, data, body = setup_upload(tmp_path)
    with http_client(service) as client:
        operation = client.get("/openapi.json").json()["paths"]["/v1/documents"]["post"]
        assert operation["security"] == [{"sessionCookie": []}]
        assert operation["x-minimum-role"] == "editor"
        assert operation["x-idempotency-required"] is True
        assert operation["responses"]["403"]["content"]["application/json"]["schema"][
            "$ref"
        ].endswith("/Error")
        headers = {p["name"] for p in operation["parameters"] if p["in"] == "header"}
        assert {"Idempotency-Key", "X-CSRF-Token"} <= headers
        response = client.get(f"/v1/documents/{doc['document_id']}")
        assert response.headers["ETag"] == '"1"'
    service.close()


def test_upload_audit_is_atomic_append_only_and_replay_does_not_duplicate(tmp_path):
    import sqlite3

    from proofops.application.uploads_security import UploadRejected

    service, doc, data, body = setup_upload(tmp_path)
    ticket = service.initiate_upload(
        TENANT, doc["document_id"], body, str(uuid4()), actor_sub="authenticated-editor"
    )
    service.receive_content(TENANT, ticket["upload_id"], data, "application/pdf")
    before = service.read_upload_audit(TENANT, doc["document_id"])
    complete = dict(sha256=body["sha256"], size_bytes=len(data))
    key = str(uuid4())
    with sqlite3.connect(tmp_path / "uploads.sqlite") as connection:
        connection.execute(
            "CREATE TRIGGER fail_audit BEFORE INSERT ON upload_audit "
            "BEGIN SELECT RAISE(ABORT, 'test storage failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="test storage failure"):
        service.complete_upload(
            TENANT, ticket["upload_id"], complete, key, actor_sub="authenticated-editor"
        )
    assert service.get_document(TENANT, doc["document_id"])["latest_version_id"] is None
    job = service.validation_job(TENANT, ticket["upload_id"])
    assert job["status"] == "running"
    with pytest.raises(UploadRejected, match="NOT_FOUND"):
        service.get_version(TENANT, job["version_id"])
    assert service.read_upload_audit(TENANT, doc["document_id"]) == before
    with sqlite3.connect(tmp_path / "uploads.sqlite") as connection:
        connection.execute("DROP TRIGGER fail_audit")
    version = service.complete_upload(
        TENANT, ticket["upload_id"], complete, key, actor_sub="authenticated-editor"
    )
    assert (
        service.complete_upload(
            TENANT, ticket["upload_id"], complete, key, actor_sub="authenticated-editor"
        )
        == version
    )
    events = service.read_upload_audit(TENANT, doc["document_id"])
    assert len(events) == len(before) + 1
    event = events[-1]
    assert event["actor_sub"] == "authenticated-editor"
    assert event["source_sha256"] == body["sha256"] and event["version_id"] == version["version_id"]
    assert event["job_id"] == job["job_id"] and event["upload_id"] == ticket["upload_id"]
    assert event["action"] == "upload.accepted"
    assert event["previous_event_hash"] == before[-1]["event_hash"]
    assert "source.pdf" not in str(events) and "Synthetic Co" not in str(events)
    with pytest.raises(UploadRejected, match="NOT_FOUND"):
        service.read_upload_audit(FOREIGN, doc["document_id"])
    with sqlite3.connect(tmp_path / "uploads.sqlite") as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("UPDATE upload_audit SET data='{}'")
    service.close()


def test_composed_app_upload_roundtrip_survives_application_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")
    monkeypatch.setenv("APP_ORIGIN", "http://testserver")
    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(tmp_path / "state.sqlite3"))
    from proofops_api.main import create_app

    app = create_app()
    service, doc, data, body = setup_upload(tmp_path, service=app.state.composition.uploads)
    with http_client(service, app=app) as client:
        ticket = client.post(f"/v1/documents/{doc['document_id']}/versions", json=body).json()
        receipt = client.post(
            ticket["post_url"],
            data=ticket["post_fields"],
            files={"file": ("synthetic.pdf", data, "application/pdf")},
        )
        assert receipt.status_code == 204
        completed = client.post(
            f"/v1/uploads/{ticket['upload_id']}/complete",
            json=dict(sha256=body["sha256"], size_bytes=len(data)),
        )
        assert completed.status_code == 202
        result = completed.json()
        assert result["job_id"] != ticket["upload_id"]
        events = service.read_upload_audit(TENANT, doc["document_id"])
        assert events[-1]["actor_sub"] == "user" and events[-1]["job_id"] == result["job_id"]
    reopened = create_app()
    with http_client(reopened.state.composition.uploads, app=reopened) as client:
        version = client.get(result["status_url"])
        assert version.status_code == 200 and version.json()["status"] == "ready"
        assert (
            reopened.state.composition.uploads.read_original(TENANT, result["resource_id"]) == data
        )
