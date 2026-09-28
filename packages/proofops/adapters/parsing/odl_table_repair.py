"""Repair OpenDataLoader stacked header/value cells using native word geometry.

Only splits a first-row stacking when native words prove a real horizontal gap:
every word inside a stacked first-row cell is fully above or fully below the
gap (full word containment), no word straddles, header/value kid bboxes each
contain at least one word on their side, and the resulting header/data bboxes
are non-overlapping. Otherwise the raw input is returned unchanged.

Pure helper: no I/O, no network, no invented text. Split bboxes reuse exact
kid bbox unions; original ids are retained in receipts (and the spanning unit
body cell keeps its original cell id).
"""

from __future__ import annotations

import copy
import math
import unicodedata
from typing import Any, cast

_EPS = 0.001  # ODL coordinates round to three decimal places.
_MIN_GAP = 0.5


def _squash(text: Any) -> str:
    return " ".join(unicodedata.normalize("NFC", str(text)).split())


def _owns(slot: list | tuple, box: list | tuple) -> bool:
    """pdfplumber's own assignment rule: the midpoint of the ink is in the slot."""
    horizontal = (box[0] + box[2]) / 2
    vertical = (box[1] + box[3]) / 2
    return slot[0] <= horizontal < slot[2] and slot[1] <= vertical < slot[3]


