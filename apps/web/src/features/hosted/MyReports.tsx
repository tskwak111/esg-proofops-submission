import { reportNames } from "./documentName";
import { runDocuments } from "./exampleCatalog";
import { useEffect, useRef, useState } from "react";
import { HostedApiError, type HostedClient, type HostedDocument, type ListedDocument, type ListedRun } from "./hostedClient";

const statuses: Record<string, string> = { queued: "대기 중", running: "분석 중", accounting_pending: "비용 정산 확인 중", partial_blocked: "판정 보류", completed: "완료", failed: "실패", cancelled: "취소됨" };
const stages: Record<string, string> = { queued: "대기", parse: "문서 파싱", extract: "주장 추출", tag: "요소 태깅", result: "결과 정리" };
function missingCollection(error: unknown): null {
  if (error instanceof HostedApiError && [404,405].includes(error.status)) return null;
  throw error;
}
function InventoryTime({ value }: { value: string }) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return <span>시각 미제공</span>;
  return <time dateTime={value}>{new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date)} KST</time>;
}
export function MyReports({ client, onOpenRun, onOpenDocument, onUnavailable, onFailure }: {
  client: HostedClient; onOpenRun: (id: string) => void; onOpenDocument: (doc: ListedDocument) => void;
  onUnavailable: (signal: AbortSignal) => void; onFailure: (err: unknown) => void;
}) {
  const [documents, setDocuments] = useState<ListedDocument[]>([]);
  const [extraDocuments, setExtraDocuments] = useState<HostedDocument[]>([]);
  const names = reportNames([...documents, ...extraDocuments]);
  const [runs, setRuns] = useState<ListedRun[]>([]);
  const [docCursor, setDocCursor] = useState<string | null>(null);
  const [runCursor, setRunCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [unavailable, setUnavailable] = useState({ documents: false, runs: false });
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const abort = useRef<AbortController | null>(null);
  useEffect(() => {
    const controller = new AbortController(); abort.current = controller;
    setBusy(true); setUnavailable({ documents: false, runs: false }); setError(""); setDocuments([]); setExtraDocuments([]); setRuns([]); setDocCursor(null); setRunCursor(null);
    void (async () => {
      try {
        const [docs, executions] = await Promise.all([client.listDocuments(null, controller.signal).catch(missingCollection), client.listRuns(null, controller.signal).catch(missingCollection)]);
        const extra = executions ? await runDocuments(client, executions.items, docs?.items ?? [], controller.signal) : [];
        if (controller.signal.aborted) return;
        setExtraDocuments(extra);
        setUnavailable({ documents: !docs, runs: !executions });
        if (docs) { setDocuments(docs.items); setDocCursor(docs.next_cursor); }
        if (executions) { setRuns(executions.items); setRunCursor(executions.next_cursor); }
        if (!docs || !executions) onUnavailable(controller.signal);
      } catch (err) {
        if (controller.signal.aborted) return;
        setError(err instanceof HostedApiError ? err.userMessage : "목록을 불러오지 못했습니다."); setBusy(false); onFailure(err); controller.abort();
      } finally { if (!controller.signal.aborted) setBusy(false); }
    })();
    return () => controller.abort();
  }, [client, refresh, onUnavailable, onFailure]);
  async function more(kind: "documents" | "runs") {
    setBusy(true); setError("");
    try {
      const signal = abort.current?.signal;
      if (kind === "documents") { const next = await client.listDocuments(docCursor, signal); if (!signal?.aborted) { setDocuments(old => [...old,...next.items.filter(item => !old.some(previous => previous.document_id === item.document_id))]); setDocCursor(next.next_cursor); } }
      else { const next = await client.listRuns(runCursor, signal); const extra = await runDocuments(client, next.items, [...documents, ...extraDocuments], signal); if (!signal?.aborted) { setExtraDocuments(old => [...old, ...extra]); setRuns(old => [...old,...next.items.filter(item => !old.some(previous => previous.run_id === item.run_id))]); setRunCursor(next.next_cursor); } }
    } catch (err) { if (!abort.current?.signal.aborted) { setError(err instanceof HostedApiError ? err.userMessage : "목록을 불러오지 못했습니다."); onFailure(err); } }
    finally { if (!abort.current?.signal.aborted) setBusy(false); }
  }
  return <section className="hosted-inventory" aria-label="이전 보고서 내역">
    <div className="hosted-section-heading"><div><h3>이전 보고서 내역</h3></div><button className="hosted-secondary" type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)}>목록 새로고침</button></div>
    {busy && <p role="status">보고서·실행 목록 불러오는 중…</p>}
    {error && <p role="alert">{error}</p>}
    {(unavailable.documents || unavailable.runs) && <p className="hosted-note" role="status">현재 서버는 {unavailable.documents && unavailable.runs ? "보고서·실행" : unavailable.documents ? "보고서" : "실행"} 목록 조회를 지원하지 않습니다. 지원되는 목록과 현재 탭의 기록, 기존 결과 링크를 사용할 수 있습니다.</p>}
    <>
        <div className="hosted-inventory-group"><h4>보고서 <span>{documents.length}</span></h4>
      <ul className="hosted-inventory-list">{documents.map(doc => <li key={doc.document_id}>
        <div className="hosted-inventory-info"><b>{names[doc.document_id] ?? doc.display_name}</b><p className="hosted-note">{doc.page_count}쪽 · 업로드 <InventoryTime value={doc.uploaded_at} /></p></div>
        <span className="hosted-state">업로드 완료</span>
        <button className="hosted-secondary" type="button" onClick={() => onOpenDocument(doc)}>범위 열기</button>
      </li>)}</ul>
      {!busy && !error && !unavailable.documents && !documents.length && <div className="hosted-empty"><p>보관 중인 보고서가 없습니다.</p><p className="hosted-note">분석 화면에서 PDF를 업로드하면 분석 범위를 확인할 수 있습니다.</p></div>}
      {docCursor && <button className="hosted-text-button" type="button" disabled={busy} onClick={() => void more("documents")}>보고서 더 보기</button>}
      </div>
      <div className="hosted-inventory-group"><h4>실행 <span>{runs.length}</span></h4>
      <ul className="hosted-inventory-list">{runs.map(run => <li key={run.run_id}>
        <div className="hosted-inventory-info"><b>{names[run.document_id] ?? `이름 미등록 보고서 · ${run.document_id.slice(0,8)}`}</b>
          <p className="hosted-note">분석 {run.run_id.slice(0,8)} · 접수 <InventoryTime value={run.created_at} /></p>
          <p className="hosted-note">{run.stage ? stages[run.stage] ?? "처리 중" : ""}{typeof run.progress === "number" ? ` · 진행 ${Math.round(Math.max(0,Math.min(1,run.progress))*100)}%` : ""}{typeof run.queue_position === "number" ? ` · 대기 순번 ${run.queue_position}` : ""}</p>
          {typeof run.progress === "number" && <progress max={1} value={Math.max(0,Math.min(1,run.progress))} aria-label={`분석 ${run.run_id.slice(0,8)} 진행률`} />}
        </div>
        <span className="hosted-state" data-status={run.status}>{statuses[run.status] ?? "상태 확인 필요"}</span>
        <button className="hosted-secondary" type="button" onClick={() => onOpenRun(run.run_id)}>{run.result_url ? "결과 열기" : "실행 상태 열기"}</button>
      </li>)}</ul>
      {!busy && !error && !unavailable.runs && !runs.length && <div className="hosted-empty"><p>표시할 실행이 없습니다.</p><p className="hosted-note">보고서의 범위를 확인한 뒤 분석을 시작하면 진행 상태와 결과가 여기에 표시됩니다.</p></div>}
      {runCursor && <button className="hosted-text-button" type="button" disabled={busy} onClick={() => void more("runs")}>실행 더 보기</button>}
      </div>
    </>
  </section>;
}
