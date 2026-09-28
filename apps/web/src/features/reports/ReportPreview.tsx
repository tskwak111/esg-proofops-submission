import { getElementLabel } from "../labels";

type BasisRef = {
  element_id?: string | null;
  source_section?: string;
  clause?: string | null;
  verification_status: "verified" | "unverified" | "unlicensed";
  rule_ids?: string[];
};

type SourceRef = {
  document_version_id: string;
  parse_manifest_id: string | null;
  page_num: number;
  bbox?: number[] | null;
  raw_text_sha256: string;
  quote: string;
};

type StatusRecord = {
  status: string;
  level?: string | null;
  provider?: string | null;
  statement_id?: string | null;
  metric_match?: string;
  period_match?: string;
  boundary_match?: string;
  evidence_refs?: SourceRef[];
};

type SafeHarborRecord =
  | { status: "not_run" }
  | {
      claim_id: string;
      applicable: boolean | null;
      category: string | null;
      checklist: unknown[];
      reasonable_basis_documented: boolean | null;
      legal_effect: "not_determined";
      mapping_status: "approved" | "unresolved";
      gap_ids: string[];
    };

type TagElement = {
  element_id: string;
  state: string;
  normalized_value: string | null;
  evidence_refs: SourceRef[];
};

type ReportClaim = {
  claim_id: string;
  claim_quote?: string | null;
  classification_review?: { origin: string; track: string; revision: number } | null;
  tag_elements?: TagElement[] | null;
  tag_revision: number;
  decision_revision: number;
  decision_status: string;
  evidence_grade: string | null;
  label: string | null;
  grade_range?: { floor: string; ceiling: string; open_elements: string[] } | null;
  review_status: string;
  missing_elements: string[];
  unresolved_elements: string[];
  gap_ids: string[];
  rule_pack_sha256: string | null;
  model_sha256: string | null;
  prompt_sha256: string | null;
  replicate_hashes: string[];
  source_refs: SourceRef[];
  source_status: string;
  basis_refs: BasisRef[];
  assurance: StatusRecord;
  safe_harbor: SafeHarborRecord;
  suggestion: string | null;
  review_action?: {
    claim_id: string;
    reasons: string[];
    checks: string[];
    unresolved_elements: string[];
    gap_ids: string[];
    source_pages: number[];
  } | null;
};

