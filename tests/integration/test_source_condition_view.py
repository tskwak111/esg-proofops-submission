"""Original rendering is a review receipt, never source/condition approval."""

import json
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256

import pytest
from proofops.adapters.local.table_notes import freeze_note_review, prepare, validate
from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_parsing import FOREIGN, TENANT, candidate, pdf


def test_original_views_bind_canonical_and_native_fragments_without_approval(monkeypatch):
    from proofops.adapters.local.source_condition_view import render_fragment, replay_fragment_view

    source = pdf()
    graph = fuse_candidates(
        (
            replace(
                candidate(
                    "view",
                    [
                        (
                            "cell",
                            "table_cell",
                            "Page 1 emissions 1234 tCO2e",
                            (70, 710, 300, 740),
                            (),
                        )
                    ],
                ),
                source_sha256=sha256(source).hexdigest(),
            ),
        ),
        tenant_id=TENANT,
    )
    block = graph.blocks[0]
    native = block.candidates[0].source
    fragment = dict(
        source_id=block.source_id,
        parser_run_id=native.parser_run_id,
        source_native_id=native.source_native_id,
        char_start=0,
        char_end=len(native.raw_text),
        raw_text_sha256=sha256(native.raw_text.encode()).hexdigest(),
    )
    before = canonical_hash(graph.to_dict())
    receipt, png = render_fragment(graph, source, fragment, tenant_id=TENANT)
    assert receipt["image_sha256"] == sha256(png).hexdigest()
    assert receipt["scope"] == "source_display_only"
    assert receipt["quote"] == native.raw_text
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert replay_fragment_view(receipt, graph, source, fragment, tenant_id=TENANT) == receipt
    assert canonical_hash(graph.to_dict()) == before and block.quality == "unverified"
    for field, wrong in (
        ("image_sha256", "0" * 64),
        ("physical_page", 2),
        ("quote", "Page 1 emissions 9999 tCO2e"),
    ):
        tampered = deepcopy(receipt)
        tampered[field] = wrong
        tampered["receipt_sha256"] = canonical_hash(
            {k: v for k, v in tampered.items() if k != "receipt_sha256"}
        )
        with pytest.raises(ValueError):
            replay_fragment_view(tampered, graph, source, fragment, tenant_id=TENANT)
    with pytest.raises(ValueError):
        render_fragment(graph, source, fragment, tenant_id=FOREIGN)
    with pytest.raises(ValueError):
        render_fragment(graph, source + b"changed", fragment, tenant_id=TENANT)

    packet = prepare(graph, source, [], tenant_id=TENANT, page=1)
    selected = packet["untrusted_document_data"]["fragments"][0]
    extracted = validate(
        {"notes": [{"fragment_ids": [selected["id"]], "target_ids": [], "kind": "unknown"}]},
        packet,
        graph,
        source,
        tenant_id=TENANT,
    )
    artifact = freeze_note_review(graph, source, packet, extracted, tenant_id=TENANT)
    fragment = dict(
        note_artifact_sha256=json.loads(artifact)["artifact_sha256"],
        packet_sha256=canonical_hash(packet),
        physical_page=1,
        fragment_ids=[selected["id"]],
        native_word_indices=sorted(selected["word_indices"]),
    )
    receipt, png = render_fragment(
        graph, source, fragment, tenant_id=TENANT, note_artifact=artifact
    )
    assert receipt["quote"] == selected["text"]
    geometry = receipt["glyph_geometry"]
    assert geometry["status"] == "matched"
    assert geometry["source_sha256"] == graph.source_sha256
    assert geometry["native_word_indices"] == fragment["native_word_indices"]
    assert geometry["proof_sha256"] == canonical_hash(
        {k: v for k, v in geometry.items() if k != "proof_sha256"}
    )
    forged = deepcopy(receipt)
    forged["glyph_geometry"]["ink_bbox"][0] += 1
    forged["glyph_geometry"]["proof_sha256"] = canonical_hash(
        {k: v for k, v in forged["glyph_geometry"].items() if k != "proof_sha256"}
    )
    forged["receipt_sha256"] = canonical_hash(
        {k: v for k, v in forged.items() if k != "receipt_sha256"}
    )
    with pytest.raises(ValueError):
        replay_fragment_view(
            forged, graph, source, fragment, tenant_id=TENANT, note_artifact=artifact
        )
    assert "source_id" not in receipt["fragment"]
    from proofops.adapters.local import native_glyph_geometry

    with monkeypatch.context() as patch:

        def unavailable(*_args):
            raise ValueError("optional glyph matching unavailable")

        patch.setattr(native_glyph_geometry, "native_word_ink_geometry", unavailable)
        displayed, same_png = render_fragment(
            graph, source, fragment, tenant_id=TENANT, note_artifact=artifact
        )
        assert displayed["glyph_geometry"] is None
        assert displayed["scope"] == "source_display_only" and same_png == png
    assert (
        replay_fragment_view(
            receipt, graph, source, fragment, tenant_id=TENANT, note_artifact=artifact
        )
        == receipt
    )
    for bad in (
        fragment | {"physical_page": 2},
        fragment | {"native_word_indices": [0]},
        fragment | {"fragment_ids": [selected["id"], selected["id"]]},
    ):
        with pytest.raises(ValueError):
            render_fragment(graph, source, bad, tenant_id=TENANT, note_artifact=artifact)


