"""AT-026: local synthetic snapshots; no model, AWS, or customer data is used."""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.application.authorization import MembershipRecord
from proofops.application.ports.jobs import JobMessage
from proofops.application.summaries import RequirementStatus, summarize_snapshot
from proofops.domain.rulepacks import canonical_json
from proofops_api.auth import SESSION_COOKIE_NAME, AuthStore

from tests.acceptance.test_rulepack_api import _seed_auth
from tests.integration.test_run_lifecycle import validate

TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "22222222-2222-4222-8222-222222222222"
RUN = "33333333-3333-4333-8333-333333333333"
DOCUMENT = "44444444-4444-4444-8444-444444444444"
CLAIM = "55555555-5555-4555-8555-555555555555"
HASH = "a" * 64


def coverage(**changes):
    values = dict(
        pages_total=4,
        pages_processed=2,
        pages_unreadable=1,
        pages_unprocessed=1,
        chunks_discovered=5,
        chunks_processed=4,
        claims_discovered=4,
        claims_decided=2,
        claims_needs_review=1,
        full_scope=True,
        complete=False,
    )
    return values | changes


def decision(status, grade=None, *, missing=(), basis_status="verified"):
    label = {
        "E0": "UNSUBSTANTIATED",
        "E1": "INCOMPLETE",
        "E2": "INCOMPLETE",
        "E3": "SUBSTANTIATED",
    }.get(grade)
    return dict(
        decision_status=status,
        evidence_grade=grade,
        label=label,
        review_status="auto_confirmed" if status == "decided" else "needs_review",
        missing_elements=list(missing),
        basis_refs=[
            canonical_json(
                {
                    "element_id": element,
                    "basis": {"verification_status": basis_status},
                }
            )
            for element in (missing or ("P1",))
        ],
    )


def test_grade_denominator_excludes_undecided_and_unreadable_work():
    result = summarize_snapshot(
        run_id=RUN,
        snapshot_epoch=7,
        coverage=coverage(),
        decisions=(
            decision("decided", "E0", missing=("G2",), basis_status="unverified"),
            decision("decided", "E3"),
            decision("blocked_evidence", missing=("G2",)),
            None,
        ),
        applicability=(
            RequirementStatus("G1", "applicable", True),
            RequirementStatus("G2", "applicable", False),
            RequirementStatus("WATER", "N_A", None),
            RequirementStatus("SCOPE3", "applicable", None, deferred=True),
            RequirementStatus("WASTE", "undetermined", None),
        ),
    ).to_api_dict()

    assert result["grade_counts"] == {"E0": 1, "E1": 0, "E2": 0, "E3": 1}
    assert sum(result["grade_counts"].values()) == 2
    assert result["undecided_count"] == 2
    assert result["coverage"]["pages_unreadable"] == 1
    assert result["not_applicable_count"] == 1
    assert result["deferred_count"] == 1
    assert result["undetermined_applicability_count"] == 1
    assert (result["applicable_count"], result["satisfied_count"]) == (2, 1)
    assert result["fulfillment_rate"] == 0.5
    assert result["missing_by_element"] == [{"element_id": "G2", "count": 2}]
    assert result["unverified_basis_count"] == 1


def test_unrun_applicability_is_unknown_not_empty_compliance():
    result = summarize_snapshot(
        run_id=RUN,
        snapshot_epoch=0,
        coverage=coverage(
            pages_processed=0,
            pages_unreadable=0,
            pages_unprocessed=4,
            chunks_discovered=0,
            chunks_processed=0,
            claims_discovered=0,
            claims_decided=0,
            claims_needs_review=0,
        ),
        decisions=(),
        applicability=None,
    ).to_api_dict()

    assert result["undetermined_applicability_count"] is None
    assert result["applicable_count"] == result["satisfied_count"] == 0
    assert result["fulfillment_rate"] is None
    assert result["coverage"]["complete"] is False


