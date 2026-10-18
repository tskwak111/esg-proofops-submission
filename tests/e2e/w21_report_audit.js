// Run with playwright-cli run-code --filename=tests/e2e/w21_report_audit.js.
// Existing E16/E17 data served on localhost:5222; only original-PDF GETs reach production.
async page => {
  const origin = 'http://127.0.0.1:5221';
  const out = '/path/to/ESG_ProofOps/outputs/agent-results/R108-redesign/lab/web/W21';
  const id = 'ca4f03c5-da7f-4ed9-bc3d-b549d6a42d85';
  const samples = {};
  for (const [name, dir] of [['lg','E17'],['kakao','E16'],['lge','E16']]) samples[name] = await (await fetch(`http://127.0.0.1:5222/${dir}/${name}.w18-result.json`)).json();
  let sample = 'lg', status = 'completed', uploaded = false, pdfFailure = false;
  const writes = [], errors = [], checks = [], pdfs = new Map();
  const check = (value, message) => { if (!value) throw Error(message); };
  page.on('pageerror', error => errors.push(error.message));
  const document = () => ({ ...samples[sample].w16_source_document, size_bytes: 2940055, expires: 1999999999, uploaded_at: '2026-10-16T00:00:00Z' });
  const run = () => ({ run_id:id, document_id:document().document_id, status, selected_pages:samples[sample].pipeline.scope.pages, stage:status==='running'?'tag':status==='queued'?'queued':'result', progress:status==='running'?.65:status==='queued'?0:1, claims_extracted:211, claims_processed:95, status_url:`/v1/runs/${id}`, result_url:status==='completed'?`/v1/runs/${id}/result`:null });
  const scope = () => ({ revision:1, reporting_scope:samples[sample].pipeline.reporting_scope, selection:{pages:run().selected_pages,claim_pages:samples[sample].pipeline.scope.claim_pages,selection_sha256:'a'.repeat(64)}, plan:{status:'ready', selected_claim_pages:samples[sample].pipeline.scope.claim_pages,selected_evidence_pages:run().selected_pages,failure_reasons:[],pages:[]} });
  await page.unroute('**/hosted-api/**');
  await page.route('**/hosted-api/**', async route => {
    const req=route.request(), path=new URL(req.url()).pathname.replace('/hosted-api','');
    if (path.includes('/original')) {
      check(req.method()==='GET','Original PDF write attempted');
      if(pdfFailure) return route.fulfill({status:404,body:'Unavailable'});
      const key=sample;
      if(!pdfs.has(key)) pdfs.set(key, page.request.get('https://esg-proofops.vercel.app/hosted-api'+samples[key].w16_source_document.url, {timeout:90000}).then(async response => { check(response.ok(),'Original PDF GET failed'); return response.body(); }));
      try { return await route.fulfill({contentType:'application/pdf',body:await pdfs.get(key)}); }
      catch(error) { errors.push(key+': '+error.message); return route.fulfill({status:502,body:'Source read failed'}); }
    }
    let body;
    if(req.method()!=='GET') {
      writes.push({method:req.method(),path,mocked:true});
      if(path==='/v1/documents') { uploaded=true; body=document(); }
      else if(path.endsWith('/scope-plan')) body=scope();
      else if(path==='/v1/runs') { status='running'; body=run(); }
      else throw Error('Unexpected local mock mutation '+path);
    } else if(path==='/v1/runtime') body={live_analysis:true,accept_uploads:true,accept_new_runs:true,limits:{max_upload_bytes:157286400,max_pdf_pages:300},upload_notice:'원본 PDF는 7일 동안 보관합니다. 선택 범위는 외부 분석 서비스로 전송됩니다.'};
    else if(path==='/v1/session') body={user_id:'w21-replay',tenant_id:'w21-replay',role:'viewer',csrf_token:'',expires_at:'2099-01-01'};
    else if(path==='/v1/documents') body={items:uploaded?[document()]:[],next_cursor:null};
    else if(path==='/v1/runs') body={items:uploaded?[run()]:[],next_cursor:null};
    else if(path.endsWith('/result')) body=samples[sample];
    else if(path===`/v1/runs/${id}`) body=run();
    else if(path===`/v1/documents/${document().document_id}`) body=document();
    else if(path.endsWith('/scope-plan')) body=scope();
    else return route.fulfill({status:404,contentType:'application/json',body:'{}'});
    return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
  });
  const capture = async (name, target) => {
    if(target) await target.evaluate(el=>window.scrollTo({top:el.getBoundingClientRect().top+scrollY-84,behavior:'instant'}));
    await page.screenshot({path:`${out}/after-${name}.png`});
  };
  const noOverflow = async label => { const sizes=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth})); check(sizes.scroll<=sizes.width,label+' overflow '+JSON.stringify(sizes)); checks.push({label,...sizes}); };
  for(const width of [1440,390]) {
    await page.setViewportSize({width,height:1000}); sample='lg'; status='completed'; uploaded=false;
    await page.goto(origin+'/analyze'); await page.getByRole('heading',{name:'보고서 업로드',exact:true}).waitFor();
    await capture(`upload-${width}`,page.locator('.hosted-upload-panel')); await noOverflow('upload-'+width);
    await page.locator('input[type=file]').setInputFiles({name:'W21-local-replay.pdf',mimeType:'application/pdf',buffer:Buffer.from('%PDF-1.7\n%W21 local mock only')});
    await page.getByRole('button',{name:'업로드',exact:true}).click();
    await page.getByRole('button',{name:'분석 시작',exact:true}).waitFor();
    await page.getByRole('button',{name:'분석 시작',exact:true}).click();
    await page.getByRole('progressbar',{name:'분석 진행률'}).waitFor();
    check(await page.getByRole('progressbar',{name:'분석 진행률'}).getAttribute('value')==='0.65','Progress differs from API');
    check(await page.locator('.analysis-steps li[aria-current=step]').innerText().then(s=>s.includes('근거 대조')),'Wrong current stage');
    await capture(`progress-${width}`,page.locator('.hosted-outcome')); await noOverflow('progress-'+width);
    status='completed'; await page.getByTestId('hosted-result').waitFor({timeout:30000});
    await page.goto(origin+`/runs/${id}/result`); await page.getByTestId('hosted-result').waitFor();
    await capture(`lg-result-${width}`,page.locator('.result-heading')); await noOverflow('result-'+width);
    check((await page.locator('.report-facts').innerText()).includes('211건'),'Lost claims');
    await page.locator('.claim-list .claim-item').first().click();
    const detail=page.getByRole('complementary',{name:'주장 상세'});
    if(width===390) await page.waitForFunction(()=>document.activeElement?.getAttribute('aria-label')==='주장 상세');
    await capture(`lg-detail-${width}`,detail); await noOverflow('detail-'+width);
    const citation=detail.getByRole('button',{name:'이 인용의 원문 보기'}).first();
    await citation.click();
    await page.getByTestId('pdf-evidence-highlight').first().waitFor({timeout:20000});
    await page.waitForTimeout(500);
    await capture(`lg-pdf-${width}`,page.locator('.source-split')); await noOverflow('pdf-'+width);
    check(await page.getByTestId('pdf-claim-highlight').count()>0,'Claim highlight missing');
    const secondCitation=detail.getByRole('button',{name:'이 인용의 원문 보기'}).nth(1);
    await secondCitation.click();
    await page.waitForFunction(()=>document.querySelector('.split-citation.active strong')?.textContent?.includes('M5'));
    check((await page.getByTestId('pdf-evidence-highlight').first().locator('title').textContent()).includes('M5'),'Identical quote jumped to wrong element');
    await detail.getByRole('button',{name:'주장 원문 보기',exact:true}).click();
    await page.getByTestId('pdf-claim-highlight').first().waitFor();
    check(await page.getByTestId('pdf-evidence-highlight').count()===0,'Claim jump retained evidence selection');
    if(width===390) { await detail.getByRole('button',{name:'목록으로 돌아가기'}).click(); check(await page.locator('.claim-list').isVisible(),'Mobile back failed'); }
    await page.getByRole('tab',{name:/^보류/}).click();
    await page.locator('.claim-list .claim-item').first().click();
    check((await detail.getByTestId('grade-panel').innerText()).includes('판정 보류'),'Hold got grade');
    await capture(`lg-hold-${width}`,detail);
    if(width===390) await detail.getByRole('button',{name:'목록으로 돌아가기'}).click();
    await page.getByRole('textbox',{name:'검색',exact:true}).fill('no-such-claim-w21');
    check(await page.locator('.claim-list .claim-item').count()===0,'Search empty state failed');
    await capture(`empty-search-${width}`,page.locator('.result-claims'));
    if(width===1440) {
      const downloadPromise=page.waitForEvent('download');
      await page.getByRole('button',{name:'결과 보고서 내보내기'}).click();
      const download=await downloadPromise; await download.saveAs(`${out}/W21-result-report.html`);
      checks.push({export:download.suggestedFilename(),filteredRows:0,expectedExportClaims:211});
    }
  }
  for(const name of ['kakao','lge']) {
    sample=name; status='completed';
    for(const width of [1440,390]) {
      await page.setViewportSize({width,height:1000}); await page.goto(origin+`/runs/${id}/result`); await page.getByTestId('hosted-result').waitFor();
      check((await page.locator('.report-facts').innerText()).includes(samples[name].claims.length+'건'),'Sample count mismatch');
      if(width===390) await page.getByRole('button',{name:'원본 PDF',exact:true}).click();
      await page.locator('.split-pdf-scroll[aria-busy=false]').waitFor({timeout:100000});
      await capture(`${name}-result-${width}`,page.locator('.result-heading')); await noOverflow(name+'-'+width);
    }
  }
  sample='lg'; pdfFailure=true;
  await page.goto(origin+`/runs/${id}/result`); await page.getByTestId('hosted-result').waitFor();
  await page.getByRole('button',{name:'원본 PDF',exact:true}).click();
  await page.locator('.source-split [role=alert]').waitFor();
  check(!/근거 없음/.test(await page.locator('.source-split [role=alert]').innerText()),'PDF error became absence');
  await capture('pdf-error-390',page.locator('.source-split')); await noOverflow('pdf-error');
  check(errors.length===0,errors.join('; '));
  return {status:'passed',checks,errors,mockedWrites:writes,productionWrites:0,newAnalyses:0,fixtureSources:['E17/lg.w18-result.json','E16/kakao.w18-result.json','E16/lge.w18-result.json']};
}
