"""R15: the opt-in render-resolution wrapper inside the REAL local run path.

Proves the wrapper is a pinned run policy, not a standalone experiment:

- a new run pins ``claim_span_render_resolution_policy_v1`` in its immutable
  snapshot, the real ``LocalExtractRunner`` publishes that wrapper's OWN
  receipt schema into the actual extract checkpoint, and the normal
  ``LocalClaimStore`` load downstream consumers already use replays it and
  admits only the per-record promoted spans -- never a whole-block flip;
- an altered receipt, a receipt naming a different ref set, and an altered or
  frozen policy are all refused instead of partially applied;
- legacy mode is untouched: the base policy still resolves to the base
  verifier, the base attestation for the same inputs is byte-identical to the
  wrapper receipt's own ``base_records``, and the prior parse revision stays
  byte-identical.

No model, network, or AWS call: the extractor probe is the existing fake and
both readers' OCR steps are the existing monkeypatched fixtures.
"""

from dataclasses import replace

import pytest
from proofops.adapters.local import claim_source_policies as policies
from proofops.adapters.local import claim_source_verification as base
from proofops.adapters.local import claim_source_verification_v1 as frozen_v1
from proofops.adapters.local import claim_span_render_resolution as wrapper
from proofops.adapters.local import selected_cell_table_verification as cell_reader
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import TENANT, candidate
from tests.integration.test_batch_attestation_cache import discovery_for
from tests.integration.test_claim_source_verification import inputs

QUOTE = "emissions 1234 tCO2e"
NATIVE = "Page 1 emissions 1234 tCO2e"
# What the base verifier's fixed-scale whole-block crop reads: the real KB
# "9.9억 원" -> "9.9억원" inter-word space loss.
BASE_RENDERED = "Page 1 emissions1234 tCO2e"


def render_gap(monkeypatch):
    """Base reader loses the space; the wrapper's deterministic-scale crop does not."""
    monkeypatch.setattr(
        base, "_rendered_text", lambda *a, **k: dict(status="read", text=BASE_RENDERED)
    )
    monkeypatch.setattr(
        cell_reader,
        "_rendered_cell",
        lambda page, box: dict(status="read", text=NATIVE, scale=11),
    )


