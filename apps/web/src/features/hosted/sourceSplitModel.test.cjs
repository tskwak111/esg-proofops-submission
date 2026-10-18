const assert = require('node:assert/strict');
const fs = require('node:fs');
const { test } = require('node:test');
const ts = require('typescript');
require.extensions['.ts'] = (module, filename) => module._compile(ts.transpileModule(fs.readFileSync(filename, 'utf8'), { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, filename);
const { locationRects, sourceHighlight, uniqueEvidence } = require('./sourceSplitModel.ts');
const { createHostedClient } = require('./hostedClient.ts');
const location = { units: 'pdf_points', origin: 'top_left', space: 'rotated_cropbox_display', bbox_format: 'x0,y0,x1,y1', page: { width_pt: 800, height_pt: 600, rotation: 90, crop_box: [50,100,650,900] }, bbox: [100,200,300,400], line_rects: null };
test('rotated/cropped stored boxes normalize once against actual viewport', () => {
  assert.deepEqual(locationRects(location, { width:800, height:600, rotation:90 }), [[.125, 1/3, .25, 1/3]]);
  assert.deepEqual(locationRects(location, { width:600, height:800, rotation:0 }), []);
});
test('missing, malformed or incompatible locations never create fallback boxes', () => {
  for (const value of [null, { ...location, origin:'bottom_left' }, { ...location, bbox:[0,0,Infinity,5] }, { ...location, bbox:[0,0,900,50] }, { ...location, bbox:[1,2,0,3] }]) assert.deepEqual(locationRects(value), []);
});
test('stored glyph rectangles take precedence over the enclosing parser paragraph', () => {
  assert.deepEqual(locationRects({ ...location, glyph_rects:[[100,200,110,212],[120,200,130,212]] }), [[.125,1/3,.0125,.02],[.15,1/3,.0125,.02]]);
});
test('paragraph-only W16 locations are regions, never exact quote highlights', () => {
  const ref = { ref_id:'source_refs/0', kind:'claim', location };
  assert.equal(sourceHighlight(ref).precision, 'region');
  assert.equal(sourceHighlight({...ref, kind:'evidence'}).precision, 'region');
  assert.equal(sourceHighlight({...ref, location:null}).precision, 'unavailable');
  assert.equal(sourceHighlight(ref, {width:600,height:800,rotation:0}).precision, 'unavailable');
});
test('exact W16 line/glyph coordinates exclude the enclosing paragraph', () => {
  const lines = [[100,200,200,212], [100,220,180,232]];
  const ref = { ref_id:'source_refs/0', location:{...location, line_rects:lines} };
  assert.deepEqual(sourceHighlight(ref), {precision:'exact', boxes:locationRects(ref.location)});
  assert.equal(sourceHighlight(ref).boxes.length, 2);
  assert.equal(sourceHighlight({...ref, location:{...location, glyph_rects:lines}}).precision, 'exact');
  assert.equal(sourceHighlight({...ref, location:{...location, line_rects:[[0,0,900,50]]}}).precision, 'unavailable');
});
test('stored table cells remain distinct from exact text and paragraph regions', () => {
  assert.equal(sourceHighlight({ref_id:'table_observation/cell_source_refs/value', location}).precision, 'cell');
});
test('evidence keeps cross-page and element identities while removing repeated provisional refs', () => {
  const ref = { kind:'evidence', element_id:'M1', source_id:'x', physical_page:36, quote:'ISO 14001' };
  assert.equal(uniqueEvidence([ref, {...ref}, {...ref, physical_page:37}, {...ref, element_id:'M2'}, {...ref, kind:'claim'}]).length, 3);
});
test('PDF credentials never follow an untrusted result URL or unvalidated document identifier', () => {
  const client = createHostedClient();
  const doc = { document_id:'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', sha256:'a'.repeat(64), url:'https://foreign.test/private' };
  assert.equal(client.originalPdfUrl(doc), `/hosted-api/v1/documents/${doc.document_id}/original?sha256=${doc.sha256}`);
  assert.throws(() => client.originalPdfUrl({...doc, document_id:'../foreign'}));
  assert.throws(() => client.originalPdfUrl({...doc, sha256:'invalid'}));
});
const { displayRefs, uniqueTextMatch } = require('./sourceSplitModel.ts');
const { claimLocationLabel, highlightLabel } = require('./sourceSplitModel.ts');
const parserClaim = () => ({ source_quality:'unverified', source_status:{status:'unverified',display:'원문 확인 필요'}, source_refs:[{source_id:'parser-source',page_num:3,location_quality:'located',verification_state:'rejected',bbox:[100,200,300,400]}], w16_source_view:{refs:[{ref_id:'source_refs/0',source_id:'parser-source',kind:'claim',physical_page:84,location:null}]} });
test('unverified parser bbox uses the original page and actual PDF dimensions without changing source status', () => {
  const claim = parserClaim(), before = JSON.stringify(claim);
  const ref = displayRefs(claim)[0];
  assert.equal(ref.physical_page,84);
  assert.deepEqual(sourceHighlight(ref,{width:800,height:600,rotation:90}),{precision:'estimated',boxes:[[.125,1/3,.25,1/3]]});
  assert.equal(claimLocationLabel(claim),'추정 위치');
  assert.equal(highlightLabel('estimated'),'추정 위치(원문 대조 전)');
  assert.notEqual(highlightLabel('estimated'),highlightLabel('exact'));
  assert.notEqual(highlightLabel('estimated'),highlightLabel('region'));
  assert.equal(JSON.stringify(claim),before);
});
test('estimated geometry follows stored rotated display dimensions once and rejects viewport mismatch', () => {
  const claim = parserClaim(); claim.w16_source_view.refs[0].location = location;
  const ref = displayRefs(claim)[0];
  assert.deepEqual(sourceHighlight(ref,{width:800,height:600,rotation:90}).boxes,[[.125,1/3,.25,1/3]]);
  assert.equal(sourceHighlight(ref,{width:600,height:800,rotation:0}).precision,'unavailable');
  assert.equal(sourceHighlight(ref).precision,'estimated');
});
test('missing or malformed parser boxes, missing physical page and unrelated refs stay unlocated', () => {
  for (const bbox of [null,[],[0,0,Infinity,5],[1,2,0,3],[-1,0,2,3]]) {
    const claim = parserClaim(); claim.source_refs[0].bbox = bbox;
    assert.equal(claimLocationLabel(claim),'위치 미확인');
    assert.equal(sourceHighlight(displayRefs(claim)[0],{width:800,height:600,rotation:0}).precision,'unavailable');
  }
  for (const change of [c=>c.w16_source_view.refs[0].physical_page=null,c=>c.source_refs[0].location_quality='unlocated',c=>c.w16_source_view.refs[0].source_id='other',c=>c.w16_source_view.refs[0].kind='evidence',c=>c.source_quality='unreadable']) {
    const claim = parserClaim(); change(claim); if(claim.source_quality==='unreadable')claim.source_status.status='unreadable';
    assert.equal(claimLocationLabel(claim),'위치 미확인');
  }
  const claim = parserClaim(); claim.source_refs[0].bbox=[0,0,900,50];
  assert.equal(sourceHighlight(displayRefs(claim)[0],{width:800,height:600,rotation:0}).precision,'unavailable');
});
test('verified precise quotes keep exact highlights and never inherit parser estimates', () => {
  const claim = parserClaim(); claim.source_quality='verified'; claim.source_status.status='verified'; claim.source_refs[0].verification_state='verified';
  claim.w16_source_view.refs[0].location={...location,line_rects:[[100,200,200,212]]};
  assert.equal(sourceHighlight(displayRefs(claim)[0]).precision,'exact');
  assert.equal(claimLocationLabel(claim),'');
  assert.equal(highlightLabel('exact'),'정확 인용 위치');
});
test('W18 coordinates precede W16 and reject invalid geometry, page and unverified refs', () => {
  const ref = {ref_id:'source_refs/0', physical_page:3, location, w18_verification_state:'verified', w18_line_rects:[[10,20,30,40]], w18_coordinates:{...location, physical_page:3}};
  assert.deepEqual(sourceHighlight(ref).boxes, [[10/800,20/600,20/800,20/600]]);
  assert.equal(sourceHighlight({...ref,w18_cell_rects:[[40,50,60,70]]}).precision,'cell');
  assert.equal(sourceHighlight({...ref,w18_verification_state:'candidate'}).precision,'unavailable');
  assert.equal(sourceHighlight({...ref,w18_coordinates:{...ref.w18_coordinates,physical_page:4}}).precision,'region');
  assert.equal(sourceHighlight({...ref,w18_line_rects:[[0,0,9999,10]]}).precision,'region');
});
test('exact text search permits only unique same-string whitespace normalization', () => {
  assert.deepEqual(uniqueTextMatch('prefix A\nB C end','AB C'),[7,12]);
  for(const [text,quote] of [['ABA','A'],['12.5','125'],['Ａ','A'],['Carbon','carbon'],['none',''],['AA','A']]) assert.equal(uniqueTextMatch(text,quote),null);
});
test('older API cannot highlight an unverified claim or candidate evidence', () => {
  const claim={source_quality:'unverified',source_refs:[{verification_state:'verified'}],elements:[{evidence_refs:[{verification_state:'candidate'}]}],w16_source_view:{refs:[{ref_id:'source_refs/0',kind:'claim',location},{ref_id:'elements/0/evidence_refs/0',kind:'evidence',location}]}};
  assert.ok(displayRefs(claim).every(ref=>sourceHighlight(ref).precision==='unavailable'));
  assert.equal(sourceHighlight(displayRefs({...claim,source_quality:'verified'})[0]).precision,'region');
});
