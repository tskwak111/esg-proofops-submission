# 27 · 파싱·표 검증·근거 좌표 상세 계약

## R25 · 로컬 대사 PDF 인용 연결 (2026-09-22)

대사 reader의 기존 `page:N` locator는 문자 그대로 일치하는 검증을 유지한다.
새 초안에서 명시한 `page:N:whitespace-v1`만 PDF 텍스트의 연속 공백·줄바꿈을
공백 하나로 정규화하여 canonical 인용을 확인한다. 인용 자체도 같은 canonical
형식이어야 하며, 지정 페이지에서 일치 위치가 하나여야 한다. 공백 제거,
숫자·문장부호·단어 변경, OCR 추측은 허용하지 않는다. 원본 PDF의 SHA-256과
페이지 검증, 제품 claim의 원문 재생 및 tenant/company/version 연결은 그대로다.
이는 페이지 텍스트 내 인용 확인이지 별도의 의미 귀속 승인이나 표 행 연결 승인이 아니다.
기존 packet/revision을 수정하지 않으며 API/DB 필드·migration 변경은 없다.
이 버전을 모르는 reader는 새 locator를 거절한다. 롤백 시 해당 새 초안을 이전
reader에 배정하지 않고 기존 문자 일치 locator의 읽기 경로만 계속 사용한다.

> ESG ProofOps · 개발 명세 1.0 · 2026-09-08
> 도메인 정본: `sources/PROJECT_DOMAIN_V2_ORIGINAL.md` (원문 2.0, 2026-09-07).

## 1. 조합의 기본값

OpenDataLoader PDF **2.5.7**을 1차 구조 추출기로 고정한다. 이 버전의 release 메타데이터와 README 를 확인했다 [S01,S02]. JSON 이 정본이며 Markdown 은 LLM 에 주는 읽기 표현일 뿐이다. Python 3.12 + Java 21 실행 이미지를 사용한다. 배포 때 lockfile, JVM 버전, 이미지 digest 를 manifest 에 기록한다.

**기본 경로:** OpenDataLoader Java/local → 정규화/검사 → 표 영역 pdfplumber 보조 추출 + Bedrock 비전 교차확인 → 충돌 시 제한된 재파싱/검토 큐. 원문 §6.1의 “표 포함 페이지는 비전 재추출” 요구를 유지한다. 비용 때문에 이를 생략한 실행은 `validation_profile=fast_preview`이며 최종 검증 완료로 표시하지 않는다.

Docling 기반 OpenDataLoader hybrid 는 복잡한 표와 이미지형 페이지의 **대체 후보 생성기**다. OpenDataLoader hybrid 와 독립 Docling 결과는 같은 엔진 계열일 수 있으므로 독립된 두 표로 세지 않는다. 기본 모드에서 모든 페이지를 3~4개의 파서로 반복 처리하지 않는다. OCR 은 텍스트/레이아웃 추출과 직접 비전 확인으로 해결되지 않는 스캔 영역의 최후 경로이며, 전체 PDF 에 자동 반복하지 않는다.

OpenDataLoader 의 공개 벤치마크 숫자는 자체 공개 코퍼스의 결과다. 이를 한국어 ESG 보고서 정확도나 셀 값 정답률로 바꾸어 발표하지 않는다. 그림 설명도 정밀 수치의 정답이 아니다 [S02,S03]. Amazon Textract 는 확인한 공식 문서상 한국어가 텍스트 인식 지원 언어에 포함되지 않아 한국어 주력 fallback 으로 채택하지 않는다 [S05].

## 2. 어댑터 인터페이스

```python
class ParserPort(Protocol):
    def parse(self, source: DocumentVersionRef, selection: PageSelection,
              profile: ParserProfile) -> ParserArtifact: ...
```

