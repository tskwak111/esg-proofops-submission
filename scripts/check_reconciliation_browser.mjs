// Real composed HTTP/browser check. Run only against the test-only local_browser_server.
// Install the browser driver outside production dependencies and set PROOFOPS_PLAYWRIGHT_MODULE.
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PROOFOPS_PLAYWRIGHT_MODULE || "playwright");
const base = process.argv[2] || "http://localhost:4193";
const output = path.resolve(process.argv[3] || ".local/reconciliation-browser");
if (!/^https?:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/.test(base)) {
  throw new Error("This synthetic harness must only target localhost");
}
if (fs.existsSync(output)) throw new Error("Evidence output already exists");
fs.mkdirSync(output, { recursive: true });
const browser = await chromium.launch({
  headless: true,
  ...(process.env.PROOFOPS_BROWSER_CHANNEL ? { channel: process.env.PROOFOPS_BROWSER_CHANNEL } : {}),
});
const assert = (condition, message) => { if (!condition) throw new Error(message); };
try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const pageErrors = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.goto(`${base}/__e2e/login`);
  const fixture = await page.evaluate(async () => (await fetch("/__e2e/review-fixture")).json());
  assert(fixture.local_synthetic && fixture.reconciliation_case_ids.length === 4, "Four synthetic cases required");
  const workspace = `/runs/${fixture.run_id}/claims/${fixture.claim_id}/reconciliation`;
  await page.goto(base + workspace);
  await page.locator("#case-select").waitFor();
  const cases = await page.evaluate(async ({ run_id, claim_id }) => {
    return (await (await fetch(`/v1/runs/${run_id}/claims/${claim_id}/reconciliation`)).json()).items;
  }, fixture);
  const c1 = cases.find(item => item.item === "C1");
  await page.locator("#case-select").selectOption(c1.case_id);
  await page.locator(".evaluate-btn").click();
  await page.getByText("대조 재평가가 완료되었습니다.", { exact: false }).waitFor();
  assert((await page.locator(".result-card").innerText()).includes("차단됨"), "Pending evaluation must block");
  const initialSnapshot = await page.evaluate(async caseId => {
    const detail = await (await fetch(`/v1/reconciliation/cases/${caseId}`)).json();
    const url = `/v1/reconciliation/cases/${caseId}/revisions/${detail.latest_result.revision}`;
    return { url, text: await (await fetch(url)).text() };
  }, c1.case_id);
  const review = page.locator(".review-section");
  await review.locator('input[type="checkbox"]').nth(0).check();
  await review.locator('input[type="checkbox"]').nth(1).check();
  await page.locator("#review-reason").fill("Synthetic browser verification of exact stored facts");
  await page.locator(".review-submit-btn").click();
  await page.getByText("검토 확인이 성공적으로 기록되었습니다.", { exact: false }).waitFor();
  await page.locator(".stale-result-notice").waitFor();
  await page.locator('.policy-admin-section input[type="checkbox"]').check();
  await page.locator("#policy-reason").fill("Synthetic fixture only; no live accounting approval");
  await page.locator(".policy-submit-btn").click();
  await page.getByText("정책 승인 상태가 업데이트되었습니다.", { exact: false }).waitFor();
  await page.locator(".evaluate-btn").click();
  await page.getByText("대조 재평가가 완료되었습니다.", { exact: false }).waitFor();
  assert((await page.locator(".result-card").innerText()).includes("대조 또는 차이 설명 확인"), "Reviewed C1 must complete");

  const verification = await page.evaluate(async ({ cases, initialSnapshot }) => {
    const session = await (await fetch("/v1/session")).json();
    const mutate = async (detail, operation, body, key = crypto.randomUUID()) => {
      const response = await fetch(`/v1/reconciliation/cases/${detail.case_id}/${operation}`, {
        method: "POST", headers: { "Content-Type": "application/json",
          "X-CSRF-Token": session.csrf_token, "If-Match": `"${detail.revision}"`, "Idempotency-Key": key },
        body: JSON.stringify(body),
      });
      if (!response.ok) throw new Error(`${operation}: HTTP ${response.status}`);
      return response.json();
    };
    const outcomes = {};
    for (const original of cases) {
      let detail = await (await fetch(`/v1/reconciliation/cases/${original.case_id}`)).json();
      if (original.item !== "C1") {
        detail = await mutate(detail, "review", { reason: "Synthetic full source review",
          confirm_source_bindings: true, confirm_decision_bindings: true,
          confirm_search_coverage: detail.packet.search.state === "complete" });
        // The shared verified claim is performance-track. C3 exercises the
        // unapproved-policy gate here; goal-track C3 rules have domain tests.
        if (original.item !== "C3") {
          detail = await mutate(detail, "policy-approval", {
            approved: true, reason: "Synthetic policy workflow test only",
          });
        }
        const key = crypto.randomUUID();
        const evaluated = await mutate(detail, "evaluate", {}, key);
        const replay = await mutate(detail, "evaluate", {}, key);
        if (JSON.stringify(evaluated) !== JSON.stringify(replay)) throw new Error("Idempotency replay changed");
        detail = evaluated;
      }
      outcomes[detail.item] = {
        execution_state: detail.latest_result.result.execution_state,
        status: detail.latest_result.result.status,
        reason_codes: detail.latest_result.result.reason_codes,
      };
      const source = detail.sources[0];
      const response = await fetch(`/v1/reconciliation/cases/${detail.case_id}/sources/${encodeURIComponent(source.source_id)}/content`);
      if (!response.ok || !response.headers.get("content-disposition")?.startsWith("attachment")) {
        throw new Error("Source is not an authorized attachment");
      }
      const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", await response.arrayBuffer()))]
        .map(byte => byte.toString(16).padStart(2, "0")).join("");
      if (hash !== source.artifact_sha256) throw new Error("Downloaded original hash mismatch");
    }
    if (await (await fetch(initialSnapshot.url)).text() !== initialSnapshot.text) throw new Error("Old revision changed");
    return { outcomes, source_download_hashes: true, immutable_old_revision: true, idempotency_replay: true,
      changed_review_requires_reevaluation_notice: true };
  }, { cases, initialSnapshot });
  for (const item of ["C1", "C2", "C4"]) {
    assert(verification.outcomes[item].execution_state === "completed", `${item} did not complete`);
  }
  assert(verification.outcomes.C3.execution_state === "blocked", "Unresolved C3 policy must remain blocked");
  assert(verification.outcomes.C3.reason_codes.includes("policy_unapproved"), "C3 approval gate was bypassed");
  await page.screenshot({ path: path.join(output, "workspace.png"), fullPage: true });
  assert(pageErrors.length === 0, `Browser errors: ${pageErrors.join("; ")}`);
  const report = { status: "passed", synthetic: true, boundary: "real composed local HTTP and SQLite",
    workspace, ...verification, page_errors: pageErrors };
  fs.writeFileSync(path.join(output, "result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} catch (error) {
  fs.writeFileSync(path.join(output, "failure.txt"), String(error));
  throw error;
} finally {
  await browser.close();
}
