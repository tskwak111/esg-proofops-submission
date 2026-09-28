"""AT-031: real local SQLite/HTTP exports over explicit synthetic parser/tag fixtures."""

import json
import sqlite3
from dataclasses import asdict, replace
from hashlib import sha256
from io import BytesIO
from uuid import uuid4
from zipfile import ZipFile

import pytest
from proofops.application.authorization import MembershipRecord
from proofops.application.ports.jobs import JobMessage
from proofops_agent.extraction import SyntheticClaimExtractor

from tests.integration.test_local_tag_runner import TENANT
from tests.integration.test_revision_coverage import prepare_rescore, resolve, workspace
from tests.integration.test_run_lifecycle import validate


def exports(tmp_path, monkeypatch, *, store_type=None, configure_runner=None):
    try:
        from proofops.adapters.local.export_store import LocalExportStore
        from proofops.application.exports import ExportService
        from proofops_api.routers.exports import build_exports_router
    except ImportError as exc:
        pytest.fail(f"Export implementation missing: {exc}")
    ws = workspace(tmp_path, monkeypatch, configure_runner=configure_runner)
    store = (store_type or LocalExportStore)(ws["service"].store, ws["runner"].claims)
    service = ExportService(store)
    now = [__import__("time").time()]
    ws["http"].app.include_router(
        build_exports_router(
            service, ws["auth"], allowed_origin="https://testserver", clock=lambda: now[0]
        )
    )
    ws.update(exports=service, export_store=store, now=now)
    return ws


def create(ws, **changes):
    return ws["http"].post(
        f'/v1/runs/{ws["run"]}/exports',
        json=dict(formats=["json", "csv", "html"], allow_partial=True) | changes,
    )


def archive(ws, result):
    download = ws["http"].post(f'/v1/exports/{result["export_id"]}/download')
    assert download.status_code == 200, download.text
    validate("Download", download.json())
    content = ws["http"].get(download.json()["url"])
    assert content.status_code == 200, content.text
    assert sha256(content.content).hexdigest() == download.json()["sha256"]
    return ZipFile(BytesIO(content.content)), download.json(), content.content


def test_real_http_bundle_manifest_provenance_idempotency_and_immutable_reopen(
    tmp_path, monkeypatch
):
    ws = exports(tmp_path, monkeypatch)
    resolve(ws)
    response = create(ws)
    assert response.status_code == 202, response.text
    result = response.json()
    validate("Export", result)
    assert result["state"] == "ready" and result["partial"] is True
    bundle, _, before = archive(ws, result)
    assert set(bundle.namelist()) == {"manifest.json", "report.json", "report.csv", "report.html"}
    manifest = json.loads(bundle.read("manifest.json"))
    model = json.loads(bundle.read("report.json"))
    assert sha256(bundle.read("manifest.json")).hexdigest() == result["manifest_sha256"]
    assert manifest["execution_profile"] == model["execution_profile"] == "local-synthetic-only"
    claim = model["claims"][0]
    assert claim["tag_revision"] == 2 and claim["decision_revision"] == 1
    assert claim["replicate_hashes"] == list(ws["tags"].replicate_hashes)
    assert claim["source_refs"] and claim["source_refs"][0]["bbox"]
    assert claim["claim_quote"] == ws["runner"].claims.list(TENANT, ws["run"])[0].quote
    assert create(ws).json() == result
    assert create(ws, formats=["json"]).status_code == 409
    # Real rules-only rescore changes current heads, never the old report or audit event.
    store, body, captured, decisions = prepare_rescore(ws)
    store.commit(ws["actor"], ws["run"], body, captured, decisions)
    reopened = type(ws["export_store"])(ws["service"].store, ws["runner"].claims)
    assert reopened.get(TENANT, result["export_id"]) == result
    assert archive(ws, result)[2] == before
    with sqlite3.connect(ws["jobs"].path) as db:
        for action in ("UPDATE job_records SET value=value", "DELETE FROM job_records"):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(action + " WHERE kind='export_snapshot'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT OR REPLACE INTO job_records SELECT * FROM job_records "
                "WHERE kind='export_artifact'"
            )


