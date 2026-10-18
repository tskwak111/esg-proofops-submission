// Mock-only browser checks. Start hosted Vite, then node test/guest-smoke.mjs [base] [artifact-dir].
// Uses the installed Playwright CLI; never contacts a backend or starts a paid analysis.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { PDFDocument, StandardFonts } from 'pdf-lib';

const base = process.argv[2] ?? 'http://127.0.0.1:5187';
const out = resolve(process.argv[3] ?? 'output/guest-smoke');
await mkdir(out, {recursive:true});
const fixture = JSON.parse(await readFile(new URL('fixtures/w21/confirmed-majority.result.json', import.meta.url)));
fixture.claims = fixture.claims.slice(0, 3);
fixture.reviews = [{review_id:'review-public',claim_id:fixture.claims[0].claim_id,status:'open',revision:1,base_tag_revision:1,reason_codes:['REVIEW:M3']}];
fixture.claims[0].w16_source_view={quote:fixture.claims[0].quote,refs:[{ref_id:'source_refs/0',source_id:fixture.claims[0].source_refs[0].source_id,kind:'claim',element_id:null,quote:fixture.claims[0].source_refs[0].quote,physical_page:24,location:{bbox:[45,65,400,85],line_rects:[[45,65,400,85]],page:{width_pt:595,height_pt:842,rotation:0,crop_box:[0,0,595,842]},units:'pdf_points',origin:'top_left',space:'rotated_cropbox_display',bbox_format:'x0,y0,x1,y1'}}]};
const pdf = await PDFDocument.create();
const font = await pdf.embedFont(StandardFonts.Helvetica);
for (let i=1;i<=26;i++) pdf.addPage([595,842]).drawText(`GW1 mock source PDF - physical page ${i}`,{x:45,y:760,size:16,font});
const bytes = await pdf.save();
const data = {base,out,fixture,pdf:Buffer.from(bytes).toString('base64'),sha:createHash('sha256').update(bytes).digest('hex')};

