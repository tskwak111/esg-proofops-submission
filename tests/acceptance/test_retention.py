"""AT-040: isolated synthetic SQLite deletion and restore; no mocks or cloud calls."""

import importlib
import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

TENANT = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"
DOC = "33333333-3333-4333-8333-333333333333"
KINDS = ("original", "derived", "search", "cache", "review", "memory", "export", "backup")


def setup(tmp_path):
    try:
        app = importlib.import_module("proofops.application.retention")
        worker = importlib.import_module("proofops_worker.deletion")
    except ImportError as exc:
        pytest.fail(f"Retention implementation missing: {exc}")
    store = worker.LocalSyntheticDeletionStore(tmp_path / "data.sqlite", tmp_path / "ledger.sqlite")
    for tenant in (TENANT, OTHER):
        for kind in KINDS:
            for version in ("v1", "v2"):
                store.put(tenant, DOC, version, kind, "item", b"synthetic content", expires_at=1)
    manifest = store.request(TENANT, DOC, requested_by="fixture", now=10)
    policy = app.RetentionPolicy(TENANT, DOC, "approved-synthetic-test-only", approved=True)
    return app, store, manifest, policy


def remaining(store, tenant=TENANT):
    with sqlite3.connect(store.data_path) as db:
        return db.execute("SELECT count(*) FROM resources WHERE tenant_id=?", (tenant,)).fetchone()[
            0
        ]


def test_actual_deletion_covers_all_versions_kinds_and_preserves_other_tenant(tmp_path):
    app, store, manifest, policy = setup(tmp_path)
    result = app.delete_document_tree(manifest, policy, store, now=20)
    assert result.status == "completed" and result.completed_at == 20
    assert len(result.deleted) == 16 and result.remaining == ()
    assert remaining(store) == 0 and remaining(store, OTHER) == 16
    assert store.read(OTHER, DOC, "v1", "original", "item") == b"synthetic content"
    assert store.attempts(TENANT, DOC)[0] == result
    assert app.delete_document_tree(manifest, policy, store, now=21).status == "completed"
    assert store.attempts(TENANT, DOC)[0] == result  # prior outcome stays immutable


@pytest.mark.parametrize(
    "change", [{"approved": False}, {"legal_hold": True}, {"retain_until": 30}]
)
def test_ttl_and_legal_policy_never_claim_physical_deletion(tmp_path, change):
    app, store, manifest, policy = setup(tmp_path)
    result = app.delete_document_tree(manifest, replace(policy, **change), store, now=20)
    assert result.status == "blocked_retention" and result.completed_at is None
    assert len(result.remaining) == 16 and remaining(store) == 16
    assert result.deleted == ()
    with pytest.raises(KeyError):
        store.read(TENANT, DOC, "v1", "original", "item")
    with pytest.raises(ValueError, match="tombstone"):
        store.put(TENANT, DOC, "new", "cache", "new", b"late writer")


def test_real_delete_failure_is_retryable_and_does_not_leak_payload(tmp_path):
    app, store, manifest, policy = setup(tmp_path)
    with sqlite3.connect(store.data_path) as db:
        db.execute("""CREATE TRIGGER hold_cache BEFORE DELETE ON resources
            WHEN OLD.kind='cache' BEGIN SELECT RAISE(ABORT, 'private body'); END""")
    failed = app.delete_document_tree(manifest, policy, store, now=20)
    assert failed.status == "failed" and failed.completed_at is None
    assert len(failed.remaining) == 2 and remaining(store) == 2
    assert "private body" not in repr(failed)
    with sqlite3.connect(store.data_path) as db:
        db.execute("DROP TRIGGER hold_cache")
    assert app.delete_document_tree(manifest, policy, store, now=21).status == "completed"
    assert store.attempts(TENANT, DOC)[0] == failed


def test_delete_ack_without_absence_does_not_complete(tmp_path):
    app, store, manifest, policy = setup(tmp_path)
    with sqlite3.connect(store.data_path) as db:
        db.execute("""CREATE TRIGGER delayed_cache BEFORE DELETE ON resources
            WHEN OLD.kind='cache' BEGIN SELECT RAISE(IGNORE); END""")
    result = app.delete_document_tree(manifest, policy, store, now=20)
    assert result.status == "running" and result.completed_at is None
    assert len(result.remaining) == 2 and remaining(store) == 2


def test_restore_replays_durable_tombstones_and_discovers_unlisted_restored_artifacts(tmp_path):
    app, store, manifest, policy = setup(tmp_path)
    backup = tmp_path / "backup.sqlite"
    with sqlite3.connect(store.data_path) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    first = app.delete_document_tree(manifest, policy, store, now=20)
    with sqlite3.connect(backup) as source, sqlite3.connect(store.data_path) as target:
        source.backup(target)
        target.execute(
            "INSERT INTO resources VALUES (?,?,?,?,?,?,?)",
            (TENANT, DOC, "restored-v3", "cache", "new", b"restored", 1),
        )
    reopened = type(store)(store.data_path, store.ledger_path)
    with pytest.raises(KeyError):
        reopened.read(TENANT, DOC, "v1", "original", "item")
    results = app.reapply_tombstones(reopened, {(TENANT, DOC): policy}, now=30)
    assert len(results) == 1 and results[0].status == "completed"
    assert len(results[0].deleted) == 17
    assert remaining(reopened) == 0 and remaining(reopened, OTHER) == 16
    assert reopened.attempts(TENANT, DOC)[0] == first


def test_wrong_scope_and_unknown_restore_policy_fail_closed(tmp_path):
    app, store, manifest, policy = setup(tmp_path)
    with pytest.raises(ValueError, match="scope"):
        app.delete_document_tree(manifest, replace(policy, tenant_id=OTHER), store, now=20)
    with pytest.raises((KeyError, ValueError)):
        app.delete_document_tree(replace(manifest, deletion_id=str(uuid4())), policy, store, now=20)
    result = app.reapply_tombstones(store, {}, now=30)[0]
    assert result.status == "blocked_retention" and remaining(store) == 16


def test_policy_validation_prevents_truthy_strings_and_invalid_times(tmp_path):
    app, store, manifest, policy = setup(tmp_path)
    for changes in ({"approved": "false"}, {"legal_hold": "false"}, {"retain_until": float("nan")}):
        with pytest.raises(ValueError):
            replace(policy, **changes)
    with pytest.raises(ValueError):
        app.delete_document_tree(manifest, policy, store, now=float("nan"))
    assert remaining(store) == 16
