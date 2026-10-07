"""Real source-bound Upstage extraction: fake transport, no network, no approvals."""

import json

import pytest
from proofops.application.claims import ExtractionOutputError, ExtractionProfile
from proofops.domain.provenance import canonical_hash
from proofops_agent.upstage_extraction import SYSTEM_PROMPT, UpstageClaimExtractor

TEXT = "회사는 2030년까지 배출량을 20% 줄이기로 했다. 일반 산업 설명이다."


def test_overlapping_quote_occurrences_are_ambiguous(tmp_path):
    probe = FakeProbe(json.dumps({"claims": ["가가"]}))
    extractor, data = make_extractor(probe, tmp_path)
    data["untrusted_document_data"]["text"] = "가가가"
    with pytest.raises(ExtractionOutputError, match="MODEL_SPAN_OR_SCHEMA_INVALID"):
        extractor.extract(data)


def test_invalid_source_hash_rejected_before_call(tmp_path):
    probe = FakeProbe('{"claims": []}')
    extractor, data = make_extractor(probe, tmp_path)
    data["source_sha256"] = "not-a-hash"
    with pytest.raises(ValueError, match="EXTRACTION_PACKET_IDENTITY_INVALID"):
        extractor.extract(data)
    assert not probe.calls


def packet(text=TEXT):
    return {
        "tenant_id": "b490d4e4-0192-426c-9dff-c6c7b8c498d3",
        "document_version_id": "35d03dcb-c9d0-40d6-a3d1-8f9dc7322ee1",
        "parse_manifest_id": "62919374-bd2d-4273-85b4-7a1f793b8c14",
        "source_sha256": "c6395dd2be7948d85fa2b52c6edb61367fa6610c6f389c2478b44cb4cfcb5bde",
        "extraction_profile": None,  # replaced per-extractor below
        "untrusted_document_data": {
            "source_id": "11111111-2222-4333-8444-555555555555",
            "page_num": 25,
            "kind": "paragraph",
            "text": text,
        },
    }


class FakeProbe:
    """Caller-owned transport double; never touches the network."""

    def __init__(self, content=None, error=None):
        self.content = content
        self.error = error
        self.calls = []

    def complete(self, system, user_json, *, request_id, max_tokens=1024, json_mode=False):
        self.calls.append(
            {
                "system": system,
                "user_json": user_json,
                "request_id": request_id,
                "max_tokens": max_tokens,
                "json_mode": json_mode,
            }
        )
        if self.error is not None:
            raise self.error
        return {
            "model": "solar-pro3",
            "provider_request_id": "fake-provider-id",
            "provider_model": "solar-pro3-260323",
            "input_tokens": 50,
            "output_tokens": 10,
            "price_snapshot": {"fake": True},
            "cost_with_vat_reserve_usd": "0.0000099",
            "response_sha256": canonical_hash(self.content),
            "content": self.content,
        }


def make_extractor(probe, tmp_path):
    extractor = UpstageClaimExtractor(probe, tmp_path / "receipts")
    data = packet()
    data["extraction_profile"] = json.loads(
        json.dumps(
            {
                "model_sha256": extractor.profile.model_sha256,
                "prompt_sha256": extractor.profile.prompt_sha256,
                "rule_sha256": extractor.profile.rule_sha256,
                "synthetic": extractor.profile.synthetic,
                "replicate_id": extractor.profile.replicate_id,
                "extraction_epoch": extractor.profile.extraction_epoch,
            }
        )
    )
    return extractor, data


def make_context_extractor(probe, tmp_path, **flags):
    """Context-profile extractor + matching packet (R03f contract).

    The packet carries the extractor's own context profile, so identity and
    replay assertions exercise the same pinned hashes the pilot freezes.
    """
    extractor = UpstageClaimExtractor(
        probe, tmp_path / "receipts", extraction_context=True, **flags
    )
    data = packet()
    data["extraction_profile"] = json.loads(
        json.dumps(
            {
                "model_sha256": extractor.profile.model_sha256,
                "prompt_sha256": extractor.profile.prompt_sha256,
                "rule_sha256": extractor.profile.rule_sha256,
                "synthetic": extractor.profile.synthetic,
                "replicate_id": extractor.profile.replicate_id,
                "extraction_epoch": extractor.profile.extraction_epoch,
            }
        )
    )
    return extractor, data


def test_profile_is_pinned_and_non_synthetic(tmp_path):
    first = UpstageClaimExtractor(FakeProbe('{"claims": []}'), tmp_path / "a")
    second = UpstageClaimExtractor(FakeProbe('{"claims": []}'), tmp_path / "b")
    assert isinstance(first.profile, ExtractionProfile)
    assert first.profile.synthetic is False
    assert (first.profile.model_sha256, first.profile.prompt_sha256, first.profile.rule_sha256) == (
        second.profile.model_sha256,
        second.profile.prompt_sha256,
        second.profile.rule_sha256,
    )
    assert first.profile.prompt_sha256 == canonical_hash(SYSTEM_PROMPT)


