"""Regression: bounded row-scoped lineage for verified table_row claims (TASK-011).

Source probe baseline (outputs/merged-table-20260919/lghh/source-probe.json):
12 candidates admitted, 21 omitted — whole-table expansion overflowed the 12-candidate
cap when a single row claim expanded all table siblings.

Contract: a single verified row in a fully positioned large table keeps every
preceding row as possible header/context, all intersecting merged cells, and the
full table quote as context. Later rows remain explicitly unprocessed. Unknown
structure or quality falls back to the old conservative expansion.
"""

from dataclasses import replace

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

# ---------------------------------------------------------------------------
# Corpus helpers
# ---------------------------------------------------------------------------

_TILE_COLS = 8  # unique column slots per page row
_TILE_W = 600 // _TILE_COLS
_TILE_H = 56  # 800 / 14 ≈ 57 px rows per page


def _block(
    kind,
    native_id,
    text,
    slot=0,
    *,
    row_number=None,
    column_number=None,
    row_span=None,
    column_span=None,
):
    """CandidateBlock that occupies a unique non-overlapping tile on the page.

    slot uniqueness prevents bbox collision in single-batch fusion.  Each block
    is placed in a distinct (col, row) tile so it never matches another block's
    geometry even for the same kind.
    """
    row_idx = slot // _TILE_COLS
    col_idx = slot % _TILE_COLS
    x0 = col_idx * _TILE_W + 2
    y0 = row_idx * _TILE_H + 2
    x1 = x0 + _TILE_W - 4
    y1 = y0 + _TILE_H - 4
    # Clamp to page; overflow slots share the corner (unique native_id keeps them separate).
    x0, y0, x1, y1 = max(0, x0), max(0, y0), min(600, x1), min(800, y1)
    if x1 <= x0 or y1 <= y0:
        x0, y0, x1, y1 = 560, 760, 598, 798
    return CandidateBlock(
        kind,
        NativeSource(
            VERSION,
            MANIFEST,
            RUN,
            native_id,
            36,
            None,
            (x0, y0, x1, y1),
            "pdf_bottom_left_points",
            text,
            0,
            len(text),
        ),
        PageGeometry(600, 800, 0, (0, 0, 600, 800)),
        row_number=row_number,
        column_number=column_number,
        row_span=row_span,
        column_span=column_span,
    )


def large_table_corpus(
    *,
    data_row_count=14,
    cols=3,
    claim_row_idx=1,  # index into data_rows (0 = first data row after header)
    with_row_numbers=True,
    merged_unit=False,
):
    """Table with 1 header row + data_row_count data rows, each with `cols` cells.

    Structure:
      table (tbl)
        hdr [table_row, row_number=1]
          hc0..hc{cols-1} [table_cell, row_number=1, column_number=1..cols]
        dr0 [table_row, row_number=2]
          dr0c0..dr0c{cols-1}
        ...
        dr{n-1} [table_row, row_number=n+1]

    with_row_numbers=False exercises the ambiguous-fallback path.
    """
    rn = (lambda r: r) if with_row_numbers else (lambda r: None)
    cn = (lambda c: c + 1) if with_row_numbers else (lambda c: None)

    slot = 0
    table = _block("table", "tbl", "TBL", slot)
    slot += 1

    hdr = _block("table_row", "hdr", "HDR", slot, row_number=rn(1))
    slot += 1
    hdr_cells = tuple(
        _block(
            "table_cell",
            f"hc{c}",
            f"HC{c}",
            slot + c,
            row_number=rn(1),
            column_number=cn(c),
            row_span=1,
            column_span=1,
        )
        for c in range(cols)
    )
    slot += cols

    data_rows = []
    all_data_cells = []
    for r in range(data_row_count):
        row_rn = rn(r + 2)  # header row=1, first data row=2
        dr = _block("table_row", f"dr{r}", f"DR{r}", slot, row_number=row_rn)
        slot += 1
        cells = tuple(
            _block(
                "table_cell",
                f"dr{r}c{c}",
                f"D{r}C{c}",
                slot + c,
                row_number=row_rn,
                column_number=cn(c),
                row_span=3 if merged_unit and r == 0 and c == 1 else 1,
                column_span=1,
            )
            for c in range(cols)
        )
        slot += cols
        data_rows.append(dr)
        all_data_cells.append(cells)

    # Edges: all rows → table; all cells → their row AND → table.
    edges: list[CandidateEdge] = [CandidateEdge("hdr", "tbl", "table_parent")]
    for c in range(cols):
        edges.append(CandidateEdge(f"hc{c}", "hdr", "table_parent"))
        edges.append(CandidateEdge(f"hc{c}", "tbl", "table_parent"))
    for r, (dr, cells) in enumerate(zip(data_rows, all_data_cells)):
        edges.append(CandidateEdge(f"dr{r}", "tbl", "table_parent"))
        for c in range(cols):
            edges.append(CandidateEdge(f"dr{r}c{c}", f"dr{r}", "table_parent"))
            edges.append(CandidateEdge(f"dr{r}c{c}", "tbl", "table_parent"))

    all_blocks = (
        table,
        hdr,
        *hdr_cells,
        *data_rows,
        *(c for cells in all_data_cells for c in cells),
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
        all_blocks,
        edges=tuple(edges),
        synthetic=True,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))

    # The claim is the chosen data row.
    target = next(b for b in graph.blocks if b.raw_text == f"DR{claim_row_idx}")
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


