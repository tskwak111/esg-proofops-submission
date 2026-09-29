"""Smallest usable launcher to analyze a NEW report PDF via the local pilot.

Thin front-end over ``evaluation.local_upstage_pilot``; it does not re-implement
the pipeline. It validates inputs and prints the exact delegated ``argv`` so an
operator can point the reviewed pilot policy at a fresh PDF beyond the demo state.

Guards enforced before any paid side effect:
* ``--pdf`` exists and every 1-based page is within the actual page count.
* ``--report-year`` is inside the app's supported range (matches VersionCreate).
* ``--period-start``/``--period-end`` are canonical ``YYYY-MM-DD`` dates, ordered.
* A new run refuses ANY non-empty state directory (never overwrites stored data).
* ``--invoke`` requires the shared budget ledger to already exist; we never mint
  or reset it. The dry plan needs no secrets and runs no subprocess.

Delegation reuses the reviewed pilot flags: verify-paragraphs, verify-claim-spans,
one of verify-merged-tables (legacy default) / --verify-selected-cells,
repair-table-headers, model ``solar-pro3``, extraction ``--max-calls 8`` with an
optional ``--extraction-total-calls`` cap, ``--live-tagging --tagging-max-calls 48``
with optional ``--native-quote-typography``, ``--live-relations`` (aliased as
``--evidence-relations``), ``--preliminary-context``, and ``--ai-project-review``, plus opt-in
``--extraction-year-notation`` (off by default).
``--serve`` is a separate explicit opt-in (never auto-enabled) to avoid hanging
an integration run. New-run caps reuse the pilot's own validators
(``extraction_budget_settings`` for the batch 1..20 / total ..2000 bound,
6..2000 for tagging); no budget framework is duplicated here and the shared
USD20 ledger ceiling is unchanged.

Optional ``--auto-scope`` (opt-in, off by default): instead of a manual
``--pages``, discover E-narrative / environmental-Data / Appendix candidate
pages with the existing offline, source-bound, no-model-call section study in
``evaluation.report_sections``. This produces a *proposal*, never an approved
scope: unknown/conflict pages are surfaced, not silently dropped or folded
into "full report"; an empty or all-unknown candidate set refuses to fall
back to the whole PDF. By default the FULL proposed scope is fed forward;
``--auto-scope-max-pages`` is the only way to declare an explicit, reported
exclusion, and it is a user-supplied page count, never derived from the
pilot's own LLM call-count budgets (``--max-calls``/``--tagging-max-calls``),
which govern extraction/tagging call counts, not parser page limits. This
module is a script, not runtime application code, so importing
``evaluation.report_sections`` here does not add an ``evaluation`` dependency
to ``packages/proofops`` or ``apps/*``.
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from datetime import date
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

# scripts/ -> repository root (owns evaluation.local_upstage_pilot).
TEAM_ROOT = Path(__file__).resolve().parents[1]
# Direct script launch puts scripts/, not the checkout, on the import path.
if str(TEAM_ROOT) not in sys.path:
    sys.path.insert(0, str(TEAM_ROOT))
# Local run state and the key file live under the repository root.
PROJECT_ROOT = TEAM_ROOT
DEFAULT_RUNS_DIR = PROJECT_ROOT / ".local" / "report-runs"
DEFAULT_KEY_FILE = PROJECT_ROOT / ".env.upstage.local"
# The pilot reads/writes this shared cumulative ledger; --invoke requires it to exist.
BUDGET_LEDGER = TEAM_ROOT / ".local" / "upstage" / "budget.sqlite3"
# Where auto-scope proposals are persisted for review; never auto-applied silently.
AUTO_SCOPE_DIR = PROJECT_ROOT / ".local" / "auto-scope"

MODEL = "solar-pro3"
EXTRACTION_MAX_CALLS = 8
TAGGING_MAX_CALLS = 48
# Reporting-year window mirrors the app's VersionCreate guard (proofops uploads).
YEAR_MIN, YEAR_MAX = 1900, 2200


class PlanError(ValueError):
    """Validation failure surfaced as a clean CLI error, not a stack trace."""


def parse_pages(raw: str) -> list[int]:
    """Sorted, de-duplicated 1-based page list; reject non-positive/non-int entries."""
    pages: set[int] = set()
    for chunk in raw.split(","):
        token = chunk.strip()
        if not token:
            continue
        try:
            value = int(token)
        except ValueError as exc:
            raise PlanError(
                f"invalid page value {token!r}; use comma-separated 1-based ints"
            ) from exc
        if value < 1:
            raise PlanError(f"page numbers are 1-based; {value} is out of range")
        pages.add(value)
    if not pages:
        raise PlanError("--pages must list at least one 1-based page")
    return sorted(pages)


def _pdf_page_count(pdf: Path) -> int:
    """Count pages with pypdf (a repo dependency); never interpret contents."""
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        count = len(PdfReader(str(pdf)).pages)
    except (PdfReadError, OSError, ValueError) as exc:
        raise PlanError(f"could not read {pdf} as a PDF: {exc}") from exc
    if count < 1:
        raise PlanError(f"{pdf} reports no pages")
    return count


def _validate_pdf_file(pdf: Path) -> None:
    """Refuse unsupported files before reading contents or discovering scope."""
    from proofops.application.uploads_security import PdfLimits

    if not pdf.is_file():
        raise PlanError(f"--pdf not found: {pdf}")
    if pdf.suffix.lower() != ".pdf":
        raise PlanError(f"--pdf must be a .pdf file: {pdf}")
    limit = PdfLimits().max_bytes
    if pdf.stat().st_size > limit:
        raise PlanError(f"--pdf exceeds the supported upload limit of {limit} bytes (100 MiB)")


def validate_pdf(pdf: Path, pages: list[int]) -> int:
    """Confirm the PDF exists and every requested page is within its page count."""
    _validate_pdf_file(pdf)
    count = _pdf_page_count(pdf)
    over = [page for page in pages if page > count]
    if over:
        raise PlanError(f"pages {over} exceed the document page count {count}")
    return count


def discover_auto_scope(pdf: Path) -> dict:
    """Run the existing offline section study and turn it into a scope proposal.

    Delegates entirely to ``evaluation.report_sections.inspect`` (no model or
    network calls; pypdf/pdfplumber only). Never approves or widens scope: the
    result keeps ``status == "candidate_only"``. Raises ``PlanError`` if the
    document yields no usable claim candidates at all, rather than silently
    falling back to every page (ambiguous/empty must not become "whole PDF").
    """
    # ``evaluation`` is not an installed package; it is only importable relative
    # to TEAM_ROOT (the same assumption ``build_pilot_argv``'s subprocess makes
    # via ``cwd=str(TEAM_ROOT)``, and how tests reach it via pytest's rootdir).
    if str(TEAM_ROOT) not in sys.path:
        sys.path.insert(0, str(TEAM_ROOT))
    from evaluation.report_sections import inspect as inspect_sections

    try:
        study = inspect_sections(pdf)
    except ValueError as exc:
        raise PlanError(f"--auto-scope could not inspect {pdf}: {exc}") from exc

    claim_pages = sorted(study["claim_candidate_pages"])
    evidence_pages = sorted(set(study["evidence_candidate_pages"]) | set(claim_pages))
    if not claim_pages:
        raise PlanError(
            "--auto-scope found no E-narrative candidate pages "
            f"(unknown={len(study['unknown_pages'])}, conflict={len(study['conflict_pages'])}); "
            "this is not evidence of an empty report. Select --pages manually instead of "
            "defaulting to the whole PDF."
        )
    return {
        "source_path": study["source_path"],
        "source_sha256": study["source_sha256"],
        "page_count": study["page_count"],
        "method": study["method"],
        "status": study["status"],
        "claim_candidate_pages": claim_pages,
        "evidence_candidate_pages": evidence_pages,
        "unknown_pages": sorted(study["unknown_pages"]),
        "conflict_pages": sorted(study["conflict_pages"]),
        "other_candidate_pages": sorted(study["other_candidate_pages"]),
        "limitations": study["limitations"],
        "map_sha256": study["map_sha256"],
    }


def apply_declared_limit(proposal: dict, max_pages: int | None) -> dict:
    """Keep the FULL proposed scope unless the operator supplies an explicit
    page-count limit via ``--auto-scope-max-pages``.

    ``EXTRACTION_MAX_CALLS``/``TAGGING_MAX_CALLS`` are LLM call-count budgets
    for the pilot's own extraction/tagging loops, not a parser page ceiling;
    they are never used here to derive a hidden page truncation. When
    ``max_pages`` is ``None`` (the default), every proposed candidate page is
    passed straight through untouched. When an operator explicitly supplies
    ``max_pages``, the lowest-numbered evidence-candidate pages are kept (with
    every claim-candidate page always kept if it fits) and the excluded pages
    are reported explicitly; this is a declared, user-chosen exclusion, not an
    automatic subset. If the resulting scope excludes any page, the caller
    marks ``declared_subset`` so downstream reporting never claims full-report
    completion.
    """
    claim_candidates = proposal["claim_candidate_pages"]
    evidence_candidates = proposal["evidence_candidate_pages"]
    if max_pages is None:
        return {
            "pages": list(evidence_candidates),
            "claim_pages": list(claim_candidates),
            "declared_subset": False,
            "excluded_claim_candidate_pages": [],
            "excluded_evidence_candidate_pages": [],
        }
    if max_pages < len(claim_candidates):
        raise PlanError(
            f"--auto-scope-max-pages {max_pages} excludes claim-candidate pages "
            f"({len(claim_candidates)} found); it must keep every claim-candidate page. "
            "Use a larger limit or select --pages manually."
        )
    pages = list(claim_candidates)
    for page in evidence_candidates:
        if page in pages:
            continue
        if len(pages) >= max_pages:
            break
        pages.append(page)
    pages.sort()
    excluded_evidence = [page for page in evidence_candidates if page not in pages]
    return {
        "pages": pages,
        "claim_pages": list(claim_candidates),
        "declared_subset": bool(excluded_evidence),
        "excluded_claim_candidate_pages": [],
        "excluded_evidence_candidate_pages": excluded_evidence,
    }


def write_auto_scope_artifact(pdf: Path, proposal: dict, applied: dict) -> Path:
    """Persist the scope proposal + applied scope for review; never silent.

    Refuses to overwrite an existing artifact with different content so a
    resumed/re-run analysis never silently mutates a previously reviewed
    proposal out from under it; a changed source or scope gets its own file.
    """
    AUTO_SCOPE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest_prefix = proposal["source_sha256"][:12]
    output = AUTO_SCOPE_DIR / f"{pdf.stem}-{digest_prefix}.json"
    payload = {
        "proposal": proposal,
        "applied": applied,
        "note": (
            "candidate_only proposal; unknown/conflict pages are not evidence "
            "absence and are not included in claim/evidence scope. The full proposed "
            "scope is used unless --auto-scope-max-pages explicitly excludes pages; "
            "declared_subset then reports that exclusion rather than claiming "
            "full-report completion."
        ),
    }
    if output.is_file():
        existing = json.loads(output.read_text())
        if existing != payload:
            raise PlanError(
                f"--auto-scope artifact already exists with different content: {output}. "
                "An existing reviewed proposal is never overwritten; remove it explicitly "
                "or investigate why the same source now yields a different scope."
            )
        return output
    with output.open("w") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return output


def _canonical_date(label: str, value: str) -> date:
    """Parse a strictly canonical ``YYYY-MM-DD`` date.

    ``date.fromisoformat`` also accepts compact and week forms; we require the
    round-tripped ``isoformat`` to equal the input so ambiguous inputs are rejected.
    """
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise PlanError(f"{label} must be a canonical ISO date YYYY-MM-DD: got {value!r}") from exc
    if parsed.isoformat() != value:
        raise PlanError(f"{label} must be canonical YYYY-MM-DD: got {value!r}")
    return parsed


def resolve_state(state: Path | None) -> Path:
    """Resolve the run's state dir; refuse any non-empty directory (never overwrite)."""
    chosen = (DEFAULT_RUNS_DIR / f"report-{uuid4().hex[:12]}") if state is None else state.resolve()
    if chosen.exists() and any(chosen.iterdir()):
        raise PlanError(
            f"{chosen} is not empty; a new analysis never overwrites existing data. "
            "Point --state at a fresh empty directory, or re-serve a stored run with "
            "the pilot's read-only --resume."
        )
    return chosen


