import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { SourceViewer, type SourceOpenRequest } from "../../components/SourceViewer";
import { StatusBadge } from "../../components/StatusBadge";
import { PreliminaryClassification } from "./PreliminaryClassification";
import { ApiError, errorMessage, isSessionError, requestJson, type Session } from "../session/api";
import {
  ReviewWorkspace,
  type Review,
  type ReviewElement,
  type ReviewResolution,
  type ReviewSnapshot,
  type SourceRef,
} from "../reviews/ReviewWorkspace";
import { getElementLabel } from "../labels";

type Track = "goal" | "performance" | "management";
type ReviewStatus = "auto_confirmed" | "needs_review" | "human_confirmed" | "ai_delegated_confirmed";
type Decision = {
  decision_revision: number;
  tag_revision: number;
  decision_status: "decided" | "blocked_evidence" | "blocked_rule_gap" | "not_applicable" | "not_run";
  evidence_grade: "E0" | "E1" | "E2" | "E3" | null;
  label: "SUBSTANTIATED" | "INCOMPLETE" | "UNSUBSTANTIATED" | null;
  sublabel: "PERF" | "IMPL" | null;
  review_status: ReviewStatus;
  missing_elements: string[];
  rule_ids: string[];
  rule_pack_sha256: string;
  semantic_hash: string;
  gap_ids: string[];
  // Engine v3+: reachable ladder grades while evidence is unresolved; not a grade.
  grade_range?: { floor: Grade; ceiling: Grade; open_elements: string[] } | null;
};
type Grade = "E0" | "E1" | "E2" | "E3";
type ClaimSummary = {
  claim_id: string;
  page_num: number;
  quote: string;
  track: Track | null;
  topic_ids: string[];
  decision: Decision | null;
  revision: number;
};
type ClaimPage = { items: ClaimSummary[]; next_cursor: string | null; snapshot_epoch: number | null };
type Assurance = {
  status: "covered" | "not_covered" | "undetermined";
  level: "limited" | "reasonable" | "none" | null;
  provider: string | null;
  statement_id: string | null;
  metric_match: "yes" | "no" | "unknown";
  period_match: "yes" | "no" | "unknown";
  boundary_match: "yes" | "no" | "unknown";
  evidence_refs: SourceRef[];
};
type FieldAgreement = {
  field_id: string;
  status: "agreed" | "conflict" | "unresolved";
  replicate_values: unknown[];
};
type ReviewCandidate = {
  source_ref: SourceRef;
  status: "candidate" | "unverified" | "unconfirmed";
  reason: string | null;
};
type ReviewProjection = {
  schema_version: 1;
  candidate_snippets: string[];
  blocked_reason: string | null;
  blocked_action: string | null;
  field_agreements: FieldAgreement[];
  raw_candidates?: ReviewCandidate[];
};
// Optional review context; P6 stays on hold without a same-scope table observation.
type ReviewedContextConsidered = { source_ref: SourceRef; status: string };
type ReviewedContext = {
  origin: "ai_delegated";
  dimensions: {
    facility: SourceRef;
    reporting_period: SourceRef;
    metric: SourceRef;
    value: SourceRef;
    unit: SourceRef;
  };
  numeric_check: {
    status: "needs_review";
    reason: "no_comparable_table_observation";
    considered: ReviewedContextConsidered[];
  };
};
type ClaimDetail = {
  claim: ClaimSummary;
  source_refs: SourceRef[];
  elements: ReviewElement[];
  assurance: Assurance;
  replicate_request_ids: string[];
  packet_sha256: string | null;
  tag_status?: "tagged" | "untagged" | null;
  suggestion: string | null;
  basis_refs: Array<{
    standard: string;
    clause: string | null;
    summary: string;
    verification_status: "verified" | "unverified" | "unlicensed";
  }>;
  rulepack_approved_by?: string | null;
  review_projection?: ReviewProjection | null;
  reviewed_context?: ReviewedContext | null;
  submitted_reviews?: Array<{
    reference_sha256: string;
    external_claim_id: string;
    origin: "data_manager_submission" | "ai_corrected_submission";
    claim_source_quality: string;
    tables: Record<"claims" | "elements" | "numeric" | "assurance", Array<Record<string, string>>>;
  }>;
};
type LoadState = "loading" | "ready" | "pending" | "error";

