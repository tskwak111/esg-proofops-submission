import { useEffect, useMemo, useRef, useState, type FormEvent, type ReactNode } from "react";
import { getElementLabel } from "../labels";
import type { PageInfo, Section } from "./sectionPicker";

type Decision = { evidence_grade: string | null; label?: string | null; missing_elements?: string[]; grade_range: { floor: string; ceiling: string; open_elements?: string[] } | null; decision_status: string };
export type LiveResultClaim = Claim;
type Claim = { quote: string; page: number; track: string | null; source_verified: boolean; blocked_reason: string | null; elements: { name: string; element_id: string; state: string; quote: string | null; source_verified: boolean }[]; decision: Decision | null };
type Result = { claims: Claim[]; pages: number[]; duration_ms: number; cost_usd: number; tagging_passes?: number; notice: string };
const tracks: Record<string, string> = { goal: "목표", performance: "성과", management: "관리체계" };
const errors: Record<string, string> = { ACCESS_DENIED: "접근 키를 확인해 주세요.", BODY_TOO_LARGE: "선택한 쪽의 PDF가 4MB를 넘습니다. 쪽수를 줄여 주세요.", INVALID_PDF: "선택한 PDF를 읽지 못했습니다.", INVALID_PAGES: "쪽 번호를 확인해 주세요.", UPSTAGE_UNAVAILABLE: "문서 파싱을 완료하지 못했습니다. 다시 시도해 주세요.", LUNA_UNAVAILABLE: "주장 분석을 완료하지 못했습니다. 다시 시도해 주세요.", REQUEST_COST_CAP: "요청 비용 한도를 넘습니다. 쪽수를 줄여 주세요." };
const sections: [Section, string][] = [["E", "환경(E)"], ["S", "사회(S)"], ["G", "지배구조(G)"], ["A", "부록"], ["O", "기타"]];
function pickPages(info: PageInfo[], selected: Section[]) {
  const matches = info.filter(item => selected.includes(item.section));
  const retained = matches.length <= 60 ? matches : [...matches].sort((a, b) =>
    Number(b.assurance) - Number(a.assurance) || b.score - a.score || a.page - b.page).slice(0, 60);
  return { pages: retained.map(item => item.page).sort((a, b) => a - b), dropped: matches.length - retained.length };
}

function parsePages(value: string, total: number): number[] {
  const pages: number[] = [];
  for (const token of value.split(",")) {
    const match = token.trim().match(/^(\d+)(?:\s*-\s*(\d+))?$/);
    if (!match) throw new Error("쪽 번호를 26,28,31 또는 26-28 형식으로 입력해 주세요.");
    const start = Number(match[1]); const end = Number(match[2] || match[1]);
    if (start < 1 || end > total || end < start || end - start > 59) throw new Error(`1–${total}쪽 중 최대 60쪽을 선택해 주세요.`);
    for (let page = start; page <= end; page++) if (!pages.includes(page)) pages.push(page);
  }
  if (!pages.length || pages.length > 60) throw new Error("1–60쪽을 선택해 주세요.");
  return pages.sort((a, b) => a - b);
}

