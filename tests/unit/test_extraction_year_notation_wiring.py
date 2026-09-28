"""Focused R03d wiring: pilot resume pin for --extraction-year-notation.

No model, network, ledger, or run side effects: the rejection path exits via
``parser.error`` before the pilot creates any state, and the restore half
exercises the pure ``apply_resume_metadata`` function only.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from evaluation.local_upstage_pilot import apply_resume_metadata  # noqa: E402


def _args(**overrides):
    base = dict(
        pdf=None,
        report_year=None,
        period_start=None,
        period_end=None,
        pages="1",
        claim_pages=None,
        model="solar-pro3",
        verify_paragraphs=False,
        verify_tables=False,
        verify_merged_tables=False,
        verify_selected_cells=False,
        native_quote_typography=False,
        repair_table_headers=False,
        verify_claim_spans=False,
        raster_ocr=False,
        live_tagging=False,
        live_relations=False,
        preliminary_context=False,
        extraction_year_notation=False,
        extraction_context=False,
        tagging_max_calls=12,
        extraction_total_calls=None,
        max_calls=8,
    )
    base.update(overrides)
    return Namespace(**base)


def test_resume_restores_saved_year_notation_flag():
    saved = {"source_path": "/tmp/elsewhere.pdf", "extraction_year_notation": True}
    args = _args()
    apply_resume_metadata(args, saved)
    assert args.extraction_year_notation is True


def test_resume_defaults_year_notation_off_for_legacy_runs():
    args = _args()
    apply_resume_metadata(args, {"source_path": "/tmp/elsewhere.pdf"})
    assert args.extraction_year_notation is False


def test_resume_cannot_add_year_notation_to_a_legacy_run(tmp_path, monkeypatch):
    """Explicit --extraction-year-notation with --resume on a legacy pilot.json
    fails closed before any state is created (no paid path, no mkdir)."""
    import evaluation.local_upstage_pilot as pilot

    state = tmp_path / "legacy-run"
    state.mkdir()
    (state / "pilot.json").write_text(json.dumps({"source_path": "/tmp/elsewhere.pdf"}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_upstage_pilot",
            "--resume",
            "--state",
            str(state),
            "--extraction-year-notation",
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()
    # Nothing was created or modified beside the manifest we wrote.
    assert sorted(p.name for p in state.iterdir()) == ["pilot.json"]


def test_resume_restores_saved_extraction_context_flag():
    saved = {"source_path": "/tmp/elsewhere.pdf", "extraction_context": True}
    args = _args()
    apply_resume_metadata(args, saved)
    assert args.extraction_context is True


def test_resume_cannot_add_extraction_context_to_a_legacy_run(tmp_path, monkeypatch):
    """Explicit --extraction-context with --resume on a legacy pilot.json
    fails closed before any state is created (no paid path, no mkdir)."""
    import evaluation.local_upstage_pilot as pilot

    state = tmp_path / "legacy-run"
    state.mkdir()
    (state / "pilot.json").write_text(json.dumps({"source_path": "/tmp/elsewhere.pdf"}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_upstage_pilot",
            "--resume",
            "--state",
            str(state),
            "--extraction-context",
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()
    assert sorted(p.name for p in state.iterdir()) == ["pilot.json"]
