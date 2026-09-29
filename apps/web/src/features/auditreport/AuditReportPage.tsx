import { useEffect, useState } from "react";
import { Link, useParams } from "react-router";
import "./audit-report.css";

type Evidence = { page: number | null; quote: string };
type Claim = {
  id: string;
  page: number | null;
  track: string | null;
  quote: string;
  statement?: string;
  source_verified: boolean;
  elements: { id: string; state: string; evidence: Evidence[] }[];
  decision: {
    grade: string | null;
    display_grade?: string;
    estimated?: boolean;
    label: string | null;
    grade_range: { floor: string; ceiling: string } | null;
    status: string;
  };
};
type Snapshot = {
  title: string;
  generated_at: string;
  partial?: boolean;
  demo_mode?: boolean;
  relaxed_rules?: boolean;
  processed_reports?: { title: string; scope?: string }[];
  coverage: {
    pages_processed: number;
    pages_total: number;
    pages_unprocessed?: number;
    pages_unreadable?: number;
    claims_discovered: number;
    claims_decided: number;
    claims_needs_review: number;
  };
  funnel?: { label: string; count: number }[];
  run: {
    executed_at?: string;
    completed_at?: string;
    model_ids?: string[];
    model_note?: string;
    rule_pack_name?: string;
    rule_pack_id?: string;
    rule_pack_hash?: string;
    demo_mode?: boolean;
    relaxed_rules?: boolean;
  };
  claims: Claim[];
};
type ReportRow = {
  id: string;
  page: number | null;
  quote: string;
  track: string;
  grade: string;
  label: string;
  evidencePages: number[];
  estimated: boolean;
};

const companies = { naver: "N사", kia: "KIA" } as const;
const tracks: Record<string, string> = { management: "관리체계", performance: "성과", goal: "목표" };
const grades = ["E3", "E2", "E1", "E0"] as const;
const gradeMeaning: Record<string, string> = {
  E3: "핵심 요소와 추가 입증 요소가 연결된 단계",
  E2: "주장 유형별 핵심 근거가 연결된 단계",
  E1: "기본적인 주장 요소가 확인된 단계",
  E0: "입증의 출발 요소가 확인되지 않은 단계",
};

function shortQuote(value: string) {
  const chars = Array.from(value.replace(/\s+/g, " ").trim());
  return chars.length > 200 ? `${chars.slice(0, 199).join("")}…` : chars.join("");
}

export function reportRow(claim: Claim, demo: boolean): ReportRow {
  const pages = claim.source_verified
    ? [...new Set((claim.elements || []).filter(item => item.state === "present")
      .flatMap(item => item.evidence || []).map(item => item.page)
      .filter((page): page is number => typeof page === "number" && Number.isInteger(page) && page > 0))].sort((a, b) => a - b)
    : [];
  const range = claim.decision.grade_range;
  const estimated = demo && !claim.decision.grade && !!claim.decision.display_grade;
  return {
    id: claim.id,
    page: claim.page,
    quote: shortQuote(claim.statement || claim.quote || ""),
    track: tracks[claim.track || ""] || "미분류",
    grade: claim.decision.grade || (estimated ? claim.decision.display_grade! : range ? `${range.floor}–${range.ceiling} 범위` : "미판정"),
    label: !claim.source_verified ? "원문 대조 필요" : ({ INCOMPLETE: "추가 근거 필요", SUBSTANTIATED: "근거 확인", UNSUBSTANTIATED: "근거 부족" } as Record<string, string>)[claim.decision.label || ""] || claim.decision.label || (estimated ? "예비 등급" : range ? "근거 보류" : "—"),
    evidencePages: pages,
    estimated,
  };
}

function formatDate(value?: string) {
  if (!value) return "기록 없음";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat("ko-KR", {
    year: "numeric", month: "long", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Seoul",
  }).format(date) + " KST";
}

