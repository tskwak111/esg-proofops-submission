import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router";
import { getElementLabel } from "../labels";
import "./live-claim.css";

type Element = { name: string; element_id: string; candidate_state: string; engine_state: string; quote: string | null; quote_source?: "claim" | "context" | null };
type Step = { name: string; track?: string | null; track_inferred?: boolean; elements?: Element[]; model?: string; duration_ms?: number; replicas?: number };
type Result = {
  status: string;
  source: { page_label: string | null };
  steps: Step[];
  replicas: number;
  display_grade: { grade: string; label: string; estimated: boolean };
  decision: { decision_status: string; evidence_grade: string | null; grade_range: { floor: string; ceiling: string; open_elements: string[] } | null; open_elements?: string[] } | null;
  explanation?: string;
  cost_estimate_usd: number;
  duration_ms?: number;
};

type Example = { title: string; claim: string; context: string; page: string };
const examples: Example[] = [
  {
    title: "관리체계 · 위원회 운영",
    claim: "• ESG위원회는 환경 및 기후 관련 안건을 포함해 최소 연3회 이상 정기적으로 개최",
    context: "• ESG위원회의 기후 관련 위험 및 기회 감독 책임은 2021년 제정된 ‘ESG위원회 운영규정’에 명시",
    page: "83",
  },
  {
    title: "성과 · 온실가스 감축",
    claim: "• 온실가스 감축량 4만 톤 이상 달성",
    context: "에너지 절감 활동 및 재생에너지 사용 확대를 통한 온실가스 감축",
    page: "93",
  },
  {
    title: "목표 · 재생에너지 전환",
    claim: "• 2030년 전력사용량의 60% 재생에너지 전환 및 예상 온실가스 배출량 60% 이상 절감",
    context: "• 풍력 발전소 지분 투자 및 PPA 계약을 통한 데이터센터용 재생에너지 확보",
    page: "93",
  },
];
const stages = ["분류", "요소 태깅", "Python 규칙엔진"];
const trackNames: Record<string, string> = { management: "관리체계", performance: "성과", goal: "목표" };
const stateNames: Record<string, string> = { present: "확인", absent: "부재 후보", unknown: "확인 전", conflict: "상충" };
const errors: Record<string, string> = {
  ACCESS_DENIED: "접근 코드를 확인해 주세요.", DEMO_NOT_CONFIGURED: "실시간 체험이 아직 설정되지 않았습니다.",
  INVALID_INPUT: "입력을 확인해 주세요. 주장은 500자, 문맥은 2,000자 이하여야 합니다.",
  BODY_TOO_LARGE: "입력 길이가 허용 범위를 넘었습니다.",
  UPSTAGE_NOT_CONFIGURED: "실시간 모델 연결이 아직 설정되지 않았습니다.",
  UPSTAGE_UNAVAILABLE: "모델 서비스에 연결할 수 없습니다. 잠시 후 다시 시도해 주세요.",
  PRICE_RECHECK_REQUIRED: "운영자가 모델 가격을 재확인해야 다시 실행할 수 있습니다.",
  REQUEST_COST_CAP: "이 입력의 예상 처리 비용이 체험 한도를 넘었습니다.",
};

