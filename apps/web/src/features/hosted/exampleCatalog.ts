import { reportNames } from "./documentName";
import { HostedApiError, completedRunPresentation, type CollectionPage, type HostedClient, type HostedDocument, type HostedResult, type ListedRun } from "./hostedClient";
import { resultBrowserSummary } from "./resultModel";
import { UX1_HASH } from "./ux1Model";
import { unsupportedGuestEndpoint } from "./guestAccess";

// Coordinator-approved v29d default; compared with the result's actual analysis policy.
// UI/UX policy hashes and the reuse wrapper policy are not analysis-policy identities.
export const CURRENT_EXAMPLE_POLICY = "f29385d0c74d1520350deb06eaa9e11b72c5829dfaefeb2417c4e8ac0fed4ebd";
export type Example = { run: ListedRun; document: HostedDocument; result: HostedResult; name: string; identity: string; counts: ReturnType<typeof resultBrowserSummary>; completion: NonNullable<ReturnType<typeof completedRunPresentation>> };

/** Explicit report links win; otherwise open the original NAVER 2025 integrated report. */
export function selectedExample(examples: Example[], runId: string | null): Example | undefined {
  const naver = (example: Example) => /(?:\bNAVER\b|네이버|N사)/i.test(`${example.document.company_name ?? ""} ${example.name}`);
  return examples.find(example => example.run.run_id === runId)
    ?? examples.find(example => naver(example) && /2025/.test(example.name) && /통합보고서|integrated/i.test(example.name))
    ?? examples.find(naver)
    ?? examples[0];
}
export const exampleRuns = (runs: ListedRun[]) => runs.filter(run => run.status === "completed")
  .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));

export async function chooseExample(runs: ListedRun[], readResult: (url: string) => Promise<HostedResult>) {
  type Candidate = { run: ListedRun; result: HostedResult; completion: NonNullable<ReturnType<typeof completedRunPresentation>> };
  let unpinned: Candidate | null = null, currentPartial: Candidate | null = null;
  let hasPolicy = false;
  for (const run of exampleRuns(runs)) {
    if (!run.result_url) continue;
    const result = await readResult(run.result_url);
    const policy = result.reuse?.source_policy_sha256 || result.pipeline?.scope?.analysis_profile_sha256;
    hasPolicy ||= !!policy;
    const completion = completedRunPresentation(result, run.status);
    if (!completion) continue;
    const candidate = { run, result, completion };
    if (policy === CURRENT_EXAMPLE_POLICY) {
      if (completion.kind === "completed") return candidate;
      currentPartial ??= candidate;
    }
    // Preserve no-policy compatibility for fully complete results only.
    if (!policy && completion.kind === "completed" && !unpinned) unpinned = candidate;
  }
  return currentPartial ?? (hasPolicy ? null : unpinned);
}

async function allPages<T>(read: (cursor: string | null) => Promise<CollectionPage<T>>, signal: AbortSignal): Promise<T[]> {
  const items: T[] = [], seen = new Set<string>();
  let cursor: string | null = null;
  do {
    signal.throwIfAborted();
    const page = await read(cursor); items.push(...page.items); cursor = page.next_cursor;
    if (cursor && seen.has(cursor)) throw new Error("목록을 끝까지 확인하지 못했습니다.");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return items;
}

export async function loadExamples(client: HostedClient, signal: AbortSignal): Promise<Example[]> {
  // Older injected clients and servers retain their tenant-only catalog. Never mix the two inventories.
  const ownInventory = () => Promise.all([
    allPages(cursor => client.listDocuments(cursor, signal), signal),
    allPages(cursor => client.listRuns(cursor, signal), signal),
  ]);
  let documents: HostedDocument[], runs: ListedRun[];
  if (typeof client.listShowcaseDocuments !== "function" || typeof client.listShowcaseRuns !== "function") [documents, runs] = await ownInventory();
  else {
    try {
      [documents, runs] = await Promise.all([
        allPages(cursor => client.listShowcaseDocuments(cursor, signal), signal),
        allPages(cursor => client.listShowcaseRuns(cursor, signal), signal),
      ]);
    } catch (error) {
      if (!unsupportedGuestEndpoint(error)) throw error;
      [documents, runs] = await ownInventory();
    }
  }
  const names = reportNames(documents);
  const docs = new Map(documents.map(doc => [doc.document_id, doc]));
  const groups = new Map<string, { name: string; runs: ListedRun[] }>();
  for (const run of exampleRuns(runs)) {
    const doc = docs.get(run.document_id);
    if (!doc) continue;
    const name = names[doc.document_id];
    if (!name) continue; // Unnamed test uploads belong in full history, not the showcase.
    const identity = doc.sha256 || doc.original_filename || doc.filename || doc.ux1_metadata?.original_filename || doc.document_id;
    const group = groups.get(identity) ?? { name, runs: [] };
    group.runs.push(run); groups.set(identity, group);
  }
  const examples = await Promise.all([...groups].map(async ([identity, group]) => {
    const chosen = await chooseExample(group.runs, url => client.getResult(url, signal, UX1_HASH));
    return chosen ? { run: chosen.run, document: docs.get(chosen.run.document_id)!, result: chosen.result, name: group.name, identity, completion: chosen.completion, counts: resultBrowserSummary(chosen.result) } : null;
  }));
  signal.throwIfAborted();
  return examples.filter((example): example is Example => example !== null).sort((a, b) => Date.parse(b.run.created_at) - Date.parse(a.run.created_at));
}

/** Run pagination may reference documents outside the visible document page. */
export async function runDocuments(client: HostedClient, runs: ListedRun[], known: HostedDocument[], signal?: AbortSignal): Promise<HostedDocument[]> {
  const missing = [...new Set(runs.map(run => run.document_id))].filter(id => !known.some(doc => doc.document_id === id));
  const loaded = await Promise.all(missing.map(async id => {
    try { return await client.getDocument(id, signal); }
    catch (error) { if (error instanceof HostedApiError && error.status === 404) return null; throw error; }
  }));
  return loaded.filter((doc): doc is HostedDocument => doc !== null);
}
