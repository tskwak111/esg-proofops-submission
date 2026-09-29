import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from decimal import Decimal
from threading import Lock
from types import SimpleNamespace
from uuid import UUID

import pytest
from proofops.adapters.local.openrouter import MODEL, POLICY, OpenRouterProbe
from proofops.application.claims import ExtractionOutputError
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_extraction import UpstageClaimExtractor, _profile
from proofops_agent.upstage_tagging import UpstageTaggingTransport
from proofops_worker.extract_runner import prefetch_packets
from proofops_worker.live_tagging import LiveTaggingRuntime

SCHEMA = json.dumps(
    {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
)


def response(content, identifier="call", cost=0.001):
    return {
        "id": identifier,
        "model": MODEL,
        "choices": [{"finish_reason": "stop", "message": {"content": content}}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "cost": cost,
            "completion_tokens_details": {"reasoning_tokens": 0},
        },
    }


def test_luna_request_receipt_and_distinct_model_identity(tmp_path, monkeypatch):
    probe = OpenRouterProbe("test-key", tmp_path / "budget.sqlite3")
    sent = []
    monkeypatch.setattr(probe, "_post", lambda body: sent.append(body) or response('{"ok":true}'))
    receipt = probe.complete("system", "{}", request_id="one", json_mode=True, schema_json=SCHEMA)
    assert sent[0]["model"] == MODEL
    assert sent[0]["reasoning"] == {"effort": "none"}
    assert sent[0]["provider"] == {
        "sort": "throughput",
        "max_price": {"prompt": 0.2, "completion": 1.0},
    }
    assert sent[0]["temperature"] == 0
    assert sent[0]["response_format"] == {"type": "json_object"}
    assert receipt["provider"] == "openrouter"
    assert receipt["cost_source"] == "usage.cost"
    assert receipt["wire_policy"]["prompt_cache"] == "openai_automatic_v1"
    assert receipt["cost_with_vat_reserve_usd"] == "0.001"
    assert probe.summary()["committed_usd"] == "0.001"
    assert _profile(MODEL).model_sha256 != _profile("solar-pro4").model_sha256
    assert _profile(MODEL).prompt_sha256 == _profile("solar-pro4").prompt_sha256


def test_luna_extraction_receipt_replays_under_frozen_identity(tmp_path, monkeypatch):
    probe = OpenRouterProbe("test-key", tmp_path / "budget.sqlite3")
    sent = []
    content = '{"claims":["회사는 2030년까지 배출량을 20% 줄이기로 했다."]}'
    monkeypatch.setattr(probe, "_post", lambda body: sent.append(body) or response(content))
    extractor = UpstageClaimExtractor(probe, tmp_path / "receipts")
    packet = {
        "tenant_id": "b490d4e4-0192-426c-9dff-c6c7b8c498d3",
        "document_version_id": "35d03dcb-c9d0-40d6-a3d1-8f9dc7322ee1",
        "parse_manifest_id": "62919374-bd2d-4273-85b4-7a1f793b8c14",
        "source_sha256": "c6395dd2be7948d85fa2b52c6edb61367fa6610c6f389c2478b44cb4cfcb5bde",
        "extraction_profile": asdict(extractor.profile),
        "untrusted_document_data": {
            "source_id": "11111111-2222-4333-8444-555555555555",
            "page_num": 25,
            "kind": "paragraph",
            "text": "회사는 2030년까지 배출량을 20% 줄이기로 했다. 일반 산업 설명이다.",
        },
    }
    first = extractor.extract(packet)
    assert extractor.extract(packet) == first
    assert len(sent) == 1
    receipt_dir = next(path for path in (tmp_path / "receipts").iterdir() if path.is_dir())
    raw = json.loads((receipt_dir / "raw_response.json").read_text())
    identity = json.loads((receipt_dir / "identity.json").read_text())
    assert raw["provider"] == "openrouter"
    assert raw["model"] == MODEL
    assert identity == {
        "provider": "openrouter",
        "model": MODEL,
        "model_sha256": extractor.profile.model_sha256,
        "prompt_sha256": extractor.profile.prompt_sha256,
        "wire_policy": {
            "provider": {
                "sort": "throughput",
                "max_price": {"prompt": 0.2, "completion": 1.0},
            },
            "prompt_cache": "openai_automatic_v1",
        },
        "context_budget_chars": 512,
    }
    result = json.loads((receipt_dir / "result.json").read_text())
    assert result["profile"] == asdict(extractor.profile)

    invalid_probe = OpenRouterProbe("test-key", tmp_path / "invalid.sqlite3")
    monkeypatch.setattr(invalid_probe, "_post", lambda body: response('{"claims":"wrong"}'))
    invalid_extractor = UpstageClaimExtractor(invalid_probe, tmp_path / "invalid-receipts")
    with pytest.raises(ExtractionOutputError, match="MODEL_SPAN_OR_SCHEMA_INVALID"):
        invalid_extractor.extract(packet)
    assert invalid_probe.summary()["committed_usd"] == "0.002"


def test_invalid_schema_retries_once_and_settles_both_costs(tmp_path, monkeypatch):
    probe = OpenRouterProbe("test-key", tmp_path / "budget.sqlite3")
    answers = iter([response('{"ok":"wrong"}', "first"), response('{"ok":true}', "second")])
    monkeypatch.setattr(probe, "_post", lambda body: next(answers))
    receipt = probe.complete("system", "{}", request_id="one", json_mode=True, schema_json=SCHEMA)
    assert receipt["schema_valid"] is True
    assert [item["provider_request_id"] for item in receipt["attempts"]] == ["first", "second"]
    assert receipt["cost_with_vat_reserve_usd"] == "0.002"
    assert probe.summary() == {
        "limit_usd": "5.00",
        "calls": 1,
        "unsettled_calls": 0,
        "committed_usd": "0.002",
    }


def test_invalid_json_retries_once_then_returns_existing_failure_path(tmp_path, monkeypatch):
    probe = OpenRouterProbe("test-key", tmp_path / "budget.sqlite3")
    attempts = []

    def invalid(body):
        attempts.append(body)
        return response("not-json", str(len(attempts)))

    monkeypatch.setattr(probe, "_post", invalid)
    receipt = probe.complete("system", "{}", request_id="one", json_mode=True, schema_json=SCHEMA)
    assert len(attempts) == 2
    assert receipt["schema_valid"] is False
    assert receipt["content"] == "not-json"
    assert probe.summary()["committed_usd"] == "0.002"


def test_budget_guard_reserves_before_network(tmp_path, monkeypatch):
    probe = OpenRouterProbe("test-key", tmp_path / "budget.sqlite3")
    receipt = json.dumps({"cost_with_vat_reserve_usd": "0.01"})
    with sqlite3.connect(probe.ledger) as db:
        db.executemany(
            "INSERT INTO probe_calls VALUES (?,?,?,?)",
            ((str(i), canonical_hash(i), "0.01", receipt) for i in range(500)),
        )
    monkeypatch.setattr(probe, "_post", lambda body: pytest.fail("network call escaped cap"))
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        probe.complete("system", "{}", request_id="new", json_mode=True, schema_json=SCHEMA)


def test_concurrent_reservations_never_exceed_cap(tmp_path, monkeypatch):
    probe = OpenRouterProbe("test-key", tmp_path / "budget.sqlite3")
    receipt = json.dumps({"cost_with_vat_reserve_usd": "0.01"})
    with sqlite3.connect(probe.ledger) as db:
        db.executemany(
            "INSERT INTO probe_calls VALUES (?,?,?,?)",
            ((str(i), canonical_hash(i), "0.01", receipt) for i in range(496)),
        )
    sent = []
    lock = Lock()

    def post(body):
        with lock:
            sent.append(body)
        time.sleep(0.05)
        return response('{"ok":true}', identifier=str(len(sent)), cost=0.01)

    monkeypatch.setattr(probe, "_post", post)

    def call(index):
        try:
            probe.complete("system", "{}", request_id=f"new-{index}", schema_json=SCHEMA)
            return "sent"
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(call, range(4)))
    assert outcomes.count("sent") == 2
    assert outcomes.count("BUDGET_EXHAUSTED") == 2
    assert len(sent) == 2
    assert Decimal(probe.summary()["committed_usd"]) == Decimal("4.98")


