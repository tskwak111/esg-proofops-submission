"""Document Parse budget, page limits, receipts and credential-safe failure checks."""

import hashlib
import io
import json
from decimal import Decimal

import pytest
from proofops.adapters.local import upstage
from proofops.adapters.local.upstage_parse import (
    PARSE_MODEL_PINNED,
    UpstageParseProbe,
)
from proofops.domain.provenance import canonical_hash


def make_pdf(num_pages: int) -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def make_encrypted_pdf() -> bytes:
    import pypdf

    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("password")
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def fake_response(pages: int, mode: str = "standard", model: str = PARSE_MODEL_PINNED):
    # Build usage with correct coverage: standard or enhanced list covers all pages
    if mode == "standard":
        usage = {"pages": pages, "standard": list(range(1, pages + 1))}
    else:
        usage = {"pages": pages, "enhanced": list(range(1, pages + 1))}
    return {
        "api": "1.0",
        "model": model,
        "content": {"text": "hello", "html": "<p>hello</p>"},
        "usage": usage,
        "elements": [],
    }


def test_wrong_billing_mode_keeps_reservation_and_raw_response(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret", tmp_path / "budget.sqlite3")
    response = fake_response(1, "enhanced")
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: response)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID"):
        client.parse(make_pdf(1), request_id="wrong-mode", mode="standard")
    assert client.summary()["unsettled_calls"] == 1
    saved = tmp_path / "parse-responses" / (canonical_hash("wrong-mode") + ".json")
    assert json.loads(saved.read_text()) == response


@pytest.fixture(autouse=True)
def fixed_pricing_date(monkeypatch):
    from datetime import UTC, datetime

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 9, tzinfo=UTC)

    monkeypatch.setattr("proofops.adapters.local.upstage_parse.datetime", FixedDateTime)
    monkeypatch.setattr(upstage, "datetime", FixedDateTime)


def test_parse_success_standard(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(2)
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: fake_response(2, "standard"))
    result = client.parse(pdf, request_id="parse-std-1", mode="standard")
    assert result["pages"] == 2
    assert result["mode"] == "standard"
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.022")
    assert result["provider_model"] == PARSE_MODEL_PINNED
    assert result["raw_response"]["usage"]["pages"] == 2
    assert "request_hash" in result
    assert "response_sha256" in result
    assert "provider_model_hash" in result
    # raw response retained and hashes
    assert result["raw_response"] == fake_response(2, "standard")
    assert result["response_sha256"] == canonical_hash(fake_response(2, "standard"))
    # ledger updated
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.022")
    # request hash equals canonical hash of reserve body
    expected_body = {
        "model": PARSE_MODEL_PINNED,
        "mode": "standard",
        "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
        "pages": 2,
        "bytes_len": len(pdf),
    }
    assert result["request_hash"] == canonical_hash(expected_body)
    assert result["request_sha256"] == canonical_hash(expected_body)


def test_parse_success_enhanced(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(3)
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: fake_response(3, "enhanced"))
    result = client.parse(pdf, request_id="parse-enhanced-1", mode="enhanced")
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.099")
    assert result["mode"] == "enhanced"
    assert result["pages"] == 3
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.099")


def test_budget_sharing_and_exhaustion_across_probes(tmp_path, monkeypatch):
    path = tmp_path / "budget.sqlite3"
    text_client = upstage.UpstageProbe("test-secret", path)
    parse_client = UpstageParseProbe("test-secret", path)

    # Mock both to fail with OSError -> keep reservation
    monkeypatch.setattr(
        text_client, "_post", lambda body: (_ for _ in ()).throw(OSError("net fail"))
    )
    monkeypatch.setattr(
        parse_client,
        "_post_parse",
        lambda pdf_bytes, mode: (_ for _ in ()).throw(OSError("net fail")),
    )

    pdf = make_pdf(1)
    # 5 text calls
    for i in range(5):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            text_client.complete("sys", "{}", request_id=f"text-{i}")
    # 5 parse calls -> total 10 reservations
    for i in range(5):
        with pytest.raises(ValueError, match="UPSTAGE_REQUEST_FAILED"):
            parse_client.parse(pdf, request_id=f"parse-{i}", mode="standard")

    assert Decimal(parse_client.summary()["committed_usd"]) == Decimal(10)
    assert Decimal(text_client.summary()["committed_usd"]) == Decimal(10)

    # Next call from either should be BUDGET_EXHAUSTED before network
    monkeypatch.setattr(
        parse_client, "_post_parse", lambda pdf_bytes, mode: pytest.fail("budget allowed network")
    )
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        parse_client.parse(pdf, request_id="parse-over", mode="standard")
    monkeypatch.setattr(text_client, "_post", lambda body: pytest.fail("budget allowed network"))
    with pytest.raises(ValueError, match="BUDGET_EXHAUSTED"):
        text_client.complete("sys", "{}", request_id="text-over")


