"""AT-003: actual local PDF parsers and explicitly synthetic conflict candidates."""

import os
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from proofops.domain.documents import NativeSource, PageGeometry
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, RectangleObject

TENANT = "11111111-1111-4111-8111-111111111111"
FOREIGN = "22222222-2222-4222-8222-222222222222"
VERSION = "33333333-3333-4333-8333-333333333333"
MANIFEST = "44444444-4444-4444-8444-444444444444"
_LOCAL_JAVA = Path("/opt/homebrew/opt/openjdk@21/bin/java")
JAVA = os.environ.get("PROOFOPS_TEST_JAVA") or (
    str(_LOCAL_JAVA) if _LOCAL_JAVA.is_file() else "java"
)


def candidate(parser, blocks, edges=(), family=None):
    from proofops.application.ingest.graph_fusion import (
        CandidateBatch,
        CandidateBlock,
        CandidateEdge,
    )

    run = str(uuid4())
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
                    1,
                    None,
                    bbox,
                    "pdf_bottom_left_points",
                    text,
                    0,
                    len(text),
                ),
                PageGeometry(600, 800, 0, (0, 0, 600, 800)),
                context=context,
            )
            for native_id, kind, text, bbox, context in blocks
        ),
        edges=tuple(CandidateEdge(*edge) for edge in edges),
    )


def test_numeric_disagreement_preserves_all_candidates_without_winner():
    from proofops.application.ingest.graph_fusion import fuse_candidates

    primary = candidate(
        "opendataloader", [("A1", "table_cell", "1,234 tCO2e", (10, 10, 100, 30), ())]
    )
    vision = candidate("vision", [("B1", "table_cell", "1,284 tCO2e", (10, 10, 100, 30), ())])
    result = fuse_candidates((primary, vision), tenant_id=TENANT)
    block = result.blocks[0]
    assert block.quality == "conflicted" and block.winner is None
    assert {source.raw_text for source in block.sources} == {"1,234 tCO2e", "1,284 tCO2e"}
    assert block.raw_text == ""  # no unapproved winner reaches canonical evidence text
    assert result.issues[0].kind == "parse_conflict"
    assert result.issues[0].state == "open"
    with pytest.raises(ValueError, match="conflict"):
        block.source_ref()
    assert result.to_dict() == fuse_candidates((vision, primary), tenant_id=TENANT).to_dict()
    assert result.candidates == (primary, vision) or result.candidates == (vision, primary)


def test_alias_edges_distinct_regions_and_parser_family_are_preserved():
    from proofops.application.ingest.graph_fusion import fuse_candidates

    a = candidate("A", [("A1", "table", "합계 1,234", (10, 10, 100, 30), ())], family="same-engine")
    b = candidate(
        "B",
        [
            ("B1", "table", "합계 1,234", (10, 10, 100, 30), ()),
            ("B2", "table_row", "세부", (10, 35, 100, 50), ()),
            ("B3", "table", "합계 1,234", (200, 10, 300, 30), ()),
        ],
        [("B2", "B1", "derived_from")],
        family="same-engine",
    )
    result = fuse_candidates((a, b), tenant_id=TENANT)
    assert len(result.blocks) == 3
    merged = next(block for block in result.blocks if len(block.sources) == 2)
    assert merged.independent_families == ("same-engine",)
    assert merged.quality == "unverified"
    assert result.edges[0].target_id == merged.source_id
    identifiers = {block.source_id for block in result.blocks}
    assert all(
        edge.source_id in identifiers and edge.target_id in identifiers for edge in result.edges
    )
    assert merged.source_ref().verification_state == "candidate"


