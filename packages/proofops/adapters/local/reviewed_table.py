"""Bridge an operator-reviewed PDF grid into an unverified canonical table.

The review artifact is source-bound but is not a parser or a verification
receipt.  This adapter deliberately builds one isolated candidate graph and
delegates numeric normalization to ``normalize_table_bindings``; it never
promotes a reviewed cell, infers a unit, or admits a numeric observation.

The one geometric correction it does make: a reviewed rectangle is drawn on the
extractor's word boxes, which are font metric boxes rather than ink.  Where those
disagree with the glyphs (measured: every pinned word of ``lotte.pdf`` page 117
inks a median 5.957pt above its reported box), the reviewed rectangle does not
contain its own text, so the native selected-cell verifier correctly refuses the
cell's promoted crop.  Each canonical cell box is therefore the ink union of
exactly the words that review already pinned to that cell -- same membership,
same raw text, same row/column/span, same source hashes.  Nothing is widened to
unpinned text and an unresolvable cell keeps the reviewed rectangle and records
why, which can only hold a cell back.
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException

from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry
from proofops.application.evidence.citations import verify_source_ref
from proofops.application.ingest.graph_fusion import (
    CandidateBatch,
    CandidateBlock,
    CandidateEdge,
    CanonicalDocumentGraph,
    fuse_candidates,
)
from proofops.application.ingest.normalize import NormalizationResult, normalize_table_bindings
from proofops.domain.documents import NativeSource, PageGeometry
from proofops.domain.numeric import observation_source_holds
from proofops.domain.provenance import canonical_hash

_SCHEMA = "reviewed_table_bridge_v1"
# Identity of the numeric-input view below. It adds no verification of its own:
# every promotion decision is read back out of the existing native receipt and
# the existing citation verifier.
_NUMERIC_INPUT_SCHEMA = "reviewed_table_numeric_input_v1"
# Canonical cell boxes now come from real glyph ink, so this bridge no longer
# produces the same parse as the R02h v1 artifacts. The parse manifest, parser run
# and config hash carry that rule identity; the raw PDF document version does not
# change, because the source did not.
_GEOMETRY_SCHEMA = "reviewed_table_native_ink_geometry_v2"
# Identity rule of a snapshot that carries more than one reviewed grid of the same
# original bytes. It is deliberately not either grid's own manifest key.
_COMBINED_SCHEMA = "reviewed_table_combined_snapshot_v1"
_TENANT = str(uuid5(NAMESPACE_URL, "proofops:reviewed-table:evaluation-tenant"))
# A reviewed word box and the reader's own word box must still be the same word.
_LINEAGE_TOL = 0.01


def _id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"proofops:reviewed-table:{name}"))


def _native_bbox(box: list[float], height: float) -> tuple[float, float, float, float]:
    x0, top, x1, bottom = box
    return (float(x0), height - float(bottom), float(x1), height - float(top))


def _require_review(review: dict, source: bytes) -> None:
    if not isinstance(review, dict) or review.get("status") != "candidate_only":
        raise ValueError("candidate-only reviewed layout required")
    if sha256(source).hexdigest() != review.get("source_sha256"):
        raise ValueError("reviewed layout source mismatch")
    if review.get("coordinate_system") != "pdf_top_left_points":
        raise ValueError("unsupported reviewed layout coordinate system")
    if (
        review.get("native_verification") != "not_run"
        or review.get("semantic_verification") != "not_run"
    ):
        raise ValueError("review artifact must not claim verification")
    if not isinstance(review.get("physical_page"), int) or review["physical_page"] < 1:
        raise ValueError("reviewed physical page required")
    if not isinstance(review.get("page_bbox"), list) or len(review["page_bbox"]) != 4:
        raise ValueError("reviewed page bbox required")
    if not isinstance(review.get("cells"), dict) or not review["cells"]:
        raise ValueError("reviewed cells required")
    if not isinstance(review.get("candidates"), list) or not review["candidates"]:
        raise ValueError("reviewed numeric candidates required")


# Caller-reviewed passages outside the table grid (footnotes, coverage notes, ...).
# ``notes``/``row_labels`` on a candidate bind an *in-table* cell key; this is the
# separate, table-level set of reviewed passages that are not any cell at all, so
# they can never be bound by that per-candidate mechanism. Nothing here promotes,
# reads or reinterprets the passage: it only carries the caller's own review
# forward instead of dropping it, and reports it as unresolved.
_EXTERNAL_CONTEXT_FIELDS = frozenset(
    {"kind", "raw_text", "bbox", "word_indices", "clipped_word_indices", "rotated_word_indices"}
)


def _external_review_context(review: dict) -> tuple[dict, ...]:
    """Validate and carry forward ``review["context"]`` byte-for-byte; never drop it.

    Each entry is a passage the caller already reviewed and pinned to real words,
    outside the table's own cells. A missing, wrong-typed or malformed entry is a
    caller-review defect and must stop the bridge rather than be silently filtered
    into an empty list, the same way a malformed candidate role already does.
    ``extraction_status`` is read back, not assumed: a clipped/rotated/unreadable
    passage stays exactly that, and only ``source_text_extracted`` can ever be
    treated as source-bound text below.
    """
    raw = review.get("context", [])
    if not isinstance(raw, list):
        raise ValueError("reviewed external context must be a list")
    words = {
        word["index"]
        for word in review.get("words") or ()
        if isinstance(word, dict) and isinstance(word.get("index"), int)
    }
    entries = []
    for entry in raw:
        if not isinstance(entry, dict) or not _EXTERNAL_CONTEXT_FIELDS <= set(entry):
            raise ValueError("reviewed external context entry malformed")
        if not isinstance(entry.get("kind"), str) or not entry["kind"]:
            raise ValueError("reviewed external context kind required")
        if not isinstance(entry.get("raw_text"), str) or not entry["raw_text"]:
            raise ValueError("reviewed external context raw_text required")
        if not isinstance(entry.get("bbox"), list) or len(entry["bbox"]) != 4:
            raise ValueError("reviewed external context bbox required")
        indices = entry.get("word_indices")
        if (
            not isinstance(indices, list)
            or not indices
            or any(type(index) is not int or index < 0 for index in indices)
        ):
            raise ValueError("reviewed external context word_indices required")
        for optional in ("clipped_word_indices", "rotated_word_indices"):
            if not isinstance(entry.get(optional), list) or any(
                type(index) is not int for index in entry[optional]
            ):
                raise ValueError(f"reviewed external context {optional} invalid")
        status = entry.get("extraction_status")
        if not isinstance(status, str) or not status:
            raise ValueError("reviewed external context extraction_status required")
        readable = (
            status == "source_text_extracted"
            and not entry["clipped_word_indices"]
            and not entry["rotated_word_indices"]
            and set(indices) <= words
        )
        entries.append(
            {
                "kind": entry["kind"],
                "raw_text": entry["raw_text"],
                "bbox": [float(v) for v in entry["bbox"]],
                "word_indices": list(indices),
                "clipped_word_indices": list(entry["clipped_word_indices"]),
                "rotated_word_indices": list(entry["rotated_word_indices"]),
                "extraction_status": status,
                # This module only ever reads the caller's own review-declared
                # lineage back; it never re-derives or verifies it against the
                # source itself. "readable" therefore means "the review declares
                # this passage as clean source-bound text", not "natively proven".
                "review_lineage_complete": readable,
                "source_verification": "not_run",
                "association_status": "unknown",
            }
        )
    return tuple(entries)


def _union(boxes) -> list[float]:
    return [
        min(box[0] for box in boxes),
        min(box[1] for box in boxes),
        max(box[2] for box in boxes),
        max(box[3] for box in boxes),
    ]


def _overlaps(inner, outer) -> bool:
    horizontal = min(inner[2], outer[2]) > max(inner[0], outer[0])
    vertical = min(inner[3], outer[3]) > max(inner[1], outer[1])
    return horizontal and vertical


@lru_cache(maxsize=4)
def _page_word_ink(source: bytes, page_number: int, pinned, lineage):
    """Reader words and their real glyph ink for one page, or a recorded refusal.

    A reviewed rectangle is drawn on the extractor's *word* boxes.  Those boxes
    are font metric boxes, not ink: on ``lotte.pdf`` page 117 every one of the
    111 pinned words inks about 6pt above its reported box (measured median
    5.957pt, range 3.290-9.751pt), so a reviewed row rectangle contains its own
    glyphs nowhere and the native verifier's promoted-crop check refuses it.
    Resolving the ink of the *same pinned words* corrects only the rectangle.
    Membership, text, grid indices and spans are never touched here, and a word
    the review did not pin is never pulled in.
    """
    try:
        with pdfplumber.open(io.BytesIO(source)) as document:
            if not 1 <= page_number <= len(document.pages):
                return (), (("status", "operator_reviewed_region"), ("reason", "page_absent"))
            words = document.pages[page_number - 1].extract_words()
    except (PdfminerException, OSError, ValueError) as error:
        return (
            (),
            (
                ("status", "operator_reviewed_region"),
                ("reason", f"source_not_readable:{type(error).__name__}"),
            ),
        )
    # The correction is index-addressed, so every pinned index must carry its own
    # recorded identity exactly once. Missing or duplicated lineage would leave
    # the box of an unchecked word to be moved.
    covered = [index for index, _, _ in lineage]
    if sorted(covered) != sorted(pinned) or len(set(covered)) != len(covered):
        raise ValueError("reviewed word lineage incomplete")
    for index, text, box in lineage:
        if index >= len(words):
            raise ValueError("reviewed word lineage out of range")
        word = words[index]
        if word["text"] != text or any(
            abs(float(word[key]) - value) > _LINEAGE_TOL
            for key, value in zip(("x0", "top", "x1", "bottom"), box, strict=True)
        ):
            # A reader change that renumbers or moves words must stop the bridge,
            # not shift a box.
            raise ValueError("reviewed word lineage drifted")
    result = native_word_ink_geometry(source, page_number, list(pinned))
    unresolved = set(result.get("unresolved_word_indices") or ())
    ink = tuple(
        (int(match["native_word_index"]), tuple(float(v) for v in match["ink_bbox"]))
        for match in result.get("matched_words") or ()
        if int(match["native_word_index"]) not in unresolved and match.get("ink_bbox")
    )
    return ink, (
        ("status", "native_word_ink_union"),
        ("reader", result.get("reader")),
        ("reader_version", result.get("reader_version")),
        ("matcher_sha256", result.get("matcher_sha256")),
        ("proof_sha256", result.get("proof_sha256")),
        ("pinned_words", len(pinned)),
        ("resolved_word_ink", len(ink)),
    )


def _canonical_cell_boxes(review: dict, source: bytes):
    """Per-cell canonical box: the ink union of that cell's own pinned words.

    A cell whose pinned ink cannot be resolved, or whose ink union does not even
    overlap the reviewed rectangle, keeps the reviewed rectangle and records why.
    That fallback can only hold a cell back; it never admits one.
    """
    cells = review["cells"]
    pinned = set()
    for cell in cells.values():
        for index in cell.get("word_indices") or ():
            if type(index) is not int or index < 0:
                raise ValueError("reviewed cell word index invalid")
            pinned.add(index)
    lineage = tuple(
        (int(word["index"]), word["text"], tuple(float(v) for v in word["bbox"]))
        for word in review.get("words") or ()
        if isinstance(word, dict) and word.get("index") in pinned
    )
    pairs, diagnostics = _page_word_ink(
        source, review["physical_page"], tuple(sorted(pinned)), lineage
    )
    ink = dict(pairs)
    boxes: dict[str, list[float]] = {}
    detail: dict[str, dict] = {}
    for key, cell in cells.items():
        region = [float(v) for v in cell["bbox"]]
        cell_words = [int(i) for i in (cell.get("word_indices") or ())]
        resolved = [ink[i] for i in cell_words if i in ink]
        record = {"reviewed_region_bbox": region, "pinned_word_indices": cell_words}
        if not cell_words or len(resolved) != len(cell_words):
            boxes[key] = region
            detail[key] = {
                **record,
                "canonical_bbox_source": "operator_reviewed_region",
                "reason": "pinned_word_ink_unresolved",
            }
            continue
        box = _union(resolved)
        if box[0] >= box[2] or box[1] >= box[3] or not _overlaps(box, region):
            boxes[key] = region
            detail[key] = {
                **record,
                "canonical_bbox_source": "operator_reviewed_region",
                "reason": "pinned_word_ink_outside_reviewed_region",
            }
            continue
        boxes[key] = box
        detail[key] = {
            **record,
            "canonical_bbox_source": "native_word_ink_union",
            "reviewed_region_offset_pt": [round(box[i] - region[i], 4) for i in range(4)],
        }
    return boxes, detail, dict(diagnostics)


def reviewed_cell_geometry(review: dict, source: bytes) -> dict:
    """Which canonical cell box came from real ink, and how far it moved."""
    _, detail, diagnostics = _canonical_cell_boxes(review, source)
    return {"cells": detail, "pinned_word_ink": diagnostics}


def graph_from_review(review: dict, source: bytes, *, tenant_id: str = _TENANT, also=()):
    """Make a one-review candidate graph with unverified cell quality only.

    ``also`` adds further reviewed grids of the *same original bytes* (another page,
    or another table of the same page) to one explicitly new combined snapshot. It
    exists so a value disclosed in two physically separate places can be read out of
    a single pinned snapshot instead of having one side's identity rewritten onto the
    other. Nothing is transplanted: every grid keeps its own physical page, its own
    reviewed rectangles, its own layout hash in the block context and its own cell
    keys, and the combined snapshot is its *own* parse manifest derived from the
    ordered layout hashes. With ``also`` empty the identity, cell native ids and
    returned mapping are byte-for-byte what they were, so existing artifacts still
    reproduce. Verified quality is never carried over: the combined snapshot starts
    entirely ``unverified`` and must be re-attested natively by the callers below.
    """
    reviews = (review, *tuple(also))
    for item in reviews:
        _require_review(item, source)
    source_sha256 = review["source_sha256"]
    layouts = []
    for item in reviews:
        layout_sha256 = item.get("layout_sha256")
        if not isinstance(layout_sha256, str) or len(layout_sha256) != 64:
            raise ValueError("reviewed layout hash required")
        if item["source_sha256"] != source_sha256:
            raise ValueError("combined reviewed layouts must share one source")
        layouts.append(layout_sha256)
    if len(set(layouts)) != len(layouts):
        raise ValueError("duplicate reviewed layout in combined snapshot")
    # A combined snapshot is a different parse rule from either grid alone, so it
    # gets its own manifest key. A single review keeps the existing key exactly.
    manifest_key = (
        layouts[0]
        if len(reviews) == 1
        else canonical_hash({"schema": _COMBINED_SCHEMA, "layout_sha256": layouts})
    )
    document_version_id = _id(f"document:{source_sha256}")
    parse_manifest_id = _id(f"manifest:{source_sha256}:{manifest_key}:{_GEOMETRY_SCHEMA}")
    parser_run_id = _id(f"review:{source_sha256}:{manifest_key}:{_GEOMETRY_SCHEMA}")
    config = {
        "schema": _SCHEMA,
        "geometry_schema": _GEOMETRY_SCHEMA,
        "layout_sha256": layouts[0],
        "tool_sha256": review.get("tool_sha256"),
        "reader_version": review.get("reader_version"),
    }
    if len(reviews) > 1:
        config["combined"] = [
            {
                "layout_sha256": layout,
                "physical_page": item["physical_page"],
                "tool_sha256": item.get("tool_sha256"),
                "reader_version": item.get("reader_version"),
            }
            for layout, item in zip(layouts, reviews, strict=True)
        ]
    config_hash = canonical_hash(config)

    def make_candidate(page, geometry, layout_sha256):
        height = geometry.height_pt

        def candidate(native_id: str, kind: str, raw_text: str, box: list[float], **grid):
            native = _native_bbox(box, height)
            return CandidateBlock(
                kind,
                NativeSource(
                    document_version_id,
                    parse_manifest_id,
                    parser_run_id,
                    native_id,
                    page,
                    None,
                    native,
                    "pdf_bottom_left_points",
                    raw_text,
                    0,
                    len(raw_text),
                ),
                geometry,
                context=("operator_reviewed_layout", f"layout_sha256={layout_sha256}"),
                parser_bbox=native,
                parser_coordinate_system="pdf_bottom_left_points",
                parser_to_canonical=geometry.to_canonical_affine(),
                **grid,
            )

        return candidate

    blocks: list = []
    edges: list = []
    native_ids: dict[str, str] = {}
    for index, item in enumerate(reviews):
        prefix = "" if index == 0 else f"{index}:"
        page = item["physical_page"]
        x0, y0, x1, y1 = item["page_bbox"]
        if (x0, y0) != (0, 0) or not (isinstance(x1, int | float) and isinstance(y1, int | float)):
            raise ValueError("offset reviewed page geometry unsupported")
        width, height = float(x1), float(y1)
        geometry = PageGeometry(width, height, 0, (0, 0, width, height))
        candidate = make_candidate(page, geometry, layouts[index])
        cells = item["cells"]
        cell_items = sorted(
            cells.items(),
            key=lambda entry: (entry[1].get("row", -1), entry[1].get("column", -1), entry[0]),
        )
        if any(
            not isinstance(cell, dict) or not isinstance(cell.get("raw_text"), str)
            for _, cell in cell_items
        ):
            raise ValueError("reviewed raw cell text required")
        if any(
            not isinstance(cell.get("bbox"), list) or len(cell["bbox"]) != 4
            for _, cell in cell_items
        ):
            raise ValueError("reviewed cell bbox required")
        canonical_boxes, _, _ = _canonical_cell_boxes(item, source)
        table_box = _union([canonical_boxes[key] for key, _ in cell_items])
        table_text = "\n".join(cell["raw_text"] for _, cell in cell_items)
        table_native_id = f"{prefix}reviewed-table"
        blocks.append(candidate(table_native_id, "table", table_text, table_box))
        if len(reviews) > 1:
            native_ids[table_native_id] = table_native_id
        for key, cell in cell_items:
            if cell.get("extraction_status") != "source_text_extracted":
                raise ValueError(f"reviewed cell source boundary held: {key}")
            row, column = cell.get("row"), cell.get("column")
            row_span, column_span = cell.get("row_span", 1), cell.get("column_span", 1)
            if any(type(value) is not int or value < 0 for value in (row, column)) or any(
                type(value) is not int or value < 1 for value in (row_span, column_span)
            ):
                raise ValueError(f"reviewed cell grid invalid: {key}")
            native_id = f"reviewed-cell:{prefix}{key}"
            blocks.append(
                candidate(
                    native_id,
                    "table_cell",
                    cell["raw_text"],
                    canonical_boxes[key],
                    table_native_id=table_native_id,
                    row_number=row,
                    column_number=column,
                    row_span=row_span,
                    column_span=column_span,
                )
            )
            edges.append(CandidateEdge(native_id, table_native_id, "table_parent"))
            native_ids[f"{prefix}{key}"] = native_id
    batch = CandidateBatch(
        tenant_id=tenant_id,
        document_version_id=document_version_id,
        parse_manifest_id=parse_manifest_id,
        source_sha256=source_sha256,
        parser_run_id=parser_run_id,
        parser_name="operator_reviewed_table_layout",
        parser_version=_SCHEMA,
        parser_family="operator_reviewed_layout",
        config_hash=config_hash,
        blocks=tuple(blocks),
        edges=tuple(edges),
        synthetic=False,
    )
    return fuse_candidates((batch,), tenant_id=tenant_id), native_ids


def normalize_reviewed_layout(review: dict, source: bytes, *, tenant_id: str = _TENANT):
    """Run existing explicit-role normalization and retain the review's raw lineage."""
    graph, native_ids = graph_from_review(review, source, tenant_id=tenant_id)
    canonical_ids = {
        candidate.source.source_native_id: block.source_id
        for block in graph.blocks
        for candidate in block.candidates
    }
    table_id = canonical_ids["reviewed-table"]
    observations, issues, role_bindings = [], list(graph.issues), []
    for index, item in enumerate(review["candidates"]):
        refs = item.get("source_cells")
        if not isinstance(refs, dict):
            raise ValueError("reviewed candidate cell roles required")
        required = {"metric", "unit", "year", "value"}
        if not required <= set(refs) or any(not isinstance(refs[key], str) for key in required):
            raise ValueError("reviewed candidate explicit metric/unit/year/value roles required")
        if any(refs[key] not in native_ids for key in required):
            raise ValueError("reviewed candidate references an unknown cell")
        binding = {
            "metric_raw": canonical_ids[native_ids[refs["metric"]]],
            "unit_raw": canonical_ids[native_ids[refs["unit"]]],
            "reporting_period": canonical_ids[native_ids[refs["year"]]],
            "value_raw": canonical_ids[native_ids[refs["value"]]],
        }
        record = {"candidate_index": index, "bindings": binding, "normalization_status": "held"}
        try:
            normalized = normalize_table_bindings(
                graph, table_id=table_id, bindings=(binding,), tenant_id=tenant_id
            )
        except ValueError as error:
            # An explicit parent row is not a geometry span: do not widen it to
            # make a child value normalize.  Keep the usable role mapping held.
            record["hold_reason"] = str(error)
        else:
            record["normalization_status"] = "normalized_unverified"
            observations.extend(normalized.observations)
            issues.extend(normalized.conflicts)
        role_bindings.append(record)
    return (
        graph,
        NormalizationResult(tuple(observations), tuple(issues), graph),
        table_id,
        role_bindings,
    )


