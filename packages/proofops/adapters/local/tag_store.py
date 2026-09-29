"""Tag checkpoint validation/replay; initial revisions belong to LocalSQLiteReviewStore.

No parallel revision tables. Checkpoints use the existing immutable artifact
pointer and retain the extraction checkpoint digest and frozen run inputs.
"""

import json
from dataclasses import asdict
from hashlib import sha256

from proofops.adapters.cache.aws import CacheNamespace, CacheRequest
from proofops.adapters.local.claim_store import LocalClaimStore
from proofops.application.budget import TokenUsage
from proofops.application.evidence.binding import ClaimContext
from proofops.application.evidence.retrieval import freeze_packet
from proofops.application.ports.jobs import JobMessage
from proofops.application.ports.models import ModelBinding
from proofops.application.reviews import ReviewInputs
from proofops.application.tagging.consensus import form_consensus, reviewable_decision
from proofops.application.tagging.relations import SYSTEM_PROMPT as RELATION_SYSTEM_PROMPT
from proofops.application.tagging.report_level_link import replay_report_level_link, strict_fallback
from proofops.application.tagging.service import TaggingSettings, TagRun
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot
from proofops.domain.rules.engine import RuleContext, evaluate
from proofops.domain.values import SourceRef, llm_tags_from_dict


def tagging_settings(snapshot, *, preliminary=False, relation=False):
    if preliminary and relation:
        raise ValueError("TAGGING_PROFILE_MISMATCH")
    prefix = "relation" if relation else "preliminary" if preliminary else "tagging"
    if prefix + "_settings" not in snapshot:
        raise ValueError("TAGGING_PROFILE_MISMATCH")
    raw = snapshot[prefix + "_settings"]
    settings = TaggingSettings(**(raw | {"binding": ModelBinding(**raw["binding"])}))
    if snapshot.get("tagging_mode") == "upstage_local":
        from proofops.application.registry import artifact_sha256

        runtime = snapshot[prefix + "_runtime"]
        if (
            canonical_hash(asdict(settings)) != snapshot[prefix + "_settings_hash"]
            or artifact_sha256(runtime) != snapshot[prefix + "_runtime_artifact_hash"]
            or settings.binding.synthetic is not False
            or settings.binding.binding_id != runtime["runtime_binding_id"]
            or settings.binding.role != "tagger"
            or settings.model_id != runtime["model_id"]
            or runtime.get("tagging_settings_sha256") != canonical_hash(asdict(settings))
            or runtime.get("input_reservation_policy_sha256")
            != snapshot["input_reservation_policy_hash"]
            or canonical_hash(snapshot["input_reservation_policy"])
            != snapshot["input_reservation_policy_hash"]
        ):
            raise ValueError("TAGGING_PROFILE_MISMATCH")
        if relation and (
            settings.model_profile != "upstage-relation-source-quotes-v1"
            or settings.system_prompt != RELATION_SYSTEM_PROMPT
        ):
            raise ValueError("TAGGING_PROFILE_MISMATCH")
        return settings
    if preliminary or relation:
        raise ValueError("PRELIMINARY_PROFILE_UNSUPPORTED")
    runtime = snapshot["runtime"]
    if (
        canonical_hash(asdict(settings)) != snapshot["tagging_settings_hash"]
        or snapshot["tagging_mode"] != "local_synthetic"
        or not settings.binding.synthetic
        or settings.binding.binding_id != runtime["runtime_binding_id"]
        or settings.binding.role != runtime["role"]
        or (settings.model_id, settings.region) != (runtime["model_id"], runtime["endpoint_region"])
        or runtime["status"] != "approved"
        or type(settings.max_tokens) is not int
        or not 1 <= settings.max_tokens <= runtime["max_output_tokens"]
    ):
        raise ValueError("TAGGING_PROFILE_MISMATCH")
    return settings


