"""Deterministic table-structural sources for one atomic claim (opt-in, v1).

Why this exists
---------------
An atomic claim cut from a table cell is often a bare value (``9.9억 원``) or a
bare row/column label (``자원 효율성``). Before this module the only context the
extraction and preliminary paths could offer such a fragment came from
``_context_candidates``, which deliberately EXCLUDES ``table``/``table_row``/
``table_cell`` kinds and then picks the nearest same-page blocks by bbox
centre. For a table cell the nearest blocks are the neighbouring *numeric*
cells, so the row metric, the column year/period, the unit and the row
qualifiers never reached the model at all. No prompt wording can supply
information that was never sent.

What it does
------------
It reuses structure the parser already stored: the existing ``table_parent``
edges and the existing per-candidate table coordinates (``table_native_id``,
``row_number``, ``column_number``, ``row_span``, ``column_span``), and the
same-table row/column coverage relation already implemented for accepted
bindings in ``application/evidence/binding.py``. Every returned entry is a
literal span of a real canonical block with its real provenance; nothing is
synthesised.

What it refuses to do
---------------------
* It never invents a quote, coordinate, unit or period, and never rewrites a
  claim quote. An adjacent row or a different column is rejected, not merged.
* An association it cannot compute (no table lineage, ambiguous lineage,
  missing/invalid row-column numbers, cross-page) stays ``unresolved``; the
  caller keeps it explicitly unknown instead of guessing.
* It does not promote source quality. Each candidate keeps whatever
  ``verify_source_ref`` actually returns, and callers must keep unverified
  candidates as interpretation context only -- never as approved evidence.
* A role is a STRUCTURAL INTERPRETATION of the parser's own layout, not an
  attestation. ``row_header``/``column_header`` say "this cell occupies the
  table's stub column / header row band for the value's row and column"; they
  do not certify that the cell defines the metric, the population or the
  period. Accepting a role as a metric or a reporting period stays the
  downstream tagging/binding decision, with its existing evidence gates.
* It makes no structural claim about assertiveness. A first-column or header
  cell is NOT classified as "not a claim": a genuine dated target or action
  can legitimately sit in a first column or a heading, so position alone
  cannot prove non-assertion. This module only supplies axes.
"""

from dataclasses import dataclass

from proofops.application.evidence.span_citations import verify_source_ref
from proofops.application.ingest.graph_fusion import CanonicalBlock, CanonicalDocumentGraph
from proofops.domain.errors import DomainValidationError
from proofops.domain.values import SourceRef, _require_uuid

TABLE_SOURCE_POLICY = "table-structural-sources-v1"

ROW_HEADER = "row_header"
COLUMN_HEADER = "column_header"
ROW_QUALIFIER = "row_qualifier"
TABLE_SOURCE_ROLES = (ROW_HEADER, COLUMN_HEADER, ROW_QUALIFIER)
_ROLE_PRIORITY = {ROW_HEADER: 0, COLUMN_HEADER: 1, ROW_QUALIFIER: 2}
_ASSOCIATION = {
    ROW_HEADER: "row_covered",
    ROW_QUALIFIER: "row_covered",
    COLUMN_HEADER: "column_covered",
}

# Bounded lineage walk: an atomic block sits inside a cell, a cell inside a row,
# a row inside a table. Four hops is already generous and keeps a malformed or
# cyclic graph from looping.
_MAX_LINEAGE_DEPTH = 4


@dataclass(frozen=True, slots=True)
class TableClaimSource:
    """One real, source-linked table axis cell for a claim's value cell."""

    ref: SourceRef
    role: str
    association: str
    table_native_id: str
    row_number: int
    column_number: int
    row_span: int
    column_span: int

    @property
    def verified(self) -> bool:
        return self.ref.verification_state == "verified"


@dataclass(frozen=True, slots=True)
class TableClaimSources:
    """Split result: only ``verified`` may ever become a numbered source."""

    lineage: str
    verified: tuple[TableClaimSource, ...] = ()
    context_only: tuple[TableClaimSource, ...] = ()
    omitted_source_ids: tuple[str, ...] = ()
    focal_table_native_id: str | None = None
    focal_row_number: int | None = None
    focal_column_number: int | None = None

    @property
    def resolved(self) -> bool:
        return self.lineage == "resolved"


