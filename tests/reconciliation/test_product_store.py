"""Storage and trust behaviour for the local reconciliation product store.

The anchor is a real verified run: the PDF is uploaded and verified, the parse,
extraction and tagging runners all execute, and the store replays that evidence
through `claims.get` before it will register anything. Real SQLite, real file
copies, real `reconcile`. No model call, no network, no customer data.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.reconciliation_store import (
    LocalReconciliationStore,
    ReconciliationRejected,
)
from proofops.adapters.local.run_store import LocalSQLiteRunStore
from proofops.application.authorization import AuthContext

from evaluation.reconciliation_fixtures import build_case
from tests.acceptance.test_upload import FOREIGN, TENANT
from tests.integration.test_local_tag_runner import verified_setup

CAPABILITIES = {
    "viewer": {"viewer"},
    "editor": {"viewer", "editor"},
    "reviewer": {"viewer", "reviewer"},
    "admin": {"viewer", "editor", "reviewer", "admin"},
}


def auth(role="admin", tenant=TENANT) -> AuthContext:
    return AuthContext(
        user_sub=f"user-{role}",
        tenant_id=tenant,
        role=role,
        capabilities=frozenset(CAPABILITIES[role]),
        session_id=str(uuid4()),
    )


# --------------------------------------------------------------------------- #
# the verified anchor
# --------------------------------------------------------------------------- #


def build_verified_run(tmp_path, monkeypatch):
    """Run the real upload/parse/extract/tag path and return its verified claim."""
    service, run_id, runner, _now, _stream = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    record = runner.tags.load_snapshot(TENANT, run_id)["claims"][0]
    claim_id = record["claim_id"]
    claims = runner.tags.claims
    claim = claims.get(TENANT, run_id, claim_id)
    assert claim.source_quality == "verified"
    return {
        "service": service,
        "run_id": run_id,
        "claim_id": claim_id,
        "claim": claim,
        "claims": claims,
        "tags": runner.tags,
        "track": runner.tags.load_inputs(TENANT, run_id, claim_id).packet.to_dict()["track"],
        "company_id": service.store.snapshot(TENANT, run_id)["document"]["company"]["company_id"],
    }


@pytest.fixture(scope="module")
def verified(tmp_path_factory):
    """One verified run for the whole module; building it is expensive."""
    from _pytest.monkeypatch import MonkeyPatch

    monkeypatch = MonkeyPatch()
    try:
        yield build_verified_run(tmp_path_factory.mktemp("verified"), monkeypatch)
    finally:
        monkeypatch.undo()


def anchor_bundle(bundle, artifacts_root: Path, *, quote: str, track: str, identity: dict) -> dict:
    """Re-point a fixture bundle at one real verified claim.

    Substitutes the fixture identity strings, replaces the claim sentence, then
    regenerates that document's bytes, hash and every binding that quotes it so
    the bundle stays internally consistent. Exported for the composed browser
    and HTTP harnesses, which anchor against a real run rather than the fixture.
    """
    bundle = _substitute(bundle, identity)
    packet = bundle["packet"]
    packet["claim"]["quote"], packet["claim"]["track"] = quote, track
    claim_source = next(
        source
        for source in packet["sources"]
        if source["source_id"] == packet["claim"]["source_id"]
    )
    claim_source["quote"] = quote
    document_id = claim_source["document_id"]
    refs = [source for source in packet["sources"] if source["document_id"] == document_id]

    text = "SYNTHETIC CROSS-DISCLOSURE TEST ONLY\n"
    for ref in refs:
        start = len(text)
        text += ref["quote"]
        ref["locator"] = f"chars:{start}:{len(text)}"
        text += "\n"
    payload = text.encode("utf-8")
    digest = sha256(payload).hexdigest()
    artifact = bundle["artifacts"][document_id]
    (artifacts_root / artifact["path"]).write_bytes(payload)
    artifact["sha256"] = digest

    document = bundle["documents"][document_id]
    document["artifact_sha256"] = digest
    for ref in refs:
        ref["artifact_sha256"] = digest
        document["source_bindings"][ref["source_id"]].update(
            locator=ref["locator"], quote=ref["quote"]
        )
    for entry in bundle["documents"].values():
        entry["decision_binding"]["claim"] = json.loads(json.dumps(packet["claim"]))
    bundle["policies"] = {}
    return bundle


def _substitute(value, mapping):
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_substitute(item, mapping) for item in value]
    if isinstance(value, dict):
        return {mapping.get(key, key): _substitute(item, mapping) for key, item in value.items()}
    return value


def prepare_bundle(tmp_path: Path, verified: dict, *, name: str = "c1-same-entities"):
    """Operator-side preparation: the agreed six-key bundle plus its artifact root."""
    artifacts_root = tmp_path / "prepared" / name / "artifacts"
    bundle = build_case(name, artifacts_root)
    claim = verified["claim"]
    anchored = anchor_bundle(
        bundle,
        artifacts_root,
        quote=claim.quote,
        track=verified["track"],
        identity={
            "fixture-tenant": TENANT,
            "fixture-company": verified["company_id"],
            "fixture-claim": verified["claim_id"],
            "sr-v1": claim.document_version_id,
        },
    )
    return anchored, artifacts_root


@pytest.fixture
def store(verified, tmp_path):
    service = verified["service"]
    return LocalReconciliationStore(
        service.store.path,
        tmp_path / "managed",
        run_store=service.store,
        claims=verified["claims"],
        tags=verified["tags"],
    )


@pytest.fixture
def registered(store, verified, tmp_path):
    bundle, artifacts_root = prepare_bundle(tmp_path, verified)
    detail = store.register_case(
        auth(), verified["run_id"], verified["claim_id"], bundle, artifacts_root
    )
    return detail, verified["run_id"], verified["claim_id"], bundle, artifacts_root


def seed_unverified_run(runs, run_id: str, claim_id: str, *, quote: str, track: str) -> None:
    """Hand-write the rows a tagged claim leaves behind, with no real extraction."""
    jobs = runs.jobs
    with jobs._transaction() as db:
        jobs._put(
            db,
            TENANT,
            run_id,
            "run",
            "META",
            {
                "tenant_id": TENANT,
                "run_id": run_id,
                "document_version_id": str(uuid4()),
                "status": "completed",
                "revision": 1,
                "mutation_epoch": 0,
                "cancellation_requested": False,
                "execution_profile": "local-synthetic-only",
            },
        )
        jobs._put(
            db, TENANT, run_id, "claim_head", claim_id, {"tag_revision": 4, "decision_revision": 0}
        )
        jobs._put(
            db,
            TENANT,
            run_id,
            "tag_revision",
            f"{claim_id}:{4:010}",
            {
                "tag_revision": 4,
                "origin": "human",
                "confirmed_tags": {"track": track, "quote": quote},
                "inputs": {"packet": {"track": track}},
            },
        )


# --------------------------------------------------------------------------- #
# schema and migration
# --------------------------------------------------------------------------- #


def fresh_store(tmp_path, name="state.sqlite3"):
    """A store with no anchored run at all, for schema-level behaviour."""
    runs = LocalSQLiteRunStore(tmp_path / name)
    return runs, LocalReconciliationStore(
        tmp_path / name,
        tmp_path / "managed",
        run_store=runs,
        claims=LocalClaimStore(runs, None, None),
    )


def test_tables_use_the_reconciliation_prefix_and_record_a_schema_version(tmp_path):
    runs, _store = fresh_store(tmp_path)
    with runs.jobs._transaction() as db:
        names = {
            row[0]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        version = db.execute("SELECT version FROM reconciliation_schema").fetchall()
    assert version == [(1,)]
    assert {
        "reconciliation_schema",
        "reconciliation_case",
        "reconciliation_head",
        "reconciliation_revision",
        "reconciliation_source",
        "reconciliation_idempotency",
    } <= {name for name in names if name.startswith("reconciliation_")}


def test_an_unknown_storage_schema_version_fails_closed(tmp_path):
    runs, _store = fresh_store(tmp_path)
    with runs.jobs._transaction() as db:
        db.execute("UPDATE reconciliation_schema SET version=99")
    with pytest.raises(ReconciliationRejected):
        LocalReconciliationStore(
            tmp_path / "state.sqlite3",
            tmp_path / "managed",
            run_store=runs,
            claims=LocalClaimStore(runs, None, None),
        )


def test_the_store_refuses_a_database_that_does_not_match_the_run_store(tmp_path):
    runs = LocalSQLiteRunStore(tmp_path / "a.sqlite3")
    with pytest.raises(ValueError):
        LocalReconciliationStore(
            tmp_path / "b.sqlite3",
            tmp_path / "m",
            run_store=runs,
            claims=LocalClaimStore(runs, None, None),
        )


def test_existing_run_and_claim_records_are_never_modified(registered, store, verified):
    detail, run_id, claim_id, _bundle, _root = registered
    with store.run_store.jobs._transaction() as db:
        head = store.run_store.jobs._get(db, TENANT, run_id, "claim_head", claim_id)
        run = store.run_store.jobs._get(db, TENANT, run_id, "run", "META")
    current = verified["claims"].current_tag(TENANT, run_id, claim_id)
    assert head["tag_revision"] == current["tag"]["tag_revision"]
    assert run["mutation_epoch"] == current["epoch"]
    assert detail["revision"] == 1


# --------------------------------------------------------------------------- #
# registration is bound to a really verified claim
# --------------------------------------------------------------------------- #


def register(store, verified, tmp_path, *, mutate=None, run_id=None, claim_id=None):
    """Register one prepared case, optionally corrupting the bundle first."""
    bundle, artifacts_root = prepare_bundle(tmp_path, verified)
    if mutate is not None:
        mutate(bundle, artifacts_root)
    return store.register_case(
        auth(),
        run_id or verified["run_id"],
        claim_id or verified["claim_id"],
        bundle,
        artifacts_root,
    )


def test_registration_starts_pending_and_unapproved(registered):
    detail = registered[0]
    assert detail["review_state"] == "pending"
    assert detail["policy_approved"] is False
    assert detail["latest_result"] is None
    assert detail["revision"] == 1
    assert detail["sources"]
    assert all(source["binding_state"] == "draft" for source in detail["sources"])


def test_registration_copies_source_bytes_into_the_managed_store(registered, store):
    detail, _run_id, _claim_id, bundle, artifacts_root = registered
    source = detail["sources"][0]
    original = artifacts_root / bundle["artifacts"][source["document_id"]]["path"]
    original.write_bytes(b"mutated after registration")
    payload, _filename, digest = store.source_content(
        auth("viewer"), detail["case_id"], source["source_id"]
    )
    assert payload != b"mutated after registration"
    assert sha256(payload).hexdigest() == digest == source["artifact_sha256"]


def test_a_hand_written_claim_head_without_real_extraction_is_refused(tmp_path, verified):
    """The decisive trust test: forged rows must not become a registrable claim."""
    runs, store = fresh_store(tmp_path, name="forged.sqlite3")
    run_id, claim_id = str(uuid4()), str(uuid4())
    bundle, artifacts_root = prepare_bundle(tmp_path, verified)
    seed_unverified_run(
        runs,
        run_id,
        claim_id,
        quote=bundle["packet"]["claim"]["quote"],
        track=bundle["packet"]["claim"]["track"],
    )
    with pytest.raises(ReconciliationRejected) as error:
        store.register_case(auth(), run_id, claim_id, bundle, artifacts_root)
    assert error.value.status in {404, 422}


def test_registration_rejects_a_claim_that_does_not_exist(store, verified, tmp_path):
    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, claim_id=str(uuid4()))
    assert error.value.status == 404


def test_registration_rejects_an_unknown_run(store, verified, tmp_path):
    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, run_id=str(uuid4()))
    assert error.value.status == 404


def test_registration_rejects_a_packet_whose_claim_text_is_invented(store, verified, tmp_path):
    def invent(bundle, _artifacts_root):
        bundle["packet"]["claim"]["quote"] = "a sentence nobody ever verified"

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=invent)
    assert error.value.code == "CLAIM_BINDING_MISMATCH"


def test_registration_rejects_a_claim_quote_that_is_merely_a_prefix(store, verified, tmp_path):
    """A substring is not the verified sentence; only exact text binds."""

    def truncate(bundle, _artifacts_root):
        bundle["packet"]["claim"]["quote"] = bundle["packet"]["claim"]["quote"][:-1]

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=truncate)
    assert error.value.code == "CLAIM_BINDING_MISMATCH"


def test_registration_rejects_a_track_that_contradicts_the_published_tag(store, verified, tmp_path):
    def flip(bundle, _artifacts_root):
        current = bundle["packet"]["claim"]["track"]
        bundle["packet"]["claim"]["track"] = "goal" if current != "goal" else "management"

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=flip)
    assert error.value.code == "CLAIM_BINDING_MISMATCH"


@pytest.mark.parametrize(
    "field", ["tenant_id", "company_id", "claim_id", "sustainability_document_version"]
)
def test_registration_rejects_an_identity_that_is_not_the_authorized_one(
    store, verified, tmp_path, field
):
    def swap(bundle, _artifacts_root):
        bundle["packet"]["identity"][field] = str(uuid4())

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=swap)
    assert error.value.code == "CASE_IDENTITY_MISMATCH"


def test_registration_rejects_a_source_whose_bytes_do_not_match_its_hash(store, verified, tmp_path):
    def corrupt(bundle, artifacts_root):
        document_id = next(iter(bundle["artifacts"]))
        (artifacts_root / bundle["artifacts"][document_id]["path"]).write_bytes(b"swapped")

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=corrupt)
    assert error.value.code == "ARTIFACT_UNVERIFIED"


def test_a_hash_that_matches_does_not_excuse_a_quote_that_does_not(store, verified, tmp_path):
    """Master's finding: never accept a source on hash agreement alone."""

    def drift(bundle, _artifacts_root):
        packet = bundle["packet"]
        other = next(
            source
            for source in packet["sources"]
            if source["source_id"] != packet["claim"]["source_id"]
        )
        other["quote"] = "이 문장은 원문에 존재하지 않습니다."
        document = bundle["documents"][other["document_id"]]
        document["source_bindings"][other["source_id"]]["quote"] = other["quote"]

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=drift)
    assert error.value.code == "SOURCE_LOCATOR_UNVERIFIED"


