import { FormEvent, useEffect, useRef, useState } from "react";
import { errorMessage, isSessionError, requestJson } from "../session/api";

type ExportFormat = "json" | "csv" | "html";
type ExportSnapshot = {
  export_id: string;
  run_id: string;
  state: "queued" | "building" | "ready" | "failed";
  snapshot_epoch: number;
  partial: boolean;
  manifest_sha256: string | null;
  created_at: string;
};
type Download = { url: string; expires_at: string; sha256: string };
type TrackedExport = { snapshot: ExportSnapshot; source: "created" | "known" };
type DownloadLink = Download & { href: string; usableUntil: number; reissues: number };

type Props = {
  apiBase?: string;
  csrfToken: string;
  tenantKey: string;
  runId: string;
  onSessionInvalid: () => void;
};

const formats = [
  ["json", "JSON"],
  ["csv", "CSV"],
  ["html", "HTML"],
] as const;
const terminalStates = new Set<ExportSnapshot["state"]>(["ready", "failed"]);
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const pollIntervalMs = 500;
const maxPolls = 20;
const privateLinkMs = 5 * 60 * 1000;

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

function apiOrigin(apiBase: string): string {
  return new URL(apiBase || window.location.origin, window.location.origin).origin;
}

function trustedDownload(apiBase: string, value: string): string {
  const origin = apiOrigin(apiBase);
  const target = new URL(value, origin);
  if (target.origin !== origin || !["http:", "https:"].includes(target.protocol)) {
    throw new Error("다운로드 주소가 신뢰된 API origin과 다릅니다.");
  }
  return target.toString();
}

function clearExportQuery() {
  const page = new URL(window.location.href);
  page.searchParams.delete("export_id");
  window.history.replaceState(window.history.state, "", `${page.pathname}${page.search}${page.hash}`);
}

function stateText(state: ExportSnapshot["state"]): string {
  return { queued: "대기 중", building: "생성 중", ready: "준비됨", failed: "실패" }[state];
}