def test_kogas_style_shifted_grid_cells_merge_without_merging_tables_or_bundles():
    """KOGAS physical-68 pattern, fully synthetic geometry.

    One parser emits a full grid, the other drops the first column (so its
    column numbers shift by one) and bundles one year column into a tall
    multi-row cell with header/year-column loss. Co-located cells must fuse
    across the shifted grid; overlapping tables, the tall bundle, and rows
    with different column coverage must stay separate; same-region value
    disagreement must stay an unresolved conflict.
    """
    from proofops.application.ingest.graph_fusion import fuse_candidates

    full = candidate(
        "opendataloader",
        [
            ("OD-T", "table", "header", (57, 282, 550, 440), ()),
            ("OD-R1", "table_row", "header row", (57, 400, 550, 418), ("row number=1",)),
            ("OD-R2", "table_row", "value row", (57, 385, 550, 400), ("row number=2",)),
            ("OD-R3", "table_row", "other row", (57, 370, 550, 385), ("row number=3",)),
            (
                "OD-H21",
                "table_cell",
                "2021년",
                (266, 400, 337, 418),
                ("row number=1", "column number=3"),
            ),
            (
                "OD-H22",
                "table_cell",
                "2022년",
                (337, 400, 408, 418),
                ("row number=1", "column number=4"),
            ),
            (
                "OD-V17",
                "table_cell",
                "17",
                (266, 385, 337, 400),
                ("row number=2", "column number=3"),
            ),
            (
                "OD-V16",
                "table_cell",
                "16",
                (408, 385, 479, 400),
                ("row number=2", "column number=5"),
            ),
            (
                "OD-V19",
                "table_cell",
                "19",
                (338, 370, 409, 385),
                ("row number=3", "column number=4"),
            ),
        ],
        [
            ("OD-H21", "OD-R1", "table_parent"),
            ("OD-H22", "OD-R1", "table_parent"),
            ("OD-V17", "OD-R2", "table_parent"),
            ("OD-V16", "OD-R2", "table_parent"),
            ("OD-V19", "OD-R3", "table_parent"),
            ("OD-R1", "OD-T", "table_parent"),
            ("OD-R2", "OD-T", "table_parent"),
            ("OD-R3", "OD-T", "table_parent"),
        ],
        family="opendataloader",
    )
    fragmented = candidate(
        "pdfplumber",
        [
            ("PL-T", "table", "header", (211, 281, 552, 439), ()),
            ("PL-R1", "table_row", "header row", (211, 400, 552, 418), ("row number=1",)),
            ("PL-R2", "table_row", "value row", (211, 385, 552, 400), ("row number=2",)),
            (
                "PL-H21",
                "table_cell",
                "2021년",
                (267, 399, 338, 419),
                ("row number=1", "column number=2"),
            ),
            (
                "PL-V17",
                "table_cell",
                "17",
                (267, 384, 338, 399),
                ("row number=2", "column number=2"),
            ),
            (
                "PL-V16X",
                "table_cell",
                "61",
                (408, 385, 479, 400),
                ("row number=2", "column number=4"),
            ),
            (
                "PL-BUNDLE",
                "table_cell",
                "2022년\n19",
                (338, 322, 410, 434),
                ("row number=1", "column number=3"),
            ),
        ],
        [
            ("PL-H21", "PL-R1", "table_parent"),
            ("PL-V17", "PL-R2", "table_parent"),
            ("PL-V16X", "PL-R2", "table_parent"),
            ("PL-BUNDLE", "PL-R1", "table_parent"),
            ("PL-R1", "PL-T", "table_parent"),
            ("PL-R2", "PL-T", "table_parent"),
        ],
        family="pdfminer",
    )
    result = fuse_candidates((full, fragmented), tenant_id=TENANT)
    # Every native candidate is retained; nothing is dropped or invented.
    assert sum(len(block.candidates) for block in result.blocks) == 9 + 7
    by_text = {}
    for block in result.blocks:
        for source in block.sources:
            by_text.setdefault(source.raw_text, []).append(block)

    header = by_text["2021년"][0]
    assert len(header.candidates) == 2  # fused across the shifted grid
    assert header.independent_families == ("opendataloader", "pdfminer")
    assert header.quality == "unverified" and header.winner == 0
    assert header.raw_text == "2021년"

    agreed_value = next(block for block in by_text["17"] if len(block.candidates) == 2)
    assert agreed_value.quality == "unverified" and agreed_value.raw_text == "17"

    # Same region, different value: unresolved conflict, no winner, both kept.
    conflict = next(
        block
        for block in result.blocks
        if {source.raw_text for source in block.sources} == {"16", "61"}
    )
    assert len(conflict.candidates) == 2 and conflict.quality == "conflicted"
    assert conflict.winner is None and conflict.raw_text == ""
    assert any(issue.kind == "parse_conflict" and issue.state == "open" for issue in result.issues)
    with pytest.raises(ValueError, match="conflict"):
        conflict.source_ref()

    # The tall bundled cell never merges into a header or a value cell.
    bundle = by_text["2022년\n19"][0]
    assert len(bundle.candidates) == 1 and bundle.quality == "unverified"
    assert by_text["2022년"][0] is not bundle  # full-grid header stays separate

    # Overlapping tables with different column coverage stay separate.
    tables = [block for block in result.blocks if block.kind == "table"]
    assert len(tables) == 2 and all(len(block.candidates) == 1 for block in tables)

    # Table lineage is preserved on both sides with no dangling edge.
    identifiers = {block.source_id for block in result.blocks}
    assert all(
        edge.source_id in identifiers and edge.target_id in identifiers for edge in result.edges
    )
    row_ids = {
        block.source_id
        for block in result.blocks
        if block.kind == "table_row" and len(block.candidates) == 1
    }
    parents = {edge.target_id for edge in result.edges if edge.source_id == header.source_id}
    assert len(parents) == 2 and parents <= row_ids
    assert result.to_dict() == fuse_candidates((fragmented, full), tenant_id=TENANT).to_dict()


