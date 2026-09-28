"""AT-036: fixed API DTOs and native keyboard-accessible web controls."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_claim_list_contract_is_closed_and_versioned() -> None:
    contract = yaml.safe_load((ROOT / "contracts/openapi.yaml").read_text())
    assert contract["openapi"].startswith("3.")
    assert contract["info"]["version"]
    response = contract["paths"]["/v1/runs/{run_id}/claims"]["get"]["responses"]["200"]
    assert response["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ClaimSummaryPage"
    }
    page = contract["components"]["schemas"]["ClaimSummaryPage"]
    assert page["additionalProperties"] is False
    assert set(page["required"]) == {"items", "next_cursor", "snapshot_epoch"}


def test_accessible_components_render_native_keyboard_controls(tmp_path: Path) -> None:
    esbuild = next((ROOT / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    entry = tmp_path / "accessibility-render.tsx"
    entry.write_text(
        """
import React from REACT;
import { renderToStaticMarkup } from SERVER;
import { SourceViewer } from SOURCE;
import { StatusBadge } from STATUS;
import { ReviewWorkspace } from REVIEW;
import assert from "node:assert/strict";
const ref={source_id:"source",document_version_id:"version",parse_manifest_id:"manifest",
page_num:2,printed_page_label:null,bbox:null,raw_text_sha256:"a".repeat(64),
quote:"<script>alert(1)</script>",char_start:0,char_end:8,
location_quality:"unlocated",verification_state:"verified"};
const source=renderToStaticMarkup(React.createElement(SourceViewer,{runId:"run",csrfToken:"csrf",
sources:[ref],onSessionInvalid:()=>{}}));
assert.ok(source.includes('<button type="button"') && source.includes("원문 위치 열기"));
assert.ok(source.includes("연결 상태: 위치 없음") && source.includes("&lt;script&gt;"));
assert.ok(!source.includes("data-source-highlight"));
const badge=renderToStaticMarkup(
React.createElement(StatusBadge,{label:"검토 필요",tone:"warning"}));
assert.ok(badge.includes("status-badge") && badge.includes("검토 필요")
&& badge.includes("aria-hidden"));
const props={session:{tenant_id:"tenant",user_id:"user",role:"reviewer",csrf_token:"csrf"},
review:{review_id:"review",run_id:"run",claim_id:"claim",status:"open",revision:1,base_tag_revision:1,reason_codes:[]},
track:"performance",elements:[{element_id:"P1",state:"unknown",evidence_refs:[ref],normalized_value:null,
credited_from:null,reason_code:null}],loadLatest:async()=>{},onResolved:()=>{},onSourceOpen:()=>{}};
const review=renderToStaticMarkup(React.createElement(ReviewWorkspace,props));
assert.ok(review.includes("<select") && review.includes("<dialog"));
assert.ok(review.includes("원문 위치 열기") && review.includes("변경 확인")
&& review.includes("취소"));
console.log("AT-036 component semantics passed");
""".replace("REACT", json.dumps(str(ROOT / "apps/web/node_modules/react/index.js")))
        .replace("SERVER", json.dumps(str(ROOT / "apps/web/node_modules/react-dom/server.node.js")))
        .replace("SOURCE", json.dumps(str(ROOT / "apps/web/src/components/SourceViewer.tsx")))
        .replace("STATUS", json.dumps(str(ROOT / "apps/web/src/components/StatusBadge.tsx")))
        .replace(
            "REVIEW", json.dumps(str(ROOT / "apps/web/src/features/reviews/ReviewWorkspace.tsx"))
        )
    )
    bundle = tmp_path / "accessibility-render.cjs"
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
    assert "AT-036 component semantics passed" in result.stdout


def test_focus_and_summary_refresh_hooks_are_wired() -> None:
    review = (ROOT / "apps/web/src/features/reviews/ReviewWorkspace.tsx").read_text()
    progress = (ROOT / "apps/web/src/features/runs/RunProgress.tsx").read_text()
    app = (ROOT / "apps/web/src/App.tsx").read_text()
    assert "onClose" in review and ".focus()" in review
    assert "onRunChanged" in progress
    assert "onRunChanged" in app and "summaryEpoch" in app