`DocumentVersionRef`는 tenant_id, document_id, document_version, original_sha256, object_version_id 를 포함한다. `PageSelection.physical_pages`는 1 기반의 정렬된 중복 없는 번호 목록이다. `ParserArtifact`는 raw_json_uri, raw_sha256, parser_name/version, config_hash, parser_run_id, pages, blocks, warnings, duration_ms 를 포함한다. 원문 입력 URI 는 내부 저장소 참조이며 임의 외부 URL 을 받을 수 없다.

실제 SDK 최소 호출은 검증된 README 의 다음 형태다. 페이지 선택이 SDK 버전에 제공되는지 확정하지 않고 존재하지 않는 인자를 만들어 넣지 않는다. 페이지 분할이 필요하면 pypdf adapter 로 파생 PDF 를 만들고 원래 물리 페이지 매핑을 기록한다.

```python
opendataloader_pdf.convert(
    input_path=[str(local_pdf)],
    output_dir=str(output_directory),
    format="json,markdown",
)
```

JVM 프로세스당 문서/페이지 배치를 처리한다. 한 텍스트 블록마다 JVM 을 새로 실행하지 않는다. 워커가 timeout, exit code, stdout/stderr 요약, 파일 크기·출력 schema 를 검증한다. stderr 에 원문이 노출될 수 있어 공개 로그에는 경로/오류코드만 보낸다.

## 3. 좌표와 페이지의 단일 계약

세 번호를 혼동하지 않는다: `physical_page`=PDF 파일상 1 기반 페이지, `page_index`=렌더러 내부 0 기반 인덱스, `printed_page_label`=페이지에 적힌 문자열. 저장과 API 는 physical_page 만 필수이며 page_index 는 항상 `physical_page-1`에서 파생한다. GRI 의 “34”는 printed page 일 수 있으므로 독립 매핑을 만든다.

canonical bbox 는 **회전·CropBox 를 적용한 표시 페이지의 왼쪽 위 원점, PDF point 단위** `[x0,y0,x1,y1]`다. `width_pt`, `height_pt`, `rotation`, `crop_box`, `native_bbox`, `native_coordinate_system`, `native_to_display_matrix`도 보존한다. OpenDataLoader 의 `[left,bottom,right,top]`과 직접 혼용하지 않는다 [S04].

회전 0도, crop 원점 0이고 높이 H 인 단순 페이지에서만 다음을 사용할 수 있다.

```text
canonical = [left, H-top, right, H-bottom]
```

회전 90/180/270도 또는 CropBox 원점 이동은 네 꼭짓점에 affine transform 을 적용한 뒤 min/max 로 구한다. 공식 렌더러 viewport 변환과 일치하는지 golden case 로 검증한다. UI 는 scale/zoom 을 적용하여 표시한다. 없는 bbox 를 영점 bbox 로 만들지 않는다. 경계 밖·NaN·역전 좌표는 parse issue 다.

로컬 fusion v3는 유한·정방향이지만 표시 페이지 경계를 벗어난 후보를 버리지 않는다.
native/parser bbox와 affine, 원문·부모 관계를 그대로 보존하고 canonical bbox는
`null`, 블록 품질은 `unlocated`, 이슈는 `source_geometry_invalid/unreadable`로 둔다.
이 후보는 다른 위치 후보와 병합하지 않으며 `SourceRef` 생성은 거부한다.
수치 정규화는 해당 후보가 값·헤더 등에 연결되면 `unreadable`/값 `null`로 남긴다.
권한 검증된 원문 PDF·페이지 미리보기는 제공하되 강조 표시는 허용하지 않는다.
NaN·역전·affine 불일치 등 구조적으로 유효하지 않은 입력은 계속 거부한다.
이슈가 있는 페이지는 워커 coverage에서 보수적으로 unreadable로 계산한다.

## 4. 원시 텍스트와 근거 참조

