const assert = require("node:assert/strict");
const fs = require("node:fs");
const { test } = require("node:test");
const ts = require("typescript");
for (const extension of [".ts", ".tsx"]) {
  require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(
    fs.readFileSync(filename, "utf8"), { compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
    } }).outputText, filename);
}
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { gradeSummary } = require("./resultModel.ts");
const { ResultView } = require("./HostedResult.tsx");
const claim = {
  claim_id: "fixture", page_num: 1, original_page_num: 26, quote: "Offline fixture",
  track: null, provisional_track: "performance", decision: null, revision: 1,
  source_refs: [], source_quality: "unverified", elements: [], missing_evidence: [],
  confirmed_grade: null, possible_grade_range: null, hold_reason: "BUDGET_EXHAUSTED",
  provisional_grade: { evidence_grade: "E1", display: "잠정·미확정", reason: "needs_review",
    grade_basis: "reachable_floor", grade_range: { floor: "E1", ceiling: "E3", open_elements: ["P3"] } },
};
const result = { claims: [claim], reviews: [], hold_reasons: [], page_map: [26], pipeline: { status: "completed" } };
test("R108 extraction budget hold explains unanalyzed items without claiming money exhaustion", () => {
  const html = renderToStaticMarkup(React.createElement(ResultView, {
    result: { ...result, hold_reasons: ["R108_BUDGET_EXHAUSTED"], pipeline: { status: "partial_blocked" } },
  }));
  assert.match(html, /분석 예산 한도로 일부 항목을 미분석/);
  assert.match(html, /호출 수·시간·비용 기록/);
  assert.doesNotMatch(html, /보류 사유 코드: R108_BUDGET_EXHAUSTED/);
  assert.match(html, /확정 등급 아님/);
});
test("server evidence_grade is displayed with range, open elements and hold", () => {
  const summary = gradeSummary(claim);
  assert.match(summary.headline, /잠정 등급 E1/);
  assert.equal(summary.confirmed, false);
  assert.match(summary.note, /E1–E3/);
  assert.match(summary.note, /P3/);
  assert.match(summary.note, /needs_review/);
});
test("detail uses provisional track without confirming it", () => {
  const html = renderToStaticMarkup(React.createElement(ResultView, { result }));
  assert.match(html, /성과 트랙 · 잠정 분류/);
  assert.doesNotMatch(html, /트랙 합의 전/);
});
test("completed empty scope differs from a blocked empty result", () => {
  const render = (pipeline, holds) => renderToStaticMarkup(React.createElement(ResultView, {
    result: { ...result, claims: [], pipeline, hold_reasons: holds },
  }));
  assert.match(render({ status: "completed" }, []), /선택한 범위의 분석이 정상 완료되었으나 환경 주장을 발견하지 못했습니다/);
  assert.match(render({ status: "partial_blocked" }, ["BUDGET_EXHAUSTED"]), /분석이 보류되어 표시할 주장이 없습니다/);
});
test("v2 whole range shows actionable hold without confirming unknown", () => {
  const held = { ...claim, provisional_grade: { evidence_grade: null, display: "판정 보류", display_type: "hold", reason: "blocked_evidence", grade_range: { floor: "E0", ceiling: "E3", open_elements: ["P3"] }, hold_reasons: ["원문 미검증"], needed_evidence: [{ track: "performance", target_grade: "E2", elements: ["P3"] }], evidence_note: "원문 확인 필요" } };
  assert.equal(gradeSummary(held).headline, "판정 보류");
  const html = renderToStaticMarkup(React.createElement(ResultView, { result: { ...result, claims: [held] } }));
  assert.match(html, /보류 이유: 원문 미검증/);
  assert.match(html, /등급 검토에 필요한 근거 요소/);
  assert.match(html, /원문 확인 필요/);
  assert.doesNotMatch(html, /확정 등급 E[0-3]/);
});
