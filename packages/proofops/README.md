# proofops

환경 주장과 원문 근거를 처리하는 공통 Python 패키지입니다. 읽는 순서는 아래와 같습니다.

1. [domain/rules/engine.py](domain/rules/engine.py): 확인된 태깅을 등급·라벨·보류 사유로 변환하는 `evaluate()`.
2. [application/evidence/retrieval.py](application/evidence/retrieval.py): 동일 문서에서 태깅에 제공할 근거 묶음 구성.
3. [application/tagging/service.py](application/tagging/service.py)와 [consensus.py](application/tagging/consensus.py): 독립 태깅 실행과 기초 합의. 현재 유형 3/3·요소 2/3 확정은 [cf6_confirmation.py](application/tagging/cf6_confirmation.py)를 [worker/tag_runner.py](../../apps/worker/src/proofops_worker/tag_runner.py)에서 호출하는 경로를 함께 확인합니다.
4. [adapters/local/source_verification.py](adapters/local/source_verification.py): 추출한 근거의 원문 대조.

[domain](domain/README.md)은 외부 I/O와 분리되어 있으며, [application](application/README.md)이 흐름을 조정하고 [adapters](adapters/README.md)가 저장소·파서·모델을 연결합니다. [composition.py](composition.py)는 실행 환경의 구성 경계입니다.
