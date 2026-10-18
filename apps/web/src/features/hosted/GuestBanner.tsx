import type { Session } from "./hostedClient";

export function GuestBanner({ session }: { session: Session | null }) {
  if (session?.role !== "guest") return null;
  return <p className="hosted-notice hosted-guest-banner" role="status" data-testid="guest-banner">올린 보고서는 이 브라우저에서만 보입니다 · 원본 PDF는 7일 후 삭제 · 새 보고서 분석은 12~30분({session.guest ? `하루 ${session.guest.fresh_runs_limit}건` : "하루 한도 적용"}) · 미리 분석된 보고서는 바로 결과</p>;
}
