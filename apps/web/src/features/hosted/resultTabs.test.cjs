const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText, filename);
const React = require('react');
const { renderToStaticMarkup: render } = require('react-dom/server');
const { resultTabFromSearch, resultTabUrl, runIdFromResultLink } = require('./hostedHistory.ts');
const { ResultView } = require('./HostedResult.tsx');
const { ExecutiveSummary, HoldReasonList, ResultDashboard } = require('./UX1Insights.tsx');
const { holdReasonLabel } = require('./resultModel.ts');
const fixture = JSON.parse(fs.readFileSync(`${__dirname}/../../../test/fixtures/w21/confirmed-majority.result.json`));
const runA = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
const runB = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';

function withLocation(search, hash, fn) {
  global.window = { location: { pathname: '/analyze', search, hash } };
  try { return fn(); } finally { delete global.window; }
}
const tabs = html => [...html.split('<div class="result-tabs"')[1].split('</div>')[0].matchAll(/<button[^>]*role="tab"[^>]*>/g)].map(match => match[0]);
const panel = (html, key) => new RegExp(`<div class="result-tabpanel result-tabpanel-${key}" role="tabpanel"[^>]*>`).exec(html)?.[0];
const panelBody = (html, key) => html.split(panel(html, key))[1].split('<div class="result-tabpanel ')[0].replace(/<\/section>$/, '');

test('tab URL keeps the #run- anchor and other params; overview stays implicit', () => {
  assert.equal(resultTabFromSearch(''), 'overview');
  assert.equal(resultTabFromSearch('?tab=claims'), 'claims');
  assert.equal(resultTabFromSearch('?tab=bogus'), 'overview');
  assert.equal(resultTabUrl({ pathname: '/analyze', search: '' }, runA, 'source'), `/analyze?tab=source#run-${runA}`);
  assert.equal(resultTabUrl({ pathname: '/analyze', search: '?tab=source&x=1' }, runA, 'overview'), `/analyze?x=1#run-${runA}`);
  assert.equal(resultTabUrl({ pathname: '/analyze', search: '?x=1' }, runA, 'info'), `/analyze?x=1&tab=info#run-${runA}`);
});

test('shared result links with only a tab param still open the run; other queries stay rejected', () => {
  assert.equal(runIdFromResultLink(`/analyze?tab=claims#run-${runA}`), runA);
  assert.equal(runIdFromResultLink(`https://example.test/analyze?tab=source#run-${runA}`), runA);
  assert.equal(runIdFromResultLink(`/analyze?token=secret#run-${runA}`), null);
  assert.equal(runIdFromResultLink(`/analyze?tab=claims&tab=info#run-${runA}`), null);
});

test('result renders an ARIA tablist; only the selected panel is visible and the rest stay hidden', () => {
  const html = render(React.createElement(ResultView, { result: fixture, runId: runA }));
  assert.match(html, /role="tablist" aria-label="결과 보기 방식"/);
  assert.match(html, /role="tablist" aria-label="검토 상태"/, 'inner review-state tabs keep a distinct label');
  const buttons = tabs(html);
  assert.equal(buttons.length, 4);
  assert.deepEqual(buttons.map(button => /aria-selected="true"/.test(button)), [true, false, false, false]);
  assert.deepEqual(buttons.map(button => /tabindex="0"/.test(button)), [true, false, false, false]);
  for (const button of buttons) {
    const controls = /aria-controls="([^"]+)"/.exec(button)[1];
    const id = /id="([^"]+)"/.exec(button)[1];
    assert.match(html, new RegExp(`id="${controls}" aria-labelledby="${id}"`));
  }
  assert.match(html, />문장별 결과<span class="result-tab-badge">\d+<\/span>/);
  assert.doesNotMatch(panel(html, 'overview'), /hidden/);
  // Static render emits every panel (hidden); lazy mounting is browser-only and covered by test/result-tabs-smoke.mjs.
  for (const key of ['claims', 'source', 'info']) assert.match(panel(html, key), /hidden=""/);
  assert.match(panelBody(html, 'claims'), /claims-section/);
  assert.match(panelBody(html, 'overview'), /rd-overview/);
  assert.doesNotMatch(panelBody(html, 'overview'), /result-report-summary/);
});

