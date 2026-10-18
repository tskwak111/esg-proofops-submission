import type { DocumentMetadata } from "./documentName";
// Hosted R108 control-plane client. Self-contained (no imports) so node tests can load it directly.
// Browser talks same-origin to `/hosted-api/*` (Vercel rewrite -> backend) so `__Host-` cookies stay first-party.

export type AnalysisBackend = "hosted" | "legacy" | "static";
export function resolveBackend(value: string | undefined): AnalysisBackend {
  return value === "hosted" || value === "static" ? value : "legacy";
}

export type Runtime = {
  analysis_backend: string; accept_new_runs: boolean; accept_uploads: boolean; live_analysis: boolean; live_disabled: boolean;
  upload_notice: string; consent_version: string; source_verifier?: string; profile?: string;
  analysis_profile?: string; analysis_profile_options?: string[]; analysis_profile_warning?: string | null;
  pipeline_profile?: string;
  provider_limits?: Record<string, { status: "unknown" | "blocked"; remaining_usd: string | null }>;
  limits: { max_upload_bytes: number; selected_pages: number; max_pdf_pages: number; original_days: number; [key: string]: unknown };
};
export type GuestAccess = { available: boolean; until: string | null; fresh_runs_per_day: number; showcase: boolean };
export type GuestSession = { until: string | null; fresh_runs_today: number; fresh_runs_limit: number };
export type Session = { user_id: string; tenant_id: string | null; role: string | null; csrf_token: string; expires_at: string; guest?: GuestSession | null };
export type HostedDocument = DocumentMetadata & { document_id: string; page_count: number; size_bytes: number; expires: number; sha256: string; display_name?: string; company_name?: string };
export type ListedDocument = HostedDocument & { display_name: string; uploaded_at: string };
export type ListedRun = HostedRun & { created_at: string };
export type CollectionPage<T> = { items: T[]; next_cursor: string | null };
export type SourcePageManifest = {
  run_id: string; document_id: string; document_version_id: string; physical_page: number; prepared_page: number;
  source_sha256: string; render_source_sha256: string; prepared_sha256: string; parse_manifest_id: string; parse_manifest_sha256: string;
  image_sha256: string; image_url: string; expires_at: string;
  image: { width_px: number; height_px: number; dpi: number; width_pt: number; height_pt: number; rotation: number; crop_box: number[] };
  coordinates: { units: string; origin: string; space: string; bbox_format: string; source_to_image: number[] };
  highlights: { ref_id: string; source_id: string; verification_state: string; bbox_normalized: number[] }[];
  unresolved_refs: { ref_id: string; reason?: string; message?: string }[]; message: string | null;
};
export type ReportingScopeInput = { report_year: number; period_start: string; period_end: string };
export type ReportingScope = { report_year: number | null; period_start: string | null; period_end: string | null; origin: string; warning?: string | null; detection_status?: string };
export type PeriodSource = { physical_page: number; quote: string; char_start: number; char_end: number; verification_state: string };
export type ScopePlan = {
  revision: number;
  reporting_scope?: ReportingScope;
  plan: { status: string; selected_claim_pages: number[]; selected_evidence_pages: number[];
    reporting_period_detection?: { status: string; candidates: (Partial<ReportingScopeInput> & { source_ref: PeriodSource; basis: string })[]; exceptions: { source_ref: PeriodSource }[] };
    failure_reasons: string[]; unreadable_pages: number[]; unknown_pages: number[]; conflict_pages: number[]; suggested_front_pages?: number[];
    pages: { page: number; category: string; reasons: string[]; snippet: string }[] };
  selection: { pages: number[]; claim_pages: number[]; selection_sha256: string } | null;
};
export type HostedRun = {
  run_id: string; document_id: string; status: string; selected_pages: number[]; status_url: string;
  error_code: string | null; stage?: string | null; queue_position?: number | null; progress?: number | null;
  provider_error_code?: string | null; provider_error_message?: string | null;
  user_active_count?: number; user_processing_count?: number;
  claims_extracted?: number; claims_processed?: number;
  claim_stage_counts?: Partial<Record<"preliminary" | "relation" | "elements", number>> | null;
  result_url?: string | null; result?: HostedResult | null;
};

