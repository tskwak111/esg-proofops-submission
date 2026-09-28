"""No network: durable USD guard and sanitized Upstage transport receipts."""

import json
from decimal import Decimal

import pytest
from proofops.adapters.local import upstage


def response(model="solar-pro3"):
    data = {
        "id": "test-provider-id",
        "model": "solar-pro3",
        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    data["model"] = model
    return data


def test_usage_settles_and_duplicate_cannot_bill_again(tmp_path, monkeypatch):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    monkeypatch.setattr(client, "_post", lambda body: response())
    result = client.complete("JSON only", "{}", request_id="one")
    assert json.loads(result["content"]) == {"ok": True}
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.0000297")
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.0000297")
    with pytest.raises(ValueError, match="DUPLICATE_PROBE_REQUEST"):
        client.complete("JSON only", "{}", request_id="one")
    assert "test-secret" not in repr(client)


def test_unknown_calls_reserve_budget_across_restarts_and_stop_before_network(
    tmp_path, monkeypatch
):
    path = tmp_path / "budget.sqlite3"
    client = upstage.UpstageProbe("test-secret", path)

    def fail(body):
        raise OSError("contains test-secret")

    monkeypatch.setattr(client, "_post", fail)
    for i in range(10):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED") as error:
            client.complete("JSON only", "{}", request_id=str(i))
        assert "test-secret" not in str(error.value)
    other = upstage.UpstageProbe("test-secret", path)
    monkeypatch.setattr(other, "_post", lambda body: pytest.fail("budget allowed network"))
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        other.complete("JSON only", "{}", request_id="over")
    assert Decimal(other.summary()["committed_usd"]) == 10


@pytest.mark.parametrize("change", ["usage", "truncated", "wrong_model", "too_many_tokens"])
def test_invalid_provider_receipt_keeps_reservation(tmp_path, monkeypatch, change):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    data = response()
    if change == "usage":
        data["usage"]["prompt_tokens"] = True
    elif change == "truncated":
        data["choices"][0]["finish_reason"] = "length"
    elif change == "wrong_model":
        data["model"] = "another-provider"
    else:
        data["usage"]["completion_tokens"] = 10000000
    monkeypatch.setattr(client, "_post", lambda body: data)
    with pytest.raises(ValueError):
        client.complete("JSON only", "{}", request_id="invalid")
    assert Decimal(client.summary()["committed_usd"]) == 1


def test_provider_observed_version_is_retained(tmp_path, monkeypatch):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    data = response()
    data["model"] = "solar-pro3-260323"
    monkeypatch.setattr(client, "_post", lambda body: data)
    result = client.complete("JSON only", "{}", request_id="versioned")
    assert result["provider_model"] == "solar-pro3-260323"


def test_explicit_json_mode_reaches_transport_and_is_recorded(tmp_path, monkeypatch):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")

    def post(body):
        assert body["response_format"] == {"type": "json_object"}
        return response()

    monkeypatch.setattr(client, "_post", post)
    result = client.complete("JSON only", "{}", request_id="json-mode", json_mode=True)
    assert result["response_format"] == {"type": "json_object"}


def test_concurrent_reservations_cannot_exceed_limit(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    monkeypatch.setattr(client, "_post", lambda body: (_ for _ in ()).throw(OSError()))

    def call(i):
        try:
            client.complete("JSON", "{}", request_id=str(i))
        except ValueError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(call, range(12)))
    assert outcomes.count("BUDGET_EXHAUSTED") == 2
    assert Decimal(client.summary()["committed_usd"]) == 10


def test_oversized_request_is_rejected_without_budget_or_network(tmp_path, monkeypatch):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    monkeypatch.setattr(client, "_post", lambda body: pytest.fail("invalid request dispatched"))
    with pytest.raises(ValueError, match="TOO_LARGE"):
        client.complete("JSON", "a" * 20000, request_id="oversized")
    assert client.summary()["calls"] == 0


def test_pro4_receipt_pins_pro4_rates(tmp_path, monkeypatch):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3", model="solar-pro4")

    def post(body):
        assert body["model"] == "solar-pro4"
        assert "reasoning_effort" not in body
        assert body["max_tokens"] <= 4096
        return response("solar-pro4")

    monkeypatch.setattr(client, "_post", post)
    result = client.complete("JSON only", "{}", request_id="pro4-one")
    assert result["model"] == "solar-pro4"
    assert result["provider_model"] == "solar-pro4"
    assert result["price_snapshot"] == upstage.PRICE_PRO4.to_dict()
    assert result["price_snapshot"]["input_per_million"] == "0.30"
    assert result["price_snapshot"]["output_per_million"] == "1.20"
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.0000594")
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.0000594")


def test_pro4_documented_version_is_retained(tmp_path, monkeypatch):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3", model="solar-pro4")
    monkeypatch.setattr(client, "_post", lambda body: response("solar-pro4-260806"))
    result = client.complete("JSON only", "{}", request_id="pro4-versioned")
    assert result["provider_model"] == "solar-pro4-260806"
    assert result["model"] == "solar-pro4"


def test_pro4_rejects_pro3_provider_model_retains_reservation(tmp_path, monkeypatch):
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3", model="solar-pro4")
    monkeypatch.setattr(client, "_post", lambda body: response("solar-pro3"))
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client.complete("JSON only", "{}", request_id="pro4-wrong")
    assert Decimal(client.summary()["committed_usd"]) == 1


def test_unsupported_model_fails_before_ledger(tmp_path):
    path = tmp_path / "budget.sqlite3"
    with pytest.raises(ValueError, match="UNSUPPORTED_MODEL"):
        upstage.UpstageProbe("test-secret", path, model="solar-pro99")
    assert not path.exists()


def test_models_share_ledger_aggregate_budget(tmp_path, monkeypatch):
    path = tmp_path / "budget.sqlite3"
    pro3 = upstage.UpstageProbe("test-secret", path)
    monkeypatch.setattr(pro3, "_post", lambda body: response("solar-pro3"))
    pro3.complete("JSON only", "{}", request_id="shared-pro3")
    # Same ledger file opens cleanly with an explicit Pro4 probe: no policy reset.
    pro4 = upstage.UpstageProbe("test-secret", path, model="solar-pro4")
    monkeypatch.setattr(pro4, "_post", lambda body: response("solar-pro4"))
    result = pro4.complete("JSON only", "{}", request_id="shared-pro4")
    assert result["model"] == "solar-pro4"
    assert Decimal(pro4.summary()["committed_usd"]) == Decimal("0.0000297") + Decimal("0.0000594")
    assert pro4.summary()["calls"] == 2


def test_legacy_default_probe_and_extractor_profile_unchanged(tmp_path, monkeypatch):
    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    default = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    assert default.model == "solar-pro3"
    monkeypatch.setattr(default, "_post", lambda body: response("solar-pro3"))
    result = default.complete("JSON only", "{}", request_id="legacy")
    assert result["model"] == "solar-pro3"
    assert result["price_snapshot"] == upstage.PRICE.to_dict()

    class LegacyFake:
        def complete(self, *args, **kwargs):
            raise AssertionError("no network")

    legacy = UpstageClaimExtractor(LegacyFake(), tmp_path / "legacy")
    current = UpstageClaimExtractor(default, tmp_path / "current")
    assert legacy.profile == current.profile

    pro4 = upstage.UpstageProbe("test-secret", tmp_path / "pro4.sqlite3", model="solar-pro4")
    alternative = UpstageClaimExtractor(pro4, tmp_path / "alternative")
    assert alternative.profile.model_sha256 != current.profile.model_sha256
    assert alternative.profile.prompt_sha256 == current.profile.prompt_sha256
    assert alternative.profile.rule_sha256 == current.profile.rule_sha256


@pytest.fixture(autouse=True)
def fixed_pricing_date(monkeypatch):
    from datetime import UTC, datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, tzinfo=UTC)

    monkeypatch.setattr(upstage, "datetime", FixedDateTime)


