"""AT-004: synthetic tables through real graph fusion and normalization; no model calls."""

import json
from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from proofops.application.ingest.graph_fusion import fuse_candidates

from .test_parsing import FOREIGN, TENANT, candidate


def table(rows, *, parser="A", spans=None, footnote=None):
    """Explicit synthetic parser output with row/column and parent edges."""
    spans = spans or {}
    blocks = [("T", "table", "Synthetic table", (0, 0, 590, 790), ())]
    edges = []
    for row, values in enumerate(rows):
        for col, value in enumerate(values):
            if value is None:
                continue
            identifier = f"r{row}c{col}"
            blocks.append(
                (
                    identifier,
                    "table_cell",
                    value,
                    (col * 40 + 1, row * 20 + 1, col * 40 + 39, row * 20 + 19),
                    (f"row number={row}", f"column number={col}"),
                )
            )
            edges.append((identifier, "T", "table_parent"))
    if footnote:
        blocks.append(("F", "footnote", footnote, (1, 400, 500, 420), ()))
        edges.append(("F", "T", "footnote_of"))
    batch = candidate(parser, blocks, edges)
    cells = []
    for block in batch.blocks:
        if block.kind == "table_cell":
            row, col = (int(item.split("=")[1]) for item in block.context)
            rs, cs = spans.get((row, col), (1, 1))
            block = replace(
                block,
                table_native_id="T",
                row_number=row,
                column_number=col,
                row_span=rs,
                column_span=cs,
            )
        cells.append(block)
    return replace(batch, blocks=tuple(cells))


def normalize(*batches):
    from proofops.application.ingest.normalize import normalize_tables

    return normalize_tables(fuse_candidates(batches, tenant_id=TENANT), tenant_id=TENANT)


def test_different_year_entity_and_scope2_basis_remain_separate_with_provenance():
    batch = table(
        [
            ["지표", "Scope", "사업장", "연도", "산정방식", "단위", "값"],
            ["배출량", "Scope 2", "서울", "2024", "시장기반", "천 tCO2e", "1.25"],
            ["배출량", "Scope 2", "서울", "2025", "시장기반", "천 tCO2e", "1.25"],
            ["배출량", "Scope 2", "부산", "2025", "시장기반", "천 tCO2e", "1.25"],
            ["배출량", "Scope 2", "부산", "2025", "위치기반", "천 tCO2e", "1.25"],
        ]
    )
    result = normalize(batch)
    assert len(result.observations) == 4
    assert {(o.reporting_period, o.subject, o.scope2_basis) for o in result.observations} == {
        ("2024", "서울", "시장기반"),
        ("2025", "서울", "시장기반"),
        ("2025", "부산", "시장기반"),
        ("2025", "부산", "위치기반"),
    }
    assert len({o.observation_id for o in result.observations}) == 4
    for observation in result.observations:
        assert observation.value_decimal == "1250.00"
        assert observation.value_raw == "1.25" and observation.scale_multiplier == "1000"
        assert observation.unit_raw == "천 tCO2e" and observation.unit_canonical == "tCO2e"
        assert observation.tenant_id == TENANT and observation.source_sha256 == "a" * 64
        assert observation.quality == "unverified"
        assert observation.source_refs and observation.source_blocks
        assert all(ref.verification_state == "candidate" for ref in observation.source_refs)
        assert all(
            ref.document_version_id == batch.document_version_id and ref.bbox is not None
            for ref in observation.source_refs
        )
    assert not result.conflicts
    assert result == normalize(batch)
    with pytest.raises(FrozenInstanceError):
        result.observations[0].value_decimal = "999"


@pytest.mark.parametrize(
    ("raw", "state", "value"),
    [
        ("-", "missing", None),
        ("", "missing", None),
        ("N/A", "missing", None),
        ("unknown", "unreadable", None),
        ("1,2", "unreadable", None),
        ("NaN", "unreadable", None),
        ("0", "value", "0"),
        ("1,234.50", "value", "1234.50"),
        ("123456789012345678901234567890.123", "value", "123456789012345678901234567890.123"),
    ],
)
def test_missing_unreadable_and_exact_decimal_are_not_zero(raw, state, value):
    result = normalize(table([["지표", "연도", "값"], ["배출량", "2025", raw]]))
    (item,) = result.observations
    assert (item.value_state, item.value_decimal, item.value_raw) == (state, value, raw)


