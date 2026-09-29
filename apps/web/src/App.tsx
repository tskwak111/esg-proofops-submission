import { StaticDemo } from "./StaticDemo";
import { FormEvent, useCallback, useEffect, useRef, useState } from "react";
import { BrowserRouter, Link, Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router";
import { ClaimWorkspace, ReviewQueueWorkspace } from "./features/claims/ClaimWorkspace";
import { ComparisonWorkspace } from "./features/comparison/ComparisonWorkspace";
import { ReconciliationWorkspace } from "./features/reconciliation/ReconciliationWorkspace";
import { RunSummary, type Summary } from "./features/dashboard/RunSummary";
import { ExportWorkspace } from "./features/reports/ExportWorkspace";
import { RunQuality } from "./features/runs/RunQuality";
import { RunForm } from "./features/runs/RunForm";
import { RunProgress, type RunSnapshot } from "./features/runs/RunProgress";
import { ApiError, errorMessage, isSessionError, requestJson, type Session } from "./features/session/api";
import { CompanySelector, type RuntimeOptions, type UploadSelection } from "./features/upload/CompanySelector";
import { UploadForm, type ReadyDocumentVersion } from "./features/upload/UploadForm";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";
const UPLOAD_ORIGIN = import.meta.env.VITE_UPLOAD_ORIGIN;
const LOCAL_SYNTHETIC = import.meta.env.VITE_LOCAL_SYNTHETIC === "true";
const emptySelection: UploadSelection = { companyId: "", rightsProfileId: "", consentProfileId: "", runtimeBindingId: "" };
const emptyOptions: RuntimeOptions = { rights_profiles: [], consent_profiles: [], runtime_bindings: [], rule_packs: [], enabled_modes: [] };

type LocalSubmission = {
  worker_enabled: boolean;
  candidate_rule_pack_id: string | null;
  selected_pages: number[];
};

export function App() {
  return <BrowserRouter><SessionApp /></BrowserRouter>;
}

function SessionApp() {
  const navigate = useNavigate();
  const location = useLocation();
  const publicPage = location.pathname === "/" || ["/demo", "/analyze", "/review", "/report", "/live"].some(path => location.pathname === path || location.pathname.startsWith(`${path}/`));
  const [staticMode, setStaticMode] = useState(import.meta.env.VITE_DEMO_STATIC === "true");
  const [session, setSession] = useState<Session | null>(null);
  const [sessionStatus, setSessionStatus] = useState<"loading" | "ready" | "missing" | "error">("loading");
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [tenantId, setTenantId] = useState("");
  const [switchingTenant, setSwitchingTenant] = useState(false);
  const [selection, setSelection] = useState<UploadSelection>(emptySelection);
  const [options, setOptions] = useState<RuntimeOptions>(emptyOptions);
  const [readyVersion, setReadyVersion] = useState<ReadyDocumentVersion | null>(null);
  const [createdRun, setCreatedRun] = useState<RunSnapshot | null>(null);
  const [dataRevision, setDataRevision] = useState(0);
  const [isSavedDemo, setIsSavedDemo] = useState(false);
  const [demoRunId, setDemoRunId] = useState<string | null>(null);
  const tenantSwitchInFlight = useRef(false);

  const loadSession = useCallback(() => {
    const controller = new AbortController();
    setSessionStatus("loading"); setSessionError(null);
    requestJson<Session>(`${API_BASE}/v1/session`, { signal: controller.signal })
      .then(next => { if (!controller.signal.aborted) { setSession(next); setSessionStatus("ready"); } })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setSession(null);
        if (reason instanceof ApiError && reason.status === 401) return setSessionStatus("missing");
        setStaticMode(true); setSessionStatus("error"); setSessionError(errorMessage(reason, "세션을 확인하지 못했습니다."));
      });
    return () => controller.abort();
  }, []);

  useEffect(() => { if (!staticMode && !publicPage) return loadSession(); }, [loadSession, staticMode, publicPage]);

  useEffect(() => {
    setIsSavedDemo(false); setDemoRunId(null);
    if (!session || !session.tenant_id) return;
    const controller = new AbortController();
    requestJson<LocalSubmission>(`${API_BASE}/local/submission`, { signal: controller.signal })
      .then(async (localInfo) => {
        if (controller.signal.aborted) return;
        const saved = localInfo.worker_enabled === false;
        setIsSavedDemo(saved);
        if (saved) {
          try {
            const runsPage = await requestJson<{ items: Array<{ run_id: string }> }>(
              `${API_BASE}/v1/runs?limit=1`,
              { signal: controller.signal }
            );
            if (!controller.signal.aborted && runsPage.items && runsPage.items.length > 0) {
              setDemoRunId(runsPage.items[0].run_id);
            }
          } catch {
            // ignore non-fatal error
          }
        }
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setIsSavedDemo(false);
        setDemoRunId(null);
      });
    return () => controller.abort();
  }, [session]);

  const clearTenantData = useCallback(() => {
    setIsSavedDemo(false); setDemoRunId(null);
    setSelection(emptySelection); setOptions(emptyOptions); setReadyVersion(null); setCreatedRun(null);
    setDataRevision(current => current + 1);
  }, []);

  const invalidateSession = useCallback(() => {
    setSession(null); clearTenantData(); setSessionStatus("missing");
  }, [clearTenantData]);

  const switchTenant = async (event: FormEvent) => {
    event.preventDefault();
    if (!session || tenantSwitchInFlight.current) return;
    tenantSwitchInFlight.current = true; setSwitchingTenant(true); setSessionError(null);
    try {
      const next = await requestJson<Session>(`${API_BASE}/v1/session/tenant`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token },
        body: JSON.stringify({ tenant_id: tenantId.trim() }),
      });
      clearTenantData(); setSession(next); setTenantId(""); navigate("/documents/new", { replace: true });
    } catch (reason: unknown) {
      if (isSessionError(reason)) return invalidateSession();
      setSessionError(errorMessage(reason, "해당 테넌트를 선택할 수 없습니다. 멤버십과 식별자를 확인해 주세요."));
    } finally {
      tenantSwitchInFlight.current = false; setSwitchingTenant(false);
    }
  };

  const canEdit = session?.role === "editor" || session?.role === "admin";
  const canRun = session?.role === "admin";
  const loginUrl = `/auth/login?return_to=${encodeURIComponent(location.pathname.startsWith("/runs/") ? location.pathname : "/documents/new")}`;
  const changed = useCallback(() => setDataRevision(current => current + 1), []);

  if (staticMode || publicPage) return <StaticDemo />;

  return <main>
    {sessionStatus === "loading" ? <p role="status">세션을 확인하는 중입니다.</p> : null}
    {sessionStatus === "missing" ? <section aria-labelledby="login-heading"><h1 id="login-heading">로그인이 필요합니다</h1><p>현재 로그인 정보가 없습니다. 구성된 로그인 경로로 이동해 주세요.</p><a href={loginUrl}>로그인</a><p>로그인 서비스를 사용할 수 없다는 안내가 나오면 관리자에게 계정 구성을 요청하세요.</p></section> : null}
    {sessionStatus === "error" ? <section aria-labelledby="session-error-heading"><h1 id="session-error-heading">세션 확인 실패</h1><p role="alert">{sessionError}</p><button type="button" onClick={loadSession} style={{ minHeight: 44 }}>다시 확인</button></section> : null}
    {sessionStatus === "ready" && session ? <>
      <section aria-labelledby="tenant-heading"><h2 id="tenant-heading">작업 공간</h2>
        {isSavedDemo ? (
          <p>선택됨: <strong>분석 작업 공간</strong> · 권한: {session.role ?? "viewer"}</p>
        ) : session.tenant_id ? (
          <p>선택됨: <code>{session.tenant_id}</code> · 권한: {session.role ?? "없음"}</p>
        ) : (
          <p role="status">선택된 테넌트가 없습니다.</p>
        )}
        <details open={!session.tenant_id}>
          <summary>{session.tenant_id ? "작업 공간 변경" : "작업 공간 선택"}</summary>
        <form onSubmit={switchTenant} aria-label="작업 테넌트 선택" style={{ display: "flex", alignItems: "end", gap: 8, flexWrap: "wrap" }}>
          <label htmlFor="tenant-id">테넌트 식별자 <input id="tenant-id" value={tenantId} onChange={event => setTenantId(event.target.value)} placeholder="00000000-0000-4000-8000-000000000000" required /></label>
          <button type="submit" disabled={switchingTenant || !tenantId.trim()} style={{ minHeight: 44 }}>{switchingTenant ? "선택 중…" : session.tenant_id ? "테넌트 변경" : "테넌트 선택"}</button>
        </form>
        </details>
        {sessionError ? <p role="alert">{sessionError}</p> : null}
      </section>
      {session.tenant_id ? <Routes>
        <Route path="/" element={<Navigate to={isSavedDemo && demoRunId ? `/runs/${demoRunId}/claims` : "/documents/new"} replace />} />
        <Route path="/documents/new" element={<DocumentFlow session={session} selection={selection} options={options} readyVersion={readyVersion} canEdit={canEdit} canRun={canRun}
          setSelection={setSelection} setOptions={setOptions} setReadyVersion={version => { setReadyVersion(version); setCreatedRun(null); }}
          onSessionInvalid={invalidateSession} onRunCreated={run => { setCreatedRun(run); navigate(`/runs/${run.run_id}`); }}
          isSavedDemo={isSavedDemo} demoRunId={demoRunId} />} />
        <Route path="/runs/:runId" element={<RunHome session={session} tenantKey={session.tenant_id} initialRun={createdRun} onSessionInvalid={invalidateSession} isSavedDemo={isSavedDemo} />} />
        <Route path="/runs/:runId/claims" element={<RunClaims session={session} onSessionInvalid={invalidateSession} onDataChanged={changed} isSavedDemo={isSavedDemo} />} />
        <Route path="/runs/:runId/claims/:claimId" element={<RunClaims session={session} onSessionInvalid={invalidateSession} onDataChanged={changed} isSavedDemo={isSavedDemo} />} />
        <Route path="/runs/:runId/claims/:claimId/reconciliation" element={<RunReconciliation session={session} onSessionInvalid={invalidateSession} isSavedDemo={isSavedDemo} />} />
        <Route path="/runs/:runId/reviews" element={<RunReviews key={`reviews:${dataRevision}`} session={session} onSessionInvalid={invalidateSession} onDataChanged={changed} isSavedDemo={isSavedDemo} />} />
        <Route path="/runs/:runId/report" element={<RunReport session={session} onSessionInvalid={invalidateSession} isSavedDemo={isSavedDemo} />} />
        <Route path="/runs/:runId/comparison" element={<RunComparison session={session} onSessionInvalid={invalidateSession} isSavedDemo={isSavedDemo} />} />
        <Route path="*" element={<section><h1>화면을 찾을 수 없습니다</h1><p><Link to="/documents/new">새 문서 등록으로 이동</Link></p></section>} />
      </Routes> : null}
    </> : null}
  </main>;
}