def test_model_selection_is_readonly_and_rejects_untyped_input(tmp_path):
    probe = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    with pytest.raises(AttributeError):
        probe.model = "solar-pro4"
    with pytest.raises(ValueError, match="UNSUPPORTED_MODEL"):
        upstage.UpstageProbe("test-secret", tmp_path / "unused.sqlite3", model=[])
    assert not (tmp_path / "unused.sqlite3").exists()


@pytest.mark.parametrize("finish_reason", ["stop", "length"])
def test_provider_response_survives_validation_and_restart(tmp_path, monkeypatch, finish_reason):
    """Deleting response persistence loses the reason an already-paid call failed."""
    from proofops.domain.provenance import canonical_hash

    ledger = tmp_path / "budget.sqlite3"
    client = upstage.UpstageProbe("test-secret", ledger)
    data = response()
    data["choices"][0]["finish_reason"] = finish_reason
    monkeypatch.setattr(client, "_post", lambda body: data)
    request_id = "../request-with-path-characters"
    if finish_reason == "stop":
        client.complete("JSON", "{}", request_id=request_id)
    else:
        with pytest.raises(ValueError, match="RECEIPT_INVALID_RESERVATION_RETAINED"):
            client.complete("JSON", "{}", request_id=request_id)
    root = tmp_path / "budget.sqlite3.responses"
    path = root / (canonical_hash(request_id) + ".json")
    assert path.exists(), "provider response was discarded"
    archived = path.read_bytes()
    assert json.loads(archived) == {"request_id": request_id, "provider_response": data}
    assert "test-secret" not in archived.decode()
    assert root.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600
    restarted = upstage.UpstageProbe("test-secret", ledger)
    with pytest.raises(ValueError, match="DUPLICATE_PROBE_REQUEST"):
        restarted.complete("JSON", "{}", request_id=request_id)
    assert path.read_bytes() == archived
    assert restarted.summary()["unsettled_calls"] == (finish_reason != "stop")


