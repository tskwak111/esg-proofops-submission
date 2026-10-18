// Pure presentation/contract helpers for the r108-hosted-result-v1 DTO (no React, node-testable).
import { elementLabels } from "../labels";
import { holdReasonText, isProcessingFailure } from "./hostedClient";
import type { ElementState, HostedResult, MissingEvidence, ResolveBody, ResultClaim, ResultDecision, ResultElement, ResultReview, SourceRefDto, TableObservation } from "./hostedClient";

export const trackText: Record<string, string> = { management: "관리체계형", goal: "목표형", performance: "성과형" };

/** unknown is never shown as absence of evidence. */
export function elementStateText(state: string, sourceQuality: string): string {
  if (state === "present") return sourceQuality === "verified" ? "근거 확인" : "원문 대조 필요";
  if (state === "unknown") return "인정된 근거 미연결(확인 필요)";
  if (state === "absent_in_scope") return "근거 없음(분석 범위 내)";
  if (state === "absent") return "근거 없음(검증된 검색 범위)";
  if (state === "unreadable") return "판독 불가";
  if (state === "conflict") return "근거 충돌";
  if (state === "not_applicable") return "비적용";
  return state;
}

export function sourceQualityText(quality: string): string {
  return quality === "verified" ? "원문 검증됨" : "원문 확인 필요";
}

/** Claim sentence verification is distinct from element-evidence verification. */
export function claimSourceText(claim: ResultClaim): string {
  return (claim.source_status?.status ?? claim.source_quality) === "verified" ? "주장 원문 · 검증됨" : "주장 원문 · 확인 필요";
}

export function claimProcessingFailed(claim: ResultClaim): boolean {
  return claim.pipeline_status === "failed" || [claim.hold_reason, claim.hold_reason_detail,
    claim.provisional_grade?.reason, claim.provisional_grade?.hold_reason_detail,
    ...(claim.provisional_grade?.hold_reasons ?? []), ...(claim.cf6_hold_reasons ?? [])]
    .some(code => typeof code === "string" && isProcessingFailure(code));
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
  const label = decision.label ? ({ SUBSTANTIATED: "입증 요건 충족", INCOMPLETE: "일부 요건 충족", UNSUBSTANTIATED: "근거 부족" } as Record<string, string>)[decision.label] ?? (/[가-힣]/.test(decision.label) ? decision.label : "판정 상세 확인") : "";
  return `${decision.evidence_grade}${label ? ` (${label})` : ""}`;
}

export function claimHoldReasons(claim: ResultClaim): string[] {
  if (claimProcessingFailed(claim)) return [holdReasonText("PROCESSING_FAILED")];
  const provisional = claim.provisional_grade;
  const detail = [
    ...(provisional?.hold_reasons ?? []), provisional?.hold_reason_detail, claim.hold_reason_detail,
    claim.hold_reason, provisional?.reason,
  ].filter((reason): reason is string => typeof reason === "string" && reason.trim().length > 0);
  return [...new Set(detail.map(reason => {
    if (/원문 위치 확인/.test(reason) && (claim.source_status?.status ?? claim.source_quality) === "verified") return "요소 근거·연결의 원문 위치 추가 확인 필요";
    return holdReasonText(reason);
  }))];
}

export function holdEvidenceNoteText(note?: string): string | undefined {
  if (note === "근거 없음은 이 패킷에 인정된 근거가 없다는 뜻이며 문서 전체 부재 판정이 아닙니다.") {
    return "확인되지 않은 요소는 현재 분석 범위에서 인정된 근거와 연결되지 않았습니다. 문서 전체의 근거 부재를 판정한 것은 아닙니다.";
  }
  return note;
}

const displayValue = (value: string | number | null | undefined) => value == null || String(value).trim() === "" ? "" : String(value).trim();

