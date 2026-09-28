"""Opt-in same-process local worker loop for the submission pilot's ``--serve``.

This drives *newly queued* runs that the web ``/documents/new`` + RunForm create
against the already-authorized local tenant. It reuses the existing durable job
store, snapshots, and ``build_composition(stage=...).run_once(...)`` exactly as
the one-shot ``--invoke`` path does; it introduces no new stage logic, no new
budget allowance, and no retry of failed external calls.

Guards preserved:

* Paid consent is the caller's explicit ``--serve-worker`` flag, distinct from
  the read-only ``--resume`` re-serve path. The loop is only started when that
  flag is set; ``worker_enabled`` in ``/local/submission`` mirrors it.
* Scope is the single local tenant passed in. No tenant discovery.
* Terminal outcomes (``failed``/``discarded``/``cancelled``) stop that run and
  are never re-invoked. ``retry`` is stopped for the served session and is
  never automatically retried. ``deferred`` means another lease owns the state, so the
  loop yields it rather than competing.
* An already-terminal run (the completed demo seed, or a finished run) is never
  re-processed: only ``queued``/``running`` runs whose current stage is still
  parse/extract/tag are eligible.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from proofops_worker.extract_batch import run_batches
from proofops_worker.extract_runner import _extractor_role_budget

LOGGER = logging.getLogger(__name__)

# Stages driven, in dependency order. The loop only calls the stage matching a
# run's current stage; earlier stages are skipped because their outbox is empty.
_STAGES = ("parse", "extract", "tag")

# Outcomes that mean the run made forward progress and should be looked at again
# on the next stage in the same sweep.
_ADVANCED = {"committed"}

# Outcomes that end this run for this sweep without re-invoking anything. A
# ``retry`` is durably re-enqueued with backoff by the job store, so re-invoking
# it here would spin/burn budget; we deliberately stop instead.
_TERMINAL = {"failed", "discarded", "cancelled", "retry"}


def _continue_extraction_if_budgeted(runner: Any, tenant_id: str, run_id: str) -> None:
    store = getattr(runner, "store", None)
    if store is None or not callable(getattr(runner, "run_batch", None)):
        return
    try:
        snapshot = store.snapshot(tenant_id, run_id)
        role_budget = _extractor_role_budget(snapshot)
        per_batch = (snapshot.get("extraction_limits") or {}).get("max_calls", 1)
        if role_budget is not None and role_budget > per_batch:
            batches = min(100, max(1, (role_budget + per_batch - 1) // per_batch))
            run_batches(runner, tenant_id=tenant_id, run_id=run_id, batches=batches)
    except Exception:
        LOGGER.error("extraction continuation failed for %s", run_id, exc_info=True)


def eligible_runs(runs_service, tenant_id: str) -> list[dict]:
    """Return newly queued/running runs for one tenant, oldest first.

    Uses the existing paginated ``RunService.list`` read model, so it inherits
    tenant scoping and soft-deletion filtering. Terminal runs (partial after a
    completed tag stage, completed, failed, cancelled) are excluded so a finished
    seed is never re-driven.
    """
    eligible: list[dict] = []
    cursor = None
    while True:
        page = runs_service.list(tenant_id, cursor=cursor, limit=100)
        for run in page["items"]:
            if (
                run["status"] in {"queued", "running"}
                and run.get("current_stage", "parse") in _STAGES
            ):
                eligible.append(run)
        cursor = page.get("next_cursor")
        if not cursor:
            break
    return eligible


def drive_run(build_composition, tenant_id: str, run: dict) -> list[tuple[str, str]]:
    """Drive one run from its current stage forward until it stops advancing.

    Returns the ``(stage, outcome)`` pairs observed. Each stage builds its own
    fenced composition and calls ``run_once`` once; a non-advancing outcome ends
    the sweep for this run so a single caller never loops on the same state.
    """
    run_id = run["run_id"]
    start = run.get("current_stage", "parse")
    remaining = _STAGES[_STAGES.index(start) :] if start in _STAGES else ()
    observed: list[tuple[str, str]] = []
    for stage in remaining:
        composition = build_composition(stage=stage)
        try:
            outcome = composition.run_once(tenant_id=tenant_id, run_id=run_id)
            if stage == "extract" and outcome in _ADVANCED:
                _continue_extraction_if_budgeted(composition, tenant_id, run_id)
        finally:
            composition.uploads.close()
            composition.uploads.registry.close()
        observed.append((stage, outcome))
        if outcome in _TERMINAL:
            break
        if outcome not in _ADVANCED:
            # idle / deferred / pending_downstream / blocked / a tag stage_status:
            # nothing more this worker should do in this sweep.
            break
    return observed


def sweep(build_composition, runs_service, tenant_id: str, stopped_runs=None) -> int:
    """One pass over all currently-eligible runs. Returns runs advanced."""
    stopped_runs = set() if stopped_runs is None else stopped_runs
    advanced = 0
    for run in eligible_runs(runs_service, tenant_id):
        if run["run_id"] in stopped_runs:
            continue
        try:
            observed = drive_run(build_composition, tenant_id, run)
        except Exception:  # pragma: no cover - defensive; one bad run must not kill loop
            LOGGER.error("worker sweep stopped for a run", exc_info=True)
            stopped_runs.add(run["run_id"])
            continue
        if any(outcome in _TERMINAL for _, outcome in observed):
            stopped_runs.add(run["run_id"])
        if any(outcome in _ADVANCED for _, outcome in observed):
            advanced += 1
    return advanced


def run_forever(
    build_composition,
    runs_service,
    tenant_id: str,
    *,
    stop: threading.Event,
    interval: float = 2.0,
    clock: Callable[[], float] = time.monotonic,
    stopped_runs=None,
) -> None:
    """Poll for newly queued runs until ``stop`` is set.

    Retry/error runs are excluded across all later sweeps of this session.
    """
    stopped_runs = set() if stopped_runs is None else stopped_runs
    LOGGER.info("local serve-worker started for one tenant")
    while not stop.is_set():
        started = clock()
        sweep(build_composition, runs_service, tenant_id, stopped_runs)
        elapsed = clock() - started
        stop.wait(max(0.0, interval - elapsed))
    LOGGER.info("local serve-worker stopped")


def start_background(
    build_composition, runs_service, tenant_id: str, *, interval: float = 2.0
) -> tuple[threading.Thread, threading.Event]:
    """Start the loop in a daemon thread; return ``(thread, stop_event)``.

    The pilot starts this immediately before ``uvicorn.run`` blocks and sets the
    stop event on shutdown so no worker outlives the served process.
    """
    # Existing unfinished runs require a new explicit analysis, not a paid retry on restart.
    existing_runs = {run["run_id"] for run in eligible_runs(runs_service, tenant_id)}
    stop = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(build_composition, runs_service, tenant_id),
        kwargs=dict(stop=stop, interval=interval, stopped_runs=existing_runs),
        name="local-serve-worker",
        daemon=True,
    )
    thread.start()
    return thread, stop