def artifact_from_review(
    review: dict, source: bytes, *, tenant_id: str = _TENANT, native_receipt=None
) -> dict:
    """Return a JSON-safe immutable evaluation artifact; never an admission record."""
    graph, result, table_id, role_bindings = normalize_reviewed_layout(
        review, source, tenant_id=tenant_id
    )
    cell_geometry = reviewed_cell_geometry(review, source)
    cells = {
        candidate.source.source_native_id.removeprefix("reviewed-cell:"): block
        for block in graph.blocks
        for candidate in block.candidates
        if candidate.source.source_native_id.startswith("reviewed-cell:")
    }
    canonical_cells = []
    for key, review_cell in sorted(review["cells"].items()):
        block = cells[key]
        geometry = cell_geometry["cells"][key]
        canonical_cells.append(
            {
                "cell_key": key,
                "canonical_cell_id": block.source_id,
                "raw_text": review_cell["raw_text"],
                "raw_text_sha256": sha256(review_cell["raw_text"].encode()).hexdigest(),
                "physical_page": review["physical_page"],
                "bbox": review_cell["bbox"],
                "canonical_bbox": list(block.bbox) if block.bbox else None,
                "canonical_bbox_source": geometry["canonical_bbox_source"],
                "reviewed_region_offset_pt": geometry.get("reviewed_region_offset_pt"),
                "canonical_bbox_hold_reason": geometry.get("reason"),
                "row": review_cell["row"],
                "column": review_cell["column"],
                "row_span": review_cell["row_span"],
                "column_span": review_cell["column_span"],
                "word_indices": review_cell["word_indices"],
                "clipped_word_indices": review_cell["clipped_word_indices"],
                "rotated_word_indices": review_cell["rotated_word_indices"],
                "extraction_status": review_cell["extraction_status"],
                "quality": block.quality,
            }
        )
    observations = []
    for item in result.observations:
        value = asdict(item)
        value["source_refs"] = [
            {**asdict(ref), "bbox": list(ref.bbox) if ref.bbox else None}
            for ref in item.source_refs
        ]
        value["source_blocks"] = [block.source_id for block in item.source_blocks]
        value["parent_relations"] = [list(relation) for relation in item.parent_relations]
        value["unit_interpretation"] = "raw_literal_only"
        observations.append(value)
    artifact = {
        "schema": _SCHEMA,
        "geometry_schema": _GEOMETRY_SCHEMA,
        "status": "unverified_candidate",
        "eligible_for_admission": False,
        "tenant_id": tenant_id,
        "source": {
            "source_sha256": review["source_sha256"],
            "physical_page": review["physical_page"],
            "coordinate_system": review["coordinate_system"],
            "page_bbox": review["page_bbox"],
            "layout_sha256": review["layout_sha256"],
            "review_status": review["status"],
            "reviewer": review["layout"].get("reviewer"),
            "layout_interpretation": review["layout_interpretation"],
        },
        "native_verification": native_receipt if native_receipt is not None else "not_run",
        "semantic_verification": "not_run",
        "canonical_cell_geometry": {
            "rule": (
                "canonical cell bbox = ink union of exactly the native words the review "
                "pinned to that cell; reviewed membership, text, grid indices and spans "
                "are unchanged and no unpinned word is included"
            ),
            "pinned_word_ink": cell_geometry["pinned_word_ink"],
            "cells_from_native_word_ink": sum(
                1
                for detail in cell_geometry["cells"].values()
                if detail["canonical_bbox_source"] == "native_word_ink_union"
            ),
            "cells_from_operator_reviewed_region": sorted(
                key
                for key, detail in cell_geometry["cells"].items()
                if detail["canonical_bbox_source"] == "operator_reviewed_region"
            ),
        },
        "canonical_table": {"table_id": table_id, "cells": canonical_cells},
        "explicit_role_bindings": role_bindings,
        "numeric_observations": observations,
        "normalization_issues": [issue.to_dict() for issue in result.conflicts],
        "holds": [
            "operator_reviewed_layout_is_not_native_source_verification",
            "semantic_metric_period_unit_binding_not_admitted",
            "raw_split_subscript_units_preserved_without_unit_conversion",
        ],
        "graph_sha256": canonical_hash(asdict(graph)),
    }
    artifact["artifact_sha256"] = canonical_hash(artifact)
    return artifact


