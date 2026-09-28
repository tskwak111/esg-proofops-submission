"""Synthetic store-boundary revisions: real pure rules, SQLite and HTTP reads.

The trusted review builder below supplies explicit synthetic confirmed facts;
HTTP/source guards are covered by test_reviews, not bypassed in production.
"""

import json
import sqlite3
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from proofops.adapters.local.rescore_store import LocalSQLiteRescoreStore
from proofops.adapters.local.summary_store import LocalSummaryStore
from proofops.application.authorization import AuthContext, MembershipRecord
from proofops.application.evidence.citations import verify_source_ref
from proofops.application.rescores import RescoreRejected, create_rescore
from proofops.application.reviews import ReviewRejected
from proofops.application.rulepacks import RulePackRecord
from proofops.domain.audit import AuditConflict
from proofops.domain.provenance import canonical_hash
from proofops.domain.rules.engine import Decision, evaluate
from proofops_api.routers.summaries import build_summaries_router

from tests.acceptance.test_rescore import revised_pack
from tests.acceptance.test_rules import inputs
from tests.integration.test_local_tag_runner import TENANT, tag_message, verified_setup
from tests.integration.test_run_lifecycle import client, validate


def workspace(tmp_path, monkeypatch, *, configure_runner=None):
    service, run_id, runner, now, _ = verified_setup(tmp_path, monkeypatch)
    if configure_runner is not None:
        configure_runner(runner, run_id)
    checkpoint = tag_message(service, run_id, now[0])
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    jobs = service.store.jobs
    with jobs._transaction() as db:
        review = jobs._all(db, TENANT, run_id, "review_head")[0]
    original = runner.tags.load_inputs(TENANT, run_id, review["claim_id"])
    synthetic, _ = inputs()
    tags = replace(
        synthetic,
        document_version_id=original.original.document_version_id,
        claim_id=review["claim_id"],
        packet_sha256=original.packet.packet_sha256,
        tag_revision=2,
        facts=tuple(
            replace(
                f,
                evidence_refs=tuple(
                    verify_source_ref(ref, original.original, tenant_id=TENANT)
                    for ref in original.context.claim.source_refs
                ),
            )
            if f.state == "present"
            else f
            for f in synthetic.facts
        ),
    )
    http, auth = client(service)
    http.app.include_router(
        build_summaries_router(LocalSummaryStore(service.store, runner.claims), auth)
    )
    actor = AuthContext("synthetic-reviewer", TENANT, "reviewer", frozenset({"reviewer"}), "test")
    return dict(
        service=service,
        jobs=jobs,
        run=run_id,
        runner=runner,
        review=review,
        original=original,
        tags=tags,
        http=http,
        auth=auth,
        actor=actor,
        checkpoint=checkpoint,
    )


def resolve(ws, *, expected=1, actor=None):
    def build(review, initial, revision):
        decision = evaluate(
            ws["tags"],
            replace(ws["original"].rule_context, decision_revision=revision),
            ws["original"].rulepack,
        )
        assert decision.decision_status == "decided" and decision.evidence_grade == "E3"
        return (
            initial | {"tag_revision": 2, "confirmed_tags": asdict(ws["tags"]), "origin": "human"},
            dict(
                decision_revision=revision,
                decision=asdict(decision),
                api=decision.to_api_dict() | {"review_status": "human_confirmed"},
            ),
        )

    return ws["runner"].reviews.store.resolve(
        actor or ws["actor"],
        ws["review"]["review_id"],
        dict(base_tag_revision=1, reason="Explicit synthetic reviewed facts"),
        expected,
        str(uuid4()),
        build,
    )


