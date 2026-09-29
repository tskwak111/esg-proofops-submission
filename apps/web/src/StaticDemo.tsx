import { useEffect, useMemo, useState } from "react";
import { Link, Route, Routes, useLocation, useNavigate, useParams } from "react-router";
import { LiveClaim } from "./features/live/LiveClaim";
import { LiveReport } from "./features/live/LiveReport";
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
    document.documentElement.classList.add("motion");
    const observer = new IntersectionObserver(entries => entries.forEach(entry => {
      if (entry.isIntersecting) { entry.target.classList.add("in-view"); observer.unobserve(entry.target); }
    }), { threshold: .1 });
    const sections = document.querySelectorAll(".demo-site main > section");
    sections.forEach(section => observer.observe(section));
    return () => observer.disconnect();
  }, [location.pathname, data]);
  return <div className="demo-site">
    <header className="site-header"><div className="site-header-inner">
      <Link className="brand" to="/" aria-label="ProofOps 홈"><span className="brand-dot" aria-hidden="true" />ProofOps</Link>
      <button className="menu-toggle" type="button" aria-expanded={menuOpen} aria-controls="site-menu" onClick={() => setMenuOpen(!menuOpen)}>{menuOpen ? "닫기" : "메뉴"}<span aria-hidden="true">{menuOpen ? "×" : "☰"}</span></button>
      <nav id="site-menu" className={menuOpen ? "open" : ""} aria-label="주요 메뉴" onClick={() => setMenuOpen(false)}><Link to="/">서비스</Link><Link to="/analyze">분석</Link><Link to="/demo">분석 사례</Link><Link to="/report/naver">보고서 예시</Link><Link to="/live">문장 분석</Link><Link to="/guide">서비스 가이드라인</Link></nav>
      <div className="header-actions"><Link className="pill pill-line" to="/demo">분석 사례</Link><Link className="pill pill-dark" to="/analyze">분석 시작</Link></div>
    </div></header>
    {isLive ? <LiveClaim /> : error ? <main className="static-main"><section className="surface"><h1>분석 결과를 불러오지 못했습니다</h1><p>잠시 후 새로고침해 주세요.</p></section></main> :
      !data ? <main className="static-main loading-main" role="status" aria-label="검토 결과 불러오는 중"><div className="skeleton skeleton-title" /><div className="skeleton skeleton-card" /><div className="skeleton skeleton-card" /></main> :
      <Routes><Route path="/" element={<Landing data={data} />} /><Route path="/analyze" element={<Analyze data={data} />} /><Route path="/guide" element={<Guide />} /><Route path={replayPath} element={<ReplayPage snapshot={data} />} /><Route path="/demo" element={<Demo data={data} />} /><Route path="/demo/:claimId" element={<Demo data={data} />} /><Route path={kiaRoute.path} element={<><div className="kia-route-toolbar"><CompanyTabs selected="kia" /><Link to="/report/kia">기아 검토 보고서 ↗</Link></div><kiaRoute.Component /></>} /><Route path={reviewRoutePath} element={<ReviewPage claims={data.claims} />} /><Route path={auditReportRoute.path} element={<AuditReportPage />} /><Route path="*" element={<NotFound />} /></Routes>}
    <footer className="site-footer"><div className="wrap footer-inner"><div><strong>ProofOps</strong><p>본 서비스는 공시 발간 전 근거 점검을 돕는 도구이며, 제3자 보증, 기업 성과 진위, 법률·회계 판단을 대신하지 않습니다.</p></div><nav aria-label="바닥글"><Link to="/analyze">분석</Link><Link to="/demo">분석 사례</Link><Link to="/report/naver">보고서 예시</Link><Link to="/live">문장 분석</Link><Link to="/guide">서비스 가이드라인</Link><a href="https://github.com/tskwak111/esg-proofops-submission" target="_blank" rel="noopener noreferrer">GitHub</a></nav><span>© 2026 ProofOps</span></div></footer>
  </div>;
}

function NotFound() { return <main className="static-main not-found"><p className="eyebrow">404 / PAGE NOT FOUND</p><h1>페이지를 찾을 수 없습니다.</h1><p>주소를 확인하거나 검토 결과로 돌아가세요.</p><Link className="primary-link" to="/demo">결과 보기 ↗</Link></main>; }

