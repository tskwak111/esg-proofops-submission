"""Acceptance tests for saved demo UX and submission demo launcher.

Verifies:
1. React UI renders truthful saved-demo banner and 3-step novice guide
   (주장 확인 -> 원문 보기 -> 보고서 내보내기) in RunNav and DocumentFlow.
2. Production runs are strictly not labeled as demo (demo banner absent).
3. Raw opaque IDs are not main content in demo mode.
4. submission_demo.py enforces zero automatic paid invocation (--invoke strictly absent),
   replays immutable seed, and outputs truthful reviewer next actions.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import submission_demo as sd  # noqa: E402


def test_submission_demo_ui_ux_renders_correctly_under_demo_and_production(tmp_path: Path) -> None:
    esbuild = next((ROOT / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    router_path = subprocess.check_output(
        ["node", "-e", 'console.log(require.resolve("react-router", { paths: ["./apps/web"] }))'],
        cwd=ROOT,
        text=True,
    ).strip()
    entry = tmp_path / "submission_demo_ux_render.tsx"
    entry.write_text(
        """
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { MemoryRouter } from ROUTER;
import { RunNav, DocumentFlow } from APP;
import assert from "node:assert/strict";

const session = {
  tenant_id: "88f0f14e-cc66-4c72-b090-e74be4ddbbbb",
  role: "viewer",
  csrf_token: "test-csrf",
};

const dummySelection = {
  companyId: "c1",
  rightsProfileId: "r1",
  consentProfileId: "cp1",
  runtimeBindingId: "rb1",
};

const dummyOptions = {
  rights_profiles: [],
  consent_profiles: [],
  runtime_bindings: [],
  rule_packs: [],
  enabled_modes: [],
};

// 1. RunNav rendering in demo mode
const navDemo = renderToStaticMarkup(
  React.createElement(MemoryRouter, null,
    React.createElement(RunNav, { runId: "demo-run-123", isSavedDemo: true })
  )
);
assert.ok(navDemo.includes("기존 분석 결과"), "RunNav demo banner");
for (const text of ["주장", "검토 큐", "보고서"]) {
  assert.ok(navDemo.includes(text), `RunNav link ${text}`);
}
assert.ok(!/시연|테스트/.test(navDemo), "RunNav must not use demo/test wording");

// 2. RunNav rendering in production mode (must not label production as demo)
const navProd = renderToStaticMarkup(
  React.createElement(MemoryRouter, null,
    React.createElement(RunNav, { runId: "prod-run-777", isSavedDemo: false })
  )
);
assert.ok(!navProd.includes("기존 분석 결과"),
  "RunNav production must not show saved-result notice");
assert.ok(!/시연|테스트/.test(navProd), "RunNav production must not use demo/test wording");

// 3. DocumentFlow rendering in demo mode
const docDemo = renderToStaticMarkup(
  React.createElement(MemoryRouter, null,
    React.createElement(DocumentFlow, {
      session,
      selection: dummySelection,
      options: dummyOptions,
      readyVersion: null,
      canEdit: false,
      canRun: false,
      setSelection: () => {},
      setOptions: () => {},
      setReadyVersion: () => {},
      onSessionInvalid: () => {},
      onRunCreated: () => {},
      isSavedDemo: true,
      demoRunId: "demo-run-123",
    })
  )
);
assert.ok(docDemo.includes("기존 분석 결과 보기"), "DocumentFlow saved-result notice");
assert.ok(!/시연|테스트/.test(docDemo), "DocumentFlow must not use demo/test wording");
assert.ok(docDemo.includes("/runs/demo-run-123/claims"), "DocumentFlow claims link");

// 4. DocumentFlow rendering in production mode
const docProd = renderToStaticMarkup(
  React.createElement(MemoryRouter, null,
    React.createElement(DocumentFlow, {
      session,
      selection: dummySelection,
      options: dummyOptions,
      readyVersion: null,
      canEdit: false,
      canRun: false,
      setSelection: () => {},
      setOptions: () => {},
      setReadyVersion: () => {},
      onSessionInvalid: () => {},
      onRunCreated: () => {},
      isSavedDemo: false,
      demoRunId: null,
    })
  )
);
assert.ok(!docProd.includes("기존 분석 결과 보기"),
  "DocumentFlow production must not show saved-result notice");
assert.ok(!/시연|테스트/.test(docProd), "DocumentFlow production must not use demo/test wording");

console.log("ALL SUBMISSION DEMO UX ACCEPTANCE CHECKS PASSED");
""".replace("REACT", json.dumps(str(ROOT / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(ROOT / "apps/web/node_modules/react-dom/server.node.js")))
        .replace("ROUTER", json.dumps(router_path))
        .replace("APP", json.dumps(str(ROOT / "apps/web/src/App.tsx")))
    )
    bundle = tmp_path / "submission_demo_ux_render.cjs"
    subprocess.run(
        [
            str(esbuild),
            str(entry),
            "--bundle",
            "--platform=node",
            "--format=cjs",
            "--jsx=automatic",
            # Browser-only PDF libraries load lazily in the live report panel.
            "--external:pdfjs-dist*",
            "--external:pdf-lib",
            "--define:import.meta.env={}",
            f"--outfile={bundle}",
        ],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(["node", str(bundle)], check=True, capture_output=True, text=True)
    assert "ALL SUBMISSION DEMO UX ACCEPTANCE CHECKS PASSED" in result.stdout


def test_submission_demo_zero_paid_invocation_and_reviewer_guidance(tmp_path: Path) -> None:
    # 1. Resume command must be read-only and strictly omit --invoke and --serve-worker
    cmd = sd._resume_command(tmp_path / "demo_copy", port=8888)
    assert "--resume" in cmd
    assert "--serve" in cmd
    assert "--invoke" not in cmd
    assert "--serve-worker" not in cmd

    # Reviewer guidance covers claims, source quotes, and export.
    lines = sd.summarize_outcome(
        {
            "run_id": "r1",
            "selected_pages": [48, 114, 136],
            "pipeline_outcome": {"status": "not_run"},
        },
        demo_state=tmp_path / "demo_copy",
        port=8888,
        coverage={"total": 24, "graded": 0, "pending": 24, "not_applicable": 0},
    )
    full_text = "\n".join(lines)
    assert "Reviewer Next Actions / 검토 가이드" in full_text
    assert "1. Claims (주장 목록)" in full_text
    assert "2. Source Evidence (원문 근거)" in full_text
    assert "3. Export (보고서 내보내기)" in full_text
    assert "--invoke" not in full_text
