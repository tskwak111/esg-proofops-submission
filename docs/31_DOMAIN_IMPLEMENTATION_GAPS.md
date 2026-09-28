# 31 · 도메인을 바꾸지 않고 처리할 미정 계약

> ESG ProofOps · 개발 명세 1.0 · 2026-09-08
> 도메인 정본: `sources/PROJECT_DOMAIN_V2_ORIGINAL.md` (원문 2.0, 2026-09-07).

## 원칙
이 목록은 사업성 비판이나 새 도메인 제안이 아니다. 원문에 값이 없는 곳에서 코딩 에이전트가 임의 기준을 만들어 넣지 못하도록 하는 실행 계약이다. **새 도메인 기준이 없어도 파서·근거 검색·태깅·검토·감사·명시 사다리 구현은 진행한다.** 범위/조항/매핑 승인 전에는 해당 자동판정만 차단한다. 이 상태를 가짜 완료나 정확도100%로 포장하지 않는다.

기술 구조 변경 권한만으로 위임받지 않은 E 임계값을 만들어서는 안 된다. 사용자가 도메인 변경을 불허했으므로, 새로운 의미를 발명하는 대신 승인된 원문 해석을 버전 기록한다. 단순 계정 값/모델 ID/대회일정은 아래 도메인 gap 이 아니라 배포 입력이다.

### GAP-001 · 원문 §4.6

**빈 계약:** 세이프하버 체크리스트는 있지만 E0~E3·reasonable_basis boolean 판정 조합이 완전하지 않음.

**구현 동작:** 범주·각요소·근거·결손을 출력. 승인 매핑 없으면 E/label/basis_boolean=null, 법적효력 미판정.

**해제 조건:** 기준 담당자가 원문 범위 내 체크리스트→판정 대응을 승인할 때만 활성화.

### GAP-002 · 원문 §4.2/4.6/§7

**빈 계약:** 목표형과 미래 예측 정보의 포함관계가 완전한 분류 규칙으로 정의되지 않음.

**구현 동작:** track 와 safe_harbor_category 를 독립 저장. 단순 goal 을 일괄 safe harbor 로 라우팅하지 않음. 애매하면 category review.

**해제 조건:** 도메인 담당자의 범주 구분 예시 승인.

### GAP-003 · 원문 §4.4/4.5

**빈 계약:** 사다리와 추가 필수/조건부 항목(P6,M4,G7/G8 등)의 최종 등급 영향이 명시되지 않은 조합.

**구현 동작:** 명시 사다리 candidate 와 요소 충족 현황을 분리. 추가 요소가 등급을 바꿀 가능성이 있는 미정 조합은 blocked_rule_gap. 임의 감점/무시 금지.

**해제 조건:** 경계 조합의 원문 해석을 승인된 test vector 로 확정.

### GAP-004 · 원문 §6 2-4 / §7

**빈 계약:** 정량/연도 직접근거와 전역인정 사이에서 기준연도·진척·계획/explicit_link 범위 일부 모호.

**구현 동작:** 숫자·목표연도 전역인정 금지를 우선 보존. scope/method/assurance 만 명시 global 허용. 기타 근거를 자동 확장하지 않음.

**해제 조건:** 요소별 source scope 승인.

### GAP-005 · 원문 §4.3/§7

**빈 계약:** INCOMPLETE(PERF/IMPL)의 모든 트랙·복합결손 매핑이 없음.

**구현 동작:** E/label 이 확정되어도 sublabel 만 null+gap 표시 가능. §7 예시와 같은 goal 결손은 IMPL 을 쓸 수 있음.

**해제 조건:** sublabel truth table 승인.

### GAP-006 · 원문 §4.4/4.6

**빈 계약:** 모든 트랙 최상급 우선과 safe harbor 별도 경로가 겹칠 때 우선순위 미명시.

