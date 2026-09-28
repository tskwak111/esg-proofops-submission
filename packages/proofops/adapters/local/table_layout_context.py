"""Original-PDF word geometry for selected tables; context only, never cell repairs."""

import io
import math
import re
from collections import Counter
from copy import deepcopy
from hashlib import sha256
from importlib.metadata import version

import pdfplumber

from proofops.application.ingest.gri import _validate_graph


def validate_word_geometry(words, width, height):
    for word in words:
        box = [word[k] for k in ("x0", "top", "x1", "bottom")]
        if any(type(n) not in (int, float) or not math.isfinite(n) for n in box) or not (
            0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
        ):
            raise ValueError("layout word geometry unsupported")


def _typed_word(word, index):
    return dict(
        index=index,
        text=word["text"],
        bbox=[word["x0"], word["top"], word["x1"], word["bottom"]],
        characters=[
            dict(text=c["text"], size=c["size"], bbox=[c["x0"], c["top"], c["x1"], c["bottom"]])
            for c in word["chars"]
        ],
    )


def separate_markers(native_words, box):
    """Preserve a unique nearby base/raised-marker pair; not a cell assignment."""
    inside = [
        (i, w)
        for i, w in enumerate(native_words)
        if w["upright"]
        and box[0] <= w["x0"] < w["x1"] <= box[2]
        and box[1] <= w["top"] < w["bottom"] <= box[3]
    ]
    result = []
    for index, word in inside:
        if not re.fullmatch(r"[1-9][0-9]?\)", word["text"]) or not word["chars"]:
            continue
        # ponytail: scan the bounded page words per marker; spatial index if large pages need it.
        bases = [
            (i, base)
            for i, base in inside
            if i != index
            and base["chars"]
            and 0 <= word["x0"] - base["x1"] <= base["bottom"] - base["top"]
            and word["bottom"] > base["top"]
            and all(
                c["size"] < base["chars"][-1]["size"]
                and c["top"] < base["chars"][-1]["top"]
                and c["bottom"] < base["chars"][-1]["bottom"]
                for c in word["chars"]
            )
        ]
        if bases:
            closest = max(base["x1"] for _, base in bases)
            bases = [(i, base) for i, base in bases if closest - base["x1"] <= 0.1]
        if len(bases) == 1:
            i, base = bases[0]
            result.append(dict(base=_typed_word(base, i), marker=_typed_word(word, index)))
    return result


def group_words(words):
    """Separate same-line runs at a gap larger than the local text height.

    # ponytail: whitespace grouping is a layout hint, not a column/status classifier.
    Reading order, semantic ownership and scans still require separate review.
    """
    if len(words) > 1000:
        raise ValueError("layout word limit")
    indices = set()
    for w in words:
        box = w.get("bbox", [])
        if (
            type(w.get("index")) is not int
            or w["index"] < 0
            or w["index"] in indices
            or not isinstance(w.get("text"), str)
            or not w["text"]
            or len(box) != 4
            or any(type(n) not in (int, float) or not math.isfinite(n) for n in box)
            or not (0 <= box[0] < box[2] and 0 <= box[1] < box[3])
        ):
            raise ValueError("invalid layout word")
        indices.add(w["index"])
    lines = []
    for w in sorted(words, key=lambda w: (w["bbox"][1], w["bbox"][0], w["index"])):
        height = w["bbox"][3] - w["bbox"][1]
        line = next(
            (
                line
                for line in reversed(lines)
                if abs(line[0]["bbox"][1] - w["bbox"][1])
                <= min(height, line[0]["bbox"][3] - line[0]["bbox"][1]) * 0.25
            ),
            None,
        )
        if line is None:
            lines.append([w])
        else:
            line.append(w)
    runs = []
    for line in lines:
        run = []
        for w in sorted(line, key=lambda w: (w["bbox"][0], w["index"])):
            if run and w["bbox"][0] - run[-1]["bbox"][2] > max(
                w["bbox"][3] - w["bbox"][1], run[-1]["bbox"][3] - run[-1]["bbox"][1]
            ):
                runs.append(run)
                run = []
            run.append(w)
        runs.append(run)
    return [
        dict(
            text=" ".join(w["text"] for w in run),
            word_indices=[w["index"] for w in run],
            bbox=[
                min(w["bbox"][0] for w in run),
                min(w["bbox"][1] for w in run),
                max(w["bbox"][2] for w in run),
                max(w["bbox"][3] for w in run),
            ],
        )
        for run in runs
    ]


def cell_source_matches(cells, layout):
    """Unique whole-fragment text proposals from an already source-validated layout.

    Whitespace is collapsed only for matching; native text/word IDs/boxes remain
    unchanged. This never upgrades canonical cells or resolves their semantics.
    """
    counts = Counter(" ".join(c["text"].split()) for c in cells)
    native_counts = Counter(" ".join(f["text"].split()) for f in layout["fragments"])
    by_text = {}
    clipped = set(layout["clipped_word_indices"])
    for fragment in layout["fragments"]:
        if not clipped.intersection(fragment["word_indices"]):
            by_text.setdefault(" ".join(fragment["text"].split()), []).append(fragment)
    result = {}
    for cell in cells:
        key = f"r{cell['row']}c{cell['column']}"
        if key in result:
            raise ValueError("duplicate cell position")
        text = " ".join(cell["text"].split())
        fragments = by_text.get(text, []) if text else []
        result[key] = dict(
            status="not_found"
            if not fragments
            else "unique_proposal"
            if len(fragments) == counts[text] == native_counts[text] == 1
            else "ambiguous",
            fragments=deepcopy(fragments),
            verified=False,
        )
    return result


