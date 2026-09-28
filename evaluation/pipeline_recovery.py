"""R01 fixed-input baseline/comparison harness (offline, no reparse, no paid calls).

Reads existing real-run receipts already on disk (previous actual local
worker + model executions recorded under ROOT `.local/five-report-outcome-20260919/`,
ROOT `outputs/scope-flow-20260920/`, and ROOT `outputs/header-role-20260920/`)
and turns them into one frozen, reproducible manifest + per-company baseline
summary.

This module does not call any model or parser except where explicitly noted
(KEPCO's stage is a *replay* of an already-captured candidate-level parse
snapshot, not a fresh PDF parse or API call). It does not invent claims,
labels, or accuracy.

Historical exposure is tracked per company AND per physical page, not as a
blanket "this company was in the heldout split" claim:
- kia/kb/naver claim+tag pages here (24/106/128, 30/46/109, 90/224/236) come
  from `.local/five-report-outcome-20260919/` (baseline commit b67b62c).
- outputs/generalization-20260920/split_manifest.frozen.json separately used
  DIFFERENT single pages for the SAME three companies for a table-normalize
  patch audit: kia page 45, kb page 30, naver page 84. Only kb's page 30
  actually overlaps between the two runs; kia 45 vs 24/106/128 and naver 84
  vs 90/224/236 do not overlap at all. Treating all three as "the same
  patch-heldout pages" would be wrong; both prior exposures are recorded
  explicitly per company below instead.
- lotte(롯데케미칼) pages (25/117/138) were run twice locally in
  outputs/scope-flow-20260920/, and once against the real Upstage API in
  outputs/pipeline-recovery-20260920/api/lotte/ (see `lotte_api_comparison`).
- kepco(한국전력공사) has no claim/tag run anywhere; only a parse-level
  candidate snapshot (outputs/header-role-20260920/, pages 74/205/259) and a
  much thinner single-page probe (.local/showcase-eval/kepco). Reused here
  as a parse-only stage; claim/tag stages remain explicitly not_run.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / ".local/team-publication-20260918"

FIVE_REPORT_DIR = APP / ".local/five-report-outcome-20260919"
FIVE_REPORT_COMPARISON = FIVE_REPORT_DIR / "comparison.json"
FIVE_REPORT_SELECTION = FIVE_REPORT_DIR / "selection.json"

SCOPE_FLOW_DIR = ROOT / "outputs/scope-flow-20260920"
SCOPE_FLOW_BEFORE = SCOPE_FLOW_DIR / "before.json"
SCOPE_FLOW_AFTER = SCOPE_FLOW_DIR / "after.json"
SCOPE_FLOW_RESULTS = SCOPE_FLOW_DIR / "RESULTS.md"

GENERALIZATION_MANIFEST = ROOT / "outputs/generalization-20260920/split_manifest.frozen.json"

HEADER_ROLE_FROZEN_INPUTS = ROOT / "outputs/header-role-20260920/frozen_inputs.json"
HEADER_ROLE_EVAL = ROOT / "outputs/header-role-20260920/header_role_eval.json"

API_LOTTE_DIR = ROOT / "outputs/pipeline-recovery-20260920/api/lotte"
API_LOTTE_RECEIPT = API_LOTTE_DIR / "receipt.json"
API_LOTTE_REQUEST = API_LOTTE_DIR / "request.json"
API_LOTTE_RESULT = API_LOTTE_DIR / "result.json"
API_LOTTE_VISUAL_REVIEW = API_LOTTE_DIR / "VISUAL_REVIEW.md"

API_KIA_DIR = ROOT / "outputs/pipeline-recovery-20260920/api/kia"
API_KIA_RECEIPT = API_KIA_DIR / "receipt.json"
API_KIA_REQUEST = API_KIA_DIR / "request.json"
API_KIA_RESULT = API_KIA_DIR / "result.json"
API_KIA_VISUAL_REVIEW = API_KIA_DIR / "VISUAL_REVIEW.md"

# GRI305's concrete link lives on physical page 139, outside the frozen
# 25/117/138 selection below. Never claim this scope verified the link.
LOTTE_NOTE_PAGE = 139

KEPCO_PDF = ROOT / "기업보고서/전력 가스/(한국전력공사)2025지속가능경영보고서(최종).pdf"
KEPCO_THIN_RECEIPT = ROOT / ".local/showcase-eval/kepco/lineage.json"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_idempotent_versioned(path: Path, content: str) -> Path:
    """Write `content` to `path` without silently overwriting changed history.

    - If `path` doesn't exist: write it (v1).
    - If `path` exists and is byte-identical: no-op, return `path` unchanged.
    - If `path` exists and differs: write a new `<stem>.v2<suffix>` (or v3,
      v4, ...) sibling instead of overwriting, and return that new path.
      The original is never touched.

    This is intentionally a small local helper, not a generic artifact/
    versioning framework — it only guards the specific corpus/baseline
    outputs this module writes.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
    existing = path.read_text(encoding="utf-8")
    if existing == content:
        return path
    version = 2
    while True:
        candidate = path.with_name(f"{path.stem}.v{version}{path.suffix}")
        if not candidate.exists():
            candidate.write_text(content, encoding="utf-8")
            return candidate
        if candidate.read_text(encoding="utf-8") == content:
            return candidate
        version += 1


