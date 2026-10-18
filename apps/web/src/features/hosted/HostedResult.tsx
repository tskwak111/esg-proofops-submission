import { WhyGrade, ExecutiveSummary, ResultDashboard } from "./UX1Insights";
import { metadataText, reportTitle } from "./documentName";
import { useEffect, useId, useMemo, useRef, useState, type ReactNode } from "react";
import { resultTabFromSearch, resultTabUrl, runIdFromHash, validResourceId, type ResultTab } from "./hostedHistory";
import { ResultTabs, resultPanelId, resultTabId, type ResultTabItem } from "./ResultTabs";
import { downloadResultReport } from "./ResultReport";
import { focusClaimDetail, focusClaimRegion } from "../claims/claimFocus";
import { ClaimBrowserControls } from "../claims/ClaimBrowserControls";
import { elementLabels } from "../labels";
import { claimRefs, originalRefPage } from "./sourcePageModel";
import { SourceSplitView, type SourceSelection } from "./SourceSplitView";
import { MissingEvidenceCard } from "../claims/MissingEvidenceCard";
import { ProvisionalHold } from "../claims/ProvisionalHold";
import { HostedApiError, holdReasonText, newIdempotencyKey, completedRunPresentation, resultExecution, type HostedClient, type HostedResult, type ResultClaim, type ResultDecision, type ResultReview } from "./hostedClient";
import { pageRanges, resultScope, holdReasonLabel, claimSourceText, claimProcessingFailed, claimEvidenceLine, w18HoldReason, allowedChoices, buildResolveBody, candidateRefs, claimHoldReasons, decisionLine, elementStateText, gradeSummary, groupClaimOccurrences, holdEvidenceNoteText, matchesResultFilters, openReviewFor, pageLabel, refKey, resultBrowserItem, resultBrowserSummary, resultElementText, resultSourceFailures, reviewStatusText, tableObservationText, trackText, type ElementChoice, type ResultFilters } from "./resultModel";

export function canReview(role: string | null | undefined) { return role === "reviewer" || role === "admin"; }

const allFilters: ResultFilters = { queue: "all", search: "", track: "all", grade: "all", status: "all" };
/** Height of the sticky site header, published by the shell as --header-h. */
function headerOffset() {
  return parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--header-h")) || 0;
}
/** Smoothly bring `target` just below the sticky header and result tab bar. */
function revealBelowTabs(target: Element | null | undefined, tabs: Element | null | undefined) {
  if (!target) return;
  const offset = headerOffset() + (tabs?.getBoundingClientRect().height ?? 0) + 12;
  window.scrollTo({ top: target.getBoundingClientRect().top + window.scrollY - offset, behavior: "smooth" });
}
function currentResult(runId: string): boolean {
  return runIdFromHash(window.location.hash) === runId || (window.location.pathname === "/demo" && new URLSearchParams(window.location.search).get("report") === runId);
}
function initialTab(runId?: string): ResultTab {
  if (typeof window === "undefined" || !runId || !currentResult(runId)) return "overview";
  return resultTabFromSearch(window.location.search);
}

