"""Local GRI candidate routing; index matches never establish evidence or applicability."""

import re
from dataclasses import replace
from hashlib import sha256
from io import BytesIO

import pdfplumber
from pypdf import PdfReader

from proofops.adapters.local.evidence_search import LocalEvidenceSearch
from proofops.application.ingest.gri import _validate_graph, build_gri_index


class LocalGRIRouting:
    def __init__(self, graph, original_pdf: bytes, *, tenant_id: str, pages):
        _validate_graph(graph, tenant_id)
        if sha256(original_pdf).hexdigest() != graph.source_sha256:
            raise ValueError("SOURCE_INTEGRITY_MISMATCH")
        selected = frozenset(pages)
        # PDF metadata is a candidate page map, not visual verification of printed numbers.
        # Never reuse legacy NativeSource labels: pypdf fabricates 1..N when absent.
        provisional = build_gri_index(graph, {}, tenant_id=tenant_id)
        page_map = {}
        try:
            reader = PdfReader(BytesIO(original_pdf), strict=True)
            if "/PageLabels" in reader.root_object:
                page_map = {
                    number: label
                    for number, label in enumerate(reader.page_labels, 1)
                    if number in selected and isinstance(label, str) and label.strip()
                }
        except Exception:
            # A damaged optional index cannot stop ordinary same-document search.
            page_map = {}
        if provisional:
            # ponytail: margin heuristic only supplies navigation candidates; visual/OCR
            # page-number recovery is needed for scans or numbers outside these bands.
            try:
                with pdfplumber.open(BytesIO(original_pdf)) as pdf:
                    for number in sorted(selected):
                        page = pdf.pages[number - 1]
                        labels = {
                            str(int(word["text"]))
                            for word in page.extract_words()
                            if re.fullmatch(r"[0-9]{1,3}", word["text"])
                            and (
                                word["bottom"] <= page.height * 0.10
                                or word["top"] >= page.height * 0.90
                            )
                            and (word["x1"] <= page.width * 0.15 or word["x0"] >= page.width * 0.85)
                        }
                        if len(labels) == 1:
                            visible = next(iter(labels))
                            metadata = page_map.get(number)
                            if metadata is not None and metadata.lstrip("0") != visible:
                                page_map.pop(number, None)
                            else:
                                page_map[number] = visible
                        elif labels:
                            page_map.pop(number, None)
            except Exception:
                # Preserve unresolved routing if optional page-label extraction fails.
                page_map = {}
        self.entries = tuple(
            entry
            for entry in build_gri_index(graph, page_map, tenant_id=tenant_id)
            if entry.source_ref.page_num in selected
        )
        row_ids = {entry.source_ref.source_id for entry in self.entries}
        rows = tuple(block for block in graph.blocks if block.source_id in row_ids)
        self.search = LocalEvidenceSearch(
            replace(graph, blocks=rows, edges=()),
            tenant_id=tenant_id,
            pages={block.page_num for block in rows},
            index_generation="local-gri-index-v1:" + graph.parse_manifest_id,
        )

    def for_claim(self, quote: str):
        hits = self.search.search(self.search.scope, quote).hits
        by_source = {entry.source_ref.source_id: entry for entry in self.entries}
        codes = tuple(dict.fromkeys(by_source[hit.source_id].indicator_code for hit in hits))
        return self.entries, codes
