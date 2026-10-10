// Run: pnpm --filter proofops-web test:hosted  (no extra deps: vite SSR loader + react-dom/server)
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

const server = await createServer({ server: { middlewareMode: true }, appType: "custom", optimizeDeps: { noDiscovery: true, include: [] }, logLevel: "error", envDir: false, ssr: { noExternal: ["react-router"] } });
const { MemoryRouter } = await server.ssrLoadModule("react-router");
const c = await server.ssrLoadModule("/src/features/hosted/hostedClient.ts");
const m = await server.ssrLoadModule("/src/features/hosted/resultModel.ts");
const rv = await server.ssrLoadModule("/src/features/hosted/HostedResult.tsx");
const v = await server.ssrLoadModule("/src/features/hosted/HostedAnalysis.tsx");
const json = (status, body, headers = {}) => new Response(body === undefined ? null : JSON.stringify(body), { status, headers });

// flag resolution
assert.equal(c.resolveBackend(undefined), "legacy");
assert.equal(c.resolveBackend("hosted"), "hosted");
assert.equal(c.resolveBackend("static"), "static");
assert.equal(c.resolveBackend("bogus"), "legacy");

// login stores csrf; writes send X-CSRF-Token + Idempotency-Key; reads do not
const calls = [];
const fetchImpl = async (url, init) => {
  calls.push({ url, ...init });
  if (url.endsWith("/v1/auth/invitation")) return json(200, { user_id: "u", tenant_id: "t", role: "admin", csrf_token: "CSRF1", expires_at: 1 });
  if (url.endsWith("/v1/documents")) return json(201, { document_id: "d1", page_count: 5, size_bytes: 3, expires: 9, sha256: "x" });
  if (url.endsWith("/v1/runs")) return json(202, { run_id: "r1", document_id: "d1", status: "queued", selected_pages: [1, 2], status_url: "/v1/runs/r1", error_code: null });
  return json(404, { error: { code: "RESOURCE_NOT_FOUND" } });
};
const client = c.createHostedClient({ fetchImpl });
await client.login(" code ");
assert.equal(JSON.parse(calls[0].body).code, "code");
assert.equal(calls[0].url, "/hosted-api/v1/auth/invitation");
assert.equal(calls[0].credentials, "same-origin");
await client.uploadDocument(new Blob(["%PDF"]));
assert.equal(calls[1].headers["Content-Type"], "application/pdf");
assert.equal(calls[1].headers["X-CSRF-Token"], "CSRF1");
assert.match(calls[1].headers["Idempotency-Key"], /^pk-[0-9a-f]{32}$/);
const scope = { revision: 2, plan: { status: "ready", selected_claim_pages: [1], selected_evidence_pages: [1, 2], failure_reasons: [], unreadable_pages: [], unknown_pages: [], conflict_pages: [], pages: [] }, selection: { pages: [1, 2], claim_pages: [1], selection_sha256: "scope-hash" } };
const run = await client.createRun("d1", scope, "pk-run", "finals-unattended-v2");
assert.equal(run.status, "queued");
assert.deepEqual(JSON.parse(calls[2].body), { document_id: "d1", scope_selection_sha256: "scope-hash" });
assert.equal(calls[2].headers["X-CSRF-Token"], "CSRF1");
assert.equal(calls[2].headers["X-Pipeline-Profile"], "finals-unattended-v2");
const scopeCalls = [];
let stale = false;
const scopeClient = c.createHostedClient({ fetchImpl: async (url, init) => {
  scopeCalls.push({ url, ...init });
  if (stale) return json(412, { error: { code: "SCOPE_CONFLICT" } });
  return json(200, scope, { ETag: '"2"' });
} });
assert.equal((await scopeClient.scopePlan("d1")).revision, 2);
assert.equal(scopeCalls.at(-1).method, "POST");
await scopeClient.getScopePlan("d1");
assert.equal(scopeCalls.at(-1).method, "GET");
await scopeClient.editScope("d1", scope, [1], [1, 2]);
assert.equal(scopeCalls.at(-1).headers["If-Match"], '"2"');
assert.deepEqual(JSON.parse(scopeCalls.at(-1).body), { selected_claim_pages: [1], selected_evidence_pages: [1, 2] });
stale = true;
await assert.rejects(scopeClient.editScope("d1", scope, [1], [1, 2]), e => e.status === 412 && e.code === "SCOPE_CONFLICT" && /최신 계획/.test(e.userMessage));
stale = false;
assert.equal((await scopeClient.getScopePlan("d1")).revision, 2);
assert.deepEqual(c.parsePageSelection("1,2,3,4,5,6,7,8,9,10", 300, 10), [1,2,3,4,5,6,7,8,9,10]);
assert.throws(() => c.parsePageSelection("1,2,3,4,5,6,7,8,9,10,11", 300, 10), /최대 10쪽/);

