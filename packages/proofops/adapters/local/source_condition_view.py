"""Original display receipts; rendering never approves text, ownership or conditions."""

import io
import json
from contextlib import closing
from dataclasses import asdict
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pdfplumber
import pypdfium2 as pdfium
import pypdfium2.raw as raw

from proofops.adapters.local.table_layout_context import validate_word_geometry
from proofops.adapters.local.table_notes import replay_note_reviews
from proofops.adapters.parsing import source_preview
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.documents import PageGeometry
from proofops.domain.provenance import canonical_hash


def _intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _glyph_selection(source, page_number, box, words):
    """Require complete original glyph coverage inside the candidate, not page approval."""
    from proofops.adapters.local.native_glyph_geometry import (
        _valid_box,
        native_word_ink_geometry,
    )

    proof = native_word_ink_geometry(source, page_number, list(range(len(words))))
    for index in proof["unresolved_word_indices"]:
        if _intersects([words[index][k] for k in ("x0", "top", "x1", "bottom")], box):
            raise ValueError("unresolved native word overlaps source view")
    selected = [w for w in proof["matched_words"] if _intersects(w["ink_bbox"], box)]
    if not selected or any(
        not (
            box[0] <= w["ink_bbox"][0] < w["ink_bbox"][2] <= box[2]
            and box[1] <= w["ink_bbox"][1] < w["ink_bbox"][3] <= box[3]
        )
        for w in selected
    ):
        raise ValueError("source view glyph clipping mismatch")
    ordered = [i for w in selected for i in w["pdfium_char_indices"]]
    if ordered != sorted(set(ordered)):
        raise ValueError("source view glyph order ambiguous")
    covered = set(ordered)
    try:
        with pdfium.PdfDocument(source) as document, closing(document[page_number - 1]) as page:
            with closing(page.get_textpage()) as text:
                for index in range(text.count_chars()):
                    if chr(raw.FPDFText_GetUnicode(text, index)).isspace():
                        continue
                    left, bottom, right, top = text.get_charbox(index, loose=False)
                    bounds = [left, page.get_height() - top, right, page.get_height() - bottom]
                    if not _valid_box(bounds, page.get_width(), page.get_height()):
                        raise ValueError("source view glyph inventory unlocated")
                    if _intersects(bounds, box) and index not in covered:
                        raise ValueError("source view glyph inventory incomplete")
                characters = []
                for index in range(ordered[0], ordered[-1] + 1):
                    character = chr(raw.FPDFText_GetUnicode(text, index))
                    if index not in covered and not character.isspace():
                        raise ValueError("source view glyph text interrupted")
                    characters.append(character)
    except pdfium.PdfiumError as exc:
        raise ValueError("source view glyph reader unavailable") from exc
    selection = dict(
        schema="canonical_glyph_selection_v1",
        source_sha256=proof["source_sha256"],
        physical_page=page_number,
        full_page_mapping_sha256=proof["proof_sha256"],
        matched_words=selected,
    )
    selection["proof_sha256"] = canonical_hash(selection)
    return selection, [w["native_word_index"] for w in selected], "".join(characters)


def render_run_fragment(store, uploads, parser, fragment, *, tenant_id, run_id):
    """Runtime entry: only original bytes and notes in the committed run are viewable."""
    from proofops.adapters.local.run_artifacts import load_run_evidence

    evidence = load_run_evidence(store, uploads, parser, tenant_id=tenant_id, run_id=run_id)
    if not isinstance(fragment, dict):
        raise ValueError("invalid source fragment")
    artifact = None
    if "note_artifact_sha256" in fragment:
        matches = [
            a
            for a in evidence["note_reviews"]
            if json.loads(a)["artifact_sha256"] == fragment["note_artifact_sha256"]
        ]
        if len(matches) != 1:
            raise ValueError("note artifact is not published in this run")
        artifact = matches[0]
    receipt, png = render_fragment(
        evidence["base_graph"],
        evidence["source"].content,
        fragment,
        tenant_id=tenant_id,
        note_artifact=artifact,
    )
    receipt.pop("receipt_sha256")
    receipt.update(
        run_id=run_id,
        run_input_hash=evidence["input_hash"],
        published_graph_sha256=canonical_hash(asdict(evidence["graph"])),
    )
    receipt["receipt_sha256"] = canonical_hash(receipt)
    return receipt, png