// r108-hosted-result-v1 (CONTRACT.md P2). Only fields the UI reads are typed; unknown extra keys are tolerated.
export type SourceRefDto = {
  source_id: string; document_version_id: string; parse_manifest_id: string; page_num: number; printed_page_label: string | null;
  bbox: number[] | null; raw_text_sha256: string; quote: string; char_start: number; char_end: number;
  location_quality: string; verification_state: string;
};
export type SourceLocation = {
  bbox: number[]; line_rects: number[][] | null; glyph_rects?: number[][] | null;
  page: { width_pt: number; height_pt: number; rotation: number; crop_box: number[] };
  units: string; origin: string; space: string; bbox_format: string;
};
export type SourceViewRef = {
  ref_id: string; source_id: string; kind: "claim" | "evidence"; element_id: string | null;
  quote: string; physical_page: number | null; location: SourceLocation | null;
  w18_line_rects?: number[][] | null; w18_cell_rects?: number[][] | null;
  w18_coordinates?: Omit<SourceLocation, "bbox" | "line_rects" | "glyph_rects"> & { source_sha256: string; physical_page: number; char_start: number; char_end: number } | null;
  w18_reason?: string | null; w18_verification_state?: string;
};
export type SourceDocument = { document_id: string; sha256: string; page_count: number; url: string };
export type ElementState = "present" | "absent" | "unknown" | "conflict" | "not_applicable";
export type ResultElement = {
  element_id: string; state: ElementState; evidence_refs: SourceRefDto[]; normalized_value: string | null; credited_from: string | null; reason_code: string | null;
};
export type GradeRange = { floor: string; ceiling: string; open_elements: string[] };
export type TableComparison = { year?: string | number | null; value?: string | number | null; column_name?: string | null };
export type TableObservation = {
  year?: string | number | null; value?: string | number | null; unit?: string | null; metric?: string | null;
  parent_metric?: string | null; parent_row?: string | null; parent_path?: string[] | null;
  facility?: string | null; facility_name?: string | null; column_name?: string | null; column_header?: string | null;
  value_column_name?: string | null; comparisons?: TableComparison[]; verification_state?: string | null;
};
export type ResultDecision = {
  rule_ids?: string[];
  confirmation_basis?: string;
  decision_revision?: number; tag_revision?: number; decision_status: string; evidence_grade: string | null; label: string | null;
  review_status: string; missing_elements?: string[]; excluded_elements?: string[]; gap_ids?: string[]; grade_range?: (GradeRange & { rule_gap?: boolean }) | null;
};
export type MissingEvidence = {
  element_id: string; evidence_status?: string; reason?: string; undetermined_reason?: string; engine_state?: string;
  candidate_refs?: SourceRefDto[]; search?: { absence_admissible?: boolean; scope_note?: string };
};
export type ResultClaim = {
  cf6_confirmation?: ResultDecision | null;
  cf6_status?: "confirmed" | "held";
  cf6_track?: string | null;
  cf6_elements?: { element_id: string; state: string; votes?: number; evidence_refs: SourceRefDto[] }[];
  cf6_hold_reasons?: string[];
  sc1_confirmation?: (ResultDecision & {
    confirmation_basis: string; confirmation_display?: string; confirmation_policy_sha256?: string;
    confirmation_scope?: { selected_pages?: number[]; inspected_pages?: number[]; document_total_pages?: number } | null;
  }) | null;
  sc1_elements?: { element_id: string; state: string; inventory_sha256?: string }[] | null;
  sc1_hold_reason?: string | null;
  w16_source_view?: { quote: string; refs: SourceViewRef[] };
  claim_id: string; page_num: number; original_page_num: number; quote: string; track: string | null; topic_ids?: string[];
  decision: ResultDecision | null; revision: number; source_refs: SourceRefDto[]; source_quality: string; elements: ResultElement[];
  source_status?: { status: string; reason?: string; display: string };
  provisional_grade?: { evidence_grade: string | null; display: string; reason: string; display_type?: string; hold_reasons?: string[]; hold_reason_detail?: string | null; evidence_note?: string; needed_evidence?: { track: string; target_grade: string; elements: string[] }[]; grade_basis?: string; grade_range?: GradeRange | null; elements?: { element_id: string; state: string; votes: number; total_replicas: number; evidence_refs: SourceRefDto[] }[] } | null;
  pipeline_stage?: string; pipeline_status?: string; provisional_track?: string | null;
  hold_reason: string | null; possible_grade_range: GradeRange | null; confirmed_grade: string | null; missing_evidence: MissingEvidence[];
  hold_reason_detail?: string | null; table_observation?: TableObservation | null; duplicate_occurrences?: string[] | null;
  confirmed_label?: string | null; confirmation_source?: string | null; confirmation_display?: string;
  confirmation_policy_sha256?: string;
  confirmation_basis?: string | null; lower_bound_display?: string | null;
  source_ref_page_space?: "original";
};
export type ResultReview = { review_id: string; run_id: string; claim_id: string; status: string; revision: number; base_tag_revision: number; reason_codes: string[] };
export type ExtractionOutcome = {
  status?: string; code?: string; selected_claim_pages?: number[];
  failed_sources?: { physical_page?: number; source_id: string; failure_code?: string }[];
  page_failures?: { physical_page?: number; code?: string }[];
};
export type HostedResult = {
  reuse?: { reused: boolean; analyzed_at: string; source_policy_sha256: string; cost_usd?: string; provider_calls?: number; review_supported?: boolean };
  w16_source_document?: SourceDocument;
  schema: string; claims: ResultClaim[]; reviews: ResultReview[]; hold_reasons: string[]; page_map: number[];
  hold_reason_details?: { code: string; message: string }[];
  pipeline?: { status?: string; reason?: string | null; reporting_scope?: Partial<ReportingScope>; stage_seconds?: Record<string, number>;
    provider_concurrency?: { completed_by_role?: Record<string, number> };
    ux1_data?: { table_count?: number | null; model_judgment_calls?: number };
    extraction_outcome?: ExtractionOutcome;
    scope?: { analysis_profile_sha256?: string; pages?: number[]; claim_pages?: number[]; declared_subset?: boolean } };
  possible_grade_range?: GradeRange | null; confirmed_grade?: string | null;
};
export type ResolveBody = {
  base_tag_revision: number; track: "goal" | "performance" | "management"; reason: string;
  elements: { element_id: string; state: ElementState; evidence_refs: SourceRefDto[]; normalized_value: string | null; credited_from: string | null; reason_code: string | null }[];
};
export type ResolveResponse = { review: ResultReview; decision: ResultDecision; new_tag_revision: number };

export class HostedApiError extends Error {
  constructor(readonly status: number, readonly code: string, readonly retryAfter: number | null, readonly path: string) {
    super(`${status} ${code}`);
  }
  get userMessage() { return describeError(this); }
  /** 409/412 on a write: someone else changed the state (or the If-Match revision is stale). */
  get isConflict() { return this.status === 409 || this.status === 412; }
}

