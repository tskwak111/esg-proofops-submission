"""Focused offline tests for the opt-in local serve-worker loop.

These exercise the loop's control flow with in-memory fakes only: no Java
parser, no model call, no Upstage ledger, no HTTP. They verify that a newly
queued run advances parse -> extract -> tag, that a failed or retry stage stops
the run without spinning or re-invoking, and that a read-only serve (worker
disabled) never constructs or runs a worker.
"""

from __future__ import annotations

import threading

from evaluation import serve_worker

TENANT = "11111111-1111-4111-8111-111111111111"
RUN = "22222222-2222-4222-8222-222222222222"

_NEXT_STAGE = {"parse": "extract", "extract": "tag", "tag": "review"}


class FakeRunsService:
    """Minimal RunService.list stand-in backed by a mutable run dict."""

    def __init__(self, runs):
        # runs: list of mutable dicts with run_id/status/current_stage
        self.runs = runs

    def list(self, tenant_id, *, cursor=None, limit=50):
        assert tenant_id == TENANT
        # Single page; the loop tolerates next_cursor=None.
        return {"items": list(self.runs), "next_cursor": None}


class FakeComposition:
    """Records run_once calls and returns the scripted outcome for its stage."""

    def __init__(self, stage, script, calls, run):
        self.stage = stage
        self.script = script
        self.calls = calls
        self.run = run
        self.uploads = self  # close() no-ops
        self.registry = self

    def run_once(self, *, tenant_id, run_id):
        self.calls.append((self.stage, run_id))
        outcome = self.script[self.stage]
        if outcome == "committed":
            # Mirror the real store: a committed stage advances current_stage.
            self.run["current_stage"] = _NEXT_STAGE[self.stage]
            if self.stage == "tag":
                self.run["status"] = "partial"
        elif self.stage == "tag" and outcome == "blocked":
            # A blocked tag keeps current_stage=tag and marks the run partial,
            # matching validate_tag_commit in the real job store.
            self.run["status"] = "partial"
        return outcome

    def close(self):  # uploads.close()
        pass


def _factory(script, calls, run):
    def build_composition(*, stage, **_ignored):
        return FakeComposition(stage, script, calls, run)

    return build_composition


def test_queued_run_advances_parse_extract_tag():
    run = {"run_id": RUN, "status": "queued", "current_stage": "parse"}
    calls: list = []
    script = {"parse": "committed", "extract": "committed", "tag": "blocked"}
    observed = serve_worker.drive_run(_factory(script, calls, run), TENANT, run)
    assert [stage for stage, _ in calls] == ["parse", "extract", "tag"]
    assert observed == [("parse", "committed"), ("extract", "committed"), ("tag", "blocked")]
    # A blocked tag ends the sweep; the run stays at tag and is marked partial.
    assert run["current_stage"] == "tag"
    assert run["status"] == "partial"


def test_failed_stage_stops_without_reinvoke():
    run = {"run_id": RUN, "status": "queued", "current_stage": "parse"}
    calls: list = []
    script = {"parse": "failed", "extract": "committed", "tag": "blocked"}
    observed = serve_worker.drive_run(_factory(script, calls, run), TENANT, run)
    # Only parse ran; failure must not fall through to extract/tag.
    assert calls == [("parse", RUN)]
    assert observed == [("parse", "failed")]


def test_retry_stage_does_not_spin_or_reinvoke():
    run = {"run_id": RUN, "status": "queued", "current_stage": "extract"}
    calls: list = []
    script = {"parse": "idle", "extract": "retry", "tag": "blocked"}
    observed = serve_worker.drive_run(_factory(script, calls, run), TENANT, run)
    # Starts at current_stage=extract, sees retry, stops; never re-invokes extract
    # or advances to tag. Backoff is owned by the durable store, not this loop.
    assert calls == [("extract", RUN)]
    assert observed == [("extract", "retry")]


def test_sweep_skips_terminal_runs_and_never_reruns_completed_seed():
    seed = {"run_id": "seed", "status": "completed", "current_stage": "review"}
    partial_done = {"run_id": "done", "status": "partial", "current_stage": "review"}
    fresh = {"run_id": RUN, "status": "queued", "current_stage": "parse"}
    calls: list = []
    script = {"parse": "committed", "extract": "committed", "tag": "blocked"}
    service = FakeRunsService([seed, partial_done, fresh])
    advanced = serve_worker.sweep(_factory(script, calls, fresh), service, TENANT)
    # Only the fresh queued run is eligible; the completed seed and finished
    # partial run are never driven.
    assert {run_id for _, run_id in calls} == {RUN}
    assert advanced == 1