@dataclass(frozen=True)
class CompanyBaseline:
    """One company's frozen input + already-executed pipeline outcome."""

    slug: str
    display_name: str
    pdf_path: str
    source_sha256: str
    pages: list[int]
    status: str  # "ok" | "parse_only" | "missing"
    reason: str | None
    receipt_source: str | None
    stage_counts: dict[str, Any]
    development_pool: bool
    prior_exposure: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "display_name": self.display_name,
            "pdf_path": self.pdf_path,
            "source_sha256": self.source_sha256,
            "pages": self.pages,
            "status": self.status,
            "reason": self.reason,
            "receipt_source": self.receipt_source,
            "stage_counts": self.stage_counts,
            "development_pool": self.development_pool,
            "prior_exposure": self.prior_exposure,
            "notes": self.notes,
        }


def _verify_hash(label: str, path: Path, expected_sha256: str) -> str:
    """Recompute the original PDF hash and fail loudly on drift.

    This is the only hashing pipeline_recovery performs; it never re-parses
    or re-extracts the PDF, it only confirms the frozen receipt still points
    at the same bytes.
    """
    if not path.exists():
        raise FileNotFoundError(f"{label}: source PDF missing at {path}")
    actual = _sha256_file(path)
    if actual != expected_sha256:
        raise ValueError(
            f"{label}: source hash changed ({expected_sha256} -> {actual}); "
            "refusing to reuse a receipt against a different document"
        )
    return actual


def _load_five_report_entry(slug: str) -> dict[str, Any] | None:
    if not FIVE_REPORT_COMPARISON.exists():
        return None
    comparison = _read_json(FIVE_REPORT_COMPARISON)
    for entry in comparison.get("results", []):
        if entry.get("company") == slug:
            return entry
    return None


def _load_five_report_pilot(slug: str) -> dict[str, Any] | None:
    pilot_path = FIVE_REPORT_DIR / slug / "pilot.json"
    if not pilot_path.exists():
        return None
    return _read_json(pilot_path)


def _generalization_exposure(slug: str) -> dict[str, Any] | None:
    """Look up the OTHER prior exposure for kia/kb/naver from a different run.

    Returns the single `original_page` used by the table-normalize patch
    audit, distinct from (and not necessarily overlapping with) the
    claim/tag pages used in `_kia_kb_naver_baseline`.
    """
    if not GENERALIZATION_MANIFEST.exists():
        return None
    manifest = _read_json(GENERALIZATION_MANIFEST)
    for company in manifest.get("companies", []):
        if company.get("slug") == slug:
            return {
                "run": "outputs/generalization-20260920 (normalize_tables patch heldout)",
                "purpose": manifest.get("purpose"),
                "original_page": company.get("original_page"),
                "source_sha256": company.get("source_sha256"),
            }
    return None


