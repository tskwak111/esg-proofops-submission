import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from evaluation.local_upstage_pilot import apply_resume_metadata


def _args():
    class Args:
        pdf = None
        pages = None
        claim_pages = None
        report_year = None
        period_start = None
        period_end = None
        model = "solar-pro3"
        extraction_source_ids = False
        extraction_assertion_prompt = False
        extraction_complete_selection = False
        extraction_content_bounds = False
        native_quote_typography = False
        extraction_year_notation = False
        extraction_context = False
        extraction_table_context = False
        verify_claim_spans = False
        verify_claim_domains = False
        verify_claim_structure = False
        repair_table_headers = False

    return Args()


# ---------------------------------------------------------------------------
# Profile constraints
# ---------------------------------------------------------------------------


def test_profile_content_bounds_requires_source_ids():
    from proofops_agent.upstage_extraction import _profile_with_options

    with pytest.raises(ValueError, match="UPSTAGE_PROFILE_OPTION_INVALID"):
        _profile_with_options("solar-pro3", extraction_content_bounds=True)


# ---------------------------------------------------------------------------
# Extractor construction
# ---------------------------------------------------------------------------


def test_extractor_content_bounds_requires_source_ids(tmp_path):
    from unittest.mock import MagicMock

    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    probe = MagicMock()
    probe.complete = MagicMock()
    probe.model = "solar-pro3"

    with pytest.raises(ValueError):
        UpstageClaimExtractor(
            probe,
            str(tmp_path),
            extraction_content_bounds=True,
        )


def test_extractor_content_bounds_valid_construction(tmp_path):
    from unittest.mock import MagicMock

    from proofops_agent.upstage_extraction import UpstageClaimExtractor, _profile_with_options

    probe = MagicMock()
    probe.complete = MagicMock()
    probe.model = "solar-pro3"

    extractor = UpstageClaimExtractor(
        probe,
        str(tmp_path),
        extraction_source_ids=True,
        extraction_content_bounds=True,
    )
    expected = _profile_with_options(
        "solar-pro3",
        source_ids=True,
        extraction_content_bounds=True,
    )
    assert extractor.profile == expected


# ---------------------------------------------------------------------------
# Pilot resume
# ---------------------------------------------------------------------------


def test_resume_restores_and_defaults_the_content_bounds_flag():
    restored = _args()
    apply_resume_metadata(
        restored,
        {
            "source_path": "/tmp/elsewhere.pdf",
            "extraction_source_ids": True,
            "extraction_content_bounds": True,
        },
    )
    assert restored.extraction_content_bounds is True

    legacy = _args()
    apply_resume_metadata(legacy, {"source_path": "/tmp/elsewhere.pdf"})
    assert legacy.extraction_content_bounds is False


def test_resume_cannot_add_content_bounds_to_a_legacy_run(tmp_path, monkeypatch, capsys):
    import evaluation.local_upstage_pilot as pilot

    state = tmp_path / "legacy-run"
    state.mkdir()
    (state / "pilot.json").write_text(
        json.dumps(
            {
                "source_path": "/tmp/elsewhere.pdf",
                "extraction_source_ids": True,
            }
        )
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
            "--extraction-content-bounds",
            "--key-file",
            str(tmp_path / "absent.key"),
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()
    err = str(capsys.readouterr().err)
    assert "--resume cannot add extraction content bounds; create a new run" in err


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def test_the_new_run_content_bounds_profile_matches_what_composition_reconstructs():
    from proofops_agent.upstage_extraction import _profile_with_options

    frozen = asdict(
        _profile_with_options(
            "solar-pro3",
            source_ids=True,
            extraction_content_bounds=True,
        )
    )
    assert frozen != asdict(_profile_with_options("solar-pro3", source_ids=True))
    settings = {
        "extraction_source_ids": True,
        "extraction_content_bounds": True,
    }
    assert frozen == asdict(
        _profile_with_options(
            "solar-pro3",
            year_notation=settings.get("extraction_year_notation") is True,
            extraction_context=settings.get("extraction_context") is True,
            extraction_table_context=settings.get("extraction_table_context") is True,
            source_ids=settings.get("extraction_source_ids") is True,
            assertion_prompt=settings.get("extraction_assertion_prompt") is True,
            complete_selection=settings.get("extraction_complete_selection") is True,
            extraction_content_bounds=settings.get("extraction_content_bounds") is True,
        )
    )


def test_analyze_report_passes_the_content_bounds_flag_through_to_the_pilot_argv():
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
    argv = build_pilot_argv(
        **common,
        extraction_source_ids=True,
        extraction_content_bounds=True,
    )
    assert "--extraction-content-bounds" in argv
    assert "--extraction-content-bounds" not in build_pilot_argv(
        **common, extraction_source_ids=True
    )