def test_fusion_version_gate_keeps_v1_legacy_and_v2_semantic_context():
    """fusion_version selects matching behavior; v2 ignores grid indices only.

    Same-region cells with shifted row/column numbers merge under v2 but stay
    separate under v1. Cells with different non-grid (semantic/scope) context
    never merge, even under v2. Unknown versions are rejected fail-closed.
    """
    from proofops.application.ingest.graph_fusion import fuse_candidates

    left = candidate(
        "opendataloader",
        [
            (
                "L1",
                "table_cell",
                "17",
                (266, 385, 337, 400),
                ("scope=A", "row number=2", "column number=3"),
            ),
            (
                "L2",
                "table_cell",
                "17",
                (408, 385, 479, 400),
                ("scope=A", "row number=2", "column number=5"),
            ),
        ],
        family="opendataloader",
    )
    right = candidate(
        "pdfplumber",
        [
            (
                "R1",
                "table_cell",
                "17",
                (267, 384, 338, 399),
                ("scope=A", "row number=2", "column number=2"),
            ),
            (
                "R2",
                "table_cell",
                "17",
                (408, 385, 479, 400),
                ("scope=B", "row number=2", "column number=4"),
            ),
        ],
        family="pdfminer",
    )
    legacy = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=1)
    assert len(legacy.blocks) == 4  # shifted grids never merge under v1
    assert all(len(block.candidates) == 1 for block in legacy.blocks)

    fixed = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=2)
    merged = [block for block in fixed.blocks if len(block.candidates) == 2]
    assert len(merged) == 1  # only the scope=A pair fuses
    assert merged[0].independent_families == ("opendataloader", "pdfminer")
    assert merged[0].raw_text == "17" and merged[0].quality == "unverified"
    scoped = [block for block in fixed.blocks if len(block.candidates) == 1]
    assert len(scoped) == 2  # scope=A vs scope=B stays separate under v2

    single = candidate("A", [("A1", "table_cell", "17", (10, 10, 100, 30), ())])
    for bad in (0, 5, -1, "2", None, True):
        with pytest.raises(ValueError, match="fusion version"):
            fuse_candidates((single,), tenant_id=TENANT, fusion_version=bad)


def test_tenant_version_dangling_edges_and_unlocated_geometry_fail_closed():
    from proofops.application.ingest.graph_fusion import fuse_candidates

    a = candidate("A", [("A1", "paragraph", "2030 target", None, ())])
    result = fuse_candidates((a,), tenant_id=TENANT)
    assert result.blocks[0].bbox is None and result.blocks[0].quality == "unlocated"
    assert result.blocks[0].source_ref().location_quality == "unlocated"
    with pytest.raises(ValueError):
        fuse_candidates((a,), tenant_id=FOREIGN)
    with pytest.raises(ValueError):
        fuse_candidates((a, replace(a, document_version_id=str(uuid4()))), tenant_id=TENANT)
    bad = candidate(
        "bad", [("A1", "paragraph", "2030 target", None, ())], [("A1", "missing", "derived_from")]
    )
    with pytest.raises(ValueError, match="edge"):
        fuse_candidates((bad,), tenant_id=TENANT)


