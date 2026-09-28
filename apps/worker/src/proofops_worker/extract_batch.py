"""Bounded extraction continuation command: process the remaining eligible sources.

The single-shot extract stage answers every source outside its bounded window
locally, with an ``unknown`` span, and commits. This command continues that run:
each invocation publishes one new immutable extract revision that model-processes
the next window of still-eligible sources and replays the receipts of the sources
an earlier revision already paid for. Earlier checkpoints stay byte-identical and
readable by their own job message; the run head advances to the newest revision.

It never reports a source as absent: every source is processed, pending,
unreadable, skipped as a non-paragraph kind, or excluded from the declared scope,
and the printed summary keeps those apart. A provider stop (for example an
exhausted budget) ends the batch, keeps the calls already charged, and leaves the
rest pending.

The composition owns the transport, credentials and budget ledger exactly as the
extract stage does; this module reads no environment of its own and makes no
provider call outside the composed extractor.

Example:

    LOCAL_EXTRACTION_MODE=upstage_probe .venv/bin/python -m proofops_worker.extract_batch \\
        --tenant-id <UUID> --run-id <UUID> --batches 2
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from uuid import UUID

from proofops.application.telemetry import SafeRuntimeFormatter

from proofops_worker.composition import build_composition
from proofops_worker.extract_runner import LocalExtractRunner

_STOP_STATUSES = frozenset(
    {"complete", "no_revision", "cancelled", "failed", "discarded", "deferred", "retry", "blocked"}
)


def run_batches(
    runner: LocalExtractRunner,
    *,
    tenant_id: str,
    run_id: str,
    batches: int,
    max_calls: int | None = None,
) -> list[dict]:
    """Run at most ``batches`` continuation batches, stopping on the first halt."""
    if type(batches) is not int or not 1 <= batches <= 100:
        raise ValueError("EXTRACTION_BATCH_COUNT_INVALID")
    if max_calls is not None and (type(max_calls) is not int or not 1 <= max_calls <= 20):
        raise ValueError("EXTRACTION_INPUT_INVALID")
    results = []
    for index in range(batches):
        summary = runner.run_batch(tenant_id=tenant_id, run_id=run_id, max_calls=max_calls)
        results.append(dict(summary, batch=index + 1))
        # Emit one flushed progress line per settled batch, before the next batch
        # runs, so a long run reports movement instead of buffering until complete.
        # Only whitelisted scalar telemetry is published: counts derive from the
        # scalar ``model_processed_after`` and the length of ``pending_after``;
        # source identifiers, arrays, paths and error text never appear here.
        processed_after = summary.get("model_processed_after")
        pending_after = summary.get("pending_after")
        print(
            json.dumps(
                {
                    "event": "extract_batch_progress",
                    "batch": index + 1,
                    "status": summary["status"],
                    "model_processed_count": (
                        processed_after if type(processed_after) is int else None
                    ),
                    "pending_count": (
                        len(pending_after) if isinstance(pending_after, list) else None
                    ),
                    "stop_code": summary.get("stop_code"),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
            flush=True,
        )
        if summary["status"] in _STOP_STATUSES or summary.get("stop_code"):
            break
    return results


def main() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeRuntimeFormatter(service="worker", env="local"))
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    arguments = argparse.ArgumentParser(
        description="Continue a run's bounded extraction in new immutable revisions"
    )
    arguments.add_argument("--tenant-id", required=True, type=UUID)
    arguments.add_argument("--run-id", required=True, type=UUID)
    arguments.add_argument(
        "--batches",
        type=int,
        default=1,
        help="Maximum continuation batches this invocation may publish",
    )
    arguments.add_argument(
        "--max-calls",
        type=int,
        default=None,
        help="Sources to model-process per batch; defaults to the run's frozen max_calls",
    )
    options = arguments.parse_args()
    runner = None
    try:
        runner = build_composition(stage="extract")
        if not isinstance(runner, LocalExtractRunner):
            raise ValueError("EXTRACTION_BATCH_REQUIRES_EXTRACT_STAGE")
        results = run_batches(
            runner,
            tenant_id=str(options.tenant_id),
            run_id=str(options.run_id),
            batches=options.batches,
            max_calls=options.max_calls,
        )
        # Only finite local status and source identifiers; never document text.
        print(json.dumps(results, sort_keys=True))
        if any(item["status"] in {"failed", "discarded"} for item in results):
            raise SystemExit(1)
    except Exception:
        logging.getLogger(__name__).error("extraction batch failed", exc_info=True)
        raise SystemExit(1) from None
    finally:
        if runner is not None:
            runner.uploads.close()
            runner.uploads.registry.close()


if __name__ == "__main__":
    main()
