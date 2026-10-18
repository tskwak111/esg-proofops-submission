const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, filename);
const { HostedApiError, createHostedClient } = require('./hostedClient.ts');
const { enterHostedSession, isShowcaseRun } = require('./guestAccess.ts');
const { loadExamples, selectedExample } = require('./exampleCatalog.ts');
const json = (body, status = 200) => new Response(JSON.stringify(body), { status });
const guest = { user_id: 'visitor', tenant_id: 'private-visitor', role: 'guest', csrf_token: 'guest-csrf', guest: { until: '2026-11-01', fresh_runs_today: 0, fresh_runs_limit: 2 } };

test('missing session automatically enters guest mode and uses its CSRF for uploads', async () => {
  const calls = [];
  const client = createHostedClient({ fetchImpl: async (url, init) => {
    calls.push({ url, ...init });
    if (url.endsWith('/v1/session')) return json({ error: { code: 'AUTH_REQUIRED' } }, 401);
    if (url.endsWith('/v1/guest')) return json({ available: true, until: guest.guest.until, fresh_runs_per_day: 2, showcase: true });
    if (url.endsWith('/v1/guest/session')) return json(guest);
    return json({ document_id: 'own-document' });
  } });
  assert.deepEqual(await enterHostedSession(client), guest);
  await client.uploadDocument(new Blob(['%PDF-1.7']));
  assert.deepEqual(calls.map(c => [c.method, c.url]), [['GET','/hosted-api/v1/session'],['GET','/hosted-api/v1/guest'],['POST','/hosted-api/v1/guest/session'],['POST','/hosted-api/v1/documents']]);
  assert.equal(calls.at(-1).headers['X-CSRF-Token'], 'guest-csrf');
  assert.ok(calls.every(c => c.credentials === 'same-origin'));
});
test('existing operator and guest sessions are preserved without minting another session', async () => {
  for (const role of ['admin', 'guest', 'organizer']) {
    const client = createHostedClient({ fetchImpl: async url => { assert.equal(url, '/hosted-api/v1/session'); return json({ ...guest, role }); } });
    assert.equal((await enterHostedSession(client)).role, role);
  }
});
test('closed and unsupported guest access fall back to the existing login', async () => {
  for (const status of [200, 404, 405, 501]) {
    const calls = [];
    const client = createHostedClient({ fetchImpl: async url => {
      calls.push(url);
      if (url.endsWith('/v1/session')) return json({ error: { code: 'AUTH_REQUIRED' } }, 401);
      return json({ available: false, error: { code: 'UNSUPPORTED' } }, status);
    } });
    assert.equal(await enterHostedSession(client), null);
    assert.equal(calls.length, 2);
  }
});
test('guest refusal messages remain Korean and do not automatically replay session creation', async () => {
  for (const [status, code, text] of [[429,'GUEST_UPLOAD_LIMIT',/업로드 한도.*분석 사례.*내일/],[429,'GUEST_DAILY_LIMIT',/하루.*한도.*분석 사례/],[429,'GUEST_SESSION_RATE_LIMIT',/접속.*잠시 후/],[403,'GUEST_ACCESS_CLOSED',/종료.*관리자 로그인/]]) {
    let posts = 0;
    const client = createHostedClient({ fetchImpl: async (url, init) => {
      if (init.method === 'POST') { posts++; return json({ error: { code } }, status); }
      if (url.endsWith('/v1/session')) return json({ error: { code: 'AUTH_REQUIRED' } }, 401);
      return json({ available: true });
    } });
    await assert.rejects(enterHostedSession(client), err => err.code === code && text.test(err.userMessage));
    assert.equal(posts, 1);
  }
});
test('showcase membership scans pages; unsupported falls back but authorization errors propagate', async () => {
  const cursors = [];
  const client = { listShowcaseRuns: async cursor => { cursors.push(cursor); return { items: cursor ? [{run_id:'public'}] : [], next_cursor: cursor ? null : 'page-2' }; } };
  assert.equal(await isShowcaseRun(client, 'public'), true);
  assert.deepEqual(cursors, [null, 'page-2']);
  assert.equal(await isShowcaseRun(client, 'private'), false);
  for (const status of [404,405,501]) assert.equal(await isShowcaseRun({listShowcaseRuns:async()=>{throw new HostedApiError(status,'UNSUPPORTED',null,'/v1/showcase/runs');}}, 'private'), false);
  await assert.rejects(isShowcaseRun({listShowcaseRuns:async()=>{throw new HostedApiError(403,'FORBIDDEN',null,'/v1/showcase/runs');}}, 'public'), err=>err.status===403);
});

test('showcase collection contract encodes cursors and uses credentialed GETs without CSRF', async () => {
  const calls = [];
  const client = createHostedClient({fetchImpl: async (url, init) => {
    calls.push({url, ...init});
    return json(url.endsWith('/v1/guest/session') ? guest : {items: [], next_cursor: null});
  }});
  await client.guestSession();
  await client.listShowcaseDocuments('opaque+/=cursor');
  await client.listShowcaseRuns('opaque+/=cursor');
  for (const [index, collection] of [[1,'documents'],[2,'runs']]) {
    const call = calls[index], url = new URL(call.url, 'https://example.test');
    assert.equal(url.pathname, `/hosted-api/v1/showcase/${collection}`);
    assert.equal(url.searchParams.get('limit'), '100');
    assert.equal(url.searchParams.get('cursor'), 'opaque+/=cursor');
    assert.equal(call.method, 'GET');
    assert.equal(call.credentials, 'same-origin');
    assert.equal(call.headers['X-CSRF-Token'], undefined);
  }
});