export function LiveReport({ file, renderClaims }: { file: File; renderClaims?: (claims: LiveResultClaim[]) => ReactNode }) {
  const panel = useRef<HTMLElement>(null);
  const results = useRef<HTMLElement>(null);
  const [range, setRange] = useState("");
  const [code, setCode] = useState("");
  const [stage, setStage] = useState("");
  const [error, setError] = useState("");
  const [result, setResult] = useState<Result | null>(null);
  const [info, setInfo] = useState<PageInfo[]>([]);
  const [checked, setChecked] = useState<Section[]>(["E", "A"]);
  const [dropped, setDropped] = useState(0);
  const busy = !!stage && stage !== "완료";
  const selectedPages = useMemo(() => {
    try { return parsePages(range, info.length || 10000); } catch { return []; }
  }, [range, info.length]);

  useEffect(() => {
    let cancelled = false;
    setStage("보고서 목차와 쪽 내용 읽는 중"); setInfo([]); setRange(""); setResult(null); setError("");
    void import("./sectionPicker").then(({ scanSections }) =>
      scanSections(file, (current, total) => { if (!cancelled) setStage(`구역 찾는 중 · ${current}/${total}쪽`); }))
      .then(found => { if (cancelled) return; setInfo(found); const picked = pickPages(found, ["E", "A"]);
        setRange(picked.pages.join(",")); setDropped(picked.dropped); setStage(""); })
      .catch(() => { if (!cancelled) { setStage(""); setError("구역을 자동으로 찾지 못했습니다. 쪽 번호를 직접 입력해 주세요."); } });
    return () => { cancelled = true; };
  }, [file]);
  useEffect(() => { panel.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }, [file]);
  useEffect(() => { if (result) results.current?.scrollIntoView({ behavior: "smooth", block: "start" }); }, [result]);

  function chooseSection(section: Section) {
    const next = checked.includes(section) ? checked.filter(item => item !== section) : [...checked, section];
    setChecked(next);
    const picked = pickPages(info, next);
    setRange(picked.pages.join(",")); setDropped(picked.dropped); setResult(null);
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setStage("선택한 쪽 준비 중"); setError(""); setResult(null);
    try {
      const { PDFDocument, PDFName } = await import("pdf-lib");
      const original = await PDFDocument.load(await file.arrayBuffer());
      const pages = parsePages(range, original.getPageCount());
      // Link annotations point at other pages, so copying them drags the whole
      // report along; thumbnails and editor data are not page content either.
      for (const page of pages) {
        const node = original.getPage(page - 1).node;
        for (const key of ["Annots", "Thumb", "PieceInfo"]) node.delete(PDFName.of(key));
      }
      const chunks: number[][] = [];
      for (let i = 0; i < pages.length; i += 10) chunks.push(pages.slice(i, i + 10));
      const combined: Result = { claims: [], pages, duration_ms: 0, cost_usd: 0, notice: "사용자 최종 검토 전" };
      const started = performance.now();
      const skipped: number[] = [];
      const failed: string[] = [];
      for (let i = 0; i < chunks.length; i++) {
        const selected = await PDFDocument.create();
        const copies = await selected.copyPages(original, chunks[i].map(page => page - 1));
        copies.forEach(page => selected.addPage(page));
        const bytes = await selected.save({ useObjectStreams: true });
        if (bytes.length > 4_000_000) {
          if (chunks[i].length === 1) { skipped.push(chunks[i][0]); chunks.splice(i, 1); i--; continue; }
          const half = Math.ceil(chunks[i].length / 2);
          chunks.splice(i, 1, chunks[i].slice(0, half), chunks[i].slice(half)); i--; continue;
        }
        setStage(`문서 파싱 · 원문 대조 · 규칙 판정 중 (${i + 1}/${chunks.length}묶음)`);
        const response = await fetch("/api/live-report", { method: "POST", body: new Blob([new Uint8Array(bytes)], { type: "application/pdf" }),
          headers: { "X-Demo-Access-Code": code, "X-Page-Numbers": JSON.stringify(chunks[i]) }, cache: "no-store" });
        const data = await response.json().catch(() => ({})) as Result & { error?: string };
        if (response.status === 403) throw new Error(errors[data.error || ""] || "접근 키를 확인해 주세요.");
        if (!response.ok || !Array.isArray(data.claims)) {
          failed.push(`${chunks[i][0]}–${chunks[i][chunks[i].length - 1]}쪽(${errors[data.error || ""] || response.status})`);
          continue;
        }
        combined.claims.push(...data.claims);
        combined.cost_usd += data.cost_usd;
        combined.duration_ms = Math.round(performance.now() - started);
        setResult({ ...combined, claims: [...combined.claims] });
      }
      const notes = [
        skipped.length ? `이미지가 커서 전송 한도(4MB)를 넘은 ${skipped.join(", ")}쪽은 건너뛰었습니다.` : "",
        failed.length ? `분석하지 못한 묶음: ${failed.join(", ")}` : "",
      ].filter(Boolean).join(" ");
      if (!combined.claims.length && (skipped.length || failed.length) && !result) setError(notes || "분석을 완료하지 못했습니다.");
      else if (notes) setError(notes);
      setResult({ ...combined, claims: [...combined.claims], duration_ms: Math.round(performance.now() - started) });
      setStage("완료");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "분석을 완료하지 못했습니다."); setStage("");
    }
  }

  const detectedBy = [...new Set(info.filter(item => selectedPages.includes(item.page)).map(item => item.source))].join("·");
  return <><section ref={panel} className="surface live-report" aria-label="실시간 보고서 분석">
    <p className="eyebrow">LIVE REPORT</p><h2>실시간 분석</h2>
    <p>선택한 쪽만 분석 서비스로 전송합니다. 원본 PDF는 이 브라우저에 남습니다.</p>
    {info.length > 0 && <div className="live-report-sections"><strong>분석할 구역</strong><div>{sections.map(([id, label]) =>
      <label key={id}><input type="checkbox" checked={checked.includes(id)} onChange={() => chooseSection(id)} /> {label} <small>{info.filter(page => page.section === id).length}쪽</small></label>)}</div>
      <p>구역 판별: {detectedBy || "키워드"} · 기본 선택: 환경(E) + 부록</p>
    </div>}
    <form onSubmit={submit} className="live-report-form">
      <label>분석할 쪽 <small>최대 60쪽 · 예: 26,28,31 또는 83-95,218-244</small><input value={range} onChange={event => { setRange(event.target.value); setDropped(0); }} required placeholder="26,28,31" /></label>
      <label>접근 키<input type="password" autoComplete="off" value={code} onChange={event => setCode(event.target.value)} required placeholder="접근 키" /></label>
      <button type="submit" disabled={busy}>{busy ? stage : "실시간 분석 시작 ↗"}</button>
    </form>
    {dropped > 0 && <p className="live-report-selection">60쪽 한도에 맞춰 검증·GRI 쪽을 우선 유지하고, 환경 키워드 점수가 낮은 {dropped}쪽을 제외했습니다.</p>}
    {selectedPages.length > 0 && <p className="live-report-selection">선택: {selectedPages.length}쪽 · {selectedPages.join(", ")}</p>}
    {error && <p className="live-report-error" role="alert">{error}</p>}
    {stage && <p className="live-report-stage" role="status">{stage}</p>}
  </section>
    {result && <section ref={results} className="surface live-report-results analyze-results" aria-label="분석 결과"><h3>선택한 쪽의 분석 결과</h3><p>{result.pages.length}쪽 · {result.claims.length}건 · {(result.duration_ms / 1000).toFixed(1)}초 · 처리 비용 ${result.cost_usd.toFixed(4)} · 사용자 최종 검토 전</p><p>묶음당 환경 관련 문단 최대 16개에서 주장 최대 5건을 추출합니다.</p>
      {result.claims.length === 0 && <p>검토한 문단에서 확인 가능한 환경 주장을 찾지 못했습니다. 다른 쪽을 선택해 주세요.</p>}
      {renderClaims ? renderClaims(result.claims) : <ol>{result.claims.map((claim, index) => <li key={`${claim.page}-${index}`}>
        <div className="live-report-claim-head"><strong>{claim.page}쪽 · {claim.track ? tracks[claim.track] || claim.track : "분류 검토 필요"}</strong><span>{claim.source_verified ? "원문 확인" : "원문 대조 필요"}</span></div>
        <blockquote>{claim.quote}</blockquote>
        {claim.decision?.decision_status === "blocked_rule_gap" ? <p>{claim.blocked_reason}</p> : claim.decision?.evidence_grade ? <p>규칙 판정 {claim.decision.evidence_grade}</p> : claim.decision?.grade_range ? <p>가능 범위 {claim.decision.grade_range.floor}–{claim.decision.grade_range.ceiling} · 검토 필요</p> : <p>{claim.blocked_reason || "판정 검토 필요"}</p>}
        {claim.elements.length > 0 && <details><summary>요소와 근거</summary><ul>{claim.elements.map(element => <li key={element.name}><strong>{getElementLabel(element.element_id)}</strong> · {element.state === "present" ? "근거 확인" : "미확인"}{element.quote && <q>{element.quote}</q>}</li>)}</ul></details>}
      </li>)}</ol>}
    </section>}
  </>;
}
