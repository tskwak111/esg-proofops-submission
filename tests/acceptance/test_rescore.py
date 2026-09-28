"""AT-020: synthetic immutable inputs, actual rules and durable rescore behavior."""

from dataclasses import asdict, replace

import pytest
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import pack_content_hash, snapshot_from_validated
from proofops.domain.rules.engine import Decision, evaluate

from tests.acceptance.test_rules import inputs, pack


def revised_pack(*, edit=None, **changes):
    old = pack()
    metadata = asdict(old)
    metadata.pop("content")
    metadata.update(version="synthetic-rescore-v2", **changes)
    contents = {path: old.file_content(path) for path in old.files}
    for content in contents.values():
        content["version"] = metadata["version"]
    if edit:
        edit(contents)
    metadata["sha256"] = pack_content_hash(metadata, contents)
    return snapshot_from_validated(metadata, contents)


def create(tags=None, context=None, **kwargs):
    try:
        from proofops.application.rescores import create_rescore
    except ImportError as exc:
        pytest.fail(f"Rescore implementation missing: {exc}")
    defaults, default_context = inputs()
    tags = defaults if tags is None else tags
    context = context or replace(default_context, decision_revision=2)
    return create_rescore(
        kwargs.pop("new_pack", revised_pack()),
        tags,
        previous_pack=kwargs.pop("previous_pack", pack()),
        context=context,
        previous_decision=kwargs.pop(
            "previous_decision", evaluate(tags, replace(context, decision_revision=1), pack())
        ),
        **kwargs,
    )


def test_compatible_pack_creates_new_engine_decision_without_changing_tags():
    tags, context = inputs()
    before = asdict(tags)
    previous = evaluate(tags, context, pack())
    new = revised_pack()
    result = create(tags, new_pack=new, previous_decision=previous)
    assert isinstance(result, Decision)
    assert result == evaluate(tags, replace(context, decision_revision=2), new)
    assert result.evidence_grade == "E3" and result.label == "SUBSTANTIATED"
    assert result.tag_revision == 1 and result.decision_revision == 2
    assert result.input_tags_sha256 == canonical_hash(before)
    assert result.rule_pack_sha256 == new.sha256 != previous.rule_pack_sha256
    assert result.semantic_hash != previous.semantic_hash
    assert asdict(tags) == before and previous.decision_revision == 1
    assert result == create(tags, new_pack=new, previous_decision=previous)


def test_new_ontology_requires_retagging_without_rewriting_old_version():
    tags, _ = inputs()
    result = create(tags, new_pack=revised_pack(ontology_version="synthetic-elements-v2"))
    assert result.code == "RETAG_REQUIRED"
    assert "ONTOLOGY_CHANGED" in result.reasons
    assert tags.ontology_version == pack().ontology_version


@pytest.mark.parametrize(
    "change",
    [
        "new_element",
        "renamed_meaning",
        "new_trigger",
        "requirement",
        "new_value_type",
        "scope_expansion",
    ],
)
def test_unbumped_input_contract_change_is_not_silently_reused(change):
    def edit(files):
        elements = files["rubric/elements.yaml"]["elements"]
        element = next(e for e in elements if e["id"] == "P1")
        if change == "new_element":
            elements.append(element | {"id": "P7"})
        elif change == "renamed_meaning":
            element["name"] = "synthetic changed meaning"
        elif change == "new_trigger":
            element["trigger"] = "new_uncertainty_fact"
        elif change == "requirement":
            element["requirement"] = "conditional"
        elif change == "new_value_type":
            element["value_type"] = "decimal"
        else:
            element["source_scopes"].append("global_bound")

    assert create(new_pack=revised_pack(edit=edit)).code == "RETAG_REQUIRED"


def test_new_rule_operand_not_in_old_tags_requires_retagging():
    def edit(files):
        files["rubric/performance.yaml"]["branches"][1]["require_all"].append(
            "uncertainty_documented"
        )

    result = create(new_pack=revised_pack(edit=edit))
    assert (
        result.code == "RETAG_REQUIRED" and "MISSING_FACT:uncertainty_documented" in result.reasons
    )


def test_changed_safe_harbor_checklist_requires_retagging():
    def edit(files):
        files["regulatory/safe_harbor.yaml"]["category_checklists"]["forward_looking"].append(
            "new_synthetic_assumption"
        )

    result = create(new_pack=revised_pack(edit=edit))
    assert result.code == "RETAG_REQUIRED" and "SAFE_HARBOR_INPUTS_CHANGED" in result.reasons


