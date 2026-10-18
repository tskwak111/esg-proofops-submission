const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText, filename);
const React = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const model = require('./resultModel.ts');
const { ResultView } = require('./HostedResult.tsx');
const fixtures = ['lg', 'kakao'].map(name => [name, JSON.parse(fs.readFileSync(`${__dirname}/../../../test/fixtures/w12/${name}.result.json`))]);

for (const [name, result] of fixtures) test(`${name}: real result projection retains every claim, grade, citation and hold`, () => {
  const items = result.claims.map(claim => model.resultBrowserItem(result, claim));
  assert.equal(items.length, name === 'lg' ? 98 : 109);
  for (let i = 0; i < items.length; i++) {
    const item = items[i], claim = result.claims[i];
    assert.equal(item.claim, claim);
    assert.equal(item.page, `p.${claim.original_page_num}`);
    assert.equal(item.track, claim.track ?? claim.provisional_track ?? 'unknown');
    assert.equal(item.confirmed, false);
    assert.equal(item.bucket, claim.provisional_grade?.display_type === 'hold' || !claim.provisional_grade?.evidence_grade ? 'hold' : 'provisional');
    assert.equal(item.grade, item.bucket === 'provisional' ? claim.provisional_grade.evidence_grade : null);
    assert.deepEqual(item.elements, claim.provisional_grade?.elements ?? claim.elements);
    for (const element of item.elements) if (element.state === 'unknown') assert.doesNotMatch(model.resultElementText(claim, element), /근거 없음/);
  }
  const counts = model.resultBrowserSummary(result);
  assert.equal(counts.confirmed, 0);
  assert.equal(counts.provisional + counts.hold, result.claims.length);
  assert.equal(Object.values(counts.grades).reduce((a, b) => a + b, 0), counts.provisional);
  assert.equal(counts.stageSeconds, Object.values(result.pipeline.stage_seconds).reduce((a, b) => a + b, 0));
  const html = renderToStaticMarkup(React.createElement(ResultView, { result }));
  for (const label of ['문장별 결과', '전체 트랙', '전체 등급', '전체 상태', '처리 시간', '요소별 근거']) assert.ok(html.includes(label), label);
  assert.doesNotMatch(html, /확정 등급 E[0-3]|SUBSTANTIATED|INCOMPLETE|UNSUBSTANTIATED/);
  assert.equal((html.match(/aria-label="요소별 근거"/g) ?? []).length, 1);
});

test('filters intersect search, track, grade, status and bucket without treating a range as a grade', () => {
  const result = fixtures[0][1];
  const item = result.claims.map(claim => model.resultBrowserItem(result, claim)).find(item => item.bucket === 'provisional');
  const filters = { queue: 'provisional', search: item.claim.quote, track: item.track, grade: item.grade, status: 'provisional' };
  assert.equal(model.matchesResultFilters(item, filters), true);
  for (const change of [{ queue: 'hold' }, { grade: 'none' }, { track: 'unknown' }, { search: 'no-such-claim' }, { status: 'confirmed' }]) assert.equal(model.matchesResultFilters(item, { ...filters, ...change }), false);
  const held = result.claims.map(claim => model.resultBrowserItem(result, claim)).find(item => item.bucket === 'hold');
  assert.equal(model.matchesResultFilters(held, { queue: 'all', search: '', track: 'all', grade: 'E0', status: 'all' }), false);
});

test('unverified or missing present citation stays unverified; missing metadata stays hidden', () => {
  const result = fixtures[0][1], claim = result.claims[0];
  const present = claim.provisional_grade.elements.find(element => element.state === 'present');
  assert.equal(model.resultElementText(claim, present), '근거 확인');
  assert.equal(model.resultElementText(claim, { ...present, evidence_refs: [] }), '원문 대조 필요');
  assert.equal(model.resultElementText(claim, { ...present, evidence_refs: present.evidence_refs.map(ref => ({ ...ref, verification_state: 'unverified' })) }), '원문 대조 필요');
  assert.equal(model.resultElementText({ ...claim, source_quality: 'unverified' }, present), '원문 대조 필요');
  assert.equal(model.resultBrowserSummary({ ...result, pipeline: {} }).stageSeconds, null);
  const held = model.resultBrowserItem(result, { ...claim, provisional_grade: { ...claim.provisional_grade, display_type: 'hold', evidence_grade: 'E0' } });
  assert.equal(held.grade, null);
  assert.equal(held.badge, '판정 보류');
  assert.equal(model.gradeSummary(held.claim).headline, '판정 보류');
  const html = renderToStaticMarkup(React.createElement(ResultView, { result: { ...result, claims: [held.claim] } }));
  assert.doesNotMatch(html, /잠정 등급 E0|잠정 E0/);
});

