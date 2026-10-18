// Result tabs browser smoke. Open a completed hosted result (/analyze#run-<id>, logged in) in Chrome
// started with --remote-debugging-port, then: node test/result-tabs-smoke.mjs [port=9229]
// Checks URL sync, back/forward, cross-tab moves, kept state (filters, PDF page/zoom, no PDF reload),
// keyboard tabs, sticky bar, queue-tabs overflow and per-tab page height at 1144px.
import assert from "node:assert/strict";

const port = Number(process.argv[2] ?? 9229);
const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
const target = targets.find(item => item.type === "page" && /\/analyze/.test(item.url) && /#run-/.test(item.url));
assert(target, "Open a hosted result at /analyze#run-<id> first");
const socket = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
let id = 0;
const pending = new Map();
socket.onmessage = ({ data }) => {
  const message = JSON.parse(data);
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
};
const send = (method, params = {}) => new Promise(resolve => { const next = ++id; pending.set(next, resolve); socket.send(JSON.stringify({ id: next, method, params })); });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function page(source) {
  const response = await send("Runtime.evaluate", { expression: `(async () => { ${source} })()`, awaitPromise: true, returnByValue: true, userGesture: true });
  if (response.result.exceptionDetails) throw new Error(response.result.exceptionDetails.exception?.description ?? response.result.exceptionDetails.text);
  return response.result.result.value;
}
async function key(name) {
  const code = { ArrowRight: 39, ArrowLeft: 37, Home: 36, End: 35, Enter: 13 }[name];
  await send("Input.dispatchKeyEvent", { type: "keyDown", key: name, code: name, windowsVirtualKeyCode: code, ...(name === "Enter" ? { text: "\r" } : {}) });
  await send("Input.dispatchKeyEvent", { type: "keyUp", key: name, code: name, windowsVirtualKeyCode: code });
}
const helpers = `
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const tab = key => document.querySelector('.result-tabs [role=tab][id$="-' + key + '"]');
  const selected = () => document.querySelector('.result-tabs [aria-selected=true]').id.split('-').pop();
  const typeInto = (input, value) => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value); input.dispatchEvent(new Event('input', { bubbles: true })); };
`;

await send("Emulation.setDeviceMetricsOverride", { width: 1144, height: 900, deviceScaleFactor: 1, mobile: false });
await sleep(300);
try {
  // Fresh load: only the selected panel is mounted; the others stay empty until first visited.
  await page(`location.reload();`).catch(() => {});
  for (let attempt = 0; attempt < 60; attempt++) {
    await sleep(500);
    if (await page(`return !!document.querySelector('.result-tabs');`).catch(() => false)) break;
  }
  const lazy = await page(`return [...document.querySelectorAll('.result-tabpanel')].map(node => ({ key: node.id.split('-').pop(), hidden: node.hidden, children: node.childElementCount }));`);
  for (const panel of lazy) assert.equal(panel.children > 0, !panel.hidden, `${panel.key} mounted only when shown`);

  // Tabs, ARIA and per-tab height.
  const heights = await page(`${helpers}
    const out = {};
    for (const key of ['overview', 'claims', 'source']) { tab(key).click(); await wait(key === 'source' ? 4000 : 600); out[key] = { height: document.documentElement.scrollHeight, search: location.search }; }
    tab('overview').click(); await wait(300);
    return out;`);
  for (const [key, { height, search }] of Object.entries(heights)) {
    // Wide screens show the whole claim list (no inner scroll) with a sticky detail, so the claims tab grows with its list.
    if (key !== "claims") assert.ok(height <= 2500, `${key} page height ${height}px`);
    assert.equal(search, key === "overview" ? "" : `?tab=${key}`);
  }

  // Back/forward follow the tab history without reopening the run.
  const history = await page(`${helpers}
    tab('claims').click(); await wait(200); tab('source').click(); await wait(200);
    const out = [selected()]; history.back(); await wait(400); out.push(selected()); history.forward(); await wait(400); out.push(selected());
    return out;`);
  assert.deepEqual(history, ["source", "claims", "source"]);

  // Summary → claims → source keeps one selected claim.
  const flow = await page(`${helpers}
    tab('overview').click(); await wait(300);
    const top = document.querySelector('.ux1-top button');
    if (!top) return null;
    const quote = top.querySelector('span').innerText.trim().slice(0, 20);
    top.click(); await wait(600);
    const claims = { tab: selected(), item: document.querySelector('.claim-item.selected .claim-quote')?.innerText.trim().slice(0, 20), detail: document.querySelector('.claim-detail blockquote')?.innerText.replace(/^“/, '').trim().slice(0, 20) };
    document.querySelector('.claim-source-button').click(); await wait(3000);
    return { quote, claims, source: { tab: selected(), item: document.querySelector('.split-claim.selected p')?.innerText.trim().slice(0, 20) } };`);
  if (flow) {
    assert.deepEqual(flow.claims, { tab: "claims", item: flow.quote, detail: flow.quote });
    assert.deepEqual(flow.source, { tab: "source", item: flow.quote });
  }

  // Source page/zoom, claims search and list scroll survive tab hops; the PDF canvas is not recreated.
  const kept = await page(`${helpers}
    tab('source').click(); await wait(800);
    const canvas = document.querySelector('.split-document canvas'); if (canvas) canvas.dataset.smoke = 'kept';
    document.querySelector('[aria-label="PDF 확대"]').click(); await wait(800);
    const zoom = document.querySelector('.split-toolbar output').innerText, pageNo = document.querySelector('[aria-label="원문 물리 쪽"]').value;
    tab('claims').click(); await wait(300);
    typeInto(document.querySelector('.result-browser-controls input'), 'p.'); await wait(300);
    const list = document.querySelector('.claim-list'); list.scrollTop = 120; const scroll = list.scrollTop;
    tab('overview').click(); await wait(300); tab('source').click(); await wait(800);
    const source = { zoom: document.querySelector('.split-toolbar output').innerText, page: document.querySelector('[aria-label="원문 물리 쪽"]').value, sameCanvas: !canvas || document.querySelector('.split-document canvas')?.dataset.smoke === 'kept' };
    tab('claims').click(); await wait(300);
    const claims = { search: document.querySelector('.result-browser-controls input').value, scroll: document.querySelector('.claim-list').scrollTop };
    typeInto(document.querySelector('.result-browser-controls input'), ''); await wait(200);
    return { before: { zoom, page: pageNo, scroll }, source, claims };`);
  assert.deepEqual(kept.source, { zoom: kept.before.zoom, page: kept.before.page, sameCanvas: true });
  assert.deepEqual(kept.claims, { search: "p.", scroll: kept.before.scroll });

  // Source list selection carries back to the claims tab.
  const reverse = await page(`${helpers}
    tab('source').click(); await wait(400);
    const pick = [...document.querySelectorAll('.split-claim')].at(-1); const quote = pick.querySelector('p').innerText.trim().slice(0, 20); pick.click(); await wait(400);
    tab('claims').click(); await wait(400);
    return { quote, detail: document.querySelector('.claim-detail blockquote')?.innerText.replace(/^“/, '').trim().slice(0, 20) };`);
  assert.equal(reverse.detail, reverse.quote);

  // Summary hold reason → claims tab, hold queue, reason filter.
  const hold = await page(`${helpers}
    tab('overview').click(); await wait(300);
    const button = document.querySelector('.ux1-holds button'); if (!button) return null;
    // Buttons show a short label; the full reason (the filter key) is in title.
    const reason = button.title; button.click(); await wait(1200);
    const chip = document.querySelector('.result-filter-chip');
    return { reason, tab: selected(), queue: document.querySelector('.queue-tabs [aria-selected=true]').id.split('-').pop(), chip: chip?.querySelector('strong').title,
      chipBelowTabs: Math.round(chip.getBoundingClientRect().top - document.querySelector('.result-tabs').getBoundingClientRect().bottom),
      reasons: [...document.querySelectorAll('.claim-item .w18-hold-reason')].map(node => node.title) };`);
  if (hold) {
    assert.equal(hold.tab, "claims");
    assert.equal(hold.queue, "hold");
    assert.ok(hold.reasons.length > 0 && hold.reasons.every(text => text.includes(hold.reason)));
    assert.equal(hold.chip, hold.reason);
    assert.equal(hold.chipBelowTabs, 12, "hold-reason click scrolls the chip right below the tab bar");
  }

  // queue-tabs no longer overflows by a pixel.
  const queue = await page(`const node = document.querySelector('.queue-tabs'); return { scroll: node.scrollHeight, client: node.clientHeight };`);
  assert.ok(queue.scroll <= queue.client, `queue-tabs ${queue.scroll}/${queue.client}`);

  // Keyboard: arrows/Home/End move focus only; Enter selects.
  await page(`${helpers} tab('overview').click(); await wait(200); tab('overview').focus();`);
  const focus = async () => page(`return { focus: document.activeElement.id.split('-').pop(), selected: document.querySelector('.result-tabs [aria-selected=true]').id.split('-').pop() };`);
  await key("ArrowRight"); assert.deepEqual(await focus(), { focus: "claims", selected: "overview" });
  await key("End"); assert.deepEqual(await focus(), { focus: "source", selected: "overview" });
  await key("ArrowRight"); assert.deepEqual(await focus(), { focus: "overview", selected: "overview" });
  await key("ArrowLeft"); await key("Enter"); await sleep(300);
  assert.deepEqual(await focus(), { focus: "source", selected: "source" });

  // Sticky bar rests under the site header after scrolling, and switching tabs lands at the bar.
  const sticky = await page(`${helpers}
    tab('overview').click(); await wait(200); scrollTo(0, document.documentElement.scrollHeight); await wait(300);
    const header = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--header-h'));
    const stuck = Math.round(document.querySelector('.result-tabs').getBoundingClientRect().top);
    tab('claims').click(); await wait(400);
    return { header: Math.round(header), stuck, afterSwitch: Math.round(document.querySelector('.result-tabs').getBoundingClientRect().top) };`);
  assert.equal(sticky.stuck, sticky.header);
  assert.equal(sticky.afterSwitch, sticky.header);
  console.log(JSON.stringify({ heights, history, flow, kept, reverse, hold, queue, sticky }, null, 2));
} finally {
  await send("Emulation.clearDeviceMetricsOverride");
  socket.close();
}