const codeMessages: Record<string, string> = {
  GUEST_DAILY_LIMIT: "오늘 새 보고서 분석 하루 한도에 도달했습니다. 올린 보고서는 유지됩니다. 미리 분석된 분석 사례를 확인하거나 내일 다시 이용해 주세요.",
  GUEST_UPLOAD_LIMIT: "오늘 업로드 한도에 도달했습니다. 미리 분석된 분석 사례를 확인하거나 내일 다시 이용해 주세요.",
  GUEST_SESSION_RATE_LIMIT: "접속 요청이 많습니다. 잠시 후 다시 시도해 주세요. 관리자 계정이 있다면 관리자 로그인을 이용해 주세요.",
  GUEST_ACCESS_CLOSED: "공개 이용 기간이 종료되었거나 현재 닫혀 있습니다. 관리자 로그인으로 접속해 주세요.",
  SCOPE_PLAN_FAILED: "읽을 수 있는 환경 본문을 찾지 못했습니다. 스캔·불명확한 쪽을 확인해 주세요. 분석 비용은 발생하지 않았습니다.",
  SCOPE_INSPECTION_FAILED: "자동 범위 계획을 만들지 못했습니다. 파일 내부 처리 한도 또는 서버 상태를 확인해 주세요.",
  SCOPE_CONFLICT: "분석 범위가 변경되었습니다. 최신 계획을 확인해 주세요.",
  REPORTING_SCOPE_INVALID: "보고연도와 날짜를 확인해 주세요. 시작일은 종료일보다 늦을 수 없습니다.",
  REPORTING_SCOPE_REQUIRED: "보고기간을 확인하고 입력한 뒤 범위를 저장해 주세요.",
  SCOPE_PLAN_REQUIRED: "업로드 후 자동 범위 계획을 먼저 만들어 주세요.",
  INVITATION_INVALID: "초대 코드가 올바르지 않거나 이미 사용·만료되었습니다.",
  ACCESS_PASSWORD_INVALID: "접근 비밀번호가 올바르지 않거나 만료·비활성화되었습니다.",
  ACCESS_PASSWORD_LOCKED: "로그인 실패가 반복되어 접근이 잠겼습니다.",
  AUTH_REQUIRED: "로그인이 필요합니다. 초대 코드 또는 접근 비밀번호로 다시 로그인해 주세요.",
  CSRF_INVALID: "요청 검증에 실패했습니다. 페이지를 새로고침한 뒤 다시 로그인해 주세요.",
  RESOURCE_NOT_FOUND: "문서 또는 분석을 찾을 수 없습니다. 이미 삭제되었거나 보관 기간이 끝났을 수 있습니다.",
  FORBIDDEN: "이 작업을 할 권한이 없습니다.",
  PAYLOAD_TOO_LARGE: "PDF가 업로드 한도를 넘습니다.",
  PDF_PASSWORD_REQUIRED: "PDF를 여는 암호가 필요합니다. 열기 암호를 해제한 PDF로 다시 업로드해 주세요.",
  PDF_INVALID: "PDF의 원본 객체·이동 링크 또는 보안 구조를 끝까지 확인하지 못했습니다. PDF 뷰어에서 ‘다른 이름으로 저장’한 파일로 다시 업로드해 주세요.",
  UPLOAD_LIMIT_EXCEEDED: "PDF가 파일 크기·쪽수 또는 내부 처리 한도를 초과했습니다. 보고서를 나누거나 최적화해 다시 업로드해 주세요.",
  PDF_CONTENT_TYPE_REQUIRED: "PDF 파일만 올릴 수 있습니다.",
  UPLOAD_DISABLED: "현재 업로드가 닫혀 있습니다.",
  ANALYSIS_DISABLED: "현재 새 분석 접수가 중지되어 있습니다.",
  SOURCE_EXPIRED: "원본 PDF 보관 기간(7일)이 끝났습니다. 다시 업로드해 주세요.",
  SOURCE_UNAVAILABLE: "원본 PDF를 사용할 수 없습니다. 다시 업로드해 주세요.",
  PAGE_SELECTION_INVALID: "쪽 선택을 확인해 주세요. 주장 최대 10쪽, 근거 검색 최대 30쪽이며 주장 쪽을 근거 검색 범위에 포함해야 합니다.",
  IDEMPOTENCY_CONFLICT: "같은 요청 키로 다른 내용이 전송되었습니다. 처음부터 다시 시도해 주세요.",
  IDEMPOTENCY_KEY_INVALID: "요청 키가 올바르지 않습니다. 다시 시도해 주세요.",
  USER_RUN_LIMIT: "이 서버의 사용자별 접수 한도에 도달했습니다. 진행 중인 분석을 확인하거나 운영자에게 문의해 주세요.",
  QUEUE_FULL: "서버 대기열(최대 8건)이 가득 찼습니다. 업로드한 문서는 유지됩니다. 대기 작업이 줄어든 뒤 분석 시작을 다시 눌러 주세요.",
  DAILY_RUN_LIMIT: "오늘의 사용자별 또는 서버 전체 분석 횟수 한도에 도달했습니다. 업로드한 문서는 유지됩니다. 내일 다시 접수해 주세요.",
  BUDGET_EXCEEDED: "새 분석의 비용을 예약할 수 없습니다. 실행·일·행사 예산 상한 또는 정산 보류 금액을 운영자가 확인해야 합니다. 업로드한 문서는 유지되며 이번 분석은 접수되지 않았습니다.",
  BUDGET_EXHAUSTED: "예약한 분석 예산이 소진되어 남은 항목을 처리하지 못했습니다. 운영자에게 비용 기록 확인을 요청해 주세요.",
  PROVIDER_CREDIT_EXHAUSTED: "분석 API 크레딧이 부족합니다. 운영자의 충전 확인이 필요합니다.",
  PROVIDER_AUTH_FAILED: "분석 API 인증에 실패했습니다. 운영자의 키 확인이 필요합니다.",
  PROVIDER_ACCESS_DENIED: "분석 API 사용 권한이 거부되었습니다. 운영자 확인이 필요합니다.",
  PROVIDER_RATE_LIMITED: "분석 API 요청 한도에 도달했습니다. 운영자 확인 후 다시 접수해 주세요.",
  PROVIDER_UNAVAILABLE: "분석 API에 일시적인 서버 오류가 발생했습니다. 운영자에게 상태 확인을 요청해 주세요.",
  RATE_LIMITED: "요청이 많습니다. 잠시 후 다시 시도해 주세요.",
  ACCOUNTING_PENDING: "이 분석은 응답 비용을 확정하지 못해 정산 확인 중입니다. 예약 금액은 유지되며 운영자 확인이 필요합니다.",
  DEPENDENCY_UNAVAILABLE: "서버 저장소를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
  ANALYSIS_RESULT_UNAVAILABLE: "분석 결과가 아직 준비되지 않았습니다. 잠시 후 다시 확인해 주세요.",
  REUSE_COORDINATES_BUSY: "이전 분석의 원문 위치를 준비하고 있습니다. 잠시 후 다시 시도해 주세요. 분석 비용은 발생하지 않았습니다.",
  RESULT_INTEGRITY_UNAVAILABLE: "원본 PDF와 분석 기록의 일치를 확인하지 못해 결과를 표시하지 않습니다. 빈 결과가 아닙니다.",
  REVIEW_CONFLICT: "다른 검토가 먼저 반영되었거나 검토 상태가 바뀌었습니다. 결과를 새로고침한 뒤 다시 검토해 주세요.",
  IF_MATCH_REQUIRED: "검토 버전 정보가 없어 제출할 수 없습니다. 결과를 새로고침해 주세요.",
  COVERAGE_OR_APPLICABILITY_REQUIRED: "‘근거 없음’은 검증된 검색 범위(커버리지) 또는 비적용 확인 없이는 인정되지 않습니다. 근거를 찾지 못한 것만으로는 없음으로 바꿀 수 없으니 ‘확인되지 않음’을 유지해 주세요.",
  COMPLETE_TRACK_ELEMENTS_REQUIRED: "해당 트랙의 모든 요소 상태가 필요합니다. 결과를 새로고침해 주세요.",
  SOURCE_REJECTED: "선택한 근거가 원문 대조에서 검증되지 않았습니다.",
  SOURCE_VALUE_REQUIRED: "이 요소는 원문에서 확인된 값(수치·연도)이 필요합니다.",
  SOURCE_VALUE_MISMATCH: "입력한 값이 원문 근거와 일치하지 않습니다.",
  REVIEW_INPUT_UNAVAILABLE: "검토에 필요한 원문 입력을 읽을 수 없습니다. 결과를 새로고침해 주세요.",
  VALIDATION_ERROR: "검토 입력이 올바르지 않습니다. 사유는 5자 이상 1000자 이하로 입력해 주세요.",
  PURGE_INCOMPLETE: "삭제가 아직 완료되지 않았습니다. 잠시 후 다시 확인해 주세요.",
};
const pdfRejections = /PDF|ENCRYPT|PASSWORD|PAGE|ACTIVE|MALFORMED|LIMIT/i;

