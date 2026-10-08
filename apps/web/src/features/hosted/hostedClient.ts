// Hosted R108 control-plane client. Self-contained (no imports) so node tests can load it directly.
// Browser talks same-origin to `/hosted-api/*` (Vercel rewrite -> backend) so `__Host-` cookies stay first-party.

export type AnalysisBackend = "hosted" | "legacy" | "static";
export function resolveBackend(value: string | undefined): AnalysisBackend {
  return value === "hosted" || value === "static" ? value : "legacy";
}

export type Runtime = {
  analysis_backend: string; accept_new_runs: boolean; accept_uploads: boolean; live_analysis: boolean; live_disabled: boolean;
  upload_notice: string; consent_version: string; source_verifier?: string; profile?: string;
  provider_limits?: { openrouter?: { status: "unknown" | "blocked"; remaining_usd: string | null } };
  limits: { max_upload_bytes: number; selected_pages: number; max_pdf_pages: number; original_days: number; [key: string]: unknown };
};
export type Session = { user_id: string; tenant_id: string | null; role: string | null; csrf_token: string; expires_at: number };
export type HostedDocument = { document_id: string; page_count: number; size_bytes: number; expires: number; sha256: string };
export type HostedRun = {
  run_id: string; document_id: string; status: string; selected_pages: number[]; status_url: string;
  error_code: string | null; stage?: string | null; queue_position?: number | null; progress?: number | null;
  result_url?: string | null; result?: HostedResult | null;
};

