const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');
for (const extension of ['.ts', '.tsx']) require.extensions[extension] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText, filename);
const React = require('react');
const { renderToStaticMarkup: render } = require('react-dom/server');
const { UX1_HASH, UX1_POLICY, cleanMetricName, claimExplanation, reportInsights, analyzedData, progressSteps, remainingEstimate } = require('./ux1Model.ts');
const { ResultView } = require('./HostedResult.tsx');
const { resultReportHtml } = require('./ResultReport.tsx');
const { ComparisonResults } = require('./UX1Comparison.tsx');
const { ux1CompanyName } = require('./ux1Metadata.ts');
const { documentName } = require('./documentName.ts');
const fixture = JSON.parse(fs.readFileSync(`${__dirname}/../../../test/fixtures/w21/confirmed-majority.result.json`));
const confirmed = fixture.claims[0];

test('UX1 renders by default with legacy and additive API fields without mutating results', () => {
  assert.equal(require('node:crypto').createHash('sha256').update(UX1_POLICY).digest('hex'), UX1_HASH);
  for (const result of [fixture, {...fixture,pipeline:{...fixture.pipeline,ux1_data:{table_count:12,model_judgment_calls:42}}}]) {
    const before=JSON.stringify(result);
    const screen=render(React.createElement(ResultView,{result}));
    for (const text of ['등급 의미','왜 이 등급인가요','분석한 데이터','PDF 리포트 저장','그린워싱 위험 신호','입증요소 부재']) assert.ok(screen.includes(text),text);
    assert.equal(JSON.stringify(result),before);
  }
});

test('deterministic explanation uses applied rules and never invents a grade for held claims', () => {
  const claim=structuredClone(confirmed);
  const decision=claim.sc1_confirmation ?? claim.decision;
  decision.rule_ids=['MGMT_E1','UNRECOGNIZED_RULE'];
  const text=claimExplanation(fixture,claim);
  assert.match(text.text,/명명된 수단 또는 구체적 시행 상태/);
  assert.match(text.text,/기록된 규칙 UNRECOGNIZED_RULE/);
  assert.deepEqual(text.rules,decision.rule_ids);
  const held=fixture.claims[124];
  const explanation=claimExplanation(fixture,held);
  assert.match(explanation.text,/판정 보류/);
  assert.doesNotMatch(explanation.text,/확정 E[0-3]|E0 규칙/);
  assert.deepEqual(explanation.rules,[]);
});

test('missing and unknown element states are distinct and excluded elements are not shortcomings', () => {
  const claim=structuredClone(confirmed);
  const d=claim.sc1_confirmation ?? claim.decision;
  d.confirmation_basis='cf6_element_agreement'; d.excluded_elements=['M5'];
  claim.cf6_elements=[{element_id:'M1',state:'absent_in_scope',evidence_refs:[]},{element_id:'M2',state:'unknown',evidence_refs:[]},{element_id:'M5',state:'absent',evidence_refs:[]}];
  // Fixture uses a direct confirmed decision for this branch.
  claim.decision=d;claim.sc1_confirmation=null;claim.confirmed_grade=d.evidence_grade;
  const text=claimExplanation(fixture,claim).text;
  assert.match(text,/근거가 연결되지 않은 요소: 이행방법/);
  assert.match(text,/확인이 남은 요소: 적용범위/);
  assert.doesNotMatch(text,/담당 조직/);
});

test('model counters are measured completed judgment calls; missing is never zero or claims times three', () => {
  const data=analyzedData({...fixture,pipeline:{provider_concurrency:{completed_by_role:{preliminary:615,elements:630,extract:91,relation:473}}}});
  assert.equal(data.modelCalls,1245);assert.equal(data.tables,null);
  assert.equal(analyzedData({...fixture,pipeline:{}}).modelCalls,null);
  assert.equal(analyzedData({...fixture,pipeline:{ux1_data:{table_count:0,model_judgment_calls:0}}}).tables,0);
  assert.equal(analyzedData({...fixture,pipeline:{ux1_data:{table_count:-1,model_judgment_calls:NaN}}}).tables,null);
});