type DocumentFlowProps = {
  session: Session;
  selection: UploadSelection;
  options: RuntimeOptions;
  readyVersion: ReadyDocumentVersion | null;
  canEdit: boolean;
  canRun: boolean;
  setSelection: (value: UploadSelection) => void;
  setOptions: (value: RuntimeOptions) => void;
  setReadyVersion: (value: ReadyDocumentVersion | null) => void;
  onSessionInvalid: () => void;
  onRunCreated: (run: RunSnapshot) => void;
  isSavedDemo?: boolean;
  demoRunId?: string | null;
};

export function DocumentFlow(props: DocumentFlowProps) {
  return <section>
    <h1>새 문서 등록</h1>
    {props.isSavedDemo && props.demoRunId ? (
      <p><Link to={`/runs/${props.demoRunId}/claims`}>기존 분석 결과 보기 ↗</Link></p>
    ) : (
      <p>공시 PDF를 등록하고 서버 검증이 끝난 실제 문서 버전으로 분석을 시작합니다.</p>
    )}
    {!props.canEdit ? <p role="alert">현재 {props.session.role ?? "미지정"} 권한으로는 기업 또는 문서를 등록할 수 없습니다.</p> : null}
    <CompanySelector apiBase={API_BASE} csrfToken={props.session.csrf_token} tenantKey={props.session.tenant_id!} value={props.selection} canCreate={props.canEdit}
      onChange={props.setSelection} onOptionsChange={props.setOptions} onSessionInvalid={props.onSessionInvalid} />
    <UploadForm apiBase={API_BASE} uploadOrigin={UPLOAD_ORIGIN} csrfToken={props.session.csrf_token} tenantKey={props.session.tenant_id!} selection={props.selection} canEdit={props.canEdit}
      onReadyVersion={props.setReadyVersion} onSessionInvalid={props.onSessionInvalid} />
    {props.readyVersion ? <RunForm apiBase={API_BASE} csrfToken={props.session.csrf_token} tenantKey={props.session.tenant_id!} version={props.readyVersion} selection={props.selection}
      options={props.options} canRun={props.canRun} onRunCreated={props.onRunCreated} onSessionInvalid={props.onSessionInvalid} /> : null}
  </section>;
}