def test_response_archive_failure_keeps_reservation_and_sanitizes_error(tmp_path, monkeypatch):
    """Storage failure must not settle a call whose response could not be preserved."""
    from proofops.domain.provenance import canonical_hash

    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    root = tmp_path / "budget.sqlite3.responses"
    root.mkdir(exist_ok=True)
    path = root / (canonical_hash("collision") + ".json")
    path.write_text("existing immutable receipt")
    monkeypatch.setattr(client, "_post", lambda body: response())
    with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
        client.complete("JSON", "{}", request_id="collision")
    assert path.read_text() == "existing immutable receipt"
    assert client.summary()["unsettled_calls"] == 1
    assert Decimal(client.summary()["committed_usd"]) == 1


def test_utf8_request_limit_matches_transmitted_bytes(tmp_path, monkeypatch):
    sent = []

    class Connection:
        def __init__(self, *args, **kwargs):
            pass

        def request(self, method, path, *, body, headers):
            sent.append(body)

        def getresponse(self):
            return self

        status = 200

        def read(self, limit):
            return json.dumps(response()).encode()

        def close(self):
            pass

    monkeypatch.setattr(upstage.http.client, "HTTPSConnection", Connection)
    client = upstage.UpstageProbe("test-secret", tmp_path / "budget.sqlite3")
    text = "한" * 4000
    client.complete("Extract JSON", text, request_id="utf8")
    assert len(sent) == 1 and len(sent[0]) <= upstage.POLICY["max_request_bytes"]
    assert json.loads(sent[0])["messages"][1]["content"] == text
    assert "한".encode() in sent[0]
    before = client.summary()
    with pytest.raises(ValueError, match="PROBE_REQUEST_TOO_LARGE"):
        client.complete("Extract JSON", "한" * 6000, request_id="too-large")
    assert len(sent) == 1 and client.summary() == before
