import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { loadExamples, selectedExample, type Example } from "./exampleCatalog";
export { exampleRuns } from "./exampleCatalog";
import { HostedApiError, type HostedClient, type Session } from "./hostedClient";
import { enterHostedSession } from "./guestAccess";
import { GuestBanner } from "./GuestBanner";
import { ResultView } from "./HostedResult";
import { ux1CompanyName } from "./ux1Metadata";

/** Public, read-only showcase with compatibility for older tenant-only servers. */
export function HostedExamples({ client }: { client: HostedClient }) {
  const [examples, setExamples] = useState<Example[]>([]);
  const [params, setParams] = useSearchParams();
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(true);
  const [login, setLogin] = useState(false);
  const [error, setError] = useState("");
  const [session, setSession] = useState<Session | null>(null);
  const report = params.get("report");
  const selected = selectedExample(examples, report);
  useEffect(() => {
    if (selected && report !== selected.run.run_id) {
      setParams(current => { current.set("report", selected.run.run_id); return current; }, { replace: true });
    }
  }, [selected, report, setParams]);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setError(""); setLogin(false);
    void (async () => {
      try {
        const current = await enterHostedSession(client, controller.signal);
        if (controller.signal.aborted) return;
        setSession(current);
        if (!current) { setLogin(true); setExamples([]); return; }
        const next = await loadExamples(client, controller.signal);
        if (!controller.signal.aborted) setExamples(next);
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof HostedApiError && [401, 403].includes(reason.status)) { setExamples([]); setSession(null); }
        if (reason instanceof HostedApiError && reason.status === 401) setLogin(true);
        else setError(reason instanceof HostedApiError ? reason.userMessage : "분석 사례를 불러오지 못했습니다. 다시 시도해 주세요.");
      } finally { if (!controller.signal.aborted) setBusy(false); }
    })();
    return () => controller.abort();
  }, [client, refresh]);
  const companyName = (example: Example) => ux1CompanyName({ ...example.document, display_name: example.name }) ?? example.name;
  return <main className="static-main examples-main">
    <div className="breadcrumb"><Link to="/">홈</Link><span>/</span>분석 사례</div>
    <section className="surface">
      <div className="example-heading"><div><p className="eyebrow">저장된 실제 분석 결과</p><h1>분석 사례</h1></div></div>
      <GuestBanner session={session} />
      <p className="hosted-note">보고서를 선택하면 저장된 결과와 원문을 바로 확인할 수 있습니다. 공개 사례는 읽기 전용이며 새 분석을 실행하지 않습니다.</p>
      {busy && <p role="status">분석 사례 불러오는 중…</p>}
      {login ? <div className="hosted-empty"><h2>로그인 후 분석 사례를 확인하세요</h2><p>보고서 접근 권한을 확인하기 위해 로그인이 필요합니다.</p><Link className="primary-link" to="/analyze?return_to=examples">로그인하고 사례 보기 →</Link></div> : <>
        {error && <p role="alert">{error}</p>}
        {!busy && !error && !examples.length && <div className="hosted-empty"><h2>아직 소개할 분석 사례가 없습니다</h2><p>이름과 완료 결과를 확인할 수 있는 보고서가 준비되면 표시됩니다.</p><Link to="/analyze">내 보고서로 이동 →</Link></div>}
        {!!examples.length && <nav className="example-selector" aria-label="보고서 선택">{examples.map(example => <Link key={example.identity} className="example-chip" to={`/demo?report=${encodeURIComponent(example.run.run_id)}`} aria-current={selected?.run.run_id === example.run.run_id ? "true" : undefined} title={example.name}>
          <strong>{companyName(example)}</strong><small>확정 {example.counts.confirmed} · 보류 {example.counts.hold}</small><span>{example.completion.kind === "partial" ? example.completion.title : example.counts.provisional ? "이전 버전 결과" : "분석 완료"}</span>
        </Link>)}</nav>}
        {selected && <section className="hosted-analysis example-result" aria-label="선택한 보고서 결과">
          <ResultView key={selected.run.run_id} result={selected.result} reportName={selected.name} companyName={companyName(selected)} pageCount={selected.document.page_count} selectedPages={selected.run.selected_pages} runStatus={selected.run.status} client={client} runId={selected.run.run_id} role={session?.role} readOnly />
        </section>}
        <button type="button" className="hosted-secondary" disabled={busy} onClick={() => setRefresh(value => value + 1)}>목록 새로고침</button>
      </>}
    </section>
  </main>;
}
