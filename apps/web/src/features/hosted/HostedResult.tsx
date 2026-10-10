import { useMemo, useRef, useState } from "react";
import { elementLabels } from "../labels";
import { ProvisionalHold } from "../claims/ProvisionalHold";
import { HostedApiError, holdReasonText, newIdempotencyKey, type HostedClient, type HostedResult, type ResultClaim, type ResultDecision, type ResultReview } from "./hostedClient";
import { allowedChoices, buildResolveBody, candidateRefs, decisionLine, elementStateText, gradeSummary, openReviewFor, pageLabel, refKey, reviewStatusText, sourceQualityText, trackText, type ElementChoice } from "./resultModel";

// Reuses the saved-case visual language (claim-list / claim-item / claim-detail / element-table classes from static-demo.css).
export function canReview(role: string | null | undefined) { return role === "reviewer" || role === "admin"; }

export function ResultView({ result, role, client, runId, onReload }: { result: HostedResult; role?: string | null; client?: HostedClient; runId?: string; onReload?: () => Promise<void> }) {
  const [selectedId, setSelectedId] = useState<string | null>(result.claims[0]?.claim_id ?? null);
  const selected = result.claims.find(claim => claim.claim_id === selectedId) ?? result.claims[0] ?? null;
  const counts = useMemo(() => ({
    total: result.claims.length,
    verified: result.claims.filter(c => c.source_quality === "verified").length,
    confirmed: result.claims.filter(c => c.confirmed_grade).length,
    open: result.reviews.filter(r => r.status === "open").length,
  }), [result]);
  return <section className="hosted-result" aria-label="분석 결과" data-testid="hosted-result">
    <p className="eyebrow">ANALYSIS RESULT</p>
    <div className="hosted-counts" role="group" aria-label="결과 요약">
      <span>추출 주장 <b>{counts.total}</b>건</span><span>원문 검증 <b>{counts.verified}</b>건</span>
      <span>확정 등급 <b>{counts.confirmed}</b>건</span><span>사람 검토 대기 <b>{counts.open}</b>건</span>
    </div>
    {result.hold_reasons.length > 0 && <div className="hosted-notice" role="status">
      <h3>보류 사유</h3>
      <ul>{result.hold_reasons.map(code => <li key={code}>{holdReasonText(code)}</li>)}</ul>
      <p>보류·확인되지 않음은 ‘근거 없음’이 아닙니다. 확정 등급은 규칙 판정이 끝난 주장에만 표시되며 전체 보고서 등급은 만들지 않습니다.</p>
    </div>}
    {result.pipeline?.reporting_scope?.report_year != null && <p className="hosted-note">보고 범위: {result.pipeline.reporting_scope.report_year}년 ({result.pipeline.reporting_scope.period_start} ~ {result.pipeline.reporting_scope.period_end})</p>}
    {!result.claims.length ? <p className="empty">{result.pipeline?.status === "completed"
      ? "선택한 범위의 분석이 정상 완료되었으나 환경 주장을 발견하지 못했습니다. 보고서 전체에 주장이 없다는 뜻은 아닙니다."
      : "분석이 보류되어 표시할 주장이 없습니다. 처리 단계와 보류 사유를 확인해 주세요. 근거 없음으로 판정한 결과가 아닙니다."}</p> :
      <div className="claims-layout live-claims-layout">
        <div className="claim-list" aria-label="분석 결과 주장 목록">{result.claims.map(claim => {
          const grade = gradeSummary(claim);
          return <button type="button" className={`claim-item ${selected?.claim_id === claim.claim_id ? "selected" : ""}`} key={claim.claim_id} onClick={() => setSelectedId(claim.claim_id)}>
            <div className="claim-item-top"><span>{pageLabel(result, claim.page_num)} · {claim.track ? trackText[claim.track] ?? claim.track : claim.provisional_track ? `${trackText[claim.provisional_track] ?? claim.provisional_track} · 잠정 분류` : "분류 미합의"}</span>
              <span className={`mini-status ${grade.confirmed ? "good" : "warn"}`}>{grade.confirmed ? decisionLine(claim.decision) : "확정 등급 아님"}</span></div>
            <p>{claim.quote}</p><small>{sourceQualityText(claim.source_quality)}{claim.hold_reason ? ` · ${holdReasonText(claim.hold_reason)}` : ""}</small></button>;
        })}</div>
        {selected && <ClaimPanel key={selected.claim_id} claim={selected} result={result} role={role} client={client} runId={runId} onReload={onReload} />}
      </div>}
  </section>;
}