**구현 동작:** 둘 다 trigger 되면 E 자동 확정 중단, 두 근거 경로 모두 보존.

**해제 조건:** 충돌 사례 priority 승인.

### GAP-007 · 원문 §4.4

**빈 계약:** 목표연도만 있고 목표수치 없음, 수치만 있고 unit 없음, 제품 변형의 일반 경계와의 결합 등 일부 경계 누락.

**구현 동작:** 명시된 branch 가 없으면 blocked_rule_gap. 다른 과거 scorer 의 threshold 로 보완하지 않음.

**해제 조건:** 사다리 edge truth table 승인.

### GAP-008 · 원문 §3.6/13.2

**빈 계약:** 조항번호/기준대응표/재배포 권리 미확정.

**구현 동작:** 원문 사용자 제공 요약과 검증상태 표시, 번호 생성 금지. unverified 를 공식 조항 검증 완료로 표시하지 않음.

**해제 조건:** 공식원문 대조+검토자/URL/일자/권리 기록.

### GAP-009 · 원문 §1.2/4.9/13.1

**빈 계약:** 법제·발효·유예·세이프하버 사실을 현재 시행법으로 재검증한 근거 미제공.

**구현 동작:** 원문 보존, 자동 법적적용 false. 준비용 참고 검토 가능. 면책보장/위법 라벨 금지.

**해제 조건:** 최신 공식자료를 담당자가 확인하고 timeline 승인.

### GAP-010 · 원문 §4.8

**빈 계약:** 실제 GICS→SASB 산업 mapping 및 topic 별 필수/선택 조합 미제공.

**구현 동작:** 산업 미확인을 N/A 로 감춰 분모에서 제거하지 않고 applicability undetermined 표시.

**해제 조건:** 산업 mapping/선택공시 분모 단위 승인.

## 차단의 단위
하나의 unresolved 조항 때문에 모든 문서를 실행 불가로 만들지 않는다. 해당 claim/element 의 decision_status 또는 basis verification 만 제한한다. 예를 들어 sublabel 매핑만 모르면 E/label 은 산출하고 sublabel 만 null 이다. 공식 조항 번호가 미확정이어도 원문 사다리의 계산 결과는 ‘프로젝트 루브릭 기준’으로 표시할 수 있지만 법정 기준 충족 완료로 표시하지 않는다. 반면 safe harbor 등급 mapping 이 없으면 그 경로의 E/label 을 산출할 수 없다.

## 승인 이력
승인은 gap_id, 원문 section, 해석 요약, before/after rulepack hash, 담당자, timestamp, boundary test vector 를 남긴다. 이는 사업 도메인을 고치라는 요청이 아니라 원문의 실행 의미를 명문화하는 작업이다. 원문과 다른 정책이 필요하면 구현자가 임의 반영하지 않고 별도 사용자 승인 대상으로 남긴다.

## AI 검토 해석 제안 (review_origin=ai_project_interpretation, 2026-09-20 R00)

사용자가 조정자의 도메인 판단(원문·사례 근거)을 위임했다. 아래는 **문서 전용 제안**이며 `28_RULE_ENGINE_CONTRACT.md`의 런타임 `decision_status` enum(예: `blocked_rule_gap`)에 새 값을 추가하지 않는다. 채택만으로 실행 결과를 바꾸지 않는다. 해당 해석을 버전 고정하여 구현·검증한 경로만 기존 decision_status 계약에 따라 판정하고, 아직 미구현·미정인 경로는 `blocked_rule_gap`을 유지한다. AI 검토 출처는 승인 이력에 `review_origin=ai_project_interpretation`으로 기록하며, 기존 `rule_ids`/`basis` 스키마에 임의 필드를 넣지 않는다. 공식 기준 검증 상태는 그대로 미검증이다. 이는 독립 전문가 gold나 공식 기준 원문 검증이 아니라 **프로젝트 내부 해석**이며, 세이프하버의 법적 면책 효과를 단정하지 않는다. C군 출력·label/evidence_grade와 절대 섞지 않는다.

