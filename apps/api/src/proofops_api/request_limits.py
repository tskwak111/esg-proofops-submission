"""Bounded, process-local request limits for the local API composition."""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass

WINDOW_SECONDS = 60.0
READ_REQUESTS_PER_MINUTE = 120
WRITE_REQUESTS_PER_MINUTE = 10
LIVE_PROBES_PER_MINUTE = 2
_MAX_REQUESTS_PER_BUCKET = READ_REQUESTS_PER_MINUTE


def _retry_after(oldest: float, now: float) -> int:
    return max(1, min(math.ceil(WINDOW_SECONDS), math.ceil(WINDOW_SECONDS - (now - oldest))))


@dataclass(frozen=True, slots=True)
class RequestLimit:
    namespace: str
    identity: str
    operation_id: str
    requests: int

    def __post_init__(self) -> None:
        if not self.namespace or not self.identity or not self.operation_id:
            raise ValueError("request limit identity must be non-empty")
        if len(self.namespace) > 32 or len(self.identity) > 512 or len(self.operation_id) > 128:
            raise ValueError("request limit identity is too long")
        if not 1 <= self.requests <= _MAX_REQUESTS_PER_BUCKET:
            raise ValueError("request limit must be between 1 and 120")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.namespace, self.identity, self.operation_id


class LocalRequestLimiter:
    """Race-safe sliding windows with bounded memory and fail-closed capacity.

    This is deliberately process-local. Non-local/multi-replica deployments
    must replace it with durable atomic request control before serving traffic.
    """

    def __init__(self, *, max_buckets: int = 4096) -> None:
        if max_buckets < 1:
            raise ValueError("max_buckets must be positive")
        self._max_buckets = max_buckets
        self._buckets: dict[tuple[str, str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def consume(self, limits: tuple[RequestLimit, ...], *, now: float) -> int | None:
        """Atomically consume all limits, returning Retry-After or ``None``."""
        if not limits or isinstance(now, bool) or not isinstance(now, int | float):
            raise ValueError("limits and a finite timestamp are required")
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        keys = [limit.key for limit in limits]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate request limit bucket")

        with self._lock:
            for key in keys:
                self._prune(key, now)
            new_keys = sum(key not in self._buckets for key in keys)
            if len(self._buckets) + new_keys > self._max_buckets:
                self._prune_all(now)
            if len(self._buckets) + new_keys > self._max_buckets:
                oldest = min(
                    (bucket[0] for bucket in self._buckets.values()),
                    default=now,
                )
                return _retry_after(oldest, now)

            retry_after = 0
            for limit in limits:
                bucket = self._buckets.get(limit.key)
                if bucket is not None and len(bucket) >= limit.requests:
                    retry_after = max(retry_after, _retry_after(bucket[0], now))
            if retry_after:
                return retry_after
            for limit in limits:
                self._buckets.setdefault(limit.key, deque()).append(float(now))
            return None

    def _prune(self, key: tuple[str, str, str], now: float) -> None:
        bucket = self._buckets.get(key)
        if bucket is None:
            return
        cutoff = now - WINDOW_SECONDS
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if not bucket:
            del self._buckets[key]

    def _prune_all(self, now: float) -> None:
        for key in tuple(self._buckets):
            self._prune(key, now)
