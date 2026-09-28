"""Bounded original-PDF marker proof; no citation, coverage or condition approval."""

import io
import json
import math
import re
from dataclasses import asdict
from hashlib import sha256

import pdfplumber

from proofops.adapters.local.source_condition_view import _intersects, render_fragment
from proofops.domain.provenance import canonical_hash

_PREFIX = re.compile(r"^([1-9][0-9]?\))")


def _root_table(block, blocks, parents):
    seen = set()
    while block.kind != "table":
        if block.source_id in seen:
            return None
        seen.add(block.source_id)
        links = parents.get(block.source_id, set())
        if len(links) != 1:
            return None
        parent = blocks.get(next(iter(links)))
        if parent is None or parent.page_num != block.page_num:
            return None
        if parent.kind not in {"table", "table_row"}:
            return None
        block = parent
    return block


def _pairs(words, indices, marker, glyphs):
    result = []
    for index in indices:
        word = words[index]
        if word["text"] != marker or not word["chars"]:
            continue
        for base_index in indices:
            base = words[base_index]
            if (
                base_index == index
                or not base["chars"]
                or not any(c.isalpha() for c in base["text"])
                or any(c.isdigit() for c in base["text"])
            ):
                continue
            last = base["chars"][-1]
            chars = [last, *word["chars"]]
            if any(
                not c["upright"]
                or not math.isfinite(c["size"])
                or c["size"] <= 0
                or len(c["matrix"]) != 6
                or not all(math.isfinite(v) for v in c["matrix"])
                or c["matrix"][1:3] != (0, 0)
                or c["matrix"][0] <= 0
                or c["matrix"][3] <= 0
                for c in chars
            ):
                continue
            # Glyph boxes avoid rejecting original kerning/oversized font metrics.
            left = glyphs[index][0] if index in glyphs else word["x0"]
            right = glyphs[base_index][2] if base_index in glyphs else base["x1"]
            if not -0.1 <= left - right <= last["size"]:
                continue
            if all(
                c["size"] < last["size"] and 0 < c["matrix"][5] - last["matrix"][5] < last["size"]
                for c in word["chars"]
            ):
                result.append((base_index, index))
    return result