// error mapping
for (const [status, code, retry, expect] of [
  [401, "AUTH_REQUIRED", null, /로그인/], [403, "CSRF_INVALID", null, /검증/], [403, "FORBIDDEN", null, /권한/],
  [404, "RESOURCE_NOT_FOUND", null, /찾을 수 없/], [409, "SOURCE_EXPIRED", null, /7일/], [413, "PAYLOAD_TOO_LARGE", null, /한도/],
  [429, "QUEUE_FULL", 42, /42초/], [429, "DAILY_RUN_LIMIT", null, /오늘/], [503, "ANALYSIS_DISABLED", null, /중지/], [503, "X", null, /일시적/],
  [422, "PAGE_SELECTION_INVALID", null, /주장 최대 10쪽/], [0, "NETWORK", null, /연결/],
]) {
  const err = new c.HostedApiError(status, code, retry, "/x");
  assert.match(err.userMessage, expect, `${status} ${code}`);
}
const failing = c.createHostedClient({ fetchImpl: async () => json(429, { error: { code: "QUEUE_FULL" } }, { "Retry-After": "60" }) });
await assert.rejects(failing.runtime(), e => e.status === 429 && e.retryAfter === 60);
const down = c.createHostedClient({ fetchImpl: async () => { throw new TypeError("net"); } });
await assert.rejects(down.runtime(), e => e.status === 0);

// polling: backoff grows, 429 Retry-After honoured, stops at terminal
const sleeps = []; let n = 0;
const poller = c.createHostedClient({
  sleep: async ms => { sleeps.push(ms); },
  fetchImpl: async () => {
    n++;
    if (n === 1) return json(429, { error: { code: "RATE_LIMITED" } }, { "Retry-After": "7" });
    const status = n < 4 ? "running" : "partial_blocked";
    return json(200, { ...run, status, error_code: status === "partial_blocked" ? "LIVE_BINDING_UNAVAILABLE" : null });
  },
});
const seen = [];
const final = await poller.pollRun(run, r => seen.push(r.status));
assert.equal(final.status, "partial_blocked");
assert.deepEqual(sleeps, [1000, 7000, 10000, 10000]); // 429 Retry-After then capped backoff
assert.deepEqual(seen, ["queued", "running", "running", "partial_blocked"]);

// outcome semantics: partial_blocked is 보류, never success
const blocked = c.runOutcome({ ...run, status: "partial_blocked", error_code: "LIVE_BINDING_UNAVAILABLE" });
assert.equal(blocked.kind, "blocked"); assert.equal(blocked.title, "보류");
assert.ok(blocked.reasons.some(r => r.includes("근거 없음")));
assert.notEqual(c.runOutcome({ ...run, status: "completed" }).kind, "blocked");
assert.ok(c.runOutcome({ ...run, status: "completed" }).reasons.length, "completed without result must not look like a verified result");

// P7 provider quota failures render the exact Korean message and stop polling.
for (const code of ["OPENROUTER_HTTP_402", "OPENROUTER_HTTP_403", "OPENAI_HTTP_401", "OPENAI_HTTP_429", "OPENAI_INSUFFICIENT_QUOTA"]) {
  const rejected = { ...run, status: "failed", error_code: code };
  assert.deepEqual(c.runOutcome(rejected).reasons, ["분석 서비스 한도 초과 — 운영자 확인 필요"]);
  let quotaReads = 0;
  const quotaPoller = c.createHostedClient({
    sleep: async () => {},
    fetchImpl: async () => { quotaReads++; return json(200, rejected); },
  });
  assert.equal((await quotaPoller.pollRun(run, () => {})).status, "failed");
  assert.equal(quotaReads, 1);
  assert.match(renderToString(createElement(v.RunOutcomeView, { run: rejected })), /분석 서비스 한도 초과 — 운영자 확인 필요/);
}

