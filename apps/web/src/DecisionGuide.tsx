import { elementLabels } from "./features/labels";

const help = {
  grade: "E0~E3는 문장에 연결된 원문 근거의 수준입니다. 기업 성과의 진위나 법 위반 여부를 판정하지 않습니다.",
  label: "SUBSTANTIATED는 이 규칙의 입증 요건 충족, INCOMPLETE는 일부 충족, UNSUBSTANTIATED는 해당 규칙의 근거 부족을 뜻합니다.",
  track: "관리체계는 조직·절차, 성과는 이미 이룬 결과, 목표는 앞으로 이루려는 수치·계획입니다.",
  state: "근거 확인은 검증된 원문 연결, 미확인은 아직 알 수 없음, 근거 없음은 확인된 부재, 충돌은 자료가 서로 다름입니다.",
  range: "가능 범위는 미해결 요소에 따라 도달할 수 있는 등급의 하한~상한입니다. 확정 등급이 아닙니다.",
  estimated: "핵심 근거 일부가 확인되기 전의 예비 판정입니다.",
};
const helpLabels: Record<keyof typeof help, string> = { grade: "등급", label: "라벨", track: "트랙", state: "요소 상태", range: "가능 범위", estimated: "예비 등급" };

export function GuideHelp({ topic }: { topic: keyof typeof help }) {
  return <details className="guide-help"><summary aria-label={`${helpLabels[topic]} 설명`}>?</summary><div role="note">{help[topic]} <a href="/demo#guide">전체 안내 ↗</a></div></details>;
}

export function DecisionGuide() {
  return <section className="surface decision-guide" id="guide" aria-labelledby="guide-title">
    <p className="eyebrow">READING THE RESULT</p><h2 id="guide-title">등급과 라벨, 이렇게 읽으세요</h2>
    <p className="guide-lead">이 결과는 공시 문장을 뒷받침하는 <strong>원문 근거의 수준</strong>을 보여줍니다. 기업 성과의 진위나 법 위반 여부를 판정하지 않습니다.</p>
    <div className="guide-grid">
      <div><h3>근거 수준 · E0 → E3</h3><p><b>E0</b> 해당 규칙에서 필요한 핵심 근거가 없다고 확인된 단계</p><p><b>E1</b> 첫 핵심 요소가 확인된 단계</p><p><b>E2</b> 범위·비교 기준까지 연결된 단계</p><p><b>E3</b> 해당 트랙의 검증·진척·보증 요건까지 연결된 단계</p><small>요건은 트랙마다 다릅니다. 미확인 상태는 자동으로 E0가 되지 않습니다.</small></div>
      <div><h3>라벨 · 판정의 짧은 이름</h3><p><b>SUBSTANTIATED</b> 이 규칙의 입증 요건 충족 (E3)</p><p><b>INCOMPLETE</b> 일부 요건 충족 (E1·E2)</p><p><b>UNSUBSTANTIATED</b> 해당 규칙의 근거 부족 (E0)</p><small>라벨은 Python 규칙엔진이 등급과 함께 계산합니다.</small></div>
      <div><h3>트랙 · 어떤 주장인가요?</h3><p><b>관리체계</b> 조직·절차·운영 방식</p><p><b>성과</b> 이미 달성하거나 측정한 결과</p><p><b>목표</b> 앞으로 이루려는 수치·계획</p><small>분류 미합의는 아직 트랙을 정하지 않은 상태입니다.</small></div>
      <div><h3>요소 상태 · 근거가 있나요?</h3><p><b>근거 확인</b> 검증된 원문 문구와 위치가 연결됨</p><p><b>미확인 (unknown)</b> 아직 판단할 수 없음</p><p><b>근거 없음 (absent)</b> 해당 근거가 없다고 확인됨</p><p><b>충돌 (conflict)</b> 근거끼리 맞지 않음</p><small>판독 불가는 읽지 못한 상태입니다. 가능 범위는 미해결 요소에 따른 하한~상한이며 확정 등급이 아닙니다.</small></div>
    </div>
    <h3>요소 이름 빠르게 찾기</h3><div className="guide-elements">{(["M", "P", "G"] as const).map(prefix => <div key={prefix}><h4>{({ M: "관리체계", P: "성과", G: "목표" })[prefix]}</h4><dl>{Object.entries(elementLabels).filter(([id]) => id.startsWith(prefix)).map(([id, name]) => <div key={id}><dt>{id}</dt><dd>{name}</dd></div>)}</dl></div>)}</div>
  </section>;
}