export function RunNav({ runId, isSavedDemo }: { runId: string; isSavedDemo?: boolean }) {
  return (
    <header style={{ marginBottom: 16 }}>
      {isSavedDemo ? <p>기존 분석 결과</p> : null}
      <nav aria-label="실행 화면">
        <ul style={{ display: "flex", gap: 16, listStyle: "none", padding: 0, margin: 0, flexWrap: "wrap" }}>
          <li><Link to="/documents/new">새 분석</Link></li>
          <li><Link to={`/runs/${runId}`}>진행</Link></li>
          <li><Link to={`/runs/${runId}/claims`}>주장</Link></li>
          <li><Link to={`/runs/${runId}/reviews`}>검토 큐</Link></li>
          <li><Link to={`/runs/${runId}/report`}>보고서</Link></li>
          <li><Link to={`/runs/${runId}/comparison`}>연도 비교</Link></li>
        </ul>
      </nav>
    </header>
  );
}

function RunHome({ session, tenantKey, initialRun, onSessionInvalid, isSavedDemo }: { session: Session; tenantKey: string; initialRun: RunSnapshot | null; onSessionInvalid: () => void; isSavedDemo?: boolean }) {
  const { runId = "" } = useParams();
  const [run, setRun] = useState<RunSnapshot | null>(() => initialRun?.run_id === runId ? initialRun : null);
  const [state, setState] = useState<"loading" | "ready" | "pending" | "error">(run ? "ready" : "loading");
  const [message, setMessage] = useState("");
  const [summary, setSummary] = useState<Summary | null>(null);
  const [summaryState, setSummaryState] = useState<"loading" | "ready" | "pending" | "error">("loading");
  const [summaryEpoch, setSummaryEpoch] = useState(0);
  const refreshSummary = useCallback(() => setSummaryEpoch(current => current + 1), []);
  useEffect(() => {
    const controller = new AbortController();
    setRun(initialRun?.run_id === runId ? initialRun : null);
    setState(initialRun?.run_id === runId ? "ready" : "loading");
    setMessage("");
    requestJson<RunSnapshot>(`${API_BASE}/v1/runs/${runId}`, { signal: controller.signal }).then(next => {
      if (!controller.signal.aborted) { setRun(next); setState("ready"); }
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) return onSessionInvalid();
      setState(reason instanceof ApiError && reason.status === 409 ? "pending" : "error");
      setMessage(errorMessage(reason, "실행 상태를 불러오지 못했습니다."));
    });
    return () => controller.abort();
  }, [initialRun, onSessionInvalid, runId, tenantKey]);
  useEffect(() => {
    const controller = new AbortController();
    setSummary(null); setSummaryState("loading");
    requestJson<Summary>(`${API_BASE}/v1/runs/${runId}/summary`, { signal: controller.signal }).then(next => {
      if (!controller.signal.aborted) { setSummary(next); setSummaryState("ready"); }
    }).catch((reason: unknown) => {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) return onSessionInvalid();
      setSummaryState(reason instanceof ApiError && (reason.status === 404 || reason.status === 409) ? "pending" : "error");
    });
    return () => controller.abort();
  }, [onSessionInvalid, runId, summaryEpoch, tenantKey]);
  return <section>
    <h1>분석 실행</h1>
    <RunNav runId={runId} isSavedDemo={isSavedDemo} />
    {state === "loading" ? <p role="status">실행 상태를 불러오는 중입니다.</p> : null}
    {state === "pending" ? <p role="status">실행 산출물이 아직 준비되지 않았습니다.</p> : null}
    {state === "error" ? <p role="alert">{message}</p> : null}
    {run ? <RunProgress apiBase={API_BASE} csrfToken={session.csrf_token} tenantKey={tenantKey} initialRun={run} onSessionInvalid={onSessionInvalid} onRunChanged={refreshSummary} /> : null}
    {summaryState === "loading" ? <p role="status">실행 요약을 불러오는 중입니다.</p> : null}
    {summaryState === "pending" ? <p role="status">실행 요약은 현재 태깅 스냅샷 뒤에 게시됩니다.</p> : null}
    {summaryState === "error" ? <p role="alert">실행 요약을 불러오지 못했습니다.</p> : null}
    {summary ? <RunSummary summary={summary} /> : null}
    <RunQuality key={`${tenantKey}:${runId}`} apiBase={API_BASE} runId={runId} csrfToken={session.csrf_token} onSessionInvalid={onSessionInvalid} />
  </section>;
}

