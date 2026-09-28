import { useEffect, useRef, useState } from "react";
import { errorMessage, isSessionError, requestJson } from "../session/api";
import { StatusBadge } from "../../components/StatusBadge";
import { CoveragePanel, type Coverage } from "./CoveragePanel";

export type RunSnapshot = {
  run_id: string;
  document_version_id: string;
  status: "queued" | "running" | "partial" | "completed" | "failed" | "cancelled";
  current_stage: string;
  revision: number;
  mutation_epoch: number;
  coverage: Coverage;
  rule_pack_sha256: string;
  created_at: string;
};

type CostSnapshot = {
  run_id: string;
  input_tokens: number;
  output_tokens: number;
  attempt_count: number;
  cache_hit_count: number;
  amount: string | null;
  currency: "USD";
  pricing_snapshot_id: string | null;
  cost_status: "known" | "partial" | "unknown_cost";
};

type Props = {
  apiBase?: string;
  csrfToken: string;
  tenantKey: string;
  initialRun: RunSnapshot;
  onSessionInvalid: () => void;
  onRunChanged?: (run: RunSnapshot) => void;
};

const terminal = new Set<RunSnapshot["status"]>([
  "partial",
  "completed",
  "failed",
  "cancelled",
]);
const stages = [
  ["parse", "문서 파싱"],
  ["extract", "주장 추출"],
  ["tag", "근거 태깅"],
  ["review", "사람 검토"],
] as const;

function runStatus(status: RunSnapshot["status"]): string {
  return {
    queued: "대기 중",
    running: "처리 중",
    partial: "부분 완료",
    completed: "완료",
    failed: "실패",
    cancelled: "취소됨",
  }[status];
}

function stageStatus(run: RunSnapshot, stage: string): string {
  if (run.status === "completed") return "완료";
  const current = stages.findIndex(([id]) => id === run.current_stage);
  const position = stages.findIndex(([id]) => id === stage);
  if (current >= 0 && position < current) return "완료";
  if (position === current) {
    if (run.status === "running") return "대기 또는 처리 중";
    if (run.status === "queued") return "대기";
    if (run.status === "partial" && stage === "review") return "검토 대기";
    return "중단";
  }
  return "대기";
}

function costText(cost: CostSnapshot): string {
  if (cost.cost_status === "known" && cost.amount !== null) {
    return `${cost.currency} ${cost.amount}`;
  }
  if (cost.cost_status === "partial") return "일부 비용만 확인됨 · 총액 미확정";
  return "단가 미설정 · 금액 미확정";
}

