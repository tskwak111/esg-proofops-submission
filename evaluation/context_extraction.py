"""Target + bounded same-page context packet (offline evaluation only).

No API calls, key/ledger access, grading, or runtime defaults. Context blocks
are for interpreting the target only; quotes validate against the target via
the real extractor validator. Failures raise ``ValueError`` (caller records
``unknown``); this module never returns ``unknown`` itself.
"""

from __future__ import annotations

from dataclasses import asdict

from proofops.application.ingest.gri import _validate_graph
from proofops_agent.upstage_extraction import SYSTEM_PROMPT

CONTEXT_SYSTEM = SYSTEM_PROMPT + (
    " The ONLY extraction target is untrusted_document_data.target.text. "
    "untrusted_document_data.context_blocks is NOT an extraction target. "
    "Use context only to interpret the target; never quote or extract a context-only assertion. "
    "If target.text is a heading, chart label, glossary term, or incomplete fragment, "
    'return {"claims": []} even if the context contains valid company assertions. '
    "Keep concise explicit year/scope/quantity targets and concrete company actions. "
    "Never complete a truncated assertion using context or infer that a listed risk happened."
)

_REFUSABLE = frozenset({"verified", "unverified"})
_EXCLUDED_KINDS = frozenset({"table", "table_cell"})


def _center(box):
    if box is None:
        return None
    return ((box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0)


def build_context_packet(graph, source_id, *, tenant_id, max_context_chars, max_context_blocks):
    """Target packet with bounded nearby same-page context refs."""
    _validate_graph(graph, tenant_id)
    if (
        type(max_context_chars) is not int
        or not 1 <= max_context_chars <= 20000
        or type(max_context_blocks) is not int
        or not 0 <= max_context_blocks <= 20
    ):
        raise ValueError("CONTEXT_BOUNDS_INVALID")
    blocks = {b.source_id: b for b in graph.blocks}
    if len(blocks) != len(graph.blocks) or source_id not in blocks:
        raise ValueError("CONTEXT_TARGET_NOT_FOUND")
    target = blocks[source_id]
    if target.quality not in _REFUSABLE or target.winner is None:
        raise ValueError("CONTEXT_TARGET_UNRESOLVABLE")
    if not target.normalized_text.strip():
        raise ValueError("CONTEXT_TARGET_EMPTY")
    center = _center(target.bbox)

    def distance(block):
        other = _center(block.bbox)
        if center is None or other is None:
            return float("inf")
        return abs(center[0] - other[0]) + abs(center[1] - other[1])

    ranked = sorted(
        (
            b
            for b in graph.blocks
            if b is not target
            and b.page_num == target.page_num
            and b.quality in _REFUSABLE
            and b.winner is not None
            and b.kind not in _EXCLUDED_KINDS
            and b.normalized_text.strip()
        ),
        key=lambda b: (distance(b), tuple(b.bbox or ()), b.normalized_text, b.source_id),
    )
    chosen, used = [], 0
    nearest = ranked[:max_context_blocks]
    ordered = sorted(nearest, key=lambda b: (tuple(b.bbox or ()), b.source_id))
    for block in ordered:
        size = len(block.normalized_text) + (1 if chosen else 0)
        if used + size > max_context_chars:
            continue  # whole blocks only; never char-truncate a neighbour
        chosen.append(block)
        used += size
    return dict(
        tenant_id=graph.tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        untrusted_document_data=dict(
            target=dict(
                source_id=target.source_id,
                page_num=target.page_num,
                kind=target.kind,
                text=target.normalized_text,
                source_ref={k: v for k, v in asdict(target.source_ref()).items() if k != "quote"},
            ),
            context_blocks=[asdict(b.source_ref()) for b in chosen],
            context_policy=dict(
                same_page_only=True,
                excluded_kinds=sorted(_EXCLUDED_KINDS),
                max_context_chars=max_context_chars,
                max_context_blocks=max_context_blocks,
                whole_blocks_only=True,
            ),
        ),
    )


def validate_target_quotes(payload, target_text):
    """Exact-target quote guard via the real extractor validator."""
    from evaluation.upstage_live_probe import locate_quotes

    if not isinstance(target_text, str) or not target_text:
        raise ValueError("CONTEXT_TARGET_TEXT_INVALID")
    return locate_quotes(payload, target_text)
