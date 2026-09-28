// Run in a Vite browser: await import('/@fs/<repo>/tests/e2e/reconciliation_workspace_check.mjs').then(m => m.check()).
// CONTROLLED HTTP BOUNDARY: window.fetch below supplies complete fixed-API DTOs; no backend is exercised here.

export async function check() {
  const reactResource = performance
    .getEntriesByType("resource")
    .map(entry => entry.name)
    .find(name => /\/react\.js\?v=/.test(name));
  const version = reactResource ? new URL(reactResource).search : "";
  const { default: React } = await import(/* @vite-ignore */ "/node_modules/.vite/deps/react.js" + version);
  const {
    default: { createRoot },
  } = await import(/* @vite-ignore */ "/node_modules/.vite/deps/react-dom_client.js" + version);
  const { ReconciliationWorkspace } = await import("/src/features/reconciliation/ReconciliationWorkspace.tsx");

  const originalFetch = window.fetch;
  const host = document.createElement("div");
  document.body.append(host);
  const root = createRoot(host);

  const flush = () => new Promise(resolve => setTimeout(resolve, 40));
  const waitFor = async text => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (host.textContent.includes(text)) return;
      await flush();
    }
    throw new Error(`Timed out waiting for: ${text}; rendered: ${host.textContent}`);
  };
  const assert = (ok, message) => {
    if (!ok) throw new Error(message);
  };
  const waitSelector = async selector => {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      if (host.querySelector(selector)) return;
      await flush();
    }
    throw new Error(`Timed out waiting for selector: ${selector}`);
  };
  const button = text =>
    [...host.querySelectorAll("button")].find(item => item.textContent.includes(text));
  const input = (element, value) => {
    const proto =
      element instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : element instanceof HTMLInputElement
          ? HTMLInputElement.prototype
          : HTMLSelectElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(element, value);
    element.dispatchEvent(new Event("change", { bubbles: true }));
    element.dispatchEvent(new Event("input", { bubbles: true }));
  };

  const runId = "11111111-1111-4111-8111-111111111111";
  const claimId = "22222222-2222-4222-8222-222222222222";
  const caseId = "33333333-3333-4333-8333-333333333333";
  const sourceIdSr = "source-sr-1";
  const sourceIdFs = "source-fs-1";

  const caseDetail = (overrides = {}) => ({
    case_id: caseId,
    run_id: runId,
    claim_id: claimId,
    item: "C1 범위 1/2 대조",
    revision: 1,
    synthetic: true,
    review_state: "pending",
    policy_approved: false,
    packet: {
      sustainability: {
        raw: "1,234,567 tCO2e",
        normalized: "1234567",
        kind: "ghg_emissions",
        unit: "tCO2e",
        currency: null,
        source_id: sourceIdSr,
      },
      financial: {
        raw: "1,234,000 tCO2e",
        normalized: "1234000",
        kind: "ghg_emissions",
        unit: "tCO2e",
        currency: null,
        source_id: sourceIdFs,
      },
      raw_quote: "2024년 연결 기준 당사의 온실가스 배출량은 1,234,567 tCO2e 입니다.",
      comparability: "comparable",
      candidates: [
        {
          candidate_id: "cand-1",
          value: "1,234,000",
          source_id: sourceIdFs,
          locator: "note/ghg",
          sha256: "c".repeat(64),
          status: "unverified",
        },
      ],
    },
    policy: {
      policy_id: "pol-1",
      scope: "연결 공시 기준 (OFS/CFS)",
      enabled_items: ["C1", "C2"],
      threshold: null,
      c3_threshold: null,
      approved: false,
      source_policy_sha256: "p".repeat(64),
    },
    sources: [
      {
        source_id: sourceIdSr,
        source_system: "SUSTAINABILITY_REPORT",
        locator: "page=42;line=10",
        artifact_sha256: "a".repeat(64),
        quote: "온실가스 배출량 1,234,567 tCO2e",
        verification_status: "verified",
        is_candidate: false,
      },
      {
        source_id: sourceIdFs,
        source_system: "DART",
        locator: "note/ghg_table",
        artifact_sha256: "b".repeat(64),
        quote: "온실가스 배출량 1,234,000 tCO2e",
        verification_status: "unverified",
        is_candidate: true,
      },
    ],
    latest_result: {
      revision: 1,
      result: {
        execution_state: "blocked",
        status: null,
        source_ids: [],
        reason: "정책 미승인 및 검토 미완료로 인한 자동 차단",
        reason_codes: ["POLICY_UNAPPROVED", "REVIEW_PENDING"],
        variance: "567",
      },
      projection: {},
      created_at: "2026-09-21T10:00:00Z",
    },
    ...overrides,
  });

  const reviewCalls = [];
  const policyCalls = [];
  const evaluateCalls = [];
  let fetchCount = 0;
  let currentCase = caseDetail();
  let lateSignal;
  let releaseLate;
  let sessionInvalidCalls = 0;
  let mode = "normal";

  window.fetch = async (url, init = {}) => {
    const target = new URL(String(url), location.origin);
    const headers = new Headers(init.headers);
    fetchCount += 1;

    // Reconciliation cases list
    if (target.pathname === `/v1/runs/${runId}/claims/${claimId}/reconciliation`) {
      if (init.signal) {
        lateSignal = init.signal;
      }
      if (mode === "late-load") {
        return new Promise(resolve => { releaseLate = () => resolve(Response.json({ items: [currentCase] })); });
      }
      return Response.json({ items: [currentCase] });
    }

    // Single case detail
    if (target.pathname === `/v1/reconciliation/cases/${caseId}`) {
      return Response.json(currentCase, { headers: { ETag: `"${currentCase.revision}"` } });
    }

    // Review submission
    if (target.pathname === `/v1/reconciliation/cases/${caseId}/review` && init.method === "POST") {
      reviewCalls.push({
        headers,
        body: JSON.parse(String(init.body)),
      });
      currentCase = caseDetail({
        review_state: "reviewed",
        revision: currentCase.revision + 1,
      });
      return Response.json(currentCase);
    }

    // Policy approval submission
    if (
      target.pathname === `/v1/reconciliation/cases/${caseId}/policy-approval` &&
      init.method === "POST"
    ) {
      policyCalls.push({
        headers,
        body: JSON.parse(String(init.body)),
      });
      currentCase = caseDetail({
        policy_approved: true,
        policy: { ...currentCase.policy, approved: true },
        revision: currentCase.revision + 1,
      });
      return Response.json(currentCase);
    }

    // Evaluate submission
    if (
      target.pathname === `/v1/reconciliation/cases/${caseId}/evaluate` &&
      init.method === "POST"
    ) {
      evaluateCalls.push({
        headers,
        body: JSON.parse(String(init.body)),
      });
      if (mode === "transport-once") {
        mode = "normal";
        throw new TypeError("controlled transport failure");
      }
      if (mode === "conflict") return Response.json({ error: { code: "REVISION_CONFLICT", message: "controlled conflict" } }, { status: 409 });
      if (mode === "expired") return Response.json({ error: { code: "SESSION_EXPIRED", message: "controlled expired session" } }, { status: 401 });
      currentCase = caseDetail({
        revision: currentCase.revision + 1,
        latest_result: {
          revision: currentCase.revision + 1,
          result: {
            execution_state: "completed",
            status: "matched",
            source_ids: [sourceIdSr, sourceIdFs],
            reason: "검토 및 정책 승인 완료 후 일치 확인",
            variance: "0",
          },
          created_at: "2026-09-21T10:05:00Z",
        },
      });
      return Response.json(currentCase);
    }

    if (target.pathname.includes("/sources/") && target.pathname.endsWith("/content")) {
      return new Response("binary content", {
        status: 200,
        headers: { "Content-Type": "application/octet-stream" },
      });
    }

    return Response.json(
      { error: { code: "NOT_FOUND", message: target.pathname } },
      { status: 404 },
    );
  };

  const sessionForRole = role => ({
    user_id: `user-${role}`,
    tenant_id: "tenant-alpha",
    role,
    csrf_token: "csrf-token-12345",
    expires_at: "2026-09-30T00:00:00Z",
  });

  try {
    // -------------------------------------------------------------------------
    // Phase 1: Viewer Mode Rendering & Verification
    // -------------------------------------------------------------------------
    root.render(
      React.createElement(ReconciliationWorkspace, {
        apiBase: location.origin,
        runId,
        claimId,
        session: sessionForRole("viewer"),
        onSessionInvalid: () => {
          sessionInvalidCalls += 1;
        },
      }),
    );
    await waitFor("재무-지속가능성 공시 대조");

    // 1. Check conspicuous badges
    assert(host.textContent.includes("합성 검증 데이터"), "Synthetic badge must be conspicuously displayed");
    assert(host.textContent.includes("검토 대기"), "Unreviewed pending state must be conspicuously displayed");
    assert(host.textContent.includes("정책 미승인 안내"), "Policy unapproved notice must be conspicuously displayed");
    assert(host.textContent.includes("차단됨"), "Blocked result status must be conspicuously displayed");

    // 2. Check NEVER grade or rating
    const textLower = host.textContent.toLowerCase();
    assert(!/\b(e0|e1|e2|e3)\b/i.test(host.textContent), "No evidence grades (E0-E3) allowed in reconciliation UI");
    assert(!host.textContent.includes("등급"), "No grade/rating labels allowed in reconciliation UI");

    // 3. Check paired values (SR vs Financial)
    assert(host.textContent.includes("1,234,567 tCO2e"), "SR original value must be displayed");
    assert(host.textContent.includes("1,234,000 tCO2e"), "Financial original value must be displayed");

    // 4. Check candidates visible as unverified
    assert(host.textContent.includes("미검증 후보"), "Extracted candidates must be marked unverified");
    assert(!host.textContent.includes("검증된 출처"), "Blocked results must not certify draft source assertions");

    // 5. Check authorized attachment link
    const attachmentLink = host.querySelector("a.source-attachment-link");
    assert(attachmentLink !== null, "Source attachment download link must be rendered");
    assert(
      attachmentLink.href.includes(`/v1/reconciliation/cases/${caseId}/sources/`),
      "Attachment link must point to authorized HTTP endpoint",
    );
    assert(!attachmentLink.href.startsWith("javascript:"), "Attachment link must never be javascript: or unsafe");

    // 6. Check immutable revision export link
    const exportLink = host.querySelector("a.export-revision-link");
    assert(exportLink !== null, "Revision export link must be rendered");
    assert(
      exportLink.href.includes(`/v1/reconciliation/cases/${caseId}/revisions/1`),
      "Export link must point to immutable revision snapshot",
    );

    // 7. Viewer role check: Unauthorized buttons HIDDEN
    assert(!button("검토 확인 제출"), "Reviewer submit button must be hidden for viewer");
    assert(!button("정책 승인 상태 업데이트"), "Admin policy approval button must be hidden for viewer");
    assert(!button("대조 재평가 실행"), "Editor evaluate button must be hidden for viewer");

    // -------------------------------------------------------------------------
    // Phase 2: Reviewer Role & Confirmation Form
    // -------------------------------------------------------------------------
    root.render(
      React.createElement(ReconciliationWorkspace, {
        apiBase: location.origin,
        runId,
        claimId,
        session: sessionForRole("reviewer"),
        onSessionInvalid: () => {
          sessionInvalidCalls += 1;
        },
      }),
    );
    await waitSelector(".review-submit-btn");

    // Editor & Admin buttons still hidden
    assert(!button("정책 승인 상태 업데이트"), "Admin policy approval button must be hidden for reviewer");
    assert(!button("대조 재평가 실행"), "Editor evaluate button must be hidden for reviewer");

    // Reviewer form elements
    const checkboxes = host.querySelectorAll('input[type="checkbox"]');
    assert(checkboxes.length >= 3, "Reviewer must have source, decision, and coverage checkboxes");

    // Check confirm_source_bindings, confirm_decision_bindings, confirm_search_coverage
    checkboxes[0].click();
    checkboxes[1].click();
    checkboxes[2].click();
    await flush();

    const textarea = host.querySelector("textarea#review-reason");
    assert(textarea !== null, "Review reason textarea must be rendered");
    input(textarea, "출처 및 결정 바인딩 사실관계 검토 완료");
    await flush();

    const reviewBtn = button("검토 확인 제출");
    assert(reviewBtn && !reviewBtn.disabled, "Review submit button must be enabled when form is complete");
    reviewBtn.click();
    await waitFor("검토 확인이 성공적으로 기록되었습니다.");

    assert(reviewCalls.length === 1, "Review API must be called once");
    assert(reviewCalls[0].headers.get("X-CSRF-Token") === "csrf-token-12345", "Review must pass CSRF token");
    assert(reviewCalls[0].headers.get("If-Match") === '"1"', "Review must pass If-Match head revision");
    const firstIdemKey = reviewCalls[0].headers.get("Idempotency-Key");
    assert(Boolean(firstIdemKey), "Review must pass Idempotency-Key");
    assert(reviewCalls[0].body.confirm_source_bindings === true, "confirm_source_bindings must be true");
    assert(reviewCalls[0].body.confirm_decision_bindings === true, "confirm_decision_bindings must be true");
    assert(reviewCalls[0].body.confirm_search_coverage === true, "confirm_search_coverage must be boolean");

    // -------------------------------------------------------------------------
    // Phase 3: Admin Role & Policy Approval Action
    // -------------------------------------------------------------------------
    root.render(
      React.createElement(ReconciliationWorkspace, {
        apiBase: location.origin,
        runId,
        claimId,
        session: sessionForRole("admin"),
        onSessionInvalid: () => {
          sessionInvalidCalls += 1;
        },
      }),
    );
    await waitSelector(".policy-submit-btn");

    // Check policy scope / threshold displays NO default values
    assert(host.textContent.includes("임계값 지정 없음"), "Policy threshold must display '임계값 지정 없음' when threshold is null (no default values)");
    assert(host.textContent.includes("연결 공시 기준"), "Policy scope must be displayed");

    const policyCheckbox = host.querySelector('.policy-admin-section input[type="checkbox"]');
    assert(policyCheckbox !== null, "Policy approval checkbox must exist");
    if (!policyCheckbox.checked) policyCheckbox.click();

    const policyReasonInput = host.querySelector("input#policy-reason");
    input(policyReasonInput, "관리자 ESG 공시 정책 정식 승인");
    await flush();

    const policySubmitBtn = button("정책 승인 상태 업데이트");
    assert(policySubmitBtn && !policySubmitBtn.disabled, "Policy submit button must be enabled");
    policySubmitBtn.click();
    await waitFor("정책 승인 상태가 업데이트되었습니다.");

    assert(policyCalls.length === 1, "Policy approval API must be called once");
    assert(policyCalls[0].headers.get("If-Match") === '"2"', "Policy approval must pass current head revision");
    assert(policyCalls[0].body.approved === true, "Policy approved must be true");

    // -------------------------------------------------------------------------
    // Phase 4: Editor Role & Evaluate Action
    // -------------------------------------------------------------------------
    root.render(
      React.createElement(ReconciliationWorkspace, {
        apiBase: location.origin,
        runId,
        claimId,
        session: sessionForRole("editor"),
        onSessionInvalid: () => {
          sessionInvalidCalls += 1;
        },
      }),
    );
    await waitSelector(".evaluate-btn");

    const evaluateBtn = button("대조 재평가 실행");
    assert(evaluateBtn !== null, "Evaluate button must be visible for editor");
    evaluateBtn.click();
    await waitFor("대조 재평가가 완료되었습니다.");
    assert(host.textContent.includes("검증된 출처"), "Only completed result source IDs are verified");
    assert(host.textContent.includes("대조 또는 차이 설명 확인"), "Strict matched status must render correctly");

    assert(evaluateCalls.length === 1, "Evaluate API must be called once");
    assert(evaluateCalls[0].headers.get("X-CSRF-Token") === "csrf-token-12345", "Evaluate must pass CSRF");
    assert(evaluateCalls[0].headers.get("If-Match") === '"3"', "Evaluate must pass head revision");
    assert(Boolean(evaluateCalls[0].headers.get("Idempotency-Key")), "Evaluate must pass Idempotency-Key");

    mode = "transport-once";
    button("대조 재평가 실행").click();
    await waitFor("최신 상태 다시 불러오기");
    const failedKey = evaluateCalls.at(-1).headers.get("Idempotency-Key");
    button("대조 재평가 실행").click();
    await waitFor("대조 재평가가 완료되었습니다.");
    assert(evaluateCalls.at(-1).headers.get("Idempotency-Key") === failedKey, "Unchanged retry must reuse idempotency key");

    mode = "conflict";
    button("대조 재평가 실행").click();
    await waitFor("최신 상태 다시 불러오기");
    assert(!host.textContent.includes("대조 재평가가 완료되었습니다."), "409 must not display mutation success");
    mode = "expired";
    button("대조 재평가 실행").click();
    for (let i = 0; i < 100 && sessionInvalidCalls === 0; i++) await flush();
    assert(sessionInvalidCalls === 1, "401 must invalidate session");

    // -------------------------------------------------------------------------
    // Phase 5: Tenant / Run Switch & Stale Response Guard
    // -------------------------------------------------------------------------
    mode = "late-load";
    root.render(React.createElement(ReconciliationWorkspace, {
      apiBase: location.origin, runId, claimId,
      session: { ...sessionForRole("viewer"), csrf_token: "new-scope-token" },
      onSessionInvalid: () => { sessionInvalidCalls += 1; },
    }));
    for (let i = 0; i < 100 && !releaseLate; i++) await flush();
    assert(typeof releaseLate === "function", "Old-scope request must actually be pending");
    const initialSignal = lateSignal;
    root.render(
      React.createElement(ReconciliationWorkspace, {
        apiBase: location.origin,
        runId: "switched-run-id",
        claimId: "switched-claim-id",
        session: { ...sessionForRole("viewer"), tenant_id: "tenant-beta" },
        onSessionInvalid: () => {
          sessionInvalidCalls += 1;
        },
      }),
    );
    await flush();
    assert(initialSignal?.aborted, "Switching tenant/run must abort in-flight requests");
    releaseLate();
    await flush();
    assert(!host.textContent.includes("1,234,567"), "Late old-tenant data must never reappear");

    return {
      viewer_mode_rendered: true,
      conspicuous_badges: true,
      paired_values_compared: true,
      unverified_candidates_flagged: true,
      safe_attachment_links: true,
      immutable_revision_export_linked: true,
      unauthorized_buttons_hidden: true,
      reviewer_confirmation_submitted: true,
      admin_policy_approved_no_defaults: true,
      editor_evaluate_executed: true,
      stale_responses_aborted: true,
      no_grades_or_ratings: true,
      unchanged_retry_key_preserved: true,
      revision_conflict_visible: true,
      expired_session_invalidated: true,
      late_private_data_discarded: true,
    };
  } finally {
    root.unmount();
    host.remove();
    window.fetch = originalFetch;
  }
}
