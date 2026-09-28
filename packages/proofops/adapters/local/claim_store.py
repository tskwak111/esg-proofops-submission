"""Immutable local extraction snapshots; stored-response replay never calls a model."""

from __future__ import annotations

import json
from dataclasses import asdict
from hashlib import sha256

from proofops.adapters.local.run_artifacts import load_run_graph
from proofops.application.claim_scope import claim_pages_for
from proofops.application.claims import ClaimScope, ExtractionProfile, discover_atomic_claims
from proofops.application.ports.jobs import JobMessage
from proofops.domain.provenance import canonical_hash


def extraction_profile(snapshot):
    profile = ExtractionProfile(**snapshot["extraction_profile"])
    if canonical_hash(asdict(profile)) != snapshot["extraction_profile_hash"] or (
        profile.synthetic and snapshot.get("extraction_mode") != "local_synthetic"
    ):
        raise ValueError("EXTRACTION_PROFILE_MISMATCH")
    return profile


def claim_scope(snapshot, graph):
    """Discovery scope for the extractor only; the parsed graph/evidence stay broad.

    When ``extraction_limits.claim_pages`` is frozen, discovery narrows to that
    subset of ``selected_pages``; retrieval/evidence keep the full parsed graph.
    An absent key preserves the exact legacy scope, hash and replay.
    """
    if snapshot["scope"] == "declared_subset":
        pages = tuple(
            claim_pages_for(snapshot.get("extraction_limits"), snapshot["selected_pages"])
        )
    else:
        pages = ()
    return ClaimScope(
        graph.tenant_id,
        graph.document_version_id,
        graph.parse_manifest_id,
        snapshot["scope"],
        pages,
    )


class _RecordedExtractor:
    def __init__(self, profile, receipts):
        self.profile = profile
        self.receipts = {item["packet_sha256"]: item for item in receipts}
        if len(self.receipts) != len(receipts):
            raise ValueError("duplicate extraction receipts")

    def extract(self, packet):
        receipt = self.receipts[canonical_hash(packet)]
        if receipt["status"] == "failed":
            raise TimeoutError
        return json.loads(receipt["raw_response_json"])