export function describeError(error: HostedApiError): string {
  const { status, code, retryAfter } = error;
  if (codeMessages[code] && !["QUEUE_FULL", "RATE_LIMITED", "ACCESS_PASSWORD_LOCKED"].includes(code)) return codeMessages[code];
  if (status === 429) {
    const wait = retryAfter ? ` ${retryAfter}초 뒤에 다시 시도해 주세요.` : " 잠시 후 다시 시도해 주세요.";
    return (codeMessages[code] ?? "요청이 너무 많습니다.") + wait;
  }
  if (codeMessages[code]) return codeMessages[code];
  switch (status) {
    case 0: return "서버에 연결할 수 없습니다. 네트워크 또는 서버 상태를 확인해 주세요.";
    case 401: return codeMessages.AUTH_REQUIRED;
    case 403: return codeMessages.FORBIDDEN;
    case 404: return codeMessages.RESOURCE_NOT_FOUND;
    case 412: return codeMessages.REVIEW_CONFLICT;
    case 409: return "현재 상태에서는 이 요청을 처리할 수 없습니다. 새로고침 후 다시 시도해 주세요.";
    case 413: return codeMessages.PAYLOAD_TOO_LARGE;
    case 422: return pdfRejections.test(code) ? `PDF를 받을 수 없습니다(${code}). 암호·활성 콘텐츠가 없는 300쪽 이하 PDF인지 확인해 주세요.` : "입력을 확인해 주세요.";
    case 502: case 503: case 504: return "서버를 일시적으로 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.";
    default: return `요청을 처리하지 못했습니다(${status}).`;
  }
}

export type ClientOptions = { base?: string; fetchImpl?: typeof fetch; sleep?: (ms: number) => Promise<void> };
export const connectionRetryMessage = "서버 연결이 잠시 끊겼습니다 — 자동 재시도 중";

export function newIdempotencyKey(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return "pk-" + Array.from(bytes, b => b.toString(16).padStart(2, "0")).join("");
}