def test_duplicate_request_keeps_original_and_no_retry(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: fake_response(1, "standard"))
    result = client.parse(pdf, request_id="dup-id", mode="standard")
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.011")

    # duplicate should raise without calling transport again
    def fail(pdf_bytes, mode):
        pytest.fail("duplicate triggered network")

    monkeypatch.setattr(client, "_post_parse", fail)
    with pytest.raises(ValueError, match="DUPLICATE_PROBE_REQUEST"):
        client.parse(pdf, request_id="dup-id", mode="standard")
    # committed only once
    assert Decimal(client.summary()["committed_usd"]) == Decimal("0.011")
    assert client.summary()["calls"] == 1


def test_page_count_bounds_rejected_without_reservation(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    monkeypatch.setattr(
        client, "_post_parse", lambda pdf_bytes, mode: pytest.fail("invalid pdf dispatched")
    )

    # Create PDF with 11 pages
    pdf_11 = make_pdf(11)
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf_11, request_id="too-many-pages", mode="standard")
    assert client.summary()["calls"] == 0

    # oversized >10MB
    pdf_small = make_pdf(1)
    oversized = pdf_small + b"a" * (10 * 1024 * 1024)
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(oversized, request_id="too-large", mode="standard")
    assert client.summary()["calls"] == 0

    # encrypted
    enc_pdf = make_encrypted_pdf()
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(enc_pdf, request_id="encrypted", mode="standard")
    assert client.summary()["calls"] == 0

    # invalid mode
    valid_pdf = make_pdf(1)
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(valid_pdf, request_id="bad-mode", mode="auto")
    assert client.summary()["calls"] == 0

    # non-bytes
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse("not-bytes", request_id="bad-type", mode="standard")  # type: ignore
    assert client.summary()["calls"] == 0


def test_page_count_mismatch_retains_reservation(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(2)

    # usage.pages != submitted pages
    def bad_pages(pdf_bytes, mode):
        return fake_response(3, "standard")  # 3 vs 2

    monkeypatch.setattr(client, "_post_parse", bad_pages)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client.parse(pdf, request_id="mismatch-pages", mode="standard")
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)
    assert client.summary()["unsettled_calls"] == 1


