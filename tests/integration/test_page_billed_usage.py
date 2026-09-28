import io
import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal

import pypdf
import pytest
from proofops.adapters.local import upstage
from proofops.adapters.local.upstage import UpstageProbe, request_usage
from proofops.adapters.local.upstage_parse import PARSE_MODEL_PINNED, UpstageParseProbe


def _pdf(pages: int = 1) -> bytes:
    writer = pypdf.PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=100, height=100)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


@pytest.fixture(autouse=True)
def fixed_pricing_date(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 19, tzinfo=UTC)

    monkeypatch.setattr(upstage, "datetime", FixedDateTime)
    monkeypatch.setattr("proofops.adapters.local.upstage_parse.datetime", FixedDateTime)


def test_page_billed_receipt_is_accounted_without_token_measurements(tmp_path, monkeypatch):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageParseProbe("test-key", ledger)
    monkeypatch.setattr(
        probe,
        "_post_parse",
        lambda _pdf, _mode: {
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 1, "standard": [1]},
        },
    )
    probe.parse(_pdf(), request_id="parse-1", mode="standard")

    usage = request_usage(ledger, ["parse-1"])

    assert usage["model_calls"] == usage["settled_calls"] == 1
    assert usage["input_tokens"] == usage["output_tokens"] == 0
    assert usage["token_usage_complete"] is False
    assert usage["document_parse_pages"] == 1
    assert Decimal(usage["committed_or_reserved_usd"]) == Decimal("0.011")
    assert usage["cost_with_vat_reserve_usd"] == "0.011"


def test_mixed_text_parse_and_unknown_preserves_costs_and_token_aggregation(tmp_path, monkeypatch):
    ledger = tmp_path / "budget.sqlite3"
    text = UpstageProbe("test-key", ledger)
    monkeypatch.setattr(
        text,
        "_post",
        lambda _body: {
            "id": "text-provider-1",
            "model": "solar-pro3-260323",
            "usage": {"prompt_tokens": 4, "completion_tokens": 3},
            "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        },
    )
    text.complete("system", "{}", request_id="text-1")
    parse = UpstageParseProbe("test-key", ledger)
    monkeypatch.setattr(
        parse,
        "_post_parse",
        lambda _pdf, _mode: {
            "model": "document-parse",
            "usage": {"pages": 2, "standard": [1, 2]},
        },
    )
    parse.parse(_pdf(2), request_id="parse-2", mode="standard")
    with sqlite3.connect(ledger) as db:
        db.execute(
            "INSERT INTO probe_calls VALUES (?, ?, ?, NULL)",
            ("unknown-1", "unknown", "1.00"),
        )

    usage = request_usage(ledger, ["text-1", "parse-2", "unknown-1"])

    assert usage["model_calls"] == 3
    assert usage["settled_calls"] == 2
    assert usage["unsettled_calls"] == 1
    assert usage["input_tokens"] == 4
    assert usage["output_tokens"] == 3
    assert usage["token_usage_complete"] is False
    assert usage["document_parse_pages"] == 2
    assert usage["cost_with_vat_reserve_usd"] == "unknown"
    assert Decimal(usage["committed_or_reserved_usd"]) == Decimal("1.022") + Decimal("0.00000264")


def test_malformed_settled_receipt_fails_closed(tmp_path):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("test-key", ledger)
    probe._reserve("bad-1", {"request": "bad"})
    with sqlite3.connect(ledger) as db:
        db.execute(
            "UPDATE probe_calls SET committed=?, receipt=? WHERE request_id=?",
            (
                "0.25",
                json.dumps({"model": "document-parse-260128", "usage": {"pages": "1"}}),
                "bad-1",
            ),
        )

    with pytest.raises(ValueError, match="ACCOUNTING_UNAVAILABLE"):
        request_usage(ledger, ["bad-1"])


@pytest.mark.parametrize(
    "fault", ["nan_cost", "negative_cost", "model_list", "provider_list", "parse_tokens"]
)
def test_corrupt_accounting_fields_raise_sanitized_error(tmp_path, fault):
    ledger = tmp_path / "budget.sqlite3"
    probe = UpstageProbe("test-key", ledger)
    probe._reserve("corrupt", {"request": "corrupt"})
    receipt = dict(
        model=PARSE_MODEL_PINNED, provider_model=PARSE_MODEL_PINNED, pages=1, usage=dict(pages=1)
    )
    cost = "0.011"
    if fault == "nan_cost":
        cost = "NaN"
    elif fault == "negative_cost":
        cost = "-1"
    elif fault == "model_list":
        receipt["model"] = []
    elif fault == "provider_list":
        receipt["provider_model"] = []
    else:
        receipt.update(input_tokens=0, output_tokens=0)
    with sqlite3.connect(ledger) as db:
        db.execute(
            "UPDATE probe_calls SET committed=?, receipt=? WHERE request_id='corrupt'",
            (cost, json.dumps(receipt)),
        )
    with pytest.raises(ValueError, match="ACCOUNTING_UNAVAILABLE"):
        request_usage(ledger, ["corrupt"])
