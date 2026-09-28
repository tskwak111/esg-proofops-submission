import { useEffect, useRef, useState } from "react";
import { ComparisonPage, type Comparison, type PriorVersion } from "./ComparisonPage";
import { ApiError, errorMessage, isSessionError, requestJson } from "../session/api";

type Props = {
  apiBase?: string;
  csrfToken: string;
  tenantKey: string;
  runId: string;
  canCompare: boolean;
  onSessionInvalid: () => void;
};

type Run = { document_version_id: string };
type Document = { document_id: string; company_id: string; title: string };
type DocumentVersion = {
  version_id: string;
  document_id: string;
  report_year: number;
  status: "validating" | "ready" | "rejected";
};
type Page<T> = { items: T[]; next_cursor: string | null };
type JobAccepted = {
  job_id: string;
  resource_id: string;
  status: "queued" | "running" | "ready";
  status_url: string;
};
type Scope = { key: string; controller: AbortController };

const catalogLimit = 10_000;
const pollIntervalMs = 500;
const maxPolls = 20;

async function allPages<T>(
  apiBase: string,
  path: string,
  signal: AbortSignal,
  label: string,
): Promise<T[]> {
  const items: T[] = [];
  const cursors = new Set<string>();
  let cursor: string | null = null;
  do {
    const query = new URLSearchParams({ limit: "100" });
    if (cursor) query.set("cursor", cursor);
    const page = await requestJson<Page<T>>(`${apiBase}${path}?${query}`, { signal });
    if (page.items.length > catalogLimit - items.length) {
      throw new Error(`${label}은 ${catalogLimit.toLocaleString("ko-KR")}개까지만 불러올 수 있습니다.`);
    }
    items.push(...page.items);
    cursor = page.next_cursor;
    if (cursor && cursors.has(cursor)) throw new Error(`${label} 페이지를 계속 불러올 수 없습니다.`);
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return items;
}

function pause(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(done, pollIntervalMs);
    function done() {
      signal.removeEventListener("abort", abort);
      resolve();
    }
    function abort() {
      window.clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    }
    signal.addEventListener("abort", abort, { once: true });
  });
}