def tag_pins(snapshot, extraction, extraction_hash):
    pins = dict(
        schema="local_tag_checkpoint_v1",
        synthetic=snapshot.get("tagging_mode") != "upstage_local",
        **{
            key: extraction[key]
            for key in (
                "tenant_id",
                "run_id",
                "document_version_id",
                "input_hash",
                "source_sha256",
                "object_version_id",
                "parse_manifest_id",
                "graph_sha256",
                "parse_checkpoint_sha256",
                "manifest_sha256",
                "rule_pack_sha256",
                "model_binding_hash",
                "extraction_profile",
                "extraction_profile_hash",
                "extraction_mode",
            )
        },
        claim_snapshot_sha256=extraction_hash,
        tagging_settings=snapshot.get("tagging_settings"),
        tagging_settings_hash=snapshot.get("tagging_settings_hash"),
        tagging_mode=snapshot.get("tagging_mode"),
        validation_profile="fast_preview",
        vision_status="not_run",
    )
    if snapshot.get("tagging_mode") == "upstage_local":
        pins.update(
            **{
                key: snapshot[key]
                for key in (
                    "preliminary_settings",
                    "preliminary_settings_hash",
                    "preliminary_runtime",
                    "preliminary_runtime_artifact_hash",
                    "tagging_runtime",
                    "tagging_runtime_artifact_hash",
                    "input_reservation_policy",
                    "input_reservation_policy_hash",
                    "rulepack_use",
                )
            }
        )
        relation_keys = (
            "relation_settings",
            "relation_settings_hash",
            "relation_runtime",
            "relation_runtime_artifact_hash",
        )
        if any(key in snapshot for key in relation_keys):
            if not all(key in snapshot for key in relation_keys):
                raise ValueError("TAG_CHECKPOINT_PIN_MISMATCH")
            pins.update({key: snapshot[key] for key in relation_keys})
    elif any(
        key in snapshot
        for key in (
            "relation_settings",
            "relation_settings_hash",
            "relation_runtime",
            "relation_runtime_artifact_hash",
        )
    ):
        raise ValueError("TAG_CHECKPOINT_PIN_MISMATCH")
    if "fact_assembly_profile" in snapshot:
        pins["fact_assembly_profile"] = snapshot["fact_assembly_profile"]
    if "report_level_link" in snapshot:
        pins["report_level_link"] = snapshot["report_level_link"]
    return pins


def validate_tag_commit(db, jobs, run, message, envelope, next_job):
    row = db.execute(
        "SELECT payload FROM run_snapshots WHERE tenant_id=? AND run_id=?",
        (message.tenant_id, message.run_id),
    ).fetchone()
    snapshot = json.loads(row[0]) if row else {}
    extraction_job = jobs._job(db, JobMessage(**run["extract_job"]))
    ref = extraction_job["artifact_ref"]
    raw = jobs._raw(db, message.tenant_id, message.run_id, "artifact", ref["key"])
    if raw is None or sha256(raw).hexdigest() != ref["sha256"]:
        raise ValueError("TAG_EXTRACTION_CHECKPOINT_INVALID")
    extraction = json.loads(raw)
    expected = tag_pins(snapshot, extraction, ref["sha256"])
    claims = envelope["claims"]
    original_ids = [c["claim_id"] for c in extraction["discovery"]["claims"]]
    decided = sum(
        item["decision"] is not None and item["decision"]["decision_status"] == "decided"
        for item in claims
    )
    coverage = dict(
        extraction["coverage"],
        claims_decided=decided,
        claims_needs_review=len(claims) - decided,
        complete=False,
    )
    if (
        message.stage != "tag"
        or next_job is not None
        or message.input_hash != snapshot["input_hash"]
        or canonical_hash({k: v for k, v in snapshot.items() if k != "input_hash"})
        != message.input_hash
        or (message.tenant_id, message.run_id, message.document_version_id)
        != (snapshot["tenant_id"], snapshot["run_id"], snapshot["document"]["version_id"])
        or run["claim_snapshot_sha256"] != ref["sha256"]
        or any(envelope.get(k) != v for k, v in expected.items())
        or [item["claim_id"] for item in claims] != original_ids
        or envelope["coverage"] != coverage
        or envelope["stage_status"] not in {"blocked", "needs_review", "completed"}
        or envelope["downstream_status"] != "human_review"
        or any(item["tag_runs"] and item.get("review_inputs") is None for item in claims)
        or any(not item["tag_runs"] and item["decision"] is not None for item in claims)
    ):
        raise ValueError("TAG_CHECKPOINT_INVALID")
    # Only the caller holding this same transaction's fence can publish revisions.
    for item in claims:
        if item["tag_runs"]:
            review = item["review_inputs"]
            if (
                review["tag_runs"] != item["tag_runs"]
                or review["decision"] != item["decision"]
                or review["claim"]["claim_id"] != item["claim_id"]
            ):
                raise ValueError("TAG_REVIEW_INPUT_MISMATCH")
    return coverage