def _retrieve_bounded(graph, claim):
    """Use the unchanged production maximum of 12,000 budget units."""
    return retrieve(graph, claim, max_tokens=12_000)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRowScopeScoped:
    """Scoped path: all rows carry integer row_number → bounded selection."""

    def test_claim_row_and_header_row_cells_admitted(self):
        """The claim row (local_claim) and header row + its cells (same_table) are
        admitted as candidates when the table is large (14 data rows × 3 cols).

        Without the fix, 14 data rows × 3 cells + 14 rows = 56 same-table blocks
        would all be in lineage_ids, overflowing the 12-candidate cap.
        """
        graph, claim = large_table_corpus(data_row_count=14, cols=3)
        data = _retrieve_bounded(graph, claim).to_dict()
        cands_by_id = {c["source_id"]: c for c in data["evidence_candidates"]}

        # Claim row must appear as local_claim.
        claim_row_id = claim.source_refs[0].source_id
        assert claim_row_id in cands_by_id, "claim row missing from evidence_candidates"
        assert cands_by_id[claim_row_id]["source_scope"] == "local_claim"

        # Header row (raw_text="HDR") must be a same_table candidate.
        hdr = next(b for b in graph.blocks if b.raw_text == "HDR")
        assert hdr.source_id in cands_by_id, "header row missing from evidence_candidates"
        assert cands_by_id[hdr.source_id]["source_scope"] == "same_table"

        # Header cells (raw_text starts "HC") must all be in candidates.
        hdr_cells = [b for b in graph.blocks if b.raw_text.startswith("HC")]
        assert hdr_cells, "no header cells found in graph"
        for hc in hdr_cells:
            assert (
                hc.source_id in cands_by_id
            ), f"header cell {hc.raw_text} missing from evidence_candidates"

    def test_unrelated_data_rows_are_in_unprocessed_not_omitted(self):
        """Data rows other than the claim row must appear in unprocessed_source_ids,
        never in omitted_source_ids.

        Pre-fix: all 14 data rows competed for 12 candidate slots, so many were
        capacity-omitted (omitted_source_ids).  Post-fix: unrelated rows are excluded
        from the lineage before the candidate loop, so they land in unprocessed.
        """
        graph, claim = large_table_corpus(data_row_count=14, cols=3, claim_row_idx=3)
        data = _retrieve_bounded(graph, claim).to_dict()
        sc = data["search_coverage"]
        cands_ids = {c["source_id"] for c in data["evidence_candidates"]}
        omitted = set(sc["omitted_source_ids"])
        unprocessed = set(sc["unprocessed_source_ids"])

        # Full accounting: every block must appear in exactly one bucket.
        all_block_ids = {b.source_id for b in graph.blocks}
        assert (
            all_block_ids <= cands_ids | omitted | unprocessed
        ), f"blocks missing from accounting: {all_block_ids - (cands_ids | omitted | unprocessed)}"

        # Only LATER rows can be excluded: preceding rows may be multi-tier headers.
        claim_row_id = claim.source_refs[0].source_id
        hdr = next(b for b in graph.blocks if b.raw_text == "HDR")
        other_rows = [
            b
            for b in graph.blocks
            if b.kind == "table_row"
            and b.source_id != claim_row_id
            and b.source_id != hdr.source_id
            and b.candidates[b.winner].row_number > 5
        ]
        assert len(other_rows) == 10
        preceding = {
            b.source_id
            for b in graph.blocks
            if b.kind == "table_row" and b.candidates[b.winner].row_number <= 5
        }
        assert not preceding.intersection(sc["row_context_excluded_source_ids"])
        for row in other_rows:
            assert row.source_id not in omitted, (
                f"unrelated row {row.raw_text} is in omitted_source_ids "
                "(should be unprocessed — scoped out, not capacity-dropped)"
            )
            assert (
                row.source_id in unprocessed
            ), f"unrelated row {row.raw_text} is not in unprocessed_source_ids"

        # Similarly, cells of unrelated rows must be in unprocessed.
        other_cells = [
            b
            for b in graph.blocks
            if b.kind == "table_cell" and b.candidates[b.winner].row_number > 5
        ]
        for cell in other_cells:
            assert cell.source_id not in omitted
            assert cell.source_id in unprocessed

    def test_candidate_count_within_12_and_packet_not_blocked_by_scope(self):
        """After scoping, the candidate count must stay ≤ 12 and the packet must
        not be blocked by the overflow (status == 'candidate' when no quality issues).
        """
        graph, claim = large_table_corpus(data_row_count=14, cols=3)
        data = _retrieve_bounded(graph, claim).to_dict()
        assert len(data["evidence_candidates"]) <= 12
        assert data["status"] == "candidate", (
            f"packet unexpectedly blocked: {data['status']}. "
            f"omitted={data['search_coverage']['omitted_source_ids']}"
        )
        assert (
            data["search_coverage"]["omitted_source_ids"] == []
        ), "some block was capacity-omitted even after row-scoped lineage"

    def test_header_cell_quotes_preserved_in_source_refs(self):
        """Header cell text (e.g. year labels, unit headers) must appear verbatim as
        source_ref quotes in the admitted candidates — required context not silently stripped.
        """
        graph, claim = large_table_corpus(data_row_count=14, cols=3)
        data = _retrieve_bounded(graph, claim).to_dict()

        hdr_cell_texts = {
            b.raw_text
            for b in graph.blocks
            if b.kind == "table_cell" and b.raw_text.startswith("HC")
        }
        union_quotes = {
            ref["quote"] for c in data["evidence_candidates"] for ref in c["source_refs"]
        }
        admitted = hdr_cell_texts & union_quotes
        assert admitted == hdr_cell_texts, (
            f"no header cell quotes found in any source_ref. "
            f"Header texts: {hdr_cell_texts!r}, admitted quotes: {union_quotes!r}"
        )

    def test_claim_row_label_preserved_verbatim_in_local_claim(self):
        """The claim row's exact raw_text (row label) is preserved as the own
        source_ref quote in the local_claim candidate."""
        graph, claim = large_table_corpus(data_row_count=14, cols=3, claim_row_idx=2)
        data = _retrieve_bounded(graph, claim).to_dict()
        local = next(
            (c for c in data["evidence_candidates"] if c["source_scope"] == "local_claim"),
            None,
        )
        assert local is not None, "no local_claim candidate"
        own_refs = [r for r in local["source_refs"] if r["source_id"] == local["source_id"]]
        assert (
            own_refs and own_refs[0]["quote"] == claim.quote
        ), "claim row label not preserved verbatim in local_claim"


