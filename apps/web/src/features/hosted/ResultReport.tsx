import { analyzedData, claimExplanation, gradeMeanings, reportInsights } from "./ux1Model";
import { metadataText, reportTitle } from "./documentName";
import type { HostedResult } from "./hostedClient";
import { holdReasonText, resultExecution } from "./hostedClient";
import { claimSourceText, w18HoldBreakdown, trackText, claimEvidenceLine, claimHoldReasons, gradeSummary, pageLabel, pageRanges, resultBrowserItem, resultBrowserSummary, resultElementText, resultScope, resultSourceFailures, tableObservationText } from "./resultModel";
import { elementLabels } from "../labels";

export type ReportContext = { reportName?: string; companyName?: string; pageCount?: number; selectedPages?: number[]; runStatus?: string; runId?: string; reportMode?: "summary" | "full" };

export function ResultReportSummary({ result, companyName, pageCount, selectedPages }: ReportContext & { result: HostedResult }) {
  companyName = metadataText(companyName);
  const year = result.pipeline?.reporting_scope?.report_year;
  const counts = resultBrowserSummary(result);
  const scope = resultScope(result, pageCount, selectedPages);
  const rows = ["E3", "E2", "E1", "E0"].map(grade => ({ grade, confirmed: counts.confirmedGrades[grade], provisional: counts.grades[grade] }));
  const max = Math.max(1, ...rows.map(row => row.confirmed + row.provisional));
  return <section className="result-report-summary" aria-label="보고서 요약">
    <dl className="report-facts">
      {companyName && <div><dt>회사</dt><dd>{companyName}</dd></div>}
      {typeof year === "number" && Number.isFinite(year) && <div><dt>보고연도</dt><dd>{year}</dd></div>}
      <div><dt>분석 범위</dt><dd>{scope.pages.length}쪽 {scope.total !== null && <small>/ 전체 {scope.total}쪽</small>}</dd></div>
      <div><dt>추출 주장</dt><dd>{counts.total}건</dd></div>
    </dl>
    {counts.provisional > 0 && <p className="hosted-note">이전 버전 결과 · 추가 확인이 필요한 주장을 별도로 표시합니다.</p>}
    <div className={`metrics result-metrics${counts.provisional ? " has-legacy" : ""}`} role="group" aria-label="판정 상태별 집계">
      <div><span>확정</span><strong>{counts.confirmed}<small>건</small></strong><p>분석 범위의 근거로 판정 완료</p></div>
      {counts.provisional > 0 && <div><span>추가 확인 필요</span><strong>{counts.provisional}<small>건</small></strong><p>이전 버전 결과</p></div>}
      <div><span>보류</span><strong>{counts.hold}<small>건</small></strong><p>판정에 필요한 확인 사항 있음</p></div>
    </div>
    <div className="report-grade-chart" role="group" aria-label="등급 분포">
      <div className="report-chart-heading"><h3>등급 분포</h3><p><span className="confirmed-key">● 확정</span> {counts.provisional > 0 && <span className="provisional-key">▧ 추가 확인 필요</span>} · 보류 {counts.hold}건은 등급 미부여</p></div>
      {rows.map(row => <div className="report-grade-row" key={row.grade} aria-label={`${row.grade} 확정 ${row.confirmed}건${counts.provisional ? `, 추가 확인 필요 ${row.provisional}건` : ""}`}>
        <b>{row.grade}</b><div className="report-grade-track" aria-hidden="true"><span className="confirmed" style={{ width: `${row.confirmed / max * 100}%` }} />{row.provisional > 0 && <span className="provisional" style={{ width: `${row.provisional / max * 100}%` }} />}</div><span>{row.confirmed + row.provisional}건 {counts.provisional > 0 && <small>(확정 {row.confirmed} · 추가 확인 필요 {row.provisional})</small>}</span>
      </div>)}
      <p className="caption">등급은 연결된 근거의 수준을 나타냅니다.{counts.provisional > 0 && " 추가 확인 필요 등급은 이전 버전에서 확인된 최소 수준이며 추가 근거에 따라 달라질 수 있습니다."}</p>
    </div>
  </section>;
}