type ResultViewProps = { result: HostedResult; reportName?: string; companyName?: string; pageCount?: number; selectedPages?: number[]; runStatus?: string; role?: string | null; readOnly?: boolean; client?: HostedClient; runId?: string; onReload?: () => Promise<void>; runSummary?: ReactNode };
export function ResultView({ result, reportName, companyName, pageCount, selectedPages, runStatus, role, readOnly = false, client, runId, onReload }: ResultViewProps) {
  if (readOnly) role = null;
  companyName = metadataText(companyName);
  reportName = reportTitle(reportName);
  const [reportMode, setReportMode] = useState<"summary" | "full">("summary");
  const panelId = useId();
  const detailRef = useRef<HTMLElement | null>(null);
  const listRef = useRef<HTMLDivElement | null>(null);
  const [sourceSelection, setSourceSelection] = useState<SourceSelection | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detailOpen, setDetailOpen] = useState(false);
  const [limit, setLimit] = useState(25);
  const [filters, setFilters] = useState<ResultFilters>(allFilters);
  const [holdReason, setHoldReason] = useState<string | null>(null);
  // Tab state is per ResultView (one per run); the URL mirrors whichever run the user last switched.
  const tabBase = useId();
  const tabsAnchor = useRef<HTMLDivElement | null>(null);
  const root = useRef<HTMLElement | null>(null);
  // Runs after selectTab's own scroll and after the target panel has rendered.
  const revealLater = (find: (scope: HTMLElement) => Element | null) => requestAnimationFrame(() => requestAnimationFrame(() => {
    if (root.current) revealBelowTabs(find(root.current), root.current.querySelector(".result-tabs"));
  }));
  const [tab, setTab] = useState<ResultTab>(() => initialTab(runId));
  const [visited, setVisited] = useState<ResultTab[]>([]);
  useEffect(() => setVisited(current => current.includes(tab) ? current : [...current, tab]), [tab]);
  useEffect(() => {
    if (!runId) return;
    const sync = () => { if (currentResult(runId)) setTab(resultTabFromSearch(window.location.search)); };
    window.addEventListener("popstate", sync);
    return () => window.removeEventListener("popstate", sync);
  }, [runId]);
  const selectTab = (next: ResultTab) => {
    if (runId && validResourceId(runId)) {
      const url = resultTabUrl(window.location, runId, next);
      if (url !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.pushState(null, "", url);
    }
    setTab(next);
    // Readers already below the result heading land at the top of the new panel, not mid-page.
    requestAnimationFrame(() => {
      const anchor = tabsAnchor.current;
      if (!anchor) return;
      const top = anchor.getBoundingClientRect().top + window.scrollY - headerOffset();
      if (window.scrollY > top) window.scrollTo({ top, behavior: "instant" });
    });
  };
  const openSource = (selection: SourceSelection) => { setSourceSelection(selection); selectTab("source"); };
  const groups = useMemo(() => groupClaimOccurrences(result.claims), [result.claims]);
  const counts = useMemo(() => resultBrowserSummary(result), [result]);
  const execution = resultExecution(result, runStatus);
  const completion = completedRunPresentation(result, runStatus);
  const scope = resultScope(result, pageCount ?? result.w16_source_document?.page_count, selectedPages);
  const failures = resultSourceFailures(result);
  const extraction = result.pipeline?.extraction_outcome;
  const holdCodes = [...new Set([...result.hold_reasons, ...(result.hold_reason_details ?? []).map(detail => detail.code),
    ...(result.pipeline?.reason ? [result.pipeline.reason] : []), ...(extraction?.code && extraction.code !== "OK" ? [extraction.code] : [])])];
  const filtered = useMemo(() => groups.flatMap(group => {
    const items = group.claims.map(claim => resultBrowserItem(result, claim)).filter(item => matchesResultFilters(item, filters) && (!holdReason || w18HoldReason(result, item.claim) === holdReason));
    return items.length ? [{ group, item: items[0], items }] : [];
  }), [groups, result, filters, holdReason]);
  const trackCount = (track: string) => result.claims.filter(claim => {
    const item = resultBrowserItem(result, claim);
    return matchesResultFilters(item, { ...filters, track }) && (!holdReason || w18HoldReason(result, item.claim) === holdReason);
  }).length;
  const narrowed = filters.track !== "all" || filters.grade !== "all" || filters.status !== "all" || filters.search !== "" || !!holdReason;
  const resetNarrowing = () => { setFilters(current => ({ ...allFilters, queue: current.queue })); setHoldReason(null); setLimit(25); setDetailOpen(false); setSelectedId(null); };
  const selectedGroup = filtered.find(row => row.group.claims.some(claim => claim.claim_id === selectedId)) ?? filtered[0];
  const selected = selectedGroup?.group.claims.find(claim => claim.claim_id === selectedId) ?? selectedGroup?.item.claim ?? null;
  const changeFilter = (key: keyof ResultFilters, value: string) => {
    setFilters(current => ({ ...current, [key]: value, ...(key === "queue" ? { grade: "all", status: "all" } : {}) }));
    if (key === "queue" && value !== "hold") setHoldReason(null);
    setLimit(25); setDetailOpen(false); setSelectedId(null);
  };
  const selectClaim = (id: string) => { setSourceSelection({ claimId: id, nonce: Date.now(), scroll: false }); setSelectedId(id); setDetailOpen(true); requestAnimationFrame(() => focusClaimDetail(detailRef.current)); };
  const openHoldReason = (reason: string) => {
    setFilters({ ...allFilters, queue: "hold" }); setHoldReason(reason); setLimit(25); setDetailOpen(false); setSelectedId(null); selectTab("claims");
    revealLater(scope => scope.querySelector(".result-filter-chip") ?? scope.querySelector(".claim-list"));
  };
  const back = () => { setDetailOpen(false); requestAnimationFrame(() => focusClaimRegion(listRef.current)); };
  const tabs = [
    ...(counts.confirmed ? [{ value: "confirmed", label: "확정", count: counts.confirmed }] : []),
    ...(counts.provisional ? [{ value: "provisional", label: "추가 확인 필요", count: counts.provisional }] : []), { value: "hold", label: "보류", count: counts.hold }, { value: "all", label: "전체", count: counts.total },
  ];
  const resultTabs: ResultTabItem[] = [
    { key: "overview", label: "요약", hint: "핵심 수치 · 보강할 곳" },
    { key: "claims", label: "문장별 결과", hint: "주장 판정과 근거", badge: counts.total },
    { key: "source", label: "원문 대조", hint: "PDF에서 근거 위치 확인" },
  ];
  // In the browser, panels mount on first visit and then stay mounted (hidden) so filters and the PDF viewer survive.
  // Without a DOM (static render) every panel is emitted, still marked hidden, so the markup stays complete.
  const eager = typeof document === "undefined";
  const panel = (key: ResultTab, children: ReactNode) => <div className={`result-tabpanel result-tabpanel-${key}`} role="tabpanel" id={resultPanelId(tabBase, key)} aria-labelledby={resultTabId(tabBase, key)} hidden={tab !== key} key={key}>
    {(eager || tab === key || visited.includes(key)) && children}</div>;
  return <section ref={root} className={`hosted-result ${detailOpen ? "is-detail-open" : ""}`} aria-label="분석 결과" data-testid="hosted-result">
    <header className="demo-heading result-heading"><div><p className="eyebrow">분석 결과{typeof result.pipeline?.reporting_scope?.report_year === "number" ? ` · ${result.pipeline.reporting_scope.report_year}` : ""}</p>
      <h2 tabIndex={-1} data-testid="result-heading">{reportName ?? (companyName ? `${companyName} · 분석 결과` : "보고서 분석 결과")}</h2>
      {completion && <p className="hosted-note" data-testid="result-completion-status">{completion.title}</p>}
      <p>보고서의 주장을 원문 근거와 대조한 결과입니다. 문장을 선택하면 판정 근거를 확인할 수 있습니다.</p></div><div className="ux1-export"><label>보고서 구성 <select value={reportMode} onChange={event => setReportMode(event.target.value as "summary" | "full")}><option value="summary">요약본</option><option value="full">전체 상세</option></select></label><button type="button" className="hosted-primary result-export" onClick={() => downloadResultReport(result, { reportMode, reportName, companyName, pageCount, selectedPages, runStatus, runId })}>결과 보고서 내보내기</button></div></header>
    <div className="result-tabs-anchor" ref={tabsAnchor} aria-hidden="true" />
    <ResultTabs base={tabBase} tabs={resultTabs} selected={tab} onSelect={selectTab} />
    {panel("overview", <>
      {result.reuse?.reused && <p className="hosted-note" data-testid="result-reuse">이전 분석 결과 · 분석 일시 <time dateTime={result.reuse.analyzed_at}>{new Date(result.reuse.analyzed_at).toLocaleString("ko-KR")}</time></p>}
      {(() => {
        const partial = execution.kind === "partial";
        // The partial notice sits on one line with its details; other states keep the separate notice card.
        const notice = (holdCodes.length > 0 || failures.length > 0 || partial || execution.kind === "failed") && <details className="result-run-notice"><summary>{partial ? "미처리 범위 보기" : "처리·추출·보류 안내"}{extraction?.failed_sources ? ` · 추출 실패 원문 ${extraction.failed_sources.length}개` : ""}{extraction?.page_failures ? ` · 쪽 처리 실패 ${extraction.page_failures.length}건` : ""}</summary>
        <ul>{holdCodes.map(code => <li key={code}>{holdReasonText(code, result.hold_reason_details)}</li>)}</ul>
        {failures.length ? <ul className="result-source-failures">{failures.map((failure, index) => <li key={index}><strong>{failure.page}</strong> · {failure.pageFailure ? "쪽 처리 실패 · " : ""}{failure.sourceIds.length > 0 ? `추출 실패 원문 ${failure.sourceIds.length}개 · ` : ""}{failure.reason}
          {failure.sourceIds.length > 0 && <details><summary>해당 원문 식별자 보기</summary><ul>{failure.sourceIds.map((id, i) => <li key={i}><code>{id}</code></li>)}</ul></details>}</li>)}</ul> : (execution.kind === "partial" || execution.kind === "failed") && <p>누락·보류 쪽의 상세 위치가 기록되지 않았습니다. 원본 쪽이나 처리 기록을 확인해 주세요.</p>}
        {failures.length > 0 && <p>p.는 원본 PDF 쪽입니다. ‘선택본 p.’는 원본 쪽 연결 정보가 없는 경우입니다. 실패한 원문에서 빠진 주장 수는 알 수 없으며, 같은 쪽의 다른 주장은 표시될 수 있습니다.</p>}
        <p>보류·확인되지 않음은 ‘근거 없음’이 아닙니다. 시스템 처리 실패는 기업의 결손이 아닙니다.</p></details>;
        return partial ? <div className="result-execution partial" role="status"><h3>{execution.title}</h3><p>{execution.meaning}</p>{notice}</div>
          : <>{execution.kind !== "completed" && <div className={`result-execution ${execution.kind}`} role="status"><h3>{execution.title}</h3><p>{execution.meaning}</p></div>}{notice}</>;
      })()}
      <ResultDashboard result={result} pageCount={pageCount} selectedPages={selectedPages} onHoldReason={openHoldReason} />
      <details className="result-scope-details"><summary>분석 범위와 집계 기준</summary>
      <details className="result-count-note"><summary>집계와 판정 기준</summary>
        <p>확정 {counts.confirmed}{counts.provisional > 0 && <> + 추가 확인 필요 {counts.provisional}</>} + 보류 {counts.hold} = 추출 주장 {counts.total}건. 각 주장은 한 상태로만 집계합니다. 원문 확인 수는 이 합계에 더하지 않습니다.</p>
        <p>원문 확인 {counts.verified} / {counts.total}건은 문구·위치 대조 결과이며 등급 확정과는 별개입니다.{counts.provisional > 0 && " 추가 확인 필요 등급은 이전 버전에서 확인된 최소 수준으로, 추가 근거에 따라 달라질 수 있습니다."}</p>
        {counts.confirmed === 0 && <p>확정 조건을 충족한 주장은 0건입니다. E0 등급이나 보고서 전체의 근거 부재를 뜻하지 않습니다.</p>}
      </details>
      {counts.stageSeconds !== null && <p className="caption">처리 시간 <strong>{Math.round(counts.stageSeconds)}초</strong> · 단계 합계, 대기시간 제외</p>}
      {result.pipeline?.reporting_scope?.period_start && result.pipeline.reporting_scope.period_end && <p className="hosted-note">보고 범위: {result.pipeline.reporting_scope.period_start} ~ {result.pipeline.reporting_scope.period_end}</p>}
      {result.pipeline?.reporting_scope?.warning && <p className="hosted-note" role="status">{result.pipeline.reporting_scope.warning}</p>}
      <section className="result-scope" aria-label="분석 범위"><h3>분석 범위 · 원본 PDF 쪽</h3>
        <p><strong>선택 {scope.pages.length}쪽{scope.total !== null ? ` / 전체 ${scope.total}쪽` : ""}</strong> · {scope.total !== null && scope.pages.length === scope.total ? "전체 쪽 선택" : "선택 범위만 분석"}</p>
        <p>분석에 포함한 쪽: {pageRanges(scope.pages) || "쪽 정보 미제공"}</p>
        {scope.claims && <p>주장 추출 대상: {pageRanges(scope.claims) || "없음"} · 그 외 선택 쪽은 근거 탐색 범위입니다.</p>}
        {scope.unselected ? <details><summary>미선택 {scope.unselected.length}쪽 · 주장 추출 대상에서 제외</summary><p>{pageRanges(scope.unselected) || "없음"}</p></details> : <p>전체 쪽 수가 없어 미선택 범위를 계산할 수 없습니다. 다른 쪽의 검증·연결된 근거는 주장 상세에 표시합니다.</p>}
      </section>
      </details>
      <ExecutiveSummary result={result} />
    </>)}
    {panel("claims", !result.claims.length ? <p className="empty">{execution.kind === "completed"
      ? "선택한 범위에서 표시할 추출 주장이 없습니다. 추출 안내를 확인해 주세요. 보고서 전체에 주장이 없다는 뜻은 아닙니다."
      : "분석이 보류되어 표시할 주장이 없습니다. 처리 단계와 보류 사유를 확인해 주세요. 근거 없음으로 판정한 결과가 아닙니다."}</p> :
      <section className="claims-section result-claims"><div className="card-heading"><div><p className="eyebrow">문장별 근거 점검</p><h2>문장별 결과</h2><p>상태를 선택하고 문장을 열어 원문과 요소별 근거를 확인하세요.</p></div><span>{filtered.reduce((sum, row) => sum + row.items.length, 0)} / {counts.total}건</span></div>
        <div className="result-browser-controls"><ClaimBrowserControls tabs={tabs} queue={filters.queue} onQueueChange={value => changeFilter("queue", value)} panelId={panelId} search={filters.search} onSearchChange={value => changeFilter("search", value)}
          track={{ value: filters.track, onChange: value => changeFilter("track", value), options: [{ value: "all", label: `전체 트랙 (${trackCount("all")})` }, { value: "goal", label: `목표형 (${trackCount("goal")})` }, { value: "performance", label: `성과형 (${trackCount("performance")})` }, { value: "management", label: `관리체계형 (${trackCount("management")})` }, { value: "unknown", label: `분류 미합의 (${trackCount("unknown")})` }] }}
          grade={{ value: filters.grade, onChange: value => changeFilter("grade", value), options: [{ value: "all", label: "전체 등급" }, ...["E3", "E2", "E1", "E0"].map(value => ({ value, label: value })), { value: "none", label: "등급 보류" }] }}
          status={{ value: filters.status, onChange: value => changeFilter("status", value), options: [{ value: "all", label: "전체 상태" }, ...(counts.confirmed ? [{ value: "confirmed", label: "확정" }] : []), ...(counts.provisional ? [{ value: "provisional", label: "추가 확인 필요" }] : []), { value: "hold", label: "판정 보류" }, { value: "source_unverified", label: "원문 대조 필요" }] }} /></div>
        {holdReason && <p className="result-filter-chip" role="status">보류 사유: <strong title={holdReason}>{holdReasonLabel(holdReason)}</strong> <button type="button" className="hosted-text-button" aria-label="사유 필터 해제" onClick={() => { setHoldReason(null); setLimit(25); }}>사유 필터 해제</button></p>}
        <div className="claims-layout" role="tabpanel" id={panelId} aria-labelledby={`${panelId}-tab-${filters.queue}`}>
          <div className="claim-list" aria-label="분석 결과 주장 목록" ref={listRef} tabIndex={-1}>{filtered.slice(0, limit).map(({ group, item }) => {
            const claim = item.claim;
            const contexts = group.claims.map(occurrence => ({ occurrence, summary: tableObservationText(occurrence.table_observation) }));
            return <button type="button" className={`claim-item ${selectedGroup?.group.key === group.key ? "selected" : ""}`} key={group.key} onClick={() => selectClaim(claim.claim_id)} aria-pressed={detailOpen && selectedGroup?.group.key === group.key}>
              <div className="claim-item-top"><span>{item.page} · {claim.track ? trackText[claim.track] ?? claim.track : claim.provisional_track ? (item.confirmed ? `${trackText[claim.provisional_track] ?? claim.provisional_track}` : `${trackText[claim.provisional_track] ?? claim.provisional_track} · 분류 확인 중`) : claimProcessingFailed(claim) ? "유형 처리 미완료" : "분류 미합의"}</span><span className={`mini-status ${item.confirmed ? "good" : "warn"}`}>{item.badge}</span></div>
              <p className="claim-quote">{claim.quote}</p>
              {w18HoldReason(result, claim) && <span className="w18-hold-reason" title={w18HoldReason(result, claim)!}>보류 · {holdReasonLabel(w18HoldReason(result, claim)!)}</span>}
              {group.occurrenceCount > 1 && <span className="duplicate-count">같은 문장 {group.occurrenceCount}곳</span>}
              {contexts.some(context => context.summary) && <div className="claim-table-context" aria-label="표 주장 문맥">{contexts.filter(context => context.summary).map(({ occurrence, summary }) => <span key={occurrence.claim_id}>{pageLabel(result, occurrence.page_num)} · {summary}</span>)}</div>}
              <small className="claim-evidence-line">{item.bucket === "hold" ? claimSourceText(claim) : claimEvidenceLine(result, claim)}</small>
            </button>;
          })}{!filtered.length && <div className="empty"><p>{narrowed ? "선택한 트랙·등급·상태·검색 조건에 맞는 주장이 없습니다." : "조건에 맞는 주장이 없습니다."}</p>{narrowed && <button type="button" className="hosted-secondary" onClick={resetNarrowing}>필터 초기화</button>}</div>}
            {filtered.length > limit && <button className="more-button" type="button" onClick={() => setLimit(current => current + 25)}>더 보기 ({filtered.length - limit}건 남음)</button>}
          </div>
          {selected && selectedGroup ? <ClaimPanel key={selected.claim_id} claim={selected} occurrences={selectedGroup.group.claims} occurrenceCount={selectedGroup.group.occurrenceCount} onSelectOccurrence={selectClaim} onOpenSource={openSource} detailRef={detailRef} onBack={back} result={result} role={role} client={client} runId={runId} onReload={onReload} /> : <aside className="claim-detail"><div className="detail-empty"><h3>주장을 선택하세요</h3><p>조건에 맞는 문장을 선택하면 근거와 판정 경로를 볼 수 있습니다.</p></div></aside>}
        </div>
      </section>)}
    {panel("source", client && runId && result.claims.length > 0 ? <SourceSplitView key={runId} result={result} client={client} selection={sourceSelection} onSelectClaim={setSelectedId} />
      : <p className="empty">{result.claims.length ? "원문 보기를 사용할 수 없습니다. 저장된 결과에 원본 PDF 연결 정보가 없습니다." : "원문과 대조할 추출 주장이 없습니다."}</p>)}
  </section>;
}

