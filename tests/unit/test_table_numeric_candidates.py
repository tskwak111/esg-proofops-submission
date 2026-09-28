"""Failing-first candidate tests for evaluation.table_numeric_candidates.

All shapes are taken from real archived HTML in
.local/table-recovery/{kia,kb,naver}/standard/response.json
(raw_response.elements[].content.html, ids kia-10/kia-13/kb-5/kb-9/naver-8).
Inline excerpts only; no PDF mutation, no network, no ledger.
"""

import pytest

from evaluation.html_table_cells import parse_table_cells
from evaluation.table_numeric_candidates import (
    candidates_from_html,
    parse_numeric_literal,
    period_qualifier,
    propose_table_candidates,
)

KIA10 = (
    "<table><thead><tr><td>구분</td><td>단위</td><td>2024(목표)</td>"
    "<td>2024(실적)</td><td>2025(목표)</td></tr></thead><tbody>"
    "<tr><td>폐기물 발생량 집약도</td><td>ton/조 원</td>"
    "<td>4,470</td><td>3,295</td><td>3,798</td></tr>"
    "<tr><td>폐기물 재활용률</td><td>%</td>"
    "<td>95.0</td><td>93.5</td><td>95.0</td></tr></tbody></table>"
)


@pytest.mark.parametrize("text", ["1 2", "1_2", "12,34", "1,,000"])
def test_malformed_numeric_separators_never_create_a_number(text):
    assert parse_numeric_literal(text)[0] is None


@pytest.mark.parametrize("text", ["2024~2025", "12024", "2030년까지 추진", "2024(목표 실적)"])
def test_ambiguous_or_prose_period_does_not_choose_first_year(text):
    assert period_qualifier(text)["year"] is None


KIA13 = (
    "<table><tbody><tr><td>구분</td><td>단위</td><td>2024</td></tr>"
    "<tr><td>제품 내 플라스틱 소재 총 사용량1</td><td>ton</td><td>39,884</td></tr>"
    "<tr><td>재활용된 제품 내 플라스틱 원료의 비율2</td><td>%</td>"
    "<td>2.5</td></tr></tbody></table>"
)

KB9 = (
    '<table><thead><tr><td rowspan="2">전략</td>'
    '<td rowspan="2">자금조달 원천</td>'
    '<td colspan="2">투자 금액</td></tr>'
    "<tr><td>2025</td><td>향후 계획</td></tr></thead><tbody>"
    "<tr><td>재생에너지 발전설비 투자</td><td>정부지원금 및 자체 예산</td>"
    "<td>1.6억 원</td><td>연도별 신규 설비 설치 시 소요 예산 편성</td></tr>"
    "<tr><td>ESG금융상품 환경 부문 공급 확대</td><td>녹색채권 등</td>"
    "<td>1.6조 원</td><td>25조 원 (2030년 누적 잔액 기준)</td></tr>"
    "</tbody></table>"
)

NAVER8 = (
    "<table><thead><tr><td>경영진 ESG KPI</td><td>세부 개선 과제</td>"
    "<td>달성 현황</td></tr></thead><tbody>"
    "<tr><td>데이터센터 재생에너지 확보 기반 마련</td>"
    "<td>PPA 추진 2025~2026년 2개년 추진 목표</td>"
    "<td>달성 6MW 태양광 PPA 개시 (진행 중)</td></tr></tbody></table>"
)


def test_target_actual_qualifiers_retained_kia10():
    cells = parse_table_cells(KIA10)
    (cand,) = propose_table_candidates(
        cells,
        (
            {
                "metric_raw": "r1c0",
                "unit_raw": "r1c1",
                "reporting_period": "r0c3",
                "value_raw": "r1c3",
            },
        ),
    )
    assert cand["binding_status"] == "candidate"
    assert cand["period"] == {
        "literal": "2024(실적)",
        "year": "2024",
        "qualifier": "actual",
        "achievement_status": "unknown",
    }
    assert cand["value"]["decimal"] == "3295"


def test_repeated_values_not_deduped_each_keeps_provenance():
    cells = parse_table_cells(KIA10)
    cands = propose_table_candidates(
        cells,
        (
            {
                "metric_raw": "r2c0",
                "unit_raw": "r2c1",
                "reporting_period": "r0c2",
                "value_raw": "r2c2",
            },
            {
                "metric_raw": "r2c0",
                "unit_raw": "r2c1",
                "reporting_period": "r0c4",
                "value_raw": "r2c4",
            },
        ),
    )
    assert [c["value"]["decimal"] for c in cands] == ["95.0", "95.0"]
    assert cands[0]["value"]["column"] != cands[1]["value"]["column"]
    assert cands[0]["period"]["qualifier"] == "target"
    assert cands[1]["period"]["qualifier"] == "target"


