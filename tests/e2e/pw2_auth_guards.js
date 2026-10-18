async page => {
  const base=page.url().startsWith('https://esg-proofops-') ? new URL(page.url()).origin : 'http://127.0.0.1:5237';
  await page.route('**/hosted-api/v1/documents?**', route => route.fulfill({status:403,contentType:'application/json',body:JSON.stringify({error:{code:'FORBIDDEN'}})}));
  await page.route('**/hosted-api/v1/runs?**', route => route.fulfill({status:405,contentType:'application/json',body:'{}'}));
  await page.goto(`${base}/analyze`); await page.reload();
  const inventory=page.getByRole('region',{name:'이전 보고서 내역',exact:true});
  await inventory.getByRole('alert').waitFor();
  if ((await inventory.innerText()).includes('서버는 목록 조회를 지원하지 않습니다')) throw new Error('Denied permission became missing API fallback');
  if (await page.getByLabel('초대 코드 또는 접근 비밀번호',{exact:true}).count()) throw new Error('403 became login');
  await page.route('**/hosted-api/v1/documents?**', route => route.fulfill({status:401,contentType:'application/json',body:JSON.stringify({error:{code:'AUTH_REQUIRED'}})}));
  await page.route('**/hosted-api/v1/runs?**', route => route.fulfill({status:503,contentType:'application/json',body:'{}'}));
  await page.reload();
  await page.getByLabel('초대 코드 또는 접근 비밀번호',{exact:true}).waitFor();
  if (await page.getByRole('region',{name:'이전 보고서 내역',exact:true}).count()) throw new Error('Expired inventory remains visible');
  return {status:'passed',simulated_collection403_not_fallback:true,simulated_collection401_expires_session_during_other503:true,actual_mutations:0};
}
