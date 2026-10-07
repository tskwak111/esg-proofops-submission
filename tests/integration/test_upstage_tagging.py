"""Real ledger/fake HTTP: no paid calls or real source-quality approval."""

import gzip
import json
from dataclasses import asdict, replace
from datetime import UTC as _UTC
from datetime import datetime as _RealDatetime
from pathlib import Path
from uuid import UUID

import pytest
from proofops.adapters.local.upstage import MODEL_PRO4, UpstageProbe
from proofops.application.ports.models import ModelBinding
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_tagging import (
    MODEL_PROFILE,
    QUOTE_V5_PROFILE,
    UpstageTaggingTransport,
)

from tests.acceptance.test_tagging import setup


# Freeze the offline transport clock; worker authorization expiry remains real
# relative to the existing fixture clock, including its explicit expiry test.
@pytest.fixture(autouse=True)
def _freeze_upstage_price_clock(monkeypatch):
    class FixedDateTime(_RealDatetime):
        @classmethod
        def now(cls, tz=None):
            return _RealDatetime(2026, 9, 10, tzinfo=_UTC).astimezone(tz)

    monkeypatch.setattr("proofops.adapters.local.upstage.datetime", FixedDateTime)


def configured(tmp_path, monkeypatch, model_profile=MODEL_PROFILE):
    inputs = setup(tmp_path)
    settings = replace(
        inputs["settings"],
        binding=ModelBinding("00000000-0000-4000-8000-000000000001", "tagger", False),
        model_id=MODEL_PRO4,
        model_profile=model_profile,
        region="provider-managed-unverified",
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
    adapter = UpstageTaggingTransport(
        probe,
        tmp_path / "receipts",
        settings=settings,
        tenant_id=inputs["tenant_id"],
        authorize=lambda selected, request: check_local_upstage_tagger(
            settings=selected, **authorization
        ),
    )
    request = dict(
        tenant_id=inputs["tenant_id"],
        claim_id=inputs["context"].claim.claim_id,
        packet_sha256=inputs["packet"].packet_sha256,
        replicate_id=1,
        request_id=str(UUID(int=987)),
        request_signature=canonical_hash("fixture"),
        binding=asdict(settings.binding),
        model_id=settings.model_id,
        model_profile=settings.model_profile,
        region=settings.region,
        system_prompt=settings.rendered_system
        + "\nValidated classification; tag only its elements: "
        + '{"track":"performance","safe_harbor_category":null}',
        temperature=0,
        max_tokens=100,
    )
    request["user_json"] = json.dumps(
        dict(
            claim_id=request["claim_id"],
            packet_sha256=request["packet_sha256"],
            replicate_id=1,
            untrusted_document_data=dict(allowed_elements=[f"P{i}" for i in range(1, 7)]),
        )
    )
    return adapter, probe, calls, request


def test_receipt_single_reservation_and_duplicate_stop(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    result = adapter.invoke(request)
    assert result.raw_response_json == "{}" and result.synthetic is False
    assert result.usage.input_tokens == 20
    assert probe.summary()["calls"] == 1 and probe.summary()["unsettled_calls"] == 0
    assert (tmp_path / "receipts" / request["request_id"] / "response.json").exists()
    with pytest.raises(ValueError, match="TAGGING_RECEIPT_EXISTS"):
        adapter.invoke(request)
    assert len(calls) == 1


def test_unknown_transport_stops_siblings_and_retains_reservation(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)

    def fail(body):
        calls.append(body)
        raise RuntimeError("secret provider detail")

    monkeypatch.setattr(probe, "_post", fail)
    first = adapter.invoke(request)
    second = adapter.invoke({**request, "request_id": str(UUID(int=988))})
    assert first.usage.status == second.usage.status == "failed"
    assert "secret" not in first.provider_response_json
    assert len(calls) == 1 and probe.summary()["unsettled_calls"] == 1
    assert probe.summary()["committed_usd"] == "1.00"


@pytest.mark.parametrize(
    "change",
    [
        dict(tenant_id=str(UUID(int=777))),
        dict(model_id="other"),
        dict(binding=dict(binding_id="other", role="tagger", synthetic=False)),
        dict(temperature=1),
        dict(replicate_id=True),
        dict(max_tokens=101),
    ],
)
def test_identity_rejected_before_spend(tmp_path, monkeypatch, change):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        adapter.invoke({**request, **change})
    assert not calls and probe.summary()["calls"] == 0


def test_compact_references_preserve_source_without_model_copying(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    original = setup(tmp_path)["packet"].to_dict()["evidence_candidates"][0]["source_refs"][0]
    user = json.loads(request["user_json"])
    user["untrusted_document_data"]["evidence_candidates"] = [dict(source_refs=[original])]
    request["user_json"] = json.dumps(user)

    def post(body):
        calls.append(body)
        assert "\\u" not in body["messages"][1]["content"]
        sent = json.loads(body["messages"][1]["content"])
        assert sent["untrusted_document_data"]["evidence_candidates"][0]["source_refs"] == ["e0"]
        assert (
            sent["untrusted_document_data"]["evidence_catalog"]["e0"]["quote"] == original["quote"]
        )
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[
                dict(
                    finish_reason="stop",
                    message=dict(
                        content=json.dumps(
                            dict(
                                elements=[
                                    dict(element_id="P1", state="present", evidence_refs=["e0"])
                                ]
                            )
                        )
                    ),
                )
            ],
        )

    monkeypatch.setattr(probe, "_post", post)
    response = adapter.invoke(request)
    assert json.loads(response.raw_response_json)["elements"][0]["evidence_refs"] == [original]
    assert json.loads(response.provider_response_json)["content"] != response.raw_response_json


def test_invented_compact_id_is_unknown_without_refund_or_retry(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)

    def post(body):
        calls.append(body)
        return dict(
            id="fixture-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[
                dict(
                    finish_reason="stop",
                    message=dict(content='{"elements":[{"evidence_refs":["outside-packet"]}]}'),
                )
            ],
        )

    monkeypatch.setattr(probe, "_post", post)
    response = adapter.invoke(request)
    assert response.raw_response_json is None and response.usage.status == "succeeded"
    assert probe.summary()["unsettled_calls"] == 0
    assert not (tmp_path / "receipts" / "transport-stop.json").exists()


def test_existing_tagging_guards_and_cache_with_single_monetary_ledger(tmp_path, monkeypatch):
    from proofops.application.budget import cost_summary

    from tests.acceptance.test_tagging import execute

    adapter, probe, calls, _ = configured(tmp_path, monkeypatch)
    inputs = setup(tmp_path)
    inputs.update(settings=adapter._settings, invoke=adapter.invoke, pricing=None)
    counted = []

    def count_messages(system, user):
        # Synthetic token value; verifies wire identity, never model-token accuracy.
        counted.append([dict(role="system", content=system), dict(role="user", content=user)])
        return 20

    inputs["count_input_tokens"] = lambda request: adapter.count_input_tokens(
        request, counter=count_messages
    )

    def post(body):
        calls.append(body)
        user = json.loads(body["messages"][1]["content"])
        wire_schema = json.loads(
            body["messages"][0]["content"]
            .split("\nOutput JSON schema:\n")[1]
            .split("\nValidated classification;")[0]
        )
        assert wire_schema["properties"]["track"] == {"const": "performance"}
        assert wire_schema["$defs"]["Element"]["properties"]["element_id"] == {
            "enum": [f"P{i}" for i in range(1, 7)]
        }
        tags = dict(
            claim_id=user["claim_id"],
            packet_sha256=user["packet_sha256"],
            replicate_id=user["replicate_id"],
            track="performance",
            safe_harbor_category=None,
            elements=[
                dict(
                    element_id=f"P{i}",
                    state="unknown",
                    evidence_refs=[],
                    normalized_value=None,
                    credited_from=None,
                    reason_code="fixture-unresolved",
                )
                for i in range(1, 7)
            ],
            superlative_quote=None,
            warnings=[],
        )
        return dict(
            id="fixture-" + str(user["replicate_id"]),
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(tags)))],
        )

    monkeypatch.setattr(probe, "_post", post)
    runs = execute(inputs)
    assert len(calls) == 3 and [r.replicate_id for r in runs] == [1, 2, 3]
    assert all(r.guarded is not None and r.synthetic for r in runs)
    assert all(r.guarded.elements[0].state == "unknown" for r in runs)
    assert probe.summary()["calls"] == 3 and probe.summary()["unsettled_calls"] == 0
    assert counted == [body["messages"] for body in calls]
    rows = inputs["usage_store"].cost_data(inputs["tenant_id"], runs[0].run_id)
    assert len(rows) == 3
    assert all(row["reservation"]["input_tokens"] == 20 for row in rows)
    costs = cost_summary(inputs["usage_store"], inputs["tenant_id"], runs[0].run_id)
    assert costs["amount"] is None
    recovered = execute(inputs)
    assert len(calls) == 3 and all(r.recovered for r in recovered)
    assert len(counted) == 3  # Recovery neither counts nor dispatches again.