export function RunProgress({
  apiBase = "",
  csrfToken,
  tenantKey,
  initialRun,
  onSessionInvalid,
  onRunChanged,
}: Props) {
  const [run, setRun] = useState(initialRun);
  const [cost, setCost] = useState<CostSnapshot | null>(null);
  const [stale, setStale] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [acting, setActing] = useState(false);
  const [pollCycle, setPollCycle] = useState(0);
  const actionRequest = useRef<{ signature: string; key: string } | null>(null);
  const actionController = useRef<AbortController | null>(null);
  const pollController = useRef<AbortController | null>(null);
  const notified = useRef(`${initialRun.revision}:${initialRun.mutation_epoch}:${initialRun.status}`);

  function publish(next: RunSnapshot) {
    const signature = `${next.revision}:${next.mutation_epoch}:${next.status}`;
    if (signature !== notified.current) {
      notified.current = signature;
      onRunChanged?.(next);
    }
  }

  useEffect(() => {
    setRun(initialRun);
    setCost(null);
    setStale(null);
    setActionError(null);
    setReason("");
    setActing(false);
    actionRequest.current = null;
    notified.current = `${initialRun.revision}:${initialRun.mutation_epoch}:${initialRun.status}`;
    return () => actionController.current?.abort();
  }, [initialRun, tenantKey]);

  useEffect(() => {
    const controller = new AbortController();
    let timer = 0;
    pollController.current = controller;

    const refresh = async () => {
      let next: RunSnapshot | null = null;
      try {
        next = await requestJson<RunSnapshot>(`${apiBase}/v1/runs/${initialRun.run_id}`, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        setRun((current) => next && next.revision >= current.revision ? next : current);
        if (next) publish(next);
        setStale(null);
        try {
          const nextCost = await requestJson<CostSnapshot>(`${apiBase}/v1/runs/${initialRun.run_id}/cost`, {
            signal: controller.signal,
          });
          if (controller.signal.aborted) return;
          setCost(nextCost);
        } catch (reason: unknown) {
          if (controller.signal.aborted) return;
          if (isSessionError(reason)) throw reason;
          setStale("비용 정보가 오래되었을 수 있습니다.");
        }
      } catch (reason: unknown) {
        if (controller.signal.aborted) return;
        if (isSessionError(reason)) {
          onSessionInvalid();
          return;
        }
        setStale(errorMessage(reason, "최신 실행 상태를 불러오지 못했습니다."));
      }
      if (!controller.signal.aborted && (!next || !terminal.has(next.status))) {
        timer = window.setTimeout(refresh, document.visibilityState === "visible" ? 2000 : 10000);
      }
    };

    void refresh();
    return () => {
      controller.abort();
      window.clearTimeout(timer);
      if (pollController.current === controller) pollController.current = null;
    };
  }, [apiBase, initialRun.run_id, onRunChanged, onSessionInvalid, pollCycle, tenantKey]);

  const act = async (action: "cancel" | "retry") => {
    const trimmed = reason.trim();
    if (acting || trimmed.length < 5) return;
    const signature = JSON.stringify({ action, revision: run.revision, reason: trimmed });
    if (actionRequest.current?.signature !== signature) {
      actionRequest.current = { signature, key: crypto.randomUUID() };
    }
    pollController.current?.abort();
    const controller = new AbortController();
    actionController.current = controller;
    setActing(true);
    setActionError(null);
    try {
      const next = await requestJson<RunSnapshot>(
        `${apiBase}/v1/runs/${run.run_id}/${action}`,
        {
          method: "POST",
          signal: controller.signal,
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrfToken,
            "Idempotency-Key": actionRequest.current.key,
            "If-Match": `"${run.revision}"`,
          },
          body: JSON.stringify({ reason: trimmed }),
        },
      );
      if (controller.signal.aborted) return;
      actionRequest.current = null;
      setReason("");
      setRun((current) => next.revision >= current.revision ? next : current);
      publish(next);
    } catch (reason: unknown) {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) {
        onSessionInvalid();
        return;
      }
      setActionError(errorMessage(reason, "실행 상태를 변경하지 못했습니다."));
    } finally {
      if (!controller.signal.aborted) {
        setActing(false);
        setPollCycle((current) => current + 1);
      }
    }
  };

  const serverTime = new Date(run.created_at);
  return (
    <section aria-labelledby="run-progress-heading" style={{ marginTop: 32 }}>
      <h2 id="run-progress-heading">분석 진행</h2>
      <p role="status" aria-live="polite">
        현재 상태: <StatusBadge label={runStatus(run.status)} tone={run.status === "completed" ? "success" : run.status === "failed" ? "danger" : "warning"} /> · 현재 단계: {stages.find(([id]) => id === run.current_stage)?.[1] ?? "후속 처리 대기"}
      </p>
      <p>시작 시각(서버 기록): {Number.isNaN(serverTime.valueOf()) ? "알 수 없음" : serverTime.toLocaleString("ko-KR")}</p>
      {stale ? <p role="alert">{stale} 마지막으로 확인한 상태를 표시합니다.</p> : null}

      <ol aria-label="분석 단계">
        {stages.map(([id, label]) => (
          <li key={id}>{label}: {stageStatus(run, id)}</li>
        ))}
      </ol>

      <CoveragePanel status={run.status} coverage={run.coverage} />

      <section aria-labelledby="cost-heading">
        <h3 id="cost-heading">사용량과 비용</h3>
        {cost ? (
          <>
            <p>{costText(cost)}</p>
            <dl>
              <dt>입력 토큰</dt><dd>{cost.input_tokens}</dd>
              <dt>출력 토큰</dt><dd>{cost.output_tokens}</dd>
              <dt>호출 시도</dt><dd>{cost.attempt_count}</dd>
              <dt>캐시 적중</dt><dd>{cost.cache_hit_count}</dd>
            </dl>
          </>
        ) : <p role="status">비용 정보를 확인하는 중입니다.</p>}
      </section>

      {(run.status === "queued" || run.status === "running" || run.status === "failed") ? (
        <section aria-labelledby="run-action-heading">
          <h3 id="run-action-heading">실행 제어</h3>
          <label htmlFor="run-action-reason">변경 사유</label>
          <textarea
            id="run-action-reason"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
            minLength={5}
            maxLength={1000}
            aria-describedby="run-action-help"
          />
          <p id="run-action-help">5자 이상 입력하세요. 최신 실행 상태를 기준으로 요청합니다.</p>
          {run.status === "failed" ? (
            <button type="button" disabled={acting || reason.trim().length < 5} onClick={() => void act("retry")} style={{ minHeight: 44 }}>
              {acting ? "요청 중…" : "실패 단계 다시 시도"}
            </button>
          ) : (
            <button type="button" disabled={acting || reason.trim().length < 5} onClick={() => void act("cancel")} style={{ minHeight: 44 }}>
              {acting ? "요청 중…" : "분석 취소"}
            </button>
          )}
          {actionError ? <p role="alert">{actionError}</p> : null}
        </section>
      ) : null}
    </section>
  );
}
