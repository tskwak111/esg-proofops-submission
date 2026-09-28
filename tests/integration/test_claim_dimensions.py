from copy import deepcopy
from dataclasses import asdict

import pytest

from evaluation.claim_dimensions import FIELDS, prepare_dimensions, validate_dimensions
from tests.acceptance.test_claims import COMPOUND, TENANT, graph_of


def test_clause_review_keeps_parent_and_never_inherits_shared_boundary():
    from evaluation.claim_dimensions import clause_review_candidates

    text = "국내 사업장에서 93GWh를 조달했으며, 이를 통해 4만 톤을 감축했습니다."
    graph = graph_of(text)
    b = graph.blocks[0]
    artifact = {
        "claims": [
            dict(
                source_id=b.source_id,
                span=dict(char_start=0, char_end=len(text), quote=text),
                source_ref=asdict(b.source_ref()),
                source_quality=b.quality,
            )
        ]
    }
    (review,) = clause_review_candidates(graph, artifact, tenant_id=TENANT)
    assert review["parent"]["span"]["quote"] == text
    assert len(review["children"]) == 2
    first, second = review["children"]
    assert first["span"]["quote"] == "국내 사업장에서 93GWh를 조달했으며,"
    assert second["span"]["quote"] == "이를 통해 4만 톤을 감축했습니다."
    assert second["context_relation"] == "unresolved_backreference"
    assert all(
        c["binding_status"] == "undetermined" and not c["eligible_for_scoring"]
        for c in review["children"]
    )
    assert "국내" not in second["source_ref"]["quote"]
    for child in review["children"]:
        s = child["span"]
        assert text[s["char_start"] : s["char_end"]] == s["quote"]
    bad = deepcopy(artifact)
    bad["claims"][0]["span"]["quote"] = "invented"
    with pytest.raises(ValueError):
        clause_review_candidates(graph, bad, tenant_id=TENANT)


def test_clause_review_retains_unsupported_parent():
    from evaluation.claim_dimensions import clause_review_candidates

    for text in (
        "93GWh를 조달했습니다.",
        "개선했으며, 이를 통해 4만 톤을 감축했습니다.",
        "93GWh를 조달했으며, 이를 통해 4만 톤을 감축했으며, 이를 통해 2톤을 절감했다.",
    ):
        graph = graph_of(text)
        b = graph.blocks[0]
        artifact = {
            "claims": [
                dict(
                    source_id=b.source_id,
                    span=dict(char_start=0, char_end=len(text), quote=text),
                    source_ref=asdict(b.source_ref()),
                    source_quality=b.quality,
                )
            ]
        }
        (review,) = clause_review_candidates(graph, artifact, tenant_id=TENANT)
        assert review["children"] == [] and review["state"] == "unresolved"
        assert review["parent"]["span"]["quote"] == text


def test_metric_cannot_be_only_an_amount_or_unit_even_when_source_exact():
    text = "약 3만 톤의 온실가스"
    graph = graph_of(text)
    b = graph.blocks[0]
    packet = prepare_dimensions(
        graph,
        {
            "claims": [
                dict(
                    source_id=b.source_id,
                    span=dict(char_start=0, char_end=len(text), quote=text),
                    source_ref=asdict(b.source_ref()),
                    source_quality=b.quality,
                )
            ]
        },
        tenant_id=TENANT,
    )
    for metric in ("약 3만 톤", "톤"):
        values = dict.fromkeys(FIELDS)
        values["metric"] = metric
        with pytest.raises(ValueError, match="metric cannot be only"):
            validate_dimensions(
                {"claims": [dict(id="q0", dimensions=values)]}, packet, graph, tenant_id=TENANT
            )
    values["metric"] = "온실가스"
    (result,) = validate_dimensions(
        {"claims": [dict(id="q0", dimensions=values)]}, packet, graph, tenant_id=TENANT
    )
    assert result["dimensions"]["metric"]["source_ref"]["quote"] == "온실가스"
    assert result["binding_status"] == "undetermined"


def test_quantity_ranges_never_yield_a_single_endpoint():
    from evaluation.claim_dimensions import quantity_candidates

    for text in (
        "20~50%의 투자비",
        "20 - 50%의 투자비",
        "20–50%의 투자비",
        "20 ～ 50%의 투자비",
        "20%~50%의 투자비",
        "20 톤~50 톤",
    ):
        values = quantity_candidates(
            {"untrusted_document_data": {"claims": [dict(id="q0", text=text)]}}
        )
        assert not [v for v in values if v["kind"] == "value"]
    values = quantity_candidates(
        {"untrusted_document_data": {"claims": [dict(id="q0", text="변화 -5% 및 35% 절감")]}}
    )
    assert [v["quote"] for v in values if v["kind"] == "value"] == ["-5%", "35%"]