def pdf(*, table=False, crop_rotate=False):
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for number in range(1, 5 if crop_rotate else 4):
        page = writer.add_blank_page(width=600, height=800)
        if crop_rotate:
            page.cropbox = RectangleObject([50, 100, 550, 750])
            page.rotate({1: 0, 2: 90, 3: 180, 4: 270}[number])
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        commands = [f"BT /F1 12 Tf 72 720 Td (Page {number} emissions 1234 tCO2e) Tj ET"]
        if table and number == 2:
            commands += ["0.5 w"] + [f"72 {y} m 350 {y} l S" for y in (600, 640, 680)]
            commands += [f"{x} 600 m {x} 680 l S" for x in (72, 210, 350)]
            commands += [
                f"BT /F1 12 Tf {x} {y} Td ({text}) Tj ET"
                for x, y, text in [
                    (80, 655, "Year"),
                    (220, 655, "Emissions"),
                    (80, 615, "2025"),
                    (220, 615, "1234 tCO2e"),
                ]
            ]
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def source(content):
    from proofops.application.ingest.graph_fusion import SourceArtifact

    return SourceArtifact(
        TENANT,
        str(uuid4()),
        VERSION,
        sha256(content).hexdigest(),
        "local-synthetic:source-1",
        content,
        synthetic=True,
    )


