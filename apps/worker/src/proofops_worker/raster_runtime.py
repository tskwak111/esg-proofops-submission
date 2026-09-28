"""Prepare scoped raster requests; no network, reservation or publication here.

Dispatch must call this against the live lease immediately before registering
and reserving the request. Persisted max-call accounting and v5 publication are
separate required gates; a prepared request alone is not permission to dispatch.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid5

from proofops.adapters.local.raster_ocr import prepare_raster_ocr
from proofops.adapters.local.raster_visibility import eligible_raster_sources, raster_ocr_policy
from proofops.adapters.local.run_artifacts import load_run_inputs
from proofops.adapters.local.source_verification import replay_native_sources
from proofops.adapters.local.upstage_parse import PARSE_MODEL_PINNED
from proofops.application.authorization import AuthContext
from proofops.application.ports.jobs import LeaseLost
from proofops.application.preflight import check_local_upstage_raster
from proofops.application.registry import artifact_sha256
from proofops.application.runs import validate_raster_snapshot
from proofops.domain.provenance import canonical_hash


def prepare_authorized_raster(runner, lease, graph, native, source_ids):
    message = lease.message
    now = int(runner.clock())
    if message.stage != "parse" or not runner.store.jobs.can_call(lease, now=now):
        raise LeaseLost("LEASE_LOST")
    snapshot, source, profile = load_run_inputs(
        runner.store, runner.uploads, tenant_id=message.tenant_id, run_id=message.run_id
    )
    validate_raster_snapshot(snapshot)
    if "raster_ocr_policy" not in snapshot or (
        message.document_version_id,
        message.input_hash,
        graph.tenant_id,
        graph.document_version_id,
        graph.parse_manifest_id,
        graph.source_sha256,
    ) != (
        source.document_version_id,
        snapshot["input_hash"],
        message.tenant_id,
        source.document_version_id,
        profile.parse_manifest_id,
        source.sha256,
    ):
        raise ValueError("RASTER_RUN_INPUT_MISMATCH")
    policy = snapshot["raster_ocr_policy"]

    def authorize():
        if not runner.store.jobs.can_call(lease, now=int(runner.clock())):
            raise LeaseLost("LEASE_LOST")
        if policy != raster_ocr_policy(
            mode=policy["mode"], max_pages=policy["max_pages"], max_calls=policy["max_calls"]
        ):
            raise ValueError("RASTER_EXECUTION_POLICY_CHANGED")
        auth = AuthContext("local-worker", message.tenant_id, "viewer", frozenset(), message.run_id)
        binding = snapshot["raster_ocr_runtime"]
        for kind, frozen, identifier in (
            ("runtime", binding, "runtime_binding_id"),
            ("consent", snapshot["consent"], "consent_profile_id"),
            ("rights", snapshot["rights"], "rights_profile_id"),
        ):
            try:
                current = runner.uploads.registry.resolve_profile(auth, kind, frozen[identifier])
            except LookupError:
                raise ValueError("RASTER_AUTHORIZATION_REVOKED") from None
            if artifact_sha256(current) != artifact_sha256(frozen):
                raise ValueError("RASTER_AUTHORIZATION_CHANGED")
        gate = check_local_upstage_raster(
            binding=binding,
            consent=snapshot["consent"],
            auth=auth,
            checked_at=datetime.fromtimestamp(int(runner.clock()), UTC).isoformat(),
            source_sha256=source.sha256,
            document_rights=snapshot["document"]["metadata"]["rights_profile_id"],
            model_sha256=canonical_hash(
                dict(model=PARSE_MODEL_PINNED, provider="upstage", transport="UpstageParseProbe")
            ),
        )
        if not gate.ready:
            raise ValueError("RASTER_PREFLIGHT_BLOCKED")

    authorize()
    if (
        not isinstance(source_ids, tuple)
        or not 1 <= len(source_ids) <= policy["max_pages"]
        or any(not isinstance(sid, str) for sid in source_ids)
        or len(set(source_ids)) != len(source_ids)
    ):
        raise ValueError("RASTER_REQUEST_LIMIT_INVALID")
    blocks = {block.source_id: block for block in graph.blocks}
    if any(
        sid not in blocks or blocks[sid].page_num not in snapshot["selected_pages"]
        for sid in source_ids
    ):
        raise ValueError("RASTER_PAGE_SCOPE_MISMATCH")
    if not isinstance(native, dict) or native.get("schema") != "native_paragraph_attestation_v2":
        raise ValueError("NATIVE_GLYPH_ATTESTATION_REQUIRED")
    replay_native_sources(native, graph, source.content, tenant_id=message.tenant_id)
    eligible = eligible_raster_sources(native)
    if not set(source_ids) <= eligible:
        raise ValueError("NATIVE_OCR_FALLBACK_INELIGIBLE")
    data, correspondence = prepare_raster_ocr(
        graph, source.content, source_ids, tenant_id=message.tenant_id
    )
    request = dict(
        schema="local_raster_ocr_request_v1",
        tenant_id=message.tenant_id,
        run_id=message.run_id,
        job_id=message.job_id,
        document_version_id=source.document_version_id,
        parse_manifest_id=profile.parse_manifest_id,
        input_hash=message.input_hash,
        source_sha256=source.sha256,
        selected_pages=snapshot["selected_pages"],
        mode=policy["mode"],
        max_pages=policy["max_pages"],
        max_calls=policy["max_calls"],
        submitted_pages=len(correspondence["rows"]),
        eligible_source_ids=sorted(
            sid for sid in eligible if blocks[sid].page_num in snapshot["selected_pages"]
        ),
        requested_source_ids=list(source_ids),
        policy_sha256=snapshot["raster_ocr_policy_hash"],
        native_attestation_sha256=canonical_hash(native),
        correspondence=correspondence,
    )
    request["request_id"] = str(uuid5(UUID(message.job_id), canonical_hash(request)))
    # Rendering may outlive authority or policy; recheck before handing off.
    authorize()
    return data, request


def dispatch_authorized_raster(runner, lease, graph, native, source_ids, *, probe, ledger):
    """One durable request, at most one dispatch; ambiguous requests stay pending.

    Returns trusted stored request/receipt for later offline composition. This
    does not publish a checkpoint or promote any source/claim quality.
    """
    from pathlib import Path

    from proofops.adapters.local.raster_job_store import (
        finish_raster_request,
        raster_receipt,
        register_raster_request,
    )
    from proofops.adapters.local.upstage_parse import UpstageParseProbe

    if (
        not isinstance(probe, UpstageParseProbe)
        or Path(probe.ledger).resolve() != Path(ledger).resolve()
    ):
        raise ValueError("RASTER_LEDGER_MISMATCH")
    data, request = prepare_authorized_raster(runner, lease, graph, native, source_ids)
    jobs = runner.store.jobs
    created = register_raster_request(jobs, lease, request, now=int(runner.clock()))
    if created:
        # The shared transport reserves before HTTP. A crash/unknown response
        # leaves the durable registration in place and never authorizes a retry.
        jobs.heartbeat(lease, now=int(runner.clock()), lease_seconds=300)
        receipt = probe.parse(data, request_id=request["request_id"], mode=request["mode"])
        finish_raster_request(jobs, lease, request["request_id"], receipt)
    saved = raster_receipt(jobs, lease.message, request["request_id"])
    if saved is None:
        raise ValueError("RASTER_REQUEST_PENDING")
    if not jobs.can_call(lease, now=int(runner.clock())):
        raise LeaseLost("LEASE_LOST")
    return request, saved
