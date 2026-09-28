export type Coverage = {
  pages_total: number;
  pages_processed: number;
  pages_unreadable: number;
  pages_unprocessed: number;
  chunks_discovered: number;
  chunks_processed: number;
  claims_discovered: number;
  claims_decided: number;
  claims_needs_review: number;
  full_scope: boolean;
  complete: boolean;
};

type Props = {
  status: "queued" | "running" | "partial" | "completed" | "failed" | "cancelled";
  coverage: Coverage;
};

export function CoveragePanel({ status, coverage }: Props) {
  const fullyReviewed = status === "completed" && coverage.full_scope && coverage.complete;
  const chunksUnprocessed = coverage.chunks_discovered - coverage.chunks_processed;
  const claimsUnprocessed =
    coverage.claims_discovered - coverage.claims_decided - coverage.claims_needs_review;

  return (
    <section aria-labelledby="coverage-heading">
      <h2 id="coverage-heading">분석 범위</h2>
      {fullyReviewed ? (
        <strong role="status">전수 검토 완료</strong>
      ) : (
        <p role="status">
          {status === "partial" ? "부분 완료" : "전수 검토 미완료"} · 미처리 영역을 확인하세요.
        </p>
      )}
      <p>{coverage.full_scope ? "문서 전체 범위" : "사용자가 지정한 부분 범위"}</p>
      <p>
        판독 불가에는 일부 구간의 글자나 좌표를 확인하지 못한 페이지도 포함됩니다.
        확인된 다른 구간의 주장과 근거는 계속 검토할 수 있습니다.
      </p>
      <dl>
        <dt>페이지</dt>
        <dd>
          전체 {coverage.pages_total}, 처리(판독 불가 제외) {coverage.pages_processed}, 판독 불가{" "}
          {coverage.pages_unreadable}, 미처리 {coverage.pages_unprocessed}
        </dd>
        <dt>청크</dt>
        <dd>
          발견 {coverage.chunks_discovered}, 처리 {coverage.chunks_processed}, 미처리{" "}
          {chunksUnprocessed}
        </dd>
        <dt>주장</dt>
        <dd>
          발견 {coverage.claims_discovered}, 판정 {coverage.claims_decided}, 검토 필요{" "}
          {coverage.claims_needs_review}, 미처리 {claimsUnprocessed}
        </dd>
      </dl>
    </section>
  );
}