2026-09-09 실보고서 회귀 보완: OpenDataLoader의 `list items` 자식도
순회하며 `list item` 텍스트는 기존 `paragraph` 후보로 수용한다. 파서 native ID,
좌표, 원문과 `section_parent` 관계를 보존한다. 목록 컨테이너를 텍스트로
합성하거나 원문에 없는 문장 연결을 만들지 않는다. 48쪽의 실제 누락 사례는
동일한 저장 JSON 재생과 별도 새 파싱으로 확인했다.
새 파싱은 새 parse_manifest_id를 사용한다. 기존 manifest는 candidates snapshot으로
그대로 재생하므로 이전 산출물을 소급 수정하지 않는다. API/DB schema migration은
없다. 롤백 시 기존 원문·새 산출물은 보존하고 새 파싱 배정을 중단한다.

`raw_text`와 `normalized_text`를 모두 보존한다. Unicode NFC, 허용된 ligature 치환, 공백 정규화의 변환 내역을 `normalization_map`으로 유지한다. 정규화된 위치에서 원시 위치로 역매핑할 수 있어야 한다. 원문 해시와 정규 텍스트 해시는 구분한다.

`SourceRef`는 document_version + artifact_hash + block_id + physical_page + char_start/end 또는 table_id/row/column + bbox + quote + quote_hash 를 가진다. 문자 offset 은 Unicode code point 기준, end-exclusive 다. Python code point 와 JavaScript UTF-16 index 를 직접 섞지 않는다. UI 는 서버가 돌려준 quote 및 bbox 를 우선 사용한다.

비전 추출 문자열은 native text 가 없을 수 있다. 이때 `origin=vision_transcription`, `region_image_sha256`, `transcription_revision`을 기록한다. 원시 PDF text 에 있었다고 표시하지 않는다. 이미지형 자료에 대한 근거 확정은 사람 확인 또는 평가로 승인된 비전 검증 프로필을 필요로 한다.

## 5. 블록 정합화와 충돌

같은 parser family 의 후보 합의는 정확도 증거로 세지 않는다. 각 블록은 (문서버전, 물리페이지, semantic type, 영역 겹침, 표/제목 문맥)으로 후보를 매칭한다. 문자열 동일만으로 병합하지 않는다. 후보 pair 의 bbox IoU 0.8 이상은 **매칭 후보 선정용 개발 기본값**이지 정답 임계값이 아니다. 작은 셀은 같은 table alignment 의 row/column 으로 정렬한다.

로컬 파싱 manifest의 `fusion_version=2`(v3에도 유지)는 같은 영역의 셀을 비교할 때 파서별
로컬 행·열 번호 차이를 허용하되 다른 명시적 문맥은 유지한다. 원시 후보의 행·열,
span, 부모 표 관계와 충돌은 보존하며, 이 매칭으로 정확도나 근거 검증을 승인하지 않는다.
기존 manifest에 필드가 없으면 v1 매칭을 사용해 저장된 graph/quality를 검증한다.
알 수 없는 버전은 거부한다. 기존 산출물 재작성·DB migration은 없으며,
수정 전 코드로 rollback하면 v2 산출물은 호환 대상이 아니므로 해당 산출물을 읽는
배포를 함께 되돌리거나 별도 새 실행으로 재파싱해야 한다. 이전 revision을 덮어쓰지 않는다.

새 산출물은 v3를 기록한다. v1/v2 산출물은 원래 동작으로 재생하며 경계 밖 후보를
소급 허용하지 않는다. 새 필드·DB migration은 없고, v3를 모르는 버전으로 rollback할
경우 v3 산출물 읽기를 거부하므로 해당 실행을 이전 reader에 배정하지 않는다.

정규 숫자·연도·단위 중 하나라도 다르면 `parse_conflict`를 만든다. 값이 다른 후보를 평균내거나 다수결로 숫자를 정하지 않는다. canonical 엔터티에는 winner 후보 또는 unresolved 상태가 있으며 모든 raw 후보 ID 를 붙인다. 사람이 선택하면 선택 이유와 수정 이력이 추가된다.

`source_quality`: verified / unverified / conflicted / unreadable / unlocated. verified 도 현실 세계의 진실을 의미하지 않고 해당 추출값의 출처 확인 상태다. 파서 전반 정확도의 신뢰구간처럼 표현하지 않는다.

## 6. 표 정규화