def test_registration_rejects_an_artifact_outside_the_operator_root(store, verified, tmp_path):
    def escape(bundle, _artifacts_root):
        bundle["artifacts"][next(iter(bundle["artifacts"]))]["path"] = "../escaped.txt"

    with pytest.raises(ReconciliationRejected):
        register(store, verified, tmp_path, mutate=escape)


def test_registration_rejects_a_document_that_no_artifact_backs(store, verified, tmp_path):
    def drop(bundle, _artifacts_root):
        bundle["artifacts"].pop(next(iter(bundle["artifacts"])))

    with pytest.raises(ReconciliationRejected):
        register(store, verified, tmp_path, mutate=drop)


def test_registration_rejects_an_unknown_bundle_key(store, verified, tmp_path):
    def extra(bundle, _artifacts_root):
        bundle["registries"] = {}

    with pytest.raises(ReconciliationRejected):
        register(store, verified, tmp_path, mutate=extra)


def test_a_rejected_registration_leaves_no_managed_bytes_behind(store, verified, tmp_path):
    def drift(bundle, _artifacts_root):
        packet = bundle["packet"]
        other = next(
            source
            for source in packet["sources"]
            if source["source_id"] != packet["claim"]["source_id"]
        )
        other["quote"] = "이 문장은 원문에 존재하지 않습니다."
        document = bundle["documents"][other["document_id"]]
        document["source_bindings"][other["source_id"]]["quote"] = other["quote"]

    before = sorted(path for path in store.artifact_root.rglob("*") if path.is_file())
    with pytest.raises(ReconciliationRejected):
        register(store, verified, tmp_path, mutate=drift)
    after = sorted(path for path in store.artifact_root.rglob("*") if path.is_file())
    assert after == before == []


