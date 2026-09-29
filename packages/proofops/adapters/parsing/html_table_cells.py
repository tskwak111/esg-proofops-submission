from __future__ import annotations

from html.parser import HTMLParser

MAX_HTML_CHARS = 200000
MAX_ROWS = 500
MAX_COLUMNS = 100
MAX_CELLS = 10000


def _span(attrs: list[tuple[str, str | None]], name: str) -> int:
    n = 0
    val: str | None = None
    for k, v in attrs:
        if k.lower() == name:
            n += 1
            val = v
    if n > 1 or val is None:
        if n > 1 or (val is None and n == 1):
            raise ValueError(f"bad {name}")
        return 1
    t = val.strip()
    if not t.isdigit() or int(t) < 1:
        raise ValueError(f"bad {name}")
    return int(t)


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.table_count = 0
        self.closed = False
        self.row_open = False
        self.row_index = 0
        self.cell_tag: str | None = None
        self.parts: list[str] = []
        self.span = (1, 1)
        self.occupied: set[tuple[int, int]] = set()
        self.cells: list[dict] = []
        self.max_row = -1
        self.max_column = -1

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        n = tag.lower()
        if n == "table":
            self.table_count += 1
            if self.table_count > 1 or self.row_open or self.cell_tag is not None:
                raise ValueError("one table only")
        elif n == "tr":
            if self.table_count != 1 or self.closed or self.row_open or self.cell_tag is not None:
                raise ValueError("bad row")
            if self.row_index >= MAX_ROWS:
                raise ValueError("too many rows")
            self.row_open = True
        elif n in ("td", "th"):
            if (
                self.table_count != 1
                or self.closed
                or not self.row_open
                or self.cell_tag is not None
            ):
                raise ValueError("bad cell")
            if len(self.cells) >= MAX_CELLS:
                raise ValueError("too many cells")
            rs, cs = _span(attrs, "rowspan"), _span(attrs, "colspan")
            if cs > MAX_COLUMNS or rs > MAX_ROWS:
                raise ValueError("span too large")
            self.cell_tag, self.parts, self.span = n, [], (rs, cs)
        elif n == "br":
            if self.cell_tag is not None:
                self.parts.append("\n")
        elif n in ("thead", "tbody", "tfoot"):
            if self.table_count != 1 or self.closed:
                raise ValueError("bad section")

    def handle_endtag(self, tag: str) -> None:
        n = tag.lower()
        if n == "table":
            if self.table_count != 1 or self.closed or self.row_open or self.cell_tag is not None:
                raise ValueError("bad table close")
            self.closed = True
        elif n == "tr":
            if not self.row_open or self.cell_tag is not None:
                raise ValueError("bad row close")
            self.row_open = False
            self.row_index += 1
        elif n in ("td", "th"):
            if self.cell_tag != n:
                raise ValueError("bad cell close")
            rs, cs = self.span
            r = self.row_index
            c = 0
            while (r, c) in self.occupied:
                c += 1
            if c + cs > MAX_COLUMNS or r + rs > MAX_ROWS:
                raise ValueError("table too large")
            for dr in range(rs):
                for dc in range(cs):
                    if (r + dr, c + dc) in self.occupied:
                        raise ValueError("overlap")
                    self.occupied.add((r + dr, c + dc))
            self.max_row = max(self.max_row, r + rs - 1)
            self.max_column = max(self.max_column, c + cs - 1)
            self.cells.append(
                {
                    "text": "".join(self.parts).strip(),
                    "row": r,
                    "column": c,
                    "row_span": rs,
                    "column_span": cs,
                }
            )
            self.cell_tag, self.parts, self.span = None, [], (1, 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "br":
            if self.cell_tag is not None:
                self.parts.append("\n")
            return
        self.handle_starttag(tag, attrs)
        if tag.lower() in ("td", "th", "tr", "table"):
            self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.cell_tag is not None:
            self.parts.append(data)


def parse_table_cells(html: str) -> tuple[dict, ...]:
    if not isinstance(html, str) or not html.strip():
        raise ValueError("invalid html")
    if len(html) > MAX_HTML_CHARS:
        raise ValueError("html too large")
    p = _TableParser()
    p.feed(html)
    p.close()
    if p.table_count != 1 or not p.closed or p.row_open or p.cell_tag is not None or not p.cells:
        raise ValueError("one closed table required")
    nr, nc = p.max_row + 1, p.max_column + 1
    if nr != p.row_index or nr > MAX_ROWS or nc > MAX_COLUMNS or len(p.cells) > MAX_CELLS:
        raise ValueError("table too large")
    for r in range(nr):
        for c in range(nc):
            if (r, c) not in p.occupied:
                raise ValueError("ragged grid")
    return tuple(p.cells)
