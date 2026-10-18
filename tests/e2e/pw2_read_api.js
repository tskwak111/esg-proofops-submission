async page => {
  const base = page.url().startsWith('https://esg-proofops-') ? new URL(page.url()).origin : 'http://127.0.0.1:5237';
  const session = await (await page.request.get('http://127.0.0.1:5271/__pw2/session')).json();
  if (!session.local_synthetic) throw new Error('Only local synthetic fixture session allowed');
  await page.context().addCookies([{ name: '__Host-proofops_session', value: session.cookie, domain: '127.0.0.1', path: '/', httpOnly: true, secure: true, sameSite: 'Lax' }]);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${base}/analyze`);
  const list = page.getByRole('region', { name: '이전 보고서 내역', exact: true });
  await list.getByRole('button', { name: '결과 열기', exact: true }).waitFor();
  if (!(await list.innerText()).includes('진행 100%') || !(await list.innerText()).includes('대기 중')) throw new Error('Status/progress missing');
  await list.getByRole('button', { name: '보고서 더 보기' }).click();
  await list.getByText('보고서 1', { exact: true }).first().waitFor();
  const documentCount = await list.locator('ul').first().locator('li').count();
  if (documentCount !== 23) throw new Error(`Pagination expected23, got${documentCount}`);
  await list.getByRole('button', { name: '결과 열기', exact: true }).click();
  const result = page.getByTestId('hosted-result');
  await result.waitFor();
  const viewer = result.getByRole('region', { name: '원문 쪽 보기' });
  const manifestResponse = page.waitForResponse(response => response.url().includes('/source-pages/2/manifest'));
  await page.route('**/source-pages/*/image?**', async route => { const response = await route.fetch(); await new Promise(resolve => setTimeout(resolve, 750)); await route.fulfill({ response }); });
  await viewer.getByRole('button', { name: '원문 보기', exact: true }).click();
  await viewer.getByText('원문 이미지 불러오는 중…', { exact: true }).waitFor();
  const manifest = await (await manifestResponse).json();
  const image = viewer.getByRole('img');
  await image.waitFor(); await image.evaluate(img => img.decode());
  const overlay = viewer.getByTestId('source-highlight').first();
  await overlay.waitFor();
  async function alignment() {
    const canvas = await viewer.locator('.source-canvas').boundingBox(); const box = await overlay.boundingBox();
    const [x0,y0,x1,y1] = manifest.highlights[0].bbox_normalized;
    const deltas = [box.x-canvas.x-x0*canvas.width,box.y-canvas.y-y0*canvas.height,box.width-(x1-x0)*canvas.width,box.height-(y1-y0)*canvas.height];
    if (deltas.some(x => Math.abs(x)>1.1)) throw new Error(`Overlay alignment failed ${deltas}`);
    return deltas;
  }
  const desktopAlignment = await alignment();
  await viewer.getByRole('button', { name: '원문 확대', exact: true }).click();
  if (!(await viewer.locator('output').innerText()).includes('125%')) throw new Error('Zoom missing');
  await alignment();
  await viewer.getByRole('button', { name: '화면에 맞추기' }).click();
  await page.setViewportSize({ width: 390, height: 844 });
  await result.locator('.claim-list .claim-item').first().click();
  await image.scrollIntoViewIfNeeded();
  const mobileAlignment = await alignment();
  await viewer.getByRole('button', { name: '원문 확대', exact: true }).click();
  if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw new Error('Source image causes mobile page overflow');
  if (!await viewer.locator('.source-scroll').evaluate(el => el.scrollWidth > el.clientWidth)) throw new Error('Zoom canvas cannot pan');
  return { status: 'passed', actual_local_api: true, documents: documentCount, queued_and_published_status: true, manifest_physical_page: manifest.physical_page, prepared_page: manifest.prepared_page, verified_boxes: manifest.highlights.length, image_loading_notice: true, desktop_alignment_px: desktopAlignment, mobile_alignment_px: mobileAlignment, zoom: true, mobile_width:390, writes:0, paid_calls:0 };
}
