import type { HostedResult, ResultClaim } from "./hostedClient";
import { analyzedData, claimExplanation, reportInsights } from "./ux1Model";
import { holdReasonLabel, pageRanges, resultBrowserSummary, resultScope, w18HoldBreakdown } from "./resultModel";

export function WhyGrade({ result, claim }: { result: HostedResult; claim: ResultClaim }) {
  const explanation = claimExplanation(result, claim);
  return <section className="ux1-why" aria-label="왜 이 등급인지"><h4>왜 이 등급인가요?</h4><p>{explanation.text}</p></section>;
}
export function HoldReasonList({ result, onHoldReason }: { result: HostedResult; onHoldReason?: (reason: string)=>void }) {
  const holds = w18HoldBreakdown(result);
  return holds.length ? <ul className="ux1-holds">{holds.slice(0,3).map(h=><li key={h.reason}>{onHoldReason
    ? <button type="button" title={h.reason} onClick={()=>onHoldReason(h.reason)}>{holdReasonLabel(h.reason)}<b>{h.count}건</b></button>
    : <span title={h.reason}>{holdReasonLabel(h.reason)} · <b>{h.count}건</b></span>}</li>)}</ul> : <p>보류 주장이 없습니다.</p>;
}
const pct = (n: number, d: number) => d ? Math.round(n / d * 100) : 0;
// Track labels already read "관리체계형"; a summary names them as claims.
const trackClaims = (label: string) => `${label} 주장`;
const gradeRows = [
  { key: "E3", label: "입증요소 충분", color: "var(--g3)" },
  { key: "E2", label: "추가 입증 필요", color: "var(--g2)" },
  { key: "E1", label: "확인 요소 부족", color: "var(--g1)" },
  { key: "E0", label: "입증요소 부재", color: "var(--g0)" },
];
/** Overview dashboard: one-line insight, four counts, status bar with hold reasons, confirmed grade distribution. */
export function ResultDashboard({ result, pageCount, selectedPages, onHoldReason }: { result: HostedResult; pageCount?: number; selectedPages?: number[]; onHoldReason?: (reason: string)=>void }) {
  const counts = resultBrowserSummary(result), scope = resultScope(result, pageCount ?? result.w16_source_document?.page_count, selectedPages);
  const byTrack = new Map<string, number>();
  for (const row of reportInsights(result).needs) byTrack.set(row.track, (byTrack.get(row.track) ?? 0) + row.count);
  const needs = [...byTrack].map(([label, count]) => ({ label, count })).sort((a, b) => b.count - a.count);
  const needsTotal = needs.reduce((sum, row) => sum + row.count, 0);
  const { total, confirmed, provisional, hold } = counts, e0 = counts.confirmedGrades.E0 ?? 0;
  const status = [
    { label: "확정", n: confirmed, color: "var(--g3)" },
    ...(provisional ? [{ label: "추가 확인 필요", n: provisional, color: "var(--g1)" }] : []),
    { label: "보류", n: hold, color: "var(--hold)" },
  ];
  return <section className="rd-overview" aria-label="보고서 요약">
    <div className="rd-insight"><span>핵심 요약</span><p>
      {confirmed ? <>확정 {confirmed}건 중 {e0 ? <><b>{e0}건({pct(e0, confirmed)}%)이 E0(근거 부재)</b>입니다.</> : "E0(근거 부재)는 없습니다."}</> : <>확정된 주장이 없습니다. 보류 {hold}건의 사유를 먼저 확인하세요.</>}
      {needs.length > 0 && <> 보강이 필요한 {needsTotal}건 중 <b>{trackClaims(needs[0].label)} {needs[0].count}건</b>부터 확인하세요.</>}
    </p></div>
    <div className="rd-kpis">
      <div><span>추출 주장</span><strong>{total}<small>건</small></strong><em>분석 범위 {scope.pages.length}{scope.total !== null && ` / ${scope.total}`}쪽<br />원문 검증 {counts.verified}건</em></div>
      <div className="rd-key"><span>확정</span><strong>{confirmed}<small>건</small></strong><em>전체의 {pct(confirmed, total)}%<br />근거로 판정 완료</em></div>
      <div><span>보류</span><strong>{hold}<small>건</small></strong><em>전체의 {pct(hold, total)}%<br />추가 확인 필요{provisional ? ` · 이전 버전 ${provisional}건` : ""}</em></div>
      <div><span>근거 보강 필요</span><strong>{needsTotal}<small>건</small></strong><em>빠진 요소가 있는 확정 주장{needs.length > 0 && <><br />{needs.map(row => `${row.label} ${row.count}`).join(" · ")}</>}</em></div>
    </div>
    <div className="rd-panels">
      <div className="rd-panel">
        <header><h3>판정 상태</h3><span>전체 {total}건</span></header>
        <div className="rd-bar" role="img" aria-label={status.map(s => `${s.label} ${s.n}건`).join(", ")}>{total > 0 && status.map(s => s.n > 0 && <i key={s.label} style={{ width: `${s.n / total * 100}%`, background: s.color }} />)}</div>
        <div className="rd-legend">{status.map(s => <span key={s.label}><i className="rd-sw" style={{ background: s.color }} />{s.label}<b>{s.n}</b></span>)}</div>
        <h4 className="rd-holds-h">주요 보류 사유</h4>
        <HoldReasonList result={result} onHoldReason={onHoldReason} />
      </div>
      <div className="rd-panel">
        <header><h3>근거 등급 분포</h3><span>확정 {confirmed}건 기준</span></header>
        {confirmed ? <>
          <div className="rd-bar" role="img" aria-label={gradeRows.map(g => `${g.key} ${counts.confirmedGrades[g.key] ?? 0}건`).join(", ")}>{gradeRows.map(g => (counts.confirmedGrades[g.key] ?? 0) > 0 && <i key={g.key} style={{ width: `${(counts.confirmedGrades[g.key] ?? 0) / confirmed * 100}%`, background: g.color }} />)}</div>
          <div className="rd-rows">{gradeRows.map(g => <div className="rd-row" key={g.key} role="group" aria-label={`${g.key} 확정 ${counts.confirmedGrades[g.key] ?? 0}건`}><b><i className="rd-sw" style={{ background: g.color }} />{g.key}</b><span>{g.label}</span><strong>{counts.confirmedGrades[g.key] ?? 0}건</strong><em>{pct(counts.confirmedGrades[g.key] ?? 0, confirmed)}%</em></div>)}</div>
        </> : <p className="caption">확정된 주장이 없습니다. 보류는 E0나 근거 부재가 아닙니다.</p>}
      </div>
    </div>
  </section>;
}
export function ExecutiveSummary({ result }: { result: HostedResult }) {
  const insight = reportInsights(result);
  return <section className="ux1-card" aria-label="보고서 종합 요약"><p className="eyebrow">다음 검토를 위한 요약</p><h3>어디부터 근거를 보강할까요?</h3>
    <div className="ux1-summary-grid is-single"><div><h4>근거 보강 필요 영역</h4><p className="caption">E0–E2 확정 주장 중 근거 없음 요소가 있는 항목 · 한 주장을 한 영역에 집계. 조건부 요소를 포함하며 모두 등급 상승 조건인 것은 아닙니다.</p>
      {insight.needs.length ? <ul className="ux1-needs">{insight.needs.slice(0,5).map(row=><li key={row.track+row.metric}><b>{trackClaims(row.track)}{row.metric && ` · ${row.metric}`}</b><strong>{row.count}건</strong><span>{[...row.elements].join(" · ")}</span></li>)}</ul> : <p>확정 주장에 기록된 보강 영역이 없습니다. 보류 상태는 따로 확인하세요.</p>}
      {insight.needs.length>5 && <details><summary>나머지 {insight.needs.length-5}개 영역</summary><ul>{insight.needs.slice(5).map(row=><li key={row.track+row.metric}>{trackClaims(row.track)}{row.metric && ` · ${row.metric}`}: {row.count}건 · {[...row.elements].join(" · ")}</li>)}</ul></details>}
    </div></div>
  </section>;
}
/** Analysis facts that used to sit in their own tab: scope, period, tables, verification and timing. */
export function AnalysisInfo({ result, pageCount, selectedPages }: { result: HostedResult; pageCount?: number; selectedPages?: number[] }) {
  const data = analyzedData(result), counts = resultBrowserSummary(result), holds = w18HoldBreakdown(result);
  const scope = resultScope(result, pageCount ?? result.w16_source_document?.page_count, selectedPages);
  const period = result.pipeline?.reporting_scope, warning = period?.warning;
  return <section className="ux1-card rd-info" aria-label="분석 정보"><h3>분석 정보</h3><dl className="ux1-data-grid">
    <div><dt>분석 쪽</dt><dd>{scope.pages.length}{scope.total !== null && ` / ${scope.total}`}<small>쪽</small></dd><p>{pageRanges(scope.pages) || "쪽 정보 없음"}</p></div>
    {scope.claims && <div><dt>주장 추출 쪽</dt><dd>{scope.claims.length}<small>쪽</small></dd><p>{pageRanges(scope.claims) || "없음"}</p></div>}
    <div><dt>보고 기간</dt><dd className="rd-info-text">{period?.period_start && period.period_end ? `${period.period_start} ~ ${period.period_end}` : "기간 정보 없음"}</dd>{warning && <p>{warning}</p>}</div>
    <div><dt>{data.tables === null ? "표 주장" : "표"}</dt><dd>{data.tables ?? data.tableClaims}<small>{data.tables === null ? "건" : "개"}</small></dd>{data.tables !== null && <p>표 주장 {data.tableClaims}건</p>}</div>
    <div><dt>원문 검증</dt><dd>{data.verified} / {result.claims.length}<small>건</small></dd></div>
    {counts.stageSeconds !== null && <div><dt>처리 시간</dt><dd>{Math.round(counts.stageSeconds)}<small>초</small></dd><p>대기 시간 제외</p></div>}
  </dl>
    {scope.unselected && scope.unselected.length > 0 && <details><summary>미선택 {scope.unselected.length}쪽</summary><p>{pageRanges(scope.unselected)}</p></details>}
    {holds.length > 3 && <details><summary>보류 사유 전체 {holds.length}개</summary><ul>{holds.map(({ reason, count }) => <li key={reason} title={reason}>{holdReasonLabel(reason)} <strong>{count}건</strong></li>)}</ul></details>}
    <p className="caption">확정 {counts.confirmed}{counts.provisional > 0 && ` + 추가 확인 필요 ${counts.provisional}`} + 보류 {counts.hold} = 추출 주장 {counts.total}건. 원문 검증 수는 등급 확정 수와 별개입니다.</p>
  </section>;
}