각 `MetricObservation`은 metric_raw, metric_id(optional), scope, scope2_basis, category, subject, organizational_boundary, reporting_period, baseline_period, unit_raw, unit_canonical, scale_multiplier, value_raw, value_decimal, denominator, assurance_ref, source_refs, table_id/row/column, footnotes, quality 를 갖는다.

숫자는 Decimal 문자열로 저장한다. `-`, 빈칸, N/A 는 0이 아니다. 단위 “천 tCO2e”는 값×1000과 원래 표기를 같이 보존한다. 퍼센트와 퍼센트포인트를 구분한다. 원단위에서 분모(매출·생산량 등)를 잃으면 합계/비교 검사를 하지 않는다. 병합 셀의 연도·Scope·단위 상속은 명시적 parent relation 을 남긴다. 표 페이지 분할 병합은 제목, 연도 열, 단위, 경계가 모두 연결되고 continuation 근거가 있을 때만 허용한다.

## 7. 수치 검사

본문과 표를 비교하기 전에 지표·연도·Scope·시장/위치기반·경계·단위·절대량/원단위가 같아야 한다. 단위 변환이 정의되지 않으면 `not_comparable`이다. 감소율은 `(baseline-current)/baseline*100`; baseline=0이면 `not_computable`이다. 표시 자릿수의 반올림 구간으로 비교하고 임의 ±5% 허용치를 넣지 않는다. 표 합계는 중복/소계/포함관계가 명확할 때만 더한다.

계산 불일치는 `NumericFinding`이며 자동으로 새로운 등급 강등 규칙이 되지 않는다. 원문에서 P6 결손이 등급에 미치는 영향이 충분히 정의되지 않은 경우 결손과 원문값을 보여주고 판정 게이트에 보낸다. 숫자를 수정해서 회사 문장을 “맞게” 만들지 않는다.

## 8. GRI 와 보증 의견서

GRI 행은 indicator_code, printed_page_refs, resolved_physical_pages, link_text, resolution_state 를 저장한다. 단일 전역 page offset 을 가정하지 않는다. 각 연결 페이지에서 실제 관련 정보가 있는지 확인하여 mismatch 를 기록한다. GRI 매핑이 없거나 틀려도 문서의 전역 검색을 중단하지 않는다.

보증은 provider, standard_raw, standard_canonical(optional), level, reporting_period, entities, facilities, covered_metrics, explicit_exclusions, source_refs 를 추출한다. 기관명만 있으면 covered 가 아니다. 보증 대상 지표·대상 기간·경계가 클레임과 맞아야 하며 명확한 제외는 not_covered, 해석 불가능은 undetermined 다. 여러 보증서가 있으면 연결을 각각 유지하고 가장 높은 수준을 임의로 전사 확장하지 않는다.

### 자동 헤더 앞 제목 행 처리 (2026-09-20)

`normalize_tables`는 표의 전체 관측 열 폭을 덮는 단일 셀 제목 행만 건너뛰고
뒤의 명시적 헤더를 사용한다. 여러 행 병합 제목과 0/1 기반 좌표를 동일하게
처리하며, 부분 폭 제목·겹침·헤더 불명확은 기존 미해결 처리를 유지한다.
제목 셀은 원문 참조에 보존하지만 단위·Scope·지표를 추측하는 데 사용하지 않는다.
기존에 지원하던 제목 없는 표의 observation ID, API/DB 형태 및 검증 게이트는
유지한다. 새 관측값도 unverified이며, 기존 저장 revision을 다시 쓰지 않는다.
rollback은 이전 구현으로 되돌리며 신규 제목 표 지원만 중단한다.

### 다단 헤더의 의미 누락 차단 (2026-09-20)

