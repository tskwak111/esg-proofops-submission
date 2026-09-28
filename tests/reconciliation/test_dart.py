"""Unit, security defense, and schema validation tests for proofops.adapters.dart.

Tests cover:
- DartClient parameter validation (timeout, max_retries, retry_backoff, base_url).
- Default transport restrictions (HTTPS origin, no redirects, bounded reads).
- Exception traceback scrubbing (verifying no secret key leaks in tracebacks).
- DartResponse wire-provenance preservation.
- Pinned receipt number and field integrity validation in get_financial_statements.
- XBRL download parameters including official reprt_code support.
- ArtifactStore ext validation, existing artifact integrity, and exclusive create.
- Zip-slip, zip-bomb, symlink, casefold alias, and overwrite security defenses.
- Safe XML parsing against UTF-8, UTF-16, and multi-byte XXE/entity bypasses.
- Normalization routines (financial amounts, KRW unit scaling, entity sets, periods).
- Rejection of bool, float, NaN, and Infinite inputs in financial amount normalization.
- Collection manifest builder compliance with collection_manifest.schema.json.
"""

from __future__ import annotations

import io
import json
import os
import traceback
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from proofops.adapters.dart import (
    ArtifactStore,
    DartAuthError,
    DartClient,
    DartError,
    DartNotFoundError,
    DartRateLimitError,
    DartResponse,
    build_collection_manifest,
    create_artifact_entry,
    normalize_entity_set,
    normalize_financial_amount,
    normalize_period,
    safe_parse_xml,
)
from proofops.adapters.dart.client import default_stdlib_transport

# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------

SCHEMA_PATH = Path("contracts/reconciliation/collection_manifest.schema.json")


@pytest.fixture
def manifest_schema() -> dict:
    with open(SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class MockTransport:
    def __init__(self, responses: list[tuple[int, dict[str, str], bytes]] | None = None):
        self.responses = list(responses or [])
        self.calls: list[dict] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, str], bytes]:
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        if not self.responses:
            return 200, {"Content-Type": "application/json"}, b'{"status":"000","message":"OK"}'
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# DartClient & API Key Redaction Tests
# ---------------------------------------------------------------------------


def test_client_init_and_repr_redaction(monkeypatch):
    monkeypatch.setenv("DART_API_KEY", "env_secret_key_12345678901234567890")
    client = DartClient(transport=MockTransport())
    assert client.api_key == "env_secret_key_12345678901234567890"
    repr_str = repr(client)
    assert "env_secret_key" not in repr_str
    assert "12345678901234567890" not in repr_str

    client_explicit = DartClient(
        api_key="explicit_secret_99999999999999999999",
        transport=MockTransport(),
    )
    assert "explicit_secret" not in repr(client_explicit)
    assert "99999999999999999999" not in repr(client_explicit)


def test_client_parameter_validation():
    # Invalid timeout
    with pytest.raises(ValueError, match="timeout"):
        DartClient(timeout=0, transport=MockTransport())
    with pytest.raises(ValueError, match="timeout"):
        DartClient(timeout=-5, transport=MockTransport())
    with pytest.raises(ValueError, match="timeout"):
        DartClient(timeout=100, transport=MockTransport())

    # Invalid max_retries
    with pytest.raises(ValueError, match="max_retries"):
        DartClient(max_retries=-1, transport=MockTransport())
    with pytest.raises(ValueError, match="max_retries"):
        DartClient(max_retries=20, transport=MockTransport())

    # Invalid retry_backoff
    with pytest.raises(ValueError, match="retry_backoff"):
        DartClient(retry_backoff=-1, transport=MockTransport())
    with pytest.raises(ValueError, match="retry_backoff"):
        DartClient(retry_backoff=50, transport=MockTransport())

    # Disallowed base_url for real network traffic
    with pytest.raises(ValueError, match="Real network requests must target"):
        DartClient(base_url="https://malicious.site.com")


def test_default_transport_restrictions():
    # Non-https
    with pytest.raises(ValueError, match="Disallowed origin"):
        default_stdlib_transport("http://opendart.fss.or.kr/api/list.json")

    # Non-opendart domain
    with pytest.raises(ValueError, match="Disallowed origin"):
        default_stdlib_transport("https://evil.com/steal_keys")


def test_error_redacts_api_key_and_traceback():
    secret = "super_secret_dart_key_abc123"
    err = DartError(
        f"Request failed for https://opendart.fss.or.kr/api/list.json?crtfc_key={secret}&corp_code=00126380",
        api_key=secret,
    )
    msg = str(err)
    assert secret not in msg
    assert "[REDACTED]" in msg
    assert "00126380" in msg

    # Verify exception traceback formatting does not leak secret
    try:
        raise err
    except DartError as caught:
        tb_lines = traceback.format_exception(caught)
        tb_text = "".join(tb_lines)
        assert secret not in tb_text
        assert "[REDACTED]" in tb_text