class TestRowScopeAmbiguousFallback:
    """Ambiguous path: row_number=None → conservative whole-table expansion retained."""

    def test_null_row_number_retains_whole_table_expansion(self):
        """When table_rows carry no integer row_number, the whole-table expansion fires.
        All table-lineage blocks must appear in one of candidates | omitted | unprocessed.
        """
        # Use a small table (3 data rows × 2 cols) to avoid budget pressure.
        graph, claim = large_table_corpus(data_row_count=3, cols=2, with_row_numbers=False)
        data = _retrieve_bounded(graph, claim).to_dict()
        sc = data["search_coverage"]
        all_ids = {b.source_id for b in graph.blocks}
        accounted = (
            {c["source_id"] for c in data["evidence_candidates"]}
            | set(sc["omitted_source_ids"])
            | set(sc["unprocessed_source_ids"])
        )
        assert all_ids <= accounted

        # The other data rows MAY appear as candidates or omitted (old behaviour);
        # they should NOT all be in unprocessed as the scoped path would produce.
        hdr = next(b for b in graph.blocks if b.raw_text == "HDR")
        claim_id = claim.source_refs[0].source_id
        other_rows = [
            b
            for b in graph.blocks
            if b.kind == "table_row" and b.source_id != claim_id and b.source_id != hdr.source_id
        ]
        # In whole-table mode, other rows are candidates (not forced to unprocessed).
        # Other rows must not all be unprocessed (which would indicate scoped mode).
        cands_ids = {c["source_id"] for c in data["evidence_candidates"]}
        other_in_cands = [r for r in other_rows if r.source_id in cands_ids]
        assert other_in_cands, (
            "With null row_numbers, other data rows must be admitted as candidates "
            "(whole-table expansion); scoped path should not have fired."
        )

    def test_table_cell_claim_never_triggers_scoped_path(self):
        """A table_cell claim (not a table_row) must never trigger the scoped path.
        The existing blocked_evidence behaviour for oversized cell bundles must be unchanged.
        """
        from tests.acceptance.test_retrieval_table_dedup import table_corpus

        graph, claim = table_corpus(claim_native_id="c0")
        # table_corpus has no row_number metadata → ambiguous fallback.
        # Claim is a table_cell → scoped path condition requires table_row claim → no scope.
        data = retrieve(graph, claim, SyntheticSearch(graph)).to_dict()
        assert data["status"] == "blocked_evidence"
        assert claim.source_refs[0].source_id in data["search_coverage"]["omitted_source_ids"]


