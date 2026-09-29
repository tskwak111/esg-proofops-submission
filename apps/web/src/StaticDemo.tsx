import { useEffect, useMemo, useState } from "react";
import { Link, Route, Routes, useLocation, useNavigate, useParams } from "react-router";
import { LiveClaim } from "./features/live/LiveClaim";
import { ReplayPage, replayPath } from "./features/replay/route";
import { kiaRoute } from "./features/kia/route";
import ReviewPage, { reviewRoutePath } from "./features/reviewsim/route";
import ReviewSimulator from "./features/reviewsim/ReviewSimulator";
import AuditReportPage from "./features/auditreport/AuditReportPage";
import { auditReportRoute } from "./features/auditreport/route";
import { DecisionGuide, GuideHelp } from "./DecisionGuide";
import { elementLabels } from "./features/labels";
import "./static-demo.css";

type Evidence = { page: number | null; quote: string };
type Element = { id: string; state: string; evidence: Evidence[] };
type Claim = {
  id: string; page: number | null; track: string | null; quote: string; source_verified: boolean;
  review_bucket?: string; demo_mode?: boolean; mode?: string | null; pipeline_status?: string;
  elements: Element[];
  decision: { grade: string | null; label: string | null; display_grade?: string | null; display_label?: string | null; display_note?: string | null; estimated?: boolean; grade_range: { floor: string; ceiling: string; open_elements: string[] } | null; status: string; missing: string[]; unresolved: string[] };
  review: { status: string; tag_revision: number; decision_revision: number; audit: string | null };
};
type Snapshot = {
  title: string; generated_at: string; partial: boolean;
  demo_coverage?: { claims_with_display_grade: number; claims_with_final_status: number; source_unverified: number; unclassified: number };
  review_confirmation: { status: string; confirmed_by: string; confirmed_at: string; scope: string };
  processed_reports: { title: string; sha256: string; result_path: string; run_label: string; scope: string }[];
  coverage: { pages_processed: number; pages_total: number; pages_unprocessed: number; pages_unreadable: number; claims_discovered: number; claims_decided: number; claims_needs_review: number };
  funnel: { label: string; count: number }[]; funnel_source: string;
  run: { model_ids: string[]; model_note: string; model_binding_hash: string | null; rule_pack_id: string; rule_pack_name: string; rule_pack_hash: string; model_cost_usd: number; model_paid_calls: number; model_elapsed_seconds: Record<string, number>; review_seconds: number; review_model_calls: number; demo_pass?: { calls: number; cost_usd: string; elapsed_seconds: number } };
  audit: { agreed: number; disagreed: number; uncertain: number; scope: string };
  claims: Claim[];
};

const trackText: Record<string, string> = { management: "관리체계", goal: "목표", performance: "성과" };
const stateText: Record<string, string> = { present: "근거 확인", absent: "근거 없음", unknown: "미확인", conflict: "근거 충돌", unreadable: "판독 불가", not_applicable: "비적용" };
const statusText: Record<string, string> = { decided: "규칙 판정", blocked_evidence: "근거 보류", blocked_rule_gap: "규칙 보류", not_run: "미판정", unclassified: "미분류", source_unverified: "원문 대조 필요", tagged: "태깅 완료" };
const confirmationText = "검토 완료 27건";

