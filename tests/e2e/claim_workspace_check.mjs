// Run in the task Vite browser:
// await import('/@fs/<repo>/tests/e2e/claim_workspace_check.mjs').then(m => m.check())
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
  const input = (element, value) => {
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype
      : element instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLSelectElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value);
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.dispatchEvent(new Event('input', { bubbles: true }));
  };
  const tenant = '11111111-1111-4111-8111-111111111111';
  const runId = '22222222-2222-4222-8222-222222222222';
  const claimId = '33333333-3333-4333-8333-333333333333';
  const reviewId = '44444444-4444-4444-8444-444444444444';
  const source = {
    source_id: '55555555-5555-4555-8555-555555555555',
    document_version_id: '66666666-6666-4666-8666-666666666666',
    parse_manifest_id: '77777777-7777-4777-8777-777777777777',
    page_num: 1, printed_page_label: null, bbox: [72, 60, 300, 100],
    raw_text_sha256: 'a'.repeat(64), quote: '<img src=x onerror=alert(1)> 1234 tCO2e',
    char_start: 0, char_end: 43, location_quality: 'located', verification_state: 'verified',
  };
  const decision = {
    decision_revision: 1, tag_revision: 1, decision_status: 'blocked_rule_gap',
    evidence_grade: null, label: null, sublabel: null, review_status: 'needs_review',
    missing_elements: [], rule_ids: [], rule_pack_sha256: 'b'.repeat(64),
    semantic_hash: 'c'.repeat(64), gap_ids: ['GAP-007'],
  };
  const claim = {
    claim_id: claimId, page_num: 1, quote: source.quote, track: 'performance',
    topic_ids: ['emissions'], decision, revision: 1,
  };
  const detail = {
    claim, source_refs: [source], elements: [{ element_id: 'P1', state: 'present',
      evidence_refs: [source], normalized_value: source.quote, credited_from: null, reason_code: null }],
    assurance: { status: 'undetermined', level: null, provider: null, statement_id: null,
      metric_match: 'unknown', period_match: 'unknown', boundary_match: 'unknown', evidence_refs: [] },
    replicate_request_ids: ['req-1', 'req-2', 'req-3'], packet_sha256: 'd'.repeat(64),
    suggestion: null, basis_refs: [],
  };
  let resolveHeaders;
  let resolveBody;
  let resolveCalls = 0;
  let sourceTickets = 0;
  let lateMode = false;
  let lateSignal;
  let releaseLate;
  const review = { review_id: reviewId, run_id: runId, claim_id: claimId,
    status: 'open', revision: 2, base_tag_revision: 1, reason_codes: ['CONSENSUS_UNRESOLVED'] };

  window.fetch = async (url, init = {}) => {
    const target = new URL(String(url), location.origin);
    if (target.pathname === '/v1/session/tenant') return Response.json({ user_id: 'reviewer-1', tenant_id: '99999999-9999-4999-8999-999999999999',
      role: 'reviewer', csrf_token: 'csrf-new', expires_at: '2026-09-10T00:00:00Z' });
    if (target.pathname === '/v1/session') return Response.json({ user_id: 'reviewer-1', tenant_id: tenant,
      role: 'reviewer', csrf_token: 'csrf-test', expires_at: '2026-09-10T00:00:00Z' });
    if (target.pathname === `/v1/runs/${runId}/claims`) {
      if (lateMode) {
        lateSignal = init.signal;
        return new Promise(resolve => { releaseLate = resolve; });
      }
      if (location.pathname.endsWith('/claims')) {
        assert(target.searchParams.get('track') === 'performance', 'Track filter must stay in URL/API');
        assert(target.searchParams.get('grade') === 'E1', 'Grade filter must stay in URL/API');
        assert(target.searchParams.get('review_status') === 'needs_review', 'Review filter must stay in URL/API');
      }
      return Response.json({ items: [claim], next_cursor: null, snapshot_epoch: 1 });
    }
    if (target.pathname === `/v1/runs/${runId}/claims/${claimId}`) return Response.json(detail);
    if (target.pathname === `/v1/runs/${runId}/reviews`) return Response.json({ items: [review], next_cursor: null, snapshot_epoch: 1 });
    if (target.pathname === `/v1/runs/${runId}/sources/${source.source_id}/view`) {
      sourceTickets += 1;
      assert(init.method === 'POST', 'Source URL must be issued with POST');
      assert(new Headers(init.headers).get('X-CSRF-Token') === 'csrf-test', 'Source POST must use CSRF');
      return Response.json({ url: `/preview.pdf?ticket=fixture-${sourceTickets}#page=1`, expires_at: '2026-09-10T00:00:00Z', sha256: 'e'.repeat(64) });
    }
    if (target.pathname === '/preview.pdf' && target.searchParams.get('ticket') === 'fixture-1') return Response.json({}, { status: 403 });
    if (target.pathname === '/preview.pdf') {
      assert(target.searchParams.get('preview') === 'page', 'Preview ticket must request the page projection');
      return new Response(new Blob(['png'], { type: 'image/png' }), {
      status: 200, headers: { 'Content-Type': 'image/png', 'X-Page-Width-Pt': '600',
        'X-Page-Height-Pt': '800', 'X-Source-Highlight': 'allowed' },
      });
    }
    if (target.pathname === `/v1/reviews/${reviewId}/resolve`) {
      resolveCalls += 1;
      resolveHeaders = new Headers(init.headers);
      resolveBody = JSON.parse(String(init.body));
      return Response.json({ error: { code: 'STALE_REVIEW_REVISION', message: 'stale', retryable: false } }, { status: 412 });
    }
    return Response.json({ error: { code: 'UNEXPECTED', message: target.pathname } }, { status: 500 });
  };

  try {
    history.pushState({}, '', `/runs/${runId}/claims?track=performance&grade=E1&review_status=needs_review`);
    root.render(React.createElement(App));
    await waitFor('1234 tCO2e');
    assert(host.querySelector('h1')?.textContent.includes('주장'), 'Claims route must not render upload page');

    history.pushState({}, '', `/runs/${runId}/claims/${claimId}`);
    window.dispatchEvent(new PopStateEvent('popstate'));
    await waitFor('보증 범위를 확인할 수 없습니다');
    assert(!host.querySelector('img[src="x"]'), 'Source quote must render as text, never HTML');
    assert(host.textContent.includes(`값: ${source.quote}`), 'Element must show its stored value as text');
    const sourceButton = [...host.querySelectorAll('button')].find(button => button.textContent.includes('원문 위치'));
    sourceButton.click();
    await waitFor('원본 PDF 새 탭에서 열기');
    assert(host.querySelector('img[alt*="1쪽"]'), 'Verified located source should render the issued PNG preview');
    assert(host.querySelector('[data-source-highlight="true"]'), 'Allowed verified bbox should render a real overlay');
    assert(sourceTickets === 2, 'Expired source preview should issue exactly one replacement ticket');
    const elementEvidence = host.querySelector('details[data-element-evidence="P1"]');
    assert(elementEvidence, 'Element evidence should be available beside its value');
    elementEvidence.querySelector('summary').click();
    assert(elementEvidence.textContent.includes(source.quote), 'Evidence must retain the exact quote');
    elementEvidence.querySelector('button').click();
    for (let attempt = 0; attempt < 20 && sourceTickets < 3; attempt += 1) await flush();
    assert(sourceTickets === 3, 'Element source button should reuse the authenticated source viewer');

    history.pushState({}, '', `/runs/${runId}/reviews?track=performance&grade=E1&review_status=needs_review`);
    window.dispatchEvent(new PopStateEvent('popstate'));
    await waitFor('태깅 검토');
    const reason = host.querySelector('textarea');
    input(reason, '412 뒤에도 남아야 하는 초안');
    await flush();
    [...host.querySelectorAll('button')].find(button => button.textContent === '변경 확인').click();
    await flush();
    [...host.querySelectorAll('dialog button')].find(button => button.textContent.includes('태깅 확정')).click();
    await waitFor('초안을 유지한 채');
    assert(resolveCalls === 1, 'Review resolution should submit exactly once');
    assert(resolveHeaders.get('X-CSRF-Token') === 'csrf-test', 'Review resolution must send CSRF');
    assert(resolveHeaders.get('If-Match') === '"2"', 'Review resolution must send quoted If-Match');
    assert(resolveBody.reason === '412 뒤에도 남아야 하는 초안', 'Review draft must reach API');
    assert(reason.value === '412 뒤에도 남아야 하는 초안', '412 must preserve the local draft');

    lateMode = true;
    history.pushState({}, '', `/runs/${runId}/claims`);
    window.dispatchEvent(new PopStateEvent('popstate'));
    for (let attempt = 0; attempt < 20 && !lateSignal; attempt += 1) await flush();
    assert(lateSignal, 'Late claim request must start before tenant replacement');
    const tenantSettings = host.querySelector('#tenant-id').closest('details');
    assert(tenantSettings && !tenantSettings.open, 'Selected tenant settings should start collapsed');
    tenantSettings.querySelector('summary').click();
    assert(tenantSettings.open, 'Tenant switch must remain accessible');
    input(host.querySelector('#tenant-id'), '99999999-9999-4999-8999-999999999999');
    await flush();
    host.querySelector('form[aria-label="작업 테넌트 선택"]').requestSubmit();
    await waitFor('새 문서 등록');
    releaseLate(Response.json({ items: [{ ...claim, quote: 'FOREIGN TENANT LEAK' }], next_cursor: null, snapshot_epoch: 2 }));
    await flush();
    assert(lateSignal.aborted, 'Tenant replacement must abort the old run request');
    assert(!host.textContent.includes('FOREIGN TENANT LEAK'), 'Late foreign-tenant data must be ignored');
    return { route: true, filters: true, source_projection: true, csrf_if_match: true,
      draft_412: true, source_renew_once: true, foreign_session_cleared: true };
  } finally {
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
    history.pushState({}, '', originalPath);
  }
}