def _kia_kb_naver_baseline(slug: str, display_name: str) -> CompanyBaseline:
    pilot = _load_five_report_pilot(slug)
    entry = _load_five_report_entry(slug)
    if pilot is None or entry is None:
        return CompanyBaseline(
            slug=slug,
            display_name=display_name,
            pdf_path="",
            source_sha256="",
            pages=[],
            status="missing",
            reason=(f"expected receipt not found under {FIVE_REPORT_DIR.relative_to(ROOT)}"),
            receipt_source=None,
            stage_counts={},
            development_pool=True,
        )
    pdf_path = Path(pilot["source_path"])
    actual_sha = _verify_hash(display_name, pdf_path, pilot["source_sha256"])
    stage_counts = {
        "claims": entry.get("claims"),
        "source_verified": entry.get("source_verified"),
        "reviews": entry.get("reviews"),
        "blocked": entry.get("blocked", {}),
        "native_reasons": entry.get("native_reasons", {}),
        "raster_requested": entry.get("raster_requested"),
        "raster_corroborated": entry.get("raster_corroborated"),
        "stages_seconds": entry.get("stages_seconds", {}),
        "decisions": entry.get("decisions"),
        "run_id": entry.get("run_id"),
        "baseline_commit": _read_json(FIVE_REPORT_COMPARISON).get("baseline_commit"),
    }
    pages = list(pilot["selected_pages"])
    exposure: list[dict[str, Any]] = [
        {
            "run": str(FIVE_REPORT_COMPARISON.relative_to(ROOT)),
            "pages": pages,
            "purpose": "real claim+tag extraction run (this baseline's source)",
        }
    ]
    other = _generalization_exposure(slug)
    overlap_note = None
    if other is not None:
        other_page = other.get("original_page")
        overlaps = isinstance(other_page, int) and other_page in pages
        overlap_note = (
            f"generalization run used page {other_page}, which "
            + ("DOES overlap" if overlaps else "does NOT overlap")
            + f" this baseline's pages {pages}."
        )
        exposure.append(
            {
                "run": other["run"],
                "pages": [other_page] if other_page is not None else [],
                "purpose": other.get("purpose"),
                "overlaps_this_baseline_pages": overlaps,
            }
        )
    notes = [
        "This company has been exposed to prior development work at least twice "
        "(claim+tag run here, and a separate table-normalize patch audit); "
        "it is a development/patch-heldout company overall, not an unseen company.",
    ]
    if overlap_note:
        notes.append(overlap_note)
    notes.append(
        "Do not describe kia/kb/naver as sharing 'the same patch-heldout pages' "
        "across runs without checking per-page overlap; only kb's page 30 overlaps."
    )
    if slug == "kia":
        kia_api = _kia_api_comparison()
        if kia_api is not None:
            stage_counts["api_comparison"] = kia_api
            exposure.append(
                {
                    "run": kia_api["artifact_dir"],
                    "pages": kia_api.get("pages_requested") or [],
                    "purpose": "one real Upstage enhanced-parse API call, same pages",
                }
            )
            notes.append(
                "A real same-page Upstage enhanced-parse artifact exists "
                f"({kia_api['artifact_dir']}); see stage_counts.api_comparison "
                "for known_failure_rows. Coordinator instruction: incorporate "
                "same-page counts/time only, element counts are not accuracy."
            )
    return CompanyBaseline(
        slug=slug,
        display_name=display_name,
        pdf_path=str(pdf_path),
        source_sha256=actual_sha,
        pages=pages,
        status="ok",
        reason=None,
        receipt_source=str(FIVE_REPORT_COMPARISON.relative_to(ROOT)),
        stage_counts=stage_counts,
        development_pool=True,
        prior_exposure=exposure,
        notes=notes,
    )


def _lotte_api_comparison() -> dict[str, Any] | None:
    """Summarize the real Upstage enhanced-parse artifact for the SAME pages.

    Reads outputs/pipeline-recovery-20260920/api/lotte/{receipt,request,
    result}.json and VISUAL_REVIEW.md, which the coordinator already
    produced with one real paid API call. This function does not call the
    API; it only records what is already on disk, including the concrete
    structural error rows the coordinator found (not a bare match-count).
    """
    if not (API_LOTTE_RECEIPT.exists() and API_LOTTE_RESULT.exists()):
        return None
    result = _read_json(API_LOTTE_RESULT)
    request = _read_json(API_LOTTE_REQUEST) if API_LOTTE_REQUEST.exists() else {}
    comparison: dict[str, Any] = {
        "artifact_dir": str(API_LOTTE_DIR.relative_to(ROOT)),
        "provider": "Upstage",
        "model": result.get("model"),
        "mode": request.get("mode"),
        "status": result.get("status"),
        "quality_status": result.get("quality_status") or request.get("quality_status"),
        "cost_usd": result.get("cost_usd") or request.get("expected_cost_with_vat_usd"),
        "pages_requested": request.get("physical_pages"),
        "provider_element_counts": result.get("elements"),
        "api_errors": [],
        "visual_adjudication": "completed",
    }
    if result.get("status") not in ("provider_succeeded", None):
        comparison["api_errors"].append({"field": "status", "value": result.get("status")})
    if API_LOTTE_VISUAL_REVIEW.exists():
        review_text = API_LOTTE_VISUAL_REVIEW.read_text(encoding="utf-8")
        comparison["visual_review_path"] = str(API_LOTTE_VISUAL_REVIEW.relative_to(ROOT))
        comparison["visual_review_sha256"] = _sha256_bytes(review_text.encode("utf-8"))
        # Known structural failure rows the coordinator already identified by
        # eye against the PDFium render; kept as literal findings, not derived
        # counts. This module does not re-derive or verify them itself.
        comparison["known_failure_rows"] = [
            "p25 environmental-management paragraph split mid-sentence across "
            "two elements ('있습' / '니다.') despite continuing as one paragraph "
            "in the original.",
            "p25 system-upgrade paragraph's final line separated from its "
            "paragraph by the API's own segmentation.",
            "p117 Scope 1 by-site table: rowspan=4 grouping misassigns "
            "'기타'/'합계' rows into the following '고정연소별' group in the "
            "provider's HTML.",
            "p117 Scope 1 by-combustion-source table: CH4/합계 rows leak into "
            "the 'Scope 2' group in the provider's HTML — a scope "
            "misattribution risk, not just a display glitch.",
            "p117 Scope 2 first data cell mixes the year label into the value "
            "cell text ('2025 750,220') rather than keeping them separate.",
            "p25 figure description is an English paraphrase with some "
            "misreadings of the original Korean; not usable as a literal quote "
            "or quantitative source.",
        ]
        comparison["reviewer_conclusion"] = (
            "Numbers are largely legible via the external parser, but row/"
            "column/group attribution in tables is not reliable from this "
            "sample; do not treat this as evidence the external parser is "
            "better than the current parser, and do not auto-promote its "
            "HTML/Markdown cells to verified without coordinate-checked review."
        )
    return comparison


