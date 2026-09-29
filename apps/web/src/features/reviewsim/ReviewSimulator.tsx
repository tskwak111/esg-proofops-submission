import { useEffect, useMemo, useState } from "react";
import { getElementLabel } from "../labels";
import "./reviewsim.css";

type Track = "goal" | "performance" | "management";
type State = "present" | "unknown" | "absent" | "conflict";
export type SimulatorClaim = {
  id: string;
  quote: string;
  page: number | null;
  track: string | null;
  elements: { id: string; state: string; evidence?: { page: number | null; quote: string }[] }[];
  decision: { grade: string | null; status: string };
  review: { tag_revision: number; decision_revision: number };
};
type Result = [string, string | null, string | null, [string, string, string[]] | null, string[], string[], string[], string[]];
type Table = {
  rule_pack_sha256: string;
  tracks: Record<Track, { ladder: string[]; other: string[]; rows: Record<string, Result> }>;
};
type Revision = { number: number; change: string; before: string; after: string; note: string };

const elementIds: Record<Track, string[]> = {
  management: ["M1", "M2", "M3", "M4", "M5", "M6"],
  performance: ["P1", "P2", "P3", "P4", "P5", "P6"],
  goal: ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"],
};
const code: Record<State, string> = { present: "p", unknown: "u", absent: "a", conflict: "n" };
const stateText: Record<State, string> = { present: "확인", unknown: "미확인", absent: "부재", conflict: "상충" };
const statusText: Record<string, string> = {
  decided: "규칙 판정", blocked_evidence: "근거 확인 필요", blocked_rule_gap: "규칙 확인 필요",
};
let tablePromise: Promise<Table> | undefined;
function loadTable(): Promise<Table> {
  tablePromise ??= fetch(`${import.meta.env.BASE_URL}demo/engine-table.json`)
    .then(response => { if (!response.ok) throw new Error("조회표를 불러오지 못했습니다."); return response.json() as Promise<Table>; })
    .catch(error => { tablePromise = undefined; throw error; });
  return tablePromise;
}

function initialStates(claim: SimulatorClaim): Record<string, State> {
  return Object.fromEntries(claim.elements.map(element => [element.id,
    element.state in code ? element.state as State : "unknown",
  ]));
}

function display(result?: Result): string {
  if (!result) return "–";
  return result[1] ?? (result[3] ? `${result[3][0]}–${result[3][1]}` : "판정 보류");
}

