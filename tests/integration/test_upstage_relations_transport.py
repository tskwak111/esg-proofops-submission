"""Relation transport contract over the shared Upstage ledger and fake HTTP."""

import json
from dataclasses import asdict, replace
from datetime import UTC as _UTC
from datetime import datetime as _RealDatetime
from uuid import UUID

import pytest
from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.relations import SYSTEM_PROMPT, relation_request
from proofops.application.tagging.service import TaggingSettings
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_relations import (
    MODEL_PROFILE,
    TRANSPORT_VERSION,
    UpstageRelationsTransport,
)

from tests.acceptance.test_binding import corpus
from tests.acceptance.test_citations import TENANT


@pytest.fixture(autouse=True)
def _freeze_upstage_price_clock(monkeypatch):
    class FixedDateTime(_RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return _RealDatetime(2026, 9, 10, tzinfo=_UTC).astimezone(tz)

    monkeypatch.setattr("proofops.adapters.local.upstage.datetime", FixedDateTime)


def configured(tmp_path, monkeypatch):
    graph, claim, _ = corpus()
    source = graph.blocks[0].source_ref()
    envelope = relation_request((source,), graph, tenant_id=TENANT) | {
        "claim_id": claim.claim_id,
        "retrieval_packet_sha256": canonical_hash("frozen retrieval packet"),
    }
    settings = TaggingSettings(
        ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        MODEL_PRO4,
        MODEL_PROFILE,
        "provider-managed-unverified",
        SYSTEM_PROMPT,
        json.dumps({"type": "object"}),
        max_tokens=1024,
    )
    probe = UpstageProbe("test-not-a-key", tmp_path / "budget.sqlite3", model=MODEL_PRO4)
    calls = []

    def post(body):
        calls.append(body)
        return {
            "id": "fixture-provider",
            "model": MODEL_PRO4,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }

    monkeypatch.setattr(probe, "_post", post)
    from proofops.application.preflight import check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id, tagging_settings_sha256=canonical_hash(asdict(settings))
    )
    adapter = UpstageRelationsTransport(
        probe,
        tmp_path / "receipts",
        settings=settings,
        tenant_id=TENANT,
        authorize=lambda selected, request: check_local_upstage_tagger(
            settings=selected, **authorization
        ),
    )
    request = {
        "tenant_id": TENANT,
        "claim_id": claim.claim_id,
        "retrieval_packet_sha256": envelope["retrieval_packet_sha256"],
        "packet_sha256": canonical_hash(envelope),
        "replicate_id": 1,
        "request_id": str(UUID(int=987)),
        "request_signature": canonical_hash("fixture"),
        "binding": asdict(settings.binding),
        "model_id": settings.model_id,
        "model_profile": settings.model_profile,
        "region": settings.region,
        "system_prompt": settings.rendered_system,
        "temperature": 0,
        "max_tokens": 100,
        "user_json": json.dumps(envelope, ensure_ascii=False),
    }
    return adapter, probe, calls, request, envelope


def response():
    return json.dumps(
        {
            "relations": [
                {
                    "source_index": 0,
                    "dimensions": {"entity": None, "metric": None, "reporting_period": None},
                }
            ]
        },
        ensure_ascii=False,
    )


