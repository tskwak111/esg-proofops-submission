import { Link, useParams } from "react-router";
import ReviewSimulator, { type SimulatorClaim } from "./ReviewSimulator";

export default function ReviewPage({ claims }: { claims: SimulatorClaim[] }) {
  const { claimId } = useParams();
  const claim = claims.find(item => item.id === claimId);
  return <main className="static-main demo-main"><div className="breadcrumb"><Link to="/demo">검토 결과</Link><span>/</span> 검토 모드</div>
    {claim ? <ReviewSimulator key={claim.id} claim={claim} /> : <section className="surface"><h1>주장을 찾을 수 없습니다</h1><Link to="/demo">결과 목록으로 돌아가기</Link></section>}
  </main>;
}