/** Only renders table context that the server explicitly included. Missing metadata stays hidden. */
export function tableObservationText(observation?: TableObservation | null): string | null {
  if (!observation) return null;
  const parents = [
    ...(Array.isArray(observation.parent_path) ? observation.parent_path : []),
    observation.parent_metric, observation.parent_row, observation.metric,
  ].map(value => typeof value === "string" ? value.trim() : "").filter(Boolean);
  const hierarchy = parents.filter((value, index) => parents.indexOf(value) === index).join(" › ");
  const year = displayValue(observation.year);
  const value = displayValue(observation.value);
  const unit = displayValue(observation.unit);
  const periodValue = [year && `${year}년`, value, unit].filter(Boolean).join(" ");
  const facility = displayValue(observation.facility_name ?? observation.facility);
  const column = displayValue(observation.column_name ?? observation.column_header ?? observation.value_column_name);
  const comparisons = (observation.comparisons ?? []).map(item => {
    const itemYear = displayValue(item.year);
    const itemValue = displayValue(item.value);
    const itemColumn = displayValue(item.column_name);
    return [itemColumn, itemYear && `${itemYear}년`, itemValue, itemValue && unit].filter(Boolean).join(" ");
  }).filter(Boolean);
  const verification = observation.verification_state === "verified" ? "표 행·값 연결 확인"
    : observation.verification_state === "conflict" ? "표 값·행 충돌"
      : observation.verification_state === "unreadable" ? "표 판독 불가"
        : observation.verification_state ? "표 행·값 연결 확인 필요" : "";
  const parts = [
    hierarchy && `지표 계층: ${hierarchy}`, periodValue, facility && `시설: ${facility}`, column && `열: ${column}`,
    comparisons.length && `비교: ${comparisons.join(" · ")}`, verification,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

export type ClaimOccurrenceGroup = { key: string; claims: ResultClaim[]; occurrenceCount: number };

/** Groups only IDs the server explicitly marks as duplicate occurrences. */
export function groupClaimOccurrences(claims: ResultClaim[]): ClaimOccurrenceGroup[] {
  const parents = new Map(claims.map(claim => [claim.claim_id, claim.claim_id]));
  const find = (id: string): string => {
    const parent = parents.get(id);
    if (!parent || parent === id) return id;
    const root = find(parent);
    parents.set(id, root);
    return root;
  };
  const union = (left: string, right: string) => {
    if (!parents.has(right)) return;
    const a = find(left), b = find(right);
    if (a !== b) parents.set(b, a);
  };
  for (const claim of claims) for (const id of claim.duplicate_occurrences ?? []) union(claim.claim_id, id);

  const grouped = new Map<string, ResultClaim[]>();
  for (const claim of claims) {
    const root = find(claim.claim_id);
    const group = grouped.get(root) ?? [];
    group.push(claim);
    grouped.set(root, group);
  }
  return [...grouped.values()].map(group => {
    const occurrenceIds = new Set(group.flatMap(claim => claim.duplicate_occurrences ?? []));
    group.forEach(claim => occurrenceIds.add(claim.claim_id));
    return { key: group[0].claim_id, claims: group, occurrenceCount: Math.max(group.length, occurrenceIds.size) };
  });
}

export function canShowFinalLabel(sourceVerified: boolean, decisionStatus: string | undefined, provisional = false): boolean {
  return sourceVerified && decisionStatus === "decided" && !provisional;
}

/** Prefer an existing public decision; SC1 is additive, never inferred from a range or element. */
export function confirmedDecision(claim: ResultClaim): ResultDecision | null {
  const grade = claim.confirmed_grade;
  const direct = claim.confirmation_source === "auto_direct_certification_c3_v1";
  const automatic = direct || claim.confirmation_source === "auto_unanimous_v1";
  const autoValid = !automatic || (claim.decision?.gap_ids?.length === 0 && !claim.decision.grade_range?.rule_gap &&
    !claim.hold_reason && /^[a-f0-9]{64}$/.test(claim.confirmation_policy_sha256 ?? "") &&
    claim.confirmed_label === claim.decision.label && !!claim.confirmed_label &&
    claim.decision.review_status === "auto_confirmed" && (!direct || !!claim.confirmation_basis));
  if (grade && grade === claim.decision?.evidence_grade && autoValid && canShowFinalLabel(claim.source_quality === "verified", claim.decision?.decision_status)) {
    return claim.decision;
  }
  const scoped = claim.sc1_confirmation;
  if (scoped?.evidence_grade && scoped.label && scoped.confirmation_basis === "analysis_scope" &&
    /^[a-f0-9]{64}$/.test(scoped.confirmation_policy_sha256 ?? "") && !claim.sc1_hold_reason &&
    !scoped.gap_ids?.length && !scoped.grade_range?.rule_gap &&
    canShowFinalLabel(claim.source_quality === "verified", scoped.decision_status)) return scoped;
  return null;
}

/** Confirmed grade and possible range are kept apart; a range never becomes a grade. */
export function gradeSummary(claim: ResultClaim): { confirmed: boolean; headline: string; note: string } {
  const decision = confirmedDecision(claim);
  if (decision) {
    if (decision === claim.sc1_confirmation) {
      const scope = claim.sc1_confirmation.confirmation_scope;
      const pages = scope?.inspected_pages;
      return { confirmed: true, headline: decisionLine(decision)!,
        note: [pages?.length ? `검사한 원본 쪽: ${pageRanges(pages)}` : "분석 범위 내 규칙엔진 판정",
          scope?.document_total_pages ? `전체 ${scope.document_total_pages}쪽` : ""].filter(Boolean).join(" · ") };
    }
    const direct = claim.confirmation_source === "auto_direct_certification_c3_v1";
    const automatic = direct || claim.confirmation_source === "auto_unanimous_v1";
    const prov = reviewStatusText(decision.review_status);
    return { confirmed: true, headline: decisionLine(decision)!, note: direct ? claim.confirmation_basis! : automatic ? "3/3 반복 판독 · 주장과 근거 원문 검증" : prov };
  }
  if (claim.cf6_status === "held") return { confirmed: false, headline: "판정 보류", note: "원문 근거와 판정 보류 사유를 확인해 주세요." };
  if (claim.provisional_grade) {
    const provisional = claim.provisional_grade;
    const range = provisional.grade_range;
    const open = range?.open_elements.map(id => `${id} ${elementLabels[id] ?? ""}`.trim()).join("·");
    const reasons = provisional.display_type === "hold" ? [] : claimHoldReasons(claim);
    const basis = provisional.grade_basis === "reachable_floor" ? "가능 범위 하한: 현재 확인된 최소 수준" : "";
    return { confirmed: false, headline: provisional.display_type === "hold" ? "판정 보류" : provisional.evidence_grade ? `추가 확인 필요 · ${provisional.evidence_grade}` : range ? `판정 보류 · 가능 범위 ${range.floor}–${range.ceiling}` : "판정 보류",
      note: [claim.lower_bound_display, basis, reasons.length ? `보류 사유: ${reasons.join(" · ")}` : provisional.reason ? holdReasonText(provisional.reason) : "",
        range ? `가능 범위 ${range.floor}–${range.ceiling} (미해결 요소: ${open || "없음"})` : ""].filter(Boolean).join(" · ") };
  }
  const range = claim.possible_grade_range;
  if (range) {
    const open = range.open_elements.map(id => `${id} ${elementLabels[id] ?? ""}`.trim()).join("·");
    return { confirmed: false, headline: "판정 보류", note: [claim.lower_bound_display, `가능 범위 ${range.floor}–${range.ceiling} (미해결 요소: ${open || "없음"}). 범위는 등급이 아니며 하한·상한만 알려 줍니다.`].filter(Boolean).join(" · ") };
  }
  return { confirmed: false, headline: "판정 보류", note: claim.hold_reason ? "판정이 보류되어 등급과 가능 범위가 없습니다." : "규칙 판정이 실행되지 않아 등급과 가능 범위가 없습니다." };
}

export function pageLabel(result: Pick<HostedResult, "page_map">, selectedPage: number): string {
  const original = result.page_map?.[selectedPage - 1];
  return original != null ? `p.${original}` : `선택본 p.${selectedPage}`;
}

export type ResultBucket = "confirmed" | "provisional" | "hold";
export type DisplayElement = { element_id: string; state: string; evidence_refs: SourceRefDto[]; votes?: number; total_replicas?: number };
export type ResultBrowserItem = {
  claim: ResultClaim; page: string; track: string; confirmed: boolean; bucket: ResultBucket;
  grade: string | null; badge: string; elements: DisplayElement[];
};
export type ResultFilters = { queue: string; search: string; track: string; grade: string; status: string };

/** A display adapter, never a grader. Preserve server values and original claim/revision. */
export function resultBrowserItem(result: Pick<HostedResult, "page_map">, claim: ResultClaim): ResultBrowserItem {
  const decision = confirmedDecision(claim);
  const confirmed = decision !== null;
  const scoped = confirmed && decision === claim.sc1_confirmation;
  const cf6 = confirmed && decision.confirmation_basis === "cf6_element_agreement";
  const provisional = claim.cf6_status === "held" ? null : claim.provisional_grade;
  const grade = confirmed ? decision.evidence_grade : provisional?.display_type !== "hold" ? provisional?.evidence_grade ?? null : null;
  const bucket = confirmed ? "confirmed" : grade ? "provisional" : "hold";
  return {
    claim, confirmed, bucket, grade, track: claim.track ?? (cf6 ? claim.cf6_track : null) ?? claim.provisional_track ?? "unknown",
    page: result.page_map?.[claim.page_num - 1] != null ? pageLabel(result, claim.page_num)
      : Number.isInteger(claim.original_page_num) && claim.original_page_num > 0 ? `p.${claim.original_page_num}` : pageLabel(result, claim.page_num),
    badge: confirmed ? `${decisionLine(decision)}` : grade ? `추가 확인 필요 · ${grade}` : "판정 보류",
    elements: cf6 && claim.cf6_elements?.length ? claim.cf6_elements : scoped && claim.sc1_elements?.length ? claim.sc1_elements.map(element => ({
      element_id: element.element_id, state: element.state,
      evidence_refs: element.state === "present"
        ? (claim.elements.find(item => item.element_id === element.element_id && item.state === "present")
          ?? provisional?.elements?.find(item => item.element_id === element.element_id && item.state === "present"))?.evidence_refs ?? [] : [],
    })) : !confirmed && provisional?.elements?.length ? provisional.elements : claim.elements,
  };
}

/** A present tag alone does not establish verified evidence. */
export function resultElementText(claim: ResultClaim, element: DisplayElement): string {
  if ((confirmedDecision(claim) ?? claim.decision)?.excluded_elements?.includes(element.element_id)) return "비적용 (조건 미발생)";
  return elementStateText(element.state, claim.source_quality === "verified" && element.evidence_refs.some(ref => ref.verification_state === "verified") ? "verified" : "unverified");
}

export function matchesResultFilters(item: ResultBrowserItem, filter: ResultFilters): boolean {
  if (filter.queue !== "all" && item.bucket !== filter.queue) return false;
  if (filter.track !== "all" && item.track !== filter.track) return false;
  if (filter.grade.startsWith("E") && item.grade !== filter.grade) return false;
  if (filter.grade === "none" && item.grade) return false;
  if (filter.status !== "all" && item.bucket !== filter.status && !(filter.status === "source_unverified" && item.claim.source_quality !== "verified")) return false;
  const search = filter.search.trim().toLowerCase();
  return !search || `${item.claim.quote} ${item.page} ${item.claim.claim_id}`.toLowerCase().includes(search);
}

export function resultBrowserSummary(result: HostedResult) {
  const items = result.claims.map(claim => resultBrowserItem(result, claim));
  const times = Object.values(result.pipeline?.stage_seconds ?? {});
  return {
    total: items.length, verified: result.claims.filter(claim => claim.source_quality === "verified").length,
    confirmed: items.filter(item => item.bucket === "confirmed").length,
    provisional: items.filter(item => item.bucket === "provisional").length,
    hold: items.filter(item => item.bucket === "hold").length,
    grades: Object.fromEntries(["E3", "E2", "E1", "E0"].map(grade => [grade, items.filter(item => item.bucket === "provisional" && item.grade === grade).length])),
    confirmedGrades: Object.fromEntries(["E3", "E2", "E1", "E0"].map(grade => [grade, items.filter(item => item.bucket === "confirmed" && item.grade === grade).length])),
    stageSeconds: times.length && times.every(time => Number.isFinite(time) && time >= 0) ? times.reduce((sum, time) => sum + time, 0) : null,
  };
}

export function pageRanges(pages: number[]): string {
  const sorted = [...new Set(pages)].sort((a, b) => a - b);
  const ranges: string[] = [];
  for (let i = 0; i < sorted.length; i++) {
    const start = sorted[i];
    let end = start;
    while (sorted[i + 1] === end + 1) end = sorted[++i];
    ranges.push(start === end ? `${start}` : `${start}–${end}`);
  }
  return ranges.join(", ");
}

/** Failure pages are in the prepared document, just like claim.page_num. Never guess an original page. */
export function resultSourceFailures(result: HostedResult) {
  const extraction = result.pipeline?.extraction_outcome;
  const groups = new Map<string, { page: string; reason: string; sourceIds: string[]; pageFailure: boolean }>();
  const add = (page: number | undefined, code: string | undefined, sourceId?: string) => {
    const label = Number.isInteger(page) && page! > 0 ? pageLabel(result, page!) : "쪽 위치 미제공";
    const reason = holdReasonText(code ?? extraction?.code ?? "", result.hold_reason_details);
    const key = `${label}:${reason}`;
    const group = groups.get(key) ?? { page: label, reason, sourceIds: [], pageFailure: false };
    if (sourceId) group.sourceIds.push(sourceId);
    else group.pageFailure = true;
    groups.set(key, group);
  };
  extraction?.page_failures?.forEach(failure => add(failure.physical_page, failure.code ?? "CLAIM_PAGE_PARSE_FAILURE"));
  extraction?.failed_sources?.forEach(failure => add(failure.physical_page, failure.failure_code ?? "CLAIM_SOURCE_EXTRACTION_FAILURE", failure.source_id));
  return [...groups.values()];
}

export function resultScope(result: HostedResult, pageCount?: number, selectedPages?: number[]) {
  const pages = [...new Set(result.pipeline?.scope?.pages ?? selectedPages ?? result.page_map ?? [])].sort((a, b) => a - b);
  const total = Number.isInteger(pageCount) && pageCount! > 0 ? pageCount! : null;
  const claims = result.pipeline?.scope?.claim_pages ?? result.pipeline?.extraction_outcome?.selected_claim_pages?.flatMap(page => result.page_map[page - 1] ? [result.page_map[page - 1]] : []);
  const selected = new Set(pages);
  const unselected = total !== null ? Array.from({ length: total }, (_, index) => index + 1).filter(page => !selected.has(page)) : null;
  return { pages, total, claims, unselected };
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
    { state: "unknown", label: "인정된 근거 미연결(확인 필요)" },
    { state: "present", label: "근거 있음", disabled: refs.length === 0, hint: refs.length === 0 ? "연결할 원문 근거 후보가 없습니다" : undefined },
    { state: "absent", label: "근거 없음(검증된 검색 범위)", hint: missing?.search?.absence_admissible === false ? "검색 범위가 검증되지 않아 서버가 거부할 수 있습니다" : undefined },
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

/** One primary display reason per held claim; counts sum to the existing hold bucket. */
export function w18HoldReason(result: HostedResult, claim: ResultClaim): string | null {
  if (resultBrowserItem(result, claim).bucket !== "hold") return null;
  if (claimProcessingFailed(claim)) return "처리 오류로 보류";
  const provisional = claim.provisional_grade;
  const reasons = [claim.hold_reason, claim.hold_reason_detail, provisional?.hold_reason_detail,
    provisional?.reason, ...(provisional?.hold_reasons ?? []), claim.decision?.decision_status].filter((value): value is string => !!value);
  const text = reasons.join(" ");
  if ((claim.source_status?.status ?? claim.source_quality) !== "verified") return "주장 원문 미확인";
  if (/BODY_TABLE_CONFLICT|TEXT_TABLE_CONFLICT|본문·표.*충돌/.test(text)) return "본문·표 수치 충돌";
  if (/CONSENSUS_UNRESOLVED|REPLICA_DISAGREEMENT|TAGGING_DISAGREEMENT|태깅 불일치/.test(text)) return "3회 판단 불일치";
  if (/REPLICA_HEADER_UNRESOLVED|REPLICA_UNRESOLVED|replica 미해결/.test(text)) return "반복 판독 미완료";
  if (/OUTSIDE_ANALYSIS_SCOPE|OUT_OF_SCOPE|분석 범위 밖/.test(text)) return "분석 범위 밖";
  if (/PRELIMINARY_TAGS_UNRESOLVED|트랙 미합의/.test(text)) return "주장 유형 미합의";
  if (/SOURCE_VALIDATION|SOURCE_ATTESTATION|원문 위치 확인/.test(text)) return "근거 원문·위치 추가 확인";
  if (/blocked_rule_gap|규칙 미정/.test(text)) return "판정 규칙 미정";
  if (/blocked_evidence|EVIDENCE_INSUFFICIENT|미확인 요소/.test(text)) return "요소 근거 부족·미연결";
  if (/RELATION_TAGS_UNRESOLVED|linkage_unclear/.test(text)) return "주장·근거 연결 미확인";
  if (/UNEXPECTED|INPUT_INVALID|PROCESSING_UNCERTAIN/.test(text)) return "분석 처리 오류";
  const code = reasons[0] ?? result.pipeline?.reason ?? result.hold_reasons[0];
  if (code === "R108_FAILED") return "분석 처리 중단";
  if (code) {
    const detail = result.hold_reason_details?.find(item => item.code === code)?.message;
    if (detail && /[가-힣]/.test(detail)) return detail;
    const translated = holdReasonText(code);
    if (translated !== code && /[가-힣]/.test(translated)) return translated;
  }
  return "보류 사유 미제공";
}

// Display-only short names; the full reason stays the filter key and goes in title/detail.
const holdLabels: [RegExp, string][] = [
  [/TIME_BUDGET|시간 한도/, "분석 시간 한도로 미처리"],
  [/BUDGET|예산/, "분석 예산 한도로 미처리"],
  [/처리 오류로 보류/, "처리 오류로 보류"],
  [/상세 사유는 제공되지 않았습니다/, "상세 사유 미제공"],
  [/사람 검토/, "사람 검토 대기"],
  [/원문 대조가 끝나지 않아/, "근거 원문·위치 추가 확인"],
  [/모델 판독이 합의되지 않아/, "3회 판단 불일치"],
  [/예비 분류\(트랙\)가 합의되지 않아/, "주장 유형 미합의"],
  [/관계가 정리되지 않아/, "주장·근거 연결 미확인"],
];
export function holdReasonLabel(reason: string): string {
  if (reason.length <= 16) return reason;
  return holdLabels.find(([pattern]) => pattern.test(reason))?.[1] ?? reason;
}

export function w18HoldBreakdown(result: HostedResult): { reason: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const claim of result.claims) {
    const reason = w18HoldReason(result, claim);
    if (reason) counts.set(reason, (counts.get(reason) ?? 0) + 1);
  }
  return [...counts].map(([reason, count]) => ({ reason, count })).sort((a, b) => b.count-a.count);
}

/** A short explanation based only on the same evidence shown in claim detail. */
export function claimEvidenceLine(result: HostedResult, claim: ResultClaim): string {
  const item = resultBrowserItem(result, claim);
  if (item.bucket === "hold") return w18HoldReason(result, claim) ?? "판정에 필요한 근거 확인";
  const verified = item.elements.filter(element => element.state === "present" && element.evidence_refs.some(ref => ref.verification_state === "verified"));
  const basis = verified.length ? `${verified.map(element => elementLabels[element.element_id] ?? element.element_id).join(" · ")} 근거 확인` : "연결된 근거와 요소 상태를 기준으로 판정";
  return item.bucket === "provisional" ? `추가 확인 필요 · ${basis}` : basis;
}