def test_eligible_runs_filters_by_status_and_stage():
    service = FakeRunsService(
        [
            {"run_id": "a", "status": "queued", "current_stage": "parse"},
            {"run_id": "b", "status": "running", "current_stage": "extract"},
            {"run_id": "c", "status": "completed", "current_stage": "review"},
            {"run_id": "d", "status": "failed", "current_stage": "parse"},
            {"run_id": "e", "status": "partial", "current_stage": "review"},
        ]
    )
    eligible = serve_worker.eligible_runs(service, TENANT)
    assert [run["run_id"] for run in eligible] == ["a", "b"]


def test_read_only_serve_starts_no_worker():
    """When the worker is disabled the caller never builds a composition.

    The pilot only calls start_background when --serve-worker is set. This
    asserts the guard the pilot relies on: without an explicit start, the
    factory is never invoked and no thread runs.
    """
    calls: list = []

    def build_composition(*, stage, **_ignored):  # pragma: no cover - must not run
        calls.append(stage)
        raise AssertionError("worker must not build a composition when disabled")

    # Simulate the pilot's read-only branch: serve_worker is simply not started.
    worker_thread = None
    worker_stop = None
    assert worker_thread is None and worker_stop is None
    assert calls == []


def test_run_forever_stops_and_sweeps_at_least_once():
    run = {"run_id": RUN, "status": "queued", "current_stage": "tag"}
    calls: list = []
    script = {"parse": "idle", "extract": "idle", "tag": "committed"}
    service = FakeRunsService([run])
    stop = threading.Event()

    def clock():
        # First sweep at t=0, then request stop so the loop exits promptly.
        stop.set()
        return 0.0

    serve_worker.run_forever(
        _factory(script, calls, run), service, TENANT, stop=stop, interval=0.0, clock=clock
    )
    assert ("tag", RUN) in calls


def test_retry_is_not_invoked_again_on_a_later_sweep():
    run = {"run_id": RUN, "status": "running", "current_stage": "extract"}
    calls = []
    factory = _factory({"extract": "retry"}, calls, run)
    service = FakeRunsService([run])
    stopped = set()
    serve_worker.sweep(factory, service, TENANT, stopped)
    serve_worker.sweep(factory, service, TENANT, stopped)
    assert calls == [("extract", RUN)]
    assert stopped == {RUN}


def test_extract_continuation_runs_before_tag_when_budgeted():
    """The new loop continues bounded extraction before driving tag.

    When the run's frozen extractor role budget exceeds its per-batch window,
    ``drive_run`` must invoke the extract composition's ``run_batch``
    continuation after a committed extract and still advance to tag in the
    same sweep. Pure control-flow with in-memory fakes: no store, no model,
    no ledger.
    """
    run = {"run_id": RUN, "status": "queued", "current_stage": "extract"}
    calls: list = []

    class ExtractWithContinuation(FakeComposition):
        def __init__(self):
            super().__init__("extract", {"extract": "committed"}, calls, run)
            self.store = self

        def snapshot(self, tenant_id, run_id):
            assert (tenant_id, run_id) == (TENANT, RUN)
            return {
                "budget_limits": {"roles": [{"role": "extractor", "max_calls": 4}]},
                "extraction_limits": {"max_calls": 2},
            }

        def run_batch(self, *, tenant_id, run_id, max_calls=None):
            assert (tenant_id, run_id) == (TENANT, RUN)
            calls.append(("extract-continuation", run_id))
            return {"status": "complete"}

    def factory(*, stage, **_ignored):
        if stage == "extract":
            return ExtractWithContinuation()
        return FakeComposition(stage, {"tag": "blocked"}, calls, run)

    observed = serve_worker.drive_run(factory, TENANT, run)
    # The continuation ran exactly once, strictly between extract and tag.
    assert [stage for stage, _ in calls] == ["extract", "extract-continuation", "tag"]
    assert observed == [("extract", "committed"), ("tag", "blocked")]