function ClaimPanel({ claim, result, role, client, runId, onReload }: { claim: ResultClaim; result: HostedResult; role?: string | null; client?: HostedClient; runId?: string; onReload?: () => Promise<void> }) {
  const grade = gradeSummary(claim);
  const review = openReviewFor(result, claim.claim_id);
  const refPages = [...new Set(claim.source_refs.map(ref => pageLabel(result, ref.page_num)))];
  return <aside className="claim-detail">
    <div className="detail-top"><div><p className="eyebrow">CLAIM DETAIL · {pageLabel(result, claim.page_num)}</p><h3>주장과 판정 근거</h3></div></div>
    <div className="source-quote"><span>{claim.source_quality === "verified" ? "보고서 원문" : "추출 문장"} · {refPages.join(", ") || pageLabel(result, claim.page_num)}</span>
      <blockquote>“{claim.quote}”</blockquote><small>{sourceQualityText(claim.source_quality)}</small></div>
    <div className="decision-panel" data-testid="grade-panel">
      <span>{claim.track ? `${trackText[claim.track] ?? claim.track} 트랙` : claim.provisional_track ? `${trackText[claim.provisional_track] ?? claim.provisional_track} 트랙 · 잠정 분류` : "트랙 합의 전"}</span>
      <strong>{grade.headline}</strong>
      <p>{grade.note}</p>
      {claim.provisional_grade && <ProvisionalHold grade={claim.provisional_grade} />}
    </div>
    {claim.pipeline_stage && <p>처리 단계: {claim.pipeline_stage} · {claim.pipeline_status}</p>}
    {claim.source_status && <p>{claim.source_status.display} · {claim.source_status.reason}</p>}
    {claim.hold_reason && <p className="hosted-note">보류 사유: {holdReasonText(claim.hold_reason)}</p>}
    {claim.provisional_grade?.elements?.length ? <div className="detail-block"><h4>잠정 요소 · 미확정</h4><table className="element-table"><thead><tr><th>요소</th><th>상태</th><th>반복 판독</th><th>검증 인용</th></tr></thead><tbody>{claim.provisional_grade.elements.map(element => <tr key={element.element_id}><th>{elementLabels[element.element_id] ?? element.element_id}</th><td>{elementStateText(element.state, "verified")}</td><td>{element.votes}/{element.total_replicas}</td><td>{element.evidence_refs.map((ref, index) => <p key={index}>{pageLabel(result, ref.page_num)} · {ref.quote}</p>)}</td></tr>)}</tbody></table></div> : null}
    <div className="detail-block"><h4>요소별 근거</h4>
      {claim.elements.length ? <div className="element-table-wrap"><table className="element-table"><thead><tr><th>요소</th><th>상태</th><th>근거 문구</th></tr></thead><tbody>
        {claim.elements.map(element => {
          const missing = claim.missing_evidence.find(m => m.element_id === element.element_id);
          return <tr key={element.element_id}><th scope="row"><b>{element.element_id}</b><span>{elementLabels[element.element_id] ?? "요소"}</span></th>
            <td><span className={`element-state ${element.state}`}>{elementStateText(element.state, claim.source_quality)}</span></td>
            <td>{element.evidence_refs.length ? element.evidence_refs.map((ref, index) => <p key={index}><small>{pageLabel(result, ref.page_num)}</small> “{ref.quote}”</p>)
              : <span className="no-evidence">{element.state === "unknown" ? "연결된 근거 문구 없음 · 근거가 없다는 뜻이 아닙니다" : "연결된 근거 문구 없음"}{missing?.undetermined_reason ? ` (${missing.undetermined_reason})` : ""}</span>}</td></tr>;
        })}</tbody></table></div>
        : <p className="caption">요소 태깅 전입니다. 빈 요소를 ‘근거 없음’으로 보지 않습니다.</p>}
    </div>
    {review && canReview(role) && client && runId && <ReviewForm key={`${review.review_id}:${review.revision}`} claim={claim} review={review} client={client} runId={runId} onReload={onReload} result={result} />}
    {review && !canReview(role) && <p className="hosted-note">이 주장은 사람 검토를 기다립니다. 이 검토는 reviewer 또는 admin 역할만 처리할 수 있습니다.</p>}
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
    <p className="hosted-note">검토 사유 코드: {review.reason_codes.join(", ") || "없음"}</p>
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