def test_valid_quotes_resolve_to_source_spans_with_receipts(tmp_path):
    quote = "배출량을 20% 줄이기로 했다"
    probe = FakeProbe(json.dumps({"claims": [quote]}, ensure_ascii=False))
    extractor, data = make_extractor(probe, tmp_path)
    result = extractor.extract(data)
    start = TEXT.index(quote)
    assert result == {
        "spans": [
            {
                "char_start": start,
                "char_end": start + len(quote),
                "quote": quote,
                "kind": "claim",
                "reason": None,
                "topic_ids": ["environment"],
            }
        ]
    }
    assert set(probe.calls[0]) == {"system", "user_json", "request_id", "max_tokens", "json_mode"}
    assert probe.calls[0]["system"] == SYSTEM_PROMPT
    assert "회사는" in probe.calls[0]["user_json"]  # model receives Korean, not escapes
    sent = json.loads(probe.calls[0]["user_json"])
    assert sent["tenant_id"] == data["tenant_id"]
    assert sent["document_version_id"] == data["document_version_id"]
    assert sent["parse_manifest_id"] == data["parse_manifest_id"]
    assert (
        sent["untrusted_document_data"]["source_id"] == data["untrusted_document_data"]["source_id"]
    )
    assert sent["untrusted_document_data"]["text"] == TEXT
    receipt = tmp_path / "receipts" / probe.calls[0]["request_id"]
    for name in ("packet.json", "request.json", "raw_response.json", "result.json"):
        assert (receipt / name).is_file()
    stored = json.loads((receipt / "raw_response.json").read_text())
    assert stored["provider_model"] == "solar-pro3-260323"
    assert stored["content"] == probe.content
    record = json.loads((receipt / "result.json").read_text())
    assert record["packet_sha256"] == canonical_hash(data)
    assert record["request_id"] == probe.calls[0]["request_id"]
    assert record["profile"]["synthetic"] is False
    assert "grade" not in json.dumps(result) and "label" not in json.dumps(result)


def test_request_id_is_derived_and_unique_per_packet(tmp_path):
    probe = FakeProbe(json.dumps({"claims": []}))
    first, data = make_extractor(probe, tmp_path)
    first.extract(data)
    other_probe = FakeProbe(json.dumps({"claims": []}))
    other = UpstageClaimExtractor(other_probe, tmp_path / "other")
    data["extraction_profile"] = {
        "model_sha256": other.profile.model_sha256,
        "prompt_sha256": other.profile.prompt_sha256,
        "rule_sha256": other.profile.rule_sha256,
        "synthetic": other.profile.synthetic,
        "replicate_id": other.profile.replicate_id,
        "extraction_epoch": other.profile.extraction_epoch,
    }
    other.extract(data)
    assert probe.calls[0]["request_id"] == other_probe.calls[0]["request_id"]
    assert 1 <= len(probe.calls[0]["request_id"]) <= 128


def test_duplicate_packet_refuses_to_overwrite_receipts(tmp_path):
    """Legitimate replay contract: identical bytes replay without a new paid
    call and leave every stored file untouched; tampered raw content cannot
    replay the stored result."""
    from proofops.domain.rulepacks import canonical_json

    probe = FakeProbe(json.dumps({"claims": []}))
    extractor, data = make_extractor(probe, tmp_path)
    first = extractor.extract(data)
    receipt = tmp_path / "receipts" / probe.calls[0]["request_id"]
    before = {
        name: (receipt / name).read_bytes()
        for name in ("packet.json", "raw_response.json", "request.json", "result.json")
    }
    second = extractor.extract(data)
    assert second == first == {"spans": []}
    assert len(probe.calls) == 1
    for name, content in before.items():
        assert (receipt / name).read_bytes() == content
    # A tampered raw response reconstructs different spans, so the stored
    # result is refused rather than served.
    raw_path = receipt / "raw_response.json"
    raw_path.chmod(0o600)
    raw = json.loads(raw_path.read_text())
    raw["content"] = json.dumps({"claims": ["배출량을 20% 줄이기로 했다"]}, ensure_ascii=False)
    raw_path.write_text(canonical_json(raw))
    with pytest.raises(ValueError, match="EXTRACTION_RECEIPT_EXISTS"):
        extractor.extract(data)
    assert len(probe.calls) == 1


@pytest.mark.parametrize(
    "content",
    [
        json.dumps({"claims": ["missing quote"]}, ensure_ascii=False),
        json.dumps({"claims": ["다."]}, ensure_ascii=False),  # ambiguous: twice in TEXT
        json.dumps(
            {"claims": ["배출량을 20% 줄이기로 했다", "20% 줄이기로 했다"]}, ensure_ascii=False
        ),
        json.dumps({"claims": [""]}, ensure_ascii=False),
        json.dumps({"claims": ["배출량을 20% 줄이기로 했다"], "grade": "E3"}, ensure_ascii=False),
        "not json at all",
    ],
)
def test_raw_invalid_responses_retained_with_sanitized_error(tmp_path, content):
    probe = FakeProbe(content)
    extractor, data = make_extractor(probe, tmp_path)
    with pytest.raises(ExtractionOutputError, match="MODEL_SPAN_OR_SCHEMA_INVALID") as error:
        extractor.extract(data)
    assert content not in str(error.value)
    receipt = tmp_path / "receipts" / probe.calls[0]["request_id"]
    assert (receipt / "packet.json").is_file()
    assert (receipt / "request.json").is_file()
    assert json.loads((receipt / "raw_response.json").read_text())["content"] == content
    failure = json.loads((receipt / "failure.json").read_text())
    assert failure["error"] == "MODEL_SPAN_OR_SCHEMA_INVALID"
    assert failure["packet_sha256"] == canonical_hash(data)


def test_transport_failure_is_sanitized_retryable_and_recorded(tmp_path):
    probe = FakeProbe(error=OSError("boom secret-credential"))
    extractor, data = make_extractor(probe, tmp_path)
    with pytest.raises(ValueError, match="UPSTREAM_UNAVAILABLE"):
        extractor.extract(data)
    receipt = tmp_path / "receipts" / probe.calls[0]["request_id"]
    assert (receipt / "packet.json").is_file()
    assert (receipt / "request.json").is_file()
    failure = json.loads((receipt / "failure.json").read_text())
    assert failure["error"] == "UPSTREAM_UNAVAILABLE"
    assert "boom" not in json.dumps(failure) and "secret" not in json.dumps(failure)


