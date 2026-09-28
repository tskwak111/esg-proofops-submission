"""At-least-once outbox delivery; duplicate queue messages are fenced by consumer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from proofops.application.ports.jobs import JobRepository


def relay_outbox(
    store: JobRepository,
    tenant_id: str,
    run_id: str,
    *,
    now: int,
    publish: Callable[[dict[str, Any]], None],
) -> int:
    sent = 0
    for event in store.pending_outbox(tenant_id, run_id, now=now):
        try:
            publish(event)
        except (OSError, TimeoutError):
            store.mark_outbox(
                tenant_id,
                run_id,
                event["event_id"],
                now=now,
                sent=False,
                expected_attempts=event["attempts"],
            )
        else:
            sent += store.mark_outbox(
                tenant_id,
                run_id,
                event["event_id"],
                now=now,
                sent=True,
                expected_attempts=event["attempts"],
            )
    return sent
