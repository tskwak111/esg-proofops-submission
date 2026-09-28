// Run in the test-only same-origin harness after /__e2e/login, on /runs/:id/report.
export async function checkExportHttp() {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const waitFor = async (predicate, message) => {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error(message);
  };
  const button = text => [...document.querySelectorAll('button')]
    .find(element => element.textContent.trim() === text);
  await waitFor(() => button('내보내기 생성'), 'Report route did not mount');
  const partial = [...document.querySelectorAll('input[type=checkbox]')]
    .find(element => element.closest('label')?.textContent.includes('부분 결과 허용'));
  assert(partial && !partial.checked, 'Partial export must default to false');
  button('내보내기 생성').click();
  await waitFor(() => document.body.textContent.includes('미완료 항목이 있습니다'),
    'Unfinished reports must explain the finalization gate');
  partial.click();
  button('다시 시도').click();
  await waitFor(() => document.body.textContent.includes('준비됨'), 'Actual export did not become ready');
  const exportId = new URL(location.href).searchParams.get('export_id');
  assert(exportId, 'Created export ID must be restorable');
  const metadataResponse = await fetch(`/v1/exports/${exportId}`);
  const metadata = await metadataResponse.json();
  assert(metadataResponse.ok && metadata.state === 'ready' && metadata.partial === true,
    'API must persist ready partial snapshot');
  const issue = [...document.querySelectorAll('button')]
    .find(element => element.textContent.includes('다운로드'));
  assert(issue, 'Ready export must provide download issuance');
  issue.click();
  await waitFor(() => document.querySelector(`a[href*="/v1/exports/${exportId}/content"]`),
    'Private link was not issued');
  const link = document.querySelector(`a[href*="/v1/exports/${exportId}/content"]`);
  assert(new URL(link.href).origin === location.origin, 'Download must use the API origin');
  const response = await fetch(link.href);
  const bytes = await response.arrayBuffer();
  assert(response.ok && response.headers.get('content-type').includes('application/zip'),
    'Actual download must return ZIP bytes');
  assert(response.headers.get('cache-control') === 'no-store'
    && response.headers.get('content-disposition').startsWith('attachment;'),
    'Private download must disable caching and use attachment disposition');
  const sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))]
    .map(value => value.toString(16).padStart(2, '0')).join('');
  const session = await (await fetch('/v1/session')).json();
  const ticketResponse = await fetch(`/v1/exports/${exportId}/download`, {
    method: 'POST', headers: { 'X-CSRF-Token': session.csrf_token },
  });
  const ticket = await ticketResponse.json();
  assert(ticketResponse.ok && ticket.sha256 === sha256, 'Downloaded ZIP must match the authorized SHA');
  const denied = await fetch(`/v1/exports/${exportId}/download`, { method: 'POST' });
  assert(denied.status === 403, 'Download issuance without CSRF must fail');
  return { route_mounted: true, finalization_gate: true, partial_ready: true,
    private_download: true, sha256_verified: true, csrf_denied: true,
    export_id: exportId, bytes: bytes.byteLength, sha256 };
}
