"""Explicit local extraction delivery with immutable receipts and fenced publication."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from uuid import UUID, uuid4, uuid5

from proofops.adapters.local.claim_store import (
    DEFERRED_BUDGET_REASON,
    DEFERRED_KIND_REASON,
    DEFERRED_LIMIT_REASON,
    LocalClaimStore,
    claim_scope,
    deferred_payload,
    discovery_coverage,
    extraction_batch_state,
    extraction_profile,
    guard_extract_continuation,
    snapshot_pins,
)
from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.adapters.local.upstage import UPSTAGE_TRANSPORT_STOP_CODES
from proofops.adapters.parsing.opendataloader import ParseFailure
from proofops.application.authorization import AuthContext
from proofops.application.claim_scope import validate_extraction_limits
from proofops.application.claims import (
    ClaimExtractorPort,
    ExtractionOutputError,
    discover_atomic_claims,
)
from proofops.application.ports.jobs import JobMessage, LeaseLost
from proofops.application.preflight import check_local_upstage_binding
from proofops.application.telemetry import TraceContext
from proofops.application.uploads_security import UploadRejected
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json

from proofops_worker.consumer import StageFailure, consume_job, with_lease_heartbeat
from proofops_worker.telemetry import observe_job


def _is_text_candidate(kind: str, text: str) -> bool:
    # ponytail: length is a routing heuristic, not a claim/absence classifier.
    return kind == "paragraph" or (kind == "heading" and len(text.strip()) >= 60)


def paragraph_priority(block):
    """Stable prose/page ordering for bounded extraction and raster recovery."""
    # ponytail: routing heuristic only; no claim or absence classification.
    return (
        not block.normalized_text.rstrip().endswith((".", "!", "?")),
        len(block.normalized_text.strip()) < 60,
        block.page_num,
        block.bbox is not None,
        tuple(block.bbox or ()),
        block.normalized_text,
    )


def select_batch_paragraph_sources(
    graph, scope, max_calls: int, *, completed=frozenset()
) -> set[str]:
    """Next bounded window of eligible prose sources, skipping completed ones.

    The ordering is exactly ``paragraph_priority`` and the per-batch bound is
    unchanged; continuation advances by excluding the sources a previous batch
    already model-processed, never by raising the bound. ``completed`` carries
    exact source identifiers, so a source is never processed twice and a source
    that was merely deferred stays eligible.
    """
    if type(max_calls) is not int or not 1 <= max_calls <= 20:
        raise ValueError("EXTRACTION_INPUT_INVALID")
    eligible = (
        block
        for block in graph.blocks
        if (scope.mode != "declared_subset" or block.page_num in scope.selected_pages)
        and block.quality in ("verified", "unverified")
        and block.winner is not None
        and _is_text_candidate(block.kind, block.normalized_text)
        and block.normalized_text.strip()
        and block.source_id not in completed
    )
    ordered = sorted(eligible, key=paragraph_priority)
    return {block.source_id for block in ordered[:max_calls]}


def select_stable_paragraph_sources(graph, scope, max_calls: int) -> set[str]:
    """Prioritize prose within a bounded preview; retain short candidates after it."""
    return select_batch_paragraph_sources(graph, scope, max_calls)


def _extractor_role_budget(snapshot: dict) -> int | None:
    """Return the run's frozen total call cap for the extractor role, if configured."""
    budget = snapshot.get("budget_limits")
    if not isinstance(budget, dict):
        return None
    roles = budget.get("roles")
    if isinstance(roles, list | tuple):
        for role in roles:
            if isinstance(role, dict) and role.get("role") == "extractor":
                max_calls = role.get("max_calls")
                if isinstance(max_calls, int) and max_calls >= 1:
                    return max_calls
    return None


def _live_extractor(extractor, graph):
    """Bind the run graph for a context-mode extractor; legacy passes through.

    Composition already built the context profile exactly when the frozen
    settings opted in, so the wrap decision reads the extractor itself — no
    snapshot shape change, and a context extractor can never run unbound
    (its own call would fail closed) nor silently fall back to legacy.
    """
    if getattr(extractor, "_context", False) is not True:
        return extractor
    from proofops_agent.upstage_extraction import GraphBoundExtractor

    return GraphBoundExtractor(extractor, graph)