export function createHostedClient(options: ClientOptions = {}) {
  const base = (options.base ?? "/hosted-api").replace(/\/$/, "");
  const doFetch = options.fetchImpl ?? ((...args: Parameters<typeof fetch>) => fetch(...args));
  const sleep = options.sleep ?? ((ms: number) => new Promise<void>(resolve => setTimeout(resolve, ms)));
  let csrf = "";
  const recovering = new Set<symbol>();
  const listeners = new Set<(message: string) => void>();
  const notifyConnection = () => listeners.forEach(listener => listener(recovering.size ? connectionRetryMessage : ""));

  async function wait(ms: number, signal?: AbortSignal) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    let abort: (() => void) | undefined;
    try {
      await Promise.race([sleep(ms), new Promise<never>((_, reject) => {
        if (signal) { abort = () => reject(new DOMException("Aborted", "AbortError")); signal.addEventListener("abort", abort, { once: true }); }
      })]);
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    } finally { if (abort) signal?.removeEventListener("abort", abort); }
  }

  async function request<T>(method: "GET" | "POST", path: string, init: Parameters<typeof requestOnce>[2] = {}): Promise<T> {
    const recovery = Symbol(path); let delay = 1000;
    try {
      for (;;) {
        try { return await requestOnce<T>(method, path, init); }
        catch (error) {
          if (method !== "GET" || init.signal?.aborted || !(error instanceof HostedApiError) || !(error.status === 0 || (error.status >= 500 && error.status !== 501))) throw error;
          recovering.add(recovery); notifyConnection();
          await wait(delay, init.signal); delay = Math.min(delay * 2, 30000);
        }
      }
    } finally { if (recovering.delete(recovery)) notifyConnection(); }
  }

  async function requestOnce<T>(method: "GET" | "POST", path: string, init: { json?: unknown; body?: BodyInit; headers?: Record<string, string>; signal?: AbortSignal; binary?: boolean } = {}): Promise<T> {
    const headers: Record<string, string> = { Accept: "application/json", ...init.headers };
    if (method !== "GET" && csrf) headers["X-CSRF-Token"] = csrf;
    let body = init.body;
    if (init.json !== undefined) { headers["Content-Type"] = "application/json"; body = JSON.stringify(init.json); }
    let response: Response;
    try {
      response = await doFetch(base + path, { method, headers, body, credentials: "same-origin", signal: init.signal });
    } catch (error) {
      if (init.signal?.aborted) throw error;
      throw new HostedApiError(0, "NETWORK", null, path);
    }
    if (!response.ok) {
      let code = "UNKNOWN";
      try { const data = await response.json(); code = String(data?.error?.code ?? code); } catch { /* non-JSON proxy error */ }
      const retry = Number(response.headers.get("Retry-After"));
      throw new HostedApiError(response.status, code, Number.isFinite(retry) && retry > 0 ? retry : null, path);
    }
    if (response.status === 204) return undefined as T;
    try {
      if (init.binary) {
        if (response.headers.get("Content-Type")?.split(";")[0] !== "image/png") throw new HostedApiError(409, "SOURCE_IMAGE_MISMATCH", null, path);
        return await response.blob() as T;
      }
      return await response.json() as T;
    }
    catch (error) {
      if (init.signal?.aborted) throw error;
      if (error instanceof HostedApiError) throw error;
      throw new HostedApiError(502, "INVALID_SERVER_RESPONSE", null, path);
    }
  }

  return {
    subscribeConnection(listener: (message: string) => void) {
      listeners.add(listener); listener(recovering.size ? connectionRetryMessage : "");
      return () => { listeners.delete(listener); };
    },
    get csrfToken() { return csrf; },
    originalPdfUrl(document: SourceDocument): string {
      if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(document.document_id) || !/^[0-9a-f]{64}$/.test(document.sha256)) throw new HostedApiError(409, "SOURCE_REFERENCE_MISMATCH", null, "/original");
      // Ignore the payload URL: credentials go only to our configured API.
      return `${base}/v1/documents/${document.document_id}/original?sha256=${document.sha256}`;
    },
    async login(code: string): Promise<Session> {
      const session = await request<Session>("POST", "/v1/auth/invitation", { json: { code: code.trim() } });
      csrf = session.csrf_token; return session;
    },
    async loginPassword(password: string, role: "admin" | "organizer"): Promise<Session> {
      const session = await request<Session>("POST", "/v1/auth/access-password", { json: { role, password } });
      csrf = session.csrf_token; return session;
    },
    async session(signal?: AbortSignal): Promise<Session> {
      const session = await request<Session>("GET", "/v1/session", { signal });
      csrf = session.csrf_token; return session;
    },
    guestAccess: (signal?: AbortSignal) => request<GuestAccess>("GET", "/v1/guest", { signal }),
    async guestSession(signal?: AbortSignal): Promise<Session> {
      const session = await request<Session>("POST", "/v1/guest/session", { signal });
      csrf = session.csrf_token; return session;
    },
    async logout(): Promise<void> { await request<void>("POST", "/v1/auth/logout"); csrf = ""; },
    runtime: (signal?: AbortSignal) => request<Runtime>("GET", "/v1/runtime", { signal }),
    listDocuments: (cursor?: string | null, signal?: AbortSignal) => request<CollectionPage<ListedDocument>>("GET", `/v1/documents?${new URLSearchParams({ limit: "20", ...(cursor ? { cursor } : {}) })}`, { signal }),
    listRuns: (cursor?: string | null, signal?: AbortSignal) => request<CollectionPage<ListedRun>>("GET", `/v1/runs?${new URLSearchParams({ limit: "20", ...(cursor ? { cursor } : {}) })}`, { signal }),
    listShowcaseDocuments: (cursor?: string | null, signal?: AbortSignal) => request<CollectionPage<ListedDocument>>("GET", `/v1/showcase/documents?${new URLSearchParams({ limit: "100", ...(cursor ? { cursor } : {}) })}`, { signal }),
    listShowcaseRuns: (cursor?: string | null, signal?: AbortSignal) => request<CollectionPage<ListedRun>>("GET", `/v1/showcase/runs?${new URLSearchParams({ limit: "100", ...(cursor ? { cursor } : {}) })}`, { signal }),
    getSourceManifest: (runId: string, page: number, claimId: string, signal?: AbortSignal) =>
      request<SourcePageManifest>("GET", `/v1/runs/${encodeURIComponent(runId)}/source-pages/${page}/manifest?${new URLSearchParams({ claim_id: claimId })}`, { signal }),
    async getSourceImage(manifest: SourcePageManifest, signal?: AbortSignal): Promise<Blob> {
      const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
      if (!uuid.test(manifest.run_id) || !uuid.test(manifest.document_version_id) || !Number.isInteger(manifest.physical_page) || manifest.physical_page < 1 || !/^[0-9a-f]{64}$/.test(manifest.image_sha256)) throw new HostedApiError(409, "SOURCE_REFERENCE_MISMATCH", null, "/source-image");
      // Construct a same-origin route; never forward session credentials to a manifest-supplied URL.
      const path = `/v1/runs/${manifest.run_id}/source-pages/${manifest.physical_page}/image?${new URLSearchParams({ image_sha256: manifest.image_sha256, document_version_id: manifest.document_version_id })}`;
      const blob = await request<Blob>("GET", path, { signal, binary: true, headers: { Accept: "image/png" } });
      const bytes = await blob.arrayBuffer();
      const signature = new Uint8Array(bytes).slice(0, 8);
      const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)), x => x.toString(16).padStart(2, "0")).join("");
      if (hash !== manifest.image_sha256 || signature.join(",") !== "137,80,78,71,13,10,26,10") throw new HostedApiError(409, "SOURCE_IMAGE_MISMATCH", null, path);
      return blob;
    },
    getDocument: (documentId: string, signal?: AbortSignal) => request<HostedDocument>("GET", `/v1/documents/${encodeURIComponent(documentId)}`, { signal }),
    uploadDocument: (file: Blob, key = newIdempotencyKey(), signal?: AbortSignal, ux1Metadata?: { original_filename?: string; title?: string; policy_sha256: string }) =>
      request<HostedDocument>("POST", "/v1/documents", { body: file, headers: { "Content-Type": "application/pdf", "Idempotency-Key": key, ...(ux1Metadata ? { "X-UX1-Metadata": encodeURIComponent(JSON.stringify(ux1Metadata)) } : {}) }, signal }),
    scopePlan: (documentId: string) => request<ScopePlan>("POST", `/v1/documents/${encodeURIComponent(documentId)}/scope-plan`),
    getScopePlan: (documentId: string, signal?: AbortSignal) => request<ScopePlan>("GET", `/v1/documents/${encodeURIComponent(documentId)}/scope-plan`, { signal }),
    editScope: (documentId: string, scope: ScopePlan, claims: number[], evidence: number[], period?: ReportingScopeInput) =>
      request<ScopePlan>("POST", `/v1/documents/${encodeURIComponent(documentId)}/scope-plan/selection`, { json: { selected_claim_pages: claims, selected_evidence_pages: evidence, manual_selection: true, ...(period ? { reporting_scope: period } : {}) }, headers: { "If-Match": `"${scope.revision}"` } }),
    createRun: (documentId: string, scope: ScopePlan, key = newIdempotencyKey(), profile?: string) =>
      request<HostedRun>("POST", "/v1/runs", { json: { document_id: documentId, scope_selection_sha256: scope.selection?.selection_sha256 }, headers: { "Idempotency-Key": key, ...(profile ? { "X-Pipeline-Profile": profile } : {}) } }),
    getRun: (statusUrl: string, signal?: AbortSignal) => request<HostedRun>("GET", statusUrl, { signal }),
    cancelRun: (runId: string) => request<HostedRun>("POST", `/v1/runs/${encodeURIComponent(runId)}/cancel`),
    getResult: (resultUrl: string, signal?: AbortSignal, ux1Policy?: string) => request<HostedResult>("GET", `${resultUrl}${resultUrl.includes("?") ? "&" : "?"}w18=true${ux1Policy ? `&ux1=${encodeURIComponent(ux1Policy)}` : ""}`, { signal }),
    /** Reviewer/admin. If-Match is the quoted integer review revision (reviews[].revision); the server recomputes the grade. */
    resolveReview: (runId: string, reviewId: string, revision: number, body: ResolveBody, key = newIdempotencyKey()) =>
      request<ResolveResponse>("POST", `/v1/runs/${encodeURIComponent(runId)}/reviews/${encodeURIComponent(reviewId)}/resolve`, { json: body, headers: { "If-Match": `"${revision}"`, "Idempotency-Key": key } }),
    // Uploader (own document) or tenant admin; 5..1000 char reason + Idempotency-Key required. Others get 403.
    deleteDocument: (documentId: string, reason = "사용자 요청에 의한 원본 PDF 삭제", key = newIdempotencyKey()) =>
      request<{ deletion_id: string; status: string }>("POST", `/v1/documents/${encodeURIComponent(documentId)}/deletion-requests`, { json: { reason }, headers: { "Idempotency-Key": key } }),

    /** Poll until a terminal status. Backoff 1s -> 10s; honours 429 Retry-After; stops on abort. */
    async pollRun(first: HostedRun, onUpdate: (run: HostedRun) => void, signal?: AbortSignal): Promise<HostedRun> {
      let run = first; let delay = 1000;
      onUpdate(run);
      while (!terminalStatuses.includes(run.status)) {
        try { await wait(delay, signal); } catch { if (signal?.aborted) return run; throw new Error("조회 대기 실패"); }
        try {
          run = await this.getRun(run.status_url, signal); onUpdate(run);
          delay = Math.min(Math.round(delay * 1.5), 10000);
        } catch (error) {
          if (signal?.aborted) return run;
          if (!(error instanceof HostedApiError)) throw error;
          if (error.status === 429) { delay = Math.max(delay, (error.retryAfter ?? 5) * 1000); continue; }
          throw error;
        }
      }
      return run;
    },
  };
}
export const terminalStatuses = ["partial_blocked", "completed", "cancelled", "failed", "accounting_pending"];
export type HostedClient = ReturnType<typeof createHostedClient>;

