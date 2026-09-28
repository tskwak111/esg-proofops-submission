import pytest

from evaluation.html_table_cells import parse_table_cells


def test_merged_two_row_header_occupancy():
    html = (
        "<table>"
        "<tr><th rowspan='2'>Category</th><th colspan='2'>2023</th></tr>"
        "<tr><th>Scope 1</th><th>Scope 2</th></tr>"
        "<tr><td>A</td><td>1</td><td>2</td></tr>"
        "</table>"
    )
    cells = parse_table_cells(html)
    assert [(c["text"], c["row"], c["column"], c["row_span"], c["column_span"]) for c in cells] == [
        ("Category", 0, 0, 2, 1),
        ("2023", 0, 1, 1, 2),
        ("Scope 1", 1, 1, 1, 1),
        ("Scope 2", 1, 2, 1, 1),
        ("A", 2, 0, 1, 1),
        ("1", 2, 1, 1, 1),
        ("2", 2, 2, 1, 1),
    ]
    assert all(set(c) == {"text", "row", "column", "row_span", "column_span"} for c in cells)


def test_repeated_values_not_deduped_and_no_span_expansion():
    html = "<table><tr><td>X</td><td>X</td></tr><tr><td>X</td><td>Y</td></tr></table>"
    cells = parse_table_cells(html)
    assert [c["text"] for c in cells] == ["X", "X", "X", "Y"]
    assert len(cells) == 4


def test_br_newline_inline_order_entities_and_strip():
    html = "<table><tr><td>  line1<br>line2 <b>bold</b> &amp; tail  </td></tr></table>"
    (cell,) = parse_table_cells(html)
    assert cell["text"] == "line1\nline2 bold & tail"
    assert cell["row"] == 0 and cell["column"] == 0


def test_invalid_spans_rejected():
    for bad in ["0", "-1", "abc", "1.5", ""]:
        with pytest.raises(ValueError):
            parse_table_cells(f"<table><tr><td colspan='{bad}'>a</td></tr></table>")
    with pytest.raises(ValueError):
        parse_table_cells("<table><tr><td rowspan='2' ROWSPAN='3'>a</td></tr></table>")


def test_structure_errors_rejected():
    with pytest.raises(ValueError):
        parse_table_cells("<table><tr><td>a</td></tr></table><table><tr><td>b</td></tr></table>")
    with pytest.raises(ValueError):
        parse_table_cells("<table><tr><td><table><tr><td>x</td></tr></table></td></tr></table>")
    with pytest.raises(ValueError):
        parse_table_cells("<table><tr><td>a<td>b</td></tr></table>")
    with pytest.raises(ValueError):
        parse_table_cells("<table><tr><td>a</td></table>")
    with pytest.raises(ValueError):
        parse_table_cells("<table><tr><td>a</td></tr><tr><td>b</td><td>c</td></tr></table>")
    with pytest.raises(ValueError):
        parse_table_cells("no tables here")


def test_bounds_rejected():
    with pytest.raises(ValueError):
        parse_table_cells("a" * 200001)
    with pytest.raises(ValueError):
        parse_table_cells("<table>" + "<tr><td>a</td></tr>" * 501 + "</table>")
    with pytest.raises(ValueError):
        parse_table_cells("<table><tr>" + "<td>a</td>" * 101 + "</tr></table>")
    cells = "<td>v</td>" * 21
    rows = "<table>" + "".join(f"<tr>{cells}</tr>" for _ in range(500)) + "</table>"
    with pytest.raises(ValueError):
        parse_table_cells(rows)


def test_rowspan_cannot_create_rows_absent_from_source():
    import pytest

    with pytest.raises(ValueError):
        parse_table_cells('<table><tr><td rowspan="2">only row</td></tr></table>')
