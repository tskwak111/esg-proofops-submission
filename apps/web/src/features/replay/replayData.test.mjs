import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { buildReplayModel, replayDuration, stageAt } from "./replayData.ts";

test("saved NAVER run becomes a complete replay without inventing counts", () => {
  const snapshot = JSON.parse(readFileSync(new URL("../../../public/demo/naver-2025.json", import.meta.url), "utf8"));
  const model = buildReplayModel(snapshot);
  assert.equal(model.stages.length, 9);
  assert.equal(model.stages[1].count, snapshot.coverage.pages_processed);
  assert.equal(model.stages[2].count, snapshot.coverage.claims_discovered);
  assert.equal(model.stages[4].count, 331);
  assert.equal(model.stages[5].count, 331);
  assert.equal(model.stages[6].count, snapshot.demo_coverage.claims_with_display_grade);
  assert.equal(model.claimsDecided, snapshot.coverage.claims_decided);
  assert.deepEqual(model.displayCounts, { confirmed: 17, estimated: 255, sourceUnverified: 59 });
  assert.deepEqual(model.demoPassStats, { calls: 788, costUsd: 0.65384, seconds: 3013.5 });
  assert.equal(model.paidCalls, snapshot.run.model_paid_calls);
  assert.equal(model.costUsd, snapshot.run.model_cost_usd);
  assert.equal(model.parseSeconds, snapshot.run.model_elapsed_seconds.parse_extraction);
  assert.equal(model.taggingSeconds, snapshot.run.model_elapsed_seconds.tagging);
  assert.equal(replayDuration(model.stages), 37700);
  assert.equal(stageAt(model.stages, replayDuration(model.stages)).index, 9);
});

test("missing optional data stays unknown and added demo pass data is used", () => {
  const model = buildReplayModel({ coverage: { claims_decided: 12 }, demo_pass: { claims_decided: 12 } });
  assert.equal(model.pagesProcessed, null);
  assert.equal(model.paidCalls, null);
  assert.deepEqual(model.demoPass, { label: "추가 분석", count: 12 });
  assert.equal(model.stages[6].count, 12);
  assert.equal(model.displayCounts, null);
  assert.equal(model.demoPassStats, null);
});

test("older funnel labels remain readable", () => {
  const model = buildReplayModel({
    funnel: [
      { label: "예비 분류 합의", count: 8 },
      { label: "관계 시도", count: 7 },
      { label: "요소 태그 발행", count: 6 },
      { label: "규칙 판정 기록", count: 5 },
    ],
  });
  assert.equal(model.stages[4].count, 8);
  assert.equal(model.stages[5].count, 6);
  assert.equal(model.stages[6].count, 5);
});