def test_bare_year_never_treated_as_achieved_kia13():
    cells = parse_table_cells(KIA13)
    (cand,) = propose_table_candidates(
        cells,
        (
            {
                "metric_raw": "r1c0",
                "unit_raw": "r1c1",
                "reporting_period": "r0c2",
                "value_raw": "r1c2",
            },
        ),
    )
    assert cand["binding_status"] == "candidate"
    assert cand["period"]["qualifier"] == "unqualified_bare_year"
    assert cand["period"]["achievement_status"] == "unknown"
    assert cand["value"]["decimal"] == "39884"


def test_wrong_year_column_is_unsupported_not_silent():
    cells = parse_table_cells(KIA10)
    (cand,) = propose_table_candidates(
        cells,
        (
            {
                "metric_raw": "r1c0",
                "unit_raw": "r1c1",
                "reporting_period": "r0c2",
                "value_raw": "r1c3",
            },
        ),
    )
    assert cand["binding_status"] == "unsupported"
    # Literal parse of the value cell is preserved; only the binding is refused.
    assert cand["value"]["decimal"] == "3295"
    assert any("row_column" in r for r in cand["reasons"])


def test_merged_value_requires_full_header_coverage():
    html = (
        "<table><tr><td>Metric</td><td>2024</td><td>2025</td></tr>"
        '<tr><td>Emissions</td><td rowspan="2">123</td><td>7</td></tr>'
        "<tr><td>Waste</td><td>8</td></tr></table>"
    )
    cells = parse_table_cells(html)
    (cand,) = propose_table_candidates(
        cells,
        ({"metric_raw": "r1c0", "reporting_period": "r0c1", "value_raw": "r1c1"},),
    )
    # Metric cell r1c0 covers only row 1 of the two-row merged value.
    assert cand["binding_status"] == "unsupported"


def test_kb9_subheader_year_binds_but_combined_value_stays_unsupported():
    cells = parse_table_cells(KB9)
    (cand,) = propose_table_candidates(
        cells,
        ({"metric_raw": "r2c0", "reporting_period": "r1c2", "value_raw": "r2c2"},),
    )
    # "1.6억 원" is value+unit combined: literal preserved, never split.
    assert cand["binding_status"] == "non_numeric_value"
    assert cand["value"]["decimal"] is None
    assert cand["value"]["text"] == "1.6억 원"
    assert cand["unit"]["inferred"] is False


def test_embedded_year_in_value_not_treated_as_period():
    cells = parse_table_cells(KB9)
    (cand,) = propose_table_candidates(
        cells,
        ({"metric_raw": "r3c0", "reporting_period": "r1c3", "value_raw": "r3c3"},),
    )
    # "향후 계획" is a non-year prose header; embedded 2030 stays literal.
    assert cand["period"]["qualifier"] == "non_year_prose"
    assert cand["period"]["year"] is None
    assert cand["binding_status"] in ("unsupported", "non_numeric_value")
    assert "2030" in cand["value"]["text"]


def test_prose_cells_are_non_numeric_naver8():
    cells = parse_table_cells(NAVER8)
    (cand,) = propose_table_candidates(
        cells,
        ({"metric_raw": "r1c0", "reporting_period": "r0c2", "value_raw": "r1c2"},),
    )
    assert cand["binding_status"] == "non_numeric_value"
    assert cand["value"]["decimal"] is None


def test_percent_sign_never_invented_as_unit():
    assert parse_numeric_literal("95.0%")[0] is None
    assert "percent" in parse_numeric_literal("95.0%")[1]
    cells = parse_table_cells(KIA10)
    (cand,) = propose_table_candidates(
        cells,
        (
            {
                "metric_raw": "r2c0",
                "unit_raw": "r2c1",
                "reporting_period": "r0c3",
                "value_raw": "r2c3",
            },
        ),
    )
    assert cand["unit"] == {"literal": "%", "inferred": False}
    assert cand["value"]["decimal"] == "93.5"


