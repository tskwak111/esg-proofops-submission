"""Bounded opt-in native paragraph attestation in the parser worker and reader.

Default behavior (verify_paragraphs=False) preserves v1-v3 checkpoints byte for
byte. Opt-in publishes fenced v4 with an immutable recomputed receipt; loading
replays notes first, then recomputes native attestation (never trusting a
claimed verified status). Tables/footnotes/binding/grades remain unresolved.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from difflib import unified_diff
from io import StringIO
from uuid import uuid4

import pytest
from proofops.adapters.local.run_artifacts import load_run_evidence
from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
from proofops.application.ingest.graph_fusion import ParserProfile
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_parsing import FOREIGN, JAVA, MANIFEST, TENANT, pdf


def runner_setup_native(tmp_path, monkeypatch, verify):
    from proofops.adapters.aws.bedrock import BedrockInvoker
    from proofops.adapters.local.models import SyntheticTagger

    def forbidden_model(*args, **kwargs):
        raise AssertionError("Local parser executor must never call a product model")

    monkeypatch.setattr(BedrockInvoker, "invoke", forbidden_model)
    monkeypatch.setattr(SyntheticTagger, "tag", forbidden_model)
    from proofops.application.telemetry import Telemetry

    from tests.integration import test_run_lifecycle as lifecycle

    monkeypatch.setattr(lifecycle, "pdf", lambda count: pdf(table=True))
    service, body = lifecycle.setup(tmp_path)
    profile = ParserProfile(MANIFEST, java_executable=JAVA)
    service.parser_profile_hash = profile.config_hash()
    service.parser_profile = profile.config_snapshot()
    http, _ = lifecycle.client(service)
    response = http.post("/v1/runs", json=body)
    assert response.status_code == 202, response.text
    from proofops_worker.local_runner import LocalParserRunner

    stream = StringIO()
    now = [lifecycle.NOW]
    runner = LocalParserRunner(
        service.store,
        service.uploads,
        OpenDataLoaderParser(tmp_path / "prepared"),
        profile=profile,
        telemetry=Telemetry(service="worker", env="test", stream=stream, hash_key=b"x" * 32),
        clock=lambda: now[0],
        verify_paragraphs=verify,
    )
    return service, response.json()["run_id"], runner, now, stream


def test_verify_paragraphs_rejects_non_boolean(tmp_path, monkeypatch):
    service, run_id, runner, _, _ = runner_setup_native(tmp_path, monkeypatch, False)
    from proofops_worker.local_runner import LocalParserRunner

    with pytest.raises(ValueError, match="boolean"):
        LocalParserRunner(
            runner.store,
            runner.uploads,
            runner.parser,
            profile=runner.profile,
            telemetry=runner.telemetry,
            verify_paragraphs="yes",
        )


def test_default_preserves_legacy_v1_checkpoint(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, _ = runner_setup_native(tmp_path, monkeypatch, False)
    assert runner.verify_paragraphs is False
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    message = JobMessage(**service.store.jobs.get_run(TENANT, run_id)["parse_job"])
    envelope = json.loads(service.store.jobs.read_checkpoint(message))
    assert envelope["schema"] == "local_parser_checkpoint_v1"
    assert not any(key.startswith("native_paragraph_") for key in envelope)
    assert service.store.jobs.parser_native_policy(message) is None
    evidence = load_run_evidence(
        service.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    assert evidence["native_attestation"] is None
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id) == evidence["graph"]


def test_opt_in_publishes_v4_and_replays_immutable_receipt(tmp_path, monkeypatch):
    from proofops.adapters.local import source_verification
    from proofops.adapters.local.run_artifacts import load_run_evidence, native_paragraph_policy
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, _ = runner_setup_native(tmp_path, monkeypatch, True)
    attest = source_verification.attest_native_sources
    receipts = []

    def record_attestation(*args, **kwargs):
        receipt = attest(*args, **kwargs)
        receipts.append(receipt)
        return receipt

    monkeypatch.setattr(source_verification, "attest_native_sources", record_attestation)
    message = JobMessage(
        **service.store.jobs.pending_outbox(TENANT, run_id, now=now[0])[0]["message"]
    )
    outcome = runner.run_once(tenant_id=TENANT, run_id=run_id)
    if outcome != "committed" and len(receipts) >= 2:
        # Pytest truncates large assertion dicts; show only actual replay differences.
        print("NATIVE_RECEIPT_REPLAY_DIFF")
        print(
            "".join(
                unified_diff(
                    json.dumps(receipts[0], sort_keys=True, indent=2).splitlines(keepends=True),
                    json.dumps(receipts[1], sort_keys=True, indent=2).splitlines(keepends=True),
                    fromfile="initial",
                    tofile="replay",
                    n=1,
                )
            )
        )
    assert outcome == "committed", {
        "job_error": service.store.jobs.get_job(message)["error_code"],
        "native_attestations": receipts,
    }
    raw = service.store.jobs.read_checkpoint(message)
    envelope = json.loads(raw)
    assert envelope["schema"] == "local_parser_checkpoint_v4"
    assert service.store.jobs.parser_native_policy(message) == native_paragraph_policy()
    assert envelope["native_paragraph_policy_sha256"] == canonical_hash(native_paragraph_policy())
    receipt = envelope["native_paragraph_attestation"]
    assert receipt["schema"] == "native_paragraph_attestation_v2"
    assert receipt["geometry_mode"] == "glyph"
    assert receipt["scope"] == "paragraph_native_and_rendered_text_only"
    assert receipt["records"], "unresolved receipts persist; records must not vanish"
    # Immutable receipt: recompute-and-compare replay, then stable across reopen.
    first = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    assert envelope["graph_sha256"] == canonical_hash(asdict(first))
    verified_ids = {r["source_id"] for r in receipt["records"] if r["status"] == "verified"}
    for block in first.blocks:
        if block.source_id in verified_ids:
            assert block.quality == "verified"
        elif block.kind == "paragraph":
            assert block.quality != "verified" or block.source_id in verified_ids
    from proofops.adapters.local.run_store import LocalSQLiteRunStore

    runner.store = LocalSQLiteRunStore(service.store.path)
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id) == first
    assert runner.store.jobs.read_checkpoint(message) == raw
    evidence = load_run_evidence(
        runner.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    assert evidence["native_attestation"] == receipt
    assert evidence["base_graph"] != first or not verified_ids


def test_tampered_receipt_or_graph_rejected_on_load(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, _ = runner_setup_native(tmp_path, monkeypatch, True)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    read = service.store.jobs.read_checkpoint
    message = JobMessage(**service.store.jobs.get_run(TENANT, run_id)["parse_job"])
    envelope = json.loads(read(message))
    assert envelope["native_paragraph_attestation"]["records"]

    tampered = deepcopy(envelope)
    record = tampered["native_paragraph_attestation"]["records"][0]
    record["status"] = "verified" if record["status"] != "verified" else "unresolved"
    monkeypatch.setattr(
        service.store.jobs, "read_checkpoint", lambda message: json.dumps(tampered).encode()
    )
    with pytest.raises(ParseFailure, match="NATIVE_PARAGRAPH_REPLAY_INVALID"):
        runner.load_graph(tenant_id=TENANT, run_id=run_id)

    forged_hash = deepcopy(envelope)
    forged_hash["graph_sha256"] = "0" * 64
    monkeypatch.setattr(
        service.store.jobs, "read_checkpoint", lambda message: json.dumps(forged_hash).encode()
    )
    with pytest.raises(ParseFailure):
        runner.load_graph(tenant_id=TENANT, run_id=run_id)
    monkeypatch.setattr(service.store.jobs, "read_checkpoint", read)
    assert runner.load_graph(tenant_id=TENANT, run_id=run_id)


def test_mismatched_source_or_tenant_rejected(tmp_path, monkeypatch):
    from proofops.application.runs import RunRejected

    service, run_id, runner, now, _ = runner_setup_native(tmp_path, monkeypatch, True)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    monkeypatch.setattr(service.uploads, "read_original", lambda *args: b"tampered PDF")
    with pytest.raises(ParseFailure, match="SOURCE_INTEGRITY_MISMATCH|NATIVE_PARAGRAPH"):
        runner.load_graph(tenant_id=TENANT, run_id=run_id)
    with pytest.raises(RunRejected, match="RESOURCE_NOT_FOUND"):
        runner.load_graph(tenant_id=FOREIGN, run_id=run_id)


def test_retry_with_flipped_flag_cannot_republish(tmp_path, monkeypatch):
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, _ = runner_setup_native(tmp_path, monkeypatch, True)
    store = service.store.jobs
    commit = store.commit_job

    def crash(*args, **kwargs):
        raise RuntimeError("injected crash before checkpoint")

    monkeypatch.setattr(store, "commit_job", crash)
    with pytest.raises(RuntimeError, match="injected crash"):
        runner.run_once(tenant_id=TENANT, run_id=run_id)
    monkeypatch.setattr(store, "commit_job", commit)
    now[0] += 1000
    # A retry that flips the opt-in choice fails closed instead of republishing.
    runner.verify_paragraphs = False
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) in {"retry", "failed"}
    assert "parse_job" not in store.get_run(TENANT, run_id)
    # The original choice recovers after explicit operator retry and publishes once.
    run = store.get_run(TENANT, run_id)
    store.retry_run(
        TENANT,
        run_id,
        expected_revision=run["revision"],
        idempotency_key=str(uuid4()),
        reason="retry with original native choice",
        actor_sub="synthetic-fixture",
        now=now[0],
    )
    runner.verify_paragraphs = True
    now[0] += 1000
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    message = JobMessage(**store.get_run(TENANT, run_id)["parse_job"])
    published = store.read_checkpoint(message)
    assert json.loads(published)["schema"] == "local_parser_checkpoint_v4"
    runner.store = LocalSQLiteRunStore(service.store.path)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "pending_downstream"
    assert runner.store.jobs.read_checkpoint(message) == published


def test_superseded_native_worker_cannot_replace_checkpoint(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, now, _ = runner_setup_native(tmp_path, monkeypatch, True)
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=now[0])[0]["message"])
    original = runner.parser.parse
    committed = []

    def supersede(*args, **kwargs):
        graph = original(*args, **kwargs)
        now[0] += 1000
        assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
        committed.append(store.read_checkpoint(message))
        return graph

    monkeypatch.setattr(runner.parser, "parse", supersede)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "discarded"
    assert store.read_checkpoint(message) == committed[0]
    assert json.loads(committed[0])["schema"] == "local_parser_checkpoint_v4"


def test_unbound_v4_checkpoint_rejected_and_v1_native_keys_rejected(tmp_path):
    from proofops.application.ports.jobs import JobMessage

    from tests.integration.test_run_lifecycle import NOW, client, setup

    service, body = setup(tmp_path)
    http, _ = client(service)
    run_id = http.post("/v1/runs", json=body).json()["run_id"]
    store = service.store.jobs
    message = JobMessage(**store.pending_outbox(TENANT, run_id, now=NOW)[0]["message"])
    from proofops.adapters.local.run_artifacts import (
        checkpoint_native_attestation,
        native_paragraph_policy,
    )

    with pytest.raises(ParseFailure, match="NATIVE_PARAGRAPH_CHECKPOINT_INVALID"):
        checkpoint_native_attestation(
            {"schema": "local_parser_checkpoint_v1", "native_paragraph_attestation": {}}
        )
    lease = store.claim_job(message, owner="worker", now=NOW, lease_seconds=60)
    envelope = dict(
        schema="local_parser_checkpoint_v4",
        input_hash=message.input_hash,
        stage_status="completed",
        downstream_status="pending",
        runtime_note_review_artifacts=[],
        graph_sha256="0" * 64,
        native_paragraph_attestation={"schema": "native_paragraph_attestation_v1"},
        native_paragraph_policy_sha256=canonical_hash(native_paragraph_policy()),
        coverage=service.get(TENANT, run_id)["coverage"]
        | {"pages_processed": 3, "pages_unprocessed": 0},
    )
    with pytest.raises(ValueError, match="NATIVE_PARAGRAPH_INPUT_NOT_BOUND"):
        store.commit_job(
            lease,
            payload=json.dumps(envelope).encode(),
            now=NOW,
            next_job=replace(message, job_id=str(uuid4()), stage="extract"),
        )
    assert "parse_job" not in store.get_run(TENANT, run_id)


@pytest.mark.parametrize("initial", [False, True])
def test_published_native_choice_cannot_be_silently_changed(tmp_path, monkeypatch, initial):
    service, run_id, runner, _, _ = runner_setup_native(tmp_path, monkeypatch, initial)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    runner.verify_paragraphs = not initial
    with pytest.raises(ValueError, match="NATIVE_PARAGRAPH_ALREADY_PUBLISHED"):
        runner.run_once(tenant_id=TENANT, run_id=run_id)


def test_native_verification_composes_with_automatic_note_review(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobMessage

    from tests.integration.test_automatic_note_runner import setup

    service, run_id, runner, _, client = setup(tmp_path, monkeypatch)
    runner.verify_paragraphs = True
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    message = JobMessage(**service.store.jobs.get_run(TENANT, run_id)["parse_job"])
    envelope = json.loads(service.store.jobs.read_checkpoint(message))
    assert envelope["schema"] == "local_parser_checkpoint_v4"
    assert envelope["runtime_note_review_artifacts"]
    assert envelope["note_review_policy_sha256"]
    evidence = load_run_evidence(
        service.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    assert evidence["native_attestation"] and evidence["note_reviews"]
    assert envelope["graph_sha256"] == canonical_hash(asdict(evidence["graph"]))
    assert client.calls == 3


def test_native_policy_pins_verifier_sources(tmp_path, monkeypatch):
    from proofops.application.ports.jobs import JobMessage

    service, run_id, runner, _, _ = runner_setup_native(tmp_path, monkeypatch, True)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    message = JobMessage(**service.store.jobs.get_run(TENANT, run_id)["parse_job"])
    policy = service.store.jobs.parser_native_policy(message)
    receipt = json.loads(service.store.jobs.read_checkpoint(message))[
        "native_paragraph_attestation"
    ]
    for key in (
        "verifier_sha256",
        "normalization_sha256",
        "rendered_reader_sha256",
        "glyph_verifier_sha256",
    ):
        assert policy[key] == receipt[key]
    from proofops_worker import local_runner

    monkeypatch.setattr(
        local_runner, "native_paragraph_policy", lambda: policy | {"verifier_sha256": "0" * 64}
    )
    with pytest.raises(ValueError, match="NATIVE_PARAGRAPH_ALREADY_PUBLISHED"):
        runner.run_once(tenant_id=TENANT, run_id=run_id)


def test_typography_tolerance_opt_in_never_changes_checkpoint_or_old_defaults(
    tmp_path, monkeypatch
):
    """R02c: the new opt-in flag must be a pure additive side artifact.

    Same input, same native policy: with the flag off (default) and on, the
    published checkpoint (schema, native_paragraph_attestation,
    native_paragraph_policy_sha256, graph_sha256) must be byte-identical,
    because this fixture's synthetic text has no curly quotes to promote and
    -- independent of that -- the wrapper must never feed the checkpoint at
    all. Only `runner.last_typography_proof` differs.
    """
    from proofops.application.ports.jobs import JobMessage
    from proofops_worker.local_runner import LocalParserRunner

    baseline_dir = tmp_path / "baseline"
    opted_dir = tmp_path / "opted"
    baseline_dir.mkdir()
    opted_dir.mkdir()

    baseline_service, baseline_run_id, baseline_runner, *_ = runner_setup_native(
        baseline_dir, monkeypatch, True
    )
    assert baseline_runner.last_typography_proof is None
    assert baseline_runner.run_once(tenant_id=TENANT, run_id=baseline_run_id) == "committed"
    baseline_message = JobMessage(
        **baseline_service.store.jobs.get_run(TENANT, baseline_run_id)["parse_job"]
    )
    baseline_checkpoint = baseline_service.store.jobs.read_checkpoint(baseline_message)
    assert baseline_runner.last_typography_proof is None  # no eligible records in this fixture

    opted_service, opted_run_id, opted_runner, *_ = runner_setup_native(
        opted_dir, monkeypatch, True
    )
    opted_runner.native_typography_tolerance = True
    assert opted_runner.run_once(tenant_id=TENANT, run_id=opted_run_id) == "committed"
    opted_message = JobMessage(
        **opted_service.store.jobs.get_run(TENANT, opted_run_id)["parse_job"]
    )
    opted_checkpoint = opted_service.store.jobs.read_checkpoint(opted_message)
    baseline_envelope = json.loads(baseline_checkpoint)
    opted_envelope = json.loads(opted_checkpoint)
    # Explicit allowlist of fields the opt-in flag could plausibly influence.
    # Everything else (run/tenant/document/manifest ids, source/graph hashes
    # transitively keyed by them) legitimately differs across two
    # independent runs and is not a claim this test makes.
    for key in (
        "schema",
        "native_paragraph_policy_sha256",
        "stage_status",
        "downstream_status",
        "validation_profile",
        "vision_status",
    ):
        assert opted_envelope.get(key) == baseline_envelope.get(key), key
    baseline_receipt = baseline_envelope["native_paragraph_attestation"]
    opted_receipt = opted_envelope["native_paragraph_attestation"]
    for key in ("schema", "geometry_mode", "scope", "coordinate_system", "reader"):
        assert opted_receipt.get(key) == baseline_receipt.get(key), key

    # Same fixture, same records-by-status/reason shape either way.
    def status_reasons(receipt):
        return sorted((r["status"], r.get("reason")) for r in receipt["records"])

    assert status_reasons(opted_receipt) == status_reasons(baseline_receipt)
    # The actually-verified block set (what matters for grading) is the same
    # regardless of the opt-in flag on this fixture, since it has no
    # quote-typography-only mismatches to promote.
    baseline_graph = baseline_runner.load_graph(tenant_id=TENANT, run_id=baseline_run_id)
    opted_graph = opted_runner.load_graph(tenant_id=TENANT, run_id=opted_run_id)

    def verified_kinds(graph):
        return sorted(block.kind for block in graph.blocks if block.quality == "verified")

    assert verified_kinds(opted_graph) == verified_kinds(baseline_graph)
    # No eligible curly-quote records in this table-only fixture, so the
    # proof records zero promotions -- but it must still have run and pinned
    # its own wrapper policy independent of the base checkpoint.
    proof = opted_runner.last_typography_proof
    assert proof is not None
    assert proof["schema"] == "native_paragraph_typography_proof_v1"
    assert proof["promoted_source_ids"] == []

    # Producer -> checkpoint -> load_run_graph for the opt-in wrapper itself:
    # the checkpoint carries the separately hashed wrapper policy + proof, the
    # base receipt/policy/graph fields keep their v4 shape, and the reader
    # recomputes the identical proof instead of trusting the stored one.
    from proofops.adapters.local.native_paragraph_typography import (
        native_paragraph_typography_policy,
    )

    expected_wrapper = native_paragraph_typography_policy()
    assert opted_envelope["native_paragraph_typography_policy_sha256"] == canonical_hash(
        expected_wrapper
    )
    assert opted_envelope["schema"] == baseline_envelope["schema"] == "local_parser_checkpoint_v4"
    stored_proof = opted_envelope["native_paragraph_typography_proof"]
    assert stored_proof["artifact_sha256"] == proof["artifact_sha256"]
    assert stored_proof["promoted_source_ids"] == []
    opted_evidence = load_run_evidence(
        opted_service.store,
        opted_service.uploads,
        opted_runner.parser,
        tenant_id=TENANT,
        run_id=opted_run_id,
    )
    assert opted_evidence["typography_proof"]["artifact_sha256"] == proof["artifact_sha256"]
    assert "native_paragraph_typography_policy_sha256" not in baseline_envelope

    with pytest.raises(ValueError, match="native_typography_tolerance requires verify_paragraphs"):
        LocalParserRunner(
            opted_runner.store,
            opted_runner.uploads,
            opted_runner.parser,
            profile=opted_runner.profile,
            telemetry=opted_runner.telemetry,
            verify_paragraphs=False,
            native_typography_tolerance=True,
        )
    with pytest.raises(ValueError, match="native_typography_tolerance must be a boolean"):
        LocalParserRunner(
            opted_runner.store,
            opted_runner.uploads,
            opted_runner.parser,
            profile=opted_runner.profile,
            telemetry=opted_runner.telemetry,
            verify_paragraphs=True,
            native_typography_tolerance="yes",
        )