def test_registration_preserves_the_tag_revision_that_supplied_the_input(
    registered, store, verified
):
    detail = registered[0]
    provenance = store.revision(auth("viewer"), detail["case_id"], 1)["provenance"]
    current = verified["claims"].current_tag(TENANT, verified["run_id"], verified["claim_id"])
    assert provenance["tag_revision"] == current["tag"]["tag_revision"]
    assert provenance["document_version_id"] == verified["claim"].document_version_id
    assert provenance["claim_source_quality"] == "verified"
    assert provenance["claim_track"] == verified["track"]


def test_an_imported_policy_approval_is_ignored(store, verified, tmp_path):
    def assert_approved(bundle, _artifacts_root):
        bundle["policy"]["approved"] = True
        bundle["policy"]["approved_by"] = "not-a-real-approver"

    detail = register(store, verified, tmp_path, mutate=assert_approved)
    assert detail["policy_approved"] is False
    assert detail["policy"]["approved"] is False
    assert detail["policy"]["approved_by"] is None


def test_an_imported_policy_registry_carries_no_authority(store, verified, tmp_path):
    def assert_registry(bundle, _artifacts_root):
        bundle["policies"] = {"deadbeef": {"approved": True}}

    detail = register(store, verified, tmp_path, mutate=assert_registry)
    assert "policies" not in detail
    evaluated = store.evaluate(auth("editor"), detail["case_id"], {}, '"1"', str(uuid4()))
    assert "policy_unapproved" in evaluated["latest_result"]["result"]["reason_codes"]