def test_uncollected_fact_is_not_manufactured_as_unknown_or_absent():
    tags, _ = inputs()
    tags = replace(tags, facts=tuple(f for f in tags.facts if f.name != "method"))
    result = create(tags)
    assert result.code == "RETAG_REQUIRED" and "MISSING_FACT:method" in result.reasons
    assert "method" not in {f.name for f in tags.facts}


@pytest.mark.parametrize("state", ["unknown", "conflict", "not_applicable"])
def test_stored_uncertainty_is_preserved_and_grade_remains_blocked(state):
    tags, _ = inputs(method=state)
    result = create(tags)
    assert isinstance(result, Decision)
    assert result.decision_status == "blocked_evidence"
    assert result.evidence_grade is result.label is None
    assert next(f for f in tags.facts if f.name == "method").state == state


def test_basis_only_change_reuses_tags_and_records_new_basis_hash():
    def edit(files):
        element = next(e for e in files["rubric/elements.yaml"]["elements"] if e["id"] == "P1")
        element["basis"]["summary"] = "Synthetic explanatory summary, no new clause approval"

    result = create(new_pack=revised_pack(edit=edit))
    assert isinstance(result, Decision)
    assert any("Synthetic explanatory summary" in basis for basis in result.basis_refs)


def test_scope_narrowing_reuses_only_still_allowed_sources():
    def edit(files):
        for e in files["rubric/elements.yaml"]["elements"]:
            if e["id"] == "P1":
                e["source_scopes"] = ["local_claim"]

    assert isinstance(create(new_pack=revised_pack(edit=edit)), Decision)
    tags, _ = inputs()
    tags = replace(
        tags,
        facts=tuple(
            replace(f, source_scope="same_table")
            if f.name == "quantitative_or_qualified_ordinal"
            else f
            for f in tags.facts
        ),
    )
    assert create(tags, new_pack=revised_pack(edit=edit)).code == "RETAG_REQUIRED"


@pytest.mark.parametrize(
    "field,value", [("tenant_id", "99999999-9999-4999-8999-999999999999"), ("mode", "advertising")]
)
def test_cross_tenant_or_mode_cannot_reuse_tags(field, value):
    with pytest.raises(DomainValidationError):
        create(new_pack=revised_pack(**{field: value}))


def test_next_decision_revision_must_not_overwrite_previous():
    tags, context = inputs()
    with pytest.raises(DomainValidationError):
        create(tags, context=context)


def test_changed_packet_cannot_reuse_same_source_tag_revision():
    tags, context = inputs()
    with pytest.raises(DomainValidationError):
        create(
            tags,
            context=replace(context, packet_sha256="f" * 64, decision_revision=2),
            previous_decision=evaluate(tags, context, pack()),
        )


def test_missing_confirmed_revision_returns_retag_required():
    try:
        from proofops.application.rescores import create_rescore
    except ImportError as exc:
        pytest.fail(str(exc))
    _, context = inputs()
    assert (
        create_rescore(revised_pack(), None, previous_pack=pack(), context=context).code
        == "RETAG_REQUIRED"
    )