def load_artifact(
    review_path: Path,
    pdf_path: Path,
    *,
    tenant_id: str = _TENANT,
    native_attest: bool = False,
) -> dict:
    review = json.loads(review_path.read_text())
    source = pdf_path.read_bytes()
    native_receipt = None
    if native_attest:
        graph, _, _, _ = normalize_reviewed_layout(review, source, tenant_id=tenant_id)
        from proofops.adapters.local.selected_cell_table_verification import attest_tables

        native_receipt = attest_tables(graph, source, tenant_id=tenant_id)
    return artifact_from_review(review, source, tenant_id=tenant_id, native_receipt=native_receipt)


# ---------------------------------------------------------------------------
# Numeric-service input view
#
# Everything below turns the reviewed grid into the input the existing pure
# numeric check already consumes (``application.numeric_analysis``). It adds no
# verifier: the promotion of a cell comes from the unmodified native selected-cell
# receipt through its own ``replay_tables``, and the promotion of a citation comes
# from the unmodified ``citations.verify_source_ref``. A reviewed role assignment
# stays an explicit operator/AI declaration and is never turned into a grade.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReviewedNumericInputs:
    """The reviewed grid expressed in the numeric service's own input types.

    ``normalization`` is the existing ``NormalizationResult``, so the observations
    and unresolved issues stay in the type the normalizer already returns.
    """

    graph: CanonicalDocumentGraph
    native_receipt: dict
    table_id: str
    cell_source_ids: dict
    promoted_source_ids: frozenset
    normalization: NormalizationResult
    candidates: tuple
    context_receipts: tuple = ()
    # Caller-reviewed footnote/coverage passages outside the table grid. Carried
    # forward from ``review["context"]`` unchanged; never verified or promoted
    # here. See ``_external_review_context``.
    external_context: tuple = ()

    @property
    def observations(self) -> tuple:
        return self.normalization.observations


