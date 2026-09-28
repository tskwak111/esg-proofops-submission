import { FormEvent, useEffect, useRef, useState } from "react";
import { ApiError, errorMessage, isSessionError, requestJson } from "../session/api";
import type { UploadSelection } from "./CompanySelector";

const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;
const UUID = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}";

type UploadTicket = {
  upload_id: string;
  document_id: string;
  post_url: string;
  post_fields: Record<string, string>;
};

type JobAccepted = {
  resource_id: string;
  status: "queued" | "running" | "ready";
  status_url: string;
};

export type ReadyDocumentVersion = {
  version_id: string;
  status: "validating" | "ready" | "rejected";
  page_count: number | null;
};

type Workflow = {
  signature: string;
  documentKey: string;
  versionKey: string;
  completeKey: string;
  documentId?: string;
  ticket?: UploadTicket;
  uploaded?: boolean;
  statusUrl?: string;
};

type Props = {
  apiBase?: string;
  uploadOrigin?: string;
  csrfToken: string;
  tenantKey: string;
  selection: UploadSelection;
  canEdit: boolean;
  onReadyVersion: (version: ReadyDocumentVersion | null) => void;
  onSessionInvalid: () => void;
};

function apiOrigin(apiBase: string): string {
  return new URL(apiBase || window.location.origin, window.location.origin).origin;
}

function uploadTarget(postUrl: string, apiBase: string, configuredOrigin?: string) {
  const ownerOrigin = apiOrigin(apiBase);
  const target = new URL(postUrl, `${ownerOrigin}/`);
  if (target.username || target.password) throw new Error("승인되지 않은 업로드 전송 주소를 거부했습니다.");
  const localPath =
    new RegExp(`^/local/uploads/${UUID}/content$`).test(target.pathname) &&
    !target.search &&
    !target.hash;
  if (target.origin === ownerOrigin && localPath) return { url: target.toString(), local: true };
  if (configuredOrigin) {
    let approved: URL;
    try {
      approved = new URL(configuredOrigin);
    } catch {
      throw new Error("승인된 업로드 전송 주소 구성이 올바르지 않습니다.");
    }
    if (
      approved.protocol === "https:" &&
      approved.pathname === "/" &&
      !approved.search &&
      !approved.hash &&
      !approved.username &&
      !approved.password &&
      target.origin === approved.origin
    ) return { url: target.toString(), local: false };
  }
  throw new Error("승인되지 않은 업로드 전송 주소를 거부했습니다.");
}

function versionStatusUrl(statusUrl: string, apiBase: string): string {
  const ownerOrigin = apiOrigin(apiBase);
  const target = new URL(statusUrl, `${ownerOrigin}/`);
  if (
    target.origin !== ownerOrigin ||
    target.username ||
    target.password ||
    target.search ||
    target.hash ||
    !new RegExp(`^/v1/versions/${UUID}$`).test(target.pathname)
  ) {
    throw new Error("서버가 안전한 문서 검증 주소를 반환하지 않았습니다.");
  }
  return target.toString();
}

async function sha256(file: File): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", await file.arrayBuffer());
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function wait(signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, 1000);
    signal.addEventListener(
      "abort",
      () => {
        window.clearTimeout(timer);
        reject(new DOMException("Aborted", "AbortError"));
      },
      { once: true },
    );
  });
}

