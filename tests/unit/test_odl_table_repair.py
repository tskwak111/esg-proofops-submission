"""Focused regression for ODL stacked header/value repair (LG p32 actuals)."""

import copy

from proofops.adapters.parsing.odl_table_repair import repair_stacked_headers


def _lg_p32_table():
    # Actual LG p32 table 123 first row + second row, copied from frozen
    # lghh-tables-v2 source.json (ODL bottom-left points).
    return {
        "type": "table",
        "id": 123,
        "page number": 32,
        "bounding box": [226.701, 17.271, 573.945, 95.722],
        "number of rows": 2,
        "number of columns": 5,
        "rows": [
            {
                "type": "table row",
                "row number": 1,
                "id": 124,
                "cells": [
                    {
                        "type": "table cell",
                        "id": 125,
                        "page number": 32,
                        "bounding box": [226.826, 68.466, 369.726, 94.722],
                        "row number": 1,
                        "column number": 1,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "heading",
                                "id": 126,
                                "page number": 32,
                                "bounding box": [291.756, 83.588, 304.798, 93.818],
                                "content": "구분",
                            },
                            {
                                "type": "paragraph",
                                "id": 127,
                                "page number": 32,
                                "bounding box": [229.535, 70.32, 284.91, 79.805],
                                "content": "비재생에너지 사용량",
                            },
                        ],
                    },
                    {
                        "type": "table cell",
                        "id": 128,
                        "page number": 32,
                        "bounding box": [369.726, 17.959, 420.999, 94.722],
                        "row number": 1,
                        "column number": 2,
                        "row span": 5,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "heading",
                                "id": 129,
                                "page number": 32,
                                "bounding box": [388.918, 83.588, 401.803, 93.818],
                                "content": "단위",
                            },
                            {
                                "type": "paragraph",
                                "id": 130,
                                "page number": 32,
                                "bounding box": [391.809, 44.833, 398.911, 54.122],
                                "content": "TJ",
                            },
                        ],
                    },
                    {
                        "type": "table cell",
                        "id": 131,
                        "page number": 32,
                        "bounding box": [420.999, 68.466, 471.898, 94.722],
                        "row number": 1,
                        "column number": 3,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "heading",
                                "id": 132,
                                "page number": 32,
                                "bounding box": [437.488, 83.588, 455.28, 93.818],
                                "content": "2023",
                            },
                            {
                                "type": "paragraph",
                                "id": 133,
                                "page number": 32,
                                "bounding box": [450.987, 70.369, 469.063, 79.658],
                                "content": "2,658",
                            },
                        ],
                    },
                    {
                        "type": "table cell",
                        "id": 134,
                        "page number": 32,
                        "bounding box": [471.898, 68.466, 522.921, 94.722],
                        "row number": 1,
                        "column number": 4,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "heading",
                                "id": 135,
                                "page number": 32,
                                "bounding box": [488.473, 83.588, 506.347, 93.818],
                                "content": "2024",
                            },
                            {
                                "type": "paragraph",
                                "id": 136,
                                "page number": 32,
                                "bounding box": [501.744, 70.369, 520.082, 79.658],
                                "content": "2,496",
                            },
                        ],
                    },
                    {
                        "type": "table cell",
                        "id": 137,
                        "page number": 32,
                        "bounding box": [522.921, 68.466, 573.82, 94.722],
                        "row number": 1,
                        "column number": 5,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "heading",
                                "id": 138,
                                "page number": 32,
                                "bounding box": [539.616, 83.588, 557.25, 93.818],
                                "content": "2025",
                            },
                            {
                                "type": "paragraph",
                                "id": 139,
                                "page number": 32,
                                "bounding box": [553.04, 70.369, 571.105, 79.658],
                                "content": "2,450",
                            },
                        ],
                    },
                ],
            },
            {
                "type": "table row",
                "row number": 2,
                "id": 140,
                "cells": [
                    {
                        "type": "table cell",
                        "id": 141,
                        "page number": 32,
                        "bounding box": [226.826, 55.698, 369.726, 68.466],
                        "row number": 2,
                        "column number": 1,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "paragraph",
                                "id": 142,
                                "page number": 32,
                                "bounding box": [229.528, 57.552, 261.087, 67.037],
                                "content": "전기 사용량",
                            }
                        ],
                    },
                ],
            },
        ],
    }