def native_attested_layout(
    review: dict,
    source: bytes,
    *,
    tenant_id: str = _TENANT,
    verify_context: bool = False,
    requested_values: bool = False,
    also=(),
):
    """Reviewed graph after the existing native attestation and its own replay.

    ``graph_from_review`` is called with its existing result shape; the graph it
    returns is handed to the existing selected-cell verifier and replay for values.
    Optional context proof/replay adds literal metric/unit/header cells; reviewed
    role declarations alone never promote anything. ``requested_values`` opts
    into a separate bounded receipt for exactly the declared value cells; the
    default and legacy context/header proofs remain unchanged.

    ``also`` is forwarded to ``graph_from_review`` so one combined snapshot can hold
    several reviewed grids. Every grid's value cells go through the same unchanged
    value verifier and every grid's declared role cells through the same context
    verifier; ``table_id`` stays the first review's table and the extra tables are
    reachable through the returned cell-id map under their ``"<n>:reviewed-table"``
    keys.
    """
    reviews = (review, *tuple(also))
    graph, native_ids = graph_from_review(review, source, tenant_id=tenant_id, also=also)
    from proofops.adapters.local.selected_cell_table_verification import (
        attest_tables,
        replay_tables,
    )

    header_receipt = None
    if requested_values:
        from proofops.adapters.local import requested_table_verification as requested

        canonical_input = {
            c.source.source_native_id: b.source_id for b in graph.blocks for c in b.candidates
        }
        selected_values = sorted(
            {
                canonical_input[
                    native_ids[("" if i == 0 else f"{i}:") + c["source_cells"]["value"]]
                ]
                for i, item in enumerate(reviews)
                for c in item["candidates"]
            }
        )
        receipt = requested.attest_tables(graph, source, selected_values, tenant_id=tenant_id)
        promoted = requested.replay_tables(
            receipt, graph, source, selected_values, tenant_id=tenant_id
        )
        if verify_context:
            # Existing context reader revalidates its own legacy header proof;
            # never disguise the requested-value receipt as a v3 receipt.
            header_receipt = attest_tables(graph, source, tenant_id=tenant_id)
    else:
        receipt = attest_tables(graph, source, tenant_id=tenant_id)
        promoted = replay_tables(receipt, graph, source, tenant_id=tenant_id)
        header_receipt = receipt
    canonical = {
        candidate.source.source_native_id: block.source_id
        for block in promoted.blocks
        for candidate in block.candidates
    }
    context_receipts = []
    if verify_context:
        from proofops.adapters.local.reviewed_context_verification import (
            MAX_CELLS,
            attest_context_cells,
            replay_context_cells,
        )

        # Both receipts attest the same original graph; combine only their replayed
        # promotions, never feed a changed graph back into the header receipt.
        value_keys: set[str] = set()
        context_keys: set[str] = set()
        for index, item in enumerate(reviews):
            prefix = "" if index == 0 else f"{index}:"
            value_keys.update(prefix + c["source_cells"]["value"] for c in item["candidates"])
            context_keys.update(
                prefix + c["source_cells"][role]
                for c in item["candidates"]
                # ``column_qualifier`` is the opt-in per-column sub-header under a
                # banded year header; it is a real reviewed cell like the other
                # three, so it goes through the same unchanged context verifier.
                for role in ("metric", "unit", "year", "column_qualifier")
                if role in c["source_cells"]
            )
        selected = sorted(canonical[native_ids[key]] for key in context_keys - value_keys)
        for offset in range(0, len(selected), MAX_CELLS):
            batch = selected[offset : offset + MAX_CELLS]
            proof = attest_context_cells(
                graph, source, batch, tenant_id=tenant_id, header_receipt=header_receipt
            )
            checked = replay_context_cells(
                proof, graph, source, batch, tenant_id=tenant_id, header_receipt=header_receipt
            )
            verified = {block.source_id for block in checked.blocks if block.quality == "verified"}
            promoted = replace(
                promoted,
                blocks=tuple(
                    replace(block, quality="verified") if block.source_id in verified else block
                    for block in promoted.blocks
                ),
            )
            context_receipts.append(proof)
    return (
        promoted,
        receipt,
        {key: canonical[native] for key, native in native_ids.items()},
        canonical["reviewed-table"],
        frozenset(
            source_id
            for record in receipt["records"]
            for selection in record["selections"]
            for source_id in selection["promoted_source_ids"]
        )
        | frozenset(
            record["source_id"]
            for proof in context_receipts
            for record in proof["records"]
            if record["status"] == "verified"
        ),
        tuple(context_receipts),
    )