# Durable fixture records are explicitly synthetic. They use the real review
# source graph, original packet, pure evaluator, SQLite and HTTP authorization.
def rescore_workspace(
    tmp_path,
    *,
    role="reviewer",
    ontology=None,
    confirmed=True,
    pin=None,
    track=None,
    origin="local-synthetic-fixture",
    real=False,
    partial=False,
):
    import json
    from uuid import uuid4

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.rulepacks import RulePackRecord
    from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags
    from proofops.domain.values import _source_ref_from_dict

    from tests.acceptance.test_reviews import post, workspace

    try:
        from proofops.adapters.local.rescore_store import LocalSQLiteRescoreStore
        from proofops.application.rescores import RescoreService
        from proofops_api.routers.rescores import build_rescores_router
    except ImportError as exc:
        pytest.fail(f"Durable rescore missing: {exc}")
    if partial:
        from proofops.application.tagging.consensus import PARTIAL_FACTS_V1, form_consensus

        def prepare(inputs):
            runs = tuple(replace(run, product_variant=False) for run in inputs.tag_runs)
            consensus = form_consensus(
                runs,
                packet=inputs.packet,
                rulepack=inputs.rulepack,
                tenant_id=inputs.context.claim.tenant_id,
                tag_revision=1,
                profile=PARTIAL_FACTS_V1,
            )
            draft = replace(
                inputs, tag_runs=runs, consensus=consensus, fact_assembly_profile=PARTIAL_FACTS_V1
            )
            return replace(
                draft,
                decision=evaluate(consensus.confirmed_tags, draft.rule_context, draft.rulepack),
            )

        ws = workspace(tmp_path, prepare_inputs=prepare)
    else:
        ws = workspace(tmp_path)
        assert post(ws).status_code == 200
    review_inputs = ws[2]
    if real:
        # Same pinned inputs as a non-synthetic run decided under an approved pack.
        review_inputs = replace(
            review_inputs,
            rule_context=replace(review_inputs.rule_context, local_synthetic=False),
            rulepack=replace(
                review_inputs.rulepack,
                status="active",
                approved_by="user:owner",
                approved_at="2026-09-25T00:00:00Z",
            ),
        )
    tenant, run, claim = (
        review_inputs.rule_context.tenant_id,
        review_inputs.run_id,
        ws[3]["claim_id"],
    )
    runs = LocalSQLiteRunStore(ws[1].store.jobs.path)
    # Explicit synthetic collection of an unknown applicability trigger that
    # current tag DTOs cannot collect; never manufactures absence or present.
    tag = ws[1].store.history(tenant, run, claim)["tags"][-1]
    raw = tag["confirmed_tags"]
    saved = ConfirmedTags(
        **(
            raw
            | {
                "tag_revision": 1 if partial else 3,
                "facts": tuple(
                    ConfirmedFact(
                        **(
                            fact
                            | {
                                "evidence_refs": tuple(
                                    _source_ref_from_dict(r) for r in fact["evidence_refs"]
                                )
                            }
                        )
                    )
                    for fact in raw["facts"]
                )
                + (
                    () if partial else (ConfirmedFact("reduction_or_improvement_claim", "unknown"),)
                ),
            }
        )
    )
    if pin is not None:
        saved = replace(saved, **{pin: "e" * 64})
    if track is not None:
        # A track correction brings the new track's facts; unconfirmed ones stay unknown.
        from proofops.domain.rules.engine import MAPPINGS

        elements = review_inputs.rulepack.file_content("rubric/elements.yaml")["elements"]
        wanted = {n for names in MAPPINGS[track].values() for n in names} | {
            e["trigger"] for e in elements if e.get("trigger") and e["id"][0] == track[0].upper()
        }
        have = {fact.name for fact in saved.facts}
        saved = replace(
            saved,
            track=track,
            facts=saved.facts
            + tuple(ConfirmedFact(name, "unknown") for name in sorted(wanted - have)),
        )
    context = replace(review_inputs.rule_context, decision_revision=1 if partial else 2)
    decision = evaluate(saved, context, review_inputs.rulepack)
    if real:
        from proofops.domain.provenance import canonical_hash

        tag = {k: v for k, v in tag.items() if k != "inputs"} | {
            "input_snapshot_sha256": canonical_hash(review_inputs.snapshot())
        }
    if not partial:
        tag = tag | {
            "tag_revision": 3,
            "confirmed_tags": asdict(saved) if confirmed else None,
            "origin": origin,
            "execution_profile": "local-synthetic-only",
        }
    with runs.jobs._transaction() as db:
        if not partial:
            runs.jobs._put(
                db, tenant, run, "tag_revision", f"{claim}:0000000003", tag, immutable=True
            )
            runs.jobs._put(
                db,
                tenant,
                run,
                "decision_revision",
                f"{claim}:0000000002",
                dict(decision_revision=2, decision=asdict(decision), api=decision.to_api_dict()),
                immutable=True,
            )
            runs.jobs._put(
                db, tenant, run, "claim_head", claim, dict(tag_revision=3, decision_revision=2)
            )
        record = runs.jobs._get(db, tenant, run, "run", "META")
        runs.jobs._bump_run(db, record, status="partial")
        db.execute(
            "INSERT INTO run_snapshots VALUES (?,?,?)",
            (
                tenant,
                run,
                json.dumps(
                    dict(
                        run_id=run,
                        tenant_id=tenant,
                        rulepack=asdict(review_inputs.rulepack),
                        document={"version_id": review_inputs.original.document_version_id},
                        execution_profile="local-synthetic-only",
                    )
                ),
            ),
        )
    target = revised_pack(
        rule_pack_id=str(uuid4()),
        status="validated",
        approved_by="synthetic-fixture",
        approved_at="2026-09-08T10:00:00Z",
        **({"ontology_version": ontology} if ontology else {}),
    )
    runs.rulepacks.add_pack(
        RulePackRecord(**{k: v for k, v in asdict(target).items() if k != "content"}),
        {p: target.file_content(p) for p in target.files},
    )
    runs.rulepacks.activate(
        tenant_id=tenant,
        rule_pack_id=target.rule_pack_id,
        expected_revision=1,
        idempotency_key=str(uuid4()),
        actor="synthetic-fixture",
        reason="Synthetic test only",
        gap_ids=target.unresolved_gap_ids,
        now=1,
    )
    service = RescoreService(
        LocalSQLiteRescoreStore(runs), load_inputs=lambda tenant, run, claim: review_inputs
    )
    from proofops.application.authorization import MembershipRecord

    ws[6].memberships.put(MembershipRecord(tenant, "admin-user", role, "active"))
    app = FastAPI()
    app.include_router(build_rescores_router(service, ws[6], allowed_origin="http://testserver"))
    client = TestClient(app)
    from proofops_api.auth import SESSION_COOKIE_NAME

    client.cookies.set(SESSION_COOKIE_NAME, "admin-session")
    headers = {k: v for k, v in ws[5].items() if k != "If-Match"}
    body = dict(rule_pack_id=target.rule_pack_id, reason="Synthetic rules-only rescore")
    return dict(
        client=client,
        service=service,
        runs=runs,
        review=ws[1],
        inputs=review_inputs,
        tenant=tenant,
        run=run,
        claim=claim,
        body=body,
        headers=headers,
        target=target,
        auth=ws[6],
    )