def _lg_p32_words():
    # Real native pdfplumber words converted to ODL bottom-left points
    # (H=595.276, rotation-0 crop==media). Subset covering table 123 band.
    raw = [
        ("구분", 291.7557, 83.5878, 304.7982, 91.0878),
        ("단위", 388.9182, 83.5878, 401.8032, 91.0878),
        ("2023", 437.4882, 83.5878, 455.2797, 91.0878),
        ("2024", 488.4732, 83.5878, 506.3472, 91.0878),
        ("2025", 539.6157, 83.5878, 557.2497, 91.0878),
        ("비재생에너지", 229.5353, 70.3204, 265.257, 77.3204),
        ("사용량", 266.657, 70.3204, 284.9095, 77.3204),
        ("2,658", 450.9873, 70.3694, 469.0627, 77.3694),
        ("2,496", 501.7443, 70.3694, 520.0822, 77.3694),
        ("2,450", 553.0403, 70.3694, 571.1045, 77.3694),
        ("TJ", 391.8093, 44.8334, 398.9108, 51.8334),
    ]
    return {32: [{"bbox": [x0, y0, x1, y1], "text": t} for t, x0, y0, x1, y1 in raw]}


def _hmm_p36_table():
    # Actual HMM p36 table 62 shape: single-kid cells, no stacking.
    return {
        "type": "table",
        "id": 62,
        "page number": 36,
        "bounding box": [51.024, 96.853, 535.748, 301.323],
        "number of rows": 18,
        "number of columns": 6,
        "rows": [
            {
                "type": "table row",
                "row number": 1,
                "id": 63,
                "cells": [
                    {
                        "type": "table cell",
                        "id": 64,
                        "page number": 36,
                        "bounding box": [51.274, 278.396, 375.732, 300.948],
                        "row number": 1,
                        "column number": 1,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "paragraph",
                                "id": 65,
                                "page number": 36,
                                "bounding box": [53.858, 284.338, 74.313, 294.681],
                                "content": "Scope",
                            }
                        ],
                    },
                    {
                        "type": "table cell",
                        "id": 66,
                        "page number": 36,
                        "bounding box": [375.732, 289.734, 535.498, 300.948],
                        "row number": 1,
                        "column number": 4,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "paragraph",
                                "id": 67,
                                "page number": 36,
                                "bounding box": [420.381, 290.007, 491.099, 300.351],
                                "content": "온실가스 배출량 (tCO2e)",
                            }
                        ],
                    },
                ],
            },
            {
                "type": "table row",
                "row number": 2,
                "id": 68,
                "cells": [
                    {
                        "type": "table cell",
                        "id": 69,
                        "page number": 36,
                        "bounding box": [375.732, 278.396, 429.071, 289.734],
                        "row number": 2,
                        "column number": 4,
                        "row span": 1,
                        "column span": 1,
                        "kids": [
                            {
                                "type": "paragraph",
                                "id": 70,
                                "page number": 36,
                                "bounding box": [390.407, 279.443, 414.389, 289.096],
                                "content": "지배기업",
                            }
                        ],
                    },
                ],
            },
        ],
    }