test('initial tab comes from ?tab= only for the run named in the anchor, so each run keeps its own tab', () => {
  const [a, b] = withLocation('?tab=claims', `#run-${runA}`, () => [runA, runB].map(runId => render(React.createElement(ResultView, { result: fixture, runId }))));
  assert.match(tabs(a)[1], /aria-selected="true"/);
  assert.match(panelBody(a, 'claims'), /claims-section/);
  assert.match(panelBody(a, 'claims'), /aria-label="검토 상태"/);
  assert.match(tabs(b)[0], /aria-selected="true"/);
  assert.match(panel(b, 'claims'), /hidden=""/);
  assert.doesNotMatch(panel(b, 'overview'), /hidden/);
});

test('info tab carries the run summary, guide, scope and period notes; the heading repeats the period warning', () => {
  const warning = '보고기간 추정 실패(기본값 사용)';
  const result = { ...fixture, pipeline: { ...fixture.pipeline, reporting_scope: { ...fixture.pipeline?.reporting_scope, warning } } };
  const html = withLocation('?tab=info', `#run-${runA}`, () => render(React.createElement(ResultView, { result, runId: runA, runSummary: React.createElement('details', { className: 'hosted-run-summary' }) })));
  const heading = html.split('<div class="ux1-export">')[0];
  assert.match(heading, /data-testid="result-warning-badge"/);
  assert.ok(heading.includes(warning));
  const info = panelBody(html, 'info');
  for (const part of ['hosted-run-summary', 'ux1-grade-guide', 'result-scope', warning]) assert.ok(info.includes(part), part);
  assert.ok(!html.split(panel(html, 'info'))[0].includes('hosted-run-summary'), 'run summary no longer sits above the heading');
});

test('hold reasons live in the overview dashboard; buttons only when the result view can route them', () => {
  const plain = render(React.createElement(HoldReasonList, { result: fixture }));
  const routed = render(React.createElement(HoldReasonList, { result: fixture, onHoldReason: () => {} }));
  assert.doesNotMatch(plain, /<button/);
  if (/<li>/.test(plain)) assert.match(routed, /<li><button type="button" title="[^"]+">[^<]+<b>\d+건<\/b><\/button>/, 'label then count, no middle dot');
  const html = render(React.createElement(ResultView, { result: fixture, runId: runA }));
  const overview = panelBody(html, 'overview');
  assert.equal(overview.split('주요 보류 사유').length - 1, 1, 'shown once, in the status panel');
  assert.match(overview.split('rd-holds-h')[1].split('</div>')[0], /ux1-holds|보류 주장이 없습니다/);
  assert.doesNotMatch(render(React.createElement(ExecutiveSummary, { result: fixture, onSelect: () => {}, holds: false })), /주요 보류 사유/);
});

test('dashboard handles zero confirmed claims without dividing by zero', () => {
  const html = render(React.createElement(ResultDashboard, { result: { ...fixture, claims: [] } }));
  assert.match(html, /확정된 주장이 없습니다/);
  assert.doesNotMatch(html, /NaN|Infinity/);
});

test('long hold reasons get short display names; short ones stay', () => {
  assert.equal(holdReasonLabel('분석 시간 한도로 새 처리를 중단했습니다. 완료된 결과와 미처리 주장을 구분해 표시합니다.'), '분석 시간 한도로 미처리');
  assert.equal(holdReasonLabel('추가 확인이 필요한 항목입니다. 상세 사유는 제공되지 않았습니다.'), '상세 사유 미제공');
  assert.equal(holdReasonLabel('주장 유형 미합의'), '주장 유형 미합의');
});

test('completed extraction gaps keep the same counted status in the heading and overview', () => {
  const { completedRunPresentation } = require('./hostedClient.ts');
  for (const claims of [fixture.claims, []]) {
    for (const extraction_outcome of [
      { status: 'partial', failed_sources: [{source_id:'unprocessed-1'}] },
      { status: 'partial', page_failures: [{physical_page:24}, {physical_page:25}] },
      { status: 'partial', failed_sources: [{source_id:'unprocessed-1'}], page_failures: [{physical_page:24}] },
    ]) {
      const result = {...fixture, claims, pipeline:{...fixture.pipeline, status:'completed', extraction_outcome}};
      const run = {run_id:runA, document_id:runB, status:'completed', selected_pages:[24,25], result};
      const expected = completedRunPresentation(result,run.status).title;
      const html=render(React.createElement(ResultView,{result,runId:runA,runStatus:run.status}));
      assert.ok(html.includes(`data-testid="result-completion-status">${expected}</p>`));
      assert.ok(html.includes(`<h3>${expected}</h3>`));
      assert.doesNotMatch(html,/부분 완료/);
    }
  }
});
