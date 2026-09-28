"""One-command local competition demo for the developer-A pipeline.

This is a thin, safe wrapper around ``evaluation.local_upstage_pilot`` for the
"show the judges a working local review UI" flow. It performs no model or cloud
calls of its own and adds no new application framework: it only

1. copies an *existing* seeded pilot state directory to a writable demo copy so
   the immutable original (real PDF pipeline output + saved real model receipts)
   is never mutated, and
2. resumes that copy through the pilot's built-in same-origin server, which
   serves the built React UI, authenticates a loopback-only session, and
   exposes the real claims / source / review / report-export endpoints.

The heavy lifting (auth, registry, run replay, export build) is entirely the
existing pilot + API. Analysis stays explicit-invoke behind the existing
cost/consent gates; this wrapper never passes ``--invoke`` and never performs a
paid retry. Blocked domain approvals surface as pending/untagged, never as a
forged grade.

Usage (resume a seeded run and serve the UI on loopback):

    uv run python scripts/submission_demo.py \
        --seed .local/developer-a-service-kakao-2025 \
        --port 8790

The command prints the exact loopback login URL to open in a browser. Stop with
Ctrl+C. Re-running with the same ``--demo-state`` reuses the writable copy when
it holds the same run; to rebuild, pass a new ``--demo-state`` path (the wrapper
never deletes a directory, so it cannot erase a seed or an unrelated path).

Safety: this wrapper never passes ``--invoke`` to the pilot, so a resume/serve
demo makes no model or cloud call and cannot trigger a paid retry. Live model
runs are launched separately, outside this launcher.
"""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Policy flags in pilot.json that the pilot resume guard compares. The wrapper
# only reads them to echo a human-readable summary; the pilot itself restores
# them from the manifest under --resume.
_SUMMARY_KEYS = (
    "model",
    "selected_pages",
    "verify_paragraphs",
    "verify_tables",
    "verify_merged_tables",
    "raster_ocr",
    "live_tagging",
    "live_relations",
)


def _load_manifest(seed: Path) -> dict:
    manifest = seed / "pilot.json"
    if not manifest.is_file():
        raise SystemExit(f"seed has no pilot.json: {manifest}")
    return json.loads(manifest.read_text())


def _assert_disjoint(seed: Path, demo_state: Path) -> None:
    """Refuse any seed/demo-state overlap *before* touching the filesystem.

    Equality, ancestor, or descendant relationships could let a later copy or
    cleanup mutate the immutable seed. We validate up front and never delete an
    existing directory, so the wrapper can never remove an arbitrary path.
    """
    seed = seed.resolve()
    demo_state = demo_state.resolve()
    if seed == demo_state:
        raise SystemExit("--demo-state must differ from --seed to keep the seed immutable")
    if seed in demo_state.parents:
        raise SystemExit("--demo-state must not live inside --seed")
    if demo_state in seed.parents:
        raise SystemExit("--demo-state must not be an ancestor of --seed")


def _prepare_writable(seed: Path, demo_state: Path) -> None:
    """Create the writable copy, or reuse an existing matching demo copy.

    The seed is only ever read. An existing demo-state is reused only when it is
    a pilot state for the *same* run; otherwise the operator must choose a new
    ``--demo-state`` path. The wrapper never deletes a directory.
    """
    _assert_disjoint(seed, demo_state)
    seed_manifest = _load_manifest(seed)
    if demo_state.exists():
        demo_manifest_path = demo_state / "pilot.json"
        if not demo_manifest_path.is_file():
            raise SystemExit(
                f"--demo-state {demo_state} exists but is not a pilot state; "
                "choose a new --demo-state path"
            )
        demo_manifest = json.loads(demo_manifest_path.read_text())
        if demo_manifest.get("run_id") != seed_manifest.get("run_id"):
            raise SystemExit(
                f"--demo-state {demo_state} holds a different run "
                f"({demo_manifest.get('run_id')}); choose a new --demo-state path"
            )
        return
    shutil.copytree(seed, demo_state)
    # Seeds are stored read-only (0o700 dirs, 0o600 files); make the copy writable.
    for path in demo_state.rglob("*"):
        try:
            path.chmod(path.stat().st_mode | 0o200)
        except OSError:
            pass


