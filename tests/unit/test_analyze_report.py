"""Offline regression for the new-report launcher (scripts/analyze_report.py).

Covers the guards that must hold before any paid side effect: out-of-range page
selection, disordered/non-canonical dates, refusal to reuse any non-empty state
directory, and the default dry plan running NO subprocess and needing no secret.
A tiny in-memory PDF is written to a temp dir purely to exercise page validation;
no model or network calls occur.
"""

from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

import analyze_report as ar  # noqa: E402


def _make_pdf(path: Path, pages: int = 3) -> Path:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def _args(pdf, **overrides):
    base = dict(
        pdf=pdf,
        pages="1,2",
        claim_pages=None,
        auto_scope=False,
        auto_scope_max_pages=None,
        report_year=2024,
        period_start="2024-01-01",
        period_end="2024-12-31",
        state=None,
        key_file=Path("/unused/.env.upstage.local"),
        invoke=False,
        serve=False,
        port=8766,
        extraction_total_calls=None,
        tagging_max_calls=48,
        verify_selected_cells=False,
        native_quote_typography=False,
        live_relations=False,
        preliminary_context=False,
        ai_project_review=False,
        extraction_year_notation=False,
        extraction_context=False,
    )
    base.update(overrides)
    return Namespace(**base)


@pytest.mark.parametrize("enabled", [False, True])
def test_claim_typography_option_reaches_pilot_with_required_wrappers(pdf, tmp_path, enabled):
    args = ar.build_parser().parse_args(
        [
            "--pdf",
            str(pdf),
            "--pages",
            "1,2",
            "--report-year",
            "2024",
            "--period-start",
            "2024-01-01",
            "--period-end",
            "2024-12-31",
            "--state",
            str(tmp_path / "typography"),
        ]
        + (["--claim-span-typography"] if enabled else [])
    )
    plan = ar.plan_run(args)
    assert plan["claim_span_typography"] is enabled
    for flag in (
        "--claim-span-typography",
        "--claim-span-bullet-spacing",
        "--claim-span-render-resolution",
    ):
        assert (flag in plan["argv"]) is enabled


def _make_named_destination_pdf(path: Path, entries: list[tuple[str, int]]) -> Path:
    """Small real PDF with named destinations so ``evaluation.report_sections``
    can classify sections without any outline/TOC parsing edge cases.

    ``entries`` uses 1-based physical page numbers; pypdf's
    ``add_named_destination`` takes a 0-based page index, so it is converted here.
    """
    from pypdf import PdfWriter

    writer = PdfWriter()
    max_page = max(page for _, page in entries)
    for _ in range(max_page + 2):
        writer.add_blank_page(width=600, height=800)
    for title, page in entries:
        writer.add_named_destination(title, page - 1)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


@pytest.fixture()
def pdf(tmp_path):
    return _make_pdf(tmp_path / "report.pdf", pages=3)


def test_page_out_of_range_rejected(pdf, tmp_path):
    args = _args(pdf, pages="1,9", state=tmp_path / "run")
    with pytest.raises(ar.PlanError, match="exceed the document page count 3"):
        ar.plan_run(args)


def test_disordered_period_rejected(pdf, tmp_path):
    args = _args(pdf, period_start="2024-12-31", period_end="2024-01-01", state=tmp_path / "run")
    with pytest.raises(ar.PlanError, match="must not be after"):
        ar.plan_run(args)


def test_noncanonical_date_rejected(pdf, tmp_path):
    # date.fromisoformat accepts compact YYYYMMDD; the canonical guard must reject it.
    args = _args(pdf, period_start="20240101", state=tmp_path / "run")
    with pytest.raises(ar.PlanError, match="canonical"):
        ar.plan_run(args)


def test_nonempty_state_dir_not_overwritten(pdf, tmp_path):
    state = tmp_path / "existing"
    state.mkdir()
    (state / "unrelated.txt").write_text("keep me")
    args = _args(pdf, state=state)
    with pytest.raises(ar.PlanError, match="not empty"):
        ar.plan_run(args)


def test_invoke_requires_budget_ledger(pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "BUDGET_LEDGER", tmp_path / "missing" / "budget.sqlite3")
    args = _args(pdf, state=tmp_path / "run", invoke=True)
    with pytest.raises(ar.PlanError, match="budget ledger not found"):
        ar.plan_run(args)


def test_claim_pages_must_be_subset(pdf, tmp_path):
    # page 3 is not in --pages 1,2, so the narrowed claim scope is rejected.
    args = _args(pdf, pages="1,2", claim_pages="1,3", state=tmp_path / "run")
    with pytest.raises(ar.PlanError, match="subset"):
        ar.plan_run(args)


