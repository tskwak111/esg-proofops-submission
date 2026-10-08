// Pure presentation/contract helpers for the r108-hosted-result-v1 DTO (no React, node-testable).
import { elementLabels } from "../labels";
import type { ElementState, HostedResult, MissingEvidence, ResolveBody, ResultClaim, ResultDecision, ResultElement, ResultReview, SourceRefDto } from "./hostedClient";

export const trackText: Record<string, string> = { management: "관리체계", goal: "목표", performance: "성과" };

/** unknown is never shown as absence of evidence. */
export function elementStateText(state: string, sourceQuality: string): string {
  if (state === "present") return sourceQuality === "verified" ? "근거 확인" : "원문 대조 필요";
  if (state === "unknown") return "확인되지 않음(근거 없음 아님)";
  if (state === "absent") return "근거 없음(검증된 검색 범위)";
  if (state === "conflict") return "근거 충돌";
  if (state === "not_applicable") return "비적용";
  return state;
}

export function sourceQualityText(quality: string): string {
  return quality === "verified" ? "원문 검증됨" : quality === "unverified" ? "원문 대조 필요" : quality;
}

export function reviewStatusText(status: string | undefined): string {
  switch (status) {
    case "human_confirmed": return "사람 확인";
    case "ai_delegated_confirmed": return "AI 위임 확인";
    case "auto_confirmed": return "자동 확정";
    case "needs_review": return "사람 검토 필요";
    default: return status ?? "";
  }
}

export function decisionLine(decision: ResultDecision | null): string | null {
  if (!decision?.evidence_grade) return null;
  return `${decision.evidence_grade}${decision.label ? ` · ${decision.label}` : ""}`;
}

/** Confirmed grade and possible range are kept apart; a range never becomes a grade. */
export function gradeSummary(claim: ResultClaim): { confirmed: boolean; headline: string; note: string } {
  const grade = claim.confirmed_grade;
  if (grade) {
    const prov = reviewStatusText(claim.decision?.review_status);
    return { confirmed: true, headline: `확정 등급 ${decisionLine(claim.decision) ?? grade}`, note: prov };
  }
  const range = claim.possible_grade_range;
  if (range) {
    const open = range.open_elements.map(id => `${id} ${elementLabels[id] ?? ""}`.trim()).join("·");
    return { confirmed: false, headline: "확정 등급 아님", note: `가능 범위 ${range.floor}–${range.ceiling} (미해결 요소: ${open || "없음"}). 범위는 등급이 아니며 하한·상한만 알려 줍니다.` };
  }
  return { confirmed: false, headline: "확정 등급 아님", note: claim.hold_reason ? "판정이 보류되어 등급과 가능 범위가 없습니다." : "규칙 판정이 실행되지 않아 등급과 가능 범위가 없습니다." };
}

export function pageLabel(result: Pick<HostedResult, "page_map">, selectedPage: number): string {
  const original = result.page_map?.[selectedPage - 1];
  return original != null ? `p.${original}` : `선택본 p.${selectedPage}`;
}

export function openReviewFor(result: HostedResult, claimId: string): ResultReview | null {
  return result.reviews.find(review => review.claim_id === claimId && review.status === "open") ?? null;
}

export function candidateRefs(claim: ResultClaim, element: ResultElement): SourceRefDto[] {
  const found = new Map<string, SourceRefDto>();
  const add = (ref: SourceRefDto) => found.set(refKey(ref), ref);
  element.evidence_refs.forEach(add);
  claim.missing_evidence.filter((m: MissingEvidence) => m.element_id === element.element_id).forEach(m => (m.candidate_refs ?? []).forEach(add));
  return [...found.values()];
}
export const refKey = (ref: SourceRefDto) => `${ref.source_id}:${ref.char_start}-${ref.char_end}`;

/** States a reviewer may pick. absent is offered but may be rejected (needs verified coverage); present needs a citation. */
export function allowedChoices(claim: ResultClaim, element: ResultElement): { state: ElementState; label: string; disabled?: boolean; hint?: string }[] {
  const refs = candidateRefs(claim, element);
  const missing = claim.missing_evidence.find(m => m.element_id === element.element_id);
  return [
    { state: "unknown", label: "확인되지 않음(근거 없음 아님)" },
    { state: "present", label: "근거 있음", disabled: refs.length === 0, hint: refs.length === 0 ? "연결할 원문 근거 후보가 없습니다" : undefined },
    { state: "absent", label: "근거 없음", hint: missing?.search?.absence_admissible === false ? "검색 범위가 검증되지 않아 서버가 거부할 수 있습니다" : undefined },
  ];
}

export type ElementChoice = { state: ElementState; refKey?: string };
export function buildResolveBody(claim: ResultClaim, review: ResultReview, choices: Record<string, ElementChoice>, reason: string): ResolveBody {
  if (claim.track !== "goal" && claim.track !== "performance" && claim.track !== "management") throw new Error("트랙이 합의되지 않은 주장은 사람 검토를 제출할 수 없습니다.");
  const text = reason.trim();
  if (text.length < 5 || text.length > 1000) throw new Error("검토 사유를 5자 이상 1000자 이하로 입력해 주세요.");
  return {
    base_tag_revision: review.base_tag_revision, track: claim.track, reason: text,
    elements: claim.elements.map(element => {
      const choice = choices[element.element_id];
      if (!choice || choice.state === element.state) return { ...element }; // unchanged elements are sent back as served
      const refs = choice.state === "present" ? candidateRefs(claim, element).filter(ref => refKey(ref) === choice.refKey) : [];
      if (choice.state === "present" && !refs.length) throw new Error(`${element.element_id}: 연결할 원문 근거를 선택해 주세요.`);
      return { ...element, state: choice.state, evidence_refs: refs, reason_code: null };
    }),
  };
}
