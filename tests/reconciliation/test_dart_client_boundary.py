"""Coordinator checks of official DART response and bounded failure handling."""

import json

import pytest
from proofops.adapters.dart import DartClient, DartError


@pytest.mark.parametrize(
    "settings",
    [
        {"timeout": float("nan")},
        {"retry_backoff": float("nan")},
        {"max_retries": True},
        {"timeout": True},
    ],
)
def test_invalid_retry_configuration(settings):
    with pytest.raises(ValueError):
        DartClient(**settings)


def test_full_statement_response_does_not_require_unpublished_fs_div_field():
    row = {
        "corp_code": "00126380",
        "bsns_year": "2024",
        "reprt_code": "11011",
        "rcept_no": "20250314000123",
        "sj_div": "CF",
        "currency": "KRW",
    }
    raw = json.dumps({"status": "000", "list": [row]}).encode()
    client = DartClient(api_key="mock", transport=lambda *a, **k: (200, {}, raw))
    response = client.get_financial_statements("00126380", 2024, expected_rcept_no=row["rcept_no"])
    assert response.raw_bytes == raw
    response.data["list"][0]["rcept_no"] = "forged"
    assert response["list"][0]["rcept_no"] == row["rcept_no"]


@pytest.mark.parametrize(
    "raw",
    [
        b"[]",
        b'{"status":"000","status":"013"}',
        b'{"status":"000","x":NaN}',
        b'{"status":"000","list":[]}',
        b'{"status":"000","list":{}}',
    ],
)
def test_malformed_statement_response_fails(raw):
    client = DartClient(api_key="mock", transport=lambda *a, **k: (200, {}, raw))
    with pytest.raises((DartError, ValueError)):
        client.get_financial_statements("00126380", 2024)


def test_pk_prefix_is_not_a_valid_archive():
    client = DartClient(api_key="mock", transport=lambda *a, **k: (200, {}, b"PK\x03\x04garbage"))
    with pytest.raises(DartError):
        client.download_document("20250314000123")


@pytest.mark.parametrize("begin,end", [("20240230", "20241231"), ("20250101", "20240101")])
def test_filing_date_validation_precedes_network(begin, end):
    def transport(*a, **k):
        pytest.fail("invalid request reached network")

    with pytest.raises(ValueError):
        DartClient(api_key="mock", transport=transport).list_filings("00126380", begin, end)
