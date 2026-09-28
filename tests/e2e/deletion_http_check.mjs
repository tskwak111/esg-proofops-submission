// Test-only synthetic harness; run after export_http_check on the same report page.
export async function checkDeletionHttp() {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const fixture = await (await fetch('/__e2e/review-fixture')).json();
  assert(fixture.local_synthetic === true, 'Only the explicit synthetic fixture may be deleted');
  const session = await (await fetch('/v1/session')).json();
  const version = await (await fetch(`/v1/versions/${fixture.document_version_id}`)).json();
  assert(version.document_id, 'Fixture document must exist before deletion');
  const exportId = new URL(location.href).searchParams.get('export_id');
  assert(exportId, 'Create a real export first');
  const headers = { 'Content-Type': 'application/json', 'X-CSRF-Token': session.csrf_token };
  const ticketResponse = await fetch(`/v1/exports/${exportId}/download`, { method: 'POST', headers });
  assert(ticketResponse.ok, 'Private ticket must exist before deletion');
  const ticket = await ticketResponse.json();
  const response = await fetch(`/v1/documents/${version.document_id}/deletion-requests`, {
    method: 'POST', headers: { ...headers, 'Idempotency-Key': crypto.randomUUID() },
    body: JSON.stringify({ reason: 'Delete only the generated browser verification fixture' }),
  });
  const request = await response.json();
  assert(response.status === 202 && request.status === 'blocked_retention',
    'Missing approved retention policy must block physical deletion');
  for (const url of [
    `/v1/documents/${version.document_id}`,
    `/v1/versions/${fixture.document_version_id}`,
    `/v1/runs/${fixture.run_id}`,
    `/v1/exports/${exportId}`,
    ticket.url,
  ]) {
    assert((await fetch(url)).status === 404, 'Deleted fixture or preexisting ticket remained accessible');
  }
  const documents = await (await fetch('/v1/documents')).json();
  assert(documents.items.every(item => item.document_id !== version.document_id),
    'Deleted fixture must disappear from document listings');
  return { request_accepted: true, policy_blocked: true, document_hidden: true,
    run_hidden: true, export_hidden: true, existing_ticket_denied: true };
}