function download(filename: string, content: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function csvCell(value: string | number | null) {
  const raw = value === null ? "" : String(value);
  const safe = /^[\s]*[=+\-@]/.test(raw) ? `'${raw}` : raw;
  return `"${safe.replace(/"/g, '""')}"`;
}

export default function AuditReportPage() {
  const { company } = useParams();
  const companyName = company && company in companies ? companies[company as keyof typeof companies] : null;
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!companyName) return;
    const controller = new AbortController();
    setSnapshot(null);
    setError(false);
    fetch(`${import.meta.env.BASE_URL}demo/${company}-2025.json`, { signal: controller.signal })
      .then(response => {
        if (!response.ok) throw new Error("snapshot unavailable");
        return response.json() as Promise<Snapshot>;
      })
      .then(data => { if (!controller.signal.aborted) setSnapshot(data); })
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [company, companyName]);

  if (!companyName) return <main className="audit-empty"><h1>보고서를 찾을 수 없습니다</h1><Link to="/demo">결과로 돌아가기</Link></main>;
  if (error) return <main className="audit-empty"><h1>보고서 데이터를 불러오지 못했습니다</h1><p>{companyName} 분석 결과를 확인해 주세요.</p><Link to="/demo">결과로 돌아가기</Link></main>;
  if (!snapshot) return <main className="audit-empty" role="status">보고서를 준비하는 중입니다…</main>;
  const data = snapshot;

  const isDemo = !!(snapshot.demo_mode || snapshot.relaxed_rules || snapshot.run.demo_mode || snapshot.run.relaxed_rules);
  const rows = snapshot.claims.map(claim => reportRow(claim, isDemo));
  const counts = Object.fromEntries(grades.map(grade => [grade, rows.filter(row => row.grade === grade).length])) as Record<string, number>;
  const decided = grades.reduce((total, grade) => total + counts[grade], 0);
  const demonstrated = rows.filter(row => row.estimated).length;
  const ranges = rows.filter(row => row.grade.includes("범위")).length;
  const pending = snapshot.claims.length - decided - ranges;
  const management = snapshot.claims.filter(claim => claim.track === "management");
  const verifiedManagement = management.filter(claim => claim.source_verified && claim.elements?.some(item => item.id === "M3" && item.state === "present" && item.evidence?.some(ref => ref.page))).length;
  const linked = rows.filter(row => row.evidencePages.length > 0).length;
  const documentTitle = snapshot.processed_reports?.[0]?.title || snapshot.title;
  const scope = snapshot.processed_reports?.[0]?.scope;
  const reportDate = snapshot.run.completed_at || snapshot.run.executed_at || snapshot.generated_at;
  const findings = [
    `관리체계 주장 ${management.length}건 중 ${verifiedManagement}건은 외부검증 요소가 원문 근거 쪽수와 연결됐습니다.`,
    `전체 주장 ${snapshot.claims.length}건 중 ${decided - demonstrated}건에 확정 등급이 기록됐습니다.${demonstrated ? ` ${demonstrated}건은 예비 등급입니다.` : ` ${ranges}건은 등급 범위로 남았습니다.`}`,
    `요소별 근거 쪽수가 연결된 주장은 ${linked}건입니다. 연결되지 않은 항목은 표에서 ‘—’로 표시합니다.`,
  ];

  function exportJson() {
    download(`${company}-proofops-audit.json`, JSON.stringify({
      company: companyName, document: documentTitle, generated_at: data.generated_at,
      rule_pack: data.run.rule_pack_name, rule_pack_hash: data.run.rule_pack_hash,
      model_ids: data.run.model_ids, scope, coverage: data.coverage,
      grade_distribution: counts, grade_denominator: decided, demonstration_count: demonstrated, range_count: ranges, pending_count: pending,
      findings, claims: rows,
    }, null, 2), "application/json;charset=utf-8");
  }

  function exportCsv() {
    const head = ["주장 ID", "원문 쪽", "문장 요약", "트랙", "등급", "라벨", "예비 등급", "핵심 근거 쪽수"];
    const body = rows.map(row => [row.id, row.page, row.quote, row.track, row.grade, row.label, row.estimated ? "예" : "아니오", row.evidencePages.join("; ")]);
    download(`${company}-proofops-claims.csv`, `\uFEFF${[head, ...body].map(line => line.map(csvCell).join(",")).join("\r\n")}`, "text/csv;charset=utf-8");
  }

  return <div className="audit-page">
    <div className="audit-toolbar" aria-label="보고서 작업">
      <Link to="/demo">← 결과로 돌아가기</Link>
      <span>{companyName} / AUDIT REPORT</span>
      <div><button type="button" onClick={() => window.print()}>PDF로 저장</button><button type="button" onClick={exportJson}>JSON 다운로드</button><button type="button" onClick={exportCsv}>CSV 다운로드</button></div>
    </div>
    <article className="audit-paper">
      <section className="audit-cover">
        <div className="audit-brand"><span className="brand-dot" aria-hidden="true" /><strong>PROOFOPS</strong><span>DISCLOSURE EVIDENCE REVIEW</span></div>
        <div className="audit-cover-main"><p className="audit-kicker">ENVIRONMENTAL DISCLOSURE / EVIDENCE REVIEW</p><h1>{companyName}<br />공시 근거 검토 보고서</h1><p>{documentTitle}</p></div>
        <div className="audit-cover-foot"><div><span>분석 일시</span><strong>{formatDate(reportDate)}</strong></div><div><span>판정 기준</span><strong>환경 주장 입증 등급</strong></div><div><span>근거 검토</span><strong>원문 인용과 쪽수 대조</strong></div><div><span>분석 범위</span><strong>{snapshot.coverage.pages_processed}/{snapshot.coverage.pages_total}쪽</strong></div></div>
      </section>

      <section className="audit-section audit-summary">
        <div className="audit-section-head"><span>01 / EXECUTIVE SUMMARY</span><h2>핵심 검토 결과</h2><p>보고서 안의 환경 주장을 원문 근거와 연결한 분석 결과입니다.</p></div>
        <div className="audit-stat-row"><div><span>추출 주장</span><strong>{snapshot.claims.length.toLocaleString()}<small>건</small></strong></div><div><span>{demonstrated ? "등급 결과" : "확정 등급"}</span><strong>{decided.toLocaleString()}<small>건</small></strong></div><div><span>근거 쪽수 연결</span><strong>{linked.toLocaleString()}<small>건</small></strong></div></div>
        <div className="audit-summary-grid"><div className="audit-chart"><h3>{demonstrated ? "등급 결과 분포" : "확정 등급 분포"} <small>분모 {decided}건</small></h3>{grades.map(grade => <div className="audit-chart-row" key={grade}><span>{grade}</span><div className="audit-chart-track"><i className={`audit-bar audit-bar-${grade.toLowerCase()}`} style={{ width: `${decided ? counts[grade] / decided * 100 : 0}%` }} /></div><strong>{counts[grade]}</strong></div>)}<p>{demonstrated ? `예비 등급 ${demonstrated}건 · ` : ""}범위 보류 {ranges}건 · 미판정 {pending}건</p></div><div className="audit-findings"><h3>주요 발견</h3><ol>{findings.map(finding => <li key={finding}>{finding}</li>)}</ol></div></div>
        <div className="audit-pipeline"><h3>처리 흐름</h3><div>{(snapshot.funnel || []).map((stage, index) => <div key={`${stage.label}-${index}`}><span>{stage.label === "표시 등급" ? "등급 결과" : stage.label}</span><strong>{stage.count.toLocaleString()}건</strong></div>)}</div></div>
        <p className="audit-scope">처리 페이지 {snapshot.coverage.pages_processed}쪽 · 미처리 {snapshot.coverage.pages_unprocessed ?? Math.max(0, snapshot.coverage.pages_total - snapshot.coverage.pages_processed - (snapshot.coverage.pages_unreadable || 0))}쪽 · 판독 불가 {snapshot.coverage.pages_unreadable || 0}쪽 · 검토 대상 {snapshot.coverage.claims_needs_review}건</p>
      </section>

      <section className="audit-section audit-claims">
        <div className="audit-section-head"><span>02 / CLAIM REGISTER</span><h2>주장별 판정과 근거</h2><p>주장 요약은 최대 200자, 쪽수는 물리 페이지 기준입니다.</p></div>
        <table><thead><tr><th scope="col">원문</th><th scope="col">문장 요약</th><th scope="col">트랙</th><th scope="col">등급 / 라벨</th><th scope="col">핵심 근거</th></tr></thead><tbody>{rows.map(row => <tr key={row.id}><td>p.{row.page ?? "?"}</td><td>{row.quote || "—"}</td><td>{row.track}</td><td><strong>{row.grade}</strong><br /><span>{row.label}</span></td><td>{row.evidencePages.length ? row.evidencePages.slice(0, 4).map(page => `p.${page}`).join(", ") + (row.evidencePages.length > 4 ? ` 외 ${row.evidencePages.length - 4}쪽` : "") : "—"}</td></tr>)}</tbody></table>
      </section>

      <section className="audit-section audit-appendix">
        <div className="audit-section-head"><span>03 / APPENDIX</span><h2>방법과 해석 범위</h2></div>
        <h3>입증 등급 사다리</h3><div className="audit-method-grid">{grades.map(grade => <div key={grade}><strong>{grade}</strong><p>{gradeMeaning[grade]}</p></div>)}</div>
        <h3>적용 원칙</h3><p>모델은 추출과 태깅을 맡고, 확정 등급과 라벨은 규칙팩의 판정 결과를 표시합니다.{demonstrated ? " 예비 등급은 핵심 근거 일부가 확인되기 전의 판정입니다." : ""} 원문 인용과 쪽수가 연결된 근거를 사용하며, 미확인·충돌·판독 불가는 근거 부재로 바꾸지 않습니다. E3는 공시 안의 입증 연결 수준이며 실제 환경 성과나 외부 인증을 보장하지 않습니다.</p>
        <h3>한계</h3><p>이 보고서는 {snapshot.partial ? "선택 페이지" : "문서"} 분석 결과를 요약합니다. 미처리 페이지와 미판정 주장은 등급 분포에서 제외되며, 연결된 쪽수는 해당 주장의 사실 여부에 대한 독립 검증을 뜻하지 않습니다.</p>
        <div className="audit-trace"><span>판정 기준</span><code>환경 주장 입증 등급</code><span>분석 자료 생성</span><code>{formatDate(snapshot.generated_at)}</code></div>
      </section>
    </article>
  </div>;
}