def test_later_tag_checkpoint_keeps_review_decision_coverage_and_export(tmp_path, monkeypatch):
    extract = SyntheticClaimExtractor.extract

    def two_claims(self, packet):
        original = extract(self, packet)["spans"][0]
        split = original["quote"].index(" 1234") + original["char_start"]
        return {
            "spans": [
                original | {"char_end": split, "quote": original["quote"][:split]},
                original | {"char_start": split + 1, "quote": original["quote"][split + 1 :]},
            ]
        }

    def leave_other_claim_for_recovery(runner, run_id):
        other = runner.claims.load_evidence(TENANT, run_id)[1].claims[1].claim_id
        preliminary = runner.preliminary
        runner.preliminary = lambda claim, graph: (
            None if claim.claim_id == other else preliminary(claim, graph)
        )

    monkeypatch.setattr(SyntheticClaimExtractor, "extract", two_claims)
    ws = exports(tmp_path, monkeypatch, configure_runner=leave_other_claim_for_recovery)
    resolve(ws)
    jobs, tenant, run_id = ws["jobs"], ws["actor"].tenant_id, ws["run"]
    run = jobs.get_run(tenant, run_id)
    assert run["coverage"]["claims_discovered"] == 2
    old_message = JobMessage(**run["tag_job"])
    old_checkpoint = jobs.read_checkpoint(old_message)
    assert old_checkpoint is not None
    assert [item["status"] for item in json.loads(old_checkpoint)["claims"]] == [
        "needs_review",
        "blocked",
    ]
    with jobs._transaction() as db:
        revisions_before = db.execute(
            "SELECT kind,record_id,value FROM job_records WHERE tenant_id=? AND run_id=? "
            "AND kind IN ('tag_revision','decision_revision') ORDER BY kind,record_id",
            (tenant, run_id),
        ).fetchall()
    message = replace(old_message, job_id=str(uuid4()), shard="tag-recovery-regression")
    jobs.enqueue(message, now=ws["runner"].clock())
    lease = jobs.claim_job(
        message, owner="test-recovery", now=ws["runner"].clock(), lease_seconds=300
    )
    assert lease is not None
    assert jobs.commit_job(
        lease, payload=old_checkpoint, now=ws["runner"].clock(), publish=lambda db: None
    )
    assert jobs.get_run(tenant, run_id)["coverage"]["claims_decided"] == 1
    assert jobs.get_run(tenant, run_id)["coverage"]["claims_needs_review"] == 1
    with jobs._transaction() as db:
        assert (
            db.execute(
                "SELECT kind,record_id,value FROM job_records WHERE tenant_id=? AND run_id=? "
                "AND kind IN ('tag_revision','decision_revision') ORDER BY kind,record_id",
                (tenant, run_id),
            ).fetchall()
            == revisions_before
        )
    assert create(ws).status_code == 202


def test_review_during_capture_retries_only_snapshot_then_renders_frozen_revisions(
    tmp_path, monkeypatch
):
    ws = exports(tmp_path, monkeypatch)
    store = ws["export_store"]
    original = store.capture
    captures = []

    def capture(*args):
        result = original(*args)
        captures.append(result)
        if len(captures) == 1:
            # Actual guarded human resolution, not a mocked revision or LLM decision.
            inputs = ws["original"]
            ws["runner"].reviews.resolve_review(
                ws["actor"],
                ws["review"]["review_id"],
                dict(
                    base_tag_revision=1,
                    track="performance",
                    reason="원문 태깅 확인",
                    elements=[asdict(e) for e in inputs.consensus.candidate_elements],
                ),
                '"1"',
                str(uuid4()),
            )
        return result

    monkeypatch.setattr(store, "capture", capture)
    response = create(ws)
    assert response.status_code == 202, response.text
    bundle, _, _ = archive(ws, response.json())
    assert len(captures) == 2
    report = json.loads(bundle.read("report.json"))
    assert report["snapshot_epoch"] == ws["jobs"].get_run(TENANT, ws["run"])["mutation_epoch"]
    assert report["claims"][0]["tag_revision"] == 2
    assert report["claims"][0]["decision_revision"] == 1
    assert report["claims"][0]["label"] is None
    with ws["jobs"]._transaction() as db:
        assert len(ws["jobs"]._all(db, TENANT, ws["run"], "export_snapshot")) == 1
        assert (
            db.execute(
                "SELECT COUNT(*) FROM job_records WHERE tenant_id=? AND run_id=? "
                "AND kind='export_artifact'",
                (TENANT, ws["run"]),
            ).fetchone()[0]
            == 1
        )


