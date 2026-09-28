"""Human tag revisions; source guards and pure rules are the only grading path.

An additive, explicitly-delegated AI review route exists for a trusted local
operator (``ReviewService.resolve_ai_delegated_review``). It reuses the exact
same source/binding/If-Match/engine guards as the human route and records an
honest ``ai_delegated`` origin -- it never writes ``human`` provenance for
machine-driven work. The HTTP body can never self-assert provenance: both
routes accept only the fixed 4-key correction body, and the AI origin is
supplied by backend constructor arguments, never by caller JSON.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from functools import partial
from inspect import signature
from typing import Any, Protocol
from unicodedata import normalize
from uuid import NAMESPACE_URL, uuid5

from proofops.application.authorization import AuthContext
from proofops.application.evidence.binding import ClaimContext, accept_binding, relation_tags_for
from proofops.application.evidence.report_level import POLICIES as REPORT_LEVEL_POLICIES
from proofops.application.evidence.report_level import check_report_level
from proofops.application.evidence.retrieval import EvidencePacket
from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.application.tagging.consensus import (
    PARTIAL_FACTS_HASH,
    PARTIAL_FACTS_V1,
    ConsensusResult,
    form_consensus,
)
from proofops.application.tagging.report_level_link import replay_report_level_link, strict_fallback
from proofops.application.tagging.service import TagRun
from proofops.domain.audit import AuditConflict
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot
from proofops.domain.rules.engine import (
    MAPPINGS,
    ConfirmedFact,
    ConfirmedTags,
    Decision,
    RuleContext,
    evaluate,
)
from proofops.domain.rules.safe_harbor import CHECKLIST_POLICY_V1
from proofops.domain.values import (
    SourceRef,
    _element_from_dict,
    _require_uuid,
    _source_ref_from_dict,
)


class ReviewRejected(ValueError):
    def __init__(self, code: str, status: int = 422):
        super().__init__(code)
        self.code, self.status = code, status


# Honest provenance labels for the additive AI-delegated route. Reuses the
# rulepack-activation prefix pattern (``ai-delegated-review:<operator>``) so a
# machine-driven revision can never be mistaken for an independent human
# confirmation. Old ``human``/``consensus`` rows stay valid and immutable.
#
# a bounded correction for a safe-harbor category the tagging headers agree
# on but the reviewed source does not support. v1 REMOVES a wrong non-null
# category only; adding or replacing one would assert a regulatory
# classification the reviewed refs do not establish, so it is refused as
# unsupported until a concrete need exists. The policy name states the scope.
CATEGORY_POLICY_V1 = "claim_category_removal_v1"
AI_DELEGATED_ORIGIN = "ai_delegated"
AI_DELEGATED_REVIEW_STATUS = "ai_delegated_confirmed"
AI_DELEGATED_REVIEWER_PREFIX = "ai-delegated-review:"
AI_DELEGATED_REVIEW_ORIGIN = "ai_project_interpretation"
HUMAN_ORIGIN = "human"
HUMAN_REVIEW_STATUS = "human_confirmed"


@dataclass(frozen=True)
class ReviewInputs:
    """Trusted loader boundary; retain complete packets, graph and actual tag receipts.

    A missing consensus is a valid initial revision. No model call is made here.
    Existing confirmed facts are the only ordinary element absence/computation
    attestations. Only the trusted AI route can review local claim triggers or
    policy-pinned safe-harbor facts; the HTTP body cannot create those attestations.
    """

    run_id: str
    context: ClaimContext
    original: CanonicalDocumentGraph
    rulepack: RulePackSnapshot
    rule_context: RuleContext
    packet: EvidencePacket
    original_packet: EvidencePacket
    tag_runs: tuple[TagRun, ...]
    consensus: ConsensusResult
    relation_tags: Mapping[str, Mapping[str, SourceRef | None]]
    tag_revision: int = 1
    decision: Decision | None = None
    fact_assembly_profile: str = "strict-v1"
    report_level_link: dict | None = None
    report_level_review: tuple[dict, ...] = ()

    def snapshot(self) -> dict:
        snapshot = dict(
            schema="review_inputs_v1",
            run_id=self.run_id,
            claim=asdict(self.context.claim),
            dimensions={k: asdict(v) if v else None for k, v in self.context.dimensions.items()},
            original=dict(
                graph_sha256=canonical_hash(asdict(self.original)),
                tenant_id=self.original.tenant_id,
                document_version_id=self.original.document_version_id,
                parse_manifest_id=self.original.parse_manifest_id,
                source_sha256=self.original.source_sha256,
            ),
            rulepack=asdict(self.rulepack),
            rule_context=asdict(self.rule_context),
            packet=self.packet.to_dict(),
            packet_sha256=self.packet.packet_sha256,
            original_packet=self.original_packet.to_dict(),
            original_packet_sha256=self.original_packet.packet_sha256,
            tag_runs=[asdict(r) for r in self.tag_runs],
            consensus=asdict(self.consensus),
            relation_tags={
                key: {k: asdict(v) if v else None for k, v in value.items()}
                for key, value in self.relation_tags.items()
            },
            tag_revision=self.tag_revision,
            decision=asdict(self.decision) if self.decision else None,
            execution_profile="local-synthetic-only"
            if self.rule_context.local_synthetic
            else "live",
        )
        if self.fact_assembly_profile == PARTIAL_FACTS_V1:
            snapshot["fact_assembly"] = {"profile": PARTIAL_FACTS_V1, "sha256": PARTIAL_FACTS_HASH}
        if self.report_level_link is not None:
            snapshot["report_level_link"] = self.report_level_link
            snapshot["report_level_review"] = list(self.report_level_review)
        return snapshot

    def validate(self) -> None:
        claim, data = self.context.claim, self.packet.to_dict()
        if (
            (claim.tenant_id, self.original.tenant_id, self.rulepack.tenant_id)
            != (self.rule_context.tenant_id,) * 3
            or (claim.document_version_id, claim.claim_id, self.packet.packet_sha256)
            != (
                self.rule_context.document_version_id,
                self.rule_context.claim_id,
                self.rule_context.packet_sha256,
            )
            or data["run_id"] != self.run_id
            or data["graph_sha256"] != canonical_hash(asdict(self.original))
            or data.get("retrieval_packet_sha256") != self.original_packet.packet_sha256
            or type(self.tag_revision) is not int
            or self.tag_revision < 1
        ):
            raise ReviewRejected("REVIEW_INPUT_MISMATCH", 409)
        result = form_consensus(
            self.tag_runs,
            packet=self.packet,
            rulepack=self.rulepack,
            tenant_id=claim.tenant_id,
            tag_revision=self.tag_revision,
            profile=self.fact_assembly_profile,
        )
        if self.report_level_link is not None:
            try:
                result = replay_report_level_link(
                    result,
                    config=self.report_level_link,
                    context=self.context,
                    graph=self.original,
                    receipts=self.report_level_review,
                    fallback_tags=strict_fallback(
                        self.tag_runs,
                        self.packet,
                        self.rulepack,
                        claim.tenant_id,
                        self.tag_revision,
                        self.fact_assembly_profile,
                    ),
                )
            except (ValueError, KeyError, TypeError) as exc:
                raise ReviewRejected("REPORT_LEVEL_LINK_REPLAY_MISMATCH", 409) from exc
        elif self.report_level_review:
            raise ReviewRejected("REPORT_LEVEL_LINK_REPLAY_MISMATCH", 409)
        if self.report_level_link is not None and (
            result.candidate_elements != self.consensus.candidate_elements
            or result.confirmed_tags != self.consensus.confirmed_tags
            or result.reasons != self.consensus.reasons
            or result.review_status != self.consensus.review_status
        ):
            raise ReviewRejected("REPORT_LEVEL_LINK_REPLAY_MISMATCH", 409)
        if result.replicate_hashes != self.consensus.replicate_hashes:
            raise ReviewRejected("REVIEW_RECEIPT_MISMATCH", 409)
        if self.consensus.confirmed_tags is not None and (
            self.consensus.confirmed_tags != result.confirmed_tags
        ):
            raise ReviewRejected("REVIEW_CONFIRMATION_MISMATCH", 409)
        if (
            self.decision is not None
            and self.decision.decision_status == "decided"
            and (
                self.fact_assembly_profile == PARTIAL_FACTS_V1
                or self.consensus.review_status != "auto_confirmed"
            )
        ):
            raise ReviewRejected("PARTIAL_FACTS_REVIEW_REQUIRED", 409)
        if self.decision is not None and (
            self.consensus.confirmed_tags is None
            or evaluate(self.consensus.confirmed_tags, self.rule_context, self.rulepack)
            != self.decision
        ):
            raise ReviewRejected("REVIEW_DECISION_MISMATCH", 409)


class ReviewStore(Protocol):
    def publish(self, inputs: ReviewInputs, review: dict) -> dict: ...
    def publish_transaction(self, db, inputs: ReviewInputs, review: dict) -> dict: ...
    def get(self, tenant_id: str, review_id: str) -> dict: ...
    def resolve(
        self,
        actor: AuthContext,
        review_id: str,
        body: dict,
        expected: int,
        key: str,
        build: Callable,
        *,
        reopen: bool = False,
    ) -> dict: ...


def parse_resolution(body: Any):
    if not isinstance(body, dict) or set(body) != {
        "base_tag_revision",
        "track",
        "elements",
        "reason",
    }:
        raise ReviewRejected("VALIDATION_ERROR")
    if (
        type(body["base_tag_revision"]) is not int
        or body["base_tag_revision"] < 1
        or not isinstance(body["track"], str)
        or body["track"] not in MAPPINGS
        or not isinstance(body["reason"], str)
        or not 5 <= len(body["reason"].strip()) <= 1000
        or not isinstance(body["elements"], list)
    ):
        raise ReviewRejected("VALIDATION_ERROR")
    elements = tuple(_element_from_dict(e) for e in body["elements"])
    identifiers = [e.element_id for e in elements]
    if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(
        MAPPINGS[body["track"]]
    ):
        raise ReviewRejected("COMPLETE_TRACK_ELEMENTS_REQUIRED")
    return elements


def _review_applicability(inputs: ReviewInputs, track: str, review: Any):
    """Verify complete atomic-claim coverage, never report-wide search absence."""
    if not isinstance(review, dict) or set(review) != {
        "policy",
        "input_snapshot_sha256",
        "track",
        "claim_source_refs",
        "source_authority",
        "triggers",
    }:
        raise ReviewRejected("APPLICABILITY_REVIEW_INVALID")
    if (
        review["policy"] != "local_claim_applicability_v1"
        or review["track"] != track
        or review["input_snapshot_sha256"] != canonical_hash(inputs.snapshot())
        or not isinstance(review["source_authority"], str)
        or not 5 <= len(review["source_authority"].strip()) <= 1000
        or not isinstance(review["triggers"], list)
        or not review["triggers"]
    ):
        raise ReviewRejected("APPLICABILITY_REVIEW_INVALID")
    claim = inputs.context.claim
    # Exact complete refs from the replayed loader, not caller-selected subquotes.
    if not claim.source_refs or canonical_hash(review["claim_source_refs"]) != canonical_hash(
        [asdict(ref) for ref in claim.source_refs]
    ):
        raise ReviewRejected("WHOLE_CLAIM_REQUIRED")
    refs = tuple(
        verify_source_ref(ref, inputs.original, tenant_id=claim.tenant_id)
        for ref in claim.source_refs
    )
    if any(ref.verification_state != "verified" for ref in refs):
        raise ReviewRejected("SOURCE_REJECTED")
    allowed = {
        element["trigger"]
        for element in inputs.rulepack.file_content("rubric/elements.yaml")["elements"]
        if element["id"] in MAPPINGS[track]
        and element.get("requirement") == "conditional"
        and element.get("trigger")
        in {
            "offset_or_carbon_neutral_claim",
            "science_based_claim",
            "reduction_or_improvement_claim",
            "governance_claim",
            "compensation_link_claim",
        }
    }
    # The management §4.4 E0 branch ("의지 표현만") is also a whole-claim judgment;
    # accept it only where the pinned track ladder actually uses it.
    branches = inputs.rulepack.file_content(f"rubric/{track}.yaml")["branches"]
    if any("willingness_only" in branch.get("when", {}) for branch in branches):
        allowed.add("willingness_only")
    facts, seen = [], set()
    for trigger in review["triggers"]:
        if not isinstance(trigger, dict) or set(trigger) != {"name", "value", "reason"}:
            raise ReviewRejected("APPLICABILITY_REVIEW_INVALID")
        name, value = trigger["name"], trigger["value"]
        if (
            not isinstance(name, str)
            or name not in allowed
            or name in seen
            or (value is not None and type(value) is not bool)
            or not isinstance(trigger["reason"], str)
            or not 5 <= len(trigger["reason"].strip()) <= 1000
        ):
            raise ReviewRejected("APPLICABILITY_REVIEW_INVALID")
        seen.add(name)
        facts.append(
            ConfirmedFact(
                name=name,
                state="unknown" if value is None else ("present" if value else "absent"),
                evidence_refs=refs,
                source_tenant_id=claim.tenant_id,
                citation_verified=True,
                binding_accepted=True,
                search_coverage_verified=value is False,
                source_scope="local_claim",
            )
        )
    receipt = dict(
        request=review,
        identity=dict(
            tenant_id=claim.tenant_id,
            document_version_id=claim.document_version_id,
            claim_id=claim.claim_id,
            run_id=inputs.run_id,
            parse_manifest_id=inputs.original.parse_manifest_id,
            source_sha256=inputs.original.source_sha256,
            graph_sha256=canonical_hash(asdict(inputs.original)),
            claim_sha256=canonical_hash(asdict(claim)),
            packet_sha256=inputs.packet.packet_sha256,
            original_packet_sha256=inputs.original_packet.packet_sha256,
            rulepack_sha256=inputs.rulepack.sha256,
            input_snapshot_sha256=canonical_hash(inputs.snapshot()),
        ),
        verified_claim_source_refs=[asdict(ref) for ref in refs],
        coverage_scope="local_claim",
    )
    return facts, receipt


def _review_safe_harbor(inputs: ReviewInputs, review: Any):
    """Create only explicit checklist facts from replayed packet references."""
    if not isinstance(review, dict) or set(review) != {
        "policy",
        "input_snapshot_sha256",
        "category",
        "source_authority",
        "facts",
    }:
        raise ReviewRejected("SAFE_HARBOR_REVIEW_INVALID")
    config = inputs.rulepack.file_content("regulatory/safe_harbor.yaml")
    category = review["category"]
    try:
        expected = tuple(config["category_checklists"][category])
    except (KeyError, TypeError):
        raise ReviewRejected("SAFE_HARBOR_REVIEW_INVALID") from None
    packet = inputs.packet.to_dict()
    headers = {
        run.guarded.safe_harbor_category for run in inputs.tag_runs if run.guarded is not None
    }
    if (
        review["policy"] != CHECKLIST_POLICY_V1
        or config.get("reasonable_basis_boolean_mapping") != CHECKLIST_POLICY_V1
        or review["input_snapshot_sha256"] != canonical_hash(inputs.snapshot())
        or not isinstance(category, str)
        or not expected
        or headers != {category}
        or packet.get("safe_harbor_category") != category
        or not isinstance(review["source_authority"], str)
        or not 5 <= len(review["source_authority"].strip()) <= 1000
        or not isinstance(review["facts"], list)
        or len(review["facts"]) != len(expected)
    ):
        raise ReviewRejected("SAFE_HARBOR_REVIEW_INVALID")

    packet_refs = _packet_ref_index(packet)

    facts, seen, verified_refs = [], set(), []
    for item in review["facts"]:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "state",
            "evidence_refs",
            "search_coverage_verified",
            "reason",
        }:
            raise ReviewRejected("SAFE_HARBOR_REVIEW_INVALID")
        name, state = item["name"], item["state"]
        if (
            name not in expected
            or name in seen
            or state not in ("present", "absent", "unknown", "conflict")
            or not isinstance(item["evidence_refs"], list)
            or type(item["search_coverage_verified"]) is not bool
            or item["search_coverage_verified"] != (state == "absent")
            or not isinstance(item["reason"], str)
            or not 5 <= len(item["reason"].strip()) <= 1000
            or (state in ("present", "absent", "conflict") and not item["evidence_refs"])
        ):
            raise ReviewRejected("SAFE_HARBOR_REVIEW_INVALID")
        seen.add(name)
        refs, scopes = [], set()
        for raw in item["evidence_refs"]:
            match = packet_refs.get(canonical_hash(raw)) if isinstance(raw, dict) else None
            if match is None or match[1] not in ("local_claim", "same_table"):
                raise ReviewRejected("SAFE_HARBOR_SOURCE_REJECTED")
            checked = verify_source_ref(
                _source_ref_from_dict(raw),
                inputs.original,
                tenant_id=inputs.context.claim.tenant_id,
            )
            if checked.verification_state != "verified":
                raise ReviewRejected("SAFE_HARBOR_SOURCE_REJECTED")
            refs.append(checked)
            scopes.add(match[1])
            verified_refs.append(asdict(checked))
        facts.append(
            ConfirmedFact(
                name=name,
                state=state,
                evidence_refs=tuple(refs),
                source_tenant_id=inputs.context.claim.tenant_id if refs else None,
                citation_verified=bool(refs),
                binding_accepted=bool(refs),
                search_coverage_verified=item["search_coverage_verified"],
                source_scope="same_table" if "same_table" in scopes else "local_claim",
            )
        )
    if seen != set(expected):
        raise ReviewRejected("SAFE_HARBOR_REVIEW_INVALID")
    receipt = {
        "request": review,
        "identity": {
            "tenant_id": inputs.context.claim.tenant_id,
            "document_version_id": inputs.context.claim.document_version_id,
            "claim_id": inputs.context.claim.claim_id,
            "run_id": inputs.run_id,
            "parse_manifest_id": inputs.original.parse_manifest_id,
            "source_sha256": inputs.original.source_sha256,
            "graph_sha256": canonical_hash(asdict(inputs.original)),
            "packet_sha256": inputs.packet.packet_sha256,
            "rulepack_sha256": inputs.rulepack.sha256,
            "input_snapshot_sha256": canonical_hash(inputs.snapshot()),
        },
        "verified_packet_source_refs": verified_refs,
    }
    return facts, receipt


def _packet_ref_index(packet: Mapping[str, Any]) -> dict[str, tuple[dict, str]]:
    """Index the immutable packet's own references by canonical hash and scope."""
    packet_refs: dict[str, tuple[dict, str]] = {}
    for raw in packet.get("claim_source_refs", []):
        packet_refs[canonical_hash(raw)] = (raw, "local_claim")
    for candidate in packet.get("evidence_candidates", []):
        scope = candidate.get("source_scope")
        for raw in candidate.get("source_refs", []):
            packet_refs.setdefault(canonical_hash(raw), (raw, scope))
    return packet_refs