const submissionFieldLabels: Record<string, string> = {
  claim_id: "제출 주장 ID", document_id: "제출 문서 ID", annotation_id: "주석 ID",
  physical_page: "PDF 쪽", quote: "제출 인용문", statement: "주장 내용", track: "주장 유형",
  topic: "주제", claim_year: "주장 연도", year: "자료 연도", notes: "검토 메모",
  annotation_status: "제출 파일의 검토 표시 (앱 승인 아님)", annotator: "제출 파일의 작성자",
  adjudicator: "제출 파일의 확정 검토자", element_id: "입증 항목", state: "제출된 상태",
  evidence_document_id: "근거 문서 ID", binding_reason: "근거 연결 사유",
  searched_scope: "검색한 범위", boundary: "조직·사업장 범위", unit: "단위",
  table_id: "표", row: "행", column: "열", metric: "지표", value_raw: "원문 수치",
  value_decimal: "계산용 수치", multiplier: "단위 배율", scope2_basis: "Scope 2 산정 기준",
  footnote_quote: "각주", expected_check: "예상 비교 결과 (검토안)", reason: "판단 사유",
  expected_status: "예상 보증 연결 (검토안)", provider: "보증 기관", standard: "보증 기준",
  level: "보증 수준", period_start: "대상 기간 시작", period_end: "대상 기간 끝",
  entities: "보증 대상 조직", metrics: "보증 대상 지표", exclusions: "제외 사항",
};

type CommonProps = {
  apiBase?: string;
  csrfToken: string;
  tenantKey: string;
  session: Session;
  runId: string;
  onSessionInvalid: () => void;
  onDataChanged: () => void;
};
export type ClaimWorkspaceProps = CommonProps & { claimId?: string };

const MAX_SNAPSHOT_PAGES = 20;
const elementIds: Record<Track, string[]> = {
  goal: Array.from({ length: 8 }, (_, index) => `G${index + 1}`),
  performance: Array.from({ length: 6 }, (_, index) => `P${index + 1}`),
  management: Array.from({ length: 6 }, (_, index) => `M${index + 1}`),
};

function completeElements(track: Track, elements: ReviewElement[]): ReviewElement[] {
  return elementIds[track].map(element_id => elements.find(element => element.element_id === element_id) ?? {
    element_id,
    state: "unknown",
    evidence_refs: [],
    normalized_value: null,
    credited_from: null,
    reason_code: null,
  });
}

async function allPages<T>(url: string, signal: AbortSignal): Promise<T[]> {
  const items: T[] = [];
  let cursor: string | null = null;
  for (let page = 0; page < MAX_SNAPSHOT_PAGES; page += 1) {
    const target = new URL(url, window.location.origin);
    target.searchParams.set("limit", "100");
    if (cursor) target.searchParams.set("cursor", cursor);
    const result = await requestJson<{ items: T[]; next_cursor: string | null }>(target.toString(), { signal });
    items.push(...result.items);
    cursor = result.next_cursor;
    if (!cursor) return items;
  }
  throw new Error("목록이 브라우저 검토 한도를 초과했습니다. 필터로 범위를 줄여 주세요.");
}

function pending(error: unknown): boolean {
  return error instanceof ApiError && error.status === 409;
}

const pendingDecisionText: Record<Exclude<Decision["decision_status"], "decided">, string> = {
  blocked_evidence: "미판정 · 입증 요소 확인 필요",
  blocked_rule_gap: "미판정 · 규칙 적용 결과가 갈리거나 규칙집에 정한 기준이 없음",
  not_applicable: "적용 제외",
  not_run: "아직 판정하지 않음",
};

const reviewStatusText: Record<ReviewStatus, string> = {
  auto_confirmed: "자동 확인",
  needs_review: "검토 필요",
  human_confirmed: "사람 확인",
  ai_delegated_confirmed: "AI 검토(위임·사람 아님)",
};

const basisVerificationText: Record<ClaimDetail["basis_refs"][number]["verification_status"], string> = {
  verified: "검증된 기준",
  unverified: "미검증 기준(원문 대조 전)",
  unlicensed: "라이선스 미확인 기준",
};

const elementStateText: Record<ReviewElement["state"], string> = {
  present: "충족(present)",
  absent: "결여(absent)",
  unknown: "미상(unknown)",
  conflict: "상충(conflict)",
  not_applicable: "적용 제외(not_applicable)",
};

const trackText: Record<Track, string> = {
  goal: "목표형",
  performance: "성과형",
  management: "관리체계형",
};

const fieldAgreementText: Record<FieldAgreement["status"], string> = {
  agreed: "합의됨",
  conflict: "상충",
  unresolved: "미해결",
};

const reviewedDimensionText: Record<keyof ReviewedContext["dimensions"], string> = {
  facility: "사업장",
  reporting_period: "보고기간",
  metric: "지표",
  value: "값",
  unit: "단위",
};

// A considered comparison lying outside the reviewed section is excluded from
// the reviewed-context read; surface only its count so the hold reason stays honest.
const OUTSIDE_REVIEWED_SECTION = "outside_reviewed_section";

function decisionText(decision: Decision | null): string {
  if (!decision) return "판정 미확정 · 상세 확인";
  if (decision.decision_status !== "decided") {
    const range = decision.grade_range;
    const pending = pendingDecisionText[decision.decision_status];
    return range ? `${pending} · 가능 범위 ${range.floor}~${range.ceiling}` : pending;
  }
  return `${decision.evidence_grade} · ${decision.label}${decision.sublabel ? ` (${decision.sublabel})` : ""}`;
}

