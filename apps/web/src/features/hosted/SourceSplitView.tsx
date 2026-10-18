import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import type { HostedClient, HostedResult, ResultClaim, SourceViewRef } from "./hostedClient";
import { holdReasonLabel, resultBrowserItem, w18HoldReason } from "./resultModel";
import { displayRefs, sourceHighlight, uniqueEvidence, selectedSourceRef, claimLocationLabel, ESTIMATED_LOCATION_LABEL } from "./sourceSplitModel";

const OriginalPdfPage = lazy(() => import("./OriginalPdfPage"));
export type SourceSelection = { claimId: string; page?: number; quote?: string; elementId?: string; nonce: number; scroll?: boolean };
type Props = { result: HostedResult; client: HostedClient; selection?: SourceSelection | null; onSelectClaim?: (id: string) => void };

export function SourceSplitView({ result, client, selection, onSelectClaim }: Props) {
  const [opened, setOpened] = useState(true);
  // The parent result tabs bring this panel into view; here a selection only reopens the split.
  useEffect(() => { if (selection) setOpened(true); }, [selection]);
  return <section className="source-split" aria-label="원문 나란히 보기">
    <div className="split-heading"><div><h3>원문 보기</h3><p>주장과 요소 근거를 원본 PDF에서 함께 확인하세요.</p></div>
      <button type="button" aria-expanded={opened} aria-label={opened ? "나란히 보기 닫기" : undefined} title={opened ? "나란히 보기 닫기" : undefined} onClick={() => setOpened(value => !value)}>{opened ? "×" : "원문 나란히 보기"}</button></div>
    {opened && (result.w16_source_document ? <SplitContent result={result} client={client} selection={selection} onSelectClaim={onSelectClaim} /> : <p role="status">원본을 사용할 수 없습니다. 이 저장 결과에는 PDF 연결 정보가 없습니다. 위치 정보 없음.</p>)}
  </section>;
}

