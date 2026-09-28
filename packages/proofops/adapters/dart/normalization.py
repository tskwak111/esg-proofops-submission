"""Data normalization and collection manifest builder conforming to collection_manifest.schema.json.

Provides:
- normalize_financial_amount: Lossless Decimal normalization with arbitrary precision (>28 digits),
  plain decimal notation, and strict unknown unit rejection.
- normalize_entity_set: Deterministic sorted entity set normalization without silent drops.
- normalize_period: ISO-8601 calendar date validation and ordering.
- create_artifact_entry: Strict artifact record builder enforcing state integrity.
- build_collection_manifest: Top-level collection manifest builder with strict boolean typing,
  nested artifact validation, time ordering, and duplicate source rejection.
"""

from __future__ import annotations

import decimal
import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_CORP_CODE_RE = re.compile(r"^[0-9]{8}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ISO_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$")
_DATE_RE = re.compile(r"^(\d{4})-?(\d{2})-?(\d{2})$")

VALID_CONSOLIDATION = {"consolidated", "separate", "unknown"}
VALID_SOURCE_SYSTEM = {"DART", "integrated_report"}
VALID_STATUS = {"retrieved", "not_available", "failed"}

KRW_UNIT_SCALES: dict[str, Decimal] = {
    "": Decimal(1),
    "원": Decimal(1),
    "krw": Decimal(1),
    "천원": Decimal(1000),
    "백만원": Decimal(1000000),
    "억원": Decimal(100000000),
    "억": Decimal(100000000),
    "조원": Decimal(1000000000000),
    "조": Decimal(1000000000000),
}

REQUIRED_ARTIFACT_KEYS = {
    "source_id",
    "document_version_id",
    "artifact_sha256",
    "corp_code",
    "fy",
    "rcept_no",
    "consolidation",
    "source_system",
    "locator",
    "status",
    "error_code",
    "fetched_at",
}


def _current_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_financial_amount(
    raw_value: str | int | Decimal | None,
    *,
    currency: str = "KRW",
    unit_raw: str = "",
) -> dict[str, Any]:
    """Normalize currency amounts to plain Decimal string representation without precision loss.

    Supports numbers exceeding 28 digits without scientific exponent notation.
    Rejects float and bool inputs to prevent precision corruption.
    Rejects NaN, Infinity, and unknown unit scales.
    Applies approved explicit unit scales for KRW (천원, 백만원, 억, 조).
    """
    if isinstance(raw_value, bool):
        raise ValueError("Boolean inputs are rejected for financial amount normalization")

    if isinstance(raw_value, float):
        raise ValueError(
            f"Float inputs are rejected to prevent IEEE-754 precision loss: {raw_value!r}. "
            "Pass string, int, or decimal.Decimal."
        )

    clean_unit = unit_raw.strip()
    if currency.upper() == "KRW":
        if clean_unit and (
            clean_unit not in KRW_UNIT_SCALES and clean_unit.lower() not in KRW_UNIT_SCALES
        ):
            raise ValueError(f"Unknown or unapproved unit for KRW: {unit_raw!r}")
    else:
        if clean_unit and clean_unit.lower() not in ("", currency.lower()):
            raise ValueError(f"Unknown or unapproved unit for {currency}: {unit_raw!r}")

    if raw_value is None:
        return {
            "raw": "",
            "normalized": None,
            "unit": currency,
            "raw_unit": unit_raw,
            "currency": currency,
            "kind": "currency_amount",
        }

    raw_str = str(raw_value).strip()

    if raw_str in ("", "-", "N/A", "null", "None"):
        return {
            "raw": raw_str,
            "normalized": None,
            "unit": currency,
            "raw_unit": unit_raw,
            "currency": currency,
            "kind": "currency_amount",
        }

    clean = raw_str.replace(",", "")
    if clean.startswith("(") and clean.endswith(")"):
        clean = f"-{clean[1:-1].strip()}"

    # Use high-precision localcontext to support numbers exceeding default 28 digits
    ctx_prec = max(100, len(clean) + 20)
    with decimal.localcontext(decimal.Context(prec=ctx_prec)):
        try:
            dec = Decimal(clean)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Failed to parse financial amount: {raw_value!r}") from exc

        if dec.is_nan() or dec.is_infinite():
            raise ValueError(f"Financial amount cannot be NaN or Infinite: {raw_value!r}")

        # Apply approved explicit unit scales if applicable
        scale = KRW_UNIT_SCALES.get(clean_unit, KRW_UNIT_SCALES.get(clean_unit.lower()))
        if scale is not None and currency.upper() == "KRW":
            dec = dec * scale

        # Format strictly in plain decimal notation without exponent (e.g. 1e+30)
        normalized_str = format(dec, "f")

    return {
        "raw": raw_str,
        "normalized": normalized_str,
        "unit": currency,
        "raw_unit": unit_raw,
        "currency": currency,
        "kind": "currency_amount",
    }