def _stored_context_reader(
    store, claim, run_id, refs, run_policy, replay_receipt, *, published_tag=None
):
    """Resolve only the explicitly requested, already-published receipt."""
    jobs = getattr(store, "jobs", None)
    if jobs is None or not hasattr(jobs, "_transaction"):
        raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
    if published_tag is None:
        try:
            with jobs._transaction() as db:
                head = jobs._get(db, claim.tenant_id, run_id, "claim_head", claim.claim_id)
                tag = jobs._get(
                    db,
                    claim.tenant_id,
                    run_id,
                    "tag_revision",
                    f'{claim.claim_id}:{head["tag_revision"]:010}',
                )
        except KeyError:
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH") from None
    else:
        tag = published_tag
        if (
            not isinstance(tag, dict)
            or (tag.get("confirmed_tags") or {}).get("claim_id") != claim.claim_id
        ):
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")

    ref_hash = canonical_hash([dict(asdict(ref), verification_state="candidate") for ref in refs])
    for prior in reversed(tag.get("report_level_review", ())):
        prior_refs = prior.get("refs")
        if (
            not isinstance(prior_refs, list)
            or canonical_hash([dict(ref, verification_state="candidate") for ref in prior_refs])
            != ref_hash
            or prior.get("source_receipt") != replay_receipt
        ):
            continue
        receipt = prior.get("source_receipt")
        if not isinstance(receipt, dict) or receipt.get("artifact_sha256") != canonical_hash(
            {key: value for key, value in receipt.items() if key != "artifact_sha256"}
        ):
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        paragraph = (
            receipt
            if isinstance(receipt.get("policy"), dict)
            else (receipt.get("receipts") or {}).get("paragraph")
        )
        if paragraph is None and receipt.get("schema") == "context_source_attestation_v1":
            from proofops.adapters.local.claim_source_verification import claim_source_policy

            policy = claim_source_policy()
            if receipt.get("policy_hashes", {}).get("paragraph") == canonical_hash(policy):
                return receipt, policy, None
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        if not isinstance(paragraph, dict) or not isinstance(paragraph.get("policy"), dict):
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        if paragraph.get("artifact_sha256") != canonical_hash(
            {key: value for key, value in paragraph.items() if key != "artifact_sha256"}
        ):
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        policy = paragraph["policy"]
        from proofops.adapters.local.claim_source_verification import claim_source_policy

        if policy != run_policy and policy != claim_source_policy():
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        recorded_hash = paragraph.get("reader_policy_sha256")
        if recorded_hash is not None and recorded_hash != canonical_hash(policy):
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        if paragraph is receipt and policy != claim_source_policy() and recorded_hash is None:
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        if receipt.get("schema") == "context_source_attestation_v1" and receipt.get(
            "policy_hashes", {}
        ).get("paragraph") != canonical_hash(policy):
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        return receipt, policy, recorded_hash
    raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")