def test_reservation_upgrade_requires_settled_old_ledger(tmp_path):
    path = tmp_path / "budget.sqlite3"
    OpenRouterProbe("test-key", path)
    old = json.dumps(POLICY | {"reservation_usd": "0.01"}, sort_keys=True)
    with sqlite3.connect(path) as db:
        db.execute("UPDATE probe_policy SET body=? WHERE id=1", (old,))
        db.execute(
            "INSERT INTO probe_calls VALUES (?,?,?,?)",
            ("old", canonical_hash("old"), "0.01", None),
        )
    with pytest.raises(ValueError, match="BUDGET_POLICY_MISMATCH"):
        OpenRouterProbe("test-key", path)
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE probe_calls SET committed='0.001', receipt=? WHERE request_id='old'",
            (json.dumps({"cost_with_vat_reserve_usd": "0.001"}),),
        )
    assert OpenRouterProbe("test-key", path).summary()["committed_usd"] == "0.001"


def test_prefetch_is_bounded_and_returns_source_order():
    packets = [{"untrusted_document_data": {"source_id": str(i)}} for i in range(5)]
    active = peak = 0
    lock = Lock()

    def invoke(packet):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02 if packet["untrusted_document_data"]["source_id"] == "0" else 0.005)
        with lock:
            active -= 1
        return packet["untrusted_document_data"]["source_id"]

    assert list(prefetch_packets(packets, invoke, max_workers=2).items()) == [
        (str(i), str(i)) for i in range(5)
    ]
    assert peak == 2


