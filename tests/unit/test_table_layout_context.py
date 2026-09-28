import pytest


def test_layout_fragments_do_not_attach_status_to_adjacent_bullet():
    from evaluation.table_layout_context import group_words

    def w(index, text, x0, x1, top):
        return dict(index=index, text=text, bbox=[x0, top, x1, top + 7.5])

    words = [
        w(0, "달성", 413, 427, 243),
        w(1, "•", 443, 446, 243),
        w(2, "6MW", 448, 467, 243),
        w(3, "(진행", 407, 422, 255),
        w(4, "중)", 424, 433, 255),
        w(5, "•", 443, 446, 255),
        w(6, "그린팩토리", 448, 481, 255),
    ]
    fragments = group_words(words)
    assert [f["text"] for f in fragments] == ["달성", "• 6MW", "(진행 중)", "• 그린팩토리"]
    assert fragments[2]["word_indices"] == [3, 4]
    assert fragments[2]["bbox"] == [407, 255, 433, 262.5]
    assert fragments == group_words(list(reversed(words)))
    with pytest.raises(ValueError):
        group_words([w(0, "x", 10, 5, 1)])


def test_layout_requires_matching_pdf_and_graph_source():
    from evaluation.table_layout_context import table_layout_context
    from tests.acceptance.test_parsing import TENANT
    from tests.acceptance.test_table_bindings import setup_case

    graph, ids, _ = setup_case()
    with pytest.raises(ValueError, match="source"):
        table_layout_context(graph, b"not the source", [ids["T"]], tenant_id=TENANT)


def test_real_pdf_words_reach_role_packet_as_context_only():
    from dataclasses import replace
    from hashlib import sha256

    from proofops.application.ingest.graph_fusion import fuse_candidates

    from evaluation.table_role_pilot import prepare_rows, validate_roles
    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    source = pdf()
    batch = replace(table([["Header"], ["value"]]), source_sha256=sha256(source).hexdigest())
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    sid = next(
        b.source_id for b in graph.blocks if b.kind == "table_cell" and b.raw_text == "value"
    )
    packet = prepare_rows(graph, [sid], tenant_id=TENANT, source_pdf=source)
    layout = packet["layout_sources"][0]
    assert "emissions" in " ".join(w["text"] for w in layout["words"])
    assert not layout["verified"] and layout["source_sha256"] == sha256(source).hexdigest()
    wire = packet["untrusted_document_data"]["table_layout"][0]
    assert "words" not in wire and wire["coordinate_system"] == "pdf_top_left_points"
    row = packet["untrusted_document_data"]["rows"][0]
    payload = {"tags": [dict(cell_id=row["cells"][0]["id"], role="unknown", basis_ids=[])]}
    assert validate_roles(payload, packet)[0]["binding_status"] == "undetermined"


