"""Partial review must remain useful without promoting unresolved source quotes."""

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_partial_summary_not_run_candidates_and_escaping(tmp_path):
    entry = tmp_path / "partial.tsx"
    code = """
import React from REACT;
import {renderToStaticMarkup} from SERVER;
import {PartialClaimSummary} from CLAIMS;
import assert from 'node:assert/strict';
const ref={source_id:'s',page_num:48,quote:'재생에너지 <b>조달</b> 확대',
verification_state:'verified'};
const elements=[{element_id:'G1',state:'present',normalized_value:'2040년',evidence_refs:[]},
 {element_id:'G6',state:'unknown',normalized_value:null,evidence_refs:[ref]},
 {element_id:'G4',state:'conflict',normalized_value:null,evidence_refs:[ref]},
 {element_id:'G3',state:'unknown',normalized_value:null,evidence_refs:[]}];
const render=(decision,projection=null,items=elements)=>renderToStaticMarkup(
 React.createElement(PartialClaimSummary,
 {decision,projection,elements:items,rawCandidates:[],onSourceOpen:()=>{}}));
for(const decision of [null,{decision_status:'not_run',missing_elements:[]}]) {
 const html=render(decision);
 for(const text of ['검토 초안','2040년','G6','G4','원문 48쪽','검토 후보','&lt;b&gt;'])
 assert.ok(html.includes(text),text);
 assert.ok(!html.includes('요건 미충족'));
 assert.ok(!html.includes('<b>조달</b>'));
 assert.ok(!html.includes('미검증 근거 후보'));
}
const approval=render(null,{blocked_reason:'DOMAIN_RULEPACK_UNAPPROVED',
blocked_action:'판정기준 승인이 필요합니다.'});
assert.ok(approval.includes('판정기준 승인이 필요합니다.'));
assert.ok(!approval.includes('DOMAIN_RULEPACK_UNAPPROVED'));
assert.equal(render({decision_status:'decided'},null), '');
assert.ok(render(null,null,[]).includes('아직 없습니다'));
console.log('partial summary passed');
"""
    for key, path in {
        "REACT": "apps/web/node_modules/react/index.js",
        "SERVER": "apps/web/node_modules/react-dom/server.node.js",
        "CLAIMS": "apps/web/src/features/claims/ClaimWorkspace.tsx",
    }.items():
        code = code.replace(key, json.dumps(str(ROOT / path)))
    entry.write_text(code)
    esbuild = next((ROOT / "node_modules/.pnpm").glob("esbuild@*/node_modules/esbuild/bin/esbuild"))
    bundle = tmp_path / "partial.cjs"
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
    assert "partial summary passed" in result.stdout