@pytest.mark.parametrize(
    ("decisions", "statuses"),
    [
        ((decision("decided"), None, None, None), ()),
        (
            (decision("decided", "E1"), None, None, None),
            (("G1", "undetermined", False),),
        ),
        (
            (decision("decided", "E1"), None, None, None),
            (("G1", "N_A", True),),
        ),
    ],
)
def test_invalid_grades_or_applicability_cannot_be_hidden(decisions, statuses):
    with pytest.raises(ValueError):
        requirements = tuple(RequirementStatus(*status) for status in statuses)
        summarize_snapshot(
            run_id=RUN,
            snapshot_epoch=1,
            coverage=coverage(),
            decisions=decisions,
            applicability=requirements,
        )


class _NoopUploads:
    pass


class _NoopParser:
    pass


def seed_store(tmp_path, *, grade="E0"):
    """Create an explicit local-synthetic immutable claim/decision snapshot."""
    from proofops.adapters.local.summary_store import LocalSummaryStore

    runs = LocalSQLiteRunStore(tmp_path / "summary.sqlite")
    jobs = runs.jobs
    jobs.create_run(TENANT, RUN, DOCUMENT)
    message = JobMessage(TENANT, RUN, DOCUMENT, str(uuid4()), "extract", "full", HASH)
    envelope = dict(
        schema="local_extract_checkpoint_v1",
        tenant_id=TENANT,
        run_id=RUN,
        document_version_id=DOCUMENT,
        input_hash=HASH,
        rule_pack_sha256=HASH,
        discovery=dict(claims=[{"claim_id": CLAIM}]),
    )
    raw = canonical_json(envelope).encode()
    digest = sha256(raw).hexdigest()
    snapshot = dict(
        tenant_id=TENANT,
        run_id=RUN,
        input_hash=HASH,
        scope="full",
        document={"version_id": DOCUMENT, "page_count": 1},
        rulepack={"sha256": HASH},
        created_at="2026-09-09T00:00:00Z",
    )
    with jobs._transaction() as db:
        db.execute(
            "INSERT INTO run_snapshots VALUES (?, ?, ?)",
            (TENANT, RUN, canonical_json(snapshot)),
        )
        jobs._put(
            db,
            TENANT,
            RUN,
            "job",
            message.job_id,
            {
                "message": asdict(message),
                "status": "succeeded",
                "artifact_ref": {
                    "key": "synthetic-summary-claims",
                    "sha256": digest,
                    "byte_size": len(raw),
                    "storage": "local-synthetic-only",
                },
            },
        )
        jobs._put(db, TENANT, RUN, "artifact", "synthetic-summary-claims", raw)
        run = jobs._get(db, TENANT, RUN, "run", "META")
        run.update(
            extract_job=asdict(message),
            claim_snapshot_sha256=digest,
            rule_pack_sha256=HASH,
            coverage=coverage(
                pages_total=1,
                pages_processed=1,
                pages_unreadable=0,
                pages_unprocessed=0,
                chunks_discovered=1,
                chunks_processed=1,
                claims_discovered=1,
                claims_decided=1,
                claims_needs_review=0,
            ),
            mutation_epoch=1,
        )
        jobs._put(db, TENANT, RUN, "run", "META", run)
        _put_revision(jobs, db, 1, grade)
    claims = LocalClaimStore(runs, _NoopUploads(), _NoopParser())
    return LocalSummaryStore(runs, claims), runs


def _put_revision(jobs, db, revision, grade):
    api = decision("decided", grade)
    api |= {
        "decision_revision": revision,
        "tag_revision": revision,
        "sublabel": None,
        "rule_ids": [],
        "rule_pack_sha256": HASH,
        "semantic_hash": str(revision) * 64,
        "gap_ids": [],
    }
    internal = api | {
        "basis_refs": api.pop("basis_refs"),
        "unresolved_elements": [],
        "excluded_elements": [],
    }
    jobs._put(
        db,
        TENANT,
        RUN,
        "tag_revision",
        f"{CLAIM}:{revision:010}",
        {"tag_revision": revision, "confirmed_tags": None, "elements": [], "inputs": {}},
        immutable=True,
    )
    jobs._put(
        db,
        TENANT,
        RUN,
        "decision_revision",
        f"{CLAIM}:{revision:010}",
        {"decision_revision": revision, "decision": internal, "api": api},
        immutable=True,
    )
    jobs._put(
        db,
        TENANT,
        RUN,
        "claim_head",
        CLAIM,
        {"tag_revision": revision, "decision_revision": revision},
    )