def prepare_rescore(ws):
    # Synthetic pack deliberately has no matching branch: exercise a real rule gap.
    target = revised_pack(
        edit=lambda files: files["rubric/performance.yaml"].update(branches=[]),
        rule_pack_id=str(uuid4()),
        status="validated",
        approved_by="synthetic-fixture",
        approved_at="2026-09-08T10:00:00Z",
    )
    runs = ws["service"].store
    runs.rulepacks.add_pack(
        RulePackRecord(**{k: v for k, v in asdict(target).items() if k != "content"}),
        {p: target.file_content(p) for p in target.files},
    )
    runs.rulepacks.activate(
        tenant_id=TENANT,
        rule_pack_id=target.rule_pack_id,
        expected_revision=1,
        idempotency_key=str(uuid4()),
        actor="synthetic-fixture",
        reason="Synthetic gap test",
        gap_ids=target.unresolved_gap_ids,
        now=1,
    )
    store = LocalSQLiteRescoreStore(runs)
    body = dict(rule_pack_id=target.rule_pack_id, reason="Synthetic rules-only revision")
    captured = store.capture(ws["actor"], ws["run"], body, str(uuid4()), None)
    context = replace(ws["original"].rule_context, decision_revision=2)
    previous = evaluate(ws["tags"], replace(context, decision_revision=1), ws["original"].rulepack)
    decision = create_rescore(
        target,
        ws["tags"],
        previous_pack=ws["original"].rulepack,
        context=context,
        previous_decision=previous,
    )
    assert isinstance(decision, Decision)
    assert decision.decision_status == "blocked_rule_gap" and decision.evidence_grade is None
    saved_tag = captured["claims"][ws["review"]["claim_id"]]["tag"]
    prepared = {
        ws["review"]["claim_id"]: dict(
            decision_revision=2,
            decision=asdict(decision),
            api=decision.to_api_dict() | {"review_status": "human_confirmed"},
            input_snapshot_sha256=canonical_hash(saved_tag["inputs"]),
            tag_record_sha256=canonical_hash(saved_tag),
        )
    }
    return store, body, captured, prepared


def read(ws, decided):
    run = ws["http"].get(f'/v1/runs/{ws["run"]}')
    assert run.status_code == 200, run.text
    run = run.json()
    validate("Run", run)
    assert run["coverage"]["claims_decided"] == decided
    assert run["coverage"]["claims_needs_review"] == 1 - decided
    summary = ws["http"].get(f'/v1/runs/{ws["run"]}/summary')
    assert summary.status_code == 200, summary.text
    summary = summary.json()
    validate("Summary", summary)
    assert summary["coverage"] == run["coverage"]
    assert sum(summary["grade_counts"].values()) == decided
    assert summary["undecided_count"] == 1 - decided
    assert summary["snapshot_epoch"] == run["mutation_epoch"]
    return run, summary


def records(ws):
    with ws["jobs"]._transaction() as db:
        return (
            db.execute(
                "SELECT * FROM job_records ORDER BY tenant_id,run_id,kind,record_id"
            ).fetchall(),
            db.execute("SELECT * FROM audit_events ORDER BY tenant_id,run_id,sequence").fetchall(),
            db.execute("SELECT * FROM audit_heads ORDER BY tenant_id,run_id").fetchall(),
        )


@pytest.mark.parametrize("mutation", ["review", "rescore"])
def test_current_counts_follow_real_engine_revisions(tmp_path, monkeypatch, mutation):
    ws = workspace(tmp_path, monkeypatch)
    before, _ = read(ws, 0)
    frozen = ws["jobs"].read_checkpoint(ws["checkpoint"])
    resolve(ws)
    if mutation == "rescore":
        store, body, captured, prepared = prepare_rescore(ws)
        store.commit(ws["actor"], ws["run"], body, captured, prepared)
    after, _ = read(ws, 1 if mutation == "review" else 0)
    counts = {"claims_decided", "claims_needs_review"}
    assert {k: v for k, v in after["coverage"].items() if k not in counts} == {
        k: v for k, v in before["coverage"].items() if k not in counts
    }
    assert after["status"] == before["status"] == "partial"
    assert after["coverage"]["complete"] is False
    assert ws["jobs"].read_checkpoint(ws["checkpoint"]) == frozen
    assert json.loads(frozen)["claims"][0]["decision"] is None
    history = ws["runner"].reviews.store.history(TENANT, ws["run"], ws["review"]["claim_id"])
    assert history["tags"][0]["confirmed_tags"] is None
    assert history["decisions"][0]["decision"]["evidence_grade"] == "E3"
    assert len(ws["runner"].transport.requests) == 3


