import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { createHostedClient, HostedApiError, type HostedClient, type Session } from "./hostedClient";
import { historyKey } from "./hostedHistory";
import { MyReports } from "./MyReports";

// Reports and runs open on the analysis page, which owns upload, scope and result state.
export function HostedArchive({ client: injected }: { client?: HostedClient }) {
  const navigate = useNavigate();
  const client = useMemo(() => injected ?? createHostedClient({ base: import.meta.env.VITE_HOSTED_API_BASE || "/hosted-api" }), [injected]);
  const [session, setSession] = useState<Session | null>(null);
  const [phase, setPhase] = useState<"checking" | "login" | "ready">("checking");
  const [error, setError] = useState("");
  const fail = useCallback((err: unknown) => {
    if (err instanceof HostedApiError) { setError(err.userMessage); if (err.status === 401) { setSession(null); setPhase("login"); } }
    else if (err instanceof Error) setError(err.message);
  }, []);
  const unavailable = useCallback(() => {}, []);
  useEffect(() => {
    const controller = new AbortController();
    client.session(controller.signal).then(next => { setSession(next); setPhase("ready"); }).catch(err => {
      if (controller.signal.aborted) return;
      if (err instanceof HostedApiError && err.status === 401) setPhase("login"); else fail(err);
    });
    return () => controller.abort();
  }, [client, fail]);
  return <section className="surface live-report hosted-analysis hosted-archive" aria-label="보관함">
    {phase === "checking" && !error && <p role="status">보관함 불러오는 중…</p>}
    {phase === "login" && <div className="hosted-panel"><p>보관함을 보려면 로그인해 주세요.</p><Link className="hosted-primary" to="/analyze">분석 화면에서 로그인</Link></div>}
    {phase === "ready" && session && <p className="hosted-note archive-note">현재 로그인한 작업 공간에서 접근 가능한 보고서의 분석 결과입니다.</p>}
    {phase === "ready" && session && <MyReports key={historyKey(session)} client={client}
      onOpenRun={id => navigate({ pathname: "/analyze", hash: `#run-${id}` })}
      onOpenDocument={doc => navigate("/analyze", { state: { openDocumentId: doc.document_id } })}
      onUnavailable={unavailable} onFailure={fail} />}
    {error && <p className="hosted-error" role="alert">{error}</p>}
  </section>;
}