def test_packet_identity_is_required(tmp_path):
    extractor, data = make_extractor(FakeProbe(json.dumps({"claims": []})), tmp_path)
    del data["parse_manifest_id"]
    with pytest.raises(ValueError, match="EXTRACTION_PACKET_IDENTITY_INVALID"):
        extractor.extract(data)


def test_no_source_quality_or_grade_is_ever_approved(tmp_path):
    quote = "배출량을 20% 줄이기로 했다"
    probe = FakeProbe(json.dumps({"claims": [quote]}, ensure_ascii=False))
    extractor, data = make_extractor(probe, tmp_path)
    result = extractor.extract(data)
    assert "source_quality" not in json.dumps(result)
    record_text = (tmp_path / "receipts" / probe.calls[0]["request_id"] / "result.json").read_text()
    assert "source_quality" not in record_text


@pytest.mark.parametrize(
    "code",
    [
        "BUDGET_EXHAUSTED",
        "BUDGET_SETTLEMENT_INVALID",
        "PRICE_RECHECK_REQUIRED",
        "PROBE_REQUEST_TOO_LARGE",
        "UPSTAGE_REQUEST_FAILED",
        "UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED",
        "UPSTREAM_UNAVAILABLE",
        "UPSTAGE_HTTP_400",
        "UPSTAGE_HTTP_429",
        "UPSTAGE_HTTP_500",
        "UPSTAGE_HTTP_503",
    ],
)
def test_budget_and_provider_stop_are_not_transient(tmp_path, code):
    extractor, data = make_extractor(FakeProbe(error=ValueError(code)), tmp_path)
    with pytest.raises(ValueError, match=code):
        extractor.extract(data)
    if code not in {"UPSTAGE_REQUEST_FAILED", "UPSTREAM_UNAVAILABLE"}:
        with pytest.raises(ValueError, match=code):
            extractor.extract(data)


def test_confirmed_truncation_is_failed_without_rebilling_or_releasing_reservation(
    tmp_path, monkeypatch
):
    from decimal import Decimal

    from proofops.adapters.local.upstage import UpstageProbe

    probe = UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    calls = []

    def truncated(body):
        calls.append(body)
        return {
            "id": "truncated-provider-response",
            "model": "solar-pro3",
            "usage": {"prompt_tokens": 100, "completion_tokens": body["max_tokens"]},
            "choices": [{"finish_reason": "length", "message": {"content": '{"claims":['}}],
        }

    monkeypatch.setattr(probe, "_post", truncated)
    extractor, data = make_extractor(probe, tmp_path)
    for _ in range(2):
        with pytest.raises(ExtractionOutputError):
            extractor.extract(data)
    assert len(calls) == 1
    assert probe.summary()["unsettled_calls"] == 1
    assert Decimal(probe.summary()["committed_usd"]) == 1
    directory = next((tmp_path / "receipts").iterdir())
    failure_bytes = (directory / "failure.json").read_bytes()
    request = json.loads((directory / "request.json").read_text())
    request_id = request["request_id"]
    assert probe.is_recorded_output_truncation(request, request_id=request_id)
    assert not probe.is_recorded_output_truncation(
        dict(request, user_json=request["user_json"] + " "), request_id=request_id
    )
    saved_path = probe._responses / (canonical_hash(request_id) + ".json")
    saved = json.loads(saved_path.read_text())
    saved["provider_response"]["usage"]["prompt_tokens"] = True
    saved_path.write_text(json.dumps(saved))
    assert not probe.is_recorded_output_truncation(request, request_id=request_id)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        extractor.extract(data)
    assert (directory / "failure.json").read_bytes() == failure_bytes
    assert len(calls) == 1


def test_pro4_profile_is_explicit_and_rejects_pro3_packet_before_call(tmp_path):
    from dataclasses import asdict

    from proofops_agent.upstage_extraction import _profile

    probe = FakeProbe('{"claims": []}')
    probe.model = "solar-pro4"
    extractor = UpstageClaimExtractor(probe, tmp_path / "pro4")
    assert extractor.profile == _profile("solar-pro4")
    data = packet()
    data["extraction_profile"] = asdict(_profile())
    with pytest.raises(ValueError, match="EXTRACTION_PACKET_IDENTITY_INVALID"):
        extractor.extract(data)
    assert not probe.calls
    data["extraction_profile"] = asdict(extractor.profile)
    assert extractor.extract(data) == {"spans": []}


@pytest.mark.parametrize("model", ["solar-pro99", None, []])
def test_unsupported_extraction_model_rejected_before_receipts(tmp_path, model):
    probe = FakeProbe('{"claims": []}')
    probe.model = model
    with pytest.raises(ValueError, match="UPSTAGE_MODEL_MISMATCH"):
        UpstageClaimExtractor(probe, tmp_path / "unsupported")
    assert not (tmp_path / "unsupported").exists()