class LocalTagStore:
    def __init__(self, store, uploads, parser):
        self.store, self.uploads, self.parser = store, uploads, parser
        self.claims = LocalClaimStore(store, uploads, parser)

    def verify_context_sources(
        self, inputs, refs, *, replay_receipt=None, pinned_run_snapshot=None, published_tag=None
    ):
        """Source-only supplementary span attestations; never mutate frozen inputs."""
        from dataclasses import replace

        from proofops.adapters.local.claim_source_policies import attest_claims
        from proofops.adapters.local.claim_source_verification import claim_source_policy
        from proofops.adapters.local.table_span_source_verification import (
            attest_table_spans,
            table_span_policy,
        )
        from proofops.application.evidence.span_citations import (
            span_verified_graph,
            verify_source_ref,
        )

        claim, graph = inputs.context.claim, inputs.original
        if not 1 <= len(refs) <= 6:
            raise ValueError("CONTEXT_SOURCE_LIMIT")
        reader = None
        reader_policy = None
        stored_receipt = None
        stored_reader_policy_hash = None
        run_id = getattr(inputs, "run_id", None)
        if run_id is not None:
            if self.store is None:
                raise ValueError("CONTEXT_SOURCE_REJECTED")
            snapshot = (
                pinned_run_snapshot
                if pinned_run_snapshot is not None
                else self.store.snapshot(claim.tenant_id, run_id)
            )
            if snapshot is None:
                raise ValueError("CONTEXT_SOURCE_REJECTED")
            if pinned_run_snapshot is not None and (
                snapshot.get("tenant_id") != claim.tenant_id
                or snapshot.get("run_id") != run_id
                or snapshot.get("document", {}).get("version_id") != graph.document_version_id
            ):
                raise ValueError("CONTEXT_SOURCE_REJECTED")
            reader_policy = snapshot.get("claim_source_policy")
            if replay_receipt is not None:
                stored_receipt, reader_policy, stored_reader_policy_hash = _stored_context_reader(
                    self.store,
                    claim,
                    run_id,
                    refs,
                    reader_policy,
                    replay_receipt,
                    published_tag=published_tag,
                )
            if reader_policy is not None:
                from proofops.adapters.local.claim_source_policies import claim_source_reader

                reader = claim_source_reader(reader_policy)
        elif replay_receipt is not None:
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        if reader is None:
            from proofops.adapters.local import claim_source_verification

            reader = claim_source_verification
        source = self.uploads.read_original(claim.tenant_id, claim.document_version_id)
        if sha256(source).hexdigest() != graph.source_sha256:
            raise ValueError("CONTEXT_SOURCE_MISMATCH")

        def attest_paragraphs(selected):
            return attest_claims(
                reader=reader,
                graph=graph,
                source=source,
                refs=selected,
                tenant_id=claim.tenant_id,
                cache=False,
            )

        kinds = {block.source_id: block.kind for block in graph.blocks}
        if any(
            kinds.get(ref.source_id) not in {"paragraph", "table_cell", "table_row"} for ref in refs
        ):
            raise ValueError("CONTEXT_SOURCE_REJECTED")
        unresolved = tuple(
            r
            for r in refs
            if verify_source_ref(r, graph, tenant_id=claim.tenant_id).verification_state
            != "verified"
        )
        paragraphs = tuple(ref for ref in unresolved if kinds[ref.source_id] == "paragraph")
        tables = tuple(ref for ref in unresolved if kinds[ref.source_id] != "paragraph")
        if tables:
            paragraph_receipt = attest_paragraphs(paragraphs) if paragraphs else None
            table_receipt = attest_table_spans(graph, source, tables, tenant_id=claim.tenant_id)
            if (
                (paragraph_receipt and len(paragraph_receipt["records"]) != len(paragraphs))
                or len(table_receipt["records"]) != len(tables)
                or any(
                    record["status"] != "verified"
                    for item in (paragraph_receipt, table_receipt)
                    if item
                    for record in item["records"]
                )
            ):
                raise ValueError("CONTEXT_SOURCE_REJECTED")
            receipts = {"paragraph": paragraph_receipt, "table": table_receipt}
            records = {
                kind: iter(item["records"] if item else ()) for kind, item in receipts.items()
            }
            ordered = [
                next(records["paragraph" if kinds[ref.source_id] == "paragraph" else "table"])
                for ref in unresolved
            ]
            receipt = dict(
                schema="context_source_attestation_v1",
                tenant_id=claim.tenant_id,
                document_version_id=graph.document_version_id,
                parse_manifest_id=graph.parse_manifest_id,
                source_sha256=graph.source_sha256,
                policy_hashes={
                    "paragraph": canonical_hash(
                        reader_policy
                        if paragraphs and reader_policy is not None
                        else claim_source_policy()
                    ),
                    "table": canonical_hash(table_span_policy()),
                },
                receipts=receipts,
                records=ordered,
            )
            receipt["artifact_sha256"] = canonical_hash(receipt)
        else:
            # Keep R38 paragraph-only receipts byte-for-byte compatible on replay.
            receipt = attest_paragraphs(unresolved)
            if reader_policy is not None and reader_policy != claim_source_policy():
                if stored_receipt is None or stored_reader_policy_hash is not None:
                    receipt["reader_policy_sha256"] = canonical_hash(reader_policy)
                    receipt["artifact_sha256"] = canonical_hash(
                        {key: value for key, value in receipt.items() if key != "artifact_sha256"}
                    )
        if len(receipt["records"]) != len(unresolved) or any(
            record["status"] != "verified" for record in receipt["records"]
        ):
            raise ValueError("CONTEXT_SOURCE_REJECTED")
        if stored_receipt is not None and canonical_hash(receipt) != canonical_hash(stored_receipt):
            raise ValueError("CONTEXT_SOURCE_REPLAY_MISMATCH")
        scoped = span_verified_graph(
            graph,
            (
                *getattr(graph, "verified_spans", ()),
                *(replace(r, verification_state="verified") for r in unresolved),
            ),
            canonical_hash(
                dict(
                    prior=getattr(graph, "span_receipt_sha256", ""),
                    supplemental=receipt["artifact_sha256"],
                )
            ),
        )
        return scoped, receipt

    def _load_snapshot_with_evidence(self, tenant_id, run_id):
        # Share the evidence replay used to verify the tag checkpoint pins.
        run = self.store.jobs.get_run(tenant_id, run_id)
        message = JobMessage(**run["tag_job"])
        payload = self.store.jobs.read_checkpoint(message)
        if payload is None or sha256(payload).hexdigest() != run["tag_snapshot_sha256"]:
            raise ValueError("TAG_CHECKPOINT_HASH_MISMATCH")
        envelope = json.loads(payload)
        evidence = self.claims.load_evidence(tenant_id, run_id)
        extraction = evidence[0]
        snapshot = self.store.snapshot(tenant_id, run_id)
        expected = tag_pins(snapshot, extraction, run["claim_snapshot_sha256"])
        if any(envelope.get(k) != v for k, v in expected.items()):
            raise ValueError("TAG_CHECKPOINT_PIN_MISMATCH")
        return envelope, evidence

    def load_snapshot(self, tenant_id, run_id):
        return self._load_snapshot_with_evidence(tenant_id, run_id)[0]

    def load_inputs(self, tenant_id, run_id, claim_id):
        run = self.store.jobs.get_run(tenant_id, run_id)
        envelope = None
        if "tag_job" in run:
            envelope, evidence = self._load_snapshot_with_evidence(tenant_id, run_id)
            item = next((item for item in envelope["claims"] if item["claim_id"] == claim_id), None)
            if item is None or item.get("review_inputs") is None:
                raise KeyError("tagged claim not published")
            raw = item["review_inputs"]
        else:
            evidence = self.claims.load_evidence(tenant_id, run_id)
            _, discovery, graph = evidence
            claim = next((c for c in discovery.claims if c.claim_id == claim_id), None)
            if claim is None:
                raise KeyError("tagged claim not published")
            try:
                with self.store.jobs._transaction() as db:
                    head = self.store.jobs._get(db, tenant_id, run_id, "claim_head", claim_id)
                    published = self.store.jobs._get(
                        db,
                        tenant_id,
                        run_id,
                        "tag_revision",
                        f"{claim_id}:{1:010}",
                    )
            except KeyError:
                raise KeyError("tagged claim not published") from None
            raw = published.get("inputs")
            snapshot = self.store.snapshot(tenant_id, run_id)
            expected = tag_pins(snapshot, evidence[0], run["claim_snapshot_sha256"])
            if (
                published.get("origin") != "consensus"
                or published.get("tag_revision") != 1
                or head.get("tag_revision", 0) < 1
                or not isinstance(raw, dict)
                or canonical_hash(raw.get("claim")) != canonical_hash(asdict(claim))
                or raw.get("run_id") != run_id
                or raw.get("original", {}).get("graph_sha256") != expected["graph_sha256"]
                or canonical_hash(raw.get("rulepack")) != canonical_hash(snapshot["rulepack"])
            ):
                raise ValueError("TAG_REPLAY_MISMATCH")
        _, discovery, graph = evidence
        claim = next(c for c in discovery.claims if c.claim_id == claim_id)
        snapshot = self.store.snapshot(tenant_id, run_id)
        rulepack = RulePackSnapshot(**snapshot["rulepack"])
        packet = freeze_packet({k: v for k, v in raw["packet"].items() if k != "packet_sha256"})
        original_packet = freeze_packet(
            {k: v for k, v in raw["original_packet"].items() if k != "packet_sha256"}
        )
        context = ClaimContext(
            claim, {k: SourceRef(**v) if v else None for k, v in raw["dimensions"].items()}
        )
        runs = []
        for entry in raw["tag_runs"]:
            request = entry["request"]
            runs.append(
                TagRun(
                    **(
                        entry
                        | dict(
                            request=CacheRequest(
                                **(request | {"namespace": CacheNamespace(**request["namespace"])})
                            ),
                            usage=TokenUsage(**entry["usage"]) if entry["usage"] else None,
                            guarded=llm_tags_from_dict(entry["guarded"])
                            if entry["guarded"]
                            else None,
                            errors=tuple(entry["errors"]),
                            source_scopes=tuple(tuple(v) for v in entry["source_scopes"]),
                            binding_hashes=tuple(tuple(v) for v in entry["binding_hashes"]),
                        )
                    )
                )
            )
        consensus = form_consensus(
            tuple(runs),
            packet=packet,
            rulepack=rulepack,
            tenant_id=tenant_id,
            tag_revision=raw["tag_revision"],
            profile=raw.get("fact_assembly", {}).get("profile", "strict-v1"),
        )
        link_config = snapshot.get("report_level_link")
        if raw.get("report_level_link") != link_config:
            raise ValueError("TAG_REPLAY_MISMATCH")
        link_receipts = tuple(raw.get("report_level_review", ()))
        if link_config is not None:
            consensus = replay_report_level_link(
                consensus,
                config=link_config,
                context=context,
                graph=graph,
                receipts=link_receipts,
                fallback_tags=strict_fallback(
                    tuple(runs),
                    packet,
                    rulepack,
                    tenant_id,
                    raw["tag_revision"],
                    raw.get("fact_assembly", {}).get("profile", "strict-v1"),
                ),
            )
        elif link_receipts:
            raise ValueError("TAG_REPLAY_MISMATCH")
        rule_context = RuleContext(**raw["rule_context"])
        decision = (
            evaluate(consensus.confirmed_tags, rule_context, rulepack)
            if consensus.confirmed_tags
            and snapshot.get("rulepack_use") != "candidate_tagging_reference_only"
            else None
        )
        decision, _ = reviewable_decision(
            decision,
            raw.get("fact_assembly", {}).get("profile", "strict-v1"),
            consensus.review_status,
        )
        inputs = ReviewInputs(
            run_id,
            context,
            graph,
            rulepack,
            rule_context,
            packet,
            original_packet,
            tuple(runs),
            consensus,
            {
                sid: {k: SourceRef(**v) if v else None for k, v in values.items()}
                for sid, values in raw["relation_tags"].items()
            },
            raw["tag_revision"],
            decision,
            raw.get("fact_assembly", {}).get("profile", "strict-v1"),
            link_config,
            link_receipts,
        )
        inputs.validate()
        for receipt in link_receipts:
            refs = tuple(SourceRef(**ref) for ref in receipt["refs"])
            _, replayed = self.verify_context_sources(inputs, refs)
            if canonical_hash(replayed) != canonical_hash(receipt["source_receipt"]):
                raise ValueError("REPORT_LEVEL_LINK_REPLAY_MISMATCH")
        if canonical_hash(inputs.snapshot()) != canonical_hash(raw):
            raise ValueError("TAG_REPLAY_MISMATCH")
        return inputs