function CompanyTabs({ selected }: { selected: "naver" | "kia" }) { return <nav className="company-tabs" aria-label="기업 선택"><Link to="/demo" aria-current={selected === "naver" ? "page" : undefined}>NAVER</Link><Link to="/demo/kia" aria-current={selected === "kia" ? "page" : undefined}>기아</Link></nav>; }


function Analyze({ data }: { data: Snapshot }) {
  const [busy, setBusy] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<{ name: string; matched: boolean; message: string } | null>(null);
  async function inspect(file?: File) {
    if (!file) return;
    setBusy(true); setResult(null); setFile(null);
    try {
      if (!file.name.toLowerCase().endsWith(".pdf") || new TextDecoder().decode(await file.slice(0, 4).arrayBuffer()) !== "%PDF") throw new Error("PDF 파일을 선택해 주세요.");
      setFile(file);
      if (!window.crypto?.subtle) throw new Error("이 브라우저에서는 파일 지문을 계산할 수 없습니다. HTTPS 또는 localhost에서 다시 열어 주세요.");
      const hash = Array.from(new Uint8Array(await window.crypto.subtle.digest("SHA-256", await file.arrayBuffer()))).map(byte => byte.toString(16).padStart(2, "0")).join("");
      const match = data.processed_reports.find(report => report.sha256 === hash);
      setResult({ name: file.name, matched: !!match, message: match ? "기존 분석 결과가 있습니다." : "새 보고서를 선택했습니다." });
    } catch (error) {
      setResult({ name: file.name, matched: false, message: error instanceof Error ? error.message : "파일을 읽지 못했습니다." });
    } finally { setBusy(false); }
  }

  return <main className="static-main analyze-main"><div className="breadcrumb"><Link to="/">홈</Link><span>/</span> 보고서 분석</div>
    <section className="analyze-heading"><p className="eyebrow">REPORT ANALYSIS</p><h1>보고서 분석 시작</h1><p>지속가능경영보고서 PDF를 올리면 분석할 쪽을 골라 바로 분석합니다. 이미 분석한 보고서는 저장된 결과도 함께 볼 수 있습니다.</p></section>
    <section className="analyze-grid"><div className="surface analyze-upload"><h2>PDF 보고서 선택</h2><p>파일을 놓거나 눌러 선택하세요.</p><label className="drop-zone" onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); void inspect(event.dataTransfer.files[0]); }}><span aria-hidden="true">↥</span><strong>{busy ? "파일 지문 계산 중…" : "PDF 파일을 여기에 놓기"}</strong><small>또는 클릭해 파일 선택 · PDF는 이 브라우저 안에서만 읽습니다</small><input type="file" accept=".pdf,application/pdf" aria-label="분석할 PDF 선택" disabled={busy} onChange={event => { void inspect(event.target.files?.[0]); event.target.value = ""; }} /></label>
      {result && <div className={`analyze-result ${result.matched ? "match" : "no-match"}`} role="status"><small>{result.name}</small><h3>{result.message}</h3>{result.matched ? <><Link to="/analyze/replay">분석 과정 보기 ↗</Link><small>아래에서 실시간 분석도 선택할 수 있습니다.</small></> : <p>아래에서 분석할 쪽을 선택해 주세요.</p>}</div>}
    </div><aside className="surface analyze-info"><p className="eyebrow">ANALYSIS</p><h2>모든 기업 보고서 분석</h2><p>글자를 선택할 수 있는 지속가능경영보고서 PDF라면 기업과 관계없이 분석합니다.</p><div><span>쪽 선택</span><strong style={{ fontFamily: "inherit", fontSize: 15 }}>목차 기반 자동 · 수정 가능</strong></div><div><span>기본 범위</span><strong style={{ fontFamily: "inherit", fontSize: 15 }}>환경(E) + 부록</strong></div><div><span>최대 분량</span><strong style={{ fontFamily: "inherit", fontSize: 15 }}>60쪽 · 10쪽씩 순차 처리</strong></div><p className="analyze-note">결과에는 주장별 인용·쪽수·원문 대조 여부·등급 또는 보류 사유가 표시됩니다. 실시간 분석에는 접근 코드가 필요합니다. 분석 사례: <Link to="/demo">NAVER</Link> · <Link to="/demo/kia">기아</Link></p></aside></section>
    {file && <LiveReport key={file.name + file.lastModified} file={file} />}
  </main>;
}

