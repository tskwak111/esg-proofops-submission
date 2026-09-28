"""Explicit extraction bindings over real graph cells; no semantic guessing."""

import pytest
from proofops.application.ingest import normalize as module
from proofops.application.ingest.graph_fusion import fuse_candidates

from .test_parsing import TENANT
from .test_tables import table


def setup_case(footnote=None):
    graph = fuse_candidates(
        (
            table(
                [
                    ["Scope 1·2 배출량", None, "단위", "2024", "2025"],
                    ["Scope 1·2 배출량", "글로벌", "tCO2e", "9,335,444", "8,889,779"],
                    [None, "국내", "tCO2e", "8,314,440", "7,764,396"],
                ],
                footnote=footnote,
            ),
        ),
        tenant_id=TENANT,
    )
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    binding = {
        name: ids[key]
        for name, key in {
            "metric_raw": "r1c0",
            "subject": "r1c1",
            "unit_raw": "r1c2",
            "reporting_period": "r0c4",
            "value_raw": "r1c4",
        }.items()
    }
    return graph, ids, binding


def test_explicit_title_table_bindings_preserve_source_and_stay_unverified():
    graph, ids, binding = setup_case()
    result = module.normalize_table_bindings(
        graph, table_id=ids["T"], bindings=(binding,), tenant_id=TENANT
    )
    (item,) = result.observations
    assert (item.metric_raw, item.subject, item.reporting_period, item.value_decimal) == (
        "Scope 1·2 배출량",
        "글로벌",
        "2025",
        "8889779",
    )
    assert item.quality == "unverified"
    assert {r.source_id for r in item.source_refs} == set(binding.values())
    assert all(r.verification_state == "candidate" for r in item.source_refs)
    assert module.normalize_tables(graph, tenant_id=TENANT).observations == ()


@pytest.mark.parametrize("change", ["wrong_row", "unknown_field", "unknown_cell", "wrong_tenant"])
def test_bad_explicit_bindings_are_rejected(change):
    graph, ids, binding = setup_case()
    tenant = TENANT
    if change == "wrong_row":
        binding["subject"] = ids["r2c1"]
    elif change == "unknown_field":
        binding["grade"] = ids["r1c4"]
    elif change == "unknown_cell":
        binding["metric_raw"] = "missing"
    else:
        tenant = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(ValueError):
        module.normalize_table_bindings(
            graph, table_id=ids["T"], bindings=(binding,), tenant_id=tenant
        )


def test_explicit_binding_keeps_invalid_geometry_unreadable():
    from dataclasses import replace

    batch = table([["Heading", "2025"], ["Emissions", "123"]])
    batch = replace(
        batch,
        blocks=tuple(
            replace(b, source=replace(b.source, native_bbox=(-5, 20, 119, 39)))
            if b.source.source_native_id == "r1c1"
            else b
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    result = module.normalize_table_bindings(
        graph,
        table_id=ids["T"],
        tenant_id=TENANT,
        bindings=(
            {"metric_raw": ids["r1c0"], "reporting_period": ids["r0c1"], "value_raw": ids["r1c1"]},
        ),
    )
    (item,) = result.observations
    assert item.value_state == "unreadable" and item.value_decimal is None
    assert ids["r1c1"] not in {ref.source_id for ref in item.source_refs}
    assert any(issue.kind == "source_geometry_invalid" for issue in result.conflicts)


def test_explicit_binding_rejects_wrong_year_column_and_duplicate_values():
    graph, ids, binding = setup_case()
    with pytest.raises(ValueError, match="row/column"):
        module.normalize_table_bindings(
            graph,
            table_id=ids["T"],
            tenant_id=TENANT,
            bindings=({**binding, "reporting_period": ids["r0c3"]},),
        )
    with pytest.raises(ValueError, match="duplicate"):
        module.normalize_table_bindings(
            graph, table_id=ids["T"], tenant_id=TENANT, bindings=(binding, binding)
        )


@pytest.mark.parametrize("axis", ["row", "column"])
@pytest.mark.parametrize("covered", [False, True])
def test_binding_must_cover_entire_merged_value(axis, covered):
    spans = {(1, 1): (2, 1)} if axis == "row" else {(1, 1): (1, 2)}
    if covered:
        spans[(1, 0) if axis == "row" else (0, 1)] = (2, 1) if axis == "row" else (1, 2)
    rows = [["Metric", "2024", "2025"], ["Emissions", "123", None], ["Waste", None, None]]
    if covered:
        rows[2 if axis == "row" else 0][0 if axis == "row" else 2] = None
    graph = fuse_candidates(
        (
            table(
                rows,
                spans=spans,
            ),
        ),
        tenant_id=TENANT,
    )
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}

    def run():
        return module.normalize_table_bindings(
            graph,
            table_id=ids["T"],
            tenant_id=TENANT,
            bindings=(
                {
                    "metric_raw": ids["r1c0"],
                    "reporting_period": ids["r0c1"],
                    "value_raw": ids["r1c1"],
                },
            ),
        )

    if covered:
        (observation,) = run().observations
        assert observation.quality == "unverified"
        assert observation.reporting_period == "2024"
    else:
        with pytest.raises(ValueError, match="row/column"):
            run()


@pytest.mark.parametrize("target,expected", [("r1c0", True), ("r0c4", True), ("r0c3", False)])
def test_explicit_binding_inherits_only_its_own_field_notes(target, expected):
    from dataclasses import replace

    note = "해외 사업장 제외"
    graph, ids, binding = setup_case(footnote=note)
    graph = replace(
        graph,
        edges=tuple(
            replace(edge, target_id=ids[target]) if edge.relation == "footnote_of" else edge
            for edge in graph.edges
        ),
    )
    item = module.normalize_table_bindings(
        graph, table_id=ids["T"], bindings=(binding,), tenant_id=TENANT
    ).observations[0]
    assert item.footnotes == ((note,) if expected else ())
    assert (ids["F"] in {ref.source_id for ref in item.source_refs}) is expected