def test_sqlite_summary_uses_one_revision_and_epoch_during_concurrent_update(tmp_path):
    from proofops.adapters.local.summary_store import LocalSummaryStore

    store, runs = seed_store(tmp_path)
    entered, release = Event(), Event()

    class PausingClaims(LocalClaimStore):
        def current_tag(self, *args, connection=None, **kwargs):
            assert connection is not None
            entered.set()
            assert release.wait(2)
            return super().current_tag(*args, connection=connection, **kwargs)

    paused = LocalSummaryStore(runs, PausingClaims(runs, _NoopUploads(), _NoopParser()))
    result = []
    reader = Thread(target=lambda: result.append(paused.get(TENANT, RUN)))
    reader.start()
    assert entered.wait(2)

    def update():
        with runs.jobs._transaction() as db:
            _put_revision(runs.jobs, db, 2, "E3")
            run = runs.jobs._get(db, TENANT, RUN, "run", "META")
            runs.jobs._bump_run(db, run)

    writer = Thread(target=update)
    writer.start()
    release.set()
    reader.join(2)
    writer.join(2)
    assert not reader.is_alive() and not writer.is_alive()
    assert result[0]["snapshot_epoch"] == 1
    assert result[0]["grade_counts"] == {"E0": 1, "E1": 0, "E2": 0, "E3": 0}
    latest = store.get(TENANT, RUN)
    assert latest["snapshot_epoch"] == 2
    assert latest["grade_counts"] == {"E0": 0, "E1": 0, "E2": 0, "E3": 1}


def test_summary_http_is_viewer_scoped_no_store_and_schema_valid(tmp_path):
    from proofops_api.routers.summaries import build_summaries_router

    store, _ = seed_store(tmp_path)
    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    _seed_auth(auth, role="viewer")
    app = FastAPI()
    app.include_router(build_summaries_router(store, auth, clock=lambda: 1))
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "admin-session")

    response = client.get(f"/v1/runs/{RUN}/summary")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    validate("Summary", response.json())
    assert response.json()["snapshot_epoch"] == 1
    assert response.json()["undetermined_applicability_count"] is None

    session = auth.sessions.get("admin-session")
    auth.sessions.put(session.__class__(**(asdict(session) | {"active_tenant_id": FOREIGN})))
    auth.memberships.put(MembershipRecord(FOREIGN, session.user_sub, "viewer", "active"))
    assert client.get(f"/v1/runs/{RUN}/summary").status_code == 404
    client.cookies.clear()
    assert client.get(f"/v1/runs/{RUN}/summary").status_code == 401


