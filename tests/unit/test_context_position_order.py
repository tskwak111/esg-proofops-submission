"""R70: new context order is independent of run-minted source IDs."""

import json
import sys
from dataclasses import replace

import pytest
from proofops.application.tagging.preliminary import (
    CONTEXT_POSITION_ORDER,
    _bounded_context_blocks,
    preliminary_table_request,
)

from tests.acceptance.test_citations import TENANT
from tests.acceptance.test_preliminary import context_corpus


def test_position_order_survives_source_id_permutation_and_keeps_legacy():
    graph, claim, _ = context_corpus()
    refs = claim.source_refs
    legacy, _ = _bounded_context_blocks(graph, refs, max_context_chars=2000, max_context_blocks=4)
    assert [entry["role"] for entry in legacy] == ["parent_paragraph", "heading", "nearby"]

    def reordered(ids):
        mapping = dict(zip((block.source_id for block in graph.blocks), ids, strict=True))
        blocks = tuple(replace(block, source_id=mapping[block.source_id]) for block in graph.blocks)
        edges = tuple(
            replace(edge, source_id=mapping[edge.source_id], target_id=mapping[edge.target_id])
            for edge in graph.edges
        )
        return replace(graph, blocks=blocks, edges=edges), tuple(
            replace(ref, source_id=mapping[ref.source_id]) for ref in refs
        )

    original, _ = _bounded_context_blocks(
        graph, refs, max_context_chars=2000, max_context_blocks=4, position_order=True
    )
    shuffled, shuffled_refs = reordered(
        tuple(reversed([block.source_id for block in graph.blocks]))
    )
    alternate, _ = _bounded_context_blocks(
        shuffled,
        shuffled_refs,
        max_context_chars=2000,
        max_context_blocks=4,
        position_order=True,
    )
    assert [(item["role"], item["text"]) for item in original] == [
        (item["role"], item["text"]) for item in alternate
    ]
    alternate_claim = replace(
        claim,
        source_refs=shuffled_refs,
        receipt=replace(claim.receipt, source_id=shuffled_refs[0].source_id),
    )
    from proofops.application.tagging.preliminary import preliminary_request

    before = preliminary_request(
        claim, graph, tenant_id=TENANT, include_context=True, position_order=True
    )
    after = preliminary_request(
        alternate_claim,
        shuffled,
        tenant_id=TENANT,
        include_context=True,
        position_order=True,
    )

    def except_ids(value):
        if isinstance(value, dict):
            return {
                key: except_ids(item)
                for key, item in value.items()
                if key not in {"source_id", "claim_sha256", "graph_sha256"}
            }
        if isinstance(value, list):
            return [except_ids(item) for item in value]
        return value

    assert except_ids(before) == except_ids(after)
    assert [entry["role"] for entry in legacy] == ["parent_paragraph", "heading", "nearby"]


def test_position_order_ties_by_content_before_source_id():
    graph, claim, fixture = context_corpus()
    near = fixture["neighbor"]
    tied = replace(
        near,
        source_id="ffffffff-ffff-4fff-8fff-ffffffffffff",
        candidates=tuple(
            replace(candidate, source=replace(candidate.source, raw_text="AAA", char_end=3))
            for candidate in near.candidates
        ),
    )
    graph = replace(graph, blocks=(*graph.blocks, tied))
    first, _ = _bounded_context_blocks(
        graph,
        claim.source_refs,
        max_context_chars=2000,
        max_context_blocks=4,
        position_order=True,
    )
    assert [entry["text"] for entry in first if entry["role"] == "nearby"] == ["AAA", near.raw_text]


def test_new_preliminary_packet_records_order_policy_without_changing_old_packet():
    graph, claim, _ = context_corpus()
    old = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True, p2=True)
    new = preliminary_table_request(
        claim,
        graph,
        tenant_id=TENANT,
        role_resolution=True,
        p2=True,
        position_order=True,
    )
    assert new["context_ordering"] == CONTEXT_POSITION_ORDER
    assert "context_ordering" not in old
    assert new["prompt_sha256"] == old["prompt_sha256"]


def test_extraction_receipt_records_order_policy_and_legacy_profile_is_stable(tmp_path):
    from proofops.domain.provenance import canonical_hash
    from proofops_agent.upstage_extraction import _profile_with_options

    from tests.integration.test_upstage_extraction import (
        FakeProbe,
        _extraction_context_graph,
        make_context_extractor,
    )

    old = _profile_with_options(extraction_context=True, source_ids=True)
    assert old == _profile_with_options(
        extraction_context=True, source_ids=True, position_order=False
    )
    new = _profile_with_options(extraction_context=True, source_ids=True, position_order=True)
    assert old.rule_sha256 != new.rule_sha256
    probe = FakeProbe('{"sentence_ids": []}')
    extractor, packet = make_context_extractor(
        probe, tmp_path, extraction_source_ids=True, position_order=True
    )
    graph, source_id = _extraction_context_graph()
    packet["untrusted_document_data"]["source_id"] = source_id
    packet["source_sha256"] = "a" * 64
    assert extractor.extract(packet, context_graph=graph) == {"spans": []}
    receipt = tmp_path / "receipts" / probe.calls[0]["request_id"]
    result = json.loads((receipt / "result.json").read_text())
    request = json.loads((receipt / "request.json").read_text())
    assert json.loads(request["user_json"])["context_ordering"] == CONTEXT_POSITION_ORDER
    assert result["context_ordering"] == CONTEXT_POSITION_ORDER
    assert result["profile"]["rule_sha256"] == new.rule_sha256
    assert result["packet_sha256"] == canonical_hash(packet)
    assert json.loads((receipt / "packet.json").read_text()) == packet


