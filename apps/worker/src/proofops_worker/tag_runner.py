"""Explicit local tagging stage with durable receipts and one fenced publication.

The actual parser's unverified graph stays fast_preview. Synthetic transport and
source-backed preliminary classification must be explicitly supplied; neither a
missing track nor absent runtime configuration selects a default product model.
"""

import json
import time
from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID, uuid4, uuid5

from proofops.adapters.cache.aws import ImmutableResponseCache
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.adapters.local.evidence_search import (
    LocalEvidenceSearch,
    collect_raw_candidate_review,
)
from proofops.adapters.local.gri_routing import LocalGRIRouting
from proofops.adapters.local.review_store import LocalSQLiteReviewStore
from proofops.adapters.local.tag_cache import SQLiteImmutableCacheClient
from proofops.adapters.local.tag_store import LocalTagStore, tag_pins, tagging_settings
from proofops.application.evidence.citations import _normalized
from proofops.application.evidence.retrieval import (
    SearchResult,
    freeze_track_packet,
    retrieve_evidence,
)
from proofops.application.ports.jobs import JobMessage, LeaseLost
from proofops.application.reviews import ReviewInputs, ReviewService
from proofops.application.tagging.consensus import (
    form_consensus,
    reviewable_checkpoint,
)
from proofops.application.tagging.consensus import (
    reviewable_decision as _review_decision,
)
from proofops.application.tagging.report_level_link import apply_report_level_link, strict_fallback
from proofops.application.tagging.service import tag_replicates
from proofops.application.telemetry import TraceContext
from proofops.domain.rulepacks import RulePackSnapshot, canonical_json
from proofops.domain.rules.engine import RuleContext, evaluate

from proofops_worker.consumer import (
    LeaseHeartbeatState,
    StageFailure,
    TagHeartbeatFailed,
    with_lease_heartbeat,
)
from proofops_worker.tag_recovery import TagRecovery
from proofops_worker.tag_reprocess import TagReprocess
from proofops_worker.telemetry import observe_job


class _TagFenceLost(BaseException):
    """Escape tagger's provider-error boundary without fabricating a response."""


def _unexpected_error_code(stage, error):
    name = type(error).__name__
    if not name.isascii() or not name.isidentifier() or len(name) > 64:
        name = "Exception"
    return f"{stage.upper()}_UNEXPECTED_{name}"


def _candidate_tokens(text):
    """Retrieval size bound for blocked claims when no tagging runtime is wired.

    Deterministic byte length only; it never selects or contacts a model.
    """
    return len(text.encode("utf-8"))


def _consensus_review_reason(consensus):
    return (
        "PARTIAL_FACTS_REVIEW_REQUIRED"
        if "PARTIAL_FACTS_REVIEW_REQUIRED" in consensus.reasons
        else "CONSENSUS_UNRESOLVED"
    )


def _prior_claim_record(claim_id, prior, rulepack_unapproved, *, pinned_profile=None):
    decision, candidate_grade = _review_decision(
        prior.decision, prior.fact_assembly_profile, prior.consensus.review_status
    )
    if rulepack_unapproved:
        reason = "DOMAIN_RULEPACK_UNAPPROVED"
    elif candidate_grade is not None or prior.consensus.confirmed_tags is None:
        reason = _consensus_review_reason(prior.consensus)
    elif decision and decision.decision_status != "decided":
        reason = decision.decision_status
    else:
        reason = None
    return reviewable_checkpoint(
        dict(
            claim_id=claim_id,
            status=(
                "completed"
                if decision and decision.decision_status == "decided"
                else "needs_review"
            ),
            reason=reason,
            tag_runs=[asdict(result) for result in prior.tag_runs],
            decision=asdict(decision) if decision else None,
            candidate_grade=candidate_grade,
            review_inputs=prior.snapshot(),
        ),
        pinned_profile=pinned_profile,
    )