// page selection
assert.deepEqual(c.parsePageSelection("3, 1", 5), [1, 3]);
assert.throws(() => c.parsePageSelection("1,2,3", 5), /최대 2쪽/);
assert.throws(() => c.parsePageSelection("9", 5), /1–5쪽/);
assert.throws(() => c.parsePageSelection("a", 5), /숫자/);

// rendering
const html = el => renderToString(createElement(MemoryRouter, null, el));
const runtimeOff = { accept_new_runs: false, accept_uploads: true, live_analysis: false, upload_notice: "원본 PDF 7일 보관", limits: { selected_pages: 2 } };
const off = html(createElement(v.RuntimeOff, { runtime: runtimeOff }));
assert.match(off, /현재 실시간 분석이 꺼져 있습니다/); assert.match(off, /href="\/demo"/);
const view = html(createElement(v.RunOutcomeView, { run: { ...run, status: "partial_blocked", error_code: "LIVE_BINDING_UNAVAILABLE" } }));
assert.match(view, /보류/); assert.match(view, /근거 없음/); assert.doesNotMatch(view, /분석 완료/);
assert.match(html(createElement(v.RunOutcomeView, { run: { ...run, queue_position: 3 } })), /대기 순번 3/);


// ---- result view / review / delete (synthetic strings only) ----
const ref = (id, page = 1, quote = "합성 인용 A") => ({ source_id: id, document_version_id: "v", parse_manifest_id: "p", page_num: page, printed_page_label: null, bbox: null, raw_text_sha256: "h", quote, char_start: 0, char_end: 5, location_quality: "located", verification_state: "verified" });
const el = (id, state, refs = []) => ({ element_id: id, state, evidence_refs: refs, normalized_value: null, credited_from: null, reason_code: null });
const claimA = { claim_id: "c1", page_num: 1, original_page_num: 22, quote: "합성 주장 문장 하나", track: "management", decision: { decision_status: "blocked_evidence", evidence_grade: null, label: null, review_status: "needs_review", grade_range: { floor: "E1", ceiling: "E3", open_elements: ["M3"] } }, revision: 1, source_refs: [ref("s1")], source_quality: "verified",
  elements: [el("M1", "present", [ref("s1")]), el("M3", "unknown")], hold_reason: "CONSENSUS_UNRESOLVED", possible_grade_range: { floor: "E1", ceiling: "E3", open_elements: ["M3"] }, confirmed_grade: null,
  missing_evidence: [{ element_id: "M3", candidate_refs: [ref("s2", 2, "합성 후보 인용")], search: { absence_admissible: false }, undetermined_reason: "linkage_unclear" }] };
const claimB = { ...claimA, claim_id: "c2", quote: "합성 확정 주장", source_refs: [ref("s3")], decision: { decision_status: "decided", evidence_grade: "E3", label: "SUBSTANTIATED", review_status: "human_confirmed", grade_range: null }, possible_grade_range: null, confirmed_grade: "E3", hold_reason: null, elements: [el("M1", "present", [ref("s3")])], missing_evidence: [] };
const claimC = { ...claimA, claim_id: "c3", track: null, source_quality: "unverified", hold_reason: "SOURCE_VALIDATION_REQUIRED", decision: null, possible_grade_range: null, elements: [], missing_evidence: [] };
const reviewA = { review_id: "rv1", run_id: "r1", claim_id: "c1", status: "open", revision: 3, base_tag_revision: 1, reason_codes: ["REVIEW:M3"] };
const result = { schema: "r108-hosted-result-v1", claims: [claimA, claimB, claimC], reviews: [reviewA], hold_reasons: ["R108_NEEDS_REVIEW"], page_map: [22, 23], pipeline: { status: "partial_blocked", reason: "R108_NEEDS_REVIEW", reporting_scope: { report_year: 2025, period_start: "2024-01-01", period_end: "2024-12-31" } } };

