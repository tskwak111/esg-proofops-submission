export const elementLabels: Record<string, string> = {
  G1: "목표연도",
  G2: "목표수치·지표",
  G3: "기준연도·기준값",
  G4: "적용범위 (Scope·조직경계)",
  G5: "현재 이행률·진척",
  G6: "전환계획·달성수단",
  G7: "상쇄(탄소배출권) 사용 계획",
  G8: "과학기반 목표 검증",
  P1: "정량수치와 단위",
  P2: "비교기준 (전년·기준연도)",
  P3: "산정방법론과 경계",
  P4: "보증 연결",
  P5: "절대량/원단위 구분 명시",
  P6: "본문 수치와 데이터 표의 일치",
  M1: "이행방법·명명된 표준",
  M2: "적용범위 (조직경계·사업장)",
  M3: "외부검증",
  M4: "이행 실적의 구체성",
  M5: "담당 조직·거버넌스",
  M6: "경영진 보상 연동"
};

export function getElementLabel(elementId: string): string {
  if (Object.hasOwn(fieldLabels, elementId)) return fieldLabels[elementId];
  const label = elementLabels[elementId];
  return Object.hasOwn(elementLabels, elementId) ? `${elementId} · ${label}` : elementId;
}

const fieldLabels: Record<string, string> = {
  quantitative_or_qualified_ordinal: "정량 수치", unit_or_qualified_ordinal: "단위", comparison_baseline: "비교 기준",
  calculation_boundary: "산정 경계", method: "산정 방법론", assurance_covered: "외부 보증", target_year: "목표 연도",
  target_metric: "목표 지표·수치", baseline_year: "기준 연도", baseline_value: "기준값", scope: "적용 범위",
  org_boundary: "조직 경계", current_progress: "현재 진척", transition_plan: "이행 계획",
};