def test_wide_year_columns_merged_scope_and_linked_unit_footnote():
    batch = table(
        [
            ["지표", "Scope", "사업장", "2024", "2025"],
            ["배출량", "Scope 2", "서울", "1", "2"],
            [None, None, "부산", "3", "4"],
        ],
        spans={(1, 0): (2, 1), (1, 1): (2, 1)},
        footnote="단위: 천 tCO2e",
    )
    result = normalize(batch)
    assert len(result.observations) == 4
    assert {(o.subject, o.reporting_period, o.value_decimal) for o in result.observations} == {
        ("서울", "2024", "1000"),
        ("서울", "2025", "2000"),
        ("부산", "2024", "3000"),
        ("부산", "2025", "4000"),
    }
    for item in result.observations:
        assert item.metric_raw == "배출량" and item.scope == "Scope 2"
        assert item.footnotes == ("단위: 천 tCO2e",)
        assert item.parent_relations
        assert {ref.quote for ref in item.source_refs} >= {"단위: 천 tCO2e", item.reporting_period}


def test_parser_value_or_header_disagreement_has_no_winner_or_average():
    rows = [["지표", "연도", "값"], ["배출량", "2025", "1234"]]
    for row, col, replacement in [(1, 2, "1284"), (1, 1, "2024")]:
        changed = [list(values) for values in rows]
        changed[row][col] = replacement
        a, b = table(rows), table(changed, parser="B")
        result = normalize(a, b)
        (item,) = result.observations
        assert item.value_state == "conflict" and item.value_decimal is None
        assert item.quality == "conflicted" and result.conflicts
        quotes = {ref.quote for ref in item.source_refs}
        assert replacement in quotes and rows[row][col] in quotes
        assert result == normalize(b, a)


def test_units_denominator_boundary_and_unknown_layout_remain_explicit():
    result = normalize(
        table(
            [
                ["지표", "연도", "값", "단위", "조직경계", "분모"],
                ["감축률", "2025", "1", "%", "운영통제", ""],
                ["감축률", "2025", "1", "%p", "재무통제", ""],
                ["원단위", "2025", "2", "tCO2e/매출", "운영통제", "매출"],
            ]
        )
    )
    assert {
        (o.unit_canonical, o.organizational_boundary, o.denominator) for o in result.observations
    } == {
        ("%", "운영통제", None),
        ("%p", "재무통제", None),
        ("tCO2e/매출", "운영통제", "매출"),
    }
    unsupported = normalize(table([["opaque", "header"], ["something", "123"]]))
    assert not unsupported.observations
    assert any(issue.kind == "table_layout_unresolved" for issue in unsupported.conflicts)


def test_foreign_and_forged_graph_cannot_produce_observations():
    from proofops.application.ingest.normalize import normalize_tables

    graph = fuse_candidates(
        (table([["지표", "연도", "값"], ["배출量", "2025", "1"]]),), tenant_id=TENANT
    )
    with pytest.raises(ValueError):
        normalize_tables(graph, tenant_id=FOREIGN)
    with pytest.raises(ValueError):
        normalize_tables(replace(graph, tenant_id=FOREIGN), tenant_id=FOREIGN)
    with pytest.raises(ValueError):
        normalize_tables(replace(graph, candidates=()), tenant_id=TENANT)


def test_actual_local_pdf_parser_to_observation_and_v1_contract(tmp_path):
    from proofops.adapters.parsing.opendataloader import OpenDataLoaderParser
    from proofops.application.ingest.graph_fusion import ParserProfile
    from proofops.application.ingest.normalize import normalize_tables

    from .test_parsing import JAVA, MANIFEST, pdf, source

    graph = OpenDataLoaderParser(tmp_path).parse(
        source(pdf(table=True)),
        ParserProfile(MANIFEST, physical_pages=(2,), java_executable=JAVA),
        tenant_id=TENANT,
    )
    result = normalize_tables(graph, tenant_id=TENANT)
    (item,) = result.observations
    assert (item.metric_raw, item.reporting_period, item.value_decimal, item.unit_canonical) == (
        "Emissions",
        "2025",
        "1234",
        "tCO2e",
    )
    assert all(ref.page_num == 2 for ref in item.source_refs)
    schema = json.loads(Path("contracts/jsonschema/api_models.schema.json").read_text())
    validator = Draft202012Validator(
        {"$ref": "#/$defs/Observation", "$defs": schema["$defs"]}, format_checker=FormatChecker()
    )
    validator.validate(item.to_dict())
    assert item.quality == "unverified" and graph.validation_profile == "fast_preview"


