"""AT-019: synthetic source/transport, real HTTP, SQLite, guards and rules."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from uuid import uuid4

import jsonschema
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.adapters.local.job_store import LocalSQLiteJobStore
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.application.authorization import AuthContext, MembershipRecord
from proofops.application.evidence.retrieval import retrieve_evidence
from proofops.domain.rules.engine import RuleContext
from proofops_api.auth import AuthStore

from tests.acceptance.test_citations import RUN, TENANT
from tests.acceptance.test_retrieval import SyntheticSearch
from tests.acceptance.test_rulepack_api import _headers, _seed_auth
from tests.acceptance.test_tagging import consensus, execute, setup


def workspace(tmp_path, *, role="reviewer", prepare_inputs=None):
    try:
        from proofops.adapters.local.review_store import LocalSQLiteReviewStore
        from proofops.application.reviews import ReviewInputs, ReviewService
        from proofops_api.routers.reviews import build_reviews_router
    except ImportError as exc:
        pytest.fail(f"Review implementation missing: {exc}")
    tagging = setup(tmp_path)
    receipts = execute(tagging)
    result = consensus(receipts, tagging)
    original_packet = retrieve_evidence(
        tagging["context"].claim,
        tagging["original"],
        SyntheticSearch(tagging["original"]),
        tenant_id=TENANT,
        run_id=RUN,
        index_generation="synthetic-v1",
        rulepack=tagging["rulepack"],
        document_context={},
        token_counter=lambda text: len(text) // 4,
    )
    inputs = ReviewInputs(
        run_id=RUN,
        context=tagging["context"],
        original=tagging["original"],
        rulepack=tagging["rulepack"],
        packet=tagging["packet"],
        original_packet=original_packet,
        tag_runs=receipts,
        consensus=result,
        relation_tags=tagging["relation_tags"],
        rule_context=RuleContext(
            TENANT,
            tagging["original"].document_version_id,
            tagging["context"].claim.claim_id,
            tagging["packet"].packet_sha256,
            local_synthetic=True,
        ),
    )
    if prepare_inputs is not None:
        inputs = prepare_inputs(inputs)
    jobs = LocalSQLiteJobStore(tmp_path / "state.sqlite")
    jobs.create_run(TENANT, RUN, inputs.original.document_version_id)
    store = LocalSQLiteReviewStore(jobs)
    service = ReviewService(store, load_inputs=lambda tenant, run, claim: inputs)
    review = service.publish(inputs)
    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    token = _seed_auth(auth, role=role)
    app = FastAPI()
    app.include_router(
        build_reviews_router(
            service,
            auth,
            allowed_origin="http://testserver",
            run_store=LocalSQLiteRunStore(jobs.path),
        )
    )
    client = TestClient(app)
    client.cookies.set("__Host-proofops_session", "admin-session")
    # Existing session cookie spelling is authoritative.
    from proofops_api.auth import SESSION_COOKIE_NAME

    client.cookies.set(SESSION_COOKIE_NAME, "admin-session")
    body = dict(
        base_tag_revision=1,
        track="performance",
        reason="원문 근거 재검토",
        elements=[asdict(e) for e in result.candidate_elements],
    )
    return client, service, inputs, review, body, _headers(token), auth


def post(ws, *, body=None, headers=None):
    client, _, _, review, default_body, default_headers, _ = ws
    return client.post(
        f'/v1/reviews/{review["review_id"]}/resolve',
        json=body or default_body,
        headers=headers or default_headers,
    )


def re_review(ws, *, body=None, headers=None):
    client, _, _, review, default_body, default_headers, _ = ws
    return client.post(
        f'/v1/reviews/{review["review_id"]}/re-review',
        json=body or default_body,
        headers=headers or default_headers,
    )


def test_resolve_new_immutable_revisions_real_engine_and_pinned_receipts(tmp_path):
    ws = workspace(tmp_path)
    _, service, inputs, review, _, _, _ = ws
    before = service.store.history(TENANT, RUN, review["claim_id"])
    response = post(ws)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["review"]["status"] == "resolved"
    assert result["review"]["revision"] == result["new_tag_revision"] == 2
    assert response.headers["etag"] == '"2"'
    assert result["decision"]["review_status"] == "human_confirmed"
    assert result["decision"]["decision_status"] == "blocked_rule_gap"
    assert result["decision"]["label"] is None
    after = service.store.history(TENANT, RUN, review["claim_id"])
    assert after["tags"][0] == before["tags"][0]
    assert len(after["tags"]) == 2 and len(after["decisions"]) == 1
    assert after["tags"][0]["confirmed_tags"] is None
    assert after["tags"][0]["inputs"]["tag_runs"] == json.loads(
        json.dumps([asdict(r) for r in inputs.tag_runs])
    )
    assert after["tags"][1]["confirmed_tags"]["replicate_hashes"] == list(
        inputs.consensus.replicate_hashes
    )
    assert service.store.jobs.get_run(TENANT, RUN)["mutation_epoch"] == 2
    from proofops.adapters.local.audit_store import LocalSQLiteAuditStore

    events = LocalSQLiteAuditStore(service.store.jobs.path).events(TENANT, RUN)
    assert events[-1].actor_sub == "admin-user" and events[-1].reason == ws[4]["reason"]
    assert events[-1].before_hash and events[-1].after_hash
    schema = json.load(open("contracts/jsonschema/api_models.schema.json"))
    jsonschema.Draft202012Validator(
        {"$ref": "#/$defs/ReviewResolution", "$defs": schema["$defs"]}
    ).validate(result)


def test_partial_fact_publication_pins_profile_and_range(tmp_path):
    from proofops.application.tagging.consensus import (
        PARTIAL_FACTS_HASH,
        PARTIAL_FACTS_V1,
        form_consensus,
    )
    from proofops.domain.rules.engine import evaluate

    def prepare(inputs):
        runs = tuple(replace(run, product_variant=False) for run in inputs.tag_runs)
        consensus = form_consensus(
            runs,
            packet=inputs.packet,
            rulepack=inputs.rulepack,
            tenant_id=TENANT,
            tag_revision=1,
            profile=PARTIAL_FACTS_V1,
        )
        draft = replace(
            inputs, tag_runs=runs, consensus=consensus, fact_assembly_profile=PARTIAL_FACTS_V1
        )
        return replace(
            draft, decision=evaluate(consensus.confirmed_tags, draft.rule_context, draft.rulepack)
        )

    ws = workspace(tmp_path, prepare_inputs=prepare)
    _, service, inputs, review, _, _, _ = ws
    history = service.store.history(TENANT, RUN, review["claim_id"])
    profile = {"profile": PARTIAL_FACTS_V1, "sha256": PARTIAL_FACTS_HASH}
    assert history["tags"][0]["fact_assembly"] == profile
    assert history["decisions"][0]["fact_assembly"] == profile
    assert history["decisions"][0]["api"]["grade_range"] == {
        "floor": "E1",
        "ceiling": "E3",
        "open_elements": ["P2", "P3", "P4"],
    }
    assert inputs.decision.review_status == "needs_review"
    body = ws[4] | {"elements": [asdict(e) for e in inputs.consensus.candidate_elements]}
    assert post(ws, body=body).status_code == 200
    reviewed = service.store.history(TENANT, RUN, review["claim_id"])
    assert reviewed["tags"][-1]["fact_assembly"] == profile
    assert reviewed["decisions"][-1]["fact_assembly"] == profile


@pytest.mark.parametrize("header,base", [('"9"', 1), ('"1"', 9)])
def test_stale_review_or_tag_returns_412_without_writes(tmp_path, header, base):
    ws = workspace(tmp_path)
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    response = post(
        ws, body=ws[4] | {"base_tag_revision": base}, headers=ws[5] | {"If-Match": header}
    )
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "STALE_REVIEW_REVISION"
    assert ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]) == before


@pytest.mark.parametrize("field", ["label", "evidence_grade", "tenant_id"])
def test_direct_result_or_tenant_input_is_422(tmp_path, field):
    ws = workspace(tmp_path)
    assert post(ws, body=ws[4] | {field: "SUBSTANTIATED"}).status_code == 422


@pytest.mark.parametrize(
    "mutation",
    [
        "no_source",
        "forged_quote",
        "foreign_document",
        "absent",
        "not_applicable",
        "duplicate",
        "numeric_check",
        "assurance",
    ],
)
def test_untrusted_elements_never_gain_verified_facts(tmp_path, mutation):
    ws = workspace(tmp_path)
    body = json.loads(json.dumps(ws[4]))
    e = body["elements"][0]
    if mutation == "no_source":
        e["evidence_refs"] = []
    elif mutation == "forged_quote":
        e["evidence_refs"][0]["quote"] = "999%"
    elif mutation == "foreign_document":
        e["evidence_refs"][0]["document_version_id"] = str(uuid4())
    elif mutation in ("absent", "not_applicable"):
        body["elements"][1]["state"] = mutation
    elif mutation == "duplicate":
        body["elements"].append(e)
    else:
        body["elements"][-1 if mutation == "numeric_check" else 3] = e | {
            "element_id": "P6" if mutation == "numeric_check" else "P4",
            "normalized_value": "covered" if mutation == "assurance" else "consistent",
        }
    assert post(ws, body=body).status_code == 422
    assert len(ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])["tags"]) == 1


def test_same_key_replay_and_changed_body_conflict(tmp_path):
    ws = workspace(tmp_path)
    first = post(ws)
    assert first.status_code == 200
    assert post(ws).json() == first.json()
    assert post(ws, body=ws[4] | {"reason": "다른 변경 사유"}).status_code == 409
    assert post(ws, headers=ws[5] | {"Idempotency-Key": "different-request-key"}).status_code == 412


def test_two_competing_reviewers_exactly_one_wins(tmp_path):
    ws = workspace(tmp_path)
    service, review, body = ws[1], ws[3], ws[4]
    from proofops.application.reviews import ReviewRejected

    def resolve(index):
        actor = AuthContext(
            f"reviewer-{index}", TENANT, "reviewer", frozenset({"viewer", "reviewer"}), str(index)
        )
        try:
            service.resolve_review(
                actor, review["review_id"], body, '"1"', f"concurrent-key-{index:04}"
            )
            return 200
        except ReviewRejected as error:
            return error.status

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(resolve, range(2))) == [200, 412]
    assert len(service.store.history(TENANT, RUN, review["claim_id"])["tags"]) == 2


def test_audit_failure_rolls_back_all_writes(tmp_path):
    ws = workspace(tmp_path)
    with sqlite3.connect(ws[1].store.jobs.path) as db:
        db.execute(
            "CREATE TRIGGER reject_review_audit BEFORE INSERT ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'injected disk write failure'); END"
        )
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    assert post(ws).status_code == 409
    assert ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]) == before
    assert ws[1].store.jobs.get_run(TENANT, RUN)["mutation_epoch"] == 1


@pytest.mark.parametrize("role", ["viewer", "editor"])
def test_nonreviewer_forbidden(tmp_path, role):
    assert post(workspace(tmp_path, role=role)).status_code == 403


def test_cross_tenant_hidden_and_csrf_required(tmp_path):
    ws = workspace(tmp_path)
    assert post(ws, headers=ws[5] | {"X-CSRF-Token": "wrong"}).status_code == 403
    auth = ws[-1]
    other = str(uuid4())
    auth.memberships.put(MembershipRecord(other, "admin-user", "reviewer", "active"))
    auth.sessions.put_with_token(
        replace(auth.sessions.get("admin-session"), active_tenant_id=other), ws[5]["X-CSRF-Token"]
    )
    assert post(ws).status_code == 404


def test_revisions_immutable_and_replay_survives_store_reopen(tmp_path):
    ws = workspace(tmp_path)
    assert post(ws).status_code == 200
    jobs = ws[1].store.jobs
    with sqlite3.connect(jobs.path) as db:
        for sql in (
            "UPDATE job_records SET value='{}' WHERE kind='tag_revision'",
            "DELETE FROM job_records WHERE kind='decision_revision'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(sql)
    from proofops.adapters.local.review_store import LocalSQLiteReviewStore

    ws[1].store = LocalSQLiteReviewStore(LocalSQLiteJobStore(jobs.path))
    assert post(ws).status_code == 200


def test_initial_publication_joins_caller_transaction_without_epoch_bump(tmp_path):
    ws = workspace(tmp_path)
    from proofops.adapters.local.review_store import LocalSQLiteReviewStore
    from proofops.application.reviews import ReviewService

    jobs = LocalSQLiteJobStore(tmp_path / "outer.sqlite")
    jobs.create_run(TENANT, RUN, ws[2].original.document_version_id)
    service = ReviewService(LocalSQLiteReviewStore(jobs), load_inputs=lambda *args: ws[2])
    with pytest.raises(RuntimeError):
        with jobs._transaction() as db:
            service.publish_transaction(db, ws[2])
            raise RuntimeError("parent tag checkpoint failed")
    assert service.store.history(TENANT, RUN, ws[3]["claim_id"])["tags"] == []
    assert jobs.get_run(TENANT, RUN)["mutation_epoch"] == 0
    with jobs._transaction() as db:
        service.publish_transaction(db, ws[2])
    assert jobs.get_run(TENANT, RUN)["mutation_epoch"] == 0
    from proofops.adapters.local.audit_store import LocalSQLiteAuditStore

    assert len(LocalSQLiteAuditStore(jobs.path).events(TENANT, RUN)) == 1


def test_review_http_rate_limit_and_required_headers(tmp_path):
    ws = workspace(tmp_path, role="admin")
    assert post(ws, headers=ws[5] | {"If-Match": "1"}).status_code == 400
    assert post(ws, headers=ws[5] | {"Idempotency-Key": "short"}).status_code == 400
    for _ in range(8):
        assert post(ws, body=ws[4] | {"label": "E3"}).status_code == 422
    assert post(ws).status_code == 429


def test_review_queue_reads_scoped_rows_and_contract(tmp_path):
    ws = workspace(tmp_path)
    response = ws[0].get(f"/v1/runs/{RUN}/reviews?limit=1")
    assert response.status_code == 200
    assert response.json() == {"items": [ws[3]], "next_cursor": None, "snapshot_epoch": 1}
    assert ws[0].get(f"/v1/runs/{RUN}/reviews?cursor=forged").status_code == 400
    assert ws[0].get(f"/v1/runs/{uuid4()}/reviews").status_code == 404
    operation = ws[0].app.openapi()["paths"]["/v1/reviews/{review_id}/resolve"]["post"]
    assert operation["requestBody"]["required"] is True
    assert any(p["name"] == "If-Match" and p["required"] for p in operation["parameters"])


def test_two_http_clients_cannot_overwrite_same_revision(tmp_path):
    ws = workspace(tmp_path)

    def submit(index):
        with TestClient(ws[0].app) as client:
            client.cookies.update(ws[0].cookies)
            return client.post(
                f'/v1/reviews/{ws[3]["review_id"]}/resolve',
                json=ws[4],
                headers=ws[5] | {"Idempotency-Key": f"http-race-key-{index:04}"},
            ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(submit, range(2))) == [200, 412]


def test_idempotency_key_cannot_be_reused_for_another_run(tmp_path):
    ws = workspace(tmp_path)
    assert post(ws).status_code == 200
    jobs = ws[1].store.jobs
    other_run, other_review = str(uuid4()), str(uuid4())
    jobs.create_run(TENANT, other_run, ws[2].original.document_version_id)
    with jobs._transaction() as db:
        # Synthetic second review locator; rejection precedes loading any tagging input.
        jobs._put(
            db,
            TENANT,
            other_run,
            "review_head",
            other_review,
            ws[3] | {"run_id": other_run, "review_id": other_review},
        )
    response = ws[0].post(f"/v1/reviews/{other_review}/resolve", json=ws[4], headers=ws[5])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_review_component_renders_actual_react_with_escaped_sources(tmp_path):
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    esbuild = next((root / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    entry = tmp_path / "review-render.tsx"
    entry.write_text(
        r"""
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { ReviewWorkspace } from COMPONENT;
import assert from "node:assert/strict";
const ref = {source_id:"source", char_start:0,char_end:8,page_num:1,
quote:"<script>alert(1)</script>"};
const props = {session:{tenant_id:"tenant",user_id:"user",role:"reviewer"},
review:{review_id:"review",run_id:"run",claim_id:"claim",status:"open",revision:1,base_tag_revision:1,reason_codes:[]},
track:"performance",elements:[{element_id:"P1",state:"unknown",evidence_refs:[ref],normalized_value:null}],
loadLatest:async()=>{},onResolved:()=>{},onSourceOpen:()=>{},localSynthetic:true};
const html=renderToStaticMarkup(React.createElement(ReviewWorkspace,props));
assert.ok(html.includes("태깅 검토") && html.includes("변경 사유"));
assert.ok(html.includes("&lt;script&gt;") && !html.includes("<script>"));
assert.ok(html.includes("로컬 합성 자료") && html.includes("태깅 확정 및 재채점"));
const empty=renderToStaticMarkup(React.createElement(ReviewWorkspace,{...props,elements:[]}));
for (const id of ["P1","P2","P3","P4","P5","P6"])
  assert.ok(empty.includes(`name="${id}"`) || empty.includes(id), `missing ${id}`);