const steps: [string, string][] = [
  ["보고서 업로드", "지속가능경영보고서 PDF를 올리면 쪽 단위로 본문·표·레이아웃을 읽습니다."],
  ["주장 추출 · 원문 대조", "환경 관련 문장을 원자 주장으로 나누고, 인용과 쪽 위치를 원문과 맞춥니다."],
  ["규칙엔진 판정", "모델이 태깅한 입증 요소를 Python 규칙엔진이 E0–E3 등급으로 계산합니다."],
  ["검토 · 보고서", "검토자가 태깅을 확인하면 판정이 다시 계산되고, 감사 보고서로 내보냅니다."],
];
const features: [string, string][] = [
  ["주장 자동 추출", "수백 쪽 보고서에서 환경 주장을 문장 단위로 찾아 관리체계·성과·목표로 분류합니다."],
  ["원문 근거 대조", "모든 근거는 물리 쪽수와 인용으로 남고, PDF 원문과 일치한 인용만 인정합니다."],
  ["규칙 기반 판정", "등급과 라벨은 버전이 고정된 규칙엔진이 계산해 언제든 같은 결과를 재현합니다."],
  ["판정 가능 범위", "확인되지 않은 요소는 ‘근거 없음’으로 처리하지 않고 가능한 등급 범위로 보여 줍니다."],
  ["검토 이력 보존", "검토자는 태깅만 수정하고, 이전 판정과 변경 이력은 덮어쓰지 않고 남습니다."],
  ["감사 보고서", "판정 분포와 주장별 근거를 A4 보고서, JSON, CSV로 바로 내보냅니다."],
];

function EvidenceTrace() {
  const rows: [string, string, string][] = [
    ["M1 · 이행 주체", "p.84", "Operation(환경운영부서)과 내부탄소가격제 TF 운영"],
    ["M2 · 적용 범위", "p.2", "네이버 주식회사 개별 기업을 기준으로 작성"],
    ["M3 · 외부 검증", "p.230 · 242", "GRI 3-3 중대 토픽 관리 · AA1000AS v3"],
  ];
  return <figure className="trace" aria-label="판정 경로 예시">
    <div className="trace-head"><span className="dot" />NAVER 2025 · p.84 · 관리체계</div>
    <blockquote>“Operation(환경운영부서)과 Internal Carbon Pricing TF 운영”</blockquote>
    <ol className="trace-rows">{rows.map(([name, page, quote], index) => <li key={name} style={{ animationDelay: `${300 + index * 350}ms` }}><span className="check">✓</span><div><strong>{name}</strong><p>{quote}</p></div><span className="trace-page">{page}</span></li>)}</ol>
    <div className="trace-result"><span>규칙엔진 판정</span><strong>E3</strong><em>SUBSTANTIATED</em></div>
  </figure>;
}

function FunnelPreview({ data }: { data: Snapshot }) {
  const max = data.coverage.claims_discovered || 1;
  const rows: [string, number][] = [["추출 주장", data.coverage.claims_discovered], ["원문 대조", data.funnel[1]?.count ?? 0], ["등급 표시", data.claims.length], ["확정 판정", data.coverage.claims_decided]];
  return <figure className="mini-card"><div className="mini-head"><span>처리 과정</span><span>NAVER 2025</span></div>{rows.map(([label, value], index) => <div className="mini-bar" key={label}><div><span>{label}</span><strong>{value}</strong></div><i><b style={{ width: `${Math.max(value / max * 100, 4)}%`, animationDelay: `${index * 120}ms` }} /></i></div>)}</figure>;
}