def test_claim_pages_subset_flows_into_pilot_argv(pdf, tmp_path):
    args = _args(pdf, pages="1,2,3", claim_pages="2", state=tmp_path / "run")
    plan = ar.plan_run(args)
    assert plan["claim_pages"] == [2]
    argv = plan["argv"]
    assert "--claim-pages" in argv
    assert argv[argv.index("--claim-pages") + 1] == "2"
    # Parsing/evidence scope (--pages) stays broad.
    assert argv[argv.index("--pages") + 1] == "1,2,3"


def test_absent_claim_pages_preserves_legacy_argv(pdf, tmp_path):
    args = _args(pdf, pages="1,2,3", state=tmp_path / "run")
    plan = ar.plan_run(args)
    assert plan["claim_pages"] is None
    assert "--claim-pages" not in plan["argv"]


def test_dry_plan_runs_no_subprocess(pdf, tmp_path, monkeypatch, capsys):
    state = tmp_path / "run"

    def _boom(*a, **k):  # pragma: no cover - must never run on the dry path
        raise AssertionError("dry plan must not execute a subprocess")

    monkeypatch.setattr(ar.subprocess, "run", _boom)
    argv = [
        "--pdf",
        str(pdf),
        "--pages",
        "2,3",
        "--report-year",
        "2024",
        "--period-start",
        "2024-01-01",
        "--period-end",
        "2024-12-31",
        "--state",
        str(state),
    ]
    assert ar.main(argv) == 0
    out = capsys.readouterr().out
    assert "evaluation.local_upstage_pilot" in out
    assert str(state) in out
    assert "DRY PLAN" in out
    # The state directory is not created on the dry path.
    assert not state.exists()


# --- --auto-scope: opt-in candidate discovery via evaluation.report_sections ---


@pytest.fixture()
def scoped_pdf(tmp_path):
    """Environment chapter, an ESG-data chapter, and an Appendix, plus a leading
    unclassified page so unknown pages are exercised too."""
    return _make_named_destination_pdf(
        tmp_path / "scoped.pdf",
        [("Environment", 2), ("ESG DATA", 5), ("Appendix", 7)],
    )


def test_auto_scope_rejects_explicit_pages(scoped_pdf, tmp_path):
    args = _args(scoped_pdf, auto_scope=True, pages="1,2", state=tmp_path / "run")
    with pytest.raises(ar.PlanError, match="explicit --pages"):
        ar.plan_run(args)


def test_auto_scope_rejects_explicit_claim_pages(scoped_pdf, tmp_path):
    args = _args(scoped_pdf, auto_scope=True, pages=None, claim_pages="2", state=tmp_path / "run")
    with pytest.raises(ar.PlanError, match="explicit --claim-pages"):
        ar.plan_run(args)


def test_auto_scope_makes_no_model_calls_and_stays_candidate_only(
    scoped_pdf, tmp_path, monkeypatch
):
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", tmp_path / "auto-scope")

    def _boom(*a, **k):  # pragma: no cover - dry auto-scope must never subprocess
        raise AssertionError("auto-scope dry plan must not execute a subprocess")

    monkeypatch.setattr(ar.subprocess, "run", _boom)
    args = _args(scoped_pdf, auto_scope=True, pages=None, state=tmp_path / "run")
    plan = ar.plan_run(args)
    proposal = plan["auto_scope_proposal"]
    assert proposal["status"] == "candidate_only"
    # Environment chapter (page 2..4) is the claim scope; ESG DATA/Appendix are evidence-only.
    assert proposal["claim_candidate_pages"] == [2, 3, 4]
    assert 5 in proposal["evidence_candidate_pages"] and 7 in proposal["evidence_candidate_pages"]
    # Page 1 precedes any classified anchor and must surface as unknown, not be dropped.
    assert 1 in proposal["unknown_pages"]
    assert plan["pages"] == proposal["evidence_candidate_pages"]
    assert plan["claim_pages"] == proposal["claim_candidate_pages"]
    # The state directory is never touched on the dry path.
    assert not plan["state"].exists()