def _parse_pilot_status(stdout: str) -> dict | None:
    """Ignore log records; keep only the pilot's final status object."""
    status = None
    for line in stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "pipeline_outcome" in obj:
            status = obj
    return status


def _latest_inspection(demo_state: Path, previous: set[Path]) -> dict | None:
    candidates = sorted(set(demo_state.glob("inspection-*.json")) - previous)
    if not candidates:
        return None
    try:
        result = json.loads(candidates[-1].read_text())
        return result if isinstance(result, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def _coverage_from_inspection(inspection: dict | None) -> dict:
    """Count the returned API page; a blocked decision is not a grade."""
    claims = (inspection or {}).get("claims")
    if not isinstance(claims, dict) or not isinstance(claims.get("items"), list):
        return {}
    items = claims["items"]
    decisions = [item.get("decision") or {} for item in items]
    graded = sum(
        d.get("decision_status") == "decided"
        and d.get("evidence_grade") in {"E0", "E1", "E2", "E3"}
        for d in decisions
    )
    excluded = sum(d.get("decision_status") == "not_applicable" for d in decisions)
    return dict(
        total=len(items),
        graded=graded,
        pending=len(items) - graded - excluded,
        not_applicable=excluded,
        has_more=bool(claims.get("next_cursor")),
    )


def _resume_command(demo_state: Path, port: int) -> str:
    return shlex.join(
        [
            sys.executable,
            "-m",
            "evaluation.local_upstage_pilot",
            "--resume",
            "--state",
            str(demo_state),
            "--port",
            str(port),
            "--serve",
        ]
    )


def summarize_outcome(
    status: dict | None, *, demo_state: Path, port: int, coverage: dict | None = None
) -> list[str]:
    """Report observed state and a read-only reopen command, never infer completeness."""
    lines = ["", "== run outcome (from the pilot's own status + saved claims) =="]
    if status is None:
        lines.append("  pilot status line not found; inspect the saved inspection-*.json directly.")
    else:
        outcome = status.get("pipeline_outcome") or {}
        for label in ("run_id", "selected_pages", "claim_pages"):
            lines.append(f"  {label}: {status.get(label)}")
        lines.extend(
            [
                f"  pipeline stage: {outcome.get('stage')}",
                f"  pipeline status: {outcome.get('status', 'unknown')}",
            ]
        )
    if coverage:
        lines.append(
            f"  visible page only: {coverage['graded']}/{coverage['total']} claims decided, "
            f"{coverage['pending']} pending; {coverage.get('not_applicable', 0)} not applicable"
        )
        if coverage.get("has_more"):
            lines.append("  More claim pages exist; these counts are not report totals.")
        if coverage["pending"]:
            lines.append(
                f"  REMAINING: {coverage['pending']} claim(s) have no rule-engine grade yet. "
                "A generated ZIP or opened review is a PARTIAL result, not a completed report."
            )
            lines.append(
                "  Open claim details to resolve source, classification, tagging or "
                "rule blockers. Reopening alone does not process pending work."
            )
    lines.append(f"  reopen (read-only, same results): {_resume_command(demo_state, port)}")
    lines.append("  [Reviewer Next Actions / 검토 가이드]")
    lines.append(
        "  1. Claims (주장 목록): /runs/<run_id>/claims - 추출된 주장과 규칙 판정 상태 확인"
    )
    lines.append("  2. Source Evidence (원문 근거): 주장 상세의 '원문 보기'로 원문 쪽수·발췌 확인")
    lines.append(
        "  3. Export (보고서 내보내기): /runs/<run_id>/report - "
        "검토용 부분 결과(JSON/CSV/HTML) 내보내기"
    )
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        type=Path,
        required=True,
        help="Existing immutable pilot state directory (contains pilot.json). "
        "It is copied, never mutated.",
    )
    parser.add_argument(
        "--demo-state",
        type=Path,
        default=None,
        help="Writable copy location (default: <seed>-demo alongside the seed).",
    )
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument(
        "--no-serve",
        action="store_true",
        help="Prepare/resume and print run status, but do not start the server.",
    )
    args = parser.parse_args()

    seed = args.seed if args.seed.is_absolute() else (ROOT / args.seed)
    seed = seed.resolve()
    if not seed.is_dir():
        raise SystemExit(f"seed directory not found: {seed}")
    manifest = _load_manifest(seed)

    demo_state = args.demo_state or seed.with_name(seed.name + "-demo")
    demo_state = (demo_state if demo_state.is_absolute() else (ROOT / demo_state)).resolve()
    _prepare_writable(seed, demo_state)

    summary = {key: manifest.get(key) for key in _SUMMARY_KEYS}
    print("== ESG ProofOps submission demo ==", flush=True)
    print(f"seed (immutable): {seed}", flush=True)
    print(f"demo state (writable copy): {demo_state}", flush=True)
    print(f"run_id: {manifest.get('run_id')}", flush=True)
    print(f"pipeline: {json.dumps(summary, ensure_ascii=False)}", flush=True)
    print(
        "zero automatic paid invocation: live model calls (--invoke) are strictly disabled; "
        "replaying saved seed results.",
        flush=True,
    )
    if not manifest.get("live_tagging"):
        print(
            "note: this run is extraction-only; claims show tag_status=untagged "
            "(pending), never a forged grade.",
            flush=True,
        )

    # Delegate to the pilot's resume+serve. --resume reconstructs pdf/year/period
    # and all policy flags from pilot.json. We deliberately never append
    # --invoke, so the demo makes no model or cloud call and cannot trigger a
    # paid retry; live runs require separate authorization.
    cmd = [
        sys.executable,
        "-m",
        "evaluation.local_upstage_pilot",
        "--resume",
        "--state",
        str(demo_state),
        "--port",
        str(args.port),
    ]
    assert "--invoke" not in cmd, "submission demo must never invoke paid model calls"
    if not args.no_serve:
        cmd.append("--serve")
        print(
            f"Starting loopback server on http://127.0.0.1:{args.port} "
            "(Ctrl+C to stop). The login URL is printed below and saved to "
            f"{demo_state / 'browser.json'}.",
            flush=True,
        )
        print("Reviewer steps:", flush=True)
        print(
            "  1. Claims (주장): Review extracted claims and rulepack evaluation state", flush=True
        )
        print(
            "  2. Original Source (원문 보기): Open claim detail and inspect source quote/page",
            flush=True,
        )
        print("  3. Export (보고서): Download partial review report in JSON/CSV/HTML", flush=True)
        # Serve is an interactive, long-lived blocking server: stream its stdout
        # straight through so the login URL and logs appear live. We do not
        # capture/parse here.
        served = subprocess.run(cmd, cwd=ROOT, check=False)
        return served.returncode

    print("Resume-only (no server) requested.", flush=True)
    previous = set(demo_state.glob("inspection-*.json"))
    status = None
    # Stream a potentially slow source replay, retaining only its final status.
    with subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, text=True) as process:
        assert process.stdout is not None
        for line in process.stdout:
            print(line, end="", flush=True)
            status = _parse_pilot_status(line) or status
        returncode = process.wait()
    coverage = _coverage_from_inspection(_latest_inspection(demo_state, previous))
    for line in summarize_outcome(status, demo_state=demo_state, port=args.port, coverage=coverage):
        print(line, flush=True)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
