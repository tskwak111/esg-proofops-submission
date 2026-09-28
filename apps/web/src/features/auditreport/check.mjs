import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { readFile } from "node:fs/promises";
import { createServer } from "vite";

const vite = await createServer({ server: { middlewareMode: true }, appType: "custom" });
try {
  const { reportRow, csvCell } = await vite.ssrLoadModule("/src/features/auditreport/AuditReportPage.tsx");
  const load = async name => JSON.parse(await readFile(new URL(`../../../public/demo/${name}-2025.json`, import.meta.url), "utf8"));
  const naver = await load("naver");
  assert.equal(naver.claims.filter(claim => claim.decision.grade).length, 17);
  assert.equal(naver.claims.map(claim => reportRow(claim, false)).filter(row => row.grade === "E3").length, 17);
  if (existsSync(new URL("../../../public/demo/kia-2025.json", import.meta.url))) {
    const kia = await load("kia");
    const kiaRow = reportRow(kia.claims[0], true);
    assert.equal(kiaRow.grade, "E1");
    assert.equal(kiaRow.estimated, true);
    assert.match(kiaRow.quote, /Scope 1·2/);
    assert.deepEqual(kiaRow.evidencePages, [35, 130, 131]);
    assert.equal(reportRow(kia.claims[0], false).grade, "E1–E3 범위");
  }
  assert.equal(csvCell('=1+1'), '"\'=1+1"');
  assert.equal(csvCell('a"b'), '"a""b"');
  assert(naver.claims.every(claim => Array.from(reportRow(claim, false).quote).length <= 200));
  console.log("audit report data check: passed");
} finally {
  await vite.close();
}