function decisionTone(decision: Decision | null): "neutral" | "success" | "warning" | "danger" {
  if (!decision) return "warning";
  if (decision.decision_status === "not_applicable") return "neutral";
  if (decision.decision_status !== "decided") return "warning";
  if (decision.evidence_grade === "E3") return "success";
  return decision.evidence_grade === "E0" ? "danger" : "warning";
}

export function ReviewedContextSection({ context }: { context: ReviewedContext }) {
  const dimensionKeys = ["facility", "reporting_period", "metric", "value", "unit"] as const;
  const excludedCount = context.numeric_check.considered.filter(
    item => item.status === OUTSIDE_REVIEWED_SECTION,
  ).length;
  return <section aria-labelledby="reviewed-context-heading">
    <h3 id="reviewed-context-heading">AI 검토 사업장 맥락 (위임·사람 확정 아님)</h3>
    <p>사용자가 위임한 AI 검토 결과입니다. 아래 원문 인용과 쪽을 함께 확인하세요.</p>
    <dl>
      {dimensionKeys.map(key => {
        const ref = context.dimensions[key];
        return <div key={key}>
          <dt>{reviewedDimensionText[key]}</dt>
          <dd>{ref.page_num}쪽 · “{ref.quote}”</dd>
        </div>;
      })}
    </dl>
    <p>P6 수치 대조: 검토 필요 · 동일 사업장·기간의 비교 근거가 없습니다.</p>
    <p role="status">비교 가능한 표 근거를 연결해야 수치 일치 여부를 판단할 수 있습니다.</p>
    {excludedCount > 0
      ? <p>검토 구간 밖으로 제외된 비교 후보: {excludedCount}건</p>
      : null}
  </section>;
}

function RawCandidatesList({ candidates, onSourceOpen }: { candidates: ReviewCandidate[]; onSourceOpen: (source: SourceRef) => void }) {
  if (candidates.length === 0) return <p>표시할 근거 후보가 아직 없습니다.</p>;
  return <ul>{candidates.map((cand, index) => <li key={`${cand.source_ref.source_id}:${index}`}>
    <p><strong>원문 {cand.source_ref.page_num}쪽</strong> · {cand.status === "unverified" ? "미검증 근거 후보" : "검토 후보 · 항목 연결 미확정"}{cand.reason && cand.status !== "unverified" ? ` (${cand.reason})` : ""}</p>
    <p>{cand.source_ref.quote}</p>
    <button type="button" onClick={() => onSourceOpen(cand.source_ref)}>원문 {cand.source_ref.page_num}쪽 보기</button>
  </li>)}</ul>;
}

export function PartialClaimSummary({
  decision,
  elements,
  rawCandidates,
  projection,
  onSourceOpen,
}: {
  decision: Decision | null;
  elements: ReviewElement[];
  rawCandidates: ReviewCandidate[];
  projection?: ReviewProjection | null;
  onSourceOpen: (source: SourceRef) => void;
}) {
  const isHeld = !decision || decision.decision_status === "blocked_evidence" || decision.decision_status === "blocked_rule_gap" || decision.decision_status === "not_run";
  if (!isHeld) return null;

  const presentElements = elements.filter(e => e.state === "present");
  const missingLabels = decision?.missing_elements?.map(getElementLabel) ?? [];

  const reason = "현재까지 추출된 내용입니다. 최종 판정이나 사람 검토 완료를 뜻하지 않습니다.";
  const action = projection?.blocked_action || "검토 후보의 원문과 항목 연결을 확인해 주세요. 확인되지 않은 항목은 미확정 상태로 유지됩니다.";

  const elementCandidates: ReviewCandidate[] = elements
    .filter(e => e.state === "unknown" || e.state === "conflict")
    .flatMap(e => e.evidence_refs.map(ref => ({
      source_ref: ref,
      status: "unconfirmed",
      reason: `${getElementLabel(e.element_id)} 후보 (${elementStateText[e.state]})`
    })));


  return (
    <section aria-labelledby="partial-summary-heading" style={{ padding: "16px", background: "#f8f9fa", border: "1px solid #dee2e6", marginBottom: "24px", borderRadius: "4px" }}>
      <h3 id="partial-summary-heading">검토 초안</h3>
      <p role="status">
        <strong>상태 및 조치:</strong> {reason}
        {missingLabels.length > 0 ? ` 누락된 입증 요소(${missingLabels.join(", ")})를 보완하기 위해 ` : " "}
        {action}
      </p>

      <h4>추출된 항목 (present)</h4>
      {presentElements.length > 0 ? (
        <ul>
          {presentElements.map(e => (
            <li key={e.element_id}>
              {getElementLabel(e.element_id)}
              {e.normalized_value !== null ? ` (추출 값: ${e.normalized_value})` : ""}
            </li>
          ))}
        </ul>
      ) : <p>표시할 추출 값이 아직 없습니다.</p>}

      <h4>검토 후보 · 항목 연결 확인 필요</h4>
      <RawCandidatesList candidates={elementCandidates} onSourceOpen={onSourceOpen} />
      {rawCandidates.length ? <details>
        <summary>추가 검색 후보 {rawCandidates.length}건 · 원문 검증 필요</summary>
        <p>주변 문맥을 찾기 위한 검색 결과입니다. 해당 항목의 근거로 채택된 것은 아닙니다.</p>
        <RawCandidatesList candidates={rawCandidates} onSourceOpen={onSourceOpen} />
      </details> : null}
    </section>
  );
}

