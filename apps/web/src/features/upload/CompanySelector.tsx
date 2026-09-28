import { FormEvent, useEffect, useRef, useState } from "react";
import { errorMessage, isSessionError, requestJson } from "../session/api";

type Company = {
  company_id: string;
  legal_name: string;
  registration_identifier: string | null;
  aliases: string[];
  created_at: string;
};

type Option = {
  id: string;
  name: string;
  status: "approved" | "unverified" | "disabled";
  reason: string | null;
};

export type RulePackOption = {
  rule_pack_id: string;
  version: string;
  sha256: string;
  status: "draft" | "validated" | "active" | "retired";
  mode: "disclosure" | "advertising";
  effective_date: string;
  unresolved_gap_ids: string[];
};

export type RuntimeOptions = {
  rights_profiles: Option[];
  consent_profiles: Option[];
  runtime_bindings: Option[];
  rule_packs: RulePackOption[];
  enabled_modes: Array<"disclosure" | "advertising">;
};

type CompanyPage = { items: Company[]; next_cursor: string | null };

export type UploadSelection = {
  companyId: string;
  rightsProfileId: string;
  consentProfileId: string;
  runtimeBindingId: string;
};

type Props = {
  apiBase?: string;
  csrfToken: string;
  tenantKey: string;
  value: UploadSelection;
  canCreate: boolean;
  onChange: (value: UploadSelection) => void;
  onOptionsChange: (value: RuntimeOptions) => void;
  onSessionInvalid: () => void;
};

const emptyOptions: RuntimeOptions = {
  rights_profiles: [],
  consent_profiles: [],
  runtime_bindings: [],
  rule_packs: [],
  enabled_modes: [],
};

