"""Offline regression for the submission-demo resume-only outcome summary.

These cover the pure helpers ``_parse_pilot_status``, ``_coverage_from_inspection``
and ``summarize_outcome`` that turn the pilot's own emitted status line plus its
saved claims list into a concise operator summary. The "remaining" signal is the
observed blocked/unjudged claims (excluding not_applicable), not a generic status word;
assurance status is never asserted; and the printed reopen command is the pilot's
read-only ``--resume`` path (never a paid ``--invoke``). No model, network, or
filesystem side effects are exercised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import submission_demo as sd  # noqa: E402


def _status_line(status: str = "not_run", *, claims: int = 6, stage=None) -> str:
    return json.dumps(
        {
            "run_id": "5a5d0d7d-c371-406f-bacf-44c4a6ddd410",
            "selected_pages": [30],
            "claim_pages": None,
            "claims_http_status": 200,
            "claims": claims,
            "pipeline_outcome": {"stage": stage, "status": status, "exit_code": 0},
        },
        ensure_ascii=False,
    )


def _inspection(decisions: list) -> dict:
    return {
        "claims": {"items": [{"claim_id": str(i), "decision": d} for i, d in enumerate(decisions)]}
    }


def test_parse_pilot_status_ignores_json_log_noise_and_picks_status_line():
    log_noise = json.dumps({"kind": "log", "event": "api_request", "http_status": 200})
    metric_noise = json.dumps({"kind": "metric", "name": "api_latency_ms", "value": 1.0})
    stdout = "\n".join([log_noise, metric_noise, _status_line("not_run"), "Resume-only requested."])
    status = sd._parse_pilot_status(stdout)
    assert status is not None
    assert status["pipeline_outcome"]["status"] == "not_run"
    assert status["claims"] == 6


def test_parse_pilot_status_returns_none_when_absent():
    stdout = "\n".join([json.dumps({"kind": "log", "event": "api_request"}), "no status here", ""])
    assert sd._parse_pilot_status(stdout) is None


def test_coverage_distinguishes_blocked_and_excluded_from_grades():
    inspection = _inspection(
        [
            None,
            {"decision_status": "blocked_evidence", "evidence_grade": None},
            {"decision_status": "not_applicable", "evidence_grade": None},
            {"decision_status": "decided", "evidence_grade": "E2"},
        ]
    )
    inspection["claims"]["next_cursor"] = "next-page"
    cov = sd._coverage_from_inspection(inspection)
    assert cov == {"total": 4, "graded": 1, "pending": 2, "not_applicable": 1, "has_more": True}
    text = "\n".join(
        sd.summarize_outcome(
            json.loads(_status_line()),
            demo_state=Path("demo"),
            port=8790,
            coverage=cov,
        )
    )
    assert "visible page only" in text
    assert "1 not applicable" in text
    assert "2 pending" in text
    assert sd._coverage_from_inspection(None) == {}
    assert sd._coverage_from_inspection({"claims": {}}) == {}


def test_resume_command_is_read_only_never_paid(tmp_path):
    cmd = sd._resume_command(tmp_path / "demo", port=8790)
    assert "--resume" in cmd and "--serve" in cmd
    assert "--invoke" not in cmd
    assert str(tmp_path / "demo") in cmd and "--port 8790" in cmd


def test_summarize_flags_partial_from_real_pending_count(tmp_path):
    status = json.loads(_status_line("not_run"))
    coverage = {"total": 6, "graded": 0, "pending": 6}
    text = "\n".join(
        sd.summarize_outcome(status, demo_state=tmp_path / "d", port=8790, coverage=coverage)
    )
    assert "pipeline status: not_run" in text
    assert "0/6 claims decided, 6 pending" in text
    assert "6 claim(s) have no rule-engine grade yet" in text
    assert "PARTIAL result, not a completed report" in text
    # No fabricated assurance assertion; reopen command is never paid.
    assert "assurance" not in text.lower()
    assert "--resume" in text and "--invoke" not in text


def test_summarize_no_pending_omits_remaining_banner(tmp_path):
    status = json.loads(_status_line("not_run"))
    coverage = {"total": 3, "graded": 3, "pending": 0}
    text = "\n".join(
        sd.summarize_outcome(status, demo_state=tmp_path / "d", port=8790, coverage=coverage)
    )
    assert "3/3 claims decided, 0 pending" in text
    assert "REMAINING" not in text
    assert "--resume" in text


def test_summarize_uses_pilot_status_verbatim_no_invented_states(tmp_path):
    # Whatever the pilot reports is echoed verbatim; the helper does not map it
    # against any hard-coded completed/graded vocabulary.
    status = json.loads(_status_line("pending_downstream"))
    text = "\n".join(
        sd.summarize_outcome(status, demo_state=tmp_path / "d", port=8790, coverage={})
    )
    assert "pipeline status: pending_downstream" in text


def test_summarize_missing_status_does_not_fabricate_completion(tmp_path):
    text = "\n".join(sd.summarize_outcome(None, demo_state=tmp_path / "d", port=8790))
    assert "status line not found" in text
    assert "--resume" in text
