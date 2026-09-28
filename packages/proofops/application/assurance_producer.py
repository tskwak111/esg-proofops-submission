"""Pure helpers turning a model's literal quote choices into `extract_assurance` input.

No network, no filesystem, no probe. This module never decides what a field
*means*; it only (a) freezes which graph blocks form one caller-declared
assurance opinion boundary, and (b) locates a model-returned literal quote
inside the *raw* text of one of those blocks' winning candidate, producing an
unambiguous ``SourceRef``. Both steps raise rather than guess on any
ambiguity, missing scope, or cross-boundary leakage.

The network/probe/receipts
adapter lives in `adapters/local/upstage_assurance.py`; this module stays
free of I/O so it can be unit-tested without any transport.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from proofops.application.assurance import LISTS, SCALARS
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.domain.errors import DomainValidationError
from proofops.domain.values import SourceRef, _require_uuid

ASSURANCE_FIELDS = frozenset(SCALARS + LISTS)


@dataclass(frozen=True, slots=True)
class OpinionBoundary:
    """One caller-declared assurance opinion's block set.

    `source_ids` is an explicit, caller-supplied selection — never inferred
    from text similarity, page proximity, or heading detection. Declaring a
    boundary is not proof the selected blocks actually form one coherent
    opinion; a caller that hands this function a mix of two different
    opinions' blocks will get a boundary that treats them as one, and
    `extract_assurance`'s per-field checks (single-value SCALARS, exclusion
    quotes must originate inside the boundary) are the only defense against
    that misuse, not this dataclass.
    """

    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_uuid("tenant_id", self.tenant_id)
        _require_uuid("document_version_id", self.document_version_id)
        _require_uuid("parse_manifest_id", self.parse_manifest_id)
        ids = tuple(self.source_ids)
        if not ids or any(not isinstance(i, str) or not i for i in ids):
            raise DomainValidationError("opinion boundary requires nonempty source_ids")
        if len(set(ids)) != len(ids):
            raise DomainValidationError("opinion boundary source_ids must be unique")
        object.__setattr__(self, "source_ids", ids)


def _require_boundary_matches_graph(
    graph: CanonicalDocumentGraph, boundary: OpinionBoundary
) -> None:
    """Reject a boundary built for a different graph, even if source_ids collide.

    `select_opinion_boundary` always builds a fresh `OpinionBoundary` from
    the graph it was given, so this only matters when a caller passes back
    an `OpinionBoundary` obtained from one graph while operating on a
    different graph (e.g. a different document/run/parse). Canonical
    `source_id`s are `uuid5`-derived from parser-local identifiers and are
    not guaranteed unique across unrelated graphs, so an identifier match
    alone is not proof of the same opinion; the boundary's own
    tenant/document_version/parse_manifest identity must also match.
    """
    if (boundary.tenant_id, boundary.document_version_id, boundary.parse_manifest_id) != (
        graph.tenant_id,
        graph.document_version_id,
        graph.parse_manifest_id,
    ):
        raise DomainValidationError(
            "opinion boundary identity does not match this graph "
            "(tenant/document_version/parse_manifest mismatch)"
        )


def select_opinion_boundary(
    graph: CanonicalDocumentGraph, source_ids: Sequence[str]
) -> tuple[OpinionBoundary, dict[str, str]]:
    """Freeze one opinion's blocks by explicit id and return each block's raw text.

    Returns the boundary plus ``{source_id: raw_text}`` for every requested
    block, using each block's *actual winning candidate's raw_text* (the same
    text `extract_assurance` will re-check citations against) — never the
    normalized/rendered text. Missing/duplicate ids, blocks without a winner,
    or blocks outside this graph raise; this function never silently narrows
    or widens the caller's declared boundary.
    """
    boundary = OpinionBoundary(
        tenant_id=graph.tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_ids=tuple(source_ids),
    )
    blocks = {b.source_id: b for b in graph.blocks}
    if len(blocks) != len(graph.blocks):
        raise DomainValidationError("duplicate canonical source_id in graph")
    texts: dict[str, str] = {}
    for source_id in boundary.source_ids:
        block = blocks.get(source_id)
        if block is None:
            raise DomainValidationError(
                f"opinion boundary references unknown source_id {source_id}"
            )
        if block.winner is None:
            raise DomainValidationError(f"opinion boundary source_id {source_id} has no winner")
        texts[source_id] = block.raw_text
    return boundary, texts


def locate_field_quote(
    graph: CanonicalDocumentGraph,
    boundary: OpinionBoundary,
    *,
    field: str,
    source_id: str,
    quote: str,
) -> SourceRef:
    """Locate one model-returned literal quote inside one boundary block's raw text.

    Requires an *unambiguous, exact, nonempty* substring match (same
    discipline as `UpstageClaimExtractor._locate`): absent or repeated quotes
    raise rather than picking a side. `source_id` must be a member of
    `boundary.source_ids` — a quote cannot be attributed to a block outside
    the caller-declared opinion, which is the mechanism preventing one
    opinion's text from lending facts to another when boundaries are kept
    disjoint by the caller. `boundary` itself must belong to `graph` (see
    `_require_boundary_matches_graph`); a boundary obtained from a different
    graph is rejected even if its source_ids happen to collide.

    Offset handling: `start`/`start + len(quote)` here are computed directly
    against `block.raw_text` (the exact bytes-as-parsed text), NOT against
    NFC-normalized text. `CanonicalBlock.source_ref(normalized_char_start=,
    normalized_char_end=)` interprets its arguments as *normalized* offsets
    and remaps them back to raw offsets internally — passing already-raw
    offsets through that parameter would silently mis-cite whenever
    normalization changes length (repeated whitespace, newlines inside the
    quote, or any NFC-affected codepoint). We therefore call
    `block.source_ref()` with no arguments (which uses the raw
    `source.char_start`/`char_end` unchanged) to obtain a validly-geometried
    `SourceRef`, then `dataclasses.replace` only `char_start`/`char_end`/
    `quote` with the real raw slice, preserving every other field
    (`bbox`, `location_quality`, `raw_text_sha256`, `page_num`, ...) exactly
    as `canonicalize_source_ref` computed them. `extract_assurance.verified`
    re-checks `raw_text[char_start:char_end] == quote` against the same raw
    text afterward, so any mistake here still fails closed downstream.
    """
    if field not in ASSURANCE_FIELDS:
        raise DomainValidationError(f"unknown assurance field: {field}")
    _require_boundary_matches_graph(graph, boundary)
    if source_id not in boundary.source_ids:
        raise DomainValidationError("quote source_id is outside the declared opinion boundary")
    if not isinstance(quote, str) or not quote.strip():
        raise DomainValidationError("assurance field quote must be a nonempty string")
    blocks = {b.source_id: b for b in graph.blocks}
    block = blocks.get(source_id)
    if block is None or block.winner is None:
        raise DomainValidationError(f"boundary source_id {source_id} unresolved in graph")
    text = block.raw_text
    if quote not in text or text.find(quote) != text.rfind(quote):
        raise DomainValidationError("assurance field quote absent or ambiguous in source")
    start = text.index(quote)
    end = start + len(quote)
    base_ref = block.source_ref()  # raw source.char_start/char_end; no normalization mapping
    return replace(base_ref, char_start=start, char_end=end, quote=text[start:end])


def build_tagged_fields(
    graph: CanonicalDocumentGraph,
    boundary: OpinionBoundary,
    field_quotes: Mapping[str, Sequence[Mapping[str, str]]],
) -> dict[str, tuple[SourceRef, ...]]:
    """Turn ``{field: [{"source_id":..., "quote":...}, ...]}`` into tagged_fields.

    Every quote is independently located and boundary-fenced via
    `locate_field_quote`; an unknown field name or malformed quote entry
    raises immediately rather than being dropped silently. A field absent
    from `field_quotes` (the model chose not to answer it) is simply absent
    from the result — this function never invents a value for a field the
    model didn't cite, and `extract_assurance` treats an absent field as
    unresolved/undetermined, never as a negative assertion.
    """
    unknown = set(field_quotes) - ASSURANCE_FIELDS
    if unknown:
        raise DomainValidationError(f"unknown assurance fields in response: {sorted(unknown)}")
    _require_boundary_matches_graph(graph, boundary)
    tagged: dict[str, tuple[SourceRef, ...]] = {}
    for field, entries in field_quotes.items():
        if not isinstance(entries, Sequence) or isinstance(entries, str | bytes):
            raise DomainValidationError(f"assurance field {field} requires a list of quotes")
        refs = []
        for entry in entries:
            if (
                not isinstance(entry, Mapping)
                or set(entry) != {"source_id", "quote"}
                or not isinstance(entry.get("source_id"), str)
                or not isinstance(entry.get("quote"), str)
            ):
                raise DomainValidationError(f"assurance field {field} entry malformed")
            refs.append(
                locate_field_quote(
                    graph,
                    boundary,
                    field=field,
                    source_id=entry["source_id"],
                    quote=entry["quote"],
                )
            )
        if refs:
            tagged[field] = tuple(refs)
    return tagged