export const providerLimitCodes = ["OPENROUTER_HTTP_402", "OPENROUTER_HTTP_403", "OPENAI_HTTP_401", "OPENAI_HTTP_429", "OPENAI_INSUFFICIENT_QUOTA", "PROVIDER_CREDIT_EXHAUSTED", "PROVIDER_AUTH_FAILED", "PROVIDER_ACCESS_DENIED", "PROVIDER_RATE_LIMITED"];
export const providerLimitMessage = "분석 서비스 한도 초과 — 운영자 확인 필요";
const blockedReasons: Record<string, string> = {
  PARSER_FAILED: "문서 파싱에 실패해 분석을 중단했습니다. 처리되지 않은 원문은 판정하지 않았습니다. 운영자의 파싱 확인이 필요합니다.",
  PARSER_MEMORY_LIMIT: "PDF 파싱이 메모리 한도에 도달했습니다. 선택 쪽을 줄여 새 분석을 접수해 주세요.",
  PARSER_JAVA_HEAP_EXHAUSTED: "PDF 파싱이 메모리 한도에 도달했습니다. 선택 쪽을 줄여 새 분석을 접수해 주세요.",
  PARSER_MEMORY_WATCHDOG_UNAVAILABLE: "서버의 파싱 자원 감시가 준비되지 않았습니다. 운영자 확인이 필요합니다.",
  PARSER_PDF_INVALID: "PDF 내부 구조를 읽지 못했습니다. PDF 뷰어에서 다시 저장한 파일로 업로드해 주세요.",
  PARSER_NATIVE_TEXT_UNREADABLE: "PDF의 보조 텍스트 파서가 파일 구조를 읽지 못했습니다. 원문과 처리 중 남긴 후보는 보존하며 확인되지 않은 내용은 판정하지 않았습니다.",
  PARSER_GEOMETRY_INVALID: "PDF의 원문 좌표를 검증하지 못했습니다. 해당 원문은 판정하지 않았습니다.",
  CLAIM_PAGES_UNREADABLE: "선택한 주장 쪽을 읽지 못했습니다. 다른 쪽 또는 다시 저장한 PDF로 접수해 주세요.",
  CLAIM_PAGE_PARSE_FAILURE: "일부 주장 쪽을 읽지 못했습니다. 처리된 주장만 표시하며 누락 쪽은 판정하지 않았습니다.",
  CLAIM_SOURCE_EXTRACTION_FAILURE: "일부 원문에서 주장을 추출하지 못했습니다. 처리된 주장만 표시합니다.",
  SOURCE_ATTESTATION_UNAVAILABLE: "원문 대조를 완료하지 못했습니다. 해당 주장은 보류하며 운영자 확인이 필요합니다.",
  SOURCE_ATTESTATION_REPLAY_MISMATCH: "원문 대조 기록의 재검증이 일치하지 않습니다. 기존 기록을 보존하고 해당 주장을 보류했습니다.",
  SOURCE_ATTESTATION_GEOMETRY_UNSUPPORTED: "이 PDF의 회전·좌표 형식을 원문 대조기가 지원하지 않아 해당 주장을 보류했습니다.",
  SOURCE_ATTESTATION_RENDER_LIMIT: "PDF 쪽의 이미지 크기가 원문 대조 한도를 초과했습니다. 해당 원문은 판정하지 않았으며 운영자의 쪽 크기 확인이 필요합니다.",
  EXTRACTION_INPUT_INVALID: "주장 추출 입력을 검증하지 못했습니다. 원문·처리 기록은 보존하며 운영자 확인이 필요합니다.",
  PROBE_REQUEST_TOO_LARGE: "선택한 원문의 모델 요청이 크기 한도를 초과했습니다. 선택 쪽을 줄여 새 분석을 접수해 주세요.",
  NO_EXTRACTABLE_CLAIM_BLOCKS: "선택 범위에 읽을 수 있는 주장 블록이 없습니다. 다른 쪽을 선택해 주세요.",
  NO_ELIGIBLE_CLAIM_BLOCKS: "선택 범위에 주장 추출 대상 블록이 없습니다. 다른 쪽을 선택해 주세요.",
  NO_CLAIMS_IN_PROCESSED_BLOCKS: "처리한 선택 범위에서 환경 주장을 추출하지 못했습니다. 보고서 전체의 근거 부재를 뜻하지 않습니다.",

  "근거 없음": "인정된 근거 미연결(확인 필요)",
  "근거 없음(검증된 검색 범위)": "인정된 근거 미연결(확인 필요)",
  blocked_evidence: "주장에 대한 근거 요소를 정확히 판단할 수 없어 입증 수준 판정을 보류했습니다.",
  blocked_rule_gap: "판정 규칙에 미정 항목이 있어 자동 판정을 보류했습니다.",
  linkage_unclear: "원문 인용과 요소의 연결을 확인해야 합니다.",
  SUPERLATIVE_UNRESOLVED: "최상급 표현의 비교 기준과 외부 검증 여부 확인이 필요합니다.",
  CITATION_REPLICAS_UNRESOLVED: "인용 반복 판독이 합의되지 않아 판정을 보류했습니다.",
  no_comparable_table_observation: "같은 범위에서 비교할 표 값을 확인하지 못했습니다.",
  needs_review: "사람 검토가 필요합니다.",
  PROVIDER_CREDIT_EXHAUSTED: "분석 API 크레딧이 부족합니다. 운영자의 충전 확인이 필요합니다.",
  PROVIDER_AUTH_FAILED: "분석 API 인증에 실패했습니다. 운영자의 키 확인이 필요합니다.",
  PROVIDER_ACCESS_DENIED: "분석 API 사용 권한이 거부되었습니다. 운영자 확인이 필요합니다.",
  PROVIDER_RATE_LIMITED: "분석 API 요청 한도에 도달했습니다. 운영자 확인 후 다시 접수해 주세요.",
  PROVIDER_UNAVAILABLE: "분석 API에 일시적인 서버 오류가 발생했습니다.",
  ACCOUNTING_PENDING: "비용 정산 확인 중입니다. 운영자 확인이 필요합니다.",
  ANALYSIS_MEMORY_LIMIT: "동시 분석의 메모리 한도에 도달해 중단되었습니다. 운영자 확인이 필요합니다.",
  OPENAI_HTTP_401: providerLimitMessage,
  OPENAI_HTTP_429: providerLimitMessage,
  OPENAI_INSUFFICIENT_QUOTA: providerLimitMessage,
  BUDGET_EXHAUSTED: "예산 상한으로 미분석",
  R108_BUDGET_EXHAUSTED: "분석 예산 한도로 일부 항목을 미분석했습니다. 호출 수·시간·비용 기록을 확인하세요.",
  BUDGET_EXCEEDED: "분석 비용 상한에 도달했습니다.",
  OPENROUTER_HTTP_402: providerLimitMessage,
  OPENROUTER_HTTP_403: providerLimitMessage,
  LIVE_BINDING_UNAVAILABLE: "실시간 모델·OCR 분석이 서버에 연결되어 있지 않아 판정하지 않았습니다.",
  LINUX_SOURCE_READER_UNRESOLVED: "이 서버의 원문 판독기가 아직 승인되지 않아 분석하지 않았습니다. 주장·등급은 만들지 않았습니다.",
  LEASE_EXPIRED: "처리 중 작업이 중단되어 비용 정산 확인 전까지 보류되었습니다.",
  LEASE_EXPIRED_NO_DISPATCH: "작업이 중단되었습니다. 모델 전송이 없어 비용 예약이 해제되었습니다. 다시 접수할 수 있습니다.",
  ANALYSIS_LOCK_BUSY: "문서가 다른 작업에서 사용 중입니다. 잠시 후 다시 접수해 주세요.",
  PREPARE_FAILED: "선택한 쪽을 준비하지 못했습니다.",
  HOSTED_PREPARATION_FAILED: "선택한 쪽을 준비하지 못했습니다.",
  R108_NEEDS_REVIEW: "일부 주장이 사람 검토를 기다리고 있어 전체를 확정하지 않았습니다.",
  SOURCE_VALIDATION_REQUIRED: "원문 대조가 끝나지 않아 판정하지 않았습니다.",
  CONSENSUS_UNRESOLVED: "모델 판독이 합의되지 않아 요소 상태가 확정되지 않았습니다.",
  PROCESSING_UNCERTAIN: "처리 불확실 — 네트워크 응답을 확인하지 못해 해당 주장만 보류했습니다.",
  PRELIMINARY_TAGS_UNRESOLVED: "예비 분류(트랙)가 합의되지 않아 요소를 판정하지 않았습니다.",
  RELATION_TAGS_UNRESOLVED: "주장과 근거의 관계가 정리되지 않아 요소를 판정하지 않았습니다.",
  REPLICA_UNRESOLVED: "반복 판독 결과가 일치하지 않습니다.",
  MODEL_SPAN_OR_SCHEMA_INVALID: "추출 응답의 원문 구간 또는 형식 검증에 실패했습니다.",
  TRANSPORT_FAILED: "추출 요청의 통신이 실패했습니다.",
};
export function isProcessingFailure(code: string): boolean {
  return /(?:^|_)UNEXPECTED(?:_|$)|(?:^|_)FAILED(?:$|:)|^INPUT_INVALID$/.test(code.trim());
}

