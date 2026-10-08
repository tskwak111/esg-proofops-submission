import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Link } from "react-router";
import { createHostedClient, HostedApiError, parsePageSelection, runOutcome, terminalStatuses, type HostedClient, type HostedDocument, type HostedRun, type Runtime, type Session } from "./hostedClient";

const statusLabel: Record<string, string> = { queued: "대기 중", running: "분석 중", accounting_pending: "비용 정산 확인 중", partial_blocked: "보류", completed: "완료", cancelled: "취소됨", failed: "실패" };

export function RuntimeOff({ runtime }: { runtime: Runtime }) {
  return <div className="hosted-notice" role="status">
    <h3>현재 실시간 분석이 꺼져 있습니다</h3>
    <p>서버에서 실시간 분석(OCR·모델)을 아직 열지 않았습니다. 업로드·접수는 시험 운영 중이며 {runtime.accept_new_runs ? "분석 접수는 열려 있으나 결과는 보류로 표시됩니다." : "새 분석 접수는 중지되어 있습니다."}</p>
    <p><Link className="primary-link" to="/demo">저장된 분석 사례 보기 ↗</Link></p>
  </div>;
}

export function RunOutcomeView({ run }: { run: HostedRun }) {
  const outcome = runOutcome(run);
  return <div className={`hosted-outcome hosted-${outcome.kind}`} role="status" aria-live="polite">
    <p className="eyebrow">분석 {run.run_id.slice(0, 8)}</p>
    <h3>{outcome.title}<small> · {run.selected_pages.join(", ")}쪽</small></h3>
    {outcome.kind === "pending" && <p>{statusLabel[run.status] ?? run.status}
      {typeof run.queue_position === "number" && ` · 대기 순번 ${run.queue_position}`}
      {typeof run.progress === "number" && ` · 진행 ${Math.round(run.progress * 100)}%`}
      {typeof run.progress === "object" && run.progress && ` · 진행 ${run.progress.done}/${run.progress.total}`}</p>}
    {outcome.reasons.map(reason => <p key={reason}>{reason}</p>)}
  </div>;
}

type Phase = "checking" | "login" | "ready";