test('execution is partial despite completed terminal status; zero claims never implies success', () => {
  const { resultExecution, runOutcome } = require('./hostedClient.ts');
  const empty = { schema: 'r108-hosted-result-v1', claims: [], reviews: [], hold_reasons: [], page_map: [24, 41], pipeline: { status: 'completed', extraction_outcome: { status: 'empty', code: 'NO_CLAIMS_IN_PROCESSED_BLOCKS' } } };
  assert.equal(resultExecution(empty).kind, 'completed');
  const partial = { ...empty, pipeline: { ...empty.pipeline, extraction_outcome: { status: 'partial', code: 'CLAIM_SOURCE_EXTRACTION_FAILURE', failed_sources: [{ physical_page: 2, source_id: 'missing-source' }] } } };
  assert.equal(resultExecution(partial).kind, 'partial');
  assert.equal(runOutcome({ status: 'completed', result: partial }).kind, 'blocked');
  const html = renderToStaticMarkup(React.createElement(ResultView, { result: partial }));
  for (const text of ['부분 완료', '확정 조건을 충족한 주장은 0건입니다', 'p.41', 'missing-source', '원문 확인', '원본 쪽', '기업의 결손이 아닙니다']) assert.ok(html.includes(text), text);
  assert.doesNotMatch(html, /정상 완료|분석 완료 ·|실행 완료 ·/);
  assert.equal(resultExecution(empty, 'failed').kind, 'failed');
  assert.equal(resultExecution({ ...empty, pipeline: { status: 'partial_blocked', reason: 'BUDGET_EXHAUSTED' } }).kind, 'partial');
  assert.equal(resultExecution({ ...empty, pipeline: {} }).kind, 'unknown');
});

test('failure reasons preserve server details and prepared-to-original page mapping', () => {
  const result = { ...fixtures[0][1], page_map: [24, 41], hold_reasons: [], hold_reason_details: [{ code: 'MODEL_SPAN_OR_SCHEMA_INVALID', message: '서버가 제공한 추출 구간 불일치 사유' }], pipeline: { status: 'completed', extraction_outcome: { status: 'partial', failed_sources: [
    { physical_page: 2, source_id: 's1', failure_code: 'MODEL_SPAN_OR_SCHEMA_INVALID' },
    { physical_page: 2, source_id: 's2', failure_code: 'MODEL_SPAN_OR_SCHEMA_INVALID' },
    { physical_page: 3, source_id: 's3' }, { source_id: 's4' },
  ], page_failures: [{ physical_page: 1, code: 'CLAIM_PAGE_PARSE_FAILURE' }] } } };
  const groups = model.resultSourceFailures(result);
  assert.equal(groups[0].page, 'p.24');
  assert.deepEqual(groups[1], { page: 'p.41', reason: '서버가 제공한 추출 구간 불일치 사유', sourceIds: ['s1', 's2'], pageFailure: false });
  assert.equal(groups[2].page, '선택본 p.3');
  assert.equal(groups[3].page, '쪽 위치 미제공');
  const html = renderToStaticMarkup(React.createElement(ResultView, { result }));
  assert.ok(html.includes('서버가 제공한 추출 구간 불일치 사유'));
  assert.ok(html.includes('추출 실패 원문 4개'));
});

