import { useEffect, useRef, useState } from "react";
import { AnnotationMode, TextLayer, GlobalWorkerOptions, getDocument, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { sourceHighlight, textSearchRects, highlightLabel, ESTIMATED_LOCATION_LABEL, type DisplaySourceRef } from "./sourceSplitModel";

GlobalWorkerOptions.workerSrc = workerUrl;

export default function OriginalPdfPage({ url, pageNumber, refs, zoom, onPageCount, sourceSha }: {
  url: string; pageNumber: number; refs: DisplaySourceRef[]; zoom: number; onPageCount: (count: number) => void; sourceSha: string;
}) {
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [failure, setFailure] = useState("");
  const [loading, setLoading] = useState(true);
  const [progress, setProgress] = useState<number | null>(null);
  const [viewport, setViewport] = useState<{ width: number; height: number; rotation: number; page: number } | null>(null);
  const textContainer = useRef<HTMLDivElement>(null);
  const [searchLayer, setSearchLayer] = useState<number | null>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const scroll = useRef<HTMLDivElement>(null);
  useEffect(() => {
    let disposed = false;
    setPdf(null); setFailure(""); setLoading(true); setViewport(null); setProgress(null);
    const task = getDocument({ url, withCredentials: true, disableAutoFetch: true, disableStream: true, rangeChunkSize: 1024 * 1024, isEvalSupported: false, enableXfa: false, wasmUrl: "/pdfjs/wasm/", cMapUrl: "/pdfjs/cmaps/", cMapPacked: true, standardFontDataUrl: "/pdfjs/standard_fonts/" });
    task.onProgress = ({ loaded, total }: { loaded: number; total: number }) => { if (!disposed && total > 0) setProgress(Math.min(100, Math.round(loaded / total * 100))); };
    void task.promise.then(document => {
      if (!disposed) { setPdf(document); onPageCount(document.numPages); }
    }).catch(error => { if (!disposed) {
      setFailure(error?.name === "PasswordException" ? "암호화된 PDF입니다. 열기 암호를 해제한 원본을 사용해 주세요." : error?.status === 401 ? "로그인이 만료되었습니다. 다시 로그인해 주세요." : [404,410].includes(error?.status) ? "원본을 사용할 수 없습니다. 삭제 여부와 보관 기간을 확인해 주세요." : error?.status === 409 ? "원본 무결성을 확인하지 못했습니다. 원본 파일을 확인해 주세요." : "원본 PDF를 불러오지 못했습니다. 네트워크·접근 권한을 확인한 뒤 다시 불러와 주세요.");
      setLoading(false);
    } });
    return () => { disposed = true; void task.destroy(); };
  }, [url, onPageCount]);
  useEffect(() => {
    if (!pdf || !canvas.current) return;
    let disposed = false;
    let render: RenderTask | undefined;
    let layer: TextLayer | undefined;
    setSearchLayer(null);
    textContainer.current?.replaceChildren();
    setLoading(true); setFailure(""); setViewport(null);
    void (async () => {
      const page = await pdf.getPage(pageNumber);
      if (disposed || !canvas.current) return;
      const base = page.getViewport({ scale: 1 });
      // Only the current page has a canvas; cap memory even for huge PDF pages.
      const scale = Math.min(2 * zoom, 2600 / Math.max(base.width, base.height));
      const view = page.getViewport({ scale });
      canvas.current.width = Math.ceil(view.width); canvas.current.height = Math.ceil(view.height);
      render = page.render({ canvas: canvas.current, viewport: view, annotationMode: AnnotationMode.DISABLE });
      await render.promise;
      if (!disposed) { setViewport({ width: base.width, height: base.height, rotation: base.rotation, page: pageNumber }); setLoading(false); }
      if (!disposed && textContainer.current) {
        try {
          const textContent = await page.getTextContent({ disableNormalization: true });
          if (disposed || !textContainer.current) { page.cleanup(); return; }
          layer = new TextLayer({ textContentSource: textContent, container: textContainer.current, viewport: base });
          // Keep the PDF.js layer's native dimensions exact (its CSS round vars
          // normally come from PDFViewer, which this canvas-only viewer does not use).
          const nativeSize = base.rawDims as { pageWidth: number; pageHeight: number };
          textContainer.current.style.width = `${nativeSize.pageWidth}px`;
          textContainer.current.style.height = `${nativeSize.pageHeight}px`;
          await layer.render();
          if (!disposed) setSearchLayer(pageNumber);
        } catch { /* Text search is optional; stored geometry remains available. */ }
      }
      page.cleanup();
    })().catch(error => {
      if (!disposed && error?.name !== "RenderingCancelledException") { setFailure("이 쪽을 표시하지 못했습니다. 다른 쪽을 선택하거나 원문을 다시 열어 주세요."); setLoading(false); }
    });
    return () => { disposed = true; render?.cancel(); layer?.cancel(); };
  }, [pdf, pageNumber, zoom]);
  const ready = viewport?.page === pageNumber && !loading && !failure;
  const visible = ready ? refs.filter(ref => ref.physical_page === pageNumber).map(ref => {
    const bound = ref.w18_coordinates?.source_sha256 === sourceSha ? ref : { ...ref, w18_coordinates: null };
    const stored = sourceHighlight(bound, viewport);
    if (["exact", "cell"].includes(stored.precision) || ref.w18_verification_state !== "verified") return { ref, ...stored };
    const boxes = searchLayer === pageNumber && textContainer.current ? textSearchRects(textContainer.current, ref.quote) : [];
    return { ref, ...(boxes.length ? { boxes, precision: "search" } : stored) };
  }) : [];
  const target = visible.find(item => item.ref.kind === "evidence" && item.boxes.length)
    ?? visible.find(item => item.ref.ref_id === "table_observation/cell_source_refs/value" && item.boxes.length)
    ?? visible.find(item => item.boxes.length);
  const targetBox = target?.boxes[0];
  const targetKey = targetBox?.join(",");
  useEffect(() => {
    const container = scroll.current;
    if (!ready || !container || !targetBox) return;
    const reveal = () => {
      const height = canvas.current?.clientHeight ?? 0;
      const width = canvas.current?.clientWidth ?? 0;
      container.scrollTo({ top: Math.max(0, targetBox[1] * height - container.clientHeight * .3), left: Math.max(0, targetBox[0] * width - container.clientWidth * .15), behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" : "smooth" });
    };
    reveal();
    const observer = new ResizeObserver(reveal); observer.observe(container);
    return () => observer.disconnect();
  }, [ready, targetKey, zoom, pageNumber]);
  return <>
    {loading && <p role="status" aria-live="polite">원문 p.{pageNumber} 불러오는 중…{!pdf && progress !== null ? ` ${progress}% 수신` : ""}</p>}
    {failure && <p role="alert">{failure}</p>}
    <div ref={textContainer} className="w18-text-layer" aria-hidden="true" />
    <div ref={scroll} className="split-pdf-scroll" tabIndex={0} aria-label={`원본 PDF p.${pageNumber}`} aria-busy={loading}>
      <div className="split-pdf-page" style={{ width: `${zoom * 100}%`, visibility: ready ? "visible" : "hidden" }}>
        <canvas ref={canvas} aria-label={`원본 PDF 물리 쪽 ${pageNumber}`} />
        {ready && <svg className="split-pdf-overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-label="저장된 원문 인용 위치">
          {visible.filter(item => item.boxes.length).map(({ ref, boxes, precision }) => <g key={ref.ref_id} className={`split-highlight ${ref.kind} ${precision}`} data-testid={`pdf-${ref.kind}-highlight`} data-ref-id={ref.ref_id} data-page={pageNumber} data-precision={precision}>
            <title>{highlightLabel(precision)} · {ref.element_id ? `${ref.element_id} 근거: ` : "주장: "}{ref.quote}</title>
            {boxes.map(([x,y,width,height], index) => <rect key={index} x={x} y={y} width={width} height={height} />)}
          </g>)}
        </svg>}
      </div>
    </div>
    {ready && visible.some(item => !item.boxes.length) && <p role="status">위치 미확인 · 검증된 인용과 이 쪽의 일치를 확인할 수 없습니다.</p>}
    {ready && visible.some(item => item.precision === "estimated") && <p className="split-note split-estimated-note" role="status">{ESTIMATED_LOCATION_LABEL} · 보라색 점선은 파서가 추정한 영역입니다. 주변 문장이 포함될 수 있으며, 원문 확인 필요·보류 상태는 유지됩니다.</p>}
    {ready && visible.some(item => item.precision === "search") && <p className="split-note" role="status">텍스트 검색 위치 · 같은 쪽에서 공백만 정규화하여 유일하게 일치한 인용입니다.</p>}
    {ready && visible.some(item => item.precision === "region") && <p className="split-note" role="status">영역 표시(정확 위치 아님) · 점선은 인용이 포함된 문단 영역이며 주변 문장이 포함될 수 있습니다.</p>}
  </>;
}
