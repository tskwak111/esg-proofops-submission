import { requestJson } from "../session/api";
import type {
  CaseDetail,
  CaseListResponse,
  PolicyApprovalSubmission,
  ReviewSubmission,
} from "./types";

export async function fetchReconciliationCases(
  apiBase: string,
  runId: string,
  claimId: string,
  signal?: AbortSignal,
): Promise<CaseDetail[]> {
  const url = `${apiBase}/v1/runs/${encodeURIComponent(runId)}/claims/${encodeURIComponent(claimId)}/reconciliation`;
  const response = await requestJson<CaseListResponse>(url, {
    method: "GET",
    signal,
    headers: {
      Accept: "application/json",
    },
  });
  return response.items ?? [];
}

export async function fetchCaseDetail(
  apiBase: string,
  caseId: string,
  signal?: AbortSignal,
): Promise<CaseDetail> {
  const url = `${apiBase}/v1/reconciliation/cases/${encodeURIComponent(caseId)}`;
  return requestJson<CaseDetail>(url, {
    method: "GET",
    signal,
    headers: {
      Accept: "application/json",
    },
  });
}

export async function submitReviewConfirmation(
  apiBase: string,
  caseId: string,
  data: ReviewSubmission,
  csrfToken: string,
  revision: number,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<CaseDetail> {
  const url = `${apiBase}/v1/reconciliation/cases/${encodeURIComponent(caseId)}/review`;
  return requestJson<CaseDetail>(url, {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
      "If-Match": `"${revision}"`,
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({
      reason: data.reason,
      confirm_source_bindings: true,
      confirm_decision_bindings: true,
      confirm_search_coverage: Boolean(data.confirm_search_coverage),
    }),
  });
}

export async function submitPolicyApproval(
  apiBase: string,
  caseId: string,
  data: PolicyApprovalSubmission,
  csrfToken: string,
  revision: number,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<CaseDetail> {
  const url = `${apiBase}/v1/reconciliation/cases/${encodeURIComponent(caseId)}/policy-approval`;
  return requestJson<CaseDetail>(url, {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
      "If-Match": `"${revision}"`,
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({
      approved: Boolean(data.approved),
      reason: data.reason,
    }),
  });
}

export async function submitEvaluate(
  apiBase: string,
  caseId: string,
  csrfToken: string,
  revision: number,
  idempotencyKey: string,
  signal?: AbortSignal,
): Promise<CaseDetail> {
  const url = `${apiBase}/v1/reconciliation/cases/${encodeURIComponent(caseId)}/evaluate`;
  return requestJson<CaseDetail>(url, {
    method: "POST",
    signal,
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
      "If-Match": `"${revision}"`,
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({}),
  });
}

export function getSourceAttachmentUrl(
  apiBase: string,
  caseId: string,
  sourceId: string,
): string {
  return `${apiBase}/v1/reconciliation/cases/${encodeURIComponent(caseId)}/sources/${encodeURIComponent(sourceId)}/content`;
}

export function getRevisionExportUrl(
  apiBase: string,
  caseId: string,
  revision: number,
): string {
  return `${apiBase}/v1/reconciliation/cases/${encodeURIComponent(caseId)}/revisions/${revision}`;
}
