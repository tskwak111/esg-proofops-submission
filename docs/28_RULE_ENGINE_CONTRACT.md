# 28 · 결정론적 판정·요소 계약

> ESG ProofOps · 개발 명세 1.0 · 2026-09-08
> 도메인 정본: `sources/PROJECT_DOMAIN_V2_ORIGINAL.md` (원문 2.0, 2026-09-07).

## 1. 입력과 출력
입력은 ConfirmedTags, RuleContext(company/기간/산업/모드/유예), RulePack snapshot 이다. 출력 Decision 은 grade/label/sublabel, decision_status, review_status, missing_elements, excluded_elements, rule_ids, basis refs, safe_harbor record, semantic hash 를 갖는다. 함수는 네트워크·시스템시간·모델 호출 없이 순수하게 계산한다. actor/timestamp 는 외부 audit metadata 이며 semantic hash 에서는 제외한다.

`ElementState`는 present/absent/unknown/conflict/not_applicable. present 는 `citation_verified AND binding_accepted`; absent 는 source search coverage 요건 충족; unknown/conflict 를 False 로 cast 하지 않는다. not_applicable 은 승인된 conditional trigger/산업·유예 적용성에서만 만들 수 있다. 표 판독 실패는 missing_elements 에 넣지 않고 unresolved_elements 에 둔다.

## 2. 적용 순서
1. 입력 schema/hash/source 동일성, 미처리·unknown 의 grade 영향 검사.
2. public disclosure vs advertising mode 선택, 산업·유예의 적용/필수/참고 축 기록.
3. 세이프하버 범주와 최상급 충돌 검사. safe harbor 만이면 별도 checklist 경로; 등급 mapping 미승인은 null.
4. 일반트랙에서 최상급 비교+검증 모두 부재이면 E0 특칙.
5. goal/performance/management 의 원문 사다리 중 가장 높은 명시 branch 선택.
6. 범주형 인증·제품 변형 조건 검사, 원문 미정 조합 gate 검사.
7. 필수/조건부 요소별 충족·결손·제외와 조항 검증 상태를 붙인다.
8. E→label 고정 함수, sublabel 은 명시된 매핑만 사용. audit-ready 결과 hash 생성.

자동 확정 판정과 최종 본문 표현은 명시된 필수/조건부 루브릭의 해석을 넘어가지 않는다. 등급에 영향을 주는 미정 조합은 internal ladder candidate 만 남기고 공개 grade 는 blocked_rule_gap 으로 유지한다.

R07b: 별도 checklist 경로의 `reasonable_basis_boolean_mapping=null`은 기존 결과를
그대로 보존한다. 명시적 새 pack의 `project_checklist_completeness_v1`만 검증된
비어 있지 않은 전체 present → true / 검증된 any absent → false / 나머지 → null을
계산한다(31장 제한 채택). API/DB 필드 변경·migration은 없다. E/label, 법적 효력,
grade mapping 미정 상태는 유지한다. 롤백은 새 실행을 기존 null pack에 고정하며
이미 생성된 파생 pack·revision은 삭제/덮어쓰지 않는다. 구버전 reader는 새 policy를
거부하므로 해당 실행은 호환 reader에 남겨야 한다.

## 3. 사다리 truth table
| 트랙 | E0 | E1 | E2 | E3 |
|---|---|---|---|---|
| goal | target year 없음(예외 아래) | year+metric | E1+baseline year/value+scope/org boundary | E2+current progress+transition plan |
| performance | 정량/인정 ordinal 없음 | value+unit 또는 인정 ordinal | E1+comparison baseline+boundary | E2+method **AND** covered assurance |
| management | willingness only | named means 또는 concrete state | E1+boundary | E2+external verification |