def test_lg_p32_stacked_header_splits_with_real_gap():
    raw = {"kids": [_lg_p32_table()]}
    snapshot = copy.deepcopy(raw)
    new_raw, receipts = repair_stacked_headers(raw, _lg_p32_words())
    # Input not mutated.
    assert raw == snapshot
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["table_id"] == 123
    assert receipt["page"] == 32
    gap_low, gap_high = receipt["gap"]
    assert gap_high - gap_low >= 0.5
    # Header bottom ~83.588, first value top ~79.805 from frozen ODL geometry.
    assert 79.0 <= gap_low <= 80.5
    assert 83.0 <= gap_high <= 84.5

    table = next(k for k in new_raw["kids"] if k.get("id") == 123)
    assert table["number of rows"] == 3
    rows = sorted(table["rows"], key=lambda r: r["row number"])
    assert [r["row number"] for r in rows] == [1, 2, 3]
    header, data, following = rows
    header_text = " ".join(k.get("content", "") for c in header["cells"] for k in c["kids"])
    for token in ("구분", "단위", "2023", "2024", "2025"):
        assert token in header_text
    assert "비재생에너지" not in header_text
    assert "2,658" not in header_text
    data_text = " ".join(k.get("content", "") for c in data["cells"] for k in c["kids"])
    assert "비재생에너지 사용량" in data_text
    assert "2,658" in data_text and "2,496" in data_text and "2,450" in data_text
    # No invented text: every repaired kid content comes from the original kids.
    orig_texts = {
        k.get("content", "")
        for r in snapshot["kids"][0]["rows"]
        for c in r["cells"]
        for k in c["kids"]
    }
    for r in table["rows"][:2]:
        for c in r["cells"]:
            for k in c["kids"]:
                assert k.get("content", "") in orig_texts
    # Spanning TJ preserved exactly once with original cell id and kid bbox.
    tj_cells = [c for c in data["cells"] if any(k.get("content") == "TJ" for k in c["kids"])]
    assert len(tj_cells) == 1
    assert tj_cells[0]["id"] == 128
    assert tj_cells[0]["row span"] == 5
    assert tj_cells[0]["bounding box"] == [391.809, 44.833, 398.911, 54.122]
    # Resulting header/data bboxes are non-overlapping.
    for entry in receipt["cell_map"]:
        assert entry["header_bbox"][1] >= entry["data_bbox"][3] + 0.5 - 0.01
    assert header["bounding box"][1] >= data["bounding box"][3] + 0.5 - 0.01
    # Old row 2 shifted to row 3 (lineage retained, no data loss).
    assert following["id"] == 140
    assert following["row number"] == 3


def test_ambiguous_straddling_word_rejects_repair():
    raw = {"kids": [_lg_p32_table()]}
    words = _lg_p32_words()
    words[32].append({"bbox": [230.0, 78.0, 280.0, 85.0], "text": "STRADDLE"})
    new_raw, receipts = repair_stacked_headers(raw, words)
    assert receipts == []
    assert new_raw == raw


def test_hmm_p36_unchanged():
    raw = {"kids": [_hmm_p36_table()]}
    words = {
        36: [
            {"bbox": [53.858, 284.338, 74.313, 294.681], "text": "Scope"},
            {"bbox": [420.381, 290.007, 491.099, 300.351], "text": "온실가스 배출량 (tCO2e)"},
            {"bbox": [390.407, 279.443, 414.389, 289.096], "text": "지배기업"},
        ]
    }
    new_raw, receipts = repair_stacked_headers(raw, words)
    assert receipts == []
    assert new_raw == raw


def test_incomplete_first_row_and_wrong_native_text_leave_input_unchanged():
    for mutation in ("partial", "wrong_text", "edge_word", "unknown_child"):
        raw = {"kids": [_lg_p32_table()]}
        words = _lg_p32_words()
        cells = raw["kids"][0]["rows"][0]["cells"]
        if mutation == "partial":
            cells[-1]["kids"] = cells[-1]["kids"][1:]
        elif mutation == "wrong_text":
            cells[-1]["kids"][1]["content"] = "2,451"
        elif mutation == "edge_word":
            words[32].append({"text": "X", "bbox": [226, 72, 230, 75]})
        else:
            cells[-1]["kids"].append({"type": "unknown", "id": 999, "content": "unresolved"})
        before = copy.deepcopy(raw)
        result, receipts = repair_stacked_headers(raw, words)
        assert not receipts, mutation
        assert result == before, mutation
        assert raw == before, mutation


def test_v2_locates_missing_rows_from_existing_cells_without_changing_v1():
    raw = {"kids": [_lg_p32_table()]}
    # A following row has no bbox in the actual ODL schema.
    row = raw["kids"][0]["rows"][1]
    row["cells"] = [copy.deepcopy(raw["kids"][0]["rows"][0]["cells"][0])]
    row["cells"][0]["row number"] = 2
    row["cells"][0]["bounding box"] = [229, 55, 285, 67]
    row.pop("bounding box", None)
    baseline, _ = repair_stacked_headers(raw, _lg_p32_words())
    assert "bounding box" not in baseline["kids"][0]["rows"][2]
    repaired, receipts = repair_stacked_headers(raw, _lg_p32_words(), locate_rows=True)
    assert repaired["kids"][0]["rows"][2]["bounding box"] == [229, 55, 285, 67]
    assert receipts[0]["located_rows"][0]["row_id"] == row["id"]
    assert "bounding box" not in raw["kids"][0]["rows"][1]
