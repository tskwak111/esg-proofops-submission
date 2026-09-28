// Run check() in the task Vite browser. Actual App/React interaction, mock API.
export async function check() {
  const resource = performance.getEntriesByType('resource').map(e => e.name).find(n => /\/react\.js\?v=/.test(n));
  const version = resource ? new URL(resource).search : '';
  const { default: React } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react.js' + version);
  const { default: { createRoot } } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react-dom_client.js' + version);
  const { App } = await import('/src/App.tsx');
  const originalFetch = window.fetch, originalPath = location.pathname + location.search;
  const host = document.createElement('div'); document.body.append(host);
  const root = createRoot(host);
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const waitFor = async predicate => {
    for (let i = 0; i < 80; i++) { if (predicate()) return; await new Promise(r => setTimeout(r, 25)); }
    throw new Error('UI timeout: ' + host.textContent);
  };
  const button = text => [...host.querySelectorAll('button')].find(b => b.textContent === text);
  const click = async text => { await waitFor(() => button(text) && !button(text).disabled); button(text).click(); };
  const run = '22222222-2222-4222-8222-222222222222', claimId = '33333333-3333-4333-8333-333333333333';
  const reviewId = '44444444-4444-4444-8444-444444444444';
  let revision = 2, head = 7;
  const posts = [];
  const elements = Array.from({ length: 6 }, (_, i) => ({ element_id: `M${i + 1}`, state: 'unknown',
    evidence_refs: [], normalized_value: null, credited_from: null, reason_code: null }));
  const review = () => ({ review_id: reviewId, run_id: run, claim_id: claimId, status: 'resolved',
    revision, base_tag_revision: 1, reason_codes: ['CONSENSUS_UNRESOLVED'] });
  const decision = () => ({ decision_revision: revision - 1, tag_revision: head, decision_status: 'blocked_evidence',
    evidence_grade: null, label: null, sublabel: null, review_status: 'human_confirmed', missing_elements: [],
    rule_ids: [], gap_ids: [], rule_pack_sha256: 'a'.repeat(64), semantic_hash: 'b'.repeat(64) });
  const claim = () => ({ claim_id: claimId, page_num: 90, quote: '재검토 대상 주장', track: 'management',
    topic_ids: ['environment'], decision: decision(), revision: head });
  window.fetch = async (url, init = {}) => {
    const path = new URL(String(url), location.origin).pathname;
    if (path === '/v1/session') return Response.json({ user_id: 'reviewer', tenant_id: run, role: 'reviewer', csrf_token: 'test' });
    if (path === `/v1/runs/${run}/reviews`) return Response.json({ items: [review()], next_cursor: null, snapshot_epoch: revision });
    if (path === `/v1/runs/${run}/claims`) return Response.json({ items: [claim()], next_cursor: null, snapshot_epoch: head });
    if (path === `/v1/runs/${run}/claims/${claimId}`) return Response.json({ claim: claim(), elements, source_refs: [],
      assurance: { status: 'undetermined', level: null, provider: null, statement_id: null, metric_match: 'unknown',
        period_match: 'unknown', boundary_match: 'unknown', evidence_refs: [] },
      replicate_request_ids: [], packet_sha256: null, tag_status: 'reviewed', suggestion: null, basis_refs: [] });
    if (path === `/v1/reviews/${reviewId}/re-review` && init.method === 'POST') {
      const body = JSON.parse(init.body), headers = new Headers(init.headers);
      posts.push({ body, match: headers.get('If-Match'), key: headers.get('Idempotency-Key') });
      assert(body.base_tag_revision === head, 'Must submit fetched current tag revision, not original review base');
      assert(headers.get('If-Match') === `"${revision}"`, 'Must submit current review revision');
      if (posts.length === 1) { revision = 3; head = 11; return Response.json({ error: { code: 'STALE_REVIEW_REVISION' } }, { status: 412 }); }
      revision++; head++;
      return Response.json({ review: review(), new_tag_revision: head, decision: decision() });
    }
    throw new Error('Unexpected API: ' + path);
  };
  const enterReason = async text => {
    const area = host.querySelector('textarea');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(area, text);
    area.dispatchEvent(new Event('input', { bubbles: true }));
    await waitFor(() => !button('변경 확인').disabled);
  };
  const submit = async () => { await click('변경 확인'); await waitFor(() => host.querySelector('dialog[open]')); await click('태깅 확정 및 재채점'); };
  try {
    history.pushState({}, '', `/runs/${run}/reviews`);
    root.render(React.createElement(App));
    await click('재검토 시작');
    await enterReason('원문을 다시 확인한 검토 사유');
    await submit();
    await waitFor(() => host.querySelector('section[aria-label="충돌 비교"]'));
    assert(host.querySelector('textarea').value === '원문을 다시 확인한 검토 사유', '412 must preserve the draft');
    await click('차이를 확인했습니다. 내 초안을 새 기준에서 다시 검토');
    await submit();
    await waitFor(() => posts.length === 2 && button('재검토 시작'));
    assert(posts[0].key !== posts[1].key, 'Rebased request needs a new idempotency key');
    await click('재검토 시작');
    await enterReason('세 번째 검토도 최신 버전으로 제출');
    await submit();
    await waitFor(() => posts.length === 3 && button('재검토 시작'));
    assert(head === 13 && revision === 5, 'Repeated review must reload the new current head');
    return { explicit_re_review: true, stale_draft_preserved: true, fresh_head_rebase: true,
      repeated_review: true, submitted_tag_revisions: posts.map(p => p.body.base_tag_revision) };
  } finally { root.unmount(); host.remove(); window.fetch = originalFetch; history.pushState({}, '', originalPath); }
}
