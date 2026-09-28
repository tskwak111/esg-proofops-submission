"""Rules-only rescore over an immutable, previously source-guarded tag revision.

No parsing, tagging, I/O or tag migration occurs here. The application adapter
loads the original source/packet and current immutable tag head before calling;
the existing pure engine remains the sole producer of grades and labels.
"""

from dataclasses import asdict, dataclass
from inspect import signature
from typing import Literal

from proofops.application.evidence.report_level import POLICIES as REPORT_LEVEL_POLICIES
from proofops.application.tagging.consensus import PARTIAL_FACTS_V1
from proofops.domain.audit import AuditConflict
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot
from proofops.domain.rules.engine import (
    MAPPINGS,
    ConfirmedTags,
    Decision,
    RuleContext,
    _validate_inputs,
    evaluate,
)

REVIEWED_TAG_ORIGINS = ("human", "ai_delegated")


@dataclass(frozen=True, slots=True)
class RetagRequired:
    reasons: tuple[str, ...]
    code: Literal["RETAG_REQUIRED"] = "RETAG_REQUIRED"


def create_rescore(
    new_pack: RulePackSnapshot,
    tags: ConfirmedTags | None,
    *,
    previous_pack: RulePackSnapshot,
    context: RuleContext,
    previous_decision: Decision | None = None,
) -> Decision | RetagRequired:
    """Return a new decision or an explicit refusal to reuse missing/changed inputs.

    context.decision_revision is the proposed next head; the durable adapter must
    CAS it together with the captured tag head. No existing tag's ontology/hash
    is relabelled to make a new pack appear compatible.
    """
    if not isinstance(new_pack, RulePackSnapshot) or not isinstance(
        previous_pack, RulePackSnapshot
    ):
        raise DomainValidationError("pinned rule snapshots required")
    if not isinstance(context, RuleContext) or (
        new_pack.tenant_id != context.tenant_id
        or previous_pack.tenant_id != context.tenant_id
        or new_pack.mode != previous_pack.mode
        or new_pack.mode != context.mode
    ):
        raise DomainValidationError("rescore tenant or mode mismatch")
    if tags is None:
        return RetagRequired(("CONFIRMED_TAGS_REQUIRED",))
    _validate_inputs(tags, context, previous_pack)
    if previous_decision is not None and (
        not isinstance(previous_decision, Decision)
        or context.decision_revision != previous_decision.decision_revision + 1
        or previous_decision.tag_revision != tags.tag_revision
        or previous_decision.input_tags_sha256 != canonical_hash(asdict(tags))
    ):
        raise DomainValidationError("previous decision/tag or next revision mismatch")
    if previous_decision is None and context.decision_revision != 1:
        raise DomainValidationError("first decision revision must be one")
    if new_pack.ontology_version != previous_pack.ontology_version:
        return RetagRequired(("ONTOLOGY_CHANGED",))

    old_list = previous_pack.file_content("rubric/elements.yaml")["elements"]
    new_list = new_pack.file_content("rubric/elements.yaml")["elements"]
    old = {element["id"]: element for element in old_list}
    new = {element["id"]: element for element in new_list}
    if len(old) != len(old_list) or len(new) != len(new_list):
        raise DomainValidationError("duplicate element definitions")
    reasons = set()
    if old.keys() != new.keys():
        reasons.add("ELEMENT_SET_CHANGED")
    for element_id in old.keys() & new.keys():
        # Basis-only edits are output metadata, not new observations.
        before = {k: v for k, v in old[element_id].items() if k not in ("basis", "source_scopes")}
        after = {k: v for k, v in new[element_id].items() if k not in ("basis", "source_scopes")}
        if before != after:
            reasons.add(f"ELEMENT_MEANING_CHANGED:{element_id}")
        if set(new[element_id]["source_scopes"]) - set(old[element_id]["source_scopes"]):
            reasons.add(f"SOURCE_SCOPES_EXPANDED:{element_id}")
    if reasons:
        return RetagRequired(tuple(sorted(reasons)))

    facts = {fact.name: fact for fact in tags.facts}
    required = set()
    for element_id, names in MAPPINGS[tags.track].items():
        definition = new[element_id]
        trigger = definition.get("trigger")
        if trigger:
            required.add(trigger)
            if trigger in facts and facts[trigger].state == "absent":
                continue
        required.update(names)
        for name in names:
            if (
                name in facts
                and facts[name].state == "present"
                and facts[name].source_scope not in definition["source_scopes"]
            ):
                reasons.add(f"SOURCE_SCOPE_RECHECK:{element_id}")

    rubric = new_pack.file_content(f"rubric/{tags.track}.yaml")
    for branch in rubric["branches"]:
        required.update(branch.get("when", {}))
        required.update(branch.get("require_all", []))
    if "exception_applies" in required:
        required.remove("exception_applies")
        required.update(("target_metric", "transition_plan"))
    if tags.product_variant and tags.track == "management":
        required.add(rubric["product_variant"]["e2_plus_requires"])
    if tags.superlative_quote or (
        "has_superlative" in facts and facts["has_superlative"].state != "absent"
    ):
        required.update(("has_superlative", "comparison_basis", "external_verification"))
    if tags.track == "performance" and "categorical_ordinal" in facts:
        required.add("certification_provider")
    old_checklists = previous_pack.file_content("regulatory/safe_harbor.yaml")[
        "category_checklists"
    ]
    new_checklists = new_pack.file_content("regulatory/safe_harbor.yaml")["category_checklists"]
    if old_checklists != new_checklists:
        reasons.add("SAFE_HARBOR_INPUTS_CHANGED")
    reasons.update(f"MISSING_FACT:{name}" for name in required - facts.keys())
    if reasons:
        return RetagRequired(tuple(sorted(reasons)))
    return evaluate(tags, context, new_pack)