class LocalClaimStore:
    def __init__(self, store, uploads, parser):
        self.store, self.uploads, self.parser = store, uploads, parser

    def load_evidence(self, tenant_id, run_id):
        """Replay once and return the mutually verified snapshot, claims and source graph."""
        snapshot = self.store.snapshot(tenant_id, run_id)
        run = self.store.jobs.get_run(tenant_id, run_id)
        if "extract_job" not in run:
            raise KeyError("claims not published")
        message = JobMessage(**run["extract_job"])
        if (
            message.tenant_id,
            message.run_id,
            message.document_version_id,
            message.stage,
            message.input_hash,
        ) != (
            tenant_id,
            run_id,
            snapshot["document"]["version_id"],
            "extract",
            snapshot["input_hash"],
        ):
            raise ValueError("CLAIM_SNAPSHOT_IDENTITY_MISMATCH")
        payload = self.store.jobs.read_checkpoint(message)
        if payload is None or sha256(payload).hexdigest() != run["claim_snapshot_sha256"]:
            raise ValueError("CLAIM_SNAPSHOT_HASH_MISMATCH")
        envelope = json.loads(payload)
        graph = load_run_graph(
            self.store, self.uploads, self.parser, tenant_id=tenant_id, run_id=run_id
        )
        profile = extraction_profile(snapshot)
        parse_checkpoint = self.store.jobs.read_checkpoint(JobMessage(**run["parse_job"]))
        if parse_checkpoint is None:
            raise ValueError("PARSE_NOT_PUBLISHED")
        expected = snapshot_pins(snapshot, graph, profile, parse_checkpoint)
        if any(envelope.get(key) != value for key, value in expected.items()):
            raise ValueError("CLAIM_SNAPSHOT_INPUT_MISMATCH")
        discovery = discover_atomic_claims(
            graph,
            claim_scope(snapshot, graph),
            extractor=_RecordedExtractor(profile, envelope["discovery"]["receipts"]),
        )
        if (
            canonical_hash(asdict(discovery)) != canonical_hash(envelope["discovery"])
            or envelope["synthetic"] is not discovery.synthetic
            or envelope["coverage"]
            != discovery_coverage(json.loads(parse_checkpoint)["coverage"], graph, discovery)
        ):
            raise ValueError("CLAIM_SNAPSHOT_REPLAY_MISMATCH")
        if "claim_source_policy" in snapshot:
            from proofops.adapters.local.claim_source_policies import claim_source_reader
            from proofops.adapters.local.native_replay_cache import replay_claims_cached
            from proofops.adapters.local.run_artifacts import load_run_inputs

            reader = claim_source_reader(snapshot["claim_source_policy"])
            _, source, _ = load_run_inputs(
                self.store, self.uploads, tenant_id=tenant_id, run_id=run_id
            )
            discovery, graph = replay_claims_cached(
                reader=reader,
                policy=snapshot["claim_source_policy"],
                receipt=envelope["claim_source_attestation"],
                graph=graph,
                source=source.content,
                discovery=discovery,
                tenant_id=tenant_id,
            )
        elif "claim_source_attestation" in envelope:
            raise ValueError("UNPINNED_CLAIM_SOURCE_ATTESTATION")
        return envelope, discovery, graph

    def load_snapshot(self, tenant_id: str, run_id: str) -> dict:
        return self.load_evidence(tenant_id, run_id)[0]

    def load(self, tenant_id: str, run_id: str):
        return self.load_evidence(tenant_id, run_id)[1]

    def list(self, tenant_id: str, run_id: str):
        return self.load(tenant_id, run_id).claims

    def get(self, tenant_id: str, run_id: str, claim_id: str):
        for claim in self.list(tenant_id, run_id):
            if claim.claim_id == claim_id:
                return claim
        raise KeyError("claim not found")

    def submitted_reviews(self, tenant_id, run_id, claim_id=None, *, connection=None):
        from proofops.adapters.local.submitted_review import read_submissions

        return read_submissions(self, tenant_id, run_id, claim_id, connection=connection)

    def current_tag(self, tenant_id: str, run_id: str, claim_id: str, *, connection=None):
        """Read one atomic head; immutable prior revisions are never scanned."""
        if connection is None:
            with self.store.jobs._transaction() as db:
                return self.current_tag(tenant_id, run_id, claim_id, connection=db)
        jobs = self.store.jobs
        run = jobs._get(connection, tenant_id, run_id, "run", "META")
        raw = jobs._raw(connection, tenant_id, run_id, "claim_head", claim_id)
        if raw is None:
            return None
        head = json.loads(raw)
        tag = jobs._get(
            connection, tenant_id, run_id, "tag_revision", f'{claim_id}:{head["tag_revision"]:010}'
        )
        decision = (
            jobs._get(
                connection,
                tenant_id,
                run_id,
                "decision_revision",
                f'{claim_id}:{head["decision_revision"]:010}',
            )
            if head["decision_revision"]
            else None
        )
        return dict(
            tag=tag, decision=decision["api"] if decision else None, epoch=run["mutation_epoch"]
        )

    @staticmethod
    def summary(claim, current):
        if current is None:
            return claim.to_summary()
        tag = current["tag"]
        track = (tag["confirmed_tags"] or {}).get("track")
        if track is None:
            track = tag["inputs"]["packet"]["track"]
        return claim.to_summary() | dict(
            track=track, decision=current["decision"], revision=tag["tag_revision"]
        )

    def page(self, discovery, run_id, *, cursor, limit, now, track, grade, review_status):
        from proofops.adapters.local.catalog_pages import initialize, page

        tenant_id = discovery.scope.tenant_id
        with self.store.jobs._transaction() as db:
            initialize(db)
            run = self.store.jobs._get(db, tenant_id, run_id, "run", "META")

            def items():
                for claim in sorted(
                    discovery.claims,
                    key=lambda c: (
                        c.source_refs[0].page_num,
                        c.source_refs[0].char_start,
                        c.source_refs[0].source_id,
                        c.claim_id,
                    ),
                ):
                    current = self.current_tag(tenant_id, run_id, claim.claim_id, connection=db)
                    item = self.summary(claim, current)
                    decision = item["decision"] or {}
                    status = decision.get("review_status", "needs_review" if current else None)
                    if (
                        (track is None or item["track"] == track)
                        and (grade is None or decision.get("evidence_grade") == grade)
                        and (review_status is None or status == review_status)
                    ):
                        yield item

            return page(
                db,
                tenant_id=tenant_id,
                endpoint="claims",
                query=dict(
                    run_id=run_id,
                    discovery=canonical_hash(asdict(discovery)),
                    track=track,
                    grade=grade,
                    review_status=review_status,
                ),
                cursor=cursor,
                limit=limit,
                now=now,
                load_items=lambda: dict(items=items(), snapshot_epoch=run["mutation_epoch"]),
            )


