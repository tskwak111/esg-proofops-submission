import type { Session } from "../session/api";

export type ReconciliationWorkspaceProps = {
  apiBase?: string;
  runId: string;
  claimId: string;
  session: Session;
  onSessionInvalid: () => void;
};

export type CaseSource = {
  binding_state?: "draft" | "confirmed";
  source_id: string;
  document_id?: string;
  artifact_sha256?: string;
  locator?: string;
  quote?: string;
  source_system?: string;
  document_role?: string;
  verification_status?: "verified" | "unverified" | "candidate";
  is_candidate?: boolean;
};

export type ValueBinding = {
  raw?: string;
  normalized?: string | null;
  kind?: string;
  unit?: string;
  currency?: string;
  source_id?: string | null;
};

export type CasePacket = {
  claim?: { quote: string };
  sustainability?: ValueBinding;
  financial?: ValueBinding;
  sr_value?: string;
  financial_value?: string;
  raw_quote?: string;
  quote?: string;
  source_locator?: string;
  locator?: string;
  source_sha256?: string;
  sha256?: string;
  candidates?: Array<{
    candidate_id: string;
    value: string;
    source_id: string;
    locator?: string;
    sha256?: string;
    status?: "unverified" | "verified";
  }>;
  comparability?: string;
  period_start?: string;
  period_end?: string;
  explanation?: {
    source_id?: string | null;
    search_complete?: boolean;
    quote?: string;
  };
};

export type CasePolicy = {
  allowed_capex_account_ids?: string[];
  c3_account_mapping_approved?: boolean;
  policy_id?: string;
  version?: string;
  approved?: boolean;
  scope?: string;
  enabled_items?: string[];
  threshold?: number | string | null;
  c3_threshold?: number | string | null;
  tolerance?: number | string | null;
  approved_by?: string | null;
  approved_on?: string | null;
  approved_at?: string | null;
  source_policy_sha256?: string;
};

export type LatestResult = {
  revision: number;
  result: {
    execution_state: "completed" | "blocked" | "not_run";
    status: "matched" | "needs_explanation" | "not_applicable" | null;
    source_ids: string[];
    reason?: string | null;
    reason_codes?: string[];
    variance?: string | number | null;
    variance_pct?: string | number | null;
    details?: string | null;
  };
  projection?: Record<string, unknown> | null;
  created_at: string;
};

export type CaseDetail = {
  case_id: string;
  run_id: string;
  claim_id: string;
  item: string;
  revision: number;
  synthetic: boolean;
  review_state: "pending" | "reviewed";
  policy_approved: boolean;
  packet: CasePacket;
  policy: CasePolicy;
  sources: CaseSource[];
  latest_result: LatestResult | null;
};

export type CaseListResponse = {
  items: CaseDetail[];
};

export type ReviewSubmission = {
  reason: string;
  confirm_source_bindings: true;
  confirm_decision_bindings: true;
  confirm_search_coverage: boolean;
};

export type PolicyApprovalSubmission = {
  approved: boolean;
  reason: string;
};