const smoke = async (page, data) => {
  page.setDefaultTimeout(10000);
  const check = (value, message) => {if(!value) throw new Error(message);};
  const errors = [], passed = [], calls = [];
  page.on('pageerror',error=>errors.push(error.message));
  let mode = 'open', role = null;
  const id = (prefix,index) => `${prefix}0000000-0000-4000-8000-${String(index+1).padStart(12,'0')}`;
  const companies=['LG화학','NAVER','Kakao','삼성전자','현대자동차','기아','SK하이닉스','SK Telecom','KT','LGE','POSCO Holdings','Samsung SDI'];
  const docs = companies.map((company,i)=>({document_id:id('2',i),company_name:company,sha256:i===0?data.sha:i.toString(16).padStart(64,'0'),original_filename:i===1?'NAVER_2025_통합보고서.pdf':`${company}_Sustainability_Report_2025_KOR.pdf`,page_count:26,size_bytes:2000,expires:1900000000,uploaded_at:'2026-10-18T00:00:00Z'}));
  const runs = docs.map((doc,i)=>({run_id:id('1',i),document_id:doc.document_id,status:'completed',selected_pages:[24,25,26],created_at:'2026-10-18T00:00:00Z',result_url:`/v1/runs/${id('1',i)}/result`,status_url:`/v1/runs/${id('1',i)}`}));
  const ownDoc = {...docs[0],document_id:id('3',0),original_filename:'Private_Sustainability_Report_2025_KOR.pdf',display_name:'Private 2025 지속가능경영보고서'};
  const ownRun = {...runs[0],run_id:id('4',0),document_id:ownDoc.document_id,result_url:`/v1/runs/${id('4',0)}/result`,status_url:`/v1/runs/${id('4',0)}`};
  const scope = {revision:1,plan:{status:'ready',selected_claim_pages:[24],selected_evidence_pages:[24,25,26],failure_reasons:[],pages:[]},selection:{selection_sha256:'scope',pages:[24,25,26],claim_pages:[24]}};
  const session = () => ({user_id:role==='guest'?'visitor':'operator',tenant_id:role==='guest'?'visitor-tenant':'operator-tenant',role,csrf_token:`${role}-csrf`,expires_at:'2026-11-01T00:00:00Z',guest:role==='guest'?{until:'2026-11-01T00:00:00Z',fresh_runs_today:0,fresh_runs_limit:2}:null});
  const collection = items => ({items,next_cursor:null});
  await page.route('**/hosted-api/**', async route => {
    const req=route.request(), url=new URL(req.url()), path=url.pathname.replace('/hosted-api',''), method=req.method();
    calls.push({path,method,body:req.postData(),csrf:req.headers()['x-csrf-token']});
    const json=(body,status=200)=>route.fulfill({status,json:body});
    const error=(code,status)=>json({error:{code}},status);
    if(path==='/v1/runtime') return json({analysis_backend:'hosted',accept_uploads:true,accept_new_runs:true,live_analysis:true,upload_notice:'PDF 원본은 7일 후 삭제됩니다.',limits:{max_upload_bytes:10485760,max_pdf_pages:300,selected_pages:10,original_days:7}});
    if(path==='/v1/session') return role?json(session()):error('AUTH_REQUIRED',401);
    if(path==='/v1/guest') return mode==='unsupported'?error('UNSUPPORTED',404):json({available:mode!=='closed',until:'2026-11-01',fresh_runs_per_day:2,showcase:true});
    if(path==='/v1/guest/session') {
      if(mode==='rate') return error('GUEST_SESSION_RATE_LIMIT',429);
      if(mode==='closed-race') return error('GUEST_ACCESS_CLOSED',403);
      role='guest';return json(session());
    }
    if(path==='/v1/auth/access-password') {role='admin';return json(session());}
    if(path==='/v1/auth/logout') {role=null;return route.fulfill({status:204});}
    if(path.startsWith('/v1/showcase/')) {
      if(mode==='fallback') return error('UNSUPPORTED',404);
      return json(collection(path.endsWith('/documents')?docs:runs));
    }
    if(path==='/v1/documents'&&method==='GET') return json(collection(mode==='open'?[]:[ownDoc]));
    if(path==='/v1/runs'&&method==='GET') return json(collection(mode==='open'?[]:[ownRun]));
    if(path==='/v1/documents'&&method==='POST') return json(ownDoc,201);
    if(path.endsWith('/scope-plan')) return json(scope);
    if(path==='/v1/runs'&&method==='POST') return mode==='daily'?error('GUEST_DAILY_LIMIT',429):json(ownRun,202);
    if(path.endsWith('/original')) return route.fulfill({status:200,contentType:'application/pdf',body:Buffer.from(data.pdf,'base64')});
    const run=[...runs,ownRun].find(run=>path===`/v1/runs/${run.run_id}`||path===`/v1/runs/${run.run_id}/result`);
    if(run) {
      if(!path.endsWith('/result')) return json(run);
      const result=structuredClone(data.fixture);
      result.w16_source_document={document_id:run.document_id,sha256:data.sha,page_count:26};
      return json(result);
    }
    const doc=[...docs,ownDoc].find(doc=>path===`/v1/documents/${doc.document_id}`);
    if(doc) return json(doc);
    return error('RESOURCE_NOT_FOUND',404);
  });
  const goto=async(path,next='open',identity=null)=>{await page.goto('about:blank');mode=next;role=identity;calls.length=0;await page.goto(data.base+path);};
  const visible=selector=>page.locator(selector).waitFor({state:'visible'});
  const shot=async name=>page.screenshot({path:`${data.out}/${name}.png`,fullPage:true});
  const noOverflow=async()=>check(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'Horizontal page overflow');
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:900});
    await goto('/analyze');
    await visible('input[type=file]');
    check(await page.locator('[data-testid=guest-banner]').isVisible(),'Guest banner shown');
    check(await page.locator('input[type=password]').count()===1&&!await page.locator('input[type=password]').isVisible(),'Password form is folded');
    check(calls.filter(c=>c.path==='/v1/guest/session').length===1,'One guest session per entry');
    check(!calls.some(c=>c.path.startsWith('/v1/showcase/')),'Own inventory never reads showcase');
    await noOverflow();await shot(`guest-analyze-${width}`);
    await page.getByText('관리자 로그인',{exact:true}).click();
    await visible('input[type=password]');
    await page.locator('input[type=password]').fill('fixture-operator-password');
    await page.getByRole('button',{name:'입장',exact:true}).click();
    await page.getByText('로그인됨 · 관리자').waitFor();
    check(calls.some(c=>c.path==='/v1/auth/access-password'),'Operator login preserved');
    passed.push(`${width}px automatic entry, folded/operator login, own inventory`);

    await goto('/demo');
    await page.locator('.example-chip').nth(11).waitFor();
    check(await page.locator('.example-chip').count()===12,'All twelve showcase reports');
    check(!calls.some(c=>['/v1/documents','/v1/runs'].includes(c.path)),'Showcase never reads private catalog');
    await page.getByTestId('result-heading').filter({hasText:'NAVER 2025 통합보고서'}).waitFor();
    check(new URL(page.url()).searchParams.get('report')===runs[1].run_id,'NAVER is default even when it is not the first report');
    check(new URL(page.url()).pathname==='/demo','Result opens inline without /analyze navigation');
    check((await page.locator('.example-chip[aria-current=true]').innerText()).includes('확정 3 · 보류 0'),'Compact counts visible');
    check(!await page.getByText('로그인 후 분석 사례를 확인하세요').count(),'No guest login wall');
    await noOverflow();await shot(`demo-after-${width}`);
    const resultReads=calls.filter(c=>c.path.endsWith('/result')).length;
    await page.locator('.example-chip').filter({hasText:'LG화학'}).click();
    await page.getByTestId('result-heading').filter({hasText:'LG화학'}).waitFor();
    check(new URL(page.url()).searchParams.get('report')===runs[0].run_id,'Company selector updates URL');
    await page.goBack();await page.getByTestId('result-heading').filter({hasText:'NAVER'}).waitFor();
    await page.goForward();await page.getByTestId('result-heading').filter({hasText:'LG화학'}).waitFor();
    await page.locator('.example-chip').filter({hasText:'NAVER'}).click();
    await page.getByTestId('result-heading').filter({hasText:'NAVER'}).waitFor();
    check(calls.filter(c=>c.path.endsWith('/result')).length===resultReads,'Selection reuses loaded results without another API fetch');
    await visible('[data-testid=result-heading]');
    for(const label of ['업로드한 PDF 삭제','분석 시작','자동 범위 계획 생성','범위 수정 저장','검토 제출']) check(!await page.getByRole('button',{name:label,exact:true}).count(),`No showcase mutation: ${label}`);
    check(await page.getByRole('tablist',{name:'결과 보기 방식'}).getByRole('tab').count()===4,'Four result tabs preserved');
    check(!(await page.locator('body').innerText()).includes('잠정'),'No forbidden provisional wording');
    await noOverflow();await shot(`guest-result-${width}`);
    await page.locator('.result-tabs [id$="-source"]').click();
    if(width===390) await page.getByRole('button',{name:'원본 PDF',exact:true}).click();
    await visible('.split-document canvas');
    await visible('[data-testid=pdf-claim-highlight]');
    check(calls.some(c=>c.path.endsWith('/original')),'Source PDF uses existing original endpoint');
    await noOverflow();await shot(`guest-source-${width}`);
    await page.locator('.result-tabs [id$="-overview"]').click();
    const popupPromise=page.waitForEvent('popup');
    await page.getByRole('button',{name:'PDF 리포트 저장',exact:true}).click();
    const popup=await popupPromise;
    await popup.waitForLoadState();
    check((await popup.locator('body').innerText()).includes('NAVER 2025 통합보고서'),'Export contains selected report name');
    await popup.close();
    check(!calls.some(c=>c.method!=='GET'&&c.path!=='/v1/guest/session'),'Showcase result sends no writes');
    passed.push(`${width}px inline NAVER default, twelve company chips, URL/back/forward, result tabs, source PDF highlights, PDF export, read-only`);
  }

  for(const mode of ['closed','unsupported']) {
    await goto('/analyze',mode);await visible('input[type=password]');
    check(!calls.some(c=>c.path==='/v1/guest/session'),'Closed/unsupported creates no visitor');
    passed.push(`${mode} login fallback`);
  }
  await goto('/analyze?operator=1');await visible('input[type=password]');
  check(!calls.some(c=>c.path==='/v1/guest/session'),'Explicit operator login skips automatic guest creation');
  passed.push('direct operator login');
  for(const [mode,text] of [['rate','접속 요청이 많습니다.'],['closed-race','공개 이용 기간이 종료되었거나 현재 닫혀 있습니다.']]) {
    await goto('/demo',mode);await page.getByRole('alert').filter({hasText:text}).waitFor();
    await page.getByRole('link',{name:'관리자 로그인',exact:true}).click();await visible('input[type=password]');
    passed.push(`${mode} Korean error and operator recovery`);
  }
  await goto('/demo','fallback','guest');await page.locator('.example-chip').waitFor();
  check(await page.locator('.example-chip').count()===1,'Unsupported showcase uses only own report');
  check(calls.some(c=>c.path==='/v1/documents')&&calls.some(c=>c.path==='/v1/runs'),'Tenant fallback loads both lists');
  passed.push('showcase 404 tenant fallback');
  await goto(`/demo?report=${runs[2].run_id}&tab=claims`,'operator','admin');
  await page.getByTestId('result-heading').filter({hasText:'Kakao'}).waitFor();
  await page.locator('.claim-item').first().click();
  check(await page.getByRole('form',{name:'사람 검토'}).count()===0,'Inline admin showcase cannot review');
  await page.locator('.result-tabs [id$="-source"]').click();
  await page.goBack();await page.locator('.result-tabs [id$="-claims"][aria-selected=true]').waitFor();
  await page.goForward();await page.locator('.result-tabs [id$="-source"][aria-selected=true]').waitFor();
  check(new URL(page.url()).searchParams.get('report')===runs[2].run_id,'Result tab history preserves selected report');
  passed.push('inline shared report/tab URL and tab back/forward, admin read-only');
  await goto('/demo?report=missing');
  await page.getByTestId('result-heading').filter({hasText:'NAVER'}).waitFor();
  check(new URL(page.url()).searchParams.get('report')===runs[1].run_id,'Unknown report link falls back to NAVER');
  await goto('/demo','closed');
  await page.getByText('로그인 후 분석 사례를 확인하세요').waitFor();
  check(!calls.some(c=>c.path==='/v1/guest/session'),'Closed demo preserves clean login prompt');
  passed.push('invalid report fallback and closed demo login');
  await goto(`/runs/${runs[0].run_id}/result?tab=claims`,'operator','admin');
  await page.locator('.claim-item').first().click();
  await visible('.claim-detail');
  check(await page.getByRole('form',{name:'사람 검토'}).count()===0,'Admin cannot review showcase');
  await page.locator('.result-tabs [id$="-info"]').click();
  await page.locator('.hosted-run-summary > summary').click();
  await page.getByText('공개 분석 사례 · 읽기 전용').waitFor();
  check(await page.getByRole('button',{name:'자동 범위 계획 생성',exact:true}).count()===0,'Admin showcase has no scope write');
  passed.push('direct showcase links are read-only for admin');
  await goto(`/runs/${ownRun.run_id}/result?tab=claims`,'operator','admin');
  await page.locator('.claim-item').first().click();
  await visible('.claim-detail');
  check(await page.getByRole('form',{name:'사람 검토'}).count()===1,'Admin own report review preserved');
  check(await page.getByRole('button',{name:'업로드한 PDF 삭제',exact:true}).count()===1,'Own report deletion preserved');
  passed.push('admin own report review and delete preserved');
  for(const mode of ['daily','success']) {
    await goto('/analyze',mode);await visible('input[type=file]');
    await page.locator('input[type=file]').setInputFiles({name:'Private_Sustainability_Report_2025_KOR.pdf',mimeType:'application/pdf',buffer:Buffer.from(data.pdf,'base64')});
    await page.getByRole('button',{name:'업로드',exact:true}).click();
    await page.getByRole('button',{name:'분석 시작',exact:true}).waitFor();
    await page.getByRole('button',{name:'분석 시작',exact:true}).click();
    if(mode==='daily') await page.getByRole('alert').filter({hasText:'오늘 새 보고서 분석 하루 한도에 도달했습니다.'}).waitFor();
    else await visible('[data-testid=result-heading]');
    const run=calls.find(c=>c.path==='/v1/runs'&&c.method==='POST');
    check(run&&!JSON.parse(run.body).force_fresh,'Guest never sends force_fresh');
    check(run.csrf==='guest-csrf','Guest run sends its own CSRF');
    check(await page.getByRole('button',{name:'업로드한 PDF 삭제',exact:true}).count()===1,'Guest own upload remains controllable');
    check(!await page.getByRole('form',{name:'사람 검토'}).count(),'Guest own report has no reviewer privileges');
    passed.push(`guest PDF upload and ${mode==='daily'?'daily refusal':'completed result'}`);
  }
  check(errors.length===0,`Page errors: ${errors.join('; ')}`);
  return {passed,pageErrors:errors,screenshots:8};
};
const script=resolve(out,'guest-playwright.js');
await writeFile(script,`async page => { const smoke = ${smoke.toString()}; return await smoke(page, ${JSON.stringify(data)}); }`);
const cli=(args)=>{const result=spawnSync('playwright-cli',['-s=gw1',...args],{encoding:'utf8',maxBuffer:4*1024*1024});process.stdout.write(result.stdout??'');process.stderr.write(result.stderr??'');if(result.status!==0||result.stdout?.includes('### Error'))throw new Error(`Playwright CLI failed: ${args[0]}`);};
try {cli(['open',base]);cli(['run-code',`--filename=${script}`]);} finally {cli(['close']);}
