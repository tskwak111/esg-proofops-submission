"""Candidate-only numeric bindings over parsed HTML table cells.

Smallest reusable evaluation helper: given cells from
:func:`evaluation.html_table_cells.parse_table_cells` and explicit
caller-supplied header assignments, bind each numeric value cell to its
explicit year/unit/metric header cells, keeping row/column/merged-span
provenance. Reuses the HTML cell parser instead of duplicating it.

Candidate outputs only: every result carries ``status="candidate_only"``
and ``verified=False``. Structural mismatches, prose/combined values and
non-year headers are preserved explicitly as ``unsupported`` /
``non_numeric_value`` — never promoted, never defaulted. No bbox is
invented (row/column/span provenance only), no ``%`` sign is turned into
a unit, no grade/label/company logic exists here.
"""

from __future__ import annotations

import re

from proofops.application.ingest.normalize import _value

from evaluation.html_table_cells import parse_table_cells

ALLOWED_ROLES = frozenset({"metric_raw", "subject", "unit_raw", "reporting_period", "value_raw"})
REQUIRED_ROLES = frozenset({"reporting_period", "value_raw"})

_MISSING_TOKENS = frozenset({"", "-", "–", "—", "N/A", "NA"})
_YEAR_RE = re.compile(r"((?:19|20|21)[0-9]{2})년?(?:\s*\((목표|실적)\))?")


def cell_key(cell: dict) -> str:
    """Anchor key ``r{row}c{column}`` for a parsed cell."""
    return f"r{cell['row']}c{cell['column']}"


def parse_numeric_literal(text) -> tuple[str | None, str]:
    """Conservatively parse a value literal to a Decimal string.

    Returns ``(decimal_or_None, reason)``. Anything that is not a bare
    numeric literal — blanks, ``-``/``N/A``, prose, Korean/alpha text
    (combined value+unit like ``1.6억 원``), ``%`` signs — yields
    ``None`` with the literal preserved by the caller. ``%`` is never
    converted into a unit.
    """
    if not isinstance(text, str):
        return None, "value_not_text"
    stripped = text.strip()
    if stripped.upper() in _MISSING_TOKENS:
        return None, "value_missing_not_zero"
    if "%" in stripped:
        return None, "percent_sign_preserved_no_unit_inferred"
    if re.search(r"[A-Za-z가-힣]", stripped):
        return None, "value_combined_or_prose_preserved"
    value, unit, _, state = _value(stripped, None)
    if state == "value" and unit is None:
        return value, "plain_numeric_literal"
    return None, "value_not_plain_numeric_preserved"


def period_qualifier(header_text) -> dict:
    """Split a year header literal into year + target/actual qualifier.

    Exact bare years or years followed by ``(목표)`` / ``(실적)`` are
    supported, including a whitespace-separated trailing footnote marker such as
    ``2024 1)``. The marker is retained, not interpreted. Other text remains
    ``non_year_prose``.
    ``achievement_status`` is always ``unknown``: a
    bare year is never treated as achieved and even an ``actual``
    qualifier stays a candidate without verification.
    """
    literal = header_text.strip() if isinstance(header_text, str) else ""
    match = _YEAR_RE.fullmatch(literal)
    footnote = re.fullmatch(r"(.+?)\s+([1-9][0-9]?\))", literal)
    if not match and footnote:
        match = _YEAR_RE.fullmatch(footnote[1])
    qualifier = "non_year_prose"
    if match:
        qualifier = {"목표": "target", "실적": "actual", None: "unqualified_bare_year"}[match[2]]
    return {
        "literal": literal,
        "year": match[1] if match else None,
        "qualifier": qualifier,
        "achievement_status": "unknown",
        **({"footnote_markers": [footnote[2]]} if match and footnote else {}),
    }


def _same_row_cover(header: dict, value: dict) -> bool:
    return (
        header["row"] <= value["row"]
        and value["row"] + value["row_span"] <= header["row"] + header["row_span"]
    )


def _year_header_cover(header: dict, value: dict) -> bool:
    return (
        header["row"] + header["row_span"] <= value["row"]
        and header["column"] <= value["column"]
        and value["column"] + value["column_span"] <= header["column"] + header["column_span"]
    )


def _provenance(cell: dict) -> dict:
    return {
        "key": cell_key(cell),
        "text": cell["text"],
        "row": cell["row"],
        "column": cell["column"],
        "row_span": cell["row_span"],
        "column_span": cell["column_span"],
    }


