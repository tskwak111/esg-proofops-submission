import sys
from dataclasses import replace
from hashlib import sha256
from io import BytesIO

from proofops.application.ingest.graph_fusion import fuse_candidates
from proofops.domain.provenance import canonical_hash

from tests.acceptance.test_parsing import TENANT, candidate, pdf
from tests.integration.test_native_table_admission import fixture


def ref_for(block, quote):
    start = block.raw_text.index(quote)
    return block.source_ref(normalized_char_start=start, normalized_char_end=start + len(quote))


def cell(graph, text):
    return next(b for b in graph.blocks if b.kind == "table_cell" and b.raw_text == text)


def crop_ocr(page, box, **kwargs):
    return dict(status="read", text=page.crop(box).extract_text() or "", image_sha256="a" * 64)


def merged_cell_fixture(text, *, second_line=None, overlay=None, cell_box=(20, 730, 420, 770)):
    from pdfplumber import open as open_pdf
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
    )
    lines = [f"BT /F1 10 Tf 30 750 Td ({text}) Tj ET"]
    if second_line:
        lines.append(f"BT /F1 10 Tf 30 735 Td ({second_line}) Tj ET")
    if overlay:
        lines.append(f"BT /F1 10 Tf 90 746 Td ({overlay}) Tj ET")
    lines.extend(
        (
            "BT /F1 10 Tf 430 750 Td (code) Tj ET",
            "BT /F1 10 Tf 30 720 Td (unit) Tj ET",
            "BT /F1 10 Tf 430 720 Td (79) Tj ET",
        )
    )
    stream = DecodedStreamObject()
    stream.set_data("\n".join(lines).encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    with open_pdf(BytesIO(source)) as document:
        words = document.pages[0].extract_words()
    top_box = (cell_box[0], 800 - cell_box[3], cell_box[2], 800 - cell_box[1])
    first_text = " ".join(
        word["text"]
        for word in words
        if word["x0"] >= top_box[0]
        and word["x1"] <= top_box[2]
        and word["top"] >= top_box[1]
        and word["bottom"] <= top_box[3]
    )
    raw = [
        ("T", "table", "", (20, 690, 560, 780), ()),
        ("R0", "table_row", f"{first_text}\tcode", (20, 730, 560, 770), ()),
        ("R1", "table_row", "unit\t79", (20, 700, 560, 730), ()),
        ("C00", "table_cell", first_text, cell_box, ()),
        ("C01", "table_cell", "code", (420, 730, 560, 770), ()),
        ("C10", "table_cell", "unit", (20, 700, 420, 730), ()),
        ("C11", "table_cell", "79", (420, 700, 560, 730), ()),
    ]
    edges = [("R0", "T", "table_parent"), ("R1", "T", "table_parent")]
    for row, ids in (("R0", ("C00", "C01")), ("R1", ("C10", "C11"))):
        edges.extend((cell_id, row, "table_parent") for cell_id in ids)
        edges.extend((cell_id, "T", "table_parent") for cell_id in ids)
    batch = candidate("line-test", raw, edges)
    batch = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(
            replace(
                block,
                table_native_id="T",
                row_number=0 if block.source.source_native_id in {"C00", "C01"} else 1,
                column_number=0 if block.source.source_native_id in {"C00", "C10"} else 1,
            )
            if block.kind == "table_cell"
            else block
            for block in batch.blocks
        ),
    )
    return source, fuse_candidates((batch,), tenant_id=TENANT)


def _line_ocr(quote, *, crop_text=None):
    def read(page, box, **kwargs):
        text = (
            quote
            if box[2] - box[0] < 200 and box[1] < 70
            else "deliberately misread full merged cell"
        )
        if crop_text is not None and text == quote:
            text = crop_text
        return dict(status="read", text=text, image_sha256="d" * 64)

    return read