def test_metric_split_rejects_a_quote_starting_inside_a_numeric_range():
    from evaluation.claim_dimensions import separate_metric_quantity

    text = "최대 20~50%의 투자비 절감"
    graph = graph_of(text)
    b = graph.blocks[0]
    packet = prepare_dimensions(
        graph,
        {
            "claims": [
                dict(
                    source_id=b.source_id,
                    span=dict(char_start=0, char_end=len(text), quote=text),
                    source_ref=asdict(b.source_ref()),
                    source_quality=b.quality,
                )
            ]
        },
        tenant_id=TENANT,
    )
    payload = {
        "claims": [
            dict(id="q0", dimensions=dict(entity=None, metric="50%의 투자비", boundary=None))
        ]
    }
    (result,) = separate_metric_quantity(payload, packet, graph, tenant_id=TENANT)
    assert result["metric_components"]["state"] == "unresolved"


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("약 3만 톤의 온실가스", ("약 3만", "톤", "온실가스")),
        ("951만 tCO₂e의 배출량", ("951만", "tCO₂e", "배출량")),
        ("3 톤/년의 온실가스", None),
        ("3 %p의 개선", None),
        ("3 톤 이상의 온실가스", None),
        ("3 톤의 온실가스와 2 톤의 폐기물", None),
        ("약 3만 톤 온실가스", None),
        ("약 3만 톤의", None),
        ("약 3만 톤의 온실가스의 배출량", None),
        ("온실가스", None),
        (None, None),
    ],
)
def test_metric_components_preserve_original_proposal_and_source(phrase, expected):
    from evaluation.claim_dimensions import separate_metric_quantity

    text = phrase or "정보 없음"
    graph = graph_of(text)
    b = graph.blocks[0]
    packet = prepare_dimensions(
        graph,
        {
            "claims": [
                dict(
                    source_id=b.source_id,
                    span=dict(char_start=0, char_end=len(text), quote=text),
                    source_ref=asdict(b.source_ref()),
                    source_quality=b.quality,
                )
            ]
        },
        tenant_id=TENANT,
    )
    payload = {
        "claims": [dict(id="q0", dimensions=dict(entity=None, metric=phrase, boundary=None))]
    }
    (result,) = separate_metric_quantity(payload, packet, graph, tenant_id=TENANT)
    assert result["binding_status"] == "undetermined"
    assert result["dimensions"]["entity"]["state"] == "unknown"
    if phrase:
        assert result["dimensions"]["metric"]["source_ref"]["quote"] == phrase
    parts = result["metric_components"]
    if expected is None:
        assert parts == {"state": "unresolved", "reason": "unsupported_or_ambiguous_metric_phrase"}
    else:
        assert parts["state"] == "local_candidate"
        for field, quote in zip(("quantity", "unit", "metric"), expected, strict=True):
            ref = parts[field]
            assert ref["quote"] == quote
            assert text[ref["char_start"] : ref["char_end"]] == quote


def test_semantic_token_ranges_cannot_borrow_from_another_claim():
    from evaluation.claim_dimensions import semantic_tokens, validate_semantic_tokens

    text = "A사는 국내 온실가스 배출량을 공개했다."
    graph = graph_of(text)
    b = graph.blocks[0]
    split = text.index("온실가스")
    packet = prepare_dimensions(
        graph,
        {
            "claims": [
                dict(
                    source_id=b.source_id,
                    span=dict(char_start=start, char_end=end, quote=text[start:end]),
                    source_ref=asdict(
                        b.source_ref(normalized_char_start=start, normalized_char_end=end)
                    ),
                    source_quality=b.quality,
                )
                for start, end in ((0, split - 1), (split, len(text)))
            ]
        },
        tenant_id=TENANT,
    )
    tokens = semantic_tokens(packet)
    assert [t["quote"] for t in tokens] == ["A사는", "국내", "온실가스", "배출량을", "공개했다."]
    payload = {
        "claims": [
            dict(id="q0", dimensions=dict(entity=["t0", "t0"], metric=None, boundary=None)),
            dict(id="q1", dimensions=dict(entity=None, metric=["t2", "t3"], boundary=None)),
        ]
    }
    result = validate_semantic_tokens(payload, packet, graph, tenant_id=TENANT)
    assert result[1]["dimensions"]["metric"]["source_ref"]["quote"] == "온실가스 배출량을"
    assert result[1]["dimensions"]["entity"]["state"] == "unknown"
    assert result[1]["binding_status"] == "undetermined"
    for span in (["t0", "t0"], ["t3", "t2"], ["t2", "t99"], "온실가스", ["t2"]):
        bad = deepcopy(payload)
        bad["claims"][1]["dimensions"]["metric"] = span
        with pytest.raises(ValueError):
            validate_semantic_tokens(bad, packet, graph, tenant_id=TENANT)