def locate_auxiliary_cells(
    nodes: list, words_by_page: dict, *, source: bytes | None = None
) -> tuple[list, list]:
    """Snap auxiliary (pdfplumber) table slots onto the ink of the words they own.

    pdfplumber assigns a cell's text by ink MIDPOINT containment, so a detected
    slot legitimately carries text its own box does not contain. On a real
    borderless numeric strip (Lotte p117) every slot sat about 3.4pt below its own
    text, so each declared box straddled two values: the declared text and the
    declared geometry described different ink, which no source verifier may
    accept. This replaces each slot box with the exact ink union of the words it
    owns under that same midpoint rule, so box and text describe the same ink.

    A table is repaired only when ownership is exact. It is left completely
    untouched when a slot with text owns no word, a slot without text owns a word,
    a word is owned by two slots, the owned words do not join to the declared cell
    text, or the snapped boxes would overlap. Nothing is invented: boxes are
    unions of real word boxes and text is never rewritten.

    ``nodes`` are auxiliary table nodes and ``words_by_page`` maps page number to
    ``[{bbox, text}]``, both in the same pdfplumber top-left media point space.
    Returns ``(nodes, receipts)``; the input is not mutated.
    """
    repaired = copy.deepcopy(nodes)
    receipts: list[dict] = []
    for table in repaired:
        if not isinstance(table, dict) or table.get("type") != "table":
            continue
        page = table.get("page number")
        words = _norm_words(words_by_page, page)
        receipt = dict(table_id=table.get("id"), page=page, status="unrepaired", reason=None)
        receipts.append(receipt)
        if words is None:
            receipt["reason"] = "words_unavailable"
            continue
        if source is not None and words and any("ink_bbox" not in w for w in words):
            try:
                from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry

                ink_res = native_word_ink_geometry(
                    source, int(cast(Any, page)), list(range(len(words)))
                )
                matched_map = {
                    m["native_word_index"]: m["ink_bbox"] for m in ink_res.get("matched_words", [])
                }
                unresolved_set = set(ink_res.get("unresolved_word_indices", []))
                for idx, w in enumerate(words):
                    if idx in matched_map:
                        w["ink_bbox"] = matched_map[idx]
                        w["resolved"] = idx not in unresolved_set
                    else:
                        w["resolved"] = False
            except Exception:
                pass
        cells = [
            cell
            for row in table.get("rows") or []
            if isinstance(row, dict)
            for cell in row.get("cells") or []
            if isinstance(cell, dict)
        ]
        if not cells or not all(_is_finite_box(cell.get("bounding box")) for cell in cells):
            receipt["reason"] = "cell_geometry_unusable"
            continue
        owners: dict[int, list[dict]] = {}
        claimed: dict[int, int] = {}
        conflict = None
        for index, cell in enumerate(cells):
            slot = cell["bounding box"]
            owned = []
            for position, word in enumerate(words):
                if not _owns(slot, word["bbox"]):
                    continue
                if position in claimed:
                    conflict = "word_owned_by_two_slots"
                claimed[position] = index
                owned.append(word)
            owners[index] = owned
        if conflict:
            receipt["reason"] = conflict
            continue
        boxes = {}
        for index, cell in enumerate(cells):
            owned = owners[index]
            content = _squash(cell.get("content", ""))
            if not owned:
                if content:
                    conflict = "cell_text_without_words"
                    break
                continue
            joined = _squash(
                " ".join(
                    word["text"]
                    for word in sorted(owned, key=lambda w: (round(w["bbox"][1], 1), w["bbox"][0]))
                )
            )
            if not content or joined != content:
                conflict = "cell_text_word_mismatch"
                break
            if any(w.get("resolved") is False for w in owned):
                conflict = "unresolved_glyph_geometry"
                break
            boxes[index] = _union([w.get("ink_bbox") or w["bbox"] for w in owned])
        if conflict:
            receipt["reason"] = conflict
            continue
        if not boxes:
            receipt["reason"] = "no_located_cell"
            continue
        ordered = sorted(boxes.items())
        for left, (_, first) in enumerate(ordered):
            for _, second in ordered[left + 1 :]:
                if (
                    min(first[2], second[2]) - max(first[0], second[0]) > _EPS
                    and min(first[3], second[3]) - max(first[1], second[1]) > _EPS
                ):
                    conflict = "located_boxes_overlap"
        if conflict:
            receipt["reason"] = conflict
            continue
        located = []
        for index, cell in enumerate(cells):
            if index in boxes:
                located.append(
                    dict(cell_id=cell.get("id"), was=cell["bounding box"], now=boxes[index])
                )
                cell["bounding box"] = boxes[index]
        # Empty slots keep no geometry of their own: drop them so the graph never
        # carries a box that owns no ink. Rows/table reduce to their located ink.
        index = 0
        rows = []
        for row in table.get("rows") or []:
            kept = []
            for cell in row.get("cells") or []:
                if isinstance(cell, dict):
                    if index in boxes:
                        kept.append(cell)
                    index += 1
            if not kept:
                continue
            row["cells"] = kept
            row["bounding box"] = _union([cell["bounding box"] for cell in kept])
            rows.append(row)
        if not rows:
            receipt["reason"] = "no_located_row"
            continue
        table["rows"] = rows
        table["bounding box"] = _union([row["bounding box"] for row in rows])
        if isinstance(table.get("number of rows"), int):
            table["number of rows"] = len(rows)
        receipt.update(
            status="repaired",
            located_cells=located,
            dropped_empty_cells=len(cells) - len(boxes),
            table_bbox=table["bounding box"],
        )
    return repaired, receipts


def _is_finite_box(box: Any) -> bool:
    return (
        isinstance(box, list | tuple)
        and len(box) == 4
        and all(type(v) in (int, float) and math.isfinite(v) for v in box)
        and box[0] < box[2]
        and box[1] < box[3]
    )


def _contains(outer: list | tuple, inner: list | tuple, eps: float = _EPS) -> bool:
    return (
        outer[0] - eps <= inner[0]
        and outer[1] - eps <= inner[1]
        and inner[2] <= outer[2] + eps
        and inner[3] <= outer[3] + eps
    )


