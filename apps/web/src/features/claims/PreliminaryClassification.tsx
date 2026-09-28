import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { ApiError, errorMessage, isSessionError, requestJson, type Session } from "../session/api";

type View = {
  eligible: boolean; ineligible_reason: string | null; blocked_reason: string | null; etag: string | null;
  sources: Array<{ source_index: number; quote: string; source_ref: { page_num: number } }>;
  dimension_axes: string[];
  current_classification: { origin: string; track: string; revision: number } | null;
  pending_job: { job_id: string; status: string } | null;
};
type Props = { apiBase: string; runId: string; claimId: string; untagged: boolean; session: Session; onSessionInvalid: () => void };
const axes: Record<string, string> = { entity: "기업·주체", metric: "지표", reporting_period: "보고 기간", facility: "사업장", scope: "Scope", product: "제품", material: "물질", boundary: "범위" };
const holds: Record<string, string> = { ALREADY_TAGGED: "이미 태깅되어 검토 큐에서 수정할 수 있습니다.", NOT_PRELIMINARY_BLOCKED: "현재 선행분류 검토 대상이 아닙니다.", SOURCE_UNVERIFIED: "원문 검증이 먼저 필요합니다.", SOURCE_VALIDATION_REQUIRED: "원문 검증이 먼저 필요합니다.", RUN_JOB_OUTSTANDING: "진행 중인 태깅 작업이 있습니다.", LINEAGE_UNAVAILABLE: "검토에 필요한 실행 기록이 아직 준비되지 않았습니다." };

export function PreliminaryClassification(props: Props) {
  if (props.session.role !== "reviewer" && props.session.role !== "admin") return null;
  return <ClassificationForm key={`${props.session.tenant_id}:${props.session.user_id}:${props.runId}:${props.claimId}`} {...props} />;
}

