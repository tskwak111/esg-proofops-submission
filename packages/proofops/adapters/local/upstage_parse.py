"""Bounded Upstage Document Parse transport; shares ledger with text probe.

Uses same SQLite ledger/policy (USD 10 limit, USD 1 reservation) via UpstageProbe.
Validates PDFs with pypdf (1..10 pages, <=10 MB, no encryption). Fixed host
api.upstage.ai POST /v1/document-digitization multipart with pinned model
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

    def _post_parse(self, pdf_bytes: bytes, mode: str) -> dict:
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
                "/v1/document-digitization",
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