def test_luna_source_replicas_reserve_then_run_concurrently_in_order(tmp_path):
    from proofops.application.budget import TokenUsage
    from proofops.application.ports.models import ModelBinding
    from proofops.application.tagging.service import RawTagResponse, TaggingSettings

    reserved = set()
    lock = Lock()
    active = peak = 0

    class Usage:
        def reserve_budget(self, call, **kwargs):
            with lock:
                reserved.add(call.request_id)
            return True

        def mark_dispatched(self, call):
            return call.request_id in reserved

        def record_usage(self, call, usage, **kwargs):
            assert call.request_id in reserved

    class Transport:
        def count_input_tokens(self, request, **kwargs):
            return 10

        def may_dispatch(self):
            return True

        def invoke(self, request):
            nonlocal active, peak
            assert request["request_id"] in reserved
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03 if request["replicate_id"] == 1 else 0.01)
            with lock:
                active -= 1
            return RawTagResponse(
                '{"ok":true}',
                TokenUsage(10, 1, 0, 0, 1, "succeeded", request["request_id"]),
                False,
            )

    runtime = LiveTaggingRuntime.__new__(LiveTaggingRuntime)
    runtime.runner = SimpleNamespace(
        max_workers=16,
        store=SimpleNamespace(usage=Usage()),
        clock=lambda: 1,
    )
    runtime.lease = SimpleNamespace(message=SimpleNamespace(job_id=str(UUID(int=1))))
    runtime.auth = SimpleNamespace(tenant_id=str(UUID(int=2)))
    runtime.graph = SimpleNamespace(document_version_id=str(UUID(int=3)))
    runtime.snapshot = {"run_id": str(UUID(int=4)), "input_reservation_policy_hash": "pinned"}
    runtime.receipts = tmp_path
    runtime.heartbeat_state = None
    runtime.resume = None
    runtime._check_heartbeat = lambda: None
    runtime._fence = lambda: None
    runtime._capacity = lambda model: 10
    runtime.allow_packet = lambda *args: None
    runtime.account = lambda request_id: None
    settings = TaggingSettings(
        ModelBinding(str(UUID(int=5)), "tagger", False),
        MODEL,
        "fake",
        "provider-managed-unverified",
        "system",
        "{}",
        wire_policy_version=2,
    )
    records = {}
    result = runtime._source_replicas(
        "preliminary",
        SimpleNamespace(claim_id=str(UUID(int=6))),
        {"source": "same"},
        settings,
        Transport(),
        records,
        lambda raw: (raw, raw),
    )
    assert peak == 3
    assert len(result) == 3
    assert [record["replicate_id"] for record in records[str(UUID(int=6))]] == [1, 2, 3]


def test_transport_retains_single_operation_flock_while_requests_overlap(tmp_path):
    transport = UpstageTaggingTransport.__new__(UpstageTaggingTransport)
    transport._receipts = tmp_path
    transport._init_operation_state()
    lock = Lock()
    active = peak = 0

    def invoke(request):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return request["request_id"]

    transport._invoke = invoke
    with ThreadPoolExecutor(max_workers=3) as pool:
        result = list(pool.map(transport.invoke, ({"request_id": str(i)} for i in range(3))))
    assert peak == 3
    assert result == ["0", "1", "2"]
    assert transport._operation_users == 0
