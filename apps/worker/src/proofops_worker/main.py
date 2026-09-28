"""Explicit one-run local parser command with content-free runtime logging."""

from __future__ import annotations

import argparse
import logging
import sys
from uuid import UUID

from proofops.application.telemetry import SafeRuntimeFormatter

from proofops_worker.composition import build_composition
from proofops_worker.local_runner import LocalParserRunner


def main() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(SafeRuntimeFormatter(service="worker", env="local"))
    logging.basicConfig(handlers=[handler], level=logging.INFO, force=True)
    arguments = argparse.ArgumentParser(description="Execute one tenant/run parser delivery")
    arguments.add_argument("--tenant-id", required=True, type=UUID)
    arguments.add_argument("--run-id", required=True, type=UUID)
    arguments.add_argument("--once", required=True, action="store_true")
    arguments.add_argument("--stage", choices=("parse", "extract", "tag"), default="parse")
    arguments.add_argument(
        "--note-review-artifact",
        action="append",
        default=[],
        metavar="FILE",
        help="Canonical source-bound note artifact; repeat for pages, parse stage only",
    )
    arguments.add_argument(
        "--review-table-notes",
        action="store_true",
        help="Automatically review tables with the shared authorized Upstage budget",
    )
    arguments.add_argument(
        "--verify-paragraphs",
        action="store_true",
        help="Verify native paragraph text against rendered PDF; parse stage only",
    )
    arguments.add_argument(
        "--native-quote-typography",
        action="store_true",
        help="Use versioned quote typography comparison; requires --verify-paragraphs",
    )
    arguments.add_argument(
        "--raster-ocr",
        action="store_true",
        help=(
            "Use authorized raster OCR fallback; requires native paragraph verification "
            "and parse stage"
        ),
    )
    options = arguments.parse_args()
    runner = None
    try:
        if options.verify_paragraphs and options.stage != "parse":
            raise ValueError("NATIVE_PARAGRAPHS_REQUIRE_PARSE_STAGE")
        if options.raster_ocr and (not options.verify_paragraphs or options.stage != "parse"):
            raise ValueError("RASTER_OCR_REQUIRE_NATIVE_PARSE_STAGE")
        if options.review_table_notes and options.note_review_artifact:
            raise ValueError("NOTE_REVIEW_INPUT_MODE_CONFLICT")
        if (
            options.review_table_notes or options.note_review_artifact
        ) and options.stage != "parse":
            raise ValueError("NOTE_REVIEWS_REQUIRE_PARSE_STAGE")
        artifacts = []
        for path in options.note_review_artifact:
            with open(path, "rb") as handle:
                raw = handle.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise ValueError("NOTE_REVIEW_ARTIFACT_TOO_LARGE")
            artifacts.append(raw.decode("utf-8"))
        runner = build_composition(
            stage=options.stage,
            review_table_notes=options.review_table_notes,
            verify_paragraphs=options.verify_paragraphs,
            native_typography_tolerance=options.native_quote_typography,
            raster_ocr=options.raster_ocr,
        )
        if artifacts:
            if not isinstance(runner, LocalParserRunner):
                raise ValueError("NOTE_REVIEWS_REQUIRE_PARSE_STAGE")
            result = runner.run_once(
                tenant_id=str(options.tenant_id),
                run_id=str(options.run_id),
                note_review_artifacts=tuple(artifacts),
            )
        else:
            result = runner.run_once(tenant_id=str(options.tenant_id), run_id=str(options.run_id))
        # Only our finite local delivery status is exposed, never parser/customer payloads.
        print(result)
        if result in {"failed", "discarded"}:
            raise SystemExit(1)
    except Exception:
        logging.getLogger(__name__).error("worker operation failed", exc_info=True)
        raise SystemExit(1) from None
    finally:
        if runner is not None:
            runner.uploads.close()
            runner.uploads.registry.close()


if __name__ == "__main__":
    main()
