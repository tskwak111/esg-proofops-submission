import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

GlobalWorkerOptions.workerSrc = workerUrl;

export type Section = "E" | "S" | "G" | "A" | "O";
export type PageInfo = { page: number; section: Section; source: "북마크" | "목차" | "머리글" | "키워드"; score: number; assurance: boolean };
type Anchor = { page: number; section: Section };
const environment = /온실가스|배출|Scope|탄소|재생에너지|RE100|에너지|용수|폐기물|기후|TCFD|감축|목표/gi;
const assurance = /검증의견서|제3자 검증|독립된 검증|Assurance|GRI Index|GRI Standards/i;
const social = /인권|인재|안전보건|공급망|고객만족|사회공헌|정보보안/gi;
const governance = /이사회|주주|조세|윤리|준법|리스크 관리|지배구조/gi;
const appendix = /ESG Data|Appendix|부록|ESG 지표|검증의견서|GRI Index|Reporting Index/gi;
const titles: [Section, RegExp][] = [
  ["A", /Appendix|부록|ESG Data|GRI Index|검증의견서/i],
  ["E", /Environmental|환경|기후변화|자원순환/i],
  ["S", /Social|사회|인권경영|인재관리/i],
  ["G", /Governance|지배구조|이사회/i],
];

function titleSection(title: string): Section | null {
  for (const [section, pattern] of titles) if (pattern.test(title)) return section;
  return null;
}

async function bookmarkAnchors(pdf: PDFDocumentProxy): Promise<Anchor[]> {
  const outline = await pdf.getOutline();
  if (!outline) return [];
  const anchors: Anchor[] = [];
  async function visit(nodes: typeof outline) {
    for (const node of nodes) {
      const section = titleSection(node.title);
      if (section && node.dest) {
        try {
          const destination = typeof node.dest === "string" ? await pdf.getDestination(node.dest) : node.dest;
          if (destination) anchors.push({ page: (await pdf.getPageIndex(destination[0])) + 1, section });
        } catch { /* Broken bookmark: use contents or text. */ }
      }
      if (node.items.length) await visit(node.items);
    }
  }
  await visit(outline);
  return anchors;
}

export function tocAnchors(text: string, total: number): Anchor[] {
  if (!/Contents|목차|Table of Contents/i.test(text.slice(0, 120))) return [];
  const headings: [Section, RegExp, RegExp][] = [
    ["E", /\bEnvironmental\b/gi, /환경(?:\s*분야)?/gi],
    ["S", /\bSocial\b/gi, /사회(?:\s*분야)?/gi],
    ["G", /\bGovernance\b/gi, /지배구조/gi],
    ["A", /ESG Data & Appendix|\bAppendix\b/gi, /부록/gi],
  ];
  const found: Anchor[] = [];
  for (const [section, english, korean] of headings) {
    const matches = [...text.matchAll(english)];
    if (!matches.length) matches.push(...text.matchAll(korean));
    const last = matches.at(-1);
    if (last?.index == null) continue;
    const nextNumber = text.slice(last.index + last[0].length, last.index + last[0].length + 110).match(/\b(\d{1,3})\b/);
    const page = Number(nextNumber?.[1]);
    if (page >= 1 && page <= total) found.push({ page, section });
  }
  return found;
}

function category(text: string): { section: Section; score: number; assurance: boolean; source: PageInfo["source"] } {
  const head = text.slice(0, 260);
  const singular = titles.filter(([, pattern]) => pattern.test(head));
  const headerSection = singular.length === 1 ? singular[0][0] : null;
  const counts: [Section, number][] = [
    ["E", [...text.matchAll(environment)].length],
    ["S", [...text.matchAll(social)].length],
    ["G", [...text.matchAll(governance)].length],
    ["A", [...text.matchAll(appendix)].length],
  ];
  counts.sort((a, b) => b[1] - a[1]);
  const section = headerSection || (counts[0][1] >= 2 && counts[0][1] > counts[1][1] ? counts[0][0] : "O");
  return { section, score: [...text.matchAll(environment)].length,
    assurance: assurance.test(text), source: headerSection ? "머리글" : "키워드" };
}

export async function scanSections(file: File, onProgress: (current: number, total: number) => void): Promise<PageInfo[]> {
  const pdf = await getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
  try {
    const total = pdf.numPages;
    const outline = await bookmarkAnchors(pdf);
    const texts: string[] = [];
    for (let page = 1; page <= total; page++) {
      const content = await (await pdf.getPage(page)).getTextContent();
      texts.push(content.items.filter(item => "str" in item).map(item => item.str).join(" "));
      if (page % 5 === 0 || page === total) onProgress(page, total);
    }
    const toc = outline.length >= 2 ? [] : texts.slice(0, Math.min(12, total)).flatMap(text => tocAnchors(text, total));
    const anchors = (outline.length >= 2 ? outline : toc).sort((a, b) => a.page - b.page);
    const source = outline.length >= 2 ? "북마크" : "목차";
    return texts.map((text, index) => {
      const guessed = category(text);
      const anchor = anchors.filter(item => item.page <= index + 1).at(-1);
      return { page: index + 1, section: anchor?.section || (anchors.length >= 2 ? "O" : guessed.section),
        source: anchor || anchors.length >= 2 ? source : guessed.source,
        score: guessed.score, assurance: guessed.assurance };
    });
  } finally { await pdf.destroy(); }
}
