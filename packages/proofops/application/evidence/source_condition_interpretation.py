"""Bounded literal interpretation; no graph promotion, coverage or numeric outcomes."""

from dataclasses import replace

from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.domain.errors import DomainValidationError
from proofops.domain.numeric import scope_note_literal, unit_note_literal
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef, _require_sha256, _require_uuid


def validate_source_condition_interpretation(
    original: CanonicalDocumentGraph,
    inventory: dict[str, dict],
    effective_state: dict,
    entry: dict,
    *,
    tenant_id: str,
) -> dict:
    """Assess a proposal against authorized original inventory and current server facts.

    Ownership values contain proposal/assessment; conditions may also be flattened
    proposals while the adapter reevaluates a complete revision. Source-view replay
    and current ownership assessment belong to the caller. Never mutate either input.
    """
    _require_uuid("tenant_id", tenant_id)
    if not isinstance(original, CanonicalDocumentGraph) or original.tenant_id != tenant_id:
        raise DomainValidationError("original canonical snapshot and matching tenant required")
    if (
        not isinstance(entry, dict)
        or set(entry) != {"id", "fragment_id", "ownership_id", "kind", "state", "value_refs"}
        or not isinstance(entry["kind"], str)
        or entry["kind"] not in {"unit_literal", "scope_literal", "unsupported_prose"}
        or not isinstance(entry["state"], str)
        or entry["state"] not in {"tagged", "unknown", "conflict", "unsupported"}
        or not isinstance(entry["value_refs"], list)
        or len(entry["value_refs"]) > 32
    ):
        raise DomainValidationError("invalid condition entry")
    for key in ("id", "fragment_id", "ownership_id"):
        _require_sha256(key, entry[key])
    if entry["fragment_id"] not in inventory:
        raise DomainValidationError("fragment not in original inventory")
    if entry["state"] != "tagged" and entry["value_refs"]:
        raise DomainValidationError("unresolved condition requires empty value refs")
    if (
        entry["state"] == "tagged"
        and entry["kind"] != "unsupported_prose"
        and not entry["value_refs"]
    ):
        raise DomainValidationError("tagged literal requires value refs")
    refs = []
    for value in entry["value_refs"]:
        try:
            ref = SourceRef(**value) if isinstance(value, dict) else value
            refs.append(verify_source_ref(ref, original, tenant_id=tenant_id))
        except (TypeError, ValueError) as exc:
            raise DomainValidationError("invalid condition source ref") from exc
    result: dict = dict(id=entry["id"], state="unknown", value=None, reasons=[])
    if entry["state"] != "tagged":
        return dict(result, state=entry["state"], reasons=["condition_" + entry["state"]])
    reasons = result["reasons"]
    fragment = inventory[entry["fragment_id"]]
    if "source_id" not in fragment:
        return dict(result, reasons=["native_condition_proof_unavailable"])
    if canonical_hash(fragment) != entry["fragment_id"]:
        return dict(result, reasons=["fragment_identity_mismatch"])
    blocks = {b.source_id: b for b in original.blocks}
    if len(blocks) != len(original.blocks):
        raise DomainValidationError("duplicate original source identity")
    note = blocks.get(fragment["source_id"])
    if note is None or type(note.winner) is not int or not 0 <= note.winner < len(note.candidates):
        return dict(result, reasons=["fragment_selection_unresolved"])
    if note.kind != "footnote":
        return dict(result, reasons=["canonical_note_kind_unresolved"])
    source = note.candidates[note.winner].source
    if (source.parser_run_id, source.source_native_id) != (
        fragment.get("parser_run_id"),
        fragment.get("source_native_id"),
    ):
        reasons.append("fragment_selection_mismatch")
    start, end = fragment.get("char_start"), fragment.get("char_end")
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start < end <= len(source.raw_text)
    ):
        return dict(result, reasons=[*reasons, "fragment_offsets_invalid"])
    text = source.raw_text[start:end]
    whole_ref = replace(note.source_ref(), char_start=start, char_end=end, quote=text)
    if (
        whole_ref.raw_text_sha256 != fragment.get("raw_text_sha256")
        or verify_source_ref(whole_ref, original, tenant_id=tenant_id).verification_state
        != "verified"
    ):
        reasons.append("original_fragment_evidence_unverified")
    for category, expected in (("citations", "confirmed"), ("classifications", "note")):
        fact = effective_state.get(category, {}).get(entry["fragment_id"], {})
        if fact.get("state") == "conflict":
            result["state"] = "conflict"
        if fact.get("state") != expected or fact.get("fragment") != fragment:
            reasons.append(category + "_unconfirmed")
    ownerships = effective_state.get("ownership", {})
    owner = ownerships.get(entry["ownership_id"], {})
    proposal, assessment = owner.get("proposal", {}), owner.get("assessment", {})
    if proposal.get("state") == "conflict" or assessment.get("state") == "conflict":
        result["state"] = "conflict"
    if (
        proposal.get("id") != entry["ownership_id"]
        or proposal.get("fragment_id") != entry["fragment_id"]
        or proposal.get("state") != "linked"
        or assessment.get("id") != entry["ownership_id"]
        or assessment.get("state") != "accepted"
    ):
        reasons.append("ownership_unaccepted")
    if reasons:
        return result
    if entry["kind"] == "unsupported_prose":
        return dict(result, state="unsupported", reasons=["unsupported_condition_syntax"])
    parse_literal = unit_note_literal if entry["kind"] == "unit_literal" else scope_note_literal
    literal = parse_literal(text)
    if literal is None:
        return dict(result, state="unsupported", reasons=["unsupported_condition_syntax"])
    if any(
        ref.verification_state != "verified"
        or ref.source_id != note.source_id
        or ref.raw_text_sha256 != fragment["raw_text_sha256"]
        or not start <= ref.char_start < ref.char_end <= end
        or ref.quote != literal
        for ref in refs
    ):
        return dict(result, reasons=["original_literal_evidence_unverified"])
    targets = {target["source_id"] for target in proposal.get("targets", [])}
    for record in effective_state.get("conditions", {}).values():
        other = record.get("proposal", record)
        if other.get("id") == entry["id"]:
            continue
        other_owner = ownerships.get(other.get("ownership_id"), {}).get("proposal", {})
        shared_target = targets & {t["source_id"] for t in other_owner.get("targets", [])}
        same_fragment = other.get("fragment_id") == entry["fragment_id"]
        if not same_fragment and not (other.get("kind") == entry["kind"] and shared_target):
            continue
        competing = other.get("state") == "conflict"
        if other.get("state") == "tagged":
            # Read the complete other fragment, not a supplied normalized value or
            # first processed assessment: reevaluation must be order independent.
            other_fragment = inventory.get(other.get("fragment_id"), {})
            other_note = blocks.get(other_fragment.get("source_id"))
            other_literal = None
            if (
                other_note is not None
                and type(other_note.winner) is int
                and 0 <= other_note.winner < len(other_note.candidates)
            ):
                a, b = other_fragment.get("char_start"), other_fragment.get("char_end")
                if type(a) is int and type(b) is int and 0 <= a < b <= len(other_note.raw_text):
                    other_literal = parse_literal(other_note.raw_text[a:b])
            competing |= other.get("kind") != entry["kind"] or other_literal != literal
        if competing:
            return dict(result, state="conflict", reasons=["competing_conditions"])
    return dict(result, state="accepted", value=literal)
