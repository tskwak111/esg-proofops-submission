"""Conservative table normalization, with immutable source bindings.

Recognizes explicit long-form headers and year columns. Unsupported layouts
remain issues for review. No cross-table/page merging, fuzzy semantic tagging,
source verification, or grade computation happens here.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, localcontext
from uuid import UUID, uuid5

from proofops.application.ingest.geometry import canonicalize_source_ref
from proofops.application.ingest.graph_fusion import (
    CanonicalBlock,
    CanonicalDocumentGraph,
    QualityIssue,
)
from proofops.domain.numeric import column_note_targets, scope_note_literal, unit_note_literal
from proofops.domain.provenance import canonical_hash
from proofops.domain.values import SourceRef

_HEADERS = {
    "metric_raw": ("지표", "metric", "항목"),
    "scope": ("scope",),
    "subject": ("사업장", "조직", "기업", "entity", "subject"),
    "reporting_period": ("연도", "기간", "year", "period"),
    "scope2_basis": ("산정방식", "시장/위치기반", "measurement basis", "scope2 basis"),
    "organizational_boundary": ("조직경계", "organizational boundary"),
    "unit_raw": ("단위", "unit"),
    "value_raw": ("값", "수치", "value"),
    "denominator": ("분모", "denominator"),
    "baseline_period": ("기준연도", "baseline year"),
    "category": ("카테고리", "category"),
    "method": ("산정방법", "method"),
}
_FIELD = {label: field for field, labels in _HEADERS.items() for label in labels}
_YEAR = re.compile(r"[12][0-9]{3}년?")
_NUMBER = re.compile(r"([+-]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]+)?)(?:\s+(.+))?")


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    tenant_id: str
    document_version_id: str
    parse_manifest_id: str
    source_sha256: str
    table_id: str
    row: int
    column: int
    metric_raw: str
    scope: str | None
    subject: str | None
    reporting_period: str
    scope2_basis: str | None
    organizational_boundary: str | None
    baseline_period: str | None
    category: str | None
    method: str | None
    unit_raw: str | None
    unit_canonical: str | None
    scale_multiplier: str
    value_raw: str
    value_decimal: str | None
    value_state: str
    denominator: str | None
    quality: str
    footnotes: tuple[str, ...]
    source_refs: tuple[SourceRef, ...]
    source_blocks: tuple[CanonicalBlock, ...]
    parent_relations: tuple[tuple[str, str, str], ...]
    metric_id: str | None = None
    assurance_ref: str | None = None

    def to_dict(self) -> dict:
        """Project only fixed v1 fields; retain this internal object for provenance."""
        return dict(
            observation_id=self.observation_id,
            metric=self.metric_raw,
            scope=self.scope,
            entity=self.subject,
            period=self.reporting_period,
            value=self.value_decimal,
            unit=self.unit_canonical,
            measurement_basis=self.scope2_basis,
            value_state=self.value_state,
            evidence_refs=[
                dict(asdict(ref), bbox=list(ref.bbox) if ref.bbox else None)
                for ref in self.source_refs
            ],
        )


@dataclass(frozen=True, slots=True)
class NormalizationResult:
    observations: tuple[Observation, ...]
    conflicts: tuple[QualityIssue, ...]
    graph: CanonicalDocumentGraph


def _issue(block: CanonicalBlock, kind: str, reason: str) -> QualityIssue:
    return QualityIssue(
        str(uuid5(UUID(block.source_id), kind)),
        kind,
        block.page_num,
        (block.source_id,),
        "open",
        reason,
    )


def _validate(graph: CanonicalDocumentGraph, tenant_id: str) -> None:
    identity = (tenant_id, graph.document_version_id, graph.parse_manifest_id, graph.source_sha256)
    if graph.tenant_id != tenant_id or not graph.candidates:
        raise ValueError("tenant mismatch or missing source candidates")
    originals = {}
    for batch in graph.candidates:
        if (
            batch.tenant_id,
            batch.document_version_id,
            batch.parse_manifest_id,
            batch.source_sha256,
        ) != identity:
            raise ValueError("candidate tenant/version/hash mismatch")
        for candidate in batch.blocks:
            originals[(batch.parser_run_id, candidate.source.source_native_id)] = candidate
    ids = {block.source_id for block in graph.blocks}
    if len(ids) != len(graph.blocks):
        raise ValueError("duplicate canonical source")
    for block in graph.blocks:
        if not block.candidates or block.quality not in (
            "verified",
            "unverified",
            "conflicted",
            "unreadable",
            "unlocated",
        ):
            raise ValueError("invalid source quality or missing provenance")
        if block.winner is not None and (
            type(block.winner) is not int or not 0 <= block.winner < len(block.candidates)
        ):
            raise ValueError("invalid selected candidate")
        for candidate in block.candidates:
            if (
                originals.get((candidate.source.parser_run_id, candidate.source.source_native_id))
                != candidate
            ):
                raise ValueError("source candidate not in immutable graph provenance")
    if any(edge.source_id not in ids or edge.target_id not in ids for edge in graph.edges):
        raise ValueError("dangling graph relation")


def _value(raw: str, unit: str | None) -> tuple[str | None, str | None, str, str]:
    match = _NUMBER.fullmatch(raw.strip())
    if match and match[2]:
        if unit and unit != match[2].strip():
            return None, unit, "1", "conflict"
        unit = match[2].strip()
    multiplier = "1"
    canonical_unit = unit
    if unit and unit.startswith("천 "):
        multiplier, canonical_unit = "1000", unit[2:].strip()
    if raw.strip().upper() in ("", "-", "N/A"):
        return None, canonical_unit, multiplier, "missing"
    if not match:
        return None, canonical_unit, multiplier, "unreadable"
    # Preserve arbitrary source precision instead of the ambient Decimal context.
    with localcontext() as context:
        context.prec = len(match[1]) + 4
        value = Decimal(match[1].replace(",", "")) * Decimal(multiplier)
    return format(value, "f"), canonical_unit, multiplier, "value"


def _observation(graph, table, row, column, value_cell, bindings, headers):
    # Only bound fields qualify this value; other year headers are context, not owners.
    targets = {table.source_id, value_cell.source_id, *(b.source_id for b in bindings.values())}
    targets.update(column_note_targets(graph, table.source_id, targets))
    note_ids = {
        edge.source_id
        for edge in graph.edges
        if edge.relation == "footnote_of" and edge.target_id in targets
    }
    footnotes = sorted(
        (block for block in graph.blocks if block.source_id in note_ids),
        key=lambda block: block.source_id,
    )
    used = {
        block.source_id: block for block in [value_cell, *bindings.values(), *headers, *footnotes]
    }
    blocks = tuple(used[key] for key in sorted(used))
    fields = {name: block.raw_text.strip() or None for name, block in bindings.items()}
    raw = value_cell.raw_text
    unit = fields.get("unit_raw")
    unit_notes = [
        block.raw_text
        for block in footnotes
        if re.match(r"(?:단위|unit)\s*:", block.raw_text.strip(), re.I)
    ]
    # Share the numeric admission grammar; unknown note syntax remains unresolved.
    parsed_notes = [unit_note_literal(text) for text in unit_notes]
    footnote_units = {unit for unit in parsed_notes if unit is not None}
    unresolved_unit_note = any(unit is None for unit in parsed_notes)
    unit_conflict = len(footnote_units | ({unit} if unit else set())) > 1
    if not unit and len(footnote_units) == 1:
        unit = next(iter(footnote_units))
    scope = fields.get("scope")
    scope_notes = {
        block.source_id: scope_note_literal(block.raw_text)
        for block in footnotes
        if re.match(r"Scope(?:\s|:)", block.raw_text.strip())
    }
    footnote_scopes = {value for value in scope_notes.values() if value is not None}
    scope_conflict = len(footnote_scopes | ({scope} if scope else set())) > 1
    if not scope and len(footnote_scopes) == 1:
        scope = next(iter(footnote_scopes))
    parsed, canonical_unit, multiplier, state = _value(raw, unit)
    if unit is None and (match := _NUMBER.fullmatch(raw.strip())) and match[2]:
        unit = match[2].strip()
    quality = "unverified"  # Normalization never verifies an extraction.
    if (
        unit_conflict
        or scope_conflict
        or any(b.quality == "conflicted" or b.winner is None for b in blocks)
    ):
        state, parsed, quality = "conflict", None, "conflicted"
    elif any(
        b.quality in ("unreadable", "unlocated")
        or any(c.has_invalid_geometry for c in b.candidates)
        for b in blocks
    ):
        state, parsed = "unreadable", None
        quality = "unreadable" if any(b.quality == "unreadable" for b in blocks) else "unlocated"
    elif (
        unresolved_unit_note
        or any(value is None for value in scope_notes.values())
        or state == "unreadable"
        or not fields.get("metric_raw")
        or not fields.get("reporting_period")
    ):
        state, parsed = "unreadable", None
        quality = "unreadable"
    elif state == "conflict":
        quality = "conflicted"
    refs = tuple(
        canonicalize_source_ref(candidate.source, candidate.geometry, source_id=block.source_id)
        for block in blocks
        for candidate in block.candidates
        if not candidate.has_invalid_geometry
    )
    relations = tuple(
        sorted(
            {(value_cell.source_id, block.source_id, name) for name, block in bindings.items()}
            | {
                (value_cell.source_id, source_id, "scope")
                for source_id, value in scope_notes.items()
                if value is not None and value == scope
            }
        )
    )
    signature = canonical_hash(
        {
            "tenant": graph.tenant_id,
            "source": graph.source_sha256,
            "table": table.source_id,
            "row": row,
            "column": column,
            "blocks": [asdict(b) for b in blocks],
            "version": "4",
        }
    )
    return Observation(
        observation_id=str(uuid5(UUID(graph.parse_manifest_id), signature)),
        tenant_id=graph.tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        table_id=table.source_id,
        row=row,
        column=column,
        metric_raw=fields.get("metric_raw") or "",
        scope=scope,
        subject=fields.get("subject"),
        reporting_period=fields.get("reporting_period") or "",
        scope2_basis=fields.get("scope2_basis"),
        organizational_boundary=fields.get("organizational_boundary"),
        baseline_period=fields.get("baseline_period"),
        category=fields.get("category"),
        method=fields.get("method"),
        unit_raw=unit,
        unit_canonical=canonical_unit,
        scale_multiplier=multiplier,
        value_raw=raw,
        value_decimal=parsed,
        value_state=state,
        denominator=fields.get("denominator"),
        quality=quality,
        footnotes=tuple(block.raw_text for block in footnotes),
        source_refs=refs,
        source_blocks=blocks,
        parent_relations=relations,
    )


def normalize_tables(graph: CanonicalDocumentGraph, *, tenant_id: str) -> NormalizationResult:
    """Normalize explicit cell/header associations; return unresolved issues too."""
    _validate(graph, tenant_id)
    by_id = {block.source_id: block for block in graph.blocks}
    aliases = {
        (c.source.parser_run_id, c.source.source_native_id): block.source_id
        for block in graph.blocks
        for c in block.candidates
    }
    tables: dict[str, list[CanonicalBlock]] = {}
    issues = list(graph.issues)
    for block in graph.blocks:
        if block.kind != "table_cell":
            continue
        locations = set()
        for candidate in block.candidates:
            table_id = aliases.get((candidate.source.parser_run_id, candidate.table_native_id))
            locations.add(
                (
                    table_id,
                    candidate.row_number,
                    candidate.column_number,
                    candidate.row_span if candidate.row_span is not None else 1,
                    candidate.column_span if candidate.column_span is not None else 1,
                )
            )
        if len(locations) != 1:
            issues.append(_issue(block, "parse_conflict", "Parser table alignment disagrees."))
            continue
        table_id, row, col, rs, cs = next(iter(locations))
        if (
            not isinstance(table_id, str)
            or table_id not in by_id
            or by_id[table_id].kind != "table"
            or any(type(n) is not int or n < 0 for n in (row, col))
            or any(type(n) is not int or not 1 <= n <= 1000 for n in (rs, cs))
        ):
            issues.append(
                _issue(block, "table_layout_unresolved", "Missing/invalid cell alignment.")
            )
            continue
        tables.setdefault(table_id, []).append(block)
    observations = []
    for table_id, cells in sorted(tables.items()):
        table = by_id[table_id]
        if (
            sum((c.candidates[0].row_span or 1) * (c.candidates[0].column_span or 1) for c in cells)
            > 100_000
        ):
            issues.append(
                _issue(table, "table_layout_unresolved", "Cell expansion limit exceeded.")
            )
            continue
        grid: dict[tuple[int, int], CanonicalBlock] = {}
        overlap = False
        # ponytail: bounded span expansion; interval lookup if large merged tables need it.
        for cell in cells:
            c = cell.candidates[0]
            for row in range(c.row_number, c.row_number + (c.row_span or 1)):
                for col in range(c.column_number, c.column_number + (c.column_span or 1)):
                    if (row, col) in grid and grid[row, col] != cell:
                        overlap = True
                    grid[row, col] = cell
        # Skip only unambiguous leading full-width single-cell title rows above the
        # explicit header. A skippable row must be a lone cell
        # whose span fills the entire observed table width; any other leading layout (partial
        # captions, overlaps, multiple cells) keeps the earliest row as the header so
        # ambiguous tables still fail closed below. Titles never supply header roles.
        first_col = min(col for _, col in grid)
        columns = {col for _, col in grid}
        title_rows: set[int] = set()
        titles: list[CanonicalBlock] = []
        header_row = min(row for row, _ in grid)
        while not overlap:
            band = {col: cell for (row, col), cell in grid.items() if row == header_row}
            distinct = set(band.values())
            if len(distinct) != 1 or set(band) != columns:
                break
            title = next(iter(distinct))
            span = title.candidates[0]
            if (
                span.column_number != first_col
                or (span.column_span or 1) != len(columns)
                or span.row_number != header_row
            ):
                break
            title_rows.update(range(header_row, header_row + (span.row_span or 1)))
            titles.append(title)
            header_row += span.row_span or 1
            if header_row not in {row for row, _ in grid}:
                # A title with nothing beneath it is not a promotable header layout.
                header_row = min(row for row, _ in grid)
                title_rows.clear()
                titles.clear()
                break
        headers = {col: cell for (row, col), cell in grid.items() if row == header_row}
        header_rows = {header_row} | title_rows
        basis_headers = {}
        next_row = {col: cell for (row, col), cell in grid.items() if row == header_row + 1}
        basis_values = {"시장기반", "위치기반", "market-based", "location-based"}
        if any(c.raw_text.strip().lower() in basis_values for c in next_row.values()) and all(
            cell in headers.values() or cell.raw_text.strip().lower() in basis_values
            for cell in next_row.values()
        ):
            header_rows.add(header_row + 1)
            basis_headers = {
                col: cell
                for col, cell in next_row.items()
                if cell.raw_text.strip().lower() in basis_values
            }
        fields = {
            col: _FIELD[cell.raw_text.strip().lower()]
            for col, cell in headers.items()
            if cell.raw_text.strip().lower() in _FIELD
        }
        years = {col for col, cell in headers.items() if _YEAR.fullmatch(cell.raw_text.strip())}
        value_columns = {col for col, name in fields.items() if name == "value_raw"} | years
        # Explicit Year + named metric columns (e.g. Year | Emissions).
        metric_columns = (
            set(headers) - set(fields)
            if "reporting_period" in fields.values()
            and "metric_raw" not in fields.values()
            and "value_raw" not in fields.values()
            else set()
        )
        value_columns |= metric_columns
        # Shared year columns require supported subheaders; preserve unknown roles.
        owners: dict[str, set[int]] = {}
        for col in value_columns:
            owners.setdefault(headers[col].source_id, set()).add(col)
        unresolved_second_level = any(
            len(cols) > 1 and any(col not in basis_headers for col in cols)
            for cols in owners.values()
        )
        header_end = max(
            h.candidates[0].row_number + (h.candidates[0].row_span or 1) for h in headers.values()
        )
        unresolved_second_level |= any(
            header_row < r < header_end
            and r not in header_rows
            and col in value_columns
            and cell not in headers.values()
            for (r, col), cell in grid.items()
        )
        if (
            overlap
            or unresolved_second_level
            or len(fields.values()) != len(set(fields.values()))
            or not value_columns
            or ("metric_raw" not in fields.values() and not metric_columns)
            or ("reporting_period" not in fields.values() and not years)
        ):
            issues.append(
                _issue(table, "table_layout_unresolved", "Ambiguous table headers/spans.")
            )
            continue
        for row in sorted({row for row, _ in grid} - header_rows):
            bindings = {
                name: grid[row, col]
                for col, name in fields.items()
                if name != "value_raw" and (row, col) in grid
            }
            for col in sorted(value_columns):
                if (row, col) not in grid:
                    issues.append(_issue(table, "table_layout_unresolved", "Missing value cell."))
                    continue
                bound = dict(bindings)
                if col in years:
                    bound["reporting_period"] = headers[col]
                if col in metric_columns:
                    bound["metric_raw"] = headers[col]
                if col in basis_headers:
                    bound["scope2_basis"] = basis_headers[col]
                item = _observation(
                    graph,
                    table,
                    row,
                    col,
                    grid[row, col],
                    bound,
                    [*headers.values(), *titles],
                )
                observations.append(item)
                if item.value_state in ("conflict", "unreadable"):
                    issues.append(
                        _issue(
                            grid[row, col],
                            "parse_conflict"
                            if item.value_state == "conflict"
                            else "table_value_unreadable",
                            "Value or binding unresolved.",
                        )
                    )
    return NormalizationResult(tuple(observations), tuple(issues), graph)


def normalize_table_bindings(
    graph: CanonicalDocumentGraph,
    *,
    table_id: str,
    bindings: tuple[dict[str, str], ...],
    tenant_id: str,
) -> NormalizationResult:
    """Normalize explicit extractor cell-role assignments, never approve them.

    Handles nonstandard headers without inventing labels or filling merged cells.
    Every role is pinned to a cell in the same table and value row; only the year
    may come from its column header. Unsupported bindings fail closed. Callers
    retain their extraction receipt separately; output remains unverified.
    """
    _validate(graph, tenant_id)
    blocks = {block.source_id: block for block in graph.blocks}
    table = blocks.get(table_id)
    if table is None or table.kind != "table":
        raise ValueError("unknown table")
    parents = {
        (candidate.source.parser_run_id, candidate.source.source_native_id)
        for candidate in table.candidates
    }

    def location(source_id):
        block = blocks.get(source_id)
        if block is None or block.kind != "table_cell":
            raise ValueError("binding requires an original table cell")
        positions = set()
        for candidate in block.candidates:
            if (candidate.source.parser_run_id, candidate.table_native_id) not in parents:
                raise ValueError("binding crosses tables")
            row, col = candidate.row_number, candidate.column_number
            rs = candidate.row_span if candidate.row_span is not None else 1
            cs = candidate.column_span if candidate.column_span is not None else 1
            if any(type(n) is not int or n < 0 for n in (row, col)):
                raise ValueError("binding requires explicit cell alignment")
            if any(type(n) is not int or not 1 <= n <= 1000 for n in (rs, cs)):
                raise ValueError("binding requires valid spans")
            positions.add((row, col, rs, cs))
        if len(positions) != 1:
            raise ValueError("binding cell alignment disagrees")
        return block, next(iter(positions))

    # Keep intervals: expanding every cell span would allocate up to a million
    # entries per cell. An omitted text tier cannot be treated as a prior value.
    table_cells = [
        location(block.source_id)
        for block in graph.blocks
        if block.kind == "table_cell"
        and any(
            (candidate.source.parser_run_id, candidate.table_native_id) in parents
            for candidate in block.candidates
        )
    ]

    observations = []
    issues = list(graph.issues)
    seen = set()
    for assignment in bindings:
        if (
            not isinstance(assignment, dict)
            or set(assignment) - (set(_HEADERS))
            or not {"metric_raw", "reporting_period", "value_raw"} <= set(assignment)
            or any(not isinstance(value, str) for value in assignment.values())
        ):
            raise ValueError("invalid table role assignment")
        value, (row, col, value_rs, value_cs) = location(assignment["value_raw"])
        if value.source_id in seen:
            raise ValueError("duplicate value binding")
        seen.add(value.source_id)
        bound = {}
        for role, source_id in assignment.items():
            block, (r, c, rs, cs) = location(source_id)
            same_row = r <= row and row + value_rs <= r + rs
            year_header = (
                role == "reporting_period"
                and r + rs <= row
                and c <= col
                and col + value_cs <= c + cs
            )
            if year_header and any(
                r + rs <= other_row < row
                and other_col < col + value_cs
                and col < other_col + other_cs
                and _value(other.raw_text, None)[3] not in ("value", "missing")
                for other, (other_row, other_col, _other_rs, other_cs) in table_cells
            ):
                raise ValueError("year header binding omits an intervening table level")
            if not same_row and not year_header:
                raise ValueError("binding crosses value row/column")
            if role != "value_raw":
                bound[role] = block
        item = _observation(graph, table, row, col, value, bound, [])
        # Role assignments can differ over identical cells: pin their semantics too.
        item = replace(
            item,
            observation_id=str(
                uuid5(UUID(item.observation_id), canonical_hash({"table_bindings_v1": assignment}))
            ),
        )
        observations.append(item)
        if item.value_state in ("conflict", "unreadable"):
            issues.append(_issue(value, "table_value_unreadable", "Value or binding unresolved."))
    return NormalizationResult(tuple(observations), tuple(issues), graph)