export function LiveClaim() {
  const [code, setCode] = useState("");
  const [claim, setClaim] = useState("");
  const [context, setContext] = useState("");
  const [page, setPage] = useState("");
  const [busy, setBusy] = useState(false);
  const [activeStage, setActiveStage] = useState(0);
  const [result, setResult] = useState<Result | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => setActiveStage(stage => Math.min(stage + 1, 2)), 3500);
    return () => window.clearInterval(timer);
  }, [busy]);

  function choose(example: Example) {
    setClaim(example.claim);
    setContext(example.context);
    setPage(example.page);
    setResult(null);
    setError("");
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setActiveStage(0); setError(""); setResult(null);
    try {
      const response = await fetch("/api/live-claim", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Demo-Access-Code": code },
        body: JSON.stringify({ claim: claim.trim(), context: context.trim(), page_label: page.trim() || null }),
        cache: "no-store",
      });
      const data = await response.json() as Result & { error?: string };
      if (!response.ok) throw new Error(errors[data.error || ""] || `요청을 처리하지 못했습니다 (${response.status}).`);
      if (!Array.isArray(data.steps) || !data.display_grade) throw new Error("실시간 API 응답을 확인할 수 없습니다.");
      setResult(data);
    } catch (reason) {
      setError(reason instanceof SyntaxError ? "이 미리보기에는 실시간 API가 연결되지 않았습니다." : reason instanceof TypeError ? "실시간 API에 연결할 수 없습니다." : reason instanceof Error ? reason.message : "요청을 완료하지 못했습니다.");
    } finally {
      setBusy(false);
    }
  }

  const tagging = result?.steps.find(step => step.name === "element_tagging");
  const classification = result?.steps.find(step => step.name === "preliminary_classification");
  const grade = result?.display_grade;
  const range = result?.decision?.grade_range;
  return <main className="static-main live-main">
    <div className="breadcrumb"><Link to="/">홈</Link><span>/</span> 실시간 체험</div>
    <div className="live-heading"><p className="eyebrow">LIVE PIPELINE</p><h1>한 문장에서 등급까지</h1><p>주장과 문맥을 넣으면 분류, 근거 태깅, 규칙 판정이 이어집니다.</p></div>
    <div className="live-grid"><section className="surface live-form-card"><div className="card-heading"><div><p className="eyebrow">INPUT</p><h2>검토할 주장</h2></div><span>시연 모드</span></div>
      <form onSubmit={submit} className="live-form">
        <label>데모 접근 코드<input type="password" autoComplete="off" value={code} onChange={e => setCode(e.target.value)} required placeholder="접근 코드" /></label>
        <div className="live-examples"><span>NAVER 2025 보고서 예시</span>{examples.map(example => <button type="button" key={example.title} onClick={() => choose(example)}><strong>{example.title}</strong><small>{example.claim}</small></button>)}</div>
        <label>주장 <small>최대 500자</small><textarea value={claim} onChange={e => { setClaim(e.target.value); setResult(null); }} maxLength={500} rows={3} required placeholder="환경 관련 주장 한 문장" /></label>
        <label>근거 문맥 <small>최대 2,000자 · 선택</small><textarea value={context} onChange={e => { setContext(e.target.value); setResult(null); }} maxLength={2000} rows={4} placeholder="같은 보고서의 관련 문단을 붙여 넣으세요." /></label>
        <label>페이지 <small>선택</small><input value={page} onChange={e => { setPage(e.target.value); setResult(null); }} maxLength={30} placeholder="예: 83" /></label>
        <button className="live-submit" type="submit" disabled={busy}>{busy ? "분석 중…" : "분석 시작 ↗"}</button>
      </form>
      {error && <p className="live-error" role="alert">{error}</p>}
      <p className="live-input-note">입력 문장은 모델로 전송됩니다. 전송 권한이 있는 텍스트를 사용해 주세요.</p>
    </section>
    <section className="surface live-result-card" aria-live="polite"><div className="card-heading"><div><p className="eyebrow">RESULT</p><h2>분석 경로</h2></div><span>{busy ? "진행 중" : result ? "완료" : "대기"}</span></div>
      <ol className="live-timeline">{stages.map((stage, index) => <li key={stage} className={result ? "done" : busy && index <= activeStage ? "active" : "waiting"}><span className="live-stage-number">0{index + 1}</span><div><strong>{stage}</strong><small>{result ? `${result.steps[index]?.duration_ms ?? 0}ms${index === 1 ? ` · ${result.replicas}회 병렬 태깅` : ""}` : busy && index === activeStage ? "처리 중…" : "대기"}</small></div></li>)}</ol>
      {result && <>
        <div className="live-summary"><span>분류</span><strong>{classification?.track ? `${trackNames[classification.track] || classification.track}${classification.track_inferred ? " (추정)" : ""}` : "분류 검토 중"}</strong><span>모델</span><strong>{classification?.model || "—"}</strong></div>
        {tagging?.elements && <div className="live-element-section"><h3>요소와 근거</h3><div className="live-table-wrap"><table className="live-table"><thead><tr><th>요소</th><th>상태</th><th>근거 문구</th></tr></thead><tbody>{tagging.elements.map(element => <tr key={element.name}><td>{getElementLabel(element.element_id)}<small>{element.name}</small></td><td>{stateNames[element.engine_state] || element.engine_state}{element.candidate_state === "present" && element.engine_state !== "present" && <small>문구 후보</small>}</td><td>{element.quote ? <><q>{element.quote}</q><small>{element.quote_source === "context" ? "문맥" : "주장"} · {result.source.page_label || "입력 텍스트"}</small></> : "—"}</td></tr>)}</tbody></table></div></div>}
        <div className="live-decision"><span>시연 모드 · {grade?.estimated ? "추정 등급" : "규칙엔진 등급"}</span><strong>{grade?.grade} <small>{grade?.label}</small></strong>{range && <p>규칙엔진 범위 {range.floor}–{range.ceiling}</p>}<h3>왜 이 등급인가</h3><p>{grade?.estimated ? `현재 확인된 요소를 바탕으로 ${grade.grade}을 표시했습니다. ` : ""}{result.explanation || "입력 텍스트에서 확인된 요소를 규칙에 대입했습니다."}</p><footer>추정 비용 ${result.cost_estimate_usd.toFixed(6)} · {result.duration_ms != null ? `${(result.duration_ms / 1000).toFixed(1)}초` : "시간 정보 없음"}</footer></div>
      </>}
      {!result && !busy && <div className="live-placeholder">예시를 선택하거나 주장을 입력하면 결과가 여기에 표시됩니다.</div>}
    </section></div>
    <p className="live-footer-note">입력 텍스트 기준 시연 결과입니다. <Link to="/demo">NAVER 보고서 결과 보기 ↗</Link></p>
  </main>;
}

export default LiveClaim;