def _source_traceable(claim, graph):
    """Does every claim ref still point at the pinned parsed text of its block?

    This is the precondition for letting a blocked claim reach local candidate
    retrieval, not a verification: source-quality attestation is exactly the gate
    such a claim is waiting on, so quality is deliberately not required here.
    A ref whose page, label, box, raw-text digest, offsets or literal quote
    disagrees with the winner candidate is not a traceable source, so its
    untrusted text never reaches search, GRI routing, a packet or any model.
    Verification (verify_source_ref) still decides everything downstream.
    """
    blocks = {block.source_id: block for block in graph.blocks}
    pinned = (
        "document_version_id",
        "parse_manifest_id",
        "page_num",
        "printed_page_label",
        "bbox",
        "raw_text_sha256",
    )
    for ref in claim.source_refs:
        block = blocks.get(ref.source_id)
        if block is None or block.winner is None or ref.location_quality != "located":
            return False
        canonical = block.source_ref()
        if (
            any(getattr(ref, name) != getattr(canonical, name) for name in pinned)
            or not 0 <= ref.char_start < ref.char_end <= len(block.raw_text)
            or _normalized(ref.quote) != _normalized(block.raw_text[ref.char_start : ref.char_end])
        ):
            return False
    return bool(claim.source_refs)


class _LocalClaimSearch:
    """Bounded synthetic local search; only retrieval's verified atomic source is used."""

    synthetic = True

    def search(self, scope, query, *, vector=None):
        return SearchResult(status="bounded")


