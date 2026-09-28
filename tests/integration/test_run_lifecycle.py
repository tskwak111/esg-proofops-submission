"""Local HTTP lifecycle using real SQLite and uploaded generated PDFs; approvals synthetic."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.adapters.local.rulepack_store import RulePackSqliteStore
from proofops.application.authorization import MembershipRecord
from proofops.application.budget import BudgetLimits, RoleLimit
from proofops.application.ingest.graph_fusion import ParserProfile
from proofops.application.registry import artifact_sha256
from proofops.application.supply_chain import SupplyChainResult
from proofops_api.auth import AuthStore

from tests.acceptance.test_parsing import JAVA
from tests.acceptance.test_preflight import binding, consent
from tests.acceptance.test_rulepack_api import GAP_IDS, _files, _pack, _seed_auth
from tests.acceptance.test_upload import RIGHTS, TENANT, pdf, setup_upload

NOW = 1789000000
FOREIGN = "22222222-2222-4222-8222-222222222222"


def setup(tmp_path):
    try:
        from proofops.adapters.local.run_store import LocalSQLiteRunStore
        from proofops.application.runs import RunService
    except ImportError as exc:
        pytest.fail(f"Run lifecycle missing: {exc}")
    uploads, doc, data, body = setup_upload(tmp_path, data=pdf(3))
    ticket = uploads.initiate_upload(TENANT, doc["document_id"], body, str(uuid4()))
    uploads.receive_content(TENANT, ticket["upload_id"], data, "application/pdf")
    version = uploads.complete_upload(
        TENANT,
        ticket["upload_id"],
        {"sha256": body["sha256"], "size_bytes": len(data)},
        str(uuid4()),
    )
    registry = uploads.registry
    for kind, artifact, id_field in [
        ("runtime", binding(), "runtime_binding_id"),
        ("consent", consent(), "consent_profile_id"),
    ]:
        if kind == "consent":
            artifact["allowed_document_rights"] = [RIGHTS]
        registry.with_option(
            TENANT,
            kind,
            artifact[id_field],
            "Synthetic fixture only",
            status="approved",
            version=artifact["version"],
            artifact=artifact,
            sha256=artifact_sha256(artifact),
            approved_by="synthetic-fixture",
            approved_at=artifact["approved_at"],
            local_synthetic=True,
        )
    path = tmp_path / "runs.sqlite"
    packs = RulePackSqliteStore(path)
    pack = _pack()
    packs.add_pack(pack, _files())
    packs.activate(
        tenant_id=TENANT,
        rule_pack_id=pack.rule_pack_id,
        expected_revision=1,
        idempotency_key=str(uuid4()),
        actor="synthetic-fixture",
        reason="Synthetic test approval",
        gap_ids=GAP_IDS,
        now=NOW,
    )
    limits = BudgetLimits(10000, 5000, (RoleLimit("tagger", 3, 5000, 1000, 10000),))
    store = LocalSQLiteRunStore(path, rulepacks=packs)
    parser = ParserProfile(str(uuid4()), java_executable=JAVA)
    service = RunService(
        store,
        uploads,
        registry,
        parser_profile_hash=parser.config_hash(),
        parser_profile=parser.config_snapshot(),
        budget_limits=limits,
        allowed_regions=("ap-northeast-2",),
        build_result=SupplyChainResult(True),
        clock=lambda: NOW,
    )
    request = dict(
        document_version_id=version["version_id"],
        mode="disclosure",
        scope="full",
        rule_pack_id=pack.rule_pack_id,
        consent_profile_id=consent()["consent_profile_id"],
        runtime_binding_id=binding()["runtime_binding_id"],
    )
    return service, request


def client(service, role="admin"):
    from proofops_api.routers.runs import build_runs_router

    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    token = _seed_auth(auth, role=role)
    app = FastAPI()
    app.include_router(build_runs_router(service, auth, allowed_origin="https://testserver"))
    http = TestClient(app, base_url="https://testserver")
    http.cookies.set("__Host-proofops_session", "admin-session")
    http.headers.update(
        {"X-CSRF-Token": token, "Origin": "https://testserver", "Idempotency-Key": str(uuid4())}
    )
    return http, auth


def validate(name, payload):
    schema = json.loads(
        (Path(__file__).parents[2] / "contracts/jsonschema/api_models.schema.json").read_text()
    )
    Draft202012Validator(
        {"$ref": f"#/$defs/{name}", "$defs": schema["$defs"]}, format_checker=FormatChecker()
    ).validate(payload)


def test_optional_tagging_settings_are_frozen_and_match_approved_runtime(tmp_path):
    from dataclasses import asdict

    from proofops.application.claims import ExtractionProfile
    from proofops.application.ports.models import ModelBinding
    from proofops.application.tagging.service import TaggingSettings
    from proofops.domain.provenance import canonical_hash

    service, body = setup(tmp_path)
    runtime = binding()
    service.extraction_profile = ExtractionProfile("1" * 64, "2" * 64, "3" * 64, True)
    service.extraction_mode = "local_synthetic"
    settings = TaggingSettings(
        ModelBinding(runtime["runtime_binding_id"], "tagger", True),
        runtime["model_id"],
        "synthetic-profile",
        runtime["endpoint_region"],
        "Synthetic local tags only",
        "{}",
        max_tokens=100,
    )
    service.tagging_settings, service.tagging_mode = settings, "local_synthetic"
    http, _ = client(service)
    first = http.post("/v1/runs", json=body)
    assert first.status_code == 202
    snapshot = service.store.snapshot(TENANT, first.json()["run_id"])
    assert snapshot["tagging_settings"] == asdict(settings)
    assert snapshot["tagging_settings_hash"] == canonical_hash(asdict(settings))
    assert snapshot["tagging_mode"] == "local_synthetic"
    for changed in (
        replace(settings, model_id="different-model"),
        replace(settings, binding=ModelBinding(str(uuid4()), "tagger", True)),
        replace(settings, binding=replace(settings.binding, synthetic=False)),
        replace(settings, region="different-region"),
        replace(settings, max_tokens=runtime["max_output_tokens"] + 1),
    ):
        service.tagging_settings = changed
        rejected = http.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())})
        assert rejected.status_code == 409
        assert rejected.json()["error"]["code"] == "CONFIG_GATE_BLOCKED"
    assert http.post("/v1/runs", json=body).json() == first.json()
    assert service.store.snapshot(TENANT, first.json()["run_id"]) == snapshot


def test_optional_extraction_profile_and_grading_pack_are_independently_frozen(tmp_path):
    from dataclasses import asdict

    from proofops.application.claims import ExtractionProfile
    from proofops.domain.provenance import canonical_hash

    service, body = setup(tmp_path)
    profile = ExtractionProfile("1" * 64, "2" * 64, "3" * 64, True)
    service.extraction_profile = profile
    service.extraction_mode = "local_synthetic"
    http, _ = client(service)
    first = http.post("/v1/runs", json=body)
    assert first.status_code == 202
    snapshot = service.store.snapshot(TENANT, first.json()["run_id"])
    assert snapshot["extraction_profile"] == asdict(profile)
    assert snapshot["extraction_profile_hash"] == canonical_hash(asdict(profile))
    assert snapshot["extraction_mode"] == "local_synthetic"
    assert snapshot["rulepack"]["sha256"] == _pack().sha256
    service.extraction_profile = replace(profile, rule_sha256="0" * 64)
    assert http.post("/v1/runs", json=body).json() == first.json()
    next_run = http.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())})
    assert next_run.status_code == 202
    assert (
        service.store.snapshot(TENANT, next_run.json()["run_id"])["extraction_profile"][
            "rule_sha256"
        ]
        == "0" * 64
    )
    assert service.store.snapshot(TENANT, first.json()["run_id"]) == snapshot
    service.extraction_profile = replace(profile, synthetic=False)
    assert (
        http.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())}).status_code
        == 409
    )


def test_http_create_reopen_job_budget_snapshots_and_exact_replay(tmp_path):
    service, body = setup(tmp_path)
    http, _ = client(service)
    result = http.post("/v1/runs", json=body)
    assert result.status_code == 202, result.text
    run = result.json()
    validate("Run", run)
    assert run["status"] == "queued" and run["coverage"]["pages_total"] == 3
    assert not run["coverage"]["complete"] and run["coverage"]["pages_processed"] == 0
    assert http.post("/v1/runs", json=body).content == result.content
    from proofops.adapters.local.run_store import LocalSQLiteRunStore

    reopened = LocalSQLiteRunStore(service.store.path)
    assert reopened.get(TENANT, run["run_id"]) == run
    snapshot = reopened.snapshot(TENANT, run["run_id"])
    assert snapshot["document"]["object_version_id"]
    assert (
        snapshot["document"]["sha256"]
        == service.uploads.version_snapshot(TENANT, body["document_version_id"])["sha256"]
    )
    assert snapshot["rulepack"]["sha256"] == run["rule_pack_sha256"]
    assert snapshot["parser_profile_hash"] == service.parser_profile_hash
    assert snapshot["parser_profile"] == dict(service.parser_profile)
    events = reopened.jobs.pending_outbox(TENANT, run["run_id"], now=NOW)
    assert len(events) == 1 and events[0]["message"]["stage"] == "parse"
    assert events[0]["message"]["input_hash"] == snapshot["input_hash"]
    cost = http.get(f"/v1/runs/{run['run_id']}/cost")
    validate("Cost", cost.json())
    assert cost.json()["cost_status"] == "unknown_cost" and cost.json()["amount"] is None
    audit = http.get(f"/v1/runs/{run['run_id']}/audit").json()
    validate("AuditEventPage", audit)
    assert len(audit["items"]) == 1 and audit["items"][0]["action"] == "run.create"
    assert http.get(f"/v1/runs/{run['run_id']}").json() == run


@pytest.mark.parametrize(
    "changes",
    [
        {"selected_pages": [1]},
        {"scope": "declared_subset"},
        {"scope": "declared_subset", "selected_pages": []},
        {"scope": "declared_subset", "selected_pages": [2, 1]},
        {"scope": "declared_subset", "selected_pages": [1, 1]},
        {"scope": "declared_subset", "selected_pages": [0]},
        {"scope": "declared_subset", "selected_pages": [4]},
        {"scope": "declared_subset", "selected_pages": [True]},
        {"parser_profile_hash": "b" * 64},
        {"budget_limits": 999},
        {"tenant_id": FOREIGN},
    ],
)
def test_scope_and_untrusted_configuration_rejected(tmp_path, changes):
    service, body = setup(tmp_path)
    http, _ = client(service)
    assert http.post("/v1/runs", json=body | changes).status_code == 422
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT count(*) FROM run_snapshots").fetchone()[0] == 0


def test_http_subset_cancel_replay_stale_and_tenant_roles(tmp_path):
    service, body = setup(tmp_path)
    http, auth = client(service)
    created = http.post(
        "/v1/runs", json=body | {"scope": "declared_subset", "selected_pages": [1, 3]}
    ).json()
    run_id = created["run_id"]
    assert created["coverage"]["full_scope"] is False
    url = f"/v1/runs/{run_id}"
    assert service.store.snapshot(TENANT, run_id)["selected_pages"] == [1, 3]
    headers = {"If-Match": '"1"', "Idempotency-Key": str(uuid4())}
    cancelled = http.post(url + "/cancel", json={"reason": "Stop this run"}, headers=headers)
    assert cancelled.status_code == 202, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    assert (
        http.post(url + "/cancel", json={"reason": "Stop this run"}, headers=headers).content
        == cancelled.content
    )
    assert (
        http.post(
            url + "/cancel",
            json={"reason": "Stop this run"},
            headers=headers | {"Idempotency-Key": str(uuid4())},
        ).status_code
        == 412
    )
    assert len(http.get(url + "/audit").json()["items"]) == 2
    viewer, _ = client(service, "viewer")
    assert viewer.get(url).status_code == 200
    assert (
        viewer.post(url + "/cancel", json={"reason": "Stop this run"}, headers=headers).status_code
        == 403
    )
    reviewer, _ = client(service, "reviewer")
    assert reviewer.post("/v1/runs", json=body).status_code == 403
    assert (
        http.post(
            url + "/cancel",
            json={"reason": "Stop this run"},
            headers=headers | {"Origin": "https://foreign.test"},
        ).status_code
        == 403
    )
    session = auth.sessions.get("admin-session")
    auth.sessions.put(replace(session, active_tenant_id=FOREIGN))
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "admin", "active"))
    for suffix in ["", "/cost", "/audit"]:
        assert http.get(url + suffix).status_code == 404
        assert http.get("/v1/runs/" + str(uuid4()) + suffix).status_code == 404


def test_create_and_action_audit_failure_roll_back(tmp_path):
    service, body = setup(tmp_path)
    http, _ = client(service)
    with sqlite3.connect(service.store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_run_audit BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    assert http.post("/v1/runs", json=body).status_code == 409
    with sqlite3.connect(service.store.path) as db:
        for table in [
            "run_snapshots",
            "job_records",
            "usage_runs",
            "run_idempotency",
            "rulepack_run_snapshots",
        ]:
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        db.execute("DROP TRIGGER fail_run_audit")
    created = http.post("/v1/runs", json=body).json()
    with sqlite3.connect(service.store.path) as db:
        db.execute(
            "CREATE TRIGGER fail_run_audit BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'injected'); END"
        )
    assert (
        http.post(
            f"/v1/runs/{created['run_id']}/cancel",
            json={"reason": "Cancel requested"},
            headers={"If-Match": '"1"'},
        ).status_code
        == 409
    )
    assert http.get(f"/v1/runs/{created['run_id']}").json() == created


def test_separate_instance_create_race_and_retry_race(tmp_path):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.ports.jobs import JobMessage
    from proofops.application.runs import RunService

    service, body = setup(tmp_path)
    other = RunService(
        LocalSQLiteRunStore(service.store.path),
        service.uploads,
        service.registry,
        parser_profile_hash=service.parser_profile_hash,
        parser_profile=service.parser_profile,
        budget_limits=service.budget_limits,
        allowed_regions=service.allowed_regions,
        build_result=service.build_result,
        clock=lambda: NOW,
    )
    first, _ = client(service)
    second, _ = client(other)
    key = str(uuid4())
    with ThreadPoolExecutor(2) as pool:
        responses = list(
            pool.map(
                lambda c: c.post("/v1/runs", json=body, headers={"Idempotency-Key": key}),
                [first, second],
            )
        )
    assert [r.status_code for r in responses] == [202, 202]
    assert responses[0].content == responses[1].content
    run_id = responses[0].json()["run_id"]
    message = JobMessage(**service.store.jobs.pending_outbox(TENANT, run_id, now=NOW)[0]["message"])
    lease = service.store.jobs.claim_job(message, owner="test-worker", now=NOW, lease_seconds=60)
    service.store.jobs.fail_job(lease, error_code="PARSE_FAILED", now=NOW)
    failed = first.get(f"/v1/runs/{run_id}").json()
    assert failed["status"] == "failed"
    headers = {"If-Match": f'"{failed["revision"]}"', "Idempotency-Key": str(uuid4())}
    url = f"/v1/runs/{run_id}/retry"
    with ThreadPoolExecutor(2) as pool:
        retries = list(
            pool.map(
                lambda c: c.post(url, json={"reason": "Retry parser failure"}, headers=headers),
                [first, second],
            )
        )
    assert [r.status_code for r in retries] == [202, 202]
    assert retries[0].content == retries[1].content
    assert retries[0].json()["status"] == "running"
    assert len(first.get(f"/v1/runs/{run_id}/audit").json()["items"]) == 2


@pytest.mark.parametrize(
    "block",
    [
        "parser",
        "parser_snapshot",
        "budget",
        "build",
        "consent",
        "runtime",
        "region",
        "ads",
        "missing_pack",
        "unapproved_pack",
    ],
)
def test_blocked_create_has_no_model_calls_or_partial_records(tmp_path, monkeypatch, block):
    from proofops.adapters.aws.bedrock import BedrockInvoker
    from proofops.adapters.local.models import SyntheticTagger

    service, body = setup(tmp_path)
    calls = []

    def forbidden_call(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Run creation must never invoke a model")

    monkeypatch.setattr(BedrockInvoker, "invoke", forbidden_call)
    monkeypatch.setattr(SyntheticTagger, "tag", forbidden_call)
    if block == "parser":
        service.parser_profile_hash = None
    elif block == "parser_snapshot":
        service.parser_profile = None
    elif block == "budget":
        service.budget_limits = None
    elif block == "build":
        service.build_result = None
    elif block == "consent":
        body["consent_profile_id"] = str(uuid4())
    elif block == "runtime":
        body["runtime_binding_id"] = str(uuid4())
    elif block == "region":
        service.allowed_regions = ("us-east-1",)
    elif block == "ads":
        body["mode"] = "advertising"
    elif block == "missing_pack":
        body["rule_pack_id"] = str(uuid4())
    else:
        pack = _pack(pack_id=str(uuid4()), status="draft", approved_by=None)
        service.store.rulepacks.add_pack(pack, _files())
        body["rule_pack_id"] = pack.rule_pack_id
    http, _ = client(service)
    response = http.post("/v1/runs", json=body)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "CONFIG_GATE_BLOCKED"
    assert calls == []
    with sqlite3.connect(service.store.path) as db:
        for table in [
            "run_snapshots",
            "job_records",
            "usage_runs",
            "audit_events",
            "run_idempotency",
            "rulepack_run_snapshots",
        ]:
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_audit_cursor_pins_cutoff_epoch_and_is_bound_and_persistent(tmp_path):
    from proofops.adapters.local.audit_store import LocalSQLiteAuditStore
    from proofops.domain.audit import ChangeSet

    service, body = setup(tmp_path)
    http, _ = client(service)
    created = http.post("/v1/runs", json=body).json()
    run_id = created["run_id"]
    audit = LocalSQLiteAuditStore(service.store.path)

    def append(index):
        audit.append(
            ChangeSet(
                TENANT,
                run_id,
                "synthetic-worker",
                "test.checkpoint",
                run_id,
                None,
                "b" * 64,
                index,
                None,
            ),
            audit.get_head(TENANT, run_id),
            event_id=str(uuid4()),
            timestamp="2026-09-09T00:00:00Z",
        )

    append(2)
    url = f"/v1/runs/{run_id}/audit"
    first = http.get(url, params={"limit": 1}).json()
    assert first["next_cursor"] and first["snapshot_epoch"] == 0
    append(3)
    from proofops.adapters.local.run_store import LocalSQLiteRunStore

    service.store = LocalSQLiteRunStore(service.store.path)
    second = http.get(url, params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [e["sequence"] for e in second["items"]] == [2]
    assert second["next_cursor"] is None and second["snapshot_epoch"] == 0
    assert len(http.get(url).json()["items"]) == 3
    for token, limit in [(first["next_cursor"] + "!", 1), (first["next_cursor"], 2)]:
        assert http.get(url, params={"limit": limit, "cursor": token}).status_code == 400
    other = http.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())}).json()
    assert (
        http.get(
            f"/v1/runs/{other['run_id']}/audit", params={"limit": 1, "cursor": first["next_cursor"]}
        ).status_code
        == 400
    )
    service.clock = lambda: NOW + 900
    assert http.get(url, params={"limit": 1, "cursor": first["next_cursor"]}).status_code == 400
    with sqlite3.connect(service.store.path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute('UPDATE audit_events SET action="tamper"')
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM run_snapshots")


def test_create_replay_survives_new_configuration_and_24h_expiry(tmp_path):
    service, body = setup(tmp_path)
    http, _ = client(service)
    original = http.post("/v1/runs", json=body)
    run_id = original.json()["run_id"]
    snapshot = service.store.snapshot(TENANT, run_id)
    assert http.post("/v1/runs", json=body | {"mode": "advertising"}).status_code == 409
    service.parser_profile_hash = "b" * 64
    service.build_result = None
    assert http.post("/v1/runs", json=body).content == original.content
    assert service.store.snapshot(TENANT, run_id) == snapshot
    service.clock = lambda: NOW + 86400
    assert http.post("/v1/runs", json=body).status_code == 409
    service.build_result = SupplyChainResult(True)
    assert http.post("/v1/runs", json=body).status_code == 409
    replacement = ParserProfile(str(uuid4()), timeout_seconds=46)
    service.parser_profile = replacement.config_snapshot()
    service.parser_profile_hash = replacement.config_hash()
    updated = http.post("/v1/runs", json=body)
    assert updated.status_code == 202
    assert updated.json()["run_id"] != run_id
    assert (
        service.store.snapshot(TENANT, updated.json()["run_id"])["parser_profile_hash"]
        == replacement.config_hash()
    )


def test_foreign_missing_document_and_mutation_guards(tmp_path):
    service, body = setup(tmp_path)
    http, _ = client(service)
    assert (
        http.post("/v1/runs", json=body | {"document_version_id": str(uuid4())}).status_code == 404
    )
    for invalid in [{"selected_pages": None}, {"label": "SUBSTANTIATED"}]:
        assert http.post("/v1/runs", json=body | invalid).status_code == 422
    run = http.post("/v1/runs", json=body).json()
    for match in ["", "1", 'W/"1"', "*"]:
        assert (
            http.post(
                f"/v1/runs/{run['run_id']}/cancel",
                json={"reason": "Stop this run"},
                headers={"If-Match": match},
            ).status_code
            == 422
        )
    assert http.post("/v1/runs", json=body, headers={"X-CSRF-Token": "bad"}).status_code == 403
    assert http.post("/v1/runs", json=body, headers={"Idempotency-Key": "short"}).status_code == 400
    for action in ["cancel", "retry"]:
        assert (
            http.post(
                f"/v1/runs/{uuid4()}/{action}",
                json={"reason": "Missing run"},
                headers={"If-Match": '"1"'},
            ).status_code
            == 404
        )


def test_local_only_guards_and_same_database_requirement(tmp_path):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.runs import RunService
    from proofops_api.routers.runs import build_runs_router

    service, _ = setup(tmp_path)
    _, auth = client(service)
    for environment, adapter in [
        ("production", "synthetic"),
        ("staging", "synthetic"),
        ("local", "bedrock"),
    ]:
        with pytest.raises(ValueError):
            RunService(
                service.store,
                service.uploads,
                service.registry,
                app_env=environment,
                model_adapter=adapter,
            )
        with pytest.raises(ValueError):
            build_runs_router(
                service,
                auth,
                allowed_origin="https://testserver",
                app_env=environment,
                model_adapter=adapter,
            )
    with pytest.raises(ValueError):
        LocalSQLiteRunStore(tmp_path / "other.sqlite", rulepacks=service.store.rulepacks)


def test_http_cost_reuses_exact_decimal_ledger(tmp_path):
    from proofops.application.budget import BudgetCall

    from tests.acceptance.test_cost import PRICE, USAGE

    service, body = setup(tmp_path)
    http, _ = client(service)
    run_id = http.post("/v1/runs", json=body).json()["run_id"]
    call = BudgetCall(
        TENANT,
        run_id,
        body["document_version_id"],
        "synthetic-request",
        1,
        "tagger",
        PRICE.model_id,
        PRICE.region,
        "a" * 64,
        "b" * 64,
        1,
    )
    usage = service.store.usage
    assert usage.reserve_budget(
        call, input_tokens=100, max_output_tokens=100, pricing=PRICE, now=NOW
    )
    assert usage.mark_dispatched(call)
    usage.record_usage(call, USAGE, now=NOW)
    cost = http.get(f"/v1/runs/{run_id}/cost").json()
    assert cost["amount"] == "0.000131" and cost["cost_status"] == "known"
    assert cost["attempt_count"] == 1 and cost["input_tokens"] == 100
    assert cost["pricing_snapshot_id"] == PRICE.snapshot_id


def test_tenant_concurrent_run_limit_is_atomic(tmp_path):
    service, body = setup(tmp_path)
    clients = [client(service)[0] for _ in range(3)]
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda http: http.post("/v1/runs", json=body), clients))
    assert sorted(r.status_code for r in results) == [202, 202, 429]
    refused = next(r for r in results if r.status_code == 429)
    assert int(refused.headers["Retry-After"]) > 0
    with sqlite3.connect(service.store.path) as db:
        assert db.execute("SELECT count(*) FROM run_snapshots").fetchone()[0] == 2


def test_retry_cannot_bypass_tenant_concurrent_run_limit(tmp_path):
    from proofops.application.ports.jobs import JobMessage

    service, body = setup(tmp_path)
    http, _ = client(service)
    run_id = http.post("/v1/runs", json=body).json()["run_id"]
    message = JobMessage(**service.store.jobs.pending_outbox(TENANT, run_id, now=NOW)[0]["message"])
    lease = service.store.jobs.claim_job(message, owner="test-worker", now=NOW, lease_seconds=60)
    service.store.jobs.fail_job(lease, error_code="PARSE_FAILED", now=NOW)
    failed = http.get(f"/v1/runs/{run_id}").json()
    for _ in range(2):
        assert (
            http.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())}).status_code
            == 202
        )
    refused = http.post(
        f"/v1/runs/{run_id}/retry",
        json={"reason": "Retry when capacity is occupied"},
        headers={"If-Match": f'"{failed["revision"]}"', "Idempotency-Key": str(uuid4())},
    )
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "TENANT_RUN_LIMIT"
    assert http.get(f"/v1/runs/{run_id}").json() == failed
    assert len(http.get(f"/v1/runs/{run_id}/audit").json()["items"]) == 1


def test_composed_adapter_hooks_share_transaction_and_accept_duplicate_snapshot(tmp_path):
    from proofops.application.rulepacks import RunSnapshot

    service, body = setup(tmp_path)
    run_id = str(uuid4())
    pointer = RunSnapshot(run_id, TENANT, body["rule_pack_id"], _pack().sha256, "queued")
    with sqlite3.connect(service.store.path) as db:
        db.execute("BEGIN IMMEDIATE")
        service.store.rulepacks.add_run_snapshot_transaction(db, pointer)
        service.store.rulepacks.add_run_snapshot_transaction(db, pointer)
        service.store.usage.create_budget_transaction(
            db, TENANT, run_id, body["document_version_id"], service.budget_limits
        )
        service.store.usage.create_budget_transaction(
            db, TENANT, run_id, body["document_version_id"], service.budget_limits
        )
        db.rollback()
    with pytest.raises(LookupError):
        service.store.rulepacks.get_run_snapshot(TENANT, run_id)


def test_openapi_operation_ids_match_fixed_contract(tmp_path):
    service, _ = setup(tmp_path)
    http, _ = client(service)
    actual = http.app.openapi()["paths"]
    assert actual["/v1/runs/{run_id}/cost"]["get"]["operationId"] == "cost_get"
    assert actual["/v1/runs/{run_id}/audit"]["get"]["operationId"] == "audit_get"


def test_active_pack_change_only_affects_new_runs_and_audit_hashes_verify(tmp_path):
    from proofops.adapters.local.audit_store import LocalSQLiteAuditStore
    from proofops.application.rulepacks import RulePackRecord, compute_pack_sha256
    from proofops.domain.provenance import canonical_hash

    service, body = setup(tmp_path)
    http, _ = client(service)
    first = http.post("/v1/runs", json=body)
    run_id = first.json()["run_id"]
    frozen = service.store.snapshot(TENANT, run_id)
    audit = LocalSQLiteAuditStore(service.store.path).events(TENANT, run_id)
    assert audit[0].after_hash == canonical_hash(
        {"run": service.store.jobs.get_run(TENANT, run_id), "snapshot": frozen}
    )
    updated_files = _files()
    updated_files["regulatory/timeline.yaml"]["synthetic_test_revision"] = 2
    record = _pack(pack_id=str(uuid4())).to_dict()
    record["sha256"] = compute_pack_sha256(record, updated_files)
    pack = RulePackRecord.from_dict(record)
    service.store.rulepacks.add_pack(pack, updated_files)
    service.store.rulepacks.activate(
        tenant_id=TENANT,
        rule_pack_id=pack.rule_pack_id,
        expected_revision=1,
        idempotency_key=str(uuid4()),
        actor="synthetic-fixture",
        reason="Synthetic second pack approval",
        gap_ids=GAP_IDS,
        now=NOW,
    )
    assert http.post("/v1/runs", json=body).content == first.content
    assert (
        http.post("/v1/runs", json=body, headers={"Idempotency-Key": str(uuid4())}).status_code
        == 409
    )
    second = http.post(
        "/v1/runs",
        json=body | {"rule_pack_id": pack.rule_pack_id},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert second.status_code == 202
    assert second.json()["rule_pack_sha256"] != first.json()["rule_pack_sha256"]
    assert service.store.snapshot(TENANT, run_id) == frozen
    assert LocalSQLiteAuditStore(service.store.path).events(TENANT, run_id) == audit