def _numeric_usability(
    observation,
    graph: CanonicalDocumentGraph,
    promoted_source_ids: frozenset,
    reviewed_context: dict,
    external_context: tuple = (),
) -> tuple[list, dict]:
    """Why the numeric service can or cannot read this observation as a source.

    Reports one hold per blocking cell, named by the role that pinned it, so a
    hold is actionable instead of a single opaque refusal. Nothing here decides a
    value: it only reads back the existing verifier's and the domain's own state.

    ``reviewed_context`` carries the reviewed cell keys the four-role normalization
    does not bind (the facility row label and the footnote cells). A nonempty one is
    its own hold: the review says this value has a facility and a note, and neither
    is bound, so the observation stays unverified even when all four literal cells
    verify. An empty list is not a hold, and nothing here invents note semantics.

    ``external_context`` is the separate table-level set of reviewed passages that
    are not any cell (see ``_external_review_context``): a nonempty one can never be
    bound by the per-candidate mechanism above, by construction, so every candidate
    of a table that carries one stays held for it. This does not say the passage
    applies to this value, this year or every year; it only refuses to let an
    unrelated, unbound, caller-reviewed passage disappear from the diagnostic.
    """
    roles = {target: name for _, target, name in observation.parent_relations}
    value_ids = {source for source, _, _ in observation.parent_relations}
    roles.update({source: "value_raw" for source in value_ids})
    holds: list[str] = []
    refs = {}
    for ref in observation.source_refs:
        role = roles.get(ref.source_id, "unpinned_context")
        refs[role] = {
            "source_id": ref.source_id,
            "quote": ref.quote,
            "verification_state": ref.verification_state,
            "natively_promoted": ref.source_id in promoted_source_ids,
        }
        if ref.verification_state != "verified":
            holds.append(f"source_unverified:{role}")
        elif ref.source_id not in promoted_source_ids:
            holds.append(f"cell_not_natively_promoted:{role}")
    if observation.value_state != "value":
        holds.append(f"value_state:{observation.value_state}")
    if len(value_ids) != 1:
        holds.append("observation_root_unresolved")
    for role, keys in sorted(reviewed_context.items()):
        if keys:
            holds.append(f"reviewed_context_unbound:{role}")
    if external_context:
        holds.extend(f"external_context_unbound:{entry['kind']}" for entry in external_context)
    source_holds = observation_source_holds(observation, graph)
    holds.extend(f"source_hold:{reason}" for reason in source_holds["reasons"])
    return sorted(dict.fromkeys(holds)), {"refs": refs, "source_holds": source_holds}