def _kia_api_comparison() -> dict[str, Any] | None:
    """Summarize the real Upstage enhanced-parse artifact for kia's same pages.

    Per the coordinator's explicit instruction, this is incorporated as
    same-page counts/time only; the visual adjudication for kia is pending,
    so `known_failure_rows`/`reviewer_conclusion` are populated from the
    coordinator's VISUAL_REVIEW.md when present (it already exists on disk
    for kia), but `visual_adjudication` is still marked accordingly rather
    than assumed complete just because a review file exists.
    """
    if not (API_KIA_RECEIPT.exists() and API_KIA_RESULT.exists()):
        return None
    result = _read_json(API_KIA_RESULT)
    request = _read_json(API_KIA_REQUEST) if API_KIA_REQUEST.exists() else {}
    comparison: dict[str, Any] = {
        "artifact_dir": str(API_KIA_DIR.relative_to(ROOT)),
        "provider": "Upstage",
        "model": result.get("model"),
        "mode": request.get("mode"),
        "status": result.get("status"),
        "quality_status": result.get("quality_status") or request.get("quality_status"),
        "cost_usd": result.get("cost_usd") or request.get("expected_cost_with_vat_usd"),
        "pages_requested": request.get("physical_pages"),
        "provider_element_counts": result.get("elements"),
        "seconds": result.get("seconds"),
        "api_errors": [],
        "visual_adjudication": "coordinator_reviewed_but_treat_as_candidate",
    }
    if result.get("status") not in ("provider_succeeded", None):
        comparison["api_errors"].append({"field": "status", "value": result.get("status")})
    if API_KIA_VISUAL_REVIEW.exists():
        review_text = API_KIA_VISUAL_REVIEW.read_text(encoding="utf-8")
        comparison["visual_review_path"] = str(API_KIA_VISUAL_REVIEW.relative_to(ROOT))
        comparison["visual_review_sha256"] = _sha256_bytes(review_text.encode("utf-8"))
        comparison["known_failure_rows"] = [
            "p24 figure element (element 10) is a generated description, not "
            "literal OCR: renames '기아 탄소중립 선언' to '기타', distorts "
            "'밸류체인별 목표 및 전략과제 선정', drops the overseas-2030/"
            "domestic-2040 RE100 distinction, and misplaces 100% global "
            "electrification into 2040 instead of the original's phase 3 "
            "(2041-2045). Must never be admitted as source quote/numeric "
            "evidence.",
            "p106 Scope1/2 table (element 18) merges separate domestic/"
            "overseas intensity rows into one HTML row via '<br>' per cell "
            "(0.55/0.32 for 2024); numeric values exist but need row "
            "reconstruction, and markdown loses column alignment under "
            "rowspan.",
            "p106 Scope3 table (element 31) footnote on moving leased assets "
            "from Scope3 to Scope1/2 starting 2024 appears as a separate "
            "element (19); any comparison must keep this boundary change "
            "attached, not drop it.",
        ]
        comparison["reviewer_conclusion"] = (
            "Retain the enhanced API as a selective candidate parser only; "
            "no blanket replacement and no figure-caption admission. Two "
            "companies (lotte, kia) support maintaining source/row ownership "
            "checks; they do not establish recall or accuracy for the "
            "external parser."
        )
    return comparison


