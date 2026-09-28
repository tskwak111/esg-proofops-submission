"""OpenDART API client with bounded retries, injectable transport, and API key redaction.

Endpoints implemented:
- /api/list.json: List disclosure filings.
- /api/fnlttSinglAcntAll.json: Full financial statements for a single company.
- /api/fnlttXbrl.xml: XBRL financial statements archive.
- /api/document.xml: Original disclosure document archive.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Iterator, Mapping
from copy import deepcopy
from datetime import datetime
from typing import Any, Protocol

from proofops.adapters.dart.artifacts import safe_parse_xml

_CORP_CODE_RE = re.compile(r"^[0-9]{8}$")
_DATE_8_RE = re.compile(r"^[0-9]{8}$")
_YEAR_4_RE = re.compile(r"^[0-9]{4}$")
_RCEPT_NO_RE = re.compile(r"^[0-9]{14}$")

VALID_REPRT_CODES = {"11011", "11012", "11013", "11014"}
VALID_FS_DIV = {"CFS", "OFS"}

MAX_BODY_BYTES = 50 * 1024 * 1024  # 50 MB bound for network downloads


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value: str) -> Any:
    raise ValueError("non-finite JSON number")


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("non-finite JSON number")
    return parsed


def redact_api_key(text: str, api_key: str | None = None) -> str:
    """Scrub crtfc_key and explicit API key from text, URLs, and exception messages."""
    if not text:
        return ""
    scrubbed = re.sub(r"(crtfc_key=)[^& \t\r\n]+", r"\1[REDACTED]", text, flags=re.IGNORECASE)
    if api_key and api_key.strip():
        scrubbed = scrubbed.replace(api_key.strip(), "[REDACTED]")
    return scrubbed


class DartError(Exception):
    """Base exception for all DART adapter errors, ensuring API key scrubbing."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_key: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.sanitized_message = redact_api_key(message, api_key)
        super().__init__(self.sanitized_message)

    def __str__(self) -> str:
        return self.sanitized_message

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.sanitized_message!r})"


class DartAuthError(DartError):
    """Raised when authentication fails (status 010, 011, 012, HTTP 401/403)."""


class DartRateLimitError(DartError):
    """Raised when DART rate limit or daily quota is exceeded (status 020, HTTP 429)."""


class DartNotFoundError(DartError):
    """Raised when requested filing or artifact does not exist (status 013, 014)."""


class DartResponse(Mapping[str, Any]):
    """Immutable representation of an OpenDART response preserving wire bytes and parsed data."""

    def __init__(
        self,
        *,
        status_code: int,
        headers: dict[str, str],
        raw_bytes: bytes,
        data: dict[str, Any] | None = None,
        endpoint: str = "",
        rcept_no: str | None = None,
    ) -> None:
        self._status_code = status_code
        self._headers = dict(headers)
        self._raw_bytes = raw_bytes
        self._data = deepcopy(data) if data is not None else {}
        self._endpoint = endpoint
        self._rcept_no = rcept_no

    @property
    def status_code(self) -> int:
        return self._status_code

    @property
    def headers(self) -> dict[str, str]:
        return dict(self._headers)

    @property
    def raw_bytes(self) -> bytes:
        """Exact raw response bytes as received over the wire."""
        return self._raw_bytes

    @property
    def data(self) -> dict[str, Any]:
        return deepcopy(self._data)

    @property
    def endpoint(self) -> str:
        return self._endpoint

    @property
    def rcept_no(self) -> str | None:
        return self._rcept_no

    def __getitem__(self, key: str) -> Any:
        return deepcopy(self._data[key])

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        return deepcopy(self._data.get(key, default))

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __repr__(self) -> str:
        return (
            f"<DartResponse endpoint={self._endpoint!r} status={self.get('status')} "
            f"bytes={len(self._raw_bytes)}>"
        )


class Transport(Protocol):
    """Protocol for pluggable HTTP transport."""

    def __call__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> tuple[int, dict[str, str], bytes]:
        """Execute HTTP GET request returning (status_code, headers, body_bytes)."""
        ...


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Disallows HTTP redirects to prevent credential leaks to third parties."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise ValueError(
            f"HTTP redirects are forbidden to prevent credential leak: {code} -> {newurl}"
        )


def _read_bounded(stream: Any, max_bytes: int) -> bytes:
    """Read stream with bounded maximum byte limit."""
    data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError(f"Response body exceeded maximum allowed limit of {max_bytes} bytes")
    return data