function ClassificationForm({ apiBase, runId, claimId, untagged, session, onSessionInvalid }: Props) {
  const [view, setView] = useState<View | null>(null);
  const [track, setTrack] = useState("");
  const [category, setCategory] = useState("");
  const [dimensions, setDimensions] = useState<Record<string, { source: string; quote: string }>>({});
  const [reason, setReason] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [stale, setStale] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null);
  const submission = useRef<{ payload: string; key: string } | null>(null);
  const endpoint = `${apiBase}/v1/runs/${runId}/claims/${claimId}/classification`;

  const load = useCallback(async () => {
    controller.current?.abort();
    const request = new AbortController(); controller.current = request;
    setBusy(true); setError("");
    try {
      const value = await requestJson<View>(endpoint, { signal: request.signal });
      if (request.signal.aborted) return;
      setView(value); setStale(false); setTrack(""); setCategory(""); setDimensions({}); setReason(""); setAcknowledged(false); setAccepted(false);
      submission.current = null;
    } catch (failure) {
      if (request.signal.aborted) return;
      if (isSessionError(failure)) return onSessionInvalid();
      setError(errorMessage(failure, "분류 검토를 불러올 수 없습니다. 서버의 지원 여부와 권한을 확인해 주세요."));
    } finally { if (!request.signal.aborted) setBusy(false); }
  }, [endpoint, onSessionInvalid]);
  useEffect(() => { void load(); return () => controller.current?.abort(); }, [load]);

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!view?.eligible || !view.etag || busy || stale || accepted || !track || !category || !acknowledged) return;
    const payload = JSON.stringify({ track, safe_harbor_category: category === "unknown" ? null : category,
      dimensions: Object.fromEntries(view.dimension_axes.map(axis => {
        const value = dimensions[axis];
        return [axis, value?.source ? { source_index: Number(value.source), quote: value.quote } : null];
      })), reason });
    if (submission.current?.payload !== payload) submission.current = { payload, key: crypto.randomUUID() };
    const request = new AbortController(); controller.current?.abort(); controller.current = request;
    setBusy(true); setError("");
    try {
      await requestJson(endpoint, { method: "POST", signal: request.signal, body: payload,
        headers: { "Content-Type": "application/json", "X-CSRF-Token": session.csrf_token,
          "If-Match": view.etag, "Idempotency-Key": submission.current.key } });
      if (!request.signal.aborted) setAccepted(true);
    } catch (failure) {
      if (request.signal.aborted) return;
      if (isSessionError(failure)) return onSessionInvalid();
      if (failure instanceof ApiError && [409, 412].includes(failure.status)) {
        setStale(true); setError("상태가 변경되었습니다. 초안을 확인한 뒤 최신 상태를 다시 불러와 주세요.");
      } else setError(errorMessage(failure, "분류를 저장하지 못했습니다. 원문 인용과 입력을 확인해 주세요."));
    } finally { if (!request.signal.aborted) setBusy(false); }
  }

  const invalidDimensions = Object.values(dimensions).some(value => value.source !== "" && !value.quote.trim());
  if (!untagged && !view?.current_classification) return null;
  return <section aria-labelledby="classification-heading" aria-busy={busy}>
    <h3 id="classification-heading">미분류 주장 검토</h3>
    {error ? <p role="alert">{error}</p> : null}
    {busy ? <p role="status">분류 검토를 처리하고 있습니다.</p> : null}
    {accepted ? <p role="status">태깅 요청을 등록했습니다. 후속 태깅이 끝난 뒤 <Link to={`/runs/${runId}/reviews`}>검토 큐</Link>에서 요소를 확인하세요. 분류만으로 등급이 확정되지는 않습니다.</p> : null}
    {view?.current_classification ? <p>분류 이력: {view.current_classification.origin === "human_classification" ? "사람 검토" : "AI 위임 검토(사람 아님)"} · {view.current_classification.track} · revision {view.current_classification.revision} · <Link to={`/runs/${runId}/reviews`}>검토 큐 열기</Link></p> : null}
    {view?.pending_job ? <p role="status">후속 태깅 상태: {view.pending_job.status}</p> : null}
    {view && !view.eligible ? <p>분류 확정 불가: {holds[view.ineligible_reason ?? ""] ?? view.ineligible_reason ?? view.blocked_reason}</p> : null}
    {(!view || stale || !view.eligible || accepted) ? <button type="button" disabled={busy} onClick={() => void load()}>최신 상태 불러오기 (초안 초기화)</button> : null}
    {view?.eligible && !accepted ? <form onSubmit={save}>
      <p>원문과 보류·응답 충돌을 확인한 뒤 직접 분류하세요. 모르는 항목은 미상으로 남습니다. 확정하면 기존 예산 안에서 후속 모델 태깅을 요청합니다.</p>
      <fieldset disabled={busy || stale}><legend>분류와 원문 근거</legend>
        <label>주장 유형 <select name="classification-track" required value={track} onChange={e => setTrack(e.target.value)}>
          <option value="">유형 선택</option><option value="goal">목표 · 앞으로 할 일</option><option value="performance">성과 · 이행 또는 보고 결과</option><option value="management">관리체계 · 조직·제도·프로세스</option>
        </select></label>
        <label>세이프하버 범주 후보 <select name="classification-category" required value={category} onChange={e => setCategory(e.target.value)}>
          <option value="">범주 검토</option><option value="unknown">미상 · 확정하지 않음</option><option value="forward_looking">미래 예측</option><option value="emissions_estimate">배출량 추정</option><option value="third_party_information">제3자 정보</option>
        </select></label>
        <p>범주 선택은 법적 면책 여부의 판정이 아닙니다.</p>
        {view.dimension_axes.map(axis => { const value = dimensions[axis] ?? { source: "", quote: "" }; return <fieldset key={axis}><legend>{axes[axis] ?? axis}</legend>
          <label>인용 원문 <select name={`classification-source-${axis}`} value={value.source} onChange={e => setDimensions(old => ({ ...old, [axis]: { source: e.target.value, quote: "" } }))}>
            <option value="">미상 · 근거를 확정하지 않음</option>
            {view.sources.map(source => <option key={source.source_index} value={String(source.source_index)}>{source.source_ref.page_num}쪽 · {source.quote}</option>)}
          </select></label>
          {value.source !== "" ? <label>원문에서 그대로 인용 <input name={`classification-quote-${axis}`} required value={value.quote} onChange={e => setDimensions(old => ({ ...old, [axis]: { ...value, quote: e.target.value } }))} /></label> : null}
        </fieldset>; })}
        <label>분류 사유 <textarea name="classification-reason" required minLength={5} maxLength={1000} value={reason} onChange={e => setReason(e.target.value)} /></label>
        <label><input name="classification-acknowledge" type="checkbox" checked={acknowledged} onChange={e => setAcknowledged(e.target.checked)} />원문·응답 충돌을 검토했으며, 미상으로 남긴 항목을 포함해 이 분류로 후속 태깅을 요청합니다.</label>
        <button data-classification-submit type="submit" disabled={busy || stale || !view.etag || !track || !category || !acknowledged || reason.trim().length < 5 || invalidDimensions}>분류 확정 및 태깅 요청</button>
      </fieldset>
    </form> : null}
  </section>;
}
