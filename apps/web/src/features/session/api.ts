export type Session = {
  user_id: string;
  tenant_id: string | null;
  role: "viewer" | "editor" | "reviewer" | "admin" | null;
  csrf_token: string;
  expires_at: string;
};

type ErrorBody = {
  error?: { code?: string; message?: string; retryable?: boolean };
};

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
    readonly retryable = false,
  ) {
    super(message);
  }
}

export async function requestJson<T>(url: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(url, { ...init, credentials: "include" });
  if (!response.ok) {
    let body: ErrorBody = {};
    try {
      body = (await response.json()) as ErrorBody;
    } catch {
      // A non-JSON proxy or signed-upload error still gets a safe local message.
    }
    throw new ApiError(
      response.status,
      body.error?.code ?? `HTTP_${response.status}`,
      body.error?.message ?? "요청을 처리하지 못했습니다.",
      body.error?.retryable ?? false,
    );
  }
  return response.json() as Promise<T>;
}

export function errorMessage(reason: unknown, fallback: string): string {
  if (!(reason instanceof ApiError)) return reason instanceof Error ? reason.message : fallback;
  switch (reason.code) {
    case "AUTH_REQUIRED":
    case "SESSION_EXPIRED":
      return "세션이 없거나 만료되었습니다. 다시 로그인해 주세요.";
    case "FORBIDDEN":
      return "이 작업을 수행할 권한이 없습니다.";
    case "CSRF_INVALID":
      return "세션이 변경되었습니다. 화면을 새로고침해 주세요.";
    case "OIDC_UNAVAILABLE":
      return "로그인 서비스를 사용할 수 없습니다. 관리자에게 구성을 요청해 주세요.";
    case "UPLOAD_LIMIT_EXCEEDED":
      return "PDF가 파일 크기·페이지 수 또는 내부 처리 한도를 초과했습니다. 100MiB 이하인 파일도 내부 구조에 따라 처리되지 않을 수 있습니다.";
    case "PDF_INVALID":
      return "서버가 유효한 PDF로 확인하지 못했습니다.";
    case "UPLOAD_INTEGRITY_MISMATCH":
      return "업로드한 파일의 무결성 검증에 실패했습니다.";
    case "UPLOAD_EXPIRED":
      return "업로드 티켓이 만료되었습니다. 같은 파일로 다시 시도해 주세요.";
    case "CONFIG_GATE_BLOCKED":
      return "승인된 실행 구성과 사전 점검 상태를 확인해 주세요.";
    case "STALE_RUN_REVISION":
      return "실행 상태가 변경되었습니다. 최신 상태를 확인한 뒤 다시 시도해 주세요.";
    case "TENANT_RUN_LIMIT":
      return "동시에 실행할 수 있는 문서 수를 초과했습니다. 진행 중인 실행을 확인해 주세요.";
    case "REPORT_NOT_FINALIZABLE":
      return "미완료 항목이 있습니다. 검토를 마치거나 부분 결과 허용을 선택해 주세요.";
    default:
      return fallback;
  }
}

export function isSessionError(reason: unknown): boolean {
  return reason instanceof ApiError && (reason.status === 401 || reason.code === "SESSION_EXPIRED");
}
