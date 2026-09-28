import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { elementLabels, getElementLabel } from "../../apps/web/src/features/labels.ts";

const domain = readFileSync(new URL("../../sources/PROJECT_DOMAIN_V2_ORIGINAL.md", import.meta.url), "utf8");
const rows = [...domain.matchAll(/^\| ([GPM]\d) \| ([^|]+) \|/gm)];
assert.equal(rows.length, 20);
assert.equal(Object.keys(elementLabels).length, rows.length);
for (const [, id, label] of rows) assert.equal(getElementLabel(id), `${id} · ${label.trim()}`);
for (const unknown of ["G9", "constructor", "__proto__", ""]) assert.equal(getElementLabel(unknown), unknown);
console.log("20 domain labels and unknown-ID fallback verified");