def reviewed_numeric_inputs(
    review: dict,
    source: bytes,
    *,
    tenant_id: str = _TENANT,
    verify_context: bool = False,
    requested_values: bool = False,
) -> ReviewedNumericInputs:
    """Normalize the reviewed roles over the natively attested graph.

    An observation is promoted to ``quality="verified"`` only when every one of
    its own source refs verified through ``verify_source_ref`` against that graph
    and every one of those cells is in the replayed native value/context proofs. That is
    strictly narrower than the refs the pure check re-validates itself, so this
    flag can never admit a value the domain would refuse. Anything else keeps the
    normalizer's ``unverified`` quality and carries its blocking cells.
    """
    graph, receipt, cell_ids, table_id, promoted_ids, context_receipts = native_attested_layout(
        review,
        source,
        tenant_id=tenant_id,
        verify_context=verify_context,
        requested_values=requested_values,
    )
    external_context = _external_review_context(review)
    observations, issues, candidates = [], list(graph.issues), []
    for index, item in enumerate(review["candidates"]):
        refs = item.get("source_cells")
        if not isinstance(refs, dict):
            raise ValueError("reviewed candidate cell roles required")
        required = {"metric", "unit", "year", "value"}
        if not required <= set(refs) or any(not isinstance(refs[key], str) for key in required):
            raise ValueError("reviewed candidate explicit metric/unit/year/value roles required")
        if any(refs[key] not in cell_ids for key in required):
            raise ValueError("reviewed candidate references an unknown cell")
        if set(refs) - required - {"notes", "row_labels", "column_qualifier"}:
            raise ValueError("unsupported reviewed cell role")
        assignment = {
            "metric_raw": cell_ids[refs["metric"]],
            "unit_raw": cell_ids[refs["unit"]],
            "reporting_period": cell_ids[refs["year"]],
            "value_raw": cell_ids[refs["value"]],
        }
        # Reviewed context the existing four-role normalization does not bind: the
        # facility row label and the footnote cells. These are real dimensions of
        # the value, so they are kept visible per candidate by their own reviewed
        # cell keys and, when nonempty, hold the observation below.
        context = {}
        for role in ("row_labels", "notes"):
            keys = refs.get(role, [])
            if not isinstance(keys, list) or any(
                not isinstance(key, str) or key not in cell_ids for key in keys
            ):
                raise ValueError("reviewed context must reference known cells")
            context[role] = list(keys)
        if "column_qualifier" in refs:
            # A banded year header states the year; the sub-header states whether the
            # column is a target or an actual, and that is not bound by the four-role
            # normalization. Keeping it as context means a candidate whose column
            # qualifier is unbound is held, so a target can never be read as an actual.
            key = refs["column_qualifier"]
            if not isinstance(key, str) or key not in cell_ids:
                raise ValueError("reviewed context must reference known cells")
            context["column_qualifier"] = [key]
        record = {
            "candidate_index": index,
            "value_cell": refs["value"],
            "role_cells": {role: refs[role] for role in ("metric", "unit", "year", "value")},
            "unbound_reviewed_context": context,
            "unbound_reviewed_dimensions": sorted(
                set(refs) - required - {"row_labels", "notes", "column_qualifier"}
            ),
            "normalization_status": "held",
        }
        try:
            normalized = normalize_table_bindings(
                graph, table_id=table_id, bindings=(assignment,), tenant_id=tenant_id
            )
        except ValueError as error:
            record["hold_reason"] = str(error)
            record["numeric_usability"] = "not_normalized"
            candidates.append(record)
            continue
        observation = normalized.observations[0]
        observation = replace(
            observation,
            source_refs=tuple(
                verify_source_ref(ref, graph, tenant_id=tenant_id)
                for ref in observation.source_refs
            ),
        )
        holds, detail = _numeric_usability(
            observation, graph, promoted_ids, context, external_context
        )
        if not holds:
            observation = replace(observation, quality="verified")
        observations.append(observation)
        issues.extend(normalized.conflicts)
        record.update(
            normalization_status="normalized",
            observation_id=observation.observation_id,
            value_raw=observation.value_raw,
            metric_raw=observation.metric_raw,
            reporting_period=observation.reporting_period,
            unit_raw=observation.unit_raw,
            unit_canonical=observation.unit_canonical,
            scale_multiplier=observation.scale_multiplier,
            scope=observation.scope,
            subject=observation.subject,
            quality=observation.quality,
            numeric_usability="usable_numeric_source" if not holds else "held",
            numeric_holds=holds,
            **detail,
        )
        candidates.append(record)
    unique = {issue.issue_id: issue for issue in issues}
    return ReviewedNumericInputs(
        graph=graph,
        native_receipt=receipt,
        table_id=table_id,
        cell_source_ids=cell_ids,
        promoted_source_ids=promoted_ids,
        normalization=NormalizationResult(
            tuple(observations), tuple(unique[key] for key in sorted(unique)), graph
        ),
        candidates=tuple(candidates),
        context_receipts=context_receipts,
        external_context=external_context,
    )