def test_new_profile_rejects_cut_quotation_without_changing_legacy_span_replay(tmp_path):
    from proofops.application.claims import validate_extraction_response

    text = "두산밥캣은 KPI에 ‘2030 온실가스 감축 목표 달성을 위한 지역별"
    old_response = {
        "spans": [
            dict(
                char_start=0,
                char_end=len(text),
                quote=text,
                kind="claim",
                reason=None,
                topic_ids=["environment"],
            )
        ]
    }
    assert validate_extraction_response(old_response, text)[0].quote == text
    probe = FakeProbe(json.dumps({"claims": [text]}, ensure_ascii=False))
    extractor, data = make_extractor(probe, tmp_path)
    data["untrusted_document_data"]["text"] = text
    with pytest.raises(ExtractionOutputError, match="MODEL_SPAN_OR_SCHEMA_INVALID"):
        extractor.extract(data)
    assert len(list((tmp_path / "receipts").glob("*/raw_response.json"))) == 1


def test_invalid_quote_does_not_discard_exact_sibling_or_hide_uncovered_text(tmp_path):
    from proofops.application.claims import ClaimScope, discover_atomic_claims

    from tests.acceptance.test_claims import MANIFEST, TENANT, VERSION, graph_of

    text = "회사는 소재 기준을 정의했습니다. 새로운 소재가 환경영향을 줄일 것입니다."
    valid = "회사는 소재 기준을 정의했습니다."
    rewritten = "신소재는 친환경적입니다."
    extractor = UpstageClaimExtractor(
        FakeProbe(json.dumps({"claims": [rewritten, valid]}, ensure_ascii=False)),
        tmp_path / "receipts",
    )
    result = discover_atomic_claims(
        graph_of(text), ClaimScope(TENANT, VERSION, MANIFEST), extractor=extractor
    )
    assert [c.quote for c in result.claims] == [valid]
    assert any(e.state == "unknown" for e in result.exclusions)
    receipt = json.loads(next((tmp_path / "receipts").glob("*/result.json")).read_text())
    assert receipt["rejected_quote_indices"] == [0]


@pytest.mark.parametrize("expired", [False, True])
def test_duplicate_receipt_root_cannot_count_historical_spend(tmp_path, monkeypatch, expired):
    from datetime import UTC, datetime

    from proofops.adapters.local import upstage

    from tests.integration.test_upstage_probe import response

    now = [datetime(2026, 9, 18, tzinfo=UTC)]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0]

    monkeypatch.setattr(upstage, "datetime", Clock)
    probe = upstage.UpstageProbe("offline-key", tmp_path / "budget.sqlite3")
    raw = response()
    raw["choices"][0]["message"]["content"] = '{"claims": []}'
    calls = []
    monkeypatch.setattr(probe, "_post", lambda body: calls.append(body) or raw)
    first, data = make_extractor(probe, tmp_path / "first")
    first.extract(data)
    if expired:
        now[0] = datetime(2026, 10, 14, tzinfo=UTC)
    second, _ = make_extractor(probe, tmp_path / "second")
    code = "PRICE_RECHECK_REQUIRED" if expired else "DUPLICATE_PROBE_REQUEST"
    with pytest.raises(ValueError, match=code):
        second.extract(data)
    assert len(calls) == 1
    assert second.usage["model_calls"] == 0
    assert second.usage["committed_or_reserved_usd"] == "0"


# --- R03b: opt-in extraction context_graph (never widens the frozen packet) ---


def _extraction_context_graph(
    neighbor_text="본 절은 관련 성과를 요약한다.",
    *,
    text=TEXT,
    tenant="b490d4e4-0192-426c-9dff-c6c7b8c498d3",
    version="35d03dcb-c9d0-40d6-a3d1-8f9dc7322ee1",
    manifest="62919374-bd2d-4273-85b4-7a1f793b8c14",
    source_sha="a" * 64,
    page=25,
    with_neighbors=True,
):
    """A real body/heading/neighbor graph whose 'body' block matches ``packet()``'s
    default source_id/text, so a real ``context_graph=`` extract() call finds it.

    Keyword overrides build the same shape around another focal (text/identity/
    page) or drop heading+neighbor for the explicit-empty case; defaults keep
    every existing caller byte-identical.
    """
    from dataclasses import replace

    from proofops.application.ingest.graph_fusion import (
        CandidateBatch,
        CandidateBlock,
        CanonicalEdge,
        fuse_candidates,
    )
    from proofops.domain.documents import NativeSource, PageGeometry

    run = "6e6a5d3a-7a8b-4a8a-9a8a-6e6a5d3a7a8b"
    heading_text = "환경 성과"

    def block(kind, native_id, page, bbox, text):
        return CandidateBlock(
            kind,
            NativeSource(
                version,
                manifest,
                run,
                native_id,
                page,
                None,
                bbox,
                "pdf_bottom_left_points",
                text,
                0,
                len(text),
            ),
            PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        )

    candidates = [
        block("paragraph", "packet-source", page, (10, 700, 500, 730), text),
    ]
    if with_neighbors:
        candidates += [
            block("heading", "heading", page, (10, 750, 500, 780), heading_text),
            block("paragraph", "neighbor", page, (10, 650, 500, 680), neighbor_text),
        ]
    batch = CandidateBatch(
        tenant,
        version,
        manifest,
        source_sha,
        run,
        "synthetic-text",
        "1",
        "synthetic",
        "b" * 64,
        tuple(candidates),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=tenant)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))

    def find(native_id):
        return next(b for b in graph.blocks if b.sources[0].source_native_id == native_id)

    body = find("packet-source")
    if with_neighbors:
        heading = find("heading")
        graph = replace(
            graph, edges=(CanonicalEdge(body.source_id, heading.source_id, "section_parent"),)
        )
    return graph, body.source_id