const viewer=renderToStaticMarkup(React.createElement(ReviewWorkspace,
{...props,session:{...props.session,role:"viewer"}}));
for (const readOnly of [viewer, renderToStaticMarkup(React.createElement(ReviewWorkspace,
{...props,localSynthetic:false,
 review:{...props.review,reason_codes:["RULEPACK_APPROVAL_REQUIRED"]}}))]) {
  assert.ok(!readOnly.includes('fieldset disabled=""'),
    "read-only source buttons must not inherit disabled");
  assert.ok(readOnly.includes('<button type="button">원문 위치 열기</button>'));
  for (const control of readOnly.match(/<(?:select|input|textarea)\b[^>]*>/g) ?? [])
    assert.ok(control.includes('disabled=""'), "tag edits remain disabled");
  assert.ok(/<button[^>]*disabled=""[^>]*>변경 확인<\/button>/.test(readOnly));
}
// A resolved review shows an explicit re-review action (reviewer), never an
// auto-open editor; it references the current head tag revision.
const resolvedProps={...props,review:{...props.review,status:"resolved",revision:2},
headTagRevision:2};
const resolved=renderToStaticMarkup(React.createElement(ReviewWorkspace,resolvedProps));
assert.ok(resolved.includes("재검토 시작") && resolved.includes("이미 확정"));
// A viewer never sees the re-review action for a resolved review.
const resolvedViewer=renderToStaticMarkup(React.createElement(ReviewWorkspace,
{...resolvedProps,session:{...props.session,role:"viewer"}}));
assert.ok(!resolvedViewer.includes("재검토 시작"));
console.log("ReviewWorkspace React render/accessibility/text-escaping checks passed");
""".replace("REACT", json.dumps(str(root / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(root / "apps/web/node_modules/react-dom/server.node.js")))
        .replace(
            "COMPONENT", json.dumps(str(root / "apps/web/src/features/reviews/ReviewWorkspace.tsx"))
        )
    )
    bundle = tmp_path / "review-render.cjs"
    subprocess.run(
        [
            str(esbuild),
            str(entry),
            "--bundle",
            "--platform=node",
            "--format=cjs",
            "--jsx=automatic",
            f"--outfile={bundle}",
        ],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(["node", str(bundle)], check=True, capture_output=True, text=True)
    assert "checks passed" in result.stdout


def test_queue_cursor_retains_original_rows_after_resolve(tmp_path):
    ws = workspace(tmp_path)
    jobs = ws[1].store.jobs
    second_id = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    original = ws[3] | {"review_id": second_id}
    with jobs._transaction() as db:
        jobs._put(db, TENANT, RUN, "review_head", second_id, original)
    first = ws[0].get(f"/v1/runs/{RUN}/reviews?limit=1").json()
    assert first["next_cursor"] is not None
    assert post(ws).status_code == 200
    with jobs._transaction() as db:
        jobs._put(db, TENANT, RUN, "review_head", second_id, original | {"status": "resolved"})
    second = ws[0].get(
        f"/v1/runs/{RUN}/reviews", params={"limit": 1, "cursor": first["next_cursor"]}
    )
    assert second.status_code == 200
    assert second.json()["items"] == [original]
    assert second.json()["snapshot_epoch"] == first["snapshot_epoch"]


def test_durable_input_loader_runs_before_write_transaction(tmp_path):
    ws = workspace(tmp_path)

    def load_inputs(tenant, run, claim):
        assert ws[1].store.jobs.get_run(tenant, run)["run_id"] == RUN
        return ws[2]

    ws[1].load_inputs = load_inputs
    assert post(ws).status_code == 200


def test_live_unapproved_rulepack_cannot_publish_human_grade(tmp_path):
    from proofops.application.reviews import ReviewRejected

    _, service, inputs, review, body, _, _ = workspace(tmp_path)
    inputs = replace(
        inputs,
        rule_context=replace(inputs.rule_context, local_synthetic=False),
        rulepack=replace(inputs.rulepack, status="draft", approved_by=None, approved_at=None),
    )
    # Exercise the service authority gate independently of the publication adapter.
    service.load_inputs = lambda tenant, run, claim: inputs
    assert "RULEPACK_APPROVAL_REQUIRED" in service._review(inputs)["reason_codes"]
    before = service.store.history(TENANT, RUN, review["claim_id"])
    actor = AuthContext("reviewer", TENANT, "reviewer", frozenset({"viewer", "reviewer"}), "test")
    with pytest.raises(ReviewRejected, match="RULEPACK_APPROVAL_REQUIRED") as error:
        service.resolve_review(actor, review["review_id"], body, '"1"', str(uuid4()))
    assert error.value.status == 409
    assert service.store.history(TENANT, RUN, review["claim_id"]) == before


@pytest.mark.parametrize("clipped,expected_status", [(False, 200), (True, 422)])
def test_scoped_roles_are_enforced_by_tagging_and_human_review(
    tmp_path, monkeypatch, clipped, expected_status
):
    from proofops.application.evidence.binding import local_relation_tags

    from tests.acceptance.test_binding import span

    original_setup = setup

    def scoped_setup(path):
        inputs = original_setup(path)
        relations = local_relation_tags(inputs["context"])
        if clipped:
            source = inputs["context"].claim.source_refs[0]
            values = next(iter(relations.values()))
            relations = {f"{source.source_id}:0:{span(source, '40%').char_start}": values}
        inputs["relation_tags"] = relations
        return inputs

    monkeypatch.setitem(workspace.__globals__, "setup", scoped_setup)
    ws = workspace(tmp_path)
    initial = ws[2].tag_runs[0].guarded.elements[0]
    assert initial.state == ("unknown" if clipped else "present")
    body = ws[4]
    body["elements"][0].update(state="present", normalized_value="40%")
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    response = post(ws, body=body)
    assert response.status_code == expected_status, response.text
    after = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    if clipped:
        assert after == before
        assert "BINDING_REJECTED" in response.text
    else:
        assert len(after["tags"]) == 2
        assert after["tags"][0] == before["tags"][0]


# --- R24: explicit re-review of an already-resolved review (repeat improvement).


def test_re_review_open_review_rejected_use_resolve_first(tmp_path):
    """A still-open review cannot use the re-review action; 412 STALE_REVIEW_REVISION."""
    ws = workspace(tmp_path)
    before = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])
    response = re_review(ws)
    assert response.status_code == 412
    assert response.json()["error"]["code"] == "STALE_REVIEW_REVISION"
    assert ws[1].store.history(TENANT, RUN, ws[3]["claim_id"]) == before


def test_re_review_appends_next_revision_and_preserves_prior_history(tmp_path):
    """Resolve once, then explicitly re-review on the current head: prior
    tag/decision revisions are immutable, a new revision is appended, provenance
    stays human, and no grade is invented."""
    ws = workspace(tmp_path)
    service, review = ws[1], ws[3]
    first = post(ws)
    assert first.status_code == 200, first.text
    assert first.json()["review"]["revision"] == first.json()["new_tag_revision"] == 2
    before = service.store.history(TENANT, RUN, review["claim_id"])
    assert len(before["tags"]) == 2 and len(before["decisions"]) == 1

    head_tag_revision = before["tags"][-1]["tag_revision"]
    body = ws[4] | {"base_tag_revision": head_tag_revision, "reason": "재검토: 동일 근거 재확인"}
    headers = ws[5] | {"If-Match": '"2"', "Idempotency-Key": "re-review-key-0001"}
    second = re_review(ws, body=body, headers=headers)
    assert second.status_code == 200, second.text
    result = second.json()
    assert result["review"]["status"] == "resolved"
    assert result["review"]["revision"] == 3
    assert result["new_tag_revision"] == 3
    assert result["decision"]["review_status"] == "human_confirmed"
    assert result["decision"]["label"] is None
    assert result["decision"]["evidence_grade"] is None

    after = service.store.history(TENANT, RUN, review["claim_id"])
    # Prior immutable revisions unchanged; exactly one new tag and decision.
    assert after["tags"][: len(before["tags"])] == before["tags"]
    assert after["decisions"][: len(before["decisions"])] == before["decisions"]
    assert len(after["tags"]) == 3 and len(after["decisions"]) == 2
    # The re-review audit event is honestly distinct from the first resolve.
    from proofops.adapters.local.audit_store import LocalSQLiteAuditStore

    events = LocalSQLiteAuditStore(service.store.jobs.path).events(TENANT, RUN)
    actions = [e.action for e in events]
    assert "review_resolved" in actions and "review_rereviewed" in actions


def test_re_review_stale_if_match_and_idempotency_retained(tmp_path):
    ws = workspace(tmp_path)
    service, review = ws[1], ws[3]
    assert post(ws).status_code == 200
    head_tag_revision = service.store.history(TENANT, RUN, review["claim_id"])["tags"][-1][
        "tag_revision"
    ]
    body = ws[4] | {"base_tag_revision": head_tag_revision, "reason": "재검토 경합"}
    # Stale If-Match (still pointing at revision 1) is rejected without writes.
    before = service.store.history(TENANT, RUN, review["claim_id"])
    stale = re_review(
        ws, body=body, headers=ws[5] | {"If-Match": '"1"', "Idempotency-Key": "rr-stale-00000001"}
    )
    assert stale.status_code == 412
    assert service.store.history(TENANT, RUN, review["claim_id"]) == before
    # Correct If-Match succeeds; identical replay returns same result, changed body conflicts.
    ok_headers = ws[5] | {"If-Match": '"2"', "Idempotency-Key": "rr-ok-0000000001"}
    first = re_review(ws, body=body, headers=ok_headers)
    assert first.status_code == 200, first.text
    assert re_review(ws, body=body, headers=ok_headers).json() == first.json()
    conflict = re_review(ws, body=body | {"reason": "다른 재검토 사유"}, headers=ok_headers)
    assert conflict.status_code == 409


def test_re_review_cross_tenant_hidden_and_csrf_required(tmp_path):
    ws = workspace(tmp_path)
    assert post(ws).status_code == 200
    head_tag_revision = ws[1].store.history(TENANT, RUN, ws[3]["claim_id"])["tags"][-1][
        "tag_revision"
    ]
    body = ws[4] | {"base_tag_revision": head_tag_revision, "reason": "재검토 권한 검사"}
    assert (
        re_review(
            ws, body=body, headers=ws[5] | {"If-Match": '"2"', "X-CSRF-Token": "wrong"}
        ).status_code
        == 403
    )
    from dataclasses import replace
    from uuid import uuid4

    from proofops.application.authorization import MembershipRecord

    auth = ws[-1]
    other = str(uuid4())
    auth.memberships.put(MembershipRecord(other, "admin-user", "reviewer", "active"))
    auth.sessions.put_with_token(
        replace(auth.sessions.get("admin-session"), active_tenant_id=other), ws[5]["X-CSRF-Token"]
    )
    assert re_review(ws, body=body, headers=ws[5] | {"If-Match": '"2"'}).status_code == 404


def test_re_review_human_route_cannot_forge_ai_metadata(tmp_path):
    """The HTTP re-review body cannot self-assert AI-delegated provenance; the
    resulting tag stays human origin regardless of injected keys."""
    ws = workspace(tmp_path)
    service, review = ws[1], ws[3]
    assert post(ws).status_code == 200
    head_tag_revision = service.store.history(TENANT, RUN, review["claim_id"])["tags"][-1][
        "tag_revision"
    ]
    # Any extra key (attempting to forge origin/delegation) is rejected by StrictDTO.
    forged = ws[4] | {
        "base_tag_revision": head_tag_revision,
        "reason": "위조 시도",
        "review_origin": "ai_project_interpretation",
        "delegated_reviewer": "attacker",
    }
    assert (
        re_review(
            ws,
            body=forged,
            headers=ws[5] | {"If-Match": '"2"', "Idempotency-Key": "rr-forge-00000001"},
        ).status_code
        == 422
    )
    # A clean re-review stays honestly human.
    clean = ws[4] | {"base_tag_revision": head_tag_revision, "reason": "정상 재검토"}
    ok = re_review(
        ws, body=clean, headers=ws[5] | {"If-Match": '"2"', "Idempotency-Key": "rr-clean-00000001"}
    )
    assert ok.status_code == 200, ok.text
    tag = service.store.history(TENANT, RUN, review["claim_id"])["tags"][-1]
    assert tag["origin"] == "human"
    assert "review_origin" not in tag and "delegated_reviewer" not in tag


def test_re_review_endpoint_in_openapi_contract(tmp_path):
    ws = workspace(tmp_path)
    paths = ws[0].app.openapi()["paths"]
    assert "/v1/reviews/{review_id}/re-review" in paths
    operation = paths["/v1/reviews/{review_id}/re-review"]["post"]
    assert operation["operationId"] == "review_re_review"
    assert operation["requestBody"]["required"] is True
    assert any(p["name"] == "If-Match" and p["required"] for p in operation["parameters"])


def test_third_re_review_tracks_current_head_and_replay_compat(tmp_path):
    """A third re-review must base on the current head (revision 3 -> 4), old
    resolve idempotency identity is preserved (reopen=False hash unchanged), and
    the reopen replay is keyed independently."""
    ws = workspace(tmp_path)
    service, review = ws[1], ws[3]
    assert post(ws).status_code == 200  # rev 1 -> 2
    hist = service.store.history(TENANT, RUN, review["claim_id"])
    head2 = hist["tags"][-1]["tag_revision"]
    r3 = re_review(
        ws,
        body=ws[4] | {"base_tag_revision": head2, "reason": "두번째 재검토"},
        headers=ws[5] | {"If-Match": '"2"', "Idempotency-Key": "rr-second-00000001"},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["review"]["revision"] == 3
    head3 = service.store.history(TENANT, RUN, review["claim_id"])["tags"][-1]["tag_revision"]
    assert head3 == 3
    r4 = re_review(
        ws,
        body=ws[4] | {"base_tag_revision": head3, "reason": "세번째 재검토"},
        headers=ws[5] | {"If-Match": '"3"', "Idempotency-Key": "rr-third-00000001"},
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["review"]["revision"] == 4
    assert r4.json()["new_tag_revision"] == 4
    # Using a stale head (2) on the current head-3 review is rejected.
    stale_head = re_review(
        ws,
        body=ws[4] | {"base_tag_revision": head2, "reason": "낡은 head"},
        headers=ws[5] | {"If-Match": '"4"', "Idempotency-Key": "rr-stalehd-0000001"},
    )
    assert stale_head.status_code == 412


def test_old_resolve_idempotency_receipt_replays_after_reopen_support(tmp_path):
    """A real stored ordinary-resolve idempotency receipt must still replay
    byte-for-byte after re-review support was added (the reopen marker must not
    perturb the reopen=False identity). Same key + same body returns the exact
    prior response; a changed body conflicts."""
    ws = workspace(tmp_path)
    service, review = ws[1], ws[3]
    headers = ws[5] | {"Idempotency-Key": "legacy-resolve-000001"}
    first = post(ws, headers=headers)
    assert first.status_code == 200, first.text
    # A real second call with the identical key/body replays the stored receipt.
    replay = post(ws, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # Confirm the stored receipt's request_hash uses the legacy 3-tuple identity.
    import json as _json

    from proofops.domain.provenance import canonical_hash

    jobs = service.store.jobs
    replay_key = canonical_hash(["admin-user", "review_resolve", "legacy-resolve-000001"])
    with jobs._transaction() as db:
        row = db.execute(
            "SELECT value FROM job_records WHERE kind='review_idempotency' AND record_id=?",
            (replay_key,),
        ).fetchone()
    stored = _json.loads(row[0])
    legacy_hash = canonical_hash([review["review_id"], ws[4], 1])
    assert stored["request_hash"] == legacy_hash
