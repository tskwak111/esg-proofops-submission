"""Immutable, fenced local storage for authorized raster OCR requests."""

from __future__ import annotations

import json
from uuid import UUID, uuid5

from proofops.adapters.local.job_store import LocalSQLiteJobStore
from proofops.adapters.local.raster_ocr import validate_raster_billing
from proofops.adapters.local.upstage_parse import PARSE_MODEL_PINNED
from proofops.application.ports.jobs import LeaseLost
from proofops.application.runs import validate_raster_snapshot
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import _require_sha256, _require_strict_int, _require_uuid

_REQUEST_KIND = "raster_request"
_RECEIPT_KIND = "raster_receipt"


def _snapshot(store, db, message):
    row = db.execute(
        "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?",
        (message.tenant_id, message.run_id),
    ).fetchone()
    if row is None:
        raise ValueError("RASTER_RUN_SNAPSHOT_MISSING")
    snapshot = json.loads(row[0])
    frozen = {key: value for key, value in snapshot.items() if key != "input_hash"}
    if (
        snapshot.get("tenant_id") != message.tenant_id
        or snapshot.get("run_id") != message.run_id
        or snapshot.get("input_hash") != message.input_hash
        or canonical_hash(frozen) != message.input_hash
    ):
        raise ValueError("RASTER_RUN_INPUT_MISMATCH")
    validate_raster_snapshot(snapshot)
    return snapshot


def _request(store, db, lease, request, now):
    store._time(now)
    message = lease.message
    if message.stage != "parse" or store._owned(db, lease, now) is None:
        raise LeaseLost("LEASE_LOST")
    if not isinstance(request, dict):
        raise ValueError("RASTER_REQUEST_INVALID")
    snapshot = _snapshot(store, db, message)
    expected_manifest = str(uuid5(UUID(message.run_id), "parse:" + message.input_hash))
    policy = snapshot["raster_ocr_policy"]
    correspondence = request.get("correspondence")
    required = {
        "schema",
        "tenant_id",
        "run_id",
        "job_id",
        "document_version_id",
        "parse_manifest_id",
        "input_hash",
        "source_sha256",
        "selected_pages",
        "mode",
        "max_pages",
        "max_calls",
        "submitted_pages",
        "eligible_source_ids",
        "requested_source_ids",
        "policy_sha256",
        "native_attestation_sha256",
        "correspondence",
        "request_id",
    }
    if set(request) != required or not isinstance(correspondence, dict):
        raise ValueError("RASTER_REQUEST_INVALID")
    for name in (
        "request_id",
        "tenant_id",
        "run_id",
        "job_id",
        "document_version_id",
        "parse_manifest_id",
    ):
        _require_uuid(name, request[name])
    for name in ("input_hash", "source_sha256", "policy_sha256", "native_attestation_sha256"):
        _require_sha256(name, request[name])
    if (
        request["schema"] != "local_raster_ocr_request_v1"
        or (request["tenant_id"], request["run_id"], request["job_id"], request["input_hash"])
        != (message.tenant_id, message.run_id, message.job_id, message.input_hash)
        or request["document_version_id"] != snapshot["document"]["version_id"]
        or request["parse_manifest_id"] != expected_manifest
        or request["source_sha256"] != snapshot["document"]["sha256"]
        or request["selected_pages"] != snapshot["selected_pages"]
        or any(request[key] != policy[key] for key in ("mode", "max_pages", "max_calls"))
        or request["policy_sha256"] != snapshot["raster_ocr_policy_hash"]
    ):
        raise ValueError("RASTER_REQUEST_SCOPE_MISMATCH")
    for name in ("max_pages", "max_calls", "submitted_pages"):
        if _require_strict_int(name, request[name]) < 1:
            raise ValueError("RASTER_REQUEST_LIMIT_INVALID")
    rows = correspondence.get("rows")
    requested, eligible = request["requested_source_ids"], request["eligible_source_ids"]
    if (
        correspondence.get("schema") != "raster_ocr_correspondence_v1"
        or any(
            correspondence.get(key) != request[key]
            for key in ("tenant_id", "document_version_id", "parse_manifest_id", "source_sha256")
        )
        or not isinstance(rows, list)
        or request["submitted_pages"] != len(rows)
        or len(rows) > request["max_pages"]
        or not isinstance(requested, list)
        or not isinstance(eligible, list)
        or any(not isinstance(item, str) for item in requested + eligible)
        or len(set(requested)) != len(requested)
        or len(set(eligible)) != len(eligible)
        or not set(requested) <= set(eligible)
        or [row.get("source_id") if isinstance(row, dict) else None for row in rows] != requested
        or any(
            not isinstance(row, dict)
            or type(row.get("submitted_page")) is not int
            or row["submitted_page"] != index
            or type(row.get("physical_page")) is not int
            or row["physical_page"] not in request["selected_pages"]
            for index, row in enumerate(rows, 1)
        )
        or correspondence.get("input_pdf_sha256") is None
        or type(correspondence.get("input_bytes")) is not int
        or correspondence["input_bytes"] < 1
    ):
        raise ValueError("RASTER_REQUEST_CORRESPONDENCE_INVALID")
    _require_sha256("input_pdf_sha256", correspondence["input_pdf_sha256"])
    unsigned = {key: value for key, value in request.items() if key != "request_id"}
    if request["request_id"] != str(uuid5(UUID(message.job_id), canonical_hash(unsigned))):
        raise ValueError("RASTER_REQUEST_ID_INVALID")
    return snapshot