def test_table_crop_is_source_bound_and_records_pixel_transform():
    import io
    from dataclasses import replace
    from hashlib import sha256

    from PIL import Image
    from proofops.application.ingest.graph_fusion import fuse_candidates

    from evaluation.table_layout_context import table_crop
    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    source = pdf()
    graph = fuse_candidates(
        (replace(table([["Header"], ["Value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    sid = next(b.source_id for b in graph.blocks if b.kind == "table")
    png, provenance = table_crop(graph, source, sid, tenant_id=TENANT)
    assert provenance["source_sha256"] == sha256(source).hexdigest()
    assert provenance["image_sha256"] == sha256(png).hexdigest()
    assert provenance["scale"] == 3 and not provenance["verified"]
    with Image.open(io.BytesIO(png)) as img:
        assert img.size == (1770, 2370)
    with pytest.raises(ValueError, match="source"):
        table_crop(graph, b"wrong PDF", sid, tenant_id=TENANT)


def test_crop_retains_source_bound_outside_page_words_in_companion_context():
    from dataclasses import replace
    from hashlib import sha256

    from proofops.application.ingest.graph_fusion import fuse_candidates

    from evaluation.table_layout_context import table_crop
    from evaluation.table_role_pilot import prepare_rows
    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    source = pdf()
    batch = table([["Header"], ["value"]])
    # Deliberately small table rectangle, with the PDF text outside it.
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(
            replace(b, source=replace(b.source, native_bbox=(0, 0, 50, 50)))
            if b.kind == "table"
            else b
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    sid = next(b.source_id for b in graph.blocks if b.raw_text == "value")
    _, lineage = table_crop(graph, source, tid, tenant_id=TENANT)
    context = lineage["page_context"]
    assert "emissions" in " ".join(w["text"] for w in context["words"])
    assert context["ownership"] == "unassigned"
    assert context["source_sha256"] == sha256(source).hexdigest()
    packet = prepare_rows(graph, [sid], tenant_id=TENANT, source_pdf=source)
    wire = packet["untrusted_document_data"]["table_layout"][0]["page_context"]
    assert "emissions" in " ".join(f["text"] for f in wire["fragments"])
    assert wire["ownership"] == "unassigned"


def test_rotated_outside_labels_are_preserved_as_unreadable_context_not_dropped():
    from dataclasses import replace
    from hashlib import sha256
    from io import BytesIO

    from proofops.application.ingest.graph_fusion import fuse_candidates
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    from evaluation.table_layout_context import table_layout_context
    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(pdf())))
    stream = DecodedStreamObject()
    stream.set_data(
        writer.pages[0].get_contents().get_data()
        + b"\nBT /F1 12 Tf 0 1 -1 0 20 400 Tm (SIDE) Tj ET"
    )
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    batch = table([["Header"], ["Value"]])
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(
            replace(b, source=replace(b.source, native_bbox=(100, 100, 300, 300)))
            if b.kind == "table"
            else b
            for b in batch.blocks
        ),
    )
    graph = fuse_candidates((batch,), tenant_id=TENANT)
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    context = table_layout_context(graph, source, [tid], tenant_id=TENANT)[0]["page_context"]
    assert context["status"] == "partial_unreadable"
    assert context["unreadable_word_indices"]
    assert set(context["unreadable_word_indices"]) <= {w["index"] for w in context["words"]}
    assert not any(
        set(f["word_indices"]) & set(context["unreadable_word_indices"])
        for f in context["fragments"]
    )


def test_pdf_superscript_style_survives_word_flattening_and_model_packet():
    from dataclasses import replace
    from hashlib import sha256
    from io import BytesIO

    from proofops.application.ingest.graph_fusion import fuse_candidates
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import DecodedStreamObject, NameObject

    from evaluation.table_role_pilot import prepare_rows
    from tests.acceptance.test_parsing import TENANT, pdf
    from tests.acceptance.test_tables import table

    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(pdf())))
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 100 400 Td (Metric) Tj /F1 7 Tf 4 Ts (1) Tj "
        b"0 Ts /F1 12 Tf 0 -30 Td (Scope) Tj /F1 12.0000000001 Tf (1) Tj ET"
    )
    writer.pages[0][NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    graph = fuse_candidates(
        (replace(table([["Header"], ["value"]]), source_sha256=sha256(source).hexdigest()),),
        tenant_id=TENANT,
    )
    sid = next(b.source_id for b in graph.blocks if b.raw_text == "value")
    packet = prepare_rows(graph, [sid], tenant_id=TENANT, source_pdf=source)
    styled = packet["layout_sources"][0]["styled_words"]
    assert [w["text"] for w in styled] == ["Metric1"]
    assert "".join(c["text"] for c in styled[0]["characters"]) == "Metric1"
    assert styled[0]["characters"][-1]["size"] == 7
    assert styled[0]["characters"][-1]["bbox"][3] < styled[0]["characters"][0]["bbox"][3]
    assert packet["untrusted_document_data"]["table_layout"][0]["styled_words"] == styled


def test_separate_marker_requires_unique_close_raised_native_words():
    from evaluation.table_layout_context import separate_markers

    def word(text, x0, x1, top, bottom, size):
        return dict(
            text=text,
            x0=x0,
            x1=x1,
            top=top,
            bottom=bottom,
            upright=True,
            chars=[dict(text=text, x0=x0, x1=x1, top=top, bottom=bottom, size=size)],
        )

    base = word("Metric", 10, 40, 100, 112, 12)
    marker = word("1)", 44, 48, 97, 104, 7)
    box = [0, 0, 200, 200]
    pairs = separate_markers([base, marker], box)
    assert len(pairs) == 1
    assert pairs[0]["base"]["index"] == 0 and pairs[0]["marker"]["index"] == 1
    assert pairs[0]["base"]["characters"][0]["size"] == 12
    assert pairs[0]["marker"]["bbox"] == [44, 97, 48, 104]
    assert separate_markers([base, dict(marker, upright=False)], box) == []
    assert separate_markers([base, dict(marker, chars=[])], box) == []
    assert separate_markers([base, marker], [0, 0, 46, 200]) == []
    assert separate_markers([base, base, marker], box) == []
    assert separate_markers([base, word("Other", 10, 40.05, 100, 112, 12), marker], box) == []
    assert separate_markers([base, word("1)", 44, 48, 150, 157, 7)], box) == []
    assert separate_markers([base, word("1)", 44, 48, 100, 112, 12)], box) == []
    assert separate_markers([base, word("1)", 90, 94, 97, 104, 7)], box) == []


def test_native_cell_matches_keep_duplicates_clipping_and_partial_text_unresolved():
    from evaluation.table_layout_context import cell_source_matches, group_words

    words = [
        dict(index=i, text=text, bbox=[i * 100, 0, i * 100 + 20, 10])
        for i, text in enumerate(["39,884", "95.0", "95.0", "Scope1", "2.5"])
    ]
    layout = dict(words=words, fragments=group_words(words), clipped_word_indices=[4])
    cells = [
        dict(row=i, column=0, text=text)
        for i, text in enumerate(["39,884", "95.0", "Scope", "2.5", "Scope1", "Scope1"])
    ]
    matches = cell_source_matches(cells, layout)
    assert matches["r0c0"]["status"] == "unique_proposal"
    assert matches["r0c0"]["fragments"][0]["word_indices"] == [0]
    assert matches["r1c0"]["status"] == "ambiguous"
    assert matches["r2c0"]["status"] == "not_found"
    assert matches["r3c0"]["status"] == "not_found"
    assert matches["r4c0"]["status"] == matches["r5c0"]["status"] == "ambiguous"
    assert all(m["verified"] is False for m in matches.values())
    matches["r0c0"]["fragments"][0]["bbox"][0] = 999
    assert layout["fragments"][0]["bbox"][0] == 0
    duplicate = dict(index=5, text="39,884", bbox=[500, 0, 520, 10])
    layout["fragments"] = group_words([*words, duplicate])
    layout["clipped_word_indices"].append(5)
    assert cell_source_matches(cells, layout)["r0c0"]["status"] == "ambiguous"
