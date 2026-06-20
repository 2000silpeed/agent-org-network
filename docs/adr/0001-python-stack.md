# Python을 구현 스택으로 선택

상태: accepted (2026-06-20)

순수 DDD 타입 모델링만 보면 TypeScript가 discriminated union·branded type으로 "불법 상태를 표현 불가능하게" 만드는 데 더 강하다. 그럼에도 이 제품의 무게중심이 v1의 LLM intent 분류·임베딩 기반 라우팅·A2A 연동으로 이동하고, 에이전트 카드 검증을 pydantic이 거의 공짜로 해주기 때문에 Python 3.12(+ pytest, pydantic v2, ruff, pyright strict)를 선택한다.

## Considered Options

- **Python** (선택): LLM/임베딩 생태계, 공식 `a2a-python` SDK, pydantic 기반 카드 검증. 타입 안전성은 pyright strict + TDD로 보강한다.
- **TypeScript** (기각): DDD 타입 표현력은 우위지만 LLM/임베딩/A2A 생태계가 얇고 기획서의 Python 구조와 어긋난다. `RoutingDecision`의 합·곱 타입은 Python sealed 계층 + `match`로 재현 가능하다.