// r108-hosted-result-v1 (CONTRACT.md P2). Only fields the UI reads are typed; unknown extra keys are tolerated.
export type SourceRefDto = {
  source_id: string; document_version_id: string; parse_manifest_id: string; page_num: number; printed_page_label: string | null;
  bbox: number[] | null; raw_text_sha256: string; quote: string; char_start: number; char_end: number;
  location_quality: string; verification_state: string;
};
export type ElementState = "present" | "absent" | "unknown" | "conflict" | "not_applicable";
export type ResultElement = {
  element_id: string; state: ElementState; evidence_refs: SourceRefDto[]; normalized_value: string | null; credited_from: string | null; reason_code: string | null;
};
export type GradeRange = { floor: string; ceiling: string; open_elements: string[] };
export type ResultDecision = {
  decision_revision?: number; tag_revision?: number; decision_status: string; evidence_grade: string | null; label: string | null;
  review_status: string; missing_elements?: string[]; grade_range?: GradeRange | null;
};
export type MissingEvidence = {
  element_id: string; evidence_status?: string; reason?: string; undetermined_reason?: string; engine_state?: string;
  candidate_refs?: SourceRefDto[]; search?: { absence_admissible?: boolean; scope_note?: string };
};
export type ResultClaim = {
  claim_id: string; page_num: number; original_page_num: number; quote: string; track: string | null; topic_ids?: string[];
  decision: ResultDecision | null; revision: number; source_refs: SourceRefDto[]; source_quality: string; elements: ResultElement[];
  hold_reason: string | null; possible_grade_range: GradeRange | null; confirmed_grade: string | null; missing_evidence: MissingEvidence[];
};
export type ResultReview = { review_id: string; run_id: string; claim_id: string; status: string; revision: number; base_tag_revision: number; reason_codes: string[] };
export type HostedResult = {
  schema: string; claims: ResultClaim[]; reviews: ResultReview[]; hold_reasons: string[]; page_map: number[];
  pipeline?: { status?: string; reason?: string | null; reporting_scope?: { report_year?: number; period_start?: string; period_end?: string } };
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
  INVITATION_INVALID: "초대 코드가 올바르지 않거나 이미 사용·만료되었습니다.",
  AUTH_REQUIRED: "로그인이 필요합니다. 초대 코드로 다시 로그인해 주세요.",
  CSRF_INVALID: "요청 검증에 실패했습니다. 페이지를 새로고침한 뒤 다시 로그인해 주세요.",
  RESOURCE_NOT_FOUND: "문서 또는 분석을 찾을 수 없습니다. 이미 삭제되었거나 보관 기간이 끝났을 수 있습니다.",
  FORBIDDEN: "이 작업을 할 권한이 없습니다.",
  PAYLOAD_TOO_LARGE: "PDF가 업로드 한도를 넘습니다.",
  PDF_CONTENT_TYPE_REQUIRED: "PDF 파일만 올릴 수 있습니다.",
  UPLOAD_DISABLED: "현재 업로드가 닫혀 있습니다.",
  ANALYSIS_DISABLED: "현재 새 분석 접수가 중지되어 있습니다.",
  SOURCE_EXPIRED: "원본 PDF 보관 기간(7일)이 끝났습니다. 다시 업로드해 주세요.",
  SOURCE_UNAVAILABLE: "원본 PDF를 사용할 수 없습니다. 다시 업로드해 주세요.",
  PAGE_SELECTION_INVALID: "쪽 선택을 확인해 주세요. 문서 범위 안에서 최대 2쪽까지 고를 수 있습니다.",
  IDEMPOTENCY_CONFLICT: "같은 요청 키로 다른 내용이 전송되었습니다. 처음부터 다시 시도해 주세요.",
  IDEMPOTENCY_KEY_INVALID: "요청 키가 올바르지 않습니다. 다시 시도해 주세요.",
  USER_RUN_LIMIT: "이미 진행 중인 분석이 있습니다. 끝난 뒤 다시 시도해 주세요.",
  QUEUE_FULL: "대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.",
  DAILY_RUN_LIMIT: "오늘의 분석 가능 횟수에 도달했습니다. 내일 다시 이용해 주세요.",
  BUDGET_EXCEEDED: "분석 비용 한도에 도달했습니다.",
  RATE_LIMITED: "요청이 많습니다. 잠시 후 다시 시도해 주세요.",
  ACCOUNTING_PENDING: "비용 정산 확인 중이라 새 분석을 접수할 수 없습니다.",
  DEPENDENCY_UNAVAILABLE: "서버 저장소를 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.",
  ANALYSIS_RESULT_UNAVAILABLE: "분석 결과가 아직 준비되지 않았습니다. 잠시 후 다시 확인해 주세요.",
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

  async function request<T>(method: "GET" | "POST", path: string, init: { json?: unknown; body?: BodyInit; headers?: Record<string, string>; signal?: AbortSignal } = {}): Promise<T> {
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
    return await response.json() as T;
  }

  return {
    get csrfToken() { return csrf; },
    async login(code: string): Promise<Session> {
      const session = await request<Session>("POST", "/v1/auth/invitation", { json: { code: code.trim() } });
      csrf = session.csrf_token; return session;
    },
    async session(): Promise<Session> {
      const session = await request<Session>("GET", "/v1/session");
      csrf = session.csrf_token; return session;
    },
    async logout(): Promise<void> { await request<void>("POST", "/v1/auth/logout"); csrf = ""; },
    runtime: () => request<Runtime>("GET", "/v1/runtime"),
    uploadDocument: (file: Blob, key = newIdempotencyKey(), signal?: AbortSignal) =>
      request<HostedDocument>("POST", "/v1/documents", { body: file, headers: { "Content-Type": "application/pdf", "Idempotency-Key": key }, signal }),
    createRun: (documentId: string, pages: number[], key = newIdempotencyKey()) =>
      request<HostedRun>("POST", "/v1/runs", { json: { document_id: documentId, selected_pages: pages }, headers: { "Idempotency-Key": key } }),
    getRun: (statusUrl: string, signal?: AbortSignal) => request<HostedRun>("GET", statusUrl, { signal }),
    cancelRun: (runId: string) => request<HostedRun>("POST", `/v1/runs/${encodeURIComponent(runId)}/cancel`),
    getResult: (resultUrl: string, signal?: AbortSignal) => request<HostedResult>("GET", resultUrl, { signal }),
    /** Reviewer/admin. If-Match is the quoted integer review revision (reviews[].revision); the server recomputes the grade. */
    resolveReview: (runId: string, reviewId: string, revision: number, body: ResolveBody, key = newIdempotencyKey()) =>
      request<ResolveResponse>("POST", `/v1/runs/${encodeURIComponent(runId)}/reviews/${encodeURIComponent(reviewId)}/resolve`, { json: body, headers: { "If-Match": `"${revision}"`, "Idempotency-Key": key } }),
    // Uploader (own document) or tenant admin; 5..1000 char reason + Idempotency-Key required. Others get 403.
    deleteDocument: (documentId: string, reason = "사용자 요청에 의한 원본 PDF 삭제", key = newIdempotencyKey()) =>
      request<{ deletion_id: string; status: string }>("POST", `/v1/documents/${encodeURIComponent(documentId)}/deletion-requests`, { json: { reason }, headers: { "Idempotency-Key": key } }),

    /** Poll until a terminal status. Backoff 1s -> 10s; honours 429 Retry-After; stops on abort. */
    async pollRun(first: HostedRun, onUpdate: (run: HostedRun) => void, signal?: AbortSignal): Promise<HostedRun> {
      let run = first; let delay = 1000; let failures = 0;
      onUpdate(run);
      while (!terminalStatuses.includes(run.status)) {
        await sleep(delay);
        if (signal?.aborted) return run;
        try {
          run = await this.getRun(run.status_url, signal); failures = 0; onUpdate(run);
          delay = Math.min(Math.round(delay * 1.5), 10000);
        } catch (error) {
          if (signal?.aborted) return run;
          if (!(error instanceof HostedApiError)) throw error;
          if (error.status === 429) { delay = Math.max(delay, (error.retryAfter ?? 5) * 1000); continue; }
          if ((error.status === 0 || error.status >= 502) && ++failures < 5) { delay = Math.min(delay * 2, 10000); continue; }
          throw error;
        }
      }
      return run;
    },
  };
}
export const terminalStatuses = ["partial_blocked", "completed", "cancelled", "failed"];
export type HostedClient = ReturnType<typeof createHostedClient>;