| GAP | 원문 절 | 제안 해석(truth-table) | 양성 예 | 음성 예 | 경계 예 |
|---|---|---|---|---|---|
| GAP-001 (세이프하버 매핑) | §4.6 | `config/regulatory/safe_harbor.yaml`에 이미 있는 `category_checklists`(범주별 named item, 예: emissions_estimate=`identified_as_estimate`/`estimation_method`/`uncertainty`)를 그대로 사용한다. 새 boolean/필드를 만들지 않는다. 각 checklist item의 상태는 기존 `ElementState`(present/absent/unknown/conflict)로 판정하며, **추출 실패나 검색 미완료로 인한 결측은 absent(=false)로 강제하지 않고 unknown으로 유지**한다. `reasonable_basis_documented`는 모든 item이 present일 때만 true, 하나라도 absent(검색 완료 후 원문에 없음이 확인됨)면 false, unknown이 하나라도 있으면 null(판정 보류) — 문구 존재만으로 의미 충족을 자동 인정하지 않음(item 문구가 있어도 해당 범주 요건을 실질적으로 충족하는지는 여전히 태깅 판단) | 세 item 모두 present(검색 완료, 원문 확인) → true | 검색 완료 후 estimation_method가 absent로 확정 → false | uncertainty item이 검색 미완료(unknown)로 남음 → null, false로 강제하지 않음 |
| GAP-002 (goal vs safe-harbor 범주) | §4.2/4.6/§7 | `track=goal`과 `safe_harbor_category`는 계속 독립 필드. goal 클레임에 "추정" "예상" "전망" 등 명시적 예측 표지 또는 산정 불확실성 서술이 있으면 category candidate로 표시, 없으면 category=null(일반 goal 사다리만 적용) | "2030년까지 40% 감축 목표"(단순 goal) → category=null | "예상 배출량은 산정 방법상 ±10% 변동 가능"이 목표 문장에 결합 → category candidate="emissions_estimate" | 목표 문장에 "전망"이라는 단어만 있고 추정 근거 서술 없음 → category candidate 표시하되 review 필요, 자동 확정 안 함 |
| GAP-003 (추가요소 등급효과: P6/M4/G7/G8) | §4.4/4.5 | 조건부 요소(G7/G8/M5/M6)는 트리거되지 않으면 `excluded_elements`에 `conditional_not_triggered`로만 기록하고 E등급 사다리 계산에서 제외(28장 §3 truth table과 합치). **P6(본문·표 수치 불일치)는 P1을 임의로 무효화(satisfied=false 강제/E1 상한)하지 않는다** — 이는 원문에 없는 임의 조합이었으므로 철회한다. 대신: source-verified된 동일 범위(같은 지표·연도·대상)의 본문값과 표값이 불일치하면 해당 수치 사실(fact)을 `conflict` 상태로 표시하고, 그 conflict가 등급 결정에 영향을 주는 경우(P1 등 필수요소의 근거로 그 수치를 쓰는 경우)에만 `blocked_rule_gap`으로 자동 확정을 중단한다. 트리거 조건이 **명시적으로 false로 확인된 경우에만** conditional 요소를 not_applicable로 표시하고, 트리거 여부가 unknown이면 not_applicable로 만들지 않는다 | 본문·표 수치 일치 + 나머지 충족 → E3 가능(변경 없음) | 같은 지표·연도의 본문·표 값이 서로 다름(범위 확인됨) → 그 수치 사실 conflict, P1이 그 수치에 의존하면 blocked_rule_gap; 다른 필수요소로 이미 E1이 결정되면 등급 자체는 유지 가능(임의 상한 없음) | G7 트리거 여부가 태깅 단계에서 unknown(상쇄 언급 여부 미확인) → not_applicable 금지, 미확정으로 유지(GAP-005와 연결) |
| GAP-005 (sublabel truth table) | §4.3/§7 | **원문 §7 예시를 정확히 반영해 수정**: 원문은 "목표연도+목표수치 있으나 기준연도·적용범위 없음 → E1/INCOMPLETE(**IMPL**)"이다(`PROJECT_DOMAIN_V2_ORIGINAL.md` L613). 즉 G3(기준연도, PERF성 요소로 보였던 것)의 결손이 실제로는 IMPL로 매핑된 유일한 확정 사례이므로, "결손이 전부 P류면 PERF/전부 M류면 IMPL"이라는 이전 대칭 규칙은 이 명시 예시와 충돌해 철회한다. 이번 제안은 **이 예시 하나만 그대로 보존**하고 일반화하지 않는다: goal 트랙에서 G3(기준연도·기준값)+G4(적용범위) 둘 다 결손이면 sublabel=IMPL(원문 예시와 동일 조합에 한정). 그 외 결손 조합(단일 요소 결손, performance/management 트랙, G3만 결손 등)은 원문에 대응 예시가 없으므로 sublabel=null로 유지하며, 이 null이 이미 확정된 E/label 산출을 막지 않는다(sublabel만 비워둠) | goal 트랙, 결손=[G3,G4] 동시(원문 예시와 동일 조합) → sublabel=IMPL | 원문에 이 조합의 PERF 매핑 예시 없음 — 반례 없음, 대칭 규칙 자체를 철회 | goal 트랙, 결손=[G3]만(G4는 충족) → 원문 예시와 다른 조합이므로 sublabel=null 유지, E/label은 그대로 산출 |
| GAP-006 (최상급 vs 세이프하버 우선순위) | §4.4/4.6 | 두 특칙이 동시 트리거되면(최상급 표현 + safe_harbor_category candidate) 어느 쪽도 자동 우선하지 않고 `E=null`, 두 근거 경로(비교기준/외부검증 상태, reasonable_basis 상태)를 모두 `applied_elements`/`safe_harbor`에 병기. review_status=needs_review 강제 | 최상급 없음, safe harbor만 트리거 → 세이프하버 경로만 적용(충돌 아님) | 최상급 있고 비교기준·외부검증 모두 있음(E0 특칙 미해당), safe harbor 미트리거 → 일반 사다리만 적용 | 최상급(비교기준 없음) + 배출추정치(방법 있음) 동시 → E=null, 두 경로 기록, 자동확정 금지 |
| GAP-007 (사다리 빈 조합) | §4.4 | 목표연도 없음+목표수치 있음+전환계획 있음(28장 E1 상한 예외의 "수치 있음"이 지표 없이 숫자만인 경우), 수치 있음+단위 없음(P1)은 명시 branch 없음 → 계속 `blocked_rule_gap`. 단 "제품 변형(특정 모델+재활용소재)과 일반 경계"의 결합은 v2.2 원문에 없으므로 4.4절 제품 특성 변형 규칙(모델명·소재명만→E1 상한, 함유비율/목표수치가 해당 제품을 직접 언급해야 E2+)을 그대로 적용하고 새 branch를 만들지 않음 | 제품명+재활용소재 40% 함유 명시 → E2 가능(원문 규칙 그대로) | 제품명만, 비율 없음 → E1 상한(원문 규칙 그대로) | 목표수치는 있으나 단위 없음("30 감축") → blocked_rule_gap 유지(해석 제안 없음, 원문에 branch 없음) |