def test_no_bbox_fabrication_no_grade_no_verified_promotion():
    cells = parse_table_cells(KIA13)
    (cand,) = propose_table_candidates(
        cells,
        (
            {
                "metric_raw": "r2c0",
                "unit_raw": "r2c1",
                "reporting_period": "r0c2",
                "value_raw": "r2c2",
            },
        ),
    )
    flat = str(cand)
    for banned in (
        "bbox",
        "grade",
        "label",
        "verified",
        "present",
        "SUBSTANTIATED",
        "INCOMPLETE",
        "company",
        "achieved",
    ):
        assert banned not in flat or (banned == "verified" and "'verified': False" in flat)
    assert cand["status"] == "candidate_only"
    assert cand["verified"] is False
    assert period_qualifier("2024(목표)")["qualifier"] == "target"
    assert period_qualifier("향후 계획")["qualifier"] == "non_year_prose"


def test_fail_closed_on_unknown_cell_duplicate_and_role():
    cells = parse_table_cells(KIA13)
    with pytest.raises(ValueError):
        propose_table_candidates(cells, ({"reporting_period": "r0c2", "value_raw": "r9c9"},))
    with pytest.raises(ValueError):
        propose_table_candidates(
            cells,
            (
                {"reporting_period": "r0c2", "value_raw": "r1c2"},
                {"reporting_period": "r0c2", "value_raw": "r1c2"},
            ),
        )
    with pytest.raises(ValueError):
        propose_table_candidates(
            cells, ({"reporting_period": "r0c2", "value_raw": "r1c2", "grade": "r1c2"},)
        )
    with pytest.raises(ValueError):
        propose_table_candidates(cells, ({"value_raw": "r1c2"},))  # period required, no default


def test_candidates_from_html_reuses_parser():
    (cand,) = candidates_from_html(
        KIA13,
        (
            {
                "metric_raw": "r1c0",
                "unit_raw": "r1c1",
                "reporting_period": "r0c2",
                "value_raw": "r1c2",
            },
        ),
    )
    assert cand["binding_status"] == "candidate"
    assert cand["value"]["decimal"] == "39884"


def test_automatic_explicit_header_discovery_preserves_repeated_target_values():
    from evaluation.table_numeric_candidates import discover_table_candidates

    result = discover_table_candidates(KIA10)
    assert len(result["candidates"]) == 6
    assert [c["value"]["decimal"] for c in result["candidates"]] == [
        "4470",
        "3295",
        "3798",
        "95.0",
        "93.5",
        "95.0",
    ]
    assert [c["period"]["qualifier"] for c in result["candidates"]] == [
        "target",
        "actual",
        "target",
        "target",
        "actual",
        "target",
    ]
    assert not result["eligible_for_scoring"]
    assert discover_table_candidates(NAVER8)["status"] == "unsupported_layout"


def test_discovery_preserves_scope_and_company_group_headers():
    from evaluation.table_numeric_candidates import discover_table_candidates

    html = (
        '<table><tr><td colspan="2">구분</td><td>단위</td><td>2024</td></tr>'
        '<tr><td colspan="4">회사A</td></tr>'
        '<tr><td rowspan="2">온실가스</td><td>Scope 1</td><td>톤</td><td>10</td></tr>'
        "<tr><td>Scope 2</td><td>톤</td><td>20</td></tr></table>"
    )
    result = discover_table_candidates(html)
    assert len(result["candidates"]) == 2
    second = result["candidates"][1]
    assert [h["text"] for h in second["row_headers"]] == ["온실가스", "Scope 2"]
    assert [h["text"] for h in second["section_headers"]] == ["회사A"]
    assert second["headers"]["metric_raw"]["text"] == "Scope 2"


def test_percent_value_requires_explicit_matching_unit_header():
    from evaluation.table_numeric_candidates import discover_table_candidates

    html = (
        "<table><tr><td>지표</td><td>단위</td><td>2022</td></tr>"
        "<tr><td>재활용률</td><td>%</td><td>97.5%</td></tr></table>"
    )
    candidate = discover_table_candidates(html)["candidates"][0]
    assert candidate["binding_status"] == "candidate"
    assert candidate["value"]["decimal"] == "97.5"
    assert candidate["value"]["text"] == "97.5%"
    assert not candidate["verified"]
    wrong_unit = discover_table_candidates(html.replace("<td>%</td>", "<td>ton</td>"))
    assert wrong_unit["candidates"][0]["binding_status"] == "non_numeric_value"