def test_real_tagging_requires_request_counter_before_spend(tmp_path, monkeypatch):
    from tests.acceptance.test_tagging import execute

    adapter, probe, calls, _ = configured(tmp_path, monkeypatch)
    inputs = setup(tmp_path)
    inputs.update(settings=adapter._settings, invoke=adapter.invoke)
    with pytest.raises(ValueError, match="TAGGING_INPUT_COUNTER_REQUIRED"):
        execute(inputs)
    assert not calls and probe.summary()["calls"] == 0
    assert not inputs["usage_store"].cost_data(
        inputs["tenant_id"], inputs["packet"].to_dict()["run_id"]
    )


def test_wire_input_over_budget_never_reaches_provider(tmp_path, monkeypatch):
    from tests.acceptance.test_tagging import execute

    adapter, probe, calls, _ = configured(tmp_path, monkeypatch)
    inputs = setup(tmp_path)
    inputs.update(
        settings=adapter._settings,
        invoke=adapter.invoke,
        count_input_tokens=lambda request: adapter.count_input_tokens(
            request, counter=lambda system, user: 10001
        ),
    )
    runs = execute(inputs)
    assert len(runs) == 3 and all(run.status == "budget_exhausted" for run in runs)
    assert not calls and probe.summary()["calls"] == 0
    assert not list((tmp_path / "receipts").iterdir())