export function StaticDemo() {
  const location = useLocation();
  const isLive = location.pathname === "/live";
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch(`${import.meta.env.BASE_URL}demo/naver-2025.json`, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error("snapshot unavailable"); return response.json() as Promise<Snapshot>; })
      .then(setData).catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, []);
  useEffect(() => setMenuOpen(false), [location.pathname]);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches || !window.IntersectionObserver) return;
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add("in-view"); observer.unobserve(entry.target); }
    }), { threshold: .1 });
    const sections = document.querySelectorAll(".demo-site main > section");
    sections.forEach(section => observer.observe(section));
    return () => observer.disconnect();
  }, [location.pathname, data]);
  return <div className="demo-site">
    <header className="site-header"><div className="site-header-inner">
      <Link className="brand" to="/" aria-label="ProofOps 홈"><img src="/logo-mark.svg" alt="" /> ProofOps</Link>
      <button className="menu-toggle" type="button" aria-expanded={menuOpen} aria-controls="site-menu" onClick={() => setMenuOpen(!menuOpen)}>{menuOpen ? "닫기" : "메뉴"}<span aria-hidden="true">{menuOpen ? "×" : "☰"}</span></button>
      <nav id="site-menu" className={menuOpen ? "open" : ""} aria-label="주요 메뉴" onClick={() => setMenuOpen(false)}><Link to="/">서비스</Link><Link to="/analyze">분석</Link><Link to="/demo">결과</Link><Link to="/report/naver">보고서</Link><Link to="/live">문장 분석</Link></nav>
      <Link className="header-cta" to="/analyze">보고서 분석 시작 <span aria-hidden="true">↗</span></Link>
    </div></header>
    {isLive ? <LiveClaim /> : error ? <main className="static-main"><section className="surface"><h1>분석 결과를 불러오지 못했습니다</h1><p>잠시 후 새로고침해 주세요.</p></section></main> :
      !data ? <main className="static-main loading-main" role="status" aria-label="검토 결과 불러오는 중"><div className="skeleton skeleton-title" /><div className="skeleton skeleton-card" /><div className="skeleton skeleton-card" /></main> :
      <Routes><Route path="/" element={<Landing data={data} />} /><Route path="/analyze" element={<Analyze data={data} />} /><Route path={replayPath} element={<ReplayPage snapshot={data} />} /><Route path="/demo" element={<Demo data={data} />} /><Route path="/demo/:claimId" element={<Demo data={data} />} /><Route path={kiaRoute.path} element={<><div className="kia-route-toolbar"><CompanyTabs selected="kia" /><Link to="/report/kia">기아 검토 보고서 ↗</Link></div><kiaRoute.Component /></>} /><Route path={reviewRoutePath} element={<ReviewPage claims={data.claims} />} /><Route path={auditReportRoute.path} element={<AuditReportPage />} /><Route path="*" element={<NotFound />} /></Routes>}
    <footer className="site-footer"><strong>ProofOps</strong><a href="https://github.com/tskwak111/esg-proofops" target="_blank" rel="noopener noreferrer">GitHub ↗</a><span>© 2026 ProofOps</span></footer>
  </div>;
}

function NotFound() { return <main className="static-main not-found"><p className="eyebrow">404 / PAGE NOT FOUND</p><h1>페이지를 찾을 수 없습니다.</h1><p>주소를 확인하거나 검토 결과로 돌아가세요.</p><Link className="primary-link" to="/demo">결과 보기 ↗</Link></main>; }

function CompanyTabs({ selected }: { selected: "naver" | "kia" }) { return <nav className="company-tabs" aria-label="기업 선택"><Link to="/demo" aria-current={selected === "naver" ? "page" : undefined}>NAVER</Link><Link to="/demo/kia" aria-current={selected === "kia" ? "page" : undefined}>기아</Link></nav>; }

const pipeline = ["PDF 파싱", "주장 추출", "원문 검증", "사전분류 · 태깅", "규칙엔진 등급", "검토", "보고서"];