def test_transport_failure_suppresses_chained_url_leak():
    secret = "leakable_secret_key_xyz987"

    def bad_transport(url, headers=None, timeout=10.0):
        raise ConnectionResetError(f"Connection reset while querying {url}")

    client = DartClient(api_key=secret, transport=bad_transport, max_retries=0)
    with pytest.raises(DartError) as exc_info:
        client.get_financial_statements(corp_code="00126380", bsns_year=2024)

    # Verify key redacted in error message and traceback
    tb_text = "".join(traceback.format_exception(exc_info.value))
    assert secret not in tb_text
    assert "[REDACTED]" in tb_text


def test_client_validation_errors():
    client = DartClient(api_key="dummy_key", transport=MockTransport())

    # corp_code must be 8 digits
    with pytest.raises(ValueError, match="corp_code"):
        client.list_filings(corp_code="123", bgn_de="20240101", end_de="20241231")
    with pytest.raises(ValueError, match="corp_code"):
        client.list_filings(corp_code="0012638A", bgn_de="20240101", end_de="20241231")

    # bgn_de and end_de must be 8 digits
    with pytest.raises(ValueError, match="bgn_de"):
        client.list_filings(corp_code="00126380", bgn_de="2024-01-01", end_de="20241231")
    with pytest.raises(ValueError, match="end_de"):
        client.list_filings(corp_code="00126380", bgn_de="20240101", end_de="2024")

    # bsns_year must be 4 digits
    with pytest.raises(ValueError, match="bsns_year"):
        client.get_financial_statements(corp_code="00126380", bsns_year="24")

    # reprt_code must be valid
    with pytest.raises(ValueError, match="reprt_code"):
        client.get_financial_statements(corp_code="00126380", bsns_year="2024", reprt_code="99999")

    # fs_div must be CFS or OFS
    with pytest.raises(ValueError, match="fs_div"):
        client.get_financial_statements(corp_code="00126380", bsns_year="2024", fs_div="INVALID")

    # rcept_no must be 14 digits
    with pytest.raises(ValueError, match="rcept_no"):
        client.download_xbrl("12345")

    # invalid reprt_code on download_xbrl
    with pytest.raises(ValueError, match="reprt_code"):
        client.download_xbrl("20240315000123", reprt_code="99999")


def test_list_filings_success_and_raw_provenance():
    payload = {
        "status": "000",
        "message": "정상",
        "page_no": 1,
        "page_count": 10,
        "total_count": 1,
        "total_page": 1,
        "list": [
            {
                "corp_code": "00126380",
                "corp_name": "삼성전자",
                "stock_code": "005930",
                "corp_cls": "Y",
                "report_nm": "사업보고서 (2024.12)",
                "rcept_no": "20250314000123",
                "flr_nm": "삼성전자",
                "rcept_dt": "20250314",
                "rm": "연",
            }
        ],
    }
    raw_wire_bytes = json.dumps(payload).encode()
    transport = MockTransport([(200, {"Content-Type": "application/json"}, raw_wire_bytes)])
    client = DartClient(api_key="secret_test_key", transport=transport)

    result = client.list_filings(corp_code="00126380", bgn_de="20250101", end_de="20250331")

    # DartResponse Mapping contract
    assert isinstance(result, DartResponse)
    assert result["status"] == "000"
    assert len(result["list"]) == 1
    assert result["list"][0]["rcept_no"] == "20250314000123"
    assert result.status_code == 200
    assert result.endpoint == "/api/list.json"

    # Raw byte provenance preservation
    assert result.raw_bytes == raw_wire_bytes

    # Verify query params in call
    assert len(transport.calls) == 1
    call_url = transport.calls[0]["url"]
    assert "corp_code=00126380" in call_url
    assert "crtfc_key=secret_test_key" in call_url
    assert "bgn_de=20250101" in call_url


def test_get_financial_statements_pinned_receipt_success():
    payload = {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "rcept_no": "20250314000123",
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
                "account_nm": "자산총계",
                "thstrm_amount": "450000000000000",
            },
            {
                "rcept_no": "20250314000123",
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
                "account_nm": "부채총계",
                "thstrm_amount": "100000000000000",
            },
        ],
    }
    raw_wire_bytes = json.dumps(payload).encode()
    transport = MockTransport([(200, {"Content-Type": "application/json"}, raw_wire_bytes)])
    client = DartClient(api_key="key", transport=transport)

    resp = client.get_financial_statements(
        corp_code="00126380",
        bsns_year=2024,
        reprt_code="11011",
        fs_div="CFS",
        expected_rcept_no="20250314000123",
    )
    assert resp.status_code == 200
    assert resp.raw_bytes == raw_wire_bytes
    assert resp.rcept_no == "20250314000123"
    assert len(resp["list"]) == 2


