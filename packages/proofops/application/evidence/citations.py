"""Exact, source-bound citation verification (FR-012 / TASK-012).

The original snapshot is the trusted internal canonical graph, loaded within an
authorized run; a caller-supplied v1 graph projection is not an original snapshot.
No I/O, fuzzy search, grade calculation, or claim-binding inference occurs here.
"""

from dataclasses import replace
from hashlib import sha256
from unicodedata import normalize

from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.domain.errors import DomainValidationError
from proofops.domain.values import SourceRef, _require_uuid

# Explicit text ligatures only; NFKC would also rewrite meaningful numbers/symbols.
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬆ": "st"})


def _normalized(text: str) -> str:
    return " ".join(normalize("NFC", text.translate(_LIGATURES)).split())


def verify_source_ref(
    ref: SourceRef, original: CanonicalDocumentGraph, *, tenant_id: str
) -> SourceRef:
    """Return a new verified/rejected ref without changing the source or tags.

    Offsets are raw Unicode code points, end-exclusive. Normalized-offset callers
    must first map through canonicalize_source_ref. Compare only that pinned raw
    span, then return its literal quote: no search elsewhere and no offset repair.
    The raw span is the normalization back-map; normalized text is never persisted
    in place of original text. Source quality must already be confirmed by the
    upstream source-quality gate (including any vision/human approval requirement).
    Verification proves citation existence only; binding acceptance remains a
    separate requirement for present. Rejection never means absent.
    """
    if not isinstance(ref, SourceRef) or not isinstance(original, CanonicalDocumentGraph):
        raise DomainValidationError("SourceRef and original canonical snapshot required")
    _require_uuid("tenant_id", tenant_id)
    if tenant_id != original.tenant_id:
        raise DomainValidationError("tenant mismatch")

    rejected = replace(ref, verification_state="rejected")
    blocks = {block.source_id: block for block in original.blocks}
    if (
        len(blocks) != len(original.blocks)
        or ref.document_version_id != original.document_version_id
        or ref.parse_manifest_id != original.parse_manifest_id
        or ref.source_id not in blocks
        or ref.location_quality != "located"
        or ref.bbox is None
    ):
        return rejected
    block = blocks[ref.source_id]
    if (
        block.quality != "verified"
        or type(block.winner) is not int
        or not 0 <= block.winner < len(block.candidates)
    ):
        return rejected
    candidate = block.candidates[block.winner]
    source = candidate.source
    # Retain the artifact/tenant/parser chain, not merely the public SourceRef.
    if not any(
        batch.tenant_id == tenant_id
        and batch.document_version_id == original.document_version_id
        and batch.parse_manifest_id == original.parse_manifest_id
        and batch.source_sha256 == original.source_sha256
        and batch.parser_run_id == source.parser_run_id
        and candidate in batch.blocks
        for batch in original.candidates
    ):
        return rejected
    if (
        source.document_version_id != ref.document_version_id
        or source.parse_manifest_id != ref.parse_manifest_id
        or source.physical_page != ref.page_num
        or source.printed_page_label != ref.printed_page_label
        or candidate.bbox != ref.bbox
        or sha256(source.raw_text.encode("utf-8")).hexdigest() != ref.raw_text_sha256
        or not 0 <= ref.char_start < ref.char_end <= len(source.raw_text)
    ):
        return rejected
    raw_quote = source.raw_text[ref.char_start : ref.char_end]
    normalized_quote = _normalized(ref.quote)
    if not normalized_quote or normalized_quote != _normalized(raw_quote):
        return rejected
    return replace(ref, quote=raw_quote, verification_state="verified")