def _lotte_baseline() -> CompanyBaseline:
    if not (SCOPE_FLOW_BEFORE.exists() and SCOPE_FLOW_AFTER.exists()):
        return CompanyBaseline(
            slug="lotte",
            display_name="롯데케미칼 (Lotte Chemical)",
            pdf_path="",
            source_sha256="",
            pages=[],
            status="missing",
            reason=f"expected receipts not found under {SCOPE_FLOW_DIR.relative_to(ROOT)}",
            receipt_source=None,
            stage_counts={},
            development_pool=False,
        )
    before = _read_json(SCOPE_FLOW_BEFORE)
    after = _read_json(SCOPE_FLOW_AFTER)
    pdf_path = ROOT / "기업보고서/철강 화학 소재/롯데케미칼 2025 ESG Report (KOR).pdf"
    # Both runs recorded the same original SHA256; verify once against the
    # live file rather than trusting the recorded value blindly.
    expected_sha = "d8dd4f3e510428fcfd86c4e0003b19f35f2d5a8b30fdf9f55ccee166d0bd5a52"
    actual_sha = _verify_hash("lotte", pdf_path, expected_sha)
    stage_counts = {
        "before": {
            "claim_pages": before.get("claim_pages"),
            "graph_pages": before.get("graph_pages"),
            "claim_count": before.get("claim_count"),
            "tagged": before.get("tagged"),
            "blocker_counts": before.get("blocker_counts", {}),
            "coverage": before.get("coverage", {}),
            "run_status": before.get("run_status"),
            "run_id": before.get("run_id"),
        },
        "after": {
            "claim_pages": after.get("claim_pages"),
            "graph_pages": after.get("graph_pages"),
            "claim_count": after.get("claim_count"),
            "tagged": after.get("tagged"),
            "blocker_counts": after.get("blocker_counts", {}),
            "coverage": after.get("coverage", {}),
            "run_status": after.get("run_status"),
            "run_id": after.get("run_id"),
        },
    }
    api_comparison = _lotte_api_comparison()
    if api_comparison is not None:
        stage_counts["api_comparison"] = api_comparison
    notes = [
        f"GRI305 concrete link is on physical page {LOTTE_NOTE_PAGE}, "
        "outside this frozen 25/117/138 selection; not verified here.",
        "Two real local runs exist (before=full graph_pages claim scope, "
        "after=claim_pages narrowed to page 25 only); both kept, not averaged.",
    ]
    if api_comparison is not None:
        notes.append(
            "A real same-page Upstage enhanced-parse artifact exists "
            f"({api_comparison['artifact_dir']}); see stage_counts.api_comparison "
            "for known_failure_rows. Its element counts are not accuracy — see "
            "reviewer_conclusion."
        )
    return CompanyBaseline(
        slug="lotte",
        display_name="롯데케미칼 (Lotte Chemical)",
        pdf_path=str(pdf_path),
        source_sha256=actual_sha,
        pages=list(after.get("graph_pages", [25, 117, 138])),
        status="ok",
        reason=None,
        receipt_source=str(SCOPE_FLOW_DIR.relative_to(ROOT)),
        stage_counts=stage_counts,
        development_pool=False,
        prior_exposure=[
            {
                "run": str(SCOPE_FLOW_DIR.relative_to(ROOT)),
                "pages": list(after.get("graph_pages", [25, 117, 138])),
                "purpose": "two real local claim+tag runs on the same pages",
            },
            {
                "run": str(API_LOTTE_DIR.relative_to(ROOT)) if api_comparison else None,
                "pages": api_comparison.get("pages_requested") if api_comparison else [],
                "purpose": "one real Upstage enhanced-parse API call, same pages",
            },
        ],
        notes=notes,
    )


def _kepco_baseline() -> CompanyBaseline:
    """KEPCO as a parse-only fifth company: real graph, no claim/tag stage.

    Reuses the candidate-level parse snapshot already captured for the
    header-role normalize_tables audit (outputs/header-role-20260920/), which
    was itself a replay of an already-fused graph (`graph.to_dict()==
    graph.json` asserted; NOT a fresh PDF parse, NOT a model/API call). This
    intentionally does not fabricate claim_count/source_verified/blocked
    fields — those stages were never run for KEPCO and are left absent
    rather than defaulted to 0 (0 would misleadingly read as "ran and found
    nothing").
    """
    if not (HEADER_ROLE_FROZEN_INPUTS.exists() and HEADER_ROLE_EVAL.exists()):
        return _kepco_missing()
    frozen_inputs = _read_json(HEADER_ROLE_FROZEN_INPUTS)
    kepco_input = next(
        (c for c in frozen_inputs.get("companies", []) if c.get("slug") == "kepco"), None
    )
    eval_data = _read_json(HEADER_ROLE_EVAL)
    kepco_eval = eval_data.get("companies", {}).get("kepco")
    if kepco_input is None or kepco_eval is None:
        return _kepco_missing()
    pdf_path = Path(kepco_input["source_path"])
    actual_sha = _verify_hash("kepco", pdf_path, kepco_input["source_sha256"])
    pages = list(kepco_input["selected_physical_pages"])
    stage_counts = {
        "PARSE": {
            "table_count": kepco_eval.get("table_count"),
            "cell_count": kepco_eval.get("cell_count"),
            "unresolved_reasons": {
                key: value.get("reason")
                for key, value in kepco_eval.get("unresolved_reasons", {}).items()
            },
            "replay_kind": eval_data.get("replay_kind"),
            "parser_family": kepco_input.get("parser_family"),
            "parse_manifest_id": kepco_input.get("parse_manifest_id"),
        },
        "EXTRACT": "not_run",
        "TAG": "not_run",
    }
    thin_probe = None
    if KEPCO_THIN_RECEIPT.exists():
        thin_probe = _read_json(KEPCO_THIN_RECEIPT)
    return CompanyBaseline(
        slug="kepco",
        display_name="한국전력공사 (KEPCO)",
        pdf_path=str(pdf_path),
        source_sha256=actual_sha,
        pages=pages,
        status="parse_only",
        reason=(
            "Only a PARSE-stage candidate snapshot exists for KEPCO "
            "(outputs/header-role-20260920/, table-structure replay, no model/"
            "API call). No EXTRACT or TAG receipt exists at any fidelity; "
            "those stages are recorded as not_run rather than fabricated. "
            "This harness did not run a fresh EXTRACT/TAG pass to fill the gap "
            "(would require standing up upload+run infrastructure, out of "
            "scope for this fixed-input offline comparison)."
        ),
        receipt_source=str(HEADER_ROLE_EVAL.relative_to(ROOT)),
        stage_counts=stage_counts,
        development_pool=False,
        prior_exposure=[
            {
                "run": str(HEADER_ROLE_EVAL.relative_to(ROOT)),
                "pages": pages,
                "purpose": "table-normalize header-role audit, parse-level candidate replay only",
            },
            (
                {
                    "run": str(KEPCO_THIN_RECEIPT.relative_to(ROOT)),
                    "pages": [thin_probe.get("original_page")] if thin_probe else [],
                    "purpose": (
                        "single-page parse probe, source_quality=unverified, "
                        "predates current pipeline"
                    ),
                }
                if thin_probe
                else {"run": None, "pages": [], "purpose": None}
            ),
        ],
        notes=[
            "prior_tuning_status from the header-role run: "
            + str(kepco_input.get("prior_tuning_status")),
            "This company's claim/tag stages remain genuinely not_run; do not "
            "report KEPCO alongside lotte/kia/kb/naver as if it has the same "
            "stage coverage.",
        ],
    )