export function ComparisonWorkspace({
  apiBase = "",
  csrfToken,
  tenantKey,
  runId,
  canCompare,
  onSessionInvalid,
}: Props) {
  const renderScopeKey = `${tenantKey}:${runId}:${csrfToken}`;
  const [currentYear, setCurrentYear] = useState<number | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [selectedDocumentId, setSelectedDocumentId] = useState("");
  const [priorVersions, setPriorVersions] = useState<PriorVersion[]>([]);
  const [selectedPriorVersionId, setSelectedPriorVersionId] = useState("");
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [comparisonId, setComparisonId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [versionsLoading, setVersionsLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const scope = useRef<Scope | null>(null);
  const versions = useRef<AbortController | null>(null);
  const workflow = useRef<AbortController | null>(null);
  const createRequest = useRef<{ body: string; key: string } | null>(null);
  const latestScopeKey = useRef(renderScopeKey);
  latestScopeKey.current = renderScopeKey;

  const active = (candidate: Scope, request?: AbortController) =>
    scope.current === candidate && candidate.key === latestScopeKey.current
    && !candidate.controller.signal.aborted && !request?.signal.aborted;

  const clearPrivateState = () => {
    versions.current?.abort();
    workflow.current?.abort();
    setCurrentYear(null);
    setDocuments([]);
    setSelectedDocumentId("");
    setPriorVersions([]);
    setSelectedPriorVersionId("");
    setComparison(null);
    setComparisonId(null);
    setVersionsLoading(false);
    setBusy(false);
    createRequest.current = null;
  };

  const invalidateSession = () => {
    scope.current?.controller.abort();
    clearPrivateState();
    setLoading(false);
    setMessage("세션이 만료되었습니다. 다시 로그인해 주세요.");
    onSessionInvalid();
  };

  useEffect(() => {
    scope.current?.controller.abort();
    versions.current?.abort();
    workflow.current?.abort();
    const nextScope = { key: renderScopeKey, controller: new AbortController() };
    scope.current = nextScope;
    clearPrivateState();
    setLoading(true);
    setMessage(null);

    void (async () => {
      let stage: "run" | "version" | "document" | "catalog" = "run";
      try {
        const run = await requestJson<Run>(`${apiBase}/v1/runs/${runId}`, {
          signal: nextScope.controller.signal,
        });
        stage = "version";
        const current = await requestJson<DocumentVersion>(
          `${apiBase}/v1/versions/${run.document_version_id}`,
          { signal: nextScope.controller.signal },
        );
        if (current.status !== "ready") {
          throw new Error("현재 실행의 원본 문서 버전이 아직 준비되지 않았습니다.");
        }
        stage = "document";
        const sourceDocument = await requestJson<Document>(
          `${apiBase}/v1/documents/${current.document_id}`,
          { signal: nextScope.controller.signal },
        );
        stage = "catalog";
        const catalog = await allPages<Document>(
          apiBase,
          "/v1/documents",
          nextScope.controller.signal,
          "문서 목록",
        );
        if (!active(nextScope)) return;
        setCurrentYear(current.report_year);
        setDocuments(catalog.filter(item => item.company_id === sourceDocument.company_id));
      } catch (reason: unknown) {
        if (!active(nextScope)) return;
        if (isSessionError(reason)) return invalidateSession();
        if (reason instanceof ApiError && reason.status === 404) {
          setMessage({
            run: "현재 실행을 찾을 수 없습니다.",
            version: "현재 실행의 원본 문서 버전을 찾을 수 없습니다.",
            document: "현재 실행의 원본 문서를 찾을 수 없습니다.",
            catalog: "같은 기업의 문서 목록을 찾을 수 없습니다.",
          }[stage]);
        } else {
          setMessage(errorMessage(reason, "비교할 문서 정보를 불러오지 못했습니다."));
        }
      } finally {
        if (active(nextScope)) setLoading(false);
      }
    })();

    return () => {
      nextScope.controller.abort();
      versions.current?.abort();
      workflow.current?.abort();
    };
    // Tenant, run, and rotated session token form the privacy scope.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, csrfToken, runId, tenantKey]);

  const selectDocument = async (documentId: string) => {
    const currentScope = scope.current;
    if (!currentScope) return;
    versions.current?.abort();
    workflow.current?.abort();
    const controller = new AbortController();
    versions.current = controller;
    setSelectedDocumentId(documentId);
    setPriorVersions([]);
    setSelectedPriorVersionId("");
    setComparison(null);
    setComparisonId(null);
    createRequest.current = null;
    setMessage(null);
    if (!documentId || currentYear === null) return setVersionsLoading(false);
    setVersionsLoading(true);
    try {
      const catalog = await allPages<DocumentVersion>(
        apiBase,
        `/v1/documents/${encodeURIComponent(documentId)}/versions`,
        controller.signal,
        "문서 버전 목록",
      );
      if (!active(currentScope, controller)) return;
      setPriorVersions(catalog
        .filter(item => item.status === "ready" && item.report_year === currentYear - 1)
        .map(item => ({ document_version_id: item.version_id, report_year: item.report_year })));
    } catch (reason: unknown) {
      if (!active(currentScope, controller)) return;
      if (isSessionError(reason)) return invalidateSession();
      setMessage(errorMessage(reason, "선택한 문서의 버전을 불러오지 못했습니다."));
    } finally {
      if (active(currentScope, controller)) setVersionsLoading(false);
    }
  };

  const poll = async (resourceId: string, currentScope: Scope, controller: AbortController) => {
    for (let attempt = 0; attempt < maxPolls; attempt += 1) {
      const value = await requestJson<Comparison>(
        `${apiBase}/v1/comparisons/${encodeURIComponent(resourceId)}`,
        { signal: controller.signal },
      );
      if (!active(currentScope, controller)) return;
      setComparison(value);
      if (value.status !== "queued") return;
      if (attempt + 1 < maxPolls) await pause(controller.signal);
    }
    if (active(currentScope, controller)) {
      setMessage("자동 상태 확인 시간이 끝났습니다. 상태를 다시 확인해 주세요.");
    }
  };

  const createComparison = async () => {
    const currentScope = scope.current;
    if (!currentScope || !canCompare || !selectedPriorVersionId || busy) return;
    const body = JSON.stringify({ prior_document_version_id: selectedPriorVersionId });
    if (createRequest.current?.body !== body) {
      createRequest.current = { body, key: crypto.randomUUID() };
    }
    const controller = new AbortController();
    workflow.current?.abort();
    workflow.current = controller;
    setBusy(true);
    setMessage(null);
    try {
      const accepted = await requestJson<JobAccepted>(`${apiBase}/v1/runs/${runId}/comparisons`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": createRequest.current.key,
        },
        body,
      });
      if (!active(currentScope, controller)) return;
      createRequest.current = null;
      setComparisonId(accepted.resource_id);
      await poll(accepted.resource_id, currentScope, controller);
    } catch (reason: unknown) {
      if (!active(currentScope, controller)) return;
      if (isSessionError(reason)) return invalidateSession();
      setMessage(reason instanceof ApiError && reason.code === "YEAR_COMPARISON_DISABLED"
        ? "이 환경에서는 다년도 비교가 활성화되지 않았습니다."
        : errorMessage(reason, "비교를 시작하지 못했습니다. 같은 선택으로 다시 시도할 수 있습니다."));
    } finally {
      if (active(currentScope, controller)) setBusy(false);
    }
  };

  const refresh = async () => {
    const currentScope = scope.current;
    if (!currentScope || !comparisonId || busy) return;
    const controller = new AbortController();
    workflow.current?.abort();
    workflow.current = controller;
    setBusy(true);
    setMessage(null);
    try {
      await poll(comparisonId, currentScope, controller);
    } catch (reason: unknown) {
      if (!active(currentScope, controller)) return;
      if (isSessionError(reason)) return invalidateSession();
      setMessage(errorMessage(reason, "비교 상태를 다시 확인하지 못했습니다."));
    } finally {
      if (active(currentScope, controller)) setBusy(false);
    }
  };

  return (
    <section aria-label="다년도 목표 비교 작업공간">
      {!selectedDocumentId ? <h1>다년도 목표 비교</h1> : null}
      <p>현재 실행과 같은 기업의 실제 문서에서 전년 버전을 선택합니다.</p>
      {!canCompare ? (
        <p role="alert">현재 권한으로는 비교를 실행할 수 없습니다.</p>
      ) : null}
      {loading ? <p role="status">비교 가능한 문서를 불러오는 중입니다.</p> : null}
      {!loading && currentYear !== null ? (
        <label htmlFor="comparison-document">
          전년 보고서가 포함된 문서{" "}
          <select
            id="comparison-document"
            value={selectedDocumentId}
            disabled={busy || versionsLoading}
            onChange={event => void selectDocument(event.target.value)}
          >
            <option value="">문서 선택</option>
            {documents.map(item => <option key={item.document_id} value={item.document_id}>{item.title}</option>)}
          </select>
        </label>
      ) : null}
      {!loading && currentYear !== null && documents.length === 0 ? (
        <p role="status">현재 실행과 같은 기업에서 선택할 수 있는 문서가 없습니다.</p>
      ) : null}
      {versionsLoading ? <p role="status">선택한 문서의 버전을 불러오는 중입니다.</p> : null}
      {selectedDocumentId && currentYear !== null && !versionsLoading ? (
        <ComparisonPage
          currentYear={currentYear}
          priorVersions={priorVersions}
          selectedPriorVersionId={selectedPriorVersionId}
          comparison={comparison}
          busy={busy}
          onPriorVersionChange={versionId => {
            workflow.current?.abort();
            setSelectedPriorVersionId(versionId);
            setComparison(null);
            setComparisonId(null);
            setMessage(null);
            createRequest.current = null;
          }}
          onCompare={canCompare ? () => void createComparison() : undefined}
        />
      ) : null}
      {comparison?.status === "queued" && comparisonId && !busy ? (
        <button type="button" onClick={() => void refresh()} style={{ minHeight: 44 }}>상태 다시 확인</button>
      ) : null}
      {message ? <p role="alert">{message}</p> : null}
    </section>
  );
}
