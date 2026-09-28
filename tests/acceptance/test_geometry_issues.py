"""Invalid locations remain traceable and cannot become numeric evidence."""

from dataclasses import asdict, replace

import pytest
from proofops.application.ingest.graph_fusion import candidates_from_snapshot, fuse_candidates
from proofops.application.ingest.normalize import normalize_tables
from proofops.domain.errors import DomainValidationError

from .test_parsing import TENANT, candidate
from .test_tables import table


def test_outside_page_candidates_survive_without_a_usable_location():
    batch = candidate(
        "A",
        [
            ("bad", "paragraph", "outside crop", (-10, 10, 100, 30), ()),
            ("good", "paragraph", "visible", (10, 40, 100, 60), ()),
        ],
    )
    bad_candidate = replace(
        batch.blocks[0], parser_bbox=(-10, 10, 100, 30), parser_to_canonical=(1, 0, 0, -1, 0, 800)
    )
    batch = replace(batch, blocks=(bad_candidate, batch.blocks[1]))
    graph = fuse_candidates((batch,), tenant_id=TENANT, fusion_version=3)
    bad = next(b for b in graph.blocks if b.raw_text == "outside crop")
    good = next(b for b in graph.blocks if b.raw_text == "visible")
    assert bad.quality == "unlocated" and bad.bbox is None
    assert bad.sources[0].native_bbox == (-10, 10, 100, 30)
    assert bad.candidates[0].parser_bbox == (-10, 10, 100, 30)
    with pytest.raises(DomainValidationError):
        bad.source_ref()
    assert good.source_ref().bbox == (10, 740, 100, 760)
    (issue,) = graph.issues
    assert (issue.kind, issue.state, issue.source_ids) == (
        "source_geometry_invalid",
        "unreadable",
        (bad.source_id,),
    )
    restored = candidates_from_snapshot([asdict(batch)])
    assert fuse_candidates(restored, tenant_id=TENANT, fusion_version=3) == graph
    for legacy in (1, 2):
        with pytest.raises(ValueError):
            fuse_candidates((batch,), tenant_id=TENANT, fusion_version=legacy)
    with pytest.raises(ValueError, match="disagrees"):
        replace(bad_candidate, parser_to_canonical=(1, 0, 0, -1, 5, 800))


def test_bad_value_geometry_remains_unreadable_even_with_forged_quality():
    batch = table([["지표", "연도", "값"], ["배출량", "2025", "123"]])
    batch = replace(
        batch,
        blocks=tuple(
            replace(b, source=replace(b.source, native_bbox=(-5, 20, 119, 39)))
            if b.source.source_native_id == "r1c2"
            else b
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT, fusion_version=3)
    invalid_id = next(b.source_id for b in graph.blocks if b.raw_text == "123")
    graph = replace(graph, blocks=tuple(replace(b, quality="verified") for b in graph.blocks))
    result = normalize_tables(graph, tenant_id=TENANT)
    (observation,) = result.observations
    assert observation.value_state == "unreadable" and observation.value_decimal is None
    assert all(ref.source_id != invalid_id for ref in observation.source_refs)
    assert any(b.source_id == invalid_id for b in observation.source_blocks)


def test_invalid_gri_row_does_not_abort_valid_index_entries():
    from proofops.application.ingest.gri import build_gri_index

    batch = candidate(
        "A",
        [
            ("bad", "table_row", "GRI 305-1\tDirect emissions\t34", (-5, 10, 100, 30), ()),
            ("good", "table_row", "GRI 305-2\tIndirect emissions\t36", (10, 40, 100, 60), ()),
        ],
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    entries = build_gri_index(graph, {"34": (2,), "36": (3,)}, tenant_id=TENANT)
    assert [e.indicator_code for e in entries] == ["305-2"]
    assert entries[0].resolved_physical_pages == (3,)
    assert any(i.kind == "source_geometry_invalid" for i in graph.issues)
