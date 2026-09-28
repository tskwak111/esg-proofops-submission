"""Focused R14 wiring: pilot NEW-run opt-in and resume pin for source-ID selection.

No model, network, ledger, or key access: only the pure argument/profile helpers
run here. The paid composition path (probe + shared ledger) is deliberately not
constructed offline.
"""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from dataclasses import asdict
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
        extraction_table_context=False,
        extraction_source_ids=False,
        extraction_assertion_prompt=False,
        tagging_max_calls=12,
        extraction_total_calls=None,
        max_calls=8,
    )
    base.update(overrides)
    return Namespace(**base)


def test_resume_restores_and_defaults_the_source_id_flag():
    restored = _args()
    apply_resume_metadata(
        restored, {"source_path": "/tmp/elsewhere.pdf", "extraction_source_ids": True}
    )
    assert restored.extraction_source_ids is True
    legacy = _args()
    apply_resume_metadata(legacy, {"source_path": "/tmp/elsewhere.pdf"})
    assert legacy.extraction_source_ids is False


def test_resume_cannot_add_source_id_selection_to_a_legacy_run(tmp_path, monkeypatch):
    """The flag is NEW-run only: adding it on --resume exits before any state work."""
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
            "--extraction-source-ids",
            "--key-file",
            str(tmp_path / "absent.key"),
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()


def test_the_new_run_settings_profile_matches_the_source_id_extractor():
    """What the pilot freezes is exactly what composition reconstructs."""
    from proofops_agent.upstage_extraction import _profile, _profile_with_options

    frozen = asdict(_profile_with_options("solar-pro3", source_ids=True))
    assert frozen != asdict(_profile("solar-pro3"))
    # Composition reads the same three booleans out of the settings file.
    settings = {"extraction_source_ids": True}
    assert frozen == asdict(
        _profile_with_options(
            "solar-pro3",
            year_notation=settings.get("extraction_year_notation") is True,
            extraction_context=settings.get("extraction_context") is True,
            extraction_table_context=settings.get("extraction_table_context") is True,
            source_ids=settings.get("extraction_source_ids") is True,
        )
    )


def test_analyze_report_passes_the_flag_through_to_the_pilot_argv():
    from scripts.analyze_report import build_pilot_argv

    common = dict(
        pdf=Path("/tmp/report.pdf"),
        pages=[1],
        claim_pages=None,
        report_year=2025,
        period_start="2025-01-01",
        period_end="2025-12-31",
        state=Path("/tmp/state"),
        key_file=Path("/tmp/key"),
        invoke=False,
        serve=False,
        port=8000,
    )
    assert "--extraction-source-ids" in build_pilot_argv(**common, extraction_source_ids=True)
    assert "--extraction-source-ids" not in build_pilot_argv(**common)


# ---------------------------------------------------------------------------
# R20 fix 2 wiring: assertion prompt is NEW-run only, requires source-ids, and
# the frozen settings profile must equal what composition reconstructs.
# ---------------------------------------------------------------------------


def test_resume_restores_and_defaults_the_assertion_prompt_flag():
    restored = _args()
    apply_resume_metadata(
        restored,
        {
            "source_path": "/tmp/elsewhere.pdf",
            "extraction_source_ids": True,
            "extraction_assertion_prompt": True,
        },
    )
    assert restored.extraction_assertion_prompt is True
    legacy = _args()
    apply_resume_metadata(legacy, {"source_path": "/tmp/elsewhere.pdf"})
    assert legacy.extraction_assertion_prompt is False


def test_assertion_prompt_requires_source_ids_at_the_pilot(tmp_path, monkeypatch):
    """--extraction-assertion-prompt without --extraction-source-ids exits before
    any state or paid work."""
    import evaluation.local_upstage_pilot as pilot

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_upstage_pilot",
            "--pdf",
            str(tmp_path / "r.pdf"),
            "--state",
            str(tmp_path / "run"),
            "--key-file",
            str(tmp_path / "absent.key"),
            "--pages",
            "1",
            "--report-year",
            "2025",
            "--period-start",
            "2025-01-01",
            "--period-end",
            "2025-12-31",
            "--extraction-assertion-prompt",
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()


def test_resume_cannot_add_the_assertion_prompt_to_a_legacy_run(tmp_path, monkeypatch):
    import evaluation.local_upstage_pilot as pilot

    state = tmp_path / "legacy-run"
    state.mkdir()
    (state / "pilot.json").write_text(
        json.dumps({"source_path": "/tmp/elsewhere.pdf", "extraction_source_ids": True})
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "local_upstage_pilot",
            "--resume",
            "--state",
            str(state),
            "--extraction-source-ids",
            "--extraction-assertion-prompt",
            "--key-file",
            str(tmp_path / "absent.key"),
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()


def test_the_new_run_assertion_profile_matches_what_composition_reconstructs():
    from proofops_agent.upstage_extraction import _profile_with_options

    frozen = asdict(_profile_with_options("solar-pro3", source_ids=True, assertion_prompt=True))
    assert frozen != asdict(_profile_with_options("solar-pro3", source_ids=True))
    settings = {"extraction_source_ids": True, "extraction_assertion_prompt": True}
    assert frozen == asdict(
        _profile_with_options(
            "solar-pro3",
            year_notation=settings.get("extraction_year_notation") is True,
            extraction_context=settings.get("extraction_context") is True,
            extraction_table_context=settings.get("extraction_table_context") is True,
            source_ids=settings.get("extraction_source_ids") is True,
            assertion_prompt=settings.get("extraction_assertion_prompt") is True,
        )
    )


def test_analyze_report_passes_the_assertion_flag_through_to_the_pilot_argv():
    from scripts.analyze_report import build_pilot_argv

    common = dict(
        pdf=Path("/tmp/report.pdf"),
        pages=[1],
        claim_pages=None,
        report_year=2025,
        period_start="2025-01-01",
        period_end="2025-12-31",
        state=Path("/tmp/state"),
        key_file=Path("/tmp/key"),
        invoke=False,
        serve=False,
        port=8000,
    )
    argv = build_pilot_argv(**common, extraction_source_ids=True, extraction_assertion_prompt=True)
    assert "--extraction-assertion-prompt" in argv
    assert "--extraction-assertion-prompt" not in build_pilot_argv(
        **common, extraction_source_ids=True
    )
