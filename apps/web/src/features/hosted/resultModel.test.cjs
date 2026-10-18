const assert = require("node:assert/strict");
const fs = require("node:fs");
const { test } = require("node:test");
const ts = require("typescript");
for (const extension of [".ts", ".tsx"]) {
  require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(
    fs.readFileSync(filename, "utf8"), { compilerOptions: {
      target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
    } }).outputText, filename);
}
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { holdReasonText } = require("./hostedClient.ts");
const { claimHoldReasons, elementStateText, gradeSummary, groupClaimOccurrences, tableObservationText } = require("./resultModel.ts");
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
const auto = { ...claim, hold_reason: null, source_quality: "verified", confirmed_grade: "E3", confirmed_label: "SUBSTANTIATED",
  confirmation_source: "auto_unanimous_v1", confirmation_display: "자동 확정", confirmation_policy_sha256: "a".repeat(64),
  provisional_grade: null, decision: { evidence_grade: "E3", label: "SUBSTANTIATED", decision_status: "decided", review_status: "auto_confirmed", gap_ids: [] } };
test("C3 direct certification renders the server basis and applies automatic guards", () => {
  const c3 = { ...auto, confirmation_source: "auto_direct_certification_c3_v1",
    confirmation_basis: "프로젝트 루브릭상 공시 내 입증요소 충족. EMS의 ISO 14001 직접 인증과 적용범위(국내 전 생산 사업장, 글로벌 17개 중 15개)를 p36에서 확인. 보고서 문맥: 2025; 인증 유효기간: 미기재." };
  assert.equal(gradeSummary(c3).headline, 'E3 (입증 요건 충족)');
  assert.equal(gradeSummary(c3).note, c3.confirmation_basis);
  assert.equal(gradeSummary({ ...c3, confirmation_policy_sha256: undefined }).confirmed, false);
  assert.equal(gradeSummary({ ...c3, confirmation_basis: undefined }).confirmed, false);
  assert.equal(gradeSummary({ ...c3, decision: { ...c3.decision, review_status: "needs_review" } }).confirmed, false);
});
test("DEC-43 lower bound is supplementary and never confirmed", () => {
  const floor = { ...claim, source_quality: "verified", lower_bound_display: "하한 E1, 최종 미확정" };
  assert.equal(gradeSummary(floor).confirmed, false);
  assert.match(gradeSummary(floor).note, /하한 E1, 최종 미확정/);
  assert.doesNotMatch(gradeSummary({ ...floor, lower_bound_display: undefined }).note, /하한 E1, 최종 미확정/);
});
test("unanimous confirmation displays its grade, label and distinct source", () => {
  const summary = gradeSummary(auto);
  assert.equal(summary.confirmed, true);
  assert.equal(summary.headline, 'E3 (입증 요건 충족)');
  const html = renderToStaticMarkup(React.createElement(ResultView, { result: { ...result, claims: [auto] } }));
  // The automatic source shows in the panel note, not as a headline prefix.
  assert.ok(html.includes(summary.note), summary.note);
  assert.doesNotMatch(html, /확정 등급|자동 확정 E/);
  assert.doesNotMatch(html.match(/data-testid="grade-panel"[^]*?<\/div>/)[0], /잠정 등급|잠정 요소/);
  const human = { ...auto, confirmation_source: "human_review", decision: { ...auto.decision, review_status: "human_confirmed" } };
  assert.match(gradeSummary(human).headline, /^E3/);
  assert.equal(gradeSummary(human).note, "사람 확인");
});
test("confirmation source remains hidden when the optional API field is absent", () => {
  const olderApi = { ...auto, confirmation_source: undefined, confirmation_display: undefined };
  assert.match(gradeSummary(olderApi).headline, /^E3/);
  assert.doesNotMatch(gradeSummary(olderApi).headline, /자동 확정/);
});
test("automatic source cannot bypass held, gap, inconsistent grade or missing policy guards", () => {
  for (const bad of [
    { source_quality: "unverified" }, { decision: { ...auto.decision, decision_status: "blocked_rule_gap" } },
    { decision: { ...auto.decision, gap_ids: ["GAP-005"] } }, { confirmed_grade: "E2" },
    { confirmation_policy_sha256: undefined }, { confirmed_label: "INCOMPLETE" },
    { hold_reason: "PROCESSING_UNCERTAIN" }, { decision: { ...auto.decision, grade_range: { rule_gap: true } } },
  ]) assert.equal(gradeSummary({ ...auto, ...bad }).confirmed, false);
});
test("R108 extraction budget hold explains unanalyzed items without claiming money exhaustion", () => {
  const html = renderToStaticMarkup(React.createElement(ResultView, {
    result: { ...result, hold_reasons: ["R108_BUDGET_EXHAUSTED"], pipeline: { status: "partial_blocked" } },
  }));
  assert.match(html, /분석 예산 한도로 일부 항목을 미분석/);
  assert.match(html, /호출 수·시간·비용 기록/);
  assert.doesNotMatch(html, /보류 사유 코드: R108_BUDGET_EXHAUSTED/);
  assert.match(html, /판정 보류/);
});
test("server evidence_grade is displayed with range, open elements and hold", () => {
  const summary = gradeSummary(claim);
  assert.match(summary.headline, /추가 확인 필요 · E1/);
  assert.equal(summary.confirmed, false);
  assert.match(summary.note, /E1–E3/);
  assert.match(summary.note, /P3/);
  assert.match(summary.note, /사람 검토가 필요합니다/);
});
test("unknown and absent keep distinct user-facing labels", () => {
  assert.equal(elementStateText("unknown", "verified"), "인정된 근거 미연결(확인 필요)");
  assert.equal(elementStateText("absent", "verified"), "근거 없음(검증된 검색 범위)");
  assert.equal(holdReasonText("근거 없음"), "인정된 근거 미연결(확인 필요)");
  assert.match(holdReasonText("SOURCE_VALIDATION_REQUIRED:unverified"), /원문 대조가 끝나지 않아 판정하지 않았습니다/);
});
test("table observation summary shows only supplied years, row hierarchy, unit, facility, column and comparisons", () => {
  const summary = tableObservationText({
    parent_path: ["환경", "폐기물"], metric: "일반 폐기물", year: "2025", value: "140", unit: "톤",
    facility_name: "여수 사업장", column_name: "배출량", comparisons: [{ year: "2024", value: "284" }],
    verification_state: "unresolved",
  });
  assert.match(summary, /지표 계층: 환경 › 폐기물 › 일반 폐기물/);
  assert.match(summary, /2025년 140 톤/);
  assert.match(summary, /시설: 여수 사업장/);
  assert.match(summary, /열: 배출량/);
  assert.match(summary, /2024년 284 톤/);
  assert.match(summary, /표 행·값 연결 확인 필요/);
  assert.equal(tableObservationText(undefined), null);
});
test("duplicate occurrences render as one card and retain each occurrence context", () => {
  const first = { ...claim, claim_id: "occurrence-1", page_num: 1, quote: "같은 수치 문장",
    duplicate_occurrences: ["occurrence-1", "occurrence-2"],
    table_observation: { year: "2025", metric: "일반 폐기물", value: "140", unit: "톤" } };
  const second = { ...claim, claim_id: "occurrence-2", page_num: 2, quote: "같은 수치 문장",
    duplicate_occurrences: ["occurrence-1", "occurrence-2"],
    table_observation: { year: "2025", metric: "지정 폐기물", value: "284", unit: "톤" } };
  const group = groupClaimOccurrences([first, second]);
  assert.equal(group.length, 1);
  assert.equal(group[0].occurrenceCount, 2);
  const html = renderToStaticMarkup(React.createElement(ResultView, {
    result: { ...result, claims: [first, second], page_map: [44, 45] },
  }));
  assert.equal((html.match(/<button type="button" class="claim-item/g) ?? []).length, 1);
  assert.match(html, /같은 문장 2곳/);
  assert.match(html, /일반 폐기물/);
  assert.match(html, /지정 폐기물/);
  assert.match(html, /p\.44/);
  assert.match(html, /p\.45/);
});
test("optional hold detail uses Korean text and never leaks known machine codes", () => {
  const held = { ...claim, hold_reason: null, hold_reason_detail: "blocked_rule_gap",
    provisional_grade: { evidence_grade: null, display: "판정 보류", display_type: "hold", reason: "blocked_rule_gap",
      hold_reason_detail: "blocked_rule_gap", hold_reasons: ["blocked_evidence", "근거 없음"], needed_evidence: [],
      evidence_note: "근거 없음은 이 패킷에 인정된 근거가 없다는 뜻이며 문서 전체 부재 판정이 아닙니다." } };
  const html = renderToStaticMarkup(React.createElement(ResultView, { result: { ...result, claims: [held] } }));
  assert.match(claimHoldReasons(held).join(" "), /판정 규칙에 미정 항목/);
  assert.match(html, /판정 규칙에 미정 항목/);
  assert.match(html, /주장에 대한 근거 요소를 정확히 판단할 수 없어/);
  assert.ok(claimHoldReasons(held).some(reason => reason === "인정된 근거 미연결(확인 필요)"));
  assert.match(html, /확인되지 않은 요소는 현재 분석 범위에서 인정된 근거와 연결되지 않았습니다/);
  assert.doesNotMatch(html, /근거 없음은 이 패킷에 인정된 근거/);
  assert.doesNotMatch(html, /blocked_rule_gap|blocked_evidence/);
});
test("detail uses provisional track without confirming it", () => {
  const html = renderToStaticMarkup(React.createElement(ResultView, { result }));
  assert.match(html, /성과형 · 분류 확인 중/);
  assert.doesNotMatch(html, /트랙 합의 전/);
});
test("completed empty scope differs from a blocked empty result", () => {
  const render = (pipeline, holds) => renderToStaticMarkup(React.createElement(ResultView, {
    result: { ...result, claims: [], pipeline, hold_reasons: holds },
  }));
  // A finished run no longer shows a completion card; the empty-state line carries the difference.
  assert.doesNotMatch(render({ status: "completed" }, []), /class="result-execution completed"/);
  assert.match(render({ status: "completed" }, []), /선택한 범위에서 표시할 추출 주장이 없습니다/);
  assert.doesNotMatch(render({ status: "completed", extraction_outcome: { status: "partial" } }, []), /분석 완료 ·|실행 완료 ·|정상 완료/);
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

test('stale confirmed-grade fields cannot display a final label on an unverified or held claim', () => {
  for (const override of [{ source_quality: 'unverified' }, { source_quality: 'verified', decision_status: 'blocked_evidence' }]) {
    const stale = { ...claim, source_quality: override.source_quality, confirmed_grade: 'E0',
      decision: { decision_status: override.decision_status ?? 'decided', evidence_grade: 'E0', label: 'UNSUBSTANTIATED' },
      provisional_grade: null, possible_grade_range: { floor: 'E0', ceiling: 'E3', open_elements: ['P3'] } };
    const summary = gradeSummary(stale);
    assert.equal(summary.confirmed, false);
    const html = renderToStaticMarkup(React.createElement(ResultView, { result: { ...result, claims: [stale] } }));
    assert.doesNotMatch(html, /UNSUBSTANTIATED/);
    assert.match(html, /E0–E3/);
  }
});
const {w18HoldReason,w18HoldBreakdown,resultBrowserSummary}=require('./resultModel.ts');
test('W18 hold labels preserve cause distinctions and count each held claim once',()=>{
  const held={...claim,source_quality:'verified',hold_reason:null,provisional_grade:null};
  const reasons=[['REPLICA_HEADER_UNRESOLVED','반복 판독 미완료'],['CONSENSUS_UNRESOLVED','3회 판단 불일치'],['BODY_TABLE_CONFLICT','본문·표 수치 충돌'],['OUT_OF_SCOPE','분석 범위 밖'],['blocked_evidence','요소 근거 부족·미연결']];
  const claims=reasons.map(([code,label],i)=>{const c={...held,claim_id:String(i),hold_reason:code};assert.equal(w18HoldReason(result,c),label);return c;});
  const fixture={...result,claims:[...claims,{...held,claim_id:'unknown',source_quality:'unverified'},auto,claim]};
  const before=JSON.stringify(fixture);
  assert.equal(w18HoldBreakdown(fixture).reduce((n,v)=>n+v.count,0),resultBrowserSummary(fixture).hold);
  assert.equal(w18HoldReason(fixture,auto),null);assert.equal(w18HoldReason(fixture,claim),null);
  assert.equal(JSON.stringify(fixture),before);
  const html=renderToStaticMarkup(React.createElement(ResultView,{result:fixture}));
  assert.match(html,/주요 보류 사유/);assert.match(html,/보류 · 반복 판독 미완료/);
});
test('W18 missing reason stays unknown and uses server detail for unmapped codes',()=>{
  const held={...claim,source_quality:'verified',hold_reason:null,provisional_grade:null};
  assert.equal(w18HoldReason(result,held),'보류 사유 미제공');
  assert.equal(w18HoldReason({...result,hold_reason_details:[{code:'NEW_REASON',message:'추가 원문 확인 필요'}]}, {...held,hold_reason:'NEW_REASON'}),'추가 원문 확인 필요');
});

const w20 = require('../../../test/fixtures/w20/scope-confirmation.result.json');
test('DEC-123: server scope confirmation uses the confirmed label and keeps inspected pages', () => {
  const scoped = w20.claims[0];
  assert.equal(gradeSummary(scoped).confirmed, true);
  assert.equal(gradeSummary(scoped).headline, 'E1 (일부 요건 충족)');
  assert.match(gradeSummary(scoped).note, /검사한 원본 쪽: 24–26/);
  assert.equal(elementStateText('absent_in_scope', 'verified'), '근거 없음(분석 범위 내)');
  for (const change of [
    { sc1_confirmation: null }, { sc1_hold_reason: 'SC1_CONFLICT' }, { source_quality: 'unverified' },
    ...[{ evidence_grade: null }, { label: null }, { decision_status: 'blocked_evidence' },
      { confirmation_basis: undefined }, { confirmation_policy_sha256: undefined },
      { gap_ids: ['GAP-005'] }, { grade_range: { rule_gap: true } }]
      .map(change => ({ sc1_confirmation: { ...scoped.sc1_confirmation, ...change } })),
  ]) assert.equal(gradeSummary({ ...scoped, ...change }).confirmed, false, JSON.stringify(change));
  assert.equal(gradeSummary({ ...scoped, sc1_confirmation: null }).headline, '추가 확인 필요 · E1');
});
