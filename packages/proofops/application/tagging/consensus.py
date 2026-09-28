"""Conservative majority candidates; only unanimous guarded tags can be confirmed."""

from collections import Counter
from dataclasses import dataclass, replace

from proofops.application.evidence.retrieval import EvidencePacket
from proofops.application.tagging.service import TagRun
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot
from proofops.domain.rules.engine import MAPPINGS, ConfirmedFact, ConfirmedTags
from proofops.domain.values import LlmElement

PARTIAL_FACTS_V1 = "partial-facts-v1"
PARTIAL_FACTS_HASH = canonical_hash({"fact_assembly": PARTIAL_FACTS_V1})


def reviewable_decision(decision, profile, review_status):
    """A complete ladder remains a candidate until its tag is actually confirmed."""
    if (
        decision
        and decision.decision_status == "decided"
        and (profile == PARTIAL_FACTS_V1 or review_status != "auto_confirmed")
    ):
        return None, decision.evidence_grade
    return decision, None


def reviewable_checkpoint(item, *, pinned_profile=None):
    """Keep an old unreviewed decision out of a new stage checkpoint."""
    decision = item.get("decision")
    decided = decision and decision.get("decision_status") == "decided"
    if item.get("status") != "completed" and not decided:
        return item
    inputs = item.get("review_inputs") or {}
    profile = (inputs.get("fact_assembly") or {}).get("profile", "strict-v1")
    status = (inputs.get("consensus") or {}).get("review_status")
    origin = inputs.get("review_origin")
    reviewed = (status, origin) in {
        ("human_confirmed", "human"),
        ("ai_delegated_confirmed", "ai_delegated"),
    }
    if not reviewed and (
        profile == PARTIAL_FACTS_V1
        or pinned_profile == PARTIAL_FACTS_V1
        or status != "auto_confirmed"
    ):
        item["status"] = "needs_review"
        item["reason"] = "PARTIAL_FACTS_REVIEW_REQUIRED"
        if decided:
            item["candidate_grade"] = decision.get("evidence_grade")
            item["decision"] = None
    return item


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    candidate_elements: tuple[LlmElement, ...]
    agreement: tuple[tuple[str, int], ...]
    review_status: str
    confirmed_tags: ConfirmedTags | None
    reasons: tuple[str, ...]
    replicate_hashes: tuple[str, ...]


