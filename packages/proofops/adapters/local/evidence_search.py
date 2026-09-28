"""Bounded local BM25 evidence search over one immutable same-document graph.

Promoted from evaluation/section_pipeline.py without importing evaluation.
Lexical hits are candidate routes only; retrieval still rechecks sources,
quality, citation and binding. No grades, labels, or quality upgrades here.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, replace
from hashlib import sha256
from math import log1p

from proofops.application.evidence.retrieval import SearchHit, SearchResult, SearchScope
from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.application.ingest.gri import _validate_graph

__all__ = [
    "LocalEvidenceSearch",
    "collect_raw_candidate_review",
    "search_terms",
    "validate_raw_candidate",
]


def search_terms(text: str) -> list[str]:
    """Korean bigrams and whole Latin/numeric tokens, for routing only."""
    terms: list[str] = []
    for word in re.findall(r"[가-힣]+|[a-z]+|\d+(?:[.,]\d+)*", text.casefold()):
        if re.fullmatch(r"[가-힣]+", word):
            terms.extend(word[i : i + 2] for i in range(len(word) - 1))
        else:
            terms.append(word)
    return terms


class LocalEvidenceSearch:
    """Existing EvidenceSearchPort over one immutable same-document graph.

    Lexical hits are candidate routes only; retrieve_evidence still checks sources,
    quality, citation and binding. Empty pages are explicit zero coverage.
    """

    def __init__(
        self,
        graph: CanonicalDocumentGraph,
        *,
        tenant_id: str,
        pages: Iterable[int],
        index_generation: str,
    ) -> None:
        _validate_graph(graph, tenant_id)
        blocks = tuple(graph.blocks)
        if len({b.source_id for b in blocks}) != len(blocks):
            raise ValueError("duplicate canonical source_id")
        if isinstance(pages, str | bytes):
            raise ValueError("pages must be an iterable of positive integers")
        items = tuple(pages)
        for page in items:
            if type(page) is not int or page < 1:
                raise ValueError("pages must be positive integers")
        available = {b.page_num for b in blocks}
        if any(page not in available for page in items):
            raise ValueError("pages must occur in graph")
        self.graph = graph
        self.synthetic = any(batch.synthetic for batch in graph.candidates)
        self.scope = SearchScope(
            tenant_id,
            graph.document_version_id,
            graph.parse_manifest_id,
            index_generation,
        )
        self.pages = frozenset(items)
        self.documents = [
            (block, Counter(search_terms(block.normalized_text)))
            for block in blocks
            if block.page_num in self.pages and block.normalized_text.strip()
        ]
        self.frequencies = Counter(term for _, terms in self.documents for term in terms)
        self.average_length = sum(sum(c.values()) for _, c in self.documents) / max(
            1, len(self.documents)
        )

    def search(
        self, scope: SearchScope, query: str, *, vector: tuple[float, ...] | None = None
    ) -> SearchResult:
        if scope != self.scope:
            raise ValueError("local evidence search scope mismatch")
        if vector is not None:
            return SearchResult(status="not_run")
        terms = set(search_terms(query))
        # ponytail: local BM25 scan; Korean bigrams route candidates, not semantic bindings.
        ranked = []
        for block, counts in self.documents:
            length = sum(counts.values())
            score = sum(
                log1p(
                    (len(self.documents) - self.frequencies[t] + 0.5) / (self.frequencies[t] + 0.5)
                )
                * counts[t]
                * 2.2
                / (counts[t] + 1.2 * (0.25 + 0.75 * length / self.average_length))
                for t in sorted(terms & counts.keys())
            )
            if score:
                ranked.append((-score, block.source_id, block))
        ranked.sort(key=lambda item: item[:2])
        return SearchResult(
            tuple(
                SearchHit(scope, block.source_id, sha256(block.raw_text.encode()).hexdigest())
                for _, _, block in ranked[:20]
            ),
            "bounded",
        )


def validate_raw_candidate(
    cand: dict,
    graph: CanonicalDocumentGraph,
    *,
    tenant_id: str,
) -> bool:
    """Validate a raw candidate record against the pinned canonical graph.

    Enforces:
    - tenant_id matches pinned graph tenant
    - no accepted or verified labels (raw candidate status must be unconfirmed, unverified, or
      candidate)
    - exact document_version_id and parse_manifest_id match
    - candidate provenance (document_version_id, parse_manifest_id, physical_page) match graph
    - location_quality is strictly located
    - bbox is finite and exactly equals canonical bbox
    - printed_page_label and verification_state match canonical ref
    - raw_text_sha256 exactly matches block raw text sha256
    - strict non-empty literal raw text span (bool offsets rejected, 0 <= start < end <=
      len(raw_text))
    - literal quote exactly matches raw_text[start:end]
    """
    if not isinstance(cand, dict):
        return False
    status = cand.get("status")
    # No accepted label allowed: status must remain unconfirmed/unverified
    if status not in ("candidate", "unverified", "unconfirmed"):
        return False
    ref_dict = cand.get("source_ref")
    if not isinstance(ref_dict, dict):
        return False
    source_id = ref_dict.get("source_id")
    if not isinstance(source_id, str):
        return False
    # Pinned graph identity checks
    if graph.tenant_id != tenant_id:
        return False
    if (
        ref_dict.get("document_version_id") != graph.document_version_id
        or ref_dict.get("parse_manifest_id") != graph.parse_manifest_id
    ):
        return False
    blocks = {b.source_id: b for b in graph.blocks}
    block = blocks.get(source_id)
    if block is None or block.winner is None:
        return False
    cand_block = block.candidates[block.winner]
    if (
        cand_block.source.document_version_id != graph.document_version_id
        or cand_block.source.parse_manifest_id != graph.parse_manifest_id
        or cand_block.source.physical_page != block.page_num
    ):
        return False
    try:
        canonical_ref = block.source_ref()
    except Exception:
        return False
    # Exact location quality
    if ref_dict.get("location_quality") != "located" or canonical_ref.location_quality != "located":
        return False
    # Exact bbox equality
    ref_bbox = ref_dict.get("bbox")
    can_bbox = canonical_ref.bbox
    if ref_bbox is None or can_bbox is None:
        return False
    if not isinstance(ref_bbox, list | tuple) or len(ref_bbox) != 4:
        return False
    if any(
        isinstance(c, bool) or not isinstance(c, int | float) or not math.isfinite(c)
        for c in ref_bbox
    ):
        return False
    if any(not math.isclose(a, b, abs_tol=1e-6) for a, b in zip(ref_bbox, can_bbox, strict=True)):
        return False
    # Exact page num and printed_page_label
    page_num = ref_dict.get("page_num")
    if (
        isinstance(page_num, bool)
        or not isinstance(page_num, int)
        or page_num != block.page_num
        or page_num < 1
    ):
        return False
    if ref_dict.get("printed_page_label") != canonical_ref.printed_page_label:
        return False
    # Exact verification_state
    if ref_dict.get("verification_state") != canonical_ref.verification_state:
        return False
    # Exact raw text sha256
    expected_hash = sha256(block.raw_text.encode("utf-8")).hexdigest()
    if (
        ref_dict.get("raw_text_sha256") != expected_hash
        or canonical_ref.raw_text_sha256 != expected_hash
    ):
        return False
    # Strict non-empty literal span and bool rejection
    start = ref_dict.get("char_start")
    end = ref_dict.get("char_end")
    if isinstance(start, bool) or not isinstance(start, int):
        return False
    if isinstance(end, bool) or not isinstance(end, int):
        return False
    if not (0 <= start < end <= len(block.raw_text)):
        return False
    quote = ref_dict.get("quote")
    if not isinstance(quote, str) or not quote:
        return False
    if quote != block.raw_text[start:end]:
        return False
    return True


def collect_raw_candidate_review(
    search: LocalEvidenceSearch,
    query: str,
    *,
    max_candidates: int = 20,
    max_excerpt_chars: int = 4000,
) -> dict:
    """Collect bounded, validated raw candidates for a query from BM25 search.

    Uses shared validate_raw_candidate to guarantee identical security and integrity gates.
    Status remains unconfirmed/unverified even for verified sources (no accepted label).
    Bounded excerpts strictly match literal raw text slices.
    """
    result = search.search(search.scope, query)
    blocks = {b.source_id: b for b in search.graph.blocks}
    candidates = []
    for hit in result.hits[:max_candidates]:
        block = blocks.get(hit.source_id)
        if block is None or block.winner is None:
            continue
        try:
            canonical_ref = block.source_ref()
        except Exception:
            continue
        raw_text = block.raw_text
        if not raw_text:
            continue
        cut = min(len(raw_text), max_excerpt_chars)
        bounded_ref = replace(
            canonical_ref,
            char_start=0,
            char_end=cut,
            quote=raw_text[:cut],
        )
        status = "unconfirmed" if block.quality == "verified" else "unverified"
        reason = "RAW_CANDIDATE_UNCONFIRMED" if block.quality == "verified" else "UNVERIFIED_SOURCE"
        cand = {
            "source_ref": asdict(bounded_ref),
            "status": status,
            "reason": reason,
        }
        if validate_raw_candidate(cand, search.graph, tenant_id=search.scope.tenant_id):
            candidates.append(cand)
    return {
        "schema_version": 1,
        "candidates": candidates,
    }