def propose_table_candidates(cells, bindings) -> tuple[dict, ...]:
    """Bind value cells to explicit header cells; candidate-only results.

    ``cells`` is the tuple returned by ``parse_table_cells``; ``bindings``
    maps roles to anchor keys (``r{row}c{column}``). ``reporting_period``
    and ``value_raw`` are required per binding — no defaults are assumed.
    Unknown keys/roles, duplicate values and non-dict assignments raise
    ``ValueError`` (fail closed); structural non-coverage yields an
    ``unsupported`` candidate instead of a silent wrong binding.
    """
    by_key = {cell_key(c): c for c in cells}
    if len(by_key) != len(tuple(cells)) or not by_key:
        raise ValueError("cells must be non-empty parsed table cells")
    if not isinstance(bindings, tuple) or not bindings:
        raise ValueError("bindings must be a non-empty tuple")
    seen_values, results = set(), []
    for assignment in bindings:
        if not isinstance(assignment, dict) or not REQUIRED_ROLES <= set(assignment):
            raise ValueError("each binding requires reporting_period and value_raw")
        if set(assignment) - ALLOWED_ROLES or any(
            not isinstance(v, str) for v in assignment.values()
        ):
            raise ValueError("invalid table role assignment")
        for key in assignment.values():
            if key not in by_key:
                raise ValueError("binding requires a known table cell")
        value = by_key[assignment["value_raw"]]
        if assignment["value_raw"] in seen_values:
            raise ValueError("duplicate value binding")
        seen_values.add(assignment["value_raw"])

        headers = {
            role: by_key[assignment[role]] if role in assignment else None
            for role in ("metric_raw", "subject", "unit_raw", "reporting_period")
        }
        reasons: list[str] = []
        supported = True
        for role in ("metric_raw", "subject", "unit_raw"):
            header = headers[role]
            if header is None:
                reasons.append(f"{role}_missing_no_default_assumed")
                continue
            if _same_row_cover(header, value):
                reasons.append(f"{role}_same_row_full_span_covered")
            else:
                supported = False
                reasons.append(f"{role}_binding_crosses_value_row_column")
        period = by_key[assignment["reporting_period"]]
        if _same_row_cover(period, value):
            reasons.append("period_same_row_full_span_covered")
        elif _year_header_cover(period, value):
            reasons.append("period_column_header_covers_full_value_span")
        else:
            supported = False
            reasons.append("period_binding_crosses_value_row_column")

        qualifier = period_qualifier(period["text"])
        supported &= qualifier["year"] is not None
        reasons.append(f"period_qualifier_retained:{qualifier['qualifier']}")
        decimal, numeric_reason = parse_numeric_literal(value["text"])
        value_footnotes = []
        marked_value = re.fullmatch(r"(.+?)(\*{1,2})", value["text"].strip())
        if decimal is None and marked_value:
            decimal, numeric_reason = parse_numeric_literal(marked_value[1])
            if decimal is not None:
                value_footnotes = [marked_value[2]]
                numeric_reason = "numeric_literal_with_unresolved_footnote"
        unit_header = headers["unit_raw"]
        if unit_header and unit_header["text"].strip() == "%" and value["text"].endswith("%"):
            decimal, numeric_reason = parse_numeric_literal(value["text"][:-1])
            if decimal is not None:
                numeric_reason = "percent_literal_matches_explicit_unit_header"
        reasons.append(numeric_reason)
        if decimal is None:
            status = "non_numeric_value"
        elif not supported:
            status = "unsupported"
        else:
            status = "candidate"
        results.append(
            {
                "status": "candidate_only",
                "binding_status": status,
                "verified": False,
                "value": {
                    **_provenance(value),
                    "decimal": decimal,
                    "footnote_markers": value_footnotes,
                },
                "headers": {
                    role: (_provenance(h) if h is not None else None) for role, h in headers.items()
                },
                "period": qualifier,
                "unit": {
                    "literal": unit_header["text"] if unit_header is not None else None,
                    "inferred": False,
                },
                "reasons": reasons,
            }
        )
    return tuple(results)


def candidates_from_html(html: str, bindings) -> tuple[dict, ...]:
    """Parse one HTML table then propose candidates (reuses parser)."""
    return propose_table_candidates(parse_table_cells(html), bindings)