function SplitContent({ result, client, selection, onSelectClaim }: Props) {
  const [selectedId, setSelectedId] = useState(result.claims[0]?.claim_id);
  const claim = result.claims.find(item => item.claim_id === selectedId) ?? result.claims[0];
  const initial = claim?.w16_source_view?.refs.find(ref => ref.kind === "claim")?.physical_page;
  const [page, setPage] = useState(initial ?? claim?.original_page_num ?? 1);
  const [pageCount, setPageCount] = useState(result.w16_source_document!.page_count);
  const [tab, setTab] = useState<"claims" | "pdf">("claims");
  const [search, setSearch] = useState("");
  const [zoom, setZoom] = useState(1);
  const [evidenceId, setEvidenceId] = useState<string | null>(null);
  const [returnPage, setReturnPage] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);
  const documentPane = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (tab === "pdf" && window.matchMedia("(max-width:760px)").matches) {
      documentPane.current?.querySelector<HTMLElement>(".split-pdf-scroll")?.focus({ preventScroll: true });
    }
  }, [tab]);
  const url = useMemo(() => client.originalPdfUrl(result.w16_source_document!), [client, result.w16_source_document]);
  const refs = useMemo(() => displayRefs(claim), [claim]);
  const evidence = uniqueEvidence(refs);
  const activeEvidence = refs.find(ref => ref.ref_id === evidenceId);
  const cells = refs.filter(ref => ref.ref_id.startsWith("table_observation/cell_source_refs/") && sourceHighlight(ref).boxes.length);
  const highlights = refs.filter(ref => (ref.kind === "claim" && (!cells.length || cells.includes(ref))) || ref.ref_id === evidenceId);
  useEffect(() => {
    if (!selection) return;
    const next = result.claims.find(item => item.claim_id === selection.claimId);
    if (!next) return;
    setSelectedId(next.claim_id);
    const target = selectedSourceRef(next.w16_source_view?.refs ?? [], selection);
    setEvidenceId(target?.kind === "evidence" ? target.ref_id : null);
    const number = selection.page ?? target?.physical_page ?? next.original_page_num;
    if (number) setPage(number);
    const claimPage = next.w16_source_view?.refs.find(ref => ref.kind === "claim")?.physical_page ?? next.original_page_num;
    setReturnPage(target?.kind === "evidence" && claimPage !== number ? claimPage : null);
    if (selection.scroll !== false) setTab("pdf");
  }, [selection, result.claims]);
  const select = (next: ResultClaim, showPdf = false) => {
    setSelectedId(next.claim_id); onSelectClaim?.(next.claim_id); setEvidenceId(null); setReturnPage(null);
    const target = next.w16_source_view?.refs.find(ref => ref.kind === "claim" && ref.physical_page)?.physical_page;
    if (target) setPage(target);
    else if (next.original_page_num > 0) setPage(next.original_page_num);
    if (showPdf) setTab("pdf");
  };
  const openEvidence = (ref: SourceViewRef) => { if (ref.physical_page !== page) setReturnPage(page); setEvidenceId(ref.ref_id); if (ref.physical_page) setPage(ref.physical_page); setTab("pdf"); };
  return <div className={`split-content tab-${tab}`}>
    <div className="split-mobile-tabs" role="group" aria-label="주장과 PDF 전환">
      <button type="button" aria-pressed={tab === "claims"} onClick={() => setTab("claims")}>주장 목록</button>
      <button type="button" aria-pressed={tab === "pdf"} onClick={() => setTab("pdf")}>원본 PDF</button>
    </div>
    <div className="split-columns">
      <aside className="split-claims" aria-label="원문 연결 주장 목록">
        <label className="split-search">주장 검색<input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="원문 문장 검색" /></label>
        <div className="split-claim-list">{result.claims.filter(item => item.quote.toLocaleLowerCase().includes(search.toLocaleLowerCase())).map(item => {
          const summary = resultBrowserItem(result, item);
          const status = summary.bucket;
          const onPage = item.w16_source_view?.refs.some(ref => ref.kind === "claim" && ref.physical_page === page);
          const locationLabel = claimLocationLabel(item);
          return <button type="button" key={item.claim_id} className={`split-claim ${item.claim_id === claim?.claim_id ? "selected" : ""} ${onPage ? "on-page" : ""}`} data-current-page={onPage || undefined} aria-pressed={item.claim_id === claim?.claim_id}
            onClick={() => select(item, true)}>
            <span className={`split-status ${status}`}>{summary.badge}</span><small>{summary.page}{onPage ? " · 보고 있는 쪽" : ""}</small>
            <p>{item.quote}</p>{w18HoldReason(result, item) && <small className="w18-hold-reason" title={w18HoldReason(result, item)!}>보류 · {holdReasonLabel(w18HoldReason(result, item)!)}</small>}{locationLabel && <small>{locationLabel}</small>}
          </button>;
        })}</div>
      </aside>
      <div ref={documentPane} className="split-document" aria-label="선택한 주장 원본 PDF">
        <div className="split-toolbar">
          <button type="button" aria-label="원문 이전 쪽" disabled={page <= 1} onClick={() => setPage(value => value - 1)}>←</button>
          <label><input aria-label="원문 물리 쪽" type="number" min={1} max={pageCount} value={page} onChange={event => { const value = Number(event.target.value); if (Number.isInteger(value) && value >= 1 && value <= pageCount) setPage(value); }} /> / {pageCount}</label>
          <button type="button" aria-label="원문 다음 쪽" disabled={page >= pageCount} onClick={() => setPage(value => value + 1)}>→</button>
          <button type="button" aria-label="PDF 축소" disabled={zoom <= 1} onClick={() => setZoom(value => Math.max(1, value - .5))}>−</button>
          <output>{Math.round(zoom * 100)}%</output><button type="button" aria-label="PDF 확대" disabled={zoom >= 3} onClick={() => setZoom(value => Math.min(3, value + .5))}>＋</button>
          <button type="button" onClick={() => setZoom(3)}>표 글자 크게 보기</button>
        </div>
        {returnPage !== null && <button type="button" className="split-back" onClick={() => { setPage(returnPage); setEvidenceId(null); setReturnPage(null); }}>← 이전 p.{returnPage}로 돌아가기</button>}
        <details className="split-evidence-picker" key={claim?.claim_id}><summary>요소 근거 {evidence.length}개</summary>
          <div className="split-citations" aria-label="선택한 주장과 요소 인용">
            <h4>선택한 주장</h4><blockquote>{claim?.w16_source_view?.quote ?? claim?.quote}</blockquote>
            {claimLocationLabel(claim) && <p>{claimLocationLabel(claim) === "추정 위치" ? ESTIMATED_LOCATION_LABEL : "위치 미확인"}</p>}
            <h4>요소별 원문 인용</h4>
            {!evidence.length && <p>연결된 요소 인용이 없습니다.</p>}
            {evidence.map(ref => <div className={`split-citation ${activeEvidence?.ref_id === ref.ref_id ? "active" : ""}`} key={ref.ref_id}>
              <strong>{ref.element_id}</strong><blockquote>{ref.quote}</blockquote>
              <button type="button" disabled={!ref.physical_page} aria-pressed={ref.ref_id === evidenceId} onClick={() => openEvidence(ref)}>{ref.physical_page ? `p.${ref.physical_page}${ref.physical_page !== page ? "로 이동" : " 근거 표시"}` : "쪽 정보 없음"}</button>
              {!sourceHighlight(ref).boxes.length && <span>위치 미확인</span>}
            </div>)}
          </div>
        </details>
        <div className="split-legend"><span className="claim">주장</span><span className="evidence">선택한 요소 근거</span><span className="exact">정확 인용·표 셀</span><span className="region">영역 표시(정확 위치 아님)</span><span className="estimated">{ESTIMATED_LOCATION_LABEL}</span></div>
        {cells.length > 0 && <p className="split-note">표의 저장된 셀 위치 · {cells.map(ref => `${({ value: "값", unit: "단위", year: "연도" } as Record<string, string>)[ref.ref_id.split("/").at(-1)!] ?? "셀"}: ${ref.quote}`).join(" · ")}</p>}
        <Suspense fallback={<p role="status">PDF 뷰어 준비 중…</p>}><OriginalPdfPage key={attempt} sourceSha={result.w16_source_document!.sha256} url={url} pageNumber={page} refs={highlights} zoom={zoom} onPageCount={setPageCount} /></Suspense>
        <button type="button" className="split-retry" onClick={() => setAttempt(value => value + 1)}>PDF 다시 불러오기</button>
      </div>
    </div>
  </div>;
}