**적용하지 않는 것**: GAP-004(전역근거 범위), GAP-008(조항번호), GAP-009(법적 시행), GAP-010(산업 매핑)은 원문 대조·외부 확인이 선행돼야 하는 사실관계형 gap이라 이번 AI 해석 대상에서 제외한다. 임의 조항 번호나 법적 효력을 만들지 않는다.

**해제 조건 (정정: D 승인이 유일한 게이트 아님)**: 사용자가 조정자의 도메인 판단을
원문·사례 근거 기반으로 명시 위임했다. 활성화의 기술적 게이트는 **조정자 채택 +
rulepack 버전 기록**(gap_id/원문절/before-after hash/timestamp/boundary test vector,
`담당자=coordinator(AI-delegated)`)이며, 도메인 담당자(D)의 별도 승인이 반드시 선행돼야
하는 것은 아니다. 다만 `review_origin=ai_project_interpretation`은 유지해 독립 전문가
검증이나 공식 기준 원문 확인으로 위장하지 않는다.

### 제한 채택: GAP-001 체크리스트 문서화 완결성 (2026-09-20 R07b)

사용자의 판단 위임에 따라 coordinator(AI-delegated)가 위 제안 중 체크리스트
boolean 해석만 채택했다. `reasonable_basis_boolean_mapping`의 지원 값은 기존
`null` 또는 `project_checklist_completeness_v1`이다. 후자는 고정된 범주 checklist가
비어 있지 않고 모든 fact가 검증된 present이면 true, 검증된 absent가 하나라도
있으면 false(unknown/conflict와 혼합돼도 false), 그 밖의 unknown/conflict/누락이면
null이다. absent는 기존 `ConfirmedFact.search_coverage_verified` 경계를 통과해야
한다. 판독 불가는 기존 unknown/conflict로 남기고 raw LLM absent를 승인하지 않는다.
빈 checklist나 not_applicable만으로 true를 만들지 않는다.