def _review_category(inputs: ReviewInputs, track: str, review: Any):
    """Validate an explicit removal of a wrong safe-harbor category; create no fact.

    The request must PIN what the models and the frozen packet actually said:
    the observed category is checked against every guarded tag-run header and
    against the packet value, so this is not an override that ignores those
    checks. Nothing here rewrites a header, a packet or a replica hash, and no
    ``ConfirmedFact`` is produced -- a category is a classification header, not
    an observed fact. Returns ``(corrected_category, receipt, superseded_names)``.
    """
    if not isinstance(review, dict) or set(review) != {
        "policy",
        "input_snapshot_sha256",
        "track",
        "observed_category",
        "corrected_category",
        "claim_source_refs",
        "basis_refs",
        "source_authority",
        "reason",
    }:
        raise ReviewRejected("CATEGORY_REVIEW_INVALID")
    config = inputs.rulepack.file_content("regulatory/safe_harbor.yaml")
    checklists = config.get("category_checklists") or {}
    observed = review["observed_category"]
    if (
        review["policy"] != CATEGORY_POLICY_V1
        or review["track"] != track
        or review["input_snapshot_sha256"] != canonical_hash(inputs.snapshot())
        # Only a category this pinned regulatory config defines can be removed;
        # an unknown name is escalated instead of silently dropped.
        or not isinstance(observed, str)
        or observed not in checklists
        or not isinstance(review["claim_source_refs"], list)
        or not isinstance(review["basis_refs"], list)
        or not review["basis_refs"]
        or any(
            not isinstance(text, str) or not 5 <= len(text.strip()) <= 1000
            for text in (review["source_authority"], review["reason"])
        )
    ):
        raise ReviewRejected("CATEGORY_REVIEW_INVALID")
    if review["corrected_category"] is not None:
        raise ReviewRejected("CATEGORY_CORRECTION_UNSUPPORTED")
    packet = inputs.packet.to_dict()
    headers = {
        run.guarded.safe_harbor_category for run in inputs.tag_runs if run.guarded is not None
    }
    if headers != {observed} or packet.get("safe_harbor_category") != observed:
        raise ReviewRejected("CATEGORY_REVIEW_OBSERVED_MISMATCH", 409)
    claim = inputs.context.claim
    # A category is a property of the whole atomic claim, so the replayed loader
    # refs must match exactly; caller-selected subquotes are refused.
    if not claim.source_refs or canonical_hash(review["claim_source_refs"]) != canonical_hash(
        [asdict(ref) for ref in claim.source_refs]
    ):
        raise ReviewRejected("WHOLE_CLAIM_REQUIRED")
    claim_refs = tuple(
        verify_source_ref(ref, inputs.original, tenant_id=claim.tenant_id)
        for ref in claim.source_refs
    )
    if any(ref.verification_state != "verified" for ref in claim_refs):
        raise ReviewRejected("CATEGORY_SOURCE_REJECTED")
    packet_refs = _packet_ref_index(packet)
    basis_refs = []
    for raw in review["basis_refs"]:
        match = packet_refs.get(canonical_hash(raw)) if isinstance(raw, dict) else None
        if match is None or match[1] not in ("local_claim", "same_table"):
            raise ReviewRejected("CATEGORY_SOURCE_REJECTED")
        checked = verify_source_ref(
            _source_ref_from_dict(raw), inputs.original, tenant_id=claim.tenant_id
        )
        if checked.verification_state != "verified":
            raise ReviewRejected("CATEGORY_SOURCE_REJECTED")
        basis_refs.append(asdict(checked))
    receipt = {
        "request": review,
        "identity": {
            "tenant_id": claim.tenant_id,
            "document_version_id": claim.document_version_id,
            "claim_id": claim.claim_id,
            "run_id": inputs.run_id,
            "parse_manifest_id": inputs.original.parse_manifest_id,
            "source_sha256": inputs.original.source_sha256,
            "graph_sha256": canonical_hash(asdict(inputs.original)),
            "claim_sha256": canonical_hash(asdict(claim)),
            "packet_sha256": inputs.packet.packet_sha256,
            "original_packet_sha256": inputs.original_packet.packet_sha256,
            "rulepack_sha256": inputs.rulepack.sha256,
            "input_snapshot_sha256": canonical_hash(inputs.snapshot()),
        },
        "observed_category": observed,
        "corrected_category": None,
        # The models' own words stay recorded next to the correction.
        "observed_tag_run_headers": [
            run.guarded.safe_harbor_category for run in inputs.tag_runs if run.guarded is not None
        ],
        "observed_packet_safe_harbor_category": packet.get("safe_harbor_category"),
        "superseded_checklist_items": [
            name for name in checklists.get(observed, ()) if isinstance(name, str)
        ],
        "verified_claim_source_refs": [asdict(ref) for ref in claim_refs],
        "verified_basis_refs": basis_refs,
    }
    return None, receipt, tuple(receipt["superseded_checklist_items"])