def prove_native_note_marker(
    graph,
    source: bytes,
    fragment: dict,
    targets: list[dict],
    note_artifacts: tuple[str, ...],
    *,
    tenant_id,
) -> dict | None:
    """Return a source-bound singleton leaf proof, or None when not uniquely proven.

    The caller supplies the original graph and committed artifacts. The pure validator
    separately checks original table lineage, target coordinates and verified refs.
    No marker_targets proposals or client-provided source-view receipts are trusted.
    """
    try:
        return _prove(graph, source, fragment, targets, note_artifacts, tenant_id)
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def _prove(graph, source, fragment, targets, note_artifacts, tenant_id):
    if (
        not isinstance(fragment, dict)
        or "note_artifact_sha256" not in fragment
        or not isinstance(targets, list)
        or len(targets) != 1
        or not isinstance(targets[0], dict)
        or not isinstance(note_artifacts, tuple)
        or len(note_artifacts) > 256
    ):
        return None
    artifacts = [
        a
        for a in note_artifacts
        if json.loads(a)["artifact_sha256"] == fragment["note_artifact_sha256"]
    ]
    if len(artifacts) != 1:
        return None
    note, _ = render_fragment(
        graph, source, fragment, note_artifact=artifacts[0], tenant_id=tenant_id
    )
    data = json.loads(artifacts[0])["packet"]["untrusted_document_data"]
    parts = [f for f in data["fragments"] if f["id"] in fragment["fragment_ids"]]
    prefix = _PREFIX.match(parts[0]["text"].strip())
    if prefix is None or not any(c.isalpha() for c in parts[0]["text"][prefix.end() :]):
        return None
    marker = prefix[1]
    if set(re.findall(r"(?<![0-9])([1-9][0-9]?\))", note["quote"])) != {marker}:
        return None
    first = parts[0]["bbox"]
    for previous, part in zip(parts, parts[1:]):
        box, prior = part["bbox"], previous["bbox"]
        height = min(first[3] - first[1], box[3] - box[1])
        if (
            _PREFIX.match(part["text"].strip())
            or not first[0] <= box[0] <= first[0] + 2 * height
            or not prior[3] <= box[1] <= prior[3] + 2 * height
        ):
            return None
    blocks = {b.source_id: b for b in graph.blocks}
    parents: dict[str, set[str]] = {}
    for edge in graph.edges:
        if edge.relation == "table_parent":
            parents.setdefault(edge.source_id, set()).add(edge.target_id)
    containers = {p for values in parents.values() for p in values}
    cells = [
        b
        for b in graph.blocks
        if b.kind == "table_cell"
        and b.page_num == note["physical_page"]
        and b.source_id not in containers
    ]
    if len(cells) > 1000:
        return None
    owners = []
    with pdfplumber.open(io.BytesIO(source)) as document:
        words = document.pages[note["physical_page"] - 1].extract_words(return_chars=True)
        if len(words) > 1000:
            return None
        marker_boxes = [
            [w[k] for k in ("x0", "top", "x1", "bottom")] for w in words if w["text"] == marker
        ]
        # ponytail: scan the bounded page cells, never nearest-only ownership.
        for cell in cells:
            if not any(
                marker in c.source.raw_text
                or (c.bbox is not None and any(_intersects(c.bbox, b) for b in marker_boxes))
                for c in cell.candidates
            ):
                continue
            table = _root_table(cell, blocks, parents)
            if table is None or table.bbox is None:
                return None
            box, note_box = table.bbox, note["highlight_bbox"]
            if not (box[0] <= note_box[0] < note_box[2] <= box[2] and box[3] <= note_box[1]):
                continue
            for other in data["fragments"]:
                other_box, text = other["bbox"], other["text"].strip()
                other_prefix = _PREFIX.match(text)
                if (
                    other["id"] not in fragment["fragment_ids"]
                    and other_prefix is not None
                    and other_prefix[1] == marker
                    and any(c.isalpha() for c in text[other_prefix.end() :])
                    and box[0] <= other_box[0] < other_box[2] <= box[2]
                    and box[3] <= other_box[1]
                ):
                    return None
            if type(cell.winner) is not int or cell.quality in {
                "conflicted",
                "unreadable",
                "unlocated",
            }:
                return None
            native = cell.candidates[cell.winner].source
            canonical = dict(
                source_id=cell.source_id,
                parser_run_id=native.parser_run_id,
                source_native_id=native.source_native_id,
                char_start=0,
                char_end=len(native.raw_text),
                raw_text_sha256=sha256(native.raw_text.encode()).hexdigest(),
            )
            receipt, _ = render_fragment(graph, source, canonical, tenant_id=tenant_id)
            indices = receipt["glyph_selection_word_indices"]
            glyphs = {}
            if indices is None:
                cb = receipt["highlight_bbox"]
                indices = [
                    i
                    for i, w in enumerate(words)
                    if cb[0] <= w["x0"] < w["x1"] <= cb[2]
                    and cb[1] <= w["top"] < w["bottom"] <= cb[3]
                ]
            else:
                glyphs = {
                    w["native_word_index"]: w["ink_bbox"]
                    for w in receipt["glyph_geometry"]["matched_words"]
                }
            for base_index, marker_index in _pairs(words, indices, marker, glyphs):
                owners.append((cell.source_id, base_index, marker_index))
    if len(owners) != 1 or owners[0][0] != targets[0].get("source_id"):
        return None
    sid, base_index, marker_index = owners[0]
    proof = dict(
        schema="native_note_marker_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        fragment_id=canonical_hash(fragment),
        target_source_ids=sorted([sid]),
        marker=marker,
        base_word_index=base_index,
        marker_word_index=marker_index,
        note_word_indices=list(fragment["native_word_indices"]),
    )
    proof["proof_sha256"] = canonical_hash(proof)
    return proof
