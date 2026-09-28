// Run in a Vite browser: await import('/@fs/<repo>/tests/e2e/export_workspace_check.mjs').then(m => m.check()).
export async function check() {
  const reactResource = performance.getEntriesByType('resource').map(entry => entry.name)
    .find(name => /\/react\.js\?v=/.test(name));
  const version = reactResource ? new URL(reactResource).search : '';
  const { default: React } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react.js' + version);
  const { default: { createRoot } } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react-dom_client.js' + version);
  const { ExportWorkspace } = await import('/src/features/reports/ExportWorkspace.tsx');
  const originalFetch = window.fetch;
  const originalPath = `${location.pathname}${location.search}`;
  const host = document.createElement('div');
  document.body.append(host);
  let root = createRoot(host);
  const flush = () => new Promise(resolve => setTimeout(resolve, 40));
  const waitFor = async text => {
    for (let attempt = 0; attempt < 80; attempt += 1) {
      if (host.textContent.includes(text)) return;
      await flush();
    }
    throw new Error(`Timed out waiting for: ${text}`);
  };
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const button = text => [...host.querySelectorAll('button')].find(item => item.textContent.includes(text));
  const runId = '11111111-1111-4111-8111-111111111111';
  const exportId = '22222222-2222-4222-8222-222222222222';
  const queryExportId = '33333333-3333-4333-8333-333333333333';
  const exportValue = (state, overrides = {}) => ({ export_id: exportId, run_id: runId,
    state, snapshot_epoch: 7, partial: false,
    manifest_sha256: state === 'ready' ? 'a'.repeat(64) : null,
    created_at: '2026-09-09T00:00:00Z', ...overrides });
  const createHeaders = [];
  const createBodies = [];
  const pollHeaders = [];
  const downloadHeaders = [];
  let createCalls = 0;
  let pollCalls = 0;
  let downloadCalls = 0;
  let lateSignal;
  let releaseLate;
  let holdKnown = false, knownSignal, releaseKnown;
  let sessionInvalid = 0;

  window.fetch = async (url, init = {}) => {
    const target = new URL(String(url), location.origin);
    const headers = new Headers(init.headers);
    if (target.pathname === `/v1/runs/${runId}/exports`) {
      createCalls += 1;
      createHeaders.push(headers);
      createBodies.push(JSON.parse(String(init.body)));
      if (createCalls <= 2) {
        return Response.json({ error: { code: 'DEPENDENCY_UNAVAILABLE', message: 'retry', retryable: true } }, { status: 503 });
      }
      if (createCalls === 3) return Response.json(exportValue('queued'), { status: 202 });
      lateSignal = init.signal;
      return new Promise(resolve => { releaseLate = () => resolve(Response.json(exportValue('ready', { snapshot_epoch: 99 }))); });
    }
    if (target.pathname === `/v1/exports/${exportId}`) {
      if (holdKnown) {
        knownSignal = init.signal;
        return new Promise(resolve => { releaseKnown = () => resolve(Response.json(exportValue('ready', { snapshot_epoch: 98 }))); });
      }
      pollHeaders.push(headers);
      pollCalls += 1;
      return Response.json(exportValue(pollCalls === 1 ? 'building' : 'ready'));
    }
    if (target.pathname === `/v1/exports/${exportId}/download`) {
      downloadHeaders.push(headers);
      downloadCalls += 1;
      return Response.json({ url: downloadCalls === 1 ? 'https://untrusted.example/export.zip' : `/private/export.zip?ticket=${downloadCalls}`,
        expires_at: downloadCalls === 2 ? '2020-01-01T00:00:00Z' : new Date(Date.now() + 600000).toISOString(),
        sha256: 'b'.repeat(64) });
    }
    if (target.pathname === `/v1/exports/${queryExportId}`) {
      return Response.json({ error: { code: 'SESSION_EXPIRED', message: 'expired', retryable: false } }, { status: 401 });
    }
    return Response.json({ error: { code: 'UNEXPECTED', message: target.pathname } }, { status: 500 });
  };

  const props = tenantKey => ({ apiBase: location.origin, csrfToken: 'csrf-test', tenantKey,
    runId, onSessionInvalid: () => { sessionInvalid += 1; } });
  try {
    history.replaceState({}, '', `/runs/${runId}/report`);
    root.render(React.createElement(ExportWorkspace, props('tenant-a')));
    await flush();
    const partial = host.querySelector('input[name="allow-partial"]');
    assert(partial && partial.checked, 'Review drafts default to partial export');
    assert(['json','csv','html'].every(format => host.querySelector(`input[value="${format}"]`)?.checked),
      'Review drafts default to all native formats');
    partial.click();
    await flush();
    assert(!partial.checked, 'Users can explicitly require complete results');
    assert(host.querySelector('input[value="json"]')?.checked, 'JSON must be the safe default format');
    assert(!button('다운로드'), 'No download control may exist before a ready export');

    button('내보내기 생성').click();
    await waitFor('다시 시도');
    button('다시 시도').click();
    await waitFor('다시 시도');
    assert(createHeaders[0].get('Idempotency-Key') === createHeaders[1].get('Idempotency-Key'),
      'The same failed body must retain its idempotency key');
    assert(createHeaders[0].get('X-CSRF-Token') === 'csrf-test', 'Create must send CSRF');
    assert(createBodies[0].allow_partial === false, 'Unchecking partial must explicitly request complete results');

    host.querySelector('input[value="html"]').click();
    await flush();
    button('다시 시도').click();
    await waitFor('준비됨');
    assert(createHeaders[2].get('Idempotency-Key') !== createHeaders[1].get('Idempotency-Key'),
      'Changing the request body must replace its idempotency key');
    assert(createBodies[2].formats.join(',') === 'json,csv', 'Selected native formats must reach the API');
    assert(pollCalls === 2, 'Accepted exports must poll until ready');
    assert(pollHeaders.every(value => !value.has('X-CSRF-Token')), 'GET polling must not leak CSRF');
    assert(host.textContent.includes('epoch 7'), 'History must show the snapshot epoch');
    assert(host.textContent.includes('a'.repeat(64)), 'History must show the manifest hash');
    assert(new URL(location.href).searchParams.get('export_id') === exportId, 'Created export must become the known URL export');

    button('다운로드 링크 발급').click();
    await waitFor('신뢰된 API origin');
    assert(!host.querySelector('a[href*="untrusted.example"]'), 'Untrusted download URLs must never be exposed or fetched');
    button('다운로드 링크 발급').click();
    await waitFor('만료된 링크 1회 재발급');
    assert(downloadHeaders[0].get('X-CSRF-Token') === 'csrf-test', 'Download issuance must send CSRF');
    button('만료된 링크 1회 재발급').click();
    await waitFor('비공개 다운로드 열기');
    const link = host.querySelector('a[href*="/private/export.zip"]');
    assert(link && new URL(link.href).origin === location.origin, 'Download URLs must stay on the trusted API origin');
    assert(downloadCalls === 3, 'Expired download may be explicitly reissued once');

    root.unmount();
    root = createRoot(host);
    root.render(React.createElement(ExportWorkspace, props('tenant-a')));
    await waitFor('URL로 다시 연 내보내기');
    assert(host.textContent.includes(exportId), 'A known export in the URL must refresh without a list endpoint');
    assert(host.textContent.includes('아직 이 화면에서 생성한 내보내기가 없습니다'),
      'A URL-refreshed export must not be invented as view-created history');

    history.replaceState({}, '', `/runs/${runId}/report`);
    button('내보내기 생성').click();
    await flush();
    assert(lateSignal, 'A replacement create request must be in flight');
    root.render(React.createElement(ExportWorkspace, props('tenant-b')));
    await flush();
    releaseLate();
    await flush();
    assert(lateSignal.aborted, 'Tenant replacement must abort the old create request');
    assert(!host.textContent.includes('epoch 99'), 'Late foreign-tenant export data must be ignored');
    assert(!host.textContent.includes(exportId), 'Tenant replacement must clear export history and private links');

    root.unmount();
    root = createRoot(host);
    holdKnown = true;
    history.replaceState({}, '', `/runs/${runId}/report?export_id=${exportId}`);
    root.render(React.createElement(ExportWorkspace, props('tenant-b')));
    await flush();
    assert(knownSignal, 'Known export restoration must start a GET');
    root.render(React.createElement(ExportWorkspace, props('tenant-c')));
    await flush();
    releaseKnown();
    await flush();
    assert(knownSignal.aborted, 'Tenant replacement must abort known-export restoration');
    assert(!host.textContent.includes('epoch 98'), 'Late known-export GET must not restore prior tenant data');
    holdKnown = false;

    root.unmount();
    root = createRoot(host);
    history.replaceState({}, '', `/runs/${runId}/report?export_id=${queryExportId}`);
    root.render(React.createElement(ExportWorkspace, props('tenant-c')));
    await waitFor('세션이 만료');
    assert(sessionInvalid === 1, 'Session failures must notify the parent once');
    assert(!host.textContent.includes(queryExportId), 'Session failures must clear known export state');
    assert(new URL(location.href).searchParams.get('export_id') === null, 'Session failures must clear the private export query');
    return { partial_default_true: true, complete_mode_opt_out: true, idempotency_retry: true, idempotency_body_reset: true,
      polling_terminal: true, ready_download_only: true, trusted_download_origin: true,
      reissue_once: true, known_url_refresh: true, tenant_cleared: true, known_tenant_cleared: true, session_cleared: true };
  } finally {
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
    history.replaceState({}, '', originalPath);
  }
}
