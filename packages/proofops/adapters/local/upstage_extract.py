"""Opt-in one-page document extraction; existing USD10 ledger, no automatic retries.

Protocol/model: https://console.upstage.ai/docs/capabilities/extract/universal-extraction
Price: https://www.upstage.ai/pricing/api rechecked 2026-09-25 (rates unchanged).
Enhanced USD0.06/page
plus 10% VAT. Settlement uses the submitted one-page count at the requested mode
rate, not token pricing or a claimed provider invoice. No production activation.
"""

import base64
import http.client
import io
import json
from datetime import UTC, datetime
from hashlib import sha256

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from proofops.adapters.local.upstage import PRICE_RECHECK_AT
from proofops.adapters.local.upstage_parse import UpstageParseProbe
from proofops.domain.provenance import canonical_hash

EXTRACT_MODEL = "information-extract-260904"
PRICE = dict(snapshot_id="upstage-extract-2026-09-13", enhanced_per_page="0.06", vat="1.10")


class UpstageExtractProbe(UpstageParseProbe):
    @property
    def model(self):
        return EXTRACT_MODEL

    def _post_extract(self, body):
        connection = http.client.HTTPSConnection("api.upstage.ai", timeout=60)
        try:
            connection.request(
                "POST",
                "/v1/information-extraction/chat/completions",
                body=json.dumps(body).encode(),
                headers={
                    "Authorization": "Bearer " + self._api_key,
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"UPSTAGE_HTTP_{response.status}")
            raw = response.read(1_048_577)
            if len(raw) > 1_048_576:
                raise ValueError("UPSTAGE_RESPONSE_TOO_LARGE")
            return json.loads(raw)
        finally:
            connection.close()

    def extract(self, pdf_bytes, schema, *, request_id):
        # ponytail: one PDF page or one PNG only; no multi-file/image batch billing.
        media_type = "application/pdf"
        if isinstance(pdf_bytes, bytes) and pdf_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            from PIL import Image

            try:
                if len(pdf_bytes) > 10 * 1024 * 1024:
                    raise ValueError("oversized image")
                with Image.open(io.BytesIO(pdf_bytes)) as img:
                    if (
                        img.format != "PNG"
                        or img.n_frames != 1
                        or img.width * img.height > 16_000_000
                    ):
                        raise ValueError("invalid image")
                    img.verify()
            except Exception:
                raise ValueError("INVALID_PROBE_REQUEST") from None
            media_type = "image/png"
        elif self._validate_pdf(pdf_bytes) != 1:
            raise ValueError("INVALID_PROBE_REQUEST")
        if datetime.now(UTC) >= PRICE_RECHECK_AT:
            raise ValueError("PRICE_RECHECK_REQUIRED")
        if (
            not isinstance(request_id, str)
            or not 1 <= len(request_id) <= 128
            or not isinstance(schema, dict)
        ):
            raise ValueError("INVALID_PROBE_REQUEST")
        try:
            schema_json = json.dumps(schema, allow_nan=False)
            if len(schema_json.encode()) > 16384 or '"$ref"' in schema_json:
                raise ValueError("INVALID_PROBE_REQUEST")
            Draft202012Validator.check_schema(schema)
        except (SchemaError, TypeError, ValueError):
            raise ValueError("INVALID_PROBE_REQUEST") from None
        body = dict(
            model=EXTRACT_MODEL,
            mode="enhanced",
            messages=[
                dict(
                    role="user",
                    content=[
                        dict(
                            type="image_url",
                            image_url=dict(
                                url="data:application/octet-stream;base64,"
                                + base64.b64encode(pdf_bytes).decode()
                            ),
                        )
                    ],
                )
            ],
            response_format=dict(
                type="json_schema", json_schema=dict(name="environmental_claims", schema=schema)
            ),
        )
        reservation = dict(
            model=EXTRACT_MODEL,
            mode="enhanced",
            pages=1,
            input_sha256=sha256(pdf_bytes).hexdigest(),
            input_media_type=media_type,
            schema_sha256=canonical_hash(schema),
            wire_sha256=canonical_hash(body),
            price_snapshot=PRICE,
        )
        self._reserve(request_id, reservation)
        try:
            data = self._post_extract(body)
        except Exception as exc:
            code = str(exc)
            if (
                code
                not in {f"UPSTAGE_HTTP_{n}" for n in (400, 401, 403, 404, 429, 500, 502, 503)}
                | {"UPSTAGE_RESPONSE_TOO_LARGE"}
                or self._api_key in code
            ):
                code = "UPSTAGE_REQUEST_FAILED"
            raise ValueError(code) from None
        # Preserve even an invalid provider receipt before validation or settlement.
        folder = self.ledger.parent / "extract-responses"
        folder.mkdir(mode=0o700, exist_ok=True)
        with (folder / (canonical_hash(request_id) + ".json")).open("x") as handle:
            json.dump(data, handle, ensure_ascii=False)
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            usage = data["usage"]
            if (
                data["model"] != EXTRACT_MODEL
                or not isinstance(data["id"], str)
                or not data["id"]
                or choice["finish_reason"] != "stop"
                or not isinstance(content, str)
                or not content.strip()
                or any(
                    type(usage[k]) is not int or usage[k] < 0
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                )
                or usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]
                or ("pages" in usage and (type(usage["pages"]) is not int or usage["pages"] != 1))
            ):
                raise ValueError("invalid receipt")
        except (KeyError, IndexError, TypeError, ValueError):
            raise ValueError("UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED") from None
        receipt = dict(
            **reservation,
            provider_request_id=data["id"],
            provider_model=data["model"],
            usage=usage,
            cost_with_vat_reserve_usd="0.066",
            cost_basis="submitted_one_page_at_requested_enhanced_rate",
            response_sha256=canonical_hash(data),
        )
        self._settle(request_id, "0.066", receipt)
        return dict(**receipt, content=content)