function ClaimPanel({ claim, occurrences, occurrenceCount, onSelectOccurrence, onOpenSource, result, role, client, runId, onReload, detailRef, onBack }: { detailRef: React.RefObject<HTMLElement | null>; onBack: () => void; claim: ResultClaim; occurrences: ResultClaim[]; occurrenceCount: number; onSelectOccurrence: (claimId: string) => void; onOpenSource: (selection: SourceSelection) => void; result: HostedResult; role?: string | null; client?: HostedClient; runId?: string; onReload?: () => Promise<void> }) {
  const openSource = (ref: { page_num: number; quote?: string; elementId?: string }) => {
    const page = originalRefPage(result, claim, ref);
    if (typeof page === "number" && Number.isInteger(page) && page > 0) onOpenSource({ claimId: claim.claim_id, page, quote: ref.quote, elementId: ref.elementId, nonce: Date.now() });
  };
  const claimPage = claim.w16_source_view?.refs.find(ref => ref.kind === "claim")?.physical_page ?? result.page_map?.[claim.page_num - 1] ?? claim.original_page_num;
  const grade = gradeSummary(claim);
  const item = resultBrowserItem(result, claim);
  const holdReasons = claimHoldReasons(claim);
  const review = openReviewFor(result, claim.claim_id);
  const tableContext = tableObservationText(claim.table_observation);
  const range = claim.provisional_grade?.grade_range ?? claim.possible_grade_range ?? claim.decision?.grade_range;
  const unconfirmed = item.elements.filter(element => ["unknown", "conflict", "unreadable", "absent"].includes(element.state));
  const processingFailed = claimProcessingFailed(claim);
  const needed = processingFailed ? [] : claim.provisional_grade?.needed_evidence ?? [];
  return <aside className="claim-detail" ref={detailRef} tabIndex={-1} aria-label="주장 상세">
    <button type="button" className="result-back" onClick={onBack}>← 목록으로 돌아가기</button>
    <div className="source-quote"><span>{claim.source_quality === "verified" ? "보고서 원문" : "추출 문장"} · {item.page}</span><blockquote>“{claim.quote}”</blockquote><small>{claimSourceText(claim)}</small>{client && runId && <button type="button" className="claim-source-button" disabled={!Number.isInteger(claimPage) || claimPage < 1} onClick={() => onOpenSource({ claimId: claim.claim_id, page: claimPage, nonce: Date.now() })}>주장 원문 보기</button>}</div>
    <WhyGrade result={result} claim={claim} />
    {tableContext && <div className="table-observation-detail"><h4>표 문맥</h4><p>{tableContext}</p></div>}
    {occurrenceCount > 1 && <details className="detail-block duplicate-detail"><summary>같은 문장 {occurrenceCount}곳</summary><ul>{occurrences.map(occurrence => <li key={occurrence.claim_id}><button type="button" aria-pressed={occurrence.claim_id === claim.claim_id} onClick={() => onSelectOccurrence(occurrence.claim_id)}><strong>{pageLabel(result, occurrence.page_num)}{occurrence.claim_id === claim.claim_id ? " · 현재 선택" : ""}</strong><span>{tableObservationText(occurrence.table_observation) ?? "추가 표 문맥 없음"}</span></button></li>)}</ul>{occurrences.length < occurrenceCount && <p>나머지 {occurrenceCount - occurrences.length}곳의 상세는 이 결과에 포함되지 않았습니다.</p>}</details>}
    <div className={`decision-panel ${grade.confirmed ? "is-confirmed" : "is-provisional"}`} data-testid="grade-panel">
      <span>{grade.confirmed && item.track !== "unknown" ? `${trackText[item.track] ?? item.track}` : claim.track ? `${trackText[claim.track] ?? claim.track}` : claim.provisional_track ? (grade.confirmed ? `${trackText[claim.provisional_track] ?? claim.provisional_track}` : `${trackText[claim.provisional_track] ?? claim.provisional_track} · 분류 확인 중`) : processingFailed ? "유형 처리 미완료" : "트랙 합의 전"}</span>{" "}
      <strong>{grade.headline}</strong><p>{grade.confirmed ? grade.note : item.bucket === "provisional" ? "이전 버전 결과 · 추가 확인 필요. 당시 연결된 근거를 기준으로 표시한 등급이며 확정 판정이 아닙니다." : "판정에 필요한 확인 사항을 아래에서 안내합니다."}</p>
      {!grade.confirmed && range && <details className="result-range"><summary>가능한 등급 범위와 미해결 요소</summary><span>가능한 등급 범위</span><b>{range.floor}–{range.ceiling}</b><p>범위는 등급이 아니며 하한·상한만 알려 줍니다.{range.open_elements?.length ? ` 미해결 요소: ${range.open_elements.map(id => elementLabels[id] ?? id).join(" · ")}` : ""}</p></details>}
    </div>
    {w18HoldReason(result, claim) && <p className="w18-hold-reason">보류 사유 · {w18HoldReason(result, claim)}</p>}
    {grade.note && !grade.confirmed && <details className="why-panel"><summary>상세 판정 기록</summary><p>{grade.note}</p></details>}
    {claim.source_status?.display && claim.source_status.status !== "verified" && <p className="hosted-note">{claim.source_status.display}{claim.source_status.reason ? ` · ${holdReasonText(claim.source_status.reason)}` : ""}</p>}
    <div className="detail-block" aria-label="요소별 근거"><h4>요소별 근거{!grade.confirmed && claim.provisional_grade?.elements?.length ? " · 추가 확인 필요" : ""}</h4>
      {item.elements.length ? <div className="element-table-wrap"><table className="element-table result-elements"><thead><tr><th>요소</th><th>상태</th><th>근거 문구</th></tr></thead><tbody>{item.elements.map(element => <tr key={element.element_id}>
        <th scope="row"><b>{element.element_id}</b>{" "}<span>{elementLabels[element.element_id] ?? "요소"}</span></th>
        <td><span className={`element-state ${element.state}`}>{resultElementText(claim, element)}</span>{element.votes != null && element.total_replicas != null && <small className="result-votes">반복 판독 {element.votes}/{element.total_replicas}</small>}</td>
        <td>{element.evidence_refs.length ? element.evidence_refs.map((ref, index) => <div className="result-citation" key={index}><small>PDF {claim.source_ref_page_space === "original" ? `p.${ref.page_num}` : pageLabel(result, ref.page_num)}{ref.printed_page_label ? ` · 인쇄 쪽 ${ref.printed_page_label}` : ""} · {ref.verification_state === "verified" ? "요소 근거 원문 · 검증됨" : "요소 근거 원문 · 확인 필요"}</small><blockquote>“{ref.quote}”</blockquote>{client && runId && <button type="button" disabled={!Number.isInteger(originalRefPage(result, claim, ref))} onClick={() => openSource({ ...ref, elementId: element.element_id })}>이 인용의 원문 보기</button>}</div>) : <span className="no-evidence">{element.state === "unknown" ? "인정된 근거 미연결(확인 필요)" : element.state === "absent_in_scope" ? "근거 없음(분석 범위 내)" : element.state === "absent" ? "근거 없음(검증된 검색 범위)" : element.state === "conflict" ? "근거 충돌 · 확인 필요" : element.state === "not_applicable" ? "비적용 확인" : "원문 인용 확인 필요"}</span>}</td>
      </tr>)}</tbody></table></div> : <p className="caption">요소 태깅 전입니다. 빈 요소를 ‘근거 없음’으로 보지 않습니다.</p>}
    </div>
    {!grade.confirmed && (holdReasons.length > 0 || unconfirmed.length > 0) && <div className="detail-block result-hold"><h4>보류 사유 · 확인 사항</h4>
      {holdReasons.length > 0 && <ul>{holdReasons.map((reason, index) => <li key={index}>{reason}</li>)}</ul>}
      {needed.length ? <ul>{needed.map((check, index) => <li key={index}>{trackText[check.track] ?? check.track} {check.target_grade} 검토: {check.elements.map(id => `${id} ${elementLabels[id] ?? ""}`).join(" · ")}</li>)}</ul> : unconfirmed.length > 0 && <p>다음 확인: {unconfirmed.map(element => `${element.element_id} ${elementLabels[element.element_id] ?? ""}`).join(" · ")}의 원문 인용·귀속과 처리 상태를 확인해 주세요.</p>}
      {!grade.confirmed && claim.provisional_grade?.display_type === "hold" && <details className="result-hold-details"><summary>확인 기록과 원문 후보 자세히 보기</summary><ProvisionalHold grade={{ ...claim.provisional_grade, hold_reasons: holdReasons, needed_evidence: needed, evidence_note: holdEvidenceNoteText(claim.provisional_grade.evidence_note) }}
        facts={[...((claim.source_status?.status ?? claim.source_quality) === "verified" ? ["주장 원문의 위치 확인 완료 (요소 귀속·등급 확정과 별도)"] : []), ...item.elements.filter(element => element.state === "present" && element.evidence_refs.some(ref => ref.verification_state === "verified")).map(element => `${elementLabels[element.element_id] ?? element.element_id}: 근거 후보 확인 · 검증 인용 연결`)]}
        sources={[...claim.source_refs.map(ref => ({ ref, label: "주장 원문" })), ...item.elements.flatMap(element => element.evidence_refs.map(ref => ({ ref, label: `${element.element_id} ${element.state === "present" ? "요소 인용 (확인 후보)" : "미확정 후보"}` })))]} pageLabel={page => pageLabel(result, page)} onSourceOpen={client && runId ? openSource : undefined} /></details>}
    </div>}
    {claim.elements.filter(element => (!grade.confirmed || item.elements.some(shown => shown.element_id === element.element_id && shown.state === element.state)) && !claim.decision?.excluded_elements?.includes(element.element_id) && ["unknown", "conflict", "absent"].includes(element.state)).map(element => <details key={`missing-${element.element_id}`} className="detail-block"><summary>{element.element_id} · 원인과 다음 확인</summary><MissingEvidenceCard element={element} missing={(() => { const missing = claim.missing_evidence.find(item => item.element_id === element.element_id); return missing ? { ...missing, reason: missing.reason ? holdReasonText(missing.reason) : undefined, undetermined_reason: missing.undetermined_reason ? holdReasonText(missing.undetermined_reason) : undefined } : undefined; })()} sources={candidateRefs(claim, element).map(ref => ({ ref, label: `${element.element_id} 근거 후보 · 귀속 미확정`, openable: claimRefs(claim).some(source => source.page_num === ref.page_num && source.document_version_id === ref.document_version_id) }))} pageLabel={page => pageLabel(result, page)} onSourceOpen={client && runId ? openSource : undefined} /></details>)}
    {review && canReview(role) && result.reuse?.review_supported !== false && client && runId && <ReviewForm key={`${review.review_id}:${review.revision}`} claim={claim} review={review} client={client} runId={runId} onReload={onReload} result={result} />}
    {review && result.reuse?.review_supported === false && <p className="hosted-note">이전 분석 결과는 조회용입니다. 태깅을 검토하려면 관리자에게 새 분석을 요청하세요.</p>}
    {review && !canReview(role) && result.reuse?.review_supported !== false && !grade.confirmed && <p className="hosted-note">이 주장은 사람 검토를 기다립니다. 이 검토는 검토자 또는 관리자만 처리할 수 있습니다.</p>}
  </aside>;
}