def test_real_worker_publishes_the_wrapper_receipt_and_claim_store_admits_its_spans(
    tmp_path, monkeypatch
):
    """The actual worker + actual claim store, pinned to the wrapper policy."""
    import json

    from proofops.adapters.local.claim_store import LocalClaimStore
    from proofops.adapters.local.run_store import LocalSQLiteRunStore
    from proofops.application.ports.jobs import JobMessage

    from tests.integration import test_run_lifecycle as lifecycle
    from tests.integration.test_real_extract_runner import FakeProbe, real_setup

    original = lifecycle.setup

    def configured(path):
        service, body = original(path)
        service.claim_source_policy = wrapper.claim_source_policy()
        return service, body

    monkeypatch.setattr(lifecycle, "setup", configured)
    render_gap(monkeypatch)
    probe = FakeProbe(json.dumps({"claims": [QUOTE]}))
    service, run_id, runner, now, probe = real_setup(tmp_path, monkeypatch, limit=1, probe=probe)

    from proofops.adapters.local.run_artifacts import load_run_graph

    real = load_run_graph(
        service.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    batch = candidate("span-r15", [("P", "paragraph", NATIVE, (70, 710, 300, 740), ())])
    batch = replace(
        batch,
        document_version_id=real.document_version_id,
        parse_manifest_id=real.parse_manifest_id,
        source_sha256=real.source_sha256,
        blocks=tuple(
            replace(
                block,
                source=replace(
                    block.source,
                    document_version_id=real.document_version_id,
                    parse_manifest_id=real.parse_manifest_id,
                ),
            )
            for block in batch.blocks
        ),
    )
    graph_input = fuse_candidates((batch,), tenant_id=TENANT)
    for module in ("proofops_worker.extract_runner", "proofops.adapters.local.claim_store"):
        monkeypatch.setattr(module + ".load_run_graph", lambda *a, **k: graph_input)

    run = service.store.jobs.get_run(TENANT, run_id)
    parse_before = service.store.jobs.read_checkpoint(JobMessage(**run["parse_job"]))
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"

    envelope, discovery, graph = runner.claims.load_evidence(TENANT, run_id)
    receipt = envelope["claim_source_attestation"]
    # The published checkpoint carries the wrapper's OWN distinct receipt, and
    # the base verifier's receipt is preserved inside it, not relabelled.
    assert envelope["schema"] == "local_extract_checkpoint_v2"
    assert receipt["schema"] == wrapper.SCHEMA != "claim_source_attestation_v1"
    assert receipt["policy"] == wrapper.claim_source_policy()
    assert receipt["graph_sha256"] == envelope["graph_sha256"]
    assert receipt["render_retries"]
    # Only the wrapper's retry admits this span; the base reader alone refuses it.
    assert all(record["status"] == "unresolved" for record in receipt["base_records"])
    assert any(record["status"] == "verified" for record in receipt["records"])
    assert discovery.claims and any(c.source_quality == "verified" for c in discovery.claims)
    # Per-record scope only: no block quality is promoted wholesale.
    assert all(block.quality != "verified" for block in graph.blocks)
    # The normal downstream claim-store load reproduces exactly the same claims.
    reopened = LocalClaimStore(
        LocalSQLiteRunStore(service.store.path), service.uploads, runner.parser
    )
    assert reopened.load(TENANT, run_id) == discovery
    # Prior immutable revision unchanged.
    assert service.store.jobs.read_checkpoint(JobMessage(**run["parse_job"])) == parse_before
    assert len(probe.calls) == 1


def test_replay_refuses_an_altered_receipt_or_a_different_ref_set(monkeypatch):
    render_gap(monkeypatch)
    source, graph, ref = inputs()
    discovery = discovery_for(graph, (ref,))
    receipt = wrapper.attest_claim_spans(
        graph, source, base.discovery_refs(discovery), tenant_id=TENANT
    )
    assert [r["status"] for r in receipt["records"]] == ["verified"]
    replayed, scoped = wrapper.replay_claim_spans(
        receipt, graph, source, discovery, tenant_id=TENANT
    )
    assert [c.source_quality for c in replayed.claims] == ["verified"]
    assert all(block.quality != "verified" for block in scoped.blocks)

    whole = graph.blocks[0].source_ref()
    for altered in (
        dict(receipt, records=[dict(receipt["records"][0], reason="forced")]),
        dict(receipt, base_records=[dict(receipt["base_records"][0], status="verified")]),
        dict(receipt, render_retries={}),
        dict(receipt, schema="claim_source_attestation_v1"),
        {k: v for k, v in receipt.items() if k != "base_records"},
    ):
        with pytest.raises(ValueError):
            wrapper.replay_claim_spans(altered, graph, source, discovery, tenant_id=TENANT)
    # A receipt attesting a different ref set is refused, never partly applied.
    with pytest.raises(ValueError):
        wrapper.replay_claim_spans(
            receipt, graph, source, discovery_for(graph, (whole,)), tenant_id=TENANT
        )


def test_policy_dispatch_admits_the_wrapper_and_keeps_legacy_mode(monkeypatch):
    live = wrapper.claim_source_policy()
    assert live["schema"] == policies.RENDER_RESOLUTION_POLICY_SCHEMA
    assert policies.claim_source_reader(live) is wrapper
    assert policies.publication_reader(live) is wrapper
    # Legacy mode preserved: the base policy still resolves to the base verifier,
    # and a frozen predecessor stays replay-only.
    assert policies.claim_source_reader(base.claim_source_policy()) is base
    assert policies.publication_reader(base.claim_source_policy()) is base
    assert policies.claim_source_reader(frozen_v1.claim_source_policy()) is frozen_v1
    for refused in (
        dict(live, wrapper_sha256="0" * 64),
        dict(live, base=base.claim_source_policy() | {"revision": "other"}),
        frozen_v1.claim_source_policy(),
    ):
        with pytest.raises(ValueError):
            policies.publication_reader(refused)
    with pytest.raises(ValueError):
        policies.claim_source_reader(dict(live, wrapper_sha256="0" * 64))


def test_run_service_accepts_the_wrapper_policy_in_its_immutable_snapshot():
    """The pin has to survive run creation, not only the adapter dispatch."""
    from types import SimpleNamespace

    from proofops.application.runs import RunService

    common = dict(
        store=SimpleNamespace(),
        uploads=SimpleNamespace(local_synthetic=True),
        registry=SimpleNamespace(),
        extraction_mode="upstage_probe",
    )
    service = RunService(**common, claim_source_policy=wrapper.claim_source_policy())
    assert service.claim_source_policy["schema"] == policies.RENDER_RESOLUTION_POLICY_SCHEMA
    assert service.claim_source_policy == wrapper.claim_source_policy()
    # An unknown schema, and the wrapper outside upstage_probe, stay refused.
    with pytest.raises(ValueError):
        RunService(**common, claim_source_policy={"schema": "invented_policy_v1"})
    with pytest.raises(ValueError):
        RunService(
            **(common | {"extraction_mode": None}),
            claim_source_policy=wrapper.claim_source_policy(),
        )


def test_the_wrapper_receipt_preserves_the_untouched_base_attestation(monkeypatch):
    render_gap(monkeypatch)
    source, graph, ref = inputs()
    baseline = base.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    receipt = wrapper.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT)
    assert receipt["base_records"] == baseline["records"]
    assert receipt["base_attestation_sha256"] == baseline["artifact_sha256"]
    assert receipt["graph_sha256"] == baseline["graph_sha256"]
    assert base.attest_claim_spans(graph, source, (ref,), tenant_id=TENANT) == baseline


def test_the_pilot_pins_the_wrapper_policy_only_on_an_explicit_new_run_opt_in():
    from argparse import Namespace

    import evaluation.local_upstage_pilot as pilot
    from tests.unit.test_extraction_source_id_wiring import _args

    assert (
        pilot.claim_source_policy_for(Namespace(claim_span_render_resolution=True))
        == wrapper.claim_source_policy()
    )
    assert (
        pilot.claim_source_policy_for(Namespace(claim_span_render_resolution=False))
        == base.claim_source_policy()
    )

    # The flag is restored from a stored run and defaults off for a legacy one,
    # so an operator cannot add it to an existing run on --resume.
    restored = _args(verify_claim_spans=True)
    pilot.apply_resume_metadata(
        restored,
        {
            "source_path": "/tmp/elsewhere.pdf",
            "verify_claim_spans": True,
            "claim_span_render_resolution": True,
        },
    )
    assert restored.claim_span_render_resolution is True
    legacy = _args(verify_claim_spans=True)
    pilot.apply_resume_metadata(legacy, {"source_path": "/tmp/elsewhere.pdf"})
    assert legacy.claim_span_render_resolution is False