def reviewed_table_claim_bridge_status() -> dict:
    """Why a real claim cannot be bound to this grid yet; never a substitute claim.

    A claim must be an actually discovered statement with its own verified source
    provenance. Binding one out of the same table cells that are the evidence would
    compare the table with itself, so no claim is constructed here. Two independent
    blockers remain, both measured on the real pages rather than assumed.
    """
    return {
        "claim_bridge": "unresolved",
        "blockers": [
            # The dimension cells of a reviewed value are never citable, so even a
            # real narrative claim could not be checked against this grid today.
            "reviewed_dimension_cells_unverified:"
            "native_selected_cell_attestation_promotes_the_value_cell_only",
            # The reviewed layout is its own parse, by construction: a different
            # geometry rule must not reuse the frozen run's parse identity.
            "reviewed_parse_manifest_differs_from_the_frozen_run_carrying_discovered_claims",
        ],
    }


def numeric_input_report(inputs: ReviewedNumericInputs, review: dict) -> dict:
    """JSON-safe diagnostic of what the numeric service can read, and what holds."""
    usable = [c for c in inputs.candidates if c.get("numeric_usability") == "usable_numeric_source"]
    blocking: dict[str, int] = {}
    for candidate in inputs.candidates:
        for hold in candidate.get("numeric_holds") or ():
            blocking[hold] = blocking.get(hold, 0) + 1
    report = {
        "schema": _NUMERIC_INPUT_SCHEMA,
        "status": "numeric_service_input_only",
        "eligible_for_admission": False,
        "semantic_verification": "not_run",
        "interpretation": review.get("layout_interpretation"),
        "tenant_id": inputs.graph.tenant_id,
        "source": {
            "source_sha256": review["source_sha256"],
            "physical_page": review["physical_page"],
            "layout_sha256": review["layout_sha256"],
            "document_version_id": inputs.graph.document_version_id,
            "parse_manifest_id": inputs.graph.parse_manifest_id,
        },
        "native_receipt": {
            "schema": inputs.native_receipt["schema"],
            "policy_sha256": inputs.native_receipt["policy_sha256"],
            "artifact_sha256": inputs.native_receipt["artifact_sha256"],
            "promoted_cell_count": len(
                {
                    source_id
                    for record in inputs.native_receipt["records"]
                    for selection in record["selections"]
                    for source_id in selection["promoted_source_ids"]
                }
            ),
        },
        "table_id": inputs.table_id,
        "observation_count": len(inputs.observations),
        "usable_numeric_source_count": len(usable),
        "blocking_holds": dict(sorted(blocking.items())),
        "candidates": [json.loads(json.dumps(c, default=str)) for c in inputs.candidates],
        "normalization_issues": [issue.to_dict() for issue in inputs.normalization.conflicts],
        "numeric_service_call": numeric_service_call(inputs),
        **reviewed_table_claim_bridge_status(),
        "holds": [
            "operator_reviewed_role_assignment_is_not_semantic_approval",
            "reviewed_row_label_and_footnote_context_is_not_bound_by_the_4_role_normalization",
            "native_selected_cell_attestation_promotes_the_value_cell_only",
        ],
    }
    if inputs.context_receipts:
        report["context_cell_receipts"] = list(inputs.context_receipts)
        report["context_cell_verification"] = {
            "verified": sum(
                r["status"] == "verified" for p in inputs.context_receipts for r in p["records"]
            ),
            "held": sum(
                r["status"] != "verified" for p in inputs.context_receipts for r in p["records"]
            ),
            "scope": "literal_text_only",
        }
        report["blockers"][0] = "reviewed_numeric_source_or_context_bindings_remain_unresolved"
        report["holds"][-1] = "context_literals_do_not_approve_semantic_bindings"
    if inputs.external_context:
        # Table-level, not per-candidate: a footnote/coverage passage the review
        # pinned outside the grid can never be bound by the per-candidate
        # notes/row_labels mechanism above, so it is reported once here rather
        # than attributed to any one value. Every candidate already carries its
        # own ``external_context_unbound:<kind>`` hold from ``_numeric_usability``.
        report["unresolved_external_context"] = list(inputs.external_context)
        report["holds"].append(
            "reviewed_external_context_present_and_unbound:"
            + ",".join(sorted({entry["kind"] for entry in inputs.external_context}))
        )
    report["report_sha256"] = canonical_hash(report)
    return report