export function ClaimWorkspace(props: ClaimWorkspaceProps) {
  return props.claimId ? <ClaimDetailView {...props} claimId={props.claimId} /> : <ClaimList {...props} />;
}

function ClaimList({ apiBase = "", tenantKey, runId, onSessionInvalid }: ClaimWorkspaceProps) {
  const [search, setSearch] = useSearchParams();
  const [items, setItems] = useState<ClaimSummary[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [message, setMessage] = useState("");
  const [moreBusy, setMoreBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const filterKey = ["track", "grade", "review_status"].map(key => search.get(key) ?? "").join(":");

  const load = useCallback(async (nextCursor?: string) => {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    if (nextCursor) setMoreBusy(true); else setState("loading");
    setMessage("");
    const target = new URL(`${apiBase}/v1/runs/${runId}/claims`, window.location.origin);
    target.searchParams.set("limit", "50");
    for (const key of ["track", "grade", "review_status"]) {
      const value = search.get(key);
      if (value) target.searchParams.set(key, value);
    }
    if (nextCursor) target.searchParams.set("cursor", nextCursor);
    try {
      const page = await requestJson<ClaimPage>(target.toString(), { signal: request.signal });
      if (request.signal.aborted) return;
      setItems(current => nextCursor ? [...current, ...page.items] : page.items);
      setCursor(page.next_cursor);
      setState("ready");
    } catch (reason: unknown) {
      if (request.signal.aborted) return;
      if (isSessionError(reason)) return onSessionInvalid();
      setState(pending(reason) ? "pending" : "error");
      setMessage(pending(reason)
        ? "주장 태깅과 무결성 검사가 아직 완료되지 않았습니다. 잠시 후 다시 확인해 주세요."
        : errorMessage(reason, "주장 목록을 불러오지 못했습니다."));
    } finally {
      if (!request.signal.aborted) setMoreBusy(false);
    }
  }, [apiBase, filterKey, onSessionInvalid, runId, search]);

  useEffect(() => {
    setItems([]);
    setCursor(null);
    void load();
    return () => controller.current?.abort();
  }, [load, tenantKey]);

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(search);
    if (value) next.set(key, value); else next.delete(key);
    next.delete("cursor");
    setSearch(next);
  }

  return <section aria-labelledby="claims-heading">
    <h1 id="claims-heading">주장 검토 목록</h1>
    <div aria-label="주장 필터" style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
      <label>트랙 <select value={search.get("track") ?? ""} onChange={event => setFilter("track", event.target.value)}>
        <option value="">전체</option><option value="goal">목표형</option><option value="performance">성과형</option><option value="management">관리체계형</option>
      </select></label>
      <label>등급 <select value={search.get("grade") ?? ""} onChange={event => setFilter("grade", event.target.value)}>
        <option value="">전체</option>{["E0", "E1", "E2", "E3"].map(grade => <option key={grade}>{grade}</option>)}
      </select></label>
      <label>검토 상태 <select value={search.get("review_status") ?? ""} onChange={event => setFilter("review_status", event.target.value)}>
        <option value="">전체</option><option value="auto_confirmed">자동 확인</option><option value="needs_review">검토 필요</option><option value="human_confirmed">사람 확인</option><option value="ai_delegated_confirmed">AI 검토(위임·사람 아님)</option>
      </select></label>
    </div>
    {state === "loading" ? <p role="status">주장 목록을 불러오는 중입니다.</p> : null}
    {state === "pending" ? <p role="status">{message}</p> : null}
    {state === "error" ? <p role="alert">{message} <button type="button" onClick={() => void load()}>다시 시도</button></p> : null}
    {state === "ready" && items.length === 0 ? <p>현재 필터에 해당하는 주장이 없습니다. 필터를 해제하거나 분석 진행 상태를 확인해 주세요.</p> : null}
    {items.length > 0 ? <table style={{ width: "100%" }}><thead><tr><th>쪽</th><th>주장</th><th>트랙</th><th>판정</th></tr></thead><tbody>
      {items.map(item => <tr key={item.claim_id}><td>{item.page_num}</td><td><Link to={`/runs/${runId}/claims/${item.claim_id}`}>{item.quote}</Link></td><td>{item.track ? trackText[item.track] : "미분류"}</td><td><StatusBadge label={decisionText(item.decision)} tone={decisionTone(item.decision)} /></td></tr>)}
    </tbody></table> : null}
    {cursor ? <button type="button" disabled={moreBusy} onClick={() => void load(cursor)} style={{ minHeight: 44 }}>{moreBusy ? "불러오는 중…" : "더 보기"}</button> : null}
  </section>;
}

function ClaimDetailView({ apiBase = "", csrfToken, tenantKey, session, runId, claimId = "", onSessionInvalid }: ClaimWorkspaceProps) {
  const [detail, setDetail] = useState<ClaimDetail | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [message, setMessage] = useState("");
  const [sourceOpen, setSourceOpen] = useState<SourceOpenRequest | null>(null);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setState("loading"); setMessage("");
    try {
      const value = await requestJson<ClaimDetail>(`${apiBase}/v1/runs/${runId}/claims/${claimId}`, { signal: controller.signal });
      if (controller.signal.aborted) return;
      setDetail(value); setState("ready");
    } catch (reason: unknown) {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) return onSessionInvalid();
      setState(pending(reason) ? "pending" : "error");
      setMessage(pending(reason) ? "이 주장의 태깅 결과가 아직 게시되지 않았습니다." : errorMessage(reason, "주장 상세를 불러오지 못했습니다."));
    }
  }, [apiBase, claimId, onSessionInvalid, runId]);

  useEffect(() => {
    setDetail(null);
    setSourceOpen(null);
    void load();
    return () => request.current?.abort();
  }, [load, tenantKey]);

  if (state === "loading") return <p role="status">주장 상세를 불러오는 중입니다.</p>;
  if (state === "pending") return <p role="status">{message}</p>;
  if (state === "error" || !detail) return <p role="alert">{message} <button type="button" onClick={() => void load()}>다시 시도</button></p>;
  const decision = detail.claim.decision;
  const untagged = detail.tag_status === "untagged" || detail.packet_sha256 === null;
  const track = detail.claim.track;
  const elements = track ? completeElements(track, detail.elements) : detail.elements;
  const projection = detail.review_projection;
  const isHeld = !decision || decision.decision_status === "blocked_evidence" || decision.decision_status === "blocked_rule_gap" || decision.decision_status === "not_run";
  return <section aria-labelledby="claim-heading">
    <h1 id="claim-heading">주장 상세</h1>
    <p><Link to={`/runs/${runId}/claims`}>주장 목록으로</Link></p>
    {untagged ? <p role="status">태깅 결과가 게시되지 않은 주장입니다. 처리 대기, 원문 검증 또는 주장 분류가 보류된 경우가 포함됩니다. 아래에서 원문을 확인할 수 있으며, 태깅 편집은 태그가 게시된 뒤에 가능합니다.</p> : null}
    <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 3fr) minmax(280px, 2fr)", gap: 24 }}>
      <SourceViewer key={`${tenantKey}:${runId}:${claimId}`} apiBase={apiBase} csrfToken={csrfToken} runId={runId} sources={detail.source_refs} openRequest={sourceOpen} onSessionInvalid={onSessionInvalid} />
      <section aria-labelledby="evidence-heading"><h2 id="evidence-heading">태깅과 판정</h2>
        <PartialClaimSummary decision={decision} elements={elements} rawCandidates={projection?.raw_candidates ?? []} projection={projection} onSourceOpen={source => setSourceOpen(current => ({ source, nonce: (current?.nonce ?? 0) + 1 }))} />
        <p>{detail.claim.quote}</p><p>트랙: {track ? trackText[track] : "미분류"}</p><p>판정: <StatusBadge label={decisionText(decision)} tone={decisionTone(decision)} /></p>{decision?.grade_range ? <p>가능 등급 범위: {decision.grade_range.floor} ~ {decision.grade_range.ceiling} (확정 등급 아님) · 확인하면 범위가 좁혀지는 요소: {decision.grade_range.open_elements.map(getElementLabel).join(", ")}</p> : null}
        {decision?.review_status ? <p>검토 상태: <StatusBadge label={reviewStatusText[decision.review_status]} tone={decision.review_status === "human_confirmed" ? "success" : "warning"} /></p> : null}
        {decision?.gap_ids.length ? <p>규칙 판정 보류(다음 규칙 항목이 갈리거나 정의되지 않음): {decision.gap_ids.join(", ")}</p> : null}
        {decision?.missing_elements.length ? <p>아직 충족이 확인되지 않은 요소: {decision.missing_elements.map(getElementLabel).join(", ")}</p> : null}
        {detail.rulepack_approved_by?.startsWith("ai-delegated-review:")
          ? <p role="status">이 판정에 쓰인 규칙집은 AI 프로젝트 검토(사람 전문가 승인 아님)로 활성화되었습니다.</p>
          : null}
        <details><summary>전체 항목 상세 ({elements.length}개)</summary>{elements.length === 0 ? <p>{untagged ? "게시된 요소가 없습니다. 원문 검증 또는 분류가 보류된 경우 추가 검토가 필요합니다." : "표시할 요소가 없습니다."}</p> : <ul>{elements.map(element => <li key={element.element_id}>
          {getElementLabel(element.element_id)}: {elementStateText[element.state]} · 근거 {element.evidence_refs.length}개
          {element.normalized_value !== null ? <p>값: {element.normalized_value}</p> : null}
          {element.evidence_refs.length ? <details data-element-evidence={element.element_id}>
            <summary>{element.element_id} 근거 내용</summary>
            <ul>{element.evidence_refs.map((source, index) => <li key={index}>
              <p>{source.page_num}쪽: {source.quote}</p>
              <button type="button" onClick={() => setSourceOpen(current => ({ source, nonce: (current?.nonce ?? 0) + 1 }))}>
                {element.element_id} 근거 {index + 1} 원문 {source.page_num}쪽 보기
              </button>
            </li>)}</ul>
          </details> : null}
        </li>)}</ul>}</details>
        <h3>보증 연결</h3>{detail.assurance.status === "undetermined" ? <p>보증 범위를 확인할 수 없습니다. 보고서 전체가 보증되었다고 간주하지 않습니다.</p> : <p>{detail.assurance.status === "covered" ? "보증 범위 안" : "보증 범위 밖"} · {detail.assurance.level ?? "수준 미확인"} · {detail.assurance.provider ?? "기관 미확인"}</p>}
        {detail.suggestion ? <><h3>수정 제안</h3><p>{detail.suggestion}</p></> : null}
        <h3>기준 근거</h3>{detail.basis_refs.length ? <ul>{detail.basis_refs.map((basis, index) => <li key={`${basis.standard}:${basis.clause}:${index}`}>{basis.standard} {basis.clause ?? "조항 미확정"}: {basis.summary} ({basisVerificationText[basis.verification_status]})</li>)}</ul> : <p>표시할 검증된 기준 근거가 없습니다.</p>}
        {detail.reviewed_context && detail.reviewed_context.origin === "ai_delegated"
          ? <ReviewedContextSection context={detail.reviewed_context} />
          : null}
        <PreliminaryClassification apiBase={apiBase} runId={runId} claimId={claimId} untagged={untagged} session={session} onSessionInvalid={onSessionInvalid} />
        {detail.submitted_reviews?.length ? <section aria-labelledby="submitted-reviews-heading">
          <h3 id="submitted-reviews-heading">제출자료 검토안 · 판정 미반영</h3>
          <p>제출자료의 상태와 수치는 비교·검토용입니다. 확정 태깅, 원문 검증 결과, 최종 판정 또는 사람 정답 승인을 뜻하지 않습니다.</p>
          {detail.submitted_reviews.map(reference => <article key={reference.reference_sha256}>
            <h4>{reference.external_claim_id} · {reference.origin === "ai_corrected_submission" ? "AI가 수정한 제출자료" : "데이터 관리자 제출자료"}</h4>
            <p>연결 당시 주장 원문 검증: {reference.claim_source_quality === "verified" ? "확인됨 (개별 제출 근거는 별도 검토 필요)" : "보류"}</p>
            {(["claims", "elements", "numeric", "assurance"] as const).map(name => <details key={name}>
              <summary>{{ claims: "주장", elements: "입증 요소", numeric: "수치 비교 입력", assurance: "보증 연결" }[name]} ({reference.tables[name].length}행)</summary>
              {reference.tables[name].length ? reference.tables[name].map((row, index) => <dl key={index}>
                {Object.entries(row).filter(([, value]) => value !== "").map(([key, value]) => <div key={key}><dt>{submissionFieldLabels[key] ?? key}</dt><dd>{value}</dd></div>)}
              </dl>) : <p>제출된 행이 없습니다.</p>}
            </details>)}
            <details><summary>검토안 식별자</summary><code>{reference.reference_sha256}</code></details>
          </article>)}
        </section> : null}
        {projection && projection.schema_version === 1 ? <section aria-labelledby="review-projection-heading"><h3 id="review-projection-heading">모델 태깅 당시 후보 (미확정)</h3>
          <p role="status">모델 태깅 당시의 미확정 기록입니다. 현재 검토 결과는 위의 태깅과 판정에 표시됩니다. 후보를 채택하려면 원문 검증을 통과해야 합니다.</p>
          {projection.blocked_reason ? <p>태깅 당시 보류 사유: {projection.blocked_reason}</p> : null}
          {projection.blocked_action ? <p>태깅 당시 안내: {projection.blocked_action}</p> : null}
          {projection.candidate_snippets.length ? <ul>{projection.candidate_snippets.map((snippet, index) => <li key={index}>{snippet}</li>)}</ul> : null}
          {projection.field_agreements.length ? <details><summary>모델 응답 필드 상세 ({projection.field_agreements.length}개)</summary>
            <ul>{projection.field_agreements.map(field => <li key={field.field_id}>{field.field_id}: {fieldAgreementText[field.status]} ({field.replicate_values.map(value => typeof value === "string" ? value : JSON.stringify(value)).join(" / ")})</li>)}</ul>
          </details> : null}
          {projection.raw_candidates && projection.raw_candidates.length && !isHeld ? <section aria-labelledby="raw-candidates-heading">
            <h4 id="raw-candidates-heading">미검증 근거 후보</h4>
            <p role="status">원문 검색으로 발견된 미검증 근거 후보입니다. 정식 근거로 채택되지 않았으며 판정 등급에 반영되지 않습니다.</p>
            <RawCandidatesList candidates={projection.raw_candidates} onSourceOpen={source => setSourceOpen(current => ({ source, nonce: (current?.nonce ?? 0) + 1 }))} />
          </section> : null}
        </section> : null}
        <details><summary>재현성 식별자</summary>{detail.packet_sha256 ? <p>Evidence packet: <code>{detail.packet_sha256}</code></p> : <p>Evidence packet: 태깅 전 (없음)</p>}{detail.replicate_request_ids.length ? <ul>{detail.replicate_request_ids.map(id => <li key={id}><code>{id}</code></li>)}</ul> : <p>재현 요청 식별자가 없습니다.{untagged ? " 태그가 게시되면 여기에 표시됩니다." : ""}</p>}</details>
      </section>
    </div>
  </section>;
}

