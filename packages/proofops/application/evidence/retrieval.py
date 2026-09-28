"""Same-document retrieval and immutable packets.

The graph must be a trusted tenant-authorized SourceStore snapshot, never a v1
client projection. Search indexes route candidates; original bytes and quality
are rechecked here. No candidate is a present fact or an accepted binding.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
from typing import Any, Protocol

from proofops.application.claims import Claim
from proofops.application.evidence.span_citations import SpanVerifiedGraph, verify_source_ref
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.application.ingest.gri import IndexEntry, _validate_graph
from proofops.application.tagging.tracks import TrackCandidate, validate_track_candidates
from proofops.domain.errors import DomainValidationError
from proofops.domain.numeric import unassigned_note_ids, unresolved_source_issue_ids
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import RulePackSnapshot, canonical_json
from proofops.domain.rules.engine import MAPPINGS
from proofops.domain.values import SourceRef, _require_sha256, _require_uuid


def attested_prose_ids(original, refs):
    if not isinstance(original, SpanVerifiedGraph):
        return set()
    blocks = {b.source_id: b for b in original.blocks}
    return {
        ref.source_id
        for ref in refs
        if ref.source_id in blocks
        and blocks[ref.source_id].kind == "paragraph"
        and blocks[ref.source_id].quality == "unverified"
        and verify_source_ref(ref, original, tenant_id=original.tenant_id).verification_state
        == "verified"
    }


def independently_attested_span_refs(original, source_id, *, tenant_id):
    """Verified external paragraph spans for one unverified atomic prose block.

    Admits only the exact independently replayed spans recorded in the trusted
    SpanVerifiedGraph, never the unverified paragraph remainder. Each stored span
    must re-verify (tenant/document/manifest/geometry/raw-offset) via the shared
    span verifier and point at this block. Deterministic by (char_start, char_end).
    Returns () when the graph is not span-verified or the block stays unverified.
    """
    if not isinstance(original, SpanVerifiedGraph):
        return ()
    block = next((b for b in original.blocks if b.source_id == source_id), None)
    if block is None or block.kind != "paragraph" or block.quality != "unverified":
        return ()
    admitted: list[SourceRef] = []
    seen: set[tuple[int, int]] = set()
    for span in sorted(original.verified_spans, key=lambda s: (s.char_start, s.char_end)):
        if span.source_id != source_id or span.verification_state != "verified":
            continue
        key = (span.char_start, span.char_end)
        if key in seen:
            continue
        checked = verify_source_ref(span, original, tenant_id=tenant_id)
        if checked.verification_state == "verified":
            seen.add(key)
            admitted.append(checked)
    return tuple(admitted)


def evidence_issue_ids(original, source_ids, refs):
    # Only atomic prose skips layout ancestry. Direct issues and table evidence
    # retain the numeric guard; the original graph and its issues stay intact.
    atomic_ids = attested_prose_ids(original, refs)
    scoped = (
        replace(
            original,
            edges=tuple(
                edge
                for edge in original.edges
                if edge.relation != "table_parent" or edge.source_id not in atomic_ids
            ),
        )
        if atomic_ids
        else original
    )
    return unresolved_source_issue_ids(scoped, source_ids)


@dataclass(frozen=True, slots=True)
class SearchScope:
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    index_generation: str

    def __post_init__(self):
        for name in ("tenant_id", "document_version_id", "parse_manifest_id"):
            _require_uuid(name, getattr(self, name))
        if not isinstance(self.index_generation, str) or not self.index_generation.strip():
            raise DomainValidationError("index generation required")


@dataclass(frozen=True, slots=True)
class SearchHit:
    scope: SearchScope
    source_id: str
    raw_text_sha256: str

    def __post_init__(self):
        _require_uuid("source_id", self.source_id)
        _require_sha256("raw_text_sha256", self.raw_text_sha256)


@dataclass(frozen=True, slots=True)
class SearchResult:
    hits: tuple[SearchHit, ...] = ()
    status: str = "bounded"


class EvidenceSearchPort(Protocol):
    synthetic: bool

    def search(
        self, scope: SearchScope, query: str, *, vector: tuple[float, ...] | None = None
    ) -> SearchResult: ...


@dataclass(frozen=True, slots=True)
class EvidencePacket:
    """Canonical JSON is the immutable storage boundary; readers get fresh copies."""

    payload_json: str
    packet_sha256: str

    def __post_init__(self):
        if canonical_hash(json.loads(self.payload_json)) != self.packet_sha256:
            raise DomainValidationError("packet hash mismatch")

    def to_dict(self) -> dict[str, Any]:
        return json.loads(self.payload_json) | {"packet_sha256": self.packet_sha256}


def freeze_packet(packet: Mapping[str, Any]) -> EvidencePacket:
    """Freeze an assembled retrieval envelope, without grade/tag/revision writes.

    This is not a trust attestation: downstream tag validation must still verify
    source and binding. Only retrieve_evidence assembles source-checked packets.
    """
    payload = canonical_json(dict(packet))
    return EvidencePacket(payload, canonical_hash(json.loads(payload)))


def freeze_track_packet(
    packet: EvidencePacket, *, track: TrackCandidate, rulepack: RulePackSnapshot
) -> EvidencePacket:
    """Freeze a complete track catalog before any replica, retaining retrieval identity.

    The retrieval packet remains immutable. Revalidating the same selected packet
    is idempotent; selecting another track requires the original retrieval packet.
    This selects requested tags only, never applicability or evidence states.
    """
    data = packet.to_dict()
    claim = track.claim
    if data.get("status") != "candidate":
        # Retrieval already refused this evidence (unverified claim source or an
        # unresolved bundle). A recorded blocked packet stays a review diagnostic
        # and can never be selected for tagging, grading or binding.
        raise DomainValidationError("blocked evidence packet cannot select a track")
    validate_track_candidates(
        [claim],
        [
            dict(
                claim_id=claim.claim_id,
                track=track.track,
                safe_harbor_category=track.safe_harbor_category,
            )
        ],
    )
    expected = dict(
        tenant_id=claim.tenant_id,
        claim_id=claim.claim_id,
        document_version_id=claim.document_version_id,
        parse_manifest_id=claim.parse_manifest_id,
        source_sha256=claim.source_sha256,
        rule_sha256=rulepack.sha256,
    )
    if rulepack.tenant_id != claim.tenant_id or any(
        data.get(key) != value for key, value in expected.items()
    ):
        raise DomainValidationError("track packet identity mismatch")
    catalog = [e["id"] for e in rulepack.file_content("rubric/elements.yaml")["elements"]]
    selected = [element for element in catalog if element in MAPPINGS[track.track]]
    if set(selected) != set(MAPPINGS[track.track]) or len(selected) != len(set(selected)):
        raise DomainValidationError("incomplete rulepack track catalog")
    if "retrieval_packet_sha256" in data:
        _require_sha256("retrieval_packet_sha256", data["retrieval_packet_sha256"])
        if (data.get("track"), data.get("safe_harbor_category"), data["allowed_elements"]) != (
            track.track,
            track.safe_harbor_category,
            selected,
        ):
            raise DomainValidationError("selected track/catalog mismatch")
        return packet
    if data["allowed_elements"] != catalog:
        raise DomainValidationError("complete retrieval catalog required for selection")
    data["retrieval_packet_sha256"] = data.pop("packet_sha256")
    data.update(
        track=track.track,
        safe_harbor_category=track.safe_harbor_category,
        allowed_elements=selected,
    )
    for candidate in data["evidence_candidates"]:
        candidate["allowed_elements"] = [
            element for element in candidate["allowed_elements"] if element in selected
        ]
    return freeze_packet(data)


def retrieve_evidence(
    claim: Claim,
    original: CanonicalDocumentGraph,
    search: EvidenceSearchPort,
    *,
    tenant_id: str,
    run_id: str,
    index_generation: str,
    rulepack: RulePackSnapshot,
    document_context: Mapping[str, str | None],
    token_counter: Callable[[str], int],
    query_vector: tuple[float, ...] | None = None,
    gri_entries: Sequence[IndexEntry] = (),
    indicator_codes: Sequence[str] = (),
    max_tokens: int = 12_000,
) -> EvidencePacket:
    """GRI → section/table → lexical20/vector20 → canonical dedupe/RRF60.

    One bounded global round (at most two permitted by the contract), twelve
    whole snippets, no silent cell/header splitting. A bundle that would exceed
    the bound keeps its own ref and reuses exact context refs already admitted
    verbatim (recorded in coverage) instead of being omitted; byte counts use
    the stored JSON. Composition injects the
    selected model's tokenizer; retrieval never calls an embedding/model API.
    """
    _require_uuid("run_id", run_id)
    _require_uuid("claim_id", claim.claim_id)
    scope = SearchScope(
        tenant_id, claim.document_version_id, claim.parse_manifest_id, index_generation
    )
    try:
        _validate_graph(original, tenant_id)
    except ValueError:
        raise DomainValidationError("source identity mismatch") from None
    if (
        claim.tenant_id,
        claim.document_version_id,
        claim.parse_manifest_id,
        claim.source_sha256,
    ) != (
        tenant_id,
        original.document_version_id,
        original.parse_manifest_id,
        original.source_sha256,
    ) or rulepack.tenant_id != tenant_id:
        raise DomainValidationError("claim/graph/rulepack identity mismatch")
    if type(max_tokens) is not int or not 1 <= max_tokens <= 12_000:
        raise DomainValidationError("token budget must be between 1 and 12000")
    if set(document_context) - {"company", "period", "industry", "boundary"} or any(
        value is not None and not isinstance(value, str) for value in document_context.values()
    ):
        raise DomainValidationError("invalid document context; grades/gold are forbidden")
    blocks = {b.source_id: b for b in original.blocks}
    if len(blocks) != len(original.blocks):
        raise DomainValidationError("duplicate canonical source_id")
    claim_refs = tuple(
        verify_source_ref(ref, original, tenant_id=tenant_id) for ref in claim.source_refs
    )
    if not claim_refs or claim.quote != " ".join(ref.quote for ref in claim_refs):
        raise DomainValidationError("claim quote/source mismatch")
    if any(
        ref.source_id not in blocks
        or (ref.document_version_id, ref.parse_manifest_id)
        != (scope.document_version_id, scope.parse_manifest_id)
        for ref in claim_refs
    ):
        raise DomainValidationError("claim reference identity mismatch")
    claim_ids = tuple(dict.fromkeys(ref.source_id for ref in claim_refs))
    if any(ref.verification_state != "verified" for ref in claim_refs):
        # Preserve actual unresolved quality below; never convert to absent.
        claim_verified = False
    else:
        claim_verified = claim.source_quality == "verified"
    definitions = rulepack.file_content("rubric/elements.yaml")["elements"]
    allowed_elements = tuple(item["id"] for item in definitions)

    parents: dict[str, set[str]] = {}
    sections: dict[str, set[str]] = {}
    for edge in original.edges:
        if edge.source_id not in blocks or edge.target_id not in blocks:
            raise DomainValidationError("dangling source lineage")
        if edge.relation in ("table_parent", "section_parent"):
            mapping = parents if edge.relation == "table_parent" else sections
            mapping.setdefault(edge.source_id, set()).add(edge.target_id)

    def ancestors(source_id: str) -> set[str]:
        visited: set[str] = set()
        pending = list(parents.get(source_id, ()))
        while pending:
            parent = pending.pop()
            if parent == source_id:
                raise DomainValidationError("cyclic table lineage")
            if parent not in visited:
                visited.add(parent)
                pending.extend(parents.get(parent, ()))
        return visited

    table_roots = {sid: {p for p in ancestors(sid) if blocks[p].kind == "table"} for sid in blocks}
    # Attested prose is an atomic source, not proof of a parser-assigned table.
    span_prose_ids = attested_prose_ids(original, claim_refs)
    claim_tables = set().union(
        *(table_roots[sid] for sid in claim_ids if sid not in span_prose_ids)
    )
    claim_sections = set().union(*(sections.get(sid, set()) for sid in claim_ids))

    # ponytail: no semantic header roles exist yet. Retain every preceding row
    # as possible context; deeper rows can still exceed the budget until explicit
    # header associations are available. Never infer that excluded rows are absent.
    row_context_excluded: set[str] = set()
    context_only_ids: set[str] = set()
    if claim_verified and len(claim_ids) == len(claim_tables) == 1:
        target, table_id = claim_ids[0], next(iter(claim_tables))
        members = {sid for sid in blocks if table_id in table_roots[sid]}
        structural = {sid for sid in members if blocks[sid].kind in ("table_row", "table_cell")}
        rows = {sid for sid in structural if blocks[sid].kind == "table_row"}
        cells = structural - rows
        positioned = {
            sid: blocks[sid].candidates[blocks[sid].winner]
            for sid in structural
            if blocks[sid].winner is not None and blocks[sid].quality == "verified"
        }
        if (
            target in rows
            and len(structural) > 12
            and blocks[table_id].quality == "verified"
            and set(positioned) == structural
            and all(type(c.row_number) is int and c.row_number > 0 for c in positioned.values())
            and len({positioned[sid].row_number for sid in rows}) == len(rows)
            and all(parents.get(sid) == {table_id} for sid in rows)
            and all(
                table_roots[sid] == {table_id}
                and len(ancestors(sid) & rows) == 1
                and positioned[next(iter(ancestors(sid) & rows))].row_number
                == positioned[sid].row_number
                and all(
                    type(value) is int and value > 0
                    for value in (
                        positioned[sid].column_number,
                        positioned[sid].column_span,
                        positioned[sid].row_span,
                    )
                )
                for sid in cells
            )
            and all(any(row in ancestors(cell) for cell in cells) for row in rows)
        ):
            last_row = positioned[target].row_number
            context_members = {sid for sid in structural if positioned[sid].row_number <= last_row}
            # Exclude later rows and their descendants. For kept cells, only
            # exact duplicate prose is context-only; distinct nearby prose stays.
            later = structural - context_members
            row_context_excluded = later | {
                sid
                for sid in members - structural
                if ancestors(sid) & later
                or (
                    blocks[sid].kind in ("paragraph", "heading")
                    and any(
                        blocks[sid].raw_text == blocks[cell].raw_text
                        for cell in ancestors(sid) & cells & context_members
                    )
                )
            }
            # The full table ref remains mandatory ancestry context, but is not
            # an independently taggable candidate in this row-scoped packet.
            context_only_ids = {table_id}
    lineage_ids = [
        sid
        for sid in blocks
        if table_roots[sid] & claim_tables or sections.get(sid, set()) & claim_sections
    ]
    gri_pages: set[int] = set()
    gri_unresolved = []
    for entry in gri_entries:
        if entry.indicator_code not in indicator_codes:
            continue
        if (entry.tenant_id, entry.document_version_id, entry.parse_manifest_id) != (
            tenant_id,
            original.document_version_id,
            original.parse_manifest_id,
        ):
            raise DomainValidationError("GRI identity mismatch")
        if (
            entry.resolution_state != "resolved"
            or verify_source_ref(entry.source_ref, original, tenant_id=tenant_id).verification_state
            != "verified"
            or not set(entry.resolved_physical_pages) <= {b.page_num for b in blocks.values()}
        ):
            gri_unresolved.append(entry.indicator_code)
        else:
            gri_pages.update(entry.resolved_physical_pages)
    rankings = [[sid for sid, b in blocks.items() if b.page_num in gri_pages], lineage_ids]
    coverage: dict[str, Any] = dict(
        routes=["gri", "section_table", "lexical", "vector"],
        rounds=1,
        gri_unresolved=gri_unresolved,
        gri_pages=sorted(gri_pages),
        rejected_hit_count=0,
        not_found_state="unknown",
        omitted_source_ids=[],
        unprocessed_source_ids=[],
        deduplicated_source_refs={},
        row_context_excluded_source_ids=sorted(row_context_excluded),
        context_only_source_ids=sorted(context_only_ids),
        source_quality={sid: b.quality for sid, b in blocks.items() if b.quality != "verified"},
        lexical_status="not_run",
        vector_status="not_run",
        token_budget=max_tokens,
        quality_issues=[asdict(issue) for issue in original.issues],
    )
    for route, vector in (("lexical", None), ("vector", query_vector)):
        if route == "vector" and vector is None:
            continue
        try:
            result = search.search(scope, claim.quote, vector=vector)
        except Exception:
            # Provider failures may include document content; record only bounded status.
            coverage[f"{route}_status"] = "failed"
            continue
        coverage[f"{route}_status"] = result.status
        ranking = []
        for hit in result.hits[:20]:
            block = blocks.get(hit.source_id)
            if (
                hit.scope != scope
                or block is None
                or sha256(block.raw_text.encode()).hexdigest() != hit.raw_text_sha256
            ):
                coverage["rejected_hit_count"] += 1
                continue
            if hit.source_id not in ranking:
                ranking.append(hit.source_id)
        rankings.append(ranking)
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, source_id in enumerate(dict.fromkeys(ranking), 1):
            scores[source_id] = scores.get(source_id, 0) + 1 / (60 + rank)
    order = list(claim_ids) + sorted(scores, key=lambda sid: (-scores[sid], sid))
    candidates: list[dict[str, Any]] = []
    used_tokens = 0
    blocked = not claim_verified
    for source_id in dict.fromkeys(order):
        if source_id in row_context_excluded | context_only_ids:
            continue
        block = blocks[source_id]
        source_scope = (
            "local_claim"
            if source_id in claim_ids
            else "same_table"
            if table_roots[source_id] & claim_tables
            else "global_bound"
        )
        elements = [item["id"] for item in definitions if source_scope in item["source_scopes"]]
        # An external hit whose paragraph stays unverified but contains an
        # independently attested span is admitted as that exact span only, as an
        # atomic prose source without parser-assigned table/section ancestry.
        external_spans = (
            independently_attested_span_refs(original, source_id, tenant_id=tenant_id)
            if source_id not in claim_ids
            else ()
        )
        source_refs = [ref for ref in claim_refs if ref.source_id == source_id] or list(
            external_spans
        )
        if unassigned_note_ids(original, source_id) or evidence_issue_ids(
            original, {source_id}, source_refs
        ):
            coverage["unprocessed_source_ids"].append(source_id)
            blocked = blocked or source_scope in ("local_claim", "same_table")
            continue
        # No GAP-004 explicit_link expansion. Parent/header/footnote lineage is
        # context, never proof of subject/period/metric/row binding.
        atomic = source_id in span_prose_ids or bool(external_spans)
        bundle_ids = {source_id} | (set() if atomic else ancestors(source_id))
        bundle_ids.update(
            edge.source_id
            for edge in original.edges
            if edge.relation in ("footnote_of", "caption_of") and edge.target_id in bundle_ids
        )
        if block.kind == "table_cell" and not (
            table_roots[source_id] and any(blocks[sid].kind == "table_row" for sid in bundle_ids)
        ):
            coverage["unprocessed_source_ids"].append(source_id)
            blocked = blocked or source_scope in ("local_claim", "same_table")
            continue
        refs = []
        for sid in [source_id] + sorted(bundle_ids - {source_id}):
            b = blocks[sid]
            selected = (
                [ref for ref in claim_refs if ref.source_id == sid]
                if sid in claim_ids
                else list(external_spans)
                if sid == source_id and external_spans
                else []
            )
            if b.winner is None or (
                b.quality != "verified"
                and not (
                    b.quality == "unverified"
                    and selected
                    and all(ref.verification_state == "verified" for ref in selected)
                )
            ):
                break
            checked = selected or [verify_source_ref(b.source_ref(), original, tenant_id=tenant_id)]
            if any(ref.verification_state != "verified" for ref in checked):
                break
            refs.extend(checked)
        else:
            full_refs = [asdict(ref) for ref in refs]
            candidate = dict(
                source_id=source_id,
                source_scope=source_scope,
                allowed_elements=elements,
                source_refs=full_refs,
            )
            tokens = token_counter(canonical_json(candidate))
            if type(tokens) is not int or tokens < 0:
                raise DomainValidationError("token counter must return a nonnegative integer")
            if context_only_ids or not (
                tokens <= 1600 and used_tokens + tokens <= max_tokens and len(candidates) < 12
            ):
                # Scoped rows compact shared context before it starves later cells.
                # Otherwise compact only oversized bundles, preserving legacy packets.
                # Store shared table/row/header
                # context once per packet instead of omitting verified evidence.
                # Drop only exact refs already admitted verbatim in an earlier
                # candidate, after all quality/ancestry/verification gates above.
                # The candidate always keeps its own ref; the union of quotes is
                # preserved and byte counts use the stored JSON. Oversized
                # bundles that still do not fit are omitted whole, as before.
                admitted = {canonical_json(ref) for c in candidates for ref in c["source_refs"]}
                kept = [full_refs[0]]
                dropped = []
                for ref in full_refs[1:]:
                    if canonical_json(ref) in admitted:
                        dropped.append(ref["source_id"])
                    else:
                        kept.append(ref)
                        admitted.add(canonical_json(ref))
                if dropped:
                    candidate["source_refs"] = kept
                    tokens = token_counter(canonical_json(candidate))
                    if type(tokens) is not int or tokens < 0:
                        raise DomainValidationError(
                            "token counter must return a nonnegative integer"
                        )
                    if (
                        tokens <= 1600
                        and used_tokens + tokens <= max_tokens
                        and len(candidates) < 12
                    ):
                        coverage["deduplicated_source_refs"][source_id] = sorted(set(dropped))
            if tokens <= 1600 and used_tokens + tokens <= max_tokens and len(candidates) < 12:
                candidates.append(candidate)
                used_tokens += tokens
                continue
            coverage["omitted_source_ids"].append(source_id)
            blocked = blocked or source_scope in ("local_claim", "same_table")
            continue
        coverage["unprocessed_source_ids"].append(source_id)
        blocked = blocked or source_scope in ("local_claim", "same_table")
    seen = {c["source_id"] for c in candidates}
    coverage["unprocessed_source_ids"] = sorted(
        set(coverage["unprocessed_source_ids"])
        | (set(blocks) - seen - set(coverage["omitted_source_ids"]))
    )
    coverage["pages"] = sorted({ref["page_num"] for c in candidates for ref in c["source_refs"]})
    coverage["tokens"] = used_tokens
    return freeze_packet(
        dict(
            schema_version="1",
            **asdict(scope),
            run_id=run_id,
            claim_id=claim.claim_id,
            atomic_quote=claim.quote,
            claim_source_refs=[asdict(ref) for ref in claim_refs],
            document_context=dict(document_context),
            allowed_elements=allowed_elements,
            evidence_candidates=candidates,
            candidate_bindings=[
                dict(source_id=c["source_id"], state="undetermined") for c in candidates
            ],
            search_coverage=coverage,
            status="blocked_evidence" if blocked else "candidate",
            source_sha256=original.source_sha256,
            graph_sha256=canonical_hash(asdict(original)),
            rule_sha256=rulepack.sha256,
            extraction_provenance=asdict(claim.receipt),
            claim_revision=claim.revision,
            query_vector_sha256=canonical_hash(query_vector),
            synthetic=search.synthetic
            or claim.receipt.profile.synthetic
            or any(batch.synthetic for batch in original.candidates),
            content_trust="untrusted_document_data",
        )
    )