function RunClaims({ session, onSessionInvalid, onDataChanged, isSavedDemo }: { session: Session; onSessionInvalid: () => void; onDataChanged: () => void; isSavedDemo?: boolean }) {
  const { runId = "", claimId } = useParams();
  return <>
    <RunNav runId={runId} isSavedDemo={isSavedDemo} />
    {claimId ? <p><Link to={`/runs/${runId}/claims/${claimId}/reconciliation`}>공시 원문 대조 · C1–C4 검토</Link></p> : null}
    <ClaimWorkspace apiBase={API_BASE} csrfToken={session.csrf_token} tenantKey={session.tenant_id!} session={session} runId={runId} claimId={claimId}
      onSessionInvalid={onSessionInvalid} onDataChanged={onDataChanged} />
  </>;
}

function RunReconciliation({ session, onSessionInvalid, isSavedDemo }: { session: Session; onSessionInvalid: () => void; isSavedDemo?: boolean }) {
  const { runId = "", claimId = "" } = useParams();
  return <>
    <RunNav runId={runId} isSavedDemo={isSavedDemo} />
    <p><Link to={`/runs/${runId}/claims/${claimId}`}>주장 상세로 돌아가기</Link></p>
    <ReconciliationWorkspace key={`${session.tenant_id}:${runId}:${claimId}`} apiBase={API_BASE}
      runId={runId} claimId={claimId} session={session} onSessionInvalid={onSessionInvalid} />
  </>;
}