const reviewQueueStatusText: Record<Review["status"], string> = {
  open: "검토 대기",
  resolved: "검토 완료",
  superseded: "대체됨",
};

export function ReviewQueueWorkspace({ apiBase = "", tenantKey, session, runId, onSessionInvalid, onDataChanged, localSynthetic = false }: CommonProps & { localSynthetic?: boolean }) {
  const [sourceOpen, setSourceOpen] = useState<SourceOpenRequest | null>(null);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [claims, setClaims] = useState<ClaimSummary[]>([]);
  const [selected, setSelected] = useState("");
  const [snapshot, setSnapshot] = useState<ReviewSnapshot | null>(null);
  const [detail, setDetail] = useState<ClaimDetail | null>(null);
  const [state, setState] = useState<LoadState>("loading");
  const [message, setMessage] = useState("");
  const controller = useRef<AbortController | null>(null);
  const allowed = session.role === "reviewer" || session.role === "admin";

  const latest = useCallback(async (reviewId: string, signal: AbortSignal): Promise<{ snapshot: ReviewSnapshot; detail: ClaimDetail }> => {
    const [freshReviews, freshClaims] = await Promise.all([
      allPages<Review>(`${apiBase}/v1/runs/${runId}/reviews`, signal),
      allPages<ClaimSummary>(`${apiBase}/v1/runs/${runId}/claims`, signal),
    ]);
    const review = freshReviews.find(item => item.review_id === reviewId);
    if (!review || !freshClaims.some(item => item.claim_id === review.claim_id)) throw new Error("최신 검토 항목을 찾을 수 없습니다.");
    const claimDetail = await requestJson<ClaimDetail>(`${apiBase}/v1/runs/${runId}/claims/${review.claim_id}`, { signal });
    if (!claimDetail.claim.track) throw new Error("트랙이 미확정되어 검토 편집기를 열 수 없습니다.");
    return { snapshot: { review, track: claimDetail.claim.track, elements: completeElements(claimDetail.claim.track, claimDetail.elements), headTagRevision: claimDetail.claim.decision?.tag_revision ?? review.base_tag_revision }, detail: claimDetail };
  }, [apiBase, runId]);

  const load = useCallback(async (preferred?: string) => {
    controller.current?.abort();
    const request = new AbortController();
    controller.current = request;
    setState("loading"); setMessage(""); setSourceOpen(null);
    try {
      const [nextReviews, nextClaims] = await Promise.all([
        allPages<Review>(`${apiBase}/v1/runs/${runId}/reviews`, request.signal),
        allPages<ClaimSummary>(`${apiBase}/v1/runs/${runId}/claims`, request.signal),
      ]);
      if (request.signal.aborted) return;
      setReviews(nextReviews); setClaims(nextClaims);
      const id = preferred && nextReviews.some(item => item.review_id === preferred) ? preferred : nextReviews.find(item => item.status === "open")?.review_id ?? nextReviews[0]?.review_id ?? "";
      setSelected(id);
      if (!id) { setSnapshot(null); setDetail(null); setState("ready"); return; }
      const value = await latest(id, request.signal);
      if (request.signal.aborted) return;
      setSnapshot(value.snapshot); setDetail(value.detail); setState("ready");
    } catch (reason: unknown) {
      if (request.signal.aborted) return;
      if (isSessionError(reason)) return onSessionInvalid();
      setState(pending(reason) ? "pending" : "error");
      setMessage(pending(reason) ? "검토 큐와 태깅 스냅샷이 아직 준비되지 않았습니다." : errorMessage(reason, "검토 큐를 불러오지 못했습니다."));
    }
  }, [apiBase, latest, onSessionInvalid, runId]);

  useEffect(() => {
    if (allowed) void load(); else setState("ready");
    return () => controller.current?.abort();
  }, [allowed, load, tenantKey]);

  async function choose(reviewId: string) {
    setSelected(reviewId); setState("loading"); setMessage(""); setSourceOpen(null);
    const request = new AbortController();
    controller.current?.abort(); controller.current = request;
    try {
      const value = await latest(reviewId, request.signal);
      if (!request.signal.aborted) { setSnapshot(value.snapshot); setDetail(value.detail); setState("ready"); }
    } catch (reason: unknown) {
      if (request.signal.aborted) return;
      if (isSessionError(reason)) return onSessionInvalid();
      setState(pending(reason) ? "pending" : "error"); setMessage(errorMessage(reason, "검토 항목을 불러오지 못했습니다."));
    }
  }

  function resolved(_result: ReviewResolution) {
    onDataChanged();
    void load(selected);
  }

  if (!allowed) return <section><h1>태깅 검토</h1><p role="alert">검토자 또는 관리자 권한이 필요합니다. 가짜 승인 화면은 제공하지 않습니다.</p></section>;
  // Claims that are undecided and not yet on the review queue (no Review row and
  // no decided Decision) are hidden by the queue alone. Surface them read-only so
  // a reviewer can open the existing claim detail for source and hold reason. The
  // set spans several blocked states (e.g. preliminary track unresolved, source
  // span unverified), so the copy stays neutral and does not assert a single cause.
  const reviewedClaimIds = new Set(reviews.map(review => review.claim_id));
  const unregistered = claims.filter(claim =>
    !reviewedClaimIds.has(claim.claim_id) &&
    (claim.decision === null || claim.decision.decision_status !== "decided"));
  return <section aria-labelledby="review-queue-heading"><h1 id="review-queue-heading">검토 큐</h1>
    {state === "loading" ? <p role="status">최신 주장과 검토 큐 스냅샷을 불러오는 중입니다.</p> : null}
    {state === "pending" ? <p role="status">{message}</p> : null}
    {state === "error" ? <p role="alert">{message} <button type="button" onClick={() => void load(selected)}>다시 시도</button></p> : null}
    {state === "ready" && reviews.length === 0 && unregistered.length === 0 ? <p>현재 검토 큐가 비어 있습니다.</p> : null}
    {reviews.length ? <nav aria-label="검토 항목"><ul>{reviews.map(review => <li key={review.review_id}><button type="button" aria-current={selected === review.review_id ? "true" : undefined} onClick={() => void choose(review.review_id)}>{claims.find(claim => claim.claim_id === review.claim_id)?.quote ?? review.claim_id} · {reviewQueueStatusText[review.status]}</button></li>)}</ul></nav> : null}
    {state === "ready" && unregistered.length ? <section aria-labelledby="unregistered-heading"><h2 id="unregistered-heading">검토 큐에 아직 오르지 않은 미판정 주장 ({unregistered.length}건)</h2>
      <p role="status">아직 검토 항목으로 등록되지 않은 미판정 주장입니다. 상세에서 원문과 처리 상태·보류 사유를 확인하세요. 아직 태깅 검토에서 편집할 수 없습니다.</p>
      <ul>{unregistered.map(claim => <li key={claim.claim_id}>{claim.page_num}쪽 · <Link to={`/runs/${runId}/claims/${claim.claim_id}`}>{claim.quote}</Link></li>)}</ul>
    </section> : null}
    {state === "ready" && snapshot && detail ? <ReviewWorkspace key={`${snapshot.review.review_id}:${snapshot.review.revision}`} {...snapshot} session={session}
      sourceChoices={detail.source_refs} loadLatest={async () => (await latest(snapshot.review.review_id, new AbortController().signal)).snapshot}
      onResolved={resolved} onSourceOpen={source => setSourceOpen(current => ({ source, nonce: (current?.nonce ?? 0) + 1 }))} localSynthetic={localSynthetic} /> : null}
    {state === "ready" && detail && snapshot ? <section aria-label="검토 원문">
      <SourceViewer key={`${tenantKey}:${runId}:${selected}`} apiBase={apiBase} csrfToken={session.csrf_token} runId={runId}
        sources={[...new Map([...detail.source_refs, ...snapshot.elements.flatMap(element => element.evidence_refs)]
          .map(source => [`${source.source_id}:${source.char_start}:${source.char_end}`, source])).values()]}
        openRequest={sourceOpen} onSessionInvalid={onSessionInvalid} />
      <Link to={`/runs/${runId}/claims/${detail.claim.claim_id}`}>원문 PDF와 전체 상세 열기</Link>
    </section> : null}
  </section>;
}