def test_omitting_context_graph_reproduces_legacy_receipts_byte_for_byte(tmp_path):
    probe = FakeProbe('{"claims": []}')
    extractor, data = make_extractor(probe, tmp_path)
    default_result = extractor.extract(data)
    call = probe.calls[0]
    assert call["system"] == SYSTEM_PROMPT
    assert "context_blocks" not in call["user_json"]
    assert default_result == {"spans": []}


def test_context_graph_attaches_full_provenance_and_never_widens_claim_spans(tmp_path):
    from proofops_agent.upstage_extraction import CONTEXT_SYSTEM_SUFFIX

    graph, source_id = _extraction_context_graph()
    probe = FakeProbe(json.dumps({"claims": []}, ensure_ascii=False))
    extractor, data = make_context_extractor(probe, tmp_path)
    data["untrusted_document_data"]["source_id"] = source_id
    data["source_sha256"] = "a" * 64
    extractor.extract(data, context_graph=graph)
    assert len(probe.calls) == 1
    call = probe.calls[0]
    assert call["system"] == SYSTEM_PROMPT + CONTEXT_SYSTEM_SUFFIX
    sent = json.loads(call["user_json"])
    data_sent = sent["untrusted_document_data"]
    assert data_sent["text"] == TEXT  # the extractable text is exactly the packet's own text
    roles = {b["role"] for b in data_sent["context_blocks"]}
    assert roles == {"heading", "nearby"}
    for block in data_sent["context_blocks"]:
        assert block["source_id"] and block["page_num"] >= 1
        assert block["quality"] in ("verified", "unverified")
        assert isinstance(block["source_ref"], dict)


def test_context_wire_changes_receipt_identity_but_not_packet_bytes(tmp_path):
    """R03f contract: the same packet under different wire content (context vs
    none) addresses a DIFFERENT receipt, while the frozen packet bytes stay
    identical. The old same-receipt-id expectation described the experimental
    hazard (one identity, two wire contents) and is replaced by this."""
    graph, source_id = _extraction_context_graph()
    probe_a = FakeProbe('{"claims": []}')
    extractor_a, data_a = make_extractor(probe_a, tmp_path / "a")
    data_a["untrusted_document_data"]["source_id"] = source_id
    extractor_a.extract(data_a)
    receipt_a = next((tmp_path / "a" / "receipts").glob("*")).name

    probe_b = FakeProbe('{"claims": []}')
    extractor_b, data_b = make_context_extractor(probe_b, tmp_path / "b")
    data_b["untrusted_document_data"]["source_id"] = source_id
    data_b["source_sha256"] = "a" * 64
    extractor_b.extract(data_b, context_graph=graph)
    receipt_b = next((tmp_path / "b" / "receipts").glob("*")).name
    assert receipt_a != receipt_b  # context-bound wire content changes identity
    packet_a = (tmp_path / "a" / "receipts" / receipt_a / "packet.json").read_text()
    packet_b = (tmp_path / "b" / "receipts" / receipt_b / "packet.json").read_text()
    assert (
        json.loads(packet_a)["untrusted_document_data"]
        == json.loads(packet_b)["untrusted_document_data"]
    )


def test_returned_claim_spans_can_never_resolve_from_context_text():
    """A model returning a quote that exists ONLY in the heading/neighbor text
    (never in the packet's own text) is rejected exactly as an absent/ambiguous
    quote always was -- context is never searched for span location."""
    from proofops.application.claims import ExtractionOutputError

    graph, source_id = _extraction_context_graph()
    probe = FakeProbe(json.dumps({"claims": ["성과"]}, ensure_ascii=False))
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        extractor, data = make_context_extractor(probe, Path(tmp))
        data["untrusted_document_data"]["source_id"] = source_id
        data["source_sha256"] = "a" * 64
        with pytest.raises(ExtractionOutputError, match="MODEL_SPAN_OR_SCHEMA_INVALID"):
            extractor.extract(data, context_graph=graph)


def test_context_graph_with_unknown_source_id_fails_closed_without_a_call(tmp_path):
    """R03g contract: a same-identity graph that lacks the packet's focal block
    fails closed — no silent empty context, no receipt, no provider call."""
    graph, _ = _extraction_context_graph()
    probe = FakeProbe(json.dumps({"claims": []}, ensure_ascii=False))
    extractor, data = make_context_extractor(probe, tmp_path)
    data["untrusted_document_data"]["source_id"] = "aaaaaaaa-1111-4111-8111-111111111111"
    data["source_sha256"] = "a" * 64
    with pytest.raises(ValueError, match="EXTRACTION_CONTEXT_FOCAL_INVALID"):
        extractor.extract(data, context_graph=graph)
    assert probe.calls == []
    assert list((tmp_path / "receipts").iterdir()) == []


def test_context_focal_text_kind_page_mismatch_fails_closed(tmp_path):
    """Focal text, kind, or page differing from the packet fails closed, even
    when the graph identity itself matches. Zero provider calls throughout."""
    graph, source_id = _extraction_context_graph()
    probe = FakeProbe(json.dumps({"claims": []}, ensure_ascii=False))
    extractor, data = make_context_extractor(probe, tmp_path)
    data["untrusted_document_data"]["source_id"] = source_id
    data["source_sha256"] = "a" * 64
    import copy

    for field in ("text", "kind", "page"):
        variant = copy.deepcopy(data)
        if field == "text":
            variant["untrusted_document_data"]["text"] = TEXT + " 달라진 문장."
        elif field == "kind":
            variant["untrusted_document_data"]["kind"] = "heading"
        else:
            variant["untrusted_document_data"]["page_num"] = 99
        with pytest.raises(ValueError, match="EXTRACTION_CONTEXT_FOCAL_INVALID"):
            extractor.extract(variant, context_graph=graph)
    assert probe.calls == []


