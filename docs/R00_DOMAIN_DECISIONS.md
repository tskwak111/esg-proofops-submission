# 팀 도메인 결정

이 문서는 프로젝트가 승인한 구현 규칙을 기록한다. 원문 v2.0의 도메인 정의를 변경하지 않으며, 공식 조항·법적 효력·데이터 권리 검증을 대신하지 않는다. 승인된 정책은 새 실행 또는 명시적 재채점에만 적용하고 기존 revision은 보존한다.

## 등급 사다리와 추가 요소

- 원문 §4.4 트랙별 등급 사다리를 시연·내부 검토의 채점 규칙으로 사용한다.
- G7·G8·P5·P6·M4·M5·M6는 등급을 바꾸지 않고 `missing_elements` 또는 `unresolved_elements`에 기록한다. 미확정 요소는 사람 검토 대상으로 둔다.
- 새 규칙집은 새 실행 또는 명시적 재채점에만 적용한다. 알 수 없는 정책 값은 차단한다.

## 가능 등급 범위

사다리 요소가 unknown이라 `blocked_evidence`인 경우, 모든 가능한 조합이 명시된 사다리 분기에 속하고 도달 가능한 등급이 둘 이상일 때만 `grade_range={floor, ceiling, open_elements}`를 보여준다. 이는 확정 등급이나 라벨이 아니며 `evidence_grade`와 `label`은 null로 유지한다. 별도 세이프하버 경로와 미정 분기에서는 범위도 null이다. 기존 판정·보고서는 재작성하지 않는다.

## 실제 실행의 재채점

비합성 실행은 대상 규칙집에 활성화 및 승인자·승인 시각 기록이 있을 때만 재채점한다. 그렇지 않으면 `RULEPACK_APPROVAL_REQUIRED`로 거절한다. 원문 인용 재검증, 입력과 태그 고정, 테넌트·문서 동일성, CAS, 불변 revision 검사는 유지한다.

## 문서 전역 근거

- M3 `GRI_ASSURED_PAGE_V1`: 검증된 GRI Index 행·검증 대상 목록·검증 기준을 함께 확인한다. 주장 쪽이 Index 범위에 들고 같은 공시번호가 검증 대상에 있어야 한다. `credited_from`은 검증 대상 목록 span이다.
- M2 `REPORT_SCOPE_V1`: 검증된 보고 범위 문장과 조직 단위를 확인한다. 주장 자체에 범위 단서가 있으면 전역 범위를 쓰지 않는다. `credited_from`은 해당 문장이다.
- 두 경로 모두 검증된 원문 span이 필요하다. 숫자·목표연도에는 적용하지 않는다. 검증되지 않은 원문을 전역 근거로 인정하지 않는다.

## 데이터 검토 결정

비율형 목표에도 `baseline_period`와 `baseline_value`가 필요하다. 목표 대비 직접 명시된 진척만 `current_progress=present`이며 계산값은 별도 `derived` 기록이다. 보증 `covered`는 주장 수치와 보증 대상 인벤토리의 지표·기간·경계를 각각 원문 검증해 연결했을 때만 산출한다. `unknown→absent`는 전체 문서 검색 범위·판독 가능성·검색 기록이 완결된 뒤에만 허용한다. 검토된 정정은 새 불변 revision으로 기록한다.

## 미정 규칙에 대한 채택 정책

아래는 팀의 프로젝트 정책이며 외부 기준의 공식 판정이나 법적 효력을 주장하지 않는다. 공식 조항·권리 확인이 필요한 항목은 계속 미검증 상태다.

