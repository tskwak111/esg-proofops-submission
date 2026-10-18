const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText, filename);
const React = require('react');
const { renderToStaticMarkup: render } = require('react-dom/server');
const { w18HoldReason, claimHoldReasons, claimSourceText, resultBrowserSummary } = require('./resultModel.ts');
const { holdReasonText } = require('./hostedClient.ts');
const { ResultView } = require('./HostedResult.tsx');
const { resultReportHtml } = require('./ResultReport.tsx');
const { gradeMeanings, stageActivity } = require('./ux1Model.ts');
const fixture = JSON.parse(fs.readFileSync(`${__dirname}/../../../test/fixtures/w21/confirmed-majority.result.json`));
const held = { ...fixture.claims[124], source_quality:'verified', source_status:{status:'verified',display:'원문 확인'}, hold_reason:'PRELIMINARY_TAGS_UNRESOLVED', provisional_grade:{display_type:'hold',hold_reasons:['원문 위치 확인 필요'], reason:'PRELIMINARY_TAGS_UNRESOLVED'} };

test('verified claim sentence is not relabeled unverified by a generic evidence-location reason', () => {
  assert.equal(claimSourceText(held),'주장 원문 · 검증됨');
  assert.equal(w18HoldReason(fixture,held),'주장 유형 미합의');
  assert.ok(claimHoldReasons(held).includes('요소 근거·연결의 원문 위치 추가 확인 필요'));
  assert.equal(claimSourceText({...held,source_status:{status:'unverified'}}),'주장 원문 · 확인 필요');
  assert.equal(w18HoldReason(fixture,{...held,source_quality:'unverified',source_status:undefined}),'주장 원문 미확인');
});

test('processing failures outrank synthetic type/source fallbacks in cards and detailed reasons', () => {
  for(const reason of ['PRELIMINARY_UNEXPECTED_OperationalError','TAG_FAILED','TRANSPORT_FAILED']) {
    const claim={...held,pipeline_status:'failed',hold_reason:reason,cf6_hold_reasons:[reason],provisional_grade:{...held.provisional_grade,reason,hold_reasons:['트랙 미합의']}};
    assert.equal(w18HoldReason(fixture,claim),'처리 오류로 보류');
    assert.match(holdReasonText(reason),reason === "TRANSPORT_FAILED" ? /통신이 실패/ : /처리 오류/);
    assert.deepEqual(claimHoldReasons(claim),['처리 오류로 보류했습니다. 이 오류는 기업의 근거 결손을 뜻하지 않습니다.']);
    const html=render(React.createElement(ResultView,{result:{...fixture,claims:[claim]}}));
    assert.match(html,/처리 오류로 보류/);
    assert.doesNotMatch(html,/주장 유형 미합의|상세 사유는 제공되지/);
  }
});

test('summary export includes every confirmed claim and hold totals, full detail remains complete', () => {
  const before=JSON.stringify(fixture),counts=resultBrowserSummary(fixture);
  const html=resultReportHtml(fixture,{reportMode:'summary',reportName:'<script>unsafe</script>'});
  assert.equal((html.match(/data-summary-claim-id=/g)||[]).length,counts.confirmed);
  assert.equal((html.match(/<article data-claim-id=/g)||[]).length,0);
  for(const text of ['요약본','등급 분포','확정 주장 목록','주요 보류 사유']) assert.ok(html.includes(text));
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
  const full=resultReportHtml(fixture,{reportMode:'full'});
  assert.equal((full.match(/<article data-claim-id=/g)||[]).length,fixture.claims.length);
  assert.equal(JSON.stringify(fixture),before);
  const screen=render(React.createElement(ResultView,{result:fixture}));
  assert.match(screen,/<option value="summary" selected="">요약본/);
  assert.match(screen,/<option value="full">전체 상세/);
});

test('E2 guide names the distinct E3 additions without asserting every track needs assurance', () => {
  const e2=gradeMeanings.find(g=>g.grade==='E2');
  for(const text of ['목표','진척','전환계획','성과','산정방법','보증','관리체계','외부검증']) assert.ok(e2.text.includes(text),text);
  assert.doesNotMatch(e2.text,/검증 연결이 미비/);
});

test('elapsed stage time is observational and does not manufacture processed claims', () => {
  const run={run_id:'r',status:'running',stage:'tag',claims_processed:0,claims_extracted:214,progress:.75};
  const first=stageActivity(run,0),later=stageActivity(run,65);
  assert.match(first.elapsed,/0초/);assert.match(later.elapsed,/1분 5초/);
  assert.match(later.description,/독립 판독|근거/);
  assert.match(later.note,/완료 건수|완료율/);
  assert.equal(run.claims_processed,0);assert.equal(run.progress,.75);
  assert.equal(stageActivity({...run,status:'completed'},90),null);
});


test('FX4 stage counters render only valid measured counts, missing fields stay optional', () => {
  const run={run_id:'fx4',status:'running',stage:'tag',claims_processed:0};
  assert.deepEqual(stageActivity(run,0).counters,[]);
  assert.deepEqual(stageActivity({...run,claim_stage_counts:{preliminary:16,relation:8,elements:0}},60).counters.map(c=>c.completed),[16,8,0]);
  assert.deepEqual(stageActivity({...run,claim_stage_counts:{preliminary:-1,relation:NaN,elements:1.5}},60).counters,[]);
  const {UX1Progress}=require('./UX1Progress.tsx');
  const html=render(React.createElement(UX1Progress,{run:{...run,claim_stage_counts:{preliminary:16,relation:8,elements:0}}}));
  for(const label of ['유형 처리','근거 관계 처리','요소 처리','16건','8건','0건','성공 판정 수와는 다릅니다']) assert.ok(html.includes(label));
});


test('known run failures retain their actionable provider/parsing explanation', () => {
  assert.match(holdReasonText('PROVIDER_AUTH_FAILED'),/인증에 실패/);
  assert.match(holdReasonText('PARSER_FAILED'),/파싱에 실패/);
});

test('unresolved claim type keeps its hold meaning without retired wording in detail and export', () => {
  const result = {...fixture, claims:[held]};
  for (const html of [render(React.createElement(ResultView,{result})), resultReportHtml(result,{reportMode:'full'})]) {
    assert.match(html, /주장 유형\(트랙\)이 합의되지 않아 요소를 판정하지 않았습니다/);
    assert.doesNotMatch(html, /예비|잠정/);
  }
  assert.equal(resultBrowserSummary(result).hold,1);
  assert.equal(resultBrowserSummary(result).confirmed,0);
});