def _coordinates(block: CanonicalBlock) -> tuple[str, int, int, int, int] | None:
    """Complete, valid table coordinates of a block's SELECTED candidate only.

    A conflicted block (no winner) has no selected coordinates, and a partial
    coordinate set is treated as absent rather than defaulted: binding treats
    missing row/column numbers as undetermined, and so do we.
    """
    if block.winner is None:
        return None
    candidate = block.candidates[block.winner]
    table_native_id = candidate.table_native_id
    row, column = candidate.row_number, candidate.column_number
    if not isinstance(table_native_id, str) or not table_native_id:
        return None
    if any(type(value) is not int or value < 0 for value in (row, column)):
        return None
    row_span = 1 if candidate.row_span is None else candidate.row_span
    column_span = 1 if candidate.column_span is None else candidate.column_span
    if any(type(value) is not int or value < 1 for value in (row_span, column_span)):
        return None
    return table_native_id, row, column, row_span, column_span


def _same_table(left: CanonicalBlock, right: CanonicalBlock) -> bool:
    """Identical parser run, physical page and parser table id.

    Same relation as ``accept_binding``'s ``same_table``: a cross-page or
    cross-parser-run table join is never inferred here.
    """
    if left.winner is None or right.winner is None:
        return False
    a, b = left.candidates[left.winner], right.candidates[right.winner]
    return bool(a.table_native_id) and (
        a.source.parser_run_id,
        a.source.physical_page,
        a.table_native_id,
    ) == (b.source.parser_run_id, b.source.physical_page, b.table_native_id)


def _covers(outer: tuple[int, int], inner: tuple[int, int]) -> bool:
    """``outer`` band (start, span) fully contains ``inner`` band."""
    return outer[0] <= inner[0] and inner[0] + inner[1] <= outer[0] + outer[1]


def _lineage_identity(block: CanonicalBlock) -> tuple[str, int] | None:
    """Parser run and physical page of a block's selected candidate."""
    if block.winner is None:
        return None
    source = block.candidates[block.winner].source
    return source.parser_run_id, source.physical_page


def _focal_cell(
    ref: SourceRef, blocks: dict[str, CanonicalBlock], parents: dict[str, tuple[str, ...]]
) -> CanonicalBlock | None:
    """The unambiguous nearest ancestor carrying complete table coordinates.

    The ref's own block is used when it already carries coordinates. Otherwise
    the existing ``table_parent`` edges are walked upward one level at a time.
    Only an ancestor on the ref's OWN parser run and physical page is eligible,
    so a same-shaped row on another page or from another parser run can never be
    mistaken for the lineage. A level that offers two or more DIFFERENT eligible
    ancestors is ambiguous and yields ``None`` (the KB value cell legitimately
    has two declared row parents, but the walk already stopped at the cell
    itself, which is the single coordinate-bearing ancestor).
    """
    block = blocks.get(ref.source_id)
    if block is None:
        return None
    identity = _lineage_identity(block)
    if identity is None:
        return None
    if _coordinates(block) is not None:
        return block
    frontier: tuple[str, ...] = (block.source_id,)
    for _ in range(_MAX_LINEAGE_DEPTH):
        candidates: dict[str, CanonicalBlock] = {}
        upward: list[str] = []
        for source_id in frontier:
            for target_id in parents.get(source_id, ()):
                target = blocks.get(target_id)
                if target is None or _lineage_identity(target) != identity:
                    continue  # never cross a parser run or a physical page
                if _coordinates(target) is None:
                    upward.append(target_id)
                else:
                    candidates.setdefault(target.source_id, target)
        if candidates:
            return next(iter(candidates.values())) if len(candidates) == 1 else None
        if not upward:
            return None
        frontier = tuple(sorted(dict.fromkeys(upward)))
    return None