class RescoreRejected(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code, self.status = code, status
        super().__init__(code)


class RescoreService:
    """Prepare with verified original inputs, then atomically CAS every decision."""

    def __init__(self, store, *, load_inputs, verify_context_sources=None):
        self.store, self.load_inputs = store, load_inputs
        self.verify_context_sources = verify_context_sources

    def create_rescore(self, actor, run_id, body, idempotency_key, if_match=None):
        import re
        from dataclasses import replace

        from proofops.application.authorization import AuthContext
        from proofops.application.evidence.span_citations import verify_source_ref
        from proofops.domain.rules.engine import ConfirmedFact
        from proofops.domain.values import _require_uuid, _source_ref_from_dict

        if not isinstance(actor, AuthContext) or not actor.has_capability("reviewer"):
            raise RescoreRejected("FORBIDDEN", 403)
        _require_uuid("run_id", run_id)
        if not isinstance(body, dict) or set(body) != {"rule_pack_id", "reason"}:
            raise RescoreRejected("VALIDATION_ERROR", 422)
        _require_uuid("rule_pack_id", body["rule_pack_id"])
        if not isinstance(body["reason"], str) or not 5 <= len(body["reason"].strip()) <= 1000:
            raise RescoreRejected("VALIDATION_ERROR", 422)
        if not isinstance(idempotency_key, str) or not 16 <= len(idempotency_key) <= 128:
            raise RescoreRejected("IDEMPOTENCY_KEY_INVALID", 400)
        expected = None
        if if_match is not None:
            if not isinstance(if_match, str) or not re.fullmatch(r'"[1-9][0-9]*"', if_match):
                raise RescoreRejected("INVALID_IF_MATCH", 400)
            expected = int(if_match[1:-1])
        captured = self.store.capture(actor, run_id, body, idempotency_key, expected)
        if "response" in captured:
            return captured["response"]
        target = RulePackSnapshot(**captured["target_pack"])
        # Real (non-synthetic) runs may be rescored only into an active pack with a
        # recorded approver; synthetic runs keep the original local-only behaviour.
        target_approved = bool(
            target.status == "active" and target.approved_by and target.approved_at
        )
        prepared = {}
        for claim_id, current in captured["claims"].items():
            raw = current["tag"].get("confirmed_tags")
            if raw is None:
                raise RescoreRejected("RETAG_REQUIRED")
            try:
                inputs = self.load_inputs(actor.tenant_id, run_id, claim_id)
                inputs.validate()
                snapshot_hash = canonical_hash(inputs.snapshot())
                tag = current["tag"]
                pinned_hash = (
                    canonical_hash(tag["inputs"])
                    if "inputs" in tag
                    else tag.get("input_snapshot_sha256")
                )
                if (
                    snapshot_hash != pinned_hash
                    or tag.get("fact_assembly") != inputs.snapshot().get("fact_assembly")
                    or inputs.run_id != run_id
                    or inputs.context.claim.claim_id != claim_id
                    or inputs.rulepack.sha256 != captured["run_snapshot"]["rulepack"]["sha256"]
                    or inputs.original.document_version_id != captured["run"]["document_version_id"]
                ):
                    raise RescoreRejected("RESCORE_INPUT_MISMATCH")
                if not inputs.rule_context.local_synthetic and not target_approved:
                    raise RescoreRejected("RULEPACK_APPROVAL_REQUIRED")
                tags = ConfirmedTags(
                    **(
                        raw
                        | {
                            "facts": tuple(
                                ConfirmedFact(
                                    **(
                                        fact
                                        | {
                                            "evidence_refs": tuple(
                                                _source_ref_from_dict(ref)
                                                for ref in fact["evidence_refs"]
                                            )
                                        }
                                    )
                                )
                                for fact in raw["facts"]
                            )
                        }
                    )
                )
                first = inputs.tag_runs[0]
                if (
                    tags.tag_revision != current["head"]["tag_revision"]
                    or tags.model_sha256 != first.model_sha256
                    or tags.prompt_sha256 != first.prompt_sha256
                    or tags.replicate_hashes != inputs.consensus.replicate_hashes
                    or tags.product_variant != first.product_variant
                    # A review revision may correct the track (original §4.2); an
                    # unreviewed tag must still match its original packet.
                    or (
                        tags.track != inputs.packet.to_dict()["track"]
                        and tag.get("origin") not in REVIEWED_TAG_ORIGINS
                    )
                ):
                    raise RescoreRejected("RESCORE_INPUT_MISMATCH")
                report_graphs = {}
                seen_report_elements = set()
                for receipt in tag.get("report_level_review", ()):
                    element_id = receipt["element_id"]
                    policy = receipt["policy"]
                    element = next(
                        (e for e in tag["elements"] if e["element_id"] == element_id), None
                    )

                    if (
                        self.verify_context_sources is None
                        or element_id in seen_report_elements
                        or REPORT_LEVEL_POLICIES.get(element_id) != policy
                        or element is None
                        or element["state"] != "present"
                        or element["reason_code"] != policy
                        or element["credited_from"] != receipt["credited_from"]
                    ):
                        raise RescoreRejected("RESCORE_SOURCE_REJECTED")
                    refs = tuple(_source_ref_from_dict(ref) for ref in receipt["refs"])
                    if not 1 <= len(refs) <= 6:
                        raise RescoreRejected("RESCORE_SOURCE_REJECTED")
                    verified_refs = tuple(
                        replace(ref, verification_state="verified") for ref in refs
                    )
                    if (
                        tuple(_source_ref_from_dict(ref) for ref in element["evidence_refs"])
                        != verified_refs
                    ):
                        raise RescoreRejected("RESCORE_SOURCE_REJECTED")
                    seen_report_elements.add(element_id)
                    try:
                        replay = (
                            {"replay_receipt": receipt["source_receipt"]}
                            if "replay_receipt" in signature(self.verify_context_sources).parameters
                            else {}
                        )
                        graph, replayed = self.verify_context_sources(inputs, refs, **replay)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise RescoreRejected("RESCORE_SOURCE_REJECTED") from exc
                    if canonical_hash(replayed) != canonical_hash(receipt["source_receipt"]):
                        raise RescoreRejected("RESCORE_SOURCE_REJECTED")
                    for name in MAPPINGS[tags.track][element_id]:
                        fact = next((f for f in tags.facts if f.name == name), None)
                        if (
                            fact is None
                            or fact.state != "present"
                            or fact.source_scope != "global_bound"
                            or fact.evidence_refs != verified_refs
                        ):
                            raise RescoreRejected("RESCORE_SOURCE_REJECTED")
                        report_graphs[name] = graph
                for fact in tags.facts:
                    for ref in fact.evidence_refs:
                        verified = verify_source_ref(
                            ref,
                            report_graphs.get(fact.name, inputs.original),
                            tenant_id=actor.tenant_id,
                        )
                        if verified != ref or verified.verification_state != "verified":
                            raise RescoreRejected("RESCORE_SOURCE_REJECTED")
                old_decision = current["decision"]
                decision = create_rescore(
                    target,
                    tags,
                    previous_pack=inputs.rulepack,
                    context=replace(
                        inputs.rule_context,
                        decision_revision=current["head"]["decision_revision"] + 1,
                    ),
                    previous_decision=Decision(**old_decision["decision"])
                    if old_decision
                    else None,
                )
            except (KeyError, ValueError, TypeError) as error:
                raise RescoreRejected("RESCORE_INPUT_UNAVAILABLE") from error
            if isinstance(decision, RetagRequired):
                raise RescoreRejected(decision.code)
            if (
                inputs.fact_assembly_profile == PARTIAL_FACTS_V1
                and tag["origin"] not in REVIEWED_TAG_ORIGINS
                and decision.decision_status == "decided"
            ):
                raise RescoreRejected("HUMAN_REVIEW_REQUIRED")
            api = decision.to_api_dict()
            if tag["origin"] == "human":
                api["review_status"] = "human_confirmed"
            elif tag["origin"] == "ai_delegated":
                # Preserve the honest AI-delegated label across rescores; never
                # promote machine-driven work to a human confirmation.
                api["review_status"] = "ai_delegated_confirmed"
            prepared[claim_id] = dict(
                decision_revision=decision.decision_revision,
                decision=asdict(decision),
                api=api,
                input_snapshot_sha256=snapshot_hash,
                tag_record_sha256=canonical_hash(tag),
                **({"fact_assembly": tag["fact_assembly"]} if "fact_assembly" in tag else {}),
            )
        try:
            return self.store.commit(actor, run_id, body, captured, prepared)
        except AuditConflict:
            raise RescoreRejected("RESCORE_CONFLICT") from None