// unknown / range vs confirmed / hold reasons
const rHtml = html(createElement(rv.ResultView, { result }));
assert.match(rHtml, /합성 주장 문장 하나/); assert.match(rHtml, /p\.22/);
assert.match(rHtml, /원문 확인 필요/);
assert.match(rHtml, /R108_NEEDS_REVIEW|일부 주장이 사람 검토/); assert.match(rHtml, /일부 주장이 사람 검토를 기다리고/);
assert.match(rHtml, /분류 미합의/);
assert.match(rHtml, /mini-status good">E3 · SUBSTANTIATED/);
const aHtml = html(createElement(rv.ResultView, { result: { ...result, claims: [claimA], reviews: [] } }));
assert.match(aHtml, /확인되지 않음\(근거 없음 아님\)/);
assert.match(aHtml, /확정 등급 아님/); assert.match(aHtml, /가능 범위 E1–E3/); assert.match(aHtml, /범위는 등급이 아니며/);
assert.doesNotMatch(aHtml, /확정 등급 E/); assert.doesNotMatch(aHtml, /근거 없음\(검증된/);
assert.match(aHtml, /합성 주장 문장 하나/);
// confirmed + provenance after resolve
const bHtml = html(createElement(rv.ResultView, { result: { ...result, claims: [claimB], reviews: [] } }));
assert.match(bHtml, /확정 등급 E3 · SUBSTANTIATED/); assert.match(bHtml, /사람 확인/);
assert.equal(m.gradeSummary(claimC).headline, "확정 등급 아님");
assert.equal(m.elementStateText("unknown", "verified"), "확인되지 않음(근거 없음 아님)");
assert.equal(m.elementStateText("present", "unverified"), "원문 대조 필요");
// partial_blocked outcome shows Korean hold reasons from the inline result
const blockedWithResult = c.runOutcome({ ...run, status: "partial_blocked", error_code: "R108_NEEDS_REVIEW", result });
assert.ok(blockedWithResult.reasons.some(r => r.includes("사람 검토")) && blockedWithResult.reasons.some(r => r.includes("근거 없음’이 아닙니다")));
assert.match(c.holdReasonText("LINUX_SOURCE_READER_UNRESOLVED"), /원문 판독기/);
assert.match(c.holdReasonText("ZZZ"), /ZZZ/);
// run status: stage / progress / queue_position
const runningHtml = html(createElement(v.RunOutcomeView, { run: { ...run, status: "running", stage: "extract", progress: 0.4, queue_position: null } }));
assert.match(runningHtml, /주장 추출/); assert.match(runningHtml, /진행 40%/); assert.match(runningHtml, /<progress/);
assert.match(html(createElement(v.RunOutcomeView, { run: { ...run, queue_position: 2, stage: "queued", progress: 0 } })), /대기 순번 2/);
// review form: reviewer/admin only
const asReviewer = html(createElement(rv.ResultView, { result: { ...result, claims: [claimA] }, role: "reviewer", client, runId: "r1" }));
assert.match(asReviewer, /사람 검토/); assert.match(asReviewer, /검토 제출/); assert.match(asReviewer, /근거 없음 — 검색 범위가 검증되지 않아 서버가 거부할 수 있습니다/);
assert.match(html(createElement(rv.ResultView, { result: { ...result, claims: [claimA] }, role: "admin", client, runId: "r1" })), /검토 제출/);
const asViewer = html(createElement(rv.ResultView, { result: { ...result, claims: [claimA] }, role: "viewer", client, runId: "r1" }));
assert.doesNotMatch(asViewer, /검토 제출/); assert.match(asViewer, /reviewer 또는 admin 역할만 처리/);
// allowed choices: present disabled when no citation exists
const noRef = m.allowedChoices({ ...claimA, missing_evidence: [] }, el("M3", "unknown"));
assert.equal(noRef.find(x => x.state === "present").disabled, true);
assert.ok(noRef.some(x => x.state === "absent"));
assert.equal(m.allowedChoices(claimA, el("M3", "unknown")).find(x => x.state === "present").disabled, false);

// resolve body: unchanged elements echoed, changed present needs a chosen citation, no grade supplied
const body = m.buildResolveBody(claimA, reviewA, { M3: { state: "present", refKey: m.refKey(ref("s2", 2)) } }, " 합성 검토 사유 ");
assert.equal(body.base_tag_revision, 1); assert.equal(body.track, "management"); assert.equal(body.reason, "합성 검토 사유");
assert.equal(body.elements.length, 2); assert.equal(body.elements[0].state, "present"); assert.equal(body.elements[1].evidence_refs[0].source_id, "s2");
assert.ok(!("evidence_grade" in body) && !("grade" in body));
assert.throws(() => m.buildResolveBody(claimA, reviewA, { M3: { state: "present" } }, "사유 충분히"), /근거를 선택/);
assert.throws(() => m.buildResolveBody(claimA, reviewA, {}, "짧음"), /5자 이상/);
assert.throws(() => m.buildResolveBody(claimC, reviewA, {}, "사유 충분히"), /트랙/);

// resolve request: CSRF + quoted If-Match + Idempotency-Key; 409/412 conflict; absent rejection message
const rc = []; let mode = "ok";
const reviewClient = c.createHostedClient({ fetchImpl: async (url, init) => {
  rc.push({ url, ...init });
  if (url.endsWith("/v1/auth/invitation")) return json(200, { user_id: "u", tenant_id: "t", role: "reviewer", csrf_token: "CSRF9", expires_at: 1 });
  if (mode === "409") return json(409, { error: { code: "REVIEW_CONFLICT" } });
  if (mode === "412") return json(412, { error: { code: "UNKNOWN" } });
  if (mode === "absent") return json(422, { error: { code: "COVERAGE_OR_APPLICABILITY_REQUIRED" } });
  if (mode === "403") return json(403, { error: { code: "FORBIDDEN" } });
  return json(200, { review: { ...reviewA, status: "resolved", revision: 4 }, decision: claimB.decision, new_tag_revision: 2 });
} });
await reviewClient.login("x");
const resolved = await reviewClient.resolveReview("r1", "rv1", 3, body, "pk-fixed");
const last = rc.at(-1);
assert.equal(last.url, "/hosted-api/v1/runs/r1/reviews/rv1/resolve"); assert.equal(last.method, "POST");
assert.equal(last.headers["If-Match"], '"3"'); assert.equal(last.headers["X-CSRF-Token"], "CSRF9"); assert.equal(last.headers["Idempotency-Key"], "pk-fixed");
assert.equal(resolved.decision.evidence_grade, "E3");
assert.equal(m.decisionLine(resolved.decision), "E3 · SUBSTANTIATED"); assert.equal(m.reviewStatusText(resolved.decision.review_status), "사람 확인");
for (const status of ["409", "412"]) {
  mode = status;
  await assert.rejects(reviewClient.resolveReview("r1", "rv1", 3, body), e => e.isConflict === true && /새로고침/.test(e.userMessage), status);
}
mode = "absent";
await assert.rejects(reviewClient.resolveReview("r1", "rv1", 3, body), e => e.status === 422 && /검색 범위|커버리지/.test(e.userMessage) && /확인되지 않음/.test(e.userMessage));

// delete: uploader (editor) allowed by backend; non-owner gets 403 with a permission message; reason + Idempotency-Key sent
mode = "ok";
await reviewClient.deleteDocument("d1", "내 문서 삭제 요청");
const del = rc.at(-1);
assert.equal(del.url, "/hosted-api/v1/documents/d1/deletion-requests"); assert.deepEqual(JSON.parse(del.body), { reason: "내 문서 삭제 요청" });
assert.match(del.headers["Idempotency-Key"], /^pk-/); assert.equal(del.headers["X-CSRF-Token"], "CSRF9");
mode = "403";
await assert.rejects(reviewClient.deleteDocument("d1"), e => e.status === 403 && /권한/.test(e.userMessage));
const source = (await import("node:fs")).readFileSync(new URL("../src/features/hosted/HostedAnalysis.tsx", import.meta.url), "utf8");
assert.match(source, /본인이 올린 문서이거나 테넌트 관리자/); assert.doesNotMatch(source, /admin 역할/);
// result retrieval
mode = "res";
const resClient = c.createHostedClient({ fetchImpl: async url => json(200, url.endsWith("/result") ? result : {}) });
assert.equal((await resClient.getResult("/v1/runs/r1/result")).claims.length, 3);

assert.match(c.holdReasonText("PROCESSING_UNCERTAIN"), /처리 불확실/);
assert.match(c.holdReasonText("BUDGET_EXHAUSTED"), /예산 상한으로 미분석/);
assert.match(html(createElement(v.RunOutcomeView, { run: { ...run, status: "running", claims_extracted: 3, claims_processed: 1 } })).replace(/<!-- -->/g, ""), /추출된 주장 3개 · 태깅 처리 1개/);
await server.close();
console.log("hosted tests passed");
