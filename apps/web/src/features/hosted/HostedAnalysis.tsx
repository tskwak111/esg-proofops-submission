import { ux1CompanyName, uploadMetadata } from "./ux1Metadata";
import { UX1_HASH } from "./ux1Model";
import { UX1Progress } from "./UX1Progress";
import { documentName, reportTitle } from "./documentName";
import { useCallback, useEffect, useId, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { completedRunPresentation, createHostedClient, holdReasonText, HostedApiError, parsePageSelection, providerLimitCodes, providerLimitMessage, resultExecution, runOutcome, terminalStatuses, type HostedClient, type HostedDocument, type HostedResult, type HostedRun, type Runtime, type Session, type ScopePlan } from "./hostedClient";
import { resultBrowserSummary, w18HoldBreakdown } from "./resultModel";
import { ResultView } from "./HostedResult";
import { enterHostedSession, isShowcaseRun } from "./guestAccess";
import { GuestBanner } from "./GuestBanner";
import { forgetDocumentHistory, historyKey, readHistory, rememberHistory, resultTabFromSearch, resultTabUrl, runIdFromHash, validResourceId, type HistoryEntry } from "./hostedHistory";

// Session-scoped history only: browser storage is disallowed (security contract);
// re-entry after reload uses the server run list API and #run links.
const historyMemory = new Map<string, string>();
const historyStore = {
  getItem: (key: string) => historyMemory.get(key) ?? null,
  setItem: (key: string, value: string) => void historyMemory.set(key, value),
  removeItem: (key: string) => void historyMemory.delete(key),
};

const scopeCategory: Record<string, string> = { ceo_message: "CEO 메시지", e_narrative: "환경 본문", assurance: "검증 의견서", gri_index: "GRI 색인", environmental_data: "환경 데이터", esg_data: "ESG 데이터", appendix: "부록", toc: "목차", conflict: "분류 충돌", unreadable: "판독 불가", unknown: "분류 미확인", other: "기타" };
const scopeReason: Record<string, string> = { section_boundaries_disagree: "구역 경계가 서로 다름", native_text_missing_or_unusable: "문자층을 읽을 수 없음", navigation_is_not_claim_text: "목차는 주장 본문에서 제외", evidence_search_only: "근거 검색 전용", native_heading: "쪽 제목 기준", section_container: "구역 분류 기준", front_20_pages: "앞쪽 20쪽 범위", ceo_title: "CEO 제목 확인", claim_extraction_candidate: "주장 추출 후보", environmental_section: "환경 구역", readable_environmental_body: "읽을 수 있는 환경 본문", physical_page_heading: "실제 쪽 제목", environmental_density: "환경 내용 밀도", source_role_unresolved: "기존 구역 분류 미확인", insufficient_environmental_body_density: "환경 본문 밀도 부족", "scope_only_intro_ceo_hierarchy;original_conflict_preserved": "CEO 구역으로 제안하되 원래 분류 충돌 보존" };
const scopeStatus: Record<string, string> = { candidate_only: "자동 선택 후보", proposed: "자동 선택 제안", ready: "범위 선택 완료", selected: "선택 완료", failed: "자동 선택 실패", unknown: "분류 미확인", conflict: "분류 충돌", unreadable: "판독 불가" };
const scopeReasonText = (reason: string) => scopeReason[reason] ?? ({ candidate_only: "자동 선택 후보", excluded_by_declared_cap: "분석 한도에 따른 제외" } as Record<string, string>)[reason] ?? "추가 분류 정보 확인 필요";
const stageLabel: Record<string, string> = { queued: "대기", parse: "문서 읽기", extract: "주장 추출", tag: "근거 대조", result: "결과 정리" };
const statusLabel: Record<string, string> = { queued: "대기 중", running: "분석 중", accounting_pending: "비용 정산 확인 중", partial_blocked: "보류", completed: "완료", cancelled: "취소됨", failed: "실패" };

export function RuntimeOff({ runtime }: { runtime: Runtime }) {
  return <div className="hosted-notice" role="status">
    <h3>실시간 분석을 일시 중지했습니다</h3>
    <p>현재 실시간 분석 서비스가 중지되어 있습니다. {runtime.accept_new_runs ? "분석 접수는 열려 있으나 결과는 보류로 표시됩니다." : "새 분석 접수는 중지되어 있습니다."}</p>
    <p><Link className="primary-link" to="/demo">저장된 분석 사례 보기 ↗</Link></p>
  </div>;
}

/** Keep counted extraction gaps consistent with the result header and example cards. */
export function runStateText(run: HostedRun, result?: HostedResult | null) {
  const current = result ?? run.result;
  const completion = current && completedRunPresentation(current, run.status);
  if (completion?.kind === "partial") return completion.title;
  if (current && (run.status === "partial_blocked" || run.status === "completed") && resultExecution(current, run.status).kind === "partial")
    return resultBrowserSummary(current).confirmed > 0 ? "부분 완료" : "보류";
  return statusLabel[run.status] ?? "상태 확인 중";
}

export function RunOutcomeView({ run }: { run: HostedRun }) {
  const outcome = runOutcome(run);
  const progress = typeof run.progress === "number" && Number.isFinite(run.progress) ? Math.min(Math.max(run.progress, 0), 1) : null;
  return <div className={`hosted-outcome hosted-${outcome.kind}`} role="status" aria-live="polite">
    <p className="eyebrow">분석 진행</p>
    <h3>{outcome.kind === "blocked" && run.result && (run.status === "partial_blocked" || run.status === "completed") ? runStateText(run) : outcome.title}<small> · {new Set(run.selected_pages).size}쪽</small></h3>
    {outcome.kind === "pending" && <>
      <p>{statusLabel[run.status] ?? "상태 확인 중"}{run.stage ? ` · ${stageLabel[run.stage] ?? "후속 처리"}` : ""}
        {typeof run.queue_position === "number" && ` · 대기 순번 ${run.queue_position}`}
        {progress !== null && ` · 진행 ${Math.round(progress * 100)}%`}</p>
      <progress max={1} value={progress ?? undefined} aria-label="분석 진행률" />
      {typeof run.claims_extracted === "number" && <p>추출된 주장 {run.claims_extracted}건 · 근거 대조 {run.claims_processed == null ? "집계 중" : `${run.claims_processed}건`}</p>}
      <UX1Progress run={run} />
      <p className="hosted-note">이 화면에서 결과가 자동으로 열립니다. 화면을 닫아도 내 보고서에서 진행 상태를 다시 확인할 수 있습니다.</p></>}
    {outcome.reasons.map(reason => <p key={reason}>{reason}</p>)}
  </div>;
}

type Phase = "checking" | "login" | "ready";
type Intake = { key: string; name: string; doc?: HostedDocument; error?: string; runId?: string; restoreRunId?: string; restored?: boolean; readOnly?: boolean };

export function HostedAnalysis({ client: injected }: { client?: HostedClient }) {
  const navigate = useNavigate();
  const location = useLocation();
  const operatorLogin = new URLSearchParams(location.search).has("operator");
  const openDocumentId = (location.state as { openDocumentId?: string } | null)?.openDocumentId;
  const client = useMemo(() => injected ?? createHostedClient({ base: import.meta.env.VITE_HOSTED_API_BASE || "/hosted-api" }), [injected]);
  const [connection, setConnection] = useState("");
  const [retryVersion, setRetryVersion] = useState(0);
  const [, setHistory] = useState<HistoryEntry[]>([]);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [phase, setPhase] = useState<Phase>("checking");
  const [session, setSession] = useState<Session | null>(null);
  const currentSession = useRef<Session | null>(session);
  currentSession.current = session;
  const [code, setCode] = useState("");
  const [loginMethod, setLoginMethod] = useState<"invitation" | "password">("password");
  const [accessRole, setAccessRole] = useState<"admin" | "organizer">("organizer");
  const [files, setFiles] = useState<File[]>([]);
  const [intake, setIntake] = useState<Intake[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const uploadAbort = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement | null>(null);
  // While a result is open the intake/history area folds into one line; it stays mounted so its state survives.
  const [openResults, setOpenResults] = useState<{ runId: string; name: string }[]>([]);
  const [intakeExpanded, setIntakeExpanded] = useState(false);
  const trackResult = useCallback((runId: string, name: string, open: boolean) => setOpenResults(items => [...items.filter(item => item.runId !== runId), ...(open ? [{ runId, name }] : [])]), []);
  const intakeAreaId = useId();
  const expire = useCallback(() => {
    uploadAbort.current?.abort(); setSession(null); setIntake([]); setFiles([]); setNotice(""); setHistory([]); setOpenResults([]); setPhase("login");
    if (fileInput.current) fileInput.current.value = "";
  }, []);
  const fail = useCallback((err: unknown) => {
    if (err instanceof HostedApiError) {
      setError(err.userMessage);
      if (err.status === 401) expire();
    } else if (err instanceof Error) setError(err.message);
  }, [expire]);

  useEffect(() => {
    let live = true; const controller = new AbortController();
    const unsubscribe = client.subscribeConnection(setConnection);
    (async () => {
      try {
        const rt = await client.runtime(controller.signal);
        if (!live) return; setRuntime(rt);
        try {
          const next = operatorLogin ? await client.session(controller.signal).catch(err => {
            if (err instanceof HostedApiError && err.status === 401) return null;
            throw err;
          }) : await enterHostedSession(client, controller.signal);
          if (live) {
            setSession(next); setPhase(next ? "ready" : "login");
            if (next && new URLSearchParams(window.location.search).get("return_to") === "examples" && !new URLSearchParams(window.location.search).has("operator")) navigate("/demo");
          }
        } catch (err) { if (live && !controller.signal.aborted) { setPhase("login"); throw err; } }
      } catch (err) { if (live && !controller.signal.aborted) fail(err); }
    })();
    return () => { live = false; controller.abort(); unsubscribe(); uploadAbort.current?.abort(); };
  }, [client, fail, retryVersion, navigate, operatorLogin]);

  const openRun = useCallback(async (id: string) => {
    if (!validResourceId(id)) { setError("올바른 분석 번호를 입력해 주세요."); return; }
    setError("");
    try {
      const next = await client.getRun(`/v1/runs/${id}`);
      const [document, readOnly] = await Promise.all([client.getDocument(next.document_id), isShowcaseRun(client, id)]);
      if (!session || !currentSession.current || historyKey(currentSession.current) !== historyKey(session)) return;
      const savedName = session ? readHistory(historyStore, historyKey(session)).find(entry => entry.documentId === document.document_id)?.name : undefined;
      const name = documentName(document) ?? reportTitle(savedName) ?? "분석 보고서";
      if (session && !readOnly) setHistory(rememberHistory(historyStore, historyKey(session), { documentId: document.document_id, name, runId: id, seenAt: Date.now() }));
      setIntake(items => items.some(item => item.runId === id) ? items : [...items, { key: id, name, doc: document, runId: id, restoreRunId: id, restored: true, readOnly }]);
      setIntakeExpanded(false);
      // A shared ?tab= link belongs to the run in its own anchor; any other run opens on the overview.
      window.history.replaceState(null, "", resultTabUrl(window.location, id, runIdFromHash(window.location.hash) === id ? resultTabFromSearch(window.location.search) : "overview"));
      requestAnimationFrame(() => {
        const article = documentElement(id);
        const heading = article?.querySelector<HTMLElement>('[data-testid="result-heading"]');
        if (heading) { heading.focus({ preventScroll: true }); (heading.closest(".result-heading") ?? heading).scrollIntoView({ block: "start", behavior: "instant" }); }
        else article?.scrollIntoView({ block: "start" });
      });
    } catch (err) { fail(err); }
  }, [client, fail, session]);
  function documentElement(id: string) { return document.getElementById(`run-${id}`); }
  useEffect(() => {
    if (!session) return;
    let live = true; const controller = new AbortController();
    const saved = readHistory(historyStore, historyKey(session)); setHistory(saved);
    void (async () => {
      const id = runIdFromHash(window.location.hash); if (live && id) await openRun(id);
    })();
    const changed = () => { const id = runIdFromHash(window.location.hash); if (id) void openRun(id); };
    window.addEventListener("hashchange", changed);
    return () => { live = false; controller.abort(); window.removeEventListener("hashchange", changed); };
  }, [session, client, fail, openRun]);
  useEffect(() => {
    if (!session || !openDocumentId || !validResourceId(openDocumentId)) return;
    const controller = new AbortController();
    void client.getDocument(openDocumentId, controller.signal).then(doc => {
      // Clear the hand-off so a reload or back navigation does not reopen it.
      navigate(location.pathname + location.search + location.hash, { replace: true, state: null });
      const name = documentName(doc) ?? "분석 보고서";
      setIntake(items => items.some(item => item.doc?.document_id === doc.document_id && !item.runId) ? items : [...items, { key: doc.document_id, name, doc, restored: true }]);
      setHistory(rememberHistory(historyStore, historyKey(session), { documentId: doc.document_id, name, seenAt: Date.now() }));
    }).catch(err => { if (!controller.signal.aborted) fail(err); });
    return () => controller.abort();
  }, [session, openDocumentId, client, fail, navigate, location.pathname, location.search, location.hash]);
  function remember(doc: HostedDocument, name: string, run?: HostedRun) {
    if (!session || !currentSession.current || historyKey(currentSession.current) !== historyKey(session)) return;
    setHistory(rememberHistory(historyStore, historyKey(session), { documentId: doc.document_id, name, runId: run?.run_id, seenAt: Date.now() }));
    if (run && validResourceId(run.run_id)) {
      setIntake(items => items.map(item => item.doc?.document_id === doc.document_id && !item.runId ? { ...item, runId: run.run_id } : item));
      window.history.replaceState(null, "", resultTabUrl(window.location, run.run_id, "overview"));
    }
  }
  async function login(event: FormEvent) {
    event.preventDefault(); setError(""); setBusy("로그인 중");
    try { const next = await (loginMethod === "password" ? client.loginPassword(code, accessRole) : client.login(code)); expire(); setSession(next); setPhase("ready"); if (new URLSearchParams(window.location.search).get("return_to") === "examples") navigate("/demo"); } catch (err) { fail(err); } finally { setCode(""); setBusy(""); }
  }
  async function upload() {
    const controller = new AbortController(); uploadAbort.current = controller;
    setError(""); setBusy("업로드 중");
    for (const file of files) {
      if (controller.signal.aborted) break;
      const key = crypto.randomUUID();
      try {
        if (new TextDecoder().decode(await file.slice(0, 4).arrayBuffer()) !== "%PDF") throw new Error("PDF 파일을 선택해 주세요.");
        if (runtime && file.size > runtime.limits.max_upload_bytes) throw new Error(`PDF가 ${Math.floor(runtime.limits.max_upload_bytes / 1048576)} MiB를 넘습니다.`);
        const metadata = { ...await uploadMetadata(file), policy_sha256: UX1_HASH };
        if (controller.signal.aborted) break;
        const doc = await client.uploadDocument(file, undefined, controller.signal, metadata);
        if (!controller.signal.aborted) { setIntake(items => [...items, { key, name: documentName(doc, file.name) ?? "분석 보고서", doc }]); remember(doc, documentName(doc, file.name) ?? "분석 보고서"); }
      } catch (err) {
        if (controller.signal.aborted) break;
        if (err instanceof HostedApiError && err.status === 401) { fail(err); break; }
        const message = err instanceof HostedApiError ? err.userMessage : err instanceof Error ? err.message : "업로드 실패";
        setIntake(items => [...items, { key, name: file.name, error: message }]);
      }
    }
    setFiles([]); if (fileInput.current) fileInput.current.value = ""; setBusy("");
  }
  function loginForm() {
    return <form className="hosted-login hosted-panel" onSubmit={login}>
      <div className="hosted-section-heading"><div><p className="eyebrow">접속</p><h3>초대 코드 또는 접근 비밀번호</h3></div></div>
      <div className="hosted-form-row hosted-login-methods">
        <label>로그인 방식<select value={loginMethod} onChange={event => { setLoginMethod(event.target.value as "invitation" | "password"); setCode(""); setError(""); }} disabled={!!busy}>
          <option value="password">접근 비밀번호 (재사용)</option><option value="invitation">초대 코드 (1회용)</option>
        </select></label>
        {loginMethod === "password" && <label>접근 계정<select value={accessRole} onChange={event => { setAccessRole(event.target.value as "admin" | "organizer"); setCode(""); }} disabled={!!busy}>
          <option value="organizer">대회측</option><option value="admin">관리자</option>
        </select></label>}
      </div>
      <div className="hosted-form-row">
      <label>초대 코드 또는 접근 비밀번호<input type="password" autoComplete={loginMethod === "password" ? "current-password" : "off"} value={code} onChange={event => setCode(event.target.value)} required minLength={loginMethod === "password" ? 16 : 32} maxLength={128} disabled={!!busy} /></label>
      <button className="hosted-primary" type="submit" disabled={!!busy}>입장</button>
      </div>
      <p className="hosted-note">10분 동안 5회 로그인에 실패하면 15분 동안 잠깁니다. 대회측 계정은 본인이 올린 보고서와 분석 결과만 볼 수 있습니다.</p>
    </form>;
  }
  const collapsed = phase === "ready" && openResults.length > 0 && !intakeExpanded;
  const hashRun = typeof window === "undefined" ? null : runIdFromHash(window.location.hash);
  const focusedResult = openResults.find(item => item.runId === hashRun) ?? openResults.at(-1);
  let body: ReactNode;
  if (phase === "checking") body = <><p role="status">분석 서비스 연결 중…</p>{error && <button type="button" onClick={() => { setError(""); setRetryVersion(value => value + 1); }}>연결 다시 시도</button>}</>;
  else if (phase === "login") body = <>
    {runtime && !runtime.live_analysis && <RuntimeOff runtime={runtime} />}
    {loginForm()}
  </>;
  else body = <>
    <div className="hosted-intake-area" id={intakeAreaId} hidden={collapsed}>
    {runtime && !runtime.live_analysis && <RuntimeOff runtime={runtime} />}
    <section className="hosted-panel hosted-upload-panel" aria-label="PDF 업로드">
    <div className="hosted-section-heading"><div><h3>보고서 업로드</h3></div><Link className="hosted-secondary hosted-archive-link" to="/archive">보관함으로 이동 <span aria-hidden="true">→</span></Link></div>
    {runtime && <p className="hosted-notice-text" data-testid="upload-notice">{runtime.upload_notice}</p>}
    <div className="hosted-upload hosted-form-row">
      <label>PDF 보고서<input ref={fileInput} type="file" accept=".pdf,application/pdf" multiple disabled={!!busy} onChange={event => setFiles(Array.from(event.target.files ?? []))} /></label>
      <button className="hosted-primary" type="button" disabled={!files.length || !!busy || runtime?.accept_uploads === false} onClick={() => void upload()}>업로드</button>
    </div>
    {runtime?.accept_uploads === false && <p className="hosted-error">현재 업로드가 닫혀 있습니다.</p>}
    </section>
    </div>
    <ul className="hosted-documents" aria-label="문서별 분석 상태">
      {intake.map(item => <li key={item.key}>
        {item.doc ? <HostedDocumentAnalysis name={item.name} doc={item.doc} client={client} runtime={runtime} session={session} initialRunId={item.restoreRunId} restored={item.restored} readOnly={item.readOnly} onResult={trackResult} onAccepted={run => remember(item.doc!, item.name, run)} onExpired={expire} onRemoved={() => { setIntake(items => items.filter(next => next.doc?.document_id !== item.doc!.document_id)); if (session) setHistory(forgetDocumentHistory(historyStore, historyKey(session), item.doc!.document_id)); setNotice(`${item.name} 원본 PDF와 분석 기록을 삭제했습니다.`); }} />
          : <article><h3>{item.name}</h3><p role="alert" className="hosted-error">{item.error}</p></article>}
      </li>)}
    </ul>
  </>;
  return <section className={`surface live-report hosted-analysis ${collapsed ? "is-intake-collapsed" : ""}`} aria-label="보고서 분석">
    {phase === "ready" && focusedResult && <nav className="hosted-result-bar" aria-label="현재 분석">
      <p><span>분석</span> <span aria-hidden="true">›</span> <strong>{focusedResult.name}</strong></p>
      <button type="button" className="hosted-secondary" aria-expanded={!collapsed} aria-controls={intakeAreaId} onClick={() => setIntakeExpanded(value => !value)}>{collapsed ? "새 분석 열기" : "새 분석 접기"}</button>
    </nav>}
    <GuestBanner session={session} />
    {session?.role === "guest" && new URLSearchParams(window.location.search).has("operator") && <section className="hosted-operator-login" aria-labelledby="admin-login-heading"><h3 id="admin-login-heading">관리자 로그인</h3>{loginForm()}</section>}
    {Object.values(runtime?.provider_limits ?? {}).some(limit => limit.status === "blocked") && <p className="hosted-error" role="alert">{providerLimitMessage}</p>}
    {connection && <p className="hosted-notice" role="status" aria-live="polite">{connection}</p>}
    {body}
    {notice && <p role="status">{notice}</p>}
    {busy && <p role="status">{busy}…</p>}
    {error && <p className="hosted-error" role="alert">{error}</p>}
  </section>;
}

function HostedDocumentAnalysis({ name, doc, client, runtime, session, initialRunId, restored, readOnly = false, onResult, onAccepted, onExpired, onRemoved }: {
  name: string; doc: HostedDocument; client: HostedClient; runtime: Runtime | null; session: Session | null; initialRunId?: string; restored?: boolean; readOnly?: boolean; onResult?: (runId: string, name: string, open: boolean) => void; onAccepted: (run: HostedRun) => void; onExpired: () => void; onRemoved: () => void;
}) {
  // The server default profile is used as-is; there is no mode picker.
  const [profile] = useState(runtime?.pipeline_profile ?? "unattended-v1");
  const [pages, setPages] = useState("");
  const [evidencePages, setEvidencePages] = useState("");
  const [reportYear, setReportYear] = useState("");
  const [periodStart, setPeriodStart] = useState("");
  const [periodEnd, setPeriodEnd] = useState("");
  const [scope, setScope] = useState<ScopePlan | null>(null);
  const [run, setRun] = useState<HostedRun | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [scopeConflict, setScopeConflict] = useState(false);
  const [result, setResult] = useState<HostedResult | null>(null);
  const abort = useRef<AbortController | null>(null);
  const resultRegion = useRef<HTMLDivElement | null>(null);
  const focusedRun = useRef<string | null>(null);
  // A result link shows a placeholder until the stored result arrives instead of the intake card.
  const [loadingResult, setLoadingResult] = useState(!!initialRunId);

  const fail = useCallback((err: unknown) => {
    if (err instanceof HostedApiError) {
      setError(err.userMessage);
      if (err.code === "SCOPE_CONFLICT") setScopeConflict(true);
      if (err.status === 401) onExpired();
    } else if (err instanceof Error) setError(err.message);
  }, [onExpired]);

  useEffect(() => {
    let live = true; const controller = new AbortController(); abort.current = controller;
    setBusy("범위 계획 불러오는 중");
    void (async () => {
      try {
        if (initialRunId) {
          setBusy("");
          const first = await client.getRun(`/v1/runs/${initialRunId}`, controller.signal);
          if (!live) return;
          const finished = await client.pollRun(first, next => { if (live) setRun(next); }, controller.signal);
          if (live && !controller.signal.aborted) await showResult(finished, controller.signal);
        } else if (!readOnly) {
          const next = restored ? await client.getScopePlan(doc.document_id, controller.signal) : await client.scopePlan(doc.document_id);
          if (live) applyScope(next);
        }
      } catch (err) { if (live && !controller.signal.aborted) fail(err); }
      finally { if (live) { setBusy(""); setLoadingResult(false); } }
    })();
    return () => { live = false; controller.abort(); };
  }, [client, doc.document_id, fail, initialRunId, restored, readOnly]);

  useEffect(() => {
    if (!result || !run || focusedRun.current === run.run_id) return;
    const heading = resultRegion.current?.querySelector<HTMLElement>('[data-testid="result-heading"]');
    if (heading) {
      focusedRun.current = run.run_id;
      heading.focus({ preventScroll: true });
      (heading.closest(".result-heading") ?? heading).scrollIntoView({ block: "start", behavior: "instant" });
    }
  }, [!!result, run?.run_id]);

  const resultRunId = result && run ? run.run_id : null;
  useEffect(() => {
    if (!resultRunId || !onResult) return;
    onResult(resultRunId, name, true);
    return () => onResult(resultRunId, name, false);
  }, [resultRunId, name, onResult]);

  function applyScope(next: ScopePlan) {
    setScope(next); setScopeConflict(false);
    setReportYear(String(next.reporting_scope?.report_year ?? ""));
    setPeriodStart(next.reporting_scope?.period_start ?? "");
    setPeriodEnd(next.reporting_scope?.period_end ?? "");
    const fallback = next.plan.status === "failed" && !next.selection ? next.plan.suggested_front_pages ?? [] : [];
    setPages((next.plan.selected_claim_pages.length ? next.plan.selected_claim_pages : fallback.slice(0, 10)).join(","));
    setEvidencePages((next.plan.selected_evidence_pages.length ? next.plan.selected_evidence_pages : fallback.slice(0, 30)).join(","));
  }
  async function reloadScope() {
    if (!doc) return;
    setError(""); setBusy("최신 계획 불러오는 중");
    try { applyScope(await client.getScopePlan(doc.document_id)); } catch (err) { fail(err); } finally { setBusy(""); }
  }
  function editedPeriod() {
    if (!scope?.reporting_scope && !reportYear && !periodStart && !periodEnd) return undefined;
    if (!/^\d{4}$/.test(reportYear) || !periodStart || !periodEnd || periodStart > periodEnd) throw new Error("보고연도와 보고기간 시작·종료일을 확인해 주세요.");
    return { report_year: Number(reportYear), period_start: periodStart, period_end: periodEnd };
  }
  function periodChanged() {
    return reportYear !== String(scope?.reporting_scope?.report_year ?? "") || periodStart !== (scope?.reporting_scope?.period_start ?? "") || periodEnd !== (scope?.reporting_scope?.period_end ?? "");
  }
  async function saveScope() {
    if (readOnly || !doc || !scope) return;
    setError(""); setBusy("범위 저장 중");
    try {
      applyScope(await client.editScope(doc.document_id, scope,
        parsePageSelection(pages, doc.page_count, 10), parsePageSelection(evidencePages, doc.page_count, 30), periodChanged() ? editedPeriod() : undefined));
    } catch (err) { fail(err); } finally { setBusy(""); }
  }
  async function start() {
    if (readOnly || !doc || !scope) return;
    setError(""); setBusy("접수 중");
    abort.current?.abort();
    const controller = new AbortController(); abort.current = controller;
    try {
      const claims = parsePageSelection(pages, doc.page_count, 10);
      const evidence = parsePageSelection(evidencePages, doc.page_count, 30);
      const period = periodChanged() ? editedPeriod() : undefined;
      const changed = periodChanged() || !scope.selection || claims.join(",") !== scope.plan.selected_claim_pages.join(",") || evidence.join(",") !== scope.plan.selected_evidence_pages.join(",");
      const current = changed ? await client.editScope(doc.document_id, scope, claims, evidence, period) : scope;
      if (controller.signal.aborted) return;
      applyScope(current);
      const accepted = await client.createRun(doc.document_id, current, undefined, profile);
      if (controller.signal.aborted) return;
      setRun(accepted); setResult(null); setBusy(""); onAccepted(accepted);
      const finished = await client.pollRun(accepted, next => { if (!controller.signal.aborted) setRun(next); }, controller.signal);
      if (!controller.signal.aborted) await showResult(finished, controller.signal);
    } catch (err) { if (!controller.signal.aborted) fail(err); }
    finally { if (abort.current === controller) setBusy(current => current === "접수 중" ? "" : current); }
  }
  async function showResult(finished: HostedRun, signal: AbortSignal) {
    if (finished.status !== "partial_blocked" && finished.status !== "completed") return;
    if (finished.result_url) {
      const next = await client.getResult(finished.result_url, signal, UX1_HASH);
      if (!signal.aborted) setResult(next);
    } else if (finished.result) setResult(finished.result);
  }
  async function reloadResult() {
    setError("");
    try { if (run?.result_url) setResult(await client.getResult(run.result_url, undefined, UX1_HASH)); } catch (err) { fail(err); }
  }
  async function cancel() {
    if (readOnly || !run) return; setError(""); setBusy("취소 중");
    try { const cancelled = await client.cancelRun(run.run_id); abort.current?.abort(); setRun(cancelled); } catch (err) { fail(err); }
    finally { setBusy(""); }
  }
  async function remove() {
    if (readOnly || !doc || !window.confirm("업로드한 원본 PDF와 분석 기록을 삭제합니다. 계속할까요?")) return;
    setError(""); setBusy("삭제 중");
    try {
      const deletion = await client.deleteDocument(doc.document_id);
      if (deletion.status !== "completed") throw new Error("삭제가 완료되지 않았습니다. 잠시 후 다시 시도해 주세요.");
      abort.current?.abort(); onRemoved();
    } catch (err) {
      if (err instanceof HostedApiError && err.status === 403) setError("삭제 권한이 없습니다. 본인이 올린 문서이거나 작업 공간 관리자여야 삭제할 수 있습니다.");
      else fail(err);
    } finally { setBusy(""); }
  }
  const IntakeSurface = result ? "details" : "div";
  const running = !!run && !terminalStatuses.includes(run.status);
  const providerBlocked = Object.values(runtime?.provider_limits ?? {}).some(limit => limit.status === "blocked")
    || (!!run?.error_code && providerLimitCodes.includes(run.error_code));
  const intakeSurface = <IntakeSurface className={result ? "hosted-panel hosted-run-summary" : "hosted-intake"}>
    {result && <summary><span className="hosted-summary-name">실행 요약 · {name}</span></summary>}
    <div className="hosted-section-heading">{!result && <h3>{name}</h3>}
    <p className="hosted-state" data-status={run?.status} role="status" data-testid="document-status">{run ? `${runStateText(run, result)}${run.status === "queued" && run.queue_position ? ` · 대기 ${run.queue_position}번째` : run.status === "running" && run.stage ? ` · ${stageLabel[run.stage] ?? "후속 처리"}` : ""}` : scope ? "분석 접수 가능" : "범위 계획 준비 중"}</p></div>
    <p className="hosted-note">{scope ? `선택 요약: 주장 ${scope.plan.selected_claim_pages.length}쪽 · 근거 검색 ${scope.plan.selected_evidence_pages.length}쪽` : run ? `접수한 선택 범위 ${run.selected_pages.length}쪽` : "분석 범위를 준비하고 있습니다."}</p>
    {!result && runtime?.accept_new_runs === false && <p className="hosted-error">현재 새 분석 접수가 중지되어 있습니다.</p>}
    {readOnly && <p className="hosted-note">공개 분석 사례 · 읽기 전용</p>}
    {!readOnly && <details className="hosted-scope" open={!run}><summary>분석 범위와 보고기간</summary><div className="hosted-pages">
      <p>총 {doc.page_count}쪽. 자동 선택: 주장 최대 10쪽 · 근거 검색 최대 30쪽(주장 포함). 수정하지 않으면 이 범위로 진행합니다.</p>
      {!scope && <button type="button" disabled={!!busy} onClick={() => { setBusy("자동 범위 계획 생성 중"); void client.scopePlan(doc.document_id).then(applyScope).catch(fail).finally(() => setBusy("")); }}>자동 범위 계획 생성</button>}
      {scope && <>
        <p>계획 상태: {scopeStatus[scope.plan.status] ?? "범위 확인 필요"} · 선택 범위만 분석하며 판정에 필요한 원문 확인이 남은 항목은 보류로 표시합니다.</p>
        {scope.plan.failure_reasons.map(reason => <p role="alert" key={reason}>자동 범위 선택 실패: {scopeReasonText(reason)}</p>)}
        {scope.plan.status === "failed" && <p className="hosted-note">앞쪽 제안 쪽을 기본값으로 넣었습니다. 원본을 확인하고 수동 범위를 지정하세요. 자동 분류 실패·판독 불명확 상태는 유지되며 근거 검증을 대신하지 않습니다.</p>}
        {scopeConflict && <button type="button" disabled={!!busy || running} onClick={() => void reloadScope()}>최신 계획 다시 불러오기</button>}
        <div className="hosted-selection">
        <label>주장 쪽 번호<input value={pages} onChange={event => setPages(event.target.value)} disabled={!!busy || running} aria-label="주장 쪽 번호" /></label>
        <label>근거 검색 쪽 번호<input value={evidencePages} onChange={event => setEvidencePages(event.target.value)} disabled={!!busy || running} aria-label="근거 검색 쪽 번호" /></label>
        <button className="hosted-secondary" type="button" disabled={!!busy || running || scopeConflict} onClick={() => void saveScope()}>범위 수정 저장</button>
        </div>
        <details><summary>쪽별 분류와 선택 이유</summary><table className="hosted-scope-table"><thead><tr><th>원본 쪽</th><th>분류</th><th>선택</th><th>이유</th></tr></thead><tbody>{scope.plan.pages.map(page => <tr key={page.page}><td>{page.page}</td><td>{scopeCategory[page.category] ?? "분류 확인 필요"}</td><td>{scope.plan.selected_claim_pages.includes(page.page) ? "주장·근거" : scope.plan.selected_evidence_pages.includes(page.page) ? "근거 전용" : "제외"}</td><td>{page.reasons.map(reason => scopeReasonText(reason)).join(" · ")}</td></tr>)}</tbody></table></details>
      </>}

    </div></details>}
    {!readOnly && !result && run?.status !== "completed" && run?.status !== "partial_blocked" && <div className="hosted-start-row">
    <button className="hosted-primary" type="button" disabled={!!busy || running || !scope || scopeConflict || providerBlocked || runtime?.accept_new_runs === false} onClick={() => void start()}>분석 시작</button>
    </div>}
    {run && <RunOutcomeView run={result ? { ...run, result } : run} />}
    {run?.error_code && !(result && w18HoldBreakdown(result).length) && <p className="hosted-error">{holdReasonText(run.provider_error_code ?? run.error_code)}</p>}
    </IntakeSurface>;
  return <article className="hosted-document" data-testid="document-analysis" id={run ? `run-${run.run_id}` : undefined}>
    {!(result && run) && (loadingResult && !error && (!run || terminalStatuses.includes(run.status))
      ? <div className="result-skeleton" role="status" aria-label="분석 결과 불러오는 중"><i className="result-skeleton-title" /><i className="result-skeleton-line" /><span className="result-skeleton-tabs"><i /><i /><i /></span><i className="result-skeleton-block" /></div>
      : intakeSurface)}
    {result && run && <div ref={resultRegion} className="hosted-result-region"><ResultView reportName={documentName(doc) ?? reportTitle(name)} companyName={ux1CompanyName(doc)} pageCount={doc.page_count} selectedPages={run.selected_pages} runStatus={run.status} result={result} role={session?.role} readOnly={readOnly} client={client} runId={run.run_id} onReload={reloadResult} runSummary={intakeSurface} /></div>}
    {!readOnly && running && <button className="hosted-secondary" type="button" disabled={!!busy} onClick={() => void cancel()}>분석 취소</button>}
    {!readOnly && <div className="hosted-document-footer">
    {doc && <button type="button" className="hosted-danger" disabled={!!busy} onClick={() => void remove()}>업로드한 PDF 삭제</button>}
    </div>}

    {busy && <p role="status">{busy}…</p>}
    {error && <p className="hosted-error" role="alert">{error}</p>}
    {providerBlocked && <p className="hosted-error">{providerLimitMessage}</p>}
  </article>;
}
