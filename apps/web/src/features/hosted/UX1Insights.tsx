import type { HostedResult, ResultClaim } from "./hostedClient";
import { analyzedData, claimExplanation, gradeMeanings, reportInsights } from "./ux1Model";
import { holdReasonLabel, resultBrowserSummary, resultScope, w18HoldBreakdown } from "./resultModel";

export function GradeGuide() {
  return <section className="ux1-card ux1-grade-guide" aria-label="등급 의미"><p className="eyebrow">결과 읽는 법</p><h3>등급은 주장을 뒷받침하는 근거의 수준입니다</h3>
    <div className="ux1-grade-grid">{gradeMeanings.map(g=><article key={g.grade} data-grade={g.grade}><b>{g.grade}</b><h4>{g.title}</h4><p>{g.text}</p></article>)}</div>
    <p className="caption">E0는 분석 범위에서 입증 근거가 제시·연결되지 않은 상태입니다. 보고서 전체의 근거 부재나 거짓·위법을 단정하지 않습니다. 보류는 등급이 아닙니다.</p>
    <details><summary>트랙별 조건과 판정 기준</summary><ul><li>목표: 연도·수치 → 기준연도·기준값·범위 → 진척·전환계획</li><li>성과: 수치·단위 → 비교기준·범위 → 산정방법·보증 연결</li><li>관리체계: 수단·시행 상태 → 적용범위 → 외부검증</li></ul><p>요소는 3회 독립 판독 중 2회 이상 합의와 검증 원문을 기준으로 연결합니다. 원문 충돌·주장 원문 미확인·유형 미합의·규칙 미정·미처리는 보류합니다. 보고 회사가 기본 주체이며 다른 주체가 명시되면 구분합니다. 지표와 기간이 일치하고 검증된 근거는 다른 쪽에서도 연결할 수 있습니다.</p><p className="caption">저장된 결과의 정책과 판정을 그대로 표시합니다. 과거 분석의 합의 기준은 다를 수 있습니다.</p></details>
  </section>;
}
export function WhyGrade({ result, claim }: { result: HostedResult; claim: ResultClaim }) {
  const explanation = claimExplanation(result, claim);
  return <section className="ux1-why" aria-label="왜 이 등급인지"><h4>왜 이 등급인가요?</h4><p>{explanation.text}</p>{explanation.rules.length>0 && <details><summary>적용 규칙 {explanation.rules.length}개</summary><p>{explanation.rules.join(" · ")}</p></details>}</section>;
}
export function HoldReasonList({ result, onHoldReason }: { result: HostedResult; onHoldReason?: (reason: string)=>void }) {
  const holds = w18HoldBreakdown(result);
  return holds.length ? <ul className="ux1-holds">{holds.slice(0,3).map(h=><li key={h.reason}>{onHoldReason
    ? <button type="button" title={h.reason} onClick={()=>onHoldReason(h.reason)}>{holdReasonLabel(h.reason)}<b>{h.count}건</b></button>
    : <span title={h.reason}>{holdReasonLabel(h.reason)} · <b>{h.count}건</b></span>}</li>)}</ul> : <p>보류 주장이 없습니다.</p>;
}
const pct = (n: number, d: number) => d ? Math.round(n / d * 100) : 0;
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
      {confirmed ? <>확정 {confirmed}건 중 {e0 ? <b>{e0}건({pct(e0, confirmed)}%)이 근거 부재(E0)</b> : "근거 부재(E0)는 없"}{e0 ? "입니다." : "습니다."}</> : <>확정된 주장이 없습니다. 보류 {hold}건의 사유를 먼저 확인하세요.</>}
      {needs.length > 0 && <> 보강이 필요한 {needsTotal}건 중 <b>{needs[0].label} {needs[0].count}건</b>부터 확인하세요.</>}
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
export function ExecutiveSummary({ result, onSelect, onHoldReason, holds = true }: { result: HostedResult; onSelect: (id: string)=>void; onHoldReason?: (reason: string)=>void; holds?: boolean }) {
  const insight = reportInsights(result);
  return <section className="ux1-card" aria-label="보고서 종합 요약"><p className="eyebrow">다음 검토를 위한 요약</p><h3>어디부터 근거를 보강할까요?</h3>
    <div className="ux1-summary-grid"><div><h4>근거 보강 필요 영역</h4><p className="caption">E0–E2 확정 주장 중 근거 없음 요소가 있는 항목 · 한 주장을 한 영역에 집계. 조건부 요소를 포함하며 모두 등급 상승 조건인 것은 아닙니다.</p>
      {insight.needs.length ? <ul className="ux1-needs">{insight.needs.slice(0,5).map(row=><li key={row.track+row.metric}><b>{row.track}{row.metric && ` · ${row.metric}`}</b><strong>{row.count}건</strong><span>{[...row.elements].join(" · ")}</span></li>)}</ul> : <p>확정 주장에 기록된 보강 영역이 없습니다. 보류 상태는 따로 확인하세요.</p>}
      {insight.needs.length>5 && <details><summary>나머지 {insight.needs.length-5}개 영역</summary><ul>{insight.needs.slice(5).map(row=><li key={row.track+row.metric}>{row.track}{row.metric && ` · ${row.metric}`}: {row.count}건 · {[...row.elements].join(" · ")}</li>)}</ul></details>}
    </div><div><h4>주요 확정 주장 {insight.top.length}개</h4><p className="caption">근거 등급 높은 순 · 같은 등급은 원본 쪽 순</p>{insight.top.length ? <ol className="ux1-top">{insight.top.map(item=><li key={item.claim.claim_id}><button type="button" onClick={()=>onSelect(item.claim.claim_id)}><b>{item.grade} · {item.page}</b><span>{item.claim.quote}</span></button></li>)}</ol> : <p>확정 주장이 없습니다.</p>}
      {holds && <><h4>주요 보류 사유</h4><HoldReasonList result={result} onHoldReason={onHoldReason} /></>}
    </div></div>
  </section>;
}
export function AnalyzedData({ result, pageCount, selectedPages }: { result: HostedResult; pageCount?: number; selectedPages?: number[] }) {
  const data = analyzedData(result), scope = resultScope(result, pageCount ?? result.w16_source_document?.page_count, selectedPages);
  return <section className="ux1-card" aria-label="분석한 데이터"><h3>분석한 데이터</h3><dl className="ux1-data-grid">
    <div><dt>{scope.total === null ? "선택 쪽" : "선택 / 전체 쪽"}</dt><dd>{scope.pages.length}{scope.total !== null && ` / ${scope.total}`}<small>쪽</small></dd></div>
    <div><dt>{data.tables === null ? "표에서 추출한 주장" : "표 수"}</dt><dd>{data.tables ?? data.tableClaims}<small>{data.tables === null ? "건" : "개"}</small></dd>{data.tables !== null && <p>표에서 추출한 주장 {data.tableClaims}건</p>}</div>
    <div><dt>주장 원문 검증</dt><dd>{data.verified} / {result.claims.length}<small>건</small></dd></div>
    {data.modelCalls !== null && <div><dt>모델 판정 호출</dt><dd>{data.modelCalls}<small>회</small></dd><p>유형·요소 판정 완료 호출 합계</p></div>}
  </dl><p className="caption">표 수는 선택 범위의 파싱된 표 블록 수이며 표 주장 수와 다릅니다. 원문 검증 수는 등급 확정 수가 아닙니다. 호출 수는 추출·관계 탐색 호출을 제외한 기록값입니다.</p></section>;
}