def _union(boxes: list[list | tuple]) -> list:
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _norm_words(words_by_page: Any, page: Any) -> list[dict] | None:
    if not isinstance(words_by_page, dict):
        return None
    words = words_by_page.get(page)
    if words is None:
        words = words_by_page.get(str(page))
    if words is None:
        for key, value in words_by_page.items():
            if str(key) == str(page):
                words = value
                break
    if not isinstance(words, list) or not words:
        return None
    norm: list[dict] = []
    for w in words:
        if not isinstance(w, dict):
            return None
        box = w.get("bbox")
        text = w.get("text")
        if (
            not isinstance(box, list | tuple)
            or not _is_finite_box(box)
            or not isinstance(text, str)
            or not text.strip()
        ):
            continue
        entry: dict[str, Any] = {"bbox": [float(v) for v in box], "text": text}
        if "ink_bbox" in w and _is_finite_box(w["ink_bbox"]):
            entry["ink_bbox"] = [float(v) for v in w["ink_bbox"]]
        if "resolved" in w:
            entry["resolved"] = bool(w["resolved"])
        norm.append(entry)
    return norm or None


def _max_id(raw: dict) -> int:
    best = 0
    stack = [raw]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            nid = node.get("id")
            if type(nid) is int and nid > best:
                best = nid
            for key in ("kids", "rows", "cells", "list items"):
                children = node.get(key)
                if isinstance(children, list):
                    stack.extend(c for c in children if isinstance(c, dict))
        elif isinstance(node, list):
            stack.extend(c for c in node if isinstance(c, dict))
    return best


