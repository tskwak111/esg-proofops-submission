"""Bounded preliminary transport over real ledger/fake HTTP: no paid calls, no grades."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC as _UTC
from datetime import datetime as _RealDatetime
from uuid import UUID

import pytest
from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.ports.models import ModelBinding
from proofops.application.tagging.preliminary import SCHEMA, SYSTEM_PROMPT, preliminary_request
from proofops.application.tagging.service import TaggingSettings
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops_agent.upstage_preliminary import (
    MODEL_PROFILE,
    TRANSPORT_VERSION,
    UpstagePreliminaryTransport,
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
    envelope = preliminary_request(claim, graph, tenant_id=TENANT)
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
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content="{}"))],
        )

    monkeypatch.setattr(probe, "_post", post)
    from proofops.application.preflight import check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id, tagging_settings_sha256=canonical_hash(asdict(settings))
    )
    adapter = UpstagePreliminaryTransport(
        probe,
        tmp_path / "receipts",
        settings=settings,
        tenant_id=TENANT,
        authorize=lambda selected, request: check_local_upstage_tagger(
            settings=selected, **authorization
        ),
    )
    request = dict(
        tenant_id=TENANT,
        claim_id=claim.claim_id,
        packet_sha256=canonical_hash(envelope),
        replicate_id=1,
        request_id=str(UUID(int=987)),
        request_signature=canonical_hash("fixture"),
        binding=asdict(settings.binding),
        model_id=settings.model_id,
        model_profile=settings.model_profile,
        region=settings.region,
        system_prompt=settings.rendered_system,
        temperature=0,
        max_tokens=100,
    )
    request["user_json"] = json.dumps(envelope, ensure_ascii=False)
    return adapter, probe, calls, request, envelope, claim, graph


def configured_with_context(tmp_path, monkeypatch):
    """Same real wire as ``configured``, but the context-bearing profile/prompt."""
    from proofops_agent.upstage_preliminary import CONTEXT_MODEL_PROFILE, CONTEXT_TRANSPORT_VERSION

    from tests.acceptance.test_preliminary import context_corpus

    graph, claim, _ = context_corpus()
    envelope = preliminary_request(claim, graph, tenant_id=TENANT, include_context=True)
    context_prompt = SYSTEM_PROMPT + _context_suffix()
    settings = TaggingSettings(
        ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        MODEL_PRO4,
        CONTEXT_MODEL_PROFILE,
        "provider-managed-unverified",
        context_prompt,
        json.dumps({"type": "object"}),
        max_tokens=1024,
    )
    probe = UpstageProbe("test-not-a-key", tmp_path / "budget.sqlite3", model=MODEL_PRO4)
    calls = []

    def post(body):
        calls.append(body)
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content="{}"))],
        )

    monkeypatch.setattr(probe, "_post", post)
    from proofops.application.preflight import check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    authorization = approvals()
    authorization.pop("settings")
    authorization["binding"].update(
        model_id=settings.model_id, tagging_settings_sha256=canonical_hash(asdict(settings))
    )
    adapter = UpstagePreliminaryTransport(
        probe,
        tmp_path / "receipts",
        settings=settings,
        tenant_id=TENANT,
        authorize=lambda selected, request: check_local_upstage_tagger(
            settings=selected, **authorization
        ),
    )
    assert adapter.TRANSPORT_VERSION == CONTEXT_TRANSPORT_VERSION
    request = dict(
        tenant_id=TENANT,
        claim_id=claim.claim_id,
        packet_sha256=canonical_hash(envelope),
        replicate_id=1,
        request_id=str(UUID(int=988)),
        request_signature=canonical_hash("fixture"),
        binding=asdict(settings.binding),
        model_id=settings.model_id,
        model_profile=settings.model_profile,
        region=settings.region,
        system_prompt=settings.rendered_system,
        temperature=0,
        max_tokens=100,
    )
    request["user_json"] = json.dumps(envelope, ensure_ascii=False)
    return adapter, probe, calls, request, envelope, claim, graph


def _context_suffix():
    from proofops.application.tagging.preliminary import CONTEXT_SYSTEM_SUFFIX

    return CONTEXT_SYSTEM_SUFFIX


def valid_response_content(claim):
    return json.dumps(
        dict(
            claim_id=claim.claim_id,
            track="performance",
            safe_harbor_category=None,
            track_confidence=0.8,
            dimensions=dict(
                entity=dict(source_index=0, quote="회사A"),
                metric=None,
                reporting_period=None,
            ),
        ),
        ensure_ascii=False,
    )


def test_valid_content_roundtrip_with_literal_unicode_and_versioned_receipt(tmp_path, monkeypatch):
    adapter, probe, calls, request, envelope, claim, graph = configured(tmp_path, monkeypatch)
    content = valid_response_content(claim)

    def post(body):
        calls.append(body)
        assert "\\u" not in body["messages"][0]["content"]
        assert "\\u" not in body["messages"][1]["content"]
        assert body["messages"][0]["content"] == adapter._settings.rendered_system
        sent = json.loads(body["messages"][1]["content"])
        assert sent == envelope
        assert sent["schema"] == SCHEMA
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=content))],
        )

    monkeypatch.setattr(probe, "_post", post)
    result = adapter.invoke(request)
    assert result.synthetic is False
    assert result.usage.status == "succeeded"
    assert result.usage.input_tokens == 20
    assert json.loads(result.raw_response_json) == json.loads(canonical_json(json.loads(content)))
    assert result.raw_response_json == canonical_json(json.loads(content))
    assert json.loads(result.provider_response_json)["content"] == content
    assert probe.summary()["calls"] == 1 and probe.summary()["unsettled_calls"] == 0
    directory = tmp_path / "receipts" / request["request_id"]
    saved = json.loads((directory / "request.json").read_text())
    assert saved["transport_version"] == TRANSPORT_VERSION == "preliminary-source-quotes-v1"
    assert saved["wire_prompt_sha256"] == canonical_hash(saved["wire_system"])
    assert saved["evidence_refs"] == {}
    assert "source_quality" not in saved["wire_user_json"]
    assert "evidence_grade" not in result.raw_response_json
    assert "evidence_grade" not in (directory / "response.json").read_text()
    from proofops.application.tagging.preliminary import validate_preliminary

    validated = validate_preliminary(
        claim, graph, json.loads(result.raw_response_json), tenant_id=TENANT
    )
    assert validated.track.track == "performance"
    assert validated.context.dimensions["entity"].quote == "회사A"
    with pytest.raises(ValueError, match="TAGGING_RECEIPT_EXISTS"):
        adapter.invoke(request)
    assert len(calls) == 1


def mutate_user(request, change):
    user = json.loads(request["user_json"])
    change(user)
    request = dict(request)
    request["user_json"] = json.dumps(user, ensure_ascii=False)
    # Exercise the envelope validator even when a caller recomputes the hash.
    request["packet_sha256"] = canonical_hash(user)
    return request


@pytest.mark.parametrize(
    "change",
    [
        lambda user: user.update(tenant_id="77777777-7777-4777-8777-777777777777"),
        lambda user: user.update(packet_sha256="b" * 64),
        lambda user: user.update(prompt_sha256="c" * 64),
        lambda user: user.update(evidence_grade="E3"),
        lambda user: user.update(label="SUBSTANTIATED"),
        lambda user: user["untrusted_document_data"].update(topic_ids=["environment"]),
        lambda user: user["untrusted_document_data"]["sources"].append(
            dict(source_index=99, text="외부")
        ),
        lambda user: user["untrusted_document_data"]["sources"].__setitem__(
            0, dict(source_index=True, text="회사A")
        ),
        lambda user: user["untrusted_document_data"]["sources"].__setitem__(
            0, dict(source_index=1, text="회사A")
        ),
        lambda user: user["untrusted_document_data"]["sources"].__setitem__(
            0, dict(source_index=0, text="")
        ),
        lambda user: user["untrusted_document_data"]["sources"].__setitem__(
            0, dict(source_index=0, text="회사A", grade="E3")
        ),
        lambda user: user.pop("graph_sha256"),
    ],
)
def test_wire_envelope_rejected_before_spend(tmp_path, monkeypatch, change):
    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    bad = mutate_user(request, change)
    with pytest.raises(ValueError):
        adapter.invoke(bad)
    with pytest.raises(ValueError):
        adapter.count_input_tokens(bad, counter=lambda a, b: pytest.fail("must not count"))
    assert not calls and probe.summary()["calls"] == 0
    assert not (tmp_path / "receipts" / request["request_id"]).exists()


@pytest.mark.parametrize(
    "change",
    [
        dict(tenant_id="77777777-7777-4777-8777-777777777777"),
        dict(model_id="other"),
        dict(model_profile="upstage-compact-ids-frozen-unicode-v1"),
        dict(temperature=1),
        dict(replicate_id=True),
        dict(max_tokens=2048),
        dict(system_prompt="forged system"),
    ],
)
def test_identity_rejected_before_spend(tmp_path, monkeypatch, change):
    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        adapter.invoke({**request, **change})
    assert not calls and probe.summary()["calls"] == 0


def test_packet_digest_mismatch_rejected_before_spend(tmp_path, monkeypatch):
    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    request["packet_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PACKET_MISMATCH"):
        adapter.invoke(request)
    assert not calls and probe.summary()["calls"] == 0


def test_revoked_authorization_blocks_before_ledger_or_counter(tmp_path, monkeypatch):
    from proofops.application.preflight import Preflight, PreflightBlocked

    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    adapter._authorize = lambda settings, request: Preflight(
        False, (), None, "2026-09-10T00:00:00Z"
    )
    with pytest.raises(PreflightBlocked, match="UPSTAGE_TAGGING_AUTHORIZATION_REQUIRED"):
        adapter.invoke(request)
    with pytest.raises(PreflightBlocked, match="UPSTAGE_TAGGING_AUTHORIZATION_REQUIRED"):
        adapter.count_input_tokens(request, counter=lambda a, b: pytest.fail("must not count"))
    assert calls == [] and probe.summary()["calls"] == 0
    assert not (tmp_path / "receipts" / request["request_id"]).exists()


def test_arbitrary_prompt_cannot_pin_itself_as_current_version(tmp_path, monkeypatch):
    from proofops.application.preflight import PreflightBlocked

    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    forged = replace(adapter._settings, system_prompt="Return arbitrary grades")
    adapter._settings = forged
    request = dict(request, system_prompt=forged.rendered_system)
    user = json.loads(request["user_json"])
    user["prompt_sha256"] = canonical_hash("Return arbitrary grades")
    request["user_json"] = json.dumps(user, ensure_ascii=False)
    request["packet_sha256"] = canonical_hash(user)
    with pytest.raises((PreflightBlocked, ValueError)):
        adapter.invoke(request)
    assert not calls and probe.summary()["calls"] == 0


def test_incomplete_receipt_blocks_new_paid_replica_after_restart(tmp_path, monkeypatch):
    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    interrupted = tmp_path / "receipts" / str(UUID(int=123456))
    interrupted.mkdir()
    (interrupted / "request.json").write_text("{}")
    response = adapter.invoke(request)
    assert response.usage.status == "failed" and not calls
    assert probe.summary()["calls"] == 0


def test_count_and_invoke_share_identical_wire(tmp_path, monkeypatch):
    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    counted = []

    def counter(system, user):
        counted.append([dict(role="system", content=system), dict(role="user", content=user)])
        return 20

    assert adapter.count_input_tokens(request, counter=counter) == 20
    adapter.invoke(request)
    assert len(calls) == 1
    assert counted == [calls[0]["messages"]]
    assert calls[0]["messages"][0]["content"] == adapter._settings.rendered_system


def test_transport_profile_is_pinned_and_isolated_from_compact(tmp_path, monkeypatch):
    from proofops_agent.upstage_tagging import MODEL_PROFILE as COMPACT_PROFILE
    from proofops_agent.upstage_tagging import UpstageTaggingTransport

    adapter, probe, _, _, _, _, _ = configured(tmp_path, monkeypatch)
    assert MODEL_PROFILE == "upstage-preliminary-source-quotes-v1"
    assert COMPACT_PROFILE == "upstage-compact-ids-frozen-unicode-v1"
    assert UpstagePreliminaryTransport.TRANSPORT_VERSION == "preliminary-source-quotes-v1"
    assert UpstageTaggingTransport.TRANSPORT_VERSION == "compact-evidence-ids-v1"
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        UpstagePreliminaryTransport(
            probe,
            tmp_path / "other-receipts",
            settings=replace(adapter._settings, model_profile=COMPACT_PROFILE),
            tenant_id=adapter._tenant,
            authorize=adapter._authorize,
        )
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        UpstageTaggingTransport(
            probe,
            tmp_path / "compact-receipts",
            settings=adapter._settings,
            tenant_id=adapter._tenant,
            authorize=adapter._authorize,
        )
    assert probe.summary()["calls"] == 0


def test_success_receipt_retains_dispatch_authorization(tmp_path, monkeypatch):
    adapter, _, _, request, _, _, _ = configured(tmp_path, monkeypatch)
    expected = adapter._authorize(adapter._settings, request).to_dict()
    adapter.invoke(request)
    saved = json.loads((tmp_path / "receipts" / request["request_id"] / "request.json").read_text())
    assert saved["authorization"] == expected


def test_composition_can_reject_a_packet_outside_authorized_source(tmp_path, monkeypatch):
    from proofops.application.preflight import PreflightBlocked

    adapter, probe, calls, request, _, _, _ = configured(tmp_path, monkeypatch)
    approved_packet = request["packet_sha256"]
    previous = adapter._authorize

    def authorize(settings, actual_request):
        if actual_request["packet_sha256"] != approved_packet:
            raise PreflightBlocked("PACKET_SCOPE_MISMATCH")
        return previous(settings, actual_request)

    adapter._authorize = authorize
    changed = dict(request, packet_sha256="b" * 64)
    with pytest.raises(PreflightBlocked, match="PACKET_SCOPE_MISMATCH"):
        adapter.invoke(changed)
    assert calls == [] and probe.summary()["calls"] == 0


# --- R03b: real wire round-trip for the opt-in context-bearing profile ---


def test_context_profile_real_invoke_roundtrip_with_full_provenance(tmp_path, monkeypatch):
    adapter, probe, calls, request, envelope, claim, graph = configured_with_context(
        tmp_path, monkeypatch
    )
    content = valid_response_content(claim)

    def post(body):
        calls.append(body)
        sent = json.loads(body["messages"][1]["content"])
        # bbox round-trips tuple->list through JSON; compare via re-serialization.
        assert sent == json.loads(json.dumps(envelope))
        assert sent["schema"] == "preliminary-source-quotes-context-v1"
        data = sent["untrusted_document_data"]
        assert set(data) == {"sources", "context_blocks", "omitted_source_ids"}
        roles = {b["role"] for b in data["context_blocks"]}
        assert roles == {"parent_paragraph", "heading", "nearby"}
        for block in data["context_blocks"]:
            assert block["source_id"] and block["page_num"] >= 1
            assert block["quality"] in ("verified", "unverified")
            assert isinstance(block["source_ref"], dict)
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=content))],
        )

    monkeypatch.setattr(probe, "_post", post)
    result = adapter.invoke(request)
    assert result.synthetic is False
    assert result.usage.status == "succeeded"
    assert len(calls) == 1
    from proofops.application.tagging.preliminary import validate_preliminary

    validated = validate_preliminary(
        claim, graph, json.loads(result.raw_response_json), tenant_id=TENANT
    )
    assert validated.track.track == "performance"
    assert validated.context.dimensions["entity"].quote == "회사A"


def test_context_profile_rejects_a_legacy_shaped_packet_and_vice_versa(tmp_path, monkeypatch):
    adapter, probe, calls, request, _, _, _ = configured_with_context(tmp_path, monkeypatch)
    # A legacy (no context_blocks) envelope sent under the context profile is rejected.
    legacy_shaped = dict(request)
    from tests.acceptance.test_binding import corpus as legacy_corpus

    legacy_graph, legacy_claim, _ = legacy_corpus()
    legacy_envelope = preliminary_request(legacy_claim, legacy_graph, tenant_id=TENANT)
    legacy_shaped["user_json"] = json.dumps(legacy_envelope, ensure_ascii=False)
    legacy_shaped["packet_sha256"] = canonical_hash(legacy_envelope)
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PACKET_INVALID"):
        adapter.invoke(legacy_shaped)
    assert calls == [] and probe.summary()["calls"] == 0


def test_legacy_profile_rejects_a_context_shaped_packet(tmp_path, monkeypatch):
    from tests.acceptance.test_preliminary import context_corpus

    adapter, probe, calls, request, _, claim, graph = configured(tmp_path, monkeypatch)
    context_graph, context_claim, _ = context_corpus()
    context_envelope = preliminary_request(
        context_claim, context_graph, tenant_id=TENANT, include_context=True
    )
    smuggled = dict(request)
    smuggled["user_json"] = json.dumps(context_envelope, ensure_ascii=False)
    smuggled["packet_sha256"] = canonical_hash(context_envelope)
    with pytest.raises(ValueError, match="UPSTAGE_PRELIMINARY_PACKET_INVALID"):
        adapter.invoke(smuggled)
    assert calls == [] and probe.summary()["calls"] == 0


def test_oversize_context_is_bounded_before_hashing_without_losing_sources(tmp_path, monkeypatch):
    adapter, probe, calls, request, envelope, _, _ = configured_with_context(tmp_path, monkeypatch)
    # Korean UTF-8 plus context provenance exceeds the real wire ceiling.
    blocks = envelope["untrusted_document_data"]["context_blocks"]
    assert blocks
    blocks[-1]["text"] = "한글 문맥 " * 2000
    original = deepcopy(envelope)
    bounded = adapter.bound_context(envelope)
    assert envelope == original
    assert (
        bounded["untrusted_document_data"]["sources"]
        == original["untrusted_document_data"]["sources"]
    )
    assert blocks[-1]["source_id"] in bounded["untrusted_document_data"]["omitted_source_ids"]
    assert canonical_hash(bounded) != canonical_hash(envelope)
    request["user_json"] = json.dumps(bounded, ensure_ascii=False)
    request["packet_sha256"] = canonical_hash(bounded)
    system, wire, _, _ = adapter._wire_request(request)
    probe.request_body(
        system, wire, request_id=request["request_id"], max_tokens=100, json_mode=True
    )
    assert calls == [] and probe.summary()["calls"] == 0
    assert adapter.bound_context(bounded) == bounded


def test_context_bound_does_not_hide_oversize_numbered_sources(tmp_path, monkeypatch):
    adapter, probe, calls, request, envelope, _, _ = configured_with_context(tmp_path, monkeypatch)
    envelope["untrusted_document_data"]["sources"][0]["text"] = "인용 근거 " * 4000
    bounded = adapter.bound_context(envelope)
    assert (
        bounded["untrusted_document_data"]["sources"]
        == envelope["untrusted_document_data"]["sources"]
    )
    request["user_json"] = json.dumps(bounded, ensure_ascii=False)
    request["packet_sha256"] = canonical_hash(bounded)
    system, wire, _, _ = adapter._wire_request(request)
    with pytest.raises(ValueError, match="PROBE_REQUEST_TOO_LARGE"):
        probe.request_body(
            system, wire, request_id=request["request_id"], max_tokens=100, json_mode=True
        )
    assert calls == [] and probe.summary()["calls"] == 0


def test_context_sizing_leaves_price_stop_to_authorized_dispatch(tmp_path, monkeypatch):
    adapter, probe, calls, request, envelope, _, _ = configured_with_context(tmp_path, monkeypatch)

    class ExpiredClock(_RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return _RealDatetime(2026, 10, 2, tzinfo=_UTC).astimezone(tz)

    monkeypatch.setattr("proofops.adapters.local.upstage.datetime", ExpiredClock)
    assert adapter.bound_context(envelope) == envelope
    with pytest.raises(ValueError, match="PRICE_RECHECK_REQUIRED"):
        adapter.count_input_tokens(request, counter=lambda system, user: 100)
    assert calls == [] and probe.summary()["calls"] == 0
