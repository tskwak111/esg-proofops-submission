// Real HTTP + mounted UI. Harness requires --include-prior-version and ENABLE_YEAR_COMPARISON=true.
export async function checkComparisonHttp() {
  const assert = (value, message) => { if (!value) throw new Error(message); };
  const waitFor = async (predicate, message) => {
    for (let attempt = 0; attempt < 100; attempt++) {
      if (predicate()) return;
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    throw new Error(message);
  };
  const fixture = await (await fetch('/__e2e/review-fixture')).json();
  assert(fixture.local_synthetic && fixture.prior_document_version_id, 'Prior synthetic fixture required');
  const version = await (await fetch(`/v1/versions/${fixture.document_version_id}`)).json();
  const originalFetch = window.fetch;
  let accepted;
  let compared;
  // Observe the real responses without substituting or changing them.
  window.fetch = async (input, init) => {
    const response = await originalFetch(input, init);
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    if (url.pathname === `/v1/runs/${fixture.run_id}/comparisons` && init?.method === 'POST') {
      accepted = { status: response.status, body: await response.clone().json() };
    }
    if (url.pathname.startsWith('/v1/comparisons/')) compared = await response.clone().json();
    return response;
  };
  try {
    await waitFor(() => document.querySelector('#comparison-document'), 'Comparison route did not load');
    const documents = document.querySelector('#comparison-document');
    assert([...documents.options].some(option => option.value === version.document_id),
      'Actual same-company document must be selectable');
    documents.value = version.document_id;
    documents.dispatchEvent(new Event('change', { bubbles: true }));
    await waitFor(() => document.querySelector('#prior-version'), 'Actual prior versions did not load');
    const versions = document.querySelector('#prior-version');
    assert([...versions.options].some(option => option.value === fixture.prior_document_version_id),
      'Uploaded ready previous-year version must be selectable');
    versions.value = fixture.prior_document_version_id;
    versions.dispatchEvent(new Event('change', { bubbles: true }));
    await waitFor(() => [...document.querySelectorAll('button')]
      .some(button => button.textContent === '비교 실행' && !button.disabled), 'Comparison action did not enable');
    [...document.querySelectorAll('button')].find(button => button.textContent === '비교 실행').click();
    await waitFor(() => document.body.textContent.includes('전년 문서의 승인된 비교 근거가 없어'),
      'Missing approved prior artifact must be explained in the actual UI');
    assert(accepted?.status === 202 && accepted.body.resource_id, 'API must persist a comparison receipt');
    assert(compared?.status === 'not_run' && compared.reason === 'prior_comparison_artifact_missing'
      && compared.changes.length === 0, 'Missing prior artifact must not invent changes');
    return { route_mounted: true, document_selected: true, prior_version_selected: true,
      actual_receipt: true, missing_artifact_not_run: true, comparison_id: accepted.body.resource_id };
  } finally {
    window.fetch = originalFetch;
  }
}