function Analyze({ data }: { data: Snapshot }) {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ name: string; matched: boolean; message: string } | null>(null);
  useEffect(() => {
    if (!result?.matched) return;
    const timer = window.setTimeout(() => navigate("/analyze/replay"), 2500);
    return () => window.clearTimeout(timer);
  }, [result, navigate]);

  async function inspect(file?: File) {
    if (!file) return;
    setBusy(true); setResult(null);
    try {
      if (!file.name.toLowerCase().endsWith(".pdf") || new TextDecoder().decode(await file.slice(0, 4).arrayBuffer()) !== "%PDF") throw new Error("PDF 파일을 선택해 주세요.");
      if (!window.crypto?.subtle) throw new Error("이 브라우저에서는 파일 지문을 계산할 수 없습니다. HTTPS 또는 localhost에서 다시 열어 주세요.");
      const hash = Array.from(new Uint8Array(await window.crypto.subtle.digest("SHA-256", await file.arrayBuffer()))).map(byte => byte.toString(16).padStart(2, "0")).join("");
      const match = data.processed_reports.find(report => report.sha256 === hash);
      setResult({ name: file.name, matched: !!match, message: match ? "분석 결과를 불러옵니다." : "일치하는 분석 결과가 없습니다." });
    } catch (error) {
      setResult({ name: file.name, matched: false, message: error instanceof Error ? error.message : "파일을 읽지 못했습니다." });
    } finally { setBusy(false); }
  }

  return <main className="static-main analyze-main"><div className="breadcrumb"><Link to="/">홈</Link><span>/</span> 보고서 분석</div>
    <section className="analyze-heading"><p className="eyebrow">REPORT ANALYSIS</p><h1>보고서에서 근거까지,<br />분석을 시작하세요.</h1><p>PDF를 선택하면 기존 분석 결과를 확인합니다. 파일은 브라우저 안에서만 읽습니다.</p></section>
    <section className="analyze-grid"><div className="surface analyze-upload"><h2>PDF 보고서 선택</h2><p>파일을 놓거나 눌러 선택하세요.</p><label className="drop-zone" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); void inspect(event.dataTransfer.files[0]); }}><span aria-hidden="true">↥</span><strong>{busy ? "파일 지문 계산 중…" : "PDF 파일을 여기에 놓기"}</strong><small>또는 클릭해 파일 선택 · PDF는 이 브라우저 안에서만 읽습니다</small><input type="file" accept=".pdf,application/pdf" aria-label="분석할 PDF 선택" disabled={busy} onChange={event => { void inspect(event.target.files?.[0]); event.target.value = ""; }} /></label>
      {result && <div className={`analyze-result ${result.matched ? "match" : "no-match"}`} role="status"><small>{result.name}</small><h3>{result.message}</h3>{result.matched ? <><Link to="/analyze/replay">분석 과정 보기 ↗</Link><small>잠시 후 분석 과정으로 이동합니다.</small></> : <><p>새 보고서의 분석은 현재 준비 중입니다.</p><Link to="/live">문장 분석하기 ↗</Link></>}</div>}
    </div><aside className="surface analyze-info"><p className="eyebrow">ANALYSIS</p><h2>NAVER 2025 통합보고서</h2><p>보고서의 공시 문장과 원문 근거를 연결한 분석 결과입니다.</p><div><span>원자 주장</span><strong>{data.coverage.claims_discovered}건</strong></div><div><span>규칙 판정</span><strong>{data.coverage.claims_decided}건</strong></div><p className="analyze-note">일치하는 보고서의 분석 결과를 불러옵니다.</p></aside></section>
  </main>;
}