export function HostedAnalysis({ client: injected }: { client?: HostedClient }) {
  const client = useMemo(() => injected ?? createHostedClient({ base: import.meta.env.VITE_HOSTED_API_BASE || "/hosted-api" }), [injected]);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [phase, setPhase] = useState<Phase>("checking");
  const [session, setSession] = useState<Session | null>(null);
  const [code, setCode] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [doc, setDoc] = useState<HostedDocument | null>(null);
  const [pages, setPages] = useState("");
  const [run, setRun] = useState<HostedRun | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [deleted, setDeleted] = useState(false);
  const abort = useRef<AbortController | null>(null);

  const fail = useCallback((err: unknown) => {
    if (err instanceof HostedApiError) {
      setError(err.userMessage);
      if (err.status === 401) { setPhase("login"); setSession(null); }
    } else if (err instanceof Error) setError(err.message);
  }, []);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const rt = await client.runtime();
        if (!live) return; setRuntime(rt);
        try { const s = await client.session(); if (!live) return; setSession(s); setPhase("ready"); }
        catch (err) { if (live && err instanceof HostedApiError && err.status === 401) setPhase("login"); else throw err; }
      } catch (err) { if (live) { fail(err); setPhase("login"); } }
    })();
    return () => { live = false; abort.current?.abort(); };
  }, [client, fail]);

  async function login(event: FormEvent) {
    event.preventDefault(); setError(""); setBusy("로그인 중");
    try { setSession(await client.login(code)); setCode(""); setPhase("ready"); } catch (err) { fail(err); } finally { setBusy(""); }
  }
  async function upload() {
    if (!file) return; setError(""); setBusy("업로드 중"); setDoc(null); setRun(null); setDeleted(false);
    try {
      if (new TextDecoder().decode(await file.slice(0, 4).arrayBuffer()) !== "%PDF") throw new Error("PDF 파일을 선택해 주세요.");
      if (runtime && file.size > runtime.limits.max_upload_bytes) throw new Error(`PDF가 ${Math.floor(runtime.limits.max_upload_bytes / 1048576)}MB를 넘습니다.`);
      const uploaded = await client.uploadDocument(file);
      setDoc(uploaded); setPages(Array.from({ length: Math.min(2, uploaded.page_count) }, (_, i) => i + 1).join(","));
    } catch (err) { fail(err); } finally { setBusy(""); }
  }
  async function start() {
    if (!doc) return; setError("");
    let selected: number[];
    try { selected = parsePageSelection(pages, doc.page_count, runtime?.limits.selected_pages ?? 2); } catch (err) { fail(err); return; }
    setBusy("접수 중");
    const controller = new AbortController(); abort.current = controller;
    try {
      const accepted = await client.createRun(doc.document_id, selected);
      setBusy("분석 진행");
      await client.pollRun(accepted, setRun, controller.signal);
    } catch (err) { fail(err); } finally { setBusy(""); }
  }
  async function cancel() {
    if (!run) return; setError("");
    try { abort.current?.abort(); setRun(await client.cancelRun(run.run_id)); } catch (err) { fail(err); }
  }
  async function remove() {
    if (!doc || !window.confirm("업로드한 원본 PDF와 분석 기록을 삭제합니다. 계속할까요?")) return;
    setError(""); setBusy("삭제 중");
    try {
      abort.current?.abort();
      const result = await client.deleteDocument(doc.document_id);
      if (result.status !== "completed") throw new Error("삭제가 완료되지 않았습니다. 잠시 후 다시 시도해 주세요.");
      setDoc(null); setRun(null); setFile(null); setDeleted(true);
    } catch (err) { fail(err); } finally { setBusy(""); }
  }
  async function logout() {
    try { await client.logout(); } catch { /* session may already be gone */ }
    setSession(null); setDoc(null); setRun(null); setPhase("login");
  }

  const running = !!run && !terminalStatuses.includes(run.status);
  let body: ReactNode;
  if (phase === "checking") body = <p role="status">서버 상태 확인 중…</p>;
  else if (runtime && !runtime.live_analysis && phase === "login" && !session) body = <>
    <RuntimeOff runtime={runtime} />
    <p className="hosted-note">그래도 접수·보류 흐름을 시험하려면 초대 코드로 로그인하세요.</p>{loginForm()}</>;
  else if (phase === "login") body = loginForm();
  else body = <>
    {runtime && !runtime.live_analysis && <RuntimeOff runtime={runtime} />}
    {runtime && <p className="hosted-notice-text" data-testid="upload-notice">{runtime.upload_notice}</p>}
    <div className="hosted-upload">
      <label>PDF 보고서<input type="file" accept=".pdf,application/pdf" disabled={!!busy || running} onChange={event => { setFile(event.target.files?.[0] ?? null); setDoc(null); setRun(null); setDeleted(false); }} /></label>
      <button type="button" disabled={!file || !!busy || running || runtime?.accept_uploads === false} onClick={() => void upload()}>업로드</button>
    </div>
    {runtime?.accept_uploads === false && <p className="hosted-error">현재 업로드가 닫혀 있습니다.</p>}
    {deleted && <p role="status">원본 PDF와 분석 기록을 삭제했습니다.</p>}
    {doc && <div className="hosted-pages">
      <p>업로드 완료 · 총 {doc.page_count}쪽. 분석할 쪽을 최대 {runtime?.limits.selected_pages ?? 2}개 고르세요.</p>
      <label>쪽 번호<input value={pages} onChange={event => setPages(event.target.value)} disabled={!!busy || running} aria-label="분석할 쪽 번호" placeholder="예: 26,28" /></label>
      <button type="button" disabled={!!busy || running || runtime?.accept_new_runs === false} onClick={() => void start()}>분석 시작</button>
      {runtime?.accept_new_runs === false && <p className="hosted-error">현재 새 분석 접수가 중지되어 있습니다.</p>}
    </div>}
    {run && <RunOutcomeView run={run} />}
    {running && <button type="button" onClick={() => void cancel()}>분석 취소</button>}
    {doc && <button type="button" className="hosted-danger" disabled={!!busy && busy !== "분석 진행"} onClick={() => void remove()}>업로드한 PDF 삭제</button>}
  </>;

  function loginForm() {
    return <form className="hosted-login" onSubmit={login}>
      <label>초대 코드<input type="password" autoComplete="off" value={code} onChange={event => setCode(event.target.value)} required minLength={32} /></label>
      <button disabled={!!busy || !code.trim()}>로그인</button>
    </form>;
  }

  return <section className="surface live-report hosted-analysis" aria-label="호스팅 분석">
    <p className="eyebrow">HOSTED ANALYSIS</p><h2>실시간 분석 (초대 코드)</h2>
    {session && <p className="hosted-session">로그인됨 · {session.role ?? "역할 없음"} <button type="button" onClick={() => void logout()}>로그아웃</button></p>}
    {body}
    {busy && <p role="status">{busy}…</p>}
    {error && <p className="hosted-error" role="alert">{error}</p>}
  </section>;
}