class _ContinuingExtractor:
    """Replay prior model receipts, call the model only inside this batch window.

    A source with a prior model receipt for this exact packet hash is answered
    from that stored payload: no call, no charge, no duplicated claim. A source in
    the window is passed to the live extractor. Everything else is answered
    locally as deferred, so it stays eligible for a later batch instead of being
    recorded as absent. The first transport stop latches: the remaining sources
    become ``extraction_budget_stop`` and the batch still publishes the calls it
    already paid for.
    """

    def __init__(
        self,
        live,
        profile,
        prior,
        window,
        max_calls,
        *,
        prior_failed=None,
        prior_calls: int = 0,
        total_budget: int | None = None,
    ):
        self.profile = profile
        self._live = live
        self._prior = prior
        self._prior_failed = prior_failed or {}
        self._window = window
        self._max_calls = max_calls
        self._prior_calls = prior_calls
        self._total_budget = total_budget
        self.calls = 0
        self.replayed: set[str] = set()
        self.processed: set[str] = set()
        self.deferred: dict[str, str] = {}
        self.stop_code = None
        self.stop_source = None

    def extract(self, packet):
        data = packet["untrusted_document_data"]
        source_id, text = data["source_id"], data["text"]
        receipt = self._prior.get(source_id)
        if receipt is not None and receipt["packet_sha256"] == canonical_hash(packet):
            self.replayed.add(source_id)
            return json.loads(receipt["raw_response_json"])
        failed_receipt = self._prior_failed.get(source_id)
        if failed_receipt is not None and failed_receipt["packet_sha256"] == canonical_hash(packet):
            self.replayed.add(source_id)
            raise ExtractionOutputError("PREVIOUS_EXTRACTION_FAILED")
        reason = None
        if not _is_text_candidate(data["kind"], text):
            reason = DEFERRED_KIND_REASON
        elif self.stop_code is not None:
            reason = DEFERRED_BUDGET_REASON
        elif source_id not in self._window or self.calls >= self._max_calls:
            reason = DEFERRED_LIMIT_REASON
        elif (
            self._total_budget is not None and self._prior_calls + self.calls >= self._total_budget
        ):
            self.stop_code = "BUDGET_EXHAUSTED"
            self.stop_source = source_id
            reason = DEFERRED_BUDGET_REASON
        if reason is not None:
            self.deferred[source_id] = reason
            return deferred_payload(text, reason)
        self.calls += 1
        try:
            payload = self._live.extract(packet)
        except ValueError as error:
            code = str(error)
            if code not in UPSTAGE_TRANSPORT_STOP_CODES:
                raise
            # Keep this batch's earlier, already charged results instead of
            # failing the stage; this source and the rest stay pending.
            self.stop_code, self.stop_source = code, source_id
            self.deferred[source_id] = DEFERRED_BUDGET_REASON
            return deferred_payload(text, DEFERRED_BUDGET_REASON)
        self.processed.add(source_id)
        return payload


