"""One explicitly scoped local parser delivery through the existing fenced consumer.

Prepared filesystem artifacts are private and not published evidence. Only a
committed checkpoint makes a verified manifest available to downstream readers.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, replace
from hashlib import sha256
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4, uuid5

from proofops.adapters.local.run_artifacts import (
    load_run_graph,
    load_run_inputs,
    native_paragraph_policy,
)
from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
from proofops.application.ingest.graph_fusion import ParserProfile
from proofops.application.ports.jobs import JobMessage, LeaseLost
from proofops.application.telemetry import Telemetry, TraceContext
from proofops.application.uploads_security import UploadRejected
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json

from proofops_worker.consumer import StageFailure, consume_job, with_lease_heartbeat
from proofops_worker.extract_runner import paragraph_priority
from proofops_worker.telemetry import observe_job


def _with_parser_heartbeat(store, lease, clock, operation):
    """Renew only this local parser's lease; any renewal failure blocks publication."""
    try:
        return with_lease_heartbeat(store, lease, clock, operation)
    except StageFailure as failure:
        if failure.error_code == "LEASE_HEARTBEAT_FAILED":
            raise StageFailure("PARSER_HEARTBEAT_FAILED", usage=failure.usage) from None
        raise


class LocalParserRunner:
    def __init__(
        self,
        store,
        uploads,
        parser: OpenDataLoaderParser,
        *,
        profile: ParserProfile,
        telemetry: Telemetry,
        clock=time.time,
        note_client=None,
        note_ledger=None,
        verify_paragraphs: bool = False,
        raster_probe=None,
        raster_ledger=None,
        native_typography_tolerance: bool = False,
    ):
        if not uploads.local_synthetic or not isinstance(profile, ParserProfile):
            raise ValueError("local parser requires local storage and executable configuration")
        if type(verify_paragraphs) is not bool:
            raise ValueError("verify_paragraphs must be a boolean")
        if type(native_typography_tolerance) is not bool:
            raise ValueError("native_typography_tolerance must be a boolean")
        if native_typography_tolerance and not verify_paragraphs:
            raise ValueError("native_typography_tolerance requires verify_paragraphs")
        self.store, self.uploads, self.parser = store, uploads, parser
        self.profile, self.telemetry, self.clock = profile, telemetry, clock
        self.verify_paragraphs = verify_paragraphs
        # Opt-in only; default False keeps every existing run's checkpoint,
        # verified-block set, and downstream grade byte-identical. When set,
        # this never mutates the stored native_paragraph_attestation receipt
        # or its policy hash -- it only additionally records a separately
        # hashed, separately versioned proof on this instance (see
        # native_paragraph_typography.py) after the unchanged base replay.
        self.native_typography_tolerance = native_typography_tolerance
        self.last_typography_proof = None
        self.note_client = note_client
        self.note_ledger = (
            note_ledger if note_ledger is not None else getattr(note_client, "ledger", None)
        )
        self.raster_probe, self.raster_ledger = raster_probe, raster_ledger

    def _raster_enabled(self, snapshot):
        raster_keys = {key for key in snapshot if key.startswith("raster_ocr_")}
        if not raster_keys:
            if self.raster_probe is not None or self.raster_ledger is not None:
                raise ValueError("RASTER_OCR_RUNTIME_NOT_SUPPORTED")
            return False
        from proofops.adapters.local.upstage_parse import UpstageParseProbe

        if (
            not self.verify_paragraphs
            or not isinstance(self.raster_probe, UpstageParseProbe)
            or self.raster_ledger is None
            or Path(self.raster_probe.ledger).resolve() != Path(self.raster_ledger).resolve()
            or (
                self.note_client is not None
                and (
                    self.note_ledger is None
                    or Path(self.note_ledger).resolve() != Path(self.raster_ledger).resolve()
                )
            )
        ):
            raise ValueError("RASTER_OCR_RUNTIME_NOT_SUPPORTED")
        return True

    def _note_policy(self):
        if self.note_client is None:
            return None
        if not isinstance(self.note_client.model, str) or not self.note_client.model:
            raise ValueError("NOTE_REVIEW_MODEL_REQUIRED")
        package = files("proofops.adapters.local")
        return dict(
            mode="automatic_pages_v2",
            model=self.note_client.model,
            extractor_sha256=sha256(
                package.joinpath("note_extraction.py").read_bytes()
            ).hexdigest(),
            batch_sha256=sha256(package.joinpath("note_review_batch.py").read_bytes()).hexdigest(),
        )

    def _inputs(self, tenant_id, run_id):
        snapshot, source, profile = load_run_inputs(
            self.store, self.uploads, tenant_id=tenant_id, run_id=run_id
        )
        if profile.config_snapshot() != self.profile.config_snapshot():
            raise ParseFailure("RUN_INPUT_INTEGRITY_MISMATCH")
        return snapshot, source, profile

    def _directory(self, source, profile):
        return (
            self.parser.artifact_root
            / source.tenant_id
            / source.document_version_id
            / profile.parse_manifest_id
        )

    def run_once(
        self, *, tenant_id: str, run_id: str, note_review_artifacts: tuple[str, ...] = ()
    ) -> str:
        if not isinstance(note_review_artifacts, tuple):
            raise ValueError("note reviews must be an immutable tuple")
        if note_review_artifacts and self.note_client is not None:
            raise ValueError("NOTE_REVIEW_INPUT_MODE_CONFLICT")
        # No tenant discovery or implicit live-provider selection.
        snapshot = self.store.snapshot(tenant_id, run_id)
        raster_enabled = self._raster_enabled(snapshot)
        run = self.store.jobs.get_run(tenant_id, run_id)
        native_policy = native_paragraph_policy() if self.verify_paragraphs else None
        if (
            "parse_job" in run
            and self.store.jobs.parser_native_policy(JobMessage(**run["parse_job"]))
            != native_policy
        ):
            raise ValueError("NATIVE_PARAGRAPH_ALREADY_PUBLISHED")
        if self.note_client is not None and "parse_job" in run:
            if (
                self.store.jobs.parser_note_policy(JobMessage(**run["parse_job"]))
                != self._note_policy()
            ):
                raise ValueError("NOTE_REVIEW_ALREADY_PUBLISHED")
            self.load_graph(tenant_id=tenant_id, run_id=run_id)
        if note_review_artifacts and "parse_job" in run:
            published = self.store.jobs.read_checkpoint(JobMessage(**run["parse_job"]))
            if published is None or json.loads(published).get(
                "runtime_note_review_artifacts"
            ) != list(note_review_artifacts):
                raise ValueError("NOTE_REVIEW_ALREADY_PUBLISHED")
            self.load_graph(tenant_id=tenant_id, run_id=run_id)
        if run["cancellation_requested"]:
            return "cancelled"
        for event in self.store.jobs.pending_outbox(tenant_id, run_id, now=int(self.clock())):
            message = JobMessage(**event["message"])
            if message.stage != "parse":
                continue
            next_job = replace(
                message, job_id=str(uuid5(UUID(message.job_id), "extract")), stage="extract"
            )

            def operation(lease):
                usage = {"model_calls": 0, "parser_executions": 0, "artifact_reused": False}
                policy = None
                attempted = []
                raster_attempted = []
                raster_registrations = ()
                raster_receipts = {}

                def invoke(*args, **kwargs):
                    try:
                        self.store.jobs.heartbeat(lease, now=int(self.clock()), lease_seconds=90)
                        self.store.jobs.register_parser_note_request(
                            lease, kwargs["request_id"], now=int(self.clock())
                        )
                    except LeaseLost:
                        raise StageFailure("NOTE_REVIEW_LEASE_LOST", usage=usage) from None
                    attempted.append(kwargs["request_id"])
                    try:
                        return self.note_client.complete(*args, **kwargs)
                    finally:
                        self.store.jobs.finish_parser_note_request(lease, kwargs["request_id"])

                try:
                    snapshot, source, profile = self._inputs(tenant_id, run_id)
                    if raster_enabled and self.native_typography_tolerance:
                        # The v5 raster replay path discards typography
                        # promotions, so committing both would publish a
                        # checkpoint no reader can reproduce. Fail closed.
                        raise StageFailure("NATIVE_TYPOGRAPHY_RASTER_UNSUPPORTED", usage=usage)
                    if (
                        message.tenant_id,
                        message.run_id,
                        message.document_version_id,
                        message.input_hash,
                    ) != (tenant_id, run_id, source.document_version_id, snapshot["input_hash"]):
                        raise ParseFailure("RUN_INPUT_INTEGRITY_MISMATCH")
                    policy = self._note_policy()
                    pinned_notes = self.store.jobs.parser_note_reviews(message)
                    if policy is None and pinned_notes is not None:
                        policy = self.store.jobs.parser_note_policy(message)
                    if policy is not None and note_review_artifacts:
                        raise ValueError("NOTE_REVIEW_INPUT_MODE_CONFLICT")
                    self.store.jobs.bind_parser_note_policy(lease, policy, now=int(self.clock()))
                    self.store.jobs.bind_parser_native_policy(
                        lease, native_policy, now=int(self.clock())
                    )
                    if policy is None:
                        pinned_notes = self.store.jobs.bind_parser_note_reviews(
                            lease, note_review_artifacts, now=int(self.clock())
                        )
                    directory = self._directory(source, profile)
                    if not directory.exists():
                        usage["parser_executions"] = 1
                        try:
                            self.parser.parse(source, profile, tenant_id=tenant_id)
                        except ParseFailure as exc:
                            if str(exc) != "PARSE_MANIFEST_EXISTS":
                                raise
                    else:
                        usage["artifact_reused"] = True
                    raw_manifest = (directory / "manifest.json").read_bytes()
                    digest = sha256(raw_manifest).hexdigest()
                    graph = self.parser.load_verified(
                        source, profile, tenant_id=tenant_id, manifest_sha256=digest
                    )
                    if policy is not None and pinned_notes is None:
                        from proofops.adapters.local.note_review_batch import review_tables

                        client = SimpleNamespace(
                            model=self.note_client.model,
                            summary=self.note_client.summary,
                            complete=invoke,
                        )
                        prepared = review_tables(
                            graph,
                            source.content,
                            client,
                            self.parser.artifact_root.parent / "note-reviews" / tenant_id / run_id,
                            tenant_id=tenant_id,
                            selected_pages=profile.physical_pages,
                        )
                        pinned_notes = self.store.jobs.bind_parser_note_reviews(
                            lease, prepared, now=int(self.clock())
                        )
                    if policy is not None:
                        from proofops.adapters.local.run_artifacts import (
                            validate_automatic_note_reviews,
                        )

                        validate_automatic_note_reviews(
                            graph, pinned_notes, policy, selected_pages=profile.physical_pages
                        )
                    if pinned_notes:
                        from proofops.adapters.local.table_notes import replay_note_reviews

                        graph = replay_note_reviews(
                            pinned_notes, graph, source.content, tenant_id=tenant_id
                        )
                    pre_native_graph = graph
                    native_receipt = None
                    if native_policy is not None:
                        from proofops.adapters.local.source_verification import (
                            attest_native_sources,
                            replay_native_sources,
                        )

                        native_receipt = attest_native_sources(
                            graph, source.content, tenant_id=tenant_id, geometry_mode="glyph"
                        )
                        if self.native_typography_tolerance:
                            from proofops.adapters.local.native_paragraph_typography import (
                                apply_typography_tolerance,
                            )

                            # The tolerance wrapper recomputes the base replay
                            # itself, so it must see the attested-input graph,
                            # not the already-replayed one: replay receipts pin
                            # `input_graph_sha256`, and a replayed graph never
                            # reproduces its own input receipt.
                            graph, self.last_typography_proof = apply_typography_tolerance(
                                native_receipt,
                                graph,
                                source.content,
                                tenant_id=tenant_id,
                            )
                        else:
                            graph = replay_native_sources(
                                native_receipt, graph, source.content, tenant_id=tenant_id
                            )
                    raster_coverage = None
                    raster_refs = None
                    if raster_enabled:
                        from proofops.adapters.local.raster_job_store import (
                            raster_receipt,
                            raster_requests,
                        )
                        from proofops.adapters.local.raster_visibility import (
                            eligible_raster_sources,
                        )

                        from proofops_worker.raster_runtime import dispatch_authorized_raster

                        pages = set(snapshot["selected_pages"])
                        blocks = {block.source_id: block for block in pre_native_graph.blocks}
                        eligible = tuple(
                            sorted(
                                (
                                    source_id
                                    for source_id in eligible_raster_sources(native_receipt)
                                    if blocks[source_id].page_num in pages
                                ),
                                key=lambda source_id: paragraph_priority(blocks[source_id]),
                            )
                        )
                        limits = snapshot["raster_ocr_policy"]
                        for start in range(0, len(eligible), limits["max_pages"]):
                            if start // limits["max_pages"] >= limits["max_calls"]:
                                break
                            ids = eligible[start : start + limits["max_pages"]]
                            request, receipt = dispatch_authorized_raster(
                                self,
                                lease,
                                pre_native_graph,
                                native_receipt,
                                ids,
                                probe=self.raster_probe,
                                ledger=self.raster_ledger,
                            )
                            raster_attempted.append(request["request_id"])
                            if receipt is None:
                                raise ValueError("RASTER_REQUEST_PENDING")
                        raster_registrations = raster_requests(self.store.jobs, message)
                        raster_receipts = {
                            item["request"]["request_id"]: raster_receipt(
                                self.store.jobs, message, item["request"]["request_id"]
                            )
                            for item in raster_registrations
                        }
                        if any(item is None for item in raster_receipts.values()):
                            raise ValueError("RASTER_REQUEST_PENDING")
                        from proofops.adapters.local.raster_checkpoint import replay_raster_records

                        graph, raster_coverage, raster_refs = replay_raster_records(
                            snapshot,
                            message,
                            native_receipt,
                            pre_native_graph,
                            source.content,
                            raster_registrations,
                            raster_receipts,
                        )
                    manifest = json.loads(raw_manifest)
                    unreadable = {
                        issue.page_num for issue in graph.issues if issue.state == "unreadable"
                    }
                    coverage = dict(
                        pages_total=snapshot["document"]["page_count"],
                        pages_processed=len(profile.physical_pages) - len(unreadable),
                        pages_unreadable=len(unreadable),
                        pages_unprocessed=snapshot["document"]["page_count"]
                        - len(profile.physical_pages),
                        chunks_discovered=0,
                        chunks_processed=0,
                        claims_discovered=0,
                        claims_decided=0,
                        claims_needs_review=0,
                        full_scope=snapshot["scope"] == "full",
                        complete=False,
                    )
                    payload = dict(
                        schema="local_parser_checkpoint_v1",
                        tenant_id=tenant_id,
                        run_id=run_id,
                        document_version_id=source.document_version_id,
                        input_hash=message.input_hash,
                        source_sha256=source.sha256,
                        object_version_id=source.object_version_id,
                        parse_manifest_id=profile.parse_manifest_id,
                        manifest_sha256=digest,
                        parser_config=profile.config_snapshot(),
                        parser_config_hash=profile.config_hash(),
                        invocation=profile.invocation_snapshot(),
                        artifacts=manifest["artifacts"],
                        stage_status="completed",
                        downstream_status="pending",
                        validation_profile="fast_preview",
                        vision_status="not_run",
                        coverage=coverage,
                    )
                    if pinned_notes:
                        payload.update(
                            schema="local_parser_checkpoint_v2",
                            runtime_note_review_artifacts=list(pinned_notes),
                            graph_sha256=canonical_hash(asdict(graph)),
                        )
                    if policy is not None:
                        payload.update(
                            schema="local_parser_checkpoint_v3",
                            runtime_note_review_artifacts=list(pinned_notes),
                            graph_sha256=canonical_hash(asdict(graph)),
                            note_review_policy_sha256=canonical_hash(policy),
                        )
                    if native_policy is not None:
                        payload.update(
                            schema="local_parser_checkpoint_v4",
                            runtime_note_review_artifacts=list(pinned_notes or ()),
                            graph_sha256=canonical_hash(asdict(graph)),
                            native_paragraph_attestation=native_receipt,
                            native_paragraph_policy_sha256=canonical_hash(native_policy),
                        )
                        if self.native_typography_tolerance:
                            from proofops.adapters.local.native_paragraph_typography import (
                                native_paragraph_typography_policy,
                            )

                            payload.update(
                                native_paragraph_typography_policy_sha256=canonical_hash(
                                    native_paragraph_typography_policy()
                                ),
                                native_paragraph_typography_proof=self.last_typography_proof,
                            )
                    if raster_enabled:
                        payload.update(
                            schema="local_parser_checkpoint_v5",
                            graph_sha256=canonical_hash(asdict(graph)),
                            raster_ocr_policy_sha256=canonical_hash(snapshot["raster_ocr_policy"]),
                            raster_ocr_artifacts=raster_refs,
                            raster_ocr_coverage=raster_coverage,
                        )
                    return canonical_json(payload).encode(), usage
                except (ParseFailure, UploadRejected) as exc:
                    raise StageFailure(str(exc), usage=usage) from None
                except (OSError, ValueError, KeyError, TypeError):
                    raise StageFailure("PARSER_FAILED", usage=usage) from None
                finally:
                    note_identifiers = self.store.jobs.parser_note_requests(message)
                    if raster_enabled:
                        from proofops.adapters.local.raster_job_store import raster_requests

                        raster_registrations = raster_requests(self.store.jobs, message)
                    raster_identifiers = (
                        tuple(item["request"]["request_id"] for item in raster_registrations)
                        if raster_enabled
                        else ()
                    )
                    identifiers = tuple(dict.fromkeys(note_identifiers + raster_identifiers))
                    if policy is not None or identifiers:
                        recorded = {
                            identifier
                            for item in self.store.jobs.list_usage(tenant_id, run_id)
                            for identifier in (
                                item.get("note_request_ids", [])
                                + item.get("raster_request_ids", [])
                            )
                        }
                        unreported = [
                            identifier for identifier in identifiers if identifier not in recorded
                        ]
                        usage.update(
                            note_request_ids=[],
                            raster_request_ids=[],
                            note_attempted_calls=len(attempted),
                            raster_attempted_calls=len(raster_attempted),
                        )
                        try:
                            from proofops.adapters.local.upstage import request_usage

                            returned = set(
                                self.store.jobs.parser_note_requests(message, returned=True)
                            )
                            returned.update(
                                request_id
                                for request_id, receipt in raster_receipts.items()
                                if receipt is not None
                            )
                            pending, finalized = [], []
                            # ponytail: per-request reads; batch if ledger latency matters.
                            for identifier in unreported:
                                item = request_usage(
                                    self.raster_ledger if raster_enabled else self.note_ledger,
                                    [identifier],
                                )
                                if item["unsettled_calls"] or (
                                    not item["model_calls"] and identifier not in returned
                                ):
                                    pending.append(identifier)
                                else:
                                    finalized.append(identifier)
                            ledger = self.raster_ledger if raster_enabled else self.note_ledger
                            usage.update(request_usage(ledger, finalized))
                            if pending:
                                usage["note_pending_request_ids"] = [
                                    item for item in pending if item in note_identifiers
                                ]
                                usage["raster_pending_request_ids"] = [
                                    item for item in pending if item in raster_identifiers
                                ]
                                usage["note_pending_usage"] = request_usage(ledger, pending)
                            usage["note_request_ids"] = [
                                item for item in finalized if item in note_identifiers
                            ]
                            usage["raster_request_ids"] = [
                                item for item in finalized if item in raster_identifiers
                            ]
                            if pending:
                                raise StageFailure("NOTE_ACCOUNTING_PENDING", usage=usage)
                        except (ValueError, OSError, TypeError):
                            usage.update(request_usage(None, []))
                            usage.update(
                                note_request_ids=[],
                                raster_request_ids=[],
                                note_pending_request_ids=unreported,
                                note_pending_usage={"cost_with_vat_reserve_usd": "unknown"},
                            )
                            raise StageFailure("ACCOUNTING_UNAVAILABLE", usage=usage) from None

            result = consume_job(
                self.store.jobs,
                message,
                owner="local-parser:" + str(uuid4()),
                clock=lambda: int(self.clock()),
                lease_seconds=math.ceil(self.profile.timeout_seconds) * 3 + 30,
                operation=lambda lease: observe_job(
                    self.telemetry,
                    lease,
                    lambda active_lease: _with_parser_heartbeat(
                        self.store.jobs, active_lease, self.clock, operation
                    ),
                    context=TraceContext.new(
                        tenant_id=tenant_id, run_id=run_id, job_id=message.job_id
                    ),
                ),
                next_job=next_job,
            )
            if result in {"committed", "ignored", "failed", "retry"}:
                self.store.jobs.mark_outbox(
                    tenant_id,
                    run_id,
                    event["event_id"],
                    now=int(self.clock()),
                    sent=True,
                    expected_attempts=event["attempts"],
                )
            return result
        return "pending_downstream" if run.get("current_stage") == "extract" else "idle"

    def load_graph(self, *, tenant_id: str, run_id: str):
        """The sole runner publication read: fence-committed pointer plus verified rich graph."""
        return load_run_graph(
            self.store, self.uploads, self.parser, tenant_id=tenant_id, run_id=run_id
        )
