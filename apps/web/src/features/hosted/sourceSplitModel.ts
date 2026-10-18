import type { ResultClaim, SourceLocation, SourceRefDto, SourceViewRef } from "./hostedClient";

// Display-only geometry. Never promote a parser estimate into verified location data.
export type DisplaySourceRef = SourceViewRef & { estimated_bbox?: number[] };
export const ESTIMATED_LOCATION_LABEL = "추정 위치(원문 대조 전)";

function validParserBox(box: unknown): box is number[] {
  return Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)
    && 0 <= box[0] && box[0] < box[2] && 0 <= box[1] && box[1] < box[3];
}

export function locationRects(location: SourceLocation | null, viewport?: { width: number; height: number; rotation: number }): number[][] {
  if (!location || location.units !== "pdf_points" || location.origin !== "top_left" || location.space !== "rotated_cropbox_display" || location.bbox_format !== "x0,y0,x1,y1") return [];
  const { width_pt: width, height_pt: height, rotation } = location.page;
  if (!Number.isFinite(width) || !Number.isFinite(height) || width <= 0 || height <= 0 || ![0,90,180,270].includes(rotation)) return [];
  if (viewport && (Math.abs(viewport.width - width) > .1 || Math.abs(viewport.height - height) > .1 || viewport.rotation !== rotation)) return [];
  const boxes = location.line_rects?.length ? location.line_rects : location.glyph_rects?.length ? location.glyph_rects : [location.bbox];
  if (!boxes.every(box => Array.isArray(box) && box.length === 4 && box.every(Number.isFinite) && 0 <= box[0] && box[0] < box[2] && box[2] <= width && 0 <= box[1] && box[1] < box[3] && box[3] <= height)) return [];
  // Stored coordinates already include rotation and crop. Never rotate twice.
  return boxes.map(([x0,y0,x1,y1]) => [x0 / width, y0 / height, (x1-x0) / width, (y1-y0) / height]);
}

export function sourceHighlight(ref: DisplaySourceRef, viewport?: { width: number; height: number; rotation: number }) {
  if (ref.w18_verification_state !== undefined && ref.w18_verification_state !== "verified") {
    if (ref.kind !== "claim" || !Number.isInteger(ref.physical_page) || ref.physical_page! <= 0 || !validParserBox(ref.estimated_bbox)) return { boxes: [], precision: "unavailable" };
    const page = ref.location?.page ?? (ref.w18_coordinates?.physical_page === ref.physical_page ? ref.w18_coordinates.page : undefined)
      ?? (viewport ? { width_pt: viewport.width, height_pt: viewport.height, rotation: viewport.rotation, crop_box: [] } : undefined);
    // The list can announce an estimate before PDF.js supplies this page's dimensions.
    if (!page) return { boxes: [], precision: "estimated" };
    const boxes = locationRects({ bbox: ref.estimated_bbox, line_rects: null, page, units: "pdf_points", origin: "top_left", space: "rotated_cropbox_display", bbox_format: "x0,y0,x1,y1" }, viewport);
    return { boxes, precision: boxes.length ? "estimated" : "unavailable" };
  }
  const w18 = ref.w18_coordinates;
  const precise = ref.w18_cell_rects?.length ? ref.w18_cell_rects : ref.w18_line_rects;
  if (w18 && precise?.length && w18.physical_page === ref.physical_page) {
    const boxes = locationRects({ ...w18, bbox: precise[0], line_rects: precise }, viewport);
    if (boxes.length) return { boxes, precision: ref.w18_cell_rects?.length ? "cell" : "exact" };
  }
  const boxes = locationRects(ref.location, viewport);
  // W16 line/glyph geometry is quote-specific; a parser bbox alone is not.
  const precision = !boxes.length ? "unavailable"
    : ref.location?.line_rects?.length || ref.location?.glyph_rects?.length ? "exact"
    : ref.ref_id.startsWith("table_observation/cell_source_refs/") ? "cell" : "region";
  return { boxes, precision };
}

export function uniqueEvidence(refs: SourceViewRef[]): SourceViewRef[] {
  const seen = new Set<string>();
  return refs.filter(ref => {
    if (ref.kind !== "evidence") return false;
    const key = JSON.stringify([ref.element_id, ref.source_id, ref.physical_page, ref.quote]);
    if (seen.has(key)) return false;
    seen.add(key); return true;
  });
}

