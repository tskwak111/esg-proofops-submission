"""Actual HTTP source annotations; no ownership, condition or grade approval."""

import time
from hashlib import sha256

from fastapi import FastAPI
from fastapi.testclient import TestClient
from proofops.adapters.local.auth_store import InMemoryMembershipStore, InMemorySessionStore
from proofops_api.auth import SESSION_COOKIE_NAME, AuthStore

from tests.acceptance.test_parsing import TENANT
from tests.acceptance.test_rulepack_api import _headers, _seed_auth
from tests.integration.test_local_parser_runner import runner_setup


def test_source_review_bootstrap_and_original_view_over_http(tmp_path, monkeypatch):
    from proofops.adapters.local.source_condition_review import LocalSourceConditionReview
    from proofops_api.routers.source_conditions import build_source_conditions_router

    _service, run_id, runner, _clock, _log = runner_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    reviews = LocalSourceConditionReview(runner.store, runner.uploads, runner.parser)
    auth = AuthStore(InMemorySessionStore(), InMemoryMembershipStore())
    token = _seed_auth(auth, role="reviewer")
    auth_time = [time.time()]
    app = FastAPI()
    app.include_router(
        build_source_conditions_router(
            reviews, auth, allowed_origin="http://testserver", clock=lambda: auth_time[0]
        )
    )
    client = TestClient(app)
    client.cookies.set(SESSION_COOKIE_NAME, "admin-session")
    path = f"/v1/runs/{run_id}/source-condition-review"
    assert client.get(path).status_code == 409
    with runner.store.jobs._transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE kind='source_condition_head'"
            ).fetchone()[0]
            == 0
        )
    assert client.post(path, json={}).status_code == 403
    assert client.post(path, json={"approved": True}, headers=_headers(token)).status_code == 422
    assert client.post(path, content=b"{", headers=_headers(token)).status_code == 422
    assert (
        client.post(path, content=b" " * (2 * 1024 * 1024), headers=_headers(token)).status_code
        == 413
    )
    published = client.post(path, json={}, headers=_headers(token))
    assert published.status_code == 200, published.text
    assert published.headers["etag"] == '"1"'
    body = published.json()
    assert body["coverage_status"] == "unknown"
    assert all(value == {} for value in body["review"]["state"].values())
    assert client.post(path, json={}, headers=_headers(token)).json() == body
    assert client.get(path).json() == body
    from proofops.adapters.local.run_artifacts import load_run_evidence

    original = load_run_evidence(
        runner.store, runner.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )["base_graph"]
    value_block = next(
        b
        for b in original.blocks
        if b.kind == "table_cell" and b.raw_text == "1234 tCO2e" and len(b.candidates) > 1
    )
    preview_path = path + "/observations"
    table = next(b for b in original.blocks if b.kind == "table")
    year = next(b for b in original.blocks if b.kind == "table_cell" and b.raw_text == "2025")
    proposal = dict(
        revision=1,
        table_id=table.source_id,
        bindings=[
            dict(
                metric_raw=value_block.source_id,
                reporting_period=year.source_id,
                value_raw=value_block.source_id,
            )
        ],
    )
    with runner.store.jobs._transaction() as db:
        records_before = db.execute("SELECT count(*) FROM job_records").fetchone()[0]
    epoch_before = runner.store.get(TENANT, run_id)["mutation_epoch"]
    assert client.post(preview_path, json=proposal).status_code == 403
    preview = client.post(preview_path, json=proposal, headers=_headers(token))
    assert preview.status_code == 200, preview.text
    proposed = preview.json()
    assert proposed["status"] == "proposal_only" and proposed["coverage_status"] == "unknown"
    assert len(proposed["observations"]) == 1
    assert len(proposed["source_holds"]) == 1
    hold = proposed["source_holds"][0]
    assert hold["observation_id"] == proposed["observations"][0]["observation_id"]
    assert "observation_source_unverified" in hold["reasons"]
    assert proposed["review_sha256"] == body["review"]["revision_sha256"]
    assert proposed["source_snapshot_sha256"] == body["review"]["source_snapshot_sha256"]
    assert all(i in proposed["issues"] for i in body["issues"])
    assert client.post(preview_path, json=proposal, headers=_headers(token)).json() == proposed
    for invalid in (
        {**proposal, "table_id": value_block.source_id},
        {**proposal, "bindings": [{}]},
        {**proposal, "bindings": [dict(proposal["bindings"][0], reporting_period=table.source_id)]},
        {**proposal, "revision": True},
    ):
        assert client.post(preview_path, json=invalid, headers=_headers(token)).status_code == 422
    assert runner.store.get(TENANT, run_id)["mutation_epoch"] == epoch_before
    with runner.store.jobs._transaction() as db:
        assert db.execute("SELECT count(*) FROM job_records").fetchone()[0] == records_before
    assert client.get(path).json() == body
    winner = value_block.candidates[value_block.winner].source
    selected = next(
        f
        for f in body["fragments"]
        if f["fragment"].get("source_id") == value_block.source_id
        and f["fragment"].get("parser_run_id") == winner.parser_run_id
        and f["fragment"].get("source_native_id") == winner.source_native_id
    )
    response = client.get(
        path + "/source-view", params={"revision": 1, "fragment_id": selected["id"]}
    )
    assert response.status_code == 200, response.text
    viewed = response.json()
    assert viewed["display"]["scope"] == "source_display_only"
    assert viewed["review_sha256"] == body["review"]["revision_sha256"]
    png = client.get(viewed["image_url"])
    assert png.status_code == 200 and png.headers["content-type"] == "image/png"
    assert sha256(png.content).hexdigest() == viewed["display"]["image_sha256"]
    assert client.get(path).json() == body
    revisions = path + "/revisions"
    annotation = dict(
        schema_version=1,
        base_source_revision=1,
        source_snapshot_sha256=body["review"]["source_snapshot_sha256"],
        classifications=[],
        ownership=[],
        conditions=[],
        claim_bindings=[],
        citations=[
            dict(
                id=selected["id"],
                fragment=selected["fragment"],
                state="confirmed",
                source_view_receipt=viewed,
            )
        ],
        reason="Checked the original rendered source",
    )
    from copy import deepcopy

    from proofops.domain.provenance import canonical_hash

    forged = deepcopy(annotation)
    display = forged["citations"][0]["source_view_receipt"]["display"]
    display["image_sha256"] = "0" * 64
    display["receipt_sha256"] = canonical_hash(
        {k: v for k, v in display.items() if k != "receipt_sha256"}
    )
    assert (
        client.post(
            revisions, json=forged, headers=_headers(token, key="source-fact-forged-01")
        ).status_code
        == 422
    )
    with runner.store.jobs._transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE kind='source_view_receipt'"
            ).fetchone()[0]
            == 0
        )
    saved = client.post(
        revisions, json=annotation, headers=_headers(token, key="source-fact-write-0001")
    )
    assert saved.status_code == 200, saved.text
    assert saved.headers["etag"] == '"2"'
    assert saved.json()["numeric_receipts"] == [] and saved.json()["coverage_status"] == "unknown"
    assert (
        client.post(
            revisions, json=annotation, headers=_headers(token, key="source-fact-write-0001")
        ).json()
        == saved.json()
    )
    assert client.get(path, params={"revision": 1}).json() == body
    body = client.get(path).json()
    assert body["review"]["revision"] == 2
    view2 = client.get(
        path + "/source-view", params={"revision": 2, "fragment_id": selected["id"]}
    ).json()
    correction = annotation | {
        "base_source_revision": 2,
        "citations": [],
        "classifications": [
            dict(
                id=selected["id"],
                fragment=selected["fragment"],
                state="not_note",
                source_view_receipt=view2,
                reason="This fragment is an in-cell numeric value",
            )
        ],
    }
    import sqlite3

    from proofops.adapters.local import review_store

    with runner.store.jobs._transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE kind='source_view_receipt'"
            ).fetchone()[0]
            == 1
        )
    with monkeypatch.context() as patch:

        def fail_audit(**kwargs):
            raise sqlite3.OperationalError("injected audit failure")

        patch.setattr(review_store, "append_audit_transaction", fail_audit)
        assert (
            client.post(
                revisions,
                json=correction,
                headers=_headers(token, if_match=2, key="source-fact-write-0002"),
            ).status_code
            == 409
        )
    assert client.get(path).json() == body
    with runner.store.jobs._transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE kind='source_view_receipt'"
            ).fetchone()[0]
            == 1
        )
    third = client.post(
        revisions,
        json=correction,
        headers=_headers(token, if_match=2, key="source-fact-write-0002"),
    )
    assert third.status_code == 200 and third.json()["review"]["revision"] == 3
    assert (
        third.json()["review"]["state"]["citations"] == saved.json()["review"]["state"]["citations"]
    )
    auth_time[0] += 61  # Exercise the next rate window without disabling request limits.
    view3 = client.get(
        path + "/source-view", params={"revision": 3, "fragment_id": selected["id"]}
    ).json()
    refreshed = annotation | {
        "base_source_revision": 3,
        "citations": [annotation["citations"][0] | {"source_view_receipt": view3}],
    }
    with monkeypatch.context() as patch:
        resolve = reviews.store.resolve_source_conditions

        def concurrent_resolve(*args, **kwargs):
            with runner.store.jobs._transaction() as db:
                run = runner.store.jobs._get(db, TENANT, run_id, "run", "META")
                runner.store.jobs._bump_run(db, run)
            return resolve(*args, **kwargs)

        patch.setattr(reviews.store, "resolve_source_conditions", concurrent_resolve)
        assert (
            client.post(
                revisions,
                json=refreshed,
                headers=_headers(token, if_match=3, key="source-fact-write-0003"),
            ).status_code
            == 412
        )
    with runner.store.jobs._transaction() as db:
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE kind='source_view_receipt'"
            ).fetchone()[0]
            == 2
        )
        assert (
            db.execute(
                "SELECT count(*) FROM job_records WHERE kind='source_condition_idempotency'"
            ).fetchone()[0]
            == 2
        )
    fourth = client.post(
        revisions, json=refreshed, headers=_headers(token, if_match=3, key="source-fact-write-0003")
    )
    assert fourth.status_code == 200 and fourth.json()["review"]["revision"] == 4
    # Ownership is factual input; a numeric cell cannot become a note by linking it.
    auth_time[0] += 61
    ownership = dict(
        id="a" * 64, fragment_id=selected["id"], targets=[], state="unknown", evidence_refs=[]
    )
    condition = dict(
        id="b" * 64,
        fragment_id=selected["id"],
        ownership_id=ownership["id"],
        kind="unsupported_prose",
        state="unknown",
        value_refs=[],
    )
    pending = annotation | {
        "base_source_revision": 4,
        "citations": [],
        "ownership": [ownership],
        "conditions": [condition],
    }
    fifth = client.post(
        revisions, json=pending, headers=_headers(token, if_match=4, key="source-owner-0001")
    )
    assert fifth.status_code == 200, fifth.text
    owned = fifth.json()["review"]["state"]["ownership"][ownership["id"]]
    interpreted = fifth.json()["review"]["state"]["conditions"][condition["id"]]
    assert interpreted["proposal"] == condition
    assert (
        interpreted["assessment"]["state"] == "unknown"
        and interpreted["assessment"]["value"] is None
    )
    assert owned["proposal"] == ownership and owned["assessment"]["state"] == "unknown"
    from pathlib import Path

    from proofops.adapters.local import native_note_ownership, source_condition_review
    from proofops.application.evidence import (
        citations,
        source_condition_interpretation,
        source_condition_ownership,
    )
    from proofops.domain import numeric

    expected_policy = canonical_hash(
        {
            "ownership": sha256(Path(source_condition_ownership.__file__).read_bytes()).hexdigest(),
            "interpretation": sha256(
                Path(source_condition_interpretation.__file__).read_bytes()
            ).hexdigest(),
            "numeric_literals": sha256(Path(numeric.__file__).read_bytes()).hexdigest(),
            "reviewed_view": sha256(
                Path(source_condition_review.__file__).read_bytes()
            ).hexdigest(),
            "citations": sha256(Path(citations.__file__).read_bytes()).hexdigest(),
            "native_note_ownership": sha256(
                Path(native_note_ownership.__file__).read_bytes()
            ).hexdigest(),
        }
    )
    assert owned["assessment"]["policy_sha256"] == expected_policy
    assert interpreted["assessment"]["policy_sha256"] == expected_policy
    assert fifth.json()["numeric_receipts"] == [] and fifth.json()["coverage_status"] == "unknown"
    assert client.get(path, params={"revision": 4}).json()["review"] == fourth.json()["review"]
    assert (
        client.post(
            revisions, json=pending, headers=_headers(token, if_match=4, key="source-owner-0001")
        ).json()
        == fifth.json()
    )
    forged_owner = pending | {
        "base_source_revision": 5,
        "ownership": [ownership | {"state": "accepted"}],
    }
    assert (
        client.post(
            revisions,
            json=forged_owner,
            headers=_headers(token, if_match=5, key="source-owner-bad1"),
        ).status_code
        == 422
    )

    forged_condition = pending | {
        "base_source_revision": 5,
        "conditions": [condition | {"value": "Scope 1"}],
    }
    assert (
        client.post(
            revisions,
            json=forged_condition,
            headers=_headers(token, if_match=5, key="source-condition-forged-value"),
        ).status_code
        == 422
    )

    orphan = pending | {
        "base_source_revision": 5,
        "conditions": [condition | {"ownership_id": "f" * 64}],
    }
    assert (
        client.post(
            revisions,
            json=orphan,
            headers=_headers(token, if_match=5, key="source-condition-orphan"),
        ).status_code
        == 422
    )

    def quality(graph):
        return next(
            b.quality for b in graph.blocks if b.source_id == selected["fragment"]["source_id"]
        )

    assert quality(original) == "unverified"
    reviewed = reviews._reviewed_graph(
        original, fifth.json()["review"]["state"], body["review"]["source_snapshot_sha256"]
    )
    assert quality(reviewed) == "verified"
    assert reviewed.issues == original.issues
    assert reviewed.candidates == original.candidates
    loser = next(
        f
        for f in body["fragments"]
        if f["fragment"].get("source_id") == value_block.source_id
        and f["fragment"].get("parser_run_id") != winner.parser_run_id
    )
    loser_view = client.get(
        path + "/source-view", params={"revision": 5, "fragment_id": loser["id"]}
    )
    assert loser_view.status_code == 200, loser_view.text
    alternative = fifth.json()["review"]["state"] | {
        "citations": {
            loser["id"]: dict(
                id=loser["id"],
                fragment=loser["fragment"],
                state="confirmed",
                source_view_receipt=loser_view.json(),
            )
        }
    }
    assert (
        quality(
            reviews._reviewed_graph(original, alternative, body["review"]["source_snapshot_sha256"])
        )
        == "unverified"
    )
    withdrawal = annotation | {
        "base_source_revision": 5,
        "citations": [
            annotation["citations"][0] | {"state": "unknown", "source_view_receipt": None}
        ],
    }
    sixth = client.post(
        revisions, json=withdrawal, headers=_headers(token, if_match=5, key="source-owner-withdraw")
    )
    assert sixth.status_code == 200, sixth.text
    assert sixth.json()["review"]["state"]["ownership"][ownership["id"]]["proposal"] == ownership
    withdrawn = reviews._reviewed_graph(
        original, sixth.json()["review"]["state"], body["review"]["source_snapshot_sha256"]
    )
    assert quality(withdrawn) == "unverified"
    assert client.get(path, params={"revision": 5}).json()["review"] == fifth.json()["review"]
    from dataclasses import asdict

    oversized = pending | {
        "base_source_revision": 6,
        "ownership": [
            ownership
            | {"evidence_refs": [asdict(value_block.source_ref()) | {"quote": "x" * (1024 * 1024)}]}
        ],
    }
    assert (
        client.post(
            revisions,
            json=oversized,
            headers=_headers(token, if_match=6, key="source-owner-size-limit"),
        ).status_code
        == 413
    )
    assert client.get(path).json()["review"] == sixth.json()["review"]
    # Per-request bounds must also bound the accumulated immutable snapshot.
    capacity_revision = 6
    for category, entry in (("ownership", ownership), ("conditions", condition)):
        for start in range(0, 255, 16):
            auth_time[0] += 61
            batch = pending | {
                "base_source_revision": capacity_revision,
                "ownership": [],
                "conditions": [],
                category: [
                    entry | {"id": canonical_hash([category, i])}
                    for i in range(start, min(start + 16, 255))
                ],
            }
            capped = client.post(
                revisions,
                json=batch,
                headers=_headers(
                    token, if_match=capacity_revision, key=f"{category}-capacity-{start:04}"
                ),
            )
            assert capped.status_code == 200, capped.text
            capacity_revision += 1
        assert len(capped.json()["review"]["state"][category]) == 256
        at_cap = pending | {"base_source_revision": capacity_revision}
        updated = client.post(
            revisions,
            json=at_cap,
            headers=_headers(token, if_match=capacity_revision, key=f"{category}-capacity-update"),
        )
        assert updated.status_code == 200
        capacity_revision += 1
        overflow = pending | {
            "base_source_revision": capacity_revision,
            category: [entry | {"id": "f" * 64}],
        }
        rejected = client.post(
            revisions,
            json=overflow,
            headers=_headers(
                token, if_match=capacity_revision, key=f"{category}-capacity-overflow"
            ),
        )
        assert rejected.status_code == 413, rejected.text
        assert client.get(path).json()["review"] == updated.json()["review"]
    with monkeypatch.context() as patch:

        def unavailable(*args):
            raise ValueError("original temporarily unavailable")

        patch.setattr(reviews, "_inputs", unavailable)
        assert (
            client.post(
                revisions, json=annotation, headers=_headers(token, key="source-fact-write-0001")
            ).json()
            == saved.json()
        )
    assert (
        client.post(
            revisions,
            json=annotation | {"reason": "Changed request reason"},
            headers=_headers(token, key="source-fact-write-0001"),
        ).status_code
        == 409
    )
    assert (
        client.post(
            revisions, json=annotation | {"grade": "E3"}, headers=_headers(token)
        ).status_code
        == 422
    )
    body = client.get(path).json()
    assert body["coverage_status"] == "unknown"
    assert (
        client.get(
            path + "/source-view", params={"revision": 1, "fragment_id": "0" * 64}
        ).status_code
        == 404
    )
    with monkeypatch.context() as patch:
        publish = reviews.store.publish_source_conditions

        def concurrent_change(inputs, *, expected_epoch):
            with runner.store.jobs._transaction() as db:
                run = runner.store.jobs._get(db, TENANT, run_id, "run", "META")
                runner.store.jobs._bump_run(db, run)
            return publish(inputs, expected_epoch=expected_epoch)

        patch.setattr(reviews.store, "publish_source_conditions", concurrent_change)
        assert client.post(path, json={}, headers=_headers(token)).status_code == 412
    assert client.get(path).json() == body
    from dataclasses import replace

    from proofops.application.authorization import MembershipRecord

    from tests.acceptance.test_parsing import FOREIGN

    auth_time[0] += 61
    assert client.post(preview_path, json=proposal, headers=_headers(token)).json() == proposed
    from proofops.application.ingest import normalize

    with monkeypatch.context() as patch:
        normalize_bindings = normalize.normalize_table_bindings

        def concurrent_preview(*args, **kwargs):
            result = normalize_bindings(*args, **kwargs)
            with runner.store.jobs._transaction() as db:
                run = runner.store.jobs._get(db, TENANT, run_id, "run", "META")
                runner.store.jobs._bump_run(db, run)
            return result

        patch.setattr(normalize, "normalize_table_bindings", concurrent_preview)
        assert client.post(preview_path, json=proposal, headers=_headers(token)).status_code == 412
    assert client.get(path).json() == body
    auth.memberships.put(MembershipRecord(TENANT, "admin-user", "viewer", "active"))
    assert (
        client.post(
            revisions, json=annotation, headers=_headers(token, key="source-fact-write-0001")
        ).status_code
        == 403
    )
    assert client.post(preview_path, json=proposal, headers=_headers(token)).status_code == 403
    assert client.get(path).status_code == 403
    assert client.post(path, json={}, headers=_headers(token)).status_code == 403
    assert client.get(viewed["image_url"]).status_code == 403
    auth.memberships.put(MembershipRecord(FOREIGN, "admin-user", "reviewer", "active"))
    auth.sessions.put(replace(auth.sessions.get("admin-session"), active_tenant_id=FOREIGN))
    assert (
        client.post(
            revisions, json=annotation, headers=_headers(token, key="source-fact-write-0001")
        ).status_code
        == 404
    )
    assert client.post(preview_path, json=proposal, headers=_headers(token)).status_code == 404
    assert client.get(path).status_code == 404
    assert client.post(path, json={}, headers=_headers(token)).status_code == 404
    assert client.get(viewed["image_url"]).status_code == 404
    client.cookies.clear()
    assert (
        client.post(
            revisions, json=annotation, headers=_headers(token, key="source-fact-write-0001")
        ).status_code
        == 401
    )
    assert client.get(viewed["image_url"]).status_code == 401
