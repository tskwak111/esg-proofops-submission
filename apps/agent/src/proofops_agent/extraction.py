"""Explicit extraction adapters; no live model binding or product model calls.

StructuredClaimExtractor accepts a caller-owned response transport. Production
composition/preflight must authorize that transport; this module grants no access.
SyntheticClaimExtractor is a small Korean/English local demonstration heuristic,
not an approved model, environmental classifier, or corpus recall measurement.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from proofops.application.claims import ExtractionProfile
from proofops.domain.provenance import canonical_hash

_ENVIRONMENT = (
    r"탄소|온실가스|배출|에너지|재생|용수|폐기물|환경|생물다양성|"
    r"carbon|emission|water|waste|energy"
)
_SENTENCES = r"(?<=[.!?])\s+|\n+"
_CLAUSES = r"(?<=으며),?\s+|(?<=했고),?\s+|(?<=하였고),?\s+|;\s*|,\s+(?=\d{4}년)|\s+and\s+"
_DEFINITION = r"(?:이란|란)\s.+(?:의미|말한다)|\bis defined as\b"


@dataclass(frozen=True, slots=True)
class StructuredClaimExtractor:
    profile: ExtractionProfile
    respond: Callable[[dict], dict]

    def extract(self, packet: dict) -> dict:
        return self.respond(packet)


class SyntheticClaimExtractor:
    profile = ExtractionProfile(
        model_sha256=canonical_hash("synthetic-local-claim-heuristic-v1"),
        prompt_sha256=canonical_hash("no model prompt; local deterministic synthetic extraction"),
        rule_sha256=canonical_hash([_ENVIRONMENT, _SENTENCES, _CLAUSES, _DEFINITION]),
        synthetic=True,
    )

    def extract(self, packet: dict) -> dict:
        text = packet["untrusted_document_data"]["text"]
        spans = []
        # ponytail: lexical demo only; use an approved extraction model for corpus semantics.
        sentence_start = 0
        for boundary in [*re.finditer(_SENTENCES, text), None]:
            end = boundary.start() if boundary else len(text)
            sentence = text[sentence_start:end]
            environmental = re.search(_ENVIRONMENT, sentence, re.IGNORECASE) is not None
            definition = re.search(_DEFINITION, sentence, re.IGNORECASE) is not None
            pieces = [None] if definition else [*re.finditer(_CLAUSES, sentence), None]
            start = sentence_start
            for split in pieces:
                stop = sentence_start + split.start() if split else end
                # Keep the Korean connective comma in its source span.
                if split and text[stop : stop + 1] == "," and stop > 0 and text[stop - 1] == "며":
                    stop += 1
                left = start + len(text[start:stop]) - len(text[start:stop].lstrip())
                right = stop - len(text[start:stop]) + len(text[start:stop].rstrip())
                if left < right:
                    spans.append(
                        dict(
                            char_start=left,
                            char_end=right,
                            quote=text[left:right],
                            kind="excluded"
                            if definition
                            else "claim"
                            if environmental
                            else "unknown",
                            reason="term_definition"
                            if definition
                            else None
                            if environmental
                            else "unclassified",
                            topic_ids=["environment"] if environmental else [],
                        )
                    )
                start = sentence_start + split.end() if split else end
            sentence_start = boundary.end() if boundary else len(text)
        return {"spans": spans}