def test_semantic_dimensions_reject_borrowed_entities_and_extra_roles():
    from evaluation.claim_dimensions import validate_semantics

    text = "A사는 국내 사업장 온실가스 배출량을 공개했다. 온실가스를 감축했다."
    graph = graph_of(text)
    b = graph.blocks[0]
    split = text.index(" 온실가스를") + 1
    artifact = {
        "claims": [
            dict(
                source_id=b.source_id,
                span=dict(char_start=start, char_end=end, quote=text[start:end]),
                source_ref=asdict(
                    b.source_ref(normalized_char_start=start, normalized_char_end=end)
                ),
                source_quality=b.quality,
            )
            for start, end in ((0, split - 1), (split, len(text)))
        ]
    }
    packet = prepare_dimensions(graph, artifact, tenant_id=TENANT)
    payload = {
        "claims": [
            dict(
                id="q0",
                dimensions=dict(entity="A사", metric="온실가스 배출량", boundary="국내 사업장"),
            ),
            dict(id="q1", dimensions=dict(entity=None, metric="온실가스", boundary=None)),
        ]
    }
    result = validate_semantics(payload, packet, graph, tenant_id=TENANT)
    assert result[0]["dimensions"]["boundary"]["source_ref"]["quote"] == "국내 사업장"
    assert result[1]["dimensions"]["entity"]["state"] == "unknown"
    assert all(r["binding_status"] == "undetermined" for r in result)
    for field, value in (("entity", "A사"), ("boundary", "국내 사업장"), ("grade", "E3")):
        bad = deepcopy(payload)
        bad["claims"][1]["dimensions"][field] = value
        with pytest.raises(ValueError):
            validate_semantics(bad, packet, graph, tenant_id=TENANT)


def test_dimension_quotes_are_bound_to_their_claim_and_null_stays_unknown():
    graph = graph_of(COMPOUND)
    block = graph.blocks[0]
    candidate = dict(
        source_id=block.source_id,
        span=dict(char_start=0, char_end=len(COMPOUND), quote=COMPOUND),
        source_ref=asdict(block.source_ref()),
        source_quality=block.quality,
    )
    packet = prepare_dimensions(graph, {"claims": [candidate]}, tenant_id=TENANT)
    dimensions = dict.fromkeys(FIELDS)
    dimensions.update(baseline_period="2020년", target_period="2030년", reporting_period="2023년")
    payload = {"claims": [dict(id="q0", dimensions=dimensions)]}
    result = validate_dimensions(payload, packet, graph, tenant_id=TENANT)
    assert result[0]["dimensions"]["baseline_period"]["source_ref"]["quote"] == "2020년"
    assert result[0]["dimensions"]["entity"]["state"] == "unknown"
    assert result[0]["binding_status"] == "undetermined"
    for quote in ("2025년", "당사는 90% 감축했습니다."):
        bad = deepcopy(payload)
        bad["claims"][0]["dimensions"]["reporting_period"] = quote
        with pytest.raises(ValueError):
            validate_dimensions(bad, packet, graph, tenant_id=TENANT)
    for bad in ({"claims": []}, {"claims": payload["claims"], "grade": "A"}):
        with pytest.raises(ValueError):
            validate_dimensions(bad, packet, graph, tenant_id=TENANT)
    empty = {"claims": [dict(id="q0", dimensions=dict.fromkeys(FIELDS))]}
    (unresolved,) = validate_dimensions(empty, packet, graph, tenant_id=TENANT)
    assert unresolved["populated_fields"] == 0
    assert unresolved["review_reason"] == "all_dimensions_unknown"


def test_period_candidate_roles_do_not_generate_quotes():
    from evaluation.claim_dimensions import period_candidates, validate_periods

    graph = graph_of(COMPOUND)
    b = graph.blocks[0]
    artifact = {
        "claims": [
            dict(
                source_id=b.source_id,
                span=dict(char_start=0, char_end=len(COMPOUND), quote=COMPOUND),
                source_ref=asdict(b.source_ref()),
                source_quality=b.quality,
            )
        ]
    }
    packet = prepare_dimensions(graph, artifact, tenant_id=TENANT)
    candidates = period_candidates(packet)
    assert [c["quote"] for c in candidates] == ["2023년", "2030년", "2020년"]
    roles = ["reporting_period", "target_period", "baseline_period"]
    payload = {"tags": [dict(id=c["id"], role=r) for c, r in zip(candidates, roles)]}
    tags = validate_periods(payload, packet, graph, tenant_id=TENANT)
    assert tags[0]["dimensions"]["baseline_period"]["source_ref"]["quote"] == "2020년"
    payload["tags"][0]["role"] = "unit"
    with pytest.raises(ValueError):
        validate_periods(payload, packet, graph, tenant_id=TENANT)