def _kepco_missing() -> CompanyBaseline:
    reason = (
        "KEPCO PDF confirmed present by SHA256 match "
        f"({KEPCO_THIN_RECEIPT.relative_to(ROOT) if KEPCO_THIN_RECEIPT.exists() else 'n/a'}), "
        "but no usable parse/extraction/tagging receipt was found at all."
    )
    sha = ""
    if KEPCO_PDF.exists():
        sha = _sha256_file(KEPCO_PDF)
    return CompanyBaseline(
        slug="kepco",
        display_name="한국전력공사 (KEPCO)",
        pdf_path=str(KEPCO_PDF) if KEPCO_PDF.exists() else "",
        source_sha256=sha,
        pages=[],
        status="missing",
        reason=reason,
        receipt_source=None,
        stage_counts={},
        development_pool=False,
        notes=["Confirmed candidate for a future run; not fabricated here."],
    )


def build_manifest() -> dict[str, Any]:
    """Build the frozen five-company manifest from existing receipts only."""
    companies = [
        _lotte_baseline(),
        _kia_kb_naver_baseline("kia", "기아 (Kia)"),
        _kia_kb_naver_baseline("kb", "KB금융그룹 (KB Financial Group)"),
        _kia_kb_naver_baseline("naver", "NAVER"),
        _kepco_baseline(),
    ]
    ok = [c for c in companies if c.status == "ok"]
    parse_only = [c for c in companies if c.status == "parse_only"]
    missing = [c for c in companies if c.status == "missing"]
    return {
        "schema": "pipeline_recovery_manifest_v1",
        "purpose": (
            "R01 fixed-input baseline inventory across 5 target companies "
            "(lotte/kia/kb/naver/kepco) reusing existing real-run receipts. "
            "No PDF was reparsed and no paid model/API call was made to build "
            "this manifest (KEPCO's stage is a replay of an already-captured "
            "parse snapshot)."
        ),
        "requested_companies": ["lotte", "kia", "kb", "naver", "kepco"],
        "companies_ok": [c.slug for c in ok],
        "companies_parse_only": [c.slug for c in parse_only],
        "companies_missing": [c.slug for c in missing],
        "companies": [c.to_dict() for c in companies],
    }


def write_manifest(output_dir: Path) -> Path:
    manifest = build_manifest()
    content = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    return write_idempotent_versioned(output_dir / "manifest.json", content)


def _lotte_review_cases() -> list[dict[str, Any]]:
    """Raw source-bound diagnostic cases from the real Lotte 'after' run quotes.

    task="claim": source is the literal extracted quote text plus its
    physical page and the source PDF hash. No prediction, no label —
    unreviewed stays unreviewed until a human/agent review file is scored
    with evaluation.review_benchmark.score_review.
    """
    if not SCOPE_FLOW_AFTER.exists():
        return []
    after = _read_json(SCOPE_FLOW_AFTER)
    sha = "d8dd4f3e510428fcfd86c4e0003b19f35f2d5a8b30fdf9f55ccee166d0bd5a52"
    cases = []
    for index, item in enumerate(after.get("quotes", [])):
        cases.append(
            dict(
                case_id=f"lotte-claim-{index:02d}",
                company_id="lotte",
                task="claim",
                source_sha256=sha,
                page=int(item["page"]),
                source={"quote": item["quote"]},
            )
        )
    return cases