class LocalTagRunner:
    def __init__(
        self,
        store,
        uploads,
        parser,
        *,
        telemetry,
        transport=None,
        preliminary=None,
        live_factory=None,
        clock=time.time,
    ):
        if not uploads.local_synthetic:
            raise ValueError("local tagging requires local storage")
        if transport is not None and getattr(transport, "synthetic", None) is not True:
            raise ValueError("explicit synthetic transport required")
        self.store, self.uploads, self.parser = store, uploads, parser
        self.telemetry, self.transport, self.preliminary, self.clock = (
            telemetry,
            transport,
            preliminary,
            clock,
        )
        # Set only while an explicit recovery job is executing, so the live runtime
        # can read the bounded acknowledged authorization for its transports.
        self.resume = None
        self.claims = LocalClaimStore(store, uploads, parser)
        self.live_factory = live_factory
        self.tags = LocalTagStore(store, uploads, parser)
        self.cache = ImmutableResponseCache(SQLiteImmutableCacheClient(store.path))
        self.reviews = ReviewService(
            LocalSQLiteReviewStore(store.jobs), load_inputs=self.tags.load_inputs
        )

    def _publish_claim(self, lease, inputs, heartbeat_state=None):
        jobs = self.store.jobs
        tenant, run_id = lease.message.tenant_id, lease.message.run_id
        if heartbeat_state is not None:
            heartbeat_state.check()
        review = self.reviews._review(inputs)
        lock = heartbeat_state.lock if heartbeat_state is not None else nullcontext()
        with lock:
            if heartbeat_state is not None:
                heartbeat_state.check()
            with jobs._transaction() as db:
                if jobs._owned(db, lease, int(self.clock())) is None:
                    raise LeaseLost("LEASE_LOST")
                self.reviews.store.publish_transaction(db, inputs, review)
                jobs._bump_run(db, jobs._get(db, tenant, run_id, "run", "META"))

    def _execute(self, lease, snapshot, usage, recovery=None, reprocess=None, heartbeat_state=None):
        message = lease.message
        tenant, run_id = message.tenant_id, message.run_id
        extraction, discovery, graph = self.claims.load_evidence(tenant, run_id)
        run = self.store.jobs.get_run(tenant, run_id)
        if (message.input_hash, message.document_version_id) != (
            snapshot["input_hash"],
            graph.document_version_id,
        ):
            raise ValueError("TAG_INPUT_MISMATCH")
        envelope = tag_pins(snapshot, extraction, run["claim_snapshot_sha256"])
        rulepack = RulePackSnapshot(**snapshot["rulepack"])
        settings = tagging_settings(snapshot) if snapshot.get("tagging_settings") else None
        live = None
        transport, preliminary_supplier = self.transport, self.preliminary
        if snapshot.get("tagging_mode") == "upstage_local":
            if self.live_factory is None:
                raise ValueError("LIVE_TAGGING_RUNTIME_REQUIRED")
            live = self.live_factory(self, snapshot, graph, lease, usage)
            live.heartbeat_state = heartbeat_state
            if live.synthetic is not False:
                raise ValueError("LIVE_TAGGING_PROVENANCE_REQUIRED")
            transport, preliminary_supplier = live, live.preliminary
        search = _LocalClaimSearch()
        generation = "local-atomic-only-v1"
        gri = None
        if live is not None:
            pages = sorted(set(snapshot["selected_pages"]) & {b.page_num for b in graph.blocks})
            generation = "local-lexical-gri-v1:" + snapshot["input_hash"]
            search = LocalEvidenceSearch(
                graph, tenant_id=tenant, pages=pages, index_generation=generation
            )
            gri = LocalGRIRouting(
                graph,
                self.uploads.read_original(tenant, graph.document_version_id),
                tenant_id=tenant,
                pages=pages,
            )
        raw_search = search if isinstance(search, LocalEvidenceSearch) else None
        if raw_search is None:
            avail_pages = sorted(
                (set(snapshot.get("selected_pages") or ()) or {b.page_num for b in graph.blocks})
                & {b.page_num for b in graph.blocks}
            )
            if avail_pages:
                raw_gen = "local-raw-review-v1:" + snapshot["input_hash"]
                raw_search = LocalEvidenceSearch(
                    graph, tenant_id=tenant, pages=avail_pages, index_generation=raw_gen
                )
        records = []
        report_source_cache = {}
        for claim in discovery.claims:
            if heartbeat_state is not None:
                heartbeat_state.check()
            if not self.store.jobs.can_call(lease, now=int(self.clock())):
                raise LeaseLost("LEASE_LOST")
            if "tag_job" not in run and recovery is None and reprocess is None:
                with self.store.jobs._transaction() as db:
                    prior_revision = self.store.jobs._raw(
                        db,
                        tenant,
                        run_id,
                        "tag_revision",
                        f"{claim.claim_id}:{1:010}",
                    )
                if prior_revision is not None:
                    prior = self.tags.load_inputs(tenant, run_id, claim.claim_id)
                    records.append(
                        _prior_claim_record(
                            claim.claim_id,
                            prior,
                            snapshot.get("rulepack_use") == "candidate_tagging_reference_only",
                            pinned_profile=snapshot.get("fact_assembly_profile", "strict-v1"),
                        )
                    )
                    continue
            # An explicit recovery attempts only its bounded authorized claims and
            # carries every other claim forward exactly as the paid stage committed
            # it: no reprocessing, no new call and no second publication of an
            # already immutable revision. A claim is also carried forward, untouched,
            # once the remaining bound can no longer finish it, so the stage never
            # publishes a half-tagged claim that could then never be retried.
            if recovery is not None and (
                claim.claim_id not in recovery.claim_ids or not recovery.can_attempt_claim()
            ):
                records.append(
                    reviewable_checkpoint(
                        recovery.carry_forward(claim.claim_id),
                        pinned_profile=snapshot.get("fact_assembly_profile", "strict-v1"),
                    )
                )
                continue
            # A manual-classification reprocess attempts exactly its one authorized
            # claim and carries every other committed claim forward verbatim: no
            # reprocessing, no new call and no second publication of an already
            # immutable revision. The target claim is also carried forward untouched
            # once the bound can no longer finish it, so the stage never publishes a
            # half-tagged claim.
            if reprocess is not None and (
                claim.claim_id not in reprocess.claim_ids or not reprocess.can_attempt_claim()
            ):
                records.append(
                    reviewable_checkpoint(
                        reprocess.carry_forward(claim.claim_id),
                        pinned_profile=snapshot.get("fact_assembly_profile", "strict-v1"),
                    )
                )
                continue
            raw_candidate_review = None
            if raw_search is not None and _source_traceable(claim, graph):
                raw_candidate_review = collect_raw_candidate_review(raw_search, claim.quote)
            reason = (
                "SOURCE_VALIDATION_REQUIRED"
                if claim.source_quality != "verified"
                else "TAGGING_RUNTIME_REQUIRED"
                if settings is None or transport is None
                else "PRELIMINARY_TAGS_REQUIRED"
                if preliminary_supplier is None
                else None
            )
            item = dict(
                claim_id=claim.claim_id,
                status="blocked",
                reason=reason,
                tag_runs=[],
                decision=None,
                review_inputs=None,
                raw_candidate_review=raw_candidate_review,
            )
            track = context = relation_tags = None
            # Paid preliminary replicas stay behind the source-quality gate; an
            # unverified source never buys model calls to make progress.
            if reason is None:
                if reprocess is not None and claim.claim_id in reprocess.claim_ids:
                    # A manual-classification reprocess supplies the reviewer's
                    # recorded, source-verified classification instead of a model
                    # preliminary call. The element stage below still runs its real
                    # replicas under the pinned run mode, so no grade is invented and
                    # no model vote is fabricated. The prior preliminary_records /
                    # preliminary_agreement of the blocked stage are preserved on the
                    # carried record for every other claim; this target claim records
                    # the distinct reviewed classification instead.
                    from proofops.application.evidence.binding import local_relation_tags
                    from proofops.application.tagging.manual_classification import (
                        ClassificationRejected,
                        classification_override,
                    )

                    try:
                        classified = classification_override(
                            reprocess.classification, claim, graph, tenant_id=tenant
                        )
                    except ClassificationRejected as error:
                        raise ValueError("REPROCESS_CLASSIFICATION_INVALID") from error
                    track, context = classified.track, classified.context
                    relation_tags = local_relation_tags(context)
                    # Preserve the prior blocked stage's original preliminary provenance
                    # for this claim; the reviewed classification is additive, never a
                    # replacement of what the model replicas actually recorded.
                    prior = reprocess.carry_forward(claim.claim_id)
                    if "preliminary_records" in prior:
                        item["preliminary_records"] = prior["preliminary_records"]
                    if "preliminary_agreement" in prior:
                        item["preliminary_agreement"] = prior["preliminary_agreement"]
                    item["reviewed_classification"] = dict(
                        classification_id=reprocess.plan.classification_id,
                        classification_sha256=reprocess.plan.classification_sha256,
                        origin=reprocess.classification.get("origin"),
                        classified_by=reprocess.classification.get("classified_by"),
                        review_origin=reprocess.classification.get("review_origin"),
                        track=track.track,
                        safe_harbor_category=classified.safe_harbor_category,
                    )
                    if context.claim != claim or track.claim != claim:
                        raise ValueError("PRELIMINARY_CLAIM_MISMATCH")
                else:
                    preliminary = preliminary_supplier(claim, graph)
                    if live is not None:
                        item["preliminary_records"] = live.preliminary_records.get(
                            claim.claim_id, []
                        )
                        item["preliminary_agreement"] = live.preliminary_agreement(claim.claim_id)
                    track, context, relation_tags = (
                        (None, None, None) if preliminary is None else preliminary
                    )
                    if track is None:
                        reason = "PRELIMINARY_TAGS_UNRESOLVED"
                    elif context.claim != claim or track.claim != claim:
                        raise ValueError("PRELIMINARY_CLAIM_MISMATCH")
            # Local, no-model candidate retrieval runs before the source and
            # consensus stops, so a blocked claim still reaches review with its
            # traceable candidates and the recorded reasons instead of nothing.
            # Identity, raw text and tenant mismatches still raise from here. A
            # blocked claim is skipped when its refs no longer point at the pinned
            # parsed text, or when this run has no approved element catalog to
            # scope candidates with; no catalog and no location is ever invented,
            # and a claim that must be tagged still fails here exactly as before.
            original_packet = None
            skipped = None
            if reason is None:
                pass
            elif not _source_traceable(claim, graph):
                skipped = "SOURCE_LOCATION_REQUIRED"
            elif "rubric/elements.yaml" not in rulepack.files:
                skipped = "RULEPACK_CATALOG_REQUIRED"
            if skipped is None:
                gri_entries, indicator_codes = gri.for_claim(claim.quote) if gri else ((), ())
                original_packet = retrieve_evidence(
                    claim,
                    graph,
                    search,
                    tenant_id=tenant,
                    run_id=run_id,
                    index_generation=generation,
                    rulepack=rulepack,
                    document_context={},
                    token_counter=transport.token_counter if transport else _candidate_tokens,
                    gri_entries=gri_entries,
                    indicator_codes=indicator_codes,
                )
            if reason:
                # An unverified source keeps blocked_evidence here, so the track
                # packet gate below can never accept it either.
                item.update(
                    reason=reason,
                    **(
                        dict(candidate_retrieval=skipped)
                        if skipped is not None
                        else dict(original_packet=original_packet.to_dict())
                    ),
                )
                records.append(item)
                continue
            if original_packet.to_dict()["status"] != "candidate":
                item.update(
                    reason="EVIDENCE_PACKET_BLOCKED", original_packet=original_packet.to_dict()
                )
                records.append(item)
                continue
            packet = freeze_track_packet(original_packet, track=track, rulepack=rulepack)
            if live is not None:
                live.allow_packet(claim.claim_id, packet.packet_sha256)
                if snapshot.get("relation_settings") is not None:
                    external_roles = live.relations(claim, packet)
                    item["relation_records"] = live.relation_records.get(claim.claim_id, [])
                    if external_roles is None:
                        item.update(
                            reason="RELATION_TAGS_UNRESOLVED",
                            original_packet=original_packet.to_dict(),
                        )
                        records.append(item)
                        continue
                    # Atomic scoped roles keep precedence over any whole-source map.
                    relation_tags = {**external_roles, **relation_tags}

            def invoke(request):
                # The budget service records actual completed usage even if the fence is
                # lost in-flight. The fenced usage wrapper below stops BEFORE reservation.
                if heartbeat_state is not None:
                    heartbeat_state.check()
                if not self.store.jobs.can_call(lease, now=int(self.clock())):
                    raise _TagFenceLost()
                try:
                    self.store.jobs.heartbeat(lease, now=int(self.clock()), lease_seconds=300)
                except Exception:
                    if heartbeat_state is not None:
                        heartbeat_state.fail()
                    raise TagHeartbeatFailed() from None
                if heartbeat_state is not None:
                    heartbeat_state.last_successful_at = int(self.clock())
                    heartbeat_state.check()
                if live is None:
                    usage["synthetic_calls"] += 1
                return transport.invoke(request)

            runner = self

            class FencedUsage:
                def __getattr__(self, name):
                    return getattr(runner.store.usage, name)

                def reserve_budget(self, *args, **kwargs):
                    if heartbeat_state is not None:
                        heartbeat_state.check()
                    if not runner.store.jobs.can_call(lease, now=int(runner.clock())):
                        raise LeaseLost("LEASE_LOST")
                    try:
                        runner.store.jobs.heartbeat(
                            lease, now=int(runner.clock()), lease_seconds=300
                        )
                    except Exception:
                        if heartbeat_state is not None:
                            heartbeat_state.fail()
                        raise TagHeartbeatFailed() from None
                    if heartbeat_state is not None:
                        heartbeat_state.last_successful_at = int(runner.clock())
                        with heartbeat_state.lock:
                            heartbeat_state.check()
                            return runner.store.usage.reserve_budget(*args, **kwargs)
                    return runner.store.usage.reserve_budget(*args, **kwargs)

                def mark_dispatched(self, call):
                    if heartbeat_state is not None:
                        heartbeat_state.check()
                    if not runner.store.jobs.can_call(lease, now=int(runner.clock())):
                        raise LeaseLost("LEASE_LOST")
                    if heartbeat_state is not None:
                        with heartbeat_state.lock:
                            heartbeat_state.check()
                            return runner.store.usage.mark_dispatched(call)
                    return runner.store.usage.mark_dispatched(call)

            tag_runs = tag_replicates(
                packet,
                context=context,
                track=track,
                original=graph,
                relation_tags=relation_tags,
                rulepack=rulepack,
                settings=settings,
                cache=self.cache,
                usage_store=FencedUsage(),
                invoke=invoke,
                tenant_id=tenant,
                ensemble_id=str(uuid5(UUID(message.job_id), claim.claim_id)),
                consent_profile=snapshot["consent"]["consent_profile_id"],
                token_counter=transport.token_counter,
                count_input_tokens=live.count_input_tokens if live is not None else None,
                now=lambda: int(self.clock()),
            )
            if live is not None:
                for tag_run in tag_runs:
                    live.account(tag_run.request.request_id)
            if heartbeat_state is not None:
                heartbeat_state.check()
            if not self.store.jobs.can_call(lease, now=int(self.clock())):
                raise LeaseLost("LEASE_LOST")
            consensus = form_consensus(
                tag_runs,
                packet=packet,
                rulepack=rulepack,
                tenant_id=tenant,
                tag_revision=1,
                profile=snapshot.get("fact_assembly_profile", "strict-v1"),
            )
            rule_context = RuleContext(
                tenant,
                graph.document_version_id,
                claim.claim_id,
                packet.packet_sha256,
                mode=snapshot["mode"],
                local_synthetic=live is None,
            )
            link_config = snapshot.get("report_level_link")
            link_receipts = ()
            if link_config is not None:

                def attest_report_sources(refs):
                    if refs not in report_source_cache:
                        report_source_cache[refs] = self.tags.verify_context_sources(
                            base_inputs,
                            refs,
                            pinned_run_snapshot=snapshot,
                        )
                    return report_source_cache[refs]

                base_inputs = ReviewInputs(
                    run_id,
                    context,
                    graph,
                    rulepack,
                    rule_context,
                    packet,
                    original_packet,
                    tag_runs,
                    consensus,
                    relation_tags,
                    fact_assembly_profile=snapshot.get("fact_assembly_profile", "strict-v1"),
                )
                consensus, link_receipts = apply_report_level_link(
                    consensus,
                    config=link_config,
                    context=context,
                    graph=graph,
                    fallback_tags=strict_fallback(
                        tag_runs,
                        packet,
                        rulepack,
                        tenant,
                        1,
                        snapshot.get("fact_assembly_profile", "strict-v1"),
                    ),
                    attest=attest_report_sources,
                )
            rulepack_unapproved = snapshot.get("rulepack_use") == "candidate_tagging_reference_only"
            decision = (
                evaluate(consensus.confirmed_tags, rule_context, rulepack)
                if consensus.confirmed_tags and not rulepack_unapproved
                else None
            )
            decision, candidate_grade = _review_decision(
                decision,
                snapshot.get("fact_assembly_profile", "strict-v1"),
                consensus.review_status,
            )
            # Explicit needs_review diagnostic: never collapse the "why" to None.
            # Domain approval gate outranks consensus, which outranks rule holds.
            if rulepack_unapproved:
                review_reason = "DOMAIN_RULEPACK_UNAPPROVED"
            elif candidate_grade is not None or not consensus.confirmed_tags:
                review_reason = _consensus_review_reason(consensus)
            elif decision and decision.decision_status != "decided":
                review_reason = decision.decision_status
            else:
                review_reason = None
            inputs = ReviewInputs(
                run_id,
                context,
                graph,
                rulepack,
                rule_context,
                packet,
                original_packet,
                tag_runs,
                consensus,
                relation_tags,
                decision=decision,
                fact_assembly_profile=snapshot.get("fact_assembly_profile", "strict-v1"),
                report_level_link=link_config,
                report_level_review=link_receipts,
            )
            self._publish_claim(lease, inputs, heartbeat_state)
            item.update(
                status="completed"
                if decision and decision.decision_status == "decided"
                else "needs_review",
                reason=review_reason,
                tag_runs=[asdict(r) for r in tag_runs],
                decision=asdict(decision) if decision else None,
                candidate_grade=candidate_grade,
                review_inputs=inputs.snapshot(),
            )
            records.append(item)
        decided = sum(item["status"] == "completed" for item in records)
        stage_status = (
            "blocked"
            # Carried-forward claims already hold published review inputs, so the
            # stage is judged on the whole record set rather than on what this one
            # operation happened to publish; a recovery never regresses a stage
            # that is already in review.
            if not any(item.get("review_inputs") is not None for item in records)
            else "needs_review"
            if decided != len(records)
            else "completed"
        )
        envelope.update(
            claims=records,
            stage_status=stage_status,
            downstream_status="human_review",
            coverage=dict(
                extraction["coverage"],
                claims_decided=decided,
                claims_needs_review=len(records) - decided,
                complete=False,
            ),
            synthetic=live is None,
        )
        if recovery is not None:
            # Auditable record of what this operation actually attempted, carried and
            # spent, kept beside the stage it committed.
            envelope["recovery"] = recovery.summary()
        if reprocess is not None:
            envelope["reprocess"] = reprocess.summary()
        return canonical_json(envelope).encode()

    def run_once(self, *, tenant_id: str, run_id: str) -> str:
        snapshot = self.store.snapshot(tenant_id, run_id)
        jobs = self.store.jobs
        run = jobs.get_run(tenant_id, run_id)
        if run["cancellation_requested"]:
            return "cancelled"
        for event in jobs.pending_outbox(tenant_id, run_id, now=int(self.clock())):
            message = JobMessage(**event["message"])
            if message.stage != "tag":
                continue
            # Resolved before any lease is taken: a stale or superseded recovery
            # authorization must fail closed without consuming an attempt.
            recovery = TagRecovery.load(self.store, self.tags, message)
            if recovery is not None:
                recovery.verify_published(self.store)
            # A manual-classification reprocess is loaded the same way and is mutually
            # exclusive with a recovery on the same message (each has its own shard and
            # job id, so only one plan kind ever matches a given message).
            reprocess = TagReprocess.load(self.store, self.tags, message)
            if reprocess is not None:
                reprocess.verify_published(self.store)
            if recovery is not None and reprocess is not None:
                raise ValueError("TAG_JOB_PLAN_AMBIGUOUS")
            lease = jobs.claim_job(
                message, owner="local-tag:" + str(uuid4()), now=int(self.clock()), lease_seconds=300
            )
            if lease is None:
                if jobs.delivery_status(message) in {"pending", "leased"}:
                    return "deferred"
                jobs.mark_outbox(
                    tenant_id,
                    run_id,
                    event["event_id"],
                    now=int(self.clock()),
                    sent=True,
                    expected_attempts=event["attempts"],
                )
                return "ignored"
            usage = {"model_calls": 0, "synthetic_calls": 0}
            heartbeat_state = LeaseHeartbeatState(int(self.clock()))

            def operation(owned):
                # An explicit recovery or reprocess job carries its own bounded
                # authorization, visible to the live runtime only while it runs.
                # Ordinary jobs have none, so nothing about their stop, receipt or
                # budget behavior changes.
                self.resume = (
                    recovery.resume
                    if recovery is not None
                    else reprocess.resume
                    if reprocess is not None
                    else None
                )
                try:
                    payload = self._execute(
                        owned,
                        snapshot,
                        usage,
                        recovery,
                        reprocess,
                        heartbeat_state=heartbeat_state,
                    )
                finally:
                    self.resume = None
                return payload, usage

            try:
                payload, _ = observe_job(
                    self.telemetry,
                    lease,
                    lambda active_lease: with_lease_heartbeat(
                        jobs, active_lease, self.clock, operation, state=heartbeat_state
                    ),
                    context=TraceContext.new(
                        tenant_id=tenant_id, run_id=run_id, job_id=message.job_id
                    ),
                    heartbeat_state=heartbeat_state,
                )
            except StageFailure as failure:
                # The keepalive itself lost the lease mid-operation (e.g. a slow
                # step between two point-heartbeats outlived the lease window).
                # This is a distinct, recoverable cause from an input/logic error:
                # record it as such instead of folding it into TAG_INPUT_INVALID.
                jobs.record_usage(lease, failure.usage)
                try:
                    jobs.fail_job(lease, error_code=failure.error_code, now=int(self.clock()))
                except LeaseLost:
                    return (
                        failure.error_code
                        if failure.error_code == "LEASE_HEARTBEAT_FAILED"
                        else "discarded"
                    )
                jobs.mark_outbox(
                    tenant_id,
                    run_id,
                    event["event_id"],
                    now=int(self.clock()),
                    sent=True,
                    expected_attempts=event["attempts"],
                )
                return (
                    failure.error_code
                    if failure.error_code == "LEASE_HEARTBEAT_FAILED"
                    else "failed"
                )
            except TagHeartbeatFailed:
                jobs.record_usage(lease, usage)
                try:
                    jobs.fail_job(lease, error_code="LEASE_HEARTBEAT_FAILED", now=int(self.clock()))
                except LeaseLost:
                    return "LEASE_HEARTBEAT_FAILED"
                jobs.mark_outbox(
                    tenant_id,
                    run_id,
                    event["event_id"],
                    now=int(self.clock()),
                    sent=True,
                    expected_attempts=event["attempts"],
                )
                return "LEASE_HEARTBEAT_FAILED"
            except (LeaseLost, _TagFenceLost):
                jobs.record_usage(lease, usage)
                try:
                    jobs.fail_job(lease, error_code="LEASE_LOST", now=int(self.clock()))
                except LeaseLost:
                    return "LEASE_LOST"
                jobs.mark_outbox(
                    tenant_id,
                    run_id,
                    event["event_id"],
                    now=int(self.clock()),
                    sent=True,
                    expected_attempts=event["attempts"],
                )
                return "LEASE_LOST"
            except (ValueError, KeyError, TypeError):
                jobs.record_usage(lease, usage)
                try:
                    jobs.fail_job(lease, error_code="TAG_INPUT_INVALID", now=int(self.clock()))
                except LeaseLost:
                    return "discarded"
                jobs.mark_outbox(
                    tenant_id,
                    run_id,
                    event["event_id"],
                    now=int(self.clock()),
                    sent=True,
                    expected_attempts=event["attempts"],
                )
                return "failed"
            except Exception as error:
                jobs.record_usage(lease, usage)
                try:
                    jobs.fail_job(
                        lease,
                        error_code=_unexpected_error_code(message.stage, error),
                        now=int(self.clock()),
                    )
                except LeaseLost:
                    return "discarded"
                jobs.mark_outbox(
                    tenant_id,
                    run_id,
                    event["event_id"],
                    now=int(self.clock()),
                    sent=True,
                    expected_attempts=event["attempts"],
                )
                return "failed"
            jobs.record_usage(lease, usage)

            def publish(db):
                if reprocess is not None:
                    reprocess.verify_publication(self.store, db)
                pending = jobs._get(db, tenant_id, run_id, "outbox", event["event_id"])
                if pending["status"] != "pending" or pending["attempts"] != event["attempts"]:
                    raise ValueError("TAG_OUTBOX_FENCE_MISMATCH")
                pending.update(status="sent", attempts=pending["attempts"] + 1)
                jobs._put(db, tenant_id, run_id, "outbox", event["event_id"], pending)

            if not jobs.commit_job(lease, payload=payload, now=int(self.clock()), publish=publish):
                return "discarded"
            return json.loads(payload)["stage_status"]
        if "tag_job" in run:
            return self.tags.load_snapshot(tenant_id, run_id)["stage_status"]
        return "idle"
