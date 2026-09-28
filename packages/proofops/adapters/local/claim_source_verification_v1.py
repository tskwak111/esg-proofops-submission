"""Original-PDF claim span receipts. Legacy paragraph receipts stay immutable."""

import io
import math
from dataclasses import asdict, replace
from hashlib import sha256
from pathlib import Path

import pdfplumber
from pdfminer.pdftypes import resolve1

from proofops.adapters.local.native_glyph_geometry import native_word_ink_geometry
from proofops.adapters.local.run_artifacts import native_paragraph_policy
from proofops.adapters.local.source_verification import _rendered_text
from proofops.application.evidence import span_citations
from proofops.application.evidence.citations import _normalized, verify_source_ref
from proofops.application.ingest.gri import _validate_graph
from proofops.domain.provenance import canonical_hash


def claim_source_policy():
    return dict(
        schema="claim_source_policy_v1",
        verifier_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        citation_sha256=sha256(Path(span_citations.__file__).read_bytes()).hexdigest(),
        native_policy=native_paragraph_policy(),
    )


def _pushbuttons_only(form):
    if not isinstance(form, dict) or not set(form) <= {"Fields", "DA", "DR"}:
        return False
    fields = resolve1(form.get("Fields"))
    if not isinstance(fields, list):
        return False
    stack = [(field, None, 0) for field in fields]
    seen = set()
    while stack:
        value, inherited_type, inherited_flags = stack.pop()
        value = resolve1(value)
        if not isinstance(value, dict) or id(value) in seen or len(seen) > 10000:
            return False
        seen.add(id(value))
        kind, flags = value.get("FT", inherited_type), value.get("Ff", inherited_flags)
        children = resolve1(value.get("Kids"))
        if children is not None:
            if not isinstance(children, list) or not children:
                return False
            stack.extend((child, kind, flags) for child in children)
        elif (
            getattr(kind, "name", None) != "Btn"
            or flags != 65536
            or getattr(value.get("Subtype"), "name", None) != "Widget"
        ):
            return False
    return True


def _appearance_overlaps(page, box):
    if box is None:
        return True
    for annotation in page.annots or []:
        if not annotation.get("data", {}).get("AP"):
            continue
        bounds = [annotation.get(k) for k in ("x0", "top", "x1", "bottom")]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds):
            return True
        x0, y0, x1, y1 = bounds
        if x0 >= x1 or y0 >= y1:
            return True
        if x1 > box[0] - 2 and x0 < box[2] + 2 and y1 > box[1] - 2 and y0 < box[3] + 2:
            return True
    return False


def _unique_quote(text, quote):
    start = text.find(quote)
    if not quote or start < 0 or text.find(quote, start + 1) >= 0:
        return False
    end = start + len(quote)
    return not (
        (start > 0 and text[start - 1].isalnum() and quote[0].isalnum())
        or (end < len(text) and text[end].isalnum() and quote[-1].isalnum())
    )


def attest_claim_spans(graph, source, refs, *, tenant_id):
    _validate_graph(graph, tenant_id)
    if (
        not isinstance(source, bytes)
        or len(source) > 100 * 1024 * 1024
        or sha256(source).hexdigest() != graph.source_sha256
    ):
        raise ValueError("CLAIM_SOURCE_MISMATCH")
    blocks = {b.source_id: b for b in graph.blocks}
    readings, glyphs, records = {}, {}, []
    with pdfplumber.open(io.BytesIO(source)) as document:
        interactive = "OCProperties" in document.doc.catalog or (
            "AcroForm" in document.doc.catalog
            and not _pushbuttons_only(resolve1(document.doc.catalog.get("AcroForm")))
        )
        for ref in refs:
            record = dict(ref=asdict(ref), status="unresolved", reason="source_invalid")
            records.append(record)
            block = blocks.get(ref.source_id)
            if (
                block is None
                or block.kind != "paragraph"
                or block.quality not in {"verified", "unverified"}
                or block.winner is None
            ):
                continue
            checked = replace(
                graph,
                blocks=tuple(
                    replace(b, quality="verified") if b.source_id == ref.source_id else b
                    for b in graph.blocks
                ),
            )
            if (
                verify_source_ref(ref, checked, tenant_id=tenant_id).verification_state
                != "verified"
            ):
                continue
            if ref.source_id not in readings:
                readings[ref.source_id] = _read_paragraph(
                    document, source, block, interactive, glyphs
                )
            reading = readings[ref.source_id]
            record.update(reason=reading["reason"], reading_sha256=canonical_hash(reading))
            quote = _normalized(ref.quote)
            if reading["reason"] == "rendered_quote_unresolved":
                native = _normalized(block.raw_text)
                rendered = _normalized(reading["rendered"].get("text", ""))
                # Require a unique occurrence in both the pinned paragraph and
                # its image; no fuzzy, whitespace removal, or number correction.
                if _unique_quote(native, quote) and _unique_quote(rendered, quote):
                    record.update(status="verified", reason=None)
    receipt = dict(
        schema="claim_source_attestation_v1",
        tenant_id=tenant_id,
        document_version_id=graph.document_version_id,
        parse_manifest_id=graph.parse_manifest_id,
        source_sha256=graph.source_sha256,
        graph_sha256=canonical_hash(asdict(graph)),
        policy=claim_source_policy(),
        records=records,
        readings=readings,
    )
    receipt["artifact_sha256"] = canonical_hash(receipt)
    return receipt