이는 프로젝트 루브릭의 **문서화 완결성**이며 법적 보호나 E등급 대응이 아니다.
`grade_mapping=null`, `mapping_status=unresolved`, `legal_effect=not_determined`와
`GAP-001`을 유지하고 E/label은 바꾸지 않는다. GAP-008/009/010도 해제하지 않는다.
기존 null pack/config와 실행 snapshot은 그대로 보존한다. 명시적
`scripts/review_rulepack.py --checklist-policy project_checklist_completeness_v1`
및 `--derived-version <새 버전>`, 또는 `derive_checklist_policy_pack`만 새 ID/버전/hash의
파생 pack을 만들며 원문 §4.6, 위임·AI 검토자·시각·정책 ID·before hash·경계 벡터를
content에 고정한다. after hash는 파생 pack 및 기존 활성화 검토 이력에 기록한다.
CLI는 `--apply` 전에는 저장/활성화하지 않는다. 기존 helper의 기본 동작은 유지한다.
현 태깅 consensus는 G/P/M fact만 생산하므로 체크리스트 producer는 다음 필요 작업이며,
이 채택이 실제 문서 end-to-end 검증 완료를 뜻하지 않는다.

### 제한 채택: 원자 주장 조건부 trigger의 local-claim 해석 (2026-09-20 R07c)

사용자의 명시적 판단 위임에 따라 coordinator(AI-delegated)는 원문 §4.5의
G7/G8/P5/M5/M6 조건을 **해당 원자 주장이 무엇을 주장하는가**로 채택했다.
전체 원자 주장 원문과 provenance를 재검증한 명시적 AI 검토만 true/false를 기록한다.
false는 보고서 전체에서 근거가 검색되지 않았다는 의미가 아니며, 판독 불완전·모호함은
unknown으로 유지한다. 키워드 누락·모델 합의·quality boolean만으로 false를 만들지 않는다.
정책 `local_claim_applicability_v1`은 검토 당시 rulepack/input 해시와 검토 사유·출처를
불변 revision에 보존한다(28장 R07c 계약). `review_origin=ai_project_interpretation`을
유지하며 기존 rulepack·모델 출력·원문 claim은 수정하지 않는다. 일반 요소 부재,
공식 기준 검증, 독립 전문가 gold 또는 다른 GAP의 승인을 뜻하지 않는다.