목표연도가 없지만 목표수치와전환계획 모두 있으면 E1상한. 일반 management/product naming 만으로 E2 승격 금지. 최상급 조건은 `NOT comparison AND NOT external_verification`이며 `OR`가 아니다. unknown 은 부재로 세지 않는다. 이 table 로 결정되지 않는 edge 는31장 GAP-007이다.

## 4. 원문 요소와 primitive facts
G1=target_year, G2=target_metric, G3=(baseline_year AND baseline_value), G4=(scope AND org_boundary), G5=current_progress, G6=transition_plan. P1=value/unit(또는 named provider 의 categorical ordinal), P2=comparison_baseline, P3=(method AND calculation_boundary), P4=assurance.status==covered. P5는 absolute/intensity 구분, P6는 numerical check 결과다. M1은 named_means OR concrete_state, M2=boundary, M3=external_verification, M4=concrete_implementation_detail.

원문은 G4에 Scope·조직경계를 함께 요구하므로 두 subfact 를 분리 저장한다. 비 GHG 주장에 GHG Scope 용어가 적용되지 않는 경우에는 업종/지표별 승인 applicability mapping 이 필요하고 모든 환경주장에 Scope1/2/3를 억지로 요구하지 않는다. 산정 단위·경계는 해당 지표에 맞는 값을 사용한다. 이런 applicability 해석이 없으면 검토 대상으로 남긴다.

## 5. 충족률과 분모
전수성은 pages/chunks/claims 처리량이다. 등급 분포 분모는 `decided`인 claim 수이고 옆에 미판정 수를 함께 둔다. 기준 충족률은 승인된 applicable requirement instance 중 satisfied 수/known applicable 수이며, `not_applicable`은 제외한다. `unknown applicability`는 excluded 로 숨기지 않고 별도수와 ‘산정 범위 미확정’을 표시한다. applicable 수0이면 rate=null, 0%로 만들지 않는다. 선택공시는 법정 필수 충족률의 분모와 섞지 않는다.

Scope3 유예는 법정 필수 축을 제외하되 사용자가 주장한 내용의 내부 입증을 참고 검토할 수 있다. 유예 때문에 근거 없는 claim 이 E3이 되지 않고, 내부 입증 부족을 법정 위반으로 부르지 않는다. 표시광고 참고 기준은 disclosure mode 의 grade 입력에서 완전히 제외한다.

## 6. Rules-only rescore
새 pack 의 element ontology/source scopes 가 기존 태깅보다 확장되지 않고 필요한 facts 가 모두 저장돼 있으면 재태깅 없이 새 revision 을 계산한다. 새로운 ‘추정 불확실성’ 필드처럼 과거에 수집하지 않은 정보가 필요한 변경은 retag_required 다. 같은 raw 태그에 기준만 바꾸는 것과 과거 raw 에 없던 사실을 추론하는 것을 구분한다. 규칙집합·엔진버전·입력 tag hash 가 달라지면 semantic hash 도 바뀐다.

## 7. 테스트 코드의 형태 (구현 후 실제 함수에 연결)
```python
def test_performance_requires_method_and_assurance(engine, performance_e2_tags, approved_test_pack):
    with_method = performance_e2_tags.with_fact("method", True)
    assert engine.evaluate(with_method, approved_test_pack).evidence_grade == "E2"
    with_both = with_method.with_fact("assurance_covered", True)
    assert engine.evaluate(with_both, approved_test_pack).evidence_grade == "E3"
```
이 예시의 fixture 에는 P6 등 추가 적용 항목이 해결된 것으로 명시한다. mock rule engine 으로 기대 결과를 되돌리는 방식으로 테스트를 통과시키지 않는다. fixture 의 builder API 는 TASK-014에서 실제 타입으로 구현한다.


## 산정범위의 원문 충실성
성과형 E2의 `calculation_boundary`는 원문 §4.4의 “산정범위”를 뜻한다. 모든 성과형 환경 주장에 GHG Scope와 조직경계 두 필드를 무조건 요구하는 새 규칙으로 바꾸지 않는다. GHG 하위 점검은 해당 지표에 적용되는 범위에서 별도 태깅한다.

