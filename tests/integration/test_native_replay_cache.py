"""Successful replay reuse must remain bound to every immutable input."""

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.adapters.local import source_verification
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import FOREIGN, TENANT, candidate, pdf


def test_replay_cache_preserves_validation_and_invalidates_inputs(monkeypatch):
    from proofops.adapters.local import native_replay_cache as cache

    cache._replays.clear()
    monkeypatch.setattr(
        source_verification,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page 1 emissions 1234 tCO2e"),
    )
    source = pdf()
    batch = replace(
        candidate(
            "cached", [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())]
        ),
        source_sha256=sha256(source).hexdigest(),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    receipt = source_verification.attest_native_sources(graph, source, tenant_id=TENANT)
    calls = []
    original = cache.replay_native_sources

    def replay(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(cache, "replay_native_sources", replay)
    first = cache.replay_cached(receipt, graph, source, tenant_id=TENANT)
    assert first.blocks[0].quality == "verified"
    assert cache.replay_cached(receipt, graph, source, tenant_id=TENANT) == first
    assert len(calls) == 1
    assert graph.blocks[0].quality == "unverified"

    forged = deepcopy(receipt)
    forged["records"][0]["status"] = "unresolved"
    for r, g, content, tenant in [
        (forged, graph, source, TENANT),
        (receipt, graph, source + b"changed", TENANT),
        (receipt, graph, source, FOREIGN),
        (receipt, replace(graph, blocks=()), source, TENANT),
    ]:
        with pytest.raises(ValueError):
            cache.replay_cached(r, g, content, tenant_id=tenant)
    assert len(calls) == 5
    policy = cache.native_paragraph_policy()
    monkeypatch.setattr(cache, "native_paragraph_policy", lambda: {**policy, "new_runtime": True})
    assert cache.replay_cached(receipt, graph, source, tenant_id=TENANT) == first
    assert len(calls) == 6
    # Returned data cannot poison cached proof; only immutable source IDs are retained.
    assert all(isinstance(value, frozenset) for value in cache._replays.values())

    monkeypatch.setattr(cache, "_MAX_REPLAYS", 2)
    cache._replays.clear()
    for revision in range(3):
        monkeypatch.setattr(
            cache, "native_paragraph_policy", lambda r=revision: {**policy, "revision": r}
        )
        cache.replay_cached(receipt, graph, source, tenant_id=TENANT)
    assert len(cache._replays) == 2
    before = len(calls)
    monkeypatch.setattr(cache, "native_paragraph_policy", lambda: {**policy, "revision": 0})
    assert cache.replay_cached(receipt, graph, source, tenant_id=TENANT) == first
    assert len(calls) == before + 1  # Evicted entries must undergo actual replay again.
    cache._replays.clear()