class LocalExtractRunner:
    def __init__(
        self, store, uploads, parser, *, extractor: ClaimExtractorPort, telemetry, clock=time.time
    ):
        if not uploads.local_synthetic:
            raise ValueError("local extraction requires local storage")
        self.store, self.uploads, self.parser = store, uploads, parser
        self.extractor, self.telemetry, self.clock = extractor, telemetry, clock
        self.claims = LocalClaimStore(store, uploads, parser)

    def _publication_payload(
        self, lease, snapshot, graph, profile, discovery, *, tenant_id, run_id
    ):
        """Assemble the extract checkpoint exactly as the single-shot stage does."""
        current = self.store.jobs.get_run(tenant_id, run_id)
        checkpoint = self.store.jobs.read_checkpoint(JobMessage(**current["parse_job"]))
        if checkpoint is None:
            raise ValueError("PARSE_NOT_PUBLISHED")
        payload = snapshot_pins(snapshot, graph, profile, checkpoint)
        payload.update(
            discovery=asdict(discovery),
            synthetic=discovery.synthetic,
            coverage=discovery_coverage(json.loads(checkpoint)["coverage"], graph, discovery),
        )
        if "claim_source_policy" in snapshot:
            from proofops.adapters.local.claim_source_policies import (
                attest_claims,
                publication_reader,
            )
            from proofops.adapters.local.claim_source_verification import discovery_refs
            from proofops.adapters.local.run_artifacts import load_run_inputs

            reader = publication_reader(snapshot["claim_source_policy"])
            _, source, _ = load_run_inputs(
                self.store, self.uploads, tenant_id=tenant_id, run_id=run_id
            )
            self.store.jobs.heartbeat(lease, now=int(self.clock()), lease_seconds=300)
            payload["claim_source_attestation"] = attest_claims(
                reader=reader,
                graph=graph,
                source=source.content,
                refs=discovery_refs(discovery),
                tenant_id=tenant_id,
                cache=True,
            )
        return payload

    def run_batch(self, *, tenant_id: str, run_id: str, max_calls: int | None = None) -> dict:
        """Process the next bounded window of eligible sources as a new revision.

        The published revision is a new immutable extract checkpoint under its own
        job id: the previous checkpoint stays byte-identical and readable by its
        own message, while the run head advances. Sources already model-processed
        are replayed from their stored receipts, so no provider call is repeated
        and no claim is duplicated. Returns an honest per-batch summary; it never
        reports a source as absent.
        """
        snapshot = self.store.snapshot(tenant_id, run_id)
        run = self.store.jobs.get_run(tenant_id, run_id)
        if run["cancellation_requested"]:
            return dict(status="cancelled")
        if "extract_job" not in run:
            return dict(status="no_revision", detail="run the extract stage once first")
        try:
            with self.store.jobs._transaction() as db:
                current = self.store.jobs._get(db, tenant_id, run_id, "run", "META")
                guard_extract_continuation(db, self.store.jobs, current, now=int(self.clock()))
        except ValueError as error:
            return dict(
                status="blocked",
                detail=(
                    "active tag lease in flight; cannot continue extraction"
                    if str(error) == "TAG_LEASE_CONFLICT"
                    else "cannot continue extraction after tag/review publication; start a new run"
                ),
            )
        # Strict verification of the prior revision before any receipt is reused:
        # this replays every stored receipt against the frozen pins and graph.
        prior_envelope, _, _ = self.claims.load_evidence(tenant_id, run_id)
        state = extraction_batch_state(prior_envelope)
        profile = extraction_profile(snapshot)
        limits = snapshot.get("extraction_limits")
        if not profile.synthetic:
            validate_extraction_limits(limits, snapshot["selected_pages"])
            if (
                snapshot.get("extraction_mode") != "upstage_probe"
                or getattr(self.extractor, "_max_tokens", None) != limits["max_output_tokens"]
            ):
                raise ValueError("EXTRACTION_PROFILE_MISMATCH")
        if profile != self.extractor.profile:
            raise ValueError("EXTRACTION_PROFILE_MISMATCH")
        frozen_calls = (limits or {}).get("max_calls", 1)
        if max_calls is not None:
            if type(max_calls) is not int or not 1 <= max_calls <= 20:
                raise ValueError("EXTRACTION_INPUT_INVALID")
            if max_calls > frozen_calls:
                raise ValueError("EXTRACTION_INPUT_INVALID")
            batch_calls = max_calls
        else:
            batch_calls = frozen_calls
        total_budget = _extractor_role_budget(snapshot)
        graph = load_run_graph(
            self.store, self.uploads, self.parser, tenant_id=tenant_id, run_id=run_id
        )
        scope = claim_scope(snapshot, graph)
        prior_charged = len(state["model_processed"]) + len(state["failed"])
        usage_records = self.store.jobs.list_usage(tenant_id, run_id)
        charged_from_usage = sum(
            row.get("extractor_calls", 0) for row in usage_records if isinstance(row, dict)
        )
        prior_calls = max(prior_charged, charged_from_usage)
        completed = set(state["model_processed"]) | set(state["failed"])
        window = select_batch_paragraph_sources(graph, scope, batch_calls, completed=completed)
        summary = dict(
            status="idle",
            revision_job_id=run["extract_job"]["job_id"],
            model_processed_before=len(completed),
            window=sorted(window),
            pending_before=sorted(state["pending"]),
        )
        if not window:
            return dict(summary, status="complete", pending_after=[], replayed=len(completed))
        base = JobMessage(**run["extract_job"])
        # One job per (stage, shard) is an immutable invariant of the job store, so
        # each batch revision takes its own shard. The first unused index is this
        # batch; an already published index is skipped, which makes the command
        # safe to re-run.
        message = None
        for index in range(1, 201):
            shard = f"{base.shard}-b{index}"
            candidate = replace(
                base,
                job_id=str(uuid5(UUID(base.job_id), f"extract-batch:{index}")),
                shard=shard[:128],
            )
            # Preserve existing legacy jobs; new long chains must not truncate
            # away their distinguishing suffix and collide with the prior batch.
            if len(shard) > 128 and self.store.jobs.delivery_status(candidate) == "not_found":
                candidate = replace(candidate, shard=f"extract-batch-{candidate.job_id}")
            if self.store.jobs.delivery_status(candidate) in {"not_found", "pending", "leased"}:
                message = candidate
                break
        if message is None:
            return dict(summary, status="batch_limit_reached")
        next_job = replace(message, job_id=str(uuid5(UUID(message.job_id), "tag")), stage="tag")
        if self.store.jobs.delivery_status(message) == "not_found":
            self.store.jobs.enqueue(message, now=int(self.clock()))
        usage = {"model_calls": 0, "extractor_calls": 0}
        outcome: dict = {}

        def operation(lease):
            since = 0 if profile.synthetic else self.extractor.usage_checkpoint()

            def sync_usage():
                if not profile.synthetic:
                    usage.update(
                        self.extractor.cumulative_usage(
                            since=since, parse_manifest_id=graph.parse_manifest_id
                        )
                    )

            runner = self
            live_extractor = _live_extractor(runner.extractor, graph)

            class FencedLive:
                profile_ = profile

                def extract(self, packet):
                    if not runner.store.jobs.can_call(lease, now=int(runner.clock())):
                        raise StageFailure("LEASE_LOST", usage=usage)
                    runner.store.jobs.heartbeat(lease, now=int(runner.clock()), lease_seconds=300)
                    if not profile.synthetic:
                        authorization = check_local_upstage_binding(
                            binding=snapshot["runtime"],
                            consent=snapshot["consent"],
                            auth=AuthContext(
                                "local-worker", tenant_id, "viewer", frozenset(), run_id
                            ),
                            checked_at=datetime.fromtimestamp(runner.clock(), UTC).isoformat(),
                            source_sha256=graph.source_sha256,
                            model_sha256=profile.model_sha256,
                        )
                        if not authorization.ready:
                            raise ValueError("LOCAL_TEST_AUTHORIZATION_INVALID")
                    usage["extractor_calls"] += 1
                    try:
                        return live_extractor.extract(packet)
                    finally:
                        sync_usage()

            try:
                extractor = _ContinuingExtractor(
                    FencedLive(),
                    profile,
                    state["model_processed"],
                    window,
                    batch_calls,
                    prior_failed=state.get("failed_receipts", {}),
                    prior_calls=prior_calls,
                    total_budget=total_budget,
                )
                discovery = discover_atomic_claims(graph, scope, extractor=extractor)
                payload = self._publication_payload(
                    lease, snapshot, graph, profile, discovery, tenant_id=tenant_id, run_id=run_id
                )
                outcome.update(
                    replayed=sorted(extractor.replayed),
                    processed=sorted(extractor.processed),
                    deferred=dict(sorted(extractor.deferred.items())),
                    stop_code=extractor.stop_code,
                    stop_source=extractor.stop_source,
                    claims=len(discovery.claims),
                )
                return canonical_json(payload).encode(), usage
            except LeaseLost:
                raise StageFailure("LEASE_LOST", usage=usage) from None
            except ValueError as error:
                code = str(error)
                if code not in UPSTAGE_TRANSPORT_STOP_CODES | {
                    "LOCAL_TEST_AUTHORIZATION_INVALID",
                    "ACCOUNTING_UNAVAILABLE",
                }:
                    code = "EXTRACTION_INPUT_INVALID"
                if code == "ACCOUNTING_UNAVAILABLE":
                    usage.update(model_calls=None, accounting_complete=False)
                raise StageFailure(code, usage=usage) from None
            except (KeyError, TypeError, ParseFailure, UploadRejected):
                raise StageFailure("EXTRACTION_INPUT_INVALID", usage=usage) from None

        result = consume_job(
            self.store.jobs,
            message,
            owner="local-extract-batch:" + str(uuid4()),
            clock=lambda: int(self.clock()),
            lease_seconds=300,
            operation=lambda lease: observe_job(
                self.telemetry,
                lease,
                lambda active_lease: with_lease_heartbeat(
                    self.store.jobs, active_lease, self.clock, operation
                ),
                context=TraceContext.new(tenant_id=tenant_id, run_id=run_id, job_id=message.job_id),
            ),
            next_job=next_job,
        )
        summary.update(status=result, usage=usage, **outcome)
        if result == "committed":
            published = extraction_batch_state(self.claims.load_evidence(tenant_id, run_id)[0])
            summary.update(
                revision_job_id=message.job_id,
                model_processed_after=len(published["model_processed"]),
                pending_after=sorted(published["pending"]),
                unreadable=sorted(published["failed"]),
                excluded=len(published["excluded"]),
                budget_stopped=sorted(published["budget_stopped"]),
            )
        return summary

    def run_once(self, *, tenant_id: str, run_id: str) -> str:
        snapshot = self.store.snapshot(tenant_id, run_id)
        run = self.store.jobs.get_run(tenant_id, run_id)
        if run["cancellation_requested"]:
            return "cancelled"
        for event in self.store.jobs.pending_outbox(tenant_id, run_id, now=int(self.clock())):
            message = JobMessage(**event["message"])
            if message.stage != "extract":
                continue
            next_job = replace(message, job_id=str(uuid5(UUID(message.job_id), "tag")), stage="tag")

            def operation(lease):
                usage = {"model_calls": 0, "extractor_calls": 0}
                is_real, since, manifest_id = False, 0, None

                def sync_usage():
                    if is_real:
                        usage.update(
                            self.extractor.cumulative_usage(
                                since=since, parse_manifest_id=manifest_id
                            )
                        )

                try:
                    profile = extraction_profile(snapshot)
                    is_real = not profile.synthetic
                    if is_real:
                        limits = snapshot.get("extraction_limits")
                        try:
                            validate_extraction_limits(limits, snapshot["selected_pages"])
                            valid_limits = True
                        except ValueError:
                            valid_limits = False
                        if (
                            not valid_limits
                            or snapshot.get("extraction_mode") != "upstage_probe"
                            or getattr(self.extractor, "_max_tokens", None)
                            != limits["max_output_tokens"]
                        ):
                            raise ValueError("EXTRACTION_PROFILE_MISMATCH")
                        since = self.extractor.usage_checkpoint()
                    if profile != self.extractor.profile or (
                        message.document_version_id != snapshot["document"]["version_id"]
                        or message.input_hash != snapshot["input_hash"]
                    ):
                        raise ValueError("EXTRACTION_PROFILE_MISMATCH")
                    graph = load_run_graph(
                        self.store, self.uploads, self.parser, tenant_id=tenant_id, run_id=run_id
                    )
                    runner = self
                    live_extractor = _live_extractor(runner.extractor, graph)
                    role_budget = _extractor_role_budget(snapshot)
                    effective_max = (
                        (
                            limits["max_calls"]
                            if role_budget is None
                            else min(limits["max_calls"], role_budget)
                        )
                        if is_real
                        else 0
                    )
                    eligible_sources: set[str] | None = None
                    if is_real:
                        eligible_sources = select_stable_paragraph_sources(
                            graph, claim_scope(snapshot, graph), effective_max
                        )

                    class FencedExtractor:
                        def __init__(self):
                            self.profile = profile
                            self.calls = 0

                        def extract(self, packet):
                            if not runner.store.jobs.can_call(lease, now=int(runner.clock())):
                                raise StageFailure("LEASE_LOST", usage=usage)
                            runner.store.jobs.heartbeat(
                                lease, now=int(runner.clock()), lease_seconds=300
                            )
                            if is_real:
                                data = packet["untrusted_document_data"]
                                reason = (
                                    "non_paragraph_kind"
                                    if not _is_text_candidate(data["kind"], data["text"])
                                    else "beyond_extraction_limit"
                                    if data.get("source_id") not in eligible_sources
                                    or self.calls >= effective_max
                                    else None
                                )
                                if reason:
                                    return {
                                        "spans": [
                                            dict(
                                                char_start=0,
                                                char_end=len(data["text"]),
                                                quote=data["text"],
                                                kind="unknown",
                                                reason=reason,
                                                topic_ids=[],
                                            )
                                        ]
                                    }
                                authorization = check_local_upstage_binding(
                                    binding=snapshot["runtime"],
                                    consent=snapshot["consent"],
                                    auth=AuthContext(
                                        "local-worker", tenant_id, "viewer", frozenset(), run_id
                                    ),
                                    checked_at=datetime.fromtimestamp(
                                        runner.clock(), UTC
                                    ).isoformat(),
                                    source_sha256=graph.source_sha256,
                                    model_sha256=profile.model_sha256,
                                )
                                if not authorization.ready:
                                    raise ValueError("LOCAL_TEST_AUTHORIZATION_INVALID")
                            self.calls += 1
                            usage["extractor_calls"] += 1
                            try:
                                return live_extractor.extract(packet)
                            finally:
                                sync_usage()

                    discovery = discover_atomic_claims(
                        graph, claim_scope(snapshot, graph), extractor=FencedExtractor()
                    )
                    current = self.store.jobs.get_run(tenant_id, run_id)
                    checkpoint = self.store.jobs.read_checkpoint(JobMessage(**current["parse_job"]))
                    if checkpoint is None:
                        raise ValueError("PARSE_NOT_PUBLISHED")
                    payload = snapshot_pins(snapshot, graph, profile, checkpoint)
                    coverage = discovery_coverage(
                        json.loads(checkpoint)["coverage"], graph, discovery
                    )
                    payload.update(
                        discovery=asdict(discovery),
                        synthetic=discovery.synthetic,
                        coverage=coverage,
                    )
                    if "claim_source_policy" in snapshot:
                        from proofops.adapters.local.claim_source_policies import (
                            attest_claims,
                            publication_reader,
                        )
                        from proofops.adapters.local.claim_source_verification import (
                            discovery_refs,
                        )
                        from proofops.adapters.local.run_artifacts import load_run_inputs

                        reader = publication_reader(snapshot["claim_source_policy"])
                        _, source, _ = load_run_inputs(
                            self.store, self.uploads, tenant_id=tenant_id, run_id=run_id
                        )
                        self.store.jobs.heartbeat(lease, now=int(self.clock()), lease_seconds=300)
                        payload["claim_source_attestation"] = attest_claims(
                            reader=reader,
                            graph=graph,
                            source=source.content,
                            refs=discovery_refs(discovery),
                            tenant_id=tenant_id,
                            cache=False,
                        )
                    return canonical_json(payload).encode(), usage
                except LeaseLost:
                    raise StageFailure("LEASE_LOST", usage=usage) from None
                except ValueError as error:
                    code = str(error)
                    if code not in UPSTAGE_TRANSPORT_STOP_CODES | {
                        "LOCAL_TEST_AUTHORIZATION_INVALID",
                        "ACCOUNTING_UNAVAILABLE",
                    }:
                        code = "EXTRACTION_INPUT_INVALID"
                    if code == "ACCOUNTING_UNAVAILABLE":
                        usage.update(model_calls=None, accounting_complete=False)
                    raise StageFailure(code, usage=usage) from None
                except (KeyError, TypeError, ParseFailure, UploadRejected):
                    raise StageFailure("EXTRACTION_INPUT_INVALID", usage=usage) from None

            result = consume_job(
                self.store.jobs,
                message,
                owner="local-extract:" + str(uuid4()),
                clock=lambda: int(self.clock()),
                lease_seconds=300,
                operation=lambda lease: observe_job(
                    self.telemetry,
                    lease,
                    lambda active_lease: with_lease_heartbeat(
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
        return "pending_downstream" if run.get("current_stage") == "tag" else "idle"
