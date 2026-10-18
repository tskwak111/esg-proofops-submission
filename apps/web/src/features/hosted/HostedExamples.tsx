import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { loadExampleCatalog, loadExample, selectedExample, type Example, type ExampleCatalogEntry } from "./exampleCatalog";
export { exampleRuns } from "./exampleCatalog";
import { HostedApiError, type HostedClient, type Session } from "./hostedClient";
import { enterHostedSession } from "./guestAccess";
import { ResultView } from "./HostedResult";
import { resultScope } from "./resultModel";
import { ux1CompanyName } from "./ux1Metadata";

const companyName = (example: Pick<Example, "document" | "name">) => {
  const name = ux1CompanyName({ ...example.document, display_name: example.name }) ?? example.name;
  return /^(?:N사|네이버|NAVER)$/i.test(name) ? "NAVER" : name;
};

/** Reuse the original editorial demo layout, with counts from the actual result. */
export function ExampleOverview({ example }: { example: Example }) {
  const { counts } = example;
  const hold = counts.total - counts.confirmed;
  const scope = resultScope(example.result, example.document.page_count, example.run.selected_pages);
  const metrics = [["추출 주장", counts.total, "공시 문장"], ["원문 검증", counts.verified, "원문 대조 완료"], ["확정", counts.confirmed, "규칙 판정"], ["보류", hold, "근거·처리 상태 확인 필요"]] as const;
  const percent = (count: number) => `${counts.total ? count / counts.total * 100 : 0}%`;
  return <>
    <section className="metrics" aria-label="분석 요약" data-testid="example-metrics">{metrics.map(([label, count, note]) => <div key={label}><span>{label}</span><strong>{count}<small>건</small></strong><p>{note}</p></div>)}</section>
    <section className="overview-grid">
      <div className="surface funnel-card"><div className="card-heading"><div><p className="eyebrow">PROCESS FUNNEL</p><h2>처리 흐름</h2></div><span>추출 → 원문 검증 → 확정·보류</span></div>
        <div className="funnel-list">{metrics.map(([label, count], index) => <div className="funnel-row" key={label}><span className="step-name"><b>{String(index + 1).padStart(2, "0")}</b>{label}</span><div className="bar-track"><div style={{ width: percent(count) }} /></div><strong>{count}</strong></div>)}</div>
        <p className="caption">선택한 {scope.pages.length}쪽의 추출 주장 {counts.total}건 = 확정 {counts.confirmed}건 + 보류 {hold}건. 원문 검증은 문구·위치 대조 결과입니다.</p>
      </div>
      <div className="surface grade-card"><p className="eyebrow">DECISION DISTRIBUTION</p><h2>등급과 보류</h2><div className="example-grade-bars">{[...Object.entries(counts.confirmedGrades), ["보류", hold] as const].map(([grade, count]) => <div className="example-grade-row" key={grade}><span>{grade}</span><div className={`grade-line ${grade === "보류" ? "pending" : grade.toLowerCase()}`}><i style={{ width: percent(count) }} /></div><strong>{count}</strong></div>)}</div><p className="caption">E3·E2·E1·E0 합계 {counts.confirmed}건 · 보류는 등급에 포함하지 않습니다.</p></div>
    </section>
  </>;
}

