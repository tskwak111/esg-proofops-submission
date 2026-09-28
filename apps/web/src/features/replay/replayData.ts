export type ReplayStage = {
  title: string;
  detail: string;
  count: number | null;
  unit: string;
  durationMs: number;
};

export type ReplayModel = {
  title: string;
  stages: ReplayStage[];
  funnel: { label: string; count: number }[];
  pagesTotal: number | null;
  pagesProcessed: number | null;
  claimsDiscovered: number | null;
  claimsVerified: number | null;
  claimsDecided: number | null;
  claimsDisplayGraded: number | null;
  displayCounts: { confirmed: number; estimated: number; sourceUnverified: number } | null;
  paidCalls: number | null;
  costUsd: number | null;
  parseSeconds: number | null;
  taggingSeconds: number | null;
  demoPass: { label: string; count: number } | null;
  demoPassStats: { calls: number | null; costUsd: number | null; seconds: number | null } | null;
  samplePage: number | null;
};

type RecordValue = Record<string, unknown>;
const record = (value: unknown): RecordValue => value && typeof value === "object" && !Array.isArray(value) ? value as RecordValue : {};
const number = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : null;
const string = (value: unknown): string | null => typeof value === "string" && value.trim() ? value : null;
const amount = (value: unknown): number | null => number(value) ?? (typeof value === "string" && /^\d+(?:\.\d+)?$/.test(value) ? number(Number(value)) : null);

export function buildReplayModel(snapshot: unknown): ReplayModel {
  const root = record(snapshot);
  const coverage = record(root.coverage);
  const demoCoverage = record(root.demo_coverage);
  const run = record(root.run);
  const elapsed = record(run.model_elapsed_seconds);
  const funnel = (Array.isArray(root.funnel) ? root.funnel : []).map(item => {
    const row = record(item);
    return { label: string(row.label), count: number(row.count) };
  }).filter((item): item is { label: string; count: number } => item.label !== null && item.count !== null);
  const countFor = (...labels: string[]) => funnel.find(item => labels.includes(item.label))?.count ?? null;
  const pagesProcessed = number(coverage.pages_processed);
  const claimsDiscovered = number(coverage.claims_discovered) ?? countFor("추출 주장");
  const claimsVerified = countFor("원문 검증");
  const claimsDecided = number(coverage.claims_decided) ?? countFor("규칙 판정 기록");
  const claimsDisplayGraded = number(demoCoverage.claims_with_display_grade) ?? countFor("표시 등급", "규칙 판정") ?? claimsDecided;
  const relationCount = countFor("관계 태깅 시도", "관계 시도");
  const taggedCount = countFor("요소 태깅 시도", "요소 태그 발행");
  const reviewCount = countFor("위임 검토") ?? countFor("검토");
  const reports = Array.isArray(root.processed_reports) ? root.processed_reports : [];
  const claims = Array.isArray(root.claims) ? root.claims : [];
  const samplePage = claims.map(item => number(record(item).page)).find(page => page !== null) ?? null;
  const demo = record(root.demo_pass ?? run.demo_pass ?? root.demo_pass_stats);
  const demoCount = number(demo.claims_decided ?? demo.decided ?? demo.count);
  const demoPassStats = { calls: number(demo.calls), costUsd: amount(demo.cost_usd), seconds: number(demo.elapsed_seconds) };
  const notes = claims.map(item => {
    const claim = record(item);
    const decision = record(claim.decision);
    return string(decision.display_grade ?? claim.display_grade) ? decision.display_note ?? claim.display_note ?? null : undefined;
  }).filter(note => note !== undefined);
  const displayCounts = notes.length && (root.demo_mode === true || run.demo_mode === true) ? {
    confirmed: notes.filter(note => note === null).length,
    estimated: notes.filter(note => note === "추정").length,
    sourceUnverified: notes.filter(note => note === "원문 미검증").length,
  } : null;
  return {
    title: string(record(reports[0]).title) ?? string(root.title) ?? "저장된 보고서",
    stages: [
      { title: "PDF 업로드", detail: "보고서 지문 확인 · 저장된 실행 연결", count: reports.length || null, unit: "개 문서", durationMs: 2600 },
      { title: "페이지 파싱", detail: "선택 페이지의 본문·OCR·표 구조 읽기", count: pagesProcessed, unit: "쪽", durationMs: 7200 },
      { title: "주장 추출", detail: "환경 관련 문장을 원자 주장으로 분리", count: claimsDiscovered, unit: "건", durationMs: 6100 },
      { title: "원문 검증", detail: "인용과 페이지 위치를 원문에 대조", count: claimsVerified, unit: "건", durationMs: 3900 },
      { title: "예비 분류", detail: "환경 주장 트랙을 분류", count: countFor("예비 분류", "예비 분류 합의"), unit: "건", durationMs: 4000 },
      { title: "관계·요소 태깅", detail: relationCount === null ? "근거 관계와 판정 요소를 연결" : `관계 태깅 ${relationCount.toLocaleString("ko-KR")}건 · 요소 태깅`, count: taggedCount, unit: "건", durationMs: 4700 },
      { title: "규칙 판정", detail: "확정 판정과 시연 표시 등급을 구분", count: claimsDisplayGraded, unit: "건", durationMs: 3200 },
      { title: "검토", detail: "판정과 보류 항목을 검토 기록에 연결", count: reviewCount, unit: "건", durationMs: 3100 },
      { title: "보고서", detail: "근거와 판정 경로를 결과 화면에 정리", count: reports.length || null, unit: "개 결과", durationMs: 2900 },
    ],
    funnel,
    pagesTotal: number(coverage.pages_total),
    pagesProcessed,
    claimsDiscovered,
    claimsVerified,
    claimsDecided,
    claimsDisplayGraded,
    displayCounts,
    paidCalls: number(run.model_paid_calls ?? run.paid_calls),
    costUsd: number(run.model_cost_usd ?? run.cost_usd),
    parseSeconds: number(elapsed.parse_extraction),
    taggingSeconds: number(elapsed.tagging),
    demoPass: demoCount === null ? null : { label: string(demo.label) ?? "시연 통과", count: demoCount },
    demoPassStats: Object.values(demoPassStats).every(value => value === null) ? null : demoPassStats,
    samplePage,
  };
}

export const replayDuration = (stages: ReplayStage[]) => stages.reduce((total, stage) => total + stage.durationMs, 0);

export function stageAt(stages: ReplayStage[], elapsedMs: number) {
  let start = 0;
  for (let index = 0; index < stages.length; index += 1) {
    const end = start + stages[index].durationMs;
    if (elapsedMs < end) return { index, fraction: Math.max(0, (elapsedMs - start) / stages[index].durationMs), start };
    start = end;
  }
  return { index: stages.length, fraction: 1, start };
}