def register_raster_request(store: LocalSQLiteJobStore, lease, request, *, now: int) -> bool:
    """Atomically register a prepared request; an exact retry is never redispatched."""
    with store._transaction() as db:
        _request(store, db, lease, request, now)
        key = request["request_id"]
        raw = store._raw(db, lease.message.tenant_id, lease.message.run_id, _REQUEST_KIND, key)
        if raw is not None:
            if json.loads(raw)["request"] != request:
                raise ValueError("RASTER_REQUEST_IMMUTABLE_MISMATCH")
            return False
        count = db.execute(
            "SELECT count(*) FROM job_records WHERE tenant_id=? AND run_id=? AND kind=?",
            (lease.message.tenant_id, lease.message.run_id, _REQUEST_KIND),
        ).fetchone()[0]
        if count >= request["max_calls"]:
            raise ValueError("RASTER_MAX_CALLS_EXCEEDED")
        store._put(
            db,
            lease.message.tenant_id,
            lease.message.run_id,
            _REQUEST_KIND,
            key,
            dict(
                request=request,
                request_sha256=canonical_hash(request),
                owner=lease.owner,
                fencing_token=lease.fencing_token,
            ),
            immutable=True,
        )
        return True


def _registered(value):
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("request"), dict)
        or value.get("request_sha256") != canonical_hash(value["request"])
        or not isinstance(value.get("owner"), str)
        or type(value.get("fencing_token")) is not int
    ):
        raise ValueError("RASTER_REQUEST_INTEGRITY_MISMATCH")
    return value


def raster_requests(store: LocalSQLiteJobStore, message) -> tuple[dict, ...]:
    with store._transaction() as db:
        store._job(db, message)
        return tuple(
            _registered(json.loads(row[0]))
            for row in db.execute(
                "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? AND kind=? "
                "AND json_extract(CAST(value AS TEXT), '$.request.job_id')=? ORDER BY record_id",
                (message.tenant_id, message.run_id, _REQUEST_KIND, message.job_id),
            )
        )


def _receipt(request, receipt):
    if (
        not isinstance(receipt, dict)
        or len(json.dumps(receipt, sort_keys=True, separators=(",", ":"), allow_nan=False).encode())
        > 2 * 1024 * 1024
    ):
        raise ValueError("RASTER_RECEIPT_INVALID")
    raw = receipt.get("raw_response")
    correspondence = request["correspondence"]
    body = dict(
        model=PARSE_MODEL_PINNED,
        mode=request["mode"],
        pdf_sha256=correspondence["input_pdf_sha256"],
        pages=request["submitted_pages"],
        bytes_len=correspondence["input_bytes"],
    )
    if (
        not isinstance(raw, dict)
        or receipt.get("model") != PARSE_MODEL_PINNED
        or receipt.get("provider_model") != PARSE_MODEL_PINNED
        or receipt.get("mode") != request["mode"]
        or type(receipt.get("pages")) is not int
        or receipt["pages"] != request["submitted_pages"]
        or raw.get("model") != PARSE_MODEL_PINNED
        or not isinstance(raw.get("usage"), dict)
        or type(raw["usage"].get("pages")) is not int
        or raw["usage"]["pages"] != request["submitted_pages"]
        or receipt.get("request_sha256") != canonical_hash(body)
        or receipt.get("response_sha256") != canonical_hash(raw)
    ):
        raise ValueError("RASTER_PROVIDER_RECEIPT_INVALID")
    validate_raster_billing(raw["usage"], request["mode"], request["submitted_pages"])