def discover_table_candidates(html: str) -> dict:
    """Discover only an explicit metric/unit/year header row; defer other layouts.

    This bounded local path preserves target/actual qualifiers. Complex semantic
    headers still require source-bound role proposals, never guessed roles.
    Preceding row labels and multirow column labels are context, not parent edges.
    """
    return discover_candidates_from_cells(parse_table_cells(html))


def discover_candidates_from_cells(cells) -> dict:
    """Same candidate discovery for an already parsed, source-matched table."""
    from proofops.application.ingest.normalize import _HEADERS

    result: dict = dict(
        status="unsupported_layout", candidates=[], deferred_cells=[], eligible_for_scoring=False
    )
    width = max(c["column"] + c["column_span"] for c in cells)
    # Skip only unambiguous full-width title/caption rows above the explicit header.
    # A skipped row must be a single cell spanning the whole table at column 0; any
    # other leading layout keeps the conservative deferral (no header is guessed).
    header_row, row = 0, 0
    while True:
        band = [c for c in cells if c["row"] == row]
        if len(band) == 1 and band[0]["column"] == 0 and band[0]["column_span"] == width:
            row += band[0]["row_span"] or 1
            header_row = row
            continue
        break
    first = [c for c in cells if c["row"] == header_row]
    metrics = [c for c in first if c["text"].strip().lower() in (*_HEADERS["metric_raw"], "구분")]
    units = [c for c in first if c["text"].strip().lower() in _HEADERS["unit_raw"]]
    years = [c for c in first if period_qualifier(c["text"])["year"] is not None]
    if len(metrics) != 1 or len(units) != 1 or not years:
        result["deferred_cells"] = [cell_key(c) for c in cells]
        return result
    bindings, contexts = [], []
    for value in cells:
        if value["row"] <= header_row:
            continue
        period = [h for h in years if _year_header_cover(h, value)]
        metric = [
            c
            for c in cells
            if metrics[0]["column"] <= c["column"]
            and c["column"] + c["column_span"] <= metrics[0]["column"] + metrics[0]["column_span"]
            and _same_row_cover(c, value)
        ]
        unit = [c for c in cells if c["column"] == units[0]["column"] and _same_row_cover(c, value)]
        if len(period) == len(unit) == 1 and metric:
            metric.sort(key=lambda c: c["column"])
            if unit[0]["row"] == header_row or metric[-1]["row"] == header_row:
                continue  # Multirow column headings are not data values.
            bindings.append(
                dict(
                    value_raw=cell_key(value),
                    metric_raw=cell_key(metric[-1]),
                    unit_raw=cell_key(unit[0]),
                    reporting_period=cell_key(period[0]),
                )
            )
            sections = [
                c
                for c in cells
                if c["column"] == 0
                and c["column_span"] == width
                and c["row"] < value["row"]
                and c["row"] != header_row
            ]
            contexts.append(
                dict(
                    row_headers=[_provenance(c) for c in metric],
                    section_headers=[_provenance(c) for c in sections[-1:]],
                    # Context only: a preceding subtotal is not an inferred parent.
                    preceding_row_headers=[
                        _provenance(c)
                        for c in cells
                        if c["column"] == metric[-1]["column"]
                        and c["column_span"] == metric[-1]["column_span"]
                        and max(
                            [0, *(s["row"] for s in sections), *(m["row"] - 1 for m in metric[:-1])]
                        )
                        < c["row"]
                        and c["row"] + c["row_span"] <= value["row"]
                    ],
                    column_headers=[_provenance(period[0])],
                    context_status="requires_semantic_review",
                )
            )
    if bindings:
        result["candidates"] = list(propose_table_candidates(cells, tuple(bindings)))
        first_data_rows: dict[str, int] = {}
        for candidate in result["candidates"]:
            key = candidate["headers"]["reporting_period"]["key"]
            row = candidate["value"]["row"]
            first_data_rows[key] = min(first_data_rows.get(key, row), row)
        for candidate, context in zip(result["candidates"], contexts, strict=True):
            candidate.update(context)
            first_data_row = first_data_rows[candidate["headers"]["reporting_period"]["key"]]
            candidate["column_headers"].extend(
                _provenance(c)
                for c in cells
                if candidate["headers"]["reporting_period"]["row"] < c["row"] < first_data_row
                and _year_header_cover(c, candidate["value"])
            )
        result["status"] = "candidate_only"
    selected = {b["value_raw"] for b in bindings}
    result["deferred_cells"] = [cell_key(c) for c in cells if cell_key(c) not in selected]
    return result
