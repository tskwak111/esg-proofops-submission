"""End-to-end: real committed run graph -> producer route -> published assurance.

Reuses the same committed-run fixture as tests/integration/test_assurance_publication.py
(extraction_setup -> LocalParserRunner.run_once) so the graph here is a real
fused/committed graph from the actual local parser, not a synthetic
in-memory graph built by hand. Drives the whole `run_assurance_producer`
route with a stubbed transport (no network, no paid call) and asserts the
result is readable back through the existing `LocalAssuranceStore.load` +
`match_assurance` path the API already serves.
"""

from __future__ import annotations

import json

import pytest
from proofops.adapters.local.assurance_store import LocalAssuranceStore
from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.application.assurance import ClaimContext, match_assurance
from proofops.domain.provenance import canonical_hash

from evaluation.assurance_run import run_assurance_producer
from tests.integration.test_local_extract_runner import extraction_setup
from tests.integration.test_local_parser_runner import TENANT
from tests.integration.test_run_lifecycle import client  # noqa: F401  (fixture wiring)


class FakeProbe:
    def __init__(self, content=None, error=None):
        self.content, self.error, self.calls = content, error, []

    @property
    def model(self):
        return "solar-pro3"

    def complete(self, system, user_json, *, request_id, max_tokens=1024, json_mode=False):
        self.calls.append(request_id)
        if self.error is not None:
            raise self.error
        return {
            "model": "solar-pro3",
            "provider_request_id": "fake-provider-id",
            "provider_model": "solar-pro3-260323",
            "input_tokens": 80,
            "output_tokens": 40,
            "response_sha256": canonical_hash(self.content),
            "content": self.content,
        }


def _committed_graph(tmp_path, monkeypatch):
    service, run_id, runner, _now, _ = extraction_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    graph = load_run_graph(
        service.store, service.uploads, runner.parser, tenant_id=TENANT, run_id=run_id
    )
    return service, run_id, runner, graph


def test_producer_route_publishes_and_is_readable_via_existing_store(tmp_path, monkeypatch):
    service, run_id, runner, graph = _committed_graph(tmp_path, monkeypatch)
    resolved_blocks = [b for b in graph.blocks if b.winner is not None and b.raw_text.strip()]
    assert len(resolved_blocks) >= 1, "fixture must produce at least one resolved block"
    source_ids = tuple(block.source_id for block in resolved_blocks)
    text = resolved_blocks[0].raw_text
    # Use a real substring of the actual committed graph's own raw text as
    # the "quote" so this is a genuine same-source citation, not a fixture.
    stripped = text.strip()
    if not stripped:
        pytest.skip("fixture block has no non-whitespace text to quote")
    quote = stripped[: max(1, min(10, len(stripped)))]
    content = json.dumps({"fields": {"provider": [{"source_index": 0, "quote": quote}]}})
    probe = FakeProbe(content)

    result = run_assurance_producer(
        store=service.store,
        uploads=service.uploads,
        parser=runner.parser,
        tenant_id=TENANT,
        run_id=run_id,
        source_ids=source_ids,
        probe=probe,
        receipts=tmp_path / "assurance-receipts",
    )
    assert result["status"] == "published"
    assert result["semantic_hash"]
    assert probe.calls  # one real (stubbed) model round-trip happened

    store = LocalAssuranceStore(service.store, service.uploads, runner.parser)
    replayed = store.load(TENANT, run_id)
    assert replayed is not None
    assert replayed.semantic_hash == result["semantic_hash"]
    assert {ref.source_id for ref in replayed.source_refs} == set(source_ids)
    # The statement's provider value is only populated if the cited block's
    # source was actually "verified" quality; either way the citation is
    # real and re-verified against the graph, which is what this route
    # guarantees (never that unverified sources become covered).
    if "provider" not in replayed.unresolved_fields:
        assert replayed.provider == quote

    match = match_assurance(
        replayed, ClaimContext(TENANT, graph.document_version_id, run_id, None, None, (), ())
    )
    assert match.statement_id == result["statement_id"]


def test_producer_route_raises_on_transport_failure_without_publishing(tmp_path, monkeypatch):
    service, run_id, runner, graph = _committed_graph(tmp_path, monkeypatch)
    resolved_blocks = [b for b in graph.blocks if b.winner is not None]
    if not resolved_blocks:
        pytest.skip("fixture produced no resolved block")
    source_ids = (resolved_blocks[0].source_id,)
    probe = FakeProbe(error=ValueError("UPSTREAM_UNAVAILABLE"))

    with pytest.raises(ValueError, match="UPSTREAM_UNAVAILABLE"):
        run_assurance_producer(
            store=service.store,
            uploads=service.uploads,
            parser=runner.parser,
            tenant_id=TENANT,
            run_id=run_id,
            source_ids=source_ids,
            probe=probe,
            receipts=tmp_path / "assurance-receipts",
        )

    store = LocalAssuranceStore(service.store, service.uploads, runner.parser)
    assert store.load(TENANT, run_id) is None
