// Run: pnpm --filter proofops-web test:hosted  (no extra deps: vite SSR loader + react-dom/server)
import assert from "node:assert/strict";
import { createServer } from "vite";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

const server = await createServer({ server: { middlewareMode: true }, appType: "custom", optimizeDeps: { noDiscovery: true, include: [] }, logLevel: "error", envDir: false, ssr: { noExternal: ["react-router"] } });
const { MemoryRouter } = await server.ssrLoadModule("react-router");
const c = await server.ssrLoadModule("/src/features/hosted/hostedClient.ts");
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
const run = await client.createRun("d1", [1, 2]);
assert.equal(run.status, "queued");
assert.deepEqual(JSON.parse(calls[2].body), { document_id: "d1", selected_pages: [1, 2] });
assert.equal(calls[2].headers["X-CSRF-Token"], "CSRF1");

// error mapping
for (const [status, code, retry, expect] of [
  [401, "AUTH_REQUIRED", null, /로그인/], [403, "CSRF_INVALID", null, /검증/], [403, "FORBIDDEN", null, /권한/],
  [404, "RESOURCE_NOT_FOUND", null, /찾을 수 없/], [409, "SOURCE_EXPIRED", null, /7일/], [413, "PAYLOAD_TOO_LARGE", null, /한도/],
  [429, "QUEUE_FULL", 42, /42초/], [429, "DAILY_RUN_LIMIT", null, /오늘/], [503, "ANALYSIS_DISABLED", null, /중지/], [503, "X", null, /일시적/],
  [422, "PAGE_SELECTION_INVALID", null, /최대 2쪽/], [0, "NETWORK", null, /연결/],
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

await server.close();
console.log("hosted tests passed");