@pytest.mark.parametrize("value", [True, -1, "20", None])
def test_invalid_input_count_is_not_a_cache_failure(tmp_path, monkeypatch, value):
    from tests.acceptance.test_tagging import execute

    adapter, probe, calls, _ = configured(tmp_path, monkeypatch)
    inputs = setup(tmp_path)

    def counter(request):
        if value is None:
            raise ValueError("private tokenizer failure")
        return value

    inputs.update(settings=adapter._settings, invoke=adapter.invoke, count_input_tokens=counter)
    runs = execute(inputs)
    assert all(run.status == "invalid_request" for run in runs)
    assert all(run.errors == ("TAGGING_INPUT_COUNT_INVALID",) for run in runs)
    if value is None:
        assert all(run.diagnostic_detail == "ValueError" for run in runs)
    assert not calls and probe.summary()["calls"] == 0


def test_input_count_diagnostic_keeps_only_allowlisted_codes(tmp_path, monkeypatch):
    from tests.acceptance.test_tagging import execute

    adapter, _, _, _ = configured(tmp_path, monkeypatch)
    inputs = setup(tmp_path)
    inputs.update(settings=adapter._settings, invoke=adapter.invoke)
    for detail, expected in (
        ("PROBE_REQUEST_TOO_LARGE", "ValueError: PROBE_REQUEST_TOO_LARGE"),
        ("FAKE_SECRET_AND_PII_SENTINEL", "ValueError"),
    ):
        inputs["count_input_tokens"] = lambda request, detail=detail: (_ for _ in ()).throw(
            ValueError(detail)
        )
        runs = execute(inputs)
        assert all(run.diagnostic_detail == expected for run in runs)
        assert all("FAKE_SECRET" not in str(asdict(run)) for run in runs)


