from copy import deepcopy

import pytest

from evaluation.table_role_pilot import prepare_rows, validate_roles
from tests.acceptance.test_parsing import TENANT
from tests.acceptance.test_table_bindings import setup_case


def test_roles_cover_cells_once_with_same_row_header_sources_and_no_grades():
    graph, ids, _ = setup_case()
    packet = prepare_rows(graph, [ids["r1c3"], ids["r1c4"]], tenant_id=TENANT)
    rows = packet["untrusted_document_data"]["rows"]
    assert len(rows) == 1
    payload = {
        "tags": [dict(cell_id=c["id"], role="unknown", basis_ids=[]) for c in rows[0]["cells"]]
    }
    result = validate_roles(payload, packet)
    assert len(result) == len(rows[0]["cells"])
    assert all(
        t["tag_status"] == "model_proposed" and t["binding_status"] == "undetermined"
        for t in result
    )
    assert all(t["source_ref"]["source_id"] in ids.values() for t in result)
    for bad in (
        {"tags": payload["tags"][:-1]},
        {"tags": payload["tags"] + [payload["tags"][0]]},
        {"tags": payload["tags"], "grade": "A"},
    ):
        with pytest.raises(ValueError):
            validate_roles(bad, packet)
    bad = deepcopy(payload)
    bad["tags"][0]["basis_ids"] = ["invented"]
    with pytest.raises(ValueError):
        validate_roles(bad, packet)
    bad = deepcopy(payload)
    bad["tags"][0]["role"] = "verified_result"
    with pytest.raises(ValueError):
        validate_roles(bad, packet)

    bad = deepcopy(payload)
    bad["tags"][0].update(role="category", basis_ids=[rows[0]["first_row_context"][-1]["id"]])
    with pytest.raises(ValueError):
        validate_roles(bad, packet)


def _literal_packet(header_quote):
    """Minimal single-cell packet with one same-column header carrying a literal quote."""
    return {
        "untrusted_document_data": {
            "rows": [
                {
                    "cells": [{"id": "c0", "column": 3}],
                    "first_row_context": [{"id": "h0", "column": 3}],
                }
            ]
        },
        "sources": {
            "c0": {"source_ref": {"quote": "cell value"}, "quality": "unverified"},
            "h0": {"source_ref": {"quote": header_quote}, "quality": "unverified"},
        },
    }


def _literal_packet_multi(header_quotes):
    """Minimal single-cell packet with several same-column headers."""
    return {
        "untrusted_document_data": {
            "rows": [
                {
                    "cells": [{"id": "c0", "column": 3}],
                    "first_row_context": [
                        {"id": f"h{i}", "column": 3} for i in range(len(header_quotes))
                    ],
                }
            ]
        },
        "sources": {
            "c0": {"source_ref": {"quote": "cell value"}, "quality": "unverified"},
            **{
                f"h{i}": {"source_ref": {"quote": quote}, "quality": "unverified"}
                for i, quote in enumerate(header_quotes)
            },
        },
    }


def test_reported_result_supported_only_by_expected_effect_header_is_rejected():
    # Core literal case plus a whitespace variant: sole basis is 기대 효과.
    for header in ("기대 효과", "  기대 효과  ", "기대효과"):
        packet = _literal_packet(header)
        bad = {"tags": [dict(cell_id="c0", role="reported_result", basis_ids=["h0"])]}
        with pytest.raises(ValueError):
            validate_roles(bad, packet)
    # unknown with empty basis is preserved even under an expected-effect header.
    packet = _literal_packet("기대 효과")
    result = validate_roles({"tags": [dict(cell_id="c0", role="unknown", basis_ids=[])]}, packet)
    assert result[0]["tag_status"] == "model_proposed"
    assert result[0]["binding_status"] == "undetermined"
    # Positive control: a 실적 header alone supports reported_result as a
    # proposal only (model_proposed/undetermined, not confirmation of achievement).
    packet = _literal_packet("실적")
    result = validate_roles(
        {"tags": [dict(cell_id="c0", role="reported_result", basis_ids=["h0"])]}, packet
    )
    assert result[0]["tag_status"] == "model_proposed"
    assert result[0]["binding_status"] == "undetermined"
    assert result[0]["basis_refs"][0]["quote"] == "실적"