def test_footnotes_keep_literal_and_column_basis_without_becoming_verified():
    from evaluation.table_numeric_candidates import discover_table_candidates

    html = (
        '<table><tr><td rowspan="2">지표</td><td rowspan="2">단위</td>'
        '<td colspan="2">2024 1)</td></tr><tr><td>지역</td><td>2) 시장</td></tr>'
        "<tr><td>Scope 2</td><td>톤</td><td>123*</td><td>100**</td></tr></table>"
    )
    result = discover_table_candidates(html)
    assert len(result["candidates"]) == 2
    a, b = result["candidates"]
    assert a["period"]["year"] == "2024"
    assert a["period"]["literal"] == "2024 1)"
    assert a["period"]["footnote_markers"] == ["1)"]
    assert a["value"]["text"] == "123*" and a["value"]["decimal"] == "123"
    assert a["value"]["footnote_markers"] == ["*"]
    assert b["value"]["footnote_markers"] == ["**"]
    assert [h["text"] for h in a["column_headers"]] == ["2024 1)", "지역"]
    assert [h["text"] for h in b["column_headers"]] == ["2024 1)", "2) 시장"]
    assert a["context_status"] == "requires_semantic_review"
    assert not a["verified"] and not result["eligible_for_scoring"]
    assert period_qualifier("2024 2025")["year"] is None
    assert parse_numeric_literal("123*")[0] is None  # strict shared literal path unchanged


def test_previous_metric_rows_are_context_not_inferred_parent_and_stop_at_company():
    from evaluation.table_numeric_candidates import discover_table_candidates

    html = (
        '<table><tr><td colspan="2">지표</td><td>단위</td><td>2024</td></tr>'
        '<tr><td colspan="4">회사A</td></tr>'
        '<tr><td rowspan="2">배출량</td><td>Scope 1 총계</td><td>톤</td><td>20</td></tr>'
        "<tr><td>사업장A</td><td>톤</td><td>10</td></tr>"
        '<tr><td colspan="4">회사B</td></tr>'
        "<tr><td>배출량</td><td>사업장B</td><td>톤</td><td>30</td></tr></table>"
    )
    a, b, c = discover_table_candidates(html)["candidates"]
    assert [h["text"] for h in b["preceding_row_headers"]] == ["Scope 1 총계"]
    assert b["headers"]["metric_raw"]["text"] == "사업장A"
    assert b["context_status"] == "requires_semantic_review"
    assert c["preceding_row_headers"] == []
    assert [h["text"] for h in c["section_headers"]] == ["회사B"]


def test_explicit_header_after_full_width_title_row_is_discovered():
    """A full-width title/caption row above the explicit header must not defer the table.

    Real reports frequently place a spanning title on the first physical row and
    the metric/unit/year header on the next. The header row is still explicit;
    only the leading full-width caption rows are skipped. Nothing is promoted:
    every candidate stays unverified and ineligible for scoring.
    """
    from evaluation.table_numeric_candidates import discover_table_candidates

    html = (
        "<table>"
        '<tr><td colspan="4">2024년 온실가스 배출량 현황</td></tr>'
        "<tr><td>구분</td><td>단위</td><td>2023</td><td>2024</td></tr>"
        "<tr><td>Scope 1</td><td>톤</td><td>100</td><td>110</td></tr>"
        "<tr><td>Scope 2</td><td>톤</td><td>200</td><td>220</td></tr>"
        "</table>"
    )
    result = discover_table_candidates(html)
    assert result["status"] == "candidate_only"
    assert [c["value"]["decimal"] for c in result["candidates"]] == [
        "100",
        "110",
        "200",
        "220",
    ]
    assert [c["headers"]["metric_raw"]["text"] for c in result["candidates"]] == [
        "Scope 1",
        "Scope 1",
        "Scope 2",
        "Scope 2",
    ]
    # The spanning title is preceding context for every data row, never a data value.
    assert all(
        c["section_headers"] and c["section_headers"][0]["text"] == "2024년 온실가스 배출량 현황"
        for c in result["candidates"]
    )
    assert not result["eligible_for_scoring"]
    assert all(not c["verified"] for c in result["candidates"])
    # The title cell itself is never emitted as a value binding.
    assert "r0c0" not in {c["value"]["key"] for c in result["candidates"]}


def test_non_full_width_row_before_header_still_defers_no_fabrication():
    """A leading row that is not a single full-width caption must not be skipped.

    Skipping only applies to unambiguous full-width title/caption rows. Any other
    leading layout keeps the conservative deferral instead of guessing a header.
    """
    from evaluation.table_numeric_candidates import discover_table_candidates

    html = (
        "<table>"
        "<tr><td>메모</td><td>비고</td><td>연도</td></tr>"
        "<tr><td>구분</td><td>단위</td><td>2024</td></tr>"
        "<tr><td>Scope 1</td><td>톤</td><td>100</td></tr>"
        "</table>"
    )
    result = discover_table_candidates(html)
    assert result["status"] == "unsupported_layout"
    assert result["candidates"] == []
    assert not result["eligible_for_scoring"]