test('summary and comparison partition grades by track, keep held claims outside grade totals', () => {
  const insights=reportInsights(fixture);
  assert.equal(insights.top.length,3);assert.ok(insights.top.every(i=>i.confirmed));
  assert.equal(insights.tracks.reduce((n,t)=>n+t.total,0),211);
  assert.equal(insights.tracks.reduce((n,t)=>n+Object.values(t.grades).reduce((a,b)=>a+b,0),0),124);
  const html=render(React.createElement(ComparisonResults,{reports:[{result:fixture,name:'보고서 A',runId:'a',pageCount:119,pages:[1]},{result:{...fixture,claims:[]},name:'보고서 B',runId:'b',pageCount:139,pages:[2]}]}));
  assert.match(html,/31건 · 15%/);assert.match(html,/0건 · 0%/);assert.doesNotMatch(html,/NaN|Infinity/);
});

test('progress reflects grouped server stages and never asserts invented substage completion', () => {
  const run={status:'running',stage:'tag',progress:0.5};
  assert.deepEqual(progressSteps(run).map(s=>s.state),['previous','previous','active','active','waiting','waiting']);
  assert.match(remainingEstimate(run),/10–23분/);
  assert.match(remainingEstimate({...run,status:'queued'}),/대기시간 미정/);
  assert.match(remainingEstimate({...run,progress:NaN}),/계산 중/);
  assert.ok(progressSteps({...run,stage:'unrecognized'}).every(s=>s.state==='waiting'));
});

test('metadata defaults to API fields, restored from API and company derivation requires a clear naming convention', () => {
  const d={display_name:'보고서 1',ux1_metadata:{original_filename:'검증회사 2025 지속가능경영보고서.pdf',title:'내부 제목'}};
  assert.equal(documentName(d),'검증회사 2025 지속가능경영보고서');
  assert.equal(ux1CompanyName(d),'검증회사');
  assert.equal(ux1CompanyName({title:'2025 ESG 보고서'}),undefined);
  assert.equal(ux1CompanyName({title:'report-final.pdf'}),undefined);
  assert.equal(ux1CompanyName({filename:'LGChem_Sustainability_Report_2025_KOR.pdf'}),'LGChem');
  assert.equal(ux1CompanyName({filename:'Kakao_SustainabilityReport2025_KR.pdf'}),'Kakao');
  assert.equal(ux1CompanyName({...d,company_name:'공식 회사'}),'공식 회사');
});

test('full printable report includes all claims and explanations, escapes metadata and user content', () => {
  const html=resultReportHtml(fixture,{reportName:'</h1><script>alert(1)</script>'});
  assert.equal((html.match(/<article data-claim-id=/g)||[]).length,211);
  assert.equal((html.match(/왜 이 등급인가요/g)||[]).length,211);
  assert.match(html,/등급 의미/);assert.match(html,/분석한 데이터/);assert.match(html,/근거 보강 필요 영역/);
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
});

const { AnalyzedData, ExecutiveSummary } = require('./UX1Insights.tsx');
const { UX1Progress } = require('./UX1Progress.tsx');
test('raw table rows and unclassified metrics merge into the track, clean names remain separate', () => {
  for (const name of [undefined, '', '지표 미분류', 'unknown', '에너지 총 소비량 2) / 글로벌 / 136,504 / 146,591', '소비량 | 글로벌 | 32', '소비량 136,504', '소비량\n32']) assert.equal(cleanMetricName(name),'');
  for (const name of ['온실가스 배출량','Scope 1','에너지 소비량']) assert.equal(cleanMetricName(name),name);
  const base=structuredClone(confirmed);
  const d=base.sc1_confirmation ?? base.decision;
  d.confirmation_basis='cf6_element_agreement';d.excluded_elements=[];d.evidence_grade='E1';
  base.cf6_elements=[{element_id:'M1',state:'absent_in_scope',evidence_refs:[]}];
  base.decision=d;base.sc1_confirmation=null;base.confirmed_grade=d.evidence_grade;
  const metrics=['지표 미분류',undefined,'에너지 총 소비량 2) / 글로벌 / 136,504 / 146,591','온실가스 배출량','에너지 소비량','물 사용량','폐기물 발생량','환경 투자','인증 범위'];
  const result={...fixture,claims:metrics.map((metric,i)=>({...base,claim_id:`metric-${i}`,table_observation:metric?{metric}:undefined}))};
  const rows=reportInsights(result).needs;
  assert.equal(rows.length,7);assert.equal(rows.find(r=>!r.metric).count,3);
  assert.equal(rows.reduce((n,r)=>n+r.count,0),metrics.length);
  const html=render(React.createElement(ExecutiveSummary,{result,onSelect(){}}));
  assert.doesNotMatch(html,/지표 미분류|136,504/);
  assert.equal((html.match(/<strong>/g)||[]).length,5);assert.match(html,/나머지 2개 영역/);
});
test('optional data is hidden individually and existing table claims remain useful', () => {
  const result={...fixture,pipeline:{},w16_source_document:undefined};
  const html=render(React.createElement(AnalyzedData,{result}));
  assert.match(html,/표에서 추출한 주장/);assert.match(html,/주장 원문 검증/);
  assert.doesNotMatch(html,/미제공|미집계|<dt>모델 판정 호출|<div><\/div>/);
  const zero=render(React.createElement(AnalyzedData,{result:{...result,pipeline:{ux1_data:{table_count:0,model_judgment_calls:0}}}}));
  assert.match(zero,/<dt>표 수<\/dt><dd>0/);assert.match(zero,/<dt>모델 판정 호출<\/dt><dd>0/);
});
test('progress uses concise completion labels and one estimate line', () => {
  const html=render(React.createElement(UX1Progress,{run:{status:'running',stage:'tag',progress:.55}}));
  for(const label of ['완료','진행 중','대기','예상 남은 시간']) assert.ok(html.includes(label));
  assert.doesNotMatch(html,/앞선 구간|처리 구간|서버가 보고한 단계/);
});