@pytest.mark.parametrize("mutation", ["review", "rescore"])
@pytest.mark.parametrize("failure", ["stale", "audit", "tenant"])
def test_rejected_mutations_keep_counts_heads_and_audit(tmp_path, monkeypatch, mutation, failure):
    ws = workspace(tmp_path, monkeypatch)
    if mutation == "rescore":
        resolve(ws)
        store, body, captured, prepared = prepare_rescore(ws)
    actor = ws["actor"]
    if failure == "audit":
        with sqlite3.connect(ws["jobs"].path) as db:
            db.execute(
                "CREATE TRIGGER reject_revision_audit BEFORE INSERT ON audit_events "
                "BEGIN SELECT RAISE(ABORT, 'synthetic audit failure'); END"
            )
    elif failure == "tenant":
        actor = replace(actor, tenant_id=str(uuid4()))
    elif mutation == "rescore":
        captured["run"]["revision"] -= 1
    before = records(ws)
    with pytest.raises((ReviewRejected, RescoreRejected, AuditConflict)) as error:
        if mutation == "review":
            resolve(ws, expected=9 if failure == "stale" else 1, actor=actor)
        else:
            store.commit(actor, ws["run"], body, captured, prepared)
    if failure != "audit":
        assert error.value.status == (404 if failure == "tenant" else 412)
    assert records(ws) == before
    read(ws, int(mutation == "rescore"))
    if failure == "tenant":
        auth = ws["auth"]
        auth.memberships.put(MembershipRecord(actor.tenant_id, "admin-user", "reviewer", "active"))
        auth.sessions.put_with_token(
            replace(auth.sessions.get("admin-session"), active_tenant_id=actor.tenant_id),
            ws["http"].headers["X-CSRF-Token"],
        )
        for path in (f'/v1/runs/{ws["run"]}', f'/v1/runs/{ws["run"]}/summary'):
            assert ws["http"].get(path).status_code == 404


def test_refresh_preserves_unheaded_unprocessed_and_unreadable_counts(tmp_path, monkeypatch):
    ws = workspace(tmp_path, monkeypatch)
    jobs, run_id, claim_id = ws["jobs"], ws["run"], ws["review"]["claim_id"]
    context = replace(ws["original"].rule_context, decision_revision=1)
    decided = evaluate(ws["tags"], context, ws["original"].rulepack)
    with jobs._transaction() as db:
        run = jobs._get(db, TENANT, run_id, "run", "META")
        # Explicit partial fixture: one headed claim, two blocked unheaded claims,
        # one unprocessed claim and unreadable/unprocessed document regions.
        run["coverage"].update(
            pages_total=3,
            pages_processed=1,
            pages_unreadable=1,
            pages_unprocessed=1,
            chunks_discovered=4,
            chunks_processed=3,
            claims_discovered=4,
            claims_decided=0,
            claims_needs_review=3,
            full_scope=False,
        )
        jobs._put(db, TENANT, run_id, "run", "META", run)
        before = json.loads(json.dumps(run))
        # Deliberate same IDs in a foreign tenant and another run: neither can
        # enter the aggregate, and joins must bind both halves to the same scope.
        for tenant, run in ((str(uuid4()), run_id), (TENANT, str(uuid4()))):
            jobs.create_run_transaction(db, tenant, run, context.document_version_id)
            jobs._put(
                db, tenant, run, "claim_head", claim_id, dict(tag_revision=1, decision_revision=1)
            )
            jobs._put(
                db,
                tenant,
                run,
                "decision_revision",
                f"{claim_id}:0000000001",
                dict(decision_revision=1, decision=asdict(decided), api=decided.to_api_dict()),
                immutable=True,
            )
        foreign_before = db.execute(
            "SELECT * FROM job_records WHERE tenant_id!=? OR run_id!=? ORDER BY 1,2,3,4",
            (TENANT, run_id),
        ).fetchall()
    resolve(ws)
    after = ws["http"].get(f"/v1/runs/{run_id}").json()
    assert after["coverage"] == before["coverage"] | {
        "claims_decided": 1,
        "claims_needs_review": 2,
    }
    assert after["status"] == before["status"] == "partial"
    with jobs._transaction() as db:
        assert (
            db.execute(
                "SELECT * FROM job_records WHERE tenant_id!=? OR run_id!=? ORDER BY 1,2,3,4",
                (TENANT, run_id),
            ).fetchall()
            == foreign_before
        )