class TestRowScopeConflictedSource:
    """Quality guards are not weakened by the scoped path."""

    def test_conflicted_claim_row_blocks_packet_and_stays_in_unprocessed(self):
        """A conflicted claim row must block the packet.  Scoped lineage must not bypass
        quality gates, and must not convert unknown quality to absent.
        """
        graph, claim = large_table_corpus(data_row_count=14, cols=3, claim_row_idx=1)
        claim_block_id = claim.source_refs[0].source_id
        graph = replace(
            graph,
            blocks=tuple(
                replace(b, quality="conflicted") if b.source_id == claim_block_id else b
                for b in graph.blocks
            ),
        )
        data = _retrieve_bounded(graph, claim).to_dict()
        assert data["status"] == "blocked_evidence"
        sc = data["search_coverage"]
        # Conflicted claim block must not appear in candidates.
        assert all(c["source_id"] != claim_block_id for c in data["evidence_candidates"])
        # Must be visible in unprocessed (not silently absent).
        assert claim_block_id in sc["unprocessed_source_ids"]
        assert sc["not_found_state"] == "unknown"

    def test_conflicted_header_row_blocks_claim_row_bundle(self):
        """A conflicted header restores conservative expansion and blocks admission."""
        graph, claim = large_table_corpus(data_row_count=14, cols=3, claim_row_idx=2)
        hdr_block = next(b for b in graph.blocks if b.raw_text == "HDR")
        graph = replace(
            graph,
            blocks=tuple(
                replace(b, quality="conflicted") if b.source_id == hdr_block.source_id else b
                for b in graph.blocks
            ),
        )
        data = _retrieve_bounded(graph, claim).to_dict()
        # Conflicted header blocks the packet (header is same_table, blocking omission).
        assert data["status"] == "blocked_evidence"
        sc = data["search_coverage"]
        # Conflicted header must remain visible in quality_issues or source_quality.
        assert hdr_block.source_id in sc["unprocessed_source_ids"] or hdr_block.source_id in sc.get(
            "source_quality", {}
        )


def test_preceding_merged_unit_survives_and_unknown_structure_falls_back():
    graph, claim = large_table_corpus(data_row_count=14, cols=3, claim_row_idx=1, merged_unit=True)
    unit = next(b for b in graph.blocks if b.raw_text == "D0C1")
    packet = retrieve(graph, claim).to_dict()
    assert packet["status"] == "candidate"
    assert unit.source_id in {c["source_id"] for c in packet["evidence_candidates"]}
    assert "D0C1" in {r["quote"] for c in packet["evidence_candidates"] for r in c["source_refs"]}
    # Missing merged-span metadata cannot authorize narrower selection.
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, candidates=tuple(replace(c, row_span=None) for c in b.candidates))
            if b.source_id == unit.source_id
            else b
            for b in graph.blocks
        ),
    )
    packet = retrieve(graph, claim).to_dict()
    assert packet["status"] == "blocked_evidence"
    assert packet["search_coverage"]["row_context_excluded_source_ids"] == []


def test_scoping_keeps_distinct_table_prose_and_excludes_only_exact_cell_duplicate():
    graph, claim = large_table_corpus(cols=2, claim_row_idx=0)
    batch = graph.candidates[0]
    distinct = _block("paragraph", "distinct", "Separate context", 100)
    duplicate = _block("paragraph", "duplicate", "HC1", 101)
    batch = replace(
        batch,
        blocks=(*batch.blocks, distinct, duplicate),
        edges=(
            *batch.edges,
            CandidateEdge("distinct", "tbl", "table_parent"),
            CandidateEdge("duplicate", "hc1", "table_parent"),
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    packet = retrieve(graph, claim).to_dict()
    ids = {c["source_id"] for c in packet["evidence_candidates"]}
    distinct_id = next(b.source_id for b in graph.blocks if b.raw_text == "Separate context")
    duplicate_id = next(
        b.source_id for b in graph.blocks if b.kind == "paragraph" and b.raw_text == "HC1"
    )
    assert packet["status"] == "candidate"
    assert distinct_id in ids
    assert duplicate_id not in ids
    assert duplicate_id in packet["search_coverage"]["row_context_excluded_source_ids"]
    assert duplicate_id in packet["search_coverage"]["unprocessed_source_ids"]