test('analysis examples admit only completed runs and preserve source records', () => {
  const { exampleRuns } = require('./HostedExamples.tsx');
  const rows = [
    {run_id:'old',status:'completed',created_at:'2026-10-01T00:00:00Z'},
    {run_id:'running',status:'running',created_at:'2026-10-17T00:00:00Z'},
    {run_id:'partial',status:'partial_blocked',created_at:'2026-10-16T00:00:00Z'},
    {run_id:'failed',status:'failed',created_at:'2026-10-16T00:00:00Z'},
  ];
  const before=JSON.stringify(rows);
  assert.deepEqual(exampleRuns(rows).map(row=>row.run_id),['old']);
  assert.equal(rows[2].status,'partial_blocked');
  assert.equal(JSON.stringify(rows),before);
});

test('budget notice reads runtime amounts and does not reuse an obsolete event cap', () => {
  const { budgetNotice } = require('./hostedClient.ts');
  assert.equal(budgetNotice({run_usd:'1.0',day_usd:'7',event_usd:'30'}),'분석 비용 한도: 실행당 USD 1.0 · 하루 USD 7 · 행사 전체 USD 30');
  assert.equal(budgetNotice({}), '분석 비용 한도는 현재 서비스 설정을 따릅니다.');
  assert.doesNotMatch(budgetNotice({run_usd:'internal_code',day_usd:-1}), /internal_code|-1/);
});


test('curated examples deduplicate file SHA across pages and select current policy, never old partials', async () => {
  const { loadExamples, CURRENT_EXAMPLE_POLICY } = require('./exampleCatalog.ts');
  const doc=(id,sha,filename)=>({document_id:id,sha256:sha,original_filename:filename,page_count:119});
  const documents=[doc('a','same','Acme_Sustainability_Report_2025_KOR.pdf'),doc('b','same'),doc('c','second','Beta 2025 지속가능경영보고서.pdf'),doc('d','test')];
  const run=(id,document_id,status,created_at)=>({run_id:id,document_id,status,created_at,result_url:`/v1/runs/${id}/result`});
  const runs=[run('new-old-policy','b','completed','2026-10-17'),run('partial','a','partial_blocked','2026-10-18'),run('current','a','completed','2026-10-16'),run('superseded','a','completed','2026-10-15'),run('unversioned','c','completed','2026-10-14'),run('test','d','completed','2026-10-19')];
  const calls=[];
  const result=policy=>({...fixture,pipeline:{...fixture.pipeline,scope:{analysis_profile_sha256:policy}}});
  const client={
    listDocuments:async cursor=>({items:cursor?documents.slice(2):documents.slice(0,2),next_cursor:cursor?null:'next'}),
    listRuns:async cursor=>({items:cursor?runs.slice(2):runs.slice(0,2),next_cursor:cursor?null:'next'}),
    getResult:async url=>{calls.push(url);return url.includes('new-old-policy')?result('old'):url.includes('unversioned')?result(undefined):result(CURRENT_EXAMPLE_POLICY);},
  };
  const examples=await loadExamples(client,new AbortController().signal);
  assert.deepEqual(examples.map(e=>e.run.run_id),['current','unversioned']);
  assert.equal(examples[0].name,'Acme 2025 지속가능경영보고서');
  assert.equal(examples[0].counts.total,211);
  assert.ok(!calls.some(url=>/partial|superseded|test/.test(url)));
});

