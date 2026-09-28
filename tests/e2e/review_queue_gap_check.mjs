// Focused browser check for the R21 review-queue visibility repair.
// Run in the task Vite browser:
// await import('/@fs/<repo>/tests/e2e/review_queue_gap_check.mjs').then(m => m.check())
//
// Verifies ReviewQueueWorkspace surfaces undecided claims not yet registered
// for review (no associated Review row, no decided Decision):
//   1. empty reviews + one such blocked claim -> the claim is listed and linked;
//   2. mixed (one open review + one unregistered undecided claim) -> the
//      reviewed/decided claim is NOT duplicated into the unregistered list;
//   3. genuinely empty (no reviews AND no unregistered undecided claims) ->
//      the existing "검토 큐가 비어 있습니다" copy is preserved.
export async function check() {
  const reactResource = performance.getEntriesByType('resource').map(entry => entry.name)
    .find(name => /\/react\.js\?v=/.test(name));
  const version = reactResource ? new URL(reactResource).search : '';
  const { default: React } = await import(/* @vite-ignore */ "/node_modules/.vite/deps/react.js" + version);
  const { default: { createRoot } } = await import(/* @vite-ignore */ "/node_modules/.vite/deps/react-dom_client.js" + version);
  const { App } = await import('/src/App.tsx');
  const originalFetch = window.fetch;
  const originalPath = `${location.pathname}${location.search}`;
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const flush = () => new Promise(resolve => setTimeout(resolve, 40));
  const waitFor = async (text) => {
    for (let attempt = 0; attempt < 50; attempt += 1) {
      if (host.textContent.includes(text)) return;
      await flush();
    }
    throw new Error(`Timed out waiting for ${text}; rendered: ${host.textContent}`);
  };
  const assert = (ok, message) => { if (!ok) throw new Error(message); };

  const tenant = '11111111-1111-4111-8111-111111111111';
  const runId = '22222222-2222-4222-8222-222222222222';
  // Blocked-before-review claim: no review row, decision null (undecided).
  const blockedId = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa';
  const blockedClaim = { claim_id: blockedId, page_num: 90, quote: '물 리스크 평가 체계 구축 (BLOCKED)',
    track: null, topic_ids: ['environment'], decision: null, revision: 1 };
  // Reviewed claim: has an open review + a track; must appear only in the review nav.
  const reviewedId = 'bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb';
  const reviewedDecision = { decision_revision: 1, tag_revision: 1, decision_status: 'blocked_rule_gap',
    evidence_grade: null, label: null, sublabel: null, review_status: 'needs_review',
    missing_elements: [], rule_ids: [], rule_pack_sha256: 'b'.repeat(64), semantic_hash: 'c'.repeat(64), gap_ids: [] };
  const reviewedClaim = { claim_id: reviewedId, page_num: 12, quote: 'REVIEWED CLAIM QUOTE',
    track: 'performance', topic_ids: ['emissions'], decision: reviewedDecision, revision: 1 };
  const reviewedReview = { review_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', run_id: runId, claim_id: reviewedId,
    status: 'open', revision: 1, base_tag_revision: 1, reason_codes: ['RULEPACK_APPROVAL_REQUIRED'] };
  // Decided claim: decision_status decided; must never appear in the unregistered list.
  const decidedId = 'dddddddd-dddd-4ddd-8ddd-dddddddddddd';
  const decidedDecision = { decision_revision: 1, tag_revision: 1, decision_status: 'decided',
    evidence_grade: 'E1', label: 'INCOMPLETE', sublabel: 'PERF', review_status: 'auto_confirmed',
    missing_elements: [], rule_ids: [], rule_pack_sha256: 'b'.repeat(64), semantic_hash: 'c'.repeat(64), gap_ids: [] };
  const decidedClaim = { claim_id: decidedId, page_num: 5, quote: 'DECIDED CLAIM QUOTE',
    track: 'performance', topic_ids: ['emissions'], decision: decidedDecision, revision: 1 };

  const source = { source_id: '55555555-5555-4555-8555-555555555555',
    document_version_id: '66666666-6666-4666-8666-666666666666',
    parse_manifest_id: '77777777-7777-4777-8777-777777777777',
    page_num: 12, printed_page_label: null, bbox: null, raw_text_sha256: 'a'.repeat(64),
    quote: 'REVIEWED CLAIM QUOTE', char_start: 0, char_end: 20, location_quality: 'candidate', verification_state: 'candidate' };
  const extraSource = {...source, source_id:'88888888-8888-4888-8888-888888888888',page_num:13,quote:'EVIDENCE FROM ANOTHER PAGE'};
  let openedSource = null;
  const detailFor = (claim) => ({ claim, source_refs: [source],
    elements: claim.track ? [{ element_id: 'P1', state: 'unknown', evidence_refs: [extraSource], normalized_value: null, credited_from: null, reason_code: null }] : [],
    assurance: { status: 'undetermined', level: null, provider: null, statement_id: null,
      metric_match: 'unknown', period_match: 'unknown', boundary_match: 'unknown', evidence_refs: [] },
    replicate_request_ids: [], packet_sha256: null, tag_status: 'untagged', suggestion: null, basis_refs: [] });

  // Scenario switches driven by the current test phase.
  let claimsItems = [];
  let reviewsItems = [];

  window.fetch = async (url, init = {}) => {
    const target = new URL(String(url), location.origin);
    if (target.pathname.endsWith(`/sources/${extraSource.source_id}/view`)) {
      openedSource = extraSource.source_id;
      return Response.json({url:'/queue-preview.png',expires_at:'2099-01-01T00:00:00Z',sha256:'a'.repeat(64)});
    }
    if (target.pathname === '/queue-preview.png') return new Response(new Uint8Array([137,80,78,71]),{headers:{'Content-Type':'image/png'}});
    if (target.pathname === '/v1/session') return Response.json({ user_id: 'reviewer-1', tenant_id: tenant,
      role: 'reviewer', csrf_token: 'csrf-test', expires_at: '2026-09-10T00:00:00Z' });
    if (target.pathname === `/v1/runs/${runId}/claims`) return Response.json({ items: claimsItems, next_cursor: null, snapshot_epoch: 1 });
    if (target.pathname === `/v1/runs/${runId}/reviews`) return Response.json({ items: reviewsItems, next_cursor: null, snapshot_epoch: 1 });
    if (target.pathname === `/v1/runs/${runId}/claims/${reviewedId}`) return Response.json(detailFor(reviewedClaim));
    if (target.pathname === `/v1/runs/${runId}/claims/${blockedId}`) return Response.json(detailFor(blockedClaim));
    return Response.json({ error: { code: 'UNEXPECTED', message: target.pathname } }, { status: 500 });
  };

  try {
    // Each scenario mounts a fresh App with a distinct key so React fully
    // remounts and ReviewQueueWorkspace refetches with that scenario's fixture.
    let scenario = 0;
    const mount = async (path) => {
      root.render(React.createElement('div'));
      await flush();
      scenario += 1;
      history.pushState({}, '', path);
      root.render(React.createElement('div', { key: `scenario-${scenario}` }, React.createElement(App)));
    };

    // Scenario 1: empty reviews + one blocked-before-review claim.
    claimsItems = [blockedClaim];
    reviewsItems = [];
    await mount(`/runs/${runId}/reviews`);
    await waitFor('검토 큐');
    await waitFor('물 리스크 평가 체계 구축 (BLOCKED)');
    const s1 = host.querySelector('section[aria-labelledby="unregistered-heading"]');
    assert(s1, 'Unregistered section must render when a blocked claim exists');
    const blockedLink = [...s1.querySelectorAll('a')].find(a => a.getAttribute('href') === `/runs/${runId}/claims/${blockedId}`);
    assert(blockedLink, 'Blocked claim must be listed with a link to its claim detail');
    assert(!host.textContent.includes('현재 검토 큐가 비어 있습니다'), 'Empty-queue copy must not show when blocked claims exist');
    // Copy must not assert a single cause (source verified / track consensus failed).
    assert(!s1.textContent.includes('원문 검증은 통과') && !s1.textContent.includes('트랙에 합의'),
      'Unregistered copy must stay neutral, not claim all passed source verification');
    assert(s1.textContent.includes('편집할 수 없습니다'), 'Cannot-edit notice must be preserved');

    // Scenario 2: mixed — one open review + one blocked claim + one decided claim.
    claimsItems = [blockedClaim, reviewedClaim, decidedClaim];
    reviewsItems = [reviewedReview];
    await mount(`/runs/${runId}/reviews`);
    await waitFor('물 리스크 평가 체계 구축 (BLOCKED)');
    // Mixed state must render both the review nav button and the review editor.
    await waitFor('REVIEWED CLAIM QUOTE');
    const reviewNav = host.querySelector('nav[aria-label="검토 항목"]');
    assert(reviewNav && reviewNav.textContent.includes('REVIEWED CLAIM QUOTE'), 'Mixed state must keep the review nav button');
    assert(host.querySelector('textarea'), 'Mixed state must keep the existing review editor');
    // Scope duplication checks to the unregistered list only; the selected
    // review legitimately renders its own claim-detail link elsewhere.
    const unregSection = host.querySelector('section[aria-labelledby="unregistered-heading"]');
    assert(unregSection, 'Unregistered section must render in mixed state');
    const unregHrefs = [...unregSection.querySelectorAll('a')].map(a => a.getAttribute('href'));
    assert(!unregHrefs.includes(`/runs/${runId}/claims/${reviewedId}`), 'Reviewed claim must not be duplicated into the unregistered list');
    assert(!unregHrefs.includes(`/runs/${runId}/claims/${decidedId}`), 'Decided claim must never appear in the unregistered list');
    assert(unregHrefs.includes(`/runs/${runId}/claims/${blockedId}`), 'Blocked claim must still be listed in mixed state');

    // Read-only review still opens evidence from another page through SourceViewer.
    const evidenceButton = [...host.querySelectorAll('button')].find(button =>
      button.textContent === '원문 위치 열기' && button.parentElement.textContent.includes(extraSource.quote));
    assert(evidenceButton && !evidenceButton.matches(':disabled'), 'Evidence viewing must stay enabled');
    assert(host.querySelector('textarea').disabled, 'Approval hold must keep edits disabled');
    evidenceButton.click();
    await waitFor('PDF 13쪽 미리보기');
    assert(openedSource === extraSource.source_id, 'Open clicked evidence, not only claim source');

    // Scenario 3: genuinely empty — no reviews and no unregistered undecided claims.
    claimsItems = [decidedClaim];
    reviewsItems = [];
    await mount(`/runs/${runId}/reviews`);
    await waitFor('현재 검토 큐가 비어 있습니다');
    assert(![...host.querySelectorAll('a')].some(a => a.getAttribute('href') === `/runs/${runId}/claims/${decidedId}`),
      'Decided-only run must preserve empty copy and list nothing');

    return { blocked_visible: true, no_duplicates: true, decided_excluded: true, genuine_empty_preserved: true };
  } finally {
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
    history.pushState({}, '', originalPath);
  }
}