def _validate_extraction_total(batch_calls: int, total: int | None) -> None:
    """Validate the optional total-run extraction cap with the pilot's own validator.

    This reuses ``evaluation.local_upstage_pilot.extraction_budget_settings``
    (batch 1..20, total batch..2000) instead of duplicating the bound here; the
    shared USD20 money ceiling still fences every provider dispatch.
    """
    if total is None:
        return
    if str(TEAM_ROOT) not in sys.path:
        sys.path.insert(0, str(TEAM_ROOT))
    from evaluation.local_upstage_pilot import extraction_budget_settings

    try:
        extraction_budget_settings(batch_calls, total)
    except ValueError as exc:
        raise PlanError(f"--extraction-total-calls invalid: {exc}") from exc


def _validate_tagging_max_calls(value: int) -> None:
    """Validate the tagging call cap against the pilot's finite 6..2000 bound."""
    if type(value) is not int or not 6 <= value <= 2000:
        raise PlanError(f"--tagging-max-calls must be an integer in 6..2000; got {value!r}")


def build_pilot_argv(
    *,
    pdf: Path,
    pages: list[int],
    claim_pages: list[int] | None,
    report_year: int,
    period_start: str,
    period_end: str,
    state: Path,
    key_file: Path,
    invoke: bool,
    serve: bool,
    port: int,
    extraction_total_calls: int | None = None,
    tagging_max_calls: int = TAGGING_MAX_CALLS,
    verify_selected_cells: bool = False,
    native_quote_typography: bool = False,
    claim_span_typography: bool = False,
    live_relations: bool = False,
    preliminary_context: bool = False,
    preliminary_actor_role: bool = False,
    ai_project_review: bool = False,
    extraction_year_notation: bool = False,
    extraction_context: bool = False,
    extraction_source_ids: bool = False,
    extraction_assertion_prompt: bool = False,
    extraction_complete_selection: bool = False,
    extraction_content_bounds: bool = False,
    parser_max_output_bytes: int | None = None,
) -> list[str]:
    """Assemble the exact argv driving ``evaluation.local_upstage_pilot``."""
    table_flag = "--verify-selected-cells" if verify_selected_cells else "--verify-merged-tables"
    argv = [
        sys.executable,
        "-m",
        "evaluation.local_upstage_pilot",
        "--pdf",
        str(pdf),
        "--state",
        str(state),
        "--key-file",
        str(key_file),
        "--pages",
        ",".join(str(page) for page in pages),
        "--report-year",
        str(report_year),
        "--period-start",
        period_start,
        "--period-end",
        period_end,
        "--model",
        MODEL,
        "--max-calls",
        str(EXTRACTION_MAX_CALLS),
        "--verify-paragraphs",
        "--verify-claim-spans",
        table_flag,
        "--repair-table-headers",
        "--live-tagging",
        "--tagging-max-calls",
        str(tagging_max_calls),
    ]
    if parser_max_output_bytes is not None:
        argv += ["--parser-max-output-bytes", str(parser_max_output_bytes)]
    if extraction_total_calls is not None:
        argv += ["--extraction-total-calls", str(extraction_total_calls)]
    if native_quote_typography:
        argv.append("--native-quote-typography")
    if claim_span_typography:
        argv += [
            "--claim-span-render-resolution",
            "--claim-span-bullet-spacing",
            "--claim-span-typography",
        ]
    if live_relations:
        argv.append("--live-relations")
    if preliminary_context or preliminary_actor_role:
        argv.append("--preliminary-context")
    if preliminary_actor_role:
        argv += [
            "--preliminary-table-context",
            "--preliminary-table-role",
            "--preliminary-goal-role",
            "--preliminary-actor-role",
        ]
    if ai_project_review:
        argv.append("--ai-project-review")
    if extraction_year_notation:
        argv.append("--extraction-year-notation")
    if extraction_context:
        argv.append("--extraction-context")
    if extraction_source_ids:
        argv.append("--extraction-source-ids")
    if extraction_assertion_prompt:
        argv.append("--extraction-assertion-prompt")
    if extraction_complete_selection:
        argv.append("--extraction-complete-selection")
    if extraction_content_bounds:
        argv.append("--extraction-content-bounds")
    if claim_pages is not None:
        argv += ["--claim-pages", ",".join(str(page) for page in claim_pages)]
    if invoke:
        argv.append("--invoke")
    if serve:
        argv += ["--serve", "--port", str(port)]
    return argv