def form_consensus(
    runs: tuple[TagRun, ...],
    *,
    packet: EvidencePacket,
    rulepack: RulePackSnapshot,
    tenant_id: str,
    tag_revision: int,
    rule_gaps: tuple[str, ...] = (),
    profile: str = "strict-v1",
) -> ConsensusResult:
    """A 2:1 majority is a review candidate, never an automatic confirmed revision.

    Raw/guarded receipts stay with the caller's immutable revision. Compound
    elements retain their complete tagged value and evidence on each constituent
    primitive; no year/unit/numeric value is inferred by splitting a string.
    Missing/unresolved elements and failed replicas cannot masquerade as absence.
    """
    data = packet.to_dict()
    if profile not in ("strict-v1", PARTIAL_FACTS_V1):
        raise DomainValidationError("unknown fact assembly profile")
    selected_track = data.get("track")
    if (
        "retrieval_packet_sha256" not in data
        or selected_track not in MAPPINGS
        or data["allowed_elements"]
        != [
            e["id"]
            for e in rulepack.file_content("rubric/elements.yaml")["elements"]
            if e["id"] in MAPPINGS[selected_track]
        ]
        or set(data["allowed_elements"]) != set(MAPPINGS[selected_track])
    ):
        raise DomainValidationError("complete frozen track packet required")
    if len(runs) != 3 or {r.replicate_id for r in runs} != {1, 2, 3}:
        raise DomainValidationError("exactly one run per replicate required")
    ordered = tuple(sorted(runs, key=lambda r: r.replicate_id))
    if data["tenant_id"] != tenant_id or rulepack.tenant_id != tenant_id:
        raise DomainValidationError("tenant mismatch")
    for run in ordered:
        if (
            run.tenant_id,
            run.claim_id,
            run.run_id,
            run.packet_sha256,
            run.graph_sha256,
            run.rule_sha256,
        ) != (
            tenant_id,
            data["claim_id"],
            data["run_id"],
            packet.packet_sha256,
            data["graph_sha256"],
            rulepack.sha256,
        ):
            raise DomainValidationError("replicate packet/provenance mismatch")
        if run.guarded and (
            run.guarded.replicate_id,
            run.guarded.packet_sha256,
            run.guarded.claim_id,
        ) != (run.replicate_id, run.packet_sha256, run.claim_id):
            raise DomainValidationError("guarded vote identity mismatch")
    if (
        len({r.request.request_id for r in ordered}) != 3
        or len({r.request.request_signature for r in ordered}) != 3
    ):
        raise DomainValidationError("independent request identities required")
    if (
        len(
            {
                (r.model_sha256, r.prompt_sha256, r.request.extraction_epoch, r.product_variant)
                for r in ordered
            }
        )
        != 1
    ):
        raise DomainValidationError("replicate model/prompt/epoch mismatch")
    if profile == PARTIAL_FACTS_V1:
        return _partial_facts(ordered, data, packet, rulepack, tenant_id, tag_revision, rule_gaps)
    reasons = list(rule_gaps)
    provider_ids = [
        r.usage.provider_request_id for r in ordered if r.usage and r.usage.provider_request_id
    ]
    if len(provider_ids) != len(set(provider_ids)):
        reasons.append("PROVIDER_RESPONSE_REPLAY")
    if any(r.status != "succeeded" or r.errors or r.guarded is None for r in ordered):
        reasons.append("REPLICA_UNRESOLVED")
    headers = {
        (r.guarded.track, r.guarded.safe_harbor_category, r.guarded.superlative_quote)
        for r in ordered
        if r.guarded
    }
    if len(headers) != 1:
        reasons.append("TRACK_CATEGORY_SUPERLATIVE_DISAGREEMENT")
    candidates, agreement, facts = [], [], []
    track = next(iter(headers))[0] if len(headers) == 1 else None
    if any(header[:2] != (selected_track, data.get("safe_harbor_category")) for header in headers):
        reasons.append("PACKET_TRACK_MISMATCH")
    requested = data["allowed_elements"]
    for element_id in requested:
        votes = []
        for run in ordered:
            if run.guarded:
                for element in run.guarded.elements:
                    if element.element_id == element_id:
                        key = (
                            element.state,
                            element.normalized_value,
                            dict(run.binding_hashes).get(element_id),
                        )
                        votes.append((key, element))
        counts = Counter(key for key, _ in votes)
        common = counts.most_common(1)
        count = common[0][1] if common else 0
        agreement.append((element_id, count))
        if count < 2:
            reasons.append(f"NO_MAJORITY:{element_id}")
            continue
        key = common[0][0]
        winners = [element for vote, element in votes if vote == key]
        refs = tuple(dict.fromkeys(ref for element in winners for ref in element.evidence_refs))
        candidate = replace(winners[0], evidence_refs=refs)
        candidates.append(candidate)
        scopes = {dict(run.source_scopes).get(element_id) for run in ordered if run.guarded}
        scope = next(
            (scope for scope in ("global_bound", "same_table", "local_claim") if scope in scopes),
            "local_claim",
        )
        if count != 3 or candidate.state in ("unknown", "conflict"):
            reasons.append(f"REVIEW:{element_id}")
        if track is not None:
            names = MAPPINGS[track].get(element_id)
            if names is None:
                reasons.append(f"ELEMENT_TRACK_MISMATCH:{element_id}")
                continue
            if candidate.state == "present":
                for name in names:
                    facts.append(
                        ConfirmedFact(
                            name,
                            "present",
                            refs,
                            tenant_id,
                            True,
                            True,
                            source_scope=scope,
                            normalized_value=candidate.normalized_value,
                        )
                    )
            else:
                # No synthetic absence/applicability attestation is minted here.
                facts.extend(ConfirmedFact(name, candidate.state) for name in names)
    hashes = tuple(run.semantic_hash for run in ordered)
    confirmed = None
    if not reasons and track is not None:
        first = ordered[0]
        header = next(iter(headers))
        confirmed = ConfirmedTags(
            tenant_id,
            data["document_version_id"],
            data["claim_id"],
            track,
            tuple(facts),
            tag_revision,
            packet.packet_sha256,
            first.model_sha256,
            first.prompt_sha256,
            hashes,
            rulepack.ontology_version,
            header[1],
            header[2],
            first.product_variant,
        )
    return ConsensusResult(
        tuple(candidates),
        tuple(agreement),
        "auto_confirmed" if confirmed else "needs_review",
        confirmed,
        tuple(sorted(set(reasons))),
        hashes,
    )