def test_c5_can_never_be_registered(store, verified, tmp_path):
    def to_c5(bundle, _artifacts_root):
        bundle["packet"]["item"] = "C5"

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=to_c5)
    assert error.value.code == "C5_DISABLED"


def test_the_synthetic_scope_must_match_the_anchored_document(store, verified, tmp_path):
    def to_real(bundle, _artifacts_root):
        bundle["packet"]["synthetic"] = False

    with pytest.raises(ReconciliationRejected) as error:
        register(store, verified, tmp_path, mutate=to_real)
    assert error.value.code == "SYNTHETIC_SCOPE_MISMATCH"


def test_an_editor_cannot_import_a_case(store, verified, tmp_path):
    bundle, artifacts_root = prepare_bundle(tmp_path, verified)
    with pytest.raises(ReconciliationRejected) as error:
        store.register_case(
            auth("editor"), verified["run_id"], verified["claim_id"], bundle, artifacts_root
        )
    assert error.value.status == 403


# --------------------------------------------------------------------------- #
# review, approval and CAS
# --------------------------------------------------------------------------- #


def review_body(coverage=False) -> dict:
    return {
        "reason": "confirmed against the original page",
        "confirm_source_bindings": True,
        "confirm_decision_bindings": True,
        "confirm_search_coverage": coverage,
    }