def snapshot_pins(snapshot, graph, profile, parse_checkpoint):
    return dict(
        schema="local_extract_checkpoint_v2"
        if "claim_source_policy" in snapshot
        else "local_extract_checkpoint_v1",
        tenant_id=snapshot["tenant_id"],
        run_id=snapshot["run_id"],
        document_version_id=graph.document_version_id,
        input_hash=snapshot["input_hash"],
        source_sha256=graph.source_sha256,
        object_version_id=snapshot["document"]["object_version_id"],
        parse_manifest_id=graph.parse_manifest_id,
        graph_sha256=canonical_hash(asdict(graph)),
        parse_checkpoint_sha256=sha256(parse_checkpoint).hexdigest(),
        manifest_sha256=json.loads(parse_checkpoint)["manifest_sha256"],
        rule_pack_sha256=snapshot["rulepack"]["sha256"],
        model_binding_hash=snapshot["model_binding_hash"],
        extraction_profile=asdict(profile),
        extraction_profile_hash=canonical_hash(asdict(profile)),
        extraction_mode=snapshot["extraction_mode"],
        stage_status="completed",
        downstream_status="pending",
        validation_profile="fast_preview",
        vision_status="not_run",
    )


def guard_extract_continuation(db, jobs, run, *, now, supersede=False):
    """Check downstream ownership under the extraction lease/publication transaction."""
    tenant, run_id = run["tenant_id"], run["run_id"]
    if (
        "tag_job" in run
        or run.get("tag_stage_status") is not None
        or run.get("status") in ("partial", "completed")
        or db.execute(
            "SELECT 1 FROM job_records WHERE tenant_id=? AND run_id=? "
            "AND kind IN ('tag_revision', 'review_revision', 'claim_head', 'review_head') LIMIT 1",
            (tenant, run_id),
        ).fetchone()
        is not None
    ):
        raise ValueError("TAG_PUBLICATION_CONFLICT")
    tags = [job for job in jobs._all(db, tenant, run_id, "job") if job["message"]["stage"] == "tag"]
    if any(job["status"] == "leased" and job["lease_until"] > now for job in tags):
        raise ValueError("TAG_LEASE_CONFLICT")
    if not supersede:
        return
    for job in tags:
        if job["status"] in {"pending", "leased"}:
            job.update(status="superseded", fencing_token=job["fencing_token"] + 1)
            jobs._save_job(db, JobMessage(**job["message"]), job)
    for event in jobs._all(db, tenant, run_id, "outbox"):
        if event.get("status") == "pending" and event.get("message", {}).get("stage") == "tag":
            event["status"] = "superseded"
            jobs._put(db, tenant, run_id, "outbox", event["event_id"], event)


