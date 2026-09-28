"""Offline regression for prefilter comparison hardstop vs ordinary invalid."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from evaluation.claim_prefilter import prepare
from evaluation.prefilter_comparison import execute
from tests.acceptance.test_claims import TENANT, graph_of


def _graph_and_two_packet_plan():
    graph = graph_of("친환경 제품입니다.", "이를 통해 12% 줄였습니다.")
    plan = prepare(graph, tenant_id=TENANT, pages=(1, 2), mode="all_text")
    # Force two packets for intra-execute hardstop testing.
    if (
        len(plan["packets"]) == 1
        and len(plan["packets"][0]["untrusted_document_data"]["targets"]) == 2
    ):
        p = plan["packets"][0]
        t0, t1 = p["untrusted_document_data"]["targets"]
        p0 = deepcopy(p)
        p1 = deepcopy(p)
        p0["untrusted_document_data"]["targets"] = [t0]
        p1["untrusted_document_data"]["targets"] = [t1]
        plan["packets"] = [p0, p1]
    return graph, plan


@pytest.mark.parametrize(
    "code",
    [
        "BUDGET_EXHAUSTED",
        "PRICE_RECHECK_REQUIRED",
        "UPSTAGE_HTTP_429",
        "UPSTAGE_HTTP_503",
        "UPSTAGE_REQUEST_FAILED",
        "UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED",
    ],
)
def test_execute_hardstop_is_fail_closed_no_extra_paid_call(tmp_path, code):
    graph, plan = _graph_and_two_packet_plan()
    assert len(plan["packets"]) == 2
    calls = []

    class HardStop:
        def complete(self, system, user_json, **kwargs):
            calls.append(
                json.loads(user_json)["untrusted_document_data"]["targets"][0]["source_id"]
            )
            raise ValueError(code)

    result = execute(plan, graph, HardStop(), tmp_path / f"hardstop-{code}")
    assert len(calls) == 1, "must not make second paid call after hardstop within execute"
    assert result["stop_error"] == code
    assert result["attempted_count"] == 1 and result["deferred_count"] == 1
    assert result["failed_packets"] == 2 and result["status"] == "partial"
    # First record exact error, second deferred.
    assert result["records"][0].get("error") == code
    assert result["records"][1].get("error") == f"deferred_after_{code}"
    # No fabricated success, no request.json for deferred.
    assert len(list((tmp_path / f"hardstop-{code}").glob("*/request.json"))) == 1
    assert len(list((tmp_path / f"hardstop-{code}").glob("*/validation.json"))) == 2
    # Deferred coverage preserved as unknown via deferred error.
    assert result["records"][1]["status"] == "failed"


def test_execute_ordinary_invalid_still_allows_next_request(tmp_path):
    graph, plan = _graph_and_two_packet_plan()
    calls = []

    class Flaky:
        def complete(self, system, user_json, **kwargs):
            calls.append(1)
            # First packet invalid sentence_ids -> will raise via execute's validation
            if len(calls) == 1:
                return {
                    "provider_model": "test",
                    "content": json.dumps({"sentence_ids": ["s999"]}),
                    "input_tokens": 10,
                    "output_tokens": 5,
                    "cost_with_vat_reserve_usd": "0.001",
                }
            # Second packet valid: use the aliased s0 that execute will expect
            # The second packet's wire alias is s0, so returning s0 is valid.
            return {
                "provider_model": "test",
                "content": json.dumps({"sentence_ids": ["s0"]}),
                "input_tokens": 10,
                "output_tokens": 5,
                "cost_with_vat_reserve_usd": "0.001",
            }

    result = execute(plan, graph, Flaky(), tmp_path / "ordinary")
    assert len(calls) == 2, "ordinary invalid must still attempt next packet"
    assert result["stop_error"] is None
    assert result["attempted_count"] == 2 and result["deferred_count"] == 0
    assert result["records"][0].get("error") == "REQUEST_OR_SOURCE_VALIDATION_FAILED"
    assert "s999" not in str(result["records"][0].get("error"))
    assert result["records"][1]["status"] == "passed"
    assert len(list((tmp_path / "ordinary").glob("*/request.json"))) == 2


@pytest.mark.parametrize(
    "code",
    [
        "BUDGET_EXHAUSTED",
        "PRICE_RECHECK_REQUIRED",
        "UPSTAGE_HTTP_429",
        "UPSTAGE_HTTP_503",
        "UPSTAGE_REQUEST_FAILED",
        "UPSTAGE_RECEIPT_INVALID_RESERVATION_RETAINED",
    ],
)
def test_run_sequencing_hardstop_across_modes_is_fail_closed(tmp_path, monkeypatch, code):
    from types import SimpleNamespace

    from evaluation import prefilter_comparison as module

    graph = graph_of("친환경 제품입니다.")
    calls = []

    class HardStop:
        def __init__(self, *_):
            pass

        def summary(self):
            return {"test_only": True}

        def complete(self, *args, **kwargs):
            calls.append(1)
            raise ValueError(code)

    monkeypatch.setattr(module, "__file__", str(tmp_path / "evaluation/probe.py"))
    monkeypatch.setattr(module, "load_graph", lambda *_: graph)
    monkeypatch.setattr(module, "UpstageProbe", HardStop)
    (tmp_path / ".env.upstage.local").write_text("UPSTAGE_API_KEY=offline-test\n")
    module.run(
        SimpleNamespace(pdf=Path("unused.pdf"), manifest=Path("unused.json"), pages=[1], live=True)
    )
    assert len(calls) == 1
    folder = next((tmp_path / ".local/prefilter").iterdir())
    first = json.loads((folder / "all_text-results.json").read_text())
    second = json.loads((folder / "filtered-results.json").read_text())
    assert first["stop_error"] == code and first["attempted_count"] == 1
    assert second["stop_error"] == code and second["attempted_count"] == 0
    assert second["deferred_count"] == 1 and second["claims"] == []
    assert not list((folder / "filtered").glob("*/request.json"))