test('scope shows whole document denominator and exact unselected physical pages without guessing', () => {
  const result = { ...fixtures[0][1], page_map: [2, 5], pipeline: { scope: { pages: [2, 5], claim_pages: [5] } } };
  assert.deepEqual(model.resultScope(result, 6), { pages: [2, 5], total: 6, claims: [5], unselected: [1, 3, 4, 6] });
  assert.equal(model.pageRanges([6, 3, 1, 4, 3]), '1, 3–4, 6');
  assert.equal(model.resultScope(result).unselected, null);
  assert.deepEqual(model.resultScope({ ...result, pipeline: {} }, 6, [1, 2]).pages, [1, 2]);
  const html = renderToStaticMarkup(React.createElement(ResultView, { result, pageCount: 6 }));
  for (const text of ['선택 2쪽 / 전체 6쪽', '주장 추출 대상: 5', '미선택 4쪽', '1, 3–4, 6']) assert.ok(html.includes(text), text);
});

test('verified lower bounds remain provisional and counts are disjoint even with hold reasons', () => {
  const base = fixtures[0][1];
  const claim = base.claims.find(claim => claim.provisional_grade?.evidence_grade && claim.provisional_grade.display_type !== 'hold');
  const floor = { ...claim, source_quality: 'verified', confirmed_grade: null, lower_bound_display: '하한 E1, 최종 미확정', hold_reason: 'REPLICA_UNRESOLVED', provisional_grade: { ...claim.provisional_grade, evidence_grade: 'E1', grade_basis: 'reachable_floor' } };
  const held = { ...floor, claim_id: 'held', provisional_grade: { ...floor.provisional_grade, display_type: 'hold' } };
  const confirmed = { ...floor, claim_id: 'confirmed', hold_reason: null, confirmed_grade: 'E3', confirmation_source: 'human_review', decision: { evidence_grade: 'E3', label: 'SUBSTANTIATED', decision_status: 'decided', review_status: 'human_confirmed' } };
  const result = { ...base, claims: [floor, held, confirmed] };
  const counts = model.resultBrowserSummary(result);
  assert.equal(counts.total, 3); assert.equal(counts.verified, 3);
  assert.equal(counts.confirmed, 1); assert.equal(counts.provisional, 1); assert.equal(counts.hold, 1);
  const html = renderToStaticMarkup(React.createElement(ResultView, { result }));
  assert.ok(html.includes('원문 확인 수는 이 합계에 더하지 않습니다'));
  assert.doesNotMatch(html, /확정 0건 — 이유/);
});

const w20 = require('../../../test/fixtures/w20/scope-confirmation.result.json');
test('DEC-123: confirmed union counts each claim once and drives cards, filters and details', () => {
  const before = JSON.stringify(w20);
  const counts = model.resultBrowserSummary(w20);
  assert.deepEqual([counts.total, counts.confirmed, counts.provisional, counts.hold], [5, 3, 1, 1]);
  const item = model.resultBrowserItem(w20, w20.claims[0]);
  assert.equal(item.bucket, 'confirmed');
  assert.equal(item.grade, 'E1');
  assert.equal(item.badge, 'E1 (일부 요건 충족)');
  assert.equal(item.elements.find(el => el.element_id === 'M2').state, 'absent_in_scope');
  assert.deepEqual(item.elements[0].evidence_refs, w20.claims[0].elements[0].evidence_refs);
  assert.equal(model.matchesResultFilters(item, { queue: 'confirmed', search: '', track: 'all', grade: 'E1', status: 'confirmed' }), true);
  assert.equal(model.w18HoldReason(w20, w20.claims[0]), null);
  const html = renderToStaticMarkup(React.createElement(ResultView, { result: w20 }));
  assert.match(html, /확정<\/span><strong>3<small>건/);
  assert.match(html, /E1 \(일부 요건 충족\)/);
  assert.match(html, /검사한 원본 쪽: 24–26/);
  assert.match(html, /근거 없음\(분석 범위 내\)/);
  assert.doesNotMatch(html, /범위 기준 확정|범위 확정|확정 0건 — 이유|M2 · 원인과 다음 확인/);
  assert.equal(JSON.stringify(w20), before, 'presentation preserves original API data');
});