def test_continuously_busy_snapshot_is_durably_requeued_and_poll_resumes(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    store = ws["export_store"]
    original = store.capture
    calls = []

    def capture(*args):
        captured = original(*args)
        calls.append(captured)
        with ws["jobs"]._transaction() as db:
            run = ws["jobs"]._get(db, TENANT, ws["run"], "run", "META")
            ws["jobs"]._bump_run(db, run)
        return captured

    monkeypatch.setattr(store, "capture", capture)
    response = create(ws)
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["state"] == "queued" and result["manifest_sha256"] is None
    assert len(calls) == 4  # Initial attempt plus at most three retries.
    assert ws["http"].post(f'/v1/exports/{result["export_id"]}/download').status_code == 409
    with ws["jobs"]._transaction() as db:
        assert not ws["jobs"]._all(db, TENANT, ws["run"], "export_snapshot")
        state = ws["jobs"]._get(db, TENANT, ws["run"], "export_state", result["export_id"])
        assert state["errors"] == ["EXPORT_SNAPSHOT_BUSY"]
    monkeypatch.setattr(store, "capture", original)
    ws["now"][0] += 6
    resumed = ws["http"].get(f'/v1/exports/{result["export_id"]}')
    assert resumed.status_code == 200 and resumed.json()["state"] == "ready"
    archive(ws, resumed.json())


def test_partial_gate_request_validation_and_tenant_revocation_download_expiry(
    tmp_path, monkeypatch
):
    ws = exports(tmp_path, monkeypatch)
    final = create(ws, allow_partial=False)
    assert final.status_code == 409 and final.json()["error"]["code"] == "REPORT_NOT_FINALIZABLE"
    for body in ({"formats": ["pdf"]}, {"formats": ["json", "json"]}, {"allow_partial": "true"}):
        assert create(ws, **body).status_code == 422
    ws["http"].headers["Idempotency-Key"] = str(uuid4())
    result = create(ws).json()
    _, ticket, _ = archive(ws, result)
    assert ws["http"].get(ticket["url"] + "x").status_code in (403, 422)
    ws["now"][0] += 301
    assert ws["http"].get(ticket["url"]).status_code == 403
    ws["now"][0] -= 301
    ws["auth"].memberships.put(MembershipRecord(TENANT, "admin-user", "viewer", "revoked"))
    assert ws["http"].get(ticket["url"]).status_code == 404


def test_cross_tenant_export_and_csrf_fail_closed(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    result = create(ws).json()
    _, ticket, _ = archive(ws, result)
    ws["http"].headers["X-CSRF-Token"] = "invalid"
    assert create(ws).status_code == 403
    assert ws["http"].post(f'/v1/exports/{result["export_id"]}/download').status_code == 403
    session = ws["auth"].sessions.get("admin-session")
    foreign = str(uuid4())
    ws["auth"].memberships.put(MembershipRecord(foreign, "admin-user", "viewer", "active"))
    ws["auth"].sessions.put(replace(session, active_tenant_id=foreign))
    assert ws["http"].get(f'/v1/exports/{result["export_id"]}').status_code == 404
    assert ws["http"].get(ticket["url"]).status_code in (403, 404)


def test_preparse_partial_and_untagged_sources_remain_honest(tmp_path, monkeypatch):
    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.export_store import LocalExportStore
    from proofops.application.exports import ExportService
    from proofops_api.routers.exports import build_exports_router

    from tests.integration.test_local_extract_runner import extraction_setup
    from tests.integration.test_run_lifecycle import client, setup

    (tmp_path / "queued").mkdir()
    (tmp_path / "extracted").mkdir()
    service, body = setup(tmp_path / "queued")
    http, auth = client(service)
    run = http.post("/v1/runs", json=body).json()
    store = LocalExportStore(service.store, LocalClaimStore(service.store, service.uploads, None))
    http.app.include_router(
        build_exports_router(ExportService(store), auth, allowed_origin="https://testserver")
    )
    ws = {"http": http, "run": run["run_id"]}
    result = create(ws).json()
    bundle, _, _ = archive(ws, result)
    report = json.loads(bundle.read("report.json"))
    assert report["parse_manifest_id"] is None and report["claims"] == []
    assert report["partial"] is True and report["coverage"]["pages_processed"] == 0

    service, run_id, runner, _, _ = extraction_setup(tmp_path / "extracted", monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    claims = LocalClaimStore(service.store, service.uploads, runner.parser)
    http, auth = client(service)
    store = LocalExportStore(service.store, claims)
    http.app.include_router(
        build_exports_router(ExportService(store), auth, allowed_origin="https://testserver")
    )
    ws = {"http": http, "run": run_id}
    response = create(ws)
    assert response.status_code == 202, response.text
    report = json.loads(archive(ws, response.json())[0].read("report.json"))
    claim = report["claims"][0]
    assert claim["tag_revision"] == claim["decision_revision"] == 0
    assert claim["model_sha256"] is None and claim["label"] is None
    assert claim["source_refs"] == json.loads(
        json.dumps([asdict(ref) for ref in claims.list(TENANT, run_id)[0].source_refs])
    )
    assert claim["source_refs"][0]["verification_state"] != "verified"


def test_artifact_corruption_and_other_user_ticket_are_rejected(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    response = create(ws).json()
    _, ticket, _ = archive(ws, response)
    session = ws["auth"].sessions.get("admin-session")
    ws["auth"].memberships.put(MembershipRecord(TENANT, "other-user", "viewer", "active"))
    ws["auth"].sessions.put(replace(session, user_sub="other-user"))
    assert ws["http"].get(ticket["url"]).status_code == 403
    ws["auth"].sessions.put(session)
    with sqlite3.connect(ws["jobs"].path) as db:
        # Explicit storage corruption bypasses the normal immutable write guard.
        db.execute("DROP TRIGGER export_immutable_update")
        db.execute(
            "UPDATE job_records SET value=? WHERE tenant_id=? AND run_id=? "
            "AND kind='export_artifact'",
            (b"corrupt", TENANT, ws["run"]),
        )
    content = ws["http"].get(ticket["url"])
    assert content.status_code == 409
    assert content.json()["error"]["code"] == "EXPORT_INTEGRITY_FAILED"
    assert ws["http"].post(f'/v1/exports/{response["export_id"]}/download').status_code == 409


def test_failed_export_has_pollable_fixed_metadata_and_no_artifact(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    failed = create(ws, allow_partial=False)
    assert failed.status_code == 409
    with ws["jobs"]._transaction() as db:
        result = ws["jobs"]._all(db, TENANT, ws["run"], "export_state")[0]["response"]
        assert not ws["jobs"]._all(db, TENANT, ws["run"], "export_snapshot")
    response = ws["http"].get(f'/v1/exports/{result["export_id"]}')
    assert response.status_code == 200, response.text
    validate("Export", response.json())
    assert response.json()["state"] == "failed"


def test_new_rescore_export_uses_valid_lineage_and_cannot_mix_tag_head(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    resolve(ws)
    store, body, captured, decisions = prepare_rescore(ws)
    store.commit(ws["actor"], ws["run"], body, captured, decisions)
    response = create(ws)
    assert response.status_code == 202, response.text
    report = json.loads(archive(ws, response.json())[0].read("report.json"))
    claim = report["claims"][0]
    assert claim["tag_revision"] == 2 and claim["decision_revision"] == 2
    assert claim["decision_status"] == "blocked_rule_gap" and claim["label"] is None
    with ws["jobs"]._transaction() as db:
        ws["jobs"]._put(
            db,
            TENANT,
            ws["run"],
            "claim_head",
            ws["review"]["claim_id"],
            dict(tag_revision=1, decision_revision=2),
        )
    ws["http"].headers["Idempotency-Key"] = str(uuid4())
    mixed = create(ws)
    assert mixed.status_code == 409 and mixed.json()["error"]["code"] == "EXPORT_INTEGRITY_FAILED"


def test_render_limit_failure_persists_failed_state_without_publishing_bytes(tmp_path, monkeypatch):
    import proofops.application.exports as module

    ws = exports(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 100)
    failed = create(ws)
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "EXPORT_SIZE_LIMIT"
    with ws["jobs"]._transaction() as db:
        assert (
            db.execute("SELECT COUNT(*) FROM job_records WHERE kind='export_artifact'").fetchone()[
                0
            ]
            == 0
        )


def test_mutation_after_freeze_does_not_recapture_or_change_pinned_report(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    original = ws["export_store"].freeze

    def freeze(*args):
        committed = original(*args)
        if committed:
            resolve(ws)
        return committed

    monkeypatch.setattr(ws["export_store"], "freeze", freeze)
    response = create(ws)
    assert response.status_code == 202, response.text
    report = json.loads(archive(ws, response.json())[0].read("report.json"))
    assert report["claims"][0]["tag_revision"] == 1
    assert report["claims"][0]["decision_revision"] == 0
    assert report["snapshot_epoch"] < ws["jobs"].get_run(TENANT, ws["run"])["mutation_epoch"]
    assert report["claims"][0]["label"] is None


def test_unfinished_export_preserves_tag_uncertainty_without_grading(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    current = ws["runner"].claims.current_tag(TENANT, ws["run"], ws["review"]["claim_id"])
    expected = [
        element["element_id"]
        for element in current["tag"]["elements"]
        if element["state"] in ("unknown", "conflict")
    ]
    assert expected
    response = create(ws)
    assert response.status_code == 202, response.text
    bundle = archive(ws, response.json())[0]
    claim = json.loads(bundle.read("report.json"))["claims"][0]
    assert claim["claim_quote"] == ws["runner"].claims.list(TENANT, ws["run"])[0].quote
    assert claim["unresolved_elements"] == expected
    assert claim["review_action"]["unresolved_elements"] == expected
    assert "unresolved_evidence" in claim["review_action"]["reasons"]
    assert claim["missing_elements"] == []
    assert claim["evidence_grade"] is None and claim["decision_status"] == "not_run"
    assert "미해결 요소의 원문 근거 귀속" in bundle.read("report.html").decode()


def test_capture_projects_immutable_tag_elements_without_grading(tmp_path, monkeypatch):
    from proofops.application.reporting import build_report_model

    ws = exports(tmp_path, monkeypatch)
    store = ws["export_store"]
    claim_id = ws["review"]["claim_id"]
    current = ws["runner"].claims.current_tag(TENANT, ws["run"], claim_id)
    assert current["tag"]["elements"]
    export_id = store.reserve(
        ws["actor"],
        ws["run"],
        dict(formats=["json", "csv", "html"], allow_partial=True),
        "tag-elements-check",
        now=ws["now"][0],
    )
    captured = store.capture(TENANT, export_id)
    record = captured["decisions"][claim_id]
    assert isinstance(record["tag_elements"], list) and record["tag_elements"]
    pinned = {e["element_id"]: e for e in current["tag"]["elements"]}
    for element in record["tag_elements"]:
        assert element["state"] == pinned[element["element_id"]]["state"]
        assert element["normalized_value"] == pinned[element["element_id"]]["normalized_value"]
        assert len(element["evidence_refs"]) == len(pinned[element["element_id"]]["evidence_refs"])
        if element["evidence_refs"]:
            assert (
                element["evidence_refs"][0]["quote"]
                == pinned[element["element_id"]]["evidence_refs"][0]["quote"]
            )
        if element["state"] == "present":
            assert element["evidence_refs"]
            assert all(
                ref.get("verification_state") == "verified" for ref in element["evidence_refs"]
            )
    model = build_report_model(captured["manifest"], captured["decisions"])
    got = [(e["element_id"], e["state"]) for e in model["claims"][0]["tag_elements"]]
    want = [(e["element_id"], e["state"]) for e in record["tag_elements"]]
    assert got == want
    assert model["claims"][0]["evidence_grade"] is None
    # Foreign/tampered evidence must fail closed at projection time.
    tampered = json.loads(json.dumps(captured["decisions"]))
    tampered[claim_id]["tag_elements"].append(
        json.loads(json.dumps(tampered[claim_id]["tag_elements"][0]))
    )
    with pytest.raises(ValueError):
        build_report_model(captured["manifest"], tampered)


def test_idempotency_accepts_contract_strings_and_invalid_json_is_422(tmp_path, monkeypatch):
    ws = exports(tmp_path, monkeypatch)
    ws["http"].headers["Idempotency-Key"] = "synthetic-export-request-0001"
    response = create(ws)
    assert response.status_code == 202, response.text
    bad = ws["http"].post(
        f'/v1/runs/{ws["run"]}/exports',
        content=b"{broken",
        headers={"Content-Type": "application/json"},
    )
    assert bad.status_code == 422


def test_capture_size_limit_rejects_before_snapshot_or_report_publication(tmp_path, monkeypatch):
    import proofops.adapters.local.export_store as module

    ws = exports(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "MAX_EXPORT_BYTES", 100)
    failed = create(ws)
    assert failed.status_code == 409 and failed.json()["error"]["code"] == "EXPORT_SIZE_LIMIT"
    with ws["jobs"]._transaction() as db:
        assert not ws["jobs"]._all(db, TENANT, ws["run"], "export_snapshot")
        assert (
            db.execute("SELECT COUNT(*) FROM job_records WHERE kind='export_artifact'").fetchone()[
                0
            ]
            == 0
        )


def test_export_preserves_ai_classification_origin_before_tagging(tmp_path, monkeypatch):
    from proofops.adapters.local.export_store import LocalExportStore
    from proofops.application.reporting import build_report_model, render_report

    from tests.integration.test_manual_classification_reprocess import (
        _actor,
        _blocked_setup,
        _body,
    )

    service, run_id, runner, now, classification, claim_id = _blocked_setup(tmp_path, monkeypatch)
    actor = _actor()
    view, body = _body(classification, run_id, claim_id, actor)
    accepted = classification.classify_ai_delegated(
        actor,
        run_id,
        claim_id,
        body,
        view["etag"],
        "export-ai-classification-check",
        delegated_reviewer="test-agent",
        delegation_authority="Synthetic test only",
        now=int(now[0]),
    )
    store = LocalExportStore(service.store, runner.claims)
    export_id = store.reserve(
        actor,
        run_id,
        dict(formats=["json", "csv", "html"], allow_partial=True),
        "export-ai-check",
        now=now[0],
    )
    captured = store.capture(TENANT, export_id)
    model = build_report_model(captured["manifest"], captured["decisions"])
    review = model["claims"][0]["classification_review"]
    assert review["origin"] == "ai_delegated_classification"
    assert review["classification_id"] == accepted["classification"]["classification_id"]
    assert model["claims"][0]["decision_status"] == "not_run"
    assert "AI 위임 분류(사람 검토 아님)" in render_report(model, "html").decode()
    assert "ai_delegated_classification" in render_report(model, "csv").decode()
    assert "ai_delegated_classification" in render_report(model, "json").decode()
    # A changed mutable head cannot relabel the immutable AI record as human.
    assert store.freeze(TENANT, export_id, captured)
    with service.store.jobs._transaction() as db:
        head = service.store.jobs._get(
            db, TENANT, run_id, "preliminary_classification_head", claim_id
        )
        service.store.jobs._put(
            db,
            TENANT,
            run_id,
            "preliminary_classification_head",
            claim_id,
            dict(head, origin="human_classification"),
        )
    from proofops.application.exports import ExportRejected

    with pytest.raises(ExportRejected, match="EXPORT_INTEGRITY_FAILED"):
        store.capture(TENANT, export_id)
    assert store.frozen(TENANT, export_id)["decisions"][claim_id]["classification_review"] == review