def test_mode_page_lists_validation_retains_reservation(tmp_path, monkeypatch):
    pdf = make_pdf(3)

    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")

    # Case 1: duplicate in list
    def dup_list(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 3, "standard": [1, 2, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client, "_post_parse", dup_list)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client.parse(pdf, request_id="dup-list", mode="standard")
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)

    # Case 2: missing coverage (gap)
    client2 = UpstageParseProbe("test-secret-parse", tmp_path / "budget2.sqlite3")

    def gap_list(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 3, "standard": [1, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client2, "_post_parse", gap_list)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client2.parse(pdf, request_id="gap", mode="standard")
    assert Decimal(client2.summary()["committed_usd"]) == Decimal(1)

    # Case 3: bool in list (True is 1)
    client3 = UpstageParseProbe("test-secret-parse", tmp_path / "budget3.sqlite3")

    def bool_list(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 2, "standard": [True, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client3, "_post_parse", bool_list)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client3.parse(pdf, request_id="bool", mode="standard")
    assert Decimal(client3.summary()["committed_usd"]) == Decimal(1)

    # Case 4: overlapping across modes
    client4 = UpstageParseProbe("test-secret-parse", tmp_path / "budget4.sqlite3")

    def overlap(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 2, "standard": [1], "enhanced": [1, 2]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client4, "_post_parse", overlap)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client4.parse(pdf, request_id="overlap", mode="standard")
    assert Decimal(client4.summary()["committed_usd"]) == Decimal(1)

    # Case 5: out of range
    client5 = UpstageParseProbe("test-secret-parse", tmp_path / "budget5.sqlite3")

    def out_of_range(pdf_bytes, mode):
        return {
            "api": "1.0",
            "model": PARSE_MODEL_PINNED,
            "usage": {"pages": 2, "standard": [1, 3]},
            "content": {"text": "x"},
        }

    monkeypatch.setattr(client5, "_post_parse", out_of_range)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client5.parse(pdf, request_id="oor", mode="standard")
    assert Decimal(client5.summary()["committed_usd"]) == Decimal(1)


def test_invalid_usage_retained_reservation_various(tmp_path, monkeypatch):
    pdf = make_pdf(1)
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")

    # usage bool
    def bool_pages(pdf_bytes, mode):
        return {"api": "1.0", "model": PARSE_MODEL_PINNED, "usage": {"pages": True}, "content": {}}

    monkeypatch.setattr(client, "_post_parse", bool_pages)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client.parse(pdf, request_id="bool-pages", mode="standard")
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)

    # wrong model
    client2 = UpstageParseProbe("test-secret-parse", tmp_path / "budget2b.sqlite3")

    def wrong_model(pdf_bytes, mode):
        return {"api": "1.0", "model": "another-provider", "usage": {"pages": 1}, "content": {}}

    monkeypatch.setattr(client2, "_post_parse", wrong_model)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client2.parse(pdf, request_id="wrong-model", mode="standard")
    assert Decimal(client2.summary()["committed_usd"]) == Decimal(1)

    # empty model
    client3 = UpstageParseProbe("test-secret-parse", tmp_path / "budget3b.sqlite3")

    def empty_model(pdf_bytes, mode):
        return {"api": "1.0", "model": "", "usage": {"pages": 1}, "content": {}}

    monkeypatch.setattr(client3, "_post_parse", empty_model)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client3.parse(pdf, request_id="empty-model", mode="standard")
    assert Decimal(client3.summary()["committed_usd"]) == Decimal(1)

    # missing usage
    client4 = UpstageParseProbe("test-secret-parse", tmp_path / "budget4b.sqlite3")

    def no_usage(pdf_bytes, mode):
        return {"api": "1.0", "model": PARSE_MODEL_PINNED, "content": {}}

    monkeypatch.setattr(client4, "_post_parse", no_usage)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED"):
        client4.parse(pdf, request_id="no-usage", mode="standard")
    assert Decimal(client4.summary()["committed_usd"]) == Decimal(1)


def test_alias_model_accepted(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)
    monkeypatch.setattr(
        client,
        "_post_parse",
        lambda pdf_bytes, mode: fake_response(1, "standard", model="document-parse"),
    )
    result = client.parse(pdf, request_id="alias", mode="standard")
    assert result["provider_model"] == "document-parse"
    assert Decimal(result["cost_with_vat_reserve_usd"]) == Decimal("0.011")


def test_no_credential_in_errors(tmp_path, monkeypatch):
    secret = "super-secret-parse-key-123"
    client = UpstageParseProbe(secret, tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)

    def fail_with_secret(pdf_bytes, mode):
        raise OSError(f"failed with {secret} leaked")

    monkeypatch.setattr(client, "_post_parse", fail_with_secret)
    with pytest.raises(ValueError) as exc:
        client.parse(pdf, request_id="cred-fail", mode="standard")
    assert secret not in str(exc.value)
    # also ensure sanitized to UPSTAGE_REQUEST_FAILED
    assert "UPSTAGE_REQUEST_FAILED" in str(exc.value)
    assert Decimal(client.summary()["committed_usd"]) == Decimal(1)

    # HTTP error sanitized but allowed codes pass through
    def http_500(pdf_bytes, mode):
        raise ValueError("UPSTAGE_HTTP_500")

    monkeypatch.setattr(client, "_post_parse", http_500)
    with pytest.raises(ValueError, match="UPSTAGE_HTTP_500") as exc2:
        client.parse(pdf, request_id="http500", mode="standard")
    assert secret not in str(exc2.value)

    # receipt invalid should not leak
    def bad_receipt(pdf_bytes, mode):
        return {"api": "1.0", "model": "", "usage": {"pages": 1}}

    monkeypatch.setattr(client, "_post_parse", bad_receipt)
    with pytest.raises(ValueError) as exc3:
        client.parse(pdf, request_id="bad-receipt-cred", mode="standard")
    assert secret not in str(exc3.value)


def test_success_cost_exact_and_ledger_row_update(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    for pages, mode, expected in [
        (1, "standard", Decimal("0.011")),
        (10, "standard", Decimal("0.11")),
        (1, "enhanced", Decimal("0.033")),
        (10, "enhanced", Decimal("0.33")),
    ]:
        pdf = make_pdf(pages)
        monkeypatch.setattr(
            client,
            "_post_parse",
            lambda pdf_bytes, mode=mode, pages=pages: fake_response(pages, mode),
        )
        result = client.parse(pdf, request_id=f"cost-{pages}-{mode}", mode=mode)
        assert Decimal(result["cost_with_vat_reserve_usd"]) == expected
        assert Decimal(result["cost_with_vat_reserve_usd"]) <= Decimal("1.00")
        # verify ledger receipt stored
        import sqlite3

        with sqlite3.connect(client.ledger) as db:
            row = db.execute(
                "SELECT committed, receipt FROM probe_calls WHERE request_id=?",
                (f"cost-{pages}-{mode}",),
            ).fetchone()
            assert row is not None
            assert Decimal(row[0]) == expected
            receipt = json.loads(row[1])
            assert receipt["response_sha256"] == canonical_hash(fake_response(pages, mode))
            assert receipt["request_sha256"] == canonical_hash(
                {
                    "model": PARSE_MODEL_PINNED,
                    "mode": mode,
                    "pdf_sha256": hashlib.sha256(pdf).hexdigest(),
                    "pages": pages,
                    "bytes_len": len(pdf),
                }
            )
            assert receipt["raw_response"] == fake_response(pages, mode)


def test_invalid_mode_and_request_id_validation(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret-parse", tmp_path / "budget.sqlite3")
    pdf = make_pdf(1)
    monkeypatch.setattr(
        client, "_post_parse", lambda pdf_bytes, mode: pytest.fail("should not reach network")
    )
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf, request_id="", mode="standard")
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf, request_id="x" * 129, mode="standard")
    with pytest.raises(ValueError, match="INVALID_PROBE_REQUEST"):
        client.parse(pdf, request_id="valid", mode="standard ")


def test_missing_mode_usage_retains_budget_and_private_archive(tmp_path, monkeypatch):
    client = UpstageParseProbe("test-secret", tmp_path / "budget.sqlite3")
    response = fake_response(1)
    response["usage"] = {"pages": 1}
    monkeypatch.setattr(client, "_post_parse", lambda pdf_bytes, mode: response)
    with pytest.raises(ValueError, match="UPSTAGE_RECEIPT_INVALID"):
        client.parse(make_pdf(1), request_id="missing-mode", mode="standard")
    assert client.summary()["unsettled_calls"] == 1
    archive = next((tmp_path / "parse-responses").glob("*.json"))
    assert archive.stat().st_mode & 0o777 == 0o400


@pytest.mark.parametrize("transport", ["text", "parse"])
def test_missing_reservation_cannot_return_settled_success(tmp_path, monkeypatch, transport):
    import sqlite3

    cls = upstage.UpstageProbe if transport == "text" else UpstageParseProbe
    client = cls("test-secret", tmp_path / "budget.sqlite3")

    def post(*args):
        with sqlite3.connect(client.ledger) as db:
            db.execute("DELETE FROM probe_calls WHERE request_id='lost'")
        if transport == "parse":
            return fake_response(1)
        return {
            "id": "fake",
            "model": "solar-pro3",
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }

    monkeypatch.setattr(client, "_post" if transport == "text" else "_post_parse", post)
    with pytest.raises(ValueError, match="BUDGET_SETTLEMENT_INVALID"):
        if transport == "text":
            client.complete("JSON", "{}", request_id="lost")
        else:
            client.parse(make_pdf(1), request_id="lost", mode="standard")
