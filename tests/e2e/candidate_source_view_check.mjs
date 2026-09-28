// Regression for the claim detail candidate source buttons.
//
// Before the fix, "원문 N쪽 보기" under 미검증 근거 후보 called
// document.getElementById(`review-source-<id>`).focus(); that element only
// exists on the review queue screen, so on the claim detail screen every
// candidate button was a silent no-op and the candidate page was never fetched.
//
// Run in the task Vite browser:
// await import('/@fs/<repo>/tests/e2e/candidate_source_view_check.mjs').then(m => m.check())
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
  const button = (text) => [...host.querySelectorAll('button')].find(node => node.textContent.includes(text));
  const previewRegion = () => host.querySelector('section[aria-label="선택한 원문 위치"]');

  const tenant = '11111111-1111-4111-8111-111111111111';
  const runId = '22222222-2222-4222-8222-222222222222';
  const claimId = '33333333-3333-4333-8333-333333333333';
  const otherClaimId = '44444444-4444-4444-8444-444444444444';
  const base = {
    document_version_id: '66666666-6666-4666-8666-666666666666',
    parse_manifest_id: '77777777-7777-4777-8777-777777777777',
    printed_page_label: null, raw_text_sha256: 'a'.repeat(64), char_start: 0, char_end: 20,
  };
  // Accepted evidence: verified and located, so it may render a highlight.
  const own = { ...base, source_id: '55555555-5555-4555-8555-555555555555', page_num: 1,
    bbox: [72, 60, 300, 100], quote: '2030년까지 배출량 42% 감축', location_quality: 'located',
    verification_state: 'verified' };
  // Model candidate: same shape and a valid bbox, but never verified.
  const candidate = { ...base, source_id: '88888888-8888-4888-8888-888888888888', page_num: 7,
    bbox: [50, 50, 200, 200], quote: '후보 문단: 감축 이행 경로 설명', location_quality: 'located',
    verification_state: 'candidate' };

  const decision = {
    decision_revision: 1, tag_revision: 1, decision_status: 'blocked_evidence',
    evidence_grade: null, label: null, sublabel: null, review_status: 'needs_review',
    missing_elements: ['G3'], rule_ids: [], rule_pack_sha256: 'b'.repeat(64),
    semantic_hash: 'c'.repeat(64), gap_ids: [],
  };
  const claim = { claim_id: claimId, page_num: 1, quote: own.quote, track: 'goal',
    topic_ids: ['emissions'], decision, revision: 1 };
  const assurance = { status: 'undetermined', level: null, provider: null, statement_id: null,
    metric_match: 'unknown', period_match: 'unknown', boundary_match: 'unknown', evidence_refs: [] };
  const replicaHash = 'sha256:9f2b7c1d4e5a6b8c9d0e1f2a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e';
  const detail = {
    claim, source_refs: [own],
    elements: [{ element_id: 'G3', state: 'unknown', evidence_refs: [], normalized_value: null,
      credited_from: null, reason_code: null }],
    assurance, replicate_request_ids: ['req-1', 'req-2', 'req-3'], packet_sha256: 'd'.repeat(64),
    tag_status: 'tagged', suggestion: null, basis_refs: [],
    review_projection: { schema_version: 1, candidate_snippets: ['감축 이행 경로 설명'],
      blocked_reason: 'SOURCE_SPAN_UNVERIFIED', blocked_action: '원문 대조 필요',
      field_agreements: [{ field_id: 'target_year', status: 'conflict',
        replicate_values: [replicaHash, replicaHash, replicaHash] }],
      raw_candidates: [{ source_ref: candidate, status: 'unverified', reason: 'QUOTE_NOT_FOUND' }] },
  };
  const otherClaim = { claim_id: otherClaimId, page_num: 9, quote: '다른 주장 인용', track: 'goal',
    topic_ids: [], decision: null, revision: 1 };
  const otherDetail = { claim: otherClaim, source_refs: [], elements: [], assurance,
    replicate_request_ids: [], packet_sha256: null, tag_status: 'untagged', suggestion: null,
    basis_refs: [], review_projection: null };

  const tickets = [];
  let failCandidateTicketOnce = true;

  window.fetch = async (url, init = {}) => {
    const target = new URL(String(url), location.origin);
    if (target.pathname === '/v1/session') return Response.json({ user_id: 'reviewer-1',
      tenant_id: tenant, role: 'reviewer', csrf_token: 'csrf-test', expires_at: '2026-09-10T00:00:00Z' });
    if (target.pathname === `/v1/runs/${runId}/claims`) return Response.json({ items: [claim, otherClaim], next_cursor: null, snapshot_epoch: 1 });
    if (target.pathname === `/v1/runs/${runId}/claims/${claimId}`) return Response.json(detail);
    if (target.pathname === `/v1/runs/${runId}/claims/${otherClaimId}`) return Response.json(otherDetail);
    if (target.pathname.endsWith('/classification')) return Response.json({ error: { code: 'NOT_FOUND', message: 'no classification' } }, { status: 404 });
    const ticket = target.pathname.match(new RegExp(`^/v1/runs/${runId}/sources/([^/]+)/view$`));
    if (ticket) {
      const sourceId = ticket[1];
      assert(init.method === 'POST', 'Source URL must be issued with POST');
      assert(new Headers(init.headers).get('X-CSRF-Token') === 'csrf-test', 'Source POST must use CSRF');
      if (sourceId === candidate.source_id && failCandidateTicketOnce) {
        failCandidateTicketOnce = false;
        tickets.push(`${sourceId}:failed`);
        return Response.json({ error: { code: 'SOURCE_VIEW_UNAVAILABLE', message: 'transient' } }, { status: 503 });
      }
      tickets.push(sourceId);
      return Response.json({ url: `/preview.pdf?ticket=${sourceId}#page=${sourceId === candidate.source_id ? 7 : 1}`,
        expires_at: '2026-09-10T00:00:00Z', sha256: 'e'.repeat(64) });
    }
    if (target.pathname === '/preview.pdf') {
      assert(target.searchParams.get('preview') === 'page', 'Preview ticket must request the page projection');
      // The server allows highlighting for both tickets; only the client-side
      // verification guard may keep the candidate overlay off.
      return new Response(new Blob(['png'], { type: 'image/png' }), { status: 200,
        headers: { 'Content-Type': 'image/png', 'X-Page-Width-Pt': '600',
          'X-Page-Height-Pt': '800', 'X-Source-Highlight': 'allowed' } });
    }
    return Response.json({ error: { code: 'UNEXPECTED', message: target.pathname } }, { status: 500 });
  };

  try {
    history.pushState({}, '', `/runs/${runId}/claims/${claimId}`);
    root.render(React.createElement(App));
    await waitFor('미검증 근거 후보');

    // The replica JSON must not dominate the page before the human sees the claim.
    const fieldSummary = [...host.querySelectorAll('details > summary')]
      .find(node => node.textContent.includes('모델 응답 필드 상세'));
    assert(fieldSummary, 'Replica field values must sit behind a native disclosure');
    assert(!fieldSummary.parentElement.open, 'Replica field disclosure must start collapsed');
    assert(fieldSummary.parentElement.textContent.includes(replicaHash), 'Exact replica values must stay available');

    // Accepted evidence keeps working, including its verified highlight.
    button('원문 위치 열기').click();
    await waitFor('원본 PDF 새 탭에서 열기');
    assert(host.querySelector('img[alt*="1쪽"]'), 'Verified source must render its issued page');
    assert(host.querySelector('[data-source-highlight="true"]'), 'Verified located source keeps its overlay');
    assert(tickets.join(',') === own.source_id, `Only the accepted source was requested: ${tickets}`);

    // First candidate click: the ticket fails, so the user sees the error.
    button('원문 7쪽 보기').click();
    await waitFor('원문 위치를 열지 못했습니다');
    assert(tickets.includes(`${candidate.source_id}:failed`), 'Candidate click must issue a candidate-specific ticket');
    assert(!host.querySelector('img[alt*="7쪽"]'), 'A failed ticket must not fabricate a page');

    // Retrying the same candidate must work rather than stay dead.
    button('원문 7쪽 보기').click();
    await waitFor('PDF 7쪽 미리보기');
    assert(tickets[tickets.length - 1] === candidate.source_id, 'Retry must re-issue the candidate ticket');
    assert(host.querySelector(`img[alt*="7쪽"]`), 'Candidate click must render the chosen candidate page');
    assert(!host.querySelector('img[alt*="1쪽"]'), 'Candidate page must replace the previous preview');
    assert(!host.querySelector('[data-source-highlight="true"]'), 'Unverified candidate must never be highlighted');
    assert(host.textContent.includes('정식 근거로 채택되지 않은 후보 위치'), 'Candidate preview must stay labelled unverified');
    assert(document.activeElement === previewRegion(), 'Candidate open must move focus to the rendered page');

    // Switching claims must drop the previous claim's source preview.
    history.pushState({}, '', `/runs/${runId}/claims/${otherClaimId}`);
    window.dispatchEvent(new PopStateEvent('popstate'));
    await waitFor('연결된 원문 근거가 없습니다');
    assert(!host.querySelector('img[alt*="7쪽"]'), `Stale source preview must be cleared on claim change; tickets=${tickets}`);
    assert(tickets.length === 3, `Claim change must not replay the previous selection: ${tickets}`);
    assert(!host.textContent.includes('PDF 7쪽 미리보기'), 'Stale preview caption must be cleared');
    assert(!host.textContent.includes(candidate.quote), 'Stale candidate must not leak into the next claim');

    return { candidate_button_issues_own_ticket: true, candidate_page_rendered: true,
      candidate_retry_after_error: true, unverified_never_highlighted: true,
      preview_focus_moved: true, stale_claim_cleared: true, accepted_source_still_works: true,
      replica_json_collapsed: true };
  } finally {
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
    history.pushState({}, '', originalPath);
  }
}
