# 17 · 환경 변수와 구성

> ESG ProofOps · 개발 명세 1.0 · 2026-09-08
> 도메인 정본: `sources/PROJECT_DOMAIN_V2_ORIGINAL.md` (원문 2.0, 2026-09-07).

## 구성 우선순위
코드 상수 < 버전 고정 config 파일 < 검증된 배포 binding/환경변수. domain thresholds 를 임의 ENV 로 덮어쓰지 않는다. `.env.example`에는 비밀값/가짜 계정 ARN 을 넣지 않았다. 빈 cloud/live 필드는 preflight fail 이다. 실제 secret 은 Secrets Manager 참조로 주입한다.

| 변수 | 기본값 | 필수 조건 | 환경 | 의미 |
|---|---|---|---|---|
| `APP_ENV` | `local` | yes | all | local/staging/production; production 은 fake adapters 거부 |
| `APP_ORIGIN` | `http://localhost:5173` | yes | all | 허용 단일 웹 origin |
| `API_PUBLIC_BASE_URL` | `http://localhost:8000` | yes | all | BFF callback/API base |
| `AWS_REGION` | `ap-northeast-2` | cloud | staging/production | 저장/API 기본 리전, 실제 account 확인 |
| `AWS_PROFILE` | `빈 값` | no | local | 개발자 credentials profile; production IAM role |
| `S3_ARTIFACT_BUCKET` | `빈 값` | cloud | staging/production | private 원본/파생물 bucket |
| `S3_QUARANTINE_BUCKET` | `빈 값` | cloud | staging/production | 검증 전 업로드 bucket |
| `DDB_CORE_TABLE` | `빈 값` | cloud | staging/production | 운영 core table |
| `DDB_AUDIT_TABLE` | `빈 값` | cloud | staging/production | append-only audit table |
| `SQS_JOB_QUEUE_URL` | `빈 값` | cloud | staging/production | stage queue URL |
| `SQS_DLQ_URL` | `빈 값` | cloud | staging/production | dead-letter queue |
| `KMS_KEY_ARN` | `빈 값` | cloud | staging/production | 암호화 key ARN |
| `OPENSEARCH_ENDPOINT` | `빈 값` | cloud | staging/production | 승인된 Serverless collection endpoint |
| `OPENSEARCH_INDEX_GENERATION` | `proofops-chunks-v1` | yes | all | 임베딩1024d index generation |
| `COGNITO_USER_POOL_ID` | `빈 값` | cloud | staging/production | 실재 user pool |
| `COGNITO_CLIENT_ID` | `빈 값` | cloud | staging/production | PKCE client ID |
| `COGNITO_DOMAIN` | `빈 값` | cloud | staging/production | Hosted UI issuer domain |
| `SESSION_SECRET_ARN` | `빈 값` | cloud | staging/production | 세션/CSRF 서명 secret 의 SecretsManager 참조 |
| `CURSOR_SECRET_ARN` | `빈 값` | cloud | staging/production | pagination HMAC key 참조 |
| `AGENTCORE_RUNTIME_ARN` | `빈 값` | cloud | staging/production | 배포 runtime ARN |
| `MODEL_BINDINGS_PATH` | `config/model_bindings.json` | live | all | 실계정 model capability snapshot 파일 |
| `CONSENT_PROFILE_PATH` | `config/consent_profile.json` | live | all | 문서권리·처리리전 승인 profile |
| `RULE_PACK_PATH` | `config/rule_pack_manifest.yaml` | yes | all | 도메인 rule pack manifest |
| `PARSER_PROFILE_PATH` | `config/parsing.yaml` | yes | all | 파서/geometry routing 설정 |
| `LIMITS_PATH` | `config/limits.yaml` | yes | all | 토큰·파일·작업 예산 |
| `LOCAL_ARTIFACT_DIR` | `.local/artifacts` | local | local | 로컬만 파일 artifact |
| `LOCAL_DATABASE_PATH` | `.local/state.sqlite3` | local | local | local integration 용 상태 |
| `MODEL_ADAPTER` | `synthetic` | yes | all | synthetic/bedrock; production 은 bedrock 만 |
| `ENABLE_HYBRID_PARSER` | `false` | yes | all | 승인 benchmark 후 true |
| `HYBRID_BACKEND_URL` | `빈 값` | hybrid | all | 승인된 local/VPC backend 만 |
| `ENABLE_LEGACY_PYMUPDF` | `false` | yes | all | license gate 승인 시에만 true |
| `ENABLE_AGENTCORE_MEMORY` | `false` | yes | all | P0 off; source truth 가 아님 |
| `ENABLE_ADVERTISING_MODE` | `false` | yes | all | 전용 승인 rules 필요 |
| `ENABLE_YEAR_COMPARISON` | `false` | yes | all | P1 feature flag |
| `ALLOW_LIVE_MODEL_TESTS` | `false` | yes | all | 비용/동의 승인 후 수동 enable |
| `OTEL_SERVICE_NAME` | `proofops-local` | yes | all | service 명, 본문 capture 비활성 |
| `LOG_LEVEL` | `INFO` | yes | all | DEBUG 여도 문서/secret logging 금지 |
| `RETENTION_POLICY_PATH` | `config/retention.yaml` | yes | all | 실제 보존/삭제 설정 |
| `LOCAL_PARSER_PROFILE_PATH` | `빈 값` | local parser execution | local | 검증된 ParserProfile.config_snapshot JSON 경로; run에 동결된 hash와 일치해야 실행 |
| `LOCAL_RUN_SETTINGS_PATH` | `빈 값` | local configured run creation | local | 64 KiB 이하 명시적 실행 JSON; build_root, budget_limits, 선택적 extraction_profile/tagging_settings |
| `LOCAL_EXTRACTION_MODE` | `빈 값` | local extraction execution | local | 명시적 local_synthetic만 허용; --stage extract와 동결 profile 필요 |
| `LOCAL_TAGGING_MODE` | `빈 값` | local tagging execution | local | 명시적 local_synthetic만 허용; --stage tag와 생성 시 동결한 태깅 설정 필요. 실제 모델 호출 승인으로 사용하지 않음 |
| `VITE_API_BASE_URL` | `빈 값` | no | web build | 공개 API base; 기본 same-origin BFF, 비밀값 금지 |
| `VITE_UPLOAD_ORIGIN` | `빈 값` | external upload | web build | 승인된 HTTPS 업로드 origin 하나; 빈 값은 same-origin local 전송만 허용 |