export default function ReviewSimulator({ claim }: { claim: SimulatorClaim }) {
  const track = claim.track && claim.track in elementIds ? claim.track as Track : null;
  const [table, setTable] = useState<Table | null>(null);
  const [error, setError] = useState("");
  const [states, setStates] = useState<Record<string, State>>(() => initialStates(claim));
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [savedNotes, setSavedNotes] = useState<Record<string, string>>({});
  const [history, setHistory] = useState<Revision[]>([]);
  const [willingnessOnly, setWillingnessOnly] = useState(false);

  useEffect(() => {
    let active = true;
    loadTable().then(value => { if (active) setTable(value); }).catch(reason => { if (active) setError(String(reason)); });
    return () => { active = false; };
  }, []);

  const result = useMemo(() => {
    if (!track || !table) return undefined;
    const group = table.tracks[track];
    const vector = group.ladder.map(id => code[states[id] ?? "unknown"]).join("");
    const other = group.other.some(id => ["unknown", "conflict"].includes(states[id] ?? "unknown")) ? "1" : "0";
    return group.rows[`${vector}${other}${track === "management" ? Number(willingnessOnly) : ""}`];
  }, [table, track, states, willingnessOnly]);

  function changeState(id: string, next: State) {
    const previous = states[id] ?? "unknown";
    if (next === previous) return;
    const nextStates = { ...states, [id]: next };
    const group = track && table?.tracks[track];
    const vector = group?.ladder.map(element => code[nextStates[element] ?? "unknown"]).join("") ?? "";
    const other = group?.other.some(element => ["unknown", "conflict"].includes(nextStates[element] ?? "unknown")) ? "1" : "0";
    const nextResult = group?.rows[`${vector}${other}${track === "management" ? Number(willingnessOnly) : ""}`];
    setStates(nextStates);
    setSavedNotes(current => ({ ...current, [id]: notes[id]?.trim() ?? "" }));
    setHistory(current => [{
      number: claim.review.decision_revision + current.length + 1,
      change: `${id} ${stateText[previous]}→${stateText[next]}`,
      before: display(result), after: display(nextResult), note: notes[id]?.trim() ?? "",
    }, ...current]);
  }

  function saveNote(id: string) {
    const note = notes[id]?.trim() ?? "";
    if (note === (savedNotes[id] ?? "")) return;
    setSavedNotes(current => ({ ...current, [id]: note }));
    setHistory(current => [{
      number: claim.review.decision_revision + current.length + 1,
      change: `${id} 메모 ${note ? "기록" : "삭제"}`,
      before: display(result), after: display(result), note,
    }, ...current]);
  }

  if (!track) return <section className="review-sim"><h2>판정 검토</h2><p>주장 유형을 확인하면 요소별 판정을 검토할 수 있습니다.</p></section>;
  const group = table?.tracks[track];
  const title = { management: "관리체계", performance: "성과", goal: "목표" }[track];
  const grade = display(result);
  return <section className="review-sim" aria-label="판정 검토">
    <div className="review-sim-head"><div><span className="review-sim-kicker">DECISION REVIEW</span><h2>판정 검토</h2><p>{title} 주장 · 원문 {claim.page ?? "?"}쪽</p></div><span className="review-sim-mode">현재 화면에서 조정</span></div>
    <blockquote>{claim.quote}</blockquote>
    <div className="review-sim-grid">
      <div className="review-sim-elements"><h3>입증 요소</h3><p className="review-sim-sub">요소 상태를 변경하면 판정 결과를 바로 확인할 수 있습니다.</p>
        {elementIds[track].map(id => <div className="review-sim-row" key={id}>
          <label htmlFor={`review-${claim.id}-${id}`}>{getElementLabel(id)}</label>
          <select id={`review-${claim.id}-${id}`} value={states[id] ?? "unknown"} disabled={!table} onChange={event => changeState(id, event.target.value as State)}>
            {(Object.keys(code) as State[]).map(state => <option key={state} value={state}>{stateText[state]}</option>)}
          </select>
          <input aria-label={`${id} 검토자 메모`} placeholder="검토자 메모" maxLength={200} value={notes[id] ?? ""} onChange={event => setNotes(current => ({ ...current, [id]: event.target.value }))} onBlur={() => saveNote(id)} />
        </div>)}
        {track === "management" && <label className="review-sim-check"><input type="checkbox" checked={willingnessOnly} onChange={event => setWillingnessOnly(event.target.checked)} /> 의향만 서술한 주장</label>}
      </div>
      <aside className="review-sim-side"><div className="review-sim-grade" key={grade + result?.[0]}>
        <span>판정 결과</span><strong>{error ? "조회 오류" : !table ? "불러오는 중" : grade}</strong>
        <p>{result?.[2] ?? statusText[result?.[0] ?? ""] ?? ""}</p>
        {result?.[3] && <small>가능 범위 {result[3][0]}–{result[3][1]} · 확인할 요소 {result[3][2].map(getElementLabel).join(", ")}</small>}
      </div>
        {result && <div className="review-sim-reason"><h3>판정 근거</h3>
          <p>{result[0] === "decided" ? "현재 입력으로 판정 가능" : statusText[result[0]]}</p>
          {result[6].filter(id => group?.ladder.includes(id)).length > 0 && <p>부족: {result[6].filter(id => group?.ladder.includes(id)).map(getElementLabel).join(", ")}</p>}
          {result[7].filter(id => group?.ladder.includes(id)).length > 0 && <p>확인 중: {result[7].filter(id => group?.ladder.includes(id)).map(getElementLabel).join(", ")}</p>}
          {result[5].length > 0 && <p>규칙 검토: {result[5].join(", ")}</p>}
          <small>변경 사항은 현재 화면에만 적용됩니다.</small>
        </div>}
        <div className="review-sim-history"><h3>변경 기록</h3><p className="review-sim-match">현재 화면에서 변경한 요소와 판정</p>
          {history.length ? <ol>{history.map(item => <li key={item.number}><b>{item.number}.</b> {item.change} · {item.before}→{item.after}{item.note && <small>“{item.note}”</small>}</li>)}</ol> : <p>요소 상태를 변경하면 기록이 여기에 표시됩니다.</p>}
        </div>
      </aside>
    </div>
  </section>;
}