def test_new_preliminary_transport_requires_position_policy(tmp_path, monkeypatch):
    from proofops.application.tagging.preliminary import P2_SYSTEM_PROMPT
    from proofops.domain.provenance import canonical_hash
    from proofops_agent.upstage_preliminary import (
        POSITION_P2_MODEL_PROFILE,
        POSITION_P2_TRANSPORT_VERSION,
    )

    from tests.integration.test_preliminary_table_role_prompt import _configured

    adapter, request, _, claim, graph = _configured(
        tmp_path,
        monkeypatch,
        profile=POSITION_P2_MODEL_PROFILE,
        prompt=P2_SYSTEM_PROMPT,
        role_resolution=True,
    )
    assert adapter.TRANSPORT_VERSION == POSITION_P2_TRANSPORT_VERSION
    old = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True, p2=True)
    request.update(user_json=json.dumps(old), packet_sha256=canonical_hash(old))
    try:
        adapter._wire_request(request)
    except ValueError:
        pass
    else:
        raise AssertionError("old P2 packet entered position profile")
    new = preliminary_table_request(
        claim,
        graph,
        tenant_id=TENANT,
        role_resolution=True,
        p2=True,
        position_order=True,
    )
    request.update(user_json=json.dumps(new), packet_sha256=canonical_hash(new))
    assert (
        json.loads(adapter._wire_request(request)[1])["context_ordering"] == CONTEXT_POSITION_ORDER
    )


def test_pilot_position_profile_and_resume_policy():
    from evaluation.local_upstage_pilot import apply_resume_metadata, live_tagging_settings
    from tests.unit.test_extraction_source_id_wiring import _args

    opts = dict(
        preliminary_context=True,
        preliminary_table_context=True,
        preliminary_table_role=True,
        preliminary_p2=True,
    )
    old = live_tagging_settings(12, **opts)
    new = live_tagging_settings(12, **opts, position_context_order=True)
    assert old["preliminary_settings"]["model_profile"].endswith("-v2-p2")
    assert new["preliminary_settings"]["model_profile"].endswith("-v2-p2-position-v1")
    args = _args()
    apply_resume_metadata(
        args, {"source_path": "/tmp/example.pdf", "position_context_order": CONTEXT_POSITION_ORDER}
    )
    assert args.position_context_order is True


def test_pilot_resume_cannot_add_position_order(tmp_path, monkeypatch, capsys):
    from evaluation import local_upstage_pilot as pilot

    state = tmp_path / "legacy"
    state.mkdir()
    (state / "pilot.json").write_text(json.dumps({"source_path": "/tmp/example.pdf"}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pilot",
            "--resume",
            "--state",
            str(state),
            "--position-context-order",
            "--key-file",
            str(tmp_path / "absent.key"),
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()
    assert "--resume cannot add position context order" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("profile", "expected_p2"),
    [
        ("upstage-preliminary-source-quotes-table-role-v1-position-v1", False),
        ("upstage-preliminary-source-quotes-table-role-v2-p2-position-v1", True),
    ],
)
def test_live_worker_uses_ordered_preliminary_packet(monkeypatch, profile, expected_p2):
    from types import SimpleNamespace

    import proofops_worker.live_tagging as live

    seen = {}

    def packet(_claim, _graph, **kwargs):
        seen.update(kwargs)
        return {"context_ordering": CONTEXT_POSITION_ORDER}

    monkeypatch.setattr(live, "preliminary_table_request", packet)
    runtime = live.LiveTaggingRuntime.__new__(live.LiveTaggingRuntime)
    runtime.preliminary_settings = SimpleNamespace(model_profile=profile)
    runtime.preliminary_transport = SimpleNamespace(bound_context=lambda value: value)
    runtime.preliminary_records = []
    runtime.auth = SimpleNamespace(tenant_id=TENANT)
    runtime.snapshot = {"position_context_order": CONTEXT_POSITION_ORDER}
    runtime._source_replicas = lambda *_args: None
    assert runtime.preliminary(object(), object()) is None
    assert seen["position_order"] is True
    assert seen["p2"] is expected_p2


def test_local_runtime_keeps_position_version_and_hash(tmp_path):
    from dataclasses import asdict

    from proofops_agent.upstage_extraction import _profile_with_options
    from proofops_api.local_runtime import load_local_runtime

    from evaluation.local_upstage_pilot import live_tagging_settings
    from tests.integration.test_local_runtime_config import _budget_limits, _parser_snapshot

    parser = tmp_path / "parser.json"
    parser.write_text(json.dumps(_parser_snapshot()))
    settings = tmp_path / "settings.json"
    body = {
        "build_root": str(tmp_path),
        "budget_limits": _budget_limits(),
        "extraction_profile": asdict(
            _profile_with_options(extraction_context=True, position_order=True)
        ),
        "extraction_limits": {"max_calls": 2, "max_output_tokens": 128},
        "extraction_context": True,
        "position_context_order": CONTEXT_POSITION_ORDER,
        **live_tagging_settings(
            12,
            preliminary_context=True,
            preliminary_table_context=True,
            preliminary_table_role=True,
            preliminary_p2=True,
            position_context_order=True,
        ),
    }
    settings.write_text(json.dumps(body))
    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser),
            "LOCAL_RUN_SETTINGS_PATH": str(settings),
            "LOCAL_EXTRACTION_MODE": "upstage_probe",
            "LOCAL_TAGGING_MODE": "upstage_local",
        }
    )
    assert runtime["position_context_order"] == CONTEXT_POSITION_ORDER