def normalize_entity_set(entities: list[str]) -> dict[str, Any]:
    """Normalize list of entity names into deterministic sorted JSON string.

    Never silently drops invalid entity IDs.
    """
    if not isinstance(entities, list):
        raise ValueError("entities must be a list of strings")

    cleaned: list[str] = []
    for idx, e in enumerate(entities):
        if not isinstance(e, str) or not e.strip():
            raise ValueError(f"Invalid entity identifier at index {idx}: {e!r}")
        cleaned.append(e.strip())

    sorted_unique = sorted(set(cleaned))

    return {
        "raw": json.dumps(entities, ensure_ascii=False),
        "normalized": json.dumps(sorted_unique, ensure_ascii=False),
        "kind": "entity_set",
        "unit": "entity",
    }


def _parse_and_format_date(date_str: str) -> str:
    m = _DATE_RE.match(date_str.strip())
    if not m:
        raise ValueError(f"Invalid date format (must be YYYYMMDD or YYYY-MM-DD): {date_str!r}")
    iso_date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # Validate actual calendar date (e.g. leap years, month boundaries)
    date.fromisoformat(iso_date_str)
    return iso_date_str


def normalize_period(period_start: str, period_end: str) -> dict[str, Any]:
    """Normalize date range to YYYY-MM-DD/YYYY-MM-DD format with calendar and order validation."""
    norm_start = _parse_and_format_date(period_start)
    norm_end = _parse_and_format_date(period_end)

    d_start = date.fromisoformat(norm_start)
    d_end = date.fromisoformat(norm_end)

    if d_start > d_end:
        raise ValueError(
            f"Invalid date range: start date {norm_start} cannot be after end date {norm_end}"
        )

    return {
        "raw": f"{period_start}/{period_end}",
        "normalized": f"{norm_start}/{norm_end}",
        "kind": "period",
    }


