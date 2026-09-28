import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";

const base = process.env.PROOFOPS_BASE_URL || "http://127.0.0.1:4173";
const chrome = process.env.PROOFOPS_CHROME_URL || "http://127.0.0.1:9229";
const shots = process.argv[2];
const data = await (await fetch(`${base}/demo/naver-2025.json`)).json();
const reviewId = data.claims.find(claim => claim.track)?.id;
const detailId = data.claims.find(claim => claim.decision.grade)?.id;
const confirmedCount = data.claims.filter(claim => claim.decision.grade).length;
const demoCount = data.claims.filter(claim => !claim.decision.grade && claim.decision.display_grade).length;
assert(reviewId && detailId);
const routes = [
  ["home", "/", ".hero"], ["analyze", "/analyze", ".analyze-main"],
  ["replay", "/analyze/replay", ".replay-page"], ["naver", "/demo", ".demo-main"],
  ["claim", `/demo/${detailId}#claims`, ".claim-detail"], ["kia", "/demo/kia", ".kia-case"],
  ["review", `/review/${reviewId}`, ".review-sim"],
  ["report-naver", "/report/naver", ".audit-page"],
  ["report-kia", "/report/kia", ".audit-page"],
  ["live", "/live", ".live-main"], ["404", "/missing-page", ".not-found"],
];
const targets = await (await fetch(`${chrome}/json`)).json();
const target = targets.find(item => item.type === "page");
assert(target, "Chrome page target required");
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
let id = 0;
const pending = new Map();
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (message.id && pending.has(message.id)) {
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    message.error ? reject(new Error(message.error.message)) : resolve(message.result);
  }
};
function call(method, params = {}) {
  return new Promise((resolve, reject) => {
    const key = ++id;
    pending.set(key, { resolve, reject });
    socket.send(JSON.stringify({ id: key, method, params }));
  });
}
async function evaluate(expression) {
  const reply = await call("Runtime.evaluate", { expression, returnByValue: true });
  if (reply.exceptionDetails) throw new Error(reply.exceptionDetails.text);
  return reply.result.value;
}
async function waitFor(expression) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${expression}`);
}

try {
  if (shots) await mkdir(shots, { recursive: true });
  await call("Page.enable");
  for (const [size, width, height] of [["desktop", 1400, 900], ["mobile", 390, 844]]) {
    await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: size === "mobile" });
    for (const [name, path, selector] of routes) {
      await call("Page.navigate", { url: `${base}${path}` });
      await waitFor(`location.pathname === ${JSON.stringify(path.split("#")[0])} && !!document.querySelector(${JSON.stringify(selector)})`);
      await new Promise(resolve => setTimeout(resolve, 350));
      const state = await evaluate(`({text: document.body.innerText, overflow: document.documentElement.scrollWidth > innerWidth})`);
      assert(!state.text.includes("불러오지 못했습니다"), `${path}: loading error`);
      assert(!state.overflow, `${path}: ${width}px horizontal overflow`);
      assert(state.text.length > 80, `${path}: empty page`);
      if (shots) {
        const shot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
        await writeFile(`${shots}/${name}-${size}.png`, Buffer.from(shot.data, "base64"));
      }
      console.log(`${size} ${path}: ok`);
    }
  }
  await call("Page.navigate", { url: `${base}/demo` });
  await waitFor('document.querySelectorAll(".queue-tabs button").length === 4');
  assert.equal(await evaluate('Number(document.querySelector(".queue-tabs button span").textContent)'), confirmedCount, "confirmed queue count");
  await evaluate('document.querySelectorAll(".queue-tabs button")[1].click()');
  await waitFor('document.querySelectorAll(".queue-tabs button")[1].getAttribute("aria-selected") === "true"');
  assert(await evaluate('document.querySelectorAll(".claim-list .claim-item").length > 0'), "estimated queue has claims");
  await evaluate('document.querySelector(".claim-list .claim-item").click()');
  await waitFor('location.search.includes("queue=estimated") && !!document.querySelector(".claim-detail .detail-actions")');
  await evaluate('document.querySelector(".detail-actions button").click()');
  await waitFor('!!document.querySelector(".claim-detail .review-sim")');
  await call("Page.navigate", { url: `${base}/` });
  await waitFor('!!document.querySelector(".menu-toggle")');
  await evaluate('document.querySelector(".menu-toggle").click()');
  await waitFor('document.querySelector(".menu-toggle").getAttribute("aria-expanded") === "true"');
  await evaluate('document.querySelector("#site-menu a").click()');
  await waitFor('document.querySelector(".menu-toggle").getAttribute("aria-expanded") === "false"');
  await call("Page.navigate", { url: `${base}/report/naver` });
  await waitFor('!!document.querySelector(".audit-summary")');
  if (data.demo_mode) assert(await evaluate(`document.querySelector(".audit-summary").innerText.includes(${JSON.stringify(`시연 등급 ${demoCount}건`)})`), "report separates demo grades");
  console.log("queue selection, claim review mode, and mobile menu: ok");
  if (shots) {
    await call("Emulation.setDeviceMetricsOverride", { width: 1400, height: 900, deviceScaleFactor: 1, mobile: false });
    await call("Page.navigate", { url: `${base}/demo#claims` });
    await waitFor('!!document.querySelector(".claims-section")');
    await evaluate('document.querySelector(".claims-section").scrollIntoView()');
    await new Promise(resolve => setTimeout(resolve, 250));
    const shot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
    await writeFile(`${shots}/naver-queue-desktop.png`, Buffer.from(shot.data, "base64"));
    for (const [size, width, height] of [["desktop", 1400, 900], ["mobile", 390, 844]]) {
      await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: size === "mobile" });
      await call("Page.navigate", { url: `${base}/analyze/replay` });
      await waitFor('!!document.querySelector(".replay-hero-bottom button")');
      await evaluate('document.querySelector(".replay-hero-bottom button").click()');
      await waitFor('document.body.innerText.includes("재생 완료")');
      const finished = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false });
      await writeFile(`${shots}/replay-finished-${size}.png`, Buffer.from(finished.data, "base64"));
    }
  }
} finally {
  socket.close();
}