def _replace_batch(source, graph, transform):
    batch = graph.candidates[0]
    changed = replace(
        batch,
        source_sha256=sha256(source).hexdigest(),
        blocks=tuple(transform(block) for block in batch.blocks),
    )
    return fuse_candidates((changed,), tenant_id=TENANT)


def stable_graph(graph):
    old_table = next((block for block in graph.blocks if block.kind == "table"), None)
    batch = graph.candidates[0]
    fixed_run = "55555555-5555-4555-8555-555555555555"
    batch = replace(
        batch,
        parser_run_id=fixed_run,
        blocks=tuple(
            replace(block, source=replace(block.source, parser_run_id=fixed_run))
            for block in batch.blocks
        ),
    )
    result = fuse_candidates((batch,), tenant_id=TENANT)
    if old_table is None:
        return result
    new_table = next(block for block in result.blocks if block.kind == "table")
    return replace(
        result,
        issues=tuple(
            replace(
                issue,
                source_ids=tuple(
                    new_table.source_id if source_id == old_table.source_id else source_id
                    for source_id in issue.source_ids
                ),
            )
            for issue in graph.issues
        ),
    )


def test_table_cell_and_row_quote_are_attested_inside_their_geometry(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    refs = (
        ref_for(cell(graph, "2024"), "2024"),
        ref_for(
            next(
                b for b in graph.blocks if b.kind == "table_row" and b.raw_text.startswith("Year")
            ),
            "2024",
        ),
    )

    receipt = verifier.attest_table_spans(graph, source, refs, tenant_id=TENANT)

    assert [record["status"] for record in receipt["records"]] == ["verified", "verified"]
    assert all(record["reason"] is None for record in receipt["records"])
    unsigned = dict(receipt)
    artifact_sha256 = unsigned.pop("artifact_sha256")
    assert receipt["policy"]["schema"] == "table_span_source_policy_v1"
    assert artifact_sha256 == canonical_hash(unsigned)


def test_wrong_text_other_column_duplicate_and_token_cut_stay_unresolved(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    value = cell(graph, "2024")
    valid = ref_for(value, "2024")
    cut = ref_for(value, "2024")
    cut = replace(cut, char_end=cut.char_start + 3, quote="202")
    other_column_batch = candidate(
        "other_column",
        [("C", "table_cell", "2024", (220, 740, 320, 770), ())],
    )
    other_column_batch = replace(other_column_batch, source_sha256=sha256(source).hexdigest())
    other_column_graph = fuse_candidates((other_column_batch,), tenant_id=TENANT)
    other_column = ref_for(cell(other_column_graph, "2024"), "2024")
    monkeypatch.setattr(
        verifier,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="2025", image_sha256="b" * 64),
    )
    wrong = verifier.attest_table_spans(graph, source, (valid,), tenant_id=TENANT)
    monkeypatch.undo()
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    misplaced = verifier.attest_table_spans(
        other_column_graph, source, (other_column,), tenant_id=TENANT
    )
    monkeypatch.setattr(
        verifier,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="2024 2024", image_sha256="c" * 64),
    )
    duplicate = verifier.attest_table_spans(graph, source, (valid,), tenant_id=TENANT)
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    truncated = verifier.attest_table_spans(graph, source, (cut,), tenant_id=TENANT)

    assert [
        receipt["records"][0]["status"] for receipt in (wrong, misplaced, duplicate, truncated)
    ] == ["unresolved"] * 4