function Landing({ data }: { data: Snapshot }) {
  return <main className="static-main">
    <section className="hero"><div className="hero-copy"><p className="eyebrow"><span className="live-dot" /> ESG EVIDENCE REVIEW</p>
      <h1>공시 문장 하나마다<br /><em>근거를 연결합니다.</em></h1>
      <p className="hero-lead">긴 지속가능성 보고서에서 환경 주장을 찾아 원문 근거, 규칙 판정, 검토 기록까지 이어 보여줍니다.</p>
      <div className="hero-actions"><Link className="primary-link" to="/analyze">보고서 분석 시작 <span>↗</span></Link><Link to="/live">문장 분석 ↗</Link><a href="#how">작동 방식 ↓</a></div>

    </div><div className="hero-visual" aria-label="NAVER 분석 결과"><div className="mock-top"><span className="mock-dots">● ● ●</span><span>PROOFOPS / REVIEW</span><span>↗</span></div><div className="mock-body"><div className="mock-label">ANALYSIS <span>01 / NAVER</span></div><h2>주장에서 근거까지,<br />한 화면에서.</h2><div className="mock-claim"><span className="tiny-label">공시 원문 · p.84</span><p>“Operation(환경운영부서)과 Internal Carbon Pricing TF(내부탄소가격제 조직) 운영”</p></div><div className="mock-result"><span className="tiny-label">근거 등급</span><strong>E3 <span>SUBSTANTIATED</span> <GuideHelp topic="grade" /><GuideHelp topic="label" /></strong><span className="mock-proof">✓ 원문 위치 확인 &nbsp; ✓ 요소 근거 연결</span></div><div className="mock-bottom">추출 → 검증 → 태깅 → 규칙 판정 <span>↗</span></div></div></div></section>
    <section className="pipeline-section" id="how"><div className="section-top"><p className="eyebrow">HOW IT WORKS</p><h2>읽기부터 보고서까지, 7단계</h2><p>모델은 추출과 태깅을 돕고, 등급과 라벨은 고정된 Python 규칙엔진이 계산합니다.</p></div><ol className="pipeline">{pipeline.map((stage, index) => <li key={stage}><span>{String(index + 1).padStart(2, "0")}</span><strong>{stage}</strong></li>)}</ol></section>
    <section className="service-flow"><div className="section-top"><p className="eyebrow">SERVICE FLOW</p><h2>서비스 흐름</h2><p>처리 과정을 살펴보고, 주장별 근거를 확인한 뒤 검토와 보고서까지 이어가세요.</p></div><div className="flow-links"><Link to="/analyze/replay"><span>01</span><strong>분석 과정</strong><small>PDF에서 판정까지</small></Link><Link to="/demo"><span>02</span><strong>결과 탐색</strong><small>주장과 근거 보기</small></Link><Link to={`/review/${data.claims.find(claim => claim.track)?.id || data.claims[0]?.id}`}><span>03</span><strong>검토</strong><small>태깅 수정 흐름</small></Link><Link to="/report/naver"><span>04</span><strong>보고서</strong><small>검토 결과 정리</small></Link><Link to="/live"><span>05</span><strong>문장 분석</strong><small>한 문장 분석</small></Link></div></section>
    <section className="landing-cta"><div><p className="eyebrow">NAVER 2025</p><h2>{data.coverage.claims_discovered}개 공시 주장,<br />근거까지 한눈에.</h2><p>문장별 판정과 검토 상태를 확인하세요.</p></div><div><div className="cta-number"><CountUp value={data.coverage.claims_decided} /><span> / {data.coverage.claims_discovered}</span></div><p>확정 판정 / 추출 주장</p><Link className="primary-link light" to="/demo">분석 결과 보기 ↗</Link></div></section>
  </main>;
}

function CountUp({ value }: { value: number }) {
  const [count, setCount] = useState(0);
  useEffect(() => {
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) { setCount(value); return; }
    const start = performance.now();
    let frame = 0;
    const tick = (now: number) => {
      setCount(Math.round(value * Math.min((now - start) / 700, 1)));
      if (now - start < 700) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value]);
  return <>{count}</>;
}

function gradeText(claim: Claim) {
  if (!claim.source_verified) return "원문 대조 필요";
  if (claim.decision.display_note) return claim.decision.display_grade ? `${claim.decision.display_grade} · 예비 등급` : "원문 대조 필요";
  if (claim.decision.grade) return claim.decision.grade;
  if (claim.decision.display_grade) return `${claim.decision.display_grade} · 예비 등급`;
  const range = claim.decision.grade_range;
  if (range) return `${range.floor}–${range.ceiling} 가능`;
  return "미판정";
}

const ladderElements: Record<string, Record<string, string[]>> = {
  management: { E1: ["M1"], E2: ["M1", "M2"], E3: ["M1", "M2", "M3"] },
  performance: { E1: ["P1"], E2: ["P1", "P2", "P3"], E3: ["P1", "P2", "P3", "P4"] },
  goal: { E1: ["G1", "G2"], E2: ["G1", "G2", "G3", "G4"], E3: ["G1", "G2", "G3", "G4", "G5", "G6"] },
};