| 항목·채택 선택지 | 채택 규칙 원문 (`drafts.json`의 `rule_text`) | 출처 확인 상태 (`drafts.json`의 `official_support`) |
|---|---|---|
| RQ-03 · A | conflict와 각 source alternative를 보존한다. 모든 유효한 alternative로 ladder 결과를 계산할 수 있으면 grade는 null로 두고 가능한 결과만 grade_range로 표시한다. 조합을 확정할 수 없으면 grade=null, grade_range=null, blocked_evidence로 둔다. conflict를 present/absent로 cast하지 않는다. | 내부 정본: 프로젝트 내부 grade contract이며 직접 정하는 공식 clause는 해당 없음. |
| GAP-001 · A | 기존 project_checklist_completeness_v1 truth table만 쓴다: 고정된 비어 있지 않은 checklist 전부 verified-present=true; verified-absent 하나 이상=false; 나머지 unknown/conflict/누락=null. E/label은 모두 null, legal_effect=not_determined 유지. | S1, S3: IFRS/KSSB safe-harbor 법적 등급 대응은 not verified; 체크리스트는 project-only. |
| GAP-002 · A | track=goal은 독립 유지한다. 확인 가능한 명시 전망/추정과 그에 연결된 가정·불확실성이 있으면 safe_harbor_category 후보로 검토; 단순 deadline·target이면 category=null; 단어만 있거나 문맥 부족이면 review. | S1: IFRS S2 ¶33–35 목표 공시는 검증됐으나 법적 safe-harbor 구분 근거는 not verified. |
| GAP-003 · A | Keep the additional-element policy: additional elements P6/M4/M5/M6/G7/G8 do not change E/label/range; preserve conditional triggers and report missing/unresolved status separately. | 내부 정본: Project domain decision; no external clause needed to interpret the project's own ladder. |
| GAP-004 · A | Allow only REPORT_SCOPE_V1 for M2 and GRI_ASSURED_PAGE_V1 for M3 with verified source refs and credited_from. All other facts are claim-local unless a separate exact-link policy is approved. Never report-level link baseline/year/progress/numeric facts. | S1: IFRS S2 ¶33–35 content verified [S1]; external standards do not prescribe document-global evidence linkage. |
| GAP-005 · A | Only exact goal missing pair [G3,G4] in the §7 example maps to IMPL. Every other unlisted combination gets sublabel=null and GAP-005; preserve E/label. | 내부 정본: Project rubric; no external standard maps these labels. |
| GAP-006 · A | If verified superlative trigger and safe_harbor_category candidate coexist, preserve both evidence paths, force grade=null and review_status=needs_review with GAP-006. If only one trigger is verified, use that route. | 내부 정본: No external source provides a combined project-grade priority. |
| GAP-007 · A | Evaluate only explicit ladder branches. For an unlisted edge such as unitless P1 or goal year/value combinations without a defined branch, return `blocked_rule_gap` with E/label null. Apply existing product rule: model+material alone capped at E1; direct product/material ratio or target is required for E2+. | S1: IFRS/KSSB exact edge-to-grade mapping not verified; the E ladder is project rubric. |
| GAP-008 · A | Retain source-supplied clauses with verification_state=unverified for project-only grade. Mark a clause verified only after exact official text/version, URL, access date, reviewer and redistribution-rights record are captured. | S1, S2, S3: IFRS S2 ¶33(e)/35 verified from official supporting material [S1]; exact KSSB matching clause not verified [S3]. |
| GAP-009 · A | 법적 적용 false 및 legal_effect=not_determined를 유지한다. as-provided rule text는 unverified로만 표시하고 준비용 검토만 허용한다. statutory compliance, violation 또는 legal-immunity label은 만들지 않는다. | S3: KSSB 공표일은 확인 [S3]; 정확한 법적 효력, 적용대상과 유예기간은 not verified. |
| GAP-010 · A | Use the versioned industry crosswalk only to present candidate industries. Set each topic applicable/not_applicable/undetermined after company activity evidence review; remove from denominator only an explicit approved not_applicable with reason; unknown stays in denominator. | S4: IFRS S2 ¶12/23/32 explanatory material [S4] says consider applicability and facts; not a fixed GICS-to-SASB automatic map. |
| REC-001 · A | Compare only verified entity sets with matching period and organizational basis. Distinct facility/legal-entity sets can be mapped only with source-verified, explicit control/boundary relation. If mapping is unresolved, execution_state=blocked and status=null; if a difference is expressly explained, status=matched. | 내부 정본: Project spec only; no external clause is used to decide entity identity. |
| REC-002 · A | Explanation=present only for an exact verified quote in the registered same-company/period disclosure package explicitly linked to the compared boundary difference. Complete search and no quote→needs_explanation; incomplete or unreadable source→blocked/null. | 내부 정본: Project scope; no official clause governs explanation binding. |
| REC-003 · B | Use approved operator mapping from financial statement line items to CAPEX, with same-period/currency aggregation; multi-year data only if exact schedule exists. | 내부 정본: No mapped account list verified here. |
| REC-004 · B | Keep threshold null and C3 threshold-based outputs blocked; allow only direct existence of a matching disclosed investment commitment to be recorded as matched. | 내부 정본: No external rule for 5.0x. |
| REC-005 · A | No explanation→needs_explanation only when all registered SR/FS package pages and allowed notes are covered, relevant pages readable, and a bounded search manifest completed. Otherwise execution_state=blocked/status=null. | 내부 정본: Project source rule; no public standard sets the exact search manifest. |
| REC-006 · A | Use verified actual start/end dates and consolidation scope. Pin the latest official corrected filing available at evaluation cutoff and preserve earlier versions/rcept_no. Exact period unavailable/incomparable→blocked; complete official lookup with no timely same-period FS follows explicit completed not_applicable reason. No annualization. | 내부 정본: No new external accounting clause used; this is the project's document-pair rule. |
| REC-007 · A | C4 matched only if the same claim/period's verified disclosure gives the classification definition and its inclusion/exclusion basis or calculation denominator. Complete search without those→needs_explanation; unclear claim/source relation→blocked. Never judge the classification's accounting or environmental correctness. | 내부 정본: No official environmental taxonomy or clause verified for these claims. |
| REC-008 · A | Runtime-block every C5 dispatch when stage>CURRENT_STAGE; return separate not_run envelope with status=null and reason=stage_disabled. Do not add C5 to reconciliation schema 1.1 or G/P/M; allow only source preservation, no verdict. | 내부 정본: Project scope boundary; no official source is needed to define product stage. |


**남은 차단:** GAP-001의 세이프하버 등급 매핑, GAP-005의 열거되지 않은 세부 라벨, GAP-007의 사다리 빈 조합, GAP-008/009의 공식 조항·법적 적용, GAP-010의 산업 적용성은 각각 보류한다. REC-003은 승인된 CAPEX 계정 매핑 전까지 차단하고 REC-004의 5.0x 임계값은 비활성이다. 검색·재무 문서가 불완전하면 C군 status를 확정하지 않는다. C5는 실행 차단한다.