def test_review_advances_the_head_and_confirms_the_bindings(registered, store):
    detail = registered[0]
    updated = store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', str(uuid4()))
    assert updated["review_state"] == "reviewed"
    assert updated["revision"] == 2
    assert all(source["binding_state"] == "confirmed" for source in updated["sources"])


def test_review_records_the_exact_snapshot_it_confirmed(registered, store):
    detail = registered[0]
    updated = store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', str(uuid4()))
    event = store.revision(auth("viewer"), detail["case_id"], updated["revision"])["event"]
    assert len(event["documents_sha256"]) == 64
    assert len(event["packet_sha256"]) == 64
    assert event["actor"] == "user-reviewer"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"reason": "ok", "confirm_source_bindings": True, "confirm_decision_bindings": True},
        dict(review_body(), confirm_source_bindings=False),
        dict(review_body(), confirm_decision_bindings=False),
        dict(review_body(), confirm_search_coverage="yes"),
        dict(review_body(), reason="   "),
        dict(review_body(), status="matched"),
    ],
)
def test_review_requires_a_complete_explicit_confirmation(registered, store, body):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected):
        store.review(auth("reviewer"), detail["case_id"], body, '"1"', str(uuid4()))


def test_review_with_a_stale_if_match_is_rejected(registered, store):
    detail = registered[0]
    store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', str(uuid4()))
    with pytest.raises(ReconciliationRejected) as error:
        store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', str(uuid4()))
    assert error.value.code == "REVISION_CONFLICT"


