# User와 Agent를 분리한 2노드 그래프로 조직을 모델링

상태: accepted (2026-06-20)

조직을 두 종류의 노드(**User**=사람, **Agent Card**=업무 에이전트)와 엣지로 모델링한다. `owns`(User→Agent)의 출발 User가 그 Agent의 **Owner**, `manages`(User→User)의 상위 User가 **Manager**(조직장), `maintains`(User→Agent)가 **Maintainer**다. 역할(Owner/Manager/Maintainer)은 노드가 아니라 엣지에서 파생된다. 사람 위계는 Agent 카드끼리가 아니라 User 사이의 `manages` 엣지로 표현되며, 미해소 Conflict의 escalation은 `Agent → Owner(User) → 그 User의 manager → … → 루트 User`로 사람 그래프를 타고 오른다.

카디널리티: User의 `manager`는 0..1(루트는 0), 한 User는 여러 Agent를 owns, 한 Agent의 Owner는 정확히 1명.

## Consequences

- `escalates_to`가 카드에서 빠지고 `User.manager`로 이동한다(ADR 0004의 카드 필드 목록 수정).
- Manager는 "Owner의 일종"이 아니라 사람을 관리하는 User 역할 — Owner를 겸할 수도, 아닐 수도 있다.
- 참조 무결성: `card.owner`·`user.manager`는 실재 User를 가리켜야 한다(`Registry.validate`).
- 루트 User가 Gap·미해소 충돌의 최종 escalation 종착이다.