function ReportPreview({ data }: { data: Snapshot }) {
  const count = (grade: string) => data.claims.filter(claim => (claim.decision.grade || claim.decision.display_grade) === grade).length;
  const grades = ["E3", "E2", "E1", "E0"];
  const total = data.claims.length || 1;
  return <figure className="mini-card report-mini"><div className="mini-head"><span>감사 보고서</span><span>PDF · JSON · CSV</span></div><h4>NAVER 공시 근거 검토 보고서</h4><div className="stack">{grades.map(grade => <i key={grade} className={grade.toLowerCase()} style={{ width: `${count(grade) / total * 100}%` }} />)}</div><ul>{grades.map(grade => <li key={grade}><span className={`sw ${grade.toLowerCase()}`} />{grade}<strong>{count(grade)}</strong></li>)}</ul></figure>;
}

function Landing({ data }: { data: Snapshot }) {
  const navigate = useNavigate();
  const [draft, setDraft] = useState("");
  const verified = data.funnel[1]?.count ?? 0;
  const reviewTarget = data.claims.find(claim => claim.decision.grade === "E3")?.id || data.claims[0]?.id;
  const example = "업로드 한 보고서 및 재무제표를 기반으로, 그린워싱으로 판별된 리스크가 높은 문장과 그 원인을 분석해줘.";
  const tools: [string, string, string, string][] = [
    ["/analyze", "보고서 분석", "어떤 기업 보고서든 선택한 쪽을 바로 분석합니다", "t-upload"],
    ["/demo", "분석 사례 · NAVER", "NAVER 2025 보고서의 주장·근거·판정", "t-results"],
    [`/review/${reviewTarget}`, "검토", "요소를 바꾸면 판정이 다시 계산됩니다", "t-review"],
    ["/report/naver", "감사 보고서", "판정 분포와 주장별 근거", "t-report"],
    ["/live", "문장 분석", "한 문장을 바로 분석합니다", "t-live"],
    ["/demo/kia", "분석 사례 · 기아", "수치 검산과 검증의견서 연결", "t-kia"],
  ];
  return <main className="landing">
    <section className="hero">
      <p className="eyebrow-c">ESG · 지속가능경영보고서 공시 검증</p>
      <h1>ProofOps</h1>
      <p className="hero-sub">기업 보고서 내 환경 주장의 근거를 공시 안에서 찾으며, 규칙엔진을 기반으로 근거 수준을 판정합니다. 근거에 대한 쪽수·원문 인용 또는 확인 범위가 함께 표시됩니다.</p>
      <Link className="pill pill-dark" to="/analyze">보고서 분석 시작 <span aria-hidden="true">→</span></Link>
      <form className="prompt" onSubmit={event => { event.preventDefault(); navigate(draft.trim() ? `/live?claim=${encodeURIComponent(draft.trim().slice(0, 500))}` : "/analyze"); }}>
        <textarea aria-label="분석할 환경 주장" value={draft} onChange={event => setDraft(event.target.value)} placeholder={example} rows={3} />
        <div className="prompt-bar"><div className="chips"><Link to="/analyze" className="chip"><span className="ic">⬆</span>PDF 업로드</Link><Link to="/demo" className="chip"><span className="ic">◎</span>분석 사례</Link><Link to="/report/naver" className="chip"><span className="ic">▤</span>보고서</Link><Link to="/demo/kia" className="chip"><span className="ic">◇</span>기아 사례</Link></div><button type="submit" className="send" aria-label="문장 분석">↑</button></div>
      </form>
      <p className="hero-note">문장을 입력하면 분류 → 요소 태깅 → 규칙엔진 판정을 바로 실행합니다</p>
    </section>

    <section className="sec sec-beige" id="how">
      <p className="eyebrow-c center">작동 방식</p><h2 className="serif center">단 몇 분 만에 핵심 근거 확인</h2>
      <div className="steps">{steps.map(([title, body], index) => <article className="card" key={title}><span className="num">{index + 1}</span><h3>{title}</h3><p>{body}</p></article>)}</div>
      <div className="proof"><p className="proof-kicker">분석 사례 · NAVER 2025 통합보고서</p><h3 className="serif center">공개된 실제 보고서로 전체 흐름을 검증했습니다.</h3>
        <dl><div><dt>분석 쪽수</dt><dd><CountUp value={data.coverage.pages_total} /></dd></div><div><dt>추출 주장</dt><dd><CountUp value={data.coverage.claims_discovered} /></dd></div><div><dt>원문 대조</dt><dd><CountUp value={verified} /></dd></div><div><dt>확정 판정</dt><dd><CountUp value={data.coverage.claims_decided} /></dd></div></dl></div>
    </section>

    <section className="sec sec-cream">
      <p className="eyebrow-c center">기능</p><h2 className="serif center">검증에 필요한 모든 기능</h2>
      <div className="features">{features.map(([title, body]) => <article className="card" key={title}><h3>{title}</h3><p>{body}</p></article>)}</div>
    </section>

    <section className="sec sec-beige">
      <p className="eyebrow-c">활용 사례</p><h2 className="serif">실무를 위한 설계</h2>
      <div className="uses">
        <div className="use"><div className="use-copy"><h3>문장마다 근거 경로를 추적</h3><p>주장 하나에 필요한 입증 요소를 원문 쪽수와 인용으로 연결합니다. 이행 주체, 적용 범위, 외부 검증이 모두 확인되면 규칙엔진이 E3로 판정합니다.</p><Link className="more" to="/demo">분석 결과 보기 →</Link></div><EvidenceTrace /></div>
        <div className="use reverse"><div className="use-copy"><h3>보고서 한 권의 처리 과정을 한눈에</h3><p>파싱부터 판정까지 단계별 처리량과 시간, 모델 호출 수를 기록합니다. 어디서 막혔는지, 무엇이 확인되지 않았는지 숨기지 않습니다.</p><Link className="more" to="/analyze/replay">처리 과정 보기 →</Link></div><FunnelPreview data={data} /></div>
        <div className="use"><div className="use-copy"><h3>감사 보고서로 바로 공유</h3><p>판정 분포, 핵심 발견, 주장별 근거를 담은 보고서를 A4 PDF와 JSON·CSV로 내보내 검토 조직과 공유합니다.</p><Link className="more" to="/report/naver">감사 보고서 보기 →</Link></div><ReportPreview data={data} /></div>
      </div>
    </section>

    <section className="sec sec-cream">
      <p className="eyebrow-c center">더 살펴보기</p><h2 className="serif center">ProofOps 서비스 살펴보기</h2>
      <div className="tools">{tools.map(([to, title, body, thumb]) => <Link className="card tool" to={to} key={title}><div className={`thumb ${thumb}`}><i /><i /><i /><i /></div><h3>{title}</h3><p>{body}</p></Link>)}</div>
    </section>

    <section className="sec sec-cream faq">
      <div className="faq-inner">
        <h2 className="serif">ProofOps는 무엇인가요?</h2>
        <p>ProofOps는 기업의 지속가능경영보고서 내 환경 주장의 근거가 공시 안에 있는지 확인하는 점검 도구입니다. 언어모델은 문장 추출 및 근거 요소 표시를 담당하며, 판정 등급 및 라벨은 사전에 정한 규칙을 기반으로 합니다.</p>
        <h2 className="serif">누구에게 필요한가요?</h2>
        <ul><li><strong>기업 지속가능경영·ESG 부서 및 공시 담당 부서</strong> — 발간 전에 근거가 빠진 주장을 찾아 보완합니다.</li><li><strong>검증기관·회계법인</strong> — 주장별 근거 경로와 판정 이력을 한 곳에서 검토합니다.</li><li><strong>공급망 담당·협력사</strong> — 공개된 보고서의 주장이 어느 쪽의 어떤 근거에 기반하는지 확인합니다.</li></ul>
      </div>
    </section>
  </main>;
}

