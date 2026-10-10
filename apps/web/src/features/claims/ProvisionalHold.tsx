import { getElementLabel } from "../labels";

export type HoldExplanation = {
  display_type?: string;
  hold_reasons?: string[];
  evidence_note?: string;
  needed_evidence?: { track: string; target_grade: string; elements: string[] }[];
};

const tracks: Record<string, string> = { goal: "목표", performance: "성과", management: "관리체계" };

export function ProvisionalHold({ grade }: { grade: HoldExplanation }) {
  if (grade.display_type !== "hold") return null;
  return <div className="provisional-hold">
    <p>보류 이유: {grade.hold_reasons?.join(" · ") || "확인 필요"}</p>
    <p>등급 검토에 필요한 근거 요소:</p>
    <ul>{grade.needed_evidence?.map(row => <li key={`${row.track}:${row.target_grade}`}>
      {tracks[row.track] || row.track} {row.target_grade} · {row.elements.map(getElementLabel).join(", ")}
    </li>)}</ul>
    {grade.evidence_note && <small>{grade.evidence_note}</small>}
  </div>;
}
