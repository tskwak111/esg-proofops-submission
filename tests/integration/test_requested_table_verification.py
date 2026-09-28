"""Explicit late-row verification keeps the old cap and source guards."""

from copy import deepcopy

import pytest
from proofops.adapters.local import selected_cell_table_verification as legacy

from tests.acceptance.test_parsing import TENANT
from tests.integration.test_selected_cell_table_verification import grid_pdf, table_graph


def test_requested_late_row_is_checked_without_promoting_earlier_values(monkeypatch):
    from proofops.adapters.local import requested_table_verification as requested

    words = [(200, 760, "2025")]
    cells = {(0, 0): "2025"}
    for row in range(1, 27):
        words.append((200, 760 - 20 * row, str(1000 + row)))
        cells[row, 0] = str(1000 + row)
    source = grid_pdf(words)
    graph = table_graph(source, cells)
    target = next(b.source_id for b in graph.blocks if b.raw_text == "1026")
    monkeypatch.setattr(
        legacy,
        "_rendered_cell",
        lambda page, box: {"status": "read", "text": page.crop(box).extract_text() or ""},
    )
    old = legacy.attest_tables(graph, source, tenant_id=TENANT)
    assert target not in {s["value_source_id"] for r in old["records"] for s in r["selections"]}
    receipt = requested.attest_tables(graph, source, [target], tenant_id=TENANT)
    assert receipt["requested_source_ids"] == [target]
    promoted = requested.replay_tables(receipt, graph, source, [target], tenant_id=TENANT)
    assert {b.source_id for b in promoted.blocks if b.quality == "verified"} == {target}
    assert legacy.attest_tables(graph, source, tenant_id=TENANT) == old
    forged = deepcopy(receipt)
    forged["requested_source_ids"] = []
    with pytest.raises(ValueError, match="mismatch"):
        requested.replay_tables(forged, graph, source, [target], tenant_id=TENANT)
    for ids in ([], [target] * 2, [target] * 25, ["unknown"], target):
        with pytest.raises(ValueError):
            requested.attest_tables(graph, source, ids, tenant_id=TENANT)
    with pytest.raises(ValueError, match="source mismatch"):
        requested.attest_tables(graph, source + b"changed", [target], tenant_id=TENANT)

    numeric_ids = [b.source_id for b in graph.blocks if b.kind == "table_cell"]
    with pytest.raises(ValueError, match="limit"):
        requested.attest_tables(graph, source, numeric_ids[:25], tenant_id=TENANT)
    with pytest.raises(ValueError):
        requested.attest_tables(
            graph, source, [target], tenant_id="22222222-2222-4222-8222-222222222222"
        )
    # Same pinned source and request, but a changed rendered digit must stay held.
    requested._attest_json.cache_clear()
    monkeypatch.setattr(
        legacy,
        "_rendered_cell",
        lambda page, box: {
            "status": "read",
            "text": (page.crop(box).extract_text() or "").replace("1026", "1027"),
        },
    )
    mismatch = requested.attest_tables(graph, source, [target], tenant_id=TENANT)
    assert mismatch["records"][0]["reason"] == "rendered_cell_text_mismatch"
    held = requested.replay_tables(mismatch, graph, source, [target], tenant_id=TENANT)
    assert not any(b.quality == "verified" for b in held.blocks)