def _kia_kb_naver_review_cases(slug: str, limit: int) -> list[dict[str, Any]]:
    """Raw diagnostic cases from prior real extraction receipts, source-only.

    Reads the already-produced extraction-receipts/*/{packet,result}.json
    pairs (real model output already on disk from the
    five-report-outcome-20260919 run): page number comes from
    packet.json's untrusted_document_data.page_num, quote spans from
    result.json's spans[].quote. Skips receipts that are unreadable or
    don't match the claim shape rather than failing the whole build.
    """
    receipts_dir = FIVE_REPORT_DIR / slug / "extraction-receipts"
    pilot = _load_five_report_pilot(slug)
    if pilot is None or not receipts_dir.exists():
        return []
    sha = pilot["source_sha256"]
    cases: list[dict[str, Any]] = []
    for receipt_dir in sorted(receipts_dir.iterdir()):
        packet_path = receipt_dir / "packet.json"
        result_path = receipt_dir / "result.json"
        try:
            packet = _read_json(packet_path)
            result = _read_json(result_path)
        except (OSError, PermissionError, json.JSONDecodeError):
            continue
        page = (packet.get("untrusted_document_data") or {}).get("page_num")
        spans = result.get("spans") if isinstance(result, dict) else None
        if not isinstance(page, int) or not spans:
            continue
        for span in spans:
            quote = span.get("quote")
            if not quote or span.get("kind") != "claim":
                continue
            cases.append(
                dict(
                    case_id=f"{slug}-claim-{len(cases):02d}",
                    company_id=slug,
                    task="claim",
                    source_sha256=sha,
                    page=int(page),
                    source={"quote": quote},
                )
            )
            if len(cases) >= limit:
                return cases
    return cases