/** Korean wording for backend hold reasons; unknown machine codes stay out of user copy. */
export function holdReasonText(code: string, details?: { code: string; message: string }[]): string {
  const detail = details?.find(item => item.code === code)?.message;
  if (detail) return detail;
  const reason = code.trim();
  if (blockedReasons[reason]) return blockedReasons[reason];
  if (isProcessingFailure(code)) return "처리 오류로 보류했습니다. 이 오류는 기업의 근거 결손을 뜻하지 않습니다.";
  if (/^REVIEW:/.test(reason)) return `${reason.slice(7)} 요소 사람 확인 필요`;
  const baseCode = reason.split(":", 1)[0];
  if (baseCode !== reason && blockedReasons[baseCode]) return blockedReasons[baseCode];
  if (/^[A-Za-z][A-Za-z0-9_:-]*$/.test(reason)) return "추가 확인이 필요한 항목입니다. 상세 사유는 제공되지 않았습니다.";
  return reason || "추가 확인이 필요해 판정을 보류했습니다.";
}
export function runOutcome(run: HostedRun): { kind: "pending" | "blocked" | "completed" | "cancelled" | "failed"; title: string; reasons: string[] } {
  const provider = run.provider_error_code;
  switch (run.status) {
    case "accounting_pending": return { kind: "blocked", title: "분석 중단 · 비용 정산 확인 중", reasons: [holdReasonText(provider ?? run.error_code ?? "ACCOUNTING_PENDING")] };
    case "partial_blocked": {
      const codes = run.result?.hold_reasons?.length ? run.result.hold_reasons : run.error_code ? [run.error_code] : [];
      const reasons = codes.length ? codes.map(code => holdReasonText(code, run.result?.hold_reason_details)) : ["분석이 일부만 진행되어 보류되었습니다."];
      reasons.push("보류는 ‘근거 없음’이 아닙니다. 판정되지 않은 주장의 근거는 확인되지 않은 상태이며, 확정 등급을 부여하지 않았습니다.");
      return { kind: "blocked", title: "보류", reasons };
    }
    case "completed": {
      const execution = resultExecution(run.result, run.status);
      return { kind: execution.kind === "partial" ? "blocked" : execution.kind === "failed" ? "failed" : "completed", title: execution.title,
        reasons: run.result ? [execution.meaning] : ["이 서버가 결과 상세를 아직 제공하지 않아 화면에 표시할 수 없습니다."] };
    }
    case "cancelled": return { kind: "cancelled", title: "취소됨", reasons: [] };
    case "failed": return { kind: "failed", title: "실패", reasons: [holdReasonText(provider ?? run.error_code ?? "알 수 없음")] };
    default: return { kind: "pending", title: run.status === "queued" ? "대기 중" : "분석 중", reasons: [] };
  }
}