class ReviewService:
    def __init__(
        self,
        store: ReviewStore,
        *,
        load_inputs: Callable[[str, str, str], ReviewInputs],
        verify_context_sources=None,
        load_run_snapshot=None,
    ):
        self.store, self.load_inputs = store, load_inputs
        self.verify_context_sources = verify_context_sources
        self.load_run_snapshot = load_run_snapshot

    def _review(self, inputs: ReviewInputs, review_id: str | None = None) -> dict:
        inputs.validate()
        claim = inputs.context.claim
        review_id = review_id or str(uuid5(NAMESPACE_URL, canonical_hash(inputs.snapshot())))
        _require_uuid("review_id", review_id)
        review = dict(
            review_id=review_id,
            run_id=inputs.run_id,
            claim_id=claim.claim_id,
            status="open",
            revision=1,
            base_tag_revision=inputs.tag_revision,
            reason_codes=list(inputs.consensus.reasons)
            + (
                []
                if inputs.rule_context.local_synthetic
                or (
                    inputs.rulepack.status == "active"
                    and inputs.rulepack.approved_by
                    and inputs.rulepack.approved_at
                )
                else ["RULEPACK_APPROVAL_REQUIRED"]
            ),
        )
        return review

    def publish(self, inputs: ReviewInputs, review_id: str | None = None) -> dict:
        return self.store.publish(inputs, self._review(inputs, review_id))

    def publish_transaction(self, connection, inputs: ReviewInputs, review_id: str | None = None):
        return self.store.publish_transaction(connection, inputs, self._review(inputs, review_id))

    def resolve_review(
        self,
        actor,
        review_id,
        body,
        if_match,
        idempotency_key,
        *,
        category_review=None,
        reopen=False,
    ):
        """Default human route: provenance is always human, never caller-chosen.

        ``reopen=True`` is the explicit human re-review action for a review that
        was already resolved: it produces the next immutable tag/decision/review
        revisions on the CURRENT head and never mutates or reverts the prior
        ones. The default (``reopen=False``) still refuses a resolved review.

        ``category_review`` is a trusted backend-only keyword (the HTTP router
        never supplies it), so the HTTP request contract is unchanged. A human
        re-review still CARRIES and re-validates a prior correction receipt
        without supplying anything.
        """
        return self._resolve_with_provenance(
            actor,
            review_id,
            body,
            if_match,
            idempotency_key,
            origin=HUMAN_ORIGIN,
            review_status=HUMAN_REVIEW_STATUS,
            reviewer_sub=None,
            extra_tag=None,
            category_review=category_review,
            reopen=reopen,
        )

    def resolve_ai_delegated_review(
        self,
        actor,
        review_id,
        body,
        if_match,
        idempotency_key,
        *,
        delegated_reviewer: str,
        delegation_authority: str,
        applicability_review: dict | None = None,
        safe_harbor_review: dict | None = None,
        category_review: dict | None = None,
        context_review: dict | None = None,
        reopen: bool = False,
    ):
        """Trusted backend-only operation for explicit user-delegated AI review.

        Not reachable from HTTP: ``delegated_reviewer`` and
        ``delegation_authority`` are constructor arguments supplied by local
        operator code (the CLI), never parsed from the correction body, so a
        remote caller cannot self-assert trusted provenance. All factual
        guards (source/binding/If-Match/engine) also apply here. Optional
        applicability and safe-harbor reviews are separately pinned and cannot
        authorize ordinary element absence or a caller-supplied grade.

        ``reopen=True`` is the explicit re-review action for an already-resolved
        review; it stays honestly ``ai_delegated`` and produces the next
        immutable revisions on the current head, never a ``human`` label and
        never a silent mutation of a prior revision.
        """
        if not isinstance(delegated_reviewer, str) or not delegated_reviewer.strip():
            raise ReviewRejected("VALIDATION_ERROR")
        if not isinstance(delegation_authority, str) or not delegation_authority.strip():
            raise ReviewRejected("VALIDATION_ERROR")
        reviewer = delegated_reviewer.strip()
        if len(reviewer) > 320 or "\n" in reviewer:
            raise ReviewRejected("VALIDATION_ERROR")
        return self._resolve_with_provenance(
            actor,
            review_id,
            body,
            if_match,
            idempotency_key,
            origin=AI_DELEGATED_ORIGIN,
            review_status=AI_DELEGATED_REVIEW_STATUS,
            reviewer_sub=f"{AI_DELEGATED_REVIEWER_PREFIX}{reviewer}",
            extra_tag={
                "review_origin": AI_DELEGATED_REVIEW_ORIGIN,
                "delegation_authority": delegation_authority.strip(),
                "delegated_reviewer": reviewer,
            },
            applicability_review=applicability_review,
            safe_harbor_review=safe_harbor_review,
            category_review=category_review,
            context_review=context_review,
            reopen=reopen,
        )

    def _resolve_with_provenance(
        self,
        actor,
        review_id,
        body,
        if_match,
        idempotency_key,
        *,
        origin,
        review_status,
        reviewer_sub,
        extra_tag,
        applicability_review=None,
        safe_harbor_review=None,
        category_review=None,
        context_review=None,
        reopen=False,
    ):
        if not isinstance(actor, AuthContext) or not actor.has_capability("reviewer"):
            raise ReviewRejected("FORBIDDEN", 403)
        _require_uuid("review_id", review_id)
        if not isinstance(if_match, str) or not re.fullmatch(r'"[1-9][0-9]*"', if_match):
            raise ReviewRejected("IF_MATCH_REQUIRED", 400)
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 128:
            raise ReviewRejected("IDEMPOTENCY_KEY_INVALID", 400)
        elements = parse_resolution(body)

        # Existing local read adapters open transactions too. Load the immutable
        # inputs before the writer lock; the transaction still compares pins and
        # both heads before publishing anything.
        target = self.store.get(actor.tenant_id, review_id)
        try:
            inputs = self.load_inputs(actor.tenant_id, target["run_id"], target["claim_id"])
        except KeyError:
            raise ReviewRejected("REVIEW_INPUT_UNAVAILABLE", 409) from None
        verifier_parameters = (
            signature(self.verify_context_sources).parameters
            if self.verify_context_sources is not None
            else {}
        )
        pinned_run_snapshot = (
            self.load_run_snapshot(actor.tenant_id, target["run_id"])
            if self.load_run_snapshot is not None and "pinned_run_snapshot" in verifier_parameters
            else None
        )
        if not inputs.rule_context.local_synthetic and not (
            inputs.rulepack.status == "active"
            and inputs.rulepack.approved_by
            and inputs.rulepack.approved_at
        ):
            raise ReviewRejected("RULEPACK_APPROVAL_REQUIRED", 409)

        def build(review, initial, decision_revision):
            inputs.validate()
            # ``initial`` is the current tag head: the consensus tag (carries the
            # full ``inputs`` snapshot) on the first resolve, or a prior reviewed
            # tag (carries only ``input_snapshot_sha256``) on an explicit
            # re-review. Both must pin the same immutable loader snapshot, so a
            # re-review can never silently rebind to different inputs.
            initial_snapshot_sha256 = (
                canonical_hash(initial["inputs"])
                if "inputs" in initial
                else initial.get("input_snapshot_sha256")
            )
            if canonical_hash(inputs.snapshot()) != initial_snapshot_sha256:
                raise ReviewRejected("REVIEW_INPUT_MISMATCH", 409)
            # On an explicit re-review the fact base is the ORIGINAL consensus,
            # but any prior reviewed applicability/safe-harbor attestation on the
            # current head must be carried forward and re-validated (never
            # silently reverted). Re-supplied reviews override the carried ones;
            # when the caller omits them we replay the prior receipt's exact
            # request through the same guard chain so an unchanged re-review
            # reproduces the same honest decision (e.g. M5/M6 exclusion).
            carried_applicability = None
            carried_safe_harbor = None
            carried_category = None
            # Preserve the ORIGINAL attestation ancestry across chained carries:
            # if the prior receipt already recorded a ``carried_from`` we keep it,
            # otherwise the immediate prior tag's provenance is the origin. This
            # stops a human->human re-review from erasing an AI-delegated ancestor.
            prior_provenance = {
                k: initial[k]
                for k in ("origin", "reviewer_sub", "review_origin", "delegated_reviewer")
                if k in initial
            }
            applicability_ancestry = prior_provenance
            safe_harbor_ancestry = prior_provenance
            category_ancestry = prior_provenance
            if reopen:
                prior_applicability = initial.get("applicability_review")
                if isinstance(prior_applicability, dict):
                    carried_applicability = prior_applicability.get("request")
                    if isinstance(prior_applicability.get("carried_from"), dict):
                        applicability_ancestry = prior_applicability["carried_from"]
                prior_safe_harbor = initial.get("safe_harbor_review")
                if isinstance(prior_safe_harbor, dict):
                    carried_safe_harbor = prior_safe_harbor.get("request")
                    if isinstance(prior_safe_harbor.get("carried_from"), dict):
                        safe_harbor_ancestry = prior_safe_harbor["carried_from"]
                prior_category = initial.get("category_review")
                if isinstance(prior_category, dict):
                    carried_category = prior_category.get("request")
                    if isinstance(prior_category.get("carried_from"), dict):
                        category_ancestry = prior_category["carried_from"]
            effective_applicability = (
                applicability_review if applicability_review is not None else carried_applicability
            )
            effective_safe_harbor = (
                safe_harbor_review if safe_harbor_review is not None else carried_safe_harbor
            )
            effective_category = (
                category_review if category_review is not None else carried_category
            )
            # Provenance of a carried (not re-supplied) attestation stays the
            # original reviewer's, never relabelled as the current actor. A human
            # HTTP re-review therefore cannot present an inherited AI-delegated
            # applicability/safe-harbor receipt as freshly human-attested.
            applicability_carried = (
                applicability_review is None and carried_applicability is not None
            )
            safe_harbor_carried = safe_harbor_review is None and carried_safe_harbor is not None
            category_carried = category_review is None and carried_category is not None
            base = inputs.consensus.confirmed_tags
            facts = {f.name: f for f in base.facts} if base else {}
            previous_names = {
                n for names in MAPPINGS[inputs.packet.to_dict()["track"]].values() for n in names
            }
            definitions = {
                e["id"]: e for e in inputs.rulepack.file_content("rubric/elements.yaml")["elements"]
            }
            trigger_names = {e["trigger"] for e in definitions.values() if e.get("trigger")}
            # A changed track requires fresh applicability, never inherited trigger tags.
            if body["track"] != inputs.packet.to_dict()["track"]:
                previous_names |= trigger_names | {"willingness_only"}
            # A category correction and a category checklist attestation cannot
            # coexist in one revision: the checklist documents the very category
            # being removed. Fail closed for a freshly supplied AND for a carried
            # safe-harbor review instead of dropping either one silently.
            corrected_category, category_receipt = (None, None)
            if effective_category is not None:
                if effective_safe_harbor is not None:
                    raise ReviewRejected("CATEGORY_REVIEW_CONFLICTS_SAFE_HARBOR", 409)
                corrected_category, category_receipt, superseded_items = _review_category(
                    inputs, body["track"], effective_category
                )
                # Exclude only the removed category's checklist names from the NEW
                # fact basis. The prior revision keeps them verbatim and immutable.
                previous_names |= set(superseded_items)
            reviewed_facts, applicability_receipt = ([], None)
            if effective_applicability is not None:
                reviewed_facts, applicability_receipt = _review_applicability(
                    inputs, body["track"], effective_applicability
                )
                previous_names |= {f.name for f in reviewed_facts}
            safe_harbor_facts, safe_harbor_receipt = ([], None)
            if effective_safe_harbor is not None:
                safe_harbor_facts, safe_harbor_receipt = _review_safe_harbor(
                    inputs, effective_safe_harbor
                )
                previous_names |= {f.name for f in safe_harbor_facts}
            new_facts = [f for n, f in facts.items() if n not in previous_names] + reviewed_facts
            new_facts += safe_harbor_facts
            prior_context = initial.get("claim_context_review") if reopen else None
            context_request = (
                context_review
                if context_review is not None
                else (prior_context.get("request") if isinstance(prior_context, dict) else None)
            )
            context_receipt = None
            if context_request is not None:
                from proofops.application.claim_context_review import review_facility_context

                if body["track"] != "performance":
                    raise ReviewRejected("CONTEXT_TRACK_MISMATCH")
                prior_p6 = next((e for e in initial["elements"] if e["element_id"] == "P6"), None)
                frozen_model_conflict = any(
                    e.element_id == "P6" and e.state == "conflict"
                    for run in inputs.tag_runs
                    if run.guarded
                    for e in run.guarded.elements
                )
                if prior_p6 and (
                    prior_p6["state"] == "present"
                    or (prior_p6["state"] == "conflict" and not frozen_model_conflict)
                ):
                    raise ReviewRejected("CONTEXT_CANNOT_OVERRIDE_NUMERIC_RESULT")
                try:
                    context_receipt = review_facility_context(
                        inputs,
                        context_request,
                        partial(
                            self.verify_context_sources, pinned_run_snapshot=pinned_run_snapshot
                        )
                        if pinned_run_snapshot is not None
                        else self.verify_context_sources,
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    raise ReviewRejected("CONTEXT_REVIEW_REJECTED") from exc
                original_refs = {
                    canonical_hash(asdict(ref)): ref
                    for run in inputs.tag_runs
                    if run.guarded
                    for e in run.guarded.elements
                    if e.element_id == "P6"
                    for ref in e.evidence_refs
                }
                if prior_p6 and any(
                    canonical_hash(ref) not in original_refs for ref in prior_p6["evidence_refs"]
                ):
                    raise ReviewRejected("CONTEXT_CANNOT_OVERRIDE_NUMERIC_RESULT")
                if context_review is None and prior_context is not None:
                    if canonical_hash(context_receipt["source_receipt"]) != canonical_hash(
                        prior_context["source_receipt"]
                    ):
                        raise ReviewRejected("CONTEXT_SOURCE_REPLAY_MISMATCH", 409)
                    context_receipt["carried_from"] = prior_context.get(
                        "carried_from", prior_provenance
                    )
            checked_elements = []
            report_level_receipts = []
            prior_report_level = {
                item["element_id"]: item for item in initial.get("report_level_review", ())
            }
            page_texts: dict[int, list[str]] | None = None
            for element in elements:
                if element.element_id == "P6" and context_receipt is not None:
                    # The caller cannot choose the computed state. No accepted
                    # table binding -> unknown, with every old citation retained.
                    element = replace(
                        element,
                        state="unknown",
                        normalized_value=None,
                        evidence_refs=tuple(original_refs.values()),
                        credited_from=None,
                        reason_code="NUMERIC_BINDING_REQUIRED",
                    )
                names = MAPPINGS[body["track"]][element.element_id]
                previous = [facts.get(name) for name in names]
                report_level = (
                    element.state == "present"
                    and element.reason_code is not None
                    and REPORT_LEVEL_POLICIES.get(element.element_id) == element.reason_code
                )
                graph = inputs.original
                source_receipt = None
                if report_level:
                    if self.verify_context_sources is None:
                        raise ReviewRejected("REPORT_LEVEL_SOURCE_REJECTED")
                    attestation_refs = element.evidence_refs
                    prior = prior_report_level.get(element.element_id)
                    prior_element = next(
                        (
                            item
                            for item in initial["elements"]
                            if item["element_id"] == element.element_id
                        ),
                        None,
                    )
                    carried = (
                        prior is not None
                        and prior_element is not None
                        and prior_element["state"] == element.state
                        and prior_element["reason_code"] == element.reason_code
                        and prior_element["credited_from"] == element.credited_from
                        and tuple(
                            replace(_source_ref_from_dict(ref), verification_state="candidate")
                            for ref in prior_element["evidence_refs"]
                        )
                        == tuple(
                            replace(ref, verification_state="candidate")
                            for ref in element.evidence_refs
                        )
                    )
                    if carried:
                        try:
                            prior_refs = tuple(_source_ref_from_dict(ref) for ref in prior["refs"])
                        except (KeyError, TypeError, ValueError) as exc:
                            raise ReviewRejected(
                                "REPORT_LEVEL_SOURCE_REPLAY_MISMATCH", 409
                            ) from exc
                        if (
                            prior.get("policy") != element.reason_code
                            or prior.get("credited_from") != element.credited_from
                            or tuple(
                                replace(ref, verification_state="candidate") for ref in prior_refs
                            )
                            != tuple(
                                replace(ref, verification_state="candidate")
                                for ref in element.evidence_refs
                            )
                        ):
                            raise ReviewRejected("REPORT_LEVEL_SOURCE_REPLAY_MISMATCH", 409)
                        attestation_refs = prior_refs
                    try:
                        replay = {}
                        if carried and "replay_receipt" in verifier_parameters:
                            replay["replay_receipt"] = prior["source_receipt"]
                        if carried and "published_tag" in verifier_parameters:
                            replay["published_tag"] = initial
                        if pinned_run_snapshot is not None:
                            replay["pinned_run_snapshot"] = pinned_run_snapshot
                        graph, source_receipt = self.verify_context_sources(
                            inputs, attestation_refs, **replay
                        )
                    except (ValueError, KeyError, TypeError) as exc:
                        raise ReviewRejected("REPORT_LEVEL_SOURCE_REJECTED") from exc
                refs = tuple(
                    verify_source_ref(ref, graph, tenant_id=actor.tenant_id)
                    for ref in element.evidence_refs
                )
                if any(ref.verification_state != "verified" for ref in refs):
                    raise ReviewRejected("SOURCE_REJECTED")
                if report_level:
                    receipt = dict(
                        element_id=element.element_id,
                        policy=element.reason_code,
                        refs=[asdict(ref) for ref in attestation_refs],
                        credited_from=element.credited_from,
                        source_receipt=source_receipt,
                    )
                    if carried and canonical_hash(prior.get("source_receipt")) != canonical_hash(
                        source_receipt
                    ):
                        raise ReviewRejected("REPORT_LEVEL_SOURCE_REPLAY_MISMATCH", 409)
                    report_level_receipts.append(receipt)
                if element.state in ("absent", "not_applicable"):
                    if not all(
                        f
                        and f.state == element.state
                        and (element.state != "absent" or f.search_coverage_verified)
                        for f in previous
                    ):
                        raise ReviewRejected("COVERAGE_OR_APPLICABILITY_REQUIRED")
                scope = "local_claim"
                if element.state == "present":
                    if element.element_id in ("P4", "P6"):
                        # Dedicated assurance/numeric results cannot be typed into existence.
                        if not all(
                            f
                            and f.state == "present"
                            and f.evidence_refs == refs
                            and f.normalized_value == element.normalized_value
                            for f in previous
                        ):
                            raise ReviewRejected("DETERMINISTIC_CHECK_REQUIRED")
                        scope = previous[0].source_scope
                    elif (
                        element.reason_code is not None
                        and REPORT_LEVEL_POLICIES.get(element.element_id) == element.reason_code
                    ):
                        # User-approved report-level chain (§6 2-4); literal checks only.
                        if page_texts is None:
                            page_texts = {}
                            for block in inputs.original.blocks:
                                page_texts.setdefault(block.page_num, []).append(block.raw_text)
                        if not check_report_level(
                            element.element_id,
                            element.reason_code,
                            refs,
                            claim_refs=tuple(
                                verify_source_ref(ref, inputs.original, tenant_id=actor.tenant_id)
                                for ref in inputs.context.claim.source_refs
                            ),
                            claim_quote=inputs.context.claim.quote,
                            page_texts=page_texts,
                            credited_from=element.credited_from,
                        ):
                            raise ReviewRejected("REPORT_LEVEL_BINDING_REJECTED")
                        scope = "global_bound"
                    else:
                        scopes = []
                        for ref in refs:
                            if (
                                accept_binding(
                                    inputs.context,
                                    ref,
                                    relation_tags_for(ref, inputs.relation_tags),
                                    original=inputs.original,
                                    tenant_id=actor.tenant_id,
                                    rulepack=inputs.rulepack,
                                    element_id=element.element_id,
                                )
                                != "accepted"
                            ):
                                raise ReviewRejected("BINDING_REJECTED")
                            local = any(
                                s.source_id == ref.source_id
                                and s.char_start <= ref.char_start
                                and ref.char_end <= s.char_end
                                for s in inputs.context.claim.source_refs
                            )
                            scopes.append(
                                "local_claim"
                                if local
                                else (
                                    "global_bound"
                                    if "global_bound"
                                    in definitions[element.element_id]["source_scopes"]
                                    else "same_table"
                                )
                            )
                        scope = (
                            "global_bound"
                            if "global_bound" in scopes
                            else ("same_table" if "same_table" in scopes else "local_claim")
                        )
                        value = element.normalized_value
                        if (
                            element.element_id in ("G1", "G2", "G3", "G5", "P1", "P2")
                            and value is None
                        ):
                            raise ReviewRejected("SOURCE_VALUE_REQUIRED")
                        if value is not None and not any(
                            " ".join(normalize("NFC", value).split())
                            == " ".join(normalize("NFC", ref.quote).split())
                            for ref in refs
                        ):
                            raise ReviewRejected("SOURCE_VALUE_MISMATCH")
                    if element.credited_from is not None and element.credited_from not in {
                        r.source_id for r in refs
                    }:
                        raise ReviewRejected("CREDITED_SOURCE_MISMATCH")
                new_facts.extend(
                    ConfirmedFact(
                        name,
                        element.state,
                        refs,
                        actor.tenant_id,
                        element.state == "present",
                        element.state == "present",
                        element.state == "absent",
                        scope,
                        element.normalized_value,
                    )
                    for name in names
                )
                checked_elements.append(asdict(replace(element, evidence_refs=refs)))
            headers = {
                (r.guarded.safe_harbor_category, r.guarded.superlative_quote)
                for r in inputs.tag_runs
                if r.guarded
            }
            if len(headers) != 1:
                raise ReviewRejected("CATEGORY_REVIEW_REQUIRED", 409)
            category, superlative = next(iter(headers))
            if category_receipt is not None:
                # Only the confirmed classification changes; the observed headers
                # above stay pinned in the receipt and in ``inputs``.
                category = corrected_category
            first = inputs.tag_runs[0]
            confirmed = ConfirmedTags(
                actor.tenant_id,
                inputs.original.document_version_id,
                review["claim_id"],
                body["track"],
                tuple(new_facts),
                body["base_tag_revision"] + 1,
                inputs.packet.packet_sha256,
                first.model_sha256,
                first.prompt_sha256,
                inputs.consensus.replicate_hashes,
                inputs.rulepack.ontology_version,
                category,
                superlative,
                first.product_variant,
            )
            decision = evaluate(
                confirmed,
                replace(inputs.rule_context, decision_revision=decision_revision),
                inputs.rulepack,
            )
            # Preserve the engine semantic hash exactly; review status is provenance metadata.
            api = decision.to_api_dict() | {"review_status": review_status}
            tag = dict(
                tag_revision=confirmed.tag_revision,
                confirmed_tags=asdict(confirmed),
                elements=checked_elements,
                origin=origin,
                reviewer_sub=reviewer_sub if reviewer_sub is not None else actor.user_sub,
                review_reason=body["reason"],
                input_snapshot_sha256=initial_snapshot_sha256,
            )
            if inputs.fact_assembly_profile == PARTIAL_FACTS_V1:
                tag["fact_assembly"] = inputs.snapshot()["fact_assembly"]
            if applicability_receipt is not None:
                if applicability_carried and applicability_ancestry:
                    applicability_receipt = {
                        **applicability_receipt,
                        "carried_from": applicability_ancestry,
                    }
                tag["applicability_review"] = applicability_receipt
            if safe_harbor_receipt is not None:
                if safe_harbor_carried and safe_harbor_ancestry:
                    safe_harbor_receipt = {
                        **safe_harbor_receipt,
                        "carried_from": safe_harbor_ancestry,
                    }
                tag["safe_harbor_review"] = safe_harbor_receipt
            if category_receipt is not None:
                if category_carried and category_ancestry:
                    category_receipt = {**category_receipt, "carried_from": category_ancestry}
                tag["category_review"] = category_receipt
            if context_receipt is not None:
                tag["claim_context_review"] = context_receipt
            if report_level_receipts:
                tag["report_level_review"] = report_level_receipts
            if extra_tag:
                tag.update(extra_tag)
            decision_record = dict(
                decision_revision=decision_revision, decision=asdict(decision), api=api
            )
            if "fact_assembly" in tag:
                decision_record["fact_assembly"] = tag["fact_assembly"]
            return tag, decision_record

        try:
            trusted_options = {}
            if applicability_review is not None:
                trusted_options["applicability_review"] = applicability_review
            if safe_harbor_review is not None:
                trusted_options["safe_harbor_review"] = safe_harbor_review
            if category_review is not None:
                trusted_options["category_review"] = category_review
            if context_review is not None:
                trusted_options["context_review"] = context_review
            # Every supplied trusted option joins the retry identity on EVERY
            # callable surface, not only when an AI provenance label is present:
            # a same-key retry with a changed receipt must conflict, never replay.
            # With no trusted option the identity stays the exact legacy shape so
            # stored receipts still replay byte-for-byte.
            retry_identity = dict(extra_tag or {}, **trusted_options) if trusted_options else {}
            return self.store.resolve(
                actor,
                review_id,
                body | {"_trusted_ai_review": retry_identity} if retry_identity else body,
                int(if_match[1:-1]),
                idempotency_key,
                build,
                reopen=reopen,
            )
        except KeyError:
            raise ReviewRejected("REVIEW_INPUT_UNAVAILABLE", 409) from None
        except AuditConflict:
            raise ReviewRejected("REVIEW_CONFLICT", 409) from None
        except DomainValidationError:
            raise ReviewRejected("VALIDATION_ERROR") from None
