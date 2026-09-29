"""Bounded Upstage Document Parse transport; shares ledger with text probe.

Uses the shared SQLite ledger/policy (USD 1 per-call reservation) via UpstageProbe.
The synchronous probe accepts 1..10 pages; async accepts a PDF whose priced
pages fit one reservation. Fixed host api.upstage.ai with pinned model
document-parse-260128, mode, ocr=auto, coordinates=true,
output_formats=[text,html]. No retries/redirects, bounded response, sanitized
errors. Pricing 2026-09-12 standard 0.01/page enhanced 0.03/page +10% VAT.
Rates rechecked 2026-09-25 at https://www.upstage.ai/pricing/api; expiry 2026-10-02.
No quality approval or graph conversion.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic, sleep
from urllib.parse import urlsplit

from proofops.adapters.local.upstage import PRICE_RECHECK_AT, UpstageProbe
from proofops.domain.provenance import canonical_hash

PARSE_MODEL_PINNED = "document-parse-260128"
PARSE_MODEL_ALIAS = "document-parse"
ALLOWED_MODELS = {PARSE_MODEL_PINNED, PARSE_MODEL_ALIAS}
ALLOWED_MODES = {"standard", "enhanced"}
MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_PAGES = 10
MIN_PAGES = 1
MAX_RESPONSE_BYTES = 1_048_576

COST_PER_PAGE = {
    "standard": Decimal("0.011"),
    "enhanced": Decimal("0.033"),
}

PRICE_SNAPSHOT = {
    "snapshot_id": "upstage-document-parse-2026-09-12",
    "model": PARSE_MODEL_PINNED,
    "captured_at": "2026-09-12T00:00:00Z",
    "standard_per_page": "0.01",
    "enhanced_per_page": "0.03",
    "vat_multiplier": "1.10",
    "region": "provider-managed-unverified",
}


class UpstageParseProbe(UpstageProbe):
    """Local evaluation transport for Document Parse; not production approval."""

    def _validate_pdf(self, pdf_bytes: bytes) -> int:
        if not isinstance(pdf_bytes, bytes):
            raise ValueError("INVALID_PROBE_REQUEST")
        if len(pdf_bytes) == 0 or len(pdf_bytes) > MAX_PDF_BYTES:
            raise ValueError("INVALID_PROBE_REQUEST")
        # actual PDF parsing with pypdf
        try:
            import pypdf  # type: ignore

            reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
            if getattr(reader, "is_encrypted", False):
                raise ValueError("INVALID_PROBE_REQUEST")
            num_pages = len(reader.pages)
        except Exception:
            raise ValueError("INVALID_PROBE_REQUEST") from None
        if not (MIN_PAGES <= num_pages <= MAX_PAGES):
            raise ValueError("INVALID_PROBE_REQUEST")
        return num_pages

    def _post_parse(self, pdf_bytes: bytes, mode: str, path="/v1/document-digitization") -> dict:
        boundary = uuid.uuid4().hex
        # Build multipart body
        # Fields: document (file), model, mode, ocr, coordinates, output_formats
        body_parts: list[bytes] = []

        def add_field(name: str, value: str) -> None:
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode())
            body_parts.append(f"{value}\r\n".encode())

        def add_file(name: str, filename: str, content: bytes, content_type: str) -> None:
            body_parts.append(f"--{boundary}\r\n".encode())
            body_parts.append(
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'.encode()
            )
            body_parts.append(f"Content-Type: {content_type}\r\n\r\n".encode())
            body_parts.append(content)
            body_parts.append(b"\r\n")

        add_file("document", "document.pdf", pdf_bytes, "application/pdf")
        add_field("model", PARSE_MODEL_PINNED)
        add_field("mode", mode)
        add_field("ocr", "auto")
        add_field("coordinates", "true")
        add_field("output_formats", '["text","html"]')

        body_parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(body_parts)

        connection = http.client.HTTPSConnection("api.upstage.ai", timeout=60)
        try:
            connection.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"UPSTAGE_HTTP_{response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("UPSTAGE_RESPONSE_TOO_LARGE")
            return json.loads(raw)
        finally:
            connection.close()

    def _async_status(self, provider_id: str) -> dict:
        connection = http.client.HTTPSConnection("api.upstage.ai", timeout=30)
        try:
            connection.request(
                "GET",
                f"/v1/document-digitization/requests/{provider_id}",
                headers={"Authorization": "Bearer " + self._api_key},
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"UPSTAGE_HTTP_{response.status}")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("UPSTAGE_RESPONSE_TOO_LARGE")
            return json.loads(raw)
        finally:
            connection.close()

    @staticmethod
    def _async_download(url: str) -> dict:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".files.upstage.ai")
            or parsed.username
            or parsed.password
            or parsed.port
        ):
            raise ValueError("UPSTAGE_DOWNLOAD_URL_INVALID")
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=30)
        try:
            connection.request("GET", parsed.path + ("?" + parsed.query if parsed.query else ""))
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"UPSTAGE_DOWNLOAD_HTTP_{response.status}")
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise ValueError("UPSTAGE_RESPONSE_TOO_LARGE")
            return json.loads(raw)
        finally:
            connection.close()

    def parse_async(self, pdf_bytes: bytes, *, request_id: str, mode: str = "standard") -> dict:
        """One ledger-reserved async job; failures retain the reservation."""
        if datetime.now(UTC) >= PRICE_RECHECK_AT:
            raise ValueError("PRICE_RECHECK_REQUIRED")
        if (
            mode not in ALLOWED_MODES
            or not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
        ):
            raise ValueError("INVALID_PROBE_REQUEST")
        if not isinstance(pdf_bytes, bytes) or not 0 < len(pdf_bytes) <= 50_000_000:
            raise ValueError("INVALID_PROBE_REQUEST")
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(pdf_bytes), strict=True)
            pages = len(reader.pages)
            if reader.is_encrypted or not 1 <= pages <= 1000:
                raise ValueError
        except Exception:
            raise ValueError("INVALID_PROBE_REQUEST") from None
        cost = COST_PER_PAGE[mode] * pages
        if cost > Decimal("1.00"):
            raise ValueError("UPSTAGE_ASYNC_RESERVATION_LIMIT")
        body = dict(
            model=PARSE_MODEL_PINNED,
            mode=mode,
            pdf_sha256=hashlib.sha256(pdf_bytes).hexdigest(),
            pages=pages,
            bytes_len=len(pdf_bytes),
            transport="async",
        )
        self._reserve(request_id, body)
        started = monotonic()
        try:
            submitted = self._post_parse(pdf_bytes, mode, "/v1/document-digitization/async")
            provider_id = submitted.get("request_id")
            if not isinstance(provider_id, str) or not provider_id or "/" in provider_id:
                raise ValueError("UPSTAGE_ASYNC_RESPONSE_INVALID")
            deadline = monotonic() + 180
            while True:
                status = self._async_status(provider_id)
                if status.get("status") == "completed":
                    break
                if status.get("status") == "failed" or monotonic() >= deadline:
                    raise ValueError("UPSTAGE_ASYNC_FAILED")
                if status.get("status") not in {"submitted", "started"}:
                    raise ValueError("UPSTAGE_ASYNC_RESPONSE_INVALID")
                sleep(1)
            if (
                status.get("total_pages") != pages
                or status.get("completed_pages") != pages
                or status.get("model") not in ALLOWED_MODELS
                or not isinstance(status.get("batches"), list)
                or len(status["batches"]) != (pages + 9) // 10
            ):
                raise ValueError("UPSTAGE_ASYNC_RESPONSE_INVALID")
            batches = [self._async_download(item["download_url"]) for item in status["batches"]]
            seen = set()
            for batch in batches:
                if batch.get("model") not in ALLOWED_MODELS or not isinstance(
                    batch.get("elements"), list
                ):
                    raise ValueError("UPSTAGE_ASYNC_RESPONSE_INVALID")
                usage = batch.get("usage", {})
                selected = usage.get(mode)
                if not isinstance(selected, list) or usage.get("pages") != len(selected):
                    raise ValueError("UPSTAGE_ASYNC_RESPONSE_INVALID")
                for page in selected:
                    if type(page) is not int or not 1 <= page <= pages or page in seen:
                        raise ValueError("UPSTAGE_ASYNC_RESPONSE_INVALID")
                    seen.add(page)
            if seen != set(range(1, pages + 1)):
                raise ValueError("UPSTAGE_ASYNC_RESPONSE_INVALID")
        except Exception as error:
            code = str(error)
            if not code.startswith("UPSTAGE_") or self._api_key in code:
                code = "UPSTAGE_REQUEST_FAILED"
            raise ValueError(code) from None
        archive = self.ledger.parent / "parse-responses"
        archive.mkdir(exist_ok=True, mode=0o700)
        path = archive / (canonical_hash(request_id) + "-async.json")
        with path.open("x", encoding="utf-8") as stream:
            json.dump(batches, stream, ensure_ascii=False)
        path.chmod(0o400)
        receipt = dict(
            model=PARSE_MODEL_PINNED,
            provider_model=status["model"],
            pages=pages,
            usage={"pages": pages, mode: list(range(1, pages + 1))},
            mode=mode,
            cost_with_vat_reserve_usd=str(cost),
            request_sha256=canonical_hash(body),
            response_sha256=canonical_hash(batches),
            provider_request_id=provider_id,
            duration_seconds=monotonic() - started,
        )
        self._settle(request_id, str(cost), receipt)
        return dict(receipt, raw_batches=batches)

    def parse(self, pdf_bytes: bytes, *, request_id: str, mode: str) -> dict:
        if datetime.now(UTC) >= PRICE_RECHECK_AT:
            raise ValueError("PRICE_RECHECK_REQUIRED")
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or not isinstance(mode, str)
            or mode not in ALLOWED_MODES
        ):
            raise ValueError("INVALID_PROBE_REQUEST")
        # Validate PDF before reserving (no budget consumption for malformed input)
        num_pages = self._validate_pdf(pdf_bytes)

        # Prepare reservation body; hash used as request_hash
        reserve_body = {
            "model": PARSE_MODEL_PINNED,
            "mode": mode,
            "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "pages": num_pages,
            "bytes_len": len(pdf_bytes),
        }
        request_hash = canonical_hash(reserve_body)
        # Reserve USD 1 before network; duplicate/budget errors propagate unsanitized but safe
        self._reserve(request_id, reserve_body)

        try:
            data = self._post_parse(pdf_bytes, mode)
        except Exception as error:
            code = str(error)
            if code not in {
                f"UPSTAGE_HTTP_{n}" for n in (400, 401, 403, 404, 429, 500, 502, 503)
            } | {"UPSTAGE_RESPONSE_TOO_LARGE"}:
                code = "UPSTAGE_REQUEST_FAILED"
            # Do not expose credential
            if self._api_key in code:
                code = "UPSTAGE_REQUEST_FAILED"
            raise ValueError(code) from None

        # Preserve provider output even if its billing receipt is rejected.
        archive = self.ledger.parent / "parse-responses"
        archive.mkdir(exist_ok=True, mode=0o700)
        archive_path = archive / (canonical_hash(request_id) + ".json")
        with archive_path.open("x", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False)
        archive_path.chmod(0o400)

        # Validate receipt
        try:
            if not isinstance(data, dict):
                raise ValueError("invalid response")
            provider_model = data.get("model")
            if not isinstance(provider_model, str) or not provider_model.strip():
                raise ValueError("invalid model")
            if provider_model not in ALLOWED_MODELS:
                raise ValueError("invalid model alias")
            usage = data.get("usage")
            if not isinstance(usage, dict):
                raise ValueError("invalid usage")
            pages_val = usage.get("pages")
            if type(pages_val) is not int:
                raise ValueError("invalid pages type")
            if pages_val != num_pages:
                raise ValueError("pages mismatch")
            pages = pages_val

            # Validate mode-page lists if provided
            def _validate_list(lst, label: str) -> set[int]:
                if not isinstance(lst, list):
                    raise ValueError(f"invalid {label}")
                seen: set[int] = set()
                for v in lst:
                    if type(v) is not int:
                        raise ValueError(f"invalid {label} bool/type")
                    if not (1 <= v <= pages):
                        raise ValueError(f"invalid {label} range")
                    if v in seen:
                        raise ValueError(f"duplicate {label}")
                    seen.add(v)
                return seen

            std_set: set[int] = set()
            enh_set: set[int] = set()
            has_std = "standard" in usage
            has_enh = "enhanced" in usage
            if has_std:
                std_val = usage["standard"]
                std_set = _validate_list(std_val, "standard")
            if has_enh:
                enh_val = usage["enhanced"]
                enh_set = _validate_list(enh_val, "enhanced")
            if not (has_std or has_enh):
                raise ValueError("billing mode missing")
            if has_std or has_enh:
                if std_set & enh_set:
                    raise ValueError("overlap")
                combined = std_set | enh_set
                expected = set(range(1, pages + 1))
                if combined != expected:
                    raise ValueError("coverage mismatch")
                if (mode == "standard" and enh_set) or (mode == "enhanced" and std_set):
                    raise ValueError("billing mode mismatch")
            # Cost settlement
            cost = COST_PER_PAGE[mode] * pages
            if cost > Decimal("1.00"):
                raise ValueError("cost exceeds reservation")
            cost_str = str(cost)
            # Additional sanity: model nonempty already checked
        except (KeyError, TypeError, ValueError) as exc:
            # If already the specific receipt error, preserve it
            if str(exc) == "UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED":
                raise
            raise ValueError("UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED") from None

        # Build receipt retaining raw response and hashes
        response_hash = canonical_hash(data)
        provider_model_hash = hashlib.sha256(provider_model.encode()).hexdigest()
        # Also store canonical hash of model string for determinism
        receipt = {
            "model": PARSE_MODEL_PINNED,
            "provider_model": provider_model,
            "provider_model_hash": provider_model_hash,
            "mode": mode,
            "pages": pages,
            "usage": usage,
            "cost_with_vat_reserve_usd": cost_str,
            "price_snapshot": PRICE_SNAPSHOT,
            "response_sha256": response_hash,
            "request_sha256": request_hash,
            "raw_response": data,
        }

        # Update same reserved row only
        self._settle(request_id, cost_str, receipt)

        # Return raw response plus receipt fields/request hash
        return {
            **receipt,
            "raw_response": data,
            "request_hash": request_hash,
            "response_sha256": response_hash,
            "provider_model_hash": provider_model_hash,
        }