자동 정규화는 하나의 연도 헤더 아래 여러 값 열이 있거나 명시적 행 병합으로
추가 헤더 층이 존재할 때, 지원되는 Scope 2 산정방식 외의 하위 역할을
임의로 생략하지 않는다. 해당 표는 `table_layout_unresolved`로 남긴다.
셀 역할 지정 경로도 연도 헤더와 값 사이의 동일 열에 해석되지 않은 문자 셀이
있으면 연결을 거부한다. 앞선 숫자/결측 데이터 행은 이 차단 대상이 아니다.
목표·실적 축 자체를 확정하는 기능은 아직 없으며, 값의 실제 달성 여부를 추론하지 않는다.
공개 API/DB 변경은 없고 역할 지정 거부는 기존 422 계약을 사용한다.
기존 저장 revision/보고서는 수정하지 않는다. rollback은 코드 복원으로 수행한다.

### 명시적 셀 역할을 통한 정규화

`normalize_table_bindings`는 비정형 헤더의 구조화 추출 결과를 받는 내부 경로다.
입력은 table_id와 원문 canonical cell ID에 대한 역할 매핑이다. 지표·연도·값은
필수이며, 같은 표·값 행 및 연도 헤더의 열 관계를 검사한다. 병합 값의 전체 행 범위가
행 역할 셀에 포함되어야 하며, 위쪽 연도 헤더는 값의 전체 열 범위를 덮고
값 행 시작 이전에 끝나야 한다. 시작 좌표만 겹치는 연결은 거부한다. 병합 셀의 누락된
span을 추측하거나 인접 행 값을 채우지 않는다. 원문 숫자·단위·좌표는 보존하고,
기존 정규화 함수를 재사용한다. 역할의 의미적 정확성은 승인하지 않으며 결과는
unverified 또는 conflict/unreadable이다. 모델/프롬프트/replica 및 역할 매핑
receipt는 호출자가 결과와 함께 저장해야 한다. 매핑은 observation ID 해시에 포함된다.

기존 자동 `normalize_tables`, API 필드, DB schema 및 이전 observation ID는
바뀌지 않는다. 새 내부 경로만 opt-in이며 DB migration은 없다. 이전 코드로
rollback하면 이 경로의 실행을 중단하고 저장된 결과/receipt는 보존한다.
실증 fixture 실행은 실제 모델 태깅·원문 품질 승인으로 간주하지 않는다.

## 9. 평가와 승인

최소 6개 보고서의 60개 표/레이아웃 영역을 초기 개발 벤치마크 목표로 잡는다. 디지털·다단·병합표·단위각주·보증서·스캔을 구분하고 실측 전 수치를 비워둔다. 비교군은 기존 parser baseline, OpenDataLoader local, 제한적 hybrid, 최종 조합이다. 측정은 cell exact match, 값-연도-단위 tuple exact match, bbox/페이지 정확성, 충돌 탐지 재현율, UNREADABLE 비율, 시간/비용이다. TEDS 만으로 숫자 정확도를 대체하지 않는다.

이 패키지에서는 원본 ESG PDF 파싱, OCR, 비전 API, benchmark 를 실행하지 않았다. `tests/fixtures/contract_cases.json`는 설계상 예상 동작을 담은 합성 테스트 벡터다.


### 좌표 스키마의 추가 검증
설계에서 사용한 OpenDataLoader bbox 순서와 1-based 페이지는 v2.5.7의 `schema.json` 1–180행을 직접 대조했다.[S16] 회전/CropBox를 포함한 실제 PDF 렌더링 왕복 검증은 구현 단계의 별도 시험이다.

### 내부 근거 모델과 v1 API 표현
위 상세 provenance는 내부 불변 source record에 보존한다. 기존 v1 API의
`SourceRef.page_num`은 `physical_page`와 같은 1-based 물리 페이지이며,
`location_quality`는 위치 확보 여부만 뜻한다. 이를 추출값의 `source_quality`나
`verification_state`와 혼용하지 않는다. `artifact_hash`, `quote_hash`, 표 행열,
테넌트 및 parser 후보/충돌 정보는 내부 record와 manifest에서 검증하고,
API serializer는 기존 JSONSchema의 필드만 명시적으로 투영한다. v1 응답에
임의 필드를 추가하지 않으며, API 표현만으로 근거를 자동 확정하지 않는다.

