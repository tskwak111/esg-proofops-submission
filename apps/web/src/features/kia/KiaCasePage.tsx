import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { getElementLabel } from "../labels";
import "./kia.css";

type Evidence = { page: number; quote: string };
type Claim = {
  id: string; page: number; track: string; quote: string; statement: string;
  elements: { id: string; state: string; evidence: Evidence[] }[];
  decision: { grade: string | null; label: string | null; status: string;
    grade_range: { floor: string; ceiling: string; open_elements: string[] } | null;
    display_grade: string | null; display_label: string | null;
    estimated: boolean; missing: string[]; unresolved: string[] };
};
type NumericCheck = { id: string; claim_id: string; status: string; reported: string;
  computed_percent: string | null; reported_percent: string | null;
  observations: (Evidence & { value: string; unit: string })[]; note: string };
type Assurance = { id: string; claim_id: string; status: string; provider: string;
  level: string; period: string; pages: number[]; quote: string; metrics: string; note: string };
type Snapshot = { title: string; source_url: string; claims: Claim[];
  numeric_checks: NumericCheck[]; assurance: Assurance[];
  run: { rule_pack_name: string; rule_pack_hash: string } };

const tracks: Record<string, string> = { performance: "성과", goal: "목표", management: "관리체계" };
const states: Record<string, string> = { present: "확인", absent: "미기재", unknown: "미확인",
  conflict: "상충", not_applicable: "비적용" };

function DecisionBadge({ claim }: { claim: Claim }) {
  const { decision } = claim;
  return <span className={`kia-grade ${decision.estimated ? "estimated" : ""}`}>
    {decision.display_grade || "보류"}{decision.estimated ? " · 예비 등급" : ""}
  </span>;
}

function ClaimDetail({ claim }: { claim: Claim }) {
  const { decision } = claim;
  return <article className="kia-detail">
    <div className="kia-detail-top"><span className="kia-overline">원문 p.{claim.page} · {tracks[claim.track]}</span><DecisionBadge claim={claim} /></div>
    <h3>{claim.statement}</h3>
    <blockquote>“{claim.quote}”</blockquote>
    <div className="kia-decision">
      <strong>{decision.display_grade || "판정 보류"} {decision.estimated ? "· 예비 등급" : ""}</strong>
      <span>{({ INCOMPLETE: "추가 근거 필요", SUBSTANTIATED: "근거 확인", UNSUBSTANTIATED: "근거 부족" } as Record<string, string>)[decision.display_label || ""] || decision.display_label || "근거 확인 필요"}</span>
      {decision.grade_range && <p>가능 범위 {decision.grade_range.floor}–{decision.grade_range.ceiling} · 미해결 {decision.grade_range.open_elements.map(getElementLabel).join(", ")}</p>}
      {decision.missing.length > 0 && <p>보완 요소 {decision.missing.map(getElementLabel).join(", ")}</p>}
    </div>
    <h4>요소별 근거</h4>
    <div className="kia-element-list">{claim.elements.map(element => <div key={element.id} className="kia-element">
      <span className={`kia-state ${element.state}`}>{states[element.state] || element.state}</span>
      <strong>{getElementLabel(element.id)}</strong>
      {element.evidence.length ? element.evidence.map((ref, index) => <p key={index}>p.{ref.page} “{ref.quote}”</p>) : <p>연결된 원문 인용 없음</p>}
    </div>)}</div>
  </article>;
}

