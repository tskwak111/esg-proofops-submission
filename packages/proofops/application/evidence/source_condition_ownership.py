"""Factual ownership only; original graph, inputs and review revisions are unchanged."""

from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.domain.errors import DomainValidationError
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef, _require_sha256, _require_uuid


def validate_source_condition_ownership(
    original: CanonicalDocumentGraph,
    inventory: dict[str, dict],
    effective_state: dict,
    entry: dict,
    *,
    tenant_id: str,
    native_proof: dict | None = None,
) -> dict:
    """Return separate {id, state, reasons}; malformed targets raise DomainValidationError.

    Caller supplies the authorized pinned internal graph, inventory mapping fragment IDs
    to fragment tuples, and server-validated effective citation/classification/ownership
    maps (not client permission flags). Source-view receipt replay belongs to the caller.
    native_proof is internal adapter output replayed from original bytes, never a client
    field or model claim. Native acceptance binds only the marked leaf, not note coverage.
    Acceptance requires original verified refs for the exact note interval, every target
    and its table. No source quality promotion, I/O, numeric output or state mutation.
    """
    _require_uuid("tenant_id", tenant_id)
    if not isinstance(original, CanonicalDocumentGraph) or original.tenant_id != tenant_id:
        raise DomainValidationError("original canonical snapshot and matching tenant required")
    if (
        not isinstance(entry, dict)
        or set(entry) != {"id", "fragment_id", "targets", "state", "evidence_refs"}
        or not isinstance(entry["id"], str)
        or not entry["id"]
        or not isinstance(entry["fragment_id"], str)
        or not isinstance(entry["state"], str)
        or entry["state"] not in {"linked", "unknown", "conflict"}
        or not isinstance(entry["targets"], list)
        or not isinstance(entry["evidence_refs"], list)
        or len(entry["targets"]) > 16
        or len(entry["evidence_refs"]) > 32
    ):
        raise DomainValidationError("invalid ownership entry")
    _require_sha256("ownership id", entry["id"])
    _require_sha256("fragment_id", entry["fragment_id"])
    if entry["fragment_id"] not in inventory:
        raise DomainValidationError("fragment not in original inventory")
    result: dict = dict(id=entry["id"], state="unknown", reasons=[])
    reasons = result["reasons"]
    blocks = {b.source_id: b for b in original.blocks}
    if len(blocks) != len(original.blocks):
        raise DomainValidationError("duplicate original source identity")
    required: set[str] = set()
    target_ids = set()
    for target in entry["targets"]:
        if not isinstance(target, dict) or set(target) != {
            "source_id",
            "table_id",
            "row",
            "column",
            "row_span",
            "column_span",
        }:
            raise DomainValidationError("invalid exact target")
        for key in ("source_id", "table_id"):
            _require_uuid(key, target[key])
        for key in ("row", "column", "row_span", "column_span"):
            value = target[key]
            if value is not None and (
                type(value) is not int or value < (1 if "span" in key else 0)
            ):
                raise DomainValidationError("invalid target coordinate")
        block, table = blocks.get(target["source_id"]), blocks.get(target["table_id"])
        if (
            block is None
            or table is None
            or table.kind != "table"
            or block.kind not in {"table", "table_row", "table_cell", "heading"}
            or block.source_id in target_ids
        ):
            raise DomainValidationError("foreign, duplicate or non-table target")
        target_ids.add(block.source_id)
        required.update((block.source_id, table.source_id))
        if type(block.winner) is not int or not 0 <= block.winner < len(block.candidates):
            reasons.append("target_selection_unresolved")
            continue
        candidate = block.candidates[block.winner]
        if tuple(target[k] for k in ("row", "column", "row_span", "column_span")) != (
            candidate.row_number,
            candidate.column_number,
            candidate.row_span,
            candidate.column_span,
        ):
            raise DomainValidationError("target coordinates do not match selected original")
        if block.kind == "table":
            if block != table:
                raise DomainValidationError("table target identity mismatch")
            continue
        aliases = {
            b.source_id
            for b in original.blocks
            for c in b.candidates
            if c.source.parser_run_id == candidate.source.parser_run_id
            and c.source.source_native_id == candidate.table_native_id
        }
        if aliases != {table.source_id}:
            raise DomainValidationError("target table lineage mismatch")
        # Follow retained row/table parents; proximity never establishes an ancestor.
        ancestors, pending = set(), [block.source_id]
        while pending:
            current = pending.pop()
            for edge in original.edges:
                if edge.source_id == current and edge.relation == "table_parent":
                    if edge.target_id not in ancestors:
                        ancestors.add(edge.target_id)
                        pending.append(edge.target_id)
        if table.source_id not in ancestors or any(
            parent not in blocks
            or blocks[parent].kind not in {"table", "table_row"}
            or (blocks[parent].kind == "table" and parent != table.source_id)
            for parent in ancestors
        ):
            reasons.append("target_table_lineage_unresolved")
        required.update(ancestors)
    refs = []
    for value in entry["evidence_refs"]:
        try:
            ref = SourceRef(**value) if isinstance(value, dict) else value
            refs.append(verify_source_ref(ref, original, tenant_id=tenant_id))
        except (TypeError, ValueError) as exc:
            raise DomainValidationError("invalid ownership source ref") from exc
    if entry["state"] != "linked":
        return dict(id=entry["id"], state=entry["state"], reasons=["ownership_" + entry["state"]])
    fragment = inventory.get(entry["fragment_id"])
    if fragment is None:
        reasons.append("fragment_not_in_inventory")
        return result
    if "source_id" not in fragment:
        if native_proof is None:
            reasons.append("native_ownership_proof_unavailable")
            return result
        expected_proof = dict(
            schema="native_note_marker_v1",
            tenant_id=tenant_id,
            document_version_id=original.document_version_id,
            parse_manifest_id=original.parse_manifest_id,
            source_sha256=original.source_sha256,
            fragment_id=entry["fragment_id"],
            target_source_ids=sorted(target_ids),
            note_word_indices=fragment.get("native_word_indices"),
        )
        if (
            not isinstance(native_proof, dict)
            or len(target_ids) != 1
            or any(blocks[sid].kind != "table_cell" for sid in target_ids)
            or canonical_hash(fragment) != entry["fragment_id"]
            or any(native_proof.get(k) != v for k, v in expected_proof.items())
            or native_proof.get("proof_sha256")
            != canonical_hash({k: v for k, v in native_proof.items() if k != "proof_sha256"})
            or any(
                type(native_proof.get(k)) is not int or native_proof[k] < 0
                for k in ("base_word_index", "marker_word_index")
            )
            or native_proof["base_word_index"] == native_proof["marker_word_index"]
            or any(
                native_proof[k] in fragment.get("native_word_indices", [])
                for k in ("base_word_index", "marker_word_index")
            )
        ):
            reasons.append("native_ownership_proof_mismatch")
            return result
        for category, expected_state in (("citations", "confirmed"), ("classifications", "note")):
            fact = effective_state.get(category, {}).get(entry["fragment_id"], {})
            if fact.get("state") == "conflict":
                result["state"] = "conflict"
            if fact.get("state") != expected_state or fact.get("fragment") != fragment:
                reasons.append(category + "_unconfirmed")
        if any(
            other.get("fragment_id") == entry["fragment_id"]
            and (other.get("state") == "conflict" or other.get("targets") != entry["targets"])
            for other in effective_state.get("ownership", {}).values()
        ):
            result["state"] = "conflict"
            reasons.append("competing_ownership")
        verified = [ref for ref in refs if ref.verification_state == "verified"]
        if len(verified) != len(refs) or not required <= {r.source_id for r in verified}:
            reasons.append("original_target_evidence_unverified")
        if not reasons:
            result.update(state="accepted", native_proof_sha256=native_proof["proof_sha256"])
        return result
    if canonical_hash(fragment) != entry["fragment_id"]:
        reasons.append("fragment_identity_mismatch")
        return result
    note = blocks.get(fragment["source_id"])
    if note is None or type(note.winner) is not int or not 0 <= note.winner < len(note.candidates):
        reasons.append("fragment_selection_unresolved")
        return result
    if note.kind != "footnote":
        reasons.append("canonical_note_kind_unresolved")
        return result
    source = note.candidates[note.winner].source
    if (source.parser_run_id, source.source_native_id) != (
        fragment.get("parser_run_id"),
        fragment.get("source_native_id"),
    ):
        reasons.append("fragment_selection_mismatch")
    for category, expected in (("citations", "confirmed"), ("classifications", "note")):
        fact = effective_state.get(category, {}).get(entry["fragment_id"], {})
        if fact.get("state") == "conflict":
            result["state"] = "conflict"
        if fact.get("state") != expected or fact.get("fragment") != fragment:
            reasons.append(category + "_unconfirmed")
    linked = {
        e.target_id
        for e in original.edges
        if e.source_id == note.source_id and e.relation == "footnote_of"
    }
    if not target_ids or not target_ids <= linked:
        reasons.append("explicit_footnote_link_missing")
    if linked - target_ids or any(
        other.get("fragment_id") == entry["fragment_id"]
        and (other.get("state") == "conflict" or other.get("targets") != entry["targets"])
        for other in effective_state.get("ownership", {}).values()
    ):
        result["state"] = "conflict"
        reasons.append("competing_ownership")
    verified = [ref for ref in refs if ref.verification_state == "verified"]
    if len(verified) != len(refs) or not required <= {r.source_id for r in verified}:
        reasons.append("original_target_evidence_unverified")
    if not any(
        r.source_id == note.source_id
        and r.char_start == fragment.get("char_start")
        and r.char_end == fragment.get("char_end")
        and r.raw_text_sha256 == fragment.get("raw_text_sha256")
        for r in verified
    ):
        reasons.append("original_fragment_evidence_unverified")
    if not reasons:
        result["state"] = "accepted"
    return result
