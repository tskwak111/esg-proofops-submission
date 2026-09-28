"""Repeated reads reuse successful verification, never another input's result."""

from dataclasses import dataclass, replace
from types import SimpleNamespace

import pytest


def test_claim_replay_is_bound_to_inputs_and_cannot_be_poisoned(monkeypatch):
    from proofops.adapters.local.native_replay_cache import replay_claims_cached

    @dataclass(frozen=True)
    class Input:
        value: str

    calls = []

    def replay(receipt, graph, source, discovery, *, tenant_id):
        calls.append(True)
        if receipt.get("bad"):
            raise ValueError("invalid receipt")
        return {"values": ["verified"]}, {"source": source.decode()}

    reader = SimpleNamespace(__name__="test-claims", replay_claim_spans=replay)
    args = dict(
        reader=reader,
        policy={"version": "test-1"},
        receipt={"proof": "a"},
        graph=Input("graph"),
        source=b"pdf",
        discovery=Input("claim"),
        tenant_id="tenant",
    )
    result = replay_claims_cached(**args)
    result[0]["values"].clear()
    assert replay_claims_cached(**args)[0]["values"] == ["verified"]
    assert len(calls) == 1
    for changed in (
        {"tenant_id": "other"},
        {"source": b"changed"},
        {"graph": replace(args["graph"], value="changed")},
        {"discovery": Input("changed")},
        {"receipt": {"proof": "b"}},
        {"policy": {"version": "test-2"}},
    ):
        replay_claims_cached(**(args | changed))
    assert len(calls) == 7
    monkeypatch.setenv("DEVELOPER_DIR", "/changed-toolchain")
    replay_claims_cached(**args)
    assert len(calls) == 8
    for _ in range(2):
        with pytest.raises(ValueError, match="invalid receipt"):
            replay_claims_cached(**(args | {"receipt": {"bad": True}}))
    assert len(calls) == 10