def rescore_post(ws, **kwargs):
    return ws["client"].post(
        f'/v1/runs/{ws["run"]}/rescores',
        json=kwargs.get("body", ws["body"]),
        headers=kwargs.get("headers", ws["headers"]),
    )


def records(ws):
    with ws["runs"].jobs._transaction() as db:
        return db.execute(
            "SELECT kind,record_id,value FROM job_records WHERE tenant_id=? AND run_id=? "
            "ORDER BY kind,record_id",
            (ws["tenant"], ws["run"]),
        ).fetchall()


def test_http_rescore_persists_real_decision_and_receipt_preserving_all_inputs(tmp_path):
    import json

    import jsonschema

    ws = rescore_workspace(tmp_path)
    history = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])
    manifest = ws["runs"].snapshot(ws["tenant"], ws["run"])
    before_run = ws["runs"].jobs.get_run(ws["tenant"], ws["run"])
    response = rescore_post(ws)
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "ready"
    schema = json.load(open("contracts/jsonschema/api_models.schema.json"))
    jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/JobAccepted", "$defs": schema["$defs"]}
    ).validate(response.json())
    get = ws["client"].get(response.json()["status_url"])
    assert get.status_code == 200 and get.json() == response.json()
    assert get.headers["cache-control"] == "no-store"
    after = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])
    assert after["tags"] == history["tags"]
    assert after["decisions"][:-1] == history["decisions"]
    assert after["decisions"][-1]["decision"]["rule_pack_sha256"] == ws["target"].sha256
    assert after["decisions"][-1]["decision"]["decision_revision"] == 3
    assert after["decisions"][-1]["decision"]["tag_revision"] == 3
    assert ws["runs"].snapshot(ws["tenant"], ws["run"]) == manifest
    after_run = ws["runs"].jobs.get_run(ws["tenant"], ws["run"])
    assert after_run["mutation_epoch"] == before_run["mutation_epoch"] + 1
    assert after_run["revision"] == before_run["revision"] + 1
    from proofops.adapters.local.audit_store import LocalSQLiteAuditStore

    assert (
        LocalSQLiteAuditStore(ws["runs"].path).events(ws["tenant"], ws["run"])[-1].action
        == "run_rescored"
    )
    persisted = records(ws)
    assert rescore_post(ws).json() == response.json() and records(ws) == persisted
    from proofops.adapters.local.rescore_store import LocalSQLiteRescoreStore

    assert (
        LocalSQLiteRescoreStore(ws["runs"]).get(
            ws["tenant"], ws["run"], response.json()["resource_id"]
        )
        == response.json()
    )