function gradeWhy(claim: Claim) {
  const decision = claim.decision;
  const ladder = ladderElements[claim.track || ""];
  const required = decision.grade === "E0" ? [] : decision.grade && ladder?.[decision.grade];
  const confirmed = (claim.source_verified ? (ladder ? claim.elements.filter(element => (required || ladder.E3).includes(element.id)) : claim.elements.slice(0, 3)) : [])
    .filter(element => element.state === "present" && element.evidence.length > 0)
    .map(element => `${element.id} ${elementLabels[element.id] || "요소"}는 ${element.evidence.slice(0, 3).map(ref => `p.${ref.page ?? "?"} “${ref.quote.slice(0, 65)}”`).join(" · ")}에서 확인`);
  const evidence = confirmed.length ? `${confirmed.join(", ")}됐습니다. ` : claim.source_verified ? "연결된 확인 근거가 아직 없습니다. " : "원문 대조가 필요합니다. ";
  if (decision.display_note) return claim.source_verified ? `${evidence}핵심 근거 일부가 확인되기 전의 예비 판정입니다.` : "원문 대조가 필요합니다.";
  if (decision.grade) {
    const next = decision.grade === "E0" ? "E1" : decision.grade === "E1" ? "E2" : decision.grade === "E2" ? "E3" : null;
    const missing = next && ladder?.[next].filter(id => !claim.elements.some(element => element.id === id && element.state === "present" && element.evidence.length));
    return `${trackText[claim.track || ""] || "해당"} 주장입니다. ${evidence}규칙 판정은 ${decision.grade}${decision.label ? ` (${decision.label})` : ""}입니다. ${next ? `${next}로 올라가려면 ${missing?.length ? missing.map(id => `${id} ${elementLabels[id]}`).join("·") + " 근거가 더 필요합니다." : "추가 규칙 요건을 충족해야 합니다."}` : "현재 사다리의 최상위 등급입니다."}`;
  }
  if (decision.grade_range) return `${evidence}${decision.grade_range.open_elements.map(id => `${id} ${elementLabels[id] || "요소"}`).join("·")}의 상태가 미해결이어서 ${decision.grade_range.floor}~${decision.grade_range.ceiling} 가능 범위만 기록됐습니다. 확정 등급은 아닙니다.`;
  return `${evidence}${decision.status === "not_run" ? "이 주장에는 규칙 판정이 실행되지 않았으므로 등급과 라벨이 없습니다." : `${statusText[decision.status] || "미해결 상태"}로 확정 등급을 계산하지 않았습니다.`}`;
}

type Queue = "decided" | "estimated" | "unverified" | "unclassified";
function queueFor(claim: Claim): Queue {
  const bucket = claim.review_bucket;
  if (bucket === "decided" || bucket === "estimated" || bucket === "unverified" || bucket === "unclassified") return bucket;
  if (claim.decision.display_note === "원문 미검증" || !claim.source_verified) return "unverified";
  if (claim.decision.grade && !claim.decision.estimated) return "decided";
  if (claim.decision.display_note === "추정") return "estimated";
  if (claim.decision.estimated || claim.decision.display_grade) return "estimated";
  if (claim.decision.grade_range) return "estimated";
  return "unclassified";
}
const queueLabels: Record<Queue, string> = { decided: "확정", estimated: "예비 등급", unverified: "원문 대조 필요", unclassified: "미분류" };
const queues = Object.keys(queueLabels) as Queue[];
const gradeOrder: Record<string, number> = { E3: 0, E2: 1, E1: 2, E0: 3 };

