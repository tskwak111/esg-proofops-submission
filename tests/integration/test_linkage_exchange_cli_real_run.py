"""CLI smoke test against a REAL stored/committed local run (not a synthetic triple).

Builds a genuine extraction-committed run through the same fixture the
extraction/parser integration tests use (extraction_setup -> LocalExtractRunner
.run_once), then invokes scripts/linkage_exchange_cli.py's build-packet
function directly against that run's real sqlite database path. Confirms:
missing --financial-context cleanly blocks (never fabricates), and a real
claim_id from the actual committed extraction is found (not a claim_not_found
false negative).

The second half of this module is the stale-review-consumer regression. It
commits REAL reviews (and a real re-review) through the unchanged
`ReviewService` on a real tag-published run, and pins that build-packet
consumes the LATEST ACCEPTED immutable tag head -- not the tag-stage model
consensus snapshot, which keeps serving pre-review tags after every accepted
re-review. Both directions are covered: a review that makes a trigger present
must reach the builder, and a later review that removes it must not leak the
formerly-present positive. The main fixtures are explicitly synthetic
(`local_synthetic=True`, generic fixture company); the real NAVER
`.local/r24-cli-rereview` state is only used by a read-only supplementary
check that copies it and asserts nothing about real financial binding.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
from dataclasses import asdict
from pathlib import Path

import pytest

from tests.integration.test_local_extract_runner import extraction_setup
from tests.integration.test_local_parser_runner import TENANT

CLI_PATH = Path(__file__).resolve().parents[2] / "scripts/linkage_exchange_cli.py"

# The NAVER run an earlier R24 probe re-reviewed to tag_revision 3 (M1 present).
# Read-only supplement only; skipped when the local state is absent.
R24_REREVIEW_STATE = Path(__file__).resolve().parents[2] / ".local/r24-cli-rereview"
R24_TENANT = "b36026f6-c2a7-4653-82b0-c4c554a1515f"
R24_RUN = "d379689e-b597-4920-a84e-6fbe72d55baf"
R24_CLAIM = "724c1911-471a-5a4d-9169-9a0341466323"


def _load_cli_module():
    spec = importlib.util.spec_from_file_location("linkage_exchange_cli", CLI_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_packet_against_real_committed_run_blocks_without_financial_context(
    tmp_path, monkeypatch, capsys
):
    import subprocess

    real_run = subprocess.run

    def safe_run(cmd, *a, **kw):
        if cmd and cmd[0] == "/bin/ps":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"1024\n")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", safe_run)

    service, run_id, runner, _now, _ = extraction_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    from proofops.adapters.local.claim_store import LocalClaimStore

    claims = LocalClaimStore(service.store, service.uploads, runner.parser)
    discovery = claims.load(TENANT, run_id)
    assert discovery.claims, "fixture must produce at least one real claim"
    claim_id = discovery.claims[0].claim_id

    cli = _load_cli_module()
    args = argparse.Namespace(
        tenant_id=TENANT,
        run_id=run_id,
        claim_id=claim_id,
        item="C1",
        database_path=service.store.path,
        financial_context=None,
        period_start=None,
        period_end=None,
        sr_published_at=None,
        trusted_company_id=None,
        contract_dir=str(cli.CONTRACT_DIR_DEFAULT),
    )
    exit_code = cli._cmd_build_packet(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "missing_financial_context" in captured.out
    assert "blocked" in captured.out


def test_build_packet_cli_rejects_untrusted_company_id_override(tmp_path, monkeypatch, capsys):
    import subprocess

    real_run = subprocess.run

    def safe_run(cmd, *a, **kw):
        if cmd and cmd[0] == "/bin/ps":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"1024\n")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", safe_run)

    service, run_id, runner, _now, _ = extraction_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    from proofops.adapters.local.claim_store import LocalClaimStore

    claims = LocalClaimStore(service.store, service.uploads, runner.parser)
    discovery = claims.load(TENANT, run_id)
    claim_id = discovery.claims[0].claim_id

    cli = _load_cli_module()
    args = argparse.Namespace(
        tenant_id=TENANT,
        run_id=run_id,
        claim_id=claim_id,
        item="C1",
        database_path=service.store.path,
        financial_context=None,
        period_start=None,
        period_end=None,
        sr_published_at=None,
        trusted_company_id="untrusted-spoofed-company-id",
        contract_dir=str(cli.CONTRACT_DIR_DEFAULT),
    )
    exit_code = cli._cmd_build_packet(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "company_mismatch" in captured.out


def test_build_packet_cli_rejects_untrusted_caller_sr_published_at_override(
    tmp_path, monkeypatch, capsys
):
    """Tiny regression: CLI must not treat caller CLI date as trusted source, nor use created_at."""
    import subprocess

    real_run = subprocess.run

    def safe_run(cmd, *a, **kw):
        if cmd and cmd[0] == "/bin/ps":
            return subprocess.CompletedProcess(cmd, 0, stdout=b"1024\n")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", safe_run)

    service, run_id, runner, _now, _ = extraction_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    from proofops.adapters.local.claim_store import LocalClaimStore

    claims = LocalClaimStore(service.store, service.uploads, runner.parser)
    discovery = claims.load(TENANT, run_id)
    claim_id = discovery.claims[0].claim_id

    cli = _load_cli_module()
    args = argparse.Namespace(
        tenant_id=TENANT,
        run_id=run_id,
        claim_id=claim_id,
        item="C1",
        database_path=service.store.path,
        financial_context=None,
        period_start=None,
        period_end=None,
        sr_published_at="2025-06-30",
        trusted_company_id=None,
        financial_local_paths=None,
        contract_dir=str(cli.CONTRACT_DIR_DEFAULT),
    )
    exit_code = cli._cmd_build_packet(args)
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "period_mismatch" in captured.out
    assert "not an approved trusted publication source" in captured.out


# ---------------------------------------------------------------------------
# Stale-review-consumer regression (R24).
#
# `_cmd_build_packet` used to read `LocalTagStore.load_inputs(...).consensus
# .confirmed_tags`: the ORIGINAL model-agreement snapshot frozen at tag-stage
# publication. Accepted reviews write new immutable `tag_revision` rows and move
# the atomic `claim_head`; that snapshot is never rewritten. Everything below
# drives real reviews through the unchanged ReviewService and pins the consumer
# to the head instead.
# ---------------------------------------------------------------------------

TRIGGER_FACT = "quantitative_or_qualified_ordinal"


def _reviewed_run(tmp_path, monkeypatch):
    """A real tag-published run plus the handles needed to commit real reviews.

    Uses the existing `verified_setup` fixture (real parser/extractor/tagger
    workers, real SQLite store, explicitly synthetic tagging mode) and the real
    `ReviewService` the tag runner itself published the review with. Nothing is
    hand-written into `tag_revision`/`claim_head`.
    """
    from proofops.application.authorization import AuthContext

    from tests.integration.test_local_tag_runner import verified_setup

    service, run_id, runner, _now, _stream = verified_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "needs_review"
    with service.store.jobs._transaction() as db:
        heads = service.store.jobs._all(db, TENANT, run_id, "review_head")
    assert len(heads) == 1
    review_id, claim_id = heads[0]["review_id"], heads[0]["claim_id"]
    inputs = runner.tags.load_inputs(TENANT, run_id, claim_id)
    # The stale source this regression is about: the tag-stage snapshot never
    # confirmed anything for this claim, so any confirmed tag the CLI reports
    # can only have come from an accepted review head.
    assert inputs.consensus.confirmed_tags is None
    claim = runner.claims.get(TENANT, run_id, claim_id)
    refs = [asdict(ref) for ref in claim.source_refs]
    assert refs

    def elements(p1_state, normalized_value):
        out = []
        for element in inputs.consensus.candidate_elements:
            data = asdict(element) | {"evidence_refs": []}
            if data["element_id"] == "P1":
                data |= {
                    "state": p1_state,
                    "evidence_refs": refs if p1_state == "present" else [],
                    "normalized_value": normalized_value,
                }
            out.append(data)
        return out

    actor = AuthContext(
        "operator-0", TENANT, "reviewer", frozenset({"viewer", "reviewer"}), "session-0"
    )

    def resolve(*, base_tag_revision, p1_state, normalized_value, key, reopen):
        body = dict(
            base_tag_revision=base_tag_revision,
            track="performance",
            reason="R24 stale-head regression: real reviewed tagging only",
            elements=elements(p1_state, normalized_value),
        )
        return runner.reviews.resolve_review(
            actor, review_id, body, f'"{base_tag_revision}"', key, reopen=reopen
        )

    return service, run_id, runner, claim, claim_id, resolve


def _synthetic_financial_context(tmp_path, company_id) -> str:
    """An explicitly synthetic FinancialContext: no real financial binding.

    `synthetic=True` and every external identity field is a named placeholder.
    Nothing here asserts a real DART/financial document exists, and no
    financial source is cited, so no byte verification is claimed either.
    """
    path = tmp_path / "synthetic-financial-context.json"
    path.write_text(
        json.dumps(
            {
                "synthetic": True,
                "company_id": company_id,
                "package_id": "synthetic-r24-stale-head-regression",
                "dart_corp_code": "00000000",
                "financial_document_version": "synthetic-financial-v0",
                "financial_fiscal_year": 2025,
                "consolidation": "unknown",
                "financial_period_start": None,
                "financial_period_end": None,
                "financial_published_at": None,
                "rcept_no": None,
                "as_of_date": "2026-09-22",
                "financial": {
                    "raw": None,
                    "normalized": None,
                    "kind": "unknown",
                    "unit": None,
                    "source_id": None,
                },
                "financial_sources": [],
            }
        )
    )
    return str(path)


def _cli_args(cli, *, tenant_id, run_id, claim_id, database_path, item, financial_context):
    return argparse.Namespace(
        tenant_id=tenant_id,
        run_id=run_id,
        claim_id=claim_id,
        item=item,
        database_path=database_path,
        financial_context=financial_context,
        period_start=None,
        period_end=None,
        sr_published_at=None,
        trusted_company_id=None,
        financial_local_paths=None,
        contract_dir=str(cli.CONTRACT_DIR_DEFAULT),
    )


def _spy_build_packet(monkeypatch, cli):
    """Capture the exact tags object the CLI hands to the unchanged builder."""
    from proofops.application import linkage_exchange

    seen = []
    real = linkage_exchange.build_packet

    def spy(**kwargs):
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(linkage_exchange, "build_packet", spy)
    return seen


def _use_fixture_parser(monkeypatch, runner):
    """Let the CLI replay the fixture's own prepared parser artifacts.

    Only the parser composition is substituted. The store, uploads, claim
    replay, review service, rules engine and the CLI's own guards all stay the
    real ones; the CLI otherwise constructs an OpenDataLoaderParser that cannot
    replay this fixture's synthetic prepared artifacts.
    """
    monkeypatch.setattr(
        "proofops.adapters.parsing.opendataloader.OpenDataLoaderParser",
        lambda *_a, **_k: runner.parser,
    )


def test_build_packet_consumes_the_accepted_review_head_not_the_model_consensus(
    tmp_path, monkeypatch, capsys
):
    """A real review that makes a trigger present must reach the builder.

    The tag-stage consensus for this claim is unconfirmed (`confirmed_tags is
    None`), so the pre-fix consumer could only ever report
    `missing_confirmed_tags`. After the fix the builder receives the accepted
    head's own ConfirmedTags at `tag_revision == 2`, with the present
    `quantitative_or_qualified_ordinal` fact and its verified evidence refs.
    No grade is asserted anywhere here; the engine's own decision is untouched.
    """
    service, run_id, runner, claim, claim_id, resolve = _reviewed_run(tmp_path, monkeypatch)
    resolved = resolve(
        base_tag_revision=1,
        p1_state="present",
        normalized_value=claim.source_refs[0].quote,
        key="r24-stale-head-review-0002",
        reopen=False,
    )
    assert resolved["new_tag_revision"] == 2

    cli = _load_cli_module()
    _use_fixture_parser(monkeypatch, runner)
    seen = _spy_build_packet(monkeypatch, cli)
    args = _cli_args(
        cli,
        tenant_id=TENANT,
        run_id=run_id,
        claim_id=claim_id,
        database_path=service.store.path,
        item="C1",
        financial_context=_synthetic_financial_context(
            tmp_path, service.store.snapshot(TENANT, run_id)["document"]["company"]["company_id"]
        ),
    )
    assert cli._cmd_build_packet(args) == 1
    report = json.loads(capsys.readouterr().out)

    assert len(seen) == 1
    tags = seen[0]["tags"]
    assert tags is not None, "builder was handed no tags at all"
    assert tags.tag_revision == 2
    assert tags.claim_id == claim_id and tags.tenant_id == TENANT
    assert tags.document_version_id == claim.document_version_id
    present = {fact.name for fact in tags.facts if fact.state == "present"}
    assert TRIGGER_FACT in present
    trigger_fact = next(fact for fact in tags.facts if fact.name == TRIGGER_FACT)
    assert trigger_fact.citation_verified and trigger_fact.binding_accepted
    assert trigger_fact.evidence_refs and all(
        ref.verification_state == "verified" and ref.page_num and ref.bbox
        for ref in trigger_fact.evidence_refs
    )
    # The reviewed trigger was really seen: C1 asks for boundary/scope, so the
    # block is the item-trigger mismatch, not "there are no confirmed tags".
    assert report["reason"] == "no_matching_item_trigger"
    # And the stale snapshot still says nothing, i.e. this came from the head.
    assert runner.tags.load_inputs(TENANT, run_id, claim_id).consensus.confirmed_tags is None


def test_later_review_removing_a_present_trigger_cannot_leak_a_stale_positive(
    tmp_path, monkeypatch, capsys
):
    """A re-review that removes the trigger must remove it from the packet input.

    Revision 2 really did record the present trigger (asserted against the
    stored immutable row), revision 3 withdraws it, and the builder must see
    revision 3 with no present trigger at all -- no fallback to the older
    revision and no fallback to the tag-stage consensus.
    """
    service, run_id, runner, claim, claim_id, resolve = _reviewed_run(tmp_path, monkeypatch)
    assert (
        resolve(
            base_tag_revision=1,
            p1_state="present",
            normalized_value=claim.source_refs[0].quote,
            key="r24-stale-head-review-0002",
            reopen=False,
        )["new_tag_revision"]
        == 2
    )
    assert (
        resolve(
            base_tag_revision=2,
            p1_state="unknown",
            normalized_value=None,
            key="r24-stale-head-review-0003",
            reopen=True,
        )["new_tag_revision"]
        == 3
    )
    jobs = service.store.jobs
    with jobs._transaction() as db:
        stale = jobs._get(db, TENANT, run_id, "tag_revision", f"{claim_id}:0000000002")
        assert jobs._get(db, TENANT, run_id, "claim_head", claim_id)["tag_revision"] == 3
    stale_present = {
        fact["name"] for fact in stale["confirmed_tags"]["facts"] if fact["state"] == "present"
    }
    assert TRIGGER_FACT in stale_present, "revision 2 must really carry the removed trigger"

    cli = _load_cli_module()
    _use_fixture_parser(monkeypatch, runner)
    seen = _spy_build_packet(monkeypatch, cli)
    args = _cli_args(
        cli,
        tenant_id=TENANT,
        run_id=run_id,
        claim_id=claim_id,
        database_path=service.store.path,
        item="C1",
        financial_context=_synthetic_financial_context(
            tmp_path, service.store.snapshot(TENANT, run_id)["document"]["company"]["company_id"]
        ),
    )
    assert cli._cmd_build_packet(args) == 1
    report = json.loads(capsys.readouterr().out)

    tags = seen[0]["tags"]
    assert tags is not None and tags.tag_revision == 3
    assert TRIGGER_FACT not in {fact.name for fact in tags.facts if fact.state == "present"}
    withdrawn = next(fact for fact in tags.facts if fact.name == TRIGGER_FACT)
    # The withdrawal is preserved as the reviewer recorded it, not converted
    # into an absence of evidence.
    assert withdrawn.state == "unknown"
    assert report["reason"] == "no_verified_trigger"


def test_build_packet_blocks_a_head_it_cannot_account_for(tmp_path, monkeypatch):
    """Malformed / cross-tenant / stale-revision heads block instead of passing.

    `current_tag` is stubbed here only to present heads a correct store cannot
    produce; the validation under test is the CLI's own, and each case must
    raise rather than silently accept or silently fall back.
    """
    service, run_id, runner, claim, claim_id, resolve = _reviewed_run(tmp_path, monkeypatch)
    assert (
        resolve(
            base_tag_revision=1,
            p1_state="present",
            normalized_value=claim.source_refs[0].quote,
            key="r24-stale-head-review-0002",
            reopen=False,
        )["new_tag_revision"]
        == 2
    )
    cli = _load_cli_module()
    claims = runner.claims
    good, revision = cli.reviewed_head_tags(
        claims, claim, tenant_id=TENANT, run_id=run_id, claim_id=claim_id
    )
    assert revision == 2 and good is not None
    with service.store.jobs._transaction() as db:
        stored = service.store.jobs._get(
            db, TENANT, run_id, "tag_revision", f"{claim_id}:0000000002"
        )

    def head(**changes):
        raw = dict(stored["confirmed_tags"]) | changes.pop("confirmed", {})
        tag = dict(stored) | {"confirmed_tags": raw} | changes
        return {"tag": tag, "decision": None, "epoch": 1}

    foreign_tenant = "22222222-2222-4222-8222-222222222222"
    cases = {
        "cross_tenant": head(confirmed={"tenant_id": foreign_tenant}),
        "stale_revision": head(confirmed={"tag_revision": 1}),
        "foreign_claim": head(confirmed={"claim_id": str(claim.document_version_id)}),
        "foreign_version": head(confirmed={"document_version_id": foreign_tenant}),
        "not_an_object": head(confirmed_tags="present"),
        "unknown_field": head(confirmed={"evidence_grade": "E1"}),
        "broken_snapshot_pin": head(input_snapshot_sha256="0" * 64),
        # Self-consistent but not the atomic head: the row and its confirmed
        # tags both claim revision 3 while claim_head still points at 2. Only
        # reading claim_head itself catches this, which is why the row's own
        # tag_revision is never treated as the authority.
        "tampered_both_revisions": head(tag_revision=3, confirmed={"tag_revision": 3}),
    }
    for label, payload in cases.items():
        monkeypatch.setattr(
            type(claims), "current_tag", lambda *a, _head=payload, **k: _head, raising=True
        )
        with pytest.raises(cli.ReviewedHeadRejected, match=r".+"):
            cli.reviewed_head_tags(
                claims, claim, tenant_id=TENANT, run_id=run_id, claim_id=claim_id
            )
            pytest.fail(f"head case accepted instead of blocked: {label}")

    # An explicitly unconfirmed head is NOT an error and NOT a stale fallback:
    # it simply carries no confirmed tags.
    unconfirmed = head(confirmed_tags=None)
    monkeypatch.setattr(
        type(claims), "current_tag", lambda *a, _head=unconfirmed, **k: _head, raising=True
    )
    assert cli.reviewed_head_tags(
        claims, claim, tenant_id=TENANT, run_id=run_id, claim_id=claim_id
    ) == (None, 2)


@pytest.mark.skipif(
    not (R24_REREVIEW_STATE / "state.sqlite3").is_file(),
    reason="local .local/r24-cli-rereview state not present",
)
def test_real_naver_rereviewed_head_is_read_at_revision_three(tmp_path, capsys):
    """Read-only supplement on the actual NAVER re-reviewed local state.

    The source directory is copied first and never written. This only confirms
    which head the fixed consumer reads (revision 3, M1's
    `named_means_or_concrete_state` present) and that every C item still blocks
    with `no_verified_trigger` under an explicitly synthetic FinancialContext.
    It asserts nothing about a real financial binding, claims no reconciliation,
    and produces no grade.
    """
    state = tmp_path / "r24-cli-rereview"
    shutil.copytree(R24_REREVIEW_STATE, state)
    database_path = state / "state.sqlite3"

    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.registry import Registry
    from proofops.application.uploads import UploadService

    registry = Registry.sqlite(database_path)
    uploads = UploadService(database_path, state / "objects", registry)
    run_store = LocalSQLiteRunStore(database_path)
    parser = OpenDataLoaderParser(state / "parser-prepared")
    claims = LocalClaimStore(run_store, uploads, parser)
    claim = claims.get(R24_TENANT, R24_RUN, R24_CLAIM)

    cli = _load_cli_module()
    tags, revision = cli.reviewed_head_tags(
        claims, claim, tenant_id=R24_TENANT, run_id=R24_RUN, claim_id=R24_CLAIM
    )
    assert revision == 3 and tags is not None and tags.tag_revision == 3
    assert tags.track == "management"
    present = {fact.name for fact in tags.facts if fact.state == "present"}
    assert present == {"named_means_or_concrete_state"}

    # The stored company identity is used exactly as recorded (a generic local
    # fixture label); no company/financial source is substituted to manufacture
    # a real-looking packet.
    snapshot = uploads.version_snapshot(R24_TENANT, claim.document_version_id)
    document = uploads.get_document(R24_TENANT, snapshot["document_id"])
    financial_context = _synthetic_financial_context(tmp_path, document["company_id"])
    reasons = {}
    for item in ("C1", "C2", "C3", "C4"):
        args = _cli_args(
            cli,
            tenant_id=R24_TENANT,
            run_id=R24_RUN,
            claim_id=R24_CLAIM,
            database_path=database_path,
            item=item,
            financial_context=financial_context,
        )
        assert cli._cmd_build_packet(args) == 1
        reasons[item] = json.loads(capsys.readouterr().out)["reason"]
    assert set(reasons.values()) == {"no_verified_trigger"}
