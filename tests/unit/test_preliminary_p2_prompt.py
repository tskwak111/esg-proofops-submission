"""P2 is an opt-in prompt only, with frozen historical packet hashes."""

import json
from types import SimpleNamespace

import pytest
from proofops.application.tagging.preliminary import P2_SYSTEM_PROMPT, preliminary_table_request
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_preliminary import P2_MODEL_PROFILE, P2_TRANSPORT_VERSION
from proofops_worker.live_tagging import LiveTaggingRuntime

from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary_table_sources import table_corpus
from tests.integration.test_preliminary_table_role_prompt import _configured


def test_p2_packet_keeps_table_sources_and_pins_new_prompt():
    graph, claim, _ = table_corpus()
    old = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True)
    new = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True, p2=True)
    assert (
        old["prompt_sha256"] == "19eeb93ef1d1067832f2dd96d5bdd3047d73cb6dbc200cd9b5b5296bbf848263"
    )
    assert new["prompt_sha256"] == canonical_hash(P2_SYSTEM_PROMPT)
    assert (
        new["prompt_sha256"] == "e17a5426d39199a4607b7bdc9be424f9c523094e487bbae5fb1533ea993ed6f4"
    )
    assert {key for key in new if new[key] != old[key]} == {"prompt_sha256"}
    assert P2_MODEL_PROFILE.endswith("-v2-p2")


def test_p2_pilot_setting_is_explicit():
    from evaluation.local_upstage_pilot import live_tagging_settings

    base = live_tagging_settings(
        12, preliminary_context=True, preliminary_table_context=True, preliminary_table_role=True
    )
    p2 = live_tagging_settings(
        12,
        preliminary_context=True,
        preliminary_table_context=True,
        preliminary_table_role=True,
        preliminary_p2=True,
    )
    assert base["preliminary_settings"]["model_profile"].endswith("table-role-v1")
    assert p2["preliminary_settings"]["model_profile"] == P2_MODEL_PROFILE
    assert p2["preliminary_settings"]["system_prompt"] == P2_SYSTEM_PROMPT


def test_p2_transport_rejects_baseline_packet(tmp_path, monkeypatch):
    adapter, request, _, claim, graph = _configured(
        tmp_path,
        monkeypatch,
        profile=P2_MODEL_PROFILE,
        prompt=P2_SYSTEM_PROMPT,
        role_resolution=True,
    )
    assert adapter.TRANSPORT_VERSION == P2_TRANSPORT_VERSION
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PROMPT_INVALID"):
        adapter._wire_request(request)
    packet = preliminary_table_request(
        claim, graph, tenant_id=TENANT, role_resolution=True, p2=True
    )
    request.update(user_json=json.dumps(packet), packet_sha256=canonical_hash(packet))
    assert json.loads(adapter._wire_request(request)[1])["prompt_sha256"] == canonical_hash(
        P2_SYSTEM_PROMPT
    )


def test_worker_selects_p2_packet_for_p2_profile(monkeypatch):
    import proofops_worker.live_tagging as live

    seen = {}

    def packet(_claim, _graph, **kwargs):
        seen.update(kwargs)
        return {"prompt_sha256": canonical_hash(P2_SYSTEM_PROMPT)}

    monkeypatch.setattr(live, "preliminary_table_request", packet)
    runtime = LiveTaggingRuntime.__new__(LiveTaggingRuntime)
    runtime.preliminary_settings = SimpleNamespace(model_profile=P2_MODEL_PROFILE)
    runtime.preliminary_transport = SimpleNamespace(bound_context=lambda value: value)
    runtime.preliminary_records = []
    runtime.auth = SimpleNamespace(tenant_id=TENANT)
    runtime._source_replicas = lambda *_args: None
    assert runtime.preliminary(object(), object()) is None
    assert seen["role_resolution"] is True
    assert seen["p2"] is True
