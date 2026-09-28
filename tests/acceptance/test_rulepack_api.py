"""HTTP contract checks for TASK-025 rule-pack activation integration."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.application.authorization import MembershipRecord, SessionRecord
from proofops.application.rulepacks import (
    RulePackRecord,
    RunSnapshot,
    compute_pack_sha256,
)
from proofops_api.auth import AuthStore

TENANT_A = "11111111-1111-4111-8111-111111111111"
TENANT_B = "22222222-2222-4222-8222-222222222222"
PACK_A = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
RUN_A = "33333333-3333-4333-8333-333333333333"
SOURCE_SHA = "0f9d7e3495d1bc6330ee4ffd2bb644ce8189c89e91dd5f9ae4c720b9ffaebc6a"
GAP_IDS = tuple(f"GAP-{number:03d}" for number in range(1, 11))


def _files() -> dict[str, dict[str, object]]:
    return {
        "regulatory/safe_harbor.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
            "source_document_sha256": SOURCE_SHA,
            "grade_mapping": None,
            "reasonable_basis_boolean_mapping": None,
        },
        "regulatory/timeline.yaml": {
            "version": "proofops-domain-v2.0-impl1",
            "effective_date": "2026-09-08",
            "source_document_sha256": SOURCE_SHA,
            "automatic_legal_applicability_enabled": False,
        },
    }


def _pack(
    *,
    tenant_id: str = TENANT_A,
    pack_id: str = PACK_A,
    status: str = "validated",
    approved_by: str | None = "domain-owner",
) -> RulePackRecord:
    files = _files()
    data: dict[str, object] = {
        "rule_pack_id": pack_id,
        "tenant_id": tenant_id,
        "version": "proofops-domain-v2.0-impl1",
        "effective_date": "2026-09-08",
        "mode": "disclosure",
        "status": status,
        "ontology_version": "proofops-elements-1",
        "source_document_sha256": SOURCE_SHA,
        "files": list(files),
        "unresolved_gap_ids": list(GAP_IDS),
        "approved_by": approved_by,
        "approved_at": "2026-09-08T10:00:00Z" if approved_by else None,
    }
    data["sha256"] = compute_pack_sha256(data, files)
    return RulePackRecord.from_dict(data)


def _client(tmp_path: Path, *, role: str = "admin") -> tuple[TestClient, object, str]:
    try:
        from proofops.adapters.local.rulepack_store import RulePackSqliteStore
        from proofops_api.rulepacks import build_rulepack_router
    except ModuleNotFoundError as exc:  # RED: endpoint/store do not exist yet.
        pytest.fail(str(exc))

    sessions = InMemorySessionStore()
    memberships = InMemoryMembershipStore()
    auth_store = AuthStore(sessions=sessions, memberships=memberships)
    csrf_token = _seed_auth(auth_store, role=role)
    rulepacks = RulePackSqliteStore(tmp_path / "state.sqlite3")
    app = FastAPI()
    app.include_router(
        build_rulepack_router(
            rulepacks,
            auth_store,
            gap_ids=GAP_IDS,
            allowed_origin="http://testserver",
        )
    )
    client = TestClient(app)
    client.cookies.set("__Host-proofops_session", "admin-session")
    return client, rulepacks, csrf_token


def _seed_auth(auth_store: AuthStore, *, role: str = "admin") -> str:
    far_future = time.time() + 10_000
    auth_store.sessions.put_with_token(
        SessionRecord(
            "admin-session",
            "admin-user",
            TENANT_A,
            auth_store.hash_csrf("csrf-token"),
            far_future,
            far_future,
            False,
        ),
        "csrf-token",
    )
    auth_store.memberships.put(MembershipRecord(TENANT_A, "admin-user", role, "active"))
    csrf_token = auth_store.sessions.csrf_token_for("admin-session")
    assert csrf_token is not None
    return csrf_token


def _headers(
    csrf_token: str, *, if_match: int = 1, key: str = "activate-rulepack-key"
) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf_token,
        "Origin": "http://testserver",
        "Idempotency-Key": key,
        "If-Match": f'"{if_match}"',
    }


def test_approved_activation_changes_new_run_pointer_not_inflight_snapshot(tmp_path: Path) -> None:
    client, store, csrf_token = _client(tmp_path)
    pack = _pack()
    store.add_pack(pack, _files())
    running = RunSnapshot(RUN_A, TENANT_A, PACK_A, pack.sha256, "running")
    store.add_run_snapshot(running)

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "approve validated pack"},
        headers=_headers(csrf_token),
    )

    assert response.status_code == 200
    assert response.json() == {
        "rule_pack_id": PACK_A,
        "version": "proofops-domain-v2.0-impl1",
        "sha256": pack.sha256,
        "status": "active",
        "mode": "disclosure",
        "effective_date": "2026-09-08",
        "unresolved_gap_ids": list(GAP_IDS),
    }
    assert response.headers["etag"] == '"2"'
    assert store.active_pack_id(TENANT_A, "disclosure") == PACK_A
    assert store.get_run_snapshot(TENANT_A, RUN_A) == running


def test_activation_requires_admin_capability(tmp_path: Path) -> None:
    client, store, csrf_token = _client(tmp_path, role="reviewer")
    store.add_pack(_pack(), _files())

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "reviewer cannot activate"},
        headers=_headers(csrf_token),
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    assert store.active_pack_id(TENANT_A, "disclosure") is None


def test_activation_requires_csrf_token_and_same_origin(tmp_path: Path) -> None:
    client, store, _csrf_token = _client(tmp_path)
    store.add_pack(_pack(), _files())
    headers = _headers("wrong-token")

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "attempt without valid csrf"},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CSRF_INVALID"
    assert store.active_pack_id(TENANT_A, "disclosure") is None


def test_cross_tenant_rule_pack_is_indistinguishable_from_missing(tmp_path: Path) -> None:
    client, store, csrf_token = _client(tmp_path)
    store.add_pack(_pack(tenant_id=TENANT_B), _files())

    hidden = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "try another tenant pack"},
        headers=_headers(csrf_token, key="cross-tenant-key"),
    )
    missing = client.post(
        "/v1/rule-packs/99999999-9999-4999-8999-999999999999/activate",
        json={"reason": "try missing tenant pack"},
        headers=_headers(csrf_token, key="missing-pack-key"),
    )

    assert hidden.status_code == missing.status_code == 404
    assert hidden.json()["error"]["code"] == missing.json()["error"]["code"]
    assert set(hidden.json()["error"]) == set(missing.json()["error"])


def test_stale_if_match_returns_current_revision_without_mutation(tmp_path: Path) -> None:
    client, store, csrf_token = _client(tmp_path)
    store.add_pack(_pack(), _files())

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "stale activation attempt"},
        headers=_headers(csrf_token, if_match=2, key="stale-activate-key"),
    )

    assert response.status_code == 412
    assert response.json()["error"]["code"] == "STALE_RULEPACK_REVISION"
    assert response.json()["error"]["details"] == {"current_revision": 1}
    assert store.active_pack_id(TENANT_A, "disclosure") is None


def test_idempotency_replays_after_restart_and_changed_body_conflicts(tmp_path: Path) -> None:
    client, store, csrf_token = _client(tmp_path)
    pack = _pack()
    store.add_pack(pack, _files())
    body = {"reason": "activate once only"}
    headers = _headers(csrf_token, key="durable-replay-key")

    first = client.post(f"/v1/rule-packs/{PACK_A}/activate", json=body, headers=headers)
    replay = client.post(f"/v1/rule-packs/{PACK_A}/activate", json=body, headers=headers)
    changed = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "different activation reason"},
        headers=headers,
    )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"2"'
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    reopened = RulePackSqliteStore(store.path)
    durable_replay = reopened.activate(
        tenant_id=TENANT_A,
        rule_pack_id=PACK_A,
        expected_revision=1,
        idempotency_key="durable-replay-key",
        actor="admin-user",
        reason=body["reason"],
        gap_ids=GAP_IDS,
        now=time.time(),
    )
    assert durable_replay.body == first.json()
    assert durable_replay.revision == 2
    assert reopened.active_pack_id(TENANT_A, "disclosure") == PACK_A
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM rulepack_revisions").fetchone()[0] == 2
        assert (
            connection.execute("SELECT count(*) FROM rulepack_idempotency_records").fetchone()[0]
            == 1
        )


@pytest.mark.parametrize(
    "pack",
    [
        _pack(status="draft"),
        _pack(approved_by=None),
    ],
    ids=["draft", "missing-recorded-approver"],
)
def test_repository_draft_or_unapproved_pack_cannot_activate(
    tmp_path: Path, pack: RulePackRecord
) -> None:
    client, store, csrf_token = _client(tmp_path)
    store.add_pack(pack, _files())

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "must not invent approval"},
        headers=_headers(csrf_token, key=f"blocked-{pack.status}-{pack.approved_by}"),
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RULEPACK_ACTIVATION_BLOCKED"
    assert store.active_pack_id(TENANT_A, "disclosure") is None


def test_empty_local_store_does_not_seed_or_activate_repository_draft(tmp_path: Path) -> None:
    client, store, csrf_token = _client(tmp_path)

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "repository draft is not runtime approval"},
        headers=_headers(csrf_token, key="no-repository-seed-key"),
    )

    assert response.status_code == 404
    assert store.active_pack_id(TENANT_A, "disclosure") is None


def test_missing_if_match_uses_contract_error_shape(tmp_path: Path) -> None:
    client, store, csrf_token = _client(tmp_path)
    store.add_pack(_pack(), _files())
    headers = _headers(csrf_token)
    del headers["If-Match"]

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "missing concurrency token"},
        headers=headers,
    )

    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert set(response.json()["error"]) == {"code", "message", "request_id", "retryable"}


@pytest.mark.parametrize("key", [None, "too-short"])
def test_missing_or_short_idempotency_key_uses_contract_error_shape(
    tmp_path: Path, key: str | None
) -> None:
    client, store, csrf_token = _client(tmp_path)
    store.add_pack(_pack(), _files())
    headers = _headers(csrf_token)
    if key is None:
        del headers["Idempotency-Key"]
    else:
        headers["Idempotency-Key"] = key

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json={"reason": "invalid idempotency key"},
        headers=headers,
    )

    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "payload",
    [
        {"reason": "no"},
        {"reason": "valid reason", "approved_by": "other-operator"},
    ],
    ids=["reason-too-short", "approval-injection"],
)
def test_invalid_action_reason_uses_contract_error_shape(
    tmp_path: Path, payload: dict[str, str]
) -> None:
    client, store, csrf_token = _client(tmp_path)
    store.add_pack(_pack(), _files())

    response = client.post(
        f"/v1/rule-packs/{PACK_A}/activate",
        json=payload,
        headers=_headers(csrf_token),
    )

    assert response.status_code == 422
    assert set(response.json()) == {"error"}
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_activation_audit_is_atomic_and_idempotent_replay_adds_no_event(tmp_path: Path) -> None:
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    store = RulePackSqliteStore(tmp_path / "state.sqlite3", id_factory=lambda: "activation-1")
    store.add_pack(_pack(), _files())
    arguments = {
        "tenant_id": TENANT_A,
        "rule_pack_id": PACK_A,
        "expected_revision": 1,
        "idempotency_key": "audit-replay-key",
        "actor": "admin-user",
        "reason": "record approved activation",
        "gap_ids": GAP_IDS,
        "now": 1_000.0,
    }

    store.activate(**arguments)
    store.activate(**arguments)

    with sqlite3.connect(store.path) as connection:
        events = connection.execute(
            """SELECT activation_id, actor_sub, reason, before_pack_id,
                      after_pack_id, activated_at
               FROM rulepack_activation_events"""
        ).fetchall()
    assert events == [
        (
            "activation-1",
            "admin-user",
            "record approved activation",
            None,
            PACK_A,
            "1970-01-01T00:16:40Z",
        )
    ]


def test_idempotency_key_expires_after_24_hours(tmp_path: Path) -> None:
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    activation_ids = iter(("activation-1", "activation-2"))
    store = RulePackSqliteStore(tmp_path / "state.sqlite3", id_factory=lambda: next(activation_ids))
    store.add_pack(_pack(), _files())
    store.activate(
        tenant_id=TENANT_A,
        rule_pack_id=PACK_A,
        expected_revision=1,
        idempotency_key="expiring-idempotency-key",
        actor="admin-user",
        reason="first activation reason",
        gap_ids=GAP_IDS,
        now=1_000.0,
    )

    after_expiry = store.activate(
        tenant_id=TENANT_A,
        rule_pack_id=PACK_A,
        expected_revision=2,
        idempotency_key="expiring-idempotency-key",
        actor="admin-user",
        reason="new intent after expiry",
        gap_ids=GAP_IDS,
        now=87_401.0,
    )

    assert after_expiry.revision == 2
    with sqlite3.connect(store.path) as connection:
        event_count = connection.execute(
            "SELECT count(*) FROM rulepack_activation_events"
        ).fetchone()[0]
        expiry = connection.execute(
            "SELECT expires_at FROM rulepack_idempotency_records"
        ).fetchone()[0]
    assert event_count == 2
    assert expiry == 173_801.0


def test_rulepack_store_coexists_without_claiming_global_schema_version(tmp_path: Path) -> None:
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 7")
    connection.execute("CREATE TABLE registry_marker (id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    RulePackSqliteStore(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 7
        assert (
            connection.execute(
                "SELECT version FROM rulepack_schema_metadata WHERE component='rulepacks'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='registry_marker'"
            ).fetchone()[0]
            == "registry_marker"
        )


def test_rulepack_store_fails_closed_on_unknown_component_schema(tmp_path: Path) -> None:
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    path = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE rulepack_schema_metadata (component TEXT PRIMARY KEY, version INTEGER)"
    )
    connection.execute("INSERT INTO rulepack_schema_metadata VALUES ('rulepacks', 2)")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="unsupported local rulepack schema version: 2"):
        RulePackSqliteStore(path)


def test_rulepack_and_run_identities_are_immutable(tmp_path: Path) -> None:
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    store = RulePackSqliteStore(tmp_path / "state.sqlite3")
    pack = _pack()
    store.add_pack(pack, _files())
    with pytest.raises(ValueError, match="immutable rule-pack identity"):
        store.add_pack(replace(pack, approved_by="another-approver"), _files())

    snapshot = RunSnapshot(RUN_A, TENANT_A, PACK_A, pack.sha256, "running")
    store.add_run_snapshot(snapshot)
    with pytest.raises(ValueError, match="immutable run snapshot"):
        store.add_run_snapshot(replace(snapshot, status="completed"))


def test_active_pack_reader_uses_durable_pointer_after_reopen(tmp_path: Path) -> None:
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    path = tmp_path / "state.sqlite3"
    store = RulePackSqliteStore(path)
    store.add_pack(_pack(), _files())
    store.activate(
        tenant_id=TENANT_A,
        rule_pack_id=PACK_A,
        expected_revision=1,
        idempotency_key="active-reader-key",
        actor="admin-user",
        reason="activate durable reader",
        gap_ids=GAP_IDS,
        now=1_000.0,
    )

    active = RulePackSqliteStore(path).list_active_packs(TENANT_A)

    assert len(active) == 1
    assert active[0].rule_pack_id == PACK_A
    assert active[0].status == "active"


def test_ai_delegated_review_store_path_makes_pack_eligible_for_real_run_active_snapshot(
    tmp_path: Path,
) -> None:
    """AT-R07 positive, real (non-synthetic) store path: an unapproved pack
    that only went through `record_ai_delegated_review` becomes readable via
    `active_snapshot_transaction`, the exact call `run_store.create()` uses to
    decide `rulepack_use="approved_grading"` for a real (non-candidate) run.
    """
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    path = tmp_path / "state.sqlite3"
    store = RulePackSqliteStore(path)
    store.add_pack(replace(_pack(), approved_by=None, approved_at=None), _files())

    stored = store.record_ai_delegated_review(
        tenant_id=TENANT_A,
        rule_pack_id=PACK_A,
        expected_revision=1,
        idempotency_key="ai-review-key-1",
        reviewer="review-operator",
        reviewed_at="2026-09-20T10:00:00Z",
        source_authority="user delegation 2026-09-20",
        note="R07 AI-delegated project review of explicit ladder",
        gap_ids=GAP_IDS,
        now=1_000.0,
    )
    assert stored.body["status"] == "active"

    reopened = RulePackSqliteStore(path)
    assert reopened.list_active_packs(TENANT_A)[0].rule_pack_id == PACK_A

    with reopened._transaction() as connection:
        snapshot = reopened.active_snapshot_transaction(connection, TENANT_A, "disclosure", PACK_A)
    assert snapshot.approved_by == "ai-delegated-review:review-operator"
    assert snapshot.status == "active"


def test_ai_delegated_review_store_path_rejects_stale_revision_and_idempotency_replay_conflict(
    tmp_path: Path,
) -> None:
    """Negative: a stale expected_revision is rejected before any write, and
    reusing the same idempotency key with a different request is rejected --
    matching the guarantees `activate()` already gives human activation."""
    from proofops.adapters.local.rulepack_store import (
        IdempotencyConflict,
        RulePackSqliteStore,
        StaleRulePackRevision,
    )

    store = RulePackSqliteStore(tmp_path / "state.sqlite3")
    store.add_pack(replace(_pack(), approved_by=None, approved_at=None), _files())

    with pytest.raises(StaleRulePackRevision):
        store.record_ai_delegated_review(
            tenant_id=TENANT_A,
            rule_pack_id=PACK_A,
            expected_revision=2,
            idempotency_key="ai-review-key-stale",
            reviewer="review-operator",
            reviewed_at="2026-09-20T10:00:00Z",
            source_authority="user delegation 2026-09-20",
            note="stale revision attempt",
            gap_ids=GAP_IDS,
            now=1_000.0,
        )

    store.record_ai_delegated_review(
        tenant_id=TENANT_A,
        rule_pack_id=PACK_A,
        expected_revision=1,
        idempotency_key="ai-review-key-2",
        reviewer="review-operator",
        reviewed_at="2026-09-20T10:00:00Z",
        source_authority="user delegation 2026-09-20",
        note="first grant",
        gap_ids=GAP_IDS,
        now=1_000.0,
    )
    with pytest.raises(IdempotencyConflict):
        store.record_ai_delegated_review(
            tenant_id=TENANT_A,
            rule_pack_id=PACK_A,
            expected_revision=2,
            idempotency_key="ai-review-key-2",
            reviewer="other-reviewer",
            reviewed_at="2026-09-20T11:00:00Z",
            source_authority="user delegation 2026-09-20",
            note="conflicting replay",
            gap_ids=GAP_IDS,
            now=1_500.0,
        )


def test_review_rulepack_cli_promotes_draft_and_real_evaluate_decides_with_local_synthetic_false(
    tmp_path: Path,
) -> None:
    """AT-R07 end-to-end proof, exercised through the CLI's own `main()`:

    a draft pack that never had a human `approved_by` is promoted to a new
    validated pack id and activated via AI-delegated review, and the
    resulting active pack -- read through the same `active_snapshot_transaction`
    call `run_store.create()` uses -- lets `domain.rules.engine.evaluate()`
    reach `decision_status="decided"` with `RuleContext(local_synthetic=False)`.
    This proves the real (non-synthetic-flag) gate is open, not merely that
    an `active`/`approved_by` field is truthy; the tags below are an
    explicitly-labelled synthetic fixture, not a claim about real-document
    accuracy or about any GAP being resolved.
    """
    import runpy

    import yaml
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore
    from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags, RuleContext, evaluate
    from proofops.domain.values import SourceRef

    root = Path(__file__).resolve().parents[2]
    config_dir = root / "config"
    manifest = yaml.safe_load((config_dir / "rule_pack_manifest.yaml").read_text())
    files = {path: yaml.safe_load((config_dir / path).read_text()) for path in manifest["files"]}
    manifest.update(
        rule_pack_id=PACK_A, tenant_id=TENANT_A, status="draft", approved_by=None, approved_at=None
    )
    manifest["sha256"] = compute_pack_sha256(manifest, files)

    db_path = tmp_path / "state.sqlite3"
    RulePackSqliteStore(db_path).add_pack(RulePackRecord.from_dict(manifest), files)

    module = runpy.run_path(str(root / "scripts" / "review_rulepack.py"))
    exit_code = module["main"](
        [
            "--state-db",
            str(db_path),
            "--tenant-id",
            TENANT_A,
            "--rule-pack-id",
            PACK_A,
            "--reviewer",
            "review-operator",
            "--reviewed-at",
            "2026-09-20T10:00:00Z",
            "--source-authority",
            "user delegation 2026-09-20",
            "--note",
            "AT-R07 end-to-end CLI proof",
            "--apply",
            "--now",
            "1000.0",
        ]
    )
    assert exit_code == 0

    store = RulePackSqliteStore(db_path)
    active = store.list_active_packs(TENANT_A)
    assert len(active) == 1
    new_pack_id = active[0].rule_pack_id
    assert new_pack_id != PACK_A  # promoted to a NEW id, draft row untouched
    assert active[0].approved_by == "ai-delegated-review:review-operator"

    with store._transaction() as connection:
        snapshot = store.active_snapshot_transaction(
            connection, TENANT_A, "disclosure", new_pack_id
        )

    source = SourceRef(
        RUN_A,
        RUN_A,
        RUN_A,
        1,
        None,
        (1, 1, 10, 10),
        "a" * 64,
        "SYNTHETIC-FIXTURE-QUOTE (test fixture, not real document text)",
        0,
        18,
        "located",
        "verified",
    )

    def fixture_fact(name: str, state: str = "present") -> ConfirmedFact:
        return ConfirmedFact(
            name=name,
            state=state,
            evidence_refs=(source,) if state == "present" else (),
            source_tenant_id=TENANT_A,
            citation_verified=state == "present",
            binding_accepted=state == "present",
            search_coverage_verified=state == "absent",
            source_scope="local_claim",
            normalized_value=None,
        )

    present = (
        "target_year",
        "target_metric",
        "baseline_year",
        "baseline_value",
        "scope",
        "org_boundary",
        "current_progress",
        "transition_plan",
    )
    absent = ("offset_or_carbon_neutral_claim", "science_based_claim")
    tags = ConfirmedTags(
        tenant_id=TENANT_A,
        document_version_id=RUN_A,
        claim_id=RUN_A,
        track="goal",
        facts=(
            tuple(fixture_fact(n) for n in present)
            + tuple(fixture_fact(n, "absent") for n in absent)
        ),
        tag_revision=1,
        packet_sha256="b" * 64,
        model_sha256="c" * 64,
        prompt_sha256="d" * 64,
        replicate_hashes=("1" * 64, "2" * 64, "3" * 64),
        ontology_version=snapshot.ontology_version,
    )
    context = RuleContext(
        tenant_id=TENANT_A,
        document_version_id=RUN_A,
        claim_id=RUN_A,
        packet_sha256="b" * 64,
        local_synthetic=False,  # the real (non-synthetic) evaluation path
    )

    decision = evaluate(tags, context, snapshot)

    assert context.local_synthetic is False
    assert decision.decision_status == "decided"
    assert decision.evidence_grade == "E3"
    assert decision.label == "SUBSTANTIATED"
    assert decision.rule_pack_sha256 == snapshot.sha256
    # This claim triggers none of the GAP-003 conditional elements
    # (G7/G8), so no unresolved GAP blocks this particular decision --
    # that is not a claim that any GAP is resolved project-wide.
    assert decision.gap_ids == ()


def test_promote_and_review_pack_helper_gives_new_run_active_pack_opt_in(
    tmp_path: Path,
) -> None:
    """The composition helper other callers (e.g. a pilot's new-run path)
    can call instead of `store.add_pack(draft, files)` to opt a single run
    into AI-delegated review: it returns a new (validated, active) pack id,
    never mutates the original draft record, and the result is readable via
    `active_snapshot_transaction` -- the same call `run_store.create()` uses
    to decide `rulepack_use`."""
    import runpy

    import yaml
    from proofops.adapters.local.rulepack_store import RulePackSqliteStore

    root = Path(__file__).resolve().parents[2]
    module = runpy.run_path(str(root / "scripts" / "review_rulepack.py"))
    promote_and_review_pack = module["promote_and_review_pack"]

    config_dir = root / "config"
    manifest = yaml.safe_load((config_dir / "rule_pack_manifest.yaml").read_text())
    files = {path: yaml.safe_load((config_dir / path).read_text()) for path in manifest["files"]}
    draft_pack_id = PACK_A
    manifest.update(
        rule_pack_id=draft_pack_id,
        tenant_id=TENANT_A,
        status="draft",
        approved_by=None,
        approved_at=None,
    )
    manifest["sha256"] = compute_pack_sha256(manifest, files)
    draft_record = RulePackRecord.from_dict(manifest)

    store = RulePackSqliteStore(tmp_path / "state.sqlite3")
    new_pack_id = promote_and_review_pack(
        store,
        tenant_id=TENANT_A,
        draft_pack=draft_record,
        files=files,
        reviewer="review-operator",
        reviewed_at="2026-09-20T10:00:00Z",
        source_authority="user delegation 2026-09-20",
        note="pilot new-run opt-in",
        now=1_000.0,
    )

    assert new_pack_id != draft_pack_id
    original, _ = store.get_pack_with_files(TENANT_A, draft_pack_id)
    assert original.status == "draft"  # the original draft row is untouched
    assert original.approved_by is None

    active = store.list_active_packs(TENANT_A)
    assert len(active) == 1
    assert active[0].rule_pack_id == new_pack_id
    assert active[0].approved_by == "ai-delegated-review:review-operator"

    with store._transaction() as connection:
        snapshot = store.active_snapshot_transaction(
            connection, TENANT_A, "disclosure", new_pack_id
        )
    assert snapshot.status == "active"


def test_generated_openapi_matches_activation_contract(tmp_path: Path) -> None:
    client, _store, _csrf_token = _client(tmp_path)
    operation = client.app.openapi()["paths"]["/v1/rule-packs/{rule_pack_id}/activate"]["post"]

    assert operation["operationId"] == "rulepack_activate"
    assert operation["security"] == [{"sessionCookie": []}]
    assert operation["x-minimum-role"] == "admin"
    assert operation["x-idempotency-required"] is True
    assert {
        (parameter["name"], parameter["in"], parameter["required"])
        for parameter in operation["parameters"]
    } == {
        ("rule_pack_id", "path", True),
        ("X-CSRF-Token", "header", True),
        ("Idempotency-Key", "header", True),
        ("If-Match", "header", True),
    }
    assert set(operation["responses"]) == {
        "200",
        "400",
        "401",
        "403",
        "404",
        "409",
        "412",
        "422",
        "429",
        "503",
    }
    response_ref = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert response_ref == {"$ref": "#/components/schemas/RulePack"}


def test_composed_local_app_exposes_rulepack_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("MODEL_ADAPTER", "synthetic")
    monkeypatch.setenv("APP_ORIGIN", "http://testserver")
    monkeypatch.setenv("LOCAL_DATABASE_PATH", str(tmp_path / "composed.sqlite3"))

    from proofops_api.main import create_app

    app = create_app()
    composition = app.state.composition
    csrf_token = _seed_auth(composition.auth_store)
    composition.rulepack_store.add_pack(_pack(), _files())

    with TestClient(app) as client:
        client.cookies.set("__Host-proofops_session", "admin-session")
        response = client.post(
            f"/v1/rule-packs/{PACK_A}/activate",
            json={"reason": "activate through composition"},
            headers=_headers(csrf_token, key="composed-activation-key"),
        )

    assert response.status_code == 200
    assert response.json()["status"] == "active"
