// Run in a Vite browser: await import('/@fs/<repo>/tests/e2e/run_progress_check.mjs').then(m => m.check()).
export async function check() {
  const reactResource = performance.getEntriesByType('resource').map(entry => entry.name)
    .find(name => /\/react\.js\?v=/.test(name));
  const version = reactResource ? new URL(reactResource).search : '';
  const { default: React } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react.js' + version);
  const { default: { createRoot } } = await import(/* @vite-ignore */ '/node_modules/.vite/deps/react-dom_client.js' + version);
  const { RunProgress } = await import('/src/features/runs/RunProgress.tsx');
  const originalFetch = window.fetch;
  const host = document.createElement('div');
  document.body.append(host);
  const root = createRoot(host);
  const flush = () => new Promise(resolve => setTimeout(resolve, 30));
  const assert = (ok, message) => { if (!ok) throw new Error(message); };
  const coverage = { pages_total: 1, pages_processed: 1, pages_unprocessed: 0,
    pages_unreadable: 0, chunks_discovered: 0, chunks_processed: 0,
    claims_discovered: 0, claims_decided: 0,
    claims_needs_review: 0, complete: false, full_scope: true };
  const run = (id, status, revision) => ({ run_id: id, document_version_id: id,
    status, current_stage: 'tag', revision, mutation_epoch: revision, coverage,
    rule_pack_sha256: '0'.repeat(64), created_at: '2026-09-09T00:00:00Z' });
  let reads = 0, release, actionSignal;
  let current = run('first', 'failed', 1);
  const updates = [];
  const props = { csrfToken: 'test-only', tenantKey: 'tenant-a',
    initialRun: current, onSessionInvalid: () => {}, onRunChanged: next => updates.push(next) };
  const render = next => root.render(React.createElement(RunProgress, next));
  const click = async () => {
    const textarea = host.querySelector('textarea');
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(textarea, 'local regression reason');
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    await flush();
    host.querySelector('button').click();
    await flush();
  };
  window.fetch = async (url, init = {}) => {
    if (init.method === 'POST') {
      actionSignal = init.signal;
      return new Promise(resolve => { release = next => resolve(Response.json(next)); });
    }
    if (String(url).endsWith('/cost')) return Response.json({ input_tokens: 0,
      output_tokens: 0, attempt_count: 0, cache_hit_count: 0, amount: null,
      currency: 'USD', cost_status: 'unknown_cost' });
    reads++;
    return Response.json(current);
  };
  try {
    render(props);
    await flush();
    await click();
    current = run('first', 'running', 2);
    release(current);
    await flush();
    assert(reads >= 2, 'Successful retry must restart polling');
    assert(updates.some(next => next.run_id === 'first' && next.mutation_epoch === 2),
      'Successful retry must notify the current summary to refresh');
    await click();
    const pendingSignal = actionSignal;
    current = run('second', 'queued', 1);
    render({ ...props, tenantKey: 'tenant-b', initialRun: current });
    await flush();
    release(run('first', 'cancelled', 99));
    await flush();
    assert(pendingSignal?.aborted, 'Tenant/run replacement must abort pending action');
    assert(host.querySelector('[role="status"]').textContent.includes('대기 중'),
      'Old action must not overwrite new tenant/run');
    assert(!updates.some(next => next.mutation_epoch === 99), 'Old action must not notify the new summary');
    assert(!host.textContent.includes('NaN'), 'Coverage uses the fixed DTO field names');
    return { retry_poll_resumed: true, stale_action_ignored: true, summary_refresh: true };
  } finally {
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
  }
}