export default function KiaCasePage() {
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState(false);
  const [selectedId, setSelectedId] = useState("DOC034-C01");
  const [track, setTrack] = useState("all");
  const [query, setQuery] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    fetch(`${import.meta.env.BASE_URL}demo/kia-2025.json`, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error("snapshot unavailable"); return response.json() as Promise<Snapshot>; })
      .then(setData).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, []);
  const claims = data?.claims;
  const filtered = useMemo(() => (claims || []).filter(claim =>
    (track === "all" || claim.track === track) &&
    `${claim.statement} ${claim.id} ${claim.page}`.toLowerCase().includes(query.toLowerCase())), [claims, track, query]);
  if (error) return <main className="kia-case"><p role="alert">기아 사례를 불러오지 못했습니다.</p></main>;
  if (!data) return <main className="kia-case"><p role="status">기아 사례를 불러오는 중입니다…</p></main>;
  const selected = claims?.find(claim => claim.id === selectedId) || filtered[0];
  const estimated = claims?.filter(claim => claim.decision.estimated).length || 0;
  return <main className="kia-case">
    <nav className="kia-breadcrumb" aria-label="경로"><Link to="/">홈</Link><span>/</span><Link to="/demo">분석 결과</Link><span>/</span>기아 2025</nav>
    <section className="kia-hero">
      <div><p className="kia-overline">KIA 2025</p><h1>기아 공시의<br /><em>근거를 따라가다.</em></h1>
        <p>선택한 주장 10건을 원문, 수치 검산, 검증의견서까지 연결했습니다.</p>
        <div className="kia-hero-links"><a href="#kia-claims">주장 살펴보기 ↗</a><a href={data.source_url} target="_blank" rel="noopener noreferrer">기아 공식 보고서 ↗</a></div>
      </div><div className="kia-hero-stat"><small>검토한 주장</small><strong>10<span>건</span></strong><p>성과 3 · 목표 2 · 관리체계 5</p></div>
    </section>
    <section className="kia-summary" aria-label="검토 요약">
      <div><span>규칙 확정</span><strong>{claims?.length ? claims.filter(c => c.decision.grade).length : 0}</strong><small>등급·라벨 산출</small></div>
      <div><span title="핵심 근거 일부가 확인되기 전의 예비 판정">예비 등급</span><strong>{estimated}</strong><small>추가 근거 확인 필요</small></div>
      <div><span>수치 검산</span><strong>{data.numeric_checks.length}</strong><small>원자료와 재계산</small></div>
      <div><span>보증 연결</span><strong>{data.assurance.length}</strong><small>의견서별 범위 확인</small></div>
    </section>
    <section className="kia-workspace" id="kia-claims"><div className="kia-section-heading"><p className="kia-overline">EVIDENCE EXPLORER</p><h2>주장별 검토</h2><p>주장을 선택하면 등급과 근거를 함께 볼 수 있습니다.</p></div>
      <div className="kia-filters"><label>검색<input value={query} onChange={event => setQuery(event.target.value)} placeholder="주장, 페이지" /></label>
        <label>트랙<select value={track} onChange={event => setTrack(event.target.value)}><option value="all">전체</option><option value="performance">성과</option><option value="goal">목표</option><option value="management">관리체계</option></select></label></div>
      <div className="kia-claims-grid"><div className="kia-claim-list" aria-label="기아 주장 목록">{filtered.map(claim => <button type="button" key={claim.id} className={selected?.id === claim.id ? "selected" : ""} onClick={() => setSelectedId(claim.id)}>
        <span>p.{claim.page} · {tracks[claim.track]}</span><DecisionBadge claim={claim} /><strong>{claim.statement}</strong>
      </button>)}{filtered.length === 0 && <p>검색 결과가 없습니다.</p>}</div>{selected && <ClaimDetail claim={selected} />}</div>
    </section>
    <section className="kia-extras" aria-label="추가 근거">
      <div className="kia-section-heading"><p className="kia-overline">CROSS-CHECK</p><h2>원문을 다시 확인한 기록</h2></div>
      <div className="kia-panels"><article className="kia-panel"><h3>수치 검산 <span>{data.numeric_checks.length}</span></h3><p className="kia-panel-intro">원문 표의 수치와 보고 문구를 다시 계산했습니다.</p>
        {data.numeric_checks.map(check => <details key={check.id}><summary><span>{check.claim_id.replace("DOC034-", "")}</span><strong>{check.reported_percent ? `약 ${check.reported_percent}%` : check.reported}</strong><em>일치</em></summary>
          <div><p>{check.computed_percent ? `재계산 ${Number(check.computed_percent).toFixed(2)}%` : "지역·Scope 소계 반올림 합계 1,178.5"}</p>
            {check.observations.map((item, index) => <p key={index}>p.{item.page} · {item.value} {item.unit} · “{item.quote}”</p>)}<small>{check.note}</small></div></details>)}</article>
        <article className="kia-panel"><h3>검증의견서 연결 <span>{data.assurance.length}</span></h3><p className="kia-panel-intro">기관과 검증 범위를 주장에 연결했습니다.</p>
          {data.assurance.map(item => <details key={item.id}><summary><span>{item.claim_id.replace("DOC034-", "")}</span><strong>{item.provider}</strong><em className={item.status === "covered" ? "" : "pending"}>{item.status === "covered" ? "포함" : "연결 미확정"}</em></summary>
            <div><p>p.{item.pages.join(", ")} · {item.level === "limited" ? "제한적 보증" : item.level} · {item.period}</p><p>“{item.quote}”</p><small>{item.metrics}</small></div></details>)}</article></div>
    </section>
    <p className="kia-provenance">주장별 원문 근거와 판정 요소를 연결했습니다.</p>
  </main>;
}
