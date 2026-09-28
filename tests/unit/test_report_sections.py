import pytest

from evaluation.report_sections import build_map


def test_section_scopes_keep_full_data_appendix_and_uncertainty():
    anchors = [
        dict(page=2, title="Environment", path=["Environment"]),
        dict(page=5, title="Social", path=["Social"]),
        dict(page=7, title="ESG DATA", path=["ESG DATA"]),
        dict(page=8, title="Social", path=["ESG DATA", "Social"]),
        dict(page=9, title="Appendix", path=["Appendix"]),
    ]
    result = build_map(10, anchors)
    assert result["claim_candidate_pages"] == [2, 3, 4]
    assert result["evidence_candidate_pages"] == [2, 3, 4, 7, 8, 9, 10]
    assert result["unknown_pages"] == [1]
    assert result["other_candidate_pages"] == [5, 6]
    assert result["status"] == "candidate_only"
    assert build_map(3, [])["unknown_pages"] == [1, 2, 3]
    conflict = build_map(
        3,
        [
            dict(page=2, title="Environment", path=["Environment"]),
            dict(page=2, title="Social", path=["Social"]),
        ],
    )
    assert conflict["claim_candidate_pages"] == []
    assert conflict["conflict_pages"] == [2, 3]


def test_alternative_chapter_titles_and_real_pdf_destinations(tmp_path):
    from hashlib import sha256

    from pypdf import PdfWriter

    from evaluation.report_sections import inspect

    pdf = tmp_path / "report.pdf"
    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(600, 800)
    for title, page in [("Planet", 1), ("People", 3), ("Facts & Figures", 4), ("Appendix", 5)]:
        writer.add_named_destination(title, page)
    writer.write(pdf)
    result = inspect(pdf)
    assert result["claim_candidate_pages"] == [2, 3]
    assert result["evidence_candidate_pages"] == [2, 3, 5, 6]
    assert result["source_sha256"] == sha256(pdf.read_bytes()).hexdigest()
    assert result["unknown_pages"] == [1]

    writer.add_named_destination("Unfamiliar chapter", 2)
    writer.write(pdf)
    result = inspect(pdf)
    assert result["claim_candidate_pages"] == [2]
    assert 3 in result["unknown_pages"]


@pytest.mark.parametrize("layout", ["plain", "multicolumn", "rotated_marker"])
def test_small_toc_link_text_resolves_opaque_destinations(tmp_path, layout):
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        TextStringObject,
    )

    from evaluation.report_sections import inspect

    writer = PdfWriter()
    for _ in range(4):
        writer.add_blank_page(600, 800)
    page = writer.pages[0]
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    marker = (
        b"BT /F1 12 Tf 0 1 -1 0 30 750 Tm (CONTENTS) Tj ET"
        if layout == "rotated_marker"
        else b"BT /F1 12 Tf 30 750 Td (CONTENTS) Tj ET"
    )
    if layout == "multicolumn":
        marker = marker.replace(b"12 Tf", b"22 Tf")
        marker += b" BT /F1 12 Tf 250 750 Td (Other column) Tj ET"
    stream.set_data(
        marker + b" BT /F1 12 Tf 30 720 Td (Environment) Tj "
        b"0 -30 Td (Social) Tj 0 -30 Td (Appendix) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    annotations = ArrayObject()
    for index, y in enumerate((720, 690, 660), 1):
        name = f"report.indd:opaque:{index}"
        writer.add_named_destination(name, index)
        annotations.append(
            writer._add_object(
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Link"),
                        NameObject("/Dest"): TextStringObject(name),
                        NameObject("/Rect"): ArrayObject(
                            [FloatObject(v) for v in (28, y - 2, 130, y + 15)]
                        ),
                    }
                )
            )
        )
    page[NameObject("/Annots")] = writer._add_object(annotations)
    pdf = tmp_path / "small-toc.pdf"
    writer.write(pdf)
    result = inspect(pdf)
    assert result["method"] == "toc_links"
    assert result["claim_candidate_pages"] == [2]
    assert result["evidence_candidate_pages"] == [2, 4]
    anchor = result["sections"][1]["anchors"][0]
    assert anchor["toc_page"] == 1 and anchor["title"] == "Environment"

    annotations[0].get_object()[NameObject("/Dest")] = ArrayObject(
        [writer.pages[1].indirect_reference, NameObject("/Fit")]
    )
    writer.write(pdf)
    assert inspect(pdf)["claim_candidate_pages"] == [2]

    # An evidence-only TOC still contributes; absence of an E entry is not evidence absence.
    page[NameObject("/Annots")] = ArrayObject([annotations[-1]])
    writer.write(pdf)
    evidence_only = inspect(pdf)
    assert evidence_only["evidence_candidate_pages"] == [4]
    assert evidence_only["claim_candidate_pages"] == []
    annotations[-1].get_object()[NameObject("/A")] = DictionaryObject(
        {
            NameObject("/S"): NameObject("/URI"),
            NameObject("/URI"): TextStringObject("https://example.invalid/report.pdf"),
        }
    )
    writer.write(pdf)
    assert inspect(pdf)["evidence_candidate_pages"] == []


