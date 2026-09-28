import React, { useEffect, useRef, useState } from "react";
import { StatusBadge } from "../../components/StatusBadge";
import { errorMessage, isSessionError } from "../session/api";
import {
  fetchReconciliationCases,
  getRevisionExportUrl,
  getSourceAttachmentUrl,
  submitEvaluate,
  submitPolicyApproval,
  submitReviewConfirmation,
} from "./api";
import type { CaseDetail, ReconciliationWorkspaceProps } from "./types";

function generateIdempotencyKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `idem-${Date.now()}-${Math.random().toString(36).slice(2, 11)}`;
}

export function ReconciliationWorkspace({
  apiBase = "",
  runId,
  claimId,
  session,
  onSessionInvalid,
}: ReconciliationWorkspaceProps): React.JSX.Element {
  const tenantKey = session.tenant_id ?? "default";
  const renderScopeKey = `${apiBase}:${tenantKey}:${runId}:${claimId}:${session.csrf_token}`;

  const [cases, setCases] = useState<CaseDetail[]>([]);
  const [selectedCaseId, setSelectedCaseId] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  // Reviewer form state
  const [confirmSourceBindings, setConfirmSourceBindings] = useState<boolean>(false);
  const [confirmDecisionBindings, setConfirmDecisionBindings] = useState<boolean>(false);
  const [confirmSearchCoverage, setConfirmSearchCoverage] = useState<boolean>(false);
  const [reviewReason, setReviewReason] = useState<string>("");

  // Admin policy approval form state
  const [policyApproved, setPolicyApproved] = useState<boolean>(false);
  const [policyReason, setPolicyReason] = useState<string>("");

  // Idempotency trackers for same-key retries per intent
  const reviewIdemRef = useRef<{ signature: string; key: string } | null>(null);
  const policyIdemRef = useRef<{ signature: string; key: string } | null>(null);
  const evaluateIdemRef = useRef<{ signature: string; key: string } | null>(null);

  // Scope tracker for aborting stale responses
  const currentScopeKeyRef = useRef<string>(renderScopeKey);
  currentScopeKeyRef.current = renderScopeKey;
  const loadAbortRef = useRef<AbortController | null>(null);
  const actionAbortRef = useRef<AbortController | null>(null);

  const isEditor = session.role === "editor" || session.role === "admin";
  const isReviewer = session.role === "reviewer" || session.role === "admin";
  const isAdmin = session.role === "admin";

  const loadCases = async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);
    try {
      const items = await fetchReconciliationCases(apiBase, runId, claimId, signal);
      if (signal?.aborted || currentScopeKeyRef.current !== renderScopeKey) return;
      setCases(items);
      if (items.length > 0) {
        setSelectedCaseId(prev => (items.some(c => c.case_id === prev) ? prev : items[0].case_id));
      } else {
        setSelectedCaseId("");
      }
    } catch (err: unknown) {
      if (signal?.aborted || currentScopeKeyRef.current !== renderScopeKey) return;
      if (isSessionError(err)) {
        onSessionInvalid();
        return;
      }
      setError(errorMessage(err, "대조 케이스 목록을 불러오지 못했습니다."));
    } finally {
      if (currentScopeKeyRef.current === renderScopeKey) {
        setLoading(false);
      }
    }
  };

  // Reset private state and load when scope changes
  useEffect(() => {
    loadAbortRef.current?.abort();
    actionAbortRef.current?.abort();

    const controller = new AbortController();
    loadAbortRef.current = controller;

    setCases([]);
    setSelectedCaseId("");
    setError(null);
    setNotice(null);
    setBusy(false);
    setConfirmSourceBindings(false);
    setConfirmDecisionBindings(false);
    setConfirmSearchCoverage(false);
    setReviewReason("");
    setPolicyApproved(false);
    setPolicyReason("");
    reviewIdemRef.current = null;
    policyIdemRef.current = null;
    evaluateIdemRef.current = null;

    loadCases(controller.signal);

    return () => {
      controller.abort();
      actionAbortRef.current?.abort();
    };
  }, [renderScopeKey]);

  const selectedCase = cases.find(c => c.case_id === selectedCaseId) ?? null;

  // Sync policy approval checkbox when selected case changes
  useEffect(() => {
    if (selectedCase) {
      setPolicyApproved(selectedCase.policy_approved);
      setPolicyReason("");
      policyIdemRef.current = null;
    }
  }, [selectedCase?.case_id, selectedCase?.revision]);

  useEffect(() => {
    setConfirmSourceBindings(false); setConfirmDecisionBindings(false);
    setConfirmSearchCoverage(false); setReviewReason("");
    reviewIdemRef.current = null;
  }, [selectedCaseId]);

  // Handle Review Submission
  const handleReviewSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedCase || !isReviewer || busy) return;
    if (!confirmSourceBindings || !confirmDecisionBindings) {
      setError("출처 바인딩 및 결정 바인딩 확인이 모두 필요합니다.");
      return;
    }
    if (!reviewReason.trim()) {
      setError("검토 사유를 입력해 주세요.");
      return;
    }

    setBusy(true);
    setError(null);
    setNotice(null);

    actionAbortRef.current?.abort();
    const controller = new AbortController();
    actionAbortRef.current = controller;

    const signature = `${selectedCase.case_id}:${selectedCase.revision}:${confirmSourceBindings}:${confirmDecisionBindings}:${confirmSearchCoverage}:${reviewReason.trim()}`;
    const idempotencyKey =
      reviewIdemRef.current?.signature === signature
        ? reviewIdemRef.current.key
        : generateIdempotencyKey();
    reviewIdemRef.current = { signature, key: idempotencyKey };

    try {
      const updated = await submitReviewConfirmation(
        apiBase,
        selectedCase.case_id,
        {
          reason: reviewReason.trim(),
          confirm_source_bindings: true,
          confirm_decision_bindings: true,
          confirm_search_coverage: confirmSearchCoverage,
        },
        session.csrf_token,
        selectedCase.revision,
        idempotencyKey,
        controller.signal,
      );
      if (currentScopeKeyRef.current !== renderScopeKey) return;
      setCases(prev => prev.map(c => (c.case_id === updated.case_id ? updated : c)));
      setNotice("검토 확인이 성공적으로 기록되었습니다.");
      setReviewReason("");
      setConfirmSourceBindings(false);
      setConfirmDecisionBindings(false);
      setConfirmSearchCoverage(false);
      reviewIdemRef.current = null;
    } catch (err: unknown) {
      if (controller.signal.aborted || currentScopeKeyRef.current !== renderScopeKey) return;
      if (isSessionError(err)) {
        onSessionInvalid();
        return;
      }
      setError(errorMessage(err, "검토 확인 제출 중 오류가 발생했습니다."));
    } finally {
      if (currentScopeKeyRef.current === renderScopeKey) {
        setBusy(false);
      }
    }
  };

  // Handle Policy Approval Submission
  const handlePolicyApprovalSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedCase || !isAdmin || busy) return;
    if (!policyReason.trim()) {
      setError("정책 승인/반려 사유를 입력해 주세요.");
      return;
    }

    setBusy(true);
    setError(null);
    setNotice(null);

    actionAbortRef.current?.abort();
    const controller = new AbortController();
    actionAbortRef.current = controller;

    const signature = `${selectedCase.case_id}:${selectedCase.revision}:${policyApproved}:${policyReason.trim()}`;
    const idempotencyKey =
      policyIdemRef.current?.signature === signature
        ? policyIdemRef.current.key
        : generateIdempotencyKey();
    policyIdemRef.current = { signature, key: idempotencyKey };

    try {
      const updated = await submitPolicyApproval(
        apiBase,
        selectedCase.case_id,
        {
          approved: policyApproved,
          reason: policyReason.trim(),
        },
        session.csrf_token,
        selectedCase.revision,
        idempotencyKey,
        controller.signal,
      );
      if (currentScopeKeyRef.current !== renderScopeKey) return;
      setCases(prev => prev.map(c => (c.case_id === updated.case_id ? updated : c)));
      setNotice("정책 승인 상태가 업데이트되었습니다.");
      setPolicyReason("");
      policyIdemRef.current = null;
    } catch (err: unknown) {
      if (controller.signal.aborted || currentScopeKeyRef.current !== renderScopeKey) return;
      if (isSessionError(err)) {
        onSessionInvalid();
        return;
      }
      setError(errorMessage(err, "정책 승인 처리 중 오류가 발생했습니다."));
    } finally {
      if (currentScopeKeyRef.current === renderScopeKey) {
        setBusy(false);
      }
    }
  };

  // Handle Evaluate Submission
  const handleEvaluateSubmit = async () => {
    if (!selectedCase || !isEditor || busy) return;

    setBusy(true);
    setError(null);
    setNotice(null);

    actionAbortRef.current?.abort();
    const controller = new AbortController();
    actionAbortRef.current = controller;

    const signature = `${selectedCase.case_id}:${selectedCase.revision}`;
    const idempotencyKey =
      evaluateIdemRef.current?.signature === signature
        ? evaluateIdemRef.current.key
        : generateIdempotencyKey();
    evaluateIdemRef.current = { signature, key: idempotencyKey };

    try {
      const updated = await submitEvaluate(
        apiBase,
        selectedCase.case_id,
        session.csrf_token,
        selectedCase.revision,
        idempotencyKey,
        controller.signal,
      );
      if (currentScopeKeyRef.current !== renderScopeKey) return;
      setCases(prev => prev.map(c => (c.case_id === updated.case_id ? updated : c)));
      setNotice("대조 재평가가 완료되었습니다.");
      evaluateIdemRef.current = null;
    } catch (err: unknown) {
      if (controller.signal.aborted || currentScopeKeyRef.current !== renderScopeKey) return;
      if (isSessionError(err)) {
        onSessionInvalid();
        return;
      }
      setError(errorMessage(err, "대조 재평가 실행 중 오류가 발생했습니다."));
    } finally {
      if (currentScopeKeyRef.current === renderScopeKey) {
        setBusy(false);
      }
    }
  };

  if (loading) {
    return (
      <section className="reconciliation-workspace" aria-busy="true">
        <h2>대조 워크스페이스 (Reconciliation Workspace)</h2>
        <div role="status" className="loading-indicator">
          대조 데이터를 불러오는 중입니다...
        </div>
      </section>
    );
  }

  if (error && cases.length === 0) {
    return (
      <section className="reconciliation-workspace">
        <h2>대조 워크스페이스 (Reconciliation Workspace)</h2>
        <div role="alert" className="error-message">
          {error}
        </div>
        <button type="button" onClick={() => loadCases()} disabled={busy}>
          다시 시도
        </button>
      </section>
    );
  }

  if (cases.length === 0) {
    return (
      <section className="reconciliation-workspace">
        <h2>대조 워크스페이스 (Reconciliation Workspace)</h2>
        <div role="status" className="empty-state">
          등록된 대조 케이스가 없습니다.
        </div>
      </section>
    );
  }

  const packet = selectedCase?.packet ?? {};
  const policy = selectedCase?.policy ?? {};
  const latestResult = selectedCase?.latest_result;
  const sources = selectedCase?.sources ?? [];
  const candidates = packet.candidates ?? [];

  // Determine result status label and tone
  const resultStatus = latestResult?.result.execution_state === "completed"
    ? latestResult.result.status : latestResult?.result.execution_state ?? "not_run";
  let resultTone: "neutral" | "success" | "warning" | "danger" = "neutral";
  let resultLabel = "미실행 (Not Run)";
  if (resultStatus === "matched") {
    resultTone = "success";
    resultLabel = "대조 또는 차이 설명 확인";
  } else if (resultStatus === "needs_explanation") {
    resultTone = "warning";
    resultLabel = "설명 보완 권장";
  } else if (resultStatus === "blocked") {
    resultTone = "danger";
    resultLabel = "차단됨 (Blocked)";
  } else if (resultStatus === "not_applicable") {
    resultLabel = "적용 대상 아님";
  }

  const isReviewed = selectedCase?.review_state === "reviewed";
  const isPolicyApproved = selectedCase?.policy_approved === true;
  const verifiedSourceIds = latestResult?.result.execution_state === "completed"
    ? latestResult.result.source_ids : [];

  return (
    <section className="reconciliation-workspace" aria-label="재무-지속가능성 공시 대조 워크스페이스">
      <header className="workspace-header">
        <h2>재무-지속가능성 공시 대조 (Reconciliation Workspace)</h2>
        <p>회계 처리의 적정성을 판단하지 않으며, 공시 간 차이에 대한 설명의 존재 여부를 점검합니다.</p>
        <p className="workspace-meta">
          <span>실행 ID: {runId}</span> | <span>클레임 ID: {claimId}</span>
        </p>
      </header>

      {notice && (
        <div role="status" className="notice-message" style={{ color: "var(--status-success)" }}>
          ✓ {notice}
        </div>
      )}

      {error && (
        <div role="alert" className="error-message" style={{ color: "var(--status-danger)" }}>
          ! {error}
          <button type="button" disabled={busy} onClick={() => loadCases()}>
            최신 상태 다시 불러오기
          </button>
        </div>
      )}

      {/* Case Selector Navigation */}
      <nav className="case-selector-nav" aria-label="대조 케이스 목록">
        <label htmlFor="case-select">대조 항목 선택: </label>
        <select
          id="case-select"
          value={selectedCaseId}
          onChange={e => setSelectedCaseId(e.target.value)}
          disabled={busy}
        >
          {cases.map(c => (
            <option key={c.case_id} value={c.case_id}>
              {c.item} (케이스: {c.case_id.slice(0, 8)}... / rev {c.revision})
            </option>
          ))}
        </select>
      </nav>

      {selectedCase && (
        <article className="case-detail-container" aria-labelledby="selected-case-heading">
          {/* Header with Case Badges */}
          <div className="case-header">
            <h3 id="selected-case-heading">
              항목: {selectedCase.item} <span className="revision-tag">(수정 버전: rev {selectedCase.revision})</span>
            </h3>

            <div className="badge-group" style={{ display: "flex", gap: "8px", flexWrap: "wrap", margin: "8px 0" }}>
              {selectedCase.synthetic && (
                <span className="status-badge" data-tone="warning" role="note">
                  [합성 검증 데이터 (Synthetic)]
                </span>
              )}

              <StatusBadge
                label={isReviewed ? "검토 완료 (Reviewed)" : "검토 대기 (Unreviewed)"}
                tone={isReviewed ? "success" : "warning"}
              />

              <StatusBadge
                label={isPolicyApproved ? "정책 승인됨 (Policy Approved)" : "정책 미승인 (Policy Unapproved)"}
                tone={isPolicyApproved ? "success" : "danger"}
              />

              <StatusBadge label={resultLabel} tone={resultTone} />
            </div>
          </div>

          {/* Prominent Policy Unapproved Notice */}
          {!isPolicyApproved && (
            <div
              role="alert"
              className="policy-unapproved-alert"
              style={{
                background: "#fef2f2",
                border: "1px solid var(--status-danger)",
                padding: "12px",
                borderRadius: "4px",
                margin: "12px 0",
              }}
            >
              <strong>정책 미승인 안내 (Policy Unapproved Notice):</strong>
              <p style={{ margin: "4px 0 0 0" }}>
                본 대조 케이스의 정책 승인이 아직 완료되지 않았습니다. 관리자의 공식 승인이 완료되기 전까지
                평가 결과는 차단(blocked) 상태로 처리될 수 있습니다.
              </p>
            </div>
          )}

          {/* Unreviewed Conspicuous Notice */}
          {!isReviewed && (
            <div
              role="note"
              className="unreviewed-notice"
              style={{
                background: "#fefce8",
                border: "1px solid var(--status-warning)",
                padding: "10px",
                borderRadius: "4px",
                margin: "12px 0",
              }}
            >
              <strong>미검토 상태 (Unreviewed):</strong> 출처 및 결정 바인딩에 대한 검토자 확인이 대기 중입니다.
            </div>
          )}

          {/* Paired Values Comparison (SR vs Financial) */}
          <section className="comparison-section" aria-labelledby="comparison-heading">
            <h4 id="comparison-heading">공시 원본 수치 대조 (Paired Values)</h4>
            <div
              className="paired-values-grid"
              style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "16px", margin: "12px 0" }}
            >
              <div
                className="paired-card sr-card"
                style={{ border: "1px solid #cbd5e1", padding: "12px", borderRadius: "4px" }}
              >
                <h5>지속가능경영보고서 원본 (Sustainability Report)</h5>
                <dl>
                  <dt>추출 값 (Raw):</dt>
                  <dd>
                    <code>{packet.sustainability?.raw ?? packet.sr_value ?? "값 없음"}</code>
                  </dd>
                  <dt>정규화 값 (Normalized):</dt>
                  <dd>
                    <code>{packet.sustainability?.normalized ?? "정규화 정보 없음"}</code>
                  </dd>
                  <dt>단위 / 통화:</dt>
                  <dd>{packet.sustainability?.unit ?? packet.sustainability?.currency ?? "미지정"}</dd>
                </dl>
              </div>

              <div
                className="paired-card financial-card"
                style={{ border: "1px solid #cbd5e1", padding: "12px", borderRadius: "4px" }}
              >
                <h5>재무제표 원본 (Financial Statement / OpenDART)</h5>
                <dl>
                  <dt>추출 값 (Raw):</dt>
                  <dd>
                    <code>{packet.financial?.raw ?? packet.financial_value ?? "값 없음"}</code>
                  </dd>
                  <dt>정규화 값 (Normalized):</dt>
                  <dd>
                    <code>{packet.financial?.normalized ?? "정규화 정보 없음"}</code>
                  </dd>
                  <dt>단위 / 통화:</dt>
                  <dd>{packet.financial?.unit ?? packet.financial?.currency ?? "미지정"}</dd>
                </dl>
              </div>
            </div>

            {packet.comparability && (
              <p>
                <strong>비교 가능성 (Comparability):</strong> {packet.comparability}
              </p>
            )}
          </section>

          <details><summary>검토 대상 입력 전체 확인</summary>
            <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(packet, null, 2)}</pre>
          </details>

          {/* Raw Quotes & Source Details */}
          <section className="sources-section" aria-labelledby="sources-heading">
            <h4 id="sources-heading">출처 및 인용문 (Sources & Raw Quotes)</h4>

            {/* Main Quote if available */}
            {(packet.claim?.quote || packet.raw_quote || packet.quote) && (
              <div className="quote-box" style={{ margin: "8px 0" }}>
                <strong>기준 클레임 인용문:</strong>
                <blockquote
                  style={{
                    margin: "4px 0",
                    padding: "8px 12px",
                    background: "#f8fafc",
                    borderLeft: "4px solid #94a3b8",
                  }}
                >
                  {packet.claim?.quote ?? packet.raw_quote ?? packet.quote}
                </blockquote>
              </div>
            )}

            {/* Source items table */}
            {sources.length > 0 ? (
              <div className="sources-table-wrapper" style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", margin: "8px 0" }}>
                  <thead>
                    <tr style={{ background: "#f1f5f9", textAlign: "left" }}>
                      <th style={{ padding: "8px" }}>출처 ID</th>
                      <th style={{ padding: "8px" }}>시스템</th>
                      <th style={{ padding: "8px" }}>위치 (Locator)</th>
                      <th style={{ padding: "8px" }}>파일 해시 (SHA-256)</th>
                      <th style={{ padding: "8px" }}>검증 상태</th>
                      <th style={{ padding: "8px" }}>원본 다운로드</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sources.map(src => (
                      <tr key={src.source_id} style={{ borderBottom: "1px solid #e2e8f0" }}>
                        <td style={{ padding: "8px" }}>
                          <code>{src.source_id}</code>
                          <blockquote style={{ margin: "8px 0" }}>{src.quote}</blockquote>
                        </td>
                        <td style={{ padding: "8px" }}>{src.document_role ?? src.source_system ?? "일반"}</td>
                        <td style={{ padding: "8px" }}>{src.locator ?? "미지정"}</td>
                        <td style={{ padding: "8px" }}>
                          <code style={{ fontSize: "11px" }}>
                            {src.artifact_sha256 ?? "없음"}
                          </code>
                        </td>
                        <td style={{ padding: "8px" }}>
                          <span
                            className="status-badge"
                            data-tone={verifiedSourceIds.includes(src.source_id) ? "success" : "warning"}
                          >
                            {verifiedSourceIds.includes(src.source_id)
                              ? "검증된 출처 (Verified Source)"
                              : "미검증 후보 (Unverified Candidate)"}
                          </span>
                        </td>
                        <td style={{ padding: "8px" }}>
                          <a
                            href={getSourceAttachmentUrl(apiBase, selectedCase.case_id, src.source_id)}
                            download
                            rel="noopener noreferrer"
                            className="source-attachment-link"
                          >
                            다운로드 (안전 첨부)
                          </a>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p>연결된 출처 문서가 없습니다.</p>
            )}

            {/* Candidate Extractions List (Visible as unverified) */}
            {candidates.length > 0 && (
              <div
                className="candidates-container"
                style={{
                  margin: "12px 0",
                  padding: "10px",
                  background: "#fffbeb",
                  border: "1px dashed #f59e0b",
                  borderRadius: "4px",
                }}
              >
                <h5>추출된 후보 수치 (Source Candidates - 미검증)</h5>
                <p style={{ margin: "4px 0", fontSize: "13px" }}>
                  * 실제 검증 결과로 승인되기 전까지 아래 후보 수치는 미검증(Unverified) 상태로 취급됩니다.
                </p>
                <ul>
                  {candidates.map(cand => (
                    <li key={cand.candidate_id}>
                      <strong>{cand.candidate_id}</strong>: {cand.value} (출처: {cand.source_id},{" "}
                      <span style={{ color: "var(--status-warning)", fontWeight: 600 }}>
                        미검증 후보
                      </span>
                      )
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </section>

          {/* Latest Result Details & Immutable Revision Export */}
          <section className="results-section" aria-labelledby="results-heading">
            <h4 id="results-heading">대조 평가 결과 (Reconciliation Result)</h4>
            {latestResult ? (
              <div
                className="result-card"
                style={{
                  border: "1px solid #cbd5e1",
                  padding: "12px",
                  borderRadius: "4px",
                  background: "#f8fafc",
                }}
              >
                {latestResult.revision < selectedCase.revision && (
                  <p role="status" className="stale-result-notice">
                    이 결과 이후 검토 또는 정책이 변경되었습니다. 현재 상태의 결과는 재평가로 확인하세요.
                  </p>
                )}
                <p>
                  <strong>결과 상태:</strong> <StatusBadge label={resultLabel} tone={resultTone} />
                </p>
                {latestResult.result.reason && (
                  <p>
                    <strong>사유 (Reason):</strong> {latestResult.result.reason}
                  </p>
                )}
                {latestResult.result.reason_codes && latestResult.result.reason_codes.length > 0 && (
                  <p>
                    <strong>사유 코드 (Reason Codes):</strong> {latestResult.result.reason_codes.join(", ")}
                  </p>
                )}
                {latestResult.result.variance !== undefined && latestResult.result.variance !== null && (
                  <p>
                    <strong>차이 (Variance):</strong> {String(latestResult.result.variance)}
                  </p>
                )}
                <p style={{ fontSize: "12px", color: "#64748b" }}>
                  기록 일시: {latestResult.created_at} (결과 버전: rev {latestResult.revision})
                </p>

                {/* Immutable Revision Snapshot Link */}
                <div style={{ marginTop: "8px" }}>
                  <a
                    href={getRevisionExportUrl(apiBase, selectedCase.case_id, latestResult.revision)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="export-revision-link"
                    style={{ fontWeight: 600 }}
                  >
                    불변 결과 스냅샷 내보내기 (JSON Export) ↗
                  </a>
                </div>
              </div>
            ) : (
              <p>아직 실행된 대조 결과가 없습니다. 에디터 권한으로 평가를 실행할 수 있습니다.</p>
            )}
          </section>

          {/* Editor Action: Evaluate (Hidden if unauthorized) */}
          {isEditor && (
            <section className="action-section evaluate-section" aria-labelledby="evaluate-heading">
              <h4 id="evaluate-heading">에디터 대조 평가 실행 (Evaluate)</h4>
              <p style={{ fontSize: "13px" }}>
                서버에 저장된 검토 스냅샷을 기반으로 대조를 재평가하고 불변 결과를 생성합니다.
              </p>
              <button
                type="button"
                onClick={handleEvaluateSubmit}
                disabled={busy}
                className="evaluate-btn"
                style={{ background: "#0284c7", color: "white", padding: "8px 16px", borderRadius: "4px" }}
              >
                {busy ? "평가 진행 중..." : "대조 재평가 실행 (Evaluate)"}
              </button>
            </section>
          )}

          {/* Reviewer Action: Confirmation Form (Hidden if unauthorized) */}
          {isReviewer && (
            <section className="action-section review-section" aria-labelledby="review-heading">
              <h4 id="review-heading">검토자 확인 (Reviewer Confirmation)</h4>
              <p style={{ fontSize: "13px" }}>
                검토자는 사실 관계와 검색 범위를 확인하며, 판정 상태를 직접 임의 선택하지 않습니다.
              </p>
              <form onSubmit={handleReviewSubmit} style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <input
                    type="checkbox"
                    checked={confirmSourceBindings}
                    onChange={e => setConfirmSourceBindings(e.target.checked)}
                    disabled={busy}
                  />
                  <span>출처 바인딩 확인 (Confirm Source Bindings)</span>
                </label>

                <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <input
                    type="checkbox"
                    checked={confirmDecisionBindings}
                    onChange={e => setConfirmDecisionBindings(e.target.checked)}
                    disabled={busy}
                  />
                  <span>결정 바인딩 확인 (Confirm Decision Bindings)</span>
                </label>

                <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <input
                    type="checkbox"
                    checked={confirmSearchCoverage}
                    onChange={e => setConfirmSearchCoverage(e.target.checked)}
                    disabled={busy}
                  />
                  <span>검색 범위 확인 (Confirm Search Coverage)</span>
                </label>

                <div>
                  <label htmlFor="review-reason" style={{ display: "block", marginBottom: "4px" }}>
                    검토 사유 (Review Reason):
                  </label>
                  <textarea
                    id="review-reason"
                    value={reviewReason}
                    onChange={e => setReviewReason(e.target.value)}
                    placeholder="검토 의견 및 확인 사유를 입력하세요"
                    disabled={busy}
                    style={{ width: "100%", minHeight: "60px", padding: "8px" }}
                  />
                </div>

                <button
                  type="submit"
                  disabled={busy || !confirmSourceBindings || !confirmDecisionBindings || !reviewReason.trim()}
                  className="review-submit-btn"
                  style={{ alignSelf: "flex-start", padding: "8px 16px" }}
                >
                  {busy ? "제출 중..." : "검토 확인 제출"}
                </button>
              </form>
            </section>
          )}

          {/* Admin Action: Policy Approval Form (Hidden if unauthorized) */}
          {isAdmin && (
            <section className="action-section policy-admin-section" aria-labelledby="policy-admin-heading">
              <h4 id="policy-admin-heading">관리자 정책 승인 (Admin Policy Approval)</h4>
              {/* Show policy scope / threshold with NO default values */}
              <div
                className="policy-details"
                style={{
                  background: "#f8fafc",
                  padding: "10px",
                  borderRadius: "4px",
                  marginBottom: "10px",
                }}
              >
                <p style={{ margin: "2px 0" }}>
                  <strong>정책 범위 (Policy Scope):</strong>{" "}
                  {policy.scope ?? (policy.enabled_items && policy.enabled_items.length > 0 ? policy.enabled_items.join(", ") : "지정 없음")}
                </p>
                <p style={{ margin: "2px 0" }}>
                  <strong>허용 임계값 (Threshold):</strong>{" "}
                  {policy.threshold !== undefined && policy.threshold !== null
                    ? String(policy.threshold)
                    : policy.c3_threshold !== undefined && policy.c3_threshold !== null
                      ? String(policy.c3_threshold)
                      : "임계값 지정 없음"}
                </p>
                {policy.source_policy_sha256 && (
                  <p style={{ margin: "2px 0", fontSize: "12px" }}>
                    <strong>정책 해시:</strong> <code>{policy.source_policy_sha256}</code>
                  </p>
                )}
                <details><summary>승인 대상 정책 전체 확인</summary>
                  <pre style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>{JSON.stringify(policy, null, 2)}</pre>
                </details>
              </div>

              <form onSubmit={handlePolicyApprovalSubmit} style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                <label style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                  <input
                    type="checkbox"
                    checked={policyApproved}
                    onChange={e => setPolicyApproved(e.target.checked)}
                    disabled={busy}
                  />
                  <span>정책 승인 허용 (Approve Policy)</span>
                </label>

                <div>
                  <label htmlFor="policy-reason" style={{ display: "block", marginBottom: "4px" }}>
                    승인 / 반려 사유 (Policy Reason):
                  </label>
                  <input
                    type="text"
                    id="policy-reason"
                    value={policyReason}
                    onChange={e => setPolicyReason(e.target.value)}
                    placeholder="관리자 승인 또는 반려 사유를 입력하세요"
                    disabled={busy}
                    style={{ width: "100%", padding: "8px" }}
                  />
                </div>

                <button
                  type="submit"
                  disabled={busy || !policyReason.trim()}
                  className="policy-submit-btn"
                  style={{ alignSelf: "flex-start", padding: "8px 16px" }}
                >
                  {busy ? "처리 중..." : "정책 승인 상태 업데이트"}
                </button>
              </form>
            </section>
          )}
        </article>
      )}
    </section>
  );
}
