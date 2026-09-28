// Run against a built Vite preview and headless Chrome with --remote-debugging-port=9229.
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const sourcePdf = process.argv[2];
assert(sourcePdf, "Pass the processed NAVER PDF path");
const targets = await (await fetch("http://127.0.0.1:9229/json")).json();
const target = targets.find(item => item.type === "page" && item.url.includes("127.0.0.1:4173"));
assert(target, "Open http://127.0.0.1:4173/analyze in Chrome first");
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
let id = 0;
const pending = new Map();
const requests = [];
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
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
async function evalText(expression) {
  const result = await call("Runtime.evaluate", { expression, returnByValue: true });
  return result.result.value;
}
async function waitFor(expression) {
  for (let attempt = 0; attempt < 100; attempt++) {
    if (await evalText(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  throw new Error(`Timed out: ${expression}`);
}
async function upload(path) {
  const { root } = await call("DOM.getDocument");
  const { nodeId } = await call("DOM.querySelector", { nodeId: root.nodeId, selector: '.drop-zone input[type="file"]' });
  assert(nodeId, "PDF input exists");
  await call("DOM.setFileInputFiles", { nodeId, files: [path] });
}

const temp = await mkdtemp(join(tmpdir(), "proofops-analyze-"));
try {
  await call("Network.enable");
  await call("Page.navigate", { url: "http://127.0.0.1:4173/analyze" });
  await waitFor('!!document.querySelector(".drop-zone input")');
  const unknown = join(temp, "unknown.pdf");
  await writeFile(unknown, "%PDF-1.4\nlocal smoke test\n");
  await upload(unknown);
  await waitFor('document.body.innerText.includes("저장된 실행 결과와 일치하지 않습니다")');
  assert(await evalText('document.body.innerText.includes("한 문장 실시간 체험하기")'));
  await upload(sourcePdf);
  await waitFor('document.body.innerText.includes("이미 분석된 보고서입니다")');
  assert(await evalText('document.body.innerText.includes("저장된 처리 기록")'));
  await waitFor('location.pathname === "/analyze/replay"');
  assert.equal(requests.filter(request => request.method !== "GET").length, 0, "PDF must not be sent to a server");
  await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  for (const [route, selector] of [["/", ".hero"], ["/demo", ".demo-main"], ["/demo/c3430427-80a9-57e4-a87a-ec9f6f1017a3", ".why-panel"], ["/analyze", ".analyze-main"], ["/live", ".live-main"]]) {
    await call("Page.navigate", { url: `http://127.0.0.1:4173${route}` });
    await waitFor(`location.pathname === ${JSON.stringify(route)} && !!document.querySelector(${JSON.stringify(selector)})`);
    assert(await evalText("document.documentElement.scrollWidth <= innerWidth"), `390px horizontal overflow on ${route}`);
  }
  console.log("unmatched PDF, cache match, redirect, no upload request, and five 390px routes: passed");
} finally {
  socket.close();
  await rm(temp, { recursive: true, force: true });
}