def _table_layout(
    coordinates: dict[str, tuple[str, int, int, int, int]],
) -> tuple[frozenset[int], frozenset[int]]:
    """Header row band and stub column set of one table, from real spans only.

    Both come from the table's own stub-head cell (the cell at the minimum row
    and minimum column), which report layouts use to declare how deep the
    header is: its ``row_span`` is the header depth. A column is a stub column
    when its cell in the first header row spans that whole depth, i.e. it has
    no sub-header beneath it -- ``전략`` and ``자금조달 원천`` do, while
    ``투자 금액`` does not because ``2025``/``향후 계획`` sit under it.

    This is what keeps a peer data cell out of the header roles: ``1.6억 원``
    in row 3 of the same column is above row 5 but is not inside the header
    band, so it is never offered as a column header.
    """
    if not coordinates:
        return frozenset(), frozenset()
    rows = {value[1] for value in coordinates.values()}
    columns = {value[2] for value in coordinates.values()}
    first_row, first_column = min(rows), min(columns)
    depth = next(
        (
            value[3]
            for value in coordinates.values()
            if (value[1], value[2]) == (first_row, first_column)
        ),
        1,
    )
    header_rows = frozenset(range(first_row, first_row + depth))
    stub_columns = frozenset(
        value[2] for value in coordinates.values() if value[1] == first_row and value[3] == depth
    ) | {first_column}
    return header_rows, stub_columns


def _axis_role(
    focal: tuple[str, int, int, int, int],
    other: tuple[str, int, int, int, int],
    header_rows: frozenset[int],
    stub_columns: frozenset[int],
) -> str | None:
    """Actual same-table row/column association, or ``None`` for no relation.

    ``row_header``/``row_qualifier``: the other cell's ROW band covers the focal
    row band and it sits strictly to the left; a stub column yields the row
    header (the row metric), any other left column yields an explicitly
    weaker ``row_qualifier`` so a neighbouring value in the same row can never
    be mistaken for the metric. This is what rejects an adjacent row: a cell in
    row 4 does not cover row 5.
    ``column_header``: the other cell's COLUMN band covers the focal column
    band, its row band lies entirely inside the table's header band, and it
    ends strictly above the focal row -- the column-header direction
    ``accept_binding`` already accepts, narrowed to real header rows.
    """
    _, focal_row, focal_column, focal_row_span, focal_column_span = focal
    _, row, column, row_span, column_span = other
    if (
        _covers((column, column_span), (focal_column, focal_column_span))
        and row + row_span <= focal_row
        and frozenset(range(row, row + row_span)) <= header_rows
    ):
        return COLUMN_HEADER
    if (
        _covers((row, row_span), (focal_row, focal_row_span))
        and column + column_span <= focal_column
    ):
        return ROW_HEADER if column in stub_columns else ROW_QUALIFIER
    return None


def _atomic_refs(
    cell: CanonicalBlock,
    blocks: dict[str, CanonicalBlock],
    children: dict[str, tuple[str, ...]],
) -> tuple[SourceRef, ...]:
    """Prefer the cell's own atomic children as the citable spans.

    A ``table_cell`` block is frequently outside an existing run's attested
    source scope (``verify_tables=false``), while the atomic paragraph inside
    it is inside it. Using the child therefore lets real source verification
    decide, instead of this module pre-deciding. Children must sit on the cell's
    own parser run and physical page and carry no table coordinates of their
    own; otherwise the cell block itself is used. Order is deterministic.
    """
    identity = _lineage_identity(cell)
    atomic = [
        blocks[child_id]
        for child_id in children.get(cell.source_id, ())
        if child_id in blocks
        and blocks[child_id].winner is not None
        and _lineage_identity(blocks[child_id]) == identity
        and _coordinates(blocks[child_id]) is None
        and blocks[child_id].raw_text.strip()
    ]
    chosen = atomic or ([cell] if cell.winner is not None and cell.raw_text.strip() else [])
    refs = []
    for block in sorted(chosen, key=lambda item: (item.page_num, item.source_id)):
        try:
            refs.append(block.source_ref())
        except ValueError:  # conflicted candidate has no selected source
            continue
    return tuple(refs)