def test_runtime_view_uses_committed_original_and_rejects_unpublished_notes(tmp_path, monkeypatch):
    from proofops.adapters.local.source_condition_view import render_run_fragment

    from tests.integration.test_local_parser_runner import runner_setup

    _service, run_id, runner, _clock, _log = runner_setup(tmp_path, monkeypatch)
    assert runner.run_once(tenant_id=TENANT, run_id=run_id) == "committed"
    graph = runner.load_graph(tenant_id=TENANT, run_id=run_id)
    block = next(b for b in graph.blocks if b.raw_text == "1234 tCO2e" and b.winner is not None)
    native = block.candidates[block.winner].source
    fragment = dict(
        source_id=block.source_id,
        parser_run_id=native.parser_run_id,
        source_native_id=native.source_native_id,
        char_start=0,
        char_end=len(native.raw_text),
        raw_text_sha256=sha256(native.raw_text.encode()).hexdigest(),
    )
    receipt, _png = render_run_fragment(
        runner.store, runner.uploads, runner.parser, fragment, tenant_id=TENANT, run_id=run_id
    )
    assert receipt["run_id"] == run_id and receipt["quote"] == "1234 tCO2e"
    with pytest.raises(ValueError, match="not published"):
        render_run_fragment(
            runner.store,
            runner.uploads,
            runner.parser,
            {"note_artifact_sha256": "0" * 64},
            tenant_id=TENANT,
            run_id=run_id,
        )


@pytest.mark.parametrize("subtype", ["/FreeText", "/Square"])
def test_default_annotation_appearance_is_not_hidden(subtype):
    from io import BytesIO

    from proofops.adapters.local.source_condition_view import render_fragment
    from proofops.adapters.parsing.source_preview import render_page_preview
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DictionaryObject,
        NameObject,
        NumberObject,
        TextStringObject,
    )

    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf())))
    annotation = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject(subtype),
            NameObject("/Rect"): ArrayObject([NumberObject(n) for n in (70, 710, 300, 740)]),
            NameObject("/Contents"): TextStringObject("Condition: excludes subsidiaries"),
            NameObject("/DA"): TextStringObject("/Helvetica 12 Tf 1 0 0 rg"),
            NameObject("/IC"): ArrayObject([NumberObject(1), NumberObject(0), NumberObject(0)]),
        }
    )
    writer.add_annotation(0, annotation)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    graph = fuse_candidates(
        (
            replace(
                candidate(
                    "annot",
                    [
                        (
                            "cell",
                            "table_cell",
                            "Page 1 emissions 1234 tCO2e",
                            (70, 710, 300, 740),
                            (),
                        )
                    ],
                ),
                source_sha256=sha256(source).hexdigest(),
            ),
        ),
        tenant_id=TENANT,
    )
    block = graph.blocks[0]
    c = block.candidates[0]
    fragment = dict(
        source_id=block.source_id,
        parser_run_id=c.source.parser_run_id,
        source_native_id=c.source.source_native_id,
        char_start=0,
        char_end=len(c.source.raw_text),
        raw_text_sha256=sha256(c.source.raw_text.encode()).hexdigest(),
    )
    receipt, png = render_fragment(graph, source, fragment, tenant_id=TENANT)
    omitted, _, _ = render_page_preview(source, 1, c.geometry)
    assert png != omitted
    assert receipt["appearance"] == "default_view_with_annotations_and_acroforms"
    assert block.quality == "unverified"


def _tight_cell(text="1234", bounds=(170, 719, 197, 729)):
    source = pdf()
    graph = fuse_candidates(
        (
            replace(
                candidate("view", [("cell", "table_cell", text, bounds, ())]),
                source_sha256=sha256(source).hexdigest(),
            ),
        ),
        tenant_id=TENANT,
    )
    block = graph.blocks[0]
    native = block.candidates[0].source
    fragment = dict(
        source_id=block.source_id,
        parser_run_id=native.parser_run_id,
        source_native_id=native.source_native_id,
        char_start=0,
        char_end=len(text),
        raw_text_sha256=sha256(text.encode()).hexdigest(),
    )
    return source, graph, fragment