def test_oversized_cell_bbox_cannot_attest_only_its_matching_subspan(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()

    def widen(block):
        if block.source.source_native_id == "C01":
            return replace(block, source=replace(block.source, native_bbox=(120, 740, 320, 770)))
        return block

    graph = _replace_batch(source, graph, widen)
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    value = next(
        block
        for block in graph.blocks
        if block.kind == "table_cell"
        and any(item.source.source_native_id == "C01" for item in block.candidates)
    )

    receipt = verifier.attest_table_spans(
        graph, source, (ref_for(value, "2024"),), tenant_id=TENANT
    )

    assert receipt["records"][0]["status"] == "unresolved"


def test_table_bbox_disjoint_from_row_cannot_attest_cell_or_row(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()
    graph = _replace_batch(
        source,
        graph,
        lambda block: replace(block, source=replace(block.source, native_bbox=(330, 710, 550, 770)))
        if block.source.source_native_id == "T"
        else block,
    )
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    value = cell(graph, "2024")
    row = next(
        block
        for block in graph.blocks
        if block.kind == "table_row" and block.raw_text.startswith("Year")
    )

    receipt = verifier.attest_table_spans(
        graph,
        source,
        (ref_for(value, "2024"), ref_for(row, "2024")),
        tenant_id=TENANT,
    )

    assert [record["status"] for record in receipt["records"]] == ["unresolved", "unresolved"]


def test_cell_bbox_in_another_column_cannot_attest_same_text(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()
    assert source.count(b"(2025)") == 1
    source = source.replace(b"(2025)", b"(2024)")
    row_text = "Year\t2024\t2024"
    table_text = row_text + "\nMWh\t25\t79"

    def move_to_other_column(block):
        native_id = block.source.source_native_id
        if native_id == "C01":
            other = next(
                item for item in graph.candidates[0].blocks if item.source.source_native_id == "C02"
            )
            return replace(
                block,
                context=("different declared slot",),
                source=replace(block.source, native_bbox=other.source.native_bbox),
            )
        if native_id == "C02":
            return replace(block, source=replace(block.source, raw_text="2024"))
        if native_id == "R0":
            return replace(block, source=replace(block.source, raw_text=row_text))
        if native_id == "T":
            return replace(block, source=replace(block.source, raw_text=table_text))
        return block

    graph = _replace_batch(source, graph, move_to_other_column)
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    value = next(
        block
        for block in graph.blocks
        if block.kind == "table_cell"
        and any(item.source.source_native_id == "C01" for item in block.candidates)
    )

    receipt = verifier.attest_table_spans(
        graph, source, (ref_for(value, "2024"),), tenant_id=TENANT
    )

    assert receipt["records"][0]["status"] == "unresolved"


def test_cell_bbox_in_another_row_cannot_attest_same_text(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()
    row_text = "Year\t25\t2025"
    table_text = row_text + "\nMWh\t25\t79"

    def move_to_other_row(block):
        native_id = block.source.source_native_id
        if native_id == "C01":
            other = next(
                item for item in graph.candidates[0].blocks if item.source.source_native_id == "C11"
            )
            return replace(
                block,
                context=("different declared slot",),
                source=replace(
                    block.source,
                    native_bbox=other.source.native_bbox,
                    raw_text="25",
                    char_end=block.source.char_start + 2,
                ),
            )
        if native_id == "R0":
            return replace(
                block,
                source=replace(
                    block.source,
                    raw_text=row_text,
                    char_end=block.source.char_start + len(row_text),
                ),
            )
        if native_id == "T":
            return replace(
                block,
                source=replace(
                    block.source,
                    raw_text=table_text,
                    char_end=block.source.char_start + len(table_text),
                ),
            )
        return block

    graph = _replace_batch(source, graph, move_to_other_row)
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    value = next(
        block
        for block in graph.blocks
        if block.kind == "table_cell"
        and any(item.source.source_native_id == "C01" for item in block.candidates)
    )

    receipt = verifier.attest_table_spans(graph, source, (ref_for(value, "25"),), tenant_id=TENANT)

    assert receipt["records"][0]["status"] == "unresolved"


def test_row_without_index_rejects_a_child_with_a_conflicting_row_index(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()

    def mismatch_child(block):
        if block.source.source_native_id == "C01":
            return replace(block, row_number=1)
        return block

    graph = _replace_batch(source, graph, mismatch_child)
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    row = next(
        block
        for block in graph.blocks
        if block.kind == "table_row" and block.raw_text.startswith("Year")
    )

    receipt = verifier.attest_table_spans(graph, source, (ref_for(row, "2024"),), tenant_id=TENANT)

    assert receipt["records"][0]["status"] == "unresolved"


def test_partial_rendered_cell_cannot_be_replaced_with_quote_crop(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()
    value = cell(graph, "2024")
    ref = ref_for(value, "2024")
    monkeypatch.setattr(
        verifier,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="table columns out of order"),
    )
    receipt = verifier.attest_table_spans(graph, source, (ref,), tenant_id=TENANT)

    assert receipt["records"][0]["status"] == "unresolved"
    assert receipt["records"][0]["reason"] == "rendered_cell_text_mismatch"


def test_missing_geometry_and_non_table_block_stay_unresolved(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = fixture()
    monkeypatch.setattr(verifier, "_rendered_text", crop_ocr)
    batch = graph.candidates[0]
    missing_geometry_batch = replace(
        batch,
        blocks=tuple(
            replace(block, source=replace(block.source, native_bbox=None))
            if block.source.source_native_id == "C01"
            else block
            for block in batch.blocks
        ),
    )
    missing_graph = fuse_candidates((missing_geometry_batch,), tenant_id=TENANT)
    missing_ref = ref_for(cell(missing_graph, "2024"), "2024")
    paragraph_batch = candidate(
        "paragraph",
        [("P1", "paragraph", "Year", (20, 30, 120, 60), ())],
    )
    paragraph_batch = replace(paragraph_batch, source_sha256=sha256(source).hexdigest())
    paragraph_graph = fuse_candidates((paragraph_batch,), tenant_id=TENANT)
    paragraph_ref = ref_for(paragraph_graph.blocks[0], "Year")

    missing = verifier.attest_table_spans(missing_graph, source, (missing_ref,), tenant_id=TENANT)
    non_table = verifier.attest_table_spans(
        paragraph_graph, source, (paragraph_ref,), tenant_id=TENANT
    )

    assert missing["records"][0]["status"] == "unresolved"
    assert non_table["records"][0]["status"] == "unresolved"


def test_line_attestation_verifies_exact_crop_for_cell_and_row(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    quote = "3-3 management 83-95"
    source, graph = merged_cell_fixture(f"{quote} ENERGY 302-1")
    target = cell(graph, f"{quote} ENERGY 302-1")
    row = next(
        block
        for block in graph.blocks
        if block.kind == "table_row" and block.raw_text.startswith(quote)
    )
    refs = (ref_for(target, quote), ref_for(row, quote))
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(quote))

    receipt = verifier.attest_table_spans(graph, source, refs, tenant_id=TENANT)
    replay = verifier.attest_table_spans(graph, source, refs, tenant_id=TENANT)

    assert [record["status"] for record in receipt["records"]] == ["verified", "verified"]
    assert receipt == replay
    for record in receipt["records"]:
        assert record["method"] == "native_glyph_line_v2"
        assert record["line_policy_sha256"] == verifier.line_span_policy_sha256()
        assert record["line_attestation"]["native_text"] == quote
        assert record["line_attestation"]["rendered"]["text"] == quote
        assert record["line_attestation"]["comparison"] == "nfc_ignore_whitespace_v1"
        region, cell_box = record["line_attestation"]["region"], record["ref"]["bbox"]
        assert cell_box[0] <= region[0] < region[2] <= cell_box[2]
        assert cell_box[1] <= region[1] < region[3] <= cell_box[3]


def test_line_attestation_refuses_foreign_glyph_intrusions_and_split_lines(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    quote = "management"
    source, graph = merged_cell_fixture("3-3 management 83-95 ENERGY 302-1", overlay="X")
    target = next(
        block
        for block in graph.blocks
        if block.kind == "table_cell" and "management" in block.raw_text
    )
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(quote))
    foreign = verifier.attest_table_spans(
        graph, source, (ref_for(target, quote),), tenant_id=TENANT
    )

    split_quote = "three hundred"
    split_source, split_graph = merged_cell_fixture("three", second_line="hundred")
    split_target = cell(split_graph, "three hundred")
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(split_quote))
    split = verifier.attest_table_spans(
        split_graph, split_source, (ref_for(split_target, split_quote),), tenant_id=TENANT
    )

    assert foreign["records"][0]["status"] == "unresolved"
    assert foreign["records"][0]["line_attestation"]["reason"] == "foreign_glyph_in_region"
    assert split["records"][0]["status"] == "unresolved"
    assert split["records"][0]["line_attestation"]["reason"] == "quote_not_one_visual_line"


def test_line_attestation_refuses_close_distinct_baselines(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    source, graph = merged_cell_fixture("three", second_line="hundred")
    assert source.count(b"30 735 Td") == 1
    source = source.replace(b"30 735 Td", b"65 745 Td")
    graph = _replace_batch(source, graph, lambda block: block)
    target = cell(graph, "three hundred")
    full_box = verifier._selected_box(target)
    real_ocr = verifier._rendered_text

    def read(page, box, **kwargs):
        if tuple(box) == tuple(full_box):
            return dict(status="read", text="full cell mismatch")
        if sys.platform == "darwin":
            return real_ocr(page, box, **kwargs)
        return dict(status="read", text="three hundred")

    monkeypatch.setattr(verifier, "_rendered_text", read)
    record = verifier.attest_table_spans(
        graph, source, (ref_for(target, "three hundred"),), tenant_id=TENANT
    )["records"][0]

    assert record["status"] == "unresolved"
    assert record["line_attestation"]["reason"] == "quote_not_one_visual_line"


def test_line_attestation_ignores_only_ocr_whitespace(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    quote = "3-1 ~ 3-3"
    source, graph = merged_cell_fixture(f"{quote} ENERGY 302-1")
    target = cell(graph, f"{quote} ENERGY 302-1")
    ref = ref_for(target, quote)
    for rendered, expected in (
        ("3-1~ 3-3", "verified"),
        ("3-1\n\t~3-3", "verified"),
        ("3-1~ 3-4", "unresolved"),
        ("3-1~ 3-", "unresolved"),
        ("3-1~ 3-33", "unresolved"),
    ):
        monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(quote, crop_text=rendered))
        record = verifier.attest_table_spans(graph, source, (ref,), tenant_id=TENANT)["records"][0]
        assert record["status"] == expected
        assert record["method"] == "native_glyph_line_v2"
        assert record["line_attestation"]["comparison"] == "nfc_ignore_whitespace_v1"


def test_line_attestation_refuses_duplicate_cut_ocr_mismatch_and_outside_geometry(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    quote = "management"
    source, graph = merged_cell_fixture("management management")
    target = cell(graph, "management management")
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(quote))
    duplicate = verifier.attest_table_spans(
        graph, source, (ref_for(target, quote),), tenant_id=TENANT
    )

    number_source, number_graph = merged_cell_fixture("1234 ENERGY 302-1")
    number = cell(number_graph, "1234 ENERGY 302-1")
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr("234"))
    cut = verifier.attest_table_spans(
        number_graph, number_source, (ref_for(number, "234"),), tenant_id=TENANT
    )

    mismatch_source, mismatch_graph = merged_cell_fixture("management ENERGY 302-1")
    mismatch = cell(mismatch_graph, "management ENERGY 302-1")
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(quote, crop_text="managernent"))
    ocr_mismatch = verifier.attest_table_spans(
        mismatch_graph, mismatch_source, (ref_for(mismatch, quote),), tenant_id=TENANT
    )
    from pdfplumber import open as open_pdf

    with open_pdf(BytesIO(mismatch_source)) as document:
        reading = verifier._page_reading(mismatch_source, document, mismatch, {})
        cell_box = verifier._selected_box(mismatch)
        valid_region = verifier._line_region(
            mismatch_source, document.pages[0], reading, quote, cell_box
        )
        outside_region = verifier._line_region(
            mismatch_source,
            document.pages[0],
            reading,
            quote,
            (valid_region["region"][0] + 1, *cell_box[1:]),
        )

    outside_source, outside_graph = merged_cell_fixture("management ENERGY 302-1")
    outside_graph = _replace_batch(
        outside_source,
        outside_graph,
        lambda block: replace(block, source=replace(block.source, native_bbox=(38, 730, 420, 770)))
        if block.source.source_native_id == "C00"
        else block,
    )
    outside = cell(outside_graph, "management ENERGY 302-1")
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(quote))
    outside_receipt = verifier.attest_table_spans(
        outside_graph, outside_source, (ref_for(outside, quote),), tenant_id=TENANT
    )

    assert [
        item["records"][0]["status"] for item in (duplicate, cut, ocr_mismatch, outside_receipt)
    ] == ["unresolved"] * 4
    assert outside_region["reason"] == "line_region_outside_cell"


def test_structural_guard_failure_never_enters_line_attestation(monkeypatch):
    from proofops.adapters.local import table_span_source_verification as verifier

    quote = "management"
    source, graph = merged_cell_fixture("management ENERGY 302-1")
    graph = _replace_batch(
        source,
        graph,
        lambda block: replace(block, source=replace(block.source, native_bbox=(450, 690, 560, 780)))
        if block.source.source_native_id == "T"
        else block,
    )
    target = cell(graph, "management ENERGY 302-1")
    monkeypatch.setattr(verifier, "_rendered_text", _line_ocr(quote))

    record = verifier.attest_table_spans(
        graph, source, (ref_for(target, quote),), tenant_id=TENANT
    )["records"][0]

    assert record["status"] == "unresolved"
    assert "method" not in record


def test_existing_full_cell_and_paragraph_receipts_keep_head_hashes(monkeypatch):
    from proofops.adapters.local import claim_source_verification, table_span_source_verification

    source, graph = fixture()
    graph = stable_graph(graph)
    monkeypatch.setattr(table_span_source_verification, "_rendered_text", crop_ocr)
    full_cell = cell(graph, "2024")
    table_receipt = table_span_source_verification.attest_table_spans(
        graph, source, (ref_for(full_cell, "2024"),), tenant_id=TENANT
    )
    paragraph_source = pdf()
    paragraph_batch = candidate(
        "span",
        [("P", "paragraph", "Page 1 emissions 1234 tCO2e", (70, 710, 300, 740), ())],
    )
    paragraph_batch = replace(paragraph_batch, source_sha256=sha256(paragraph_source).hexdigest())
    paragraph_graph = stable_graph(fuse_candidates((paragraph_batch,), tenant_id=TENANT))
    paragraph_block = paragraph_graph.blocks[0]
    quote = "emissions 1234 tCO2e"
    paragraph_ref = replace(
        paragraph_block.source_ref(),
        char_start=7,
        char_end=7 + len(quote),
        quote=quote,
    )
    monkeypatch.setattr(
        claim_source_verification,
        "_rendered_text",
        lambda *a, **k: dict(status="read", text="Page I emissions 1234 tCO2e"),
    )
    paragraph_receipt = claim_source_verification.attest_claim_spans(
        paragraph_graph, paragraph_source, (paragraph_ref,), tenant_id=TENANT
    )

    assert (
        canonical_hash(table_receipt)
        == "cf0ca7a2df1c07350c2b8ee239a1ea432e812200d868da80b862862086bc2b7b"
    )
    assert (
        canonical_hash(paragraph_receipt)
        == "0492382c327303cd622d46c398095a778c7c4648c98243d9f0b00fb51eaf5636"
    )