def test_abbreviated_year_notation_opt_in_accepts_actual_lotte_response(tmp_path):
    """R03d: the observed ‘24 년도 failure, fixed by versioned opt-in policy.

    Uses the ACTUAL stored Lotte p25 receipt fixture (packet text + provider
    content); the FakeProbe serves that stored content with no network, budget,
    or paid call. Legacy behaviour rejects all 5 exact model quotes as an
    unterminated paired quotation; the opt-in profile accepts the same 5 with
    literal text/offsets, replays them from the receipt without a second call,
    and still rejects genuinely truncated quotations and digit confounds.
    R03e tightens the mark to an explicit year noun and keeps closed
    ‘24년 ...’ pairs as ordinary quotations.
    """
    from pathlib import Path

    from proofops.domain.provenance import canonical_hash as _hash
    from proofops_agent.upstage_extraction import (
        _RULE_DESCRIPTOR_YEAR_NOTATION,
        _profile_with_year_notation,
        validate_extraction_response_with_year_notation,
    )

    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/pipeline/live-extraction/extraction-year-abbreviation"
    )
    text = json.loads((fixture / "packet.json").read_text())["untrusted_document_data"]["text"]
    content = json.loads((fixture / "raw_response.json").read_text())["content"]
    quotes = json.loads(content)["claims"]
    assert len(quotes) == 5
    assert all(text.count(quote) == 1 for quote in quotes)

    def packet_for(extractor, txt=text):
        data = packet(txt)
        data["extraction_profile"] = json.loads(
            json.dumps(
                {
                    "model_sha256": extractor.profile.model_sha256,
                    "prompt_sha256": extractor.profile.prompt_sha256,
                    "rule_sha256": extractor.profile.rule_sha256,
                    "synthetic": extractor.profile.synthetic,
                    "replicate_id": extractor.profile.replicate_id,
                    "extraction_epoch": extractor.profile.extraction_epoch,
                }
            )
        )
        return data

    # Legacy default: the ‘24 mark reads as an unterminated quotation, so every
    # exact quote is rejected and the failure receipt mirrors the stored one.
    legacy = UpstageClaimExtractor(FakeProbe(content), tmp_path / "legacy")
    with pytest.raises(ExtractionOutputError, match="MODEL_SPAN_OR_SCHEMA_INVALID"):
        legacy.extract(packet_for(legacy))
    stored_failure = json.loads(next((tmp_path / "legacy").glob("*/failure.json")).read_text())
    assert stored_failure["error"] == "MODEL_SPAN_OR_SCHEMA_INVALID"

    # Opt-in: same 5 quotes accepted with literal source text and offsets.
    probe = FakeProbe(content)
    extractor = UpstageClaimExtractor(probe, tmp_path / "receipts", extraction_year_notation=True)
    assert extractor.profile.rule_sha256 == _hash(_RULE_DESCRIPTOR_YEAR_NOTATION)
    assert extractor.profile.rule_sha256 != legacy.profile.rule_sha256
    assert (
        extractor.profile.model_sha256,
        extractor.profile.prompt_sha256,
    ) == (
        legacy.profile.model_sha256,
        legacy.profile.prompt_sha256,
    )
    assert extractor.profile == _profile_with_year_notation()
    result = extractor.extract(packet_for(extractor))
    assert [span["quote"] for span in result["spans"]] == quotes
    for span in result["spans"]:
        assert span["quote"] == text[span["char_start"] : span["char_end"]]
    # No normalization, no invented year facts: the literal ‘ mark survives and
    # no four-digit year appears that the source does not contain.
    assert "‘" in result["spans"][0]["quote"]
    assert all("2024" not in span["quote"] for span in result["spans"])
    # Receipt replay serves the identical spans without a second provider call:
    # live and replay used the same validator.
    replayed = extractor.extract(packet_for(extractor))
    assert replayed == result
    assert len(probe.calls) == 1
    assert "’" not in text  # the mark is genuinely unpaired in this source

    # Counterpart: a genuinely truncated paired quotation still fails closed,
    # through the same opt-in extractor path (no special-casing).
    truncated = "보고서는 “환경 목표를 달성했다고 밝혔다. 다음 문장이다."
    probe_truncated = FakeProbe(json.dumps({"claims": ["다음 문장이다."]}, ensure_ascii=False))
    extractor_truncated = UpstageClaimExtractor(
        probe_truncated, tmp_path / "truncated", extraction_year_notation=True
    )
    data_truncated = packet(truncated)
    data_truncated["extraction_profile"] = json.loads(
        json.dumps(
            {
                "model_sha256": extractor_truncated.profile.model_sha256,
                "prompt_sha256": extractor_truncated.profile.prompt_sha256,
                "rule_sha256": extractor_truncated.profile.rule_sha256,
                "synthetic": extractor_truncated.profile.synthetic,
                "replicate_id": extractor_truncated.profile.replicate_id,
                "extraction_epoch": extractor_truncated.profile.extraction_epoch,
            }
        )
    )
    with pytest.raises(ExtractionOutputError, match="MODEL_SPAN_OR_SCHEMA_INVALID"):
        extractor_truncated.extract(data_truncated)

    # Confounds are not blindly tolerated: ‘ + three digits keeps its unclosed
    # range, and a leading ’ (U+2019) is never an abbreviated-year mark, so a
    # claim inside its unsafe region still fails; ASCII ' was never paired.
    with pytest.raises(ExtractionOutputError, match="unclosed source quotation"):
        validate_extraction_response_with_year_notation(
            {
                "spans": [
                    dict(
                        char_start=0,
                        char_end=10,
                        quote="‘123456789",
                        kind="claim",
                        reason=None,
                        topic_ids=[],
                    )
                ]
            },
            "‘123456789 이후 문장이다.",
        )
    with pytest.raises(ExtractionOutputError, match="unclosed source quotation"):
        validate_extraction_response_with_year_notation(
            {
                "spans": [
                    dict(
                        char_start=0,
                        char_end=5,
                        quote="앞 ’24",
                        kind="claim",
                        reason=None,
                        topic_ids=[],
                    )
                ]
            },
            "앞 ’24 뒤 문장이다.",
        )
    # R03e: without an explicit year noun there is no exemption — bare ‘24,
    # counters (‘24개) and durations (‘24시간) still fail closed.
    for bare_text, bare_quote, bare_end in [
        ("‘24 이후 문장이다.", "‘24 이후", 6),
        ("‘24개 이후 문장이다.", "‘24개", 4),
        ("‘24시간 이후다.", "‘24시간", 5),
    ]:
        assert bare_text[:bare_end] == bare_quote
        with pytest.raises(ExtractionOutputError, match="unclosed source quotation"):
            validate_extraction_response_with_year_notation(
                {
                    "spans": [
                        dict(
                            char_start=0,
                            char_end=bare_end,
                            quote=bare_quote,
                            kind="claim",
                            reason=None,
                            topic_ids=[],
                        )
                    ]
                },
                bare_text,
            )

    # R03e positive: a genuinely CLOSED ‘24년 ...’ pair keeps quotation
    # semantics — no orphan closing. The claim overlaps the region an orphaned
    # closing would have poisoned, so only the corrected policy accepts it.
    closed_text = "회사는 ‘24년 감축 목표’ 달성을 선언했다."
    probe_closed = FakeProbe(json.dumps({"claims": [closed_text]}, ensure_ascii=False))
    extractor_closed = UpstageClaimExtractor(
        probe_closed, tmp_path / "closed", extraction_year_notation=True
    )
    result_closed = extractor_closed.extract(packet_for(extractor_closed, closed_text))
    assert [span["quote"] for span in result_closed["spans"]] == [closed_text]
    assert len(probe_closed.calls) == 1

    ascii_text = "회사는 '24 계획을 세웠다. 다음 문장이다."
    ascii_span = {
        "spans": [
            dict(
                char_start=0,
                char_end=16,
                quote="회사는 '24 계획을 세웠다.",
                kind="claim",
                reason=None,
                topic_ids=[],
            )
        ]
    }
    assert (
        validate_extraction_response_with_year_notation(ascii_span, ascii_text)[0].quote
        == "회사는 '24 계획을 세웠다."
    )


