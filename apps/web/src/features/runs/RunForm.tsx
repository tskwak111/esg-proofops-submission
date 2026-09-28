import { FormEvent, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage, isSessionError, requestJson } from "../session/api";
import type { RuntimeOptions, UploadSelection } from "../upload/CompanySelector";
import type { ReadyDocumentVersion } from "../upload/UploadForm";
import type { RunSnapshot } from "./RunProgress";

type Preflight = {
  ready: boolean;
  checks: Array<{ name: string; status: "pass" | "fail" | "not_run"; reason: string }>;
  binding_sha256: string | null;
  checked_at: string;
};

type LocalSubmission = {
  worker_enabled: boolean;
  candidate_rule_pack_id: string | null;
  selected_pages: number[];
};

type Props = {
  apiBase?: string;
  csrfToken: string;
  tenantKey: string;
  version: ReadyDocumentVersion;
  selection: UploadSelection;
  options: RuntimeOptions;
  canRun: boolean;
  onRunCreated: (run: RunSnapshot) => void;
  onSessionInvalid: () => void;
};

function parsePages(value: string, total: number): number[] {
  const parts = value.split(",").map((part) => part.trim());
  if (!value.trim() || parts.some((part) => !/^[1-9][0-9]*$/.test(part))) {
    throw new Error("분석할 페이지를 쉼표로 구분해 입력해 주세요. 예: 1, 3, 5");
  }
  const pages = parts.map(Number);
  if (pages.some((page) => page > total)) {
    throw new Error(`실제 문서 범위인 1~${total}페이지만 선택할 수 있습니다.`);
  }
  if (new Set(pages).size !== pages.length || pages.some((page, index) => index > 0 && pages[index - 1] > page)) {
    throw new Error("페이지는 중복 없이 오름차순으로 입력해 주세요.");
  }
  return pages;
}

