"""Measure the NFR-002 local profile without certifying a production SLO."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from math import ceil
from threading import Barrier
from time import perf_counter

READERS = 20
RUNS_PER_TENANT = 2
READ_P95_TARGET_SECONDS = 1.0
ACCEPT_P95_TARGET_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class SliMeasurement:
    environment: str
    read_count: int
    accept_count: int
    read_p95_seconds: float
    accept_p95_seconds: float
    read_status_codes: tuple[int, ...]
    accept_status_codes: tuple[int, ...]
    read_p95_target_seconds: float
    accept_p95_target_seconds: float
    certification_status: str = "not_certified"


def _timed(call: Callable[[], int]) -> tuple[int, float]:
    started = perf_counter()
    return call(), perf_counter() - started


def _p95(values: list[float]) -> float:
    return sorted(values)[ceil(len(values) * 0.95) - 1]


def exercise_load_profile(
    *,
    read: Callable[[], int],
    accept_run: Callable[[str], int],
    tenant_ids: tuple[str, ...],
    environment: str = "local_synthetic",
) -> SliMeasurement:
    """Run 20 reads and two run accepts per supplied tenant against real local boundaries."""
    if not tenant_ids or len(set(tenant_ids)) != len(tenant_ids):
        raise ValueError("tenant_ids must be non-empty and unique")
    if environment != "local_synthetic":
        raise ValueError("this profile records only explicit local synthetic measurements")
    accepts = [
        _timed(partial(accept_run, tenant_id))
        for tenant_id in tenant_ids
        for _ in range(RUNS_PER_TENANT)
    ]
    start = Barrier(READERS)

    def concurrent_read(_):
        start.wait(timeout=10)
        return _timed(read)

    with ThreadPoolExecutor(max_workers=READERS) as workers:
        reads = list(workers.map(concurrent_read, range(READERS)))
    return SliMeasurement(
        environment=environment,
        read_count=len(reads),
        accept_count=len(accepts),
        read_p95_seconds=_p95([elapsed for _, elapsed in reads]),
        accept_p95_seconds=_p95([elapsed for _, elapsed in accepts]),
        read_status_codes=tuple(status for status, _ in reads),
        accept_status_codes=tuple(status for status, _ in accepts),
        read_p95_target_seconds=READ_P95_TARGET_SECONDS,
        accept_p95_target_seconds=ACCEPT_P95_TARGET_SECONDS,
    )