@pytest.mark.parametrize("bad", [None, "1", '"0"', '"abc"', "", "*", '"1'])
def test_review_rejects_a_missing_or_malformed_if_match(registered, store, bad):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected):
        store.review(auth("reviewer"), detail["case_id"], review_body(), bad, str(uuid4()))


@pytest.mark.parametrize("key", [None, "", "short-key", "k" * 129, 17])
def test_review_rejects_an_unusable_idempotency_key(registered, store, key):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected):
        store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', key)


def test_repeating_an_idempotency_key_replays_the_same_response(registered, store):
    detail = registered[0]
    key = str(uuid4())
    first = store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', key)
    second = store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', key)
    assert first == second
    assert store.get_case(auth("viewer"), detail["case_id"])["revision"] == 2


def test_the_same_key_with_different_input_conflicts(registered, store):
    detail = registered[0]
    key = str(uuid4())
    store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', key)
    with pytest.raises(ReconciliationRejected) as error:
        store.review(auth("reviewer"), detail["case_id"], review_body(coverage=True), '"1"', key)
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_a_reviewer_cannot_approve_the_policy(registered, store):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected) as error:
        store.approve_policy(
            auth("reviewer"),
            detail["case_id"],
            {"approved": True, "reason": "not mine to give"},
            '"1"',
            str(uuid4()),
        )
    assert error.value.status == 403


def test_an_editor_cannot_review(registered, store):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected) as error:
        store.review(auth("editor"), detail["case_id"], review_body(), '"1"', str(uuid4()))
    assert error.value.status == 403


def test_a_viewer_cannot_evaluate(registered, store):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected) as error:
        store.evaluate(auth("viewer"), detail["case_id"], {}, '"1"', str(uuid4()))
    assert error.value.status == 403


def test_admin_policy_approval_records_actor_time_and_policy_hash(registered, store):
    detail = registered[0]
    updated = store.approve_policy(
        auth("admin"),
        detail["case_id"],
        {"approved": True, "reason": "approved by owner"},
        '"1"',
        str(uuid4()),
    )
    assert updated["policy_approved"] is True
    approval = store.revision(auth("viewer"), detail["case_id"], updated["revision"])["event"]
    assert approval["actor"] == "user-admin"
    assert len(approval["policy_sha256"]) == 64
    assert approval["created_at"].endswith("Z")
    assert approval["synthetic_only"] is True


def test_policy_approval_is_independent_of_review(registered, store):
    detail = registered[0]
    approved = store.approve_policy(
        auth("admin"), detail["case_id"], {"approved": True, "reason": "owner"}, '"1"', str(uuid4())
    )
    assert approved["policy_approved"] is True
    assert approved["review_state"] == "pending"


