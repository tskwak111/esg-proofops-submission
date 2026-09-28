import type { Coverage } from "../runs/CoveragePanel";

export type Summary = {
  run_id: string;
  snapshot_epoch: number;
  coverage: Coverage;
  grade_counts: Record<"E0" | "E1" | "E2" | "E3", number>;
  undecided_count: number;
  not_applicable_count: number;
  deferred_count: number;
  unverified_basis_count: number;
  missing_by_element: { element_id: string; count: number }[];
  applicable_count: number;
  satisfied_count: number;
  fulfillment_rate: number | null;
  undetermined_applicability_count?: number | null;
};

export function RunSummary({ summary }: { summary: Summary }) {
  const { coverage } = summary;
  const gradeDenominator = Object.values(summary.grade_counts).reduce(
    (total, count) => total + count,
    0,
  );
  const unprocessedClaims =
    coverage.claims_discovered - coverage.claims_decided - coverage.claims_needs_review;
  const unprocessedRate = coverage.claims_discovered
    ? `${((unprocessedClaims / coverage.claims_discovered) * 100).toFixed(1)}%`
    : "산정 대상 없음";
  const unknownApplicability = summary.undetermined_applicability_count;

  return (
    <section aria-labelledby="run-summary-heading">
      <h2 id="run-summary-heading">실행 요약</h2>
      <p>등급 분포 분모 {gradeDenominator}건</p>
      <ul aria-label="등급 분포">
        {(["E0", "E1", "E2", "E3"] as const).map((grade) => (
          <li key={grade}>
            {grade} {summary.grade_counts[grade]}건
          </li>
        ))}
      </ul>
      <dl>
        <dt>판정 상태</dt>
        <dd>
          미판정 {summary.undecided_count}건 · 검토 필요 {coverage.claims_needs_review}건
        </dd>
        <dt>처리 범위</dt>
        <dd>
          페이지 미처리 {coverage.pages_unprocessed}쪽 · 판독 불가 {coverage.pages_unreadable}쪽 (일부 구간 포함)
          {" · "}주장 미처리 {unprocessedRate} (검토 대기는 위에 별도 표시)
        </dd>
        <dt>기준 적용성</dt>
        <dd>
          적용 {summary.applicable_count}건 · 적용 제외 {summary.not_applicable_count}건 · 유예{" "}
          {summary.deferred_count}건 ·{" "}
          {unknownApplicability == null
            ? "산정 범위 미확정"
            : `산정 범위 미확정 ${unknownApplicability}건`}
        </dd>
        <dt>기준 충족률</dt>
        <dd>
          {summary.fulfillment_rate == null
            ? "산정 불가"
            : `${summary.satisfied_count}/${summary.applicable_count} (${(
                summary.fulfillment_rate * 100
              ).toFixed(1)}%)`}
        </dd>
        <dt>검증되지 않은 기준 근거</dt>
        <dd>{summary.unverified_basis_count}건</dd>
      </dl>
      <h3>결손 요소</h3>
      {summary.missing_by_element.length ? (
        <ul>
          {summary.missing_by_element.map(({ element_id, count }) => (
            <li key={element_id}>
              {element_id} {count}건
            </li>
          ))}
        </ul>
      ) : (
        <p>
          {gradeDenominator === 0
            ? "아직 입증 판정이 없어 결손 여부를 확인할 수 없습니다."
            : "판정된 주장에서는 확인된 결손 요소가 없습니다."}
        </p>
      )}
    </section>
  );
}