export function RunForm({
  apiBase = "",
  csrfToken,
  tenantKey,
  version,
  selection,
  options,
  canRun,
  onRunCreated,
  onSessionInvalid,
}: Props) {
  const [mode, setMode] = useState<"disclosure" | "advertising">("disclosure");
  const [scope, setScope] = useState<"full" | "declared_subset">("full");
  const [pageText, setPageText] = useState("");
  const [rulePackId, setRulePackId] = useState("");
  const [local, setLocal] = useState<LocalSubmission | null>(null);
  const [localLoading, setLocalLoading] = useState(true);
  const [localError, setLocalError] = useState(false);
  const [phase, setPhase] = useState("준비된 문서 버전의 분석 범위를 확인해 주세요.");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const request = useRef<{ signature: string; preflightKey: string; runKey: string } | null>(null);
  const inFlight = useRef(false);
  const activePacks = options.rule_packs.filter(
    (pack) => pack.status === "active" && pack.mode === mode,
  );
  const advertisingReady = options.enabled_modes.includes("advertising") &&
    options.rule_packs.some((pack) => pack.status === "active" && pack.mode === "advertising");

  useEffect(() => {
    const request = new AbortController();
    setLocal(null); setLocalLoading(true); setLocalError(false);
    requestJson<LocalSubmission>(`${apiBase}/local/submission`, { signal: request.signal })
      .then(value => {
        if (request.signal.aborted) return;
        if (typeof value.worker_enabled !== "boolean" ||
            !(value.candidate_rule_pack_id === null || typeof value.candidate_rule_pack_id === "string") ||
            !Array.isArray(value.selected_pages) || !value.selected_pages.every(p => Number.isInteger(p) && p > 0)) {
          throw new Error("로컬 실행 설정을 확인할 수 없습니다.");
        }
        setLocal(value);
        if (value.selected_pages.length) {
          setScope("declared_subset"); setPageText(value.selected_pages.join(","));
        }
      })
      .catch(reason => {
        if (request.signal.aborted) return;
        if (isSessionError(reason)) onSessionInvalid();
        if (!(reason instanceof ApiError && reason.status === 404)) setLocalError(true);
      })
      .finally(() => { if (!request.signal.aborted) setLocalLoading(false); });
    return () => request.abort();
  }, [apiBase, tenantKey, version.version_id, onSessionInvalid]);

  const candidatePackId = mode === "disclosure" ? local?.candidate_rule_pack_id : null;
  const localBlocked = localLoading || localError || local?.worker_enabled === false;
  useEffect(() => {
    if (rulePackId === candidatePackId && candidatePackId) return;
    if (!activePacks.some((pack) => pack.rule_pack_id === rulePackId)) {
      setRulePackId(activePacks[0]?.rule_pack_id ?? candidatePackId ?? "");
    }
  }, [activePacks, candidatePackId, rulePackId]);

  useEffect(() => {
    controller.current?.abort();
    inFlight.current = false;
    request.current = null;
    setMode("disclosure");
    setScope("full");
    setPageText("");
    setError(null);
    setBusy(false);
    setPhase("준비된 문서 버전의 분석 범위를 확인해 주세요.");
    return () => controller.current?.abort();
  }, [tenantKey, version.version_id]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (inFlight.current || localBlocked) return;
    if (!canRun) {
      setError("분석 전 사전 점검은 관리자 권한이 필요합니다.");
      return;
    }
    if (version.page_count === null) {
      setError("서버가 문서 페이지 수를 확인한 뒤 분석을 시작할 수 있습니다.");
      return;
    }
    if (!selection.consentProfileId || !selection.runtimeBindingId || !rulePackId) {
      setError("승인된 동의·실행 환경과 사용할 규칙집을 선택해 주세요.");
      return;
    }
    let pages: number[] | undefined;
    try {
      pages = scope === "declared_subset" ? parsePages(pageText, version.page_count) : undefined;
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "분석 페이지를 확인해 주세요.");
      return;
    }

    const signature = JSON.stringify({
      version: version.version_id,
      mode,
      scope,
      pages,
      rulePackId,
      consentProfileId: selection.consentProfileId,
      runtimeBindingId: selection.runtimeBindingId,
    });
    if (request.current?.signature !== signature) {
      request.current = {
        signature,
        preflightKey: crypto.randomUUID(),
        runKey: crypto.randomUUID(),
      };
    }
    const abortController = new AbortController();
    controller.current?.abort();
    controller.current = abortController;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      setPhase("승인된 실행 환경과 데이터 처리 동의를 사전 점검하는 중입니다.");
      const preflight = await requestJson<Preflight>(`${apiBase}/v1/preflight`, {
        method: "POST",
        signal: abortController.signal,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": request.current.preflightKey,
        },
        body: JSON.stringify({
          runtime_binding_id: selection.runtimeBindingId,
          consent_profile_id: selection.consentProfileId,
          include_live_model_probe: false,
        }),
      });
      if (!preflight.ready) {
        setError("사전 점검을 통과하지 못했습니다. 관리자에게 승인 구성을 확인해 달라고 요청하세요.");
        setPhase("분석 실행을 시작하지 않았습니다.");
        return;
      }
      setPhase("사전 점검을 통과했습니다. 분석 실행을 등록하는 중입니다.");
      const run = await requestJson<RunSnapshot>(`${apiBase}/v1/runs`, {
        method: "POST",
        signal: abortController.signal,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": request.current.runKey,
        },
        body: JSON.stringify({
          document_version_id: version.version_id,
          mode,
          scope,
          ...(pages ? { selected_pages: pages } : {}),
          rule_pack_id: rulePackId,
          consent_profile_id: selection.consentProfileId,
          runtime_binding_id: selection.runtimeBindingId,
        }),
      });
      request.current = null;
      setPhase("분석 실행이 등록되었습니다.");
      onRunCreated(run);
    } catch (reason: unknown) {
      if (abortController.signal.aborted) return;
      if (isSessionError(reason)) {
        onSessionInvalid();
        return;
      }
      setError(errorMessage(reason, "분석 실행을 시작하지 못했습니다. 같은 설정으로 다시 시도해 주세요."));
      setPhase("분석 실행을 시작하지 않았습니다.");
    } finally {
      if (!abortController.signal.aborted) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  };

  return (
    <form onSubmit={submit} aria-label="문서 분석 실행" style={{ marginTop: 32 }}>
      <fieldset disabled={busy} aria-busy={busy} style={{ display: "grid", gap: 8 }}>
        <legend>분석 실행</legend>
        <p>준비된 문서: {version.page_count === null ? "페이지 수 확인 중" : `${version.page_count}페이지`}</p>
        <label htmlFor="run-mode">검토 모드</label>
        <select id="run-mode" value={mode} onChange={(event) => setMode(event.target.value as typeof mode)}>
          <option value="disclosure">공시 검토</option>
          <option value="advertising" disabled={!advertisingReady}>광고 문구 검토</option>
        </select>
        {!advertisingReady ? <p role="status">광고 문구 검토는 승인된 전용 규칙집이 없어 시작할 수 없습니다.</p> : null}

        {localLoading ? <p role="status">실행 가능 상태를 확인하고 있습니다.</p> : null}
        {localError ? <p role="alert">로컬 실행 상태를 확인하지 못했습니다. 화면을 새로고침해 주세요.</p> : null}
        {local?.worker_enabled === false ? <p role="status">현재는 저장 결과 열람 모드입니다. 분석 워커를 켠 뒤 새 분석을 시작할 수 있습니다.</p> : null}
        <label htmlFor="rule-pack">규칙집</label>
        <select id="rule-pack" value={rulePackId} onChange={(event) => setRulePackId(event.target.value)} disabled={activePacks.length === 0 && !candidatePackId} required>
          <option value="">현재 모드의 활성 규칙집을 선택하세요</option>
          {candidatePackId && !activePacks.some(pack => pack.rule_pack_id === candidatePackId) ? <option value={candidatePackId}>초안 기준 · 태깅만 수행 / 등급 보류</option> : null}
          {activePacks.map((pack) => <option key={pack.rule_pack_id} value={pack.rule_pack_id}>{pack.version}</option>)}
        </select>
        {activePacks.length === 0 && !candidatePackId ? <p role="status">현재 모드에 사용할 활성 규칙집이 없습니다.</p> : null}
        {rulePackId === candidatePackId && candidatePackId ? <p role="status">초안은 추출·태깅 참고용입니다. 승인 전에는 등급과 검토 수정 확정을 보류합니다.</p> : null}

        <label htmlFor="run-scope">분석 범위</label>
        <select id="run-scope" value={scope} onChange={(event) => setScope(event.target.value as typeof scope)}>
          <option value="full">문서 전체</option>
          <option value="declared_subset">지정한 페이지만</option>
        </select>
        {scope === "declared_subset" ? (
          <>
            <label htmlFor="selected-pages">분석할 실제 PDF 페이지</label>
            <input
              id="selected-pages"
              value={pageText}
              onChange={(event) => setPageText(event.target.value)}
              inputMode="numeric"
              pattern="[0-9, ]+"
              placeholder="예: 1, 3, 5"
              aria-describedby="selected-pages-help"
              required
            />
            <p id="selected-pages-help">PDF 파일의 1부터 {version.page_count ?? "확인된 마지막"} 페이지까지, 중복 없이 오름차순으로 입력하세요.</p>
          </>
        ) : null}
        <button type="submit" disabled={localBlocked || !canRun || version.page_count === null || !rulePackId || !selection.consentProfileId || !selection.runtimeBindingId} style={{ minHeight: 44 }}>
          {busy ? "확인 중…" : "사전 점검 후 분석 시작"}
        </button>
      </fieldset>
      {!canRun ? <p role="status">분석 전 사전 점검과 실행 시작은 관리자 권한이 필요합니다.</p> : null}
      <p role="status" aria-live="polite">{phase}</p>
      {error ? <p role="alert">{error}</p> : null}
    </form>
  );
}
