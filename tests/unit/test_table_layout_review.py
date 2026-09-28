"""Bounded real-layout regression; these development PDFs are not independent gold."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from evaluation.table_layout_review import review_layout

ROOT = Path(__file__).resolve().parents[2]
SOURCE_DOCUMENTS = ROOT / "tests/fixtures/source-documents"
LAYOUTS = ROOT / "tests/fixtures/pipeline/reviewed-table-layout"


# The exact customer-report regression remains runnable locally; PDFs are private.
@pytest.mark.skipif(
    not all((SOURCE_DOCUMENTS / f"{name}.pdf").exists() for name in ("lotte", "kia")),
    reason="customer PDFs are local-only; run with the private corpus to verify these layouts",
)
def test_two_reviewed_layouts_and_source_boundary_holds():
    results = {}
    for name in ("lotte", "kia"):
        source = SOURCE_DOCUMENTS / f"{name}.pdf"
        spec = json.loads((LAYOUTS / f"{name}-layout.json").read_text())
        result = review_layout(source, spec)
        results[name] = result
        assert len(result["candidates"]) == 30
        assert all(c["binding_status"] == "complete_candidate" for c in result["candidates"])
        assert all(not c["verified"] for c in result["candidates"])
        assert not result["eligible_for_scoring"]
        assert result["native_verification"] == "not_run"
        latest = max(c["year"] for c in result["candidates"])
        expected = (
            [
                "1840806",
                "53727",
                "1355069",
                "342893",
                "2177",
                "3594672",
                "3588733",
                "2986",
                "2954",
                "3594672",
            ]
            if name == "lotte"
            else [
                "1178.5",
                "386.3",
                "238.6",
                "147.7",
                "792.2",
                "471.2",
                "321.0",
                "0.43",
                "0.55",
                "0.32",
            ]
        )
        assert [c["value_decimal"] for c in result["candidates"] if c["year"] == latest] == expected
        wrong = deepcopy(spec)
        wrong["source_sha256"] = "0" * 64
        with pytest.raises(ValueError, match="source mismatch"):
            review_layout(source, wrong)
        if name == "lotte":
            cut = deepcopy(spec)
            cut["x"][3] = 450  # cuts through the unit, never silently accepted
            held = review_layout(source, cut)
            assert all(c["binding_status"] == "held" for c in held["candidates"])
    lotte = results["lotte"]
    assert lotte["cells"]["r1c0"]["row_span"] == 6
    assert lotte["cells"]["r7c0"]["row_span"] == 4
    assert "절사값" in lotte["cells"]["r1c6"]["raw_text"]
    assert lotte["cells"]["r8c1"]["raw_text"] == "N2O"
    kia = results["kia"]
    children = [c for c in kia["candidates"] if c["row"] in (3, 6) and c["year"] == "2024"]
    assert [c["metric_raw"] for c in children] == ["직접 배출량(Scope 1)", "간접 배출량(Scope 2)"]
    assert all(c["row_labels"] == ["국내"] for c in children)
    assert "2" in kia["cells"]["r3c1"]["raw_text"]
    assert all(c["unit_canonical"] is None for c in kia["candidates"])
    assert "Category8" in kia["context"][0]["raw_text"]
