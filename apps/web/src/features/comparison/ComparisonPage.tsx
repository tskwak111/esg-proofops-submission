import type { FormEvent } from "react";

export type Comparison = {
  comparison_id: string;
  status: "queued" | "completed" | "not_run" | "failed";
  reason: string | null;
  changes: {
    current_claim_id: string | null;
    prior_claim_id: string | null;
    type: "modified" | "removed_candidate" | "new" | "ambiguous";
    reason: string;
  }[];
};

export type PriorVersion = { document_version_id: string; report_year: number };

export type ComparisonPageProps = {
  currentYear: number;
  priorVersions: PriorVersion[];
  selectedPriorVersionId?: string;
  comparison: Comparison | null;
  busy?: boolean;
  onPriorVersionChange?: (versionId: string) => void;
  onCompare?: () => void;
};

const changeText = {
  modified: "목표 변경 후보",
  removed_candidate: "목표 삭제 후보",
  new: "새 목표 후보",
  ambiguous: "목표 매칭 검토 필요",
};

export function ComparisonPage(props: ComparisonPageProps) {
  const submit = (event: FormEvent) => {
    event.preventDefault();
    props.onCompare?.();
  };
  const noPrior = props.priorVersions.length === 0;

  return (
    <section aria-labelledby="comparison-heading">
      <h1 id="comparison-heading">다년도 목표 비교</h1>
      <p>전년 근거는 현재 연도 등급에 사용하지 않습니다.</p>
      <form onSubmit={submit}>
        <label htmlFor="prior-version">{props.currentYear}년과 비교할 전년 문서</label>{" "}
        <select
          id="prior-version"
          value={props.selectedPriorVersionId ?? ""}
          onChange={(event) => props.onPriorVersionChange?.(event.target.value)}
          disabled={noPrior || props.busy}
        >
          <option value="">전년 문서 선택</option>
          {props.priorVersions.map((version) => (
            <option key={version.document_version_id} value={version.document_version_id}>
              {version.report_year}년
            </option>
          ))}
        </select>{" "}
        <button
          type="submit"
          disabled={noPrior || props.busy || !props.selectedPriorVersionId || !props.onCompare}
          style={{ minHeight: 44 }}
        >
          {props.busy ? "비교 중…" : "비교 실행"}
        </button>
      </form>

      {noPrior || comparisonMissingPrior(props.comparison) ? (
        <p role="status">전년 문서가 없어 비교를 실행하지 않았습니다.</p>
      ) : null}
      {props.comparison?.status === "queued" ? <p role="status">비교 작업 대기 중입니다.</p> : null}
      {props.comparison?.status === "failed" ? (
        <p role="alert">비교를 완료하지 못했습니다.</p>
      ) : null}
      {props.comparison?.status === "not_run" && !comparisonMissingPrior(props.comparison) ? (
        <p role="status">{notRunText(props.comparison.reason)}</p>
      ) : null}
      {props.comparison?.status === "completed" ? (
        props.comparison.changes.length ? (
          <ul aria-label="목표 변경 후보">
            {props.comparison.changes.map((change, index) => (
              <li key={`${change.current_claim_id ?? "none"}-${change.prior_claim_id ?? "none"}-${index}`}>
                {changeText[change.type]}
                {change.type === "removed_candidate" ? " — 삭제로 확정하지 않습니다." : ""}
              </li>
            ))}
          </ul>
        ) : (
          <p>확인된 목표 변경 후보가 없습니다.</p>
        )
      ) : null}
    </section>
  );
}

function comparisonMissingPrior(comparison: Comparison | null) {
  return (
    comparison?.status === "not_run" && comparison.reason === "prior_document_version_missing"
  );
}

function notRunText(reason: string | null) {
  if (reason === "prior_comparison_artifact_missing") {
    return "전년 문서의 승인된 비교 근거가 없어 비교를 실행하지 않았습니다.";
  }
  if (reason === "prior_document_version_not_previous_year") {
    return "선택한 문서가 직전 연도 문서가 아니어서 비교하지 않았습니다.";
  }
  if (reason === "document_periods_not_comparable") {
    return "보고 기간 또는 문서 유형이 호환되지 않아 비교하지 않았습니다.";
  }
  if (reason === "approved_claim_snapshot_missing") {
    return "승인된 목표 스냅샷이 없어 비교를 실행하지 않았습니다.";
  }
  if (reason === "comparison_source_unverified") {
    return "검증된 원문 근거가 없어 비교를 실행하지 않았습니다.";
  }
  if (reason === "target_comparison_key_missing") {
    return "승인된 목표 비교 기준이 없어 자동 비교하지 않았습니다.";
  }
  return "비교 조건을 확인할 수 없어 비교를 실행하지 않았습니다.";
}
