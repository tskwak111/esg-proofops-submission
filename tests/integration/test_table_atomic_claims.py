from copy import deepcopy

import pytest
from proofops.application.ingest.graph_fusion import fuse_candidates

from tests.acceptance.test_parsing import FOREIGN, TENANT
from tests.acceptance.test_tables import table


def case():
    graph = fuse_candidates(
        (
            table(
                [
                    ["과제", "달성 현황"],
                    ["태양광 확대", "달성 • 춘천 6MW PPA 개시 (진행 중) • 사옥 1MW PPA 추가 개시"],
                    ["재활용 확대", "수거 거점 연동 완료"],
                ]
            ),
        ),
        tenant_id=TENANT,
    )
    ids = {b.candidates[0].source.source_native_id: b.source_id for b in graph.blocks}
    return graph, ids


def test_atomic_rows_split_calls_and_preserve_unassigned_status_and_source_spans():
    from evaluation.table_atomic_claims import prepare, validate

    graph, ids = case()
    packets = prepare(graph, [ids["r1c1"], ids["r2c1"]], tenant_id=TENANT)
    assert len(packets) == 2
    packet = packets[0]
    target = packet["untrusted_document_data"]["target_ids"][0]
    result = validate(
        {
            "cells": [
                {
                    "cell_id": target,
                    "claims": [
                        "춘천 6MW PPA 개시",
                        "사옥 1MW PPA 추가 개시",
                    ],
                }
            ]
        },
        packet,
        graph,
        tenant_id=TENANT,
    )
    assert len(result["claims"]) == 2
    assert all(c["source_id"] == ids["r1c1"] for c in result["claims"])
    assert all(c["source_ref"]["verification_state"] == "candidate" for c in result["claims"])
    assert all(not c["eligible_for_scoring"] for c in result["claims"])
    assert any("진행 중" in s["quote"] for s in result["coverage"][0]["unreturned_spans"])
    assert result["coverage"][0]["state"] == "partial"
    assert result["claims"][0]["source_ref"]["quote"] == "춘천 6MW PPA 개시"


@pytest.mark.parametrize(
    "bad", ["rewrite", "duplicate", "context", "omit", "grade", "tamper", "tenant"]
)
def test_atomic_rejects_untrusted_output_or_packet(bad):
    from evaluation.table_atomic_claims import prepare, validate

    graph, ids = case()
    packet = prepare(graph, [ids["r1c1"]], tenant_id=TENANT)[0]
    target = packet["untrusted_document_data"]["target_ids"][0]
    payload = {"cells": [{"cell_id": target, "claims": ["춘천 6MW PPA 개시"]}]}
    if bad == "rewrite":
        payload["cells"][0]["claims"] = ["춘천 8MW PPA 개시"]
    elif bad == "duplicate":
        payload["cells"] *= 2
    elif bad == "context":
        payload["cells"][0]["cell_id"] = packet["untrusted_document_data"]["rows"][0][
            "first_row_context"
        ][0]["id"]
    elif bad == "omit":
        payload["cells"] = []
    elif bad == "grade":
        payload["grade"] = "E3"
    elif bad == "tamper":
        packet = deepcopy(packet)
        packet["untrusted_document_data"]["rows"][0]["cells"][-1]["text"] = "forged"
    with pytest.raises(ValueError):
        validate(payload, packet, graph, tenant_id=FOREIGN if bad == "tenant" else TENANT)


def test_empty_extraction_remains_unknown_and_reuses_dimension_contract():
    from evaluation.claim_dimensions import prepare_dimensions
    from evaluation.table_atomic_claims import prepare, validate

    graph, ids = case()
    packet = prepare(graph, [ids["r2c1"]], tenant_id=TENANT)[0]
    target = packet["untrusted_document_data"]["target_ids"][0]
    empty = validate(
        {"cells": [{"cell_id": target, "claims": []}]}, packet, graph, tenant_id=TENANT
    )
    assert empty["coverage"][0]["state"] == "not_returned"
    assert empty["coverage"][0]["unreturned_spans"][0]["state"] == "unknown"
    result = validate(
        {"cells": [{"cell_id": target, "claims": ["수거 거점 연동 완료"]}]},
        packet,
        graph,
        tenant_id=TENANT,
    )
    dimensions = prepare_dimensions(graph, result, tenant_id=TENANT)
    assert dimensions["untrusted_document_data"]["claims"][0]["text"] == "수거 거점 연동 완료"