def test_context_mode_requires_a_matching_graph_before_any_call(tmp_path):
    """Cross-identity graphs and missing graphs fail closed with no receipt
    and no provider call; a legacy extractor handed a graph fails closed
    instead of silently changing its wire."""
    graph, source_id = _extraction_context_graph()
    probe = FakeProbe(json.dumps({"claims": []}))
    extractor, data = make_context_extractor(probe, tmp_path)
    data["untrusted_document_data"]["source_id"] = source_id
    # Default packet source_sha256 (c6395...) differs from the graph's (a*64).
    with pytest.raises(ValueError, match="EXTRACTION_CONTEXT_IDENTITY_INVALID"):
        extractor.extract(data, context_graph=graph)
    assert probe.calls == []
    assert list((tmp_path / "receipts").iterdir()) == []
    with pytest.raises(ValueError, match="EXTRACTION_CONTEXT_REQUIRED"):
        extractor.extract(data)
    assert probe.calls == []
    assert list((tmp_path / "receipts").iterdir()) == []
    legacy, legacy_data = make_extractor(FakeProbe('{"claims": []}'), tmp_path / "legacy")
    with pytest.raises(ValueError, match="EXTRACTION_CONTEXT_UNEXPECTED"):
        legacy.extract(legacy_data, context_graph=graph)


def test_changed_context_cannot_replay_prior_receipt(tmp_path):
    """Same packet under changed context addresses a new receipt (a new paid
    call) and leaves the prior receipt byte-identical; replay still serves an
    unchanged context without a second call."""
    graph_a, source_id = _extraction_context_graph()
    graph_b, _ = _extraction_context_graph(neighbor_text="이웃 문단이 바뀌었다.")
    probe = FakeProbe(json.dumps({"claims": []}))
    extractor, data = make_context_extractor(probe, tmp_path)
    data["untrusted_document_data"]["source_id"] = source_id
    data["source_sha256"] = "a" * 64
    first = extractor.extract(data, context_graph=graph_a)
    receipt_a = tmp_path / "receipts" / probe.calls[0]["request_id"]
    before = {
        name: (receipt_a / name).read_bytes() for name in receipt_a.iterdir() if name.is_file()
    }
    before = {path.name: content for path, content in before.items()}
    second = extractor.extract(data, context_graph=graph_b)
    assert second == first
    assert len(probe.calls) == 2
    assert probe.calls[0]["request_id"] != probe.calls[1]["request_id"]
    for name, content in before.items():
        assert (receipt_a / name).read_bytes() == content
    replayed = extractor.extract(data, context_graph=graph_a)
    assert replayed == first
    assert len(probe.calls) == 2