def plan_run(args: argparse.Namespace) -> dict:
    """Validate all inputs and produce the run plan. No side effects."""
    pdf = args.pdf.expanduser().resolve()
    auto_scope_proposal: dict | None = None
    auto_scope_applied: dict | None = None
    auto_scope_artifact: Path | None = None
    if getattr(args, "auto_scope", False):
        if args.pages is not None:
            raise PlanError("--auto-scope cannot be combined with an explicit --pages")
        if getattr(args, "claim_pages", None) is not None:
            raise PlanError("--auto-scope cannot be combined with an explicit --claim-pages")
        _validate_pdf_file(pdf)
        auto_scope_proposal = discover_auto_scope(pdf)
        if auto_scope_proposal["source_sha256"] != sha256(pdf.read_bytes()).hexdigest():
            raise PlanError(f"--auto-scope source changed while inspecting {pdf}")
        max_pages = getattr(args, "auto_scope_max_pages", None)
        auto_scope_applied = apply_declared_limit(auto_scope_proposal, max_pages)
        pages = auto_scope_applied["pages"]
        claim_pages = auto_scope_applied["claim_pages"]
        auto_scope_artifact = write_auto_scope_artifact(
            pdf, auto_scope_proposal, auto_scope_applied
        )
    else:
        if args.pages is None:
            raise PlanError("--pages is required unless --auto-scope is set")
        pages = parse_pages(args.pages)
        claim_pages = None
        raw_claim_pages = getattr(args, "claim_pages", None)
        if raw_claim_pages is not None:
            claim_pages = parse_pages(raw_claim_pages)
            extra = [page for page in claim_pages if page not in pages]
            if extra:
                raise PlanError(
                    f"--claim-pages {extra} are not within --pages; it must be a subset"
                )
    page_count = validate_pdf(pdf, pages)
    if not YEAR_MIN <= args.report_year <= YEAR_MAX:
        raise PlanError(
            f"--report-year {args.report_year} outside supported {YEAR_MIN}..{YEAR_MAX}"
        )
    start = _canonical_date("--period-start", args.period_start)
    end = _canonical_date("--period-end", args.period_end)
    if start > end:
        raise PlanError(
            f"--period-start {args.period_start} must not be after "
            f"--period-end {args.period_end}"
        )
    state = resolve_state(args.state)
    if args.invoke and not BUDGET_LEDGER.is_file():
        raise PlanError(
            f"shared budget ledger not found: {BUDGET_LEDGER}. It must already exist; "
            "we never create or reset an independent budget."
        )
    parser_max_output_bytes = getattr(args, "parser_max_output_bytes", None)
    from evaluation.local_upstage_pilot import parser_output_limit

    try:
        parser_output_limit(parser_max_output_bytes)
    except ValueError as exc:
        raise PlanError(str(exc)) from exc
    tagging_max_calls = getattr(args, "tagging_max_calls", TAGGING_MAX_CALLS)
    _validate_tagging_max_calls(tagging_max_calls)
    extraction_total_calls = getattr(args, "extraction_total_calls", None)
    _validate_extraction_total(EXTRACTION_MAX_CALLS, extraction_total_calls)
    verify_selected_cells = bool(getattr(args, "verify_selected_cells", False))
    native_quote_typography = bool(getattr(args, "native_quote_typography", False))
    claim_span_typography = bool(getattr(args, "claim_span_typography", False))
    live_relations = bool(getattr(args, "live_relations", False))
    preliminary_actor_role = bool(getattr(args, "preliminary_actor_role", False))
    preliminary_context = (
        bool(getattr(args, "preliminary_context", False)) or preliminary_actor_role
    )
    ai_project_review = bool(getattr(args, "ai_project_review", False))
    extraction_year_notation = bool(getattr(args, "extraction_year_notation", False))
    extraction_context = bool(getattr(args, "extraction_context", False))
    extraction_source_ids = bool(getattr(args, "extraction_source_ids", False))
    extraction_assertion_prompt = bool(getattr(args, "extraction_assertion_prompt", False))
    extraction_complete_selection = bool(getattr(args, "extraction_complete_selection", False))
    extraction_content_bounds = bool(getattr(args, "extraction_content_bounds", False))
    argv = build_pilot_argv(
        pdf=pdf,
        pages=pages,
        claim_pages=claim_pages,
        report_year=args.report_year,
        period_start=args.period_start,
        period_end=args.period_end,
        state=state,
        key_file=args.key_file.expanduser().resolve(),
        invoke=args.invoke,
        serve=args.serve,
        port=args.port,
        extraction_total_calls=extraction_total_calls,
        tagging_max_calls=tagging_max_calls,
        verify_selected_cells=verify_selected_cells,
        native_quote_typography=native_quote_typography,
        claim_span_typography=claim_span_typography,
        live_relations=live_relations,
        preliminary_context=preliminary_context,
        preliminary_actor_role=preliminary_actor_role,
        ai_project_review=ai_project_review,
        extraction_year_notation=extraction_year_notation,
        extraction_context=extraction_context,
        extraction_source_ids=extraction_source_ids,
        extraction_assertion_prompt=extraction_assertion_prompt,
        extraction_complete_selection=extraction_complete_selection,
        extraction_content_bounds=extraction_content_bounds,
        parser_max_output_bytes=parser_max_output_bytes,
    )
    return {
        "argv": argv,
        "state": state,
        "pdf": pdf,
        "pages": pages,
        "claim_pages": claim_pages,
        "page_count": page_count,
        "invoke": bool(args.invoke),
        "serve": bool(args.serve),
        "port": args.port,
        "parser_max_output_bytes": parser_max_output_bytes,
        "extraction_total_calls": extraction_total_calls,
        "tagging_max_calls": tagging_max_calls,
        "verify_selected_cells": verify_selected_cells,
        "native_quote_typography": native_quote_typography,
        "claim_span_typography": claim_span_typography,
        "live_relations": live_relations,
        "preliminary_context": preliminary_context,
        "preliminary_actor_role": preliminary_actor_role,
        "ai_project_review": ai_project_review,
        "extraction_year_notation": extraction_year_notation,
        "extraction_context": extraction_context,
        "extraction_source_ids": extraction_source_ids,
        "auto_scope_proposal": auto_scope_proposal,
        "auto_scope_applied": auto_scope_applied,
        "auto_scope_artifact": auto_scope_artifact,
    }


