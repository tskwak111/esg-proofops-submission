// Run in a Vite browser: await import('/@fs/<repo>/tests/e2e/comparison_workspace_check.mjs').then(m => m.check()).
// CONTROLLED HTTP BOUNDARY: window.fetch below supplies complete fixed-API DTOs; no backend is exercised here.
export async function check() {
  const reactResource = performance.getEntriesByType('resource').map(entry => entry.name)
    .find(name => /\/react\.js\?v=/.test(name));
  const version = reactResource ? new URL(reactResource).search : '';
  const { default: React } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react.js' + version);
  const { default: { createRoot } } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react-dom_client.js' + version);
  const { ComparisonWorkspace } = await import('/src/features/comparison/ComparisonWorkspace.tsx');
  const originalFetch = window.fetch;
  const host = document.createElement('div');
  document.body.append(host);
  let root = createRoot(host);
  const flush = () => new Promise(resolve => setTimeout(resolve, 40));
  const waitFor = async text => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (host.textContent.includes(text)) return;
      await flush();
    }
    throw new Error(`Timed out waiting for: ${text}`);
  };
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const input = (element, value) => {
    Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new Event('change', { bubbles: true }));
  };
  const button = text => [...host.querySelectorAll('button')].find(item => item.textContent.includes(text));

  const runId = '11111111-1111-4111-8111-111111111111';
  const currentVersionId = '22222222-2222-4222-8222-222222222222';
  const currentDocumentId = '33333333-3333-4333-8333-333333333333';
  const priorDocumentId = '44444444-4444-4444-8444-444444444444';
  const foreignDocumentId = '55555555-5555-4555-8555-555555555555';
  const priorVersionId = '66666666-6666-4666-8666-666666666666';
  const comparisonId = '77777777-7777-4777-8777-777777777777';
  const companyId = '88888888-8888-4888-8888-888888888888';
  const foreignCompanyId = '99999999-9999-4999-8999-999999999999';
  const createdAt = '2026-09-09T00:00:00Z';
  const run = { run_id: runId, document_version_id: currentVersionId, status: 'completed',
    current_stage: 'report', revision: 1, mutation_epoch: 1,
    coverage: { pages_total: 1, pages_processed: 1, pages_unreadable: 0, pages_unprocessed: 0,
      chunks_discovered: 1, chunks_processed: 1, claims_discovered: 1, claims_decided: 1,
      claims_needs_review: 0, full_scope: true, complete: true },
    rule_pack_sha256: 'a'.repeat(64), created_at: createdAt };
  const documentRecord = (document_id, company_id, title) => ({ document_id, company_id, title,
    document_type: 'sustainability_report', latest_version_id: null, revision: 1, created_at: createdAt });
  const documentVersion = (version_id, document_id, report_year, status = 'ready') => ({
    version_id, document_id, sha256: 'b'.repeat(64), report_year, page_count: 1, status, created_at: createdAt });
  const completed = { comparison_id: comparisonId, status: 'completed', reason: null,
    changes: [{ current_claim_id: null, prior_claim_id: priorVersionId,
      type: 'removed_candidate', reason: 'controlled candidate' }] };
  const calls = [];
  let createCalls = 0;
  let comparisonReads = 0;
  let mode = 'normal';
  let lateLoadHeld = false;
  let lateSignal;
  let releaseLate;
  let sessionInvalid = 0;

  window.fetch = async (url, init = {}) => {
    const target = new URL(String(url), location.origin);
    calls.push({ path: target.pathname, cursor: target.searchParams.get('cursor'), init, mode });
    if (mode === 'late-load' && !lateLoadHeld && target.pathname === `/v1/runs/${runId}`) {
      lateLoadHeld = true;
      lateSignal = init.signal;
      return new Promise(resolve => { releaseLate = () => resolve(Response.json(run)); });
    }
    if (target.pathname === `/v1/runs/${runId}`) return Response.json(run);
    if (target.pathname === `/v1/versions/${currentVersionId}`) {
      if (mode === 'missing-artifact') return Response.json({ error: { code: 'NOT_FOUND', message: 'missing' } }, { status: 404 });
      return Response.json(documentVersion(currentVersionId, currentDocumentId, 2025));
    }
    if (target.pathname === `/v1/documents/${currentDocumentId}`) {
      return Response.json(documentRecord(currentDocumentId, companyId, '현재 보고서'));
    }
    if (target.pathname === '/v1/documents') {
      if (mode === 'repeat-cursor') {
        return Response.json({ items: [documentRecord(currentDocumentId, companyId, '현재 보고서')],
          next_cursor: 'repeated', snapshot_epoch: 1 });
      }
      return target.searchParams.get('cursor')
        ? Response.json({ items: [documentRecord(priorDocumentId, companyId, '전년 보고서')], next_cursor: null, snapshot_epoch: 1 })
        : Response.json({ items: [documentRecord(currentDocumentId, companyId, '현재 보고서'),
          documentRecord(foreignDocumentId, foreignCompanyId, '다른 기업 보고서')], next_cursor: 'documents-2', snapshot_epoch: 1 });
    }
    if (target.pathname === `/v1/documents/${priorDocumentId}/versions`) {
      return target.searchParams.get('cursor')
        ? Response.json({ items: [documentVersion(priorVersionId, priorDocumentId, 2024)], next_cursor: null, snapshot_epoch: 2 })
        : Response.json({ items: [documentVersion('aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa', priorDocumentId, 2024, 'validating'),
          documentVersion('bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb', priorDocumentId, 2023)], next_cursor: 'versions-2', snapshot_epoch: 2 });
    }
    if (target.pathname === `/v1/runs/${runId}/comparisons` && init.method === 'POST') {
      createCalls += 1;
      if (mode === 'session') {
        return Response.json({ error: { code: 'SESSION_EXPIRED', message: 'expired', retryable: false } }, { status: 401 });
      }
      if (mode === 'environment-disabled') {
        return Response.json({ error: { code: 'YEAR_COMPARISON_DISABLED', message: 'disabled', retryable: false } }, { status: 409 });
      }
      if (mode === 'late-create') {
        lateSignal = init.signal;
        return new Promise(resolve => { releaseLate = () => resolve(Response.json({
          job_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', resource_id: comparisonId,
          status: 'queued', status_url: '/v1/evil' }, { status: 202 })); });
      }
      if (createCalls === 1) return Response.json({ error: { code: 'DEPENDENCY_UNAVAILABLE', message: 'retry', retryable: true } }, { status: 503 });
      return Response.json({ job_id: 'cccccccc-cccc-4ccc-8ccc-cccccccccccc', resource_id: comparisonId,
        status: 'queued', status_url: '/v1/evil' }, { status: 202 });
    }
    if (target.pathname === `/v1/comparisons/${comparisonId}`) {
      if (mode === 'late-get') {
        lateSignal = init.signal;
        return new Promise(resolve => { releaseLate = () => resolve(Response.json(completed)); });
      }
      comparisonReads += 1;
      return Response.json(comparisonReads === 1
        ? { comparison_id: comparisonId, status: 'queued', reason: null, changes: [] }
        : completed);
    }
    return Response.json({ error: { code: 'UNEXPECTED', message: target.pathname } }, { status: 500 });
  };

  const props = (tenantKey, overrides = {}) => ({ apiBase: location.origin, csrfToken: 'csrf-test', tenantKey,
    runId, canCompare: true, onSessionInvalid: () => { sessionInvalid += 1; }, ...overrides });
  const selectPrior = async () => {
    const firstNewCall = calls.length;
    await waitFor('전년 보고서');
    assert(!calls.slice(firstNewCall).some(call => call.path.endsWith('/versions') && call.path.includes(priorDocumentId)),
      'Document versions must not be fetched eagerly');
    const documentSelect = host.querySelector('#comparison-document');
    assert(documentSelect && ![...documentSelect.options].some(option => option.textContent.includes('다른 기업')),
      'Only documents for the current company may be offered');
    input(documentSelect, priorDocumentId);
    await waitFor('2024년');
    assert([...host.querySelectorAll('#prior-version option')].filter(option => option.value).length === 1,
      'Only ready versions from currentYear-1 may be offered');
    input(host.querySelector('#prior-version'), priorVersionId);
    await flush();
  };

  try {
    root.render(React.createElement(ComparisonWorkspace, props('tenant-a')));
    await selectPrior();
    assert(calls.filter(call => call.path === '/v1/documents').map(call => call.cursor).join(',') === ',documents-2',
      'Document pagination must follow each cursor exactly once');
    assert(calls.filter(call => call.path.endsWith('/versions')).map(call => call.cursor).join(',') === ',versions-2',
      'Selected document version pagination must follow each cursor exactly once');

    button('비교 실행').click();
    await waitFor('비교를 시작하지 못했습니다');
    button('비교 실행').click();
    await waitFor('삭제로 확정하지 않습니다');
    const creates = calls.filter(call => call.path === `/v1/runs/${runId}/comparisons`);
    assert(new Headers(creates[0].init.headers).get('Idempotency-Key') === new Headers(creates[1].init.headers).get('Idempotency-Key'),
      'Retrying the same body must retain its idempotency key');
    assert(new Headers(creates[1].init.headers).get('X-CSRF-Token') === 'csrf-test', 'Comparison create must send CSRF');
    assert(JSON.parse(String(creates[1].init.body)).prior_document_version_id === priorVersionId,
      'Comparison create must send only the explicit prior version');
    assert(calls.some(call => call.path === `/v1/comparisons/${comparisonId}`), 'Accepted jobs must use the fixed comparison GET');
    assert(!calls.some(call => call.path === '/v1/evil'), 'Server status_url must not replace the fixed GET contract');

    root.unmount(); root = createRoot(host); mode = 'late-load'; lateLoadHeld = false; lateSignal = null;
    root.render(React.createElement(ComparisonWorkspace, props('tenant-load-a')));
    await flush();
    root.render(React.createElement(ComparisonWorkspace, props('tenant-load-a', {
      runId: 'dddddddd-dddd-4ddd-8ddd-dddddddddddd' })));
    await flush(); releaseLate(); await flush();
    assert(lateSignal.aborted, 'Run replacement must abort an old catalog load');

    root.unmount(); root = createRoot(host); mode = 'normal';
    root.render(React.createElement(ComparisonWorkspace, props('tenant-create-a')));
    await selectPrior(); mode = 'late-create'; lateSignal = null;
    button('비교 실행').click(); await flush();
    root.render(React.createElement(ComparisonWorkspace, props('tenant-create-b')));
    await flush(); releaseLate(); await flush();
    assert(lateSignal.aborted, 'Tenant replacement must abort comparison creation');
    assert(!host.textContent.includes('삭제로 확정하지 않습니다'), 'Late create responses must not restore comparison state');

    root.unmount(); root = createRoot(host); mode = 'normal'; createCalls = 1;
    root.render(React.createElement(ComparisonWorkspace, props('tenant-get-a')));
    await selectPrior(); mode = 'late-get'; lateSignal = null;
    button('비교 실행').click();
    for (let attempt = 0; attempt < 40 && !lateSignal; attempt += 1) await flush();
    assert(lateSignal, 'Accepted comparison must start a fixed GET');
    root.render(React.createElement(ComparisonWorkspace, props('tenant-get-a', { csrfToken: 'csrf-new' })));
    await flush(); releaseLate(); await flush();
    assert(lateSignal.aborted, 'Session replacement must abort comparison GET');
    assert(!host.textContent.includes('삭제로 확정하지 않습니다'), 'Late GET responses must not restore comparison results');

    root.unmount(); root = createRoot(host); mode = 'normal';
    root.render(React.createElement(ComparisonWorkspace, props('tenant-disabled', { canCompare: false })));
    await selectPrior();
    await waitFor('현재 권한으로는 비교를 실행할 수 없습니다');
    assert(button('비교 실행')?.disabled, 'Disabled comparison policy must disable creation');
    assert(!host.textContent.includes('YEAR_COMPARISON_DISABLED'), 'Product UI must not expose the role gate as a backend code');

    root.unmount(); root = createRoot(host); mode = 'normal';
    root.render(React.createElement(ComparisonWorkspace, props('tenant-environment')));
    await selectPrior(); mode = 'environment-disabled';
    button('비교 실행').click();
    await waitFor('이 환경에서는 다년도 비교가 활성화되지 않았습니다');
    assert(!host.textContent.includes('YEAR_COMPARISON_DISABLED'), 'Product UI must not expose the environment error code');

    root.unmount(); root = createRoot(host); mode = 'normal';
    root.render(React.createElement(ComparisonWorkspace, props('tenant-session')));
    await selectPrior(); mode = 'session';
    button('비교 실행').click();
    await waitFor('세션이 만료');
    assert(sessionInvalid === 1, 'Session failures must notify the parent once');
    assert(!host.textContent.includes('전년 보고서'), 'Session failures must clear private catalog state');

    root.unmount(); root = createRoot(host); mode = 'missing-artifact';
    root.render(React.createElement(ComparisonWorkspace, props('tenant-missing')));
    await waitFor('현재 실행의 원본 문서 버전을 찾을 수 없습니다');
    assert(!host.textContent.includes('전년 문서가 없어'), 'A missing current artifact must not be described as a missing prior');
    assert(sessionInvalid === 1, 'Non-session failures must not invalidate the session');

    root.unmount(); root = createRoot(host); mode = 'repeat-cursor';
    root.render(React.createElement(ComparisonWorkspace, props('tenant-repeat')));
    await waitFor('문서 목록 페이지를 계속 불러올 수 없습니다');
    assert(calls.filter(call => call.path === '/v1/documents' && call.mode === 'repeat-cursor').length === 2,
      'Repeated catalog cursors must stop pagination');
    return { controlled_http_boundary: true, lazy_versions: true, same_company: true,
      prior_year_ready_only: true, bounded_pagination: true, stable_idempotency: true,
      csrf: true, fixed_comparison_get: true, completed_candidate: true,
      late_load_ignored: true, late_create_ignored: true, late_get_ignored: true,
      role_gate: true, environment_disabled: true, session_cleared: true, missing_artifact_truthful: true,
      repeated_cursor_guard: true };
  } finally {
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
  }
}