def validate_extract_commit(db, jobs, run, message, envelope, next_job, *, now):
    """Validate publication pins inside the same transaction as checkpoint/outbox."""
    row = db.execute(
        "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?",
        (message.tenant_id, message.run_id),
    ).fetchone()
    if row is None:
        raise ValueError("extract requires frozen run")
    snapshot = json.loads(row[0])
    frozen = {key: value for key, value in snapshot.items() if key != "input_hash"}
    profile = extraction_profile(snapshot)
    parse_message = JobMessage(**run["parse_job"])
    parse_job = jobs._job(db, parse_message)
    ref = parse_job["artifact_ref"]
    raw = jobs._raw(db, message.tenant_id, message.run_id, "artifact", ref["key"])
    if raw is None or sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("parse checkpoint corruption")
    parsed = json.loads(raw)
    discovery, coverage = envelope["discovery"], envelope["coverage"]
    expected_coverage = dict(
        parsed["coverage"],
        chunks_discovered=coverage["chunks_discovered"],
        chunks_processed=len(discovery["processed_source_ids"]),
        claims_discovered=len(discovery["claims"]),
        claims_decided=0,
        claims_needs_review=len(discovery["claims"]),
        complete=False,
    )
    pins = dict(
        tenant_id=message.tenant_id,
        run_id=message.run_id,
        document_version_id=message.document_version_id,
        input_hash=message.input_hash,
        source_sha256=snapshot["document"]["sha256"],
        object_version_id=snapshot["document"]["object_version_id"],
        parse_manifest_id=parsed["parse_manifest_id"],
        parse_checkpoint_sha256=ref["sha256"],
        manifest_sha256=parsed["manifest_sha256"],
        extraction_profile=asdict(profile),
        extraction_profile_hash=snapshot["extraction_profile_hash"],
        extraction_mode=snapshot["extraction_mode"],
        rule_pack_sha256=snapshot["rulepack"]["sha256"],
        model_binding_hash=snapshot["model_binding_hash"],
        stage_status="completed",
        downstream_status="pending",
        validation_profile="fast_preview",
        vision_status="not_run",
    )
    if "claim_source_policy" in snapshot:
        receipt = envelope.get("claim_source_attestation", {})
        if (
            envelope.get("schema") != "local_extract_checkpoint_v2"
            or receipt.get("policy") != snapshot["claim_source_policy"]
            or receipt.get("graph_sha256") != envelope.get("graph_sha256")
            or receipt.get("artifact_sha256")
            != canonical_hash({k: v for k, v in receipt.items() if k != "artifact_sha256"})
            or any(
                receipt.get(k) != envelope.get(k)
                for k in ("tenant_id", "document_version_id", "parse_manifest_id", "source_sha256")
            )
        ):
            raise ValueError("CLAIM_SOURCE_ATTESTATION_INVALID")
    elif (
        "claim_source_attestation" in envelope
        or envelope.get("schema") != "local_extract_checkpoint_v1"
    ):
        raise ValueError("UNPINNED_CLAIM_SOURCE_ATTESTATION")
    represented = set(discovery["processed_source_ids"]) | {
        item["source_id"] for item in discovery["exclusions"]
    }
    if (
        message.stage != "extract"
        or next_job is None
        or next_job.stage != "tag"
        or next_job.input_hash != message.input_hash
        or canonical_hash(frozen) != snapshot["input_hash"]
        or snapshot["input_hash"] != message.input_hash
        or snapshot["document"]["version_id"] != message.document_version_id
        or any(envelope.get(key) != value for key, value in pins.items())
        or coverage != expected_coverage
        or type(coverage["chunks_discovered"]) is not int
        or coverage["chunks_discovered"] != len(represented)
        or any(
            type(value) is not int or value < 0
            for key, value in coverage.items()
            if key not in {"complete", "full_scope"}
        )
        or coverage["complete"] is not False
        or envelope["synthetic"] is not discovery["synthetic"]
        or (profile.synthetic and envelope["synthetic"] is not True)
        or discovery["scope"]
        != dict(
            tenant_id=message.tenant_id,
            document_version_id=message.document_version_id,
            parse_manifest_id=parsed["parse_manifest_id"],
            mode=snapshot["scope"],
            selected_pages=claim_pages_for(
                snapshot.get("extraction_limits"), snapshot["selected_pages"]
            )
            if snapshot["scope"] != "full"
            else [],
        )
    ):
        raise ValueError("invalid local extract checkpoint")
    guard_extract_continuation(db, jobs, run, now=now, supersede=True)


