import json
from decimal import Decimal

import pytest
from proofops.adapters.local.upstage import UpstageProbe
from proofops.domain.provenance import canonical_hash

from tests.integration.test_upstage_parse import make_pdf

SCHEMA = {
    "type": "object",
    "properties": {"claims": {"type": "array", "items": {"type": "string"}}},
}


def test_extract_one_page_shares_budget_and_preserves_raw_receipt(tmp_path, monkeypatch):
    from proofops.adapters.local.upstage_extract import EXTRACT_MODEL, UpstageExtractProbe

    client = UpstageExtractProbe("test-secret", tmp_path / "budget.sqlite3")
    response = {
        "id": "iex-test",
        "model": EXTRACT_MODEL,
        "choices": [{"finish_reason": "stop", "message": {"content": '{"claims":[]}'}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    }
    monkeypatch.setattr(client, "_post_extract", lambda body: response)
    receipt = client.extract(make_pdf(1), SCHEMA, request_id="image-one")
    assert receipt["content"] == '{"claims":[]}'
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.066")
    assert UpstageProbe("test-secret", client.ledger).summary() == client.summary()
    assert receipt["pages"] == 1 and receipt["mode"] == "enhanced"
    saved = client.ledger.parent / "extract-responses" / (canonical_hash("image-one") + ".json")
    assert json.loads(saved.read_text()) == response
    with pytest.raises(ValueError, match="DUPLICATE_PROBE_REQUEST"):
        client.extract(make_pdf(1), SCHEMA, request_id="image-one")


@pytest.mark.parametrize("failure", ["pdf", "pages", "schema", "remote_schema"])
def test_invalid_image_probe_never_reserves(tmp_path, failure):
    from proofops.adapters.local.upstage_extract import UpstageExtractProbe

    client = UpstageExtractProbe("test-secret", tmp_path / "budget.sqlite3")
    pdf = b"bad" if failure == "pdf" else make_pdf(2 if failure == "pages" else 1)
    schema = (
        {"type": "madeup"}
        if failure == "schema"
        else {"$ref": "https://invalid.test"}
        if failure == "remote_schema"
        else SCHEMA
    )
    with pytest.raises(ValueError):
        client.extract(pdf, schema, request_id="bad")
    assert client.summary()["calls"] == 0


@pytest.mark.parametrize("failure", ["transport", "model", "usage"])
def test_failed_extract_keeps_reservation_without_secret(tmp_path, monkeypatch, failure):
    from proofops.adapters.local.upstage_extract import EXTRACT_MODEL, UpstageExtractProbe

    client = UpstageExtractProbe("test-secret", tmp_path / "budget.sqlite3")

    def post(body):
        if failure == "transport":
            raise ConnectionError("test-secret")
        return {
            "id": "iex-test",
            "model": "unknown" if failure == "model" else EXTRACT_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 99},
        }

    monkeypatch.setattr(client, "_post_extract", post)
    with pytest.raises(ValueError) as error:
        client.extract(make_pdf(1), SCHEMA, request_id="bad-response")
    assert "test-secret" not in str(error.value)
    assert client.summary()["unsettled_calls"] == 1
    assert client.summary()["committed_usd"] == "1.00"


@pytest.fixture(autouse=True)
def fixed_pricing_date(monkeypatch):
    from datetime import UTC, datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 13, tzinfo=UTC)

    monkeypatch.setattr("proofops.adapters.local.upstage_extract.datetime", FixedDateTime)


def test_png_input_preserves_one_image_charge_and_rejects_animated_image(tmp_path, monkeypatch):
    import io

    from PIL import Image
    from proofops.adapters.local.upstage_extract import EXTRACT_MODEL, UpstageExtractProbe

    png = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(png, format="PNG")
    client = UpstageExtractProbe("test-secret", tmp_path / "budget.sqlite3")
    observed = []

    def post(body):
        observed.append(body)
        return {
            "id": "iex-image",
            "model": EXTRACT_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": '{"claims":[]}'}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }

    monkeypatch.setattr(client, "_post_extract", post)
    receipt = client.extract(png.getvalue(), SCHEMA, request_id="png")
    assert receipt["input_media_type"] == "image/png"
    assert receipt["pages"] == 1
    assert observed[0]["messages"][0]["content"][0]["image_url"]["url"].startswith(
        "data:application/octet-stream;base64,"
    )
    animated = io.BytesIO()
    Image.new("RGB", (20, 20), "white").save(
        animated, format="PNG", save_all=True, append_images=[Image.new("RGB", (20, 20), "black")]
    )
    with pytest.raises(ValueError):
        client.extract(animated.getvalue(), SCHEMA, request_id="animated")
    assert client.summary()["calls"] == 1


@pytest.mark.parametrize("day,expired", [(25, False), (2, True)])
def test_rechecked_document_price_window(tmp_path, monkeypatch, day, expired):
    from datetime import UTC, datetime

    from proofops.adapters.local import upstage_extract, upstage_parse

    from tests.integration.test_upstage_parse import fake_response

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10 if expired else 9, day, tzinfo=UTC)

    for module in (upstage_extract, upstage_parse):
        monkeypatch.setattr(module, "datetime", Clock)
    extract = upstage_extract.UpstageExtractProbe("offline", tmp_path / "extract.sqlite3")
    parse = upstage_parse.UpstageParseProbe("offline", tmp_path / "parse.sqlite3")
    monkeypatch.setattr(
        extract,
        "_post_extract",
        lambda body: {
            "id": "test",
            "model": upstage_extract.EXTRACT_MODEL,
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    monkeypatch.setattr(parse, "_post_parse", lambda pdf, mode: fake_response(1, mode))
    for client, invoke in (
        (extract, lambda: extract.extract(make_pdf(1), SCHEMA, request_id="extract-window")),
        (parse, lambda: parse.parse(make_pdf(1), request_id="parse-window", mode="standard")),
    ):
        if expired:
            with pytest.raises(ValueError, match="PRICE_RECHECK_REQUIRED"):
                invoke()
        else:
            invoke()
        assert client.summary()["calls"] == (0 if expired else 1)