def test_extraction_only_position_order_needs_no_p2_or_live_tagging(tmp_path):
    from dataclasses import asdict

    from proofops_agent.upstage_extraction import _profile_with_options
    from proofops_api.local_runtime import load_local_runtime

    from tests.integration.test_local_runtime_config import _budget_limits, _parser_snapshot

    parser = tmp_path / "parser.json"
    parser.write_text(json.dumps(_parser_snapshot()))
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "build_root": str(tmp_path),
                "budget_limits": _budget_limits(),
                "extraction_profile": asdict(
                    _profile_with_options(
                        extraction_context=True,
                        position_order=True,
                    )
                ),
                "extraction_limits": {"max_calls": 2, "max_output_tokens": 128},
                "extraction_context": True,
                "position_context_order": CONTEXT_POSITION_ORDER,
            }
        )
    )
    runtime = load_local_runtime(
        {
            "LOCAL_PARSER_PROFILE_PATH": str(parser),
            "LOCAL_RUN_SETTINGS_PATH": str(settings),
            "LOCAL_EXTRACTION_MODE": "upstage_probe",
        }
    )
    assert runtime["position_context_order"] == CONTEXT_POSITION_ORDER


def test_default_table_role_has_distinct_position_profile_and_transport(tmp_path, monkeypatch):
    from proofops.application.tagging.preliminary import (
        CONTEXT_SYSTEM_SUFFIX,
        SYSTEM_PROMPT,
        TABLE_ROLE_SYSTEM_SUFFIX,
        TABLE_SYSTEM_SUFFIX,
    )
    from proofops.domain.provenance import canonical_hash
    from proofops_agent.upstage_preliminary import (
        POSITION_TABLE_ROLE_MODEL_PROFILE,
        POSITION_TABLE_ROLE_TRANSPORT_VERSION,
    )

    from evaluation.local_upstage_pilot import live_tagging_settings
    from tests.integration.test_preliminary_table_role_prompt import _configured

    opts = dict(
        preliminary_context=True, preliminary_table_context=True, preliminary_table_role=True
    )
    old = live_tagging_settings(12, **opts)
    new = live_tagging_settings(12, **opts, position_context_order=True)
    assert old["preliminary_settings"]["model_profile"] == (
        "upstage-preliminary-source-quotes-table-role-v1"
    )
    assert new["preliminary_settings"]["model_profile"] == POSITION_TABLE_ROLE_MODEL_PROFILE
    assert (
        new["preliminary_settings"]["system_prompt"] == old["preliminary_settings"]["system_prompt"]
    )
    prompt = SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX + TABLE_SYSTEM_SUFFIX + TABLE_ROLE_SYSTEM_SUFFIX
    adapter, request, _, claim, graph = _configured(
        tmp_path,
        monkeypatch,
        profile=POSITION_TABLE_ROLE_MODEL_PROFILE,
        prompt=prompt,
        role_resolution=True,
    )
    assert adapter.TRANSPORT_VERSION == POSITION_TABLE_ROLE_TRANSPORT_VERSION
    old_packet = preliminary_table_request(claim, graph, tenant_id=TENANT, role_resolution=True)
    new_packet = preliminary_table_request(
        claim,
        graph,
        tenant_id=TENANT,
        role_resolution=True,
        position_order=True,
    )
    assert "context_ordering" not in old_packet
    assert new_packet["context_ordering"] == CONTEXT_POSITION_ORDER
    request.update(user_json=json.dumps(old_packet), packet_sha256=canonical_hash(old_packet))
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PACKET_INVALID"):
        adapter._wire_request(request)
    request.update(user_json=json.dumps(new_packet), packet_sha256=canonical_hash(new_packet))
    assert (
        json.loads(adapter._wire_request(request)[1])["context_ordering"] == CONTEXT_POSITION_ORDER
    )
