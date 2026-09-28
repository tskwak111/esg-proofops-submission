"""Reviewed-tag-head consumers: reconciliation anchoring and the C-item trigger map.

One REAL `ReviewService` revision drives both regressions. The fixture run is the
existing `verified_setup` one (real parser/extractor/tagger workers, real SQLite,
`local_synthetic` tagging) and the review is committed through the unchanged
`resolve_review`; nothing is hand-written into `tag_revision`/`claim_head`, and no
financial gold set is read. Track `performance` -> `management` is a track
transition the review contract already allows (`reviews.parse_resolution` accepts
any track in `MAPPINGS` and then demands that track's complete element set), and
`management.M2` is the canonical producer of the `org_boundary` fact.

Two consumers of that reviewed head are pinned here:

* `LocalReconciliationStore._verified_claim` must anchor a case against the track
  of the CURRENT accepted head, not the original preliminary tag packet, and must
  record the atomic `claim_head` revision rather than a row's self-reported one.
* `linkage_exchange.TRIGGER_TAG_MAP` must recognise the canonical `org_boundary`
  fact name, so an accepted organisational-boundary review is not silently lost
  on the way to the C1 packet builder.

No grade or label is asserted anywhere: the rules engine's own decision is
untouched, and the synthetic FinancialContext proves nothing about real financial
readiness.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest
from proofops.adapters.local.reconciliation_store import (
    LocalReconciliationStore,
    ReconciliationRejected,
)
from proofops.application.authorization import AuthContext
from proofops.application.linkage_exchange import _verified_triggers

from tests.integration.test_linkage_exchange_cli_real_run import (
    _cli_args,
    _load_cli_module,
    _spy_build_packet,
    _synthetic_financial_context,
    _use_fixture_parser,
)
from tests.integration.test_local_parser_runner import TENANT
from tests.reconciliation.test_product_store import auth, prepare_bundle

ORIGINAL_TRACK = "performance"
REVIEWED_TRACK = "management"
# management.M2 -> ("org_boundary",); see packages/proofops/domain/rules/management.py.
BOUNDARY_ELEMENT = "M2"
BOUNDARY_FACT = "org_boundary"


@pytest.fixture(scope="module")
def reviewed(tmp_path_factory):
    """A real run whose accepted head moved to another track. Built once; costly."""
    from _pytest.monkeypatch import MonkeyPatch

    monkeypatch = MonkeyPatch()
    try:
        yield _track_changing_review(tmp_path_factory.mktemp("reviewed"), monkeypatch)
    finally:
        monkeypatch.undo()


def _track_changing_review(tmp_path, monkeypatch) -> dict:
    from tests.integration.test_local_tag_runner import verified_setup

    service, run_id, runner, _now, _stream = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    with service.store.jobs._transaction() as db:
        heads = service.store.jobs._all(db, TENANT, run_id, "review_head")
    assert len(heads) == 1
    review_id, claim_id = heads[0]["review_id"], heads[0]["claim_id"]

    inputs = runner.tags.load_inputs(TENANT, run_id, claim_id)
    # The stale source both regressions are about: the tag-stage packet is frozen
    # at publication and is never rewritten by a later accepted review.
    assert inputs.packet.to_dict()["track"] == ORIGINAL_TRACK
    claim = runner.claims.get(TENANT, run_id, claim_id)
    assert claim.source_quality == "verified"
    refs = [asdict(ref) for ref in claim.source_refs]
    assert refs

    def element(element_id):
        present = element_id == BOUNDARY_ELEMENT
        return {
            "element_id": element_id,
            "state": "present" if present else "unknown",
            "evidence_refs": refs if present else [],
            "normalized_value": claim.source_refs[0].quote if present else None,
            "credited_from": None,
            "reason_code": None,
        }

    actor = AuthContext(
        "operator-0", TENANT, "reviewer", frozenset({"viewer", "reviewer"}), "session-0"
    )
    resolved = runner.reviews.resolve_review(
        actor,
        review_id,
        {
            "base_tag_revision": 1,
            "track": REVIEWED_TRACK,
            "reason": "reviewed-head consumer regression: real track transition only",
            "elements": [element(eid) for eid in ("M1", "M2", "M3", "M4", "M5", "M6")],
        },
        '"1"',
        "reviewed-head-consumers-0002",
        reopen=False,
    )
    assert resolved["new_tag_revision"] == 2
    with service.store.jobs._transaction() as db:
        head = service.store.jobs._get(db, TENANT, run_id, "claim_head", claim_id)
    assert head["tag_revision"] == 2
    return {
        "service": service,
        "run_id": run_id,
        "claim_id": claim_id,
        "claim": claim,
        "claims": runner.claims,
        "tags": runner.tags,
        "runner": runner,
        "track": REVIEWED_TRACK,
        "company_id": service.store.snapshot(TENANT, run_id)["document"]["company"]["company_id"],
    }


# --------------------------------------------------------------------------- #
# A. reconciliation anchoring
# --------------------------------------------------------------------------- #


def _store(reviewed, tmp_path):
    service = reviewed["service"]
    return LocalReconciliationStore(
        service.store.path,
        tmp_path / "managed",
        run_store=service.store,
        claims=reviewed["claims"],
        tags=reviewed["tags"],
    )


def test_registration_anchors_on_the_reviewed_head_track(reviewed, tmp_path):
    """A packet bound to the CURRENT accepted track registers and is recorded as such.

    Before the fix `_verified_track` read the original preliminary packet, so a
    packet built from the reviewed head was rejected as CLAIM_BINDING_MISMATCH.
    """
    store = _store(reviewed, tmp_path)
    bundle, artifacts_root = prepare_bundle(tmp_path, reviewed)
    assert bundle["packet"]["claim"]["track"] == REVIEWED_TRACK

    detail = store.register_case(
        auth(), reviewed["run_id"], reviewed["claim_id"], bundle, artifacts_root
    )
    provenance = store.revision(auth("viewer"), detail["case_id"], 1)["provenance"]
    assert provenance["claim_track"] == REVIEWED_TRACK
    # The atomic claim_head revision, not a row's self-reported value.
    with reviewed["service"].store.jobs._transaction() as db:
        head = reviewed["service"].store.jobs._get(
            db, TENANT, reviewed["run_id"], "claim_head", reviewed["claim_id"]
        )
    assert provenance["tag_revision"] == head["tag_revision"] == 2


def test_registration_refuses_the_superseded_preliminary_track(reviewed, tmp_path):
    """The withdrawn original track must not still anchor a case.

    Before the fix this was the ONLY track that registered, which is exactly the
    stale-review leak: an accepted review had already moved the claim off it.
    """
    store = _store(reviewed, tmp_path)
    bundle, artifacts_root = prepare_bundle(tmp_path, reviewed)
    bundle["packet"]["claim"]["track"] = ORIGINAL_TRACK
    for entry in bundle["documents"].values():
        entry["decision_binding"]["claim"]["track"] = ORIGINAL_TRACK

    with pytest.raises(ReconciliationRejected) as rejected:
        store.register_case(
            auth(), reviewed["run_id"], reviewed["claim_id"], bundle, artifacts_root
        )
    assert rejected.value.code == "CLAIM_BINDING_MISMATCH"


def test_a_head_row_that_contradicts_the_atomic_claim_head_is_refused(
    reviewed, tmp_path, monkeypatch
):
    """Heads a correct store cannot produce must block, never anchor or fall back.

    `current_tag` is stubbed only to present those heads; the checks under test
    are the reconciliation store's own. `missing_identity` and `empty_confirmed`
    are the cases a "check only the keys that are there" guard would have let
    through, and `null_confirmed_after_review` is the stale-fallback case: a
    reviewed head with no confirmed tags must not reach back to the preliminary
    packet.
    """
    store = _store(reviewed, tmp_path)
    bundle, artifacts_root = prepare_bundle(tmp_path, reviewed)
    with reviewed["service"].store.jobs._transaction() as db:
        stored = reviewed["service"].store.jobs._get(
            db, TENANT, reviewed["run_id"], "tag_revision", f"{reviewed['claim_id']}:{2:010}"
        )

    def head(**changes):
        confirmed = changes.pop("confirmed", None)
        raw = dict(stored["confirmed_tags"]) | (confirmed or {})
        return {
            "tag": dict(stored) | {"confirmed_tags": raw} | changes,
            "decision": None,
            "epoch": 0,
        }

    foreign = "22222222-2222-4222-8222-222222222222"
    cases = {
        # Self-consistent but not the atomic head: only claim_head catches it.
        "tampered_both_revisions": head(tag_revision=3, confirmed={"tag_revision": 3}),
        "cross_tenant": head(confirmed={"tenant_id": foreign}),
        "foreign_version": head(confirmed={"document_version_id": foreign}),
        "stale_confirmed_revision": head(confirmed={"tag_revision": 1}),
        "missing_identity": {
            "tag": dict(stored) | {"confirmed_tags": {"track": REVIEWED_TRACK}},
            "decision": None,
            "epoch": 0,
        },
        "empty_confirmed": {
            "tag": dict(stored) | {"confirmed_tags": {}},
            "decision": None,
            "epoch": 0,
        },
        "null_confirmed_after_review": {
            "tag": dict(stored) | {"confirmed_tags": None},
            "decision": None,
            "epoch": 0,
        },
        "broken_snapshot_pin": head(input_snapshot_sha256="0" * 64),
    }
    for label, payload in cases.items():
        monkeypatch.setattr(
            type(reviewed["claims"]), "current_tag", lambda *a, _head=payload, **k: _head
        )
        with pytest.raises(ReconciliationRejected) as rejected:
            store.register_case(
                auth(), reviewed["run_id"], reviewed["claim_id"], bundle, artifacts_root
            )
        assert rejected.value.code == "TAG_HEAD_UNACCOUNTABLE", label


def test_a_head_track_outside_the_known_tracks_is_refused(reviewed, tmp_path, monkeypatch):
    """A non-empty string is not enough: the track must be a real rubric track."""
    store = _store(reviewed, tmp_path)
    bundle, artifacts_root = prepare_bundle(tmp_path, reviewed)
    with reviewed["service"].store.jobs._transaction() as db:
        stored = reviewed["service"].store.jobs._get(
            db, TENANT, reviewed["run_id"], "tag_revision", f"{reviewed['claim_id']}:{2:010}"
        )
    invented = {
        "tag": dict(stored)
        | {"confirmed_tags": dict(stored["confirmed_tags"]) | {"track": "reconciliation"}},
        "decision": None,
        "epoch": 0,
    }
    monkeypatch.setattr(
        type(reviewed["claims"]), "current_tag", lambda *a, _head=invented, **k: _head
    )
    with pytest.raises(ReconciliationRejected) as rejected:
        store.register_case(
            auth(), reviewed["run_id"], reviewed["claim_id"], bundle, artifacts_root
        )
    assert rejected.value.code == "CLAIM_NOT_VERIFIED"


# --------------------------------------------------------------------------- #
# B. canonical trigger fact name
# --------------------------------------------------------------------------- #


def _head_tags(reviewed, cli):
    tags, revision = cli.reviewed_head_tags(
        reviewed["claims"],
        reviewed["claim"],
        tenant_id=TENANT,
        run_id=reviewed["run_id"],
        claim_id=reviewed["claim_id"],
    )
    assert revision == 2 and tags is not None and tags.tag_revision == 2
    return tags


def test_a_real_review_produces_the_canonical_boundary_fact_name(reviewed):
    """The producer side: an accepted M2 review records `org_boundary`, not the
    C-item trigger spelling. This is what the consumer has to recognise."""
    cli = _load_cli_module()
    tags = _head_tags(reviewed, cli)
    assert tags.track == REVIEWED_TRACK
    present = {fact.name for fact in tags.facts if fact.state == "present"}
    assert present == {BOUNDARY_FACT}
    assert "organizational_boundary" not in {fact.name for fact in tags.facts}


def test_the_reviewed_boundary_fact_reaches_the_c1_trigger(reviewed):
    """`_verified_triggers` must map the real fact to the contract trigger.

    Verification guards are unchanged: the fact still has to be present with a
    verified citation, an accepted binding and real evidence refs.
    """
    cli = _load_cli_module()
    triggers = _verified_triggers(_head_tags(reviewed, cli))
    assert [(t.fact_name, t.trigger_element) for t in triggers] == [
        (BOUNDARY_FACT, "organizational_boundary")
    ]
    assert triggers[0].normalized_value == reviewed["claim"].source_refs[0].quote
    assert triggers[0].evidence_refs and all(
        ref.verification_state == "verified" for ref in triggers[0].evidence_refs
    )


def test_the_c1_builder_no_longer_reports_the_reviewed_trigger_as_absent(
    reviewed, tmp_path, monkeypatch, capsys
):
    """End of the chain: the CLI's C1 build no longer blocks on `no_verified_trigger`.

    The FinancialContext is explicitly synthetic, so the remaining block is a
    financial-context gap, never a claim of real packet readiness.
    """
    cli = _load_cli_module()
    _use_fixture_parser(monkeypatch, reviewed["runner"])
    seen = _spy_build_packet(monkeypatch, cli)
    cli._cmd_build_packet(
        _cli_args(
            cli,
            tenant_id=TENANT,
            run_id=reviewed["run_id"],
            claim_id=reviewed["claim_id"],
            database_path=reviewed["service"].store.path,
            item="C1",
            financial_context=_synthetic_financial_context(tmp_path, reviewed["company_id"]),
        )
    )
    report = capsys.readouterr().out
    assert len(seen) == 1 and seen[0]["tags"].tag_revision == 2
    assert "no_verified_trigger" not in report
    assert "no_matching_item_trigger" not in report