def table_structural_sources(
    graph: CanonicalDocumentGraph,
    refs: tuple[SourceRef, ...],
    *,
    tenant_id: str,
    max_sources: int = 8,
    max_chars: int = 600,
) -> TableClaimSources:
    """Row/column axis cells for ``refs``, split by ACTUAL verification state.

    ``refs`` are the claim's own literal source spans (or, on the extraction
    side, the focal block's own span). The result is ``unresolved`` whenever no
    ref resolves to an unambiguous coordinate-bearing table cell -- notably for
    a page the parser emitted without any table structure, where inventing a
    row or column would be fabrication.
    """
    _require_uuid("tenant_id", tenant_id)
    if not isinstance(graph, CanonicalDocumentGraph) or not isinstance(refs, tuple) or not refs:
        raise DomainValidationError("canonical graph and claim source refs required")
    if any(not isinstance(ref, SourceRef) for ref in refs):
        raise DomainValidationError("canonical graph and claim source refs required")
    if (
        type(max_sources) is not int
        or not 0 <= max_sources <= 20
        or type(max_chars) is not int
        or not 1 <= max_chars <= 20000
    ):
        raise DomainValidationError("table source bounds invalid")
    blocks = {block.source_id: block for block in graph.blocks}
    if len(blocks) != len(graph.blocks):
        raise DomainValidationError("invalid source lineage")
    parents: dict[str, tuple[str, ...]] = {}
    children: dict[str, tuple[str, ...]] = {}
    for edge in graph.edges:
        if edge.relation != "table_parent":
            continue
        parents[edge.source_id] = (*parents.get(edge.source_id, ()), edge.target_id)
        children[edge.target_id] = (*children.get(edge.target_id, ()), edge.source_id)

    claim_source_ids = {ref.source_id for ref in refs}
    focal_cells: dict[str, tuple[CanonicalBlock, tuple[str, int, int, int, int]]] = {}
    for ref in refs:
        cell = _focal_cell(ref, blocks, parents)
        coordinates = None if cell is None else _coordinates(cell)
        if cell is None or coordinates is None:
            # Hold the whole claim: offering one cell's axes as context for a
            # multi-source claim whose other source did not resolve would attach
            # an association that was never computed.
            return TableClaimSources(lineage="unresolved")
        focal_cells.setdefault(cell.source_id, (cell, coordinates))
    # ponytail: v1 has one focal-cell identity; multi-cell claims need explicit
    # per-source associations before their axes can safely be combined.
    if len(focal_cells) != 1:
        return TableClaimSources(lineage="unresolved")

    found: dict[str, TableClaimSource] = {}
    for cell, coordinates in focal_cells.values():
        siblings = {
            other.source_id: other_coordinates
            for other in graph.blocks
            if (other_coordinates := _coordinates(other)) is not None and _same_table(cell, other)
        }
        header_rows, stub_columns = _table_layout(siblings)
        for other_id, other_coordinates in siblings.items():
            other = blocks[other_id]
            if other_id == cell.source_id or other_id in claim_source_ids:
                continue
            role = _axis_role(coordinates, other_coordinates, header_rows, stub_columns)
            if role is None:
                continue
            table_native_id, row, column, row_span, column_span = other_coordinates
            for ref in _atomic_refs(other, blocks, children):
                if ref.source_id in claim_source_ids or ref.source_id in found:
                    continue
                # Only what verification ACTUALLY returns; "verified" alone may
                # become a numbered source, every other state stays context.
                found[ref.source_id] = TableClaimSource(
                    ref=verify_source_ref(ref, graph, tenant_id=tenant_id),
                    role=role,
                    association=_ASSOCIATION[role],
                    table_native_id=table_native_id,
                    row_number=row,
                    column_number=column,
                    row_span=row_span,
                    column_span=column_span,
                )

    ordered = sorted(
        found.values(),
        key=lambda item: (
            _ROLE_PRIORITY[item.role],
            item.column_number,
            item.row_number,
            item.ref.source_id,
        ),
    )
    chosen: list[TableClaimSource] = []
    omitted: list[str] = []
    used = 0
    for item in ordered[:max_sources]:
        size = len(item.ref.quote)
        if used + size > max_chars:
            omitted.append(item.ref.source_id)  # whole cells only; never truncate a header
            continue
        chosen.append(item)
        used += size
    omitted.extend(item.ref.source_id for item in ordered[max_sources:])
    focal_cell, focal_coordinates = next(iter(focal_cells.values()))
    return TableClaimSources(
        lineage="resolved",
        verified=tuple(item for item in chosen if item.verified),
        context_only=tuple(item for item in chosen if not item.verified),
        omitted_source_ids=tuple(omitted),
        focal_table_native_id=focal_coordinates[0],
        focal_row_number=focal_coordinates[1],
        focal_column_number=focal_coordinates[2],
    )
