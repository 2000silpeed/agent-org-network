# 충돌 해소를 정적 규칙이 아닌 판례(Precedent) 학습 루프로 모델링

상태: accepted (2026-06-20)

담당이 깨끗하게 정해지지 않는 Conflict(후보가 여럿인 **Overlap**, 후보가 없는 **Gap**)는 업무의 그레이 영역이라 한 번에 규칙으로 닫을 수 없다. 그래서 충돌을 사람이 해소하게 하고(Overlap은 후보 Owner 합의 → 실패 시 Manager, Gap은 Manager가 지정 또는 신규 카드 생성), 그 합의를 append-only **Precedent**로 기록해 라우터가 이후 유사 케이스에 참조하게 한다. 라우터의 행동 = 선언된 규칙 + 누적된 판례.

## Consequences

- append-only 감사 로그가 곧 판례 저장소다 — 별도 시스템이 아니다.
- Precedent는 그대로 라우팅 회귀 테스트 케이스가 된다(학습 루프 = TDD 스위트).
- 불변식: **어떤 질문도 미아로 남지 않는다.** 0건 매칭 → 루트 Manager로 Escalation.
- 반복되는 Gap은 "아무도 안 맡는 영역" backlog로 드러나 신규 Agent Card 필요 신호가 된다.
- 합의는 카드를 파괴적으로 수정하지 않는다 — 카드 경계 수정은 판례의 선택적 후속 결과일 뿐이다.
- Manager는 팀/도메인별 계층(`escalates_to`) + 루트 기본 Manager로 표현하며, 공통 상위를 찾는 트리 등반(LCA)은 후순위로 미룬다.