def test_http_rescore_accepts_partial_fact_revision_without_retag(tmp_path):
    ws = rescore_workspace(tmp_path, partial=True)
    response = rescore_post(ws)
    assert response.status_code == 202, response.text
    decisions = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])["decisions"]
    assert decisions[-1]["decision"]["decision_status"] == "blocked_evidence"
    assert decisions[-1]["api"]["grade_range"] == {
        "floor": "E1",
        "ceiling": "E3",
        "open_elements": ["P2", "P3", "P4"],
    }
    assert decisions[-1]["fact_assembly"] == decisions[0]["fact_assembly"]


def test_partial_fact_rescore_cannot_publish_candidate_grade(tmp_path, monkeypatch):
    from proofops.application import rescores

    ws = rescore_workspace(tmp_path, partial=True)
    original = rescores.create_rescore

    def candidate(*args, **kwargs):
        return replace(original(*args, **kwargs), decision_status="decided", evidence_grade="E3")

    monkeypatch.setattr(rescores, "create_rescore", candidate)
    response = rescore_post(ws)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "HUMAN_REVIEW_REQUIRED"


@pytest.mark.parametrize("change", ["ontology", "no_confirmed"])
def test_http_retag_required_never_creates_ready_job_or_changes_heads(tmp_path, change):
    ws = rescore_workspace(
        tmp_path,
        ontology="synthetic-elements-v2" if change == "ontology" else None,
        confirmed=change != "no_confirmed",
    )
    before = records(ws)
    response = rescore_post(ws)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RETAG_REQUIRED"
    assert records(ws) == before


@pytest.mark.parametrize("boundary", ["stale", "label", "csrf", "key", "foreign", "viewer"])
def test_http_validation_and_isolation_before_mutation(tmp_path, boundary):
    from uuid import uuid4

    ws = rescore_workspace(tmp_path, role="viewer" if boundary == "viewer" else "reviewer")
    headers, body = dict(ws["headers"]), dict(ws["body"])
    status = dict(stale=412, label=422, csrf=403, key=400, foreign=404, viewer=403)[boundary]
    if boundary == "stale":
        headers["If-Match"] = '"999"'
    if boundary == "label":
        body["label"] = "SUBSTANTIATED"
    if boundary == "csrf":
        headers["X-CSRF-Token"] = "wrong"
    if boundary == "key":
        headers.pop("Idempotency-Key")
    before = records(ws)
    run = str(uuid4()) if boundary == "foreign" else ws["run"]
    response = ws["client"].post(f"/v1/runs/{run}/rescores", json=body, headers=headers)
    assert response.status_code == status, response.text
    assert records(ws) == before


def test_audit_failure_rolls_back_whole_rescore(tmp_path):
    import sqlite3

    ws = rescore_workspace(tmp_path)
    before = records(ws)
    with sqlite3.connect(ws["runs"].path) as db:
        db.execute(
            "CREATE TRIGGER fail_rescore BEFORE INSERT ON audit_events "
            "WHEN NEW.action='run_rescored' BEGIN "
            "SELECT RAISE(ABORT,'synthetic disk failure'); END"
        )
    assert rescore_post(ws).status_code == 409
    assert records(ws) == before


def test_rescore_rechecks_tag_head_after_loading_original_inputs(tmp_path):
    ws = rescore_workspace(tmp_path)
    before = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])

    def load(tenant, run, claim):
        # Real concurrent state change in SQLite while no writer lock is held.
        with ws["runs"].jobs._transaction() as db:
            ws["runs"].jobs._bump_run(db, ws["runs"].jobs._get(db, tenant, run, "run", "META"))
        return ws["inputs"]

    ws["service"].load_inputs = load
    response = rescore_post(ws)
    assert response.status_code == 412, response.text
    assert ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"]) == before