def test_multilevel_year_and_basis_headers_inherit_only_explicit_spans():
    result = normalize(
        table(
            [
                ["지표", "사업장", "2025", None],
                [None, None, "시장기반", "위치기반"],
                ["배출량", "서울", "10", "20"],
            ],
            spans={(0, 0): (2, 1), (0, 1): (2, 1), (0, 2): (1, 2)},
        )
    )
    assert {(o.reporting_period, o.scope2_basis, o.value_decimal) for o in result.observations} == {
        ("2025", "시장기반", "10"),
        ("2025", "위치기반", "20"),
    }
    assert all(o.parent_relations for o in result.observations)


def test_missing_binding_or_bad_span_cannot_become_readable_value():
    missing = normalize(table([["지표", "연도", "값"], [None, "2025", "1"]]))
    assert missing.observations[0].value_state == "unreadable"
    assert missing.observations[0].value_decimal is None
    invalid = normalize(
        table([["지표", "연도", "값"], ["배출량", "2025", "1"]], spans={(1, 2): (0, 1)})
    )
    assert not invalid.observations
    assert invalid.conflicts


def test_unlocated_and_unreadable_sources_never_become_missing():
    from proofops.application.ingest.normalize import normalize_tables

    graph = fuse_candidates(
        (table([["지표", "연도", "값"], ["배출량", "2025", "-"]]),), tenant_id=TENANT
    )
    for quality in ("unreadable", "unlocated"):
        blocks = tuple(
            replace(b, quality=quality) if b.raw_text == "-" else b for b in graph.blocks
        )
        (item,) = normalize_tables(replace(graph, blocks=blocks), tenant_id=TENANT).observations
        assert item.value_state == "unreadable" and item.value_decimal is None
        assert item.quality == quality


def test_cell_footnote_only_applies_to_its_value():
    batch = table([["지표", "2024", "2025"], ["배출량", "1", "2"]], footnote="단위: 천 tCO2e")
    edges = tuple(
        replace(e, target_native_id="r1c2") if e.relation == "footnote_of" else e
        for e in batch.edges
    )
    result = normalize(replace(batch, edges=edges))
    assert {(o.reporting_period, o.value_decimal, o.unit_raw) for o in result.observations} == {
        ("2024", "1", None),
        ("2025", "2000", "천 tCO2e"),
    }


def test_ambiguous_headers_and_excessive_spans_fail_closed():
    for batch in (
        table(
            [["지표", "연도", "사업장", "사업장", "값"], ["배출량", "2025", "서울", "부산", "1"]]
        ),
        table([["지표", "연도", "값"], ["배출량", "2025", "1"]], spans={(1, 2): (1000, 1000)}),
    ):
        result = normalize(batch)
        assert not result.observations
        assert result.conflicts


def test_normalization_revision_identity_includes_changed_bindings():
    batch = table([["지표", "연도", "값"], ["배출량", "2025", "1"]])
    before = normalize(batch)
    cells = tuple(
        replace(b, source=replace(b.source, raw_text="2024")) if b.source.raw_text == "2025" else b
        for b in batch.blocks
    )
    after = normalize(replace(batch, blocks=cells))
    assert before.observations[0].reporting_period == "2025"
    assert after.observations[0].reporting_period == "2024"
    assert before.observations[0].observation_id != after.observations[0].observation_id