export function ReviewForm({ claim, review, client, runId, onReload, result }: { claim: ResultClaim; review: ResultReview; client: HostedClient; runId: string; onReload?: () => Promise<void>; result: Pick<HostedResult, "page_map"> }) {
  const [choices, setChoices] = useState<Record<string, ElementChoice>>({});
  const [reason, setReason] = useState("");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<ResultDecision | null>(null);
  const key = useRef(newIdempotencyKey());
  const edit = (next: Record<string, ElementChoice>) => { setChoices(next); key.current = newIdempotencyKey(); setError(""); };

  async function submit(event: React.FormEvent) {
    event.preventDefault(); setError(""); setConflict(false);
    let body;
    try { body = buildResolveBody(claim, review, choices, reason); } catch (err) { setError((err as Error).message); return; }
    setBusy(true);
    try {
      const response = await client.resolveReview(runId, review.review_id, review.revision, body, key.current);
      setDone(response.decision);
      await onReload?.();
    } catch (err) {
      if (err instanceof HostedApiError) { setError(err.userMessage); setConflict(err.isConflict); } else setError((err as Error).message);
    } finally { setBusy(false); }
  }

  if (done) return <div className="hosted-outcome hosted-completed" role="status" data-testid="review-done">
    <h4>검토 반영됨</h4>
    <p>{decisionLine(done) ?? "확정 등급 아님"}{done.evidence_grade ? ` · ${reviewStatusText(done.review_status)}` : ""}</p>
    <p className="hosted-note">등급은 서버 규칙엔진이 다시 계산한 값이며 사람이 등급을 직접 입력하지 않습니다.</p></div>;

  return <form className="hosted-review" onSubmit={submit} aria-label="사람 검토">
    <h4>사람 검토 <small>(요소 상태만 수정 · 등급은 서버가 계산)</small></h4>
    <p className="hosted-note">검토 사유: {review.reason_codes.map(code => holdReasonText(code)).join(" · ") || "없음"}</p>
    {claim.elements.map(element => {
      const choiceList = allowedChoices(claim, element);
      const choice = choices[element.element_id] ?? { state: element.state };
      const refs = candidateRefs(claim, element);
      return <fieldset key={element.element_id}><legend>{element.element_id} {elementLabels[element.element_id] ?? ""} · 현재 {elementStateText(element.state, claim.source_quality)}</legend>
        <select aria-label={`${element.element_id} 상태`} value={choice.state} onChange={e => edit({ ...choices, [element.element_id]: { state: e.target.value as ElementChoice["state"], refKey: choice.refKey ?? (refs[0] && refKey(refs[0])) } })}>
          {!choiceList.some(c => c.state === element.state) && <option value={element.state}>{elementStateText(element.state, claim.source_quality)} (현재 유지)</option>}
          {choiceList.map(c => <option key={c.state} value={c.state} disabled={c.disabled}>{`${c.label}${c.hint ? ` — ${c.hint}` : ""}`}</option>)}
        </select>
        {choice.state === "present" && choice.state !== element.state && <select aria-label={`${element.element_id} 근거 선택`} value={choice.refKey ?? ""} onChange={e => edit({ ...choices, [element.element_id]: { state: "present", refKey: e.target.value } })}>
          {refs.map(ref => <option key={refKey(ref)} value={refKey(ref)}>{pageLabel(result, ref.page_num)} “{ref.quote.slice(0, 50)}”</option>)}</select>}
      </fieldset>;
    })}
    <label>검토 사유 (5–1000자)<textarea value={reason} onChange={e => { setReason(e.target.value); key.current = newIdempotencyKey(); }} maxLength={1000} required minLength={5} /></label>
    <button disabled={busy}>{busy ? "제출 중…" : "검토 제출"}</button>
    {error && <p className="hosted-error" role="alert">{error}</p>}
    {conflict && <p role="alert">다른 변경과 충돌했습니다. <button type="button" onClick={() => void onReload?.()}>결과 새로고침</button></p>}
  </form>;
}
