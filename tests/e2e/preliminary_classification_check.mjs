// Run with Vite: import('/@fs/<repo>/tests/e2e/preliminary_classification_check.mjs').then(m => m.check())
export async function check() {
  const resource = performance.getEntriesByType('resource').map(x => x.name).find(x => /\/react\.js\?v=/.test(x));
  const version = resource ? new URL(resource).search : '';
  const { default: React } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react.js' + version);
  const { default: { createRoot } } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react-dom_client.js' + version);
  const { App } = await import('/src/App.tsx');
  const originalFetch = window.fetch, originalPath = location.pathname + location.search;
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host);
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const flush = () => new Promise(resolve => setTimeout(resolve, 40));
  const wait = async text => {
    for (let i = 0; i < 50; i++) { if (host.textContent.includes(text)) return; await flush(); }
    throw new Error('Missing ' + text + ': ' + host.textContent);
  };
  const input = (selector, value) => {
    const element = host.querySelector(selector);
    assert(element, 'Missing input ' + selector);
    const prototype = element instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : element instanceof HTMLInputElement ? HTMLInputElement.prototype : HTMLSelectElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(element, value);
    element.dispatchEvent(new Event('change', { bubbles: true }));
    element.dispatchEvent(new Event('input', { bubbles: true }));
  };
  const run = '22222222-2222-4222-8222-222222222222', claim = '33333333-3333-4333-8333-333333333333';
  const endpoint = `/v1/runs/${run}/claims/${claim}/classification`;
  let role = 'reviewer', eligible = true, stale = false, posts = [], reads = 0, scenario = 0;
  const state = () => ({ run_id: run, claim_id: claim, eligible, ineligible_reason: eligible ? null : 'SOURCE_VALIDATION_REQUIRED', blocked_reason: 'PRELIMINARY_TAGS_UNRESOLVED',
    etag: '"' + 'a'.repeat(64) + '"', sources: [{ source_index: 0, quote: '물 리스크 평가 체계 구축', source_ref: { page_num: 90 } }],
    dimension_axes: ['entity', 'metric', 'reporting_period'], current_classification: null, pending_job: null });
  window.fetch = async (url, init = {}) => {
    const path = new URL(String(url), location.origin).pathname;
    if (path === '/v1/session') return Response.json({ user_id: 'reviewer', tenant_id: '11111111-1111-4111-8111-111111111111', role, csrf_token: 'csrf-test', expires_at: '2099-01-01T00:00:00Z' });
    if (path === `/v1/runs/${run}/claims/${claim}`) return Response.json({
      claim: { claim_id: claim, page_num: 90, quote: '물 리스크 평가 체계 구축', track: null, decision: null, revision: 1, topic_ids: [] },
      source_refs: [], elements: [], assurance: { status: 'undetermined', evidence_refs: [] },
      replicate_request_ids: [], packet_sha256: null, tag_status: 'untagged', suggestion: null, basis_refs: [],
      review_projection: { schema_version: 1, candidate_snippets: [], blocked_reason: 'PRELIMINARY_TAGS_UNRESOLVED', blocked_action: null, field_agreements: [] },
    });
    if (path === endpoint && init.method === 'POST') {
      posts.push({ body: JSON.parse(init.body), headers: new Headers(init.headers) });
      return stale ? Response.json({ error: { code: 'STALE_CLASSIFICATION', message: 'stale' } }, { status: 412 })
        : Response.json({ classification: { origin: 'human_classification', track: 'management', revision: 1 }, reprocess_job: { job_id: claim, status: 'pending', claim_ids: [claim] } }, { status: 202 });
    }
    if (path === endpoint) { reads++; return Response.json(state()); }
    return Response.json({ error: { code: 'UNEXPECTED', message: path } }, { status: 500 });
  };
  const mount = async () => {
    history.replaceState({}, '', `/runs/${run}/claims/${claim}`);
    root.render(React.createElement(App, { key: ++scenario }));
    await wait('물 리스크 평가 체계 구축'); await flush();
  };
  const prepare = async (withQuote = false) => {
    await wait('분류 확정 및 태깅 요청');
    const submit = host.querySelector('[data-classification-submit]');
    assert(submit.disabled, 'No default classification can be submitted');
    input('[name="classification-track"]', 'management');
    input('[name="classification-category"]', 'unknown');
    input('[name="classification-reason"]', '원문에서 평가 체계 구축을 확인했습니다.');
    if (withQuote) {
      input('[name="classification-source-metric"]', '0'); await flush();
      input('[name="classification-quote-metric"]', '물 리스크');
    }
    host.querySelector('[name="classification-acknowledge"]').click();
    await flush();
    assert(!submit.disabled, 'Explicit classification should enable submit');
    submit.click();
  };
  try {
    await mount(); await prepare(true); await wait('태깅 요청을 등록했습니다');
    assert(posts.length === 1, 'One submission');
    assert(posts[0].headers.get('If-Match') === state().etag && posts[0].headers.get('X-CSRF-Token') === 'csrf-test', 'CAS/CSRF required');
    assert(posts[0].headers.get('Idempotency-Key').length >= 16, 'Idempotency key required');
    assert(posts[0].body.track === 'management' && posts[0].body.safe_harbor_category === null, 'Explicit classification values');
    assert(posts[0].body.dimensions.entity === null && posts[0].body.dimensions.reporting_period === null, 'Unknown dimensions stay unknown');
    assert(posts[0].body.dimensions.metric.source_index === 0 && posts[0].body.dimensions.metric.quote === '물 리스크', 'Preserve exact selected source and quote');
    assert(!('origin' in posts[0].body) && !('track_confidence' in posts[0].body) && !('grade' in posts[0].body), 'No invented provenance/confidence/grade');
    stale = true; await mount(); await prepare(); await wait('상태가 변경되었습니다');
    assert(host.querySelector('[data-classification-submit]').disabled, 'Stale draft must not auto-resubmit');
    assert(host.querySelector('[name="classification-reason"]').value.includes('원문에서'), 'Keep draft after conflict');
    eligible = false; await mount(); await wait('원문 검증이 먼저 필요합니다');
    assert(!host.querySelector('[data-classification-submit]'), 'Source-blocked claim has no submission form');
    role = 'viewer'; const before = reads; await mount(); await flush();
    assert(reads === before && !host.querySelector('[data-classification-submit]'), 'Viewer cannot enter reviewer flow');
    return { explicit_selection: true, secure_submission: true, stale_blocked: true, source_blocked: true, viewer_guard: true };
  } finally { root.unmount(); host.remove(); window.fetch = originalFetch; history.replaceState({}, '', originalPath); }
}