def default_stdlib_transport(
    url: str,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, dict[str, str], bytes]:
    """Default secure transport using Python standard library urllib.

    Enforces HTTPS origin opendart.fss.or.kr, rejects redirects, and bounds response reads.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme.lower() != "https" or parsed.netloc.lower() != "opendart.fss.or.kr":
        raise ValueError(
            f"Disallowed origin for real network traffic: {parsed.scheme}://{parsed.netloc}. "
            "Real network requests must target https://opendart.fss.or.kr"
        )

    req_headers = {"User-Agent": "ProofOps-DartAdapter/1.0"}
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(url, headers=req_headers, method="GET")
    opener = urllib.request.build_opener(_NoRedirectHandler)

    try:
        with opener.open(req, timeout=timeout) as resp:
            status_code = resp.status
            resp_headers = {k: v for k, v in resp.headers.items()}
            body = _read_bounded(resp, MAX_BODY_BYTES)
            return status_code, resp_headers, body
    except urllib.error.HTTPError as e:
        status_code = e.code
        resp_headers = {k: v for k, v in e.headers.items()} if e.headers else {}
        body = _read_bounded(e, MAX_BODY_BYTES) if hasattr(e, "read") else b""
        return status_code, resp_headers, body


class DartClient:
    """Bounded, defensive OpenDART API client."""

    def __init__(
        self,
        api_key: str = "",
        *,
        transport: Transport | None = None,
        base_url: str = "https://opendart.fss.or.kr",
        timeout: float = 10.0,
        max_retries: int = 3,
        retry_backoff: float = 0.5,
    ) -> None:
        # Validate timeout, retries, and backoff
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError(f"timeout must be finite positive number <= 60.0, got {timeout!r}")

        if type(max_retries) is not int or max_retries < 0 or max_retries > 10:
            raise ValueError(
                f"max_retries must be an integer between 0 and 10, got {max_retries!r}"
            )

        if (
            type(retry_backoff) not in (int, float)
            or not math.isfinite(retry_backoff)
            or not 0 <= retry_backoff <= 30
        ):
            raise ValueError(
                f"retry_backoff must be finite number between 0.0 and 30.0, got {retry_backoff!r}"
            )

        clean_base = base_url.rstrip("/")
        if transport is None:
            if clean_base != "https://opendart.fss.or.kr":
                raise ValueError("Real network requests must target https://opendart.fss.or.kr")

        resolved_key = api_key or os.environ.get("DART_API_KEY", "")
        self._api_key = resolved_key
        self._base_url = clean_base
        self._transport = transport or default_stdlib_transport
        self._timeout = float(timeout)
        self._max_retries = max_retries
        self._retry_backoff = float(retry_backoff)

    @property
    def api_key(self) -> str:
        return self._api_key

    @property
    def base_url(self) -> str:
        return self._base_url

    def __repr__(self) -> str:
        # Crucial: NEVER display API key in repr
        return f"<DartClient base_url={self._base_url!r}>"

    def _execute_http(self, url: str) -> tuple[int, dict[str, str], bytes]:
        """Execute request with bounded retries and exponential backoff for 5xx server errors."""
        attempt = 0
        while True:
            try:
                status_code, headers, body = self._transport(
                    url,
                    headers={"Accept": "*/*"},
                    timeout=self._timeout,
                )
            except Exception as exc:
                if attempt >= self._max_retries:
                    sanitized_exc_msg = redact_api_key(str(exc), self._api_key)
                    raise DartError(
                        f"HTTP transport failed after {attempt} retries: {sanitized_exc_msg}",
                        api_key=self._api_key,
                    ) from None
                attempt += 1
                time.sleep(min(30, self._retry_backoff * (2 ** (attempt - 1))))
                continue

            if (
                type(status_code) is not int
                or not isinstance(body, bytes)
                or len(body) > MAX_BODY_BYTES
            ):
                raise DartError("Invalid or oversized HTTP response")
            if status_code == 429:
                raise DartRateLimitError(
                    f"HTTP 429 Rate limited for {url}",
                    status_code=429,
                    api_key=self._api_key,
                )

            if 500 <= status_code < 600:
                if attempt >= self._max_retries:
                    raise DartError(
                        f"HTTP {status_code} server error for {url}: {body[:200]!r}",
                        status_code=status_code,
                        api_key=self._api_key,
                    )
                attempt += 1
                time.sleep(min(30, self._retry_backoff * (2 ** (attempt - 1))))
                continue

            return status_code, headers, body

    def _request_json(self, endpoint: str, params: dict[str, Any]) -> DartResponse:
        """Request JSON endpoint and handle common DART status codes."""
        query_params = {"crtfc_key": self._api_key}
        for k, v in params.items():
            if v is not None:
                query_params[k] = str(v)

        url = f"{self._base_url}{endpoint}?{urllib.parse.urlencode(query_params)}"
        status_code, headers, body = self._execute_http(url)

        if status_code in (401, 403):
            raise DartAuthError(
                f"HTTP {status_code} authentication error for {url}",
                status_code=status_code,
                api_key=self._api_key,
            )

        if status_code != 200:
            raise DartError(
                f"HTTP {status_code} error for {url}: {body[:200]!r}",
                status_code=status_code,
                api_key=self._api_key,
            )

        try:
            data = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_json_pairs,
                parse_constant=_nonfinite,
                parse_float=_finite_float,
            )
            if not isinstance(data, dict):
                raise ValueError("JSON object required")
        except Exception:
            raise DartError(
                f"Failed to parse JSON response from {url}: {body[:200]!r}",
                api_key=self._api_key,
            ) from None

        dart_status = data.get("status")
        dart_message = data.get("message", "")

        if dart_status in ("010", "011", "012", "901"):
            raise DartAuthError(
                f"DART authentication failed: [{dart_status}] {dart_message}",
                status_code=status_code,
                api_key=self._api_key,
            )
        if dart_status == "014":
            raise DartNotFoundError(
                f"DART artifact not found: [{dart_status}] {dart_message}",
                status_code=status_code,
                api_key=self._api_key,
            )
        if dart_status == "020":
            raise DartRateLimitError(
                f"DART rate limit/quota exceeded: [{dart_status}] {dart_message}",
                status_code=status_code,
                api_key=self._api_key,
            )

        # Invariant: JSON statuses other than 000 and 013 must fail
        if dart_status not in ("000", "013"):
            raise DartError(
                f"DART error response: [{dart_status}] {dart_message}",
                status_code=status_code,
                api_key=self._api_key,
            )

        return DartResponse(
            status_code=status_code,
            headers=headers,
            raw_bytes=body,
            data=data,
            endpoint=endpoint,
        )

    def _request_zip(self, endpoint: str, params: dict[str, Any]) -> bytes:
        """Request binary zip archive or parse error XML/JSON."""
        query_params = {"crtfc_key": self._api_key}
        for k, v in params.items():
            if v is not None:
                query_params[k] = str(v)

        url = f"{self._base_url}{endpoint}?{urllib.parse.urlencode(query_params)}"
        status_code, _, body = self._execute_http(url)

        if status_code in (401, 403):
            raise DartAuthError(
                f"HTTP {status_code} authentication error for {url}",
                status_code=status_code,
                api_key=self._api_key,
            )

        if status_code != 200:
            raise DartError("DART archive HTTP failure", status_code=status_code)
        # Validate the archive directory; a PK prefix alone is insufficient.
        if status_code == 200:
            if (
                body.startswith(b"PK\x03\x04")
                or body.startswith(b"PK\x05\x06")
                or body.startswith(b"PK\x07\x08")
            ):
                try:
                    with zipfile.ZipFile(io.BytesIO(body)) as archive:
                        entries = archive.infolist()
                        if (
                            not entries
                            or len(entries) > 1000
                            or sum(e.file_size for e in entries) > MAX_BODY_BYTES
                        ):
                            raise ValueError("archive size invalid")
                except (zipfile.BadZipFile, ValueError):
                    raise DartError("Invalid or oversized DART archive") from None
                return body

        # If not a zip, DART returns an XML error or JSON error
        try:
            root = safe_parse_xml(body)
            status_elem = root.find("status")
            message_elem = root.find("message")
            dart_status = (
                status_elem.text.strip() if status_elem is not None and status_elem.text else ""
            )
            dart_message = (
                message_elem.text.strip() if message_elem is not None and message_elem.text else ""
            )

            if dart_status in ("010", "011", "012"):
                raise DartAuthError(
                    f"DART authentication failed: [{dart_status}] {dart_message}",
                    status_code=status_code,
                    api_key=self._api_key,
                )
            if dart_status in ("013", "014"):
                raise DartNotFoundError(
                    f"DART artifact not found: [{dart_status}] {dart_message}",
                    status_code=status_code,
                    api_key=self._api_key,
                )
            if dart_status == "020":
                raise DartRateLimitError(
                    f"DART rate limit/quota exceeded: [{dart_status}] {dart_message}",
                    status_code=status_code,
                    api_key=self._api_key,
                )

            raise DartError(
                f"DART error response: [{dart_status}] {dart_message}",
                status_code=status_code,
                api_key=self._api_key,
            )
        except DartError:
            raise
        except Exception:
            pass

        # Check if JSON error
        try:
            data = json.loads(body.decode("utf-8"))
            dart_status = data.get("status")
            dart_message = data.get("message", "")
            if dart_status in ("010", "011", "012"):
                raise DartAuthError(
                    f"DART authentication failed: [{dart_status}] {dart_message}",
                    status_code=status_code,
                    api_key=self._api_key,
                )
            if dart_status in ("013", "014"):
                raise DartNotFoundError(
                    f"DART artifact not found: [{dart_status}] {dart_message}",
                    status_code=status_code,
                    api_key=self._api_key,
                )
            if dart_status == "020":
                raise DartRateLimitError(
                    f"DART rate limit/quota exceeded: [{dart_status}] {dart_message}",
                    status_code=status_code,
                    api_key=self._api_key,
                )
            raise DartError(
                f"DART error response: [{dart_status}] {dart_message}",
                status_code=status_code,
                api_key=self._api_key,
            )
        except DartError:
            raise
        except Exception:
            pass

        raise DartError(
            f"HTTP {status_code} error or non-zip response from {url}: {body[:200]!r}",
            status_code=status_code,
            api_key=self._api_key,
        )

    def list_filings(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        *,
        pblntf_ty: str | None = None,
        pblntf_detail_ty: str | None = None,
        last_reprt_at: str = "Y",
        page_no: int = 1,
        page_count: int = 100,
    ) -> DartResponse:
        """Search disclosure filings (/api/list.json)."""
        clean_corp = str(corp_code).strip()
        if not _CORP_CODE_RE.match(clean_corp):
            raise ValueError(f"corp_code must be exactly 8 digits, got {corp_code!r}")

        clean_bgn = str(bgn_de).strip()
        if not _DATE_8_RE.match(clean_bgn):
            raise ValueError(f"bgn_de must be exactly 8 digits (YYYYMMDD), got {bgn_de!r}")

        clean_end = str(end_de).strip()
        if not _DATE_8_RE.match(clean_end):
            raise ValueError(f"end_de must be exactly 8 digits (YYYYMMDD), got {end_de!r}")

        begin = datetime.strptime(clean_bgn, "%Y%m%d").date()
        end = datetime.strptime(clean_end, "%Y%m%d").date()
        if begin > end:
            raise ValueError("filing date range reversed")
        if (
            type(page_no) is not int
            or page_no < 1
            or type(page_count) is not int
            or not 1 <= page_count <= 100
            or last_reprt_at not in {"Y", "N"}
        ):
            raise ValueError("invalid filing pagination or report selector")

        params = {
            "corp_code": clean_corp,
            "bgn_de": clean_bgn,
            "end_de": clean_end,
            "last_reprt_at": last_reprt_at,
            "page_no": page_no,
            "page_count": page_count,
        }
        if pblntf_ty is not None:
            params["pblntf_ty"] = pblntf_ty
        if pblntf_detail_ty is not None:
            params["pblntf_detail_ty"] = pblntf_detail_ty

        return self._request_json("/api/list.json", params)

    def get_financial_statements(
        self,
        corp_code: str,
        bsns_year: int | str,
        reprt_code: str = "11011",
        fs_div: str = "CFS",
        *,
        expected_rcept_no: str | None = None,
    ) -> DartResponse:
        """Retrieve full single company financial statements (/api/fnlttSinglAcntAll.json).

        Strictly validates that every row in the result contains all required fields
        (corp_code, bsns_year, reprt_code, rcept_no), matches expected parameters,
        and does not mix detected receipts. The official full-statement response
        does not require fs_div; it is bound by the request. If echoed, it must match.
        """
        clean_corp = str(corp_code).strip()
        if not _CORP_CODE_RE.match(clean_corp):
            raise ValueError(f"corp_code must be exactly 8 digits, got {corp_code!r}")

        clean_year = str(bsns_year).strip()
        if not _YEAR_4_RE.match(clean_year):
            raise ValueError(f"bsns_year must be 4 digits (YYYY), got {bsns_year!r}")

        clean_reprt = str(reprt_code).strip()
        if clean_reprt not in VALID_REPRT_CODES:
            raise ValueError(f"reprt_code must be one of {VALID_REPRT_CODES}, got {reprt_code!r}")

        clean_fs = str(fs_div).strip()
        if clean_fs not in VALID_FS_DIV:
            raise ValueError(f"fs_div must be one of {VALID_FS_DIV}, got {fs_div!r}")

        clean_pinned_rcept: str | None = None
        if expected_rcept_no is not None:
            clean_pinned_rcept = str(expected_rcept_no).strip()
            if not _RCEPT_NO_RE.match(clean_pinned_rcept):
                raise ValueError(
                    f"expected_rcept_no must be exactly 14 digits, got {expected_rcept_no!r}"
                )

        params = {
            "corp_code": clean_corp,
            "bsns_year": clean_year,
            "reprt_code": clean_reprt,
            "fs_div": clean_fs,
        }
        response = self._request_json("/api/fnlttSinglAcntAll.json", params)

        # Validate rows in list against requested params and pinned receipt
        rows = response.get("list")
        if response.get("status") == "000" and (not isinstance(rows, list) or not rows):
            raise DartError("Successful statement response missing rows")
        pinned_detected: str | None = clean_pinned_rcept
        if isinstance(rows, list) and rows:
            for idx, row in enumerate(rows):
                if not isinstance(row, dict):
                    raise ValueError(f"Statement row {idx} is not a dictionary")

                row_corp = row.get("corp_code")
                if not row_corp or str(row_corp).strip() != clean_corp:
                    raise ValueError(
                        f"Row {idx} corp_code missing or mismatch: expected {clean_corp}, "
                        f"got {row_corp!r}"
                    )

                row_year = row.get("bsns_year")
                if not row_year or str(row_year).strip() != clean_year:
                    raise ValueError(
                        f"Row {idx} bsns_year missing or mismatch: expected {clean_year}, "
                        f"got {row_year!r}"
                    )

                row_reprt = row.get("reprt_code")
                if not row_reprt or str(row_reprt).strip() != clean_reprt:
                    raise ValueError(
                        f"Row {idx} reprt_code missing or mismatch: expected {clean_reprt}, "
                        f"got {row_reprt!r}"
                    )

                row_fs = row.get("fs_div")
                if row_fs is not None and str(row_fs).strip() != clean_fs:
                    raise ValueError(
                        f"Row {idx} fs_div missing or mismatch: expected {clean_fs}, "
                        f"got {row_fs!r}"
                    )

                row_rcept = row.get("rcept_no")
                if not row_rcept or not str(row_rcept).strip():
                    raise ValueError(f"Row {idx} rcept_no is missing or empty")

                clean_row_rcept = str(row_rcept).strip()
                if not _RCEPT_NO_RE.fullmatch(clean_row_rcept):
                    raise ValueError("Invalid statement receipt format")
                if clean_pinned_rcept is not None:
                    if clean_row_rcept != clean_pinned_rcept:
                        raise ValueError(
                            f"Row {idx} pinned rcept_no mismatch: expected {clean_pinned_rcept}, "
                            f"got {clean_row_rcept}"
                        )
                else:
                    if pinned_detected is None:
                        pinned_detected = clean_row_rcept
                    elif pinned_detected != clean_row_rcept:
                        raise ValueError(
                            f"Mixed detected receipt numbers in statement rows: "
                            f"{pinned_detected} vs {clean_row_rcept}"
                        )

        return DartResponse(
            status_code=response.status_code,
            headers=response.headers,
            raw_bytes=response.raw_bytes,
            data=response.data,
            endpoint=response.endpoint,
            rcept_no=pinned_detected,
        )

    def download_xbrl(self, rcept_no: str, reprt_code: str = "11011") -> bytes:
        """Download XBRL financial statement zip package (/api/fnlttXbrl.xml)."""
        clean_rcept = str(rcept_no).strip()
        if not _RCEPT_NO_RE.match(clean_rcept):
            raise ValueError(f"rcept_no must be exactly 14 digits, got {rcept_no!r}")

        clean_reprt = str(reprt_code).strip()
        if clean_reprt not in VALID_REPRT_CODES:
            raise ValueError(f"reprt_code must be one of {VALID_REPRT_CODES}, got {reprt_code!r}")

        return self._request_zip(
            "/api/fnlttXbrl.xml",
            {"rcept_no": clean_rcept, "reprt_code": clean_reprt},
        )

    def download_document(self, rcept_no: str) -> bytes:
        """Download disclosure document original zip package (/api/document.xml)."""
        clean_rcept = str(rcept_no).strip()
        if not _RCEPT_NO_RE.match(clean_rcept):
            raise ValueError(f"rcept_no must be exactly 14 digits, got {rcept_no!r}")

        return self._request_zip("/api/document.xml", {"rcept_no": clean_rcept})