def create_artifact_entry(
    source_id: str,
    document_version_id: str,
    corp_code: str,
    fy: int,
    *,
    artifact_sha256: str | None = None,
    rcept_no: str | None = None,
    consolidation: str = "consolidated",
    source_system: str = "DART",
    locator: str | None = None,
    status: str = "retrieved",
    error_code: str | None = None,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Create a single artifact dictionary adhering to collection_manifest.schema.json."""
    if not source_id or not isinstance(source_id, str):
        raise ValueError("source_id must be a non-empty string")
    if not document_version_id or not isinstance(document_version_id, str):
        raise ValueError("document_version_id must be a non-empty string")

    if not isinstance(corp_code, str):
        raise ValueError(f"corp_code must be a string, got {type(corp_code).__name__}")
    clean_corp = corp_code.strip()
    if not _CORP_CODE_RE.match(clean_corp):
        raise ValueError(f"corp_code must be exactly 8 digits, got {corp_code!r}")

    if isinstance(fy, bool) or not isinstance(fy, int):
        raise ValueError(f"fy must be an integer (booleans rejected), got {fy!r}")

    if consolidation not in VALID_CONSOLIDATION:
        raise ValueError(
            f"consolidation must be one of {VALID_CONSOLIDATION}, got {consolidation!r}"
        )

    if source_system not in VALID_SOURCE_SYSTEM:
        raise ValueError(
            f"source_system must be one of {VALID_SOURCE_SYSTEM}, got {source_system!r}"
        )

    if status not in VALID_STATUS:
        raise ValueError(f"status must be one of {VALID_STATUS}, got {status!r}")

    clean_sha: str | None = None
    if artifact_sha256 is not None:
        clean_sha = artifact_sha256.lower().strip()
        if not _SHA256_RE.match(clean_sha):
            raise ValueError(
                f"artifact_sha256 must be 64-char hex or None, got {artifact_sha256!r}"
            )

    clean_rcept: str | None = None
    if rcept_no is not None:
        if not isinstance(rcept_no, str):
            raise ValueError(f"rcept_no must be a string or None, got {type(rcept_no).__name__}")
        clean_rcept = rcept_no.strip()
        if not clean_rcept:
            raise ValueError("rcept_no cannot be blank or empty")

    clean_locator: str | None = None
    if locator is not None:
        clean_locator = str(locator).strip()
        if not clean_locator:
            raise ValueError("locator cannot be blank or empty")

    clean_error: str | None = None
    if error_code is not None:
        clean_error = str(error_code).strip()
        if not clean_error:
            raise ValueError("error_code cannot be blank or empty")

    # Enforce status-specific invariants
    if status == "retrieved":
        if clean_sha is None:
            raise ValueError("Status 'retrieved' requires non-null artifact_sha256")
        if clean_locator is None:
            raise ValueError("Status 'retrieved' requires non-null locator")
        if not clean_locator:
            raise ValueError("Status 'retrieved' requires non-blank locator")
        if clean_error is not None:
            raise ValueError("Status 'retrieved' requires null error_code")
    else:  # status in ("not_available", "failed")
        if clean_sha is not None:
            raise ValueError(f"Status {status!r} requires null artifact_sha256")
        if clean_locator is not None:
            raise ValueError(f"Status {status!r} requires null locator")
        if clean_error is None:
            raise ValueError(f"Status {status!r} requires non-empty error_code")
        if not clean_error:
            raise ValueError(f"Status {status!r} requires non-blank error_code")

    if fetched_at is None:
        fetched_at = _current_utc_iso()
    elif not _ISO_Z_RE.match(fetched_at):
        raise ValueError(
            f"fetched_at must be an ISO-8601 UTC timestamp ending in Z, got {fetched_at!r}"
        )
    else:
        try:
            datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        except Exception as exc:
            raise ValueError(f"Invalid timestamp in fetched_at: {fetched_at!r}") from exc

    return {
        "source_id": source_id,
        "document_version_id": document_version_id,
        "artifact_sha256": clean_sha,
        "corp_code": clean_corp,
        "fy": fy,
        "rcept_no": clean_rcept,
        "consolidation": consolidation,
        "source_system": source_system,
        "locator": clean_locator,
        "status": status,
        "error_code": clean_error,
        "fetched_at": fetched_at,
    }


def _validate_nested_artifact(entry: Any) -> dict[str, Any]:
    """Validate that an artifact entry strictly adheres to collection_manifest.schema.json."""
    if not isinstance(entry, dict):
        raise ValueError(f"Artifact entry must be a dictionary, got {type(entry).__name__}")

    extra_keys = set(entry.keys()) - REQUIRED_ARTIFACT_KEYS
    if extra_keys:
        raise ValueError(f"Artifact entry contains invalid extra keys: {extra_keys}")

    missing_keys = REQUIRED_ARTIFACT_KEYS - set(entry.keys())
    if missing_keys:
        raise ValueError(f"Artifact entry is missing required keys: {missing_keys}")

    # Re-validate state rules and format
    canonical = create_artifact_entry(
        source_id=entry["source_id"],
        document_version_id=entry["document_version_id"],
        corp_code=entry["corp_code"],
        fy=entry["fy"],
        artifact_sha256=entry["artifact_sha256"],
        rcept_no=entry["rcept_no"],
        consolidation=entry["consolidation"],
        source_system=entry["source_system"],
        locator=entry["locator"],
        status=entry["status"],
        error_code=entry["error_code"],
        fetched_at=entry["fetched_at"],
    )

    # Require exact equality and exact types with canonical validated entry:
    # do NOT silently coerce or allow non-conforming types (e.g. corp_code as int, rcept_no as int)
    for k, v in canonical.items():
        entry_val = entry[k]
        if entry_val != v or type(entry_val) is not type(v):
            raise ValueError(
                f"Artifact entry field {k!r} has invalid value or type: "
                f"expected {type(v).__name__} {v!r}, got {type(entry_val).__name__} {entry_val!r}"
            )

    return canonical


def build_collection_manifest(
    manifest_id: str,
    package_id: str,
    artifacts: list[dict[str, Any]],
    *,
    synthetic: bool = False,
    fetched_at: str | None = None,
) -> dict[str, Any]:
    """Construct top-level collection manifest conforming to collection_manifest.schema.json.

    Validates strict boolean typing for synthetic, validates each nested artifact,
    verifies time ordering against manifest fetched_at, and rejects duplicate source IDs.
    """
    if not manifest_id or not isinstance(manifest_id, str):
        raise ValueError("manifest_id must be a non-empty string")
    if not package_id or not isinstance(package_id, str):
        raise ValueError("package_id must be a non-empty string")
    if not artifacts or not isinstance(artifacts, list):
        raise ValueError("artifacts must be a non-empty list of artifact entries")

    if type(synthetic) is not bool:
        raise ValueError(f"synthetic must be a strict boolean, got {type(synthetic).__name__}")

    if fetched_at is None:
        fetched_at = _current_utc_iso()
    elif not _ISO_Z_RE.match(fetched_at):
        raise ValueError(
            f"fetched_at must be an ISO-8601 UTC timestamp ending in Z, got {fetched_at!r}"
        )

    try:
        manifest_dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError(f"Invalid timestamp in manifest fetched_at: {fetched_at!r}") from exc

    # Validate nested artifacts, uniqueness of source IDs, and time ordering
    seen_sources: set[str] = set()
    validated_artifacts: list[dict[str, Any]] = []
    for idx, entry in enumerate(artifacts):
        canonical_entry = _validate_nested_artifact(entry)
        sid = canonical_entry["source_id"]
        if sid in seen_sources:
            raise ValueError(f"Duplicate source_id in manifest artifacts: {sid!r}")
        seen_sources.add(sid)

        entry_dt = datetime.fromisoformat(canonical_entry["fetched_at"].replace("Z", "+00:00"))
        if entry_dt > manifest_dt:
            raise ValueError(
                f"Artifact {idx} fetched_at ({canonical_entry['fetched_at']}) cannot be after "
                f"manifest fetched_at ({fetched_at})"
            )
        validated_artifacts.append(canonical_entry)

    return {
        "schema_version": "collection-1",
        "manifest_id": manifest_id,
        "package_id": package_id,
        "synthetic": synthetic,
        "fetched_at": fetched_at,
        "artifacts": validated_artifacts,
    }