def test_reported_result_rejected_when_any_same_column_header_is_expected_effect():
    # Same-column headers 기대 효과 plus 실적: citing only 실적 omits the
    # contradictory expected-effect header, so the proposal is still rejected.
    packet = _literal_packet_multi(("기대 효과", "실적"))
    with pytest.raises(ValueError):
        validate_roles(
            {"tags": [dict(cell_id="c0", role="reported_result", basis_ids=["h1"])]}, packet
        )
    # Citing both headers is rejected as well.
    with pytest.raises(ValueError):
        validate_roles(
            {"tags": [dict(cell_id="c0", role="reported_result", basis_ids=["h0", "h1"])]},
            packet,
        )
    # Whitespace variant in the omitted header still triggers the guard.
    packet = _literal_packet_multi(("  기대 효과  ", "실적"))
    with pytest.raises(ValueError):
        validate_roles(
            {"tags": [dict(cell_id="c0", role="reported_result", basis_ids=["h1"])]}, packet
        )
    # unknown is unchanged: still a model_proposed, undetermined proposal.
    result = validate_roles({"tags": [dict(cell_id="c0", role="unknown", basis_ids=[])]}, packet)
    assert result[0]["tag_status"] == "model_proposed"
    assert result[0]["binding_status"] == "undetermined"


def test_merged_header_covers_value_column_without_approving_evidence():
    packet = _literal_packet("기대 효과")
    row = packet["untrusted_document_data"]["rows"][0]
    row["first_row_context"][0].update(column=2, column_span=2)
    payload = {"tags": [dict(cell_id="c0", role="expected_effect", basis_ids=["h0"])]}
    result = validate_roles(payload, packet)
    assert result[0]["binding_status"] == "undetermined"
    payload["tags"][0]["role"] = "reported_result"
    with pytest.raises(ValueError):
        validate_roles(payload, packet)
    row["cells"][0].update(column_span=2)
    payload["tags"][0]["role"] = "expected_effect"
    with pytest.raises(ValueError):
        validate_roles(payload, packet)  # partial overlap cannot cover this merged value


def test_prepared_rows_preserve_parser_cell_spans():
    graph, ids, _ = setup_case()
    packet = prepare_rows(graph, [ids["r1c4"]], tenant_id=TENANT)
    for row in packet["untrusted_document_data"]["rows"]:
        for cell in row["cells"] + row["first_row_context"]:
            assert cell["row_span"] == 1 and cell["column_span"] == 1


def test_repeated_cell_text_does_not_select_other_table_rows():
    graph, ids, _ = setup_case()
    packet = prepare_rows(graph, [ids["r1c2"]], tenant_id=TENANT)  # tCO2e occurs twice
    rows = packet["untrusted_document_data"]["rows"]
    assert [r["row_number"] for r in rows] == [1]


def test_same_header_source_keeps_one_id_across_rows_but_repeated_values_do_not():
    graph, ids, _ = setup_case()
    packet = prepare_rows(graph, [ids["r1c2"], ids["r2c2"]], tenant_id=TENANT)
    first, second = packet["untrusted_document_data"]["rows"]
    assert first["first_row_context"] == second["first_row_context"]
    assert first["cells"][2]["id"] != second["cells"][1]["id"]  # distinct tCO2e cells
    for row in (first, second):
        for cell in row["cells"]:
            assert set(cell["allowed_basis_ids"]) <= {h["id"] for h in row["first_row_context"]}


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_packet_rows_follow_source_table_order_not_uuid_order(monkeypatch, seed):
    from uuid import UUID

    from tests.acceptance import test_parsing

    monkeypatch.setattr(test_parsing, "uuid4", lambda: UUID(int=seed, version=4))
    graph, ids, _ = setup_case()
    packet = prepare_rows(graph, [ids["r0c4"], ids["r1c4"], ids["r2c4"]], tenant_id=TENANT)
    assert [r["row_number"] for r in packet["untrusted_document_data"]["rows"]] == [0, 1, 2]


