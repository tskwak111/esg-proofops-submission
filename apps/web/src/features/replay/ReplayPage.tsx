import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router";
import { buildReplayModel, replayDuration, stageAt } from "./replayData";
import "./replay.css";

export type ReplayPageProps = { snapshot?: unknown };

const formatCount = (value: number | null) => value === null ? "—" : value.toLocaleString("ko-KR");
const formatTime = (seconds: number | null) => {
  if (seconds === null) return "—";
  const minutes = Math.round(seconds / 60);
  return `${Math.floor(minutes / 60)}시간 ${minutes % 60}분`;
};
const playbackClock = (ms: number) => {
  const seconds = Math.floor(ms / 1000);
  return [Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60), seconds % 60].map(part => String(part).padStart(2, "0")).join(":");
};

export default function ReplayPage({ snapshot }: ReplayPageProps) {
  const [loaded, setLoaded] = useState<unknown>(null);
  const [error, setError] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const model = useMemo(() => snapshot || loaded ? buildReplayModel(snapshot ?? loaded) : null, [snapshot, loaded]);
  const duration = model ? replayDuration(model.stages) : 0;
  const finished = !!model && elapsed >= duration;

  useEffect(() => {
    if (snapshot !== undefined) return;
    const controller = new AbortController();
    fetch(`${import.meta.env.BASE_URL}demo/naver-2025.json`, { signal: controller.signal })
      .then(response => { if (!response.ok) throw new Error("snapshot unavailable"); return response.json() as Promise<unknown>; })
      .then(setLoaded)
      .catch(() => { if (!controller.signal.aborted) setError(true); });
    return () => controller.abort();
  }, [snapshot]);

  useEffect(() => {
    if (!model || finished) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      setElapsed(duration);
      return;
    }
    const started = performance.now() - elapsed;
    const timer = window.setInterval(() => setElapsed(Math.min(duration, performance.now() - started)), 80);
    return () => window.clearInterval(timer);
  }, [model, duration, finished]);

  if (error) return <main className="static-main replay-page"><div className="replay-error"><h1>분석 결과를 불러오지 못했습니다</h1><p>잠시 후 다시 시도해 주세요.</p><Link to="/analyze">분석 화면으로 돌아가기</Link></div></main>;
  if (!model) return <main className="static-main replay-page"><p role="status" className="replay-loading">분석 결과를 불러오는 중입니다…</p></main>;

  const current = stageAt(model.stages, elapsed);
  const progress = duration ? Math.min(100, elapsed / duration * 100) : 100;
  const tick = (stageIndex: number) => {
    const count = model.stages[stageIndex].count;
    if (count === null) return null;
    if (finished || current.index > stageIndex) return count;
    if (current.index < stageIndex) return 0;
    return Math.round(count * current.fraction);
  };
  const logs = model.stages.flatMap((stage, index) => {
    const start = model.stages.slice(0, index).reduce((sum, previous) => sum + previous.durationMs, 0);
    if (elapsed < start) return [];
    const count = index === current.index && !finished ? tick(index) : stage.count;
    const page = index === 2 && model.samplePage !== null ? `p.${model.samplePage} 포함 · ` : "";
    return [{ time: playbackClock(start), title: stage.title, message: `${page}${count === null ? stage.detail : `${formatCount(count)}${stage.unit} 처리`}` }];
  }).slice(-5);

  return <main className="static-main replay-page">
    <div className="breadcrumb"><Link to="/">홈</Link><span>/</span><Link to="/analyze">보고서 분석</Link><span>/</span>분석 흐름</div>
    <section className="replay-hero" aria-labelledby="replay-title">
      <div className="replay-hero-top"><span className="replay-kicker"><i /> ANALYSIS FLOW</span></div>
      <div className="replay-hero-body"><div><h1 id="replay-title">보고서 한 권이<br /><em>검토 결과</em>가 되기까지</h1><p>{model.title}</p></div><div className="replay-hero-number"><strong>{finished ? formatCount(model.claimsDisplayGraded) : formatCount(tick(6))}</strong><span>건 {model.displayCounts ? "분석 등급" : "판정 기록"}</span></div></div>
      <div className="replay-progress-heading"><span>{finished ? "분석 완료" : `${model.stages[current.index]?.title ?? "완료"} 진행 중`}</span><strong>{Math.round(progress)}%</strong></div>
      <div className="replay-progress-track" role="progressbar" aria-label="분석 흐름" aria-valuenow={Math.round(progress)} aria-valuemin={0} aria-valuemax={100}><div style={{ width: `${progress}%` }} /></div>
      <div className="replay-hero-bottom"><span>{playbackClock(elapsed)} / {playbackClock(duration)}</span>{finished ? <button type="button" onClick={() => setElapsed(0)}>처음부터 보기 ↺</button> : <button type="button" onClick={() => setElapsed(duration)}>결과 바로 보기 ↗</button>}</div>
    </section>

    <div className="replay-metrics" aria-label="분석 수치">
      <div><span>페이지 파싱</span><strong>{formatCount(tick(1))}<small> / {formatCount(model.pagesTotal)}쪽</small></strong><p>선택 페이지 기준</p></div>
      <div><span>주장 추출</span><strong>{formatCount(tick(2))}<small>건</small></strong><p>원자 주장</p></div>
      <div><span>원문 검증</span><strong>{formatCount(tick(3))}<small>건</small></strong><p>페이지·인용 대조</p></div>
      <div><span>규칙 판정</span><strong>{formatCount(tick(6))}<small>건</small></strong><p>{model.displayCounts ? "예비 등급 포함" : "판정 결과"}</p></div>
    </div>

    <div className="replay-grid">
      <section className="replay-timeline" aria-labelledby="replay-steps-title"><div className="replay-section-head"><div><span className="eyebrow">PIPELINE</span><h2 id="replay-steps-title">처리 과정</h2></div><span>01 — 09</span></div>
        <ol>{model.stages.map((stage, index) => {
          const state = finished || index < current.index ? "done" : index === current.index ? "active" : "waiting";
          return <li key={stage.title} className={`replay-step ${state}`}><span className="replay-step-dot" aria-hidden="true">{state === "done" ? "✓" : String(index + 1).padStart(2, "0")}</span><div className="replay-step-copy"><div><h3>{stage.title}</h3><span>{state === "done" ? "완료" : state === "active" ? "진행 중" : "대기"}</span></div><p>{stage.detail}</p></div><strong>{state === "waiting" ? "—" : formatCount(tick(index))}<small>{stage.unit}</small></strong></li>;
        })}</ol>
      </section>

      <div className="replay-side">
        <section className="replay-console" aria-labelledby="replay-log-title"><div className="replay-console-top"><div><span className="replay-console-light" /><h2 id="replay-log-title">처리 기록</h2></div><span>ANALYSIS</span></div><p className="replay-console-caption">단계별 처리 현황</p><div className="replay-log-lines" aria-live="off">{logs.map(log => <p key={log.title}><time>[{log.time}]</time><span>{log.title}</span> {log.message}</p>)}</div><div className="replay-console-foot"><span className={finished ? "complete" : ""} />{finished ? "모든 단계 완료" : "단계별 분석 중"}</div></section>
        <section className="replay-run-stats" aria-labelledby="replay-run-title"><div className="replay-section-head"><div><span className="eyebrow">PROCESS DATA</span><h2 id="replay-run-title">분석 처리 현황</h2></div></div><dl><div><dt>파싱·추출</dt><dd>{formatTime(model.parseSeconds)}{model.parseSeconds !== null && <small className="replay-seconds">{formatCount(model.parseSeconds)}초</small>}</dd></div><div><dt>관계·태깅</dt><dd>{formatTime(model.taggingSeconds)}{model.taggingSeconds !== null && <small className="replay-seconds">{formatCount(model.taggingSeconds)}초</small>}</dd></div><div><dt>유료 모델 호출</dt><dd>{formatCount(model.paidCalls)}<small>회</small></dd></div><div><dt>모델 비용</dt><dd>{model.costUsd === null ? "—" : `$${model.costUsd.toFixed(2)}`}</dd></div>{model.demoPass && <div><dt>{model.demoPass.label}</dt><dd>{formatCount(model.demoPass.count)}<small>건</small></dd></div>}{model.demoPassStats && <><div><dt>추가 분석 호출</dt><dd>{formatCount(model.demoPassStats.calls)}<small>회</small></dd></div><div><dt>추가 분석 비용</dt><dd>{model.demoPassStats.costUsd === null ? "—" : `$${model.demoPassStats.costUsd.toFixed(2)}`}</dd></div><div><dt>추가 분석 시간</dt><dd>{formatTime(model.demoPassStats.seconds)}{model.demoPassStats.seconds !== null && <small className="replay-seconds">{formatCount(model.demoPassStats.seconds)}초</small>}</dd></div></>}</dl></section>
        <section className="replay-funnel" aria-labelledby="replay-funnel-title"><div className="replay-section-head"><div><span className="eyebrow">PROCESS FLOW</span><h2 id="replay-funnel-title">단계별 처리 기록</h2></div></div><div>{model.funnel.map(item => <div className="replay-funnel-row" key={item.label}><span>{item.label === "표시 등급" ? "등급 결과" : item.label}</span><div><i style={{ width: `${model.claimsDiscovered ? Math.max(3, item.count / model.claimsDiscovered * 100) : 0}%` }} /></div><strong>{formatCount(item.count)}</strong></div>)}</div></section>
      </div>
    </div>
    {finished && <section className="replay-finish" aria-live="polite"><div><span className="eyebrow">REVIEW RESULTS</span><h2>{formatCount(model.claimsDisplayGraded)}건 {model.displayCounts ? "분석 등급" : "판정 완료"}</h2><p>{model.displayCounts ? `${formatCount(model.displayCounts.confirmed)}건 확정 · ${formatCount(model.displayCounts.estimated)}건 예비 등급 · ${formatCount(model.displayCounts.sourceUnverified)}건 원문 대조 필요` : "주장별 원문 근거와 판정 경로를 살펴보세요."}</p></div><Link to="/demo">결과 보기 <span aria-hidden="true">↗</span></Link></section>}
  </main>;
}