### 자동 각주 검토의 선택 페이지 계약

새 자동 실행의 `automatic_pages_v2` 정책은 동결된 `selected_pages`를 모두 검토한다.
표가 인식되지 않은 페이지도 원본 native word에서 각주 후보를 추출하며, 표·셀이나
귀속 SourceRef를 만들어내지 않는다. 이 페이지의 귀속은 unknown, 점수 반영은 false다.
검토 artifact마다 원본 해시·페이지·단어 인덱스·좌표를 재검증하고 페이지별 open
`table_note_review` issue로 기존 해당 페이지 block들을 보류한다. 이름과 달리 이
issue는 미인식 표가 있는 것으로 확정하는 표 block이나 ownership edge가 아니다.

선택 페이지의 누락·중복·범위 밖 artifact는 발행을 거부한다. 페이지 검토 receipt 수는
각주 누락률 또는 completeness를 뜻하지 않는다. 읽을 native fragment가 없으면
`not_run/NO_READABLE_NATIVE_WORDS`를 기록하고 모델을 호출하지 않는다. 화면 밖
단어 좌표, 지원하지 않는 회전·CropBox 등은 실패로 보존한다.

기존 `automatic_v1`은 당시의 표 기반 검토 의미와 immutable graph hash를 유지한다.
새 정책을 구버전으로 읽어 검토 완료로 바꾸지 않는다. checkpoint v3와 기존 opaque
job record를 재사용하므로 공개 API·DB column migration은 없다. Rollback은 새
정책 생성 중단과 해당 정책 reader 유지가 필요하며, 미지원 reader는 명시적으로 거부한다.
이 검사는 원문 도메인·귀속 승인이나
전체 보고서 서비스 품질의 확정이 아니다.

### 선택형 로컬 표 원문 검증 (`native_table_source_v1`)

`ParserProfile.table_source_policy_sha256`가 있는 새 parse만 기존 native PDF와
macOS Apple Vision 셀 OCR을 대조한다. 로컬 실행은 `--verify-tables`로 켠다.
네트워크 호출/새 dependency/공개 API 또는 DB column 변경은 없다. 미설정 profile은
새 키를 snapshot에서 생략해 기존 config hash와 manifest 읽기를 유지한다.

확인 범위는 **병합 없는 완전한 직사각형 표의 문자와 셀 배치**다. 각 셀의 행·열,
같은 행/열 좌표 정렬, 표/행 텍스트 일치, native 문자 누락·잘림, 렌더링 OCR의
문자 일치를 모두 요구한다. 테이블당 4~40셀, 실행당 최대 96셀 OCR로 제한한다.
원문 충돌, 미확정 각주, 지원하지 않는 회전/CropBox, 미지원 OCR 플랫폼은 보류한다.
셀 바깥 단위·캡션·각주는 승인하지 않는다. 숫자/연도의 원문 일치는 해당 주장의
지표·기간·경계에 귀속된다는 뜻이 아니며 기존 binding과 규칙엔진을 그대로 거친다.

원래 `graph.json`, candidates, quality는 그대로 저장한다. 별도 `table-source.json`에
원본·테넌트·문서버전·manifest·입력 graph·검증 코드/reader 해시와 셀별 렌더링 기록을
남기고 기존 manifest artifact 해시에 포함한다. parse 반환 및 load_verified에서
동일 원문으로 재생한 뒤 통과한 table/row/cell만 새 graph view에서 verified로 바꾼다.
해당 표의 `table_vision_not_run`만 해소하며 다른 issue나 기존 revision은 수정하지 않는다.
전체 validation_profile은 fast_preview이고, 전체 비전 검증 완료를 뜻하지 않는다.

Rollback은 새 opt-in 생성 중단이다. 새 manifest를 읽으려면 당시 pinned verifier
코드/reader 버전이 필요하며, hash 불일치는 차단한다. 이전 manifest/receipt를
새 코드에 맞춰 덮어쓰지 않는다. merge/다단 헤더와 외부 단위 귀속은 후속 파싱 작업이다.

