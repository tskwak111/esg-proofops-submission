// playwright-cli run-code --filename; intercepted fixtures only, no real authentication.
async page => {
  const base = new URL(page.url()).origin;
  const output = '/path/to/ESG_ProofOps/outputs/agent-results/R108-redesign/lab/fix/A1';
  let loggedIn = false, lock = true, loginCalls = 0, invitationCalls = 0;
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  await page.unrouteAll();
  await page.route('**/hosted-api/**', async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    let status = 200, body = {};
    const account = {user_id:'access-password:organizer',tenant_id:'fixture-tenant',role:'organizer',csrf_token:'',expires_at:'2026-10-20T14:59:00Z'};
    if (path.endsWith('/runtime')) body = {live_analysis:false,accept_uploads:false,accept_new_runs:false,limits:{max_upload_bytes:1048576},upload_notice:'합성 로그인 검증'};
    else if (path.endsWith('/access-password')) {
      loginCalls++;
      const payload = request.postDataJSON();
      if (payload.role !== 'organizer') throw new Error('Incorrect account selection');
      if (lock) {status=429;body={error:{code:'ACCESS_PASSWORD_LOCKED'}};}
      else {loggedIn=true;body=account;}
    } else if (path.endsWith('/invitation')) {invitationCalls++;loggedIn=true;body=account;}
    else if (path.endsWith('/session')) {status=loggedIn?200:401;body=loggedIn?account:{error:{code:'AUTH_REQUIRED'}};}
    else if (path.endsWith('/documents')) body={items:[],next_cursor:null};
    else if (path.endsWith('/runs')) body={items:[],next_cursor:null};
    else if (path.endsWith('/logout')) {loggedIn=false;status=204;}
    else throw new Error('Unexpected fixture API route');
    await route.fulfill({status,contentType:'application/json',headers:status===429?{'Retry-After':'900'}:{},body:status===204?'':JSON.stringify(body)});
  });
  await page.goto(`${base}/analyze`);
  const input = page.getByLabel('초대 코드 또는 접근 비밀번호', {exact:true});
  await input.waitFor();
  if (await input.getAttribute('type') !== 'password') throw new Error('Credential is visible');
  await page.screenshot({path:`${output}/login-fixture.png`,fullPage:true});
  await input.fill('fixture-only-password');
  await page.getByRole('button',{name:'입장',exact:true}).click();
  await page.getByRole('alert').filter({hasText:'900초'}).waitFor();
  if (await input.inputValue()) throw new Error('Credential retained after failure');
  await page.screenshot({path:`${output}/locked-fixture.png`,fullPage:true});
  lock=false;
  await input.fill('fixture-only-password');
  await page.getByRole('button',{name:'입장',exact:true}).click();
  await page.getByRole('region',{name:'이전 보고서 내역',exact:true}).waitFor();
  if (await input.count()) throw new Error('Credential field remained after login');
  await page.screenshot({path:`${output}/organizer-fixture.png`,fullPage:true});
  await page.getByRole('button',{name:'로그아웃',exact:true}).click();
  await input.waitFor();
  await page.getByRole('combobox', {name:'로그인 방식',exact:true}).selectOption('invitation');
  await input.fill('fixture-invitation-code-01234567890123456789');
  await page.getByRole('button',{name:'입장',exact:true}).click();
  await page.getByRole('region',{name:'이전 보고서 내역',exact:true}).waitFor();
  if (failures.length || loginCalls!==2 || invitationCalls!==1) throw new Error('Login fixture failed');
  return {status:'passed',password_attempts:loginCalls,invitation_attempts:invitationCalls,lock_notice:true,credential_cleared:true,actual_requests:0};
}