@pytest.mark.parametrize(
    "note",
    [
        "단위: 천 tCO2e (부산만 해당)",
        "단위: 천 tCO2e 부산만 해당",
        "단위: 천 tCO2e\n단, 부산 사업장은 제외",
        "unit: thousand tonnes, except overseas sites",
        "단위: 천 tCO2e-부산한정",
        "단위: 천 tCO2e부산한정",
        "단위:",
    ],
)
def test_scoped_unit_notes_block_numeric_normalization_in_both_paths(note):
    from proofops.application.ingest.normalize import normalize_table_bindings

    batch = table([["지표", "연도", "값"], ["배출량", "2025", "1"]], footnote=note)
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    explicit = normalize_table_bindings(
        graph,
        table_id=ids["T"],
        tenant_id=TENANT,
        bindings=(
            dict(metric_raw=ids["r1c0"], reporting_period=ids["r1c1"], value_raw=ids["r1c2"]),
        ),
    )
    for result in (normalize(batch), explicit):
        item = result.observations[0]
        assert item.value_decimal is None
        assert item.value_state == "unreadable"
        assert item.quality == "unreadable"
        assert item.footnotes == (note,)
        assert any(ref.quote == note for ref in item.source_refs)
        assert result.conflicts


def test_year_header_note_does_not_leak_to_other_year_observations():
    from proofops.application.ingest.normalize import normalize_tables

    note = "해외 사업장 제외"
    graph = fuse_candidates(
        (table([["지표", "2024", "2025"], ["배출량", "1", "2"]], footnote=note),),
        tenant_id=TENANT,
    )
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    graph = replace(
        graph,
        edges=tuple(
            replace(edge, target_id=ids["r0c1"]) if edge.relation == "footnote_of" else edge
            for edge in graph.edges
        ),
    )
    items = normalize_tables(graph, tenant_id=TENANT).observations
    assert {item.reporting_period: item.footnotes for item in items} == {
        "2024": (note,),
        "2025": (),
    }


