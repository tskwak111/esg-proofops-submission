"""Citation verification for replayed, source-bound spans; no paragraph promotion.

The legacy verifier and its file digest remain unchanged for historical receipts.
Only the trusted adapter may construct a SpanVerifiedGraph after receipt replay.
"""

from dataclasses import dataclass, fields, replace
from typing import cast

from proofops.application.evidence.citations import verify_source_ref as _verify
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.domain.values import SourceRef


@dataclass(frozen=True, slots=True)
class SpanVerifiedGraph(CanonicalDocumentGraph):
    verified_spans: tuple[SourceRef, ...] = ()
    span_receipt_sha256: str = ""


def span_verified_graph(graph, refs, receipt_sha256):
    return SpanVerifiedGraph(
        **{field.name: getattr(graph, field.name) for field in fields(CanonicalDocumentGraph)},
        verified_spans=tuple(refs),
        span_receipt_sha256=receipt_sha256,
    )


def verify_source_ref(
    ref: SourceRef, original: CanonicalDocumentGraph, *, tenant_id: str
) -> SourceRef:
    result = _verify(ref, original, tenant_id=tenant_id)
    if result.verification_state == "verified" or not isinstance(original, SpanVerifiedGraph):
        return result
    block = next((b for b in original.blocks if b.source_id == ref.source_id), None)
    if block is None or block.quality != "unverified":
        return result
    for span in original.verified_spans:
        if (
            span.source_id != ref.source_id
            or span.verification_state != "verified"
            or not span.char_start <= ref.char_start < ref.char_end <= span.char_end
        ):
            continue
        # This temporary view never escapes the verifier; whole-block consumers
        # continue seeing unverified. Both the stored span and requested ref must
        # satisfy every legacy tenant/artifact/geometry/raw-offset check.
        checked = replace(
            cast(CanonicalDocumentGraph, original),
            blocks=tuple(
                replace(b, quality="verified") if b.source_id == ref.source_id else b
                for b in original.blocks
            ),
        )
        if _verify(span, checked, tenant_id=tenant_id).verification_state == "verified":
            return _verify(ref, checked, tenant_id=tenant_id)
    return result