/** One representative report; query parameters cannot fetch other companies. */
export function HostedExamples({ client }: { client: HostedClient }) {
  const [catalog, setCatalog] = useState<ExampleCatalogEntry[]>([]);
  const [loaded, setLoaded] = useState<Record<string, Example | null>>({});
  const [params, setParams] = useSearchParams();
  const [refresh, setRefresh] = useState(0);
  const [busy, setBusy] = useState(true);
  const [login, setLogin] = useState(false);
  const [error, setError] = useState("");
  const [resultError, setResultError] = useState("");
  const [session, setSession] = useState<Session | null>(null);
  const report = params.get("report");
  const selected = selectedExample(catalog, null);
  const example = selected ? loaded[selected.identity] : undefined;
  const year = selected?.name.match(/(?:19|20)\d{2}/)?.[0];
  const scope = example ? resultScope(example.result, example.document.page_count, example.run.selected_pages) : null;
  useEffect(() => {
    const runId = example?.run.run_id;
    if (runId && report !== runId) setParams(current => { current.set("report", runId); return current; }, { replace: true });
  }, [example, report, setParams]);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setError(""); setLogin(false); setCatalog([]); setLoaded({});
    void (async () => {
      try {
        const current = await enterHostedSession(client, controller.signal);
        if (controller.signal.aborted) return;
        setSession(current);
        if (!current) { setLogin(true); return; }
        const next = await loadExampleCatalog(client, controller.signal);
        if (!controller.signal.aborted) setCatalog(next);
      } catch (reason) {
        if (controller.signal.aborted) return;
        if (reason instanceof HostedApiError && [401, 403].includes(reason.status)) setSession(null);
        if (reason instanceof HostedApiError && reason.status === 401) setLogin(true);
        else setError(reason instanceof HostedApiError ? reason.userMessage : "분석 사례를 불러오지 못했습니다. 다시 시도해 주세요.");
      } finally { if (!controller.signal.aborted) setBusy(false); }
    })();
    return () => controller.abort();
  }, [client, refresh]);
  useEffect(() => {
    setResultError("");
    if (!selected || example !== undefined) return;
    const controller = new AbortController();
    void loadExample(client, selected, controller.signal).then(result => {
      if (!controller.signal.aborted) setLoaded(current => ({ ...current, [selected.identity]: result }));
    }).catch(reason => {
      if (!controller.signal.aborted) setResultError(reason instanceof HostedApiError ? reason.userMessage : "보고서 결과를 불러오지 못했습니다. 목록을 새로고침해 주세요.");
    });
    return () => controller.abort();
  }, [client, selected, example]);
  return <main className="static-main demo-main examples-main">
    <div className="breadcrumb"><Link to="/">홈</Link><span>/</span>분석 사례</div>
    <section className="demo-heading example-editorial-heading"><div><p className="eyebrow">ANALYSIS{year ? ` · ${year}` : ""}</p><h1>분석 사례{selected ? ` · ${companyName(selected)}${year ? ` ${year}` : ""}` : ""}</h1><p>실제 보고서에서 추출한 주장과 원문 근거, 규칙 판정을 탐색할 수 있습니다.</p>{scope && <div className="badges"><span className="badge amber">분석 범위: {scope.pages.length}/{scope.total ?? "전체 쪽 미제공"}쪽</span><span className="badge">{example?.completion.title} · 읽기 전용</span></div>}</div></section>
    {busy && <p role="status">분석 사례 목록 불러오는 중…</p>}
    {login ? <section className="surface hosted-empty"><h2>로그인 후 분석 사례를 확인하세요</h2><p>보고서 접근 권한을 확인하기 위해 로그인이 필요합니다.</p><Link className="primary-link" to="/analyze?return_to=examples">로그인하고 사례 보기 →</Link></section> : <>
      {error && <p role="alert">{error}</p>}
      {!busy && !error && !catalog.length && <section className="surface hosted-empty"><h2>아직 소개할 분석 사례가 없습니다</h2><p>이름과 완료 결과를 확인할 수 있는 보고서가 준비되면 표시됩니다.</p><Link to="/analyze">내 보고서로 이동 →</Link></section>}
      {selected && example === undefined && !resultError && <p role="status">{companyName(selected)} 분석 결과 불러오는 중…</p>}
      {resultError && <p role="alert">{resultError}</p>}
      {example === null && <p role="status">현재 정책으로 완료된 분석 결과가 없습니다. 잠시 후 다시 확인해 주세요.</p>}
      {example && report === example.run.run_id && <><ExampleOverview example={example} /><section className="hosted-analysis example-result" aria-label="선택한 보고서 결과"><ResultView key={example.run.run_id} result={example.result} reportName={example.name} companyName={companyName(example)} pageCount={example.document.page_count} selectedPages={example.run.selected_pages} runStatus={example.run.status} client={client} runId={example.run.run_id} role={session?.role} readOnly /></section></>}
      <p className="caption">다른 보고서는 <Link to="/analyze">분석 페이지</Link>에서 PDF를 올리면 바로 확인할 수 있습니다</p>
      <button type="button" className="hosted-secondary" disabled={busy} onClick={() => setRefresh(value => value + 1)}>목록 새로고침</button>
    </>}
  </main>;
}
