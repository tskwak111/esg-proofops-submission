"""Candidate grouping contract for fuse_candidates (page/kind buckets, complete link).

The group scan is bucketed by (physical page, kind); these tests pin the behaviour that
bucketing must not change: no cross-page or cross-kind merge even at identical geometry,
complete-link membership (a block must match every member of a group, not just one), the
first-matching-group choice in creation order when several groups could accept a block,
table-parent compatibility at fusion version 4, and edge remapping onto merged blocks.
Every supported fusion version is exercised; nothing here asserts parsing accuracy.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict

import pytest
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    CandidateEdge,
    _matches,
    candidates_from_snapshot,
    fuse_candidates,
)
from proofops.domain.documents import NativeSource, PageGeometry

TENANT = "11111111-1111-4111-8111-111111111111"
VERSION = "33333333-3333-4333-8333-333333333333"
MANIFEST = "44444444-4444-4444-8444-444444444444"
RUNS = {
    "A": "55555555-5555-4555-8555-555555555551",
    "B": "55555555-5555-4555-8555-555555555552",
    "C": "55555555-5555-4555-8555-555555555553",
}
GEOMETRY = PageGeometry(600, 800, 0, (0, 0, 600, 800))
SUPPORTED = (1, 2, 3, 4)


def batch(parser: str, blocks, edges=(), family=None) -> CandidateBatch:
    """blocks: (native_id, page, kind, text, bbox, context, table_native_id)."""
    run = RUNS[parser]
    return CandidateBatch(
        tenant_id=TENANT,
        document_version_id=VERSION,
        parse_manifest_id=MANIFEST,
        source_sha256="a" * 64,
        parser_run_id=run,
        parser_name=parser,
        parser_version="synthetic-fixture",
        parser_family=family or parser,
        config_hash="b" * 64,
        synthetic=True,
        blocks=tuple(
            CandidateBlock(
                kind,
                NativeSource(
                    VERSION,
                    MANIFEST,
                    run,
                    native_id,
                    page,
                    None,
                    bbox,
                    "pdf_bottom_left_points",
                    text,
                    0,
                    len(text),
                ),
                GEOMETRY,
                context=context,
                table_native_id=table_native_id,
            )
            for native_id, page, kind, text, bbox, context, table_native_id in blocks
        ),
        edges=tuple(CandidateEdge(*edge) for edge in edges),
    )


def grouping(graph):
    """Native ids per canonical block, ordered as stored: the fusion decision itself."""
    return [
        (block.page_num, block.kind, tuple(s.source_native_id for s in block.sources))
        for block in graph.blocks
    ]


def test_identical_geometry_never_merges_across_pages_or_kinds():
    box = (0, 0, 100, 100)
    left = batch(
        "A",
        [
            ("a-p1", 1, "paragraph", "same text", box, (), None),
            ("a-p2", 2, "paragraph", "same text", box, (), None),
            ("a-h1", 1, "heading", "same text", box, (), None),
        ],
    )
    right = batch(
        "B",
        [
            ("b-p1", 1, "paragraph", "same text", box, (), None),
            ("b-p2", 2, "paragraph", "same text", box, (), None),
            ("b-h1", 1, "heading", "same text", box, (), None),
        ],
    )
    for version in SUPPORTED:
        graph = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=version)
        assert sorted(grouping(graph)) == sorted(
            [
                (1, "heading", ("a-h1", "b-h1")),
                (1, "paragraph", ("a-p1", "b-p1")),
                (2, "paragraph", ("a-p2", "b-p2")),
            ]
        ), version
        # every canonical block keeps exactly one page and one kind
        for block in graph.blocks:
            assert {s.physical_page for s in block.sources} == {block.page_num}
            assert {c.kind for c in block.candidates} == {block.kind}


def test_complete_link_keeps_a_partial_match_out_of_an_existing_group():
    # IoU(a, b) = 0.9 -> merge; IoU(c, a) = 0.88 but IoU(c, b) = 0.78 -> c must stay apart.
    first = batch("A", [("a", 1, "paragraph", "text", (0, 0, 100, 100), (), None)])
    second = batch("B", [("b", 1, "paragraph", "text", (0, 0, 100, 90), (), None)])
    third = batch("C", [("c", 1, "paragraph", "text", (0, 12, 100, 100), (), None)])
    for version in SUPPORTED:
        graph = fuse_candidates((first, second, third), tenant_id=TENANT, fusion_version=version)
        assert sorted(grouping(graph)) == sorted(
            [(1, "paragraph", ("a", "b")), (1, "paragraph", ("c",))]
        ), version
        merged = next(b for b in graph.blocks if len(b.sources) == 2)
        assert merged.independent_families == ("A", "B") and merged.winner == 0


def test_first_matching_group_in_creation_order_wins():
    # a1 and a2 do not match each other (IoU 0.67); b matches both (IoU 0.82 each), so the
    # scan must attach it to a1's group, which was created first.
    left = batch(
        "A",
        [
            ("a1", 1, "paragraph", "text", (0, 0, 100, 100), (), None),
            ("a2", 1, "paragraph", "text", (0, 20, 100, 120), (), None),
        ],
    )
    right = batch("B", [("b", 1, "paragraph", "text", (0, 10, 100, 110), (), None)])
    for version in SUPPORTED:
        graph = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=version)
        assert sorted(grouping(graph)) == sorted(
            [(1, "paragraph", ("a1", "b")), (1, "paragraph", ("a2",))]
        ), version


def test_transitive_chain_across_pages_stays_page_local_and_remaps_edges():
    # Same text and same geometry on three pages, plus a per-page table parent edge.
    pages = (1, 2, 3)
    left = batch(
        "A",
        [(f"a-t{p}", p, "table", "table", (0, 0, 200, 100), (), None) for p in pages]
        + [
            (f"a-c{p}", p, "table_cell", "cell", (0, 0, 50, 50), ("row number=1",), f"a-t{p}")
            for p in pages
        ],
        edges=[(f"a-c{p}", f"a-t{p}", "table_parent") for p in pages],
    )
    right = batch(
        "B",
        [(f"b-t{p}", p, "table", "table", (0, 0, 200, 100), (), None) for p in pages]
        + [
            (f"b-c{p}", p, "table_cell", "cell", (0, 0, 50, 50), ("row number=1",), f"b-t{p}")
            for p in pages
        ],
        edges=[(f"b-c{p}", f"b-t{p}", "table_parent") for p in pages],
    )
    for version in SUPPORTED:
        graph = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=version)
        assert sorted(grouping(graph)) == sorted(
            [(p, "table", (f"a-t{p}", f"b-t{p}")) for p in pages]
            + [(p, "table_cell", (f"a-c{p}", f"b-c{p}")) for p in pages]
        ), version
        cells = {b.page_num: b.source_id for b in graph.blocks if b.kind == "table_cell"}
        tables = {b.page_num: b.source_id for b in graph.blocks if b.kind == "table"}
        # the two parser edges per page collapse onto one canonical edge for that page
        assert sorted(graph.edges, key=lambda e: e.source_id) == sorted(
            (type(graph.edges[0])(cells[p], tables[p], "table_parent") for p in pages),
            key=lambda e: e.source_id,
        ), version


def test_version_four_requires_a_compatible_table_parent():
    # Same cell geometry/context on the same page, but the two parsers' parent tables do
    # not match: version 4 must keep the cells apart, versions 1-3 merge them as before.
    left = batch(
        "A",
        [
            ("a-t", 1, "table", "table", (0, 0, 200, 100), (), None),
            ("a-c", 1, "table_cell", "cell", (0, 0, 50, 50), (), "a-t"),
        ],
    )
    right = batch(
        "B",
        [
            ("b-t", 1, "table", "table", (0, 0, 200, 40), (), None),
            ("b-c", 1, "table_cell", "cell", (0, 0, 50, 50), (), "b-t"),
        ],
    )
    merged = sorted(
        [(1, "table", ("a-t",)), (1, "table", ("b-t",)), (1, "table_cell", ("a-c", "b-c"))]
    )
    separate = sorted(
        [
            (1, "table", ("a-t",)),
            (1, "table", ("b-t",)),
            (1, "table_cell", ("a-c",)),
            (1, "table_cell", ("b-c",)),
        ]
    )
    for version in (1, 2, 3):
        graph = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=version)
        assert sorted(grouping(graph)) == merged, version
    graph = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=4)
    assert sorted(grouping(graph)) == separate


def test_mixed_pages_and_kinds_are_deterministic_and_snapshot_stable():
    blocks_a, blocks_b = [], []
    for page in (1, 2, 3, 4):
        for index, kind in enumerate(("paragraph", "heading", "caption", "footnote")):
            offset = 120 * index
            blocks_a.append(
                (
                    f"a-{kind}-{page}",
                    page,
                    kind,
                    f"{kind} {page}",
                    (0, offset, 100, offset + 100),
                    (),
                    None,
                )
            )
            blocks_b.append(
                (
                    f"b-{kind}-{page}",
                    page,
                    kind,
                    f"{kind} {page}",
                    (0, offset, 100, offset + 90),
                    (),
                    None,
                )
            )
    left, right = batch("A", blocks_a), batch("B", blocks_b)
    expected = sorted(
        (page, kind, (f"a-{kind}-{page}", f"b-{kind}-{page}"))
        for page in (1, 2, 3, 4)
        for kind in ("paragraph", "heading", "caption", "footnote")
    )
    for version in SUPPORTED:
        graph = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=version)
        assert sorted(grouping(graph)) == expected, version
        again = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=version)
        assert [b.source_id for b in again.blocks] == [b.source_id for b in graph.blocks]
        assert again.to_dict() == graph.to_dict()
        restored = candidates_from_snapshot([asdict(b) for b in (left, right)])
        assert (
            fuse_candidates(restored, tenant_id=TENANT, fusion_version=version).to_dict()
            == graph.to_dict()
        )


def test_unsupported_fusion_version_is_still_rejected():
    only = batch("A", [("a", 1, "paragraph", "text", (0, 0, 100, 100), (), None)])
    for version in (0, 5, True, 3.0):
        with pytest.raises(ValueError, match="unsupported fusion version"):
            fuse_candidates((only,), tenant_id=TENANT, fusion_version=version)


def count_bbox_reads(monkeypatch) -> Counter[str]:
    """Count CandidateBlock.bbox property evaluations per block, keeping the real value."""
    original = CandidateBlock.bbox
    counts: Counter[str] = Counter()

    def fget(self):
        counts[self.source.source_native_id] += 1
        return original.fget(self)

    monkeypatch.setattr(CandidateBlock, "bbox", property(fget))
    return counts


def test_matches_projects_each_operand_bbox_once_and_keeps_short_circuiting(monkeypatch):
    """`bbox` recomputes the canonical projection on every read, so `_matches` must read it
    once per operand (it used to read each one twice: guard, then IoU) and must still not
    read it at all when kind or page already rejects the pair, nor read the right operand
    when the left one has no canonical bbox."""
    left = batch(
        "A",
        [
            ("a-p1", 1, "paragraph", "text", (0, 0, 100, 100), (), None),
            ("a-p2", 2, "paragraph", "text", (0, 0, 100, 100), (), None),
            ("a-h1", 1, "heading", "text", (0, 0, 100, 100), (), None),
            ("a-unlocated", 1, "paragraph", "text", None, (), None),
        ],
    )
    right = batch("B", [("b-p1", 1, "paragraph", "text", (0, 0, 100, 95), (), None)])
    blocks = {block.source.source_native_id: block for block in left.blocks + right.blocks}
    assert blocks["a-unlocated"].bbox is None and blocks["a-p1"].bbox is not None
    counts = count_bbox_reads(monkeypatch)
    for version in SUPPORTED:
        counts.clear()  # IoU 0.95 -> a real match, both operands projected exactly once
        assert _matches(blocks["a-p1"], blocks["b-p1"], fusion_version=version) is True
        assert counts == {"a-p1": 1, "b-p1": 1}, (version, counts)
        for rejected_first in ("a-p2", "a-h1"):  # page / kind mismatch touches no bbox
            counts.clear()
            assert _matches(blocks[rejected_first], blocks["b-p1"], fusion_version=version) is False
            assert counts == {}, (version, rejected_first, counts)
        counts.clear()  # unlocated left: read once, right operand never evaluated
        assert _matches(blocks["a-unlocated"], blocks["b-p1"], fusion_version=version) is False
        assert counts == {"a-unlocated": 1}, (version, counts)
