"""Pure pinned-pack evaluator for the explicit general ladders.

Confirmed facts are an internal, trusted citation/binding boundary, never raw LLM
output. Source checks here verify the retained attestations; upstream citation
and binding services must produce them after checking original evidence.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from itertools import product
from typing import Any

from proofops.domain.errors import DomainValidationError
from proofops.domain.rulepacks import RulePackSnapshot, canonical_json
from proofops.domain.rules import goal, management, performance
from proofops.domain.rules.exceptions import apply_source_exceptions
from proofops.domain.values import (
    ELEMENT_STATES,
    GRADE_LABEL_MAP,
    SAFE_HARBOR_CATEGORIES,
    TRACKS,
    ElementState,
    SourceRef,
    Track,
    _require_sha256,
    _require_strict_int,
    _require_uuid,
)

ENGINE_VERSION = "explicit-ladders-exceptions-3"
# User decision 2026-09-25 resolving GAP-003: the §4.4 ladder alone sets the
# grade; §4.5 additional elements stay in missing/unresolved and review status.
ADDITIONAL_REPORT_ONLY = "report_without_grade_effect"
MAPPINGS = {
    "goal": goal.ELEMENTS,
    "performance": performance.ELEMENTS,
    "management": management.ELEMENTS,
}


@dataclass(frozen=True, slots=True)
class ConfirmedFact:
    name: str
    state: ElementState
    evidence_refs: tuple[SourceRef, ...] = ()
    source_tenant_id: str | None = None
    citation_verified: bool = False
    binding_accepted: bool = False
    search_coverage_verified: bool = False
    source_scope: str = "local_claim"
    normalized_value: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name or self.state not in ELEMENT_STATES:
            raise DomainValidationError("invalid confirmed fact")
        if self.normalized_value is not None and not isinstance(self.normalized_value, str):
            raise DomainValidationError("normalized_value must be a string or null")
        if not isinstance(self.source_scope, str):
            raise DomainValidationError("source_scope must be a string")
        refs = tuple(self.evidence_refs)
        if any(not isinstance(ref, SourceRef) for ref in refs):
            raise DomainValidationError("invalid source reference")
        object.__setattr__(self, "evidence_refs", refs)
        for value in (self.citation_verified, self.binding_accepted, self.search_coverage_verified):
            if type(value) is not bool:
                raise DomainValidationError("verification flags must be booleans")
        if self.state == "present" and not (
            refs and self.citation_verified and self.binding_accepted and self.source_tenant_id
        ):
            raise DomainValidationError("present requires verified citation and accepted binding")
        if self.state == "absent" and not self.search_coverage_verified:
            raise DomainValidationError("absent requires verified search coverage")


@dataclass(frozen=True, slots=True)
class ConfirmedTags:
    tenant_id: str
    document_version_id: str
    claim_id: str
    track: Track
    facts: tuple[ConfirmedFact, ...]
    tag_revision: int
    packet_sha256: str
    model_sha256: str
    prompt_sha256: str
    replicate_hashes: tuple[str, ...]
    ontology_version: str
    safe_harbor_category: str | None = None
    superlative_quote: str | None = None
    product_variant: bool = False

    def __post_init__(self) -> None:
        for name in ("tenant_id", "document_version_id", "claim_id"):
            _require_uuid(name, getattr(self, name))
        for name in ("packet_sha256", "model_sha256", "prompt_sha256"):
            _require_sha256(name, getattr(self, name))
        if type(self.product_variant) is not bool:
            raise DomainValidationError("product_variant must be boolean")
        if (
            self.safe_harbor_category is not None
            and self.safe_harbor_category not in SAFE_HARBOR_CATEGORIES
        ):
            raise DomainValidationError("invalid safe harbor category")
        if self.superlative_quote is not None and not isinstance(self.superlative_quote, str):
            raise DomainValidationError("superlative_quote must be string or null")
        if self.track not in TRACKS:
            raise DomainValidationError("invalid track")
        if _require_strict_int("tag_revision", self.tag_revision) < 1:
            raise DomainValidationError("tag_revision must be positive")
        facts = tuple(self.facts)
        if any(not isinstance(f, ConfirmedFact) for f in facts):
            raise DomainValidationError("expected confirmed facts, not LLM tags")
        if len({f.name for f in facts}) != len(facts):
            raise DomainValidationError("duplicate fact names")
        object.__setattr__(self, "facts", tuple(sorted(facts, key=lambda f: f.name)))
        hashes = tuple(self.replicate_hashes)
        if len(hashes) != 3:
            raise DomainValidationError("retain hashes for replicate 1, 2, 3 in order")
        for value in hashes:
            _require_sha256("replicate hash", value)
        object.__setattr__(self, "replicate_hashes", hashes)


@dataclass(frozen=True, slots=True)
class RuleContext:
    tenant_id: str
    document_version_id: str
    claim_id: str
    packet_sha256: str
    decision_revision: int = 1
    mode: str = "disclosure"
    local_synthetic: bool = False
    company_id: str | None = None
    reporting_period: str | None = None
    industry: str | None = None
    regulatory_axis: str = "advisory_unverified"

    def __post_init__(self) -> None:
        for name in ("tenant_id", "document_version_id", "claim_id"):
            _require_uuid(name, getattr(self, name))
        _require_sha256("packet_sha256", self.packet_sha256)
        if _require_strict_int("decision_revision", self.decision_revision) < 1:
            raise DomainValidationError("decision_revision must be positive")
        if type(self.local_synthetic) is not bool:
            raise DomainValidationError("local_synthetic must be boolean")


@dataclass(frozen=True, slots=True)
class Decision:
    decision_revision: int
    tag_revision: int
    decision_status: str
    evidence_grade: str | None
    label: str | None
    sublabel: str | None
    review_status: str
    missing_elements: tuple[str, ...]
    rule_ids: tuple[str, ...]
    rule_pack_sha256: str
    semantic_hash: str
    gap_ids: tuple[str, ...]
    unresolved_elements: tuple[str, ...]
    excluded_elements: tuple[str, ...]
    ladder_candidate: str | None
    basis_refs: tuple[str, ...]
    input_tags_sha256: str
    local_synthetic: bool
    # Reachable ladder grades while evidence is unresolved; never a grade or label.
    # Defaults keep decisions stored before engine v3 loadable.
    grade_floor: str | None = None
    grade_ceiling: str | None = None
    grade_open_elements: tuple[str, ...] = ()

    def to_api_dict(self) -> dict[str, Any]:
        """Explicit v1 projection; internal candidate/provenance stay in the revision."""
        grade_range = (
            dict(
                floor=self.grade_floor,
                ceiling=self.grade_ceiling,
                open_elements=list(self.grade_open_elements),
            )
            if self.grade_floor is not None
            else None
        )
        fields = (
            "decision_revision",
            "tag_revision",
            "decision_status",
            "evidence_grade",
            "label",
            "sublabel",
            "review_status",
            "missing_elements",
            "rule_ids",
            "rule_pack_sha256",
            "semantic_hash",
            "gap_ids",
        )
        return {
            key: list(value) if isinstance(value := getattr(self, key), tuple) else value
            for key in fields
        } | {"grade_range": grade_range}


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("ascii")).hexdigest()


def _select(branches: list[dict[str, Any]], facts: dict[str, bool]) -> tuple[str | None, str]:
    # The no-year exception is derived, never accepted as a caller-supplied override.
    facts = {
        **facts,
        "exception_applies": facts.get("target_metric", False)
        and facts.get("transition_plan", False),
    }
    matches = []
    for branch in branches:
        conditions = branch.get("when", {name: True for name in branch.get("require_all", [])})
        if all(facts[name] == value for name, value in conditions.items()):
            if branch.get("cap"):
                return branch["grade"], branch["id"]
            matches.append((branch["grade"], branch["id"]))
    return max(matches) if matches else (None, "")


def _validate_inputs(tags: ConfirmedTags, context: RuleContext, pack: RulePackSnapshot) -> None:
    """Shared trust boundary for general ladders and standalone exceptions."""
    if not isinstance(tags, ConfirmedTags) or not isinstance(context, RuleContext):
        raise DomainValidationError("expected ConfirmedTags and RuleContext")
    for name in ("tenant_id", "document_version_id", "claim_id", "packet_sha256"):
        if getattr(tags, name) != getattr(context, name):
            raise DomainValidationError("input identity mismatch")
    if pack.tenant_id != context.tenant_id or tags.ontology_version != pack.ontology_version:
        raise DomainValidationError("rulepack identity mismatch")
    if context.mode != "disclosure" or pack.mode != context.mode:
        raise DomainValidationError("dedicated advertising evaluator required")
    if context.regulatory_axis != "advisory_unverified":
        raise DomainValidationError("legal applicability approval not implemented")
    if pack.status not in ("draft", "validated", "active"):
        raise DomainValidationError("pack is not available for evaluation")
    if not context.local_synthetic and not (
        pack.status == "active" and pack.approved_by and pack.approved_at
    ):
        raise DomainValidationError("draft pack requires explicit local synthetic use")

    elements = MAPPINGS[tags.track]
    definitions = {e["id"]: e for e in pack.file_content("rubric/elements.yaml")["elements"]}
    allowed = {
        name: definitions[e]["source_scopes"] for e, names in elements.items() for name in names
    }
    facts = {f.name: f for f in tags.facts}
    for fact in facts.values():
        if fact.state != "present":
            continue
        if fact.source_tenant_id != tags.tenant_id:
            raise DomainValidationError("source identity mismatch")
        for ref in fact.evidence_refs:
            if (
                ref.document_version_id != tags.document_version_id
                or ref.verification_state != "verified"
                or ref.location_quality != "located"
                or not ref.quote.strip()
                or ref.char_end <= ref.char_start
            ):
                raise DomainValidationError(
                    "source is not verified, located same-document evidence"
                )
        scopes = allowed.get(fact.name, ["local_claim", "same_table"])
        if fact.name == "external_verification":
            scopes = ["local_claim", "global_bound"]
        # Partly unresolved explicit_link scopes are not automatically expanded (GAP-004).
        if fact.source_scope not in scopes or fact.source_scope == "explicit_link":
            raise DomainValidationError("source scope is not approved for this fact")
        if fact.name == "assurance_covered" and fact.normalized_value != "covered":
            raise DomainValidationError("assurance requires covered status, not a provider name")


def evaluate(tags: ConfirmedTags, context: RuleContext, pack: RulePackSnapshot) -> Decision:
    """Evaluate explicit source branches; never infer absent from unresolved evidence."""
    effect = apply_source_exceptions(tags, context, pack)
    elements = MAPPINGS[tags.track]
    definitions = {e["id"]: e for e in pack.file_content("rubric/elements.yaml")["elements"]}
    facts = {f.name: f for f in tags.facts}

    def state(name: str) -> str:
        if effect.ordinal_state is not None and name in performance.ELEMENTS["P1"]:
            return effect.ordinal_state
        return facts[name].state if name in facts else "unknown"

    missing, unresolved, excluded, additional = [], [], [], []
    bases = []
    for element, names in elements.items():
        definition = definitions[element]
        bases.append(canonical_json({"element_id": element, **definition["basis"]}))
        trigger = definition.get("trigger")
        if trigger and state(trigger) == "absent":
            excluded.append(element)
            continue
        states = [state(name) for name in names]
        if (trigger and state(trigger) != "present") or any(
            s not in ("present", "absent") for s in states
        ):
            unresolved.append(element)
        elif "absent" in states:
            missing.append(element)
        if element in ("G7", "G8", "P5", "P6", "M4", "M5", "M6") and (
            element in missing or element in unresolved
        ):
            additional.append(element)

    rubric = pack.file_content(f"rubric/{tags.track}.yaml")
    # Packs without a policy keep the frozen impl1 GAP-003 blocking behaviour.
    additional_policy = rubric.get("additional_element_policy")
    if additional_policy not in (None, ADDITIONAL_REPORT_ONLY):
        raise DomainValidationError("unsupported additional element policy")
    branches = rubric["branches"]
    names = {
        name
        for branch in branches
        for name in (branch.get("when", {}).keys() | set(branch.get("require_all", [])))
    }
    names.discard("exception_applies")
    if tags.track == "goal":
        names.update(("target_metric", "transition_plan"))
    known = {
        name: state(name) == "present" for name in names if state(name) in ("present", "absent")
    }
    unknown = sorted(names - known.keys())
    # ponytail: at most 8 primitive ladder facts; use symbolic evaluation if ontology expands.
    if len(names) > 12:
        raise DomainValidationError("unsupported ladder ontology size")
    outcomes = {
        _select(branches, {**known, **dict(zip(unknown, values, strict=True))})
        for values in product((False, True), repeat=len(unknown))
    }
    grades = {grade for grade, _ in outcomes}
    candidate = next(iter(grades)) if len(grades) == 1 else None
    if effect.cap_grade:
        grades = {min(grade, effect.cap_grade) if grade else None for grade in grades}
    grade_candidate = next(iter(grades)) if len(grades) == 1 else None
    status = "decided"
    gaps = []
    if len(grades) > 1:
        status = "blocked_evidence"
    elif grade_candidate is None:
        status = "blocked_rule_gap"
        gaps.append("GAP-007")
    if additional and additional_policy is None:
        status = (
            "blocked_evidence" if any(e in unresolved for e in additional) else "blocked_rule_gap"
        )
        gaps.append("GAP-003")
    if effect.override_grade:
        # Original §4.4 explicitly places the superlative override before all ladders.
        status = "decided"
        gaps = []
    if effect.unresolved_elements:
        status = "blocked_evidence"
        unresolved.extend(effect.unresolved_elements)
    if effect.gap_ids:
        status = "blocked_rule_gap"
        gaps.extend(effect.gap_ids)
    grade = (effect.override_grade or grade_candidate) if status == "decided" else None
    # Only ladder unknowns may widen the range: separate task paths, rule gaps and the
    # legacy GAP-003 block keep their candidate private.
    has_range = (
        status == "blocked_evidence"
        and len(grades) > 1
        and None not in grades
        and not (additional and additional_policy is None)
        and not effect.unresolved_elements
        and not effect.gap_ids
    )
    reachable = {grade for grade in grades if grade is not None}
    ladder_elements = {
        element for element, fact_names in elements.items() if set(fact_names) & names
    }
    bases.append(
        canonical_json(
            {
                "source_section": "4.4",
                "clause": None,
                "verification_status": "unverified",
                "rule_ids": effect.rule_ids,
            }
        )
    )
    sublabel = None
    # Only the exact original §7 example is mapped; do not generalize to other gaps.
    if (
        tags.track == "goal"
        and grade == "E1"
        and set(missing) == {"G3", "G4", "G5", "G6"}
        and all(state(name) == "absent" for element in missing for name in elements[element])
        and not unresolved
        and set(excluded) == {"G7", "G8"}
    ):
        sublabel = "IMPL"
    elif grade in ("E1", "E2"):
        gaps.append("GAP-005")
    decision = Decision(
        context.decision_revision,
        tags.tag_revision,
        status,
        grade,
        GRADE_LABEL_MAP[grade] if grade else None,
        sublabel,
        "auto_confirmed" if status == "decided" and not unresolved else "needs_review",
        tuple(missing),
        tuple(sorted({rule for _, rule in outcomes if rule} | set(effect.rule_ids))),
        pack.sha256,
        "",
        tuple(sorted(set(gaps))),
        tuple(unresolved),
        tuple(excluded),
        candidate,
        tuple(bases),
        _hash(asdict(tags)),
        context.local_synthetic,
        min(reachable) if has_range else None,
        max(reachable) if has_range else None,
        tuple(e for e in unresolved if e in ladder_elements) if has_range else (),
    )
    return replace(
        decision,
        semantic_hash=_hash(
            {
                "engine_version": ENGINE_VERSION,
                "tags": asdict(tags),
                "context": asdict(context),
                "pack_sha256": pack.sha256,
                "decision": asdict(decision),
            }
        ),
    )
