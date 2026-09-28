"""A stopped paid pipeline must not run later stages or report success."""

import sys
from types import SimpleNamespace

import pytest

from evaluation.local_upstage_pilot import run_live_stages


@pytest.mark.parametrize("failed_stage", ["parse", "extract", "tag", None])
@pytest.mark.parametrize("continuation", [False, True])
def test_stage_failure_stops_pipeline_and_closes_resources(monkeypatch, failed_stage, continuation):
    from proofops_worker import composition, extract_batch

    visited, closed = [], []

    def build(**options):
        stage = options["stage"]
        visited.append(stage)
        return SimpleNamespace(
            run_once=lambda **_: (
                "failed"
                if stage == failed_stage and not (continuation and stage == "extract")
                else "committed"
            ),
            uploads=SimpleNamespace(
                close=lambda: closed.append(stage),
                registry=SimpleNamespace(close=lambda: None),
            ),
        )

    monkeypatch.setattr(composition, "build_composition", build)
    monkeypatch.setattr(
        extract_batch,
        "run_batches",
        lambda *_, **__: [{"status": "failed" if failed_stage == "extract" else "complete"}],
    )
    args = SimpleNamespace(
        verify_paragraphs=False,
        native_quote_typography=False,
        raster_ocr=False,
        extraction_total_calls=16 if continuation else None,
        max_calls=8,
    )
    result = run_live_stages(args, tenant_id="tenant", run_id="run")
    expected = ["parse", "extract", "tag"]
    if failed_stage:
        expected = expected[: expected.index(failed_stage) + 1]
    assert visited == closed == expected
    assert result["exit_code"] == (1 if failed_stage else 0)
    assert result["stage"] == expected[-1]
    assert result["status"] == ("failed" if failed_stage else "committed")


def test_resume_continues_pending_extraction_before_tagging(monkeypatch):
    from proofops_worker import composition, extract_batch

    visited = []

    def build(**options):
        stage = options["stage"]
        visited.append(stage)
        return SimpleNamespace(
            run_once=lambda **_: "pending_downstream" if stage == "extract" else "committed",
            uploads=SimpleNamespace(
                close=lambda: None, registry=SimpleNamespace(close=lambda: None)
            ),
        )

    def batches(*_, **__):
        visited.append("continue_extract")
        return [{"status": "complete"}]

    monkeypatch.setattr(composition, "build_composition", build)
    monkeypatch.setattr(extract_batch, "run_batches", batches)
    args = SimpleNamespace(
        verify_paragraphs=False,
        native_quote_typography=False,
        raster_ocr=False,
        extraction_total_calls=16,
        max_calls=8,
    )
    assert run_live_stages(args, tenant_id="tenant", run_id="run")["exit_code"] == 0
    assert visited == ["parse", "extract", "continue_extract", "tag"]


@pytest.mark.parametrize("status", ["deferred", "retry", "blocked"])
def test_batch_loop_stops_on_temporary_or_ownership_halt(status):
    from proofops_worker.extract_batch import run_batches

    runner = SimpleNamespace(run_batch=lambda **_: {"status": status})
    assert run_batches(runner, tenant_id="tenant", run_id="run", batches=100) == [
        {"status": status, "batch": 1}
    ]


@pytest.mark.parametrize("outcome", ["idle", "ignored"])
@pytest.mark.parametrize(
    "batch_result",
    [
        {"status": "no_revision"},
        {"status": "committed", "pending_after": ["unprocessed"], "stop_code": "BUDGET_EXHAUSTED"},
        {"status": "committed", "pending_after": ["unprocessed"]},
    ],
)
def test_restart_without_complete_extraction_never_tags(monkeypatch, outcome, batch_result):
    from proofops_worker import composition, extract_batch

    visited = []

    def build(**options):
        stage = options["stage"]
        visited.append(stage)
        return SimpleNamespace(
            run_once=lambda **_: outcome if stage == "extract" else "committed",
            uploads=SimpleNamespace(
                close=lambda: None, registry=SimpleNamespace(close=lambda: None)
            ),
        )

    monkeypatch.setattr(composition, "build_composition", build)
    monkeypatch.setattr(extract_batch, "run_batches", lambda *_, **__: [batch_result])
    args = SimpleNamespace(
        verify_paragraphs=False,
        native_quote_typography=False,
        raster_ocr=False,
        extraction_total_calls=16,
        max_calls=8,
    )
    result = run_live_stages(args, tenant_id="tenant", run_id="run")
    assert visited == ["parse", "extract"]
    assert result["exit_code"] == 1


@pytest.mark.parametrize("outcome", ["idle", "ignored"])
def test_single_batch_resume_requires_a_published_extract(monkeypatch, outcome):
    from proofops_worker import composition

    visited = []

    def build(**options):
        stage = options["stage"]
        visited.append(stage)
        return SimpleNamespace(
            run_once=lambda **_: outcome if stage == "extract" else "committed",
            store=SimpleNamespace(jobs=SimpleNamespace(get_run=lambda *_: {})),
            uploads=SimpleNamespace(
                close=lambda: None, registry=SimpleNamespace(close=lambda: None)
            ),
        )

    monkeypatch.setattr(composition, "build_composition", build)
    args = SimpleNamespace(
        verify_paragraphs=False,
        native_quote_typography=False,
        raster_ocr=False,
        extraction_total_calls=None,
        max_calls=8,
    )
    result = run_live_stages(args, tenant_id="tenant", run_id="run")
    assert visited == ["parse", "extract"]
    assert result == {"stage": "extract", "status": "no_revision", "exit_code": 1}


def test_run_batches_streams_safe_progress_before_next_call(monkeypatch):
    import json

    import proofops_worker.extract_batch as module

    events = []
    summaries = [
        {
            "status": "committed",
            "model_processed_after": 3,
            "pending_after": ["private-source"],
            "raw_error": "private-error",
        },
        {"status": "blocked", "stop_code": "BUDGET_EXHAUSTED"},
    ]

    def run_batch(**_):
        index = sum(event == "call" for event in events)
        events.append("call")
        return summaries[index]

    def capture(line, *, file, flush):
        assert file is sys.stderr and flush is True
        events.append(json.loads(line))

    monkeypatch.setattr(module, "print", capture, raising=False)
    results = module.run_batches(
        SimpleNamespace(run_batch=run_batch), tenant_id="tenant", run_id="run", batches=100
    )
    assert results == [dict(summary, batch=i + 1) for i, summary in enumerate(summaries)]
    assert events == [
        "call",
        {
            "event": "extract_batch_progress",
            "batch": 1,
            "status": "committed",
            "model_processed_count": 3,
            "pending_count": 1,
            "stop_code": None,
        },
        "call",
        {
            "event": "extract_batch_progress",
            "batch": 2,
            "status": "blocked",
            "model_processed_count": None,
            "pending_count": None,
            "stop_code": "BUDGET_EXHAUSTED",
        },
    ]
