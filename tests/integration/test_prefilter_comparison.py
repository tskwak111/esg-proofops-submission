"""Compare real planning/validation paths; only replace the paid transport."""

import json

from evaluation import prefilter_comparison as comparison
from evaluation.claim_prefilter import prepare
from tests.acceptance.test_claims import TENANT, graph_of


def test_response_failure_is_retained_and_not_scored_as_zero_recall(tmp_path):
    graph = graph_of("친환경 제품입니다.")
    plan = prepare(graph, tenant_id=TENANT, pages=(1,))

    class Transport:
        def complete(self, system, user_json, **kwargs):
            return {
                "provider_model": "test",
                "content": '{"blocks": []}',
                "input_tokens": 100,
                "output_tokens": 4,
                "cost_with_vat_reserve_usd": "0.01",
            }

    result = comparison.execute(plan, graph, Transport(), tmp_path)
    assert result["status"] == "partial"
    assert result["failed_packets"] == 1
    assert result["claims"] == []
    assert result["input_tokens"] == 100
    assert (
        json.loads(next(tmp_path.glob("*/response.json")).read_text())["content"]
        == '{"blocks": []}'
    )
    report = comparison.compare(result, result, plan)
    assert report["recall"] is None
    assert report["status"] == "partial"


def test_filter_omissions_are_separate_from_model_disagreements():
    def claim(sid, start, end):
        return {"source_id": sid, "span": {"char_start": start, "char_end": end}}

    baseline = dict(status="passed", claims=[claim("a", 0, 5), claim("b", 0, 4)])
    filtered = dict(status="passed", claims=[claim("a", 0, 4)])
    plan = {"packets": [{"untrusted_document_data": {"targets": [{"source_id": "a"}]}}]}
    report = comparison.compare(baseline, filtered, plan)
    assert len(report["baseline_claims_in_deferred_sources"]) == 1
    assert report["baseline_claims_in_deferred_sources"][0]["source_id"] == "b"
    assert len(report["baseline_only_exact_spans"]) == 2
    assert report["recall"] is None


def test_short_wire_ids_resolve_only_through_recorded_packet_mapping(tmp_path):
    graph = graph_of("친환경 제품입니다.")
    plan = prepare(graph, tenant_id=TENANT, pages=(1,))

    class Transport:
        def complete(self, system, user_json, **kwargs):
            packet = json.loads(user_json)
            assert (
                packet["untrusted_document_data"]["targets"][0]["sentences"][0]["sentence_id"]
                == "s0"
            )
            return {
                "provider_model": "test",
                "content": '{"sentence_ids":["s0"]}',
                "input_tokens": 50,
                "output_tokens": 5,
                "cost_with_vat_reserve_usd": "0.001",
            }

    result = comparison.execute(plan, graph, Transport(), tmp_path)
    assert result["status"] == "passed"
    assert result["claims"][0]["source_ref"]["quote"] == "친환경 제품입니다."
    request = json.loads(next(tmp_path.glob("*/request.json")).read_text())
    assert result["claims"][0]["sentence_id"] == request["sentence_id_map"]["s0"]
    assert request["wire_version"] == 2


def test_unknown_and_duplicate_short_ids_remain_failed(tmp_path):
    graph = graph_of("친환경 제품입니다.")
    plan = prepare(graph, tenant_id=TENANT, pages=(1,))

    class Transport:
        def __init__(self, ids):
            self.ids = ids

        def complete(self, *args, **kwargs):
            return {
                "provider_model": "test",
                "content": json.dumps({"sentence_ids": self.ids}),
                "input_tokens": 50,
                "output_tokens": 5,
                "cost_with_vat_reserve_usd": "0.001",
            }

    for index, ids in enumerate((["s999"], ["s0", "s0"])):
        result = comparison.execute(plan, graph, Transport(ids), tmp_path / str(index))
        assert result["status"] == "partial" and result["claims"] == []