/** Verification is read from existing server refs even when an older API omits W18. */
export function displayRefs(claim?: ResultClaim): DisplaySourceRef[] {
  return (claim?.w16_source_view?.refs ?? []).map(ref => {
    let raw: unknown = claim;
    for (const key of ref.ref_id.split("/")) raw = raw && typeof raw === "object" ? (raw as Record<string, unknown>)[key] : undefined;
    const state = raw && typeof raw === "object" ? (raw as { verification_state?: string }).verification_state : undefined;
    const claimVerified = (claim?.source_status?.status ?? claim?.source_quality) === "verified";
    const source = raw as SourceRefDto | undefined;
    const estimated = (claim?.source_status?.status ?? claim?.source_quality) === "unverified" && ref.kind === "claim"
      && /^source_refs\/\d+$/.test(ref.ref_id) && source?.source_id === ref.source_id && source?.location_quality === "located"
      && Number.isInteger(source.page_num) && source.page_num > 0 && Number.isInteger(ref.physical_page) && ref.physical_page! > 0 && validParserBox(source.bbox);
    return { ...ref, ...(estimated ? { estimated_bbox: source!.bbox! } : {}), w18_verification_state: ref.kind === "claim" && !claimVerified ? "unverified" : state ?? ref.w18_verification_state ?? "unverified" };
  });
}

export function claimLocationLabel(claim?: ResultClaim): string {
  const highlights = displayRefs(claim).filter(ref => ref.kind === "claim").map(ref => sourceHighlight(ref));
  if (highlights.some(item => item.precision !== "estimated" && item.boxes.length)) return "";
  return highlights.some(item => item.precision === "estimated") ? "추정 위치" : "위치 미확인";
}

export function highlightLabel(precision: string): string {
  return precision === "estimated" ? ESTIMATED_LOCATION_LABEL : precision === "search" ? "텍스트 검색 위치"
    : precision === "region" ? "영역 표시(정확 위치 아님)" : precision === "cell" ? "저장된 표 셀 위치" : "정확 인용 위치";
}

/** Only whitespace normalization. Punctuation, digits and codepoints stay literal. */
export function uniqueTextMatch(text: string, quote: string): [number, number] | null {
  const offsets: number[] = [];
  let normalized = "";
  for (let i = 0; i < text.length; i++) if (!/\s/u.test(text[i])) { normalized += text[i]; offsets.push(i); }
  const target = quote.replace(/\s/gu, "");
  if (!target) return null;
  const start = normalized.indexOf(target);
  if (start < 0 || normalized.indexOf(target, start + 1) >= 0) return null;
  return [offsets[start], offsets[start + target.length - 1] + 1];
}

/** Measure an exact DOM Range in PDF.js's own text layer; no width interpolation. */
export function textSearchRects(container: HTMLElement, quote: string): number[][] {
  const nodes: { node: Text; start: number; end: number }[] = [];
  const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
  let text = "";
  while (walker.nextNode()) {
    const node = walker.currentNode as Text;
    nodes.push({ node, start: text.length, end: text.length + node.length }); text += node.data;
  }
  const match = uniqueTextMatch(text, quote);
  if (!match) return [];
  const first = nodes.find(item => item.start <= match[0] && match[0] < item.end);
  const last = nodes.find(item => item.start < match[1] && match[1] <= item.end);
  if (!first || !last) return [];
  const range = document.createRange();
  range.setStart(first.node, match[0] - first.start); range.setEnd(last.node, match[1] - last.start);
  const page = container.getBoundingClientRect();
  const boxes = [...range.getClientRects()].filter(rect => rect.width > 0 && rect.height > 0).map(rect => [(rect.left-page.left)/page.width, (rect.top-page.top)/page.height, rect.width/page.width, rect.height/page.height]);
  if (!boxes.length || boxes.some(box => !box.every(Number.isFinite) || box[0] < 0 || box[1] < 0 || box[0]+box[2] > 1.001 || box[1]+box[3] > 1.001)) return [];
  return boxes;
}

/** An evidence jump must not resolve to the claim when their quotes are identical. */
export function selectedSourceRef(refs: SourceViewRef[], selection: { quote?: string; page?: number; elementId?: string }) {
  if (!selection.quote) return refs.find(ref => ref.kind === "claim");
  return refs.find(ref => ref.kind === "evidence" && ref.quote === selection.quote &&
    (!selection.page || ref.physical_page === selection.page) && (!selection.elementId || ref.element_id === selection.elementId));
}
