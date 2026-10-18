# 애플리케이션

업로드부터 근거 구성·태깅·검토·내보내기까지의 흐름을 조정합니다. 외부 구현과의 경계는 [ports](ports/)에 있습니다.

- [claims.py](claims.py): 원자 단위 주장 추출·발견.
- [ingest/graph_fusion.py](ingest/graph_fusion.py): 파서 산출물과 출처를 문서 그래프로 구성.
- [evidence/retrieval.py](evidence/retrieval.py), [binding.py](evidence/binding.py): 근거 묶음과 인용 연결.
- [tagging/service.py](tagging/service.py), [consensus.py](tagging/consensus.py): 독립 태깅과 기초 합의 구성.
- [tagging/cf6_confirmation.py](tagging/cf6_confirmation.py): 현재 유형 3/3·요소 2/3 확정 조건. 아래 worker가 이 경로를 호출합니다.
- [analysis_profile.py](analysis_profile.py): 현재 분석 정책 기본값과 호환 진입점.
- [policies/registry.py](policies/registry.py): 정책 재생 순서와 hosted 허용 목록.
- [policies/legacy/analysis_profiles.py](policies/legacy/analysis_profiles.py): 저장된 실행을 재현하는 과거 정책 정의.
- [reviews.py](reviews.py), [exports.py](exports.py): 태깅 검토와 결과 내보내기.

태깅에서 판정으로 이어지는 흐름은 [worker/tag_runner.py](../../../apps/worker/src/proofops_worker/tag_runner.py)와 함께 읽습니다. 저장된 정책과 결과는 기존 실행을 재현할 때 그대로 사용합니다.
