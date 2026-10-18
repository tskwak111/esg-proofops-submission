import type { HostedResult, HostedRun, ResultClaim } from "./hostedClient";
import { elementLabels } from "../labels";
import { confirmedDecision, resultBrowserItem, resultBrowserSummary, trackText, w18HoldReason } from "./resultModel";

// Additive API projection policy; the web UI is always enabled. Analysis payloads remain immutable.
export const UX1_POLICY = "ux1-results-v1:domain-v2:dec125-127-154:deterministic:observed-progress";
export const UX1_HASH = "bf9c32d96dde26ab8575bedc8cbbce157e74c1a1dd9c0b57634f4cdd9fcf50b3";
export const gradeMeanings = [
  { grade: "E3", title: "입증요소 충분", text: "트랙에서 요구하는 입증요소를 충분히 갖췄습니다." },
  { grade: "E2", title: "트랙별 추가 입증 필요", text: "핵심 요소와 범위가 확인된 단계입니다. E3에는 목표형은 현재 진척과 전환계획, 성과형은 산정방법과 해당 성과의 보증 연결, 관리체계는 외부검증이 추가로 필요합니다." },
  { grade: "E1", title: "확인 요소 부족", text: "주장은 있으나 확인에 필요한 요소가 부족합니다." },
  { grade: "E0", title: "입증요소 부재", text: "분석 범위에서 필요한 입증요소를 찾거나 연결하지 못했습니다. 그린워싱 위험 신호로 살펴볼 항목입니다." },
];
// Wording follows config/rubric/*.yaml, source §4.4 and the additive DEC127 rule.
const ruleText: Record<string, string> = {
  GOAL_NO_YEAR: "목표연도 근거가 없어 E0 규칙이 적용되었습니다.",
  GOAL_NO_YEAR_EXCEPTION: "목표연도는 없지만 목표수치와 전환계획이 있어 E1 상한 예외가 적용되었습니다.",
  GOAL_E1: "목표연도와 목표수치가 확인되어 목표형 E1 규칙이 적용되었습니다.",
  GOAL_E2: "목표연도·수치, 기준연도·기준값, 적용범위가 확인되어 목표형 E2 규칙이 적용되었습니다.",
  GOAL_E3: "목표·기준·범위에 현재 진척과 전환계획까지 확인되어 목표형 E3 규칙이 적용되었습니다.",
  PERF_NO_VALUE: "정량수치 또는 인정 가능한 인증등급 근거가 없어 성과형 E0 규칙이 적용되었습니다.",
  PERF_E1: "수치와 단위 또는 인정 가능한 인증등급이 확인되어 성과형 E1 규칙이 적용되었습니다.",
  PERF_E2: "수치·단위에 비교기준과 산정범위가 확인되어 성과형 E2 규칙이 적용되었습니다.",
  PERF_E3: "수치·단위·비교기준·범위에 산정방법과 보증 연결까지 확인되어 성과형 E3 규칙이 적용되었습니다.",
  MGMT_WILLINGNESS: "의지 표현만 확인되어 관리체계형 E0 규칙이 적용되었습니다.",
  MGMT_NO_MEANS_IN_SCOPE_E0: "분석 범위에서 명명된 수단이나 구체적인 시행 상태의 근거를 연결하지 못해 E0 규칙이 적용되었습니다.",
  MGMT_E1: "명명된 수단 또는 구체적 시행 상태가 확인되어 관리체계형 E1 규칙이 적용되었습니다.",
  MGMT_E2: "수단·시행 상태와 적용범위가 확인되어 관리체계형 E2 규칙이 적용되었습니다.",
  MGMT_E3: "수단·시행 상태, 적용범위, 외부검증이 확인되어 관리체계형 E3 규칙이 적용되었습니다.",
  PRODUCT_NAMES_CAP: "제품·소재의 직접적인 비율·목표 근거가 없어 제품명·소재명만으로는 E1이 상한입니다.",
  SUPERLATIVE_E0: "최상급 표현의 비교기준과 외부검증이 모두 없어 트랙에 우선하는 E0 규칙이 적용되었습니다.",
  CATEGORICAL_ORDINAL: "인증기관이 명시된 범주형 인증등급의 인정 조건을 적용했습니다.",
};
export function claimExplanation(result: HostedResult, claim: ResultClaim) {
  const item = resultBrowserItem(result, claim);
  if (item.bucket === "hold") return { text: `판정 보류 · ${w18HoldReason(result, claim) ?? "원문·유형·규칙 확인 필요"}. 보류는 E0이나 근거 부재 판정이 아닙니다.`, rules: [] };
  const decision = confirmedDecision(claim);
  const rules = decision?.rule_ids ?? [];
  const lead = item.confirmed ? `${trackText[item.track] ?? "해당"} 주장의 등급은 ${item.grade}입니다.` : `이전 버전 결과 · ${item.grade}는 당시 연결된 근거에 따른 등급이며 추가 확인이 필요합니다. 확정 판정이 아닙니다.`;
  const applied = rules.map(id => ruleText[id] ?? `기록된 규칙 ${id}가 적용되었습니다.`).join(" ");
  const present = item.elements.filter(e => e.state === "present" && e.evidence_refs.some(r => r.verification_state === "verified"));
  const absent = item.elements.filter(e => ["absent", "absent_in_scope"].includes(e.state) && !decision?.excluded_elements?.includes(e.element_id));
  const unresolved = item.elements.filter(e => ["unknown", "conflict", "unreadable"].includes(e.state));
  const names = (elements: typeof item.elements) => elements.map(e => elementLabels[e.element_id] ?? e.element_id).join(", ");
  return { text: [lead, applied, present.length ? `원문 근거가 연결된 요소: ${names(present)}.` : "", absent.length ? `분석 범위에서 근거가 연결되지 않은 요소: ${names(absent)}.` : "", unresolved.length ? `확인이 남은 요소: ${names(unresolved)}.` : "", !rules.length ? "적용 규칙 ID가 제공되지 않아 요소 상태를 함께 표시합니다." : ""].filter(Boolean).join(" "), rules };
}