def test_expected_financial_effect_and_mixed_progress_cannot_be_reported_result():
    for header, quote in [
        ("예상 재무적 영향", "이자수익 증가"),
        ("향후 계획", "25조 원"),
        ("달성 현황", "달성 · 6MW 태양광 PPA 개시 (진행 중)"),
    ]:
        packet = _literal_packet(header)
        packet["sources"]["c0"]["source_ref"]["quote"] = quote
        payload = {"tags": [dict(cell_id="c0", role="reported_result", basis_ids=["h0"])]}
        with pytest.raises(ValueError):
            validate_roles(payload, packet)
        payload["tags"][0].update(role="unknown", basis_ids=[])
        assert validate_roles(payload, packet)[0]["binding_status"] == "undetermined"


def test_second_header_row_reaches_packet_without_inventing_parent_edges():
    from dataclasses import replace

    from proofops.application.ingest.graph_fusion import fuse_candidates

    from tests.acceptance.test_tables import table

    batch = table(
        [
            ["전략", "투자 금액", None],
            [None, "2025", "향후 계획"],
            ["재생에너지", "1.6억 원", "2030년 누적 25조 원"],
        ]
    )
    blocks = []
    for b in batch.blocks:
        name = b.source.source_native_id
        if name == "r0c0":
            b = replace(b, row_span=2)
        if name == "r0c1":
            b = replace(b, column_span=2)
        blocks.append(b)
    graph = fuse_candidates((replace(batch, blocks=tuple(blocks)),), tenant_id=TENANT)
    sid = next(
        b.source_id for b in graph.blocks if b.candidates[0].source.source_native_id == "r2c2"
    )
    packet = prepare_rows(graph, [sid], tenant_id=TENANT)
    row = packet["untrusted_document_data"]["rows"][0]
    assert [c["text"] for c in row["additional_header_context"]] == ["2025", "향후 계획"]
    cell = next(c for c in row["cells"] if c["column"] == 2)
    ids = cell["allowed_basis_ids"]
    assert {packet["sources"][i]["source_ref"]["quote"] for i in ids} == {"투자 금액", "향후 계획"}
    payload = {"tags": [dict(cell_id=c["id"], role="unknown", basis_ids=[]) for c in row["cells"]]}
    tag = next(t for t in payload["tags"] if t["cell_id"] == cell["id"])
    tag.update(role="expected_effect", basis_ids=ids[:1])
    with pytest.raises(ValueError):
        validate_roles(payload, packet)
    tag["basis_ids"] = ids
    assert validate_roles(payload, packet)[-1]["binding_status"] == "undetermined"


@pytest.mark.parametrize(
    "role", ["reported_result", "expected_effect", "activity_description", "category"]
)
def test_mixed_progress_cannot_escape_unknown_by_changing_role(role):
    packet = _literal_packet("달성 현황")
    packet["sources"]["c0"]["source_ref"]["quote"] = "달성 · 태양광 PPA 개시 (진행 중)"
    with pytest.raises(ValueError):
        validate_roles({"tags": [dict(cell_id="c0", role=role, basis_ids=["h0"])]}, packet)


def test_completed_result_header_does_not_support_expected_effect():
    packet = _literal_packet("달성 현황")
    packet["sources"]["c0"]["source_ref"]["quote"] = "수거 거점 연동 완료"
    with pytest.raises(ValueError):
        validate_roles(
            {"tags": [dict(cell_id="c0", role="expected_effect", basis_ids=["h0"])]}, packet
        )


def test_expected_effect_header_cannot_be_relabelled_as_activity():
    packet = _literal_packet("예상 재무적 영향")
    with pytest.raises(ValueError):
        validate_roles(
            {"tags": [dict(cell_id="c0", role="activity_description", basis_ids=["h0"])]}, packet
        )