def test_actual_opendataloader_text_and_tables_keep_physical_pages_and_artifacts(tmp_path):
    import json

    from jsonschema import Draft202012Validator, FormatChecker
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.ingest.graph_fusion import ParserProfile

    item = source(pdf(table=True))
    profile = ParserProfile(MANIFEST, physical_pages=(2,), java_executable=JAVA)
    result = OpenDataLoaderParser(tmp_path).parse(item, profile, tenant_id=TENANT)
    assert {block.page_num for block in result.blocks} == {2}
    assert any("Page 2 emissions 1234" in block.raw_text for block in result.blocks)
    assert any(
        block.kind == "table_cell" and block.raw_text == "1234 tCO2e" for block in result.blocks
    )
    assert {batch.parser_name for batch in result.candidates} == {"opendataloader", "pdfplumber"}
    assert any(len(block.sources) == 2 and block.kind == "table_cell" for block in result.blocks)
    assert result.validation_profile == "fast_preview"
    assert any(issue.kind == "table_vision_not_run" for issue in result.issues)
    manifest_path = tmp_path / TENANT / VERSION / MANIFEST / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert (
        manifest["source_sha256"] == item.sha256
        and manifest["object_version_id"] == item.object_version_id
    )
    assert manifest["parser_version"] == "2.5.7" and manifest["synthetic_source"] is True
    for filename, digest in manifest["artifacts"].items():
        assert sha256((manifest_path.parent / filename).read_bytes()).hexdigest() == digest
    assert "source.json" in manifest["artifacts"] and "source.md" in manifest["artifacts"]
    schema = json.loads(Path("contracts/jsonschema/document_graph.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(result.to_dict())
    with pytest.raises(ValueError, match="EXISTS"):
        OpenDataLoaderParser(tmp_path).parse(item, profile, tenant_id=TENANT)


def test_parser_rejects_foreign_hash_timeout_and_out_of_range_pages_without_success(tmp_path):
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.ingest.graph_fusion import ParserProfile

    parser = OpenDataLoaderParser(tmp_path)
    item = source(pdf())
    profile = ParserProfile(MANIFEST, java_executable=JAVA)
    for bad_source, bad_profile, tenant in [
        (item, profile, FOREIGN),
        (replace(item, sha256="0" * 64), profile, TENANT),
        (item, replace(profile, physical_pages=(4,)), TENANT),
        (item, replace(profile, timeout_seconds=0.000001), TENANT),
    ]:
        with pytest.raises(ValueError):
            parser.parse(bad_source, bad_profile, tenant_id=tenant)
    assert not list(tmp_path.rglob("manifest.json"))


def test_actual_cropped_and_rotated_coordinates_are_not_transformed_twice(tmp_path):
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.ingest.graph_fusion import ParserProfile

    graph = OpenDataLoaderParser(tmp_path).parse(
        source(pdf(crop_rotate=True)),
        ParserProfile(MANIFEST, physical_pages=(1, 2, 3, 4), java_executable=JAVA),
        tenant_id=TENANT,
    )
    first = next(
        block for block in graph.blocks if block.page_num == 1 and "Page 1" in block.raw_text
    )
    rotated = next(
        block for block in graph.blocks if block.page_num == 3 and "1234" in block.raw_text
    )
    assert first.bbox[0] == pytest.approx(22)
    assert 18 < first.bbox[1] < 34
    assert first.sources[0].native_bbox[0] == pytest.approx(72)
    assert rotated.bbox[2] == pytest.approx(478)
    assert 617 < rotated.bbox[1] < 634
    assert first.to_dict()["provenance"][0]["native_bbox"][0] == pytest.approx(22)
    assert first.source_ref().bbox == first.bbox
    assert rotated.source_ref().bbox == rotated.bbox
    for page in (2, 4):
        extracted = [block for block in graph.blocks if block.page_num == page and block.raw_text]
        if extracted:
            assert extracted[0].sources[0].native_bbox[0] == pytest.approx(72)
        else:
            assert any(
                issue.page_num == page and issue.state == "unreadable" for issue in graph.issues
            )


def test_actual_concurrent_parse_manifests_publish_once_without_overwrite(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
    from proofops.application.ingest.graph_fusion import ParserProfile

    item, profile = source(pdf()), ParserProfile(MANIFEST, java_executable=JAVA)
    barrier = Barrier(2)

    def run():
        barrier.wait()
        try:
            return OpenDataLoaderParser(tmp_path).parse(item, profile, tenant_id=TENANT)
        except ParseFailure as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(), range(2)))
    assert sum(isinstance(result, str) for result in results) == 1
    assert "PARSE_MANIFEST_EXISTS" in results
    assert len(list(tmp_path.rglob("manifest.json"))) == 1
    assert not list(tmp_path.rglob(".parse-*"))


def test_parser_provenance_is_immutable_and_cannot_disagree_with_canonical_geometry():
    block = candidate("A", [("A1", "paragraph", "1234", (10, 10, 100, 30), ())]).blocks[0]
    bbox, matrix = [10, 10, 100, 30], [1, 0, 0, -1, 0, 800]
    frozen = replace(block, parser_bbox=bbox, parser_to_canonical=matrix)
    bbox[0], matrix[4] = 999, 999
    assert frozen.parser_bbox == (10, 10, 100, 30)
    assert frozen.parser_to_canonical == (1, 0, 0, -1, 0, 800)
    for changes in (
        {"parser_bbox": (0, 0, 0, 0)},
        {"parser_to_canonical": (1, 0, 0, -1, float("nan"), 800)},
        {"parser_bbox": (10, 10, 100, 30), "parser_to_canonical": (1, 0, 0, -1, 5, 800)},
    ):
        with pytest.raises(ValueError):
            replace(block, **changes)


def test_actual_blank_page_and_resource_limits_do_not_become_successful_evidence(tmp_path):
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser, ParseFailure
    from proofops.application.ingest.graph_fusion import ParserProfile

    writer, output = PdfWriter(), BytesIO()
    writer.add_blank_page(width=600, height=800)
    writer.write(output)
    parser = OpenDataLoaderParser(tmp_path)
    graph = parser.parse(
        source(output.getvalue()), ParserProfile(MANIFEST, java_executable=JAVA), tenant_id=TENANT
    )
    assert not graph.blocks
    assert any(
        issue.kind == "no_extractable_text" and issue.state == "unreadable"
        for issue in graph.issues
    )
    for limit in ({"max_output_bytes": 1}, {"memory_bytes": 1}):
        identifier = str(uuid4())
        with pytest.raises(ParseFailure):
            parser.parse(
                source(pdf(table=True)),
                ParserProfile(identifier, java_executable=JAVA, **limit),
                tenant_id=TENANT,
            )
        assert not (tmp_path / TENANT / VERSION / identifier).exists()
    assert not list(tmp_path.rglob(".parse-*"))


def test_v4_keeps_same_location_cells_in_incompatible_parent_tables_separate():
    from dataclasses import replace

    from proofops.application.ingest.graph_fusion import fuse_candidates

    left = candidate(
        "primary",
        [
            ("T", "table", "metric year value", (10, 100, 400, 400), ()),
            ("C", "table_cell", "25", (100, 200, 150, 230), ()),
        ],
        [("C", "T", "table_parent")],
    )
    right = candidate(
        "partial",
        [
            ("T", "table", "value only", (90, 190, 160, 240), ()),
            ("C", "table_cell", "25", (100, 200, 150, 230), ()),
        ],
        [("C", "T", "table_parent")],
    )

    def scoped(batch):
        return replace(batch, blocks=tuple(replace(b, table_native_id="T") for b in batch.blocks))

    left, right = scoped(left), scoped(right)
    legacy = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=3)
    assert len([b for b in legacy.blocks if b.kind == "table_cell"]) == 1
    fixed = fuse_candidates((left, right), tenant_id=TENANT, fusion_version=4)
    cells = [b for b in fixed.blocks if b.kind == "table_cell"]
    assert len(cells) == 2
    assert all(len([e for e in fixed.edges if e.source_id == c.source_id]) == 1 for c in cells)