## 검증
production 에서 APP_ORIGIN 은 https, MODEL_ADAPTER=bedrock, live bindings/동의/권리 필수, fake model·localhost endpoint·wildcard region·blank KMS/secret 은 시작 차단한다. local 에서만 synthetic adapter 를 허용하며 결과에 합성 표시를 요구한다. config 를 런타임 임의 reload 하지 않고 새 run 시작 시 hash 를 동결한다. rule/profile 의 version/effective_date 는 필수, unverified standard 는 approved basis 로 표시할 수 없다.


## 날짜 필드의 의미
설정 파일의 effective_date는 이 구현 설정의 적용일이다. 미검증 기준 템플릿의 standard_effective_date는 null이며, 2026-09-08을 실제 기준이나 법률의 발효일로 주장하지 않는다.

## 명시적 로컬 실행 설정

로컬 API는 `LOCAL_PARSER_PROFILE_PATH`의 `ParserProfile.config_snapshot()` JSON과
`LOCAL_RUN_SETTINGS_PATH`를 읽는다. 각 파일은 일반 파일이며 최대 64 KiB다.
실행 설정에는 절대 경로 `build_root`와 `budget_limits`가 필요하다.
빌드 검증은 해당 루트에서 실제로 재실행하며 JSON의 `ready` 값을 신뢰하지 않는다.
`budget_limits`는 기존 `BudgetLimits`의 `input_tokens`, `output_tokens`, `roles`,
`max_attempts`를 사용한다. 각 역할은 `role`, `max_calls`, `max_input_tokens`,
`max_output_tokens`, `max_context_tokens`를 명시한다.

추출을 켜려면 `LOCAL_EXTRACTION_MODE=local_synthetic`와 기존 `ExtractionProfile`을
직렬화한 `extraction_profile`이 함께 필요하다. 프로필은 설치된 실제 어댑터에서
내보낸다. 예: `uv run python -c 'import json; from dataclasses import asdict;
from proofops_agent.extraction import SyntheticClaimExtractor;
print(json.dumps(asdict(SyntheticClaimExtractor.profile)))'`.
작업자는 동결된 프로필과 실제 어댑터 프로필을 비교하며 다른 해시를 허용하지 않는다.

태깅은 추출 설정에 더해 `LOCAL_TAGGING_MODE=local_synthetic`와
`TaggingSettings`를 직렬화한 `tagging_settings`가 필요하다. `binding`은 기존
`ModelBinding` 구조이며 `synthetic=true`여야 한다. 모델 식별자·리전·프로필·
프롬프트·출력 스키마를 임의로 채우지 않는다. 생성 시 기존 runtime/consent/rights
및 규칙팩 검증도 통과해야 한다. 설정만으로 승인이나 실제 모델 호출 권한을 얻지 않는다.

잘못된 타입·중복 키·알 수 없는 필드·불완전한 모드 조합은 거부한다.
설정이 없으면 기존 실행 차단을 유지한다. 롤백은 새 설정을 비우고 새 실행 생성을
중지하며, 이미 저장한 run 설정과 태깅·판정·감사 이력은 그대로 보존한다.

로컬 웹 개발 서버는 저장소 루트 환경 설정의 `API_PUBLIC_BASE_URL`을 `/v1`, `/auth`, `/local/uploads` 프록시 대상으로 사용한다. 다른 프로젝트가 기본 포트를 사용하는 경우 명시적으로 분리된 API 주소를 설정한다. 이 값은 API 프로세스를 시작하거나 승인 profile을 생성하지 않는다.
