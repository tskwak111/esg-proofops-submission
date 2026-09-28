"""Actual PDF/parser and worker chain; controlled OCR/HTTP, no paid calls or grades."""

from dataclasses import asdict
from uuid import uuid4

import pytest
from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.registry import Registry
from proofops_worker.extract_runner import LocalExtractRunner
from proofops_worker.live_tagging import LiveTaggingRuntime
from proofops_worker.tag_runner import LocalTagRunner

from tests.integration.test_live_tagging_pipeline import _fake_post_factory
from tests.integration.test_raster_parser_worker import setup_worker
from tests.integration.test_upstage_parse import fixed_pricing_date  # noqa: F401


def test_recovered_raster_paragraph_reaches_live_candidate_review(tmp_path, monkeypatch):
    from proofops.application.rulepacks import RulePackRecord
    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    from tests.acceptance.test_rules import pack as full_pack
    from tests.integration import test_raster_request_authorization as authorization

    configured = authorization.configured

    def complete_registry(path):
        service, body = configured(path)
        full = full_pack()
        values = {key: value for key, value in asdict(full).items() if key != "content"}
        values.update(
            rule_pack_id=str(uuid4()),
            tenant_id=authorization.AUTH.tenant_id,
            status="validated",
            approved_by=None,
            approved_at=None,
        )
        pack = RulePackRecord(**values)
        service.store.rulepacks.add_pack(pack, {p: full.file_content(p) for p in full.files})
        body["rule_pack_id"] = pack.rule_pack_id
        registry = Registry.sqlite(service.store.path)
        for option in service.registry._options.values():
            values = asdict(option)
            values["option_id"] = values.pop("id")
            registry.with_option(**values)
        service.registry = service.uploads.registry = registry
        return service, body

    monkeypatch.setattr(authorization, "configured", complete_registry)
    service, parser_runner, lease, raster_probe, raster_calls = setup_worker(
        tmp_path, monkeypatch, heading=True
    )
    identity = dict(tenant_id=lease.message.tenant_id, run_id=lease.message.run_id)
    tenant, run_id = identity["tenant_id"], identity["run_id"]
    assert parser_runner.run_once(**identity) == "committed"
    assert len(raster_calls) == 1
    parsed = service.store.jobs.read_checkpoint(lease.message)
    monkeypatch.setattr(raster_probe, "parse", lambda *a, **k: pytest.fail("unexpected OCR"))

    calls = []
    response = _fake_post_factory(calls)
    extract_probe = UpstageProbe("test-only", raster_probe.ledger)
    tag_probe = UpstageProbe("test-only", raster_probe.ledger, model=MODEL_PRO4)
    monkeypatch.setattr(extract_probe, "_post", response)
    monkeypatch.setattr(tag_probe, "_post", response)
    extract = LocalExtractRunner(
        service.store,
        service.uploads,
        parser_runner.parser,
        extractor=UpstageClaimExtractor(
            extract_probe, tmp_path / "extract-receipts", max_tokens=1024
        ),
        telemetry=parser_runner.telemetry,
        clock=parser_runner.clock,
    )
    assert extract.run_once(**identity) == "committed"
    claims = extract.claims.list(tenant, run_id)
    assert len(claims) == 1 and claims[0].source_quality == "verified"
    assert "1234 tCO2e" in claims[0].quote

    def live(owner, snapshot, graph, active_lease, usage):
        return LiveTaggingRuntime(
            owner,
            snapshot,
            graph,
            active_lease,
            usage,
            probe=tag_probe,
            ledger=raster_probe.ledger,
            receipts=tmp_path / "tag-receipts",
        )

    tag = LocalTagRunner(
        service.store,
        service.uploads,
        parser_runner.parser,
        telemetry=parser_runner.telemetry,
        clock=parser_runner.clock,
        live_factory=live,
    )
    assert tag.run_once(**identity) == "needs_review"
    assert len(calls) == 7  # one extractor, three preliminary, three element responses
    inputs = tag.tags.load_inputs(tenant, run_id, claims[0].claim_id)
    assert len(inputs.tag_runs) == 3
    assert inputs.decision is None
    assert all(e.state == "unknown" for e in inputs.consensus.candidate_elements)
    assert service.store.jobs.read_checkpoint(lease.message) == parsed
    before = service.cost(tenant, run_id)
    monkeypatch.setattr(extract_probe, "_post", lambda *a: pytest.fail("replay extraction call"))
    monkeypatch.setattr(tag_probe, "_post", lambda *a: pytest.fail("replay tagging call"))
    tag = LocalTagRunner(
        type(service.store)(service.store.path),
        service.uploads,
        parser_runner.parser,
        telemetry=parser_runner.telemetry,
        clock=parser_runner.clock,
        live_factory=live,
    )
    assert tag.tags.load_inputs(tenant, run_id, claims[0].claim_id) == inputs
    assert tag.run_once(**identity) == "needs_review"
    assert len(calls) == 7 and len(raster_calls) == 1
    assert service.cost(tenant, run_id) == before
    assert raster_probe.summary()["calls"] == 8
    assert raster_probe.summary()["unsettled_calls"] == 0