def test_parallel_review_and_approval_on_one_head_leaves_exactly_one_winner(registered, store):
    detail = registered[0]
    outcomes: list[object] = []
    barrier = threading.Barrier(2)

    def attempt(call):
        barrier.wait()
        try:
            outcomes.append(call())
        except (ReconciliationRejected, sqlite3.DatabaseError) as error:
            outcomes.append(error)

    threads = [
        threading.Thread(
            target=attempt,
            args=(
                lambda: store.review(
                    auth("reviewer"), detail["case_id"], review_body(), '"1"', str(uuid4())
                ),
            ),
        ),
        threading.Thread(
            target=attempt,
            args=(
                lambda: store.approve_policy(
                    auth("admin"),
                    detail["case_id"],
                    {"approved": True, "reason": "parallel"},
                    '"1"',
                    str(uuid4()),
                ),
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    succeeded = [outcome for outcome in outcomes if isinstance(outcome, dict)]
    assert len(succeeded) == 1, outcomes
    assert store.get_case(auth("viewer"), detail["case_id"])["revision"] == 2


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #


def approve_and_review(store, case_id):
    reviewed = store.review(auth("reviewer"), case_id, review_body(), '"1"', str(uuid4()))
    return store.approve_policy(
        auth("admin"),
        case_id,
        {"approved": True, "reason": "owner approval"},
        f'"{reviewed["revision"]}"',
        str(uuid4()),
    )


def test_a_pending_case_evaluates_to_a_visible_blocked_result(registered, store):
    detail = registered[0]
    updated = store.evaluate(auth("editor"), detail["case_id"], {}, '"1"', str(uuid4()))
    result = updated["latest_result"]["result"]
    assert result["execution_state"] == "blocked"
    assert result["status"] is None
    assert result["reason_codes"]


def test_a_reviewed_but_unapproved_case_stays_blocked_on_the_policy(registered, store):
    detail = registered[0]
    reviewed = store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', str(uuid4()))
    updated = store.evaluate(
        auth("editor"), detail["case_id"], {}, f'"{reviewed["revision"]}"', str(uuid4())
    )
    assert updated["latest_result"]["result"]["reason_codes"] == ["policy_unapproved"]


def test_an_approved_but_unreviewed_case_stays_blocked_on_the_bindings(registered, store):
    detail = registered[0]
    approved = store.approve_policy(
        auth("admin"), detail["case_id"], {"approved": True, "reason": "owner"}, '"1"', str(uuid4())
    )
    updated = store.evaluate(
        auth("editor"), detail["case_id"], {}, f'"{approved["revision"]}"', str(uuid4())
    )
    result = updated["latest_result"]["result"]
    assert result["execution_state"] == "blocked"
    assert "document_registry_missing" in result["reason_codes"]


def test_a_reviewed_and_approved_case_reaches_the_engine(registered, store):
    detail = registered[0]
    approved = approve_and_review(store, detail["case_id"])
    evaluated = store.evaluate(
        auth("editor"), detail["case_id"], {}, f'"{approved["revision"]}"', str(uuid4())
    )
    result = evaluated["latest_result"]["result"]
    assert result["schema_version"] == "1.1"
    assert result["execution_state"] == "completed"
    assert result["status"] == "matched"
    assert result["engine_version"] == "reconciliation-engine-1.1.0"
    assert evaluated["latest_result"]["projection"]["projection_schema_version"] == (
        "reconciliation-presentation-1"
    )


def test_evaluation_appends_an_immutable_result_revision(registered, store):
    detail = registered[0]
    first = store.evaluate(auth("editor"), detail["case_id"], {}, '"1"', str(uuid4()))
    second = store.evaluate(
        auth("editor"), detail["case_id"], {}, f'"{first["revision"]}"', str(uuid4())
    )
    assert second["revision"] > first["revision"]
    replayed = store.revision(auth("viewer"), detail["case_id"], first["latest_result"]["revision"])
    assert replayed["result"] == first["latest_result"]["result"]


@pytest.mark.parametrize(
    "statement",
    ["UPDATE reconciliation_revision SET actor='x'", "DELETE FROM reconciliation_revision"],
)
def test_a_stored_revision_cannot_be_updated_or_deleted(registered, store, statement):
    detail = registered[0]
    store.evaluate(auth("editor"), detail["case_id"], {}, '"1"', str(uuid4()))
    with pytest.raises(sqlite3.DatabaseError):
        with store.run_store.jobs._transaction() as db:
            db.execute(statement)


def test_the_registered_case_snapshot_cannot_be_rewritten(registered, store):
    assert registered[0]["case_id"]
    with pytest.raises(sqlite3.DatabaseError):
        with store.run_store.jobs._transaction() as db:
            db.execute("UPDATE reconciliation_case SET item='C4'")


def test_the_result_never_carries_a_grade_or_a_fabricated_status(registered, store):
    detail = registered[0]
    updated = store.evaluate(auth("editor"), detail["case_id"], {}, '"1"', str(uuid4()))
    result = updated["latest_result"]["result"]
    assert result["status"] in {None, "matched", "needs_explanation", "not_applicable"}
    assert result["execution_state"] in {"completed", "blocked", "not_run"}
    for forbidden in ("evidence_grade", "label", "sublabel", "score"):
        assert forbidden not in result


def test_evaluate_refuses_any_payload_at_all(registered, store):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected):
        store.evaluate(
            auth("editor"), detail["case_id"], {"policy": {"approved": True}}, '"1"', str(uuid4())
        )


def test_mutating_a_managed_source_after_registration_is_detected(registered, store):
    detail = registered[0]
    source_id = detail["sources"][0]["source_id"]
    approved = approve_and_review(store, detail["case_id"])
    store.managed_path(TENANT, detail["case_id"], source_id).write_bytes(b"tampered")
    with pytest.raises(ReconciliationRejected):
        store.source_content(auth("viewer"), detail["case_id"], source_id)
    updated = store.evaluate(
        auth("editor"), detail["case_id"], {}, f'"{approved["revision"]}"', str(uuid4())
    )
    assert updated["latest_result"]["result"]["execution_state"] == "blocked"


# --------------------------------------------------------------------------- #
# tenancy and durability
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "call",
    [
        lambda store, case, source: store.get_case(auth("viewer", FOREIGN), case),
        lambda store, case, source: store.revision(auth("viewer", FOREIGN), case, 1),
        lambda store, case, source: store.source_content(auth("viewer", FOREIGN), case, source),
        lambda store, case, source: store.review(
            auth("reviewer", FOREIGN), case, review_body(), '"1"', str(uuid4())
        ),
        lambda store, case, source: store.approve_policy(
            auth("admin", FOREIGN), case, {"approved": True, "reason": "x" * 6}, '"1"', str(uuid4())
        ),
        lambda store, case, source: store.evaluate(
            auth("editor", FOREIGN), case, {}, '"1"', str(uuid4())
        ),
    ],
)
def test_a_foreign_tenant_is_refused_as_not_found(registered, store, call):
    detail = registered[0]
    with pytest.raises(ReconciliationRejected) as error:
        call(store, detail["case_id"], detail["sources"][0]["source_id"])
    assert error.value.status == 404


def test_case_survives_reopening_the_database(registered, store, verified, tmp_path):
    detail = registered[0]
    store.review(auth("reviewer"), detail["case_id"], review_body(), '"1"', str(uuid4()))
    service = verified["service"]
    reopened = LocalReconciliationStore(
        service.store.path,
        tmp_path / "managed",
        run_store=service.store,
        claims=verified["claims"],
        tags=verified["tags"],
    )
    after = reopened.get_case(auth("viewer"), detail["case_id"])
    assert after["review_state"] == "reviewed"
    assert after["revision"] == 2
    assert all(source["binding_state"] == "confirmed" for source in after["sources"])


def test_listing_returns_only_cases_for_the_requested_claim(registered, store, verified):
    detail, run_id, claim_id, _bundle, _root = registered
    listed = store.list_for_claim(auth("viewer"), run_id, claim_id)
    assert detail["case_id"] in {item["case_id"] for item in listed["items"]}
    assert all(item["claim_id"] == claim_id for item in listed["items"])
    assert store.list_for_claim(auth("viewer"), run_id, str(uuid4()))["items"] == []
    assert store.list_for_claim(auth("viewer", FOREIGN), run_id, claim_id)["items"] == []