def test_real_local_rescore_head_is_visible_through_populated_summary_http(tmp_path, monkeypatch):
    from proofops.adapters.local.rescore_store import LocalSQLiteRescoreStore
    from proofops.adapters.local.summary_store import LocalSummaryStore
    from proofops.application.authorization import AuthContext
    from proofops.application.rescores import RescoreService
    from proofops.application.rulepacks import RulePackRecord
    from proofops.domain.rules.engine import ConfirmedFact, ConfirmedTags, evaluate
    from proofops.domain.values import _source_ref_from_dict
    from proofops_api.routers.summaries import build_summaries_router

    from tests.acceptance.test_rescore import revised_pack
    from tests.integration.test_local_tag_runner import TENANT as PIPELINE_TENANT
    from tests.integration.test_local_tag_runner import verified_setup

    service, run_id, runner, _, _ = verified_setup(tmp_path, monkeypatch)
    store = LocalSummaryStore(service.store, runner.claims)
    before = store.get(PIPELINE_TENANT, run_id)
    assert sum(before["grade_counts"].values()) == 0
    assert before["undecided_count"] == before["coverage"]["claims_discovered"] == 1

    assert runner.run_once(tenant_id=PIPELINE_TENANT, run_id=run_id) == "needs_review"
    after = store.get(PIPELINE_TENANT, run_id)
    assert after["snapshot_epoch"] > before["snapshot_epoch"]
    assert sum(after["grade_counts"].values()) == 0
    assert after["undecided_count"] == 1
    assert after["undetermined_applicability_count"] is None

    claim_id = runner.claims.list(PIPELINE_TENANT, run_id)[0].claim_id
    inputs = runner.tags.load_inputs(PIPELINE_TENANT, run_id, claim_id)
    actor = AuthContext(
        "synthetic-reviewer",
        PIPELINE_TENANT,
        "reviewer",
        frozenset({"viewer", "reviewer"}),
        "synthetic-session",
    )
    with service.store.jobs._transaction() as db:
        review = service.store.jobs._all(db, PIPELINE_TENANT, run_id, "review_head")[0]
    runner.reviews.resolve_review(
        actor,
        review["review_id"],
        {
            "base_tag_revision": 1,
            "track": "performance",
            "reason": "Synthetic original review",
            "elements": [asdict(e) for e in inputs.consensus.candidate_elements],
        },
        '"1"',
        "summary-review-key",
    )

    # Explicit synthetic collection of the otherwise-uncollected trigger. It is
    # unknown, never promoted to absent/present, and lets a rules-only rescore run.
    tag = runner.reviews.store.history(PIPELINE_TENANT, run_id, claim_id)["tags"][-1]
    raw = tag["confirmed_tags"]
    confirmed = ConfirmedTags(
        **(
            raw
            | {
                "tag_revision": 3,
                "facts": tuple(
                    ConfirmedFact(
                        **(
                            fact
                            | {
                                "evidence_refs": tuple(
                                    _source_ref_from_dict(ref) for ref in fact["evidence_refs"]
                                )
                            }
                        )
                    )
                    for fact in raw["facts"]
                )
                + (ConfirmedFact("reduction_or_improvement_claim", "unknown"),),
            }
        )
    )
    decision = evaluate(
        confirmed, replace(inputs.rule_context, decision_revision=2), inputs.rulepack
    )
    tag = tag | {
        "tag_revision": 3,
        "confirmed_tags": asdict(confirmed),
        "origin": "local-synthetic-fixture",
        "execution_profile": "local-synthetic-only",
    }
    with service.store.jobs._transaction() as db:
        service.store.jobs._put(
            db,
            PIPELINE_TENANT,
            run_id,
            "tag_revision",
            f"{claim_id}:0000000003",
            tag,
            immutable=True,
        )
        service.store.jobs._put(
            db,
            PIPELINE_TENANT,
            run_id,
            "decision_revision",
            f"{claim_id}:0000000002",
            {
                "decision_revision": 2,
                "decision": asdict(decision),
                "api": decision.to_api_dict(),
            },
            immutable=True,
        )
        service.store.jobs._put(
            db,
            PIPELINE_TENANT,
            run_id,
            "claim_head",
            claim_id,
            {"tag_revision": 3, "decision_revision": 2},
        )
        service.store.jobs._bump_run(
            db, service.store.jobs._get(db, PIPELINE_TENANT, run_id, "run", "META")
        )

    target = revised_pack(
        rule_pack_id=str(uuid4()),
        status="validated",
        approved_by="synthetic-fixture",
        approved_at="2026-09-09T00:00:00Z",
    )
    service.store.rulepacks.add_pack(
        RulePackRecord(**{k: v for k, v in asdict(target).items() if k != "content"}),
        {path: target.file_content(path) for path in target.files},
    )
    service.store.rulepacks.activate(
        tenant_id=PIPELINE_TENANT,
        rule_pack_id=target.rule_pack_id,
        expected_revision=1,
        idempotency_key=str(uuid4()),
        actor="synthetic-fixture",
        reason="Synthetic rules-only rescore",
        gap_ids=target.unresolved_gap_ids,
        now=1,
    )
    receipt = RescoreService(
        LocalSQLiteRescoreStore(service.store), load_inputs=runner.tags.load_inputs
    ).create_rescore(
        actor,
        run_id,
        {"rule_pack_id": target.rule_pack_id, "reason": "Synthetic rules-only rescore"},
        "summary-rescore-key",
    )
    assert receipt["status"] == "ready"

    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    _seed_auth(auth, role="viewer")
    app = FastAPI()
    app.include_router(build_summaries_router(store, auth, clock=lambda: 1))
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "admin-session")
    response = client.get(f"/v1/runs/{run_id}/summary")
    assert response.status_code == 200, response.text
    validate("Summary", response.json())
    history = runner.reviews.store.history(PIPELINE_TENANT, run_id, claim_id)
    assert history["decisions"][-1]["decision"]["rule_pack_sha256"] == target.sha256
    assert sum(response.json()["grade_counts"].values()) == 0
    assert response.json()["undecided_count"] == 1

    # A changed-pack head without the exact immutable receipt must still fail closed.
    with service.store.jobs._transaction() as db:
        forged = json.loads(json.dumps(history["decisions"][-1]))
        forged["decision_revision"] = 4
        forged["decision"]["decision_revision"] = 4
        forged["api"]["decision_revision"] = 4
        service.store.jobs._put(
            db,
            PIPELINE_TENANT,
            run_id,
            "decision_revision",
            f"{claim_id}:0000000004",
            forged,
            immutable=True,
        )
        service.store.jobs._put(
            db,
            PIPELINE_TENANT,
            run_id,
            "claim_head",
            claim_id,
            {"tag_revision": 3, "decision_revision": 4},
        )
        service.store.jobs._bump_run(
            db, service.store.jobs._get(db, PIPELINE_TENANT, run_id, "run", "META")
        )
    assert client.get(f"/v1/runs/{run_id}/summary").status_code == 409