def finish_raster_request(
    store: LocalSQLiteJobStore, lease, request_id: str, receipt: dict
) -> None:
    """Keep an exact provider return only for the lease that registered its request."""
    _require_uuid("request_id", request_id)
    with store._transaction() as db:
        message = lease.message
        store._job(db, message)
        registered = _registered(
            store._get(db, message.tenant_id, message.run_id, _REQUEST_KIND, request_id)
        )
        if registered["request"].get("job_id") != message.job_id or (
            registered["owner"],
            registered["fencing_token"],
        ) != (lease.owner, lease.fencing_token):
            raise LeaseLost("unknown raster request owner")
        _receipt(registered["request"], receipt)
        key = request_id
        wrapper = dict(
            request_sha256=registered["request_sha256"],
            receipt=receipt,
            receipt_sha256=canonical_hash(receipt),
        )
        raw = store._raw(db, message.tenant_id, message.run_id, _RECEIPT_KIND, key)
        if raw is not None:
            if json.loads(raw) != wrapper:
                raise ValueError("RASTER_RECEIPT_IMMUTABLE_MISMATCH")
            return
        store._put(
            db, message.tenant_id, message.run_id, _RECEIPT_KIND, key, wrapper, immutable=True
        )


def raster_receipt(store: LocalSQLiteJobStore, message, request_id: str) -> dict | None:
    _require_uuid("request_id", request_id)
    with store._transaction() as db:
        store._job(db, message)
        registered = _registered(
            store._get(db, message.tenant_id, message.run_id, _REQUEST_KIND, request_id)
        )
        if registered["request"].get("job_id") != message.job_id:
            raise KeyError("resource not found")
        raw = store._raw(db, message.tenant_id, message.run_id, _RECEIPT_KIND, request_id)
        if raw is None:
            return None
        wrapper = json.loads(raw)
        if wrapper.get("request_sha256") != registered.get("request_sha256") or wrapper.get(
            "receipt_sha256"
        ) != canonical_hash(wrapper.get("receipt")):
            raise ValueError("RASTER_RECEIPT_INTEGRITY_MISMATCH")
        _receipt(registered["request"], wrapper["receipt"])
        return wrapper


def validate_raster_checkpoint_bindings(db, store, message, snapshot, envelope):
    """Check published pointers against scoped immutable records in one transaction."""
    from proofops.adapters.local.raster_visibility import eligible_raster_sources, raster_ocr_policy
    from proofops.adapters.local.run_artifacts import (
        checkpoint_raster,
        raster_policy_matches_accepted,
    )

    versioned = envelope.get("schema") == "local_parser_checkpoint_v5"
    enabled = "raster_ocr_policy" in snapshot
    if versioned != enabled:
        raise ValueError("RASTER_CHECKPOINT_POLICY_MISMATCH")
    if not enabled:
        return
    validate_raster_snapshot(snapshot)
    refs, coverage = checkpoint_raster(envelope)
    policy = snapshot["raster_ocr_policy"]
    if (
        not raster_policy_matches_accepted(policy, raster_ocr_policy)
        or envelope["raster_ocr_policy_sha256"] != snapshot["raster_ocr_policy_hash"]
        or envelope.get("native_paragraph_policy_sha256") != policy["native_policy_sha256"]
    ):
        raise ValueError("RASTER_CHECKPOINT_POLICY_MISMATCH")
    native = envelope["native_paragraph_attestation"]
    eligible = eligible_raster_sources(native)
    if coverage["eligible_source_ids"] != sorted(eligible):
        raise ValueError("RASTER_CHECKPOINT_COVERAGE_MISMATCH")
    stored_refs, requested = [], set()
    for row in db.execute(
        "SELECT value FROM job_records WHERE tenant_id=? AND run_id=? AND kind=? "
        "AND json_extract(CAST(value AS TEXT), '$.request.job_id')=? ORDER BY record_id",
        (message.tenant_id, message.run_id, _REQUEST_KIND, message.job_id),
    ):
        registered = _registered(json.loads(row[0]))
        request = registered["request"]
        if (
            request["native_attestation_sha256"] != canonical_hash(native)
            or request["correspondence"]["graph_sha256"] != native["input_graph_sha256"]
            or request["input_hash"] != message.input_hash
            or request["policy_sha256"] != snapshot["raster_ocr_policy_hash"]
            or request["eligible_source_ids"] != sorted(eligible)
            or requested & set(request["requested_source_ids"])
        ):
            raise ValueError("RASTER_CHECKPOINT_REQUEST_MISMATCH")
        requested.update(request["requested_source_ids"])
        wrapper = store._get(
            db, message.tenant_id, message.run_id, _RECEIPT_KIND, request["request_id"]
        )
        if wrapper["request_sha256"] != registered["request_sha256"] or wrapper[
            "receipt_sha256"
        ] != canonical_hash(wrapper["receipt"]):
            raise ValueError("RASTER_RECEIPT_INTEGRITY_MISMATCH")
        _receipt(request, wrapper["receipt"])
        stored_refs.append(
            dict(
                request_id=request["request_id"],
                request_sha256=registered["request_sha256"],
                receipt_sha256=wrapper["receipt_sha256"],
            )
        )
    if (
        refs != stored_refs
        or len(refs) > policy["max_calls"]
        or coverage["requested_source_ids"] != sorted(requested)
    ):
        raise ValueError("RASTER_CHECKPOINT_INPUT_MISMATCH")