function Demo({ data }: { data: Snapshot }) {
  const { claimId } = useParams();
  const { hash, search: routeSearch } = useLocation();
  const [track, setTrack] = useState("all");
  const [grade, setGrade] = useState("all");
  const [status, setStatus] = useState("all");
  const [search, setSearch] = useState("");
  const [limit, setLimit] = useState(25);
  const [queue, setQueue] = useState<Queue>(() => {
    const requested = new URLSearchParams(routeSearch).get("queue");
    return queues.includes(requested as Queue) ? requested as Queue : queueFor(data.claims.find(claim => claim.id === claimId) || data.claims.find(claim => claim.decision.grade) || data.claims[0]);
  });
  const selected = data.claims.find(claim => claim.id === claimId && queueFor(claim) === queue) || null;
  useEffect(() => { if (hash === "#claims") document.getElementById("claims")?.scrollIntoView(); }, [hash, claimId]);
  const filtered = useMemo(() => data.claims.filter(claim => {
    if (queueFor(claim) !== queue) return false;
    if (track !== "all" && (claim.track || "unknown") !== track) return false;
    if (grade.startsWith("E") && (claim.decision.grade || claim.decision.display_grade) !== grade) return false;
    if (grade === "range" && !claim.decision.grade_range && !claim.decision.display_note) return false;
    if (grade === "none" && (claim.decision.grade || claim.decision.display_grade || claim.decision.grade_range)) return false;
    if (status !== "all" && claim.decision.status !== status) return false;
    return !search || `${claim.quote} ${claim.id} ${claim.page ?? ""}`.toLowerCase().includes(search.toLowerCase());
  }).sort((a, b) => (gradeOrder[a.decision.grade || a.decision.display_grade || a.decision.grade_range?.floor || ""] ?? 4) - (gradeOrder[b.decision.grade || b.decision.display_grade || b.decision.grade_range?.floor || ""] ?? 4) || (a.page ?? 9999) - (b.page ?? 9999)), [data.claims, queue, track, grade, status, search]);
  const counts = { decided: data.claims.filter(c => queueFor(c) === "decided").length, range: data.claims.filter(c => queueFor(c) === "estimated").length, pending: data.claims.filter(c => queueFor(c) === "unverified" || queueFor(c) === "unclassified").length };
  const queueCounts = Object.fromEntries(queues.map(name => [name, data.claims.filter(claim => queueFor(claim) === name).length])) as Record<Queue, number>;
  return <main className="static-main demo-main"><div className="breadcrumb"><Link to="/">홈</Link><span>/</span> 분석 결과</div><CompanyTabs selected="naver" />
    <section className="demo-heading"><div><p className="eyebrow">ANALYSIS · 2025</p><h1>NAVER 분석 결과</h1><p>실제 보고서에서 추출한 주장과 원문 근거, 규칙 판정을 탐색할 수 있습니다.</p><div className="badges"><span className="badge amber">분석 범위: {data.coverage.pages_processed}/{data.coverage.pages_total}쪽</span><span className="badge blue">{confirmationText}</span></div><Link className="report-link" to="/report/naver">검토 보고서 보기 ↗</Link></div><div className="heading-side"><span>REVIEW STATUS</span><strong>검토 기록 27건 <i /></strong><small>검토일 {new Date(data.generated_at).toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" })}</small></div></section>
    <section className="metrics" aria-label="분석 요약"><div><span>추출 주장</span><strong><CountUp value={data.coverage.claims_discovered} /><small>건</small></strong><p>공시 문장</p></div><div><span>원문 검증</span><strong><CountUp value={data.funnel[1].count} /><small>건</small></strong><p>원문 대조 완료</p></div><div><span>확정 판정</span><strong><CountUp value={counts.decided} /><small>건</small></strong><p>규칙 판정</p></div><div><span>예비 등급</span><strong><CountUp value={counts.range} /><small>건</small></strong><p>근거 확인 필요</p></div></section>
    <section className="overview-grid"><div className="surface funnel-card"><div className="card-heading"><div><p className="eyebrow">PROCESS FUNNEL</p><h2>처리 흐름</h2></div><span>추출 → 태깅 → 판정</span></div><div className="funnel-list">{data.funnel.map((step, index) => <div className="funnel-row" key={step.label}><span className="step-name"><b>{String(index + 1).padStart(2, "0")}</b>{["추출 주장", "원문 검증", "예비 분류", "관계 분석", "요소 분석", "등급 표시"][index] || step.label}</span><div className="bar-track"><div style={{ width: `${Math.max(step.count / data.coverage.claims_discovered * 100, 3)}%` }} /></div><strong>{step.count}</strong></div>)}</div></div>
      <div className="surface grade-card"><p className="eyebrow">DECISION DISTRIBUTION</p><h2>등급과 보류 <GuideHelp topic="grade" /><GuideHelp topic="range" /></h2><div className="grade-bars"><div><span><b>확정</b> 규칙 판정</span><strong>{counts.decided}</strong></div><div className="grade-line e3"><i style={{ width: `${counts.decided / data.claims.length * 100}%` }} /></div><div><span><b>예비 등급</b> <GuideHelp topic="estimated" /></span><strong>{counts.range}</strong></div><div className="grade-line range"><i style={{ width: `${counts.range / data.claims.length * 100}%` }} /></div><div><span><b>나머지</b> 확인 대기</span><strong>{counts.pending}</strong></div><div className="grade-line pending"><i style={{ width: `${counts.pending / data.claims.length * 100}%` }} /></div></div><p className="caption">전체 {data.claims.length}건 중 확정 판정과 예비 등급을 구분해 표시합니다.</p></div></section>
    <section className="notice"><a href="https://www.navercorp.com/esg/esgReports" target="_blank" rel="noopener noreferrer">NAVER 원문 보고서 ↗</a></section>
    <section className="claims-section" id="claims"><div className="card-heading"><div><p className="eyebrow">CLAIM REVIEW</p><h2>문장별 결과</h2><p>상태를 선택하고 문장을 열어 원문과 요소별 근거를 확인하세요.</p></div><span>{filtered.length} / {data.claims.length}건</span></div>
      <div className="queue-tabs" role="tablist" aria-label="검토 상태">{queues.map(name => <button type="button" role="tab" aria-selected={queue === name} key={name} onClick={() => { setQueue(name); setGrade("all"); setStatus("all"); setLimit(25); }}>{queueLabels[name]} <span>{queueCounts[name]}</span></button>)}</div>
      <div className="filters"><label>검색<input value={search} onChange={event => { setSearch(event.target.value); setLimit(25); }} placeholder="문장, 페이지 검색" /></label><label>트랙 <GuideHelp topic="track" /><select value={track} onChange={event => { setTrack(event.target.value); setLimit(25); }}><option value="all">전체 트랙</option><option value="management">관리체계</option><option value="goal">목표</option><option value="performance">성과</option><option value="unknown">분류 미합의</option></select></label><label>등급 <GuideHelp topic="grade" /><select value={grade} onChange={event => { setGrade(event.target.value); setLimit(25); }}><option value="all">전체 등급</option>{["E3", "E2", "E1", "E0"].map(value => <option key={value} value={value}>{value}</option>)}<option value="range">추정</option><option value="none">미판정</option></select></label><label>상태 <GuideHelp topic="state" /><select value={status} onChange={event => { setStatus(event.target.value); setLimit(25); }}><option value="all">전체 상태</option><option value="decided">규칙 판정</option><option value="blocked_evidence">근거 보류</option><option value="not_run">미판정</option></select></label></div>
      <div className="claims-layout"><div className="claim-list" aria-label="주장 목록">{filtered.slice(0, limit).map(claim => <Link className={`claim-item ${selected?.id === claim.id ? "selected" : ""}`} to={`/demo/${claim.id}?queue=${queue}#claims`} key={claim.id}><div className="claim-item-top"><span>p.{claim.page ?? "?"} · {claim.track ? trackText[claim.track] : <span title="트랙 태깅 합의 전">분류 미합의 ⓘ</span>}</span><span className={`mini-status ${queueFor(claim) === "decided" ? "good" : queueFor(claim) === "estimated" || queueFor(claim) === "unverified" ? "warn" : "muted"}`}>{gradeText(claim)}</span></div><p>{claim.quote}</p><small>{(!claim.source_verified ? "원문 대조 필요" : claim.decision.estimated ? "예비 등급" : statusText[claim.decision.status] || claim.decision.status) }{claim.review.audit === "uncertain" ? " · 사용자 확인 완료" : ""}</small></Link>)}{filtered.length === 0 ? <p className="empty">조건에 맞는 주장이 없습니다.</p> : null}{filtered.length > limit ? <button className="more-button" onClick={() => setLimit(limit + 25)}>더 보기 ({filtered.length - limit}건 남음)</button> : null}</div><ClaimDetail key={selected?.id || "empty"} claim={selected} queue={queue} /></div>
    </section>
    <DecisionGuide />
  </main>;
}

function ClaimDetail({ claim, queue }: { claim: Claim | null; queue: Queue }) {
  const [reviewMode, setReviewMode] = useState(false);
  if (!claim) return <aside className="claim-detail"><div className="detail-empty"><span>↖</span><h3>주장을 선택하세요</h3><p>왼쪽 목록에서 문장을 선택하면 근거와 판정 경로를 볼 수 있습니다.</p></div></aside>;
  const d = claim.decision;
  return <aside className="claim-detail"><div className="detail-top"><div><p className="eyebrow">CLAIM DETAIL · p.{claim.page ?? "?"}</p><h3>주장과 판정 근거</h3></div><Link to={`/demo?queue=${queue}#claims`} aria-label="상세 닫기">×</Link></div><div className="source-quote"><span>{claim.source_verified ? "보고서 원문" : "추출 문장"} · p.{claim.page ?? "?"}</span><blockquote>“{claim.quote}”</blockquote><small>{claim.source_verified ? "원문 인용 검증 기록 있음" : "원문 대조 필요"}</small></div>
    <div className="detail-actions"><button type="button" aria-pressed={reviewMode} onClick={() => setReviewMode(!reviewMode)}>검토 모드 {reviewMode ? "닫기" : "열기"}</button><Link to={`/report/naver`}>보고서 ↗</Link></div>
    {reviewMode && <ReviewSimulator claim={claim} />}
    <div className="why-panel"><h4>왜 이 등급인가 <GuideHelp topic="grade" /></h4><p>{gradeWhy(claim)}</p></div>
    <div className="decision-panel"><span>{!claim.source_verified ? "원문 대조 필요" : d.display_note ? "예비 등급" : claim.review.status === "user_confirmed" ? "검토 완료" : "분석 결과"}</span><strong>{gradeText(claim)} <GuideHelp topic={d.estimated ? "estimated" : d.grade_range ? "range" : "grade"} /></strong><p>{d.label || d.display_label || statusText[d.status] || d.status} <GuideHelp topic="label" /></p></div>
    <div className="detail-block"><h4>요소별 근거 <GuideHelp topic="state" /></h4>{claim.elements.length ? <div className="element-table-wrap"><table className="element-table"><thead><tr><th>요소</th><th>상태</th><th>근거 문구</th></tr></thead><tbody>{claim.elements.map(element => <tr key={element.id}><th scope="row"><b>{element.id}</b><span>{elementLabels[element.id] || element.id}</span></th><td><span className={`element-state ${element.state}`}>{!claim.source_verified && element.state === "present" ? "원문 대조 필요" : stateText[element.state] || element.state}</span></td><td>{element.evidence.length ? element.evidence.map((ref, index) => <p key={index}><small>p.{ref.page ?? "?"}</small> “{ref.quote}”</p>) : <span className="no-evidence">연결된 근거 문구 없음</span>}</td></tr>)}</tbody></table></div> : <p className="caption">요소 태깅 전입니다. 빈 요소를 absent로 보지 않습니다.</p>}</div>
    <div className="provenance"><span className="badge blue">{claim.review.status === "user_confirmed" ? "검토 완료" : claim.decision.estimated ? "예비 등급" : "분석 결과"}</span></div>
  </aside>;
}