def test_canonical_ink_fallback_preserves_text_and_original_source():
    from proofops.adapters.local.source_condition_view import render_fragment, replay_fragment_view

    source, graph, fragment = _tight_cell()
    before = canonical_hash(graph.to_dict())
    receipt, png = render_fragment(graph, source, fragment, tenant_id=TENANT)
    assert receipt["quote"] == "1234" and receipt["highlight_bbox"] == [170, 71, 197, 81]
    assert receipt["glyph_selection_word_indices"] == [3]
    assert receipt["glyph_geometry"]["source_sha256"] == graph.source_sha256
    assert receipt["scope"] == "source_display_only" and png.startswith(b"\x89PNG")
    assert replay_fragment_view(receipt, graph, source, fragment, tenant_id=TENANT) == receipt
    assert canonical_hash(graph.to_dict()) == before and graph.blocks[0].quality == "unverified"
    tampered = deepcopy(receipt)
    tampered["glyph_selection_word_indices"] = [2]
    tampered["receipt_sha256"] = canonical_hash(
        {k: v for k, v in tampered.items() if k != "receipt_sha256"}
    )
    with pytest.raises(ValueError):
        replay_fragment_view(tampered, graph, source, fragment, tenant_id=TENANT)


@pytest.mark.parametrize(
    "text,bounds", [("4321", (170, 719, 197, 729)), ("1234", (180, 719, 197, 729))]
)
def test_ink_fallback_rejects_wrong_order_and_actual_clipping(text, bounds):
    from proofops.adapters.local.source_condition_view import render_fragment

    source, graph, fragment = _tight_cell(text, bounds)
    with pytest.raises(ValueError):
        render_fragment(graph, source, fragment, tenant_id=TENANT)


def test_ink_fallback_requires_independent_glyph_inventory_coverage(monkeypatch):
    from proofops.adapters.local import native_glyph_geometry
    from proofops.adapters.local.source_condition_view import render_fragment

    source, graph, fragment = _tight_cell()
    original = native_glyph_geometry.native_word_ink_geometry

    def incomplete(*args):
        result = original(*args)
        # Pretend the mapping omitted a glyph while retaining its word bounds.
        result["matched_words"][3]["pdfium_char_indices"].pop()
        return result

    monkeypatch.setattr(native_glyph_geometry, "native_word_ink_geometry", incomplete)
    with pytest.raises(ValueError):
        render_fragment(graph, source, fragment, tenant_id=TENANT)


@pytest.mark.parametrize("unresolved", [0, 3])
def test_glyph_fallback_holds_overlapping_unresolved_words_only(monkeypatch, unresolved):
    from proofops.adapters.local import native_glyph_geometry
    from proofops.adapters.local.source_condition_view import render_fragment

    source, graph, fragment = _tight_cell()
    original = native_glyph_geometry.native_word_ink_geometry

    def partial(*args):
        result = original(*args)
        result["matched_words"] = [
            w for w in result["matched_words"] if w["native_word_index"] != unresolved
        ]
        result.update(unresolved_word_indices=[unresolved], status="unresolved", ink_bbox=None)
        return result

    monkeypatch.setattr(native_glyph_geometry, "native_word_ink_geometry", partial)
    if unresolved == 3:
        with pytest.raises(ValueError, match="unresolved native word"):
            render_fragment(graph, source, fragment, tenant_id=TENANT)
    else:
        receipt, _ = render_fragment(graph, source, fragment, tenant_id=TENANT)
        assert receipt["glyph_selection_word_indices"] == [3]


@pytest.mark.parametrize("marker_y", [720, 728])
def test_glyph_fallback_preserves_original_adjacent_marker_without_inserting_space(marker_y):
    from io import BytesIO

    from proofops.adapters.local.source_condition_view import render_fragment
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    source, graph, fragment = _tight_cell("12342)", (170, 718, 207, 736))
    reader = PdfReader(BytesIO(source))
    page = reader.pages[0]
    stream = DecodedStreamObject()
    stream.set_data(
        (
            r"BT /F1 12 Tf 170 720 Td (1234) Tj /F1 6 Tf "
            rf"1 0 0 1 196.688 {marker_y} Tm (2\)) Tj ET"
        ).encode()
    )
    page[NameObject("/Contents")] = stream
    writer = PdfWriter()
    writer.add_page(page)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    graph = replace(
        graph,
        source_sha256=sha256(source).hexdigest(),
        candidates=tuple(
            replace(b, source_sha256=sha256(source).hexdigest()) for b in graph.candidates
        ),
    )
    if marker_y == 728:
        with pytest.raises(ValueError, match="glyph order ambiguous"):
            render_fragment(graph, source, fragment, tenant_id=TENANT)
        return
    receipt, _ = render_fragment(graph, source, fragment, tenant_id=TENANT)
    assert receipt["quote"] == "12342)"
    assert receipt["glyph_selection_word_indices"] == [0, 1]