def test_runner_preserves_unknown_and_stops_after_transport_failure(tmp_path):
    from evaluation.table_atomic_claims import run

    class Client:
        model = "test-only"
        calls = 0

        def summary(self):
            return {"calls": self.calls}

        def complete(self, *args, **kwargs):
            self.calls += 1
            raise ConnectionError("must not expose raw provider body")

    graph, ids = case()
    client = Client()
    result = run(graph, [ids["r1c1"], ids["r2c1"]], client, tmp_path / "run", tenant_id=TENANT)
    assert client.calls == 1
    assert [r["status"] for r in result["requests"]] == ["failed", "deferred"]
    assert all(c["state"] == "not_returned" for c in result["coverage"])
    assert result["claims"] == []
    assert "must not expose" not in (tmp_path / "run" / "result.json").read_text()
    with pytest.raises(FileExistsError):
        run(graph, [ids["r1c1"]], client, tmp_path / "run", tenant_id=TENANT)


def test_document_rows_rebind_only_unique_same_row_literal_quotes():
    from evaluation.table_atomic_claims import bind_document_rows

    graph, ids = case()
    payload = {
        "rows": [
            dict(
                row_heading="태양광 확대",
                claims=[
                    "춘천 6MW PPA 개시",
                    "사옥 1MW PPA 추가 개시",
                    "수거 거점 연동 완료",
                    "없는 내용",
                ],
                standalone_status=["달성", "(진행 중)"],
            )
        ]
    }
    result = bind_document_rows(graph, ids["T"], payload, tenant_id=TENANT)
    assert len(result["claims"]) == 2
    assert len(result["unbound"]) == 2
    assert all(c["source_id"] == ids["r1c1"] for c in result["claims"])
    assert all(not c["eligible_for_scoring"] for c in result["claims"])
    assert result["status_context"][0]["binding_status"] == "undetermined"
    assert all(item["state"] == "unknown" for item in result["unbound"])
    with pytest.raises(ValueError):
        bind_document_rows(graph, ids["T"], payload, tenant_id=FOREIGN)
    with pytest.raises(ValueError):
        bind_document_rows(graph, ids["T"], {**payload, "grade": "E3"}, tenant_id=TENANT)


def test_document_row_heading_mismatch_does_not_search_the_whole_document():
    from evaluation.table_atomic_claims import bind_document_rows

    graph, ids = case()
    result = bind_document_rows(
        graph,
        ids["T"],
        {
            "rows": [
                dict(row_heading="다른 회사", claims=["수거 거점 연동 완료"], standalone_status=[])
            ]
        },
        tenant_id=TENANT,
    )
    assert result["claims"] == []
    assert result["unbound"][0]["reason"] == "row_heading_absent_or_ambiguous"


def test_document_quote_glyph_alignment_preserves_source_and_model_quotes():
    from evaluation.table_atomic_claims import bind_document_rows

    graph = fuse_candidates(
        (table([["과제", "내용"], ["재활용", "전국 ‘재활용품 회수’ 거점 연동 완료"]]),),
        tenant_id=TENANT,
    )
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    quote = "전국 '재활용품 회수' 거점 연동 완료"
    result = bind_document_rows(
        graph,
        tid,
        {"rows": [dict(row_heading="재활용", claims=[quote], standalone_status=[])]},
        tenant_id=TENANT,
    )
    assert len(result["claims"]) == 1
    claim = result["claims"][0]
    assert claim["source_ref"]["quote"] == "전국 ‘재활용품 회수’ 거점 연동 완료"
    assert claim["model_quote"] == quote
    assert claim["match_mode"] == "quote_glyph_alignment"
    assert not claim["eligible_for_scoring"]


