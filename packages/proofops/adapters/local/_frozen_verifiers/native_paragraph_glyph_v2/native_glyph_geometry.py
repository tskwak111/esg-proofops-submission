"""Source-bound tight glyph diagnostics; never text or source-quality approval."""

import io
import math
from bisect import bisect_left
from collections import Counter, defaultdict
from contextlib import closing
from ctypes import c_double
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as raw
from pdfminer.pdfexceptions import PDFException
from pdfplumber.utils.exceptions import PdfminerException

from proofops.adapters.local.table_layout_context import validate_word_geometry
from proofops.domain.provenance import canonical_hash

ORIGIN_TOLERANCE_PT = 0.001
MAX_CHARS = 20000


def _union(boxes):
    return [
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    ]


def _valid_box(box, width, height):
    return all(math.isfinite(n) for n in box) and (
        0 <= box[0] < box[2] <= width and 0 <= box[1] < box[3] <= height
    )


def _match_words(page, textpage, indices):
    count = textpage.count_chars()
    if not 0 <= count <= MAX_CHARS or len(page.chars) > MAX_CHARS:
        raise ValueError("native character inventory limit exceeded")
    words = page.extract_words(return_chars=True)
    if indices[-1] >= len(words):
        raise ValueError("native word index outside page")
    inventory = defaultdict(list)
    for index in range(count):
        x, y = c_double(), c_double()
        if raw.FPDFText_GetCharOrigin(textpage, index, x, y) and all(
            math.isfinite(v) for v in (x.value, y.value)
        ):
            inventory[raw.FPDFText_GetUnicode(textpage, index)].append((x.value, y.value, index))
    for entries in inventory.values():
        entries.sort()

    mapped = {}
    for word_index in indices:
        word = words[word_index]
        try:
            validate_word_geometry([word], page.width, page.height)
        except ValueError:
            continue
        if not word["upright"] or not word["chars"]:
            continue
        char_indices, boxes = [], []
        for char in word["chars"]:
            matrix = char["matrix"]
            if (
                len(char["text"]) != 1
                or not char["upright"]
                or len(matrix) != 6
                or not all(math.isfinite(v) for v in matrix)
                or matrix[1] != 0
                or matrix[2] != 0
                or matrix[0] <= 0
                or matrix[3] <= 0
            ):
                break
            x, y = matrix[4:6]
            entries = inventory.get(ord(char["text"]), [])
            start = bisect_left(entries, (x - ORIGIN_TOLERANCE_PT,))
            matches = []
            for position in range(start, len(entries)):
                px, py, index = entries[position]
                if px > x + ORIGIN_TOLERANCE_PT:
                    break
                if math.isclose(px, x, abs_tol=ORIGIN_TOLERANCE_PT, rel_tol=0) and math.isclose(
                    py, y, abs_tol=ORIGIN_TOLERANCE_PT, rel_tol=0
                ):
                    matches.append(index)
                    if len(matches) > 1:
                        break
            if len(matches) != 1:
                break
            index = matches[0]
            try:
                left, bottom, right, top = textpage.get_charbox(index, loose=False)
            except pdfium.PdfiumError:
                break
            box = [left, page.height - top, right, page.height - bottom]
            if not _valid_box(box, page.width, page.height):
                break
            char_indices.append(index)
            boxes.append(box)
        else:
            mapped[word_index] = dict(
                native_word_index=word_index,
                pdfium_char_indices=char_indices,
                font_bbox=[float(word[k]) for k in ("x0", "top", "x1", "bottom")],
                ink_bbox=_union(boxes),
            )
    uses = Counter(i for word in mapped.values() for i in word["pdfium_char_indices"])
    matched = [w for w in mapped.values() if all(uses[i] == 1 for i in w["pdfium_char_indices"])]
    resolved = {w["native_word_index"] for w in matched}
    return matched, [i for i in indices if i not in resolved]


def native_word_ink_geometry(
    source: bytes, page_number: int, native_word_indices: list[int]
) -> dict:
    """Map original pdfplumber chars by Unicode AND origin, with no approximation.

    Physical pages are 1-based; boxes are top-left PDF points. Bad requests,
    unsupported pages and oversized inventories raise ValueError. Glyph failures
    remain unresolved. Tight font glyph boxes do not establish rendered visibility.
    """
    if not isinstance(source, bytes) or not source or len(source) > 100 * 1024 * 1024:
        raise ValueError("invalid native glyph source")
    if type(page_number) is not int or page_number < 1:
        raise ValueError("invalid physical page")
    if (
        not isinstance(native_word_indices, list)
        or not 1 <= len(native_word_indices) <= 1000
        or any(type(i) is not int or i < 0 for i in native_word_indices)
        or sorted(set(native_word_indices)) != native_word_indices
    ):
        raise ValueError("invalid native word indices")
    indices = native_word_indices.copy()
    try:
        with pdfplumber.open(io.BytesIO(source)) as document, pdfium.PdfDocument(source) as native:
            if page_number > len(document.pages) or page_number > len(native):
                raise ValueError("physical page outside document")
            page = document.pages[page_number - 1]
            if (
                page.rotation
                or tuple(page.mediabox[:2]) != (0, 0)
                or tuple(page.cropbox) != tuple(page.mediabox)
                or not all(math.isfinite(v) and v > 0 for v in (page.width, page.height))
            ):
                raise ValueError("native glyph page geometry unsupported")
            with closing(native[page_number - 1]) as native_page:
                media = native_page.get_mediabox()
                if (
                    native_page.get_rotation()
                    or media[:2] != (0, 0)
                    or native_page.get_cropbox() != media
                    or not all(
                        math.isclose(a, b, abs_tol=0.001, rel_tol=0)
                        for a, b in zip(media[2:], (page.width, page.height), strict=True)
                    )
                ):
                    raise ValueError("native glyph page geometry mismatch")
                with closing(native_page.get_textpage()) as textpage:
                    matched, unresolved = _match_words(page, textpage, indices)
    except (PDFException, PdfminerException, pdfium.PdfiumError) as exc:
        raise ValueError("invalid native glyph PDF") from exc
    result = dict(
        schema="native_glyph_geometry_v1",
        source_sha256=sha256(source).hexdigest(),
        physical_page=page_number,
        native_word_indices=indices,
        reader="pdfium",
        reader_version=version("pypdfium2"),
        matcher_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        origin_tolerance_pt=ORIGIN_TOLERANCE_PT,
        matched_words=matched,
        unresolved_word_indices=unresolved,
        ink_bbox=None if unresolved else _union([w["ink_bbox"] for w in matched]),
        status="unresolved" if unresolved else "matched",
    )
    result["proof_sha256"] = canonical_hash(result)
    return result