/** Only concise names qualify as metrics; table rows and ambiguous values stay in the track group. */
export function cleanMetricName(value: unknown): string {
  if (typeof value !== "string") return "";
  const name = value.trim();
  if (!name || name.length > 60 || /[|/\n\r\t]|\d[,.]\d|\d{3,}|\d\s*[%)]/.test(name)
    || /^(?:지표\s*미분류|미분류|미제공|없음|unknown|undefined|null|n\/?a|[-—])$/i.test(name)) return "";
  return /[a-z가-힣]/i.test(name) ? name : "";
}

export function reportInsights(result: HostedResult) {
  const items = result.claims.map(c => resultBrowserItem(result, c));
  const needs = new Map<string, { track: string; metric: string; count: number; elements: Set<string> }>();
  for (const item of items.filter(i => i.confirmed && i.grade !== "E3")) {
    const missing = item.elements.filter(e => ["absent", "absent_in_scope"].includes(e.state) && !confirmedDecision(item.claim)?.excluded_elements?.includes(e.element_id));
    if (!missing.length) continue;
    const metric = cleanMetricName(item.claim.table_observation?.metric);
    const key = `${item.track}:${metric}`;
    const row = needs.get(key) ?? { track: trackText[item.track] ?? "분류 미합의", metric, count: 0, elements: new Set<string>() };
    row.count++; missing.forEach(e => row.elements.add(elementLabels[e.element_id] ?? e.element_id)); needs.set(key, row);
  }
  const top = items.filter(i => i.confirmed).sort((a,b) => (b.grade ?? "").localeCompare(a.grade ?? "") || a.claim.original_page_num - b.claim.original_page_num).slice(0,3);
  const tracks = ["goal", "performance", "management", "unknown"].map(track => {
    const rows = items.filter(i => i.track === track || (track === "unknown" && !["goal","performance","management"].includes(i.track)));
    return { track, name: trackText[track] ?? "분류 미합의", total: rows.length, confirmed: rows.filter(i=>i.confirmed).length, hold: rows.filter(i=>i.bucket==="hold").length, grades: Object.fromEntries(["E3","E2","E1","E0"].map(g=>[g,rows.filter(i=>i.confirmed && i.grade===g).length])) };
  });
  return { needs: [...needs.values()].sort((a,b)=>b.count-a.count), top, tracks };
}
const count = (n: unknown): number | null => typeof n === "number" && Number.isSafeInteger(n) && n >= 0 ? n : null;
export function analyzedData(result: HostedResult) {
  const roles = result.pipeline?.provider_concurrency?.completed_by_role;
  // Role counters are completed calls, not claims × 3 and not all provider attempts.
  const preliminary = count(roles?.preliminary), elements = count(roles?.elements);
  return { tables: count(result.pipeline?.ux1_data?.table_count), tableClaims: result.claims.filter(c=>c.table_observation).length,
    modelCalls: count(result.pipeline?.ux1_data?.model_judgment_calls) ?? (preliminary !== null && elements !== null ? preliminary + elements : null),
    preliminary, elements, verified: resultBrowserSummary(result).verified };
}