def repair_stacked_headers(
    raw: dict, words_by_page: dict, *, locate_rows: bool = False
) -> tuple[dict, list[dict]]:
    """Split stacked first-row header/value cells where geometry proves a gap.

    Args:
        raw: ODL ``source.json`` shape (``{"kids": [...]}``) in ODL
            bottom-left points. Not mutated.
        words_by_page: ``{page: [{bbox, text}]}`` with bboxes already in ODL
            bottom-left points (caller converts native pdfplumber words and
            restricts to rotation-0 crop==media origin-0 pages).

    Returns:
        ``(new_raw, receipts)``. ``new_raw`` is a deep copy; unchanged tables
        are left intact. One receipt per repaired table.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("kids"), list):
        raise ValueError("raw must be an ODL source dict with a kids list")
    if not isinstance(words_by_page, dict):
        raise ValueError("words_by_page must be a dict")
    new_raw: dict = copy.deepcopy(raw)
    receipts: list[dict] = []
    next_id = _max_id(new_raw) + 1

    for table_index, original_table in enumerate(new_raw.get("kids", [])):
        table = copy.deepcopy(original_table)
        if not isinstance(table, dict) or table.get("type") != "table":
            continue
        rows = table.get("rows")
        if not isinstance(rows, list) or not rows:
            continue
        rows_sorted = sorted(
            [r for r in rows if isinstance(r, dict)],
            key=lambda r: r.get("row number", 0),
        )
        first = rows_sorted[0]
        page = first.get("page number", table.get("page number"))
        first_cells = first.get("cells")
        if not isinstance(first_cells, list) or not first_cells:
            continue
        stacked: list[dict] = []
        for cell in first_cells:
            if not isinstance(cell, dict):
                continue
            kids = cell.get("kids")
            if not isinstance(kids, list):
                continue
            headings = [
                k
                for k in kids
                if isinstance(k, dict)
                and k.get("type") == "heading"
                and _is_finite_box(k.get("bounding box"))
            ]
            paras = [
                k
                for k in kids
                if isinstance(k, dict)
                and k.get("type") == "paragraph"
                and _is_finite_box(k.get("bounding box"))
            ]
            if not headings or not paras or len(headings) + len(paras) != len(kids):
                continue
            head_bottom = min(k["bounding box"][1] for k in headings)
            para_top = max(k["bounding box"][3] for k in paras)
            if head_bottom - para_top < _MIN_GAP:
                continue
            stacked.append(
                {
                    "cell": cell,
                    "headings": headings,
                    "paras": paras,
                    "head_bottom": float(head_bottom),
                    "para_top": float(para_top),
                }
            )
        if len(stacked) < 2 or len(stacked) != len(first_cells):
            continue
        gap_low = max(s["para_top"] for s in stacked)
        gap_high = min(s["head_bottom"] for s in stacked)
        if gap_high - gap_low < _MIN_GAP:
            continue
        words = _norm_words(words_by_page, page)
        if words is None:
            continue
        # Validate every stacked cell against native words.
        valid = True
        for s in stacked:
            cell = s["cell"]
            cbox = cell.get("bounding box")
            if not _is_finite_box(cbox):
                valid = False
                break
            touching = [
                w
                for w in words
                if (
                    min(cbox[2], w["bbox"][2]) - max(cbox[0], w["bbox"][0]) > _EPS
                    and min(cbox[3], w["bbox"][3]) - max(cbox[1], w["bbox"][1]) > _EPS
                )
            ]
            if any(not _contains(cbox, w["bbox"]) for w in touching):
                valid = False
                break
            inside = touching
            if not inside:
                valid = False
                break
            above = [w for w in inside if w["bbox"][1] >= gap_high - _EPS]
            below = [w for w in inside if w["bbox"][3] <= gap_low + _EPS]
            if len(above) + len(below) != len(inside):
                valid = False  # straddler in the gap
                break
            if not above or not below:
                valid = False
                break
            for k in s["headings"]:
                if not any(_contains(k["bounding box"], w["bbox"]) for w in above):
                    valid = False
                    break
            if not valid:
                break
            for k in s["paras"]:
                # Spanning unit paragraph (e.g. TJ) lives inside the tall cell
                # but far below the first-row band; it still must contain one
                # of the below words. Paragraphs of the first data record do.
                if not any(_contains(k["bounding box"], w["bbox"]) for w in below):
                    valid = False
                    break
            if not valid:
                break
            for kid in s["headings"] + s["paras"]:
                owned = sorted(
                    (w for w in inside if _contains(kid["bounding box"], w["bbox"])),
                    key=lambda w: (-w["bbox"][3], w["bbox"][0]),
                )

                def normalize(text):
                    return "".join(unicodedata.normalize("NFC", text).split())

                content = kid.get("content")
                if not isinstance(content, str) or normalize(content) != normalize(
                    " ".join(w["text"] for w in owned)
                ):
                    valid = False
                    break
            if not valid:
                break
            for w in inside:
                in_head = any(_contains(k["bounding box"], w["bbox"]) for k in s["headings"])
                in_para = any(_contains(k["bounding box"], w["bbox"]) for k in s["paras"])
                if not (in_head or in_para):
                    valid = False  # word in cell but outside kid boxes: ambiguous
                    break
            if not valid:
                break
        if not valid:
            continue
        # Build split rows. Bboxes reuse exact kid unions (no invention).
        header_cells: list[dict] = []
        data_cells: list[dict] = []
        cell_map: list[dict] = []
        for s in stacked:
            cell = s["cell"]
            orig_id = cell.get("id")
            cbox = cell["bounding box"]
            head_box = _union([k["bounding box"] for k in s["headings"]])
            para_box = _union([k["bounding box"] for k in s["paras"]])
            # Non-overlap guard on derived bboxes.
            if not (head_box[1] >= para_box[3] + _MIN_GAP - _EPS):
                valid = False
                break
            is_span = int(cell.get("row span", 1) or 1) > 1
            if is_span:
                # Header unit cell is new; body keeps the original cell id so
                # the TJ lineage is preserved across data rows.
                h_id = next_id
                next_id += 1
                header_cells.append(
                    {
                        "type": "table cell",
                        "id": h_id,
                        "page number": cell.get("page number", page),
                        "bounding box": head_box,
                        "row number": 1,
                        "column number": cell.get("column number"),
                        "row span": 1,
                        "column span": cell.get("column span", 1),
                        "kids": copy.deepcopy(s["headings"]),
                        "repaired_from": orig_id,
                    }
                )
                cell["bounding box"] = para_box
                cell["row number"] = 2
                cell["kids"] = copy.deepcopy(s["paras"])
                # row span keeps covering the data rows; column kept.
                data_cells.append(cell)
                cell_map.append(
                    {
                        "orig_cell": orig_id,
                        "header_cell": h_id,
                        "data_cell": orig_id,
                        "header_bbox": head_box,
                        "data_bbox": para_box,
                        "spanning": True,
                    }
                )
            else:
                h_id = next_id
                next_id += 1
                d_id = next_id
                next_id += 1
                header_cells.append(
                    {
                        "type": "table cell",
                        "id": h_id,
                        "page number": cell.get("page number", page),
                        "bounding box": head_box,
                        "row number": 1,
                        "column number": cell.get("column number"),
                        "row span": 1,
                        "column span": cell.get("column span", 1),
                        "kids": copy.deepcopy(s["headings"]),
                        "repaired_from": orig_id,
                    }
                )
                data_cells.append(
                    {
                        "type": "table cell",
                        "id": d_id,
                        "page number": cell.get("page number", page),
                        "bounding box": para_box,
                        "row number": 2,
                        "column number": cell.get("column number"),
                        "row span": 1,
                        "column span": cell.get("column span", 1),
                        "kids": copy.deepcopy(s["paras"]),
                        "repaired_from": orig_id,
                    }
                )
                cell_map.append(
                    {
                        "orig_cell": orig_id,
                        "header_cell": h_id,
                        "data_cell": d_id,
                        "header_bbox": head_box,
                        "data_bbox": para_box,
                        "spanning": False,
                    }
                )
        if not valid:
            continue
        # Remove original stacked cells from the first row object; it becomes
        # the second (data) row. Non-stacked first-row cells cannot exist in
        # the observed stacked tables; abort rather than guess if they do.
        first_cell_ids = {id(c) for c in first_cells}
        stacked_ids = {id(s["cell"]) for s in stacked}
        if first_cell_ids != stacked_ids:
            continue
        header_row_id = next_id
        next_id += 1
        header_row_box = _union([c["bounding box"] for c in header_cells])
        data_row_box = _union([c["bounding box"] for c in data_cells])
        if not (header_row_box[1] >= data_row_box[3] + _MIN_GAP - _EPS):
            continue
        header_row = {
            "type": "table row",
            "id": header_row_id,
            "row number": 1,
            "cells": header_cells,
            "bounding box": header_row_box,
            "page number": page,
            "repaired_from": first.get("id"),
        }
        first["cells"] = data_cells
        first["row number"] = 2
        first["bounding box"] = data_row_box
        # Renumber following rows (data rows shift by one).
        for r in rows_sorted[1:]:
            if isinstance(r.get("row number"), int):
                r["row number"] = int(r["row number"]) + 1
            for c in r.get("cells", []) or []:
                if isinstance(c, dict) and isinstance(c.get("row number"), int):
                    c["row number"] = int(c["row number"]) + 1
        table["rows"] = [header_row] + rows_sorted
        if isinstance(table.get("number of rows"), int):
            table["number of rows"] = int(table["number of rows"]) + 1
        located_rows = []
        if locate_rows:
            for row in table["rows"]:
                boxes = [cell.get("bounding box") for cell in row["cells"]]
                if (
                    row.get("bounding box") is None
                    and boxes
                    and all(_is_finite_box(b) for b in boxes)
                ):
                    row["bounding box"] = _union(boxes)
                    located_rows.append(
                        dict(
                            row_id=row["id"],
                            cell_ids=[c["id"] for c in row["cells"]],
                            bbox=row["bounding box"],
                        )
                    )
        new_raw["kids"][table_index] = table
        receipts.append(
            {
                "table_id": table.get("id"),
                "page": page,
                "gap": [gap_low, gap_high],
                "header_row_id": header_row_id,
                "data_row_id": first.get("id"),
                "cell_map": cell_map,
                "words_checked": len(words),
                "lineage": {
                    "table": table.get("id"),
                    "orig_first_row": first.get("id"),
                    "orig_cells": [
                        s["cell"].get("repaired_from", s["cell"].get("id")) for s in stacked
                    ],
                },
            }
        )
        if locate_rows:
            receipts[-1]["located_rows"] = located_rows
    return new_raw, receipts