def _partial_facts(ordered, data, packet, rulepack, tenant_id, tag_revision, rule_gaps):
    """Assemble complete guarded element states without asserting a missing search was negative."""
    headers = {
        (r.guarded.track, r.guarded.safe_harbor_category, r.guarded.superlative_quote)
        for r in ordered
        if r.guarded
    }
    hashes = tuple(r.semantic_hash for r in ordered)
    retryable = {"MODEL_UNAVAILABLE", "BUDGET_EXHAUSTED", "TAGGING_INPUT_COUNT_INVALID"}
    fatal = any(
        error != "DETERMINISTIC_CHECK_REQUIRED:P6"
        and not error.startswith("UNRESOLVED:")
        and not (run.guarded is None and error in retryable)
        for run in ordered
        for error in run.errors
    )
    providers = [
        r.usage.provider_request_id for r in ordered if r.usage and r.usage.provider_request_id
    ]
    if (
        len(headers) != 1
        or next(iter(headers))[:2] != (data["track"], data.get("safe_harbor_category"))
        or len(providers) != len(set(providers))
        or fatal
    ):
        return ConsensusResult((), (), "needs_review", None, ("PARTIAL_FACTS_UNSAFE",), hashes)
    track, category, superlative = next(iter(headers))
    elements, agreement, facts, reasons = [], [], [], list(rule_gaps)
    for element_id in data["allowed_elements"]:
        votes = [
            next((e for e in run.guarded.elements if e.element_id == element_id), None)
            if run.guarded
            else None
            for run in ordered
        ]
        keys = [
            (
                e.state,
                e.normalized_value,
                dict(run.binding_hashes).get(element_id),
                dict(run.source_scopes).get(element_id),
            )
            if e is not None
            else None
            for e, run in zip(votes, ordered, strict=True)
        ]
        agreement.append(
            (element_id, max((keys.count(key) for key in keys if key is not None), default=0))
        )
        valid_keys = {key for key in keys if key is not None}
        if len(valid_keys) > 1:
            state = "conflict"
        elif any(key is None for key in keys):
            state = "unknown"
        elif (
            votes[0].state == "present"
            and all(key[2] is not None for key in keys)
            and all(ref.verification_state == "verified" for e in votes for ref in e.evidence_refs)
        ):
            state = "present"
        elif votes[0].state == "unknown":
            state = "unknown"
        else:
            # LLM absence/N/A never supplies search coverage or applicability authority.
            state = "unknown"
        refs = (
            tuple(dict.fromkeys(ref for e in votes if e for ref in e.evidence_refs))
            if state == "present"
            else ()
        )
        base = next((e for e in votes if e is not None), None)
        elements.append(
            replace(
                base,
                state=state,
                evidence_refs=refs,
                normalized_value=base.normalized_value if state == "present" else None,
                credited_from=base.credited_from if state == "present" else None,
            )
            if base
            else LlmElement(element_id, "unknown", (), None, None, "UNSEARCHED")
        )
        if state != "present":
            reasons.append(f"REVIEW:{element_id}")
        scopes = {dict(run.source_scopes).get(element_id) for run in ordered if run.guarded}
        scope = next(
            (s for s in ("global_bound", "same_table", "local_claim") if s in scopes), "local_claim"
        )
        for name in MAPPINGS[track][element_id]:
            facts.append(
                ConfirmedFact(
                    name,
                    "present",
                    refs,
                    tenant_id,
                    True,
                    True,
                    source_scope=scope,
                    normalized_value=base.normalized_value,
                )
                if state == "present"
                else ConfirmedFact(name, state)
            )
    # The packet has every track ID, but rule-branch and conditional facts may
    # have no element vote. Record them as unknown so rescore can reuse this tag.
    observed = {fact.name for fact in facts}
    definitions = rulepack.file_content("rubric/elements.yaml")["elements"]
    required = {
        e["trigger"] for e in definitions if e["id"] in MAPPINGS[track] and e.get("trigger")
    }
    for branch in rulepack.file_content(f"rubric/{track}.yaml")["branches"]:
        required.update(branch.get("when", {}))
        required.update(branch.get("require_all", ()))
    required.discard("exception_applies")
    if track == "goal":
        required.update(("target_metric", "transition_plan"))
    facts.extend(ConfirmedFact(name, "unknown") for name in sorted(required - observed))
    if all(element.state == "present" for element in elements):
        # This profile only publishes reviewable partial facts, never an automatic confirmation.
        return ConsensusResult(
            tuple(elements),
            tuple(agreement),
            "needs_review",
            None,
            ("PARTIAL_FACTS_REVIEW_REQUIRED",),
            hashes,
        )
    first = ordered[0]
    tags = ConfirmedTags(
        tenant_id,
        data["document_version_id"],
        data["claim_id"],
        track,
        tuple(facts),
        tag_revision,
        packet.packet_sha256,
        first.model_sha256,
        first.prompt_sha256,
        hashes,
        rulepack.ontology_version,
        category,
        superlative,
        first.product_variant,
    )
    return ConsensusResult(
        tuple(elements), tuple(agreement), "needs_review", tags, tuple(sorted(set(reasons))), hashes
    )