function RunReport({ session, onSessionInvalid, isSavedDemo }: { session: Session; onSessionInvalid: () => void; isSavedDemo?: boolean }) {
  const { runId = "" } = useParams();
  return <>
    <RunNav runId={runId} isSavedDemo={isSavedDemo} />
    <ExportWorkspace key={`${session.tenant_id}:${runId}`} apiBase={API_BASE}
      csrfToken={session.csrf_token} tenantKey={session.tenant_id!} runId={runId} onSessionInvalid={onSessionInvalid} />
  </>;
}

function RunComparison({ session, onSessionInvalid, isSavedDemo }: { session: Session; onSessionInvalid: () => void; isSavedDemo?: boolean }) {
  const { runId = "" } = useParams();
  return <><RunNav runId={runId} isSavedDemo={isSavedDemo} /><ComparisonWorkspace key={`${session.tenant_id}:${runId}`} apiBase={API_BASE}
    csrfToken={session.csrf_token} tenantKey={session.tenant_id!} runId={runId}
    canCompare={session.role === "editor" || session.role === "admin"} onSessionInvalid={onSessionInvalid} /></>;
}

function RunReviews({ session, onSessionInvalid, onDataChanged, isSavedDemo }: { session: Session; onSessionInvalid: () => void; onDataChanged: () => void; isSavedDemo?: boolean }) {
  const { runId = "" } = useParams();
  return <><RunNav runId={runId} isSavedDemo={isSavedDemo} /><ReviewQueueWorkspace apiBase={API_BASE} csrfToken={session.csrf_token} tenantKey={session.tenant_id!} session={session} runId={runId}
    onSessionInvalid={onSessionInvalid} onDataChanged={onDataChanged} localSynthetic={LOCAL_SYNTHETIC} /></>;
}