export type ReportModel = {
  schema: "report_model_v1";
  tenant_id: string;
  run_id: string;
  document_version_id: string;
  parse_manifest_id: string;
  source_sha256: string;
  snapshot_epoch: number;
  generated_at: string;
  execution_profile: string;
  rule_pack_hashes: string[];
  coverage: {
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
  unverified_basis: number;
  partial: boolean;
  unfinished_count: number;
  unverified_clause_count: number;
  claims: ReportClaim[];
};

const decisionText: Record<string, string> = {
  blocked_evidence: "근거 불확실로 미판정",
  blocked_rule_gap: "규칙 공백으로 미판정",
  not_applicable: "적용 제외",
  not_run: "미실행",
};

const assuranceText: Record<string, string> = {
  covered: "보증 범위 안",
  not_covered: "보증 범위 밖",
  undetermined: "보증 범위 미확정",
  not_run: "보증 확인 미실행",
};

function safeHarborText(record: SafeHarborRecord) {
  if ("status" in record) return "세이프하버 미실행";
  if (record.applicable === false) return "세이프하버 비대상 · 법적 효력 미판정";
  return record.mapping_status === "unresolved"
    ? "세이프하버 근거 기록 · 법적 효력 미판정"
    : "세이프하버 근거 기록";
}

export function ReportPreview({ report }: { report: ReportModel }) {
  const { coverage } = report;
  return (
    <article aria-labelledby="report-heading">
      <h1 id="report-heading">감사 리포트</h1>
      <p role="status">{report.partial ? "검토용 부분 리포트" : "완료 리포트"}</p>
      <dl>
        <dt>스냅샷</dt>
        <dd>
          epoch {report.snapshot_epoch} · document {report.document_version_id} · parse{" "}
          {report.parse_manifest_id ?? "미발행"}
          {" · "}source {report.source_sha256}
        </dd>
        <dt>미완료</dt>
        <dd>미완료 {report.unfinished_count}건</dd>
        <dt>처리 범위</dt>
        <dd>
          판독 불가 {coverage.pages_unreadable}쪽 · 미처리 {coverage.pages_unprocessed}쪽
        </dd>
        <dt>미확인 기준 근거</dt>
        <dd>{report.unverified_clause_count}건</dd>
      </dl>

      {report.claims.map((claim) => (
        <section key={claim.claim_id} aria-labelledby={`claim-${claim.claim_id}`}>
          <h2 id={`claim-${claim.claim_id}`}>주장 {claim.claim_id}</h2>
          <p>검토 대상 주장: {claim.claim_quote ?? "이전 스냅샷에 주장 문장이 저장되지 않았습니다"}</p>
          <p>
            판정: {claim.decision_status === "decided"
              ? `${claim.evidence_grade} / ${claim.label}`
              : decisionText[claim.decision_status] ?? claim.decision_status}
          </p>
          {claim.grade_range ? (
            <p>
              가능 등급 범위: {claim.grade_range.floor} ~ {claim.grade_range.ceiling} (확정 등급 아님) · 확인하면
              범위가 좁혀지는 요소: {claim.grade_range.open_elements.map(getElementLabel).join(", ")}
            </p>
          ) : null}
          <p>
            revision: tag {claim.tag_revision} / decision {claim.decision_revision} · 검토: {claim.review_status}
          </p>
          {claim.classification_review ? <p>선행분류 기록: {claim.classification_review.origin === "ai_delegated_classification" ? "AI 위임 분류(사람 검토 아님)" : "사람 분류 검토"} · {claim.classification_review.track} · revision {claim.classification_review.revision} (등급 승인 아님)</p> : null}
          <p>규칙 팩: {claim.rule_pack_sha256 ?? "미실행"}</p>
          <p>
            모델: {claim.model_sha256 ?? "미실행"} · 프롬프트: {claim.prompt_sha256 ?? "미실행"} · replicas:{" "}
            {claim.replicate_hashes.length ? claim.replicate_hashes.join(", ") : "미실행"}
          </p>
          {claim.source_refs.length ? (
            <ul aria-label="원문 근거 위치">
              {claim.source_refs.map((source, index) => (
                <li key={`${source.page_num}-${index}`}>
                  p.{source.page_num}
                  {source.bbox ? ` [${source.bbox.join(", ")}]` : " 위치 좌표 미확인"}: {source.quote}
                  {" · "}source {source.raw_text_sha256}
                </li>
              ))}
            </ul>
          ) : (
            <p>원문 근거 위치 미실행</p>
          )}
          {claim.suggestion ? <p>수정 제안: {claim.suggestion}</p> : <p>확정된 수정 제안 없음</p>}
          {claim.review_action?.checks.length ? (
            <section aria-label="다음 검토 작업">
              <h3>다음 검토 작업</h3>
              <ul>{claim.review_action.checks.map((check, index) => <li key={index}>{check}</li>)}</ul>
            </section>
          ) : null}
          {claim.unresolved_elements.length ? (
            <p>미해결 요소: {claim.unresolved_elements.map(getElementLabel).join(", ")}</p>
          ) : null}
          {claim.tag_elements === undefined || claim.tag_elements === null ? (
            <p>태그 요소 미포함(이전 스냅샷)</p>
          ) : claim.tag_elements.length === 0 ? (
            <p>태그된 요소 없음(미태깅)</p>
          ) : (
            <section aria-label="태그 요소">
              <h3>태그 요소</h3>
              <ul>
                {claim.tag_elements.map((element) => (
                  <li key={element.element_id}>
                    {getElementLabel(element.element_id)}: {element.state} · 값: {element.normalized_value ?? "기록 없음"}
                    {element.evidence_refs.length ? (
                      <ul>
                        {element.evidence_refs.map((source, index) => (
                          <li key={index}>
                            p.{source.page_num}: {source.quote}
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <p>인용 없음</p>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          )}
          {claim.basis_refs.length ? (
            <ul aria-label="기준 근거">
              {claim.basis_refs.map((basis, index) => (
                <li key={`${basis.source_section ?? "basis"}-${index}`}>
                  {basis.clause && basis.verification_status === "verified"
                    ? basis.clause
                    : "조항 미확인"}
                  {basis.source_section ? ` · 원문 §${basis.source_section}` : ""} · {basis.verification_status}
                </li>
              ))}
            </ul>
          ) : (
            <p>기준 근거 미실행</p>
          )}
          <p>{assuranceText[claim.assurance.status] ?? claim.assurance.status}</p>
          <p>{safeHarborText(claim.safe_harbor)}</p>
        </section>
      ))}
    </article>
  );
}
