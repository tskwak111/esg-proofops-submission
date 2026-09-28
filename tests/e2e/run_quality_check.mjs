// Run from the task Vite browser, like review_queue_gap_check.mjs.
export async function check() {
  const resource = performance.getEntriesByType('resource').map(e => e.name).find(n => /\/react\.js\?v=/.test(n));
  const version = resource ? new URL(resource).search : '';
  const {default: React} = await import('/node_modules/.vite/deps/react.js' + version);
  const {default: {createRoot}} = await import('/node_modules/.vite/deps/react-dom_client.js' + version);
  const {RunQuality} = await import('/src/features/runs/RunQuality.tsx');
  const originalFetch = window.fetch, host = document.createElement('div');
  document.body.append(host); const root = createRoot(host);
  const assert = (v, message) => { if (!v) throw Error(message); };
  const wait = async text => { for(let i=0;i<60;i++) { if(host.textContent.includes(text)) return; await new Promise(r=>setTimeout(r,25)); } throw Error(host.textContent); };
  const button = text => [...host.querySelectorAll('button')].find(b=>b.textContent===text);
  const ok = value => new Response(JSON.stringify(value),{status:200,headers:{'Content-Type':'application/json'}});
  let mode='pages', opened=false, invalidated=false;
  const item = {issue_id:'a',kind:'image_text_not_extracted',page_num:76,source_ids:['image'],state:'open',reason:'画像候補 / 이미지 글 누락 여부를 확인하세요.'};
  const source = {source_id:'image',document_version_id:'d',parse_manifest_id:'p',page_num:76,printed_page_label:null,bbox:[1,1,80,90],raw_text_sha256:'a'.repeat(64),quote:'',char_start:0,char_end:0,location_quality:'located',verification_state:'candidate'};
  const invalid = () => { invalidated=true; };
  function mount(key) {root.render(React.createElement(RunQuality,{key,runId:key,csrfToken:'csrf',onSessionInvalid:invalid}));}
  window.fetch = async (input, init={}) => {
    const url=new URL(typeof input==='string'?input:input.url,location.origin);
    if(url.pathname.endsWith('/quality')) {
      if(mode==='error') return new Response(JSON.stringify({error:{code:'TEST_FAILURE',message:'load failed'}}),{status:500});
      if(mode==='auth') return new Response('{}',{status:401});
      if(mode==='empty') return ok({items:[],next_cursor:null});
      return url.searchParams.has('cursor') ? ok({items:[{...item,issue_id:'b',page_num:22,reason:'SECOND PAGE',source_ids:[]}],next_cursor:null}) : ok({items:[item],next_cursor:'next'});
    }
    if(url.pathname.endsWith('/sources/image')) return ok(source);
    if(url.pathname.endsWith('/sources/image/view')) {assert(init.method==='POST' && init.headers['X-CSRF-Token']==='csrf','ticket security');opened=true;return ok({url:'/quality-preview',expires_at:'2099-01-01T00:00:00Z',sha256:'a'.repeat(64)});}
    if(url.pathname==='/quality-preview') return new Response(new Uint8Array([137,80,78,71]),{status:200,headers:{'Content-Type':'image/png','X-Page-Width-Pt':'100','X-Page-Height-Pt':'100','X-Highlight-Allowed':'false'}});
    throw Error(`Unexpected ${url}`);
  };
  try {
    mount('one');await wait('이미지 영역 확인 필요');
    button('품질 항목 더 보기').click();await wait('SECOND PAGE');
    assert(host.textContent.includes('76쪽') && !button('품질 항목 더 보기'),'pagination');
    button('해당 원문 열기').click();await wait('PDF 76쪽 미리보기');
    assert(opened && !host.querySelector('[data-source-highlight]'),'candidate cannot be highlighted as verified');
    mode='error';mount('two');await wait('품질 정보 다시 확인');
    assert(!host.textContent.includes('76쪽') && !host.querySelector('img'),'tenant/run remount clears old source');
    mode='empty';button('품질 정보 다시 확인').click();await wait('추출의 완전성이 검증된 것은 아닙니다.');
    mode='auth';mount('three');await new Promise(r=>setTimeout(r,100));assert(invalidated,'session rejection');
    return {passed:true,checks:['image warning','pagination','authenticated candidate preview','tenant clear','failure retry','empty not completeness','session rejection']};
  } finally {root.unmount();host.remove();window.fetch=originalFetch;}
}