def test_graph_bound_extractor_rejects_non_context_inner(tmp_path):
    from proofops_agent.upstage_extraction import GraphBoundExtractor

    graph, _ = _extraction_context_graph()
    legacy, _ = make_extractor(FakeProbe('{"claims": []}'), tmp_path)
    with pytest.raises(ValueError, match="UPSTAGE_CONTEXT_EXTRACTOR_REQUIRED"):
        GraphBoundExtractor(legacy, graph)
    inner, _ = make_context_extractor(FakeProbe('{"claims": []}'), tmp_path / "inner")
    bound = GraphBoundExtractor(inner, graph)
    assert bound.profile == inner.profile


def test_context_blocks_travel_the_real_extract_runner_path(tmp_path, monkeypatch):
    """The runner binds its own loaded graph: every provider call on the real
    run_once path carries pinned context keys, and the frozen snapshot keeps
    the context profile hash."""
    from tests.acceptance.test_upload import TENANT
    from tests.integration.test_real_extract_runner import FakeProbe as RunnerProbe
    from tests.integration.test_real_extract_runner import inject_graph, real_setup

    probe = RunnerProbe(json.dumps({"claims": []}))

    def factory(probe, receipts):
        return UpstageClaimExtractor(probe, receipts, extraction_context=True)

    service, run_id, runner, now, probe = real_setup(
        tmp_path, monkeypatch, limit=2, probe=probe, extractor_factory=factory
    )
    inject_graph(monkeypatch, service, run_id, ["paragraph"] * 3)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    assert probe.calls, "context extractor made no provider call"
    for call in probe.calls:
        sent = json.loads(call["user_json"])["untrusted_document_data"]
        assert sent["text"].startswith("paragraph block")
        assert "context_blocks" in sent and "omitted_source_ids" in sent
    snapshot = service.store.snapshot(TENANT, run_id)
    assert snapshot["extraction_profile"]["rule_sha256"] == runner.extractor.profile.rule_sha256


def test_year_and_context_combined_keep_actual_lotte_five_claims(tmp_path):
    """Year + context options compose: the actual stored Lotte response still
    resolves its honest 5 focal claims against a real lone-focal graph
    (explicit empty context, pinned)."""
    from pathlib import Path

    fixture = (
        Path(__file__).resolve().parents[2]
        / "tests/fixtures/pipeline/live-extraction/extraction-year-abbreviation"
    )
    stored = json.loads((fixture / "packet.json").read_text())
    text = stored["untrusted_document_data"]["text"]
    content = json.loads((fixture / "raw_response.json").read_text())["content"]
    quotes = json.loads(content)["claims"]
    assert len(quotes) == 5
    graph, source_id = _extraction_context_graph(
        text=text,
        tenant=stored["tenant_id"],
        version=stored["document_version_id"],
        manifest=stored["parse_manifest_id"],
        source_sha=stored["source_sha256"],
        page=stored["untrusted_document_data"]["page_num"],
        with_neighbors=False,
    )
    probe = FakeProbe(content)
    extractor = UpstageClaimExtractor(
        probe,
        tmp_path / "receipts",
        extraction_year_notation=True,
        extraction_context=True,
    )
    data = packet(text)
    data.update(
        tenant_id=stored["tenant_id"],
        document_version_id=stored["document_version_id"],
        parse_manifest_id=stored["parse_manifest_id"],
        source_sha256=stored["source_sha256"],
    )
    data["untrusted_document_data"]["source_id"] = source_id
    data["extraction_profile"] = json.loads(
        json.dumps(
            {
                "model_sha256": extractor.profile.model_sha256,
                "prompt_sha256": extractor.profile.prompt_sha256,
                "rule_sha256": extractor.profile.rule_sha256,
                "synthetic": extractor.profile.synthetic,
                "replicate_id": extractor.profile.replicate_id,
                "extraction_epoch": extractor.profile.extraction_epoch,
            }
        )
    )
    result = extractor.extract(data, context_graph=graph)
    assert [span["quote"] for span in result["spans"]] == quotes
    assert extractor.extract(data, context_graph=graph) == result
    assert len(probe.calls) == 1
    stored_request = json.loads(next((tmp_path / "receipts").glob("*/request.json")).read_text())
    sent = json.loads(stored_request["user_json"])["untrusted_document_data"]
    assert sent["context_blocks"] == [] and sent["omitted_source_ids"] == []


@pytest.mark.parametrize("code", ["UPSTAGE_REQUEST_FAILED", "UPSTREAM_UNAVAILABLE"])
def test_replayed_network_failure_holds_source_without_rebilling_or_stopping_next_source(
    tmp_path, code
):
    from proofops_worker.extract_runner import _ContinuingExtractor

    probe = FakeProbe(error=ValueError(code))
    extractor, first = make_extractor(probe, tmp_path)
    with pytest.raises(ValueError, match=code):
        extractor.extract(first)
    directory = tmp_path / "receipts" / probe.calls[0]["request_id"]
    failure = (directory / "failure.json").read_bytes()
    second = json.loads(json.dumps(first))
    second["untrusted_document_data"]["source_id"] = "22222222-2222-4333-8444-555555555555"
    probe.error = None
    probe.content = '{"claims": []}'
    continuing = _ContinuingExtractor(
        extractor,
        extractor.profile,
        {},
        {
            first["untrusted_document_data"]["source_id"],
            second["untrusted_document_data"]["source_id"],
        },
        2,
    )
    with pytest.raises(ExtractionOutputError, match="PREVIOUS_TRANSPORT_FAILED"):
        continuing.extract(first)
    assert continuing.stop_code is None
    assert continuing.extract(second) == {"spans": []}
    assert len(probe.calls) == 2  # first failed attempt + only the new source
    assert (directory / "failure.json").read_bytes() == failure
