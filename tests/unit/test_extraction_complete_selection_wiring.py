"""Focused R34 wiring: pilot NEW-run opt-in and resume pin for complete-selection prompt.

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
        preliminary_table_context=False,
        preliminary_table_role=False,
        extraction_year_notation=False,
        extraction_context=False,
        extraction_table_context=False,
        extraction_source_ids=False,
        extraction_assertion_prompt=False,
        extraction_complete_selection=False,
        claim_span_render_resolution=False,
        claim_span_bullet_spacing=False,
        claim_span_typography=False,
        tagging_max_calls=12,
        extraction_total_calls=None,
        max_calls=8,
    )
    base.update(overrides)
    return Namespace(**base)


# ---------------------------------------------------------------------------
# Prompt constant: the additive suffix must be byte-identical to the tested wire
# ---------------------------------------------------------------------------

_EXPECTED_COMPLETE_SELECTION_SUFFIX = (
    " Evaluate EVERY supplied sentence independently and return ALL qualifying sentence_ids,"
    " not just the first or most prominent claim."
    " A paragraph may contain several claims."
    " A literal company-specific statement that it performs climate scenario analysis,"
    " and a stated finding of that analysis, can qualify even when it concerns a"
    " conditional future financial impact of an environmental transition."
    " Do not treat a modeled finding as a realized environmental improvement;"
    " this step only selects statements for later review."
    " Resolve '이에', '분석 결과', and similar references using the"
    " supplied paragraph's own sentences without inventing content."
    " General risk descriptions, topic headings and chart axes still do not qualify"
    " merely because a nearby sentence is a claim."
)


def test_complete_selection_suffix_constant_matches_probe_wire():
    """COMPLETE_SELECTION_SYSTEM_SUFFIX must equal the exact bytes tested in the probe."""
    from proofops_agent.upstage_extraction import COMPLETE_SELECTION_SYSTEM_SUFFIX

    assert COMPLETE_SELECTION_SYSTEM_SUFFIX == _EXPECTED_COMPLETE_SELECTION_SUFFIX
    from proofops.domain.provenance import canonical_hash

    assert (
        canonical_hash(COMPLETE_SELECTION_SYSTEM_SUFFIX)
        == "e450b6d977e17229b3f62d87520dfadc2cd318fa3aad346edab90357b610a036"
    )


# ---------------------------------------------------------------------------
# Profile: complete_selection produces a distinct hash; legacy hashes unchanged
# ---------------------------------------------------------------------------


def test_complete_selection_profile_differs_from_assertion_only():
    from proofops_agent.upstage_extraction import _profile_with_options

    assertion_only = asdict(
        _profile_with_options("solar-pro3", source_ids=True, assertion_prompt=True)
    )
    complete_selection = asdict(
        _profile_with_options(
            "solar-pro3",
            source_ids=True,
            assertion_prompt=True,
            complete_selection=True,
        )
    )
    assert assertion_only != complete_selection


def test_legacy_source_id_prompt_hash_unchanged():
    """complete_selection=False must never change the existing assertion hash."""
    from proofops_agent.upstage_extraction import _profile_with_options

    before = asdict(_profile_with_options("solar-pro3", source_ids=True, assertion_prompt=True))
    # Confirm that a call with the new kwarg=False is identical
    after = asdict(
        _profile_with_options(
            "solar-pro3",
            source_ids=True,
            assertion_prompt=True,
            complete_selection=False,
        )
    )
    assert before == after
    assert (
        before["prompt_sha256"]
        == "0d9b97d259e63195a3baa215e14b65139bbce1703f82308c64f48a400d7ea982"
    )
    assert (
        before["rule_sha256"] == "aaaa221311da5fcc2eae2363e31c4a9a7434b5920820f840f9e6213a6a674890"
    )


def test_complete_selection_requires_source_ids():
    """complete_selection requires source_ids; enabling it alone raises ValueError."""
    from proofops_agent.upstage_extraction import _profile_with_options

    with pytest.raises(ValueError, match="UPSTAGE_PROFILE_OPTION_INVALID"):
        _profile_with_options("solar-pro3", complete_selection=True)


def test_complete_selection_requires_assertion_prompt():
    """complete_selection requires assertion_prompt; enabling it without that raises ValueError."""
    from proofops_agent.upstage_extraction import _profile_with_options

    with pytest.raises(ValueError, match="UPSTAGE_PROFILE_OPTION_INVALID"):
        _profile_with_options("solar-pro3", source_ids=True, complete_selection=True)


# ---------------------------------------------------------------------------
# Extractor construction: invalid dependency raises before any call
# ---------------------------------------------------------------------------


def test_extractor_complete_selection_requires_assertion_prompt(tmp_path):
    """UpstageClaimExtractor with complete_selection but not assertion_prompt fails closed."""
    from unittest.mock import MagicMock

    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    probe = MagicMock()
    probe.complete = MagicMock()
    probe.model = "solar-pro3"

    with pytest.raises(ValueError, match="UPSTAGE_EXTRACTION_COMPLETE_SELECTION_INVALID"):
        UpstageClaimExtractor(
            probe,
            str(tmp_path),
            extraction_source_ids=True,
            extraction_complete_selection=True,
            # assertion_prompt deliberately omitted (False)
        )


def test_extractor_complete_selection_without_source_ids_fails(tmp_path):
    """UpstageClaimExtractor with complete_selection but not source_ids fails closed."""
    from unittest.mock import MagicMock

    from proofops_agent.upstage_extraction import UpstageClaimExtractor

    probe = MagicMock()
    probe.complete = MagicMock()
    probe.model = "solar-pro3"

    with pytest.raises(ValueError):
        UpstageClaimExtractor(
            probe,
            str(tmp_path),
            extraction_complete_selection=True,
        )


def test_extractor_complete_selection_valid_construction(tmp_path):
    """UpstageClaimExtractor with complete_selection, source_ids, assertion_prompt constructs."""
    from unittest.mock import MagicMock

    from proofops_agent.upstage_extraction import UpstageClaimExtractor, _profile_with_options

    probe = MagicMock()
    probe.complete = MagicMock()
    probe.model = "solar-pro3"

    extractor = UpstageClaimExtractor(
        probe,
        str(tmp_path),
        extraction_source_ids=True,
        extraction_assertion_prompt=True,
        extraction_complete_selection=True,
    )
    expected = _profile_with_options(
        "solar-pro3",
        source_ids=True,
        assertion_prompt=True,
        complete_selection=True,
    )
    assert extractor.profile == expected


# ---------------------------------------------------------------------------
# System prompt: suffix appended only when complete_selection=True
# ---------------------------------------------------------------------------


def test_system_prompt_includes_complete_selection_suffix():
    from proofops_agent.upstage_extraction import (
        COMPLETE_SELECTION_SYSTEM_SUFFIX,
        _system_prompt,
    )

    prompt = _system_prompt(
        extraction_context=False,
        extraction_table_context=False,
        source_ids=True,
        assertion_prompt=True,
        complete_selection=True,
    )
    assert prompt.endswith(COMPLETE_SELECTION_SYSTEM_SUFFIX)


def test_system_prompt_does_not_include_complete_selection_suffix_by_default():
    from proofops_agent.upstage_extraction import (
        COMPLETE_SELECTION_SYSTEM_SUFFIX,
        _system_prompt,
    )

    prompt = _system_prompt(
        extraction_context=False,
        extraction_table_context=False,
        source_ids=True,
        assertion_prompt=True,
        complete_selection=False,
    )
    assert not prompt.endswith(COMPLETE_SELECTION_SYSTEM_SUFFIX)


# ---------------------------------------------------------------------------
# Pilot resume: restores and pins the flag
# ---------------------------------------------------------------------------


def test_resume_restores_and_defaults_the_complete_selection_flag():
    restored = _args()
    apply_resume_metadata(
        restored,
        {
            "source_path": "/tmp/elsewhere.pdf",
            "extraction_source_ids": True,
            "extraction_assertion_prompt": True,
            "extraction_complete_selection": True,
        },
    )
    assert restored.extraction_complete_selection is True

    legacy = _args()
    apply_resume_metadata(legacy, {"source_path": "/tmp/elsewhere.pdf"})
    assert legacy.extraction_complete_selection is False


def test_resume_cannot_add_complete_selection_to_a_legacy_run(tmp_path, monkeypatch, capsys):
    """The flag is NEW-run only: adding it on --resume exits before any state work."""
    import evaluation.local_upstage_pilot as pilot

    state = tmp_path / "legacy-run"
    state.mkdir()
    (state / "pilot.json").write_text(
        json.dumps(
            {
                "source_path": "/tmp/elsewhere.pdf",
                "extraction_source_ids": True,
                "extraction_assertion_prompt": True,
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
            "--extraction-assertion-prompt",
            "--extraction-complete-selection",
            "--key-file",
            str(tmp_path / "absent.key"),
        ],
    )
    with pytest.raises(SystemExit):
        pilot.main()
    assert "--resume cannot add extraction complete selection" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Pilot settings: complete_selection profile matches what composition reconstructs
# ---------------------------------------------------------------------------


def test_the_new_run_complete_selection_profile_matches_what_composition_reconstructs():
    """What the pilot freezes is exactly what composition reconstructs."""
    from proofops_agent.upstage_extraction import _profile_with_options

    frozen = asdict(
        _profile_with_options(
            "solar-pro3",
            source_ids=True,
            assertion_prompt=True,
            complete_selection=True,
        )
    )
    assert frozen != asdict(
        _profile_with_options("solar-pro3", source_ids=True, assertion_prompt=True)
    )
    # Composition reads these booleans out of the settings file.
    settings = {
        "extraction_source_ids": True,
        "extraction_assertion_prompt": True,
        "extraction_complete_selection": True,
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
        )
    )


def test_analyze_report_passes_the_complete_selection_flag_through_to_the_pilot_argv():
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
        extraction_assertion_prompt=True,
        extraction_complete_selection=True,
    )
    assert "--extraction-complete-selection" in argv
    assert "--extraction-complete-selection" not in build_pilot_argv(
        **common, extraction_source_ids=True, extraction_assertion_prompt=True
    )