function Guide() {
  const tracks: [string, string][] = [
    ["목표형", "목표연도·수치, 기준값·적용범위, 진척·이행수단을 확인합니다."],
    ["성과형", "수치·단위, 비교기준·산정방법·경계, 외부 검증 연결을 확인합니다."],
    ["관리체계형", "구체적 수단, 적용범위, 외부 검증을 확인합니다."],
  ];
  const grades: [string, string, string][] = [
    ["E3", "SUBSTANTIATED", "해당 유형의 입증 요소가 공시 안에서 모두 확인됨"],
    ["E2 · E1", "INCOMPLETE", "일부 입증 요소만 확인됨"],
    ["E0", "UNSUBSTANTIATED", "핵심 입증 요소가 없음을 확인함"],
    ["범위 · 보류", "등급 범위 / 보류", "확인되지 않은 요소가 있으면 가능한 등급 범위나 보류 사유를 표시"],
  ];
  return <main className="landing guide-main">
    <section className="hero">
      <p className="eyebrow-c">서비스 가이드라인</p>
      <h1>ProofOps 작동 방식과 기능</h1>
      <p className="hero-sub">본 서비스는 공시 발간 전 근거 점검을 돕는 도구이며, 제3자 보증, 기업 성과 진위, 법률·회계 판단을 대신하지 않습니다.</p>
    </section>
    <section className="sec sec-beige">
      <p className="eyebrow-c center">작동 방식</p><h2 className="serif center">보고서에서 판정까지 네 단계</h2>
      <div className="steps">{steps.map(([title, body], index) => <article className="card" key={title}><span className="num">{index + 1}</span><h3>{title}</h3><p>{body}</p></article>)}</div>
    </section>
    <section className="sec sec-cream">
      <p className="eyebrow-c center">서비스 기능</p><h2 className="serif center">주요 기능</h2>
      <div className="features">{features.map(([title, body]) => <article className="card" key={title}><h3>{title}</h3><p>{body}</p></article>)}</div>
    </section>
    <section className="sec sec-beige">
      <p className="eyebrow-c center">판정 기준</p><h2 className="serif center">주장 유형과 근거 수준</h2>
      <div className="features">{tracks.map(([title, body]) => <article className="card" key={title}><h3>{title}</h3><p>{body}</p></article>)}</div>
      <div className="features">{grades.map(([grade, label, body]) => <article className="card" key={grade}><h3>{grade}</h3><p><strong>{label}</strong></p><p>{body}</p></article>)}</div>
    </section>
    <section className="sec sec-cream faq">
      <div className="faq-inner">
        <h2 className="serif">이용 전에 확인해 주세요</h2>
        <ul>
          <li>언어모델은 문장 추출 및 근거 요소 표시를 담당하고, 판정 등급과 라벨은 사전에 정한 규칙으로 계산합니다.</li>
          <li>근거가 원문과 일치하는지 확인되지 않은 주장은 “원문 대조 필요”로 표시하며 확정 등급을 주지 않습니다.</li>
          <li>확인하지 못한 근거를 “근거 없음”으로 처리하지 않습니다.</li>
          <li>결과는 보고서 안의 주장과 근거의 연결을 보여 줄 뿐, 기업의 실제 환경 성과나 법 위반 여부를 판단하지 않습니다.</li>
        </ul>
        <p><Link className="pill pill-dark" to="/analyze">보고서 분석 시작 <span aria-hidden="true">→</span></Link></p>
      </div>
    </section>
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
  const explicit = data.claims.find(claim => claim.id === claimId && queueFor(claim) === queue) || null;
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
  const selected = explicit || (typeof window !== "undefined" && window.innerWidth > 1024 ? filtered[0] || null : null);
  const counts = { decided: data.claims.filter(c => queueFor(c) === "decided").length, range: data.claims.filter(c => queueFor(c) === "estimated").length, pending: data.claims.filter(c => queueFor(c) === "unverified" || queueFor(c) === "unclassified").length };
  const queueCounts = Object.fromEntries(queues.map(name => [name, data.claims.filter(claim => queueFor(claim) === name).length])) as Record<Queue, number>;
  return <main className="static-main demo-main"><div className="breadcrumb"><Link to="/">홈</Link><span>/</span> 분석 결과</div><CompanyTabs selected="naver" />
    <section className="demo-heading"><div><p className="eyebrow">ANALYSIS · 2025</p><h1>분석 사례 · NAVER 2025</h1><p>실제 보고서에서 추출한 주장과 원문 근거, 규칙 판정을 탐색할 수 있습니다.</p><div className="badges"><span className="badge amber">분석 범위: {data.coverage.pages_processed}/{data.coverage.pages_total}쪽</span><span className="badge blue">{confirmationText}</span></div><Link className="report-link" to="/report/naver">검토 보고서 보기 ↗</Link></div><div className="heading-side"><span>REVIEW STATUS</span><strong>검토 기록 27건 <i /></strong><small>검토일 {new Date(data.generated_at).toLocaleDateString("ko-KR", { timeZone: "Asia/Seoul" })}</small></div></section>
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