/** Completed run with a known extraction gap; claims and grades are unchanged. */
export function completedRunPresentation(result: HostedResult, runStatus?: string) {
  if (runStatus !== "completed" || (result.pipeline?.status && result.pipeline.status !== "completed")) return null;
  const extraction = result.pipeline?.extraction_outcome;
  // The backend publishes successful extraction as "claims"; "completed" is kept for older results.
  if (extraction?.status && !["claims", "completed", "partial"].includes(extraction.status)) return null;
  const sources = extraction?.failed_sources?.length ?? 0, pages = extraction?.page_failures?.length ?? 0;
  if (sources || pages) return {
    kind: "partial" as const,
    title: ["분석 완료", ...(sources ? [`미처리 원문 ${sources}건`] : []), ...(pages ? [`미처리 ${pages}쪽`] : [])].join(" · "),
    meaning: "일부 쪽·원문의 처리가 완료되지 않아 처리된 주장만 표시합니다. 미처리 원문은 판정하지 않았습니다.",
  };
  if (extraction?.status === "partial") return null; // No count: do not imply a known complete scope.
  return { kind: "completed" as const, title: "분석 완료 · 선택 범위", meaning: "선택한 범위의 처리가 끝났습니다. 주장별 확정·보류 상태와 근거를 확인하세요." };
}

/** Presentation only: terminal completion does not imply successful extraction or a final grade. */
export function resultExecution(result?: HostedResult | null, runStatus?: string) {
  const status = runStatus ?? result?.pipeline?.status;
  const extraction = result?.pipeline?.extraction_outcome;
  if (status === "failed" || result?.pipeline?.status === "failed") return { kind: "failed", title: "실행 실패", meaning: "처리 중 오류로 분석이 끝났습니다. 남아 있는 결과만 표시하며 미처리 원문은 판정하지 않았습니다." };
  const completed = result && completedRunPresentation(result, runStatus);
  if (completed) return completed;
  if ([status, result?.pipeline?.status, extraction?.status].some(value => ["partial", "partial_blocked", "blocked", "accounting_pending"].includes(value ?? "")) || extraction?.failed_sources?.length || extraction?.page_failures?.length) {
    return { kind: "partial", title: "부분 완료 · 미처리 범위 확인", meaning: "일부 쪽·원문의 처리가 완료되지 않아 처리된 주장만 표시합니다. 미처리 범위는 아래 안내에서 확인하세요." };
  }
  if (status === "completed") return { kind: "completed", title: "분석 완료 · 선택 범위", meaning: "선택한 범위의 처리가 끝났습니다. 주장별 확정·보류 상태와 근거를 확인하세요." };
  if (status === "cancelled") return { kind: "cancelled", title: "실행 취소", meaning: "취소 전 남은 결과만 표시합니다. 처리하지 않은 범위는 판정하지 않았습니다." };
  return { kind: "unknown", title: "실행 상태 확인 필요", meaning: "이 결과만으로 실행 완료 여부를 확인할 수 없습니다." };
}

export function parsePageSelection(value: string, pageCount: number, max = 2): number[] {
  const pages: number[] = [];
  for (const token of value.split(/[,\s]+/).filter(Boolean)) {
    if (!/^\d+$/.test(token)) throw new Error("쪽 번호는 숫자를 쉼표로 구분해 입력해 주세요. 예: 26,28");
    const page = Number(token);
    if (page < 1 || page > pageCount) throw new Error(`1–${pageCount}쪽 중에서 선택해 주세요.`);
    if (!pages.includes(page)) pages.push(page);
  }
  if (!pages.length) throw new Error("분석할 쪽을 1개 이상 입력해 주세요.");
  if (pages.length > max) throw new Error(`한 번에 최대 ${max}쪽까지 분석할 수 있습니다.`);
  return pages.sort((a, b) => a - b);
}