export const providerLimitCodes = ["OPENROUTER_HTTP_402", "OPENROUTER_HTTP_403"];
export const providerLimitMessage = "분석 서비스 한도 초과 — 운영자 확인 필요";
const blockedReasons: Record<string, string> = {
  OPENROUTER_HTTP_402: providerLimitMessage,
  OPENROUTER_HTTP_403: providerLimitMessage,
  LIVE_BINDING_UNAVAILABLE: "실시간 모델·OCR 분석이 서버에 연결되어 있지 않아 판정하지 않았습니다.",
  LINUX_SOURCE_READER_UNRESOLVED: "이 서버의 원문 판독기가 아직 승인되지 않아 분석하지 않았습니다. 주장·등급은 만들지 않았습니다.",
  LEASE_EXPIRED: "처리 중 작업이 중단되어 비용 정산 확인 전까지 보류되었습니다.",
  PREPARE_FAILED: "선택한 쪽을 준비하지 못했습니다.",
  HOSTED_PREPARATION_FAILED: "선택한 쪽을 준비하지 못했습니다.",
  R108_NEEDS_REVIEW: "일부 주장이 사람 검토를 기다리고 있어 전체를 확정하지 않았습니다.",
  SOURCE_VALIDATION_REQUIRED: "원문 대조가 끝나지 않아 판정하지 않았습니다.",
  CONSENSUS_UNRESOLVED: "모델 판독이 합의되지 않아 요소 상태가 확정되지 않았습니다.",
  PRELIMINARY_TAGS_UNRESOLVED: "예비 분류(트랙)가 합의되지 않아 요소를 판정하지 않았습니다.",
  RELATION_TAGS_UNRESOLVED: "주장과 근거의 관계가 정리되지 않아 요소를 판정하지 않았습니다.",
  REPLICA_UNRESOLVED: "반복 판독 결과가 일치하지 않습니다.",
};
/** Korean text for a backend hold/reason code; unknown codes are shown as codes, never dropped. */
export function holdReasonText(code: string): string {
  if (blockedReasons[code]) return blockedReasons[code];
  if (/^REVIEW:/.test(code)) return `${code.slice(7)} 요소 사람 확인 필요`;
  return `보류 사유 코드: ${code}`;
}
export function runOutcome(run: HostedRun): { kind: "pending" | "blocked" | "completed" | "cancelled" | "failed"; title: string; reasons: string[] } {
  switch (run.status) {
    case "partial_blocked": {
      const codes = run.result?.hold_reasons?.length ? run.result.hold_reasons : run.error_code ? [run.error_code] : [];
      const reasons = codes.length ? codes.map(holdReasonText) : ["분석이 일부만 진행되어 보류되었습니다."];
      reasons.push("보류는 ‘근거 없음’이 아닙니다. 판정되지 않은 주장의 근거는 확인되지 않은 상태이며, 확정 등급을 부여하지 않았습니다.");
      return { kind: "blocked", title: "보류", reasons };
    }
    case "completed": return { kind: "completed", title: "분석 완료", reasons: run.result ? [] : ["이 서버가 결과 상세를 아직 제공하지 않아 화면에 표시할 수 없습니다."] };
    case "cancelled": return { kind: "cancelled", title: "취소됨", reasons: [] };
    case "failed": return { kind: "failed", title: "실패", reasons: [holdReasonText(run.error_code ?? "알 수 없음")] };
    default: return { kind: "pending", title: run.status === "queued" ? "대기 중" : "분석 중", reasons: [] };
  }
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
