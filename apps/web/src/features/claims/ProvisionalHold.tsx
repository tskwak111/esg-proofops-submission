import { getElementLabel } from "../labels";
import { MissingEvidenceCard, type MissingEvidenceCardProps } from "./MissingEvidenceCard";

export type HoldExplanation = {
  display_type?: string;
  hold_reasons?: string[];
  evidence_note?: string;
  needed_evidence?: { track: string; target_grade: string; elements: string[] }[];
};
const tracks: Record<string, string> = { goal: "목표형", performance: "성과형", management: "관리체계형" };
export function ProvisionalHold({ grade, ...context }: { grade: HoldExplanation } & Omit<MissingEvidenceCardProps, "reasons" | "evidenceNote">) {
  if (grade.display_type !== "hold") return null;
  const checks = grade.needed_evidence?.map(row => `${tracks[row.track] || row.track} ${row.target_grade} 등급 검토에 필요한 근거 요소: ${row.elements.map(getElementLabel).join(", ")}`) ?? [];
  return <div className="provisional-hold"><MissingEvidenceCard {...context} reasons={grade.hold_reasons} evidenceNote={grade.evidence_note} nextChecks={[...(context.nextChecks ?? []), ...checks]} /></div>;
}