@pytest.mark.parametrize(
    ("note", "row_scope", "scope", "state"),
    [
        ("Scope: Scope 1", "", "Scope 1", "value"),
        ("Scope 2", "", "Scope 2", "value"),
        ("Scope: Scope 1", "Scope 2", "Scope 2", "conflict"),
        ("Scope: Scope 1 해외 제외", "", None, "unreadable"),
    ],
)
def test_scope_note_normalization_preserves_roles_and_conflicts_in_both_paths(
    note, row_scope, scope, state
):
    from proofops.application.ingest.normalize import normalize_table_bindings

    batch = table(
        [["지표", "연도", "Scope", "값"], ["배출량", "2025", row_scope, "1"]],
        footnote=note,
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    explicit = normalize_table_bindings(
        graph,
        table_id=ids["T"],
        tenant_id=TENANT,
        bindings=(
            dict(
                metric_raw=ids["r1c0"],
                reporting_period=ids["r1c1"],
                scope=ids["r1c2"],
                value_raw=ids["r1c3"],
            ),
        ),
    )
    for result in (normalize(batch), explicit):
        (item,) = result.observations
        assert (item.scope, item.value_state) == (scope, state)
        assert item.footnotes == (note,)
        assert any(ref.quote == note for ref in item.source_refs)
        if state == "value":
            assert (ids["r1c3"], ids["F"], "scope") in item.parent_relations
            assert item.quality == "unverified"
        else:
            assert item.value_decimal is None and result.conflicts


def test_leading_full_width_title_row_is_skipped_before_explicit_header():
    # A lone full-width caption above the header must not be mistaken for the header;
    # the real header row drives binding and the title is preserved only as context.
    batch = table(
        [
            ["온실가스 배출량 현황", None, None],
            ["지표", "연도", "값"],
            ["배출량", "2025", "1,234"],
            ["배출량", "2024", "5,678"],
        ],
        spans={(0, 0): (1, 3)},
    )
    result = normalize(batch)
    assert {(o.metric_raw, o.reporting_period, o.value_decimal) for o in result.observations} == {
        ("배출량", "2025", "1234"),
        ("배출량", "2024", "5678"),
    }
    for item in result.observations:
        # Title is never promoted into a metric/unit/scope, only kept as provenance.
        assert item.unit_raw is None and item.scope is None
        assert "온실가스 배출량 현황" in {ref.quote for ref in item.source_refs}
    assert not result.conflicts


def test_leading_title_row_survives_metric_rename_and_row_shift():
    # Same skip must hold when the metric label differs and extra title rows shift data.
    batch = table(
        [
            ["2025 지속가능경영 부록", None, None, None],
            ["대상 데이터", None, None, None],
            ["항목", "기간", "단위", "값"],
            ["총배출량", "2025", "tCO2e", "42"],
        ],
        spans={(0, 0): (1, 4), (1, 0): (1, 4)},
    )
    result = normalize(batch)
    (item,) = result.observations
    assert (item.metric_raw, item.reporting_period, item.unit_canonical, item.value_decimal) == (
        "총배출량",
        "2025",
        "tCO2e",
        "42",
    )
    quotes = {ref.quote for ref in item.source_refs}
    assert {"2025 지속가능경영 부록", "대상 데이터"} <= quotes


def test_partial_width_leading_caption_is_not_skipped_and_fails_closed():
    # A caption spanning only part of the width is ambiguous; do not guess a header.
    batch = table(
        [
            ["부분 제목", None, "값"],
            ["지표", "연도", "값"],
            ["배출량", "2025", "1"],
        ],
        spans={(0, 0): (1, 2)},
    )
    result = normalize(batch)
    assert not result.observations
    assert any(issue.kind == "table_layout_unresolved" for issue in result.conflicts)


def test_full_width_title_without_following_rows_fails_closed():
    # A title with nothing beneath it is not a promotable header layout.
    batch = table([["제목만 있는 표", None, None]], spans={(0, 0): (1, 3)})
    result = normalize(batch)
    assert not result.observations
    assert any(issue.kind == "table_layout_unresolved" for issue in result.conflicts)


def test_full_width_title_over_non_header_body_still_fails_closed():
    # Skipping the title must still leave an unrecognizable header to reject.
    batch = table(
        [
            ["표 제목", None],
            ["opaque", "header"],
            ["something", "123"],
        ],
        spans={(0, 0): (1, 2)},
    )
    result = normalize(batch)
    assert not result.observations
    assert any(issue.kind == "table_layout_unresolved" for issue in result.conflicts)


def test_scope_note_on_one_year_cannot_supply_other_year():
    batch = table([["지표", "2024", "2025"], ["배출량", "1", "2"]], footnote="Scope: Scope 1")
    batch = replace(
        batch,
        edges=tuple(
            replace(e, target_native_id="r0c1") if e.relation == "footnote_of" else e
            for e in batch.edges
        ),
    )
    assert {o.reporting_period: o.scope for o in normalize(batch).observations} == {
        "2024": "Scope 1",
        "2025": None,
    }


@pytest.mark.parametrize("offset", [0, 1, 7])
def test_title_rowspan_and_coordinate_origin_do_not_change_field_mapping(offset):
    batch = table(
        [
            ["arbitrary caption", None, None],
            [None, None, None],
            ["metric", "year", "value"],
            ["water", "2024", "17"],
        ],
        spans={(0, 0): (2, 3)},
    )
    batch = replace(
        batch,
        blocks=tuple(
            replace(b, row_number=b.row_number + offset, column_number=b.column_number + offset)
            if b.kind == "table_cell"
            else b
            for b in batch.blocks
        ),
    )
    result = normalize(batch)
    (item,) = result.observations
    assert (item.metric_raw, item.reporting_period, item.value_decimal) == ("water", "2024", "17")
    assert (item.row, item.column) == (3 + offset, 2 + offset)
    assert item.quality == "unverified" and not result.conflicts


def test_multilevel_target_actual_second_header_is_not_flattened_to_bare_year():
    # A year spanning a 목표/실적 (target/actual) sub-header carries a second-level
    # semantic the normalizer cannot represent. It must NOT silently emit both body
    # values as identical bare-year observations; it fails closed as unresolved.
    batch = table(
        [
            ["지표", "2025", None],
            [None, "목표", "실적"],
            ["배출량", "10", "20"],
        ],
        spans={(0, 0): (2, 1), (0, 1): (1, 2)},
    )
    result = normalize(batch)
    assert not result.observations
    assert any(issue.kind == "table_layout_unresolved" for issue in result.conflicts)


def test_bare_merged_year_over_two_values_without_second_level_fails_closed():
    # A single merged year over two distinct value columns, with no second-level
    # header to disambiguate them, is the same ambiguous flattening and must not
    # produce two duplicate bare-year observations.
    batch = table(
        [["지표", "2025", None], ["배출량", "10", "20"]],
        spans={(0, 1): (1, 2)},
    )
    result = normalize(batch)
    assert not result.observations
    assert any(issue.kind == "table_layout_unresolved" for issue in result.conflicts)


def test_scope2_basis_second_level_header_remains_supported_contrast():
    # Contrast: the ONE recognized second-level header (Scope 2 measurement basis)
    # still resolves both sub-columns into distinct supported observations.
    batch = table(
        [
            ["지표", "사업장", "2025", None],
            [None, None, "시장기반", "위치기반"],
            ["배출량", "서울", "10", "20"],
        ],
        spans={(0, 0): (2, 1), (0, 1): (2, 1), (0, 2): (1, 2)},
    )
    result = normalize(batch)
    assert {(o.reporting_period, o.scope2_basis, o.value_decimal) for o in result.observations} == {
        ("2025", "시장기반", "10"),
        ("2025", "위치기반", "20"),
    }
    assert not result.conflicts


def test_ordinary_distinct_year_columns_still_normalize():
    # Plain (non-merged) year columns are unaffected by the second-level guard.
    result = normalize(table([["지표", "2024", "2025"], ["배출량", "1", "2"]]))
    assert {(o.reporting_period, o.value_decimal) for o in result.observations} == {
        ("2024", "1"),
        ("2025", "2"),
    }
    assert not result.conflicts


def test_explicit_binding_year_header_omitting_intervening_level_is_rejected():
    # Sibling explicit path: choosing the merged bare-year header + a body value while
    # omitting the intervening 목표/실적 header reaches the same demonstrated loss.
    # It must fail closed (ValueError) rather than emit a bare-year observation.
    from proofops.application.ingest.normalize import normalize_table_bindings

    batch = table(
        [
            ["지표", "2025", None],
            [None, "목표", "실적"],
            ["배출량", "10", "20"],
        ],
        spans={(0, 0): (2, 1), (0, 1): (1, 2)},
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    for value_key in ("r2c1", "r2c2"):
        with pytest.raises(ValueError):
            normalize_table_bindings(
                graph,
                table_id=ids["T"],
                tenant_id=TENANT,
                bindings=(
                    dict(
                        metric_raw=ids["r2c0"],
                        reporting_period=ids["r0c1"],
                        value_raw=ids[value_key],
                    ),
                ),
            )


def test_explicit_binding_direct_year_column_header_still_supported_contrast():
    # Contrast: a year column header directly above its value (no intervening level)
    # remains a supported explicit binding in both wide-year columns.
    from proofops.application.ingest.normalize import normalize_table_bindings

    batch = table([["지표", "2024", "2025"], ["배출량", "1", "2"]])
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    result = normalize_table_bindings(
        graph,
        table_id=ids["T"],
        tenant_id=TENANT,
        bindings=(
            dict(metric_raw=ids["r1c0"], reporting_period=ids["r0c1"], value_raw=ids["r1c1"]),
            dict(metric_raw=ids["r1c0"], reporting_period=ids["r0c2"], value_raw=ids["r1c2"]),
        ),
    )
    assert {(o.reporting_period, o.value_decimal) for o in result.observations} == {
        ("2024", "1"),
        ("2025", "2"),
    }


def test_unmerged_year_headers_do_not_discard_spanned_header_tier():
    result = normalize(
        table(
            [["지표", "2025", "2025"], [None, "목표", "실적"], ["water", "10", "20"]],
            spans={(0, 0): (2, 1)},
        )
    )
    assert not result.observations
    assert any(i.kind == "table_layout_unresolved" for i in result.conflicts)


@pytest.mark.parametrize("preceding_value", ["1", "-", "10 tCO2e"])
def test_explicit_year_header_can_cross_prior_numeric_data_rows(preceding_value):
    from proofops.application.ingest.normalize import normalize_table_bindings

    graph = fuse_candidates(
        (table([["지표", "2025"], ["energy", preceding_value], ["water", "20"]]),), tenant_id=TENANT
    )
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    result = normalize_table_bindings(
        graph,
        table_id=ids["T"],
        tenant_id=TENANT,
        bindings=(
            dict(metric_raw=ids["r2c0"], reporting_period=ids["r0c1"], value_raw=ids["r2c1"]),
        ),
    )
    assert result.observations[0].value_decimal == "20"
