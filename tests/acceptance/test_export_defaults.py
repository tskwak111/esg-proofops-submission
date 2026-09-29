"""Review draft defaults remain explicit and visibly partial."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_export_workspace_defaults_render(tmp_path: Path) -> None:
    esbuild = next((ROOT / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    entry = tmp_path / "export-workspace-render.tsx"
    entry.write_text(
        """
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { ExportWorkspace } from EXPORT;
import assert from "node:assert/strict";

const html = renderToStaticMarkup(
    React.createElement(ExportWorkspace, { 
        csrfToken: "test", 
        tenantKey: "tenant", 
        runId: "run", 
        onSessionInvalid: () => {} 
    })
);

// Formats are checked by default
assert.ok(html.includes('checked="" value="json"'));
assert.ok(html.includes('checked="" value="csv"'));
assert.ok(html.includes('checked="" value="html"'));

// Allow partial is checked by default and shows the warning
assert.ok(html.includes('name="allow-partial" checked=""'));
assert.ok(html.includes('미완료 항목을 포함한 검토용 결과입니다. 최종본이 아닙니다.'));

console.log("review-draft export defaults passed");
""".replace("REACT", json.dumps(str(ROOT / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(ROOT / "apps/web/node_modules/react-dom/server.node.js")))
        .replace(
            "EXPORT", json.dumps(str(ROOT / "apps/web/src/features/reports/ExportWorkspace.tsx"))
        )
    )
    bundle = tmp_path / "export-workspace-render.cjs"
    subprocess.run(
        [
            str(esbuild),
            str(entry),
            "--bundle",
            "--platform=node",
            "--format=cjs",
            "--jsx=automatic",
            f"--outfile={bundle}",
        ],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(["node", str(bundle)], check=True, capture_output=True, text=True)
    assert "review-draft export defaults passed" in result.stdout