const escapeHtml = (value: unknown) => String(value ?? "").replace(/[&<>"']/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]!));

/** Standalone, printable report. Export all claims, independent of UI filters/pagination. */
export function resultReportHtml(result: HostedResult, context: ReportContext = {}): string {
  context = { ...context, companyName: metadataText(context.companyName), reportName: reportTitle(context.reportName) };
  const e = escapeHtml;
  const counts = resultBrowserSummary(result);
  const scope = resultScope(result, context.pageCount, context.selectedPages);
  const execution = resultExecution(result, context.runStatus);
  const reporting = result.pipeline?.reporting_scope;
  const failures = resultSourceFailures(result);
  const data = analyzedData(result), insight = reportInsights(result);
  if (context.reportMode === "summary") {
    const confirmed = result.claims.map(claim => resultBrowserItem(result, claim)).filter(item => item.confirmed);
    const holds = w18HoldBreakdown(result);
    const excerpt = (quote: string) => { const text = quote.replace(/\s+/g, " ").trim(); return text.length > 80 ? `${text.slice(0, 80)}…` : text; };
    return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>ProofOps · ${e(context.reportName || "분석 결과")} · 요약본</title><style>
    body{font:14px/1.5 system-ui,sans-serif;color:#18362b;max-width:980px;margin:32px auto;padding:0 20px}h1{font-size:24px}h2{font-size:18px;margin:20px 0 8px}p{margin:8px 0}table{border-collapse:collapse;width:100%;margin:12px 0}th,td{padding:5px 8px;text-align:left;vertical-align:top;border-bottom:1px solid #cbd8d0}td:last-child{overflow-wrap:anywhere}.note{color:#52665c;font-size:12px}.summary-claims{font-size:12px;line-height:1.4}.summary-claims th:not(:last-child),.summary-claims td:not(:last-child){white-space:nowrap}thead{display:table-header-group}@page{size:A4;margin:14mm}@media print{body{margin:0;padding:0;font-size:10pt}h2{break-after:avoid}tr{break-inside:avoid}.summary-claims{font-size:9pt;line-height:1.3}.summary-claims th,.summary-claims td{padding:2px 5px}}
    </style></head><body><header><p>ProofOps · 근거 점검 요약본</p><h1>${e(context.reportName || "분석 결과 보고서")}</h1>
    <p>${context.companyName ? `${e(context.companyName)} · ` : ""}${e(execution.title)}</p><p>선택 ${scope.pages.length}쪽${scope.total !== null ? ` / 전체 ${scope.total}쪽` : ""} · 원본 PDF ${e(pageRanges(scope.pages))}</p>
    ${counts.provisional ? '<p class="note">이전 버전 결과 · 추가 확인 필요 주장은 확정·보류와 별도로 집계합니다.</p>' : ""}
    <p><b>전체 ${counts.total}건 · 확정 ${counts.confirmed}건${counts.provisional ? ` · 추가 확인 필요 ${counts.provisional}건` : ""} · 보류 ${counts.hold}건</b></p>
    <p class="note">선택 범위의 입증 수준입니다. 보류는 E0나 기업의 근거 결손이 아니며, 원문 검증과 등급 확정은 별개입니다.</p></header>
    <h2>등급 분포</h2><table aria-label="등급 분포"><thead><tr><th>등급</th><th>확정</th>${counts.provisional ? "<th>추가 확인 필요</th>" : ""}</tr></thead><tbody>${["E3","E2","E1","E0"].map(grade=>`<tr><th>${grade}</th><td>${counts.confirmedGrades[grade]}건</td>${counts.provisional ? `<td>${counts.grades[grade]}건</td>` : ""}</tr>`).join("")}</tbody></table>
    <p class="note">${e(gradeMeanings.find(g=>g.grade === "E2")!.text)}</p>
    <h2>주요 보류 사유</h2>${holds.length ? `<ul>${holds.map(h=>`<li>${e(h.reason)} · ${h.count}건</li>`).join("")}</ul>` : "<p>보류 주장 없음</p>"}<p class="note">주장마다 대표 사유 하나를 집계합니다.</p>
    ${reporting?.warning ? `<p>${e(reporting.warning)}</p>` : ""}${failures.length ? `<p>처리·추출 실패 위치 ${failures.length}건 · 상세 내용은 전체 상세에서 확인하세요.</p>` : ""}
    <h2>확정 주장 목록 · ${confirmed.length}건</h2><p class="note">모든 확정 주장을 포함합니다. 문장은 80자까지 발췌하며, 전문·요소별 근거·보류 주장 기록은 ‘전체 상세’에서 확인하세요.</p>
    ${confirmed.length ? `<table class="summary-claims"><thead><tr><th>쪽</th><th>트랙</th><th>등급</th><th>주장 발췌</th></tr></thead><tbody>${confirmed.map(item=>`<tr data-summary-claim-id="${e(item.claim.claim_id)}"><td>${e(item.page)}</td><td>${e(trackText[item.track] ?? "미분류")}</td><td>${e(item.grade)}</td><td>${e(excerpt(item.claim.quote))}</td></tr>`).join("")}</tbody></table>` : "<p>확정 주장 없음</p>"}
    <footer class="note">${context.runId ? `분석 번호 ${e(context.runId)} · ` : ""}${result.w16_source_document?.sha256 ? `원본 식별값 ${e(result.w16_source_document.sha256)}` : ""}<p>요약본 · 브라우저 인쇄로 PDF 저장 가능</p></footer></body></html>`;
  }
  const notices = [...new Set([...result.hold_reasons, ...(result.hold_reason_details ?? []).map(item => item.code), ...(result.pipeline?.reason ? [result.pipeline.reason] : []), ...(result.pipeline?.extraction_outcome?.code && result.pipeline.extraction_outcome.code !== "OK" ? [result.pipeline.extraction_outcome.code] : [])])];
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><title>ProofOps · ${e(context.reportName || "분석 결과 보고서")}</title><style>
  body{font:15px/1.7 system-ui,sans-serif;color:#18362b;max-width:980px;margin:40px auto;padding:0 24px}h1{font-size:28px}h2{font-size:20px}h3{font-size:17px}header,article{border-bottom:1px solid #cbd8d0;padding-bottom:24px;margin-bottom:28px}table{border-collapse:collapse;width:100%;margin:16px 0}th,td{border:1px solid #cbd8d0;padding:10px;text-align:left;vertical-align:top}blockquote{margin:8px 0;padding-left:14px;border-left:3px solid #267b61;white-space:pre-wrap}p,td,blockquote{overflow-wrap:anywhere}.note{color:#52665c}pre{white-space:pre-wrap;overflow-wrap:anywhere}dt{font-weight:bold}dd{margin:0 0 8px}@media print{body{margin:0;padding:0;font-size:10pt}h2,h3{break-after:avoid}tr,blockquote{break-inside:avoid}thead{display:table-header-group}}@media(max-width:600px){body{margin:20px auto;padding:0 12px}table{font-size:12px}th,td{padding:6px}}
  @page{size:A4;margin:16mm 14mm}</style></head><body><header><p>ProofOps · 보고서 근거 점검</p><h1>${e(context.reportName || "분석 결과 보고서")}</h1>
  <p>${e(execution.title)} · ${e(execution.meaning)}</p><dl>${context.companyName ? `<dt>회사</dt><dd>${e(context.companyName)}</dd>` : ""}${typeof reporting?.report_year === "number" && Number.isFinite(reporting.report_year) ? `<dt>보고연도</dt><dd>${e(reporting.report_year)}</dd>` : ""}${reporting?.period_start && reporting.period_end ? `<dt>보고기간</dt><dd>${e(reporting.period_start)} ~ ${e(reporting.period_end)}</dd>` : ""}<dt>분석 범위</dt><dd>선택 ${scope.pages.length}쪽${scope.total !== null ? ` / 전체 ${scope.total}쪽` : ""} · 원본 PDF ${e(pageRanges(scope.pages))}</dd>${scope.claims?.length ? `<dt>주장 추출 대상</dt><dd>${e(pageRanges(scope.claims))}</dd>` : ""}</dl>
  ${counts.provisional ? '<p class="note">이전 버전 결과 · 추가 확인 필요 주장은 확정·보류와 별도로 집계합니다.</p>' : ""}
  <p>전체 ${counts.total}건 · 확정 ${counts.confirmed}건${counts.provisional ? ` · 추가 확인 필요 ${counts.provisional}건` : ""} · 보류 ${counts.hold}건</p><p>원문 확인 ${counts.verified} / ${counts.total}건${counts.stageSeconds !== null ? ` · 처리 시간 ${Math.round(counts.stageSeconds)}초 (단계 합계·대기 제외)` : ""}</p>
  ${`<h2>등급 의미</h2>${gradeMeanings.map(g=>`<p><b>${g.grade} · ${g.title}</b> — ${g.text}</p>`).join("")}<p>등급은 분석 범위의 입증 수준입니다. 기업 성과의 진위·위법을 단정하지 않으며 보류는 E0가 아닙니다.</p><h2>분석한 데이터</h2><p>${data.tables !== null ? `표 ${data.tables}개 · ` : ""}표 주장 ${data.tableClaims}건 · 주장 원문 검증 ${data.verified}건</p><h2>근거 보강 필요 영역</h2>${insight.needs.slice(0,5).map(n=>`<p>${e(n.track)}${n.metric ? ` · ${e(n.metric)}` : ""}: ${n.count}건 — ${e([...n.elements].join(", "))}</p>`).join("") || "<p>기록된 보강 영역 없음</p>"}${insight.needs.length > 5 ? `<p>나머지 ${insight.needs.length - 5}개 영역 · ${insight.needs.slice(5).reduce((sum,n)=>sum+n.count,0)}건</p>` : ""}<h2>주요 확정 주장</h2>${insight.top.map(i=>`<p>${e(i.grade)} · ${e(i.page)}</p><blockquote>${e(i.claim.quote)}</blockquote>`).join("") || "<p>확정 주장 없음</p>"}`}
  <table aria-label="등급 분포"><thead><tr><th>등급</th><th>확정</th>${counts.provisional ? "<th>추가 확인 필요</th>" : ""}</tr></thead><tbody>${["E3","E2","E1","E0"].map(grade => `<tr><th>${grade}</th><td>${counts.confirmedGrades[grade]}건</td>${counts.provisional ? `<td>${counts.grades[grade]}건</td>` : ""}</tr>`).join("")}</tbody></table>
  <p class="note">보류에는 등급을 부여하지 않습니다.${counts.provisional ? " 추가 확인 필요 등급은 이전 버전에서 확인된 최소 수준으로 추가 근거에 따라 달라질 수 있습니다." : ""} 원문 위치 확인과 등급 확정은 별개입니다. 선택하지 않은 쪽은 주장 추출 대상에서 제외하며 다른 쪽의 검증·연결된 근거는 상세 인용에 표시합니다. 이 결과는 기업 성과의 진위나 제3자 보증을 대신하지 않습니다.</p>
  ${reporting?.warning ? `<p>${e(reporting.warning)}</p>` : ""}${notices.map(code => `<p>${e(holdReasonText(code, result.hold_reason_details))}</p>`).join("")}${failures.map(failure => `<p>${e(failure.page)} · ${e(failure.reason)} · ${e(failure.sourceIds.join(", "))}</p>`).join("")}
  <p class="note">${context.runId ? `분석 번호 ${e(context.runId)} · ` : ""}${result.w16_source_document?.sha256 ? `원본 식별값 ${e(result.w16_source_document.sha256)} · ` : ""} 전체 주장 포함 · 브라우저 인쇄로 PDF 저장 가능</p></header>
  ${result.claims.map((claim, index) => {
    const item = resultBrowserItem(result, claim);
    const grade = gradeSummary(claim);
    // Omit only the obsolete display caption from the audit view; retain decisions, reasons and evidence.
    return `<article data-claim-id="${e(claim.claim_id)}"><h2>${index + 1}. ${e(item.page)} · ${e(item.badge)}</h2><blockquote>${e(claim.quote)}</blockquote><p>${e(claimSourceText(claim))}${tableObservationText(claim.table_observation) ? ` · ${e(tableObservationText(claim.table_observation))}` : ""}</p><p>${e(claimEvidenceLine(result, claim))}</p><p>${e(grade.note)}</p><h3>왜 이 등급인가요?</h3><p>${e(claimExplanation(result, claim).text)}</p>${claimExplanation(result, claim).rules.length ? `<p class="note">${e(claimExplanation(result, claim).rules.join(" · "))}</p>` : ""}${(item.confirmed ? [] : claimHoldReasons(claim)).map(reason => `<p>${e(reason)}</p>`).join("")}<table><thead><tr><th>요소</th><th>상태</th><th>원문 근거</th></tr></thead><tbody>${item.elements.map(element => `<tr><th>${e(elementLabels[element.element_id] ?? element.element_id)}</th><td>${e(resultElementText(claim, element))}</td><td>${element.evidence_refs.map(ref => `<p>${e(claim.source_ref_page_space === "original" ? `p.${ref.page_num}` : pageLabel(result, ref.page_num))} · ${ref.verification_state === "verified" ? "원문 확인" : "원문 대조 필요"}</p><blockquote>${e(ref.quote)}</blockquote>`).join("") || "연결된 원문 인용 없음"}</td></tr>`).join("")}</tbody></table><details><summary>원본 판정 기록</summary><pre>${e(JSON.stringify({ claim_id: claim.claim_id, revision: claim.revision, source_quality: claim.source_quality, decision: claim.decision, sc1_confirmation: claim.sc1_confirmation, provisional_grade: claim.provisional_grade ? { ...claim.provisional_grade, display: undefined } : claim.provisional_grade, hold_reason: claim.hold_reason }, null, 2))}</pre></details></article>`;
  }).join("") || "<p>선택한 범위에 표시할 추출 주장이 없습니다. 보고서 전체에 주장이 없다는 뜻은 아닙니다.</p>"}</body></html>`;
}

export function downloadResultReport(result: HostedResult, context: ReportContext) {
  const url = URL.createObjectURL(new Blob([resultReportHtml(result, context)], { type: "text/html;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `ProofOps-${(context.companyName || context.reportName || "분석결과").replace(/[^\p{L}\p{N}._-]/gu, "_").slice(0,80)}.html`;
  document.body.appendChild(link); link.click(); link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Default to the compact summary; full detail remains an explicit choice. */
export function printResultReport(result: HostedResult, context: ReportContext): boolean {
  const popup = window.open("", "_blank");
  if (!popup) return false;
  popup.opener = null;
  popup.document.open();
  popup.document.write(resultReportHtml(result, { ...context, reportMode: context.reportMode ?? "summary" }));
  popup.document.close();
  void popup.document.fonts.ready.then(() => { if (!popup.closed) { popup.focus(); popup.print(); } });
  return true;
}
