"""R28: reviewed facility context read projection in the claim detail web view.

Renders the ReviewedContextSection with a synthetic AI-delegated reviewed_context
and asserts the small read-only projection shows the reviewed dimensions with
source quote/page, honest AI-delegated (not human gold) provenance, the P6 hold
text (never a grade pass), the excluded outside_reviewed_section count, and that
untrusted quote text is React-escaped. Uses the existing esbuild + renderToStaticMarkup
pattern with the already-installed toolchain; no new dependency is introduced.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_reviewed_context_read_projection_renders(tmp_path: Path) -> None:
    esbuild = next((ROOT / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    entry = tmp_path / "reviewed-context-render.tsx"
    entry.write_text(
        """
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { ReviewedContextSection } from CLAIMS;
import assert from "node:assert/strict";
const ref=(page,quote)=>({source_id:"source-"+page,document_version_id:"version",
parse_manifest_id:"manifest",page_num:page,printed_page_label:null,bbox:null,
raw_text_sha256:"a".repeat(64),quote,char_start:0,char_end:quote.length,
location_quality:"located",verification_state:"verified"});
const context={origin:"ai_delegated",dimensions:{
facility:ref(3,"울산 <b>1</b>공장"),reporting_period:ref(3,"2024 회계연도"),
metric:ref(4,"온실가스 배출량"),value:ref(4,"12,345"),unit:ref(4,"tCO2e")},
numeric_check:{status:"needs_review",reason:"no_comparable_table_observation",
considered:[{source_ref:ref(9,"타 사업장 합계"),status:"outside_reviewed_section"},
{source_ref:ref(9,"연결 합계"),status:"outside_reviewed_section"},
{source_ref:ref(4,"자체 표"),status:"own_source_unresolved"}]}};
const html=renderToStaticMarkup(React.createElement(ReviewedContextSection,{context}));
// Reviewed dimensions with page and quote are surfaced.
assert.ok(html.includes("사업장") && html.includes("보고기간") && html.includes("지표"));
assert.ok(html.includes("2024 회계연도") && html.includes("12,345") && html.includes("tCO2e"));
assert.ok(html.includes("3쪽") && html.includes("4쪽"));
// Honest AI-delegated provenance, not human gold.
assert.ok(html.includes("위임") && html.includes("사람"));
// P6 stays on hold; the copy explicitly refuses present / grade pass.
assert.ok(html.includes("검토 필요") && html.includes("동일 사업장·기간의 비교 근거가 없습니다"));
assert.ok(html.includes("비교 가능한 표 근거를 연결해야"));
// Excluded outside_reviewed_section count only (2 of 3), not the own-source one.
assert.ok(html.includes("제외된 비교 후보: 2건"));
// React escaping of untrusted quote text; no raw markup injected.
assert.ok(html.includes("&lt;b&gt;") && !html.includes("<b>1</b>"));
console.log("R28 reviewed-context projection passed");
""".replace("REACT", json.dumps(str(ROOT / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(ROOT / "apps/web/node_modules/react-dom/server.node.js")))
        .replace(
            "CLAIMS", json.dumps(str(ROOT / "apps/web/src/features/claims/ClaimWorkspace.tsx"))
        )
    )
    bundle = tmp_path / "reviewed-context-render.cjs"
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
    assert "R28 reviewed-context projection passed" in result.stdout