def numeric_service_call(inputs: ReviewedNumericInputs) -> dict:
    """Invoke the existing numeric service on this grid with no claim and no binding.

    This is a real call into ``application.numeric_analysis``, not a projection of
    one: the reviewed graph and its normalized observations are the inputs the
    service itself validates. ``claims=()`` and ``bindings=()`` because no claim of
    this document is bound to the grid, so the honest result carries no outcome and
    no finding. Supplying a binding here would be manufacturing acceptance.
    """
    from proofops.application.numeric_analysis import analyze_numeric_consistency

    report = analyze_numeric_consistency(
        tenant_id=inputs.graph.tenant_id,
        original=inputs.graph,
        observations=inputs.observations,
        bindings=(),
        claims=(),
    )
    return {
        "invocation": "application.numeric_analysis.analyze_numeric_consistency",
        "claims_supplied": 0,
        "bindings_supplied": 0,
        "observations_supplied": len(inputs.observations),
        "tenant_id": report.tenant_id,
        "document_version_id": report.document_version_id,
        "parse_manifest_id": report.parse_manifest_id,
        "source_sha256": report.source_sha256,
        "outcome_count": len(report.outcomes),
        "has_findings": report.has_findings,
        "interpretation": (
            "no claim of this document is bound to the reviewed grid, so the service "
            "returns no outcome; this is an executed integration, not a finding and "
            "not a grade"
        ),
    }
