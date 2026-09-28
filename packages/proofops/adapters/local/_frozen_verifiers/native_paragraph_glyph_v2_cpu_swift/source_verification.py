"""Original-PDF paragraph attestations; no table/footnote binding or grade approval."""

import io
import json
import math
import subprocess
import sys
import tempfile
from dataclasses import asdict, replace
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pdfplumber
from pdfminer.pdftypes import resolve1
from PIL import ImageOps

from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry
from proofops.application.evidence import citations
from proofops.application.evidence.citations import _normalized
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.provenance import canonical_hash


def _rendered_text(page, box, *, padding_px=0):
    if sys.platform != "darwin":
        return dict(
            status="unresolved", reason="rendered_reader_unavailable", error="UnsupportedPlatform"
        )
    if page.width * page.height * 9 > 16_000_000:
        return dict(status="unresolved", reason="render_limit")
    try:
        with page.to_image(resolution=216).original as image:
            pixels = [
                math.floor(box[0] * 3),
                math.floor(box[1] * 3),
                math.ceil(box[2] * 3),
                math.ceil(box[3] * 3),
            ]
            with image.crop(pixels) as crop:
                buffer = io.BytesIO()
                # Keep original pixels; blank margins prevent edge-touching OCR errors.
                with ImageOps.expand(crop, border=padding_px, fill="white") as padded:
                    padded.save(buffer, format="PNG")
        png = buffer.getvalue()
        with tempfile.TemporaryDirectory(prefix="proofops-source-") as folder:
            path = Path(folder) / "region.png"
            path.write_bytes(png)
            output = subprocess.run(
                ["swift", str(Path(__file__).with_name("native_ocr.swift")), str(path)],
                check=True,
                capture_output=True,
                timeout=30,
            )
        result = json.loads(output.stdout)
        if not isinstance(result.get("text"), str) or len(result["text"]) > 200000:
            raise ValueError("invalid rendered text")
        return dict(
            status="read",
            image_sha256=sha256(png).hexdigest(),
            pixel_bbox=pixels,
            scale=3,
            padding_px=padding_px,
            **result,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return dict(
            status="unresolved", reason="rendered_reader_unavailable", error=type(error).__name__
        )


def attest_native_sources(graph, source, *, tenant_id, geometry_mode="font"):
    """Create a replayable receipt; caller stores it as a new immutable artifact."""
    if geometry_mode not in {"font", "glyph"}:
        raise ValueError("unsupported native geometry mode")
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
    ):
        raise ValueError("native source mismatch")
    records = []
    glyph_pages = {}
    with pdfplumber.open(io.BytesIO(source)) as document:
        form = resolve1(document.doc.catalog.get("AcroForm"))
        # A default font dictionary with explicitly zero fields is not interactive.
        # Unknown form features and malformed/missing field arrays stay unresolved.
        empty_form = (
            isinstance(form, dict)
            and set(form) <= {"Fields", "DA", "DR"}
            and isinstance(resolve1(form.get("Fields")), list)
            and resolve1(form["Fields"]) == []
        )
        interactive = "OCProperties" in document.doc.catalog or (
            "AcroForm" in document.doc.catalog and not empty_form
        )
        for block in graph.blocks:
            record = dict(
                source_id=block.source_id,
                status="unresolved",
                reason="relationship_validation_required",
                words=[],
            )
            records.append(record)
            if block.kind != "paragraph":
                continue
            record["reason"] = "source_quality_unresolved"
            if block.quality != "unverified" or block.winner is None:
                continue
            candidate = block.candidates[block.winner]
            if not any(candidate in batch.blocks for batch in graph.candidates):
                raise ValueError("native candidate provenance mismatch")
            box, geometry = candidate.bbox, candidate.geometry
            if not 1 <= block.page_num <= len(document.pages):
                raise ValueError("native page outside document")
            page = document.pages[block.page_num - 1]
            record["reason"] = "interactive_visibility_requires_review"
            if interactive or any(
                annotation.get("data", {}).get("AP") for annotation in (page.annots or [])
            ):
                continue
            record["reason"] = "geometry_unsupported"
            if (
                box is None
                or candidate.has_invalid_geometry
                or page.rotation
                or geometry.rotation
                or tuple(page.bbox[:2]) != (0, 0)
                or tuple(geometry.crop_box[:2]) != (0, 0)
                or abs(page.width - geometry.width_pt) > 0.001
                or abs(page.height - geometry.height_pt) > 0.001
            ):
                continue
            words = page.extract_words()
            glyph_boxes = {}
            if geometry_mode == "glyph":
                page_key = str(block.page_num)
                if page_key not in glyph_pages:
                    try:
                        glyph_pages[page_key] = native_word_ink_geometry(
                            source, block.page_num, list(range(len(words)))
                        )
                    except ValueError:
                        glyph_pages[page_key] = dict(status="unresolved")
                proof = glyph_pages[page_key]
                record["reason"] = "glyph_geometry_unresolved"
                if not isinstance(proof.get("matched_words"), list):
                    continue
                glyph_boxes = {
                    w["native_word_index"]: w["ink_bbox"] for w in proof["matched_words"]
                }
                unresolved = set(proof.get("unresolved_word_indices", ()))
                if set(glyph_boxes) | unresolved != set(range(len(words))):
                    continue
                # Unmapped words in this crop remain a blocker. Unrelated rotated
                # navigation elsewhere on the page must not disable every paragraph.
                if any(
                    words[i]["x1"] > box[0]
                    and words[i]["x0"] < box[2]
                    and words[i]["bottom"] > box[1]
                    and words[i]["top"] < box[3]
                    for i in unresolved
                ):
                    continue
            clipped = False
            for index, word in enumerate(words):
                if geometry_mode == "glyph" and index not in glyph_boxes:
                    continue
                wb = (
                    glyph_boxes[index]
                    if geometry_mode == "glyph"
                    else [word["x0"], word["top"], word["x1"], word["bottom"]]
                )
                if wb[2] <= box[0] or wb[0] >= box[2] or wb[3] <= box[1] or wb[1] >= box[3]:
                    continue
                if not word["upright"] or not (
                    # Parser coordinates are rounded to 0.001pt; allow only that quantization.
                    box[0] - 0.001 <= wb[0] < wb[2] <= box[2] + 0.001
                    and box[1] - 0.001 <= wb[1] < wb[3] <= box[3] + 0.001
                ):
                    clipped = True
                record["words"].append(dict(index=index, text=word["text"], bbox=wb))
            raw = " ".join(w["text"] for w in record["words"])
            record["reason"] = "clipped_or_rotated_words" if clipped else "text_mismatch"
            if not clipped and raw and _normalized(raw) == _normalized(candidate.source.raw_text):
                rendered = _rendered_text(page, box)
                if (
                    rendered["status"] == "read"
                    and rendered["text"]
                    and _normalized(rendered["text"]) != _normalized(raw)
                ):
                    # One fixed, text-blind retry; retain both independent readings.
                    retry = _rendered_text(page, box, padding_px=6)
                    record["rendered_attempts"] = [rendered, retry]
                    rendered = retry
                record["rendered"] = rendered
                record["reason"] = "rendered_text_unresolved"
                if rendered["status"] == "read" and _normalized(rendered["text"]) == _normalized(
                    raw
                ):
                    record.update(status="verified", reason=None)
    result = dict(
        schema="native_paragraph_attestation_v1",
        reader="pdfplumber",
        reader_version=version("pdfplumber"),
        renderer_version=version("pypdfium2"),
        rendered_reader_sha256=sha256(
            Path(__file__).with_name("native_ocr.swift").read_bytes()
        ).hexdigest(),
        text_reader_version=version("pdfminer.six"),
        verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        normalization_sha256=sha256(Path(citations.__file__).read_bytes()).hexdigest(),
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        input_graph_sha256=canonical_hash(asdict(graph)),
        records=records,
        scope="paragraph_native_and_rendered_text_only",
        coordinate_system="pdf_top_left_points",
    )
    if geometry_mode == "glyph":
        result.update(
            schema="native_paragraph_attestation_v2",
            geometry_mode="glyph",
            glyph_geometry=glyph_pages,
            glyph_verifier_sha256=sha256(
                Path(__file__).with_name("native_glyph_geometry.py").read_bytes()
            ).hexdigest(),
        )
    result["artifact_sha256"] = canonical_hash(result)
    return result


def replay_native_sources(receipt, graph, source, *, tenant_id):
    """Recompute against original bytes before returning a new source-quality view."""
    mode = "glyph" if receipt.get("schema") == "native_paragraph_attestation_v2" else "font"
    expected = attest_native_sources(graph, source, tenant_id=tenant_id, geometry_mode=mode)
    if canonical_hash(receipt) != canonical_hash(expected):
        raise ValueError("native attestation mismatch")
    verified = {r["source_id"] for r in expected["records"] if r["status"] == "verified"}
    return replace(
        graph,
        blocks=tuple(
            replace(block, quality="verified") if block.source_id in verified else block
            for block in graph.blocks
        ),
    )