export function ExportWorkspace({ apiBase = "", csrfToken, tenantKey, runId, onSessionInvalid }: Props) {
  // Review drafts are explicit partial requests; API defaults and final-result checks are unchanged.
  const [selected, setSelected] = useState<ExportFormat[]>(["json", "csv", "html"]);
  const [allowPartial, setAllowPartial] = useState(true);
  const [tracked, setTracked] = useState<TrackedExport[]>([]);
  const [links, setLinks] = useState<Record<string, DownloadLink>>({});
  const [busy, setBusy] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [clock, setClock] = useState(Date.now());
  const createRequest = useRef<{ signature: string; key: string } | null>(null);
  const workflow = useRef<AbortController | null>(null);
  const download = useRef<AbortController | null>(null);
  const previousScope = useRef<string | null>(null);

  const clearPrivateState = () => {
    workflow.current?.abort();
    download.current?.abort();
    setTracked([]);
    setLinks({});
    setBusy(false);
    setDownloadBusy(null);
    createRequest.current = null;
  };

  const invalidateSession = () => {
    clearPrivateState();
    clearExportQuery();
    setMessage("세션이 만료되었습니다. 다시 로그인해 주세요.");
    onSessionInvalid();
  };

  const remember = (snapshot: ExportSnapshot, source: TrackedExport["source"]) => {
    if (snapshot.run_id !== runId) throw new Error("현재 실행과 다른 export 응답은 표시할 수 없습니다.");
    setTracked(current => {
      const existing = current.find(item => item.snapshot.export_id === snapshot.export_id);
      const next = { snapshot, source: existing?.source === "created" ? "created" : source };
      return [next, ...current.filter(item => item.snapshot.export_id !== snapshot.export_id)];
    });
  };

  const poll = async (initial: ExportSnapshot, controller: AbortController, source: TrackedExport["source"]) => {
    let current = initial;
    for (let attempt = 0; attempt < maxPolls && !terminalStates.has(current.state); attempt += 1) {
      await pause(controller.signal);
      current = await requestJson<ExportSnapshot>(`${apiBase}/v1/exports/${current.export_id}`, {
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      remember(current, source);
    }
    if (!controller.signal.aborted && !terminalStates.has(current.state)) {
      setMessage("자동 상태 확인 시간이 끝났습니다. 알려진 export를 다시 확인해 주세요.");
    }
  };

  const trackUntilTerminal = async (
    initial: ExportSnapshot,
    controller: AbortController,
    source: TrackedExport["source"],
  ) => {
    if (controller.signal.aborted) return;
    remember(initial, source);
    await poll(initial, controller, source);
  };

  useEffect(() => () => {
    workflow.current?.abort();
    download.current?.abort();
  }, []);

  useEffect(() => {
    const scope = `${tenantKey}:${runId}`;
    const scopeChanged = previousScope.current !== null && previousScope.current !== scope;
    previousScope.current = scope;
    clearPrivateState();
    setSelected(["json", "csv", "html"]);
    setAllowPartial(true);
    setMessage(null);

    const page = new URL(window.location.href);
    if (scopeChanged) {
      clearExportQuery();
      return;
    }
    const exportId = page.searchParams.get("export_id");
    if (!exportId || !uuid.test(exportId)) return;

    const controller = new AbortController();
    workflow.current = controller;
    setBusy(true);
    void requestJson<ExportSnapshot>(`${apiBase}/v1/exports/${exportId}`, { signal: controller.signal })
      .then(value => trackUntilTerminal(value, controller, "known"))
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        if (isSessionError(reason)) return invalidateSession();
        setMessage(errorMessage(reason, "알려진 export 상태를 불러오지 못했습니다."));
      })
      .finally(() => { if (!controller.signal.aborted) setBusy(false); });
    return () => controller.abort();
    // Props identify a new tenant/run workspace; callbacks intentionally use that snapshot.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBase, runId, tenantKey]);

  useEffect(() => {
    const nextExpiry = Object.values(links)
      .map(link => link.usableUntil)
      .filter(value => value > clock)
      .sort((left, right) => left - right)[0];
    if (!nextExpiry) return;
    const timer = window.setTimeout(() => setClock(Date.now()), Math.min(nextExpiry - clock + 25, privateLinkMs));
    return () => window.clearTimeout(timer);
  }, [clock, links]);

  const createExport = async (event?: FormEvent) => {
    event?.preventDefault();
    if (busy || selected.length === 0) return;
    const body = { formats: formats.map(([value]) => value).filter(value => selected.includes(value)), allow_partial: allowPartial };
    const signature = JSON.stringify(body);
    if (createRequest.current?.signature !== signature) {
      createRequest.current = { signature, key: crypto.randomUUID() };
    }
    const controller = new AbortController();
    workflow.current?.abort();
    workflow.current = controller;
    setBusy(true);
    setMessage(null);
    try {
      const value = await requestJson<ExportSnapshot>(`${apiBase}/v1/runs/${runId}/exports`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": createRequest.current.key,
        },
        body: signature,
      });
      if (controller.signal.aborted) return;
      if (value.run_id !== runId) throw new Error("현재 실행과 다른 export 응답은 표시할 수 없습니다.");
      createRequest.current = null;
      const page = new URL(window.location.href);
      page.searchParams.set("export_id", value.export_id);
      window.history.replaceState(window.history.state, "", `${page.pathname}${page.search}${page.hash}`);
      await trackUntilTerminal(value, controller, "created");
    } catch (reason: unknown) {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) return invalidateSession();
      setMessage(errorMessage(reason, "내보내기를 생성하지 못했습니다."));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const refresh = async (snapshot: ExportSnapshot, source: TrackedExport["source"]) => {
    if (busy) return;
    const controller = new AbortController();
    workflow.current?.abort();
    workflow.current = controller;
    setBusy(true);
    setMessage(null);
    try {
      const value = await requestJson<ExportSnapshot>(`${apiBase}/v1/exports/${snapshot.export_id}`, {
        signal: controller.signal,
      });
      if (!controller.signal.aborted) await trackUntilTerminal(value, controller, source);
    } catch (reason: unknown) {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) return invalidateSession();
      setMessage(errorMessage(reason, "export 상태를 다시 확인하지 못했습니다."));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  };

  const issueDownload = async (snapshot: ExportSnapshot, reissue: boolean) => {
    const existing = links[snapshot.export_id];
    if (snapshot.state !== "ready" || downloadBusy || (reissue && (!existing || existing.reissues >= 1 || existing.usableUntil > Date.now()))) return;
    const controller = new AbortController();
    download.current?.abort();
    download.current = controller;
    setDownloadBusy(snapshot.export_id);
    setMessage(null);
    try {
      const ticket = await requestJson<Download>(`${apiBase}/v1/exports/${snapshot.export_id}/download`, {
        method: "POST",
        signal: controller.signal,
        headers: { "X-CSRF-Token": csrfToken },
      });
      if (controller.signal.aborted) return;
      const expiresAt = Date.parse(ticket.expires_at);
      if (Number.isNaN(expiresAt)) throw new Error("다운로드 만료 시각이 올바르지 않습니다.");
      const now = Date.now();
      const href = trustedDownload(apiBase, ticket.url);
      setLinks(current => ({
        ...current,
        [snapshot.export_id]: {
          ...ticket,
          href,
          usableUntil: Math.min(expiresAt, now + privateLinkMs),
          reissues: reissue ? existing.reissues + 1 : 0,
        },
      }));
      setClock(now);
    } catch (reason: unknown) {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) return invalidateSession();
      setMessage(errorMessage(reason, "비공개 다운로드 링크를 발급하지 못했습니다."));
    } finally {
      if (!controller.signal.aborted) setDownloadBusy(null);
    }
  };

  const renderExport = ({ snapshot, source }: TrackedExport) => {
    const link = links[snapshot.export_id];
    const expired = Boolean(link && link.usableUntil <= clock);
    return (
      <li key={snapshot.export_id}>
        <h3><code>{snapshot.export_id}</code></h3>
        <p>{snapshot.partial ? "검토용 부분 스냅샷" : "최종 스냅샷"} · {stateText(snapshot.state)}</p>
        <p>epoch {snapshot.snapshot_epoch}</p>
        <p>manifest hash: <code>{snapshot.manifest_sha256 ?? "생성 전"}</code></p>
        {!terminalStates.has(snapshot.state) ? (
          <button type="button" disabled={busy} onClick={() => void refresh(snapshot, source)} style={{ minHeight: 44 }}>상태 다시 확인</button>
        ) : null}
        {snapshot.state === "failed" ? <p role="alert">export 생성에 실패했습니다. 저장된 결과는 다운로드할 수 없습니다.</p> : null}
        {snapshot.state === "ready" && !link ? (
          <button type="button" disabled={downloadBusy === snapshot.export_id} onClick={() => void issueDownload(snapshot, false)} style={{ minHeight: 44 }}>
            {downloadBusy === snapshot.export_id ? "링크 발급 중…" : "다운로드 링크 발급"}
          </button>
        ) : null}
        {snapshot.state === "ready" && link && !expired ? (
          <p><a href={link.href} target="_blank" rel="noreferrer">비공개 다운로드 열기</a> · 최대 5분 유효 · SHA-256 <code>{link.sha256}</code></p>
        ) : null}
        {snapshot.state === "ready" && link && expired ? (
          link.reissues < 1
            ? <button type="button" disabled={downloadBusy === snapshot.export_id} onClick={() => void issueDownload(snapshot, true)} style={{ minHeight: 44 }}>만료된 링크 1회 재발급</button>
            : <p role="alert">재발급한 링크도 만료되었습니다. 새 export를 생성해 주세요.</p>
        ) : null}
      </li>
    );
  };

  const created = tracked.filter(item => item.source === "created");
  const known = tracked.filter(item => item.source === "known");
  return (
    <section aria-labelledby="export-heading">
      <h1 id="export-heading">리포트 내보내기</h1>
      <p>현재 실행의 불변 스냅샷을 JSON, CSV 또는 HTML 감사 꾸러미로 생성합니다.</p>
      <form onSubmit={createExport}>
        <fieldset>
          <legend>내보낼 형식</legend>
          {formats.map(([value, label]) => (
            <label key={value} style={{ marginRight: 16 }}>
              <input
                type="checkbox"
                name="formats"
                value={value}
                checked={selected.includes(value)}
                onChange={event => setSelected(current => event.target.checked
                  ? [...current, value]
                  : current.filter(item => item !== value))}
              /> {label}
            </label>
          ))}
        </fieldset>
        <label>
          <input name="allow-partial" type="checkbox" checked={allowPartial} onChange={event => setAllowPartial(event.target.checked)} /> 부분 결과 허용
        </label>
        <p>미완료 항목을 포함한 검토용 결과입니다. 최종본이 아닙니다. 체크를 해제하면 완료된 결과만 내보냅니다.</p>
        {selected.length === 0 ? <p role="alert">형식을 하나 이상 선택하세요.</p> : null}
        <button type="submit" disabled={busy || selected.length === 0} style={{ minHeight: 44 }}>
          {busy ? "처리 중…" : createRequest.current ? "다시 시도" : created.length ? "새 내보내기 생성" : "내보내기 생성"}
        </button>
      </form>
      {message ? <p role="alert">{message}</p> : null}

      <section aria-labelledby="created-export-heading">
        <h2 id="created-export-heading">이 화면에서 생성한 내보내기</h2>
        {created.length ? <ol>{created.map(renderExport)}</ol> : <p>아직 이 화면에서 생성한 내보내기가 없습니다.</p>}
      </section>
      {known.length ? (
        <section aria-labelledby="known-export-heading">
          <h2 id="known-export-heading">URL로 다시 연 내보내기</h2>
          <ul>{known.map(renderExport)}</ul>
        </section>
      ) : null}
    </section>
  );
}