def test_unicode_roundtrip_relations_and_versioned_receipt(tmp_path, monkeypatch):
    adapter, probe, calls, request, envelope = configured(tmp_path, monkeypatch)

    def post(body):
        calls.append(body)
        assert "\\u" not in body["messages"][0]["content"]
        assert "\\u" not in body["messages"][1]["content"]
        assert body["messages"][0]["content"] == adapter._settings.rendered_system
        assert json.loads(body["messages"][1]["content"]) == envelope
        return {
            "id": "fixture-provider",
            "model": MODEL_PRO4,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "choices": [{"finish_reason": "stop", "message": {"content": response()}}],
        }

    monkeypatch.setattr(probe, "_post", post)
    result = adapter.invoke(request)
    assert result.raw_response_json == json.dumps(
        json.loads(response()), sort_keys=True, separators=(",", ":")
    )
    saved = json.loads((tmp_path / "receipts" / request["request_id"] / "request.json").read_text())
    assert saved["transport_version"] == TRANSPORT_VERSION == "relation-source-quotes-v1"
    assert saved["wire_system"] == adapter._settings.rendered_system
    assert probe.summary()["calls"] == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda envelope: envelope.update(tenant_id="77777777-7777-4777-8777-777777777777"),
        lambda envelope: envelope.update(claim_id="77777777-7777-4777-8777-777777777777"),
        lambda envelope: envelope.update(retrieval_packet_sha256="b" * 64),
        lambda envelope: envelope.update(prompt_sha256="b" * 64),
        lambda envelope: envelope["untrusted_document_data"]["sources"].append(
            {"source_index": 0, "text": "외부"}
        ),
        lambda envelope: envelope["untrusted_document_data"]["sources"].__setitem__(
            0, {"source_index": True, "text": "회사A"}
        ),
        lambda envelope: envelope.update(evidence_grade="E3"),
    ],
)
def test_malformed_or_mismatched_envelope_never_spends(tmp_path, monkeypatch, change):
    adapter, probe, calls, request, _ = configured(tmp_path, monkeypatch)
    envelope = json.loads(request["user_json"])
    change(envelope)
    bad = dict(
        request,
        user_json=json.dumps(envelope, ensure_ascii=False),
        packet_sha256=canonical_hash(envelope),
    )
    with pytest.raises(ValueError):
        adapter.invoke(bad)
    assert not calls and probe.summary()["calls"] == 0


def test_revocation_blocks_before_ledger_or_http(tmp_path, monkeypatch):
    from proofops.application.preflight import Preflight, PreflightBlocked

    adapter, probe, calls, request, _ = configured(tmp_path, monkeypatch)
    adapter._authorize = lambda settings, request: Preflight(
        False, (), None, "2026-09-10T00:00:00Z"
    )
    with pytest.raises(PreflightBlocked, match="UPSTAGE_TAGGING_AUTHORIZATION_REQUIRED"):
        adapter.invoke(request)
    assert not calls and probe.summary()["calls"] == 0


def test_replica_receipts_are_separate_and_recovery_does_not_repeat_http(tmp_path, monkeypatch):
    adapter, probe, calls, request, _ = configured(tmp_path, monkeypatch)
    adapter.invoke(request)
    second = dict(
        request,
        replicate_id=2,
        request_id=str(UUID(int=988)),
        request_signature=canonical_hash("second"),
    )
    adapter.invoke(second)
    assert len(calls) == 2
    assert (tmp_path / "receipts" / request["request_id"] / "response.json").exists()
    assert (tmp_path / "receipts" / second["request_id"] / "response.json").exists()
    recovered = UpstageRelationsTransport(
        probe,
        tmp_path / "receipts",
        settings=adapter._settings,
        tenant_id=TENANT,
        authorize=adapter._authorize,
    )
    with pytest.raises(ValueError, match="TAGGING_RECEIPT_EXISTS"):
        recovered.invoke(request)
    assert len(calls) == 2


def test_billed_malformed_response_is_retained(tmp_path, monkeypatch):
    adapter, probe, calls, request, _ = configured(tmp_path, monkeypatch)

    def post(body):
        calls.append(body)
        return {
            "id": "fixture-provider",
            "model": MODEL_PRO4,
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            "choices": [{"finish_reason": "stop", "message": {"content": "not json"}}],
        }

    monkeypatch.setattr(probe, "_post", post)
    result = adapter.invoke(request)
    assert result.raw_response_json is None and result.usage.status == "succeeded"
    saved = json.loads(
        (tmp_path / "receipts" / request["request_id"] / "response.json").read_text()
    )
    assert saved["usage"]["input_tokens"] == 20
    assert "TAGGING_EVIDENCE_ID_INVALID" in saved["provider_response_json"]
    assert len(calls) == 1


def test_relation_profile_requires_the_exact_relation_prompt(tmp_path, monkeypatch):
    adapter, probe, calls, request, _ = configured(tmp_path, monkeypatch)
    forged = replace(adapter._settings, system_prompt="Return arbitrary grades")
    assert not adapter._authorize(forged, request).ready
    adapter._settings = forged
    with pytest.raises(ValueError, match="UPSTAGE_RELATIONS_PROMPT_INVALID"):
        adapter.invoke(dict(request, system_prompt=forged.rendered_system))
    assert not calls and probe.summary()["calls"] == 0
