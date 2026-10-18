import { HostedApiError, type HostedClient, type Session } from "./hostedClient";

export function unsupportedGuestEndpoint(error: unknown): boolean {
  return error instanceof HostedApiError && [404, 405, 501].includes(error.status);
}

/** Keep existing identities; create a cookie-backed visitor only when explicitly available. */
export async function enterHostedSession(client: HostedClient, signal?: AbortSignal): Promise<Session | null> {
  try { return await client.session(signal); }
  catch (error) { if (!(error instanceof HostedApiError) || error.status !== 401) throw error; }
  try {
    const access = await client.guestAccess(signal);
    if (!access.available) return null;
  } catch (error) { if (unsupportedGuestEndpoint(error)) return null; throw error; }
  return client.guestSession(signal);
}

/** Verify direct links too; the query string is never an authorization or editability signal. */
export async function isShowcaseRun(client: HostedClient, runId: string, signal?: AbortSignal): Promise<boolean> {
  if (typeof client.listShowcaseRuns !== "function") return false;
  let cursor: string | null = null;
  const seen = new Set<string>();
  try {
    do {
      signal?.throwIfAborted();
      const page = await client.listShowcaseRuns(cursor, signal);
      if (page.items.some(run => run.run_id === runId)) return true;
      cursor = page.next_cursor;
      if (cursor && seen.has(cursor)) throw new Error("공개 사례 목록을 끝까지 확인하지 못했습니다.");
      if (cursor) seen.add(cursor);
    } while (cursor);
    return false;
  } catch (error) { if (unsupportedGuestEndpoint(error)) return false; throw error; }
}