def test_get_financial_statements_pinned_receipt_mismatch_rejected():
    payload = {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "rcept_no": "99999999999999",  # Different receipt
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
            }
        ],
    }
    transport = MockTransport([(200, {}, json.dumps(payload).encode())])
    client = DartClient(api_key="key", transport=transport)

    with pytest.raises(ValueError, match="pinned rcept_no mismatch"):
        client.get_financial_statements(
            corp_code="00126380",
            bsns_year=2024,
            expected_rcept_no="20250314000123",
        )


def test_get_financial_statements_field_mismatches_rejected():
    client = DartClient(api_key="key", transport=MockTransport())

    # corp_code mismatch in row
    payload_bad_corp = {
        "status": "000",
        "list": [
            {
                "rcept_no": "20250314000123",
                "corp_code": "00999999",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
            }
        ],
    }
    client._transport = MockTransport([(200, {}, json.dumps(payload_bad_corp).encode())])
    with pytest.raises(ValueError, match="corp_code missing or mismatch"):
        client.get_financial_statements("00126380", 2024)

    # bsns_year mismatch in row
    payload_bad_year = {
        "status": "000",
        "list": [
            {
                "rcept_no": "20250314000123",
                "corp_code": "00126380",
                "bsns_year": "2023",
                "reprt_code": "11011",
                "fs_div": "CFS",
            }
        ],
    }
    client._transport = MockTransport([(200, {}, json.dumps(payload_bad_year).encode())])
    with pytest.raises(ValueError, match="bsns_year missing or mismatch"):
        client.get_financial_statements("00126380", 2024)

    # fs_div mismatch in row
    payload_bad_fs = {
        "status": "000",
        "list": [
            {
                "rcept_no": "20250314000123",
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "OFS",
            }
        ],
    }
    client._transport = MockTransport([(200, {}, json.dumps(payload_bad_fs).encode())])
    with pytest.raises(ValueError, match="fs_div missing or mismatch"):
        client.get_financial_statements("00126380", 2024, fs_div="CFS")


def test_get_financial_statements_missing_fields_and_mixed_receipts():
    client = DartClient(api_key="key", transport=MockTransport())

    # Row missing rcept_no
    payload_no_rcept = {
        "status": "000",
        "list": [
            {
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
            }
        ],
    }
    client._transport = MockTransport([(200, {}, json.dumps(payload_no_rcept).encode())])
    with pytest.raises(ValueError, match="rcept_no is missing"):
        client.get_financial_statements("00126380", 2024)

    # Mixed detected receipts in unpinned call
    payload_mixed_rcept = {
        "status": "000",
        "list": [
            {
                "rcept_no": "20250314000123",
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
            },
            {
                "rcept_no": "20250314000999",
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
            },
        ],
    }
    client._transport = MockTransport([(200, {}, json.dumps(payload_mixed_rcept).encode())])
    with pytest.raises(ValueError, match="Mixed detected receipt numbers"):
        client.get_financial_statements("00126380", 2024)


def test_status_codes_routing_and_fail_closed():
    client = DartClient(api_key="key", transport=MockTransport())

    # Status 012 -> DartAuthError
    client._transport = MockTransport([(200, {}, b'{"status":"012","message":"IP unauthorized"}')])
    with pytest.raises(DartAuthError, match="012"):
        client.list_filings("00126380", "20250101", "20250102")

    # Status 014 -> DartNotFoundError
    client._transport = MockTransport([(200, {}, b'{"status":"014","message":"File missing"}')])
    with pytest.raises(DartNotFoundError, match="014"):
        client.list_filings("00126380", "20250101", "20250102")

    # Status 099 unknown -> DartError
    client._transport = MockTransport([(200, {}, b'{"status":"099","message":"Unknown err"}')])
    with pytest.raises(DartError, match="099"):
        client.list_filings("00126380", "20250101", "20250102")


def test_zip_non_200_http_rejected():
    # HTTP 500 returning PK... header must not be treated as zip
    fake_pk = b"PK\x03\x04errorpayload"
    transport = MockTransport([(500, {}, fake_pk)])
    client = DartClient(api_key="key", transport=transport, max_retries=0)

    with pytest.raises(DartError):
        client.download_document("20240315000123")


def test_list_filings_no_data():
    payload = {"status": "013", "message": "조회된 데이터가 없습니다."}
    transport = MockTransport(
        [(200, {"Content-Type": "application/json"}, json.dumps(payload).encode())]
    )
    client = DartClient(api_key="secret_test_key", transport=transport)

    result = client.list_filings(corp_code="00126380", bgn_de="20250101", end_de="20250102")
    assert result["status"] == "013"
    assert result["message"] == "조회된 데이터가 없습니다."


def test_auth_failure_raises_dart_auth_error():
    payload = {"status": "010", "message": "등록되지 않은 키입니다."}
    transport = MockTransport(
        [(200, {"Content-Type": "application/json"}, json.dumps(payload).encode())]
    )
    client = DartClient(api_key="invalid_key", transport=transport)

    with pytest.raises(DartAuthError, match="등록되지 않은 키입니다"):
        client.get_financial_statements(corp_code="00126380", bsns_year=2024)