test('a reused result matches its source policy; known old policy is never a no-pin fallback', async () => {
  const { chooseExample, CURRENT_EXAMPLE_POLICY } = require('./exampleCatalog.ts');
  const rows=['new','old'].map((run_id,i)=>({run_id,status:'completed',created_at:`2026-10-${17-i}`,result_url:`/${run_id}`}));
  const current={...fixture,reuse:{source_policy_sha256:CURRENT_EXAMPLE_POLICY},pipeline:{scope:{analysis_profile_sha256:'wrapper'}}};
  assert.equal((await chooseExample(rows,async()=>current)).run.run_id,'new');
  assert.equal(await chooseExample(rows,async url=>({...fixture,pipeline:{scope:{analysis_profile_sha256:url==='/new'?undefined:'old'}}})),null);
});

test('history names resolve filename metadata across identical uploads without using claim text', () => {
  const { reportNames }=require('./documentName.ts');
  const docs=[{document_id:'named',sha256:'same',ux1_metadata:{original_filename:'Kakao_SustainabilityReport2025_KR.pdf'}},{document_id:'legacy',sha256:'same',display_name:'분석 보고서'},{document_id:'unknown',sha256:'other',display_name:'분석 보고서'}];
  const names=reportNames(docs);
  assert.equal(names.legacy,'Kakao 2025 지속가능경영보고서');
  assert.equal(names.unknown,undefined);
});

const extractionCases = [
  [{ status: 'partial', failed_sources: [{ source_id: 'unprocessed-1', physical_page: 24 }, { source_id: 'unprocessed-2', physical_page: 25 }] }, '분석 완료 · 미처리 원문 2건'],
  [{ status: 'partial', page_failures: [{ physical_page: 24, code: 'PARSER_TIMEOUT' }, { physical_page: 25, code: 'PARSER_MEMORY_LIMIT' }] }, '분석 완료 · 미처리 2쪽'],
  [{ status: 'partial', failed_sources: [{ source_id: 'unprocessed-1', physical_page: 24 }], page_failures: [{ physical_page: 25, code: 'PARSER_TIMEOUT' }] }, '분석 완료 · 미처리 원문 1건 · 미처리 1쪽'],
];
for (const [extraction_outcome, title] of extractionCases) test(`current-policy extraction-only partial is shown honestly: ${title}`, async () => {
  const { loadExamples, CURRENT_EXAMPLE_POLICY } = require('./exampleCatalog.ts');
  const { resultExecution } = require('./hostedClient.ts');
  const result = { ...fixture, pipeline: { ...fixture.pipeline, status: 'completed', scope: { analysis_profile_sha256: CURRENT_EXAMPLE_POLICY }, extraction_outcome } };
  const before = JSON.stringify(result);
  const run = { run_id: 'partial', document_id: 'doc', status: 'completed', created_at: '2026-10-18', result_url: '/partial' };
  const examples = await loadExamples({ listDocuments: async () => ({ items: [{ document_id: 'doc', sha256: 'same', original_filename: '2025_기아_지속가능경영보고서.pdf' }], next_cursor: null }), listRuns: async () => ({ items: [run], next_cursor: null }), getResult: async () => result }, new AbortController().signal);
  assert.equal(examples.length, 1);
  assert.equal(examples[0].completion.title, title);
  assert.deepEqual(examples[0].counts, require('./resultModel.ts').resultBrowserSummary(fixture));
  assert.equal(resultExecution(result, 'completed').kind, 'partial');
  const screen = render(React.createElement(ResultView, { result, runStatus: 'completed' }));
  assert.ok(screen.includes(`data-testid="result-completion-status">${title}</p>`));
  assert.match(screen, /미처리 원문은 판정하지 않았습니다/);
  assert.equal(JSON.stringify(result), before);
});