def test_run_summary_renders_all_denominators_and_unknown_applicability(tmp_path):
    root = Path(__file__).resolve().parents[2]
    esbuild = next((root / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    entry = tmp_path / "summary-render.tsx"
    entry.write_text(
        """
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { RunSummary } from COMPONENT;
import assert from "node:assert/strict";
const summary={run_id:"run",snapshot_epoch:7,
coverage:{pages_total:4,pages_processed:2,pages_unreadable:1,pages_unprocessed:1,
chunks_discovered:5,chunks_processed:4,claims_discovered:5,claims_decided:2,
claims_needs_review:1,full_scope:true,complete:false},
grade_counts:{E0:1,E1:0,E2:0,E3:1},undecided_count:2,not_applicable_count:1,
deferred_count:1,unverified_basis_count:1,missing_by_element:[{element_id:"G2",count:2}],
applicable_count:2,satisfied_count:1,fulfillment_rate:0.5,
undetermined_applicability_count:2};
const html=renderToStaticMarkup(React.createElement(RunSummary,{summary}));
for (const text of ["등급 분포 분모 2건","미판정 2건","검토 필요 1건","판독 불가 1쪽",
"미처리 40.0%","적용 제외 1건","유예 1건","산정 범위 미확정 2건","G2 2건"])
  assert.ok(html.includes(text), text);
const unknown=renderToStaticMarkup(React.createElement(RunSummary,
{summary:{...summary,undetermined_applicability_count:null}}));
assert.ok(unknown.includes("산정 범위 미확정"));
const ungraded=renderToStaticMarkup(React.createElement(RunSummary,
{summary:{...summary,grade_counts:{E0:0,E1:0,E2:0,E3:0},missing_by_element:[]}}));
assert.ok(ungraded.includes("아직 입증 판정이 없어 결손 여부를 확인할 수 없습니다."));
assert.ok(!ungraded.includes("결손 요소가 없습니다"));
const graded=renderToStaticMarkup(React.createElement(RunSummary,
{summary:{...summary,missing_by_element:[]}}));
assert.ok(graded.includes("판정된 주장에서는 확인된 결손 요소가 없습니다."));
console.log("RunSummary denominator/status checks passed");
""".replace("REACT", json.dumps(str(root / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(root / "apps/web/node_modules/react-dom/server.node.js")))
        .replace(
            "COMPONENT",
            json.dumps(str(root / "apps/web/src/features/dashboard/RunSummary.tsx")),
        )
    )
    bundle = tmp_path / "summary-render.cjs"
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
    rendered = subprocess.run(["node", str(bundle)], check=True, capture_output=True, text=True)
    assert "checks passed" in rendered.stdout
