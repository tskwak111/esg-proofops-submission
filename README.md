# ESG ProofOps

**지속가능성 공시의 환경 주장을 원문 근거와 함께 검토하는 도구입니다.** 보고서의 주장과 근거 위치를 연결하고, 빠진 정보와 사람의 확인이 필요한 항목을 보여줍니다. 기업의 실제 환경성과나 위법 여부를 판정하지 않습니다.

## 작동 방식

```text
PDF → 문단·표 파싱과 위치 보존 → 원자 주장 추출 → 같은 문서의 근거 검색
    → 인용·귀속 검증 → 모델 분류·요소 태깅 → Python 규칙엔진 판정
    → 검토 화면·보고서 → 사람의 태깅 수정과 새 revision
```

Upstage 모델은 추출·분류·태깅에 사용합니다. E0~E3 등급과 라벨은 Python 규칙엔진이 계산합니다. 검증되지 않은 인용을 `present`로 인정하지 않고, `unknown`·`conflict`·`unreadable`을 단순한 근거 부재로 바꾸지 않습니다. 미정 규칙이나 증거 충돌은 보류 또는 등급 범위로 표시합니다. [도메인 계약](docs/00_MASTER_SPEC.md) · [미정 규칙](docs/31_DOMAIN_IMPLEMENTATION_GAPS.md)

## 공개 데모

<https://esg-proofops.vercel.app>에서 다음 경로를 볼 수 있습니다.

| 경로 | 내용 |
| --- | --- |
| `/` | 제품 소개와 검토 흐름 |
| `/analyze` | 브라우저에서 PDF 지문을 확인하고 데모로 이동 |
| `/analyze/replay` | 저장된 처리 단계 재생 |
| `/demo` | NAVER 2025 선택 페이지의 주장·근거·등급 탐색 |
| `/demo/kia` | 기아 2025 선택 사례와 숫자·보증 확인 |
| `/review/:id` | 선택한 주장에 대한 검토 시뮬레이션 |
| `/report/naver`, `/report/kia` | 브라우저 검토 보고서와 내보내기 |
| `/live` | 한 문장 실시간 분류·태깅·규칙 계산 체험 |

**시연 모드**는 결과를 살펴보는 데 필요한 완화된 처리입니다. 분류에서 과반수 트랙을 사용하고, 태깅을 단일 패스로 실행하며, 미해결 근거가 남은 경우 추정 등급을 별도로 표시합니다. 추정 등급은 확정 판정이 아닙니다. `/live`는 PDF 전체 검증이 아닌 입력한 주장과 문맥 한 건의 체험이며, 제출 양식에서 제공한 접근 코드가 필요합니다.

## 구성

| 영역 | 기술·위치 |
| --- | --- |
| 웹 화면 | React, TypeScript, Vite · `apps/web/` |
| API·작업 처리 | FastAPI, Python · `apps/api/`, `apps/worker/` |
| 파이프라인·규칙 | Python 도메인·애플리케이션·어댑터 · `packages/proofops/` |
| 모델 연결 | Upstage 추출·태깅 어댑터 · `apps/agent/` |
| 실시간 체험 | 제한된 단일 주장 API · `api/` |
| 계약·검증 | `contracts/`, `tests/`, `scripts/` |
| 데이터·문서 | 공개용 파생 데모 JSON · `apps/web/public/demo/`; 설계 문서 · `docs/` |

로컬 실행 상태는 SQLite와 로컬 객체 저장소에 보관합니다. 문서 버전, 원문 위치, 모델·프롬프트·규칙 해시, 수정 revision을 추적합니다. `config/`에는 고정된 규칙 구성이 있습니다.

## 로컬 실행

Python 3.12, `uv`, Node.js, `pnpm`이 필요합니다.

**정적 데모**는 API 키와 PDF 원본 없이 열 수 있습니다.

```bash
pnpm install --frozen-lockfile
VITE_DEMO_STATIC=true pnpm --filter proofops-web dev
```

**저장된 실제 실행 결과**가 별도로 있다면, `pilot.json`을 포함한 권리 확인된 상태 디렉터리를 복사해 로컬 API와 화면을 엽니다. 공개 저장소에는 그 상태와 보고서 원본이 없습니다.

```bash
uv sync --locked
uv run python scripts/submission_demo.py --seed /path/to/saved-pilot-state --port 8790
```

**새 PDF 분석**에는 사용 권한이 있는 PDF, 로컬 `.env.upstage.local`의 `UPSTAGE_API_KEY`, 이미 승인되어 생성된 `.local/upstage/budget.sqlite3` 예산 장부가 필요합니다. 다음 명령은 입력 계획만 확인합니다. 유료 모델 실행을 명시할 때 `--invoke`를 추가합니다. 선택 페이지를 제한적으로 처리하므로 보고서 전수 분석을 뜻하지 않습니다. [실행 세부사항](docs/SUBMISSION_DEMO.md)

```bash
uv run python scripts/analyze_report.py --pdf /path/to/report.pdf \
  --pages 25,117,138 --report-year 2025 \
  --period-start 2025-01-01 --period-end 2025-12-31
```

## 범위와 권리

공개 결과는 선택 페이지·선택 주장에 한정됩니다. 원문 판독 불가, 근거 귀속 충돌, 미승인 규칙은 보류합니다. 독립 정답 자료에 대한 정확도, 전체 보고서 성능, AWS 운영 배포, 법적 면책 효과는 검증 결과로 주장하지 않습니다.

원본 보고서 PDF와 페이지 이미지는 재배포 권리를 확인하지 못해 저장소에 넣지 않았습니다. 공개 데모에는 짧은 인용, 물리 쪽번호, 공식 보고서 링크와 파생 데이터만 포함합니다. 코드의 별도 `LICENSE` 파일이 없으므로 사용·재배포 허가를 추정하지 마세요.

## 팀

- **Developer A:** 공시 파이프라인과 웹 화면
- **Developer B:** DART·재무 대사(C1–C4)