export const analysisSteps = ["파싱", "주장 추출", "3회 독립 판정", "원문 대조", "규칙엔진", "게시"];
export function progressSteps(run: HostedRun) {
  // Current API groups verification and grading into tag/result; never invent completion events.
  const active = run.stage === "parse" ? [0] : run.stage === "extract" ? [1] : run.stage === "tag" ? [2,3] : run.stage === "result" ? [3,4,5] : [];
  return analysisSteps.map((label,index) => ({ label, state: run.status === "completed" ? "done" : active.includes(index) ? "active" : active.length && index < active[0] ? "previous" : "waiting" }));
}
export function remainingEstimate(run: HostedRun) {
  if (run.status === "queued") return "대기시간 미정 · 분석 시작 후 예상 약 20–45분";
  const p = run.progress;
  if (typeof p !== "number" || !Number.isFinite(p) || p <= 0 || p >= 1) return "남은 시간 계산 중 · 전체 분석 예상 약 20–45분";
  return `예상 남은 시간 약 ${Math.max(1,Math.round(20*(1-p)))}–${Math.max(2,Math.ceil(45*(1-p)))}분`;
}

/** Local time measures observation, never completed work or a synthetic percentage. */
export function stageActivity(run: HostedRun, observedSeconds: number) {
  if (!["running", "queued"].includes(run.status)) return null;
  const seconds = Math.max(0, Math.floor(Number.isFinite(observedSeconds) ? observedSeconds : 0));
  const descriptions: Record<string, string> = {
    queued: "분석 시작 순서를 기다리고 있습니다.",
    parse: "선택한 PDF 쪽의 문장과 표를 읽고 원문 위치를 연결하고 있습니다.",
    extract: "읽은 문장과 표에서 환경 주장 후보를 추출하고 있습니다.",
    tag: "주장 유형과 요소를 독립 판독하고, 근거 문구·범위·원문 위치를 대조하고 있습니다.",
    result: "연결된 근거에 규칙을 적용하고 결과를 정리하고 있습니다.",
  };
  return {
    elapsed: `${Math.floor(seconds / 60)}분 ${seconds % 60}초`,
    counters: ([ ["preliminary", "유형 처리"], ["relation", "근거 관계 처리"], ["elements", "요소 처리"] ] as const).flatMap(([key, label]) => {
      const completed = count(run.claim_stage_counts?.[key]);
      return completed === null ? [] : [{ key, label, completed }];
    }),
    description: descriptions[run.status === "queued" ? "queued" : run.stage ?? ""] ?? "서버에서 분석을 처리하고 있습니다.",
    note: "이 화면에서 현재 단계를 관찰한 시간입니다. 완료 건수는 서버 집계 시 갱신되며, 경과 시간은 완료율이나 서버 응답 확인을 뜻하지 않습니다.",
  };
}
