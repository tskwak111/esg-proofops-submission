"""Process-local cache coverage for offline v5 raster replay."""

from copy import deepcopy
from dataclasses import replace

import pytest

from tests.integration.test_raster_checkpoint_replay import _stored
from tests.integration.test_raster_parser_worker import setup_worker
from tests.integration.test_raster_request_authorization import prepare, setup_request


def test_v5_reader_reuses_completed_raster_replay(tmp_path, monkeypatch):
    import proofops.adapters.local.native_replay_cache as cache
    import proofops.adapters.local.raster_checkpoint as checkpoint

    cache._raster_replays.clear()
    service, runner, lease, _probe, _calls = setup_worker(tmp_path, monkeypatch, heading=True)
    identity = {"tenant_id": lease.message.tenant_id, "run_id": lease.message.run_id}
    assert runner.run_once(**identity) == "committed"
    real = checkpoint.replay_raster_records
    calls = 0

    def counted(*args):
        nonlocal calls
        calls += 1
        return real(*args)

    monkeypatch.setattr(checkpoint, "replay_raster_records", counted)
    runner.load_graph(**identity)
    runner.load_graph(**identity)
    assert calls == 1


def _replay_inputs(tmp_path, monkeypatch):
    service, runner, lease, graph, native, snapshot = setup_request(tmp_path, monkeypatch)
    _, request = prepare(runner, lease, graph, native)
    registrations, receipts = _stored(service, lease, request, graph.blocks[0].raw_text)
    return (
        snapshot,
        lease.message,
        native,
        graph,
        service.uploads.read_original(lease.message.tenant_id, graph.document_version_id),
        registrations,
        receipts,
    )


def test_v5_cache_keys_every_replay_dependency_and_returns_fresh_containers(tmp_path, monkeypatch):
    import proofops.adapters.local.native_replay_cache as cache
    import proofops.adapters.local.raster_checkpoint as checkpoint
    import proofops.adapters.local.raster_visibility as visibility

    cache._raster_replays.clear()
    args = _replay_inputs(tmp_path, monkeypatch)
    calls = 0

    def replay(snapshot, message, native, graph, source, registrations, receipts):
        nonlocal calls
        calls += 1
        return (
            graph,
            {"sources": [native["records"][0]["source_id"]]},
            [{"request_ids": [row["request"]["request_id"] for row in registrations]}],
        )

    monkeypatch.setattr(checkpoint, "replay_raster_records", replay)
    first = cache.replay_raster_cached(*args)
    first[1]["sources"].append("mutated")
    first[2][0]["request_ids"].append("mutated")
    second = cache.replay_raster_cached(*args)
    assert calls == 1
    assert second[1]["sources"] == [args[2]["records"][0]["source_id"]]
    assert second[2][0]["request_ids"] == [args[5][0]["request"]["request_id"]]

    changed = list(args)
    changed[0] = deepcopy(args[0]) | {"input_hash": "0" * 64}
    cache.replay_raster_cached(*changed)
    changed = list(args)
    changed[1] = replace(args[1], shard="cache-key-change")
    cache.replay_raster_cached(*changed)
    changed = list(args)
    changed[2] = deepcopy(args[2])
    changed[2]["records"][0]["reason"] = "changed"
    cache.replay_raster_cached(*changed)
    changed = list(args)
    changed[3] = replace(args[3], source_sha256="0" * 64)
    cache.replay_raster_cached(*changed)
    changed = list(args)
    changed[4] = args[4] + b"changed"
    cache.replay_raster_cached(*changed)
    changed = list(args)
    changed[5] = (deepcopy(args[5][0]) | {"owner": "changed"},)
    cache.replay_raster_cached(*changed)
    changed = list(args)
    changed[6] = deepcopy(args[6]) | {"changed": {}}
    cache.replay_raster_cached(*changed)
    original_policy = visibility.raster_ocr_policy
    monkeypatch.setattr(
        visibility,
        "raster_ocr_policy",
        lambda **kwargs: original_policy(**kwargs) | {"cache_key": "changed"},
    )
    cache.replay_raster_cached(*args)
    monkeypatch.setattr(cache.sys, "platform", "cache-key-change")
    cache.replay_raster_cached(*args)
    monkeypatch.setattr(cache, "version", lambda name: "changed-reader-version")
    cache.replay_raster_cached(*args)
    assert calls == 11


def test_v5_cache_does_not_store_failures_and_evicts_lru_entries(tmp_path, monkeypatch):
    import proofops.adapters.local.native_replay_cache as cache
    import proofops.adapters.local.raster_checkpoint as checkpoint

    cache._raster_replays.clear()
    args = _replay_inputs(tmp_path, monkeypatch)
    calls = 0

    def replay(snapshot, message, native, graph, source, registrations, receipts):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("replay failed")
        return graph, {}, []

    monkeypatch.setattr(checkpoint, "replay_raster_records", replay)
    with pytest.raises(ValueError, match="replay failed"):
        cache.replay_raster_cached(*args)
    cache.replay_raster_cached(*args)
    cache.replay_raster_cached(*args)
    assert calls == 2

    cache._raster_replays.clear()
    for index in range(65):
        changed = list(args)
        changed[0] = deepcopy(args[0]) | {"input_hash": f"{index:064x}"}
        cache.replay_raster_cached(*changed)
    cache.replay_raster_cached(*args)
    assert calls == 68