def test_rate_limit_raises_dart_rate_limit_error():
    transport = MockTransport([(429, {}, b"Too Many Requests")])
    client = DartClient(api_key="key", transport=transport, max_retries=1)

    with pytest.raises(DartRateLimitError, match="429"):
        client.get_financial_statements(corp_code="00126380", bsns_year=2024)


def test_server_error_retries_and_fails():
    transport = MockTransport(
        [
            (500, {}, b"Internal Server Error"),
            (503, {}, b"Service Unavailable"),
            (502, {}, b"Bad Gateway"),
        ]
    )
    client = DartClient(api_key="key", transport=transport, max_retries=2, retry_backoff=0.01)

    with pytest.raises(DartError, match="502"):
        client.get_financial_statements(corp_code="00126380", bsns_year=2024)
    assert len(transport.calls) == 3


def test_download_xbrl_success_with_reprt_code():
    fake_zip_bytes = make_zip({"report.xml": b"<xbrl>data</xbrl>"})
    transport = MockTransport([(200, {"Content-Type": "application/octet-stream"}, fake_zip_bytes)])
    client = DartClient(api_key="key", transport=transport)

    downloaded = client.download_xbrl("20240315000123", reprt_code="11012")
    assert downloaded == fake_zip_bytes
    assert len(transport.calls) == 1
    assert "rcept_no=20240315000123" in transport.calls[0]["url"]
    assert "reprt_code=11012" in transport.calls[0]["url"]


def test_download_xbrl_xml_error():
    error_xml = """<?xml version="1.0" encoding="UTF-8"?>
    <result>
        <status>013</status>
        <message>해당 서식은 XBRL 파일이 존재하지 않습니다.</message>
    </result>""".encode()
    transport = MockTransport([(200, {"Content-Type": "text/xml"}, error_xml)])
    client = DartClient(api_key="key", transport=transport)

    with pytest.raises(DartNotFoundError, match="존재하지 않습니다"):
        client.download_xbrl("20240315000123")


def test_download_document_success():
    fake_zip_bytes = make_zip({"doc.xml": b"<document>content</document>"})
    transport = MockTransport([(200, {"Content-Type": "application/octet-stream"}, fake_zip_bytes)])
    client = DartClient(api_key="key", transport=transport)

    downloaded = client.download_document("20240315000123")
    assert downloaded == fake_zip_bytes
    assert len(transport.calls) == 1
    assert "rcept_no=20240315000123" in transport.calls[0]["url"]


# ---------------------------------------------------------------------------
# ArtifactStore & Security Defenses Tests
# ---------------------------------------------------------------------------