def print_plan(plan: dict) -> None:
    """Print the resolved plan: exact argv and state path, no secrets."""
    print("Resolved report analysis plan (no secrets):")
    print(f"  cwd:       {TEAM_ROOT}")
    print(f"  state dir: {plan['state']}")
    print(f"  pdf:       {plan['pdf']} ({plan['page_count']} pages)")
    if plan["auto_scope_proposal"] is not None:
        proposal = plan["auto_scope_proposal"]
        applied = plan["auto_scope_applied"]
        print(f"  auto-scope method:            {proposal['method']} (status={proposal['status']})")
        print(f"  auto-scope claim candidates:  {proposal['claim_candidate_pages']}")
        print(f"  auto-scope evidence candidates: {proposal['evidence_candidate_pages']}")
        print(f"  auto-scope unknown pages:     {proposal['unknown_pages']}")
        print(f"  auto-scope conflict pages:    {proposal['conflict_pages']}")
        print(f"  auto-scope declared_subset:   {applied['declared_subset']}")
        if applied["declared_subset"]:
            print(
                "    NOTE: --auto-scope-max-pages explicitly excluded evidence pages: "
                f"{applied['excluded_evidence_candidate_pages']}. This run is a declared "
                "partial subset, not full-report completion."
            )
        else:
            print("    Full proposed scope is used (no --auto-scope-max-pages exclusion).")
        print(f"  auto-scope artifact:          {plan['auto_scope_artifact']}")
    print(f"  pages:     {plan['pages']}")
    claim_scope = plan["claim_pages"] if plan["claim_pages"] is not None else "(all selected pages)"
    print(f"  claim_pages: {claim_scope}")
    table_mode = (
        "verify-selected-cells" if plan["verify_selected_cells"] else "verify-merged-tables"
    )
    pilot_opts = [f"table={table_mode}", f"tagging-max-calls={plan['tagging_max_calls']}"]
    if plan["extraction_total_calls"] is not None:
        pilot_opts.append(f"extraction-total-calls={plan['extraction_total_calls']}")
    for flag in (
        "native_quote_typography",
        "claim_span_typography",
        "live_relations",
        "preliminary_context",
        "preliminary_actor_role",
        "ai_project_review",
        "extraction_year_notation",
        "extraction_context",
        "extraction_source_ids",
    ):
        if plan[flag]:
            pilot_opts.append(flag.replace("_", "-"))
    print(f"  pilot options: {', '.join(pilot_opts)} (extraction batch={EXTRACTION_MAX_CALLS})")
    print(f"  invoke:    {plan['invoke']}   serve: {plan['serve']}   port: {plan['port']}")
    print("  exact argv:")
    print("    " + shlex.join(plan["argv"]))
    if not plan["invoke"]:
        print("  DRY PLAN: no model calls, no subprocess. Re-run with --invoke for the paid path.")
    elif plan["auto_scope_proposal"] is not None:
        print(
            f"  NOTE: the pilot's own --max-calls {EXTRACTION_MAX_CALLS} / "
            f"--tagging-max-calls {plan['tagging_max_calls']} may still leave part of this "
            "scope unprocessed at invoke time; that is reported by the pilot's own "
            "run artifacts, not assumed here."
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze_report",
        description="Analyze a new report PDF via the local pilot. Dry plan unless --invoke.",
    )
    parser.add_argument("--pdf", type=Path, required=True, help="Path to the report PDF")
    parser.add_argument(
        "--parser-max-output-bytes",
        type=int,
        default=None,
        help="NEW run parser artifact cap (default 20000000; max 128 MiB).",
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="Comma-separated 1-based pages, e.g. '30,31'. Required unless --auto-scope.",
    )
    parser.add_argument(
        "--claim-pages",
        default=None,
        help="Optional comma-separated 1-based subset of --pages to extract claims from. "
        "Parsing and evidence still cover all --pages; omit to keep legacy behaviour. "
        "Not compatible with --auto-scope (which derives both scopes itself).",
    )
    parser.add_argument(
        "--auto-scope",
        action="store_true",
        help="Opt-in: discover E-narrative/Data/Appendix candidate pages offline via "
        "evaluation.report_sections (no model calls) instead of manual --pages/"
        "--claim-pages. Writes a reviewable proposal under "
        f"{AUTO_SCOPE_DIR}. By default the FULL proposed scope is used; see "
        "--auto-scope-max-pages to declare an explicit exclusion.",
    )
    parser.add_argument(
        "--auto-scope-max-pages",
        type=int,
        default=None,
        help="Only with --auto-scope: an explicit, user-chosen cap on the number of "
        "evidence-candidate pages to include (every claim-candidate page is always "
        "kept). This is NOT derived from --max-calls/--tagging-max-calls, which are "
        "LLM call budgets, not a page limit. Omit to keep the full proposed scope; "
        "excluded pages are always reported explicitly, never silently dropped.",
    )
    parser.add_argument(
        "--report-year", type=int, required=True, help="Reporting year (no guessing)"
    )
    parser.add_argument("--period-start", required=True, help="ISO date YYYY-MM-DD")
    parser.add_argument("--period-end", required=True, help="ISO date YYYY-MM-DD")
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="Run state dir (default: <root>/.local/report-runs/<unique-id>)",
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        default=DEFAULT_KEY_FILE,
        help="Pilot API key file (default: <root>/.env.upstage.local)",
    )
    parser.add_argument(
        "--extraction-total-calls",
        type=int,
        default=None,
        help="Optional total-run extraction allowance across continuation batches "
        f"(at least the legacy batch {EXTRACTION_MAX_CALLS}, at most 2000; omit to keep "
        "the one-batch allowance). Validated by the pilot's own "
        "extraction_budget_settings; the shared USD20 ceiling still applies.",
    )
    parser.add_argument(
        "--tagging-max-calls",
        type=int,
        default=TAGGING_MAX_CALLS,
        help=f"Live-tagging call cap, an integer in 6..2000 (default {TAGGING_MAX_CALLS}, "
        "the legacy value). Passed straight to the pilot's --tagging-max-calls.",
    )
    parser.add_argument(
        "--verify-selected-cells",
        action="store_true",
        help="Exclusive replacement for the legacy --verify-merged-tables table check: "
        "pass the pilot's --verify-selected-cells instead. Omit to keep merged-table "
        "verification.",
    )
    parser.add_argument(
        "--native-quote-typography",
        action="store_true",
        help="Pass the pilot's --native-quote-typography (requires its built-in "
        "--verify-paragraphs without raster OCR, which this launcher always uses).",
    )
    parser.add_argument(
        "--claim-span-typography",
        action="store_true",
        help="Opt a new run into rendered quote/middle-dot comparison, including "
        "the required render-resolution and bullet-spacing wrappers.",
    )
    parser.add_argument(
        "--live-relations",
        "--evidence-relations",
        dest="live_relations",
        action="store_true",
        help="Pass the pilot's exact --live-relations flag for bounded evidence-relation "
        "tagging (--evidence-relations is the same opt-in under its task name; the "
        "delegated argv always carries the pilot-exact --live-relations). Requires the "
        "built-in --live-tagging, which this launcher always enables.",
    )
    parser.add_argument(
        "--preliminary-context",
        action="store_true",
        help="Pass the pilot's --preliminary-context (bounded source-bound context for "
        "preliminary classification; requires the built-in --live-tagging).",
    )
    parser.add_argument(
        "--preliminary-actor-role",
        action="store_true",
        help="Use the actor/goal-period classification profile for a NEW run. "
        "Includes its required context/table/goal options; no rule approval or paid "
        "call is enabled by this flag. Existing saved runs are unchanged.",
    )
    parser.add_argument(
        "--ai-project-review",
        action="store_true",
        help="Pass the pilot's --ai-project-review: promote the draft rulepack to an "
        "AI-delegated-reviewed pack for this NEW run (GAPs preserved; not an "
        "independent expert gold or legal approval).",
    )
    parser.add_argument(
        "--extraction-year-notation",
        action="store_true",
        help="Pass the pilot's opt-in --extraction-year-notation for this NEW run: "
        "accept finite explicit abbreviated-year punctuation (‘ + two ASCII digits, "
        "e.g. ‘24 년도) as source punctuation rather than an unterminated paired "
        "quotation. No year is inferred and no text is normalized; off by default.",
    )
    parser.add_argument(
        "--extraction-context",
        action="store_true",
        help="Pass the pilot's opt-in --extraction-context for this NEW run: send "
        "bounded source-linked heading/adjacent context with each extraction call "
        "under a distinct context profile. Quotes still resolve from the focal "
        "source only; off by default.",
    )
    parser.add_argument(
        "--extraction-source-ids",
        action="store_true",
        help="Pass the pilot's opt-in --extraction-source-ids for this NEW run: the "
        "model selects locally minted sentence ids instead of retyping the quote, and "
        "the span is restored from the original offsets. Exact-source matching is "
        "unchanged; a selected sentence is a whole source sentence (atomicity "
        "unreviewed); off by default.",
    )
    parser.add_argument(
        "--extraction-assertion-prompt",
        action="store_true",
        help="Require the selected source sentence itself to assert a claim. "
        "Requires --extraction-source-ids; off by default.",
    )
    parser.add_argument(
        "--extraction-complete-selection",
        action="store_true",
        help="Review every source sentence; requires --extraction-assertion-prompt.",
    )
    parser.add_argument(
        "--extraction-content-bounds",
        action="store_true",
        help="Use exact source spans without a nonnumeric terminal stop; "
        "requires --extraction-source-ids. New-run opt-in only.",
    )
    parser.add_argument(
        "--invoke",
        action="store_true",
        help="Delegate to the pilot (paid; shared budget ledger must exist)",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="After invoking, serve the review UI (blocking). Explicit opt-in",
    )
    parser.add_argument("--port", type=int, default=8766, help="Serve port when --serve is set")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.serve and not args.invoke:
        parser.error("--serve requires --invoke; serving only makes sense after a run")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.auto_scope_max_pages is not None and not args.auto_scope:
        parser.error("--auto-scope-max-pages requires --auto-scope")
    if args.auto_scope_max_pages is not None and args.auto_scope_max_pages < 1:
        parser.error("--auto-scope-max-pages must be a positive integer")
    try:
        plan = plan_run(args)
    except PlanError as exc:
        parser.error(str(exc))
    print_plan(plan)
    if not args.invoke:
        return 0
    plan["state"].mkdir(parents=True, exist_ok=True, mode=0o700)
    return subprocess.run(plan["argv"], cwd=str(TEAM_ROOT), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