def render_fragment(graph, source, fragment, *, tenant_id, note_artifact=None):
    """Return a pinned receipt and its full-page PNG for explicit source review.

    graph is the trusted base parser graph, before runtime-note issue decoration.
    Fragments must come from the application's pinned inventory. This boundary
    independently checks source membership; it never edits that graph's quality.
    """
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
        or not isinstance(fragment, dict)
    ):
        raise ValueError("source view input mismatch")
    candidate = None
    glyph_geometry = None
    glyph_selection_word_indices = None
    if set(fragment) == {
        "source_id",
        "parser_run_id",
        "source_native_id",
        "char_start",
        "char_end",
        "raw_text_sha256",
    }:
        if note_artifact is not None:
            raise ValueError("canonical fragment cannot borrow a note artifact")
        block = next((b for b in graph.blocks if b.source_id == fragment["source_id"]), None)
        if block is None:
            raise ValueError("source fragment not found")
        matches = [
            c
            for c in block.candidates
            if (
                c.source.parser_run_id,
                c.source.source_native_id,
                sha256(c.source.raw_text.encode()).hexdigest(),
            )
            == (
                fragment["parser_run_id"],
                fragment["source_native_id"],
                fragment["raw_text_sha256"],
            )
        ]
        if len(matches) != 1:
            raise ValueError("source candidate mismatch")
        candidate = matches[0]
        start, end = fragment["char_start"], fragment["char_end"]
        if (
            not any(candidate in batch.blocks for batch in graph.candidates)
            or type(start) is not int
            or type(end) is not int
            or not 0 <= start < end <= len(candidate.source.raw_text)
            or candidate.bbox is None
            or candidate.has_invalid_geometry
        ):
            raise ValueError("source fragment geometry or offsets invalid")
        page_number, box = candidate.source.physical_page, candidate.bbox
        quote = candidate.source.raw_text[start:end]
    elif set(fragment) == {
        "note_artifact_sha256",
        "packet_sha256",
        "physical_page",
        "fragment_ids",
        "native_word_indices",
    }:
        # Reuses original-word replay, including historical artifact compatibility.
        replay_note_reviews((note_artifact,), graph, source, tenant_id=tenant_id)
        artifact = json.loads(note_artifact)
        packet = artifact["packet"]
        data = packet["untrusted_document_data"]
        ids, indices = fragment["fragment_ids"], fragment["native_word_indices"]
        if (
            artifact["artifact_sha256"] != fragment["note_artifact_sha256"]
            or canonical_hash(packet) != fragment["packet_sha256"]
            or type(fragment["physical_page"]) is not int
            or fragment["physical_page"] != data["page"]
            or not isinstance(ids, list)
            or not ids
            or any(not isinstance(i, str) for i in ids)
            or len(set(ids)) != len(ids)
            or not isinstance(indices, list)
            or any(type(i) is not int for i in indices)
        ):
            raise ValueError("native fragment identity mismatch")
        parts = [f for f in data["fragments"] if f["id"] in ids]
        if (
            [f["id"] for f in parts] != ids
            or sorted({i for f in parts for i in f["word_indices"]}) != indices
            or set(indices) & set(data["unreadable_word_indices"])
        ):
            raise ValueError("native fragment word union mismatch")
        page_number = data["page"]
        box = (
            min(f["bbox"][0] for f in parts),
            min(f["bbox"][1] for f in parts),
            max(f["bbox"][2] for f in parts),
            max(f["bbox"][3] for f in parts),
        )
        quote = "\n".join(f["text"] for f in parts)
        from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry

        try:
            glyph_geometry = native_word_ink_geometry(source, page_number, indices)
        except ValueError:
            # Optional diagnostic cannot suppress an otherwise valid original display.
            glyph_geometry = None
    else:
        raise ValueError("invalid source fragment fields")

    with pdfplumber.open(io.BytesIO(source)) as document:
        if type(page_number) is not int or not 1 <= page_number <= len(document.pages):
            raise ValueError("source view page invalid")
        page = document.pages[page_number - 1]
        if "OCProperties" in document.doc.catalog:
            raise ValueError("optional content visibility unsupported")
        if (
            page.rotation
            or tuple(page.bbox[:2]) != (0, 0)
            or tuple(page.cropbox) != tuple(page.mediabox)
        ):
            raise ValueError("source view geometry unsupported")
        geometry = PageGeometry(page.width, page.height, 0, tuple(page.mediabox))
        if candidate is not None:
            if candidate.geometry != geometry:
                raise ValueError("source view geometry mismatch")
            words = page.extract_words()
            validate_word_geometry(words, page.width, page.height)
            selected = [
                w
                for w in words
                if not (
                    w["x1"] <= box[0]
                    or w["x0"] >= box[2]
                    or w["bottom"] <= box[1]
                    or w["top"] >= box[3]
                )
            ]
            if any(
                not w["upright"]
                or not (
                    box[0] <= w["x0"] < w["x1"] <= box[2]
                    and box[1] <= w["top"] < w["bottom"] <= box[3]
                )
                for w in selected
            ) or _normalized(" ".join(w["text"] for w in selected)) != _normalized(
                candidate.source.raw_text
            ):
                glyph_geometry, glyph_selection_word_indices, glyph_text = _glyph_selection(
                    source, page_number, box, words
                )
                if _normalized(glyph_text) != _normalized(candidate.source.raw_text):
                    raise ValueError("source view text or clipping mismatch")
    png, width, height = source_preview.render_page_preview(
        source, page_number, geometry, include_annotations_and_forms=True
    )
    receipt = dict(
        schema="source_fragment_view_v1",
        scope="source_display_only",
        appearance="default_view_with_annotations_and_acroforms",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        fragment=fragment,
        fragment_sha256=canonical_hash(fragment),
        physical_page=page_number,
        coordinate_system="pdf_top_left_points",
        highlight_bbox=list(box),
        glyph_geometry=glyph_geometry,
        glyph_selection_word_indices=glyph_selection_word_indices,
        page_width_pt=width,
        page_height_pt=height,
        quote=quote,
        image_sha256=sha256(png).hexdigest(),
        renderer_version=version("pypdfium2"),
        reader_version=version("pdfplumber"),
        text_reader_version=version("pdfminer.six"),
        renderer_sha256=sha256(Path(source_preview.__file__).read_bytes()).hexdigest(),
        verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
    )
    receipt["receipt_sha256"] = canonical_hash(receipt)
    return receipt, png


def replay_fragment_view(receipt, graph, source, fragment, *, tenant_id, note_artifact=None):
    expected, _png = render_fragment(
        graph, source, fragment, tenant_id=tenant_id, note_artifact=note_artifact
    )
    if canonical_hash(receipt) != canonical_hash(expected):
        raise ValueError("source view receipt mismatch")
    return expected
