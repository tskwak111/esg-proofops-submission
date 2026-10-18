import type { Session } from "./hostedClient";

const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function runIdFromHash(hash: string): string | null {
  const id = hash.startsWith("#run-") ? hash.slice(5) : "";
  return uuid.test(id) ? id : null;
}
export function validResourceId(id: string) { return uuid.test(id); }
// Result tabs live in ?tab= so switching tabs never fires hashchange (which reopens and scrolls the run).
export const resultTabs = ["overview", "claims", "source"] as const;
export type ResultTab = typeof resultTabs[number];
export function resultTabFromSearch(search: string): ResultTab {
  const value = new URLSearchParams(search).get("tab");
  return resultTabs.find(tab => tab === value) ?? "overview";
}
/** Same path and other query params; only `tab` and the #run- anchor change. Overview stays implicit. */
export function resultTabUrl(location: Pick<Location, "pathname" | "search">, runId: string, tab: ResultTab): string {
  const params = new URLSearchParams(location.search);
  if (tab === "overview") params.delete("tab"); else params.set("tab", tab);
  const query = params.toString();
  return `${location.pathname}${query ? `?${query}` : ""}#run-${runId}`;
}
// Links supply only an identifier; requests always use our authenticated client.
export function runIdFromResultPath(path: string): string | null {
  const id = /^\/(?:v1\/runs\/([^/]+)\/result|runs\/([^/]+)\/result)\/?$/.exec(path);
  const value = id?.[1] ?? id?.[2] ?? "";
  return validResourceId(value) ? value : null;
}
export function runIdFromResultLink(input: string): string | null {
  const value = input.trim();
  if (validResourceId(value)) return value;
  if (!value.startsWith("/") && !/^https?:\/\//.test(value)) return null;
  try {
    const url = new URL(value, "https://proofops.invalid");
    const tabOnly = url.pathname === "/analyze" && [...url.searchParams.keys()].every(key => key === "tab") && url.searchParams.getAll("tab").length <= 1;
    if (url.username || url.password || (url.search && !tabOnly)) return null;
    if (url.pathname === "/analyze") return runIdFromHash(url.hash);
    if (url.hash) return null;
    return runIdFromResultPath(url.pathname.replace(/^\/hosted-api(?=\/)/, ""));
  } catch { return null; }
}
export type HistoryEntry = { documentId: string; name: string; runId?: string; seenAt: number };
export function historyKey(session: Pick<Session, "user_id" | "tenant_id">) {
  return `proofops-intake-v1:${session.user_id}:${session.tenant_id}`;
}
export function readHistory(storage: Pick<Storage, "getItem">, key: string, now = Date.now()): HistoryEntry[] {
  try {
    const value = JSON.parse(storage.getItem(key) ?? "null");
    if (value?.schema !== 1 || !Array.isArray(value.entries)) return [];
    return value.entries.filter((entry: HistoryEntry) => entry && typeof entry.documentId === "string" && uuid.test(entry.documentId)
      && (entry.runId === undefined || (typeof entry.runId === "string" && uuid.test(entry.runId)))
      && typeof entry.name === "string" && entry.name.length <= 180 && Number.isFinite(entry.seenAt)
      && entry.seenAt <= now && entry.seenAt > now - 7 * 86400000).slice(-100)
      .map((entry: HistoryEntry) => ({ documentId: entry.documentId, name: entry.name, runId: entry.runId, seenAt: entry.seenAt }));
  } catch { return []; }
}
export function rememberHistory(storage: Pick<Storage, "getItem" | "setItem">, key: string, entry: HistoryEntry): HistoryEntry[] {
  if (!uuid.test(entry.documentId) || (entry.runId && !uuid.test(entry.runId))) return readHistory(storage, key);
  const entries = [...readHistory(storage, key).filter(previous => entry.runId
    ? previous.runId !== entry.runId && !(previous.documentId === entry.documentId && !previous.runId)
    : previous.documentId !== entry.documentId),
  { documentId: entry.documentId, name: entry.name.slice(0, 180), runId: entry.runId, seenAt: entry.seenAt }].slice(-100);
  try { storage.setItem(key, JSON.stringify({ schema: 1, entries })); } catch { /* Result URL remains usable when browser storage is unavailable. */ }
  return entries;
}
export function forgetDocumentHistory(storage: Pick<Storage, "getItem" | "setItem">, key: string, documentId: string): HistoryEntry[] {
  const entries = readHistory(storage, key).filter(entry => entry.documentId !== documentId);
  try { storage.setItem(key, JSON.stringify({ schema: 1, entries })); } catch { /* Browser metadata only; server deletion already completed. */ }
  return entries;
}
