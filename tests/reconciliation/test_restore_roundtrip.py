"""Public registration/revision survive a quiesced backup of a synthetic run.

SQLite, upload, extraction/tag runners and reconciliation stores are real;
the parser/model adapters and reviewer identity are explicit synthetic fixtures.
This is recovery correctness, not live-model or human-review evidence.
"""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.reconciliation_store import (
    LocalReconciliationStore,
    ReconciliationRejected,
)
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.adapters.local.tag_store import LocalTagStore
from proofops.application.uploads import UploadService

from tests.acceptance.test_upload import TENANT
from tests.integration.test_local_tag_runner import SyntheticVerifiedParser
from tests.reconciliation.test_product_store import (
    auth,
    build_verified_run,
    prepare_bundle,
    review_body,
)


def test_public_case_and_revision_restore_without_reading_live_artifacts(tmp_path, monkeypatch):
    live = tmp_path / "live"
    live.mkdir()
    verified = build_verified_run(live, monkeypatch)
    service = verified["service"]
    store = LocalReconciliationStore(
        service.store.path,
        live / "reconciliation-artifacts",
        run_store=service.store,
        claims=verified["claims"],
        tags=verified["tags"],
    )
    bundle, source_root = prepare_bundle(tmp_path / "prepared", verified)
    detail = store.register_case(
        auth(), verified["run_id"], verified["claim_id"], bundle, source_root
    )
    case_id = detail["case_id"]
    reviewer = auth("reviewer")
    key = str(uuid4())
    body = review_body()
    expected = store.review(reviewer, case_id, body, '"1"', key)
    assert expected["revision"] == 2
    assert expected["policy_approved"] is False
    revisions = [store.revision(auth(), case_id, n) for n in (1, 2)]
    sources = {
        source["source_id"]: store.source_content(auth(), case_id, source["source_id"])
        for source in expected["sources"]
    }
    assert sources

    # No writer runs between the database snapshots and artifact copies.
    # This fixture has separate upload/run DBs; the composed product uses one.
    backup = tmp_path / "backup"
    backup.mkdir()
    databases = list(live.glob("*.sqlite"))
    assert {path.name for path in databases} >= {"runs.sqlite", "uploads.sqlite"}
    for database in databases:
        with sqlite3.connect(database) as source, sqlite3.connect(backup / database.name) as target:
            source.backup(target)
    for tree in ("objects", "synthetic-prepared", "reconciliation-artifacts"):
        shutil.copytree(live / tree, backup / tree)
    restored = tmp_path / "restored"
    shutil.copytree(backup, restored)

    # Reject any accidental fallback to the original artifact tree.
    original_read = Path.read_bytes

    def restored_only(path):
        assert not path.resolve().is_relative_to(live.resolve())
        return original_read(path)

    monkeypatch.setattr(Path, "read_bytes", restored_only)
    runs = LocalSQLiteRunStore(restored / "runs.sqlite")
    uploads = UploadService(
        restored / "uploads.sqlite", restored / "objects", service.uploads.registry
    )
    parser = SyntheticVerifiedParser(restored / "synthetic-prepared")
    claims = LocalClaimStore(runs, uploads, parser)
    tags = LocalTagStore(runs, uploads, parser)
    recovered = LocalReconciliationStore(
        restored / "runs.sqlite",
        restored / "reconciliation-artifacts",
        run_store=runs,
        claims=claims,
        tags=tags,
    )
    assert claims.get(TENANT, verified["run_id"], verified["claim_id"]) == verified["claim"]
    assert recovered.get_case(auth(), case_id) == expected
    assert [recovered.revision(auth(), case_id, n) for n in (1, 2)] == revisions
    assert recovered.review(reviewer, case_id, body, '"1"', key) == expected
    for source_id, payload in sources.items():
        assert recovered.source_content(auth(), case_id, source_id) == payload
    with sqlite3.connect(restored / "runs.sqlite") as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)

    source_id = next(iter(sources))
    recovered.managed_path(TENANT, case_id, source_id).write_bytes(b"tampered restore")
    with pytest.raises(ReconciliationRejected) as error:
        recovered.source_content(auth(), case_id, source_id)
    assert error.value.code == "SOURCE_UNVERIFIED"
    uploads._db.close()