## 직접 인용과 근거 연결의 기술적 구분 (2026-09-19)

원문 §4.1의 페이지·원문 스팬, §4.4의 관리체계 사다리, §6의 직접근거 규칙을
함께 적용한다. 검증된 원자 클레임 스팬 안에 정확히 포함된 인용이고 해당 요소가
`local_claim`을 허용하면, 그 인용의 **위치상 귀속**에는 별도 기업·지표·보고기간
조인 키를 요구하지 않는다. 예를 들어 관리체계의 명명된 표준을 확인하는 데 보고기간
리터럴을 새로 만들어 넣지 않는다. 이는 인용이 같은 원자 클레임 안에 있다는 판정이며,
인용 내용이 M1/M2 등의 의미를 충족하는지, 적용성·등급이 확정됐는지는 별도다.

제공된 역할 스팬은 하나만 있어도 원문·소유 범위·기간 형식을 검증하며, 양쪽 역할의
충돌은 거부한다. 추가 적용 축(product/material/facility/scope/boundary)에 명시된
null은 계속 미확정이다. 구절 범위의 충돌·이탈·모호성은 역할 미제공과 구분하여
binding=undetermined로 남긴다. 기본 기업·지표·보고기간의 null을 실제 값이나
not_applicable로 바꾸지 않는다.

같은 보고서의 다른 문단·표·부록 근거는 기존 기업·지표·기간·적용 축 및 행열 검사를
그대로 거친다. 다른 연도/제품 수치를 차용하거나 숫자·목표연도를 문서 전역에서
끌어오는 것은 허용하지 않는다. LLM의 등급 결정, 새 조항/루브릭 기준, 도메인 승인,
기존 revision 변경은 이 기술 구분에 포함되지 않는다.

### R07c: 위임 AI의 원자 주장 조건부 적용 검토

기존 로컬 `resolve_ai_delegated_review(..., applicability_review=...)` 및
`scripts/review_ai_delegated.py --applicability-review-json <파일> --apply`만
`local_claim_applicability_v1` 검토를 받는다. 별도 JSON은 `policy`,
`input_snapshot_sha256`, `track`, `claim_source_refs`, `source_authority`,
`triggers`만 포함한다. 각 trigger는 `name/value/reason`이며 value는 true/false/null이다.
이름은 고정 rulepack의 해당 track 조건부 trigger와 지원된 다섯 claim trigger의
교집합이다. 2026-09-25부터 고정 track 사다리 분기가 `willingness_only`를 쓰면(관리체계 E0 "의지 표현만")
같은 원자 주장 검토로 그 값도 받는다. 다른 track에서는 거절한다. 누락·null은 false가 아니다. HTTP 수정 body는 기존 4키를 유지한다.

trusted loader가 원문 provenance를 재생하고, 검토 refs가 원자 주장 전체 refs와
정확히 같으며 모든 span이 재검증된 경우에만 적용한다. false의 coverage는
`local_claim` 안에서 "이 주장이 해당 내용을 주장하는가"의 검토이며 보고서 전체
근거 부재를 뜻하지 않는다. 일반 요소 absent/N/A 생성 권한은 추가하지 않는다.
track 변경 시 기존 trigger를 제거한다. 판정·excluded_elements는 기존 Python
engine이 계산한다. 검토 요청·검증 span·claim/graph/source/packet/rulepack/input 해시,
AI 검토자·위임·사유·출처는 새 immutable tag revision 및 기존 audit hash에 묶인다.
If-Match와 idempotency는 유지하며 새 option도 재시도 identity에 포함한다.
공개 DTO/DB migration은 없다. 미사용 경로와 기존 revision은 유지하고 rollback은
새 option 사용 중단이다. 독립 human/gold 확인으로 취급하지 않는다.
