"""Pure conservative sentence boundaries over already-normalized source text.

Moved here unchanged from ``evaluation.claim_prefilter`` so the production
extraction adapter can reuse the exact boundary policy the real ID-selection
experiment used, without any runtime import of the evaluation package.
``evaluation.claim_prefilter`` re-exports this function, so both paths compute
byte-identical spans.

No segmentation model, no normalization, no rewriting: the returned offsets
index the supplied string directly, so a caller always restores the literal
original substring.
"""

from __future__ import annotations

import re

_BOUNDARY = re.compile(r".+?(?:[.!?](?=\s|$)|\n+|$)", re.S)


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Conservative local boundaries; whitespace-trimmed, non-overlapping, ordered."""
    # ponytail: punctuation boundaries only; add Korean segmentation if measured errors warrant it.
    return [
        (
            match.start() + len(match[0]) - len(match[0].lstrip()),
            match.end() - len(match[0]) + len(match[0].rstrip()),
        )
        for match in _BOUNDARY.finditer(text)
        if match[0].strip()
    ]