def test_english_topic_and_glossary_routing():
    from evaluation.report_sections import role

    assert role(["Climate Action"]) == "e_narrative"
    assert role(["Glossary"]) == "appendix"


def test_data_sheet_titles_are_evidence_containers():
    from evaluation.report_sections import role

    assert role(["ESG Fact Sheet"]) == "esg_data"
    assert role(["지속가능경영 Data"]) == "esg_data"


@pytest.mark.parametrize("early_data", [False, True])
def test_ghg_energy_topic_routing_and_scope(tmp_path, early_data):
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject,
        DecodedStreamObject,
        DictionaryObject,
        FloatObject,
        NameObject,
        TextStringObject,
    )

    from evaluation.report_sections import inspect, role

    assert role(["온실가스 및 에너지 관리"]) == "e_narrative"
    assert role(["ESG DATA", "환경"]) == "esg_data"
    writer = PdfWriter()
    for _ in range(10):
        writer.add_blank_page(600, 800)
    writer.add_named_destination("Environment", 4)
    writer.add_named_destination("Appendix", 6)
    if early_data:
        writer.add_named_destination("ESG DATA", 1)
        writer.add_named_destination("Overview", 2)
    page = writer.pages[0]
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 30 750 Td (CONTENTS) Tj ET "
        b"BT /F1 12 Tf 30 720 Td (Topic 3. GHG) Tj "
        b"0 -30 Td (ESG DATA) Tj 0 -30 Td (Environment) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    annotations = ArrayObject()
    # GHG narrative p4 is after the early data interval p2 and before E chapter p5.
    ghg_page = 4 if early_data else 2
    for number, target_page in enumerate((ghg_page, 9, 10)):
        name = f"report.indd:section:{number}"
        writer.add_named_destination(name, target_page - 1)
        annotations.append(
            writer._add_object(
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Annot"),
                        NameObject("/Subtype"): NameObject("/Link"),
                        NameObject("/Dest"): TextStringObject(name),
                        NameObject("/Rect"): ArrayObject(
                            [
                                FloatObject(v)
                                for v in (28, 718 - number * 30, 130, 735 - number * 30)
                            ]
                        ),
                    }
                )
            )
        )
    page[NameObject("/Annots")] = writer._add_object(annotations)
    pdf = tmp_path / "scattered.pdf"
    writer.write(pdf)
    result = inspect(pdf)
    assert result["method"] == "toc_links"
    assert ghg_page in result["claim_candidate_pages"] and 5 in result["claim_candidate_pages"]
    assert 10 not in result["claim_candidate_pages"]
    assert {7, 9, 10} <= set(result["evidence_candidate_pages"])
    if early_data:
        assert 2 not in result["claim_candidate_pages"] and 2 in result["evidence_candidate_pages"]


@pytest.mark.parametrize("title", ["자원순환36", "폐기물 관리", "Circular economy", "Recycling"])
def test_circularity_boundary_stays_environmental_but_data_child_stays_evidence(title):
    anchors = [
        dict(page=1, path=["Environment"]),
        dict(page=2, path=[title]),
        dict(page=4, path=["Social"]),
        dict(page=5, path=["ESG DATA", title]),
    ]
    result = build_map(6, anchors)
    assert result["claim_candidate_pages"] == [1, 2, 3]
    assert result["evidence_candidate_pages"] == [1, 2, 3, 5, 6]
    assert build_map(2, [dict(page=1, path=["Sustainable growth"])])["unknown_pages"] == [1, 2]


def test_unclassified_large_heading_does_not_override_explicit_toc_role():
    anchors = [
        dict(
            page=2,
            title="Green in Action",
            path=["Green in Action"],
            method="large_heading_fallback",
        ),
        dict(page=2, title="탄소중립", path=["탄소중립"], method="toc_links"),
    ]
    mapped = build_map(4, anchors)
    assert mapped["claim_candidate_pages"] == [2, 3, 4]
    assert mapped["sections"][1]["anchors"] == anchors
    # Explicit opposed meanings remain a conflict; fallback-only unknown stays unknown.
    anchors[0]["path"] = ["Social"]
    assert build_map(4, anchors)["conflict_pages"] == [2, 3, 4]
    anchors[0]["path"] = ["Environmental Social"]
    assert build_map(4, anchors)["conflict_pages"] == [2, 3, 4]
    anchors[0]["path"] = ["Green in Action"]
    assert build_map(4, anchors[:1])["unknown_pages"] == [1, 2, 3, 4]