def test_competing_rescores_commit_exactly_one_rules_batch(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from proofops.application.authorization import AuthContext
    from proofops.application.rescores import RescoreRejected

    ws = rescore_workspace(tmp_path)
    barrier = Barrier(2)

    def load(tenant, run, claim):
        barrier.wait(timeout=10)
        return ws["inputs"]

    ws["service"].load_inputs = load

    def create(index):
        actor = AuthContext(
            f"reviewer-{index}",
            ws["tenant"],
            "reviewer",
            frozenset({"viewer", "reviewer"}),
            str(index),
        )
        try:
            ws["service"].create_rescore(
                actor, ws["run"], ws["body"], f"concurrent-rescore-{index}"
            )
            return 202
        except RescoreRejected as exc:
            return exc.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, range(2))) == [202, 412]
    history = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])
    assert len(history["decisions"]) == 3


def test_same_key_changed_body_conflicts_and_new_key_appends_next_revision(tmp_path):
    ws = rescore_workspace(tmp_path)
    first = rescore_post(ws)
    assert first.status_code == 202
    before = records(ws)
    assert (
        rescore_post(ws, body=ws["body"] | {"reason": "Changed synthetic reason"}).status_code
        == 409
    )
    assert records(ws) == before
    second = rescore_post(ws, headers=ws["headers"] | {"Idempotency-Key": "second-rescore-key"})
    assert second.status_code == 202, second.text
    assert second.json()["resource_id"] != first.json()["resource_id"]
    history = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])
    assert [d["decision_revision"] for d in history["decisions"]] == [1, 2, 3, 4]
    assert {d["decision"]["tag_revision"] for d in history["decisions"][1:]} == {3}


def test_foreign_tenant_cannot_read_or_rescore_existing_run(tmp_path):
    from uuid import uuid4

    from proofops.application.authorization import MembershipRecord

    ws = rescore_workspace(tmp_path)
    first = rescore_post(ws)
    assert first.status_code == 202
    before = records(ws)
    tenant = str(uuid4())
    session = ws["auth"].sessions.get("admin-session")
    ws["auth"].sessions.put(replace(session, active_tenant_id=tenant))
    ws["auth"].memberships.put(MembershipRecord(tenant, "admin-user", "reviewer", "active"))
    assert ws["client"].get(first.json()["status_url"]).status_code == 404
    assert rescore_post(ws).status_code == 404
    assert records(ws) == before


@pytest.mark.parametrize("operation", ["UPDATE", "DELETE", "REPLACE"])
def test_rescore_receipt_is_append_only_at_database_boundary(tmp_path, operation):
    import sqlite3

    ws = rescore_workspace(tmp_path)
    assert rescore_post(ws).status_code == 202
    with sqlite3.connect(ws["runs"].path) as db, pytest.raises(sqlite3.IntegrityError):
        if operation == "UPDATE":
            db.execute("UPDATE job_records SET value='{}' WHERE kind='rescore_artifact'")
        elif operation == "DELETE":
            db.execute("DELETE FROM job_records WHERE kind='rescore_artifact'")
        else:
            db.execute(
                "INSERT OR REPLACE INTO job_records SELECT * FROM job_records "
                "WHERE kind='rescore_artifact'"
            )


def test_inflight_active_pack_change_prevents_ready_receipt(tmp_path):
    ws = rescore_workspace(tmp_path)
    before = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])

    def load(tenant, run, claim):
        # Real pointer move represents concurrent activation/deactivation.
        with ws["runs"].jobs._transaction() as db:
            db.execute("DELETE FROM rulepack_active_pointers WHERE tenant_id=?", (tenant,))
        return ws["inputs"]

    ws["service"].load_inputs = load
    assert rescore_post(ws).status_code in (404, 412)
    assert ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"]) == before
    assert all(kind != "rescore_artifact" for kind, _, _ in records(ws))


