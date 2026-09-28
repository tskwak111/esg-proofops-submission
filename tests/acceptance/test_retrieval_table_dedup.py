"""TASK-011 regression: shared table/row/cell context must not duplicate per bundle.

Real HMM source-selected probe (outputs/table-admission-20260919/hmm-final/
source-probe.json): the row candidate carries row+table refs, the header
candidate carries header+table refs, and 6 verified cell bundles (cell+row/table
+table, 3 refs each) exceed the conservative 1600-byte per-snippet bound, so the
packet is blocked_evidence while full table/row/headers are admitted.

Fix contract (coordinator-confirmed): retain each exact verified context ref
once across the packet, always retain each candidate's own ref, compact only
already-admitted exact refs after all quality/ancestry gates, count actual
compact JSON bytes. No limit increase, no silent clipping, no gate bypass.
"""

import json
from dataclasses import asdict, replace

from proofops.application.claims import Claim, ExtractionProfile, ExtractionReceipt
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    CandidateEdge,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry

from tests.acceptance.test_citations import MANIFEST, OTHER, RUN, TENANT, VERSION
from tests.acceptance.test_retrieval import SyntheticSearch, retrieve


def _block(kind, native_id, text, index=0):
    y0 = 10 + index * 50
    return CandidateBlock(
        kind,
        NativeSource(
            VERSION,
            MANIFEST,
            RUN,
            native_id,
            36,
            None,
            (10, y0, 300, y0 + 40),
            "pdf_bottom_left_points",
            text,
            0,
            len(text),
        ),
        PageGeometry(600, 800, 0, (0, 0, 600, 800)),
    )


def table_corpus(claim_native_id="row", cell_count=4, pad=100):
    """Table + header row + data row + cells sharing row/table ancestors."""
    table = _block("table", "tbl", "YEAR-" + "y" * (pad - 5), 0)
    header = _block("table_row", "hdr", "HDR-" + "h" * (pad - 4), 1)
    row = _block("table_row", "row", "ROW-" + "r" * (pad - 4), 2)
    cells = tuple(
        _block("table_cell", f"c{i}", f"C{i}-" + "c" * (pad - 3), 3 + i) for i in range(cell_count)
    )
    edges = (
        CandidateEdge("hdr", "tbl", "table_parent"),
        CandidateEdge("row", "tbl", "table_parent"),
        *(
            edge
            for i in range(cell_count)
            for edge in (
                CandidateEdge(f"c{i}", "row", "table_parent"),
                CandidateEdge(f"c{i}", "tbl", "table_parent"),
            )
        ),
    )
    batch = CandidateBatch(
        TENANT,
        VERSION,
        MANIFEST,
        "a" * 64,
        RUN,
        "synthetic",
        "1",
        "synthetic",
        "b" * 64,
        (table, header, row, *cells),
        edges=edges,
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    prefix = {"tbl": "YEAR-", "hdr": "HDR-", "row": "ROW-", "c0": "C0-"}[claim_native_id]
    target = next(b for b in graph.blocks if b.raw_text.startswith(prefix))
    profile = ExtractionProfile("c" * 64, "d" * 64, "e" * 64, True)
    claim = Claim(
        OTHER,
        TENANT,
        VERSION,
        MANIFEST,
        graph.source_sha256,
        target.raw_text,
        (target.source_ref(),),
        "verified",
        (),
        ExtractionReceipt(target.source_id, "f" * 64, None, None, profile, "ok"),
    )
    return graph, claim


def test_shared_table_context_is_deduplicated_not_omitted():
    graph, claim = table_corpus()
    data = retrieve(graph, claim).to_dict()
    by_id = {c["source_id"]: c for c in data["evidence_candidates"]}
    cells = [b for b in graph.blocks if b.kind == "table_cell"]

    assert data["status"] == "candidate"
    assert data["search_coverage"]["omitted_source_ids"] == []
    for cell in cells:
        assert cell.source_id in by_id, f"verified cell omitted: {cell.source_id}"
        own = [r for r in by_id[cell.source_id]["source_refs"] if r["source_id"] == cell.source_id]
        assert len(own) == 1 and own[0]["quote"] == cell.raw_text

    # No silent clipping: every admitted block quote survives somewhere in the packet.
    union_quotes = {r["quote"] for c in data["evidence_candidates"] for r in c["source_refs"]}
    for block in graph.blocks:
        assert block.raw_text in union_quotes

    # Shared context stored once when the bound requires it: each candidate
    # leads with its own ref; in-budget full bundles are untouched, oversized
    # bundles reuse already-admitted exact refs (recorded, byte-counted).
    for c in data["evidence_candidates"]:
        assert c["source_refs"][0]["source_id"] == c["source_id"]
    assert data["search_coverage"]["deduplicated_source_refs"]

    # Real accounting: per-candidate bound holds on the actual stored JSON.
    from proofops.domain.rulepacks import canonical_json

    for c in data["evidence_candidates"]:
        assert len(canonical_json(c).encode()) <= 1600


def test_oversized_claim_bundle_stays_blocked_after_dedup():
    # Contract: oversized bundles are omitted whole, never split. A claim cell
    # whose full cell+row+table bundle exceeds the per-snippet bound keeps the
    # packet blocked even though other cells deduplicate; dedup must not
    # suppress blocked status when the atomic value is unavailable.
    graph, claim = table_corpus(claim_native_id="c0")
    data = retrieve(graph, claim, SyntheticSearch(graph)).to_dict()
    assert data["status"] == "blocked_evidence"
    assert claim.source_refs[0].source_id in data["search_coverage"]["omitted_source_ids"]
    # Admitted candidates still carry complete context: row/table quotes survive.
    union_quotes = {r["quote"] for c in data["evidence_candidates"] for r in c["source_refs"]}
    row = next(b for b in graph.blocks if b.raw_text.startswith("ROW-"))
    table = next(b for b in graph.blocks if b.kind == "table")
    assert row.raw_text in union_quotes
    assert table.raw_text in union_quotes
    for c in data["evidence_candidates"]:
        assert c["source_refs"][0]["source_id"] == c["source_id"]


def test_dedup_never_bypasses_quality_gates():
    from dataclasses import replace as _replace

    graph, claim = table_corpus()
    graph = _replace(
        graph,
        blocks=tuple(
            _replace(b, quality="conflicted") if b.kind == "table" else b for b in graph.blocks
        ),
    )
    data = retrieve(graph, claim, SyntheticSearch(graph)).to_dict()
    assert data["status"] == "blocked_evidence"
    assert data["search_coverage"]["omitted_source_ids"] == []
    assert all(
        c["source_id"] not in {b.source_id for b in graph.blocks if b.kind == "table_cell"}
        for c in data["evidence_candidates"]
    )
    assert json.loads(json.dumps(data))  # packet stays canonical-serializable
    assert asdict(claim.receipt) == data["extraction_provenance"]