def test_archived_element_wire_is_byte_identical_for_existing_profile():
    from proofops.application.ports.models import ModelBinding
    from proofops.application.tagging.service import TaggingSettings

    with gzip.open(Path(__file__).with_name("element_wire_receipts.json.gz"), "rt") as stream:
        receipts = json.load(stream)
    assert {item["run"] for item in receipts} == {"r52-naver-p2", "r67-naver-typography"}
    for item in receipts:
        settings = item["settings"] | {"binding": ModelBinding(**item["settings"]["binding"])}
        transport = object.__new__(UpstageTaggingTransport)
        transport._settings = TaggingSettings(**settings)
        transport._authorize_request = lambda request: None
        system, user, _, _, _ = transport._wire_request(item["request"])
        assert user.encode() == item["wire_user_json"].encode()
        assert system.encode() == item["wire_system"].encode()


def test_element_wire_compacts_provenance_before_size_check(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch, QUOTE_V5_PROFILE)
    assert adapter.TRANSPORT_VERSION == "compact-source-quotes-v5"
    original = setup(tmp_path)["packet"].to_dict()["evidence_candidates"][0]["source_refs"][0]
    user = json.loads(request["user_json"])
    data = user["untrusted_document_data"]
    data["atomic_quote"] = original["quote"]
    data["claim_source_refs"] = [original]
    data["evidence_candidates"] = [
        dict(
            source_id=str(UUID(int=index + 1)),
            source_scope="local_claim",
            allowed_elements=[f"P{i}" for i in range(1, 7)],
            source_refs=[original | {"source_id": str(UUID(int=index + 1))}],
        )
        for index in range(20)
    ]
    request["user_json"] = json.dumps(user, ensure_ascii=False)
    adapter.count_input_tokens(request, counter=lambda *_: 100)
    _, wire, refs, _, _ = adapter._wire_request(request)
    sent = json.loads(wire)["untrusted_document_data"]
    assert sent["claim_source_refs"] == [
        {"quote": original["quote"], "page_num": original["page_num"]}
    ]
    assert all("source_id" not in candidate for candidate in sent["evidence_candidates"])
    assert len(refs) == 20 and not calls and probe.summary()["calls"] == 0


def test_openrouter_validates_compact_quote_wire_before_restoring_provenance(tmp_path, monkeypatch):
    from jsonschema import Draft202012Validator
    from proofops.adapters.local.openrouter import OpenRouterProbe

    adapter, _, _, request = configured(tmp_path, monkeypatch, QUOTE_V5_PROFILE)
    original = setup(tmp_path)["packet"].to_dict()["evidence_candidates"][0]["source_refs"][0]
    original.update(quote="회사A는 배출량 40% 감축", char_start=100, char_end=115)
    user = json.loads(request["user_json"])
    user["untrusted_document_data"]["evidence_candidates"] = [dict(source_refs=[original])]
    request["user_json"] = json.dumps(user)
    payload = dict(
        claim_id=request["claim_id"],
        packet_sha256=request["packet_sha256"],
        replicate_id=1,
        track="performance",
        safe_harbor_category=None,
        elements=[
            dict(
                element_id=f"P{index}",
                state="present" if index == 1 else "unknown",
                evidence_refs=[{"id": "e0", "quote": "40%"}] if index == 1 else [],
                normalized_value="40%" if index == 1 else None,
                credited_from=None,
                reason_code=None if index == 1 else "unresolved",
            )
            for index in range(1, 7)
        ],
        superlative_quote=None,
        warnings=[],
    )
    adapter._probe = OpenRouterProbe("test-key", tmp_path / "openrouter.sqlite3")

    def complete(*args, schema_json, **kwargs):
        Draft202012Validator(json.loads(schema_json)).validate(payload)
        return dict(
            content=json.dumps(payload),
            schema_valid=True,
            input_tokens=1,
            output_tokens=1,
            provider_request_id="fixture-provider",
        )

    monkeypatch.setattr(adapter._probe, "complete", complete)
    response = adapter.invoke(request)
    restored = json.loads(response.raw_response_json)["elements"][0]["evidence_refs"][0]
    assert restored == original | dict(quote="40%", char_start=109, char_end=112)