def test_real_local_tag_pipeline_uncollected_applicability_requires_retag(tmp_path, monkeypatch):
    from proofops.adapters.local.rescore_store import LocalSQLiteRescoreStore
    from proofops.application.authorization import AuthContext
    from proofops.application.rescores import RescoreRejected, RescoreService

    from tests.integration.test_local_tag_runner import verified_setup

    runs, run_id, runner, _, _ = verified_setup(tmp_path, monkeypatch)
    tenant = "11111111-1111-4111-8111-111111111111"
    assert runner.run_once(tenant_id=tenant, run_id=run_id) == "needs_review"
    claim = runner.claims.list(tenant, run_id)[0].claim_id
    inputs = runner.tags.load_inputs(tenant, run_id, claim)
    actor = AuthContext(
        "synthetic-reviewer", tenant, "reviewer", frozenset({"viewer", "reviewer"}), "s"
    )
    reviews = runner.reviews
    with runs.store.jobs._transaction() as db:
        review = runs.store.jobs._all(db, tenant, run_id, "review_head")[0]
    body = dict(
        base_tag_revision=1,
        track="performance",
        reason="Synthetic original review",
        elements=[asdict(e) for e in inputs.consensus.candidate_elements],
    )
    assert (
        reviews.resolve_review(actor, review["review_id"], body, '"1"', "real-pipeline-review-key")[
            "new_tag_revision"
        ]
        == 2
    )
    before = runner.reviews.store.history(tenant, run_id, claim)
    service = RescoreService(
        LocalSQLiteRescoreStore(runs.store), load_inputs=runner.tags.load_inputs
    )
    with pytest.raises(RescoreRejected) as failure:
        service.create_rescore(
            actor,
            run_id,
            dict(rule_pack_id=inputs.rulepack.rule_pack_id, reason="Synthetic rules-only rescore"),
            "real-pipeline-rescore-key",
        )
    assert failure.value.code == "RETAG_REQUIRED"
    assert runner.reviews.store.history(tenant, run_id, claim) == before
    assert len(runner.transport.requests) == 3


@pytest.mark.parametrize("pin", ["model_sha256", "prompt_sha256"])
def test_saved_revision_must_keep_original_replica_model_and_prompt_pins(tmp_path, pin):
    ws = rescore_workspace(tmp_path, pin=pin)
    before = records(ws)
    response = rescore_post(ws)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RESCORE_INPUT_MISMATCH"
    assert records(ws) == before


def test_mounted_rescore_metadata_matches_fixed_operation_contract(tmp_path):
    import json

    ws = rescore_workspace(tmp_path)
    api = ws["client"].app.openapi()
    operations = json.load(open("contracts/api_operations.json"))
    for contract in (
        item for item in operations if item["operation"] in {"rescore", "rescore_get"}
    ):
        mounted = api["paths"][contract["path"]][contract["method"].lower()]
        assert mounted["operationId"] == contract["operation"]
        assert mounted["x-minimum-role"] == contract["role"]
        assert mounted["x-idempotency-required"] == contract["idempotency"]
        assert str(contract["success"]) in mounted["responses"]


def _unapproved_target(ws, monkeypatch):
    capture = ws["service"].store.capture

    def unapproved(*args, **kwargs):
        captured = capture(*args, **kwargs)
        if "target_pack" in captured:
            captured["target_pack"] = captured["target_pack"] | dict(
                approved_by=None, approved_at=None
            )
        return captured

    monkeypatch.setattr(ws["service"].store, "capture", unapproved)
    return ws


def test_real_run_rescore_is_allowed_only_under_an_approved_active_target(tmp_path):
    ws = rescore_workspace(tmp_path, real=True)
    response = rescore_post(ws)
    assert response.status_code == 202, response.text
    after = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])
    decision = after["decisions"][-1]["decision"]
    assert decision["rule_pack_sha256"] == ws["target"].sha256
    assert decision["local_synthetic"] is False


def test_real_run_rescore_rejects_unapproved_target_without_writing(tmp_path, monkeypatch):
    ws = _unapproved_target(rescore_workspace(tmp_path, real=True), monkeypatch)
    before = records(ws)
    response = rescore_post(ws)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RULEPACK_APPROVAL_REQUIRED"
    assert records(ws) == before


@pytest.mark.parametrize("origin", ["human", "ai_delegated"])
def test_reviewed_track_correction_is_rescored_under_the_reviewed_track(tmp_path, origin):
    ws = rescore_workspace(tmp_path, track="goal", origin=origin)
    response = rescore_post(ws)
    assert response.status_code == 202, response.text
    after = ws["review"].store.history(ws["tenant"], ws["run"], ws["claim"])
    assert after["decisions"][-1]["decision"]["rule_pack_sha256"] == ws["target"].sha256


def test_unreviewed_tag_cannot_change_track_through_rescore(tmp_path):
    ws = rescore_workspace(tmp_path, track="goal")
    before = records(ws)
    response = rescore_post(ws)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RESCORE_INPUT_MISMATCH"
    assert records(ws) == before
