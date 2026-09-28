"""Offline replay of trusted raster records for a future v5 checkpoint."""

from dataclasses import replace
from uuid import UUID, uuid5

from proofops.adapters.local.raster_job_store import _receipt, _registered
from proofops.adapters.local.raster_visibility import (
    corroborate_native_visibility,
    eligible_raster_sources,
    raster_ocr_policy,
)
from proofops.adapters.local.source_verification import replay_native_sources
from proofops.application.runs import validate_raster_snapshot
from proofops.domain.provenance import canonical_hash

_REQUEST_FIELDS = {
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


def _frozen_snapshot(snapshot, message, graph):
    if not isinstance(snapshot, dict):
        raise ValueError("RASTER_CHECKPOINT_SNAPSHOT_INVALID")
    required = {
        "raster_ocr_policy",
        "raster_ocr_policy_hash",
        "raster_ocr_runtime",
        "raster_ocr_runtime_artifact_hash",
    }
    if {key for key in snapshot if key.startswith("raster_ocr_")} != required:
        raise ValueError("RASTER_CHECKPOINT_SNAPSHOT_INVALID")
    validate_raster_snapshot(snapshot)
    frozen = {key: value for key, value in snapshot.items() if key != "input_hash"}
    document = snapshot.get("document")
    if (
        snapshot.get("input_hash") != message.input_hash
        or canonical_hash(frozen) != message.input_hash
        or not isinstance(document, dict)
        or message.stage != "parse"
        or (message.tenant_id, message.run_id, message.document_version_id)
        != (snapshot.get("tenant_id"), snapshot.get("run_id"), document.get("version_id"))
        or (
            graph.tenant_id,
            graph.document_version_id,
            graph.source_sha256,
        )
        != (message.tenant_id, document.get("version_id"), document.get("sha256"))
        or graph.parse_manifest_id
        != str(uuid5(UUID(message.run_id), "parse:" + message.input_hash))
    ):
        raise ValueError("RASTER_CHECKPOINT_SCOPE_MISMATCH")
    policy = snapshot["raster_ocr_policy"]
    if policy != raster_ocr_policy(
        mode=policy["mode"], max_pages=policy["max_pages"], max_calls=policy["max_calls"]
    ):
        raise ValueError("RASTER_CHECKPOINT_POLICY_CHANGED")
    return document, policy


def _request(registration, *, snapshot, message, graph, native, policy, eligible):
    registered = _registered(registration)
    request = registered["request"]
    if (
        set(registration) != {"request", "request_sha256", "owner", "fencing_token"}
        or not isinstance(request, dict)
        or set(request) != _REQUEST_FIELDS
        or not registered["owner"].strip()
        or type(registered["fencing_token"]) is not int
        or registered["fencing_token"] < 1
    ):
        raise ValueError("RASTER_CHECKPOINT_REQUEST_INVALID")
    try:
        unsigned = {key: value for key, value in request.items() if key != "request_id"}
        expected_id = str(
            uuid5(
                UUID(message.job_id),
                canonical_hash(unsigned),
            )
        )
    except (TypeError, ValueError):
        raise ValueError("RASTER_CHECKPOINT_REQUEST_INVALID")
    if (
        request.get("schema") != "local_raster_ocr_request_v1"
        or request.get("request_id") != expected_id
        or (
            request.get("tenant_id"),
            request.get("run_id"),
            request.get("job_id"),
            request.get("input_hash"),
        )
        != (message.tenant_id, message.run_id, message.job_id, message.input_hash)
        or (
            request.get("document_version_id"),
            request.get("parse_manifest_id"),
            request.get("source_sha256"),
        )
        != (graph.document_version_id, graph.parse_manifest_id, graph.source_sha256)
        or request.get("selected_pages") != snapshot["selected_pages"]
        or any(request.get(key) != policy[key] for key in ("mode", "max_pages", "max_calls"))
        or request.get("policy_sha256") != snapshot["raster_ocr_policy_hash"]
        or request.get("native_attestation_sha256") != canonical_hash(native)
    ):
        raise ValueError("RASTER_CHECKPOINT_REQUEST_SCOPE_MISMATCH")
    requested = request.get("requested_source_ids")
    request_eligible = request.get("eligible_source_ids")
    correspondence = request.get("correspondence")
    rows = correspondence.get("rows") if isinstance(correspondence, dict) else None
    if (
        not isinstance(requested, list)
        or not isinstance(request_eligible, list)
        or not isinstance(rows, list)
        or any(not isinstance(source_id, str) for source_id in requested + request_eligible)
        or len(requested) != len(set(requested))
        or sorted(request_eligible) != sorted(eligible)
        or not set(requested) <= eligible
        or any(
            type(request.get(key)) is not int
            for key in ("max_pages", "max_calls", "submitted_pages")
        )
        or request.get("submitted_pages") != len(rows)
        or not 1 <= len(rows) <= policy["max_pages"]
        or [row.get("source_id") if isinstance(row, dict) else None for row in rows] != requested
        or any(
            not isinstance(row, dict)
            or type(row.get("physical_page")) is not int
            or row["physical_page"] not in snapshot["selected_pages"]
            for row in rows
        )
    ):
        raise ValueError("RASTER_CHECKPOINT_REQUEST_CORRESPONDENCE_INVALID")
    return registered, request


def replay_raster_records(snapshot, message, native, graph, source, registrations, receipts):
    """Return the offline-composed graph, exact coverage, and immutable artifact refs."""
    document, policy = _frozen_snapshot(snapshot, message, graph)
    if (
        not isinstance(native, dict)
        or native.get("schema") != "native_paragraph_attestation_v2"
        or not isinstance(registrations, tuple)
        or not isinstance(receipts, dict)
        or native.get("tenant_id") != message.tenant_id
        or (
            native.get("document_version_id"),
            native.get("parse_manifest_id"),
            native.get("source_sha256"),
        )
        != (document["version_id"], graph.parse_manifest_id, document["sha256"])
    ):
        raise ValueError("RASTER_CHECKPOINT_INPUT_INVALID")
    baseline = replay_native_sources(native, graph, source, tenant_id=message.tenant_id)
    pages = {block.source_id: block.page_num for block in graph.blocks}
    eligible = {
        source_id
        for source_id in eligible_raster_sources(native)
        if pages[source_id] in snapshot["selected_pages"]
    }
    if len(registrations) > policy["max_calls"]:
        raise ValueError("RASTER_CHECKPOINT_MAX_CALLS_EXCEEDED")

    seen, requested, corroborated, refs = set(), set(), set(), []
    for registration in registrations:
        registered, request = _request(
            registration,
            snapshot=snapshot,
            message=message,
            graph=graph,
            native=native,
            policy=policy,
            eligible=eligible,
        )
        request_id = request["request_id"]
        if request_id in seen or requested & set(request["requested_source_ids"]):
            raise ValueError("RASTER_CHECKPOINT_REQUEST_DUPLICATE")
        seen.add(request_id)
        requested.update(request["requested_source_ids"])
        wrapper = receipts.get(request_id)
        if (
            not isinstance(wrapper, dict)
            or set(wrapper) != {"request_sha256", "receipt", "receipt_sha256"}
            or wrapper.get("request_sha256") != registered["request_sha256"]
            or wrapper.get("receipt_sha256") != canonical_hash(wrapper.get("receipt"))
        ):
            raise ValueError("RASTER_CHECKPOINT_RECEIPT_INVALID")
        try:
            _receipt(request, wrapper["receipt"])
        except (KeyError, TypeError):
            raise ValueError("RASTER_CHECKPOINT_RECEIPT_INVALID")
        _, proof = corroborate_native_visibility(
            native,
            request["correspondence"],
            wrapper["receipt"],
            graph,
            source,
            request_sha256=canonical_hash(request["correspondence"]),
            receipt_sha256=wrapper["receipt_sha256"],
            tenant_id=message.tenant_id,
        )
        corroborated.update(proof["corroborated_source_ids"])
        refs.append(
            {
                "request_id": request_id,
                "request_sha256": registered["request_sha256"],
                "receipt_sha256": wrapper["receipt_sha256"],
            }
        )
    if set(receipts) != seen:
        raise ValueError("RASTER_CHECKPOINT_RECEIPT_SET_MISMATCH")

    composed = replace(
        baseline,
        blocks=tuple(
            replace(block, quality="verified") if block.source_id in corroborated else block
            for block in baseline.blocks
        ),
    )
    return (
        composed,
        {
            "eligible_source_ids": sorted(eligible),
            "requested_source_ids": sorted(requested),
            "corroborated_source_ids": sorted(corroborated),
            "unresolved_source_ids": sorted(eligible - corroborated),
            "failed_source_ids": [],
        },
        sorted(refs, key=lambda ref: ref["request_id"]),
    )