test('showcase loading paginates both catalogs, reads existing results and never merges private inventory', async () => {
  const fixture = JSON.parse(fs.readFileSync(`${__dirname}/../../../test/fixtures/w21/confirmed-majority.result.json`));
  const calls = [];
  const client = createHostedClient({fetchImpl: async url => {
    calls.push(url);
    const path = new URL(url, 'https://example.test');
    const second = path.searchParams.has('cursor');
    if (path.pathname.endsWith('/showcase/documents')) return json({items: second ? [{document_id:'public-doc', original_filename:'Acme_Sustainability_Report_2025_KOR.pdf', sha256:'public-sha'}] : [], next_cursor:second?null:'doc-page-2'});
    if (path.pathname.endsWith('/showcase/runs')) return json({items: second ? [{run_id:'public-run',document_id:'public-doc',status:'completed',created_at:'2026-10-18',result_url:'/v1/runs/public-run/result'}] : [],next_cursor:second?null:'run-page-2'});
    if (path.pathname.endsWith('/public-run/result')) return json(fixture);
    throw new Error(`Private inventory must not be read: ${url}`);
  }});
  const examples = await loadExamples(client, new AbortController().signal);
  assert.deepEqual(examples.map(e=>[e.run.run_id,e.name]), [['public-run','Acme 2025 지속가능경영보고서']]);
  assert.equal(examples[0].document.document_id,'public-doc');
  assert.deepEqual(examples[0].result,fixture);
  assert.equal(calls.length,5);
});

test('inline example defaults to NAVER 2025 integrated report, with explicit links and safe fallbacks', () => {
  const example=(id,name,company_name)=>({run:{run_id:id},document:{company_name},name});
  const rows=[example('lg','LG화학 2025 지속가능경영보고서'),example('naver-old','NAVER 2024 통합보고서'),example('naver','NAVER 2025 통합보고서'),example('kakao','Kakao 2025 ESG 보고서')];
  assert.equal(selectedExample(rows,null).run.run_id,'naver');
  assert.equal(selectedExample(rows,'kakao').run.run_id,'kakao');
  assert.equal(selectedExample(rows,'missing').run.run_id,'naver');
  assert.equal(selectedExample([rows[0]],null).run.run_id,'lg');
  assert.equal(selectedExample([],null),undefined);
  assert.equal(selectedExample([rows[0],example('n','2025 통합보고서','NAVER')],null).run.run_id,'n');
});

test('unsupported showcase falls back to own catalogs; permission errors never trigger fallback', async () => {
  for (const status of [404,405,501,403]) {
    const ownReads = [];
    const client = createHostedClient({fetchImpl:async url=>{
      if(url.includes('/showcase/')) return json({error:{code:'UNSUPPORTED'}},status);
      ownReads.push(url); return json({items:[],next_cursor:null});
    }});
    const loading = loadExamples(client,new AbortController().signal);
    if(status===403) {await assert.rejects(loading,err=>err.status===403);assert.equal(ownReads.length,0);}
    else {assert.deepEqual(await loading,[]);assert.deepEqual(ownReads.sort(),['/hosted-api/v1/documents?limit=20','/hosted-api/v1/runs?limit=20']);}
  }
});

test('daily guest run refusal preserves the accepted upload and never requests force_fresh', async () => {
  let request;
  const client = createHostedClient({fetchImpl: async (url,init)=>{
    if(url.endsWith('/v1/guest/session')) return json(guest);
    request=init; return json({error:{code:'GUEST_DAILY_LIMIT'}},429);
  }});
  await client.guestSession();
  await assert.rejects(client.createRun('own-document',{selection:{selection_sha256:'scope'}}),err=>err.code==='GUEST_DAILY_LIMIT'&&/올린 보고서는 유지/.test(err.userMessage));
  assert.equal(request.headers['X-CSRF-Token'],'guest-csrf');
  assert.deepEqual(JSON.parse(request.body),{document_id:'own-document',scope_selection_sha256:'scope'});
});

test('lazy showcase publishes catalog without result reads, then resolves only the selected report', async () => {
  const { loadExampleCatalog, loadExample } = require('./exampleCatalog.ts');
  const fixture = JSON.parse(fs.readFileSync(`${__dirname}/../../../test/fixtures/w21/confirmed-majority.result.json`));
  const reads = [];
  const documents = ['Kakao', 'NAVER'].map((company, i) => ({ document_id: `doc-${i}`, sha256: `sha-${i}`, original_filename: `${company}_2025_통합보고서.pdf` }));
  const runs = documents.map((doc, i) => ({ run_id: `run-${i}`, document_id: doc.document_id, status: 'completed', created_at: '2026-10-18', result_url: `/v1/runs/run-${i}/result` }));
  const client = { listShowcaseDocuments: async () => ({ items: documents, next_cursor: null }), listShowcaseRuns: async () => ({ items: runs, next_cursor: null }), getResult: async url => { reads.push(url); return fixture; } };
  const signal = new AbortController().signal;
  const catalog = await loadExampleCatalog(client, signal);
  assert.equal(catalog.length, 2);
  assert.deepEqual(reads, []);
  const selected = selectedExample(catalog, null);
  assert.equal(selected.run.run_id, 'run-1');
  const result = await loadExample(client, selected, signal);
  assert.equal(result.run.run_id, 'run-1');
  assert.deepEqual(reads, ['/v1/runs/run-1/result']);
  const controller = new AbortController(); controller.abort();
  await assert.rejects(loadExampleCatalog(client, controller.signal), err => err.name === 'AbortError');
});
