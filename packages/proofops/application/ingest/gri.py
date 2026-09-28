"""Deterministic GRI Content Index routing."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from proofops.application.ingest.graph_fusion import CanonicalDocumentGraph
from proofops.domain.values import SourceRef

_CODE = re.compile(r"(?<![\w-])(?:GRI\s+)?(?P<code>\d{1,3}-\d{1,3})(?![\w-])", re.I)
_PAGE_SUFFIX = re.compile(
    r"(?P<refs>(?:p{1,2}\.?\s*)?\d+(?:\s*[-–—]\s*\d+)?"
    r"(?:\s*[,;/]\s*(?:p{1,2}\.?\s*)?\d+(?:\s*[-–—]\s*\d+)?)*)"
    r"(?:\s*(?:쪽|페이지))?\s*$",
    re.I,
)


@dataclass(frozen=True, slots=True)
class IndexEntry:
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    indicator_code: str
    printed_page_refs: tuple[str, ...]
    resolved_physical_pages: tuple[int, ...]
    link_text: str
    resolution_state: str
    source_ref: SourceRef


def _printed_pages(page_map: Mapping[object, object]) -> dict[str, tuple[int, ...]]:
    if not isinstance(page_map, Mapping):
        raise ValueError("printed-page map must be a mapping")
    result: dict[str, set[int]] = {}
    for label_or_page, pages_or_label in page_map.items():
        if type(label_or_page) is int:
            if label_or_page < 1 or not isinstance(pages_or_label, str):
                raise ValueError("invalid physical-page to printed-label mapping")
            label, pages = pages_or_label.strip(), (label_or_page,)
        elif isinstance(label_or_page, str):
            label = label_or_page.strip()
            pages = (
                (pages_or_label,)
                if type(pages_or_label) is int
                else tuple(pages_or_label)
                if isinstance(pages_or_label, Sequence)
                and not isinstance(pages_or_label, str | bytes)
                else ()
            )
        else:
            raise ValueError("invalid printed-page map key")
        if not label or not pages or any(type(page) is not int or page < 1 for page in pages):
            raise ValueError("invalid printed-page map entry")
        result.setdefault(label, set()).update(pages)
    return {label: tuple(sorted(pages)) for label, pages in result.items()}


def _page_refs(text: str, code_end: int) -> tuple[str, ...]:
    match = _PAGE_SUFFIX.search(text)
    if match is None or match.start() < code_end:
        return ()
    refs: list[str] = []
    for raw in re.split(r"[,;/]", match.group("refs")):
        value = re.sub(r"^p{1,2}\.?\s*", "", raw.strip(), flags=re.I)
        range_match = re.fullmatch(r"(\d+)\s*[-–—]\s*(\d+)", value)
        if range_match:
            start, end = map(int, range_match.groups())
            values = map(str, range(start, end + 1)) if 0 <= end - start <= 200 else (value,)
        else:
            values = (value,)
        for item in values:
            if item not in refs:
                refs.append(item)
    return tuple(refs)


def _validate_graph(graph: CanonicalDocumentGraph, tenant_id: str) -> None:
    if not isinstance(graph, CanonicalDocumentGraph) or graph.tenant_id != tenant_id:
        raise ValueError("NOT_FOUND")
    identity = (tenant_id, graph.document_version_id, graph.parse_manifest_id, graph.source_sha256)
    if not graph.candidates or any(
        (
            batch.tenant_id,
            batch.document_version_id,
            batch.parse_manifest_id,
            batch.source_sha256,
        )
        != identity
        for batch in graph.candidates
    ):
        raise ValueError("NOT_FOUND")
    if any(
        not block.candidates
        or any(
            (candidate.source.document_version_id, candidate.source.parse_manifest_id)
            != (graph.document_version_id, graph.parse_manifest_id)
            for candidate in block.candidates
        )
        for block in graph.blocks
    ):
        raise ValueError("invalid graph provenance")


def build_gri_index(
    graph: CanonicalDocumentGraph,
    printed_page_map: Mapping[object, object],
    *,
    tenant_id: str | None = None,
) -> tuple[IndexEntry, ...]:
    """Map GRI row page labels to explicit physical-page candidates.

    A missing label is unresolved. This function never infers a document-wide
    offset and never upgrades a missing mapping to a confirmed mismatch.
    """
    caller_tenant = graph.tenant_id if tenant_id is None else tenant_id
    _validate_graph(graph, caller_tenant)
    page_map = _printed_pages(printed_page_map)
    entries = []
    for block in graph.blocks:
        if (
            block.kind != "table_row"
            or block.winner is None
            or any(candidate.has_invalid_geometry for candidate in block.candidates)
        ):
            continue
        code = _CODE.search(block.raw_text)
        refs = _page_refs(block.raw_text, code.end()) if code else ()
        if not code or not refs:
            continue
        pages = tuple(sorted({page for ref in refs for page in page_map.get(ref, ())}))
        entries.append(
            IndexEntry(
                tenant_id=graph.tenant_id,
                document_version_id=graph.document_version_id,
                parse_manifest_id=graph.parse_manifest_id,
                indicator_code=code.group("code"),
                printed_page_refs=refs,
                resolved_physical_pages=pages,
                link_text=block.raw_text,
                resolution_state="resolved"
                if all(ref in page_map for ref in refs)
                else "unresolved",
                source_ref=block.source_ref(),
            )
        )
    return tuple(entries)
