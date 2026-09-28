"""Bounded, process-local OIDC state; explicit local composition only."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass

from proofops.adapters.local.auth_store import hash_token


@dataclass(frozen=True, slots=True, repr=False)
class PendingLogin:
    nonce: str
    verifier: str
    binding_hash: str
    return_to: str
    expires_at: float


class InMemoryOIDCStore:
    """Never use across API processes; restart invalidates all pending logins."""

    def __init__(self, *, capacity: int = 4096) -> None:
        if not 1 <= capacity <= 4096:
            raise ValueError("OIDC capacity must be between 1 and 4096")
        self.capacity = capacity
        self._pending: dict[str, PendingLogin] = {}
        self._codes: dict[str, float] = {}
        # ponytail: process-local lock; shared atomic TTL store before multiple API processes.
        self._lock = threading.Lock()

    def _prune(self, now: float) -> None:
        self._pending = {k: v for k, v in self._pending.items() if now < v.expires_at}
        self._codes = {k: v for k, v in self._codes.items() if now < v}

    def put(self, state: str, pending: PendingLogin, *, now: float) -> None:
        with self._lock:
            self._prune(now)
            if not now < pending.expires_at <= now + 600:
                raise ValueError("OIDC state lifetime must be at most ten minutes")
            if len(self._pending) >= self.capacity:
                raise RuntimeError("OIDC state capacity exceeded")
            key = hash_token(state)
            if key in self._pending:
                raise ValueError("OIDC state already exists")
            self._pending[key] = pending

    def consume(self, state: str, binding: str, *, now: float) -> PendingLogin | None:
        with self._lock:
            self._prune(now)
            key = hash_token(state)
            pending = self._pending.get(key)
            if pending is None or not secrets.compare_digest(
                pending.binding_hash, hash_token(binding)
            ):
                return None
            return self._pending.pop(key)

    def reserve_code(self, code: str, *, now: float) -> bool:
        with self._lock:
            self._prune(now)
            key = hash_token(code)
            if key in self._codes or len(self._codes) >= self.capacity:
                return False
            self._codes[key] = now + 600
            return True