def test_compact_element_profile_has_distinct_cache_request_identity(tmp_path, monkeypatch):
    from proofops.adapters.cache.aws import CacheNamespace, cache_request

    adapter, _, _, request = configured(tmp_path, monkeypatch, "upstage-compact-source-quotes-v4")
    old = adapter._settings
    new = replace(old, model_profile=QUOTE_V5_PROFILE)
    namespace = CacheNamespace(request["tenant_id"], "consent", str(UUID(int=88)), "tagger")
    signatures = [
        cache_request(
            namespace=namespace,
            request_id=request["request_id"],
            temperature=0,
            model_id=settings.model_id,
            model_profile=settings.model_sha256,
            prompt_sha256=settings.prompt_sha256,
            schema_sha256=canonical_hash(settings.schema_json),
            packet_sha256=request["packet_sha256"],
            tools=[],
            max_tokens=settings.max_tokens,
            replicate_id=1,
            extraction_epoch=1,
        ).request_signature
        for settings in (old, new)
    ]
    assert old.model_sha256 != new.model_sha256
    assert signatures[0] != signatures[1]


def test_incomplete_receipt_blocks_new_paid_replica_after_restart(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    interrupted = tmp_path / "receipts" / str(UUID(int=123456))
    interrupted.mkdir()
    (interrupted / "request.json").write_text("{}")
    response = adapter.invoke(request)
    assert response.usage.status == "failed" and not calls
    assert probe.summary()["calls"] == 0


def test_busy_operation_does_not_dispatch(tmp_path, monkeypatch):
    import fcntl

    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    with (tmp_path / "receipts" / ".operation.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert adapter.invoke(request).usage.status == "failed"
    assert not calls and probe.summary()["calls"] == 0


def test_missing_platform_lock_never_dispatches_or_reserves(tmp_path, monkeypatch):
    import sys

    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    with pytest.raises(ModuleNotFoundError, match="fcntl"):
        adapter.invoke(request)
    assert not calls and probe.summary()["calls"] == 0
    assert not list((tmp_path / "receipts").iterdir())


def test_unfrozen_classification_is_rejected_before_paid_tagging(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    request["system_prompt"] = adapter._settings.rendered_system
    with pytest.raises(ValueError, match="CLASSIFICATION_REQUIRED"):
        adapter.invoke(request)
    assert not calls


def test_transport_transform_profile_is_part_of_cache_identity(tmp_path, monkeypatch):
    adapter, probe, _, _ = configured(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="BINDING_INVALID"):
        UpstageTaggingTransport(
            probe,
            tmp_path / "other-receipts",
            settings=replace(adapter._settings, model_profile="unpinned-wire-transform"),
            tenant_id=adapter._tenant,
            authorize=adapter._authorize,
        )
    assert probe.summary()["calls"] == 0


def test_revoked_authorization_blocks_before_ledger_or_counter(tmp_path, monkeypatch):
    from proofops.application.preflight import Preflight, PreflightBlocked

    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    adapter._authorize = lambda settings, request: Preflight(
        False, (), None, "2026-09-10T00:00:00Z"
    )
    with pytest.raises(PreflightBlocked, match="UPSTAGE_TAGGING_AUTHORIZATION_REQUIRED"):
        adapter.invoke(request)
    with pytest.raises(PreflightBlocked, match="UPSTAGE_TAGGING_AUTHORIZATION_REQUIRED"):
        adapter.count_input_tokens(request, counter=lambda a, b: pytest.fail("must not count"))
    assert calls == [] and probe.summary()["calls"] == 0
    assert not (tmp_path / "receipts" / request["request_id"]).exists()


def test_success_receipt_retains_dispatch_authorization(tmp_path, monkeypatch):
    adapter, _, _, request = configured(tmp_path, monkeypatch)
    expected = adapter._authorize(adapter._settings, request).to_dict()
    adapter.invoke(request)
    saved = json.loads((tmp_path / "receipts" / request["request_id"] / "request.json").read_text())
    assert saved["authorization"] == expected


def test_approval_expiry_between_count_and_invoke_blocks_spend(tmp_path, monkeypatch):
    from proofops.application.preflight import PreflightBlocked, check_local_upstage_tagger

    from tests.integration.test_upstage_tagger_preflight import configured as approvals

    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    args = approvals()
    args.pop("settings")
    args["binding"].update(
        model_id=adapter._settings.model_id,
        tagging_settings_sha256=canonical_hash(asdict(adapter._settings)),
    )
    adapter._authorize = lambda settings, request: check_local_upstage_tagger(
        settings=settings, **args
    )
    assert adapter.count_input_tokens(request, counter=lambda a, b: 17) == 17
    args["checked_at"] = args["binding"]["expires_at"]
    with pytest.raises(PreflightBlocked):
        adapter.invoke(request)
    assert calls == [] and probe.summary()["calls"] == 0


def test_extractor_only_preflight_cannot_authorize_tagger(tmp_path, monkeypatch):
    from proofops.application.preflight import PreflightBlocked, check_local_upstage_binding

    from tests.acceptance.test_preflight import AUTH, NOW
    from tests.integration.test_upstage_runtime import profiles

    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    binding, consent = profiles()
    ready = check_local_upstage_binding(
        binding=binding, consent=consent, auth=AUTH, checked_at=NOW, source_sha256="a" * 64
    )
    assert ready.ready
    adapter._authorize = lambda settings, request: ready
    with pytest.raises(PreflightBlocked):
        adapter.invoke(request)
    assert calls == [] and probe.summary()["calls"] == 0


def test_partial_approval_result_cannot_authorize_transport(tmp_path, monkeypatch):
    from proofops.application.preflight import Check, Preflight, PreflightBlocked

    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    adapter._authorize = lambda *args: Preflight(
        True,
        (Check("tagging_settings", "pass", ""), Check("selected_document_rights", "pass", "")),
        "a" * 64,
        "2026-09-09T00:00:00Z",
    )
    with pytest.raises(PreflightBlocked):
        adapter.invoke(request)
    assert calls == [] and probe.summary()["calls"] == 0


def test_composition_can_reject_a_packet_outside_authorized_source(tmp_path, monkeypatch):
    from proofops.application.preflight import PreflightBlocked

    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    approved_packet = request["packet_sha256"]
    previous = adapter._authorize

    def authorize(settings, actual_request):
        if actual_request["packet_sha256"] != approved_packet:
            raise PreflightBlocked("PACKET_SCOPE_MISMATCH")
        return previous(settings, actual_request)

    adapter._authorize = authorize
    changed = request | dict(packet_sha256="b" * 64)
    with pytest.raises(PreflightBlocked, match="PACKET_SCOPE_MISMATCH"):
        adapter.invoke(changed)
    assert calls == [] and probe.summary()["calls"] == 0


@pytest.mark.parametrize(
    "profile", ["upstage-compact-coverage-unicode-v2", "upstage-compact-source-quotes-v3"]
)
def test_coverage_summary_preserves_unknown_and_original_request(tmp_path, monkeypatch, profile):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch, profile)
    user = json.loads(request["user_json"])
    coverage = dict(
        not_found_state="unknown",
        omitted_source_ids=[],
        unprocessed_source_ids=[str(UUID(int=i + 1)) for i in range(400)],
    )
    user["untrusted_document_data"]["search_coverage"] = coverage
    request["user_json"] = json.dumps(user)
    before = request["user_json"]
    system, wire, _, _, _ = adapter._wire_request(request)
    summary = json.loads(wire)["untrusted_document_data"]["search_coverage"]
    assert summary["not_found_state"] == "unknown"
    assert summary["unprocessed_source_count"] == 400
    assert summary["unprocessed_source_ids_sha256"] == canonical_hash(
        coverage["unprocessed_source_ids"]
    )
    assert "unprocessed_source_ids" not in summary
    assert request["user_json"] == before
    assert len(wire.encode()) < 2000
    assert not calls


def test_oversized_wire_rejected_during_count_before_receipt_or_reservation(tmp_path, monkeypatch):
    adapter, probe, calls, request = configured(tmp_path, monkeypatch)
    user = json.loads(request["user_json"])
    user["untrusted_document_data"]["atomic_quote"] = "가" * 10000
    request["user_json"] = json.dumps(user)
    with pytest.raises(ValueError, match="PROBE_REQUEST_TOO_LARGE"):
        adapter.count_input_tokens(request, counter=lambda *_: 1)
    assert not calls
    assert not list((tmp_path / "receipts").iterdir())


@pytest.mark.parametrize(
    "selection,quote,start,end",
    [
        ({"id": "e0", "quote": "40%"}, "40%", 109, 112),
        ({"id": "e0", "quote": "회사A는 배출량 40% 감축"}, "회사A는 배출량 40% 감축", 100, 115),
        ({"id": "e0", "quote": "감소"}, None, None, None),
        ({"id": "e0", "quote": ""}, None, None, None),
        ({"id": "e9", "quote": "40%"}, None, None, None),
        ({"id": "e0", "quote": "40%", "char_start": 0}, None, None, None),
        ("e0", None, None, None),
    ],
)
def test_quote_profile_restores_only_unique_literal_subspans(
    tmp_path, monkeypatch, selection, quote, start, end
):
    adapter, probe, calls, request = configured(
        tmp_path, monkeypatch, "upstage-compact-source-quotes-v3"
    )
    original = setup(tmp_path)["packet"].to_dict()["evidence_candidates"][0]["source_refs"][0]
    original.update(quote="회사A는 배출량 40% 감축", char_start=100, char_end=115)
    user = json.loads(request["user_json"])
    user["untrusted_document_data"]["evidence_candidates"] = [dict(source_refs=[original])]
    request["user_json"] = json.dumps(user)
    before = request["user_json"]

    def post(body):
        calls.append(body)
        return dict(
            id="quote-provider",
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[
                dict(
                    finish_reason="stop",
                    message=dict(
                        content=json.dumps({"elements": [{"evidence_refs": [selection]}]})
                    ),
                )
            ],
        )

    monkeypatch.setattr(probe, "_post", post)
    response = adapter.invoke(request)
    if quote is None:
        assert response.raw_response_json is None
        assert (
            json.loads(response.provider_response_json)["validation_error"]
            == "TAGGING_EVIDENCE_ID_INVALID"
        )
    else:
        restored = json.loads(response.raw_response_json)["elements"][0]["evidence_refs"][0]
        assert restored == original | dict(quote=quote, char_start=start, char_end=end)
    assert request["user_json"] == before
    assert probe.summary()["calls"] == 1 and probe.summary()["unsettled_calls"] == 0
    assert len(calls) == 1


def test_quote_profile_rejects_overlapping_ambiguous_quote(tmp_path, monkeypatch):
    adapter, _, _, request = configured(tmp_path, monkeypatch, "upstage-compact-source-quotes-v3")
    original = setup(tmp_path)["packet"].to_dict()["evidence_candidates"][0]["source_refs"][0]
    original.update(quote="aaa", char_start=10, char_end=13)
    with pytest.raises(ValueError):
        adapter._restore_ref({"id": "e0", "quote": "aa"}, {"e0": original})


@pytest.mark.parametrize("supply_roles,expected", [(True, "present"), (False, "present")])
def test_quote_profile_passes_literal_value_guard_but_never_bypasses_binding(
    tmp_path, monkeypatch, supply_roles, expected
):
    from tests.acceptance.test_tagging import execute

    adapter, probe, calls, _ = configured(tmp_path, monkeypatch, "upstage-compact-source-quotes-v3")
    inputs = setup(tmp_path)
    responder = inputs["invoke"]
    inputs.update(settings=adapter._settings, invoke=adapter.invoke, pricing=None)
    if not supply_roles:
        inputs["relation_tags"] = {}
    inputs["count_input_tokens"] = lambda request: adapter.count_input_tokens(
        request, counter=lambda *_: 20
    )

    def post(body):
        calls.append(body)
        user = json.loads(body["messages"][1]["content"])
        payload = json.loads(responder(user).raw_response_json)
        payload["elements"][0]["evidence_refs"] = [{"id": "e0", "quote": "40%"}]
        return dict(
            id="quote-" + str(user["replicate_id"]),
            model=MODEL_PRO4,
            usage=dict(prompt_tokens=20, completion_tokens=10),
            choices=[dict(finish_reason="stop", message=dict(content=json.dumps(payload)))],
        )

    monkeypatch.setattr(probe, "_post", post)
    runs = execute(inputs)
    assert len(runs) == 3 and all(r.guarded.elements[0].state == expected for r in runs)
    assert all(r.guarded.elements[0].evidence_refs[0].quote == "40%" for r in runs)
    assert probe.summary()["calls"] == 3 and probe.summary()["unsettled_calls"] == 0
    recovered = execute(inputs)
    assert all(r.recovered for r in recovered) and len(calls) == 3
