"""Atomic candidate discovery, with immutable provenance and explicit coverage gaps.

No grading or persistence occurs here. Track classification belongs to TASK-009.
The caller supplies an extractor explicitly; there is no silent synthetic fallback.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Protocol
from uuid import UUID, uuid5

from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph, QualityIssue
from proofops.domain.provenance import canonical_hash
from proofops.domain.rulepacks import canonical_json
from proofops.domain.values import SourceRef, _require_sha256, _require_uuid


@dataclass(frozen=True, slots=True)
class ClaimScope:
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    mode: str = "full"
    selected_pages: tuple[int, ...] = ()

    def __post_init__(self):
        for name in ("tenant_id", "document_version_id", "parse_manifest_id"):
            _require_uuid(name, getattr(self, name))
        pages = tuple(self.selected_pages)
        if (
            self.mode not in ("full", "declared_subset")
            or any(type(page) is not int or page < 1 for page in pages)
            or tuple(sorted(set(pages))) != pages
            or (self.mode == "full" and pages)
            or (self.mode == "declared_subset" and not pages)
        ):
            raise ValueError("invalid full/declared_subset scope")
        object.__setattr__(self, "selected_pages", pages)


@dataclass(frozen=True, slots=True)
class ExtractionProfile:
    model_sha256: str
    prompt_sha256: str
    rule_sha256: str
    synthetic: bool
    replicate_id: int = 1
    extraction_epoch: int = 1

    def __post_init__(self):
        for name in ("model_sha256", "prompt_sha256", "rule_sha256"):
            _require_sha256(name, getattr(self, name))
        if (
            type(self.synthetic) is not bool
            or type(self.replicate_id) is not int
            or self.replicate_id not in (1, 2, 3)
            or type(self.extraction_epoch) is not int
            or self.extraction_epoch < 1
        ):
            raise ValueError("invalid extraction profile")


class ExtractionOutputError(ValueError):
    """Rejected model content; preserve this block as unknown and continue."""


class ClaimExtractorPort(Protocol):
    @property
    def profile(self) -> ExtractionProfile: ...

    def extract(self, packet: dict) -> dict: ...


@dataclass(frozen=True, slots=True)
class ExtractionSpan:
    char_start: int
    char_end: int
    quote: str
    kind: str
    reason: str | None
    topic_ids: tuple[str, ...]


def _unclosed_quotation_ranges(text: str) -> list[tuple[int, int]]:
    # ponytail: detect explicit paired-quote cuts, not grammatical completeness.
    ranges = []
    for opening, closing in (("‘", "’"), ("“", "”")):
        stack = []
        for i, char in enumerate(text):
            if (
                char == "’"
                and 0 < i < len(text) - 1
                and all(c.isascii() and c.isalpha() for c in (text[i - 1], text[i + 1]))
            ):
                continue  # English apostrophe, e.g. O’Reilly
            if char == opening:
                stack.append(i)
            elif char == closing:
                if stack:
                    stack.pop()
                else:
                    ranges.append((0, i + 1))
        ranges.extend((i, len(text)) for i in stack)
    return ranges


def validate_extraction_response(
    payload: object, text: str, *, reject_unclosed_quotations: bool = False
) -> tuple[ExtractionSpan, ...]:
    """Strict no-grade boundary; offsets are NFC code points, end-exclusive.

    New extractor profiles opt into quotation checks. Stored legacy receipts
    keep their original replay behavior; this flag never changes old revisions.
    """
    if not isinstance(payload, dict) or set(payload) != {"spans"}:
        raise ValueError("extraction response must contain only spans")
    if not isinstance(payload["spans"], list):
        raise ValueError("spans must be an array")
    spans = []
    unclosed = _unclosed_quotation_ranges(text) if reject_unclosed_quotations else []
    fields = {"char_start", "char_end", "quote", "kind", "reason", "topic_ids"}
    for item in payload["spans"]:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError("invalid extraction fields; grades and labels are prohibited")
        start, end = item["char_start"], item["char_end"]
        if (
            type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(text)
            or item["quote"] != text[start:end]
            or not text[start:end].strip()
        ):
            raise ValueError("extraction quote/offset does not match source")
        kind, reason, topics = item["kind"], item["reason"], item["topic_ids"]
        if kind == "claim" and any(start < right and end > left for left, right in unclosed):
            raise ValueError("claim overlaps an unclosed source quotation")
        if kind == "claim" and (
            (start > 0 and text[start - 1].isalnum() and text[start].isalnum())
            or (end < len(text) and text[end - 1].isalnum() and text[end].isalnum())
            or any(
                number.start() < boundary < number.end()
                for number in re.finditer(r"[+−-]?\d+(?:[,.]\d+)*%?", text)
                for boundary in (start, end)
            )
        ):
            raise ValueError("claim quote cuts a token boundary")
        if (
            kind not in ("claim", "excluded", "unknown")
            or (kind == "claim" and reason is not None)
            or (kind != "claim" and (not isinstance(reason, str) or not reason.strip()))
            or not isinstance(topics, list)
            or any(not isinstance(topic, str) or not topic.strip() for topic in topics)
        ):
            raise ValueError("invalid extraction kind/reason/topics")
        spans.append(ExtractionSpan(start, end, item["quote"], kind, reason, tuple(topics)))
    spans.sort(key=lambda span: span.char_start)
    if any(left.char_end > right.char_start for left, right in zip(spans, spans[1:])):
        raise ValueError("overlapping extraction spans")
    return tuple(spans)


@dataclass(frozen=True, slots=True)
class ExtractionReceipt:
    source_id: str
    packet_sha256: str
    response_sha256: str | None
    raw_response_json: str | None
    profile: ExtractionProfile
    status: str


@dataclass(frozen=True, slots=True)
class Claim:
    claim_id: str
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    quote: str
    source_refs: tuple[SourceRef, ...]
    source_quality: str
    topic_ids: tuple[str, ...]
    receipt: ExtractionReceipt
    revision: int = 1

    def to_summary(self) -> dict:
        """Existing v1 API projection, leaving all decisions/track unset."""
        return dict(
            claim_id=self.claim_id,
            page_num=self.source_refs[0].page_num,
            quote=self.quote,
            track=None,
            topic_ids=list(self.topic_ids),
            decision=None,
            revision=self.revision,
        )


@dataclass(frozen=True, slots=True)
class ClaimExclusion:
    source_id: str
    page_num: int
    reason: str
    state: str
    source_ref: SourceRef | None = None


@dataclass(frozen=True, slots=True)
class ClaimDiscovery:
    scope: ClaimScope
    source_sha256: str
    claims: tuple[Claim, ...]
    exclusions: tuple[ClaimExclusion, ...]
    processed_source_ids: tuple[str, ...]
    receipts: tuple[ExtractionReceipt, ...]
    synthetic: bool
    quality_issues: tuple[QualityIssue, ...]


def discover_atomic_claims(
    graph: CanonicalDocumentGraph, scope: ClaimScope, *, extractor: ClaimExtractorPort
) -> ClaimDiscovery:
    """Visit every in-scope block; never impose a topic/count quota.

    Model failures and uncovered text remain unknown, not non-claims/absence.
    SourceStore/worker authorization must supply the trusted graph and scope.
    """
    identity = (scope.tenant_id, scope.document_version_id, scope.parse_manifest_id)
    if (graph.tenant_id, graph.document_version_id, graph.parse_manifest_id) != identity:
        raise ValueError("graph identity mismatch")
    _require_sha256("source_sha256", graph.source_sha256)
    for batch in graph.candidates:
        if (batch.tenant_id, batch.document_version_id, batch.parse_manifest_id) != identity:
            raise ValueError("candidate identity mismatch")
        if batch.source_sha256 != graph.source_sha256:
            raise ValueError("candidate artifact identity mismatch")
    ids = set()
    for block in graph.blocks:
        _require_uuid("source_id", block.source_id)
        if not block.candidates or block.source_id in ids:
            raise ValueError("empty/duplicate canonical source")
        ids.add(block.source_id)
        for source in block.sources:
            if (source.document_version_id, source.parse_manifest_id) != identity[1:]:
                raise ValueError("source identity mismatch")
    profile = extractor.profile
    if not isinstance(profile, ExtractionProfile):
        raise ValueError("extraction profile required")
    claims, exclusions, processed, receipts = [], [], [], []
    for block in sorted(graph.blocks, key=lambda item: (item.page_num, item.source_id)):
        if scope.mode == "declared_subset" and block.page_num not in scope.selected_pages:
            exclusions.append(
                ClaimExclusion(
                    block.source_id, block.page_num, "outside_declared_subset", "excluded"
                )
            )
            continue
        if block.quality not in ("verified", "unverified") or block.winner is None:
            state = {"conflicted": "conflict", "unreadable": "unreadable"}.get(
                block.quality, "unknown"
            )
            exclusions.append(ClaimExclusion(block.source_id, block.page_num, block.quality, state))
            continue
        text = block.normalized_text
        if not text.strip():
            exclusions.append(
                ClaimExclusion(
                    block.source_id, block.page_num, "empty_source", "unknown", block.source_ref()
                )
            )
            continue
        packet = dict(
            tenant_id=scope.tenant_id,
            document_version_id=scope.document_version_id,
            parse_manifest_id=scope.parse_manifest_id,
            source_sha256=graph.source_sha256,
            extraction_profile=asdict(profile),
            untrusted_document_data=dict(
                source_id=block.source_id,
                page_num=block.page_num,
                kind=block.kind,
                text=text,
            ),
        )
        packet_hash = canonical_hash(packet)
        try:
            payload = extractor.extract(packet)
        except (TimeoutError, ConnectionError, ExtractionOutputError):
            receipts.append(
                ExtractionReceipt(block.source_id, packet_hash, None, None, profile, "failed")
            )
            exclusions.append(
                ClaimExclusion(
                    block.source_id,
                    block.page_num,
                    "extraction_failed",
                    "unknown",
                    block.source_ref(),
                )
            )
            continue
        spans = validate_extraction_response(payload, text)
        receipt = ExtractionReceipt(
            block.source_id,
            packet_hash,
            canonical_hash(payload),
            canonical_json(payload),
            profile,
            "processed",
        )
        receipts.append(receipt)
        processed.append(block.source_id)
        cursor = 0
        for span in (*spans, ExtractionSpan(len(text), len(text), "", "unknown", None, ())):
            if text[cursor : span.char_start].strip():
                exclusions.append(
                    ClaimExclusion(
                        block.source_id,
                        block.page_num,
                        "unprocessed_span",
                        "unknown",
                        block.source_ref(
                            normalized_char_start=cursor, normalized_char_end=span.char_start
                        ),
                    )
                )
            if span.char_start == span.char_end:
                break
            ref = block.source_ref(
                normalized_char_start=span.char_start, normalized_char_end=span.char_end
            )
            if span.kind == "claim":
                claim_hash = canonical_hash(
                    dict(
                        tenant_id=scope.tenant_id,
                        source_sha256=graph.source_sha256,
                        source=asdict(ref),
                        receipt=asdict(receipt),
                    )
                )
                claims.append(
                    Claim(
                        str(uuid5(UUID(scope.parse_manifest_id), claim_hash)),
                        *identity,
                        graph.source_sha256,
                        ref.quote,
                        (ref,),
                        block.quality,
                        span.topic_ids,
                        receipt,
                    )
                )
            else:
                exclusions.append(
                    ClaimExclusion(
                        block.source_id,
                        block.page_num,
                        span.reason or "unclassified",
                        span.kind,
                        ref,
                    )
                )
            cursor = span.char_end
    return ClaimDiscovery(
        scope,
        graph.source_sha256,
        tuple(claims),
        tuple(exclusions),
        tuple(processed),
        tuple(receipts),
        profile.synthetic or any(batch.synthetic for batch in graph.candidates),
        tuple(graph.issues),
    )