test('showcase still rejects unsuccessful run/pipeline/extraction states and unquantified partials', async () => {
  const { chooseExample, CURRENT_EXAMPLE_POLICY } = require('./exampleCatalog.ts');
  const result = { ...fixture, pipeline: { status: 'completed', scope: { analysis_profile_sha256: CURRENT_EXAMPLE_POLICY }, extraction_outcome: extractionCases[0][0] } };
  const run = { run_id: 'partial', status: 'completed', created_at: '2026-10-18', result_url: '/partial' };
  for (const status of ['partial_blocked', 'failed', 'cancelled', 'accounting_pending', 'blocked', 'unknown', 'queued', 'running']) {
    assert.equal(await chooseExample([{ ...run, status }], async () => { throw new Error('must not read excluded runs'); }), null, status);
    assert.equal(await chooseExample([run], async () => ({ ...result, pipeline: { ...result.pipeline, status } })), null, `pipeline ${status}`);
    assert.equal(await chooseExample([run], async () => ({ ...result, pipeline: { ...result.pipeline, extraction_outcome: { ...extractionCases[0][0], status } } })), null, `extraction ${status}`);
  }
  assert.equal(await chooseExample([run], async () => ({ ...result, pipeline: { ...result.pipeline, extraction_outcome: { status: 'partial' } } })), null);
  assert.equal(await chooseExample([run], async () => ({ ...result, pipeline: { ...result.pipeline, scope: { analysis_profile_sha256: 'old-policy' } } })), null);
  assert.equal(await chooseExample([run], async () => ({ ...result, pipeline: { ...result.pipeline, scope: {} } })), null, 'partial results require the current policy');
});

test('same PDF prefers a current fully completed result, otherwise its newest current partial', async () => {
  const { loadExamples, chooseExample, CURRENT_EXAMPLE_POLICY } = require('./exampleCatalog.ts');
  const partial = { ...fixture, pipeline: { status: 'completed', scope: { analysis_profile_sha256: CURRENT_EXAMPLE_POLICY }, extraction_outcome: extractionCases[0][0] } };
  const full = { ...partial, pipeline: { ...partial.pipeline, extraction_outcome: { status: 'completed' } } };
  // Production publishes successful extraction as status 'claims'; it must count as fully completed.
  const claims = { ...partial, pipeline: { ...partial.pipeline, extraction_outcome: { status: 'claims', code: 'OK' } } };
  assert.equal((await chooseExample([{ run_id: 'claims', status: 'completed', created_at: '2026-10-18', result_url: '/claims' }], async () => claims)).completion.kind, 'completed');
  const runs = ['new-partial', 'older-partial', 'full', 'old-full'].map((run_id, i) => ({ run_id, document_id: i % 2 ? 'copy' : 'original', status: 'completed', created_at: `2026-10-${18-i}`, result_url: '/' + run_id }));
  const read = async url => url === '/full' ? full : url === '/old-full' ? { ...full, pipeline: { ...full.pipeline, scope: { analysis_profile_sha256: 'old-policy' } } } : partial;
  assert.equal((await chooseExample(runs, read)).run.run_id, 'full');
  assert.equal((await chooseExample(runs.filter(r => r.run_id !== 'full'), read)).run.run_id, 'new-partial');
  const docs = ['original', 'copy'].map(document_id => ({ document_id, sha256: 'identical-pdf', original_filename: '2025_기아_지속가능경영보고서.pdf' }));
  const examples = await loadExamples({ listDocuments: async () => ({ items: docs, next_cursor: null }), listRuns: async () => ({ items: runs, next_cursor: null }), getResult: read }, new AbortController().signal);
  assert.equal(examples.length, 1);
  assert.equal(examples[0].run.run_id, 'full');
});

test('editorial showcase reuses live counts and keeps held claims outside confirmed grades', () => {
  const { ExampleOverview } = require('./HostedExamples.tsx');
  const { resultBrowserSummary } = require('./resultModel.ts');
  for (const result of [fixture, { ...fixture, claims: [] }]) {
    const counts = resultBrowserSummary(result);
    const example = { result, counts, document: { page_count: 119 }, run: { selected_pages: [1, 2] } };
    const html = render(React.createElement(ExampleOverview, { example }));
    for (const text of ['PROCESS FUNNEL', 'DECISION DISTRIBUTION', '추출 주장', '원문 검증', '확정', '보류']) assert.ok(html.includes(text));
    assert.match(html, new RegExp(`추출 주장 ${counts.total}건 = 확정 ${counts.confirmed}건 \\+ 보류 ${counts.total - counts.confirmed}건`));
    assert.equal(Object.values(counts.confirmedGrades).reduce((a, b) => a + b, 0), counts.confirmed);
    assert.doesNotMatch(html, /예비 등급|잠정|NaN|Infinity/);
  }
});
