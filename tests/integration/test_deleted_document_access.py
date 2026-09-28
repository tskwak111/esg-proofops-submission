"""Deletion tombstones fence existing local data, tickets, caches and worker leases."""

from dataclasses import replace
from uuid import uuid4

import pytest
from proofops.application.registry import Registry
from proofops.application.uploads import UploadRejected, UploadService

from tests.acceptance.test_exports import archive, create, exports
from tests.integration import test_run_lifecycle as lifecycle
from tests.integration.test_local_tag_runner import TENANT


def test_tombstone_hides_existing_artifacts_and_fences_work(tmp_path, monkeypatch):
    from proofops.adapters.local.retention_store import LocalRetentionStore
    from proofops_api.routers.deletion import build_deletion_router

    database = tmp_path / "runs.sqlite"
    uploads = UploadService(database, tmp_path / "objects", Registry.sqlite(database))
    original_upload = lifecycle.setup_upload
    monkeypatch.setattr(
        lifecycle,
        "setup_upload",
        lambda directory, **kwargs: original_upload(directory, service=uploads, **kwargs),
    )
    ws = exports(tmp_path, monkeypatch)
    run_id = ws["run"]
    version_id = ws["jobs"].get_run(TENANT, run_id)["document_version_id"]
    version = uploads.version_snapshot(TENANT, version_id)
    document_id = version["document_id"]
    document = uploads.get_document(TENANT, document_id)
    uploads.create_document(
        TENANT,
        {key: document[key] for key in ("company_id", "title", "document_type")},
        str(uuid4()),
    )
    cursor = uploads.list_documents(TENANT, limit=1)["next_cursor"]
    assert cursor
    result = create(ws).json()
    _, ticket, old_bytes = archive(ws, result)
    now = int(ws["now"][0])
    message = replace(ws["checkpoint"], job_id=str(uuid4()), shard="retention-race")
    ws["jobs"].enqueue(message, now=now)
    lease = ws["jobs"].claim_job(message, owner="deletion-race", now=now, lease_seconds=60)
    assert lease and ws["jobs"].can_call(lease, now=now)

    retention = LocalRetentionStore(uploads)
    ws["http"].app.include_router(
        build_deletion_router(retention, ws["auth"], allowed_origin="https://testserver")
    )
    response = ws["http"].post(
        f"/v1/documents/{document_id}/deletion-requests",
        json={"reason": "Explicit synthetic deletion request"},
    )
    assert response.status_code == 202, response.text
    request = response.json()
    assert request["status"] == "blocked_retention"
    with ws["jobs"]._transaction() as db:
        assert ws["jobs"].active_run_count(db, TENANT) == 0
    for read in (
        lambda: uploads.get_document(TENANT, document_id),
        lambda: uploads.get_version(TENANT, version_id),
        lambda: uploads.read_original(TENANT, version_id),
        lambda: uploads.list_versions(TENANT, document_id),
    ):
        with pytest.raises(UploadRejected):
            read()
    assert all(d["document_id"] != document_id for d in uploads.list_documents(TENANT)["items"])
    with pytest.raises(UploadRejected):
        uploads.list_documents(TENANT, cursor=cursor, limit=1)
    assert ws["service"].store.list(TENANT, now=now)["items"] == []
    assert ws["http"].get(f"/v1/runs/{run_id}").status_code == 404
    assert ws["http"].get(f"/v1/exports/{result['export_id']}").status_code == 404
    assert ws["http"].get(ticket["url"]).status_code == 404
    assert ws["http"].post(f"/v1/exports/{result['export_id']}/download").status_code == 404
    assert not ws["jobs"].can_call(lease, now=now)
    assert not ws["jobs"].commit_job(lease, payload=b"late worker output", now=now)
    with pytest.raises((ValueError, KeyError)):
        ws["runner"].reviews.store.get(TENANT, ws["review"]["review_id"])
    # Missing policy hides access but does not pretend retained immutable bytes were erased.
    assert (uploads.root / "original" / TENANT / f"{version_id}.pdf").is_file()
    with ws["jobs"]._transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE tenant_id=? AND run_id=? "
                "AND kind='export_artifact'",
                (TENANT, run_id),
            ).fetchone()[0]
            == 1
        )
    assert old_bytes