export function UploadForm({
  apiBase = "",
  uploadOrigin,
  csrfToken,
  tenantKey,
  selection,
  canEdit,
  onReadyVersion,
  onSessionInvalid,
}: Props) {
  const [title, setTitle] = useState("");
  const [documentType, setDocumentType] = useState<"sustainability_report" | "annual_report_section">(
    "sustainability_report",
  );
  const [reportYear, setReportYear] = useState(new Date().getFullYear() - 1);
  const [periodStart, setPeriodStart] = useState("");
  const [periodEnd, setPeriodEnd] = useState("");
  const [industrySystem, setIndustrySystem] = useState<"gics" | "sasb" | "custom" | "unknown">(
    "unknown",
  );
  const [industryCode, setIndustryCode] = useState("");
  const [consolidationScope, setConsolidationScope] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState("PDF와 메타데이터를 선택해 주세요.");
  const [error, setError] = useState<string | null>(null);
  const [version, setVersion] = useState<ReadyDocumentVersion | null>(null);
  const workflow = useRef<Workflow | null>(null);
  const controller = useRef<AbortController | null>(null);
  const inFlight = useRef(false);
  const previousTenant = useRef(tenantKey);

  useEffect(() => () => controller.current?.abort(), []);

  useEffect(() => {
    const changed = previousTenant.current !== tenantKey;
    previousTenant.current = tenantKey;
    controller.current?.abort();
    workflow.current = null;
    inFlight.current = false;
    setTitle("");
    setDocumentType("sustainability_report");
    setReportYear(new Date().getFullYear() - 1);
    setPeriodStart("");
    setPeriodEnd("");
    setIndustrySystem("unknown");
    setIndustryCode("");
    setConsolidationScope("");
    setFile(null);
    setBusy(false);
    setError(null);
    setVersion(null);
    onReadyVersion(null);
    setPhase(changed ? "테넌트가 변경되었습니다. PDF와 메타데이터를 다시 확인해 주세요." : "PDF와 메타데이터를 선택해 주세요.");
  }, [onReadyVersion, tenantKey]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (inFlight.current || !file) return;
    if (!canEdit) {
      setError("현재 권한으로는 문서를 등록할 수 없습니다.");
      return;
    }
    if (!selection.companyId || !selection.rightsProfileId || !selection.consentProfileId || !selection.runtimeBindingId) {
      setError("기업, 문서 사용 권리, 데이터 처리 동의와 실행 환경을 모두 선택해 주세요.");
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setError("파일이 100MiB 제한을 초과했습니다.");
      return;
    }

    const abortController = new AbortController();
    controller.current?.abort();
    controller.current = abortController;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    setVersion(null);
    onReadyVersion(null);
    try {
      setPhase("파일 무결성 정보를 준비하는 중입니다.");
      const hash = await sha256(file);
      if (abortController.signal.aborted) return;
      const versionBody = {
        filename: file.name,
        size_bytes: file.size,
        sha256: hash,
        report_year: reportYear,
        industry_system: industrySystem,
        ...(industryCode.trim() ? { industry_code: industryCode.trim() } : {}),
        ...(consolidationScope.trim() ? { consolidation_scope: consolidationScope.trim() } : {}),
        period_start: periodStart,
        period_end: periodEnd,
        rights_profile_id: selection.rightsProfileId,
      };
      const signature = JSON.stringify({ selection, title, documentType, versionBody });
      if (workflow.current?.signature !== signature) {
        workflow.current = {
          signature,
          documentKey: crypto.randomUUID(),
          versionKey: crypto.randomUUID(),
          completeKey: crypto.randomUUID(),
        };
      }
      const current = workflow.current;
      const writeHeaders = (key: string) => ({
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken,
        "Idempotency-Key": key,
      });

      if (!current.documentId) {
        setPhase("문서를 등록하는 중입니다.");
        const document = await requestJson<{ document_id: string }>(`${apiBase}/v1/documents`, {
          method: "POST",
          signal: abortController.signal,
          headers: writeHeaders(current.documentKey),
          body: JSON.stringify({ company_id: selection.companyId, title, document_type: documentType }),
        });
        current.documentId = document.document_id;
      }

      if (!current.ticket) {
        setPhase("업로드 티켓을 발급하는 중입니다.");
        current.ticket = await requestJson<UploadTicket>(
          `${apiBase}/v1/documents/${current.documentId}/versions`,
          {
            method: "POST",
            signal: abortController.signal,
            headers: writeHeaders(current.versionKey),
            body: JSON.stringify(versionBody),
          },
        );
      }

      if (!current.uploaded) {
        setPhase("PDF를 업로드하는 중입니다.");
        const target = uploadTarget(current.ticket.post_url, apiBase, uploadOrigin);
        const form = new FormData();
        Object.entries(current.ticket.post_fields).forEach(([key, value]) => form.append(key, value));
        form.append("file", file, file.name);
        const receipt = await fetch(target.url, {
          method: "POST",
          redirect: "error",
          signal: abortController.signal,
          credentials: target.local ? "include" : "omit",
          headers: target.local ? { "X-CSRF-Token": csrfToken } : undefined,
          body: form,
        });
        if (!receipt.ok && !(target.local && receipt.status === 409)) {
          throw new ApiError(receipt.status, `HTTP_${receipt.status}`, "PDF 전송에 실패했습니다.");
        }
        current.uploaded = true;
      }

      if (!current.statusUrl) {
        setPhase("서버 검증을 요청하는 중입니다.");
        const accepted = await requestJson<JobAccepted>(
          `${apiBase}/v1/uploads/${current.ticket.upload_id}/complete`,
          {
            method: "POST",
            signal: abortController.signal,
            headers: writeHeaders(current.completeKey),
            body: JSON.stringify({ sha256: hash, size_bytes: file.size }),
          },
        );
        current.statusUrl = versionStatusUrl(accepted.status_url, apiBase);
      }

      for (let attempts = 0; attempts < 120; attempts += 1) {
        setPhase("서버가 PDF 형식, 100MiB·300페이지 제한과 무결성을 검증하는 중입니다.");
        const next = await requestJson<ReadyDocumentVersion>(current.statusUrl, {
          signal: abortController.signal,
        });
        if (next.status === "ready") {
          setVersion(next);
          onReadyVersion(next);
          setPhase("문서 버전이 준비되었습니다.");
          return;
        }
        if (next.status === "rejected") throw new Error("서버가 문서 버전을 거부했습니다.");
        await wait(abortController.signal);
      }
      throw new Error("서버 검증이 계속 진행 중입니다. 잠시 후 같은 요청으로 다시 확인해 주세요.");
    } catch (reason: unknown) {
      if (abortController.signal.aborted) return;
      if (reason instanceof ApiError && reason.code === "UPLOAD_EXPIRED" && workflow.current) {
        workflow.current.versionKey = crypto.randomUUID();
        workflow.current.completeKey = crypto.randomUUID();
        delete workflow.current.ticket;
        delete workflow.current.uploaded;
        delete workflow.current.statusUrl;
      }
      if (isSessionError(reason)) onSessionInvalid();
      setError(errorMessage(reason, "업로드를 완료하지 못했습니다. 잠시 후 같은 요청으로 다시 시도해 주세요."));
      setPhase("업로드를 완료하지 못했습니다.");
    } finally {
      if (!abortController.signal.aborted) {
        inFlight.current = false;
        setBusy(false);
      }
    }
  };

  return (
    <form onSubmit={submit} aria-label="PDF 문서 등록">
      <fieldset disabled={busy || !canEdit} aria-busy={busy} style={{ display: "grid", gap: 8, marginTop: 24 }}>
        <legend>PDF와 보고 메타데이터</legend>
        <label htmlFor="document-title">문서 제목</label>
        <input id="document-title" value={title} onChange={(event) => setTitle(event.target.value)} maxLength={200} required />
        <label htmlFor="document-type">문서 유형</label>
        <select id="document-type" value={documentType} onChange={(event) => setDocumentType(event.target.value as typeof documentType)}>
          <option value="sustainability_report">지속가능경영보고서</option>
          <option value="annual_report_section">사업보고서 지속가능성 정보란</option>
        </select>
        <label htmlFor="report-year">보고연도</label>
        <input id="report-year" type="number" min={1900} max={2200} value={reportYear} onChange={(event) => setReportYear(event.target.valueAsNumber)} required />
        <label htmlFor="period-start">보고기간 시작일</label>
        <input id="period-start" type="date" value={periodStart} onChange={(event) => setPeriodStart(event.target.value)} required />
        <label htmlFor="period-end">보고기간 종료일</label>
        <input id="period-end" type="date" value={periodEnd} onChange={(event) => setPeriodEnd(event.target.value)} required />
        <label htmlFor="industry-system">산업 분류 체계</label>
        <select id="industry-system" value={industrySystem} onChange={(event) => setIndustrySystem(event.target.value as typeof industrySystem)}>
          <option value="unknown">미확인</option>
          <option value="gics">GICS</option>
          <option value="sasb">SASB</option>
          <option value="custom">기타</option>
        </select>
        <label htmlFor="industry-code">산업 코드(선택)</label>
        <input id="industry-code" value={industryCode} onChange={(event) => setIndustryCode(event.target.value)} />
        <label htmlFor="consolidation-scope">연결범위(선택)</label>
        <input id="consolidation-scope" value={consolidationScope} onChange={(event) => setConsolidationScope(event.target.value)} />
        <label htmlFor="pdf-file">PDF 파일</label>
        <input key={tenantKey} id="pdf-file" type="file" accept="application/pdf,.pdf" onChange={(event) => setFile(event.target.files?.[0] ?? null)} aria-describedby="pdf-help" required />
        <p id="pdf-help">최대 100MiB·300페이지입니다. 페이지 수와 PDF 내용은 업로드 후 서버가 검증합니다.</p>
        <button type="submit" disabled={busy || !canEdit || !file || !selection.companyId || !selection.rightsProfileId || !selection.consentProfileId || !selection.runtimeBindingId} style={{ minHeight: 44 }}>
          {busy ? "등록 중…" : error ? "같은 요청으로 다시 시도" : "PDF 등록"}
        </button>
      </fieldset>
      {!canEdit ? <p role="status">현재 권한으로는 문서를 등록할 수 없습니다.</p> : null}
      <p role="status" aria-live="polite">{phase}</p>
      {error ? <p role="alert">{error}</p> : null}
      {version ? (
        <p role="status">준비된 문서 버전: {version.version_id}{version.page_count === null ? "" : ` · ${version.page_count}페이지`}</p>
      ) : null}
    </form>
  );
}