def test_document_quote_alignment_does_not_resolve_ambiguous_sources_or_changed_numbers():
    from evaluation.table_atomic_claims import bind_document_rows

    for cells, quote in [
        (["'재활용' 10% 확대", "‘재활용’ 10% 확대"], "'재활용' 10% 확대"),
        (["‘재활용’ 10% 확대"], "'재활용' 20% 확대"),
        (["'재활용' 10% 확대 및 ‘재활용’ 10% 확대"], "'재활용' 10% 확대"),
        (["'재활용' 10% 확대 및 ‘재활용’ 10% 확대", "‘재활용’ 10% 확대"], "'재활용' 10% 확대"),
    ]:
        graph = fuse_candidates(
            (table([["과제"] + ["내용"] * len(cells), ["재활용"] + cells]),), tenant_id=TENANT
        )
        tid = next(b.source_id for b in graph.blocks if b.kind == "table")
        result = bind_document_rows(
            graph,
            tid,
            {"rows": [dict(row_heading="재활용", claims=[quote], standalone_status=[])]},
            tenant_id=TENANT,
        )
        assert result["claims"] == []
        assert result["unbound"][0]["state"] == "unknown"


def test_document_binding_excludes_merged_header_rows_and_aligns_whole_headings():
    from evaluation.table_atomic_claims import bind_document_rows

    graph = fuse_candidates(
        (
            table(
                [["범주", "연도"], ["헤더", "2025"], ["‘재활용’", "수거 완료"]],
                spans={(0, 1): (2, 1)},
            ),
        ),
        tenant_id=TENANT,
    )
    tid = next(b.source_id for b in graph.blocks if b.kind == "table")
    result = bind_document_rows(
        graph,
        tid,
        {
            "rows": [
                dict(row_heading="헤더", claims=["2025"], standalone_status=[]),
                dict(row_heading="'재활용'", claims=["수거 완료"], standalone_status=[]),
                dict(row_heading="재활용", claims=["수거 완료"], standalone_status=[]),
            ]
        },
        tenant_id=TENANT,
    )
    assert [c["span"]["quote"] for c in result["claims"]] == ["수거 완료"]
    assert result["claims"][0]["row_heading_ref"]["quote"] == "‘재활용’"
    assert len(result["unbound"]) == 2
    assert all(c["state"] == "unknown" for c in result["unbound"])


def test_document_coverage_exposes_omitted_cells_and_partial_status_without_absence():
    from evaluation.table_atomic_claims import bind_document_rows

    graph, ids = case()
    payload = {
        "rows": [
            dict(
                row_heading="태양광 확대", claims=["춘천 6MW PPA 개시"], standalone_status=["달성"]
            )
        ]
    }
    result = bind_document_rows(graph, ids["T"], payload, tenant_id=TENANT)
    cells = {c["source_id"]: c for c in result["cell_coverage"]}
    assert len(cells) == 6
    assert cells[ids["r0c1"]]["structural_role"] == "header_context"
    assert cells[ids["r1c0"]]["structural_role"] == "row_heading_context"
    assert cells[ids["r1c1"]]["state"] == "partial"
    assert cells[ids["r2c1"]]["state"] == "not_returned"
    assert cells[ids["r2c1"]]["source_ref"]["quote"] == "수거 거점 연동 완료"
    gaps = cells[ids["r1c1"]]["unreturned_spans"]
    assert any("진행 중" in gap["quote"] for gap in gaps)
    assert all(gap["state"] == "unknown" for c in cells.values() for gap in c["unreturned_spans"])
    empty = bind_document_rows(graph, ids["T"], {"rows": []}, tenant_id=TENANT)
    assert len(empty["cell_coverage"]) == 6
    assert all(c["state"] == "not_returned" for c in empty["cell_coverage"])
    assert empty["decision"] is None


def test_document_coverage_preserves_conflicted_source_cells():
    from dataclasses import replace

    from evaluation.table_atomic_claims import bind_document_rows

    graph, ids = case()
    graph = replace(
        graph,
        blocks=tuple(
            replace(b, winner=None, quality="conflict") if b.source_id == ids["r2c1"] else b
            for b in graph.blocks
        ),
    )
    result = bind_document_rows(graph, ids["T"], {"rows": []}, tenant_id=TENANT)
    assert len(result["cell_coverage"]) == 5
    unresolved = result["unresolved_cells"]
    assert len(unresolved) == 1
    assert unresolved[0]["source_id"] == ids["r2c1"]
    assert unresolved[0]["source_quality"] == "conflict"
    assert unresolved[0]["state"] == "unknown"
    assert unresolved[0]["candidate_sources"][0]["raw_text"] == "수거 거점 연동 완료"