def test_orion_actual_linkless_toc_resolves_e_narrative_and_evidence():
    from pathlib import Path

    from evaluation.report_sections import inspect

    orion_pdf = Path("tests/fixtures/source-documents/orion.pdf")
    if not orion_pdf.exists():
        pytest.skip("tests/fixtures/source-documents/orion.pdf not present")

    res = inspect(orion_pdf)
    assert res["method"] == "toc_text_fallback"
    assert res["claim_candidate_pages"] == list(range(68, 83))
    evidence = set(res["evidence_candidate_pages"])
    assert set(range(68, 83)) <= evidence
    assert {91, 92} <= evidence
    assert set(range(93, 108)) <= evidence
    assert set(range(108, 117)) <= evidence

    # Relative tiers stop E narrative at Social 83
    sec_roles = {
        s["start_page"]: (s["role"], [a["title"] for a in s["anchors"]]) for s in res["sections"]
    }
    assert sec_roles[68][0] == "e_narrative" and "친환경 경영" in sec_roles[68][1]
    assert sec_roles[73][0] == "e_narrative" and "기후변화 대응" in sec_roles[73][1]
    assert sec_roles[80][0] == "e_narrative" and "환경영향 저감" in sec_roles[80][1]
    assert sec_roles[82][0] == "e_narrative" and "생물다양성" in sec_roles[82][1]
    assert sec_roles[83][0] == "other" and "사회공헌" in sec_roles[83][1]
    assert sec_roles[91][0] == "appendix" and "대외평가·수상" in sec_roles[91][1]
    assert sec_roles[93][0] == "esg_data" and "ESG DATA" in sec_roles[93][1]
    assert sec_roles[108][0] == "appendix" and "GRI Standards Index" in sec_roles[108][1]
    assert res["issues"] == []


def test_linkless_toc_wrong_offset_remains_unknown(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from evaluation.report_sections import inspect

    def make_font(writer):
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        return DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})

    w = PdfWriter()
    for _ in range(5):
        w.add_blank_page(600, 800)
    for p in w.pages:
        p[NameObject("/Resources")] = make_font(w)

    # Page 1: TOC claims Environment is at page 3, Social at page 5
    s1 = DecodedStreamObject()
    s1.set_data(
        b"BT /F1 15 Tf 40 700 Td (Contents) Tj ET "
        b"BT /F1 10 Tf 40 650 Td (Environment) Tj 300 0 Td (3) Tj ET "
        b"0 -30 Td (Social) Tj 300 0 Td (5) Tj ET"
    )
    w.pages[0][NameObject("/Contents")] = w._add_object(s1)

    # Page 3 has mismatched heading 'Different Topic' (wrong offset)
    s3 = DecodedStreamObject()
    s3.set_data(b"BT /F1 16 Tf 40 650 Td (Different Topic) Tj ET")
    w.pages[2][NameObject("/Contents")] = w._add_object(s3)

    # Page 5 has valid heading 'Social'
    s5 = DecodedStreamObject()
    s5.set_data(b"BT /F1 16 Tf 40 650 Td (Social) Tj ET")
    w.pages[4][NameObject("/Contents")] = w._add_object(s5)

    pdf = tmp_path / "wrong_offset.pdf"
    w.write(pdf)

    res = inspect(pdf)
    assert res["method"] == "toc_text_fallback"
    assert res["claim_candidate_pages"] == []
    assert any(
        issue["kind"] == "toc_text_unverified_destination" and issue["printed_page"] == 3
        for issue in res["issues"]
    )
    # Destination page 3 remains unknown
    assert 3 in res["unknown_pages"]


def test_linkless_toc_repeated_nav_header_negative(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from evaluation.report_sections import inspect

    def make_font(writer):
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        return DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})

    w = PdfWriter()
    for _ in range(4):
        w.add_blank_page(600, 800)
    for p in w.pages:
        p[NameObject("/Resources")] = make_font(w)

    # Running header at top of every page containing nav tokens
    nav = b"BT /F1 8 Tf 40 760 Td (Company Report Environment Social Appendix) Tj ET "

    # Page 1: TOC with repeated nav
    s1 = DecodedStreamObject()
    s1.set_data(
        nav + b"BT /F1 15 Tf 40 700 Td (Contents) Tj ET "
        b"BT /F1 10 Tf 40 650 Td (Social) Tj 300 0 Td (3) Tj ET"
    )
    w.pages[0][NameObject("/Contents")] = w._add_object(s1)

    # Page 2: Repeated nav, body text only
    s2 = DecodedStreamObject()
    s2.set_data(nav + b"BT /F1 10 Tf 40 650 Td (Regular body text without heading) Tj ET")
    w.pages[1][NameObject("/Contents")] = w._add_object(s2)

    # Page 3: Repeated nav, true heading Social 16pt
    s3 = DecodedStreamObject()
    s3.set_data(nav + b"BT /F1 16 Tf 40 650 Td (Social) Tj ET")
    w.pages[2][NameObject("/Contents")] = w._add_object(s3)

    pdf = tmp_path / "repeated_nav.pdf"
    w.write(pdf)

    res = inspect(pdf)
    assert res["method"] == "toc_text_fallback"
    # Repeated nav does NOT cause false Environment claim candidates
    assert res["claim_candidate_pages"] == []
    assert res["other_candidate_pages"] == [3, 4]
    assert 1 in res["unknown_pages"] and 2 in res["unknown_pages"]
