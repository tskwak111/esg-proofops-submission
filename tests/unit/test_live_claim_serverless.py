"""One fake-call path through the serverless claim demo; no network or paid model."""

import importlib.util
import json
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest
import yaml

MODULE = Path(__file__).resolve().parents[2] / "api/live-claim.py"
SPEC = importlib.util.spec_from_file_location("live_claim", MODULE)
live = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(live)


def test_rule_pack_snapshot_matches_repo_config():
    root = MODULE.parents[1]
    manifest = yaml.safe_load((root / "config/rule_pack_manifest.yaml").read_text())
    assert set(live.PACK.files) == set(manifest["files"])
    for name in manifest["files"]:
        assert live.PACK.file_content(name) == yaml.safe_load((root / "config" / name).read_text())


def test_fake_pipeline_and_guards(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")
    called = []

    def fake(system, user, max_tokens):
        called.append((user, max_tokens))
        if len(called) == 1:
            content = {
                "claim_id": user["claim_id"],
                "track": "goal",
                "safe_harbor_category": None,
                "dimensions": {},
            }
        else:
            content = {
                "elements": [
                    {
                        "name": name,
                        "state": "present"
                        if name in ("target_year", "target_metric")
                        else "absent",
                        "quote": (
                            "2030년"
                            if name == "target_year"
                            else "온실가스 배출량"
                            if name == "target_metric"
                            else None
                        ),
                    }
                    for name in user["elements"]
                ]
            }
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    claim = "2030년 온실가스 배출량을 줄이겠습니다."
    with pytest.raises(live.LiveError) as error:
        live.run_claim({"claim": claim}, access_code="wrong", call_model=fake)
    assert error.value.code == "ACCESS_DENIED" and not called
    result = live.run_claim({"claim": claim}, access_code="local-only", call_model=fake)
    assert len(called) == 4
    assert result["replicas"] == 3
    assert result["steps"][1]["replicas"] == 3
    assert result["steps"][1]["model"] == "solar-pro4"
    assert result["decision"]["decision_status"] == "blocked_evidence"
    assert result["decision"]["evidence_grade"] is None
    assert result["decision"]["label"] is None
    assert result["decision"]["grade_range"]["floor"] == "E1"
    assert result["decision"]["grade_range"]["ceiling"] == "E3"
    assert result["decision"]["review_status"] == "needs_review"
    assert result["display_grade"]["grade"] == "E1"
    assert result["display_grade"]["estimated"]
    assert result["fact_assembly"] == "partial-facts-v1"
    assert "E3까지" in result["explanation"]
    assert result["steps"][1]["elements"][0]["engine_state"] in ("unknown", "present")
    assert all(
        item["engine_state"] == "unknown"
        for item in result["steps"][1]["elements"]
        if item["candidate_state"] == "absent"
    )
    assert "원문 PDF 검증 없음" in result["notice"]


def test_unmatched_quote_stays_unknown(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")

    def fake(system, user, max_tokens):
        content = (
            {"claim_id": user["claim_id"], "track": "goal", "safe_harbor_category": None}
            if "sources" in user
            else {
                "elements": [
                    {"name": name, "state": "present", "quote": "없는 근거"}
                    for name in user["elements"]
                ]
            }
        )
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    result = live.run_claim(
        {"claim": "2030년 배출을 줄입니다"}, access_code="local-only", call_model=fake
    )
    assert result["status"] == "needs_review"
    assert all(
        item["engine_state"] == "unknown" and item["quote"] is None
        for item in result["steps"][1]["elements"]
    )


def test_context_quote_and_majority_vote(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")
    calls = []

    def fake(system, user, max_tokens):
        calls.append(user)
        content = (
            {"claim_id": user["claim_id"], "track": "goal", "safe_harbor_category": None}
            if "sources" in user
            else {
                "elements": [
                    {"name": "target_year", "state": "present", "quote": "2030년"},
                    {"name": "scope", "state": "present", "quote": "Scope 1·2"}
                    if len(calls) != 3
                    else {"name": "scope", "state": "unknown", "quote": None},
                    {"name": "method", "state": "present", "quote": "없는 문구"},
                ]
            }
        )
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    result = live.run_claim(
        {
            "claim": "2030년 배출 감축",
            "context": "사업장 Scope 1·2 배출량을 줄이겠습니다.",
            "page_label": "86",
        },
        access_code="local-only",
        call_model=fake,
    )
    elements = {item["name"]: item for item in result["steps"][1]["elements"]}
    assert len(calls) == 4
    assert elements["scope"]["engine_state"] == "present"
    assert elements["scope"]["quote_source"] == "context"
    assert elements["target_year"]["quote_source"] == "claim"
    assert result["source"]["context_sha256"] == live._hash(
        "사업장 Scope 1·2 배출량을 줄이겠습니다."
    )


def test_object_tags_and_rule_gap(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")

    def fake(system, user, max_tokens):
        content = (
            {
                "claim_id": user["claim_id"],
                "track": "goal",
                "safe_harbor_category": "forward_looking",
            }
            if "sources" in user
            else {
                "elements": {
                    "target_year": {"state": "present", "quote": "2030년"},
                    "target_metric": {"state": "present", "quote": "배출량"},
                }
            }
        )
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    result = live.run_claim(
        {"claim": "2030년 배출량을 줄이겠다."}, access_code="local-only", call_model=fake
    )
    assert result["status"] == "needs_review"
    assert result["decision"]["review_status"] == "needs_review"
    assert result["decision"]["evidence_grade"] is None
    assert "GAP-001" in result["decision"]["gap_ids"]
    assert "세이프하버" in result["explanation"]
    assert {
        item["name"] for item in result["steps"][1]["elements"] if item["engine_state"] == "present"
    } == {"target_year", "target_metric"}


def test_omitted_elements_remain_unknown(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")

    def fake(system, user, max_tokens):
        content = (
            {"claim_id": user["claim_id"], "track": "management", "safe_harbor_category": None}
            if "sources" in user
            else {
                "elements": [
                    {
                        "name": "concrete_implementation_detail",
                        "state": "present",
                        "quote": "연 3회",
                    }
                ]
            }
        )
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    result = live.run_claim(
        {"claim": "ESG위원회는 연 3회 정기적으로 개최한다."},
        access_code="local-only",
        call_model=fake,
    )
    elements = result["steps"][1]["elements"]
    assert len(elements) == len(set().union(*live.MAPPINGS["management"].values()))
    present = next(item for item in elements if item["name"] == "concrete_implementation_detail")
    assert present["engine_state"] == "present"
    assert all(
        item["engine_state"] == "unknown"
        for item in elements
        if item["name"] != "concrete_implementation_detail"
    )


def test_local_quote_cannot_prove_bound_assurance(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")

    def fake(system, user, max_tokens):
        content = (
            {"claim_id": user["claim_id"], "track": "performance", "safe_harbor_category": None}
            if "sources" in user
            else {
                "elements": [
                    {
                        "name": "assurance_covered",
                        "state": "present",
                        "quote": "제3자 검증",
                        "extra": "ignored",
                    },
                    {"name": "method", "state": "not_applicable", "quote": "제3자 검증"},
                    {"name": "method", "state": "present", "quote": "제3자 검증"},
                ],
                "comment": "ignored",
            }
        )
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    result = live.run_claim(
        {"claim": "2024년 배출량은 제3자 검증을 받았다."},
        access_code="local-only",
        call_model=fake,
    )
    elements = {item["name"]: item for item in result["steps"][1]["elements"]}
    assert elements["assurance_covered"]["candidate_state"] == "present"
    assert elements["assurance_covered"]["engine_state"] == "unknown"
    assert elements["method"]["engine_state"] == "conflict"


def test_http_handler_with_fake_model(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")

    def fake(system, user, max_tokens):
        content = (
            {"claim_id": user["claim_id"], "track": None, "safe_harbor_category": None}
            if "sources" in user else {"elements": []}
        )
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    monkeypatch.setattr(live, "_provider", fake)
    server = HTTPServer(("127.0.0.1", 0), live.handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/api/live-claim",
            json.dumps({"claim": "2030년 배출을 줄입니다"}).encode(),
            {"Content-Type": "application/json", "X-Demo-Access-Code": "local-only"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
            result = json.load(response)
            assert result["status"] == "needs_review"
            assert result["steps"][0]["track_inferred"]
            assert result["steps"][0]["track"] == "goal"
            assert result["display_grade"]["grade"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_slow_classification_uses_one_tagging_replica(monkeypatch):
    monkeypatch.setenv("DEMO_ACCESS_CODE", "local-only")
    original = live._step
    calls = []

    def fake(system, user, max_tokens):
        calls.append(user)
        content = (
            {"claim_id": user["claim_id"], "track": "goal", "safe_harbor_category": None}
            if "sources" in user else {"elements": []}
        )
        return {
            "choices": [{"message": {"content": json.dumps(content)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        }

    def slow_step(*args):
        result = original(*args)
        if "sources" in args[2]:
            return result[0], result[1], 31_000, result[3], result[4]
        return result

    monkeypatch.setattr(live, "_step", slow_step)
    result = live.run_claim(
        {"claim": "2030년 감축 목표"}, access_code="local-only", call_model=fake
    )
    assert len(calls) == 2
    assert result["replicas"] == 1