def test_artifact_store_crud(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    data = b"Hello DART Reconciliation!"
    sha256 = store.store(data, ext=".txt")
    assert len(sha256) == 64
    assert store.has(sha256, ext=".txt")

    # Content-addressed path structure <root>/<sha[:2]>/<sha>.txt
    expected_path = tmp_path / sha256[:2] / f"{sha256}.txt"
    assert expected_path.is_file()
    assert store.path_for(sha256, ext=".txt") == expected_path

    # Retrieval
    retrieved = store.get(sha256, ext=".txt")
    assert retrieved == data

    # Missing file raises FileNotFoundError
    with pytest.raises(FileNotFoundError):
        store.get("0" * 64, ext=".txt")

    # Hash mismatch raises ValueError
    corrupted_sha = "a" * 64
    corrupted_path = tmp_path / corrupted_sha[:2] / corrupted_sha
    corrupted_path.parent.mkdir(parents=True, exist_ok=True)
    corrupted_path.write_bytes(b"tampered content")
    with pytest.raises(ValueError, match="Integrity check failed"):
        store.get(corrupted_sha)


def test_artifact_store_ext_rejections(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    data = b"Some bytes"

    # Slashes, colons, nulls, traversal
    with pytest.raises(ValueError, match="Invalid extension"):
        store.store(data, ext="/slash.txt")
    with pytest.raises(ValueError, match="Invalid extension"):
        store.store(data, ext="..\\evil.txt")
    with pytest.raises(ValueError, match="Invalid extension"):
        store.store(data, ext="C:file.txt")
    with pytest.raises(ValueError, match="Invalid extension"):
        store.store(data, ext="file\0.txt")


def test_artifact_store_existing_integrity_and_exclusive_create(tmp_path: Path):
    store = ArtifactStore(tmp_path)
    data = b"Test deterministic storage"
    sha256 = store.store(data, ext=".bin")

    # Storing identical content again succeeds and validates
    assert store.store(data, ext=".bin") == sha256

    # Tampering with existing file on disk causes next store() to detect corruption
    target = store.path_for(sha256, ext=".bin")
    target.write_bytes(b"corrupted contents")
    with pytest.raises(ValueError, match="Integrity check failed: existing artifact"):
        store.store(data, ext=".bin")


def test_zip_safe_extraction(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    target_dir = tmp_path / "extracted"

    valid_zip = make_zip(
        {
            "folder/file1.txt": b"first file",
            "file2.json": b'{"key": "value"}',
        }
    )

    extracted_paths = store.extract_zip_safe(valid_zip, target_dir)
    assert len(extracted_paths) == 2
    assert (target_dir / "folder" / "file1.txt").read_bytes() == b"first file"
    assert (target_dir / "file2.json").read_bytes() == b'{"key": "value"}'


def test_zip_slip_rejection(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    target_dir = tmp_path / "extracted"

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("../evil.txt", b"malicious content")
    evil_zip = buf.getvalue()

    with pytest.raises(ValueError, match="[Pp]ath traversal|[Uu]nsafe"):
        store.extract_zip_safe(evil_zip, target_dir)


def test_zip_casefold_and_overwrite_rejection(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    target_dir = tmp_path / "extracted"

    # Casefold collision in archive (e.g. File.txt and file.txt)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("MyFile.txt", b"case 1")
        zf.writestr("myfile.txt", b"case 2")
    collision_zip = buf.getvalue()

    with pytest.raises(ValueError, match="Duplicate or casefold collision"):
        store.extract_zip_safe(collision_zip, target_dir)

    # Pre-existing file in target_dir prevents overwrite
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "already_exists.txt").write_bytes(b"existing")
    single_zip = make_zip({"already_exists.txt": b"new"})
    with pytest.raises(ValueError, match="Zip extraction would overwrite existing file"):
        store.extract_zip_safe(single_zip, target_dir)


def test_zip_bomb_file_count(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    target_dir = tmp_path / "extracted"

    zip_bytes = make_zip({f"file_{i}.txt": b"x" for i in range(5)})
    with pytest.raises(ValueError, match="[Ff]ile count limit"):
        store.extract_zip_safe(zip_bytes, target_dir, max_files=3)


def test_zip_bomb_single_size(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    target_dir = tmp_path / "extracted"

    large_content = b"0" * 1024
    zip_bytes = make_zip({"large.bin": large_content})
    with pytest.raises(ValueError, match="[Ss]ingle file size limit"):
        store.extract_zip_safe(zip_bytes, target_dir, max_single_size=500)


def test_zip_bomb_total_size(tmp_path: Path):
    store = ArtifactStore(tmp_path / "artifacts")
    target_dir = tmp_path / "extracted"

    chunk = b"0" * 400
    zip_bytes = make_zip({f"f_{i}.bin": chunk for i in range(5)})
    with pytest.raises(ValueError, match="[Tt]otal uncompressed size limit"):
        store.extract_zip_safe(zip_bytes, target_dir, max_total_size=1000)


def test_xxe_safe_parsing_utf8_and_utf16():
    # Regular valid XML
    safe_xml = b"<root><child id='1'>value</child></root>"
    root = safe_parse_xml(safe_xml)
    assert root.tag == "root"
    assert root.find("child").text == "value"

    # UTF-8 XML with malicious ENTITY definition
    evil_utf8 = b"""<?xml version="1.0"?>
    <!DOCTYPE lolz [
     <!ENTITY lol "lol">
     <!ELEMENT lolz (#PCDATA)>
     <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
    ]>
    <lolz>&lol1;</lolz>"""
    with pytest.raises(ValueError, match="[Ff]orbidden (entity|DOCTYPE)"):
        safe_parse_xml(evil_utf8)

    # UTF-16 LE XML entity bypass attempt
    evil_utf16 = """<?xml version="1.0" encoding="UTF-16"?>
    <!DOCTYPE test [
      <!ENTITY xxe "evil">
    ]>
    <test>&xxe;</test>""".encode("utf-16-le")
    with pytest.raises(ValueError, match="[Ff]orbidden (entity|DOCTYPE)"):
        safe_parse_xml(evil_utf16)


# ---------------------------------------------------------------------------
# Normalization Tests
# ---------------------------------------------------------------------------


def test_normalize_financial_amount():
    res1 = normalize_financial_amount("1,234,567,890")
    assert res1["raw"] == "1,234,567,890"
    assert res1["normalized"] == "1234567890"
    assert res1["currency"] == "KRW"
    assert res1["kind"] == "currency_amount"

    # Parentheses negative
    res2 = normalize_financial_amount("(50,000)")
    assert res2["normalized"] == "-50000"

    # Standard negative string with decimals
    res3 = normalize_financial_amount("-12500.50", currency="USD")
    assert res3["normalized"] == "-12500.50"
    assert res3["currency"] == "USD"

    # Decimal input
    res_dec = normalize_financial_amount(Decimal("9876543210.12"))
    assert res_dec["normalized"] == "9876543210.12"

    # KRW unit scaling
    res_won = normalize_financial_amount("100", unit_raw="원")
    assert res_won["normalized"] == "100"

    res_thousand = normalize_financial_amount("100", unit_raw="천원")
    assert res_thousand["normalized"] == "100000"

    res_million = normalize_financial_amount("100", unit_raw="백만원")
    assert res_million["normalized"] == "100000000"

    res_billion = normalize_financial_amount("100", unit_raw="억원")
    assert res_billion["normalized"] == "10000000000"

    res_trillion = normalize_financial_amount("2", unit_raw="조원")
    assert res_trillion["normalized"] == "2000000000000"

    # Rejection of float, bool, NaN, Infinity
    with pytest.raises(ValueError, match="Float inputs are rejected"):
        normalize_financial_amount(12500.50)

    with pytest.raises(ValueError, match="Boolean inputs are rejected"):
        normalize_financial_amount(True)

    with pytest.raises(ValueError, match="NaN or Infinite"):
        normalize_financial_amount(Decimal("NaN"))

    with pytest.raises(ValueError, match="NaN or Infinite"):
        normalize_financial_amount(Decimal("Infinity"))

    # Empty / dash
    res4 = normalize_financial_amount("-")
    assert res4["normalized"] is None

    res5 = normalize_financial_amount(None)
    assert res5["normalized"] is None


def test_normalize_entity_set():
    entities = [" Samsung Electronics ", "Apple Inc.", "Samsung Electronics", " Google "]
    res = normalize_entity_set(entities)
    assert res["kind"] == "entity_set"
    assert res["unit"] == "entity"
    parsed_norm = json.loads(res["normalized"])
    assert parsed_norm == ["Apple Inc.", "Google", "Samsung Electronics"]

    # Rejects empty or non-string entities without silent drops
    with pytest.raises(ValueError, match="Invalid entity identifier"):
        normalize_entity_set(["Apple", "   ", "Google"])

    with pytest.raises(ValueError, match="Invalid entity identifier"):
        normalize_entity_set(["Apple", None, "Google"])


def test_normalize_period():
    res = normalize_period("20240101", "20241231")
    assert res["normalized"] == "2024-01-01/2024-12-31"
    assert res["kind"] == "period"

    res_iso = normalize_period("2024-01-01", "2024-12-31")
    assert res_iso["normalized"] == "2024-01-01/2024-12-31"

    # Start date after end date rejected
    with pytest.raises(ValueError, match="start date .* cannot be after end date"):
        normalize_period("2024-12-31", "2024-01-01")

    # Invalid calendar dates rejected
    with pytest.raises(ValueError):
        normalize_period("2024-02-30", "2024-03-01")


# ---------------------------------------------------------------------------
# Collection Manifest Builder & Schema Validation
# ---------------------------------------------------------------------------


def test_collection_manifest_conforms_to_schema(manifest_schema: dict):
    dummy_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    entry1 = create_artifact_entry(
        source_id="dart-fs-2024",
        document_version_id="fs-v1",
        corp_code="00126380",
        fy=2024,
        artifact_sha256=dummy_sha,
        rcept_no="20250314000123",
        consolidation="consolidated",
        source_system="DART",
        locator="fnlttSinglAcntAll/CFS",
        status="retrieved",
        fetched_at="2026-09-21T00:00:00Z",
    )

    entry2 = create_artifact_entry(
        source_id="dart-xbrl-2024",
        document_version_id="xbrl-v1",
        corp_code="00126380",
        fy=2024,
        artifact_sha256=None,
        rcept_no="20250314000123",
        consolidation="unknown",
        source_system="DART",
        locator=None,
        status="not_available",
        error_code="013",
        fetched_at="2026-09-21T00:00:00Z",
    )

    manifest = build_collection_manifest(
        manifest_id="manifest-00126380-2024",
        package_id="pkg-00126380-2024",
        artifacts=[entry1, entry2],
        synthetic=False,
        fetched_at="2026-09-21T00:00:00Z",
    )

    # Validate against formal JSON schema
    jsonschema.validate(instance=manifest, schema=manifest_schema)
    assert manifest["schema_version"] == "collection-1"
    assert manifest["manifest_id"] == "manifest-00126380-2024"
    assert len(manifest["artifacts"]) == 2


def test_collection_manifest_state_rules():
    dummy_sha = "a" * 64

    # retrieved requires non-null sha
    with pytest.raises(ValueError, match="requires non-null artifact_sha256"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            artifact_sha256=None,
            locator="loc",
            status="retrieved",
        )

    # retrieved requires non-null locator
    with pytest.raises(ValueError, match="requires non-null locator"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            artifact_sha256=dummy_sha,
            locator=None,
            status="retrieved",
        )

    # retrieved requires null error_code
    with pytest.raises(ValueError, match="requires null error_code"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            artifact_sha256=dummy_sha,
            locator="loc",
            status="retrieved",
            error_code="ERR",
        )

    # not_available requires null artifact_sha256
    with pytest.raises(ValueError, match="requires null artifact_sha256"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            artifact_sha256=dummy_sha,
            locator=None,
            status="not_available",
            error_code="013",
        )

    # not_available requires null locator
    with pytest.raises(ValueError, match="requires null locator"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            artifact_sha256=None,
            locator="loc",
            status="not_available",
            error_code="013",
        )

    # not_available requires non-empty error_code
    with pytest.raises(ValueError, match="requires non-empty error_code"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            artifact_sha256=None,
            locator=None,
            status="not_available",
            error_code=None,
        )


def test_collection_manifest_rejects_duplicate_sources():
    dummy_sha = "b" * 64
    entry1 = create_artifact_entry(
        source_id="duplicate-source-id",
        document_version_id="v1",
        corp_code="00126380",
        fy=2024,
        artifact_sha256=dummy_sha,
        locator="loc1",
        status="retrieved",
    )
    entry2 = create_artifact_entry(
        source_id="duplicate-source-id",
        document_version_id="v2",
        corp_code="00126380",
        fy=2024,
        artifact_sha256=dummy_sha,
        locator="loc2",
        status="retrieved",
    )

    with pytest.raises(ValueError, match="Duplicate source_id in manifest artifacts"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[entry1, entry2],
        )


def test_end_to_end_collection_cli_integration(tmp_path: Path, manifest_schema: dict):
    from evaluation.reconciliation_collect import collect

    store = ArtifactStore(tmp_path / "artifacts")
    stmt_payload = {
        "status": "000",
        "message": "정상",
        "list": [
            {
                "rcept_no": "20250314000123",
                "corp_code": "00126380",
                "bsns_year": "2024",
                "reprt_code": "11011",
                "fs_div": "CFS",
                "account_nm": "매출액",
                "thstrm_amount": "300000000000000",
            }
        ],
    }
    fake_zip = make_zip({"doc.xml": b"<xml>content</xml>"})
    transport = MockTransport(
        [
            (200, {"Content-Type": "application/json"}, json.dumps(stmt_payload).encode()),
            (200, {"Content-Type": "application/octet-stream"}, fake_zip),
            (200, {"Content-Type": "application/octet-stream"}, fake_zip),
        ]
    )
    client = DartClient(api_key="test_key", transport=transport)

    manifest = collect(
        client,
        store,
        corp_code="00126380",
        fy=2024,
        rcept_no="20250314000123",
        package_id="pkg-test",
        manifest_id="man-test",
        document_version_id="doc-test",
        kinds=["statements", "document", "xbrl"],
    )

    # Validate resulting manifest against collection_manifest.schema.json
    jsonschema.validate(instance=manifest, schema=manifest_schema)
    assert manifest["schema_version"] == "collection-1"
    assert len(manifest["artifacts"]) == 3
    for art in manifest["artifacts"]:
        assert art["status"] == "retrieved"
        assert art["locator"] is not None
        assert (store.root / art["locator"]).is_file()


def test_normalize_financial_amount_high_precision_and_plain_notation():
    # Number with 35 digits: must preserve all digits without scientific notation
    large_num = "12345678901234567890123456789012345"
    res = normalize_financial_amount(large_num)
    assert res["normalized"] == large_num
    assert "e" not in res["normalized"].lower()

    # Scale multiplication on large number
    res_scaled = normalize_financial_amount(large_num, unit_raw="천원")
    assert res_scaled["normalized"] == large_num + "000"

    # Rejection of unknown units
    with pytest.raises(ValueError, match="Unknown or unapproved unit"):
        normalize_financial_amount("100", unit_raw="unknown_unit")

    with pytest.raises(ValueError, match="Unknown or unapproved unit"):
        normalize_financial_amount("100", currency="USD", unit_raw="won")


def test_manifest_strict_bool_and_nested_validation():
    entry = create_artifact_entry(
        source_id="s1",
        document_version_id="v1",
        corp_code="00126380",
        fy=2024,
        artifact_sha256="c" * 64,
        locator="loc",
        status="retrieved",
        fetched_at="2026-09-21T00:00:00Z",
    )

    # synthetic must be strict bool
    with pytest.raises(ValueError, match="synthetic must be a strict boolean"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[entry],
            synthetic=1,  # int, not bool
        )

    with pytest.raises(ValueError, match="synthetic must be a strict boolean"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[entry],
            synthetic="true",  # str, not bool
        )

    # Nested artifact missing required keys
    bad_entry = dict(entry)
    del bad_entry["corp_code"]
    with pytest.raises(ValueError, match="missing required keys"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[bad_entry],
        )

    # Nested artifact with extra keys
    bad_entry_extra = dict(entry)
    bad_entry_extra["unapproved_key"] = "evil"
    with pytest.raises(ValueError, match="invalid extra keys"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[bad_entry_extra],
        )


def test_manifest_time_ordering():
    # Artifact timestamp after manifest timestamp rejected
    entry_future = create_artifact_entry(
        source_id="s1",
        document_version_id="v1",
        corp_code="00126380",
        fy=2024,
        artifact_sha256="d" * 64,
        locator="loc",
        status="retrieved",
        fetched_at="2026-09-21T12:00:00Z",  # 12:00
    )

    with pytest.raises(ValueError, match="cannot be after manifest fetched_at"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[entry_future],
            fetched_at="2026-09-21T10:00:00Z",  # Earlier: 10:00
        )


def test_safe_parse_xml_declared_encoding_dtd_rejection():
    # XML declaring ISO-8859-1 with DOCTYPE
    xml_iso = (
        b"<?xml version='1.0' encoding='ISO-8859-1'?>\n"
        b"<!DOCTYPE test SYSTEM 'http://evil.com/xxe'>\n"
        b"<test>data</test>"
    )
    with pytest.raises(ValueError, match="Forbidden DOCTYPE declaration"):
        safe_parse_xml(xml_iso)

    # XML declaring windows-1252 with ENTITY
    xml_win = (
        b"<?xml version='1.0' encoding='windows-1252'?>\n"
        b"<!DOCTYPE test [\n"
        b"  <!ENTITY myent 'injected'>\n"
        b"]>\n"
        b"<test>&myent;</test>"
    )
    with pytest.raises(ValueError, match="Forbidden DOCTYPE declaration|Forbidden entity"):
        safe_parse_xml(xml_win)


def test_create_artifact_entry_bool_fy_and_blank_fields_rejected():
    # fy as boolean must be rejected
    with pytest.raises(ValueError, match="fy must be an integer"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=True,  # bool rejected
            artifact_sha256="c" * 64,
            locator="loc",
            status="retrieved",
        )

    with pytest.raises(ValueError, match="fy must be an integer"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=False,  # bool rejected
            artifact_sha256="c" * 64,
            locator="loc",
            status="retrieved",
        )

    # Blank locator rejected for retrieved status
    with pytest.raises(ValueError, match="locator cannot be blank|non-blank locator"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            artifact_sha256="c" * 64,
            locator="   ",  # blank string rejected
            status="retrieved",
        )

    # Blank error_code rejected for failed status
    with pytest.raises(ValueError, match="error_code cannot be blank|non-blank error_code"):
        create_artifact_entry(
            source_id="s1",
            document_version_id="v1",
            corp_code="00126380",
            fy=2024,
            error_code="   ",  # blank string rejected
            status="failed",
        )


def test_safe_parse_xml_utf32_bom_dtd_rejection():
    # UTF-32-LE with DOCTYPE
    xml_text = "<?xml version='1.0'?><!DOCTYPE root><root/>"
    utf32le_bytes = xml_text.encode("utf-32-le")
    with pytest.raises(ValueError, match="Forbidden DOCTYPE declaration"):
        safe_parse_xml(utf32le_bytes)

    # UTF-32-BE with DOCTYPE
    utf32be_bytes = xml_text.encode("utf-32-be")
    with pytest.raises(ValueError, match="Forbidden DOCTYPE declaration"):
        safe_parse_xml(utf32be_bytes)


def test_normalize_financial_amount_raw_unit_and_canonical_currency():
    res = normalize_financial_amount("100", unit_raw="천원")
    assert res["raw_unit"] == "천원"
    assert res["unit"] == "KRW"
    assert res["currency"] == "KRW"
    assert res["normalized"] == "100000"


def test_artifact_store_hardlink_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    store = ArtifactStore(tmp_path)
    data = b"content to fail closed"

    def mock_link_fail(src: Any, dst: Any) -> None:
        raise OSError("Cross-device link forbidden or unsupported")

    monkeypatch.setattr(os, "link", mock_link_fail)
    with pytest.raises(RuntimeError, match="Atomic publish failed.*Failing closed"):
        store.store(data)

    # Verify temp dir has no leftover files
    assert list(store.tmp_dir.glob("*.tmp")) == []


def test_nested_artifact_non_string_types_rejected():
    entry = create_artifact_entry(
        source_id="s1",
        document_version_id="v1",
        corp_code="00126380",
        fy=2024,
        artifact_sha256="c" * 64,
        locator="loc",
        status="retrieved",
        fetched_at="2026-09-21T00:00:00Z",
    )

    # Int corp_code in entry dictionary rejected
    bad_corp_entry = dict(entry)
    bad_corp_entry["corp_code"] = 126380
    with pytest.raises(ValueError, match="corp_code must be a string|invalid value or type"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[bad_corp_entry],
        )

    # Int rcept_no in entry dictionary rejected
    bad_rcept_entry = dict(entry)
    bad_rcept_entry["rcept_no"] = 20240101000001
    with pytest.raises(ValueError, match="rcept_no must be a string|invalid value or type"):
        build_collection_manifest(
            manifest_id="m1",
            package_id="p1",
            artifacts=[bad_rcept_entry],
        )