def test_auto_scope_writes_reviewable_artifact(scoped_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", tmp_path / "auto-scope")
    args = _args(scoped_pdf, auto_scope=True, pages=None, state=tmp_path / "run")
    plan = ar.plan_run(args)
    artifact = plan["auto_scope_artifact"]
    assert artifact is not None and artifact.is_file()
    import json

    payload = json.loads(artifact.read_text())
    assert payload["proposal"]["status"] == "candidate_only"
    assert payload["applied"]["pages"] == plan["pages"]
    assert payload["applied"]["claim_pages"] == plan["claim_pages"]


def test_auto_scope_never_overwrites_existing_artifact_with_changed_content(
    scoped_pdf, tmp_path, monkeypatch
):
    scope_dir = tmp_path / "auto-scope"
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", scope_dir)
    args = _args(scoped_pdf, auto_scope=True, pages=None, state=tmp_path / "run")
    plan = ar.plan_run(args)
    artifact = plan["auto_scope_artifact"]
    original = artifact.read_text()
    # Re-running with the identical source/scope reuses the existing artifact untouched.
    plan_again = ar.plan_run(
        _args(scoped_pdf, auto_scope=True, pages=None, state=tmp_path / "run2")
    )
    assert plan_again["auto_scope_artifact"] == artifact
    assert artifact.read_text() == original
    # Tampering with the stored artifact must be detected and refused, not silently replaced.
    artifact.write_text(original.replace('"declared_subset": false', '"declared_subset": true'))
    with pytest.raises(ar.PlanError, match="already exists with different content"):
        ar.plan_run(_args(scoped_pdf, auto_scope=True, pages=None, state=tmp_path / "run3"))


def test_auto_scope_empty_candidates_do_not_default_to_whole_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", tmp_path / "auto-scope")
    # No named destinations at all and no large-heading matches: every page stays
    # unknown, so there is no E-narrative candidate at all.
    blank_pdf = _make_named_destination_pdf(tmp_path / "blank.pdf", [("Unfamiliar", 1)])
    args = _args(blank_pdf, auto_scope=True, pages=None, state=tmp_path / "run")
    with pytest.raises(ar.PlanError, match="no E-narrative candidate pages"):
        ar.plan_run(args)


def test_auto_scope_declares_full_scope_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", tmp_path / "auto-scope")
    # A wide Environment chapter well beyond the pilot's LLM call budgets
    # (EXTRACTION_MAX_CALLS/TAGGING_MAX_CALLS); those must NOT truncate pages.
    wide_pdf = _make_named_destination_pdf(
        tmp_path / "wide.pdf", [("Environment", 2), ("Appendix", 60)]
    )
    args = _args(wide_pdf, auto_scope=True, pages=None, state=tmp_path / "run")
    plan = ar.plan_run(args)
    proposal = plan["auto_scope_proposal"]
    applied = plan["auto_scope_applied"]
    assert len(proposal["claim_candidate_pages"]) > ar.EXTRACTION_MAX_CALLS
    assert len(proposal["evidence_candidate_pages"]) > ar.TAGGING_MAX_CALLS
    # Full proposed scope is used untouched; LLM call caps never truncate pages.
    assert plan["pages"] == proposal["evidence_candidate_pages"]
    assert plan["claim_pages"] == proposal["claim_candidate_pages"]
    assert applied["declared_subset"] is False
    assert applied["excluded_evidence_candidate_pages"] == []


def test_auto_scope_max_pages_must_keep_every_claim_candidate(scoped_pdf, tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", tmp_path / "auto-scope")
    args = _args(
        scoped_pdf,
        auto_scope=True,
        pages=None,
        auto_scope_max_pages=1,
        state=tmp_path / "run",
    )
    with pytest.raises(ar.PlanError, match="keep every claim-candidate page"):
        ar.plan_run(args)


def test_auto_scope_max_pages_declares_explicit_evidence_exclusion(
    scoped_pdf, tmp_path, monkeypatch
):
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", tmp_path / "auto-scope")
    args = _args(
        scoped_pdf,
        auto_scope=True,
        pages=None,
        auto_scope_max_pages=4,
        state=tmp_path / "run",
    )
    plan = ar.plan_run(args)
    proposal = plan["auto_scope_proposal"]
    applied = plan["auto_scope_applied"]
    # Every claim-candidate page is always kept.
    assert set(proposal["claim_candidate_pages"]) <= set(plan["pages"])
    assert len(plan["pages"]) == 4
    assert applied["declared_subset"] is True
    assert applied["excluded_evidence_candidate_pages"]
    assert set(plan["pages"]) < set(proposal["evidence_candidate_pages"])


def test_auto_scope_max_pages_requires_auto_scope_flag(pdf, tmp_path):
    argv = [
        "--pdf",
        str(pdf),
        "--pages",
        "1,2",
        "--auto-scope-max-pages",
        "5",
        "--report-year",
        "2024",
        "--period-start",
        "2024-01-01",
        "--period-end",
        "2024-12-31",
        "--state",
        str(tmp_path / "run"),
    ]
    with pytest.raises(SystemExit):
        ar.main(argv)


def test_auto_scope_claim_pages_pass_existing_frozen_validator(scoped_pdf, tmp_path, monkeypatch):
    """Integration proof: auto-scope-derived claim_pages satisfy the existing,
    unmodified ``claim_scope.validate_extraction_limits``/``claim_pages_for``
    contract exactly like a manually-selected subset would."""
    monkeypatch.setattr(ar, "AUTO_SCOPE_DIR", tmp_path / "auto-scope")
    args = _args(scoped_pdf, auto_scope=True, pages=None, state=tmp_path / "run")
    plan = ar.plan_run(args)

    from proofops.application.claim_scope import claim_pages_for, validate_extraction_limits

    selected_pages = plan["pages"]
    limits = {"max_calls": 8, "max_output_tokens": 512, "claim_pages": plan["claim_pages"]}
    validate_extraction_limits(limits, selected_pages)  # must not raise
    assert claim_pages_for(limits, selected_pages) == plan["claim_pages"]


def test_auto_scope_flag_present_in_cli_parser():
    parser = ar.build_parser()
    args = parser.parse_args(
        [
            "--pdf",
            "/tmp/does-not-matter.pdf",
            "--auto-scope",
            "--report-year",
            "2024",
            "--period-start",
            "2024-01-01",
            "--period-end",
            "2024-12-31",
        ]
    )
    assert args.auto_scope is True
    assert args.pages is None


def test_new_pilot_flags_flow_into_dry_argv_and_caps_validated(pdf, tmp_path):
    """New pilot passthroughs reach the delegated argv; finite caps are enforced.

    Dry plan only: no subprocess, no model calls, no budget ledger changes.
    Legacy defaults (batch 8 extraction / merged tables / 48 tagging) hold when
    the new flags are omitted.
    """
    legacy = ar.plan_run(_args(pdf, state=tmp_path / "legacy"))
    assert legacy["extraction_total_calls"] is None
    assert legacy["tagging_max_calls"] == 48
    assert "--verify-merged-tables" in legacy["argv"]
    assert "--verify-selected-cells" not in legacy["argv"]
    assert "--extraction-total-calls" not in legacy["argv"]

    args = _args(
        pdf,
        state=tmp_path / "run",
        extraction_total_calls=16,
        tagging_max_calls=12,
        verify_selected_cells=True,
        native_quote_typography=True,
        live_relations=True,
        preliminary_context=True,
        ai_project_review=True,
    )
    plan = ar.plan_run(args)
    argv = plan["argv"]
    assert argv[argv.index("--extraction-total-calls") + 1] == "16"
    assert argv[argv.index("--tagging-max-calls") + 1] == "12"
    # Selected-cells exclusively replaces the legacy merged-tables check.
    assert "--verify-selected-cells" in argv
    assert "--verify-merged-tables" not in argv
    assert "--native-quote-typography" in argv
    assert "--live-relations" in argv
    assert "--preliminary-context" in argv
    assert "--ai-project-review" in argv

    # The --evidence-relations alias maps to the pilot-exact --live-relations.
    parser = ar.build_parser()
    aliased = parser.parse_args(
        [
            "--pdf",
            str(pdf),
            "--pages",
            "1,2",
            "--report-year",
            "2024",
            "--period-start",
            "2024-01-01",
            "--period-end",
            "2024-12-31",
            "--state",
            str(tmp_path / "aliased"),
            "--evidence-relations",
        ]
    )
    assert aliased.live_relations is True
    aliased_plan = ar.plan_run(aliased)
    assert "--live-relations" in aliased_plan["argv"]

    # Finite caps fail closed: total below the batch of 8, above 2000, or a
    # tagging cap outside the pilot's 6..2000 bound never reaches argv.
    for bad_total in (7, 2001):
        with pytest.raises(ar.PlanError, match="extraction-total-calls"):
            ar.plan_run(_args(pdf, state=tmp_path / "run", extraction_total_calls=bad_total))
    for bad_tagging in (5, 2001):
        with pytest.raises(ar.PlanError, match="tagging-max-calls"):
            ar.plan_run(_args(pdf, state=tmp_path / "run", tagging_max_calls=bad_tagging))


def test_extraction_year_notation_is_opt_in_and_flows_into_dry_argv(pdf, tmp_path):
    """R03d: --extraction-year-notation defaults off and passes pilot-exact."""
    legacy = ar.plan_run(_args(pdf, state=tmp_path / "legacy"))
    assert legacy["extraction_year_notation"] is False
    assert "--extraction-year-notation" not in legacy["argv"]
    plan = ar.plan_run(_args(pdf, state=tmp_path / "run", extraction_year_notation=True))
    assert plan["extraction_year_notation"] is True
    assert "--extraction-year-notation" in plan["argv"]


def test_extraction_context_is_opt_in_and_flows_into_dry_argv(pdf, tmp_path):
    """R03f: --extraction-context defaults off and passes pilot-exact."""
    legacy = ar.plan_run(_args(pdf, state=tmp_path / "legacy"))
    assert legacy["extraction_context"] is False
    assert "--extraction-context" not in legacy["argv"]
    plan = ar.plan_run(_args(pdf, state=tmp_path / "run", extraction_context=True))
    assert plan["extraction_context"] is True
    assert "--extraction-context" in plan["argv"]
    capped = ar.plan_run(_args(pdf, state=tmp_path / "capped", tagging_max_calls=2000))
    assert capped["tagging_max_calls"] == 2000
    assert capped["argv"][capped["argv"].index("--tagging-max-calls") + 1] == "2000"


def test_explicit_parser_output_limit_is_frozen_in_new_run_argv(pdf, tmp_path):
    limit = 64 * 1024 * 1024
    plan = ar.plan_run(_args(pdf, state=tmp_path / "new", parser_max_output_bytes=limit))
    assert plan["argv"][plan["argv"].index("--parser-max-output-bytes") + 1] == str(limit)
    for invalid in (0, -1, True, 128 * 1024 * 1024 + 1):
        with pytest.raises(ar.PlanError, match="parser-max-output-bytes"):
            ar.plan_run(_args(pdf, state=tmp_path / "new", parser_max_output_bytes=invalid))


@pytest.mark.parametrize("auto_scope", [False, True])
def test_oversized_pdf_rejected_before_read_or_scope_discovery(tmp_path, monkeypatch, auto_scope):
    from proofops.application.uploads_security import PdfLimits

    source = tmp_path / "oversized.pdf"
    with source.open("wb") as stream:
        stream.truncate(PdfLimits().max_bytes + 1)

    def unexpected(*args, **kwargs):
        raise AssertionError("Oversized input must be refused before content processing")

    monkeypatch.setattr(ar, "_pdf_page_count", unexpected)
    monkeypatch.setattr(ar, "discover_auto_scope", unexpected)
    state = tmp_path / "new-state"
    with pytest.raises(ar.PlanError, match="104857600 bytes"):
        ar.plan_run(
            _args(source, auto_scope=auto_scope, pages=None if auto_scope else "1", state=state)
        )
    assert not state.exists()


def test_actor_role_option_reaches_current_profile_without_paid_defaults(pdf, tmp_path):
    args = ar.build_parser().parse_args([
        '--pdf', str(pdf), '--pages', '1', '--report-year', '2024',
        '--period-start', '2024-01-01', '--period-end', '2024-12-31',
        '--state', str(tmp_path / 'actor'), '--preliminary-actor-role',
    ])
    plan = ar.plan_run(args)
    for flag in ('--preliminary-context', '--preliminary-table-context',
                 '--preliminary-table-role', '--preliminary-goal-role',
                 '--preliminary-actor-role'):
        assert plan['argv'].count(flag) == 1
    assert plan['preliminary_actor_role'] is True
    assert plan['preliminary_context'] is True
    assert '--invoke' not in plan['argv']
    assert '--ai-project-review' not in plan['argv']
    assert not plan['state'].exists()
    from evaluation.local_upstage_pilot import live_tagging_settings
    settings = live_tagging_settings(
        48, preliminary_context=True,
        preliminary_table_context=True, preliminary_table_role=True,
        preliminary_goal_role=True, preliminary_actor_role=True,
    )
    assert settings['preliminary_settings']['model_profile'].endswith('actor-role-v2')
    assert settings['tagging_settings']['model_profile'].endswith('source-quotes-v4')


def test_direct_script_dry_plan_from_outside_checkout(pdf, tmp_path):
    import subprocess

    state = tmp_path / 'direct-run'
    result = subprocess.run([
        sys.executable, str(Path(ar.__file__).resolve()), '--pdf', str(pdf),
        '--pages', '1', '--report-year', '2024', '--period-start', '2024-01-01',
        '--period-end', '2024-12-31', '--state', str(state), '--preliminary-actor-role',
    ], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'DRY PLAN' in result.stdout and '--preliminary-actor-role' in result.stdout
    assert not state.exists()