### ODL 첫 행의 헤더·데이터 분리 (로컬 opt-in)

`ParserProfile.table_structure_repair="odl_header_v1"` 또는 pilot의
`--repair-table-headers`는 ODL 첫 행의 각 셀에 heading/paragraph가 함께 있는
경우만 분리한다. 원문 native 단어 전체 포함·문자 일치·공통 수평 간격을 확인하며,
경계에 걸친 단어, 일부 열만 분리 가능, 미해석 자식은 전체 표를 그대로 둔다.
좌표 반올림 오차는 0.001pt만 허용한다. 병합 단위의 row span은 유지하고 텍스트를
다른 행에 복제하지 않는다. 회전·CropBox 이동, 중첩 표, 일반 다단 헤더는 대상 밖이다.

`source.json` 원본은 보존하며 `source-repaired.json`과 `table-repair.json`에 새 구조,
원래/새 셀 ID·좌표 연결, 원본 JSON의 canonical serialization 해시를 보관한다.
후보는 새 구조로 생성하되 parser family는 같고 verified로 승격하지 않는다.
모든 산출물의 실제 파일 해시는 기존 manifest에 별도로 기록·재생 검증한다.
옵션 미설정 시 기존 config hash/행동을 유지한다. 새 manifest만 사용하며 기존
실행은 다시 쓰지 않는다. API/DB migration은 없다. Rollback은 옵션을 끄고,
새 profile reader를 유지하거나 해당 실행을 구버전에 배정하지 않는 방식이다.

실제 LG p32 및 HMM p124의 총 3개 표에서 새 헤더/첫 데이터 40셀의 native 문자
일치를 확인했다. LG p122 및 다른 복잡한 표는 여전히 미해결이고, 병합 셀의
입증 승인·주장 귀속·전체 파싱 정확도를 이 결과로 승인하지 않는다.

### 병합 표 검증과 표 부모 보존 (로컬 opt-in v2)

`--repair-table-headers`의 새 실행은 `odl_header_v2`를 사용한다. v1의 헤더 분리에
더해 복구한 표의 bbox 없는 행은 기존 셀 bbox의 합집합으로 위치를 기록한다.
원래 셀 ID·새 행 bbox는 receipt에 보존하며, 기존 v1 실행은 재작성하지 않는다.

이 옵션의 새 parser manifest는 `fusion_version=4`다. 표 하위 블록을 합칠 때
부모 표 영역도 기존 후보 매칭 기준을 통과해야 한다. 완전한 표와 일부 열만 잡힌
보조 표는 같은 위치의 숫자가 있더라도 별개 후보로 남는다. 원시 후보와 충돌을
삭제하지 않으며, v1/v2/v3 manifest는 종전 알고리즘으로 읽는다.

`--verify-merged-tables`는 기존 `--verify-tables`와 상호 배타적인 새 검증 정책이다.
`merged_table_verification.py`는 명시적 span의 유한·완전·비중복 격자, 정방향 행/열,
행 문자와 부모 연결, 모든 native 문자의 유일한 셀 포함, 원문 문자와 렌더링 OCR의
일치를 확인한다. 좌표 반올림 오차는 최대 0.001pt, 셀 40개·OCR 96회 한도를 유지한다.
작은 반전색 헤더를 위해 OCR 입력에 흰 여백 24px를 붙이며 원문 픽셀은 바꾸지 않는다.
셀·검증된 행·표만 품질을 승격하고 하위 paragraph/heading은 자동 승격하지 않는다.
숫자·단위의 의미적 주장 귀속이나 등급은 이 경로의 승인 대상이 아니다.

v1 검증 파일과 정책 해시는 유지한다. parser는 저장된 policy hash로 v1/v2를
선택하며 알 수 없는 정책은 거부한다. DB/API migration 없음. Rollback은 새 옵션
배정 중단과 v4/v2 reader 유지이며, 구버전 reader에 새 산출물을 배정하지 않는다.