def discovery_coverage(parsed_coverage, graph, discovery):
    return dict(
        parsed_coverage,
        chunks_discovered=len(graph.blocks),
        chunks_processed=len(discovery.processed_source_ids),
        claims_discovered=len(discovery.claims),
        claims_decided=0,
        claims_needs_review=len(discovery.claims),
        complete=False,
    )


# A deferred source was answered locally, without a model call: it carries one
# unknown span over its whole text. ``beyond_extraction_limit`` and
# ``extraction_budget_stop`` stay eligible for a later batch;
# ``non_paragraph_kind`` is a routing skip, not a pending item.
DEFERRED_LIMIT_REASON = "beyond_extraction_limit"
DEFERRED_BUDGET_REASON = "extraction_budget_stop"
DEFERRED_KIND_REASON = "non_paragraph_kind"
DEFERRED_REASONS = frozenset((DEFERRED_LIMIT_REASON, DEFERRED_BUDGET_REASON, DEFERRED_KIND_REASON))


def deferred_payload(text: str, reason: str) -> dict:
    """The local, model-free answer for a source outside this batch."""
    if reason not in DEFERRED_REASONS:
        raise ValueError("EXTRACTION_INPUT_INVALID")
    return {
        "spans": [
            dict(
                char_start=0,
                char_end=len(text),
                quote=text,
                kind="unknown",
                reason=reason,
                topic_ids=[],
            )
        ]
    }


def _deferred_reason(raw_response_json):
    """Return the deferred reason of a stored receipt payload, else ``None``."""
    try:
        spans = json.loads(raw_response_json)["spans"]
    except (TypeError, ValueError, KeyError):
        return None
    if len(spans) != 1:
        return None
    span = spans[0]
    reason = span.get("reason")
    if span.get("kind") == "unknown" and reason in DEFERRED_REASONS:
        return reason
    return None


def extraction_batch_state(envelope):
    """Exact per-source state of a stored extraction revision, from receipts only.

    ``model_processed`` are the sources a batch actually spent a model call on and
    whose stored payload may be replayed instead of re-charged. ``pending`` are the
    sources that were answered locally because they were outside the batch window
    or after a budget stop, so they remain eligible. Nothing here is inferred from
    text: every set comes from the stored receipt payloads and exclusions.
    """
    discovery = envelope["discovery"]
    model_processed, pending, skipped, failed, stops = {}, {}, set(), set(), {}
    failed_receipts = {}
    for receipt in discovery["receipts"]:
        source_id = receipt["source_id"]
        if receipt["status"] == "failed":
            failed.add(source_id)
            failed_receipts[source_id] = receipt
            continue
        reason = _deferred_reason(receipt["raw_response_json"])
        if reason is None:
            model_processed[source_id] = receipt
        elif reason == DEFERRED_KIND_REASON:
            skipped.add(source_id)
        else:
            pending[source_id] = receipt
            if reason == DEFERRED_BUDGET_REASON:
                stops[source_id] = reason
    excluded = {
        item["source_id"]
        for item in discovery["exclusions"]
        if item["source_id"] not in model_processed
        and item["source_id"] not in pending
        and item["source_id"] not in skipped
        and item["source_id"] not in failed
    }
    return dict(
        model_processed=model_processed,
        pending=pending,
        skipped_non_paragraph=skipped,
        failed=failed,
        failed_receipts=failed_receipts,
        budget_stopped=stops,
        excluded=excluded,
    )
