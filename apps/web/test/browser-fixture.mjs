import { createReadStream, existsSync } from "node:fs";
import { createServer } from "node:http";
import { extname, join, normalize } from "node:path";

const root = normalize(join(import.meta.dirname, "../dist"));
const port = Number(process.env.PORT ?? 4174);
const ids = {
  tenant: "11111111-1111-4111-8111-111111111111",
  viewerTenant: "22222222-2222-4222-8222-222222222222",
  rights: "33333333-3333-4333-8333-333333333333",
  consent: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
  runtime: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
  companyA: "44444444-4444-4444-8444-444444444444",
  companyB: "55555555-5555-4555-8555-555555555555",
  document: "66666666-6666-4666-8666-666666666666",
  upload: "77777777-7777-4777-8777-777777777777",
  version: "88888888-8888-4888-8888-888888888888",
  job: "99999999-9999-4999-8999-999999999999",
};
let mode = "editor";
let failDocumentOnce = false;
const idempotency = [];
const companyCursors = [];
const companies = [company(ids.companyA, "가 기업"), company(ids.companyB, "나 기업")];

function company(company_id, legal_name) {
  return { company_id, legal_name, registration_identifier: null, aliases: [], created_at: "2026-09-09T00:00:00Z" };
}

function json(response, status, body) {
  response.writeHead(status, { "content-type": "application/json", "cache-control": "no-store" });
  response.end(JSON.stringify(body));
}

async function body(request) {
  const chunks = [];
  for await (const chunk of request) chunks.push(chunk);
  return Buffer.concat(chunks).toString();
}

function error(response, status, code) {
  json(response, status, { error: { code, message: code, request_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", retryable: status >= 500 } });
}

function record(request) {
  const key = request.headers["idempotency-key"];
  if (key) idempotency.push({ path: request.url, key });
}

const server = createServer(async (request, response) => {
  const url = new URL(request.url, `http://${request.headers.host}`);
  if (url.pathname === "/__fixture/state") return json(response, 200, { mode, idempotency, companyCursors });
  if (url.pathname === "/__fixture/mode") {
    mode = url.searchParams.get("value") ?? "editor";
    failDocumentOnce = mode === "retry";
    idempotency.length = 0;
    companyCursors.length = 0;
    return json(response, 200, { mode });
  }
  if (url.pathname === "/v1/session") {
    if (mode === "missing") return error(response, 401, "AUTH_REQUIRED");
    if (mode === "unavailable") return error(response, 503, "OIDC_UNAVAILABLE");
    return json(response, 200, {
      user_id: "browser-fixture-user",
      tenant_id: mode === "no-tenant" ? null : ids.tenant,
      role: mode === "no-tenant" ? null : mode === "viewer" ? "viewer" : "editor",
      csrf_token: "browser-fixture-csrf",
      expires_at: "2026-09-10T00:00:00Z",
    });
  }
  if (url.pathname === "/v1/session/tenant" && request.method === "POST") {
    return json(response, 200, {
      user_id: "browser-fixture-user",
      tenant_id: ids.viewerTenant,
      role: "viewer",
      csrf_token: "rotated-browser-fixture-csrf",
      expires_at: "2026-09-10T00:00:00Z",
    });
  }
  if (url.pathname === "/v1/companies" && request.method === "GET") {
    const cursor = url.searchParams.get("cursor");
    companyCursors.push(cursor);
    return json(response, 200, {
      items: cursor ? [companies[1]] : [companies[0]],
      next_cursor: cursor ? null : "page-2",
      snapshot_epoch: 1,
    });
  }
  if (url.pathname === "/v1/companies" && request.method === "POST") {
    record(request);
    const parsed = JSON.parse(await body(request));
    const created = company("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", parsed.legal_name);
    companies.push(created);
    return json(response, 201, created);
  }
  if (url.pathname === "/v1/runtime-options") {
    return json(response, 200, {
      rights_profiles: [{ id: ids.rights, name: "공개 보고서 사용 승인", status: "approved", reason: null }],
      consent_profiles: mode === "missing-consent" ? [] : [{ id: ids.consent, name: "문서 처리 동의", status: "approved", reason: null }],
      runtime_bindings: mode === "missing-runtime" ? [] : [{ id: ids.runtime, name: "승인된 로컬 실행 환경", status: "approved", reason: null }],
      rule_packs: [], enabled_modes: ["disclosure"],
    });
  }
  if (url.pathname === "/v1/documents" && request.method === "POST") {
    record(request);
    if (failDocumentOnce) {
      failDocumentOnce = false;
      return error(response, 503, "DEPENDENCY_UNAVAILABLE");
    }
    await body(request);
    return json(response, 201, { document_id: ids.document });
  }
  if (url.pathname === `/v1/documents/${ids.document}/versions` && request.method === "POST") {
    record(request);
    await body(request);
    return json(response, 201, {
      upload_id: ids.upload,
      document_id: ids.document,
      post_url: mode === "unsafe" ? "https://unapproved.invalid/upload" : `/local/uploads/${ids.upload}/content`,
      post_fields: { ticket: "fixture-ticket" },
      expires_at: "2026-09-09T01:00:00Z",
    });
  }
  if (url.pathname === `/local/uploads/${ids.upload}/content` && request.method === "POST") {
    await body(request);
    response.writeHead(204);
    return response.end();
  }
  if (url.pathname === `/v1/uploads/${ids.upload}/complete` && request.method === "POST") {
    record(request);
    await body(request);
    return json(response, 202, { job_id: ids.job, resource_id: ids.version, status: "ready", status_url: `/v1/versions/${ids.version}` });
  }
  if (url.pathname === `/v1/versions/${ids.version}`) {
    return json(response, 200, { version_id: ids.version, document_id: ids.document, sha256: "0".repeat(64), report_year: 2025, page_count: 1, status: "ready", created_at: "2026-09-09T00:00:00Z" });
  }

  const requested = url.pathname === "/" ? "/index.html" : url.pathname;
  const file = normalize(join(root, requested));
  const target = file.startsWith(root) && existsSync(file) ? file : join(root, "index.html");
  const type = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" }[extname(target)] ?? "application/octet-stream";
  response.writeHead(200, { "content-type": type });
  createReadStream(target).pipe(response);
});

server.listen(port, "127.0.0.1", () => console.log(`browser fixture: http://127.0.0.1:${port}`));