async function allCompanies(apiBase: string, signal: AbortSignal): Promise<Company[]> {
  const companies: Company[] = [];
  const cursors = new Set<string>();
  let cursor: string | null = null;
  do {
    const query = new URLSearchParams({ limit: "100" });
    if (cursor) query.set("cursor", cursor);
    const page = await requestJson<CompanyPage>(`${apiBase}/v1/companies?${query}`, { signal });
    companies.push(...page.items);
    cursor = page.next_cursor;
    if (cursor && cursors.has(cursor)) throw new Error("기업 목록 페이지를 계속 불러올 수 없습니다.");
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return companies;
}

export function CompanySelector({
  apiBase = "",
  csrfToken,
  tenantKey,
  value,
  canCreate,
  onChange,
  onOptionsChange,
  onSessionInvalid,
}: Props) {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [options, setOptions] = useState<RuntimeOptions>(emptyOptions);
  const [newCompanyName, setNewCompanyName] = useState("");
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const createRequest = useRef<{ body: string; key: string } | null>(null);
  const createController = useRef<AbortController | null>(null);
  const createInFlight = useRef(false);
  const currentTenant = useRef(tenantKey);
  currentTenant.current = tenantKey;

  useEffect(() => {
    const controller = new AbortController();
    createController.current?.abort();
    createInFlight.current = false;
    createRequest.current = null;
    setCompanies([]);
    setOptions(emptyOptions);
    onOptionsChange(emptyOptions);
    setNewCompanyName("");
    setError(null);
    setLoading(true);
    setCreating(false);
    onChange({ companyId: "", rightsProfileId: "", consentProfileId: "", runtimeBindingId: "" });

    Promise.all([
      allCompanies(apiBase, controller.signal),
      requestJson<RuntimeOptions>(`${apiBase}/v1/runtime-options`, { signal: controller.signal }),
    ])
      .then(([nextCompanies, nextOptions]) => {
        if (controller.signal.aborted) return;
        setCompanies(nextCompanies);
        setOptions(nextOptions);
        onOptionsChange(nextOptions);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        if (isSessionError(reason)) onSessionInvalid();
        setError(errorMessage(reason, "기업과 승인 정보를 불러오지 못했습니다."));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [apiBase, onChange, onOptionsChange, onSessionInvalid, tenantKey]);

  const set = (field: keyof UploadSelection, selected: string) =>
    onChange({ ...value, [field]: selected });

  const createCompany = async (event: FormEvent) => {
    event.preventDefault();
    const legalName = newCompanyName.trim();
    if (!legalName || !canCreate || createInFlight.current) return;
    const tenantAtStart = tenantKey;
    const body = JSON.stringify({ legal_name: legalName, aliases: [] });
    if (createRequest.current?.body !== body) {
      createRequest.current = { body, key: crypto.randomUUID() };
    }
    const controller = new AbortController();
    createController.current = controller;
    createInFlight.current = true;
    setCreating(true);
    setError(null);
    try {
      const company = await requestJson<Company>(`${apiBase}/v1/companies`, {
        method: "POST",
        signal: controller.signal,
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken,
          "Idempotency-Key": createRequest.current.key,
        },
        body,
      });
      if (currentTenant.current !== tenantAtStart) return;
      setCompanies((current) =>
        [...current, company].sort((a, b) => a.legal_name.localeCompare(b.legal_name, "ko")),
      );
      set("companyId", company.company_id);
      setNewCompanyName("");
      createRequest.current = null;
    } catch (reason: unknown) {
      if (controller.signal.aborted) return;
      if (isSessionError(reason)) onSessionInvalid();
      setError(errorMessage(reason, "기업을 등록하지 못했습니다. 같은 이름으로 다시 시도해 주세요."));
    } finally {
      if (currentTenant.current === tenantAtStart) {
        createInFlight.current = false;
        setCreating(false);
      }
    }
  };

  const approvedRights = options.rights_profiles.filter((option) => option.status === "approved");
  const approvedConsents = options.consent_profiles.filter((option) => option.status === "approved");
  const approvedRuntimes = options.runtime_bindings.filter((option) => option.status === "approved");

  return (
    <fieldset disabled={loading} aria-busy={loading} style={{ display: "grid", gap: 8, marginTop: 24 }}>
      <legend>기업과 문서 권리</legend>
      {loading ? <p role="status">기업과 승인 정보를 불러오는 중입니다.</p> : null}
      {error ? <p role="alert">{error}</p> : null}
      <label htmlFor="company">기업</label>
      <select id="company" value={value.companyId} onChange={(event) => set("companyId", event.target.value)} required>
        <option value="">기업을 선택하세요</option>
        {companies.map((company) => <option key={company.company_id} value={company.company_id}>{company.legal_name}</option>)}
      </select>
      {!loading && companies.length === 0 ? <p role="status">등록된 기업이 없습니다. 편집 권한이 있으면 아래에서 새 기업을 등록하세요.</p> : null}

      <form onSubmit={createCompany} aria-label="새 기업 등록" style={{ display: "grid", gap: 8 }}>
        <label htmlFor="new-company">새 기업의 법적 명칭</label>
        <input id="new-company" value={newCompanyName} onChange={(event) => setNewCompanyName(event.target.value)} maxLength={200} disabled={!canCreate || creating} />
        <button type="submit" disabled={!canCreate || creating || !newCompanyName.trim()} style={{ minHeight: 44 }}>{creating ? "등록 중…" : "기업 등록"}</button>
        {!canCreate ? <p>현재 권한으로는 기업을 등록할 수 없습니다.</p> : null}
      </form>

      <label htmlFor="rights-profile">문서 사용 권리</label>
      <select id="rights-profile" value={value.rightsProfileId} onChange={(event) => set("rightsProfileId", event.target.value)} disabled={approvedRights.length === 0} required>
        <option value="">승인된 권리를 선택하세요</option>
        {approvedRights.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
      </select>
      {!loading && approvedRights.length === 0 ? <p role="status">승인된 문서 사용 권리가 없습니다. 관리자에게 권리 승인을 요청하세요.</p> : null}

      <label htmlFor="consent-profile">데이터 처리 동의</label>
      <select id="consent-profile" value={value.consentProfileId} onChange={(event) => set("consentProfileId", event.target.value)} disabled={approvedConsents.length === 0} required>
        <option value="">승인된 동의를 선택하세요</option>
        {approvedConsents.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
      </select>
      {!loading && approvedConsents.length === 0 ? <p role="status">승인된 데이터 처리 동의가 없습니다. 관리자에게 동의 승인을 요청하세요.</p> : null}

      <label htmlFor="runtime-binding">실행 환경</label>
      <select id="runtime-binding" value={value.runtimeBindingId} onChange={(event) => set("runtimeBindingId", event.target.value)} disabled={approvedRuntimes.length === 0} required>
        <option value="">승인된 실행 환경을 선택하세요</option>
        {approvedRuntimes.map((option) => <option key={option.id} value={option.id}>{option.name}</option>)}
      </select>
      {!loading && approvedRuntimes.length === 0 ? <p role="status">승인된 실행 환경이 없습니다. 관리자에게 실행 환경 승인을 요청하세요.</p> : null}
      {!loading && !options.rule_packs.some((pack) => pack.status === "active") ? <p role="status">업로드 후 분석 실행 전 활성 규칙집 구성이 필요합니다.</p> : null}
    </fieldset>
  );
}