def _read_paragraph(document, source, block, interactive, glyphs):
    unresolved = dict(status="unresolved", reason="geometry_unsupported")
    candidate = block.candidates[block.winner]
    box, geometry = candidate.bbox, candidate.geometry
    if not 1 <= block.page_num <= len(document.pages):
        return unresolved
    page = document.pages[block.page_num - 1]
    if interactive or _appearance_overlaps(page, box):
        return dict(status="unresolved", reason="interactive_visibility_requires_review")
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
        return unresolved
    if block.page_num not in glyphs:
        words = page.extract_words()
        try:
            proof = native_word_ink_geometry(source, block.page_num, list(range(len(words))))
        except ValueError:
            proof = {}
        glyphs[block.page_num] = words, proof
    words, proof = glyphs[block.page_num]
    if not isinstance(proof.get("matched_words"), list):
        return dict(status="unresolved", reason="glyph_geometry_unresolved")
    boxes = {w["native_word_index"]: w["ink_bbox"] for w in proof["matched_words"]}
    missing = set(proof.get("unresolved_word_indices", ()))
    if set(boxes) | missing != set(range(len(words))):
        return dict(status="unresolved", reason="glyph_geometry_unresolved")
    if any(
        words[i]["x1"] > box[0]
        and words[i]["x0"] < box[2]
        and words[i]["bottom"] > box[1]
        and words[i]["top"] < box[3]
        for i in missing
    ):
        return dict(status="unresolved", reason="glyph_geometry_unresolved")
    selected = []
    for index, word in enumerate(words):
        if index not in boxes:
            continue
        wb = boxes[index]
        if wb[2] <= box[0] or wb[0] >= box[2] or wb[3] <= box[1] or wb[1] >= box[3]:
            continue
        if not word["upright"] or not (
            box[0] - 0.001 <= wb[0] < wb[2] <= box[2] + 0.001
            and box[1] - 0.001 <= wb[1] < wb[3] <= box[3] + 0.001
        ):
            return dict(status="unresolved", reason="clipped_or_rotated_words")
        selected.append(dict(index=index, text=word["text"], bbox=wb))
    raw = " ".join(w["text"] for w in selected)
    if not raw or _normalized(raw) != _normalized(block.raw_text):
        return dict(status="unresolved", reason="text_mismatch")
    rendered = _rendered_text(page, box)
    attempts = [rendered]
    if rendered.get("status") == "read" and _normalized(rendered.get("text", "")) != _normalized(
        raw
    ):
        rendered = _rendered_text(page, box, padding_px=6)
        attempts.append(rendered)
    return dict(
        status="unresolved",
        reason="rendered_quote_unresolved"
        if rendered.get("status") == "read"
        else "rendered_reader_unavailable",
        words=selected,
        rendered=rendered,
        rendered_attempts=attempts,
        glyph_geometry=proof,
    )


def discovery_refs(discovery):
    return tuple(
        ref
        for claim in discovery.claims
        if claim.source_quality == "unverified"
        for ref in claim.source_refs
    )


def replay_claim_spans(receipt, graph, source, discovery, *, tenant_id):
    expected = attest_claim_spans(graph, source, discovery_refs(discovery), tenant_id=tenant_id)
    if canonical_hash(receipt) != canonical_hash(expected):
        raise ValueError("CLAIM_SOURCE_RECEIPT_MISMATCH")
    from proofops.domain.values import SourceRef

    refs = tuple(
        replace(SourceRef(**r["ref"]), verification_state="verified")
        for r in expected["records"]
        if r["status"] == "verified"
    )
    scoped = span_citations.span_verified_graph(graph, refs, expected["artifact_sha256"])
    claims = []
    for claim in discovery.claims:
        if claim.source_quality != "unverified":
            claims.append(claim)
            continue
        checked = tuple(
            span_citations.verify_source_ref(ref, scoped, tenant_id=tenant_id)
            for ref in claim.source_refs
        )
        claims.append(
            replace(claim, source_quality="verified", source_refs=checked)
            if checked and all(ref.verification_state == "verified" for ref in checked)
            else claim
        )
    return replace(discovery, claims=tuple(claims)), scoped