def table_layout_context(graph, source, table_ids, *, tenant_id, allow_unresolved=False):
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
    ):
        raise ValueError("layout source mismatch")
    blocks = {b.source_id: b for b in graph.blocks}
    if not table_ids or len(table_ids) > 10 or len(set(table_ids)) != len(table_ids):
        raise ValueError("layout table limit")
    result = []
    with pdfplumber.open(io.BytesIO(source)) as document:
        for sid in sorted(table_ids):
            table = blocks.get(sid)
            if table is None or table.kind != "table":
                raise ValueError("located unconflicted table required")
            unresolved = table.winner is None or table.bbox is None
            if unresolved and not allow_unresolved:
                raise ValueError("located unconflicted table required")
            page = document.pages[table.page_num - 1]
            if (
                page.rotation
                or tuple(page.bbox[:2]) != (0, 0)
                or tuple(page.cropbox) != tuple(page.mediabox)
            ):
                raise ValueError("layout geometry unsupported")
            if not unresolved:
                geometry = table.candidates[table.winner].geometry
                if (
                    geometry.rotation
                    or tuple(geometry.crop_box[:2]) != (0, 0)
                    or abs(page.width - geometry.width_pt) > 0.001
                    or abs(page.height - geometry.height_pt) > 0.001
                ):
                    raise ValueError("layout geometry unsupported")
            box = None if unresolved else table.bbox
            words, outside, styled = [], [], []
            clipped, unreadable = [], []
            native_words = page.extract_words(return_chars=True)
            validate_word_geometry(native_words, page.width, page.height)
            for index, w in enumerate(native_words):
                wb = [w["x0"], w["top"], w["x1"], w["bottom"]]
                if not w["upright"]:
                    unreadable.append(index)
                word = dict(index=index, text=w["text"], bbox=wb)
                if box is None:
                    outside.append(word)
                    continue
                if not (box[0] <= wb[0] <= wb[2] <= box[2] and box[1] <= wb[1] <= wb[3] <= box[3]):
                    outside.append(word)
                if wb[2] <= box[0] or wb[0] >= box[2] or wb[3] <= box[1] or wb[1] >= box[3]:
                    continue
                if not w["upright"]:
                    raise ValueError("rotated layout words unsupported")
                words.append(word)
                # Preserve typography, not a guessed footnote marker (e.g. Scope1).
                if (
                    len(
                        {
                            tuple(round(c[k], 3) for k in ("size", "top", "bottom"))
                            for c in w["chars"]
                        }
                    )
                    > 1
                ):
                    styled.append(_typed_word(w, index))
                if not (box[0] <= wb[0] <= wb[2] <= box[2] and box[1] <= wb[1] <= wb[3] <= box[3]):
                    clipped.append(index)
            if len(outside) > 1000:
                raise ValueError("layout word limit")
            result.append(
                dict(
                    table_id=sid,
                    page=table.page_num,
                    bbox=box,
                    source_sha256=graph.source_sha256,
                    coordinate_system="pdf_top_left_points",
                    reader="pdfplumber",
                    reader_version=version("pdfplumber"),
                    words=words,
                    styled_words=styled,
                    separate_markers=[] if box is None else separate_markers(native_words, box),
                    fragments=group_words(words),
                    clipped_word_indices=clipped,
                    page_context=dict(
                        source_sha256=graph.source_sha256,
                        page=table.page_num,
                        words=outside,
                        fragments=group_words([w for w in outside if w["index"] not in unreadable]),
                        unreadable_word_indices=unreadable,
                        status="partial_unreadable"
                        if unreadable
                        else "candidate_only"
                        if outside
                        else "no_native_words",
                        ownership="unassigned",
                        scope="same_page_table_location_unresolved"
                        if unresolved
                        else "same_page_outside_table_including_boundary_words",
                        completeness="native_words_only",
                        verified=False,
                    ),
                    status="unresolved_table"
                    if unresolved
                    else "candidate_only"
                    if words
                    else "unreadable",
                    verified=False,
                )
            )
    return result


def table_crop(graph, source, table_id, *, tenant_id):
    """Render one source-checked, unrotated table for a bounded image-input probe."""
    import pypdfium2 as pdfium

    layout = table_layout_context(graph, source, [table_id], tenant_id=tenant_id)[0]
    scale = 3
    with pdfium.PdfDocument(source) as document:
        page = document[layout["page"] - 1]
        try:
            if page.get_width() * page.get_height() * scale**2 > 16_000_000:
                raise ValueError("layout render pixel limit")
            box = layout["bbox"]
            pixels = [
                math.floor(box[0] * scale),
                math.floor(box[1] * scale),
                math.ceil(box[2] * scale),
                math.ceil(box[3] * scale),
            ]
            bitmap = page.render(scale=scale)
            try:
                if not (
                    0 <= pixels[0] < pixels[2] <= bitmap.width
                    and 0 <= pixels[1] < pixels[3] <= bitmap.height
                ):
                    raise ValueError("layout crop outside page")
                with bitmap.to_pil() as image:
                    with image.crop(pixels) as cropped:
                        output = io.BytesIO()
                        cropped.save(output, format="PNG")
            finally:
                bitmap.close()
        finally:
            page.close()
    png = output.getvalue()
    return png, dict(
        table_id=table_id,
        source_sha256=graph.source_sha256,
        tenant_id=graph.tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        page=layout["page"],
        table_bbox=box,
        page_context=layout["page_context"],
        pixel_bbox=pixels,
        scale=scale,
        coordinate_system="pdf_top_left_points",
        image_sha256=sha256(png).hexdigest(),
        renderer="pypdfium2",
        renderer_version=version("pypdfium2"),
        verified=False,
    )