def build_review_packet(target_min: int = 30, target_max: int = 50) -> dict[str, Any] | None:
    """Assemble the 30-50 source-bound diagnostic packet via review_benchmark.

    Reuses evaluation.review_benchmark.freeze_cases verbatim (no new eval
    framework). Draws only from cases already recoverable from existing
    receipts; if fewer than a handful of real cases exist, returns None
    rather than padding with invented content.

    IMPORTANT: these are model-extracted candidate quotes, not an
    independent, source-first enumeration of every claim on each page. They
    are useful as a diagnostic review sample (do these candidates look right
    when checked against the source?) but must NOT be treated as a recall
    denominator — a model that missed a claim entirely would never produce
    a case for it here, so recall cannot be computed from this packet.
    """
    from evaluation.review_benchmark import freeze_cases

    cases = list(_lotte_review_cases())
    remaining = max(target_max - len(cases), 0)
    per_company = max(remaining // 3, 1) if remaining else 0
    for slug in ("kia", "kb", "naver"):
        if len(cases) >= target_max:
            break
        cases.extend(_kia_kb_naver_review_cases(slug, per_company))
    cases = cases[:target_max]
    if len(cases) < 5:
        # Not enough real source-bound content recovered from receipts to
        # call this a diagnostic packet; do not fabricate filler cases.
        return None
    return freeze_cases(cases)


def write_review_packet(output_dir: Path) -> Path | None:
    packet = build_review_packet()
    if packet is None:
        return None
    content = json.dumps(packet, ensure_ascii=False, indent=2) + "\n"
    return write_idempotent_versioned(output_dir / "review_packet.json", content)


def write_review_html(output_dir: Path, packet_path: Path) -> Path | None:
    from evaluation.review_benchmark import review_html

    packet = _read_json(packet_path)
    html = review_html(packet)
    return write_idempotent_versioned(output_dir / "review_packet.html", html)


def write_baseline_summary(output_dir: Path, manifest: dict[str, Any]) -> Path:
    """One RESULTS.md-adjacent machine-readable summary of stage progression.

    No invented precision/recall. Only counts already present in the
    manifest's stage_counts per company are surfaced here. Companies whose
    claim/tag stages were never run (kepco) report those stages as
    "not_run" rather than as zero counts.
    """
    rows = []
    for company in manifest["companies"]:
        if company["status"] == "missing":
            rows.append(
                {
                    "slug": company["slug"],
                    "status": "missing",
                    "reason": company["reason"],
                }
            )
            continue
        if company["status"] == "parse_only":
            sc = company["stage_counts"]
            rows.append(
                {
                    "slug": company["slug"],
                    "status": "parse_only",
                    "pages": company["pages"],
                    "PARSE": sc.get("PARSE", {}),
                    "EXTRACT": sc.get("EXTRACT", "not_run"),
                    "TAG": sc.get("TAG", "not_run"),
                    "reason": company["reason"],
                }
            )
            continue
        sc = company["stage_counts"]
        if company["slug"] == "lotte":
            after = sc["after"]
            row = {
                "slug": company["slug"],
                "status": "ok",
                "pages": company["pages"],
                "claim_count": after["claim_count"],
                "tagged": after["tagged"],
                "blocked": after["blocker_counts"],
                "pages_processed": after["coverage"]["pages_processed"],
                "pages_total": after["coverage"]["pages_total"],
                "run_status": after["run_status"],
            }
            if "api_comparison" in sc:
                row["api_comparison"] = {
                    "status": sc["api_comparison"].get("status"),
                    "mode": sc["api_comparison"].get("mode"),
                    "cost_usd": sc["api_comparison"].get("cost_usd"),
                    "known_failure_row_count": len(
                        sc["api_comparison"].get("known_failure_rows", [])
                    ),
                    "note": "see corpus manifest.json for full failure rows; "
                    "counts here are not accuracy",
                }
            rows.append(row)
        else:
            row = {
                "slug": company["slug"],
                "status": "ok",
                "pages": company["pages"],
                "claim_count": sc["claims"],
                "source_verified": sc["source_verified"],
                "reviews": sc["reviews"],
                "blocked": sc["blocked"],
            }
            if "api_comparison" in sc:
                row["api_comparison"] = {
                    "status": sc["api_comparison"].get("status"),
                    "mode": sc["api_comparison"].get("mode"),
                    "cost_usd": sc["api_comparison"].get("cost_usd"),
                    "visual_adjudication": sc["api_comparison"].get("visual_adjudication"),
                    "known_failure_row_count": len(
                        sc["api_comparison"].get("known_failure_rows", [])
                    ),
                    "note": "see corpus manifest for full failure rows; "
                    "counts here are not accuracy",
                }
            rows.append(row)
    content = json.dumps({"companies": rows}, ensure_ascii=False, indent=2) + "\n"
    return write_idempotent_versioned(output_dir / "baseline_summary.json", content)


def external_parser_targets(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """Callable seam: same fixed inputs, for a coordinator's external-parser run.

    Returns exactly the source_sha256/pdf_path/pages that an external parser
    (e.g. paid Upstage enhanced parse) should run against to be comparable to
    this baseline, for every company with a usable page selection (both "ok"
    and "parse_only" status). Does not invoke any parser or model itself.
    `--print-targets` on the CLI prints this as JSON for a separate process/
    coordinator to consume. Lotte already has a real same-page Upstage
    artifact recorded (see manifest.companies[lotte].stage_counts.
    api_comparison); this seam does not imply it needs to be re-run.
    """
    return [
        {
            "slug": c["slug"],
            "pdf_path": c["pdf_path"],
            "source_sha256": c["source_sha256"],
            "pages": c["pages"],
            "already_has_api_comparison": bool(
                isinstance(c.get("stage_counts"), dict) and "api_comparison" in c["stage_counts"]
            ),
        }
        for c in manifest["companies"]
        if c["status"] in ("ok", "parse_only")
    ]


def _cli(argv: list[str] | None = None) -> int:
    import argparse

    default_out = ROOT / "outputs/pipeline-recovery-20260920"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus-out",
        default=str(default_out / "corpus"),
        help="Directory to write manifest.json (and review packet) into",
    )
    parser.add_argument(
        "--baseline-out",
        default=str(default_out / "baseline"),
        help="Directory to write baseline_summary.json into",
    )
    parser.add_argument(
        "--with-review-packet",
        action="store_true",
        help="Also build the 30-50 source-bound diagnostic review packet",
    )
    parser.add_argument(
        "--print-targets",
        action="store_true",
        help="Print the same-input external-parser comparison targets as JSON and exit",
    )
    args = parser.parse_args(argv)

    corpus_out = Path(args.corpus_out)
    baseline_out = Path(args.baseline_out)

    manifest_path = write_manifest(corpus_out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    if args.print_targets:
        print(json.dumps(external_parser_targets(manifest), ensure_ascii=False, indent=2))
        return 0

    summary_path = write_baseline_summary(baseline_out, manifest)
    print(f"wrote {manifest_path}")
    print(f"wrote {summary_path}")
    print(f"companies_ok={manifest['companies_ok']}")
    print(f"companies_parse_only={manifest['companies_parse_only']}")
    print(f"companies_missing={manifest['companies_missing']}")

    if args.with_review_packet:
        packet_path = write_review_packet(corpus_out)
        if packet_path is None:
            print("review packet: skipped (fewer than 5 recoverable source-bound cases)")
        else:
            packet = json.loads(packet_path.read_text(encoding="utf-8"))
            print(f"wrote {packet_path} ({len(packet['cases'])} cases)")
            html_path = write_review_html(corpus_out, packet_path)
            if html_path is not None:
                print(f"wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