def test_quantity_candidates_preserve_scope_approximation_and_units():
    from evaluation.claim_dimensions import quantity_candidates, validate_quantities

    text = (
        "2019년 Scope 1 및 Scope 2 기준값은 951만 tCO₂e이며, "
        "2025년 8건으로 약 3만 톤을 감축했습니다."
    )
    graph = graph_of(text)
    b = graph.blocks[0]
    packet = prepare_dimensions(
        graph,
        {
            "claims": [
                dict(
                    source_id=b.source_id,
                    span=dict(char_start=0, char_end=len(text), quote=text),
                    source_ref=asdict(b.source_ref()),
                    source_quality=b.quality,
                )
            ]
        },
        tenant_id=TENANT,
    )
    candidates = quantity_candidates(packet)
    assert {c["quote"] for c in candidates} == {
        "Scope 1 및 Scope 2",
        "951만 tCO₂e",
        "tCO₂e",
        "약 3만 톤",
        "톤",
    }
    payload = {
        "tags": [
            dict(id=c["id"], role="scope" if c["kind"] == "scope" else "unknown")
            for c in candidates
        ]
    }
    result = validate_quantities(payload, packet, graph, tenant_id=TENANT)
    assert result[0]["dimensions"]["scope"]["source_ref"]["quote"] == "Scope 1 및 Scope 2"
    payload["tags"][0]["role"] = "reported_value"
    with pytest.raises(ValueError):
        validate_quantities(payload, packet, graph, tenant_id=TENANT)


def test_unit_candidates_do_not_match_english_words_or_percentage_points():
    from evaluation.claim_dimensions import quantity_candidates

    packet = {
        "untrusted_document_data": {"claims": [dict(id="q0", text="whole warehouse 3%p 5%포인트")]}
    }
    assert quantity_candidates(packet) == []


def test_unsupported_scope_ranges_do_not_become_partial_scopes():
    from evaluation.claim_dimensions import quantity_candidates

    packet = {
        "untrusted_document_data": {"claims": [dict(id="q0", text="Scope 1–3 및 Scope 1·20")]}
    }
    assert quantity_candidates(packet) == []


def test_measurement_roles_preserve_claim_basis_and_unstated_boundaries():
    from evaluation.claim_dimensions import validate_measurements

    graph = graph_of(COMPOUND)
    b = graph.blocks[0]
    packet = prepare_dimensions(
        graph,
        {
            "claims": [
                dict(
                    source_id=b.source_id,
                    span=dict(char_start=0, char_end=len(COMPOUND), quote=COMPOUND),
                    source_ref=asdict(b.source_ref()),
                    source_quality=b.quality,
                )
            ]
        },
        tenant_id=TENANT,
    )
    result = validate_measurements(
        {"tags": [dict(id="q0", role="emissions_change")]}, packet, graph, tenant_id=TENANT
    )
    assert result[0]["measurement_role"]["source_ref"]["quote"] == COMPOUND
    assert result[0]["dimensions"]["entity"]["state"] == "unknown"
    assert result[0]["dimensions"]["boundary"]["state"] == "unknown"
    assert result[0]["binding_status"] == "undetermined"
    for payload in (
        {"tags": []},
        {"tags": [dict(id="q0", role="verified_reduction")]},
        {"tags": [dict(id="q0", role="emissions_change", grade="A")]},
    ):
        with pytest.raises(ValueError):
            validate_measurements(payload, packet, graph, tenant_id=TENANT)


def test_reporting_period_cannot_be_an_organizational_boundary():
    from evaluation.claim_dimensions import validate_semantics

    for quote in ("2023년부터 2025년까지", "2023–2025", "2025년"):
        text = f"{quote} 국내 사업장 온실가스 배출량"
        graph = graph_of(text)
        block = graph.blocks[0]
        packet = prepare_dimensions(
            graph,
            {
                "claims": [
                    dict(
                        source_id=block.source_id,
                        span=dict(char_start=0, char_end=len(text), quote=text),
                        source_ref=asdict(block.source_ref()),
                        source_quality=block.quality,
                    )
                ]
            },
            tenant_id=TENANT,
        )
        payload = {
            "claims": [
                dict(id="q0", dimensions=dict(entity=None, metric="온실가스", boundary=quote))
            ]
        }
        with pytest.raises(ValueError, match="boundary cannot be only a period"):
            validate_semantics(payload, packet, graph, tenant_id=TENANT)
        payload["claims"][0]["dimensions"]["boundary"] = f"{quote} 국내 사업장"
        (result,) = validate_semantics(payload, packet, graph, tenant_id=TENANT)
        assert result["dimensions"]["boundary"]["state"] == "model_proposed"
        assert result["binding_status"] == "undetermined"
