"""합의-소싱 COMPLEMENTS 엣지 — ADR 0038 결정 1·2.

`ComplementEdge`(frozen 값 객체)·`EdgeStore`(포트)·`InMemoryEdgeStore`(구현)를 담는다.
`Precedent`(라우팅 학습)와 대칭인 두 번째 합의 학습 산물이다 — 판례가 "이 intent의
front는 누구인가"(미래 라우팅)를 바꾸듯, 상보 엣지는 "front를 누가 보완하나"(미래
접지)를 바꾼다. 전이 ≠ 기록: 미래 접지 결정(`EdgeGroundingSelector`)이 읽는 학습된
도메인 상태지 절차 로그(AuditLog)가 아니다.

방출 지점은 이 모듈 밖(`conflict.py`의 `ConsensusService` — `Agreed` 분기에서
`precedents.record` 바로 곁, ADR 0038 결정 2·3). 이 모듈은 순수 값 객체+포트만.
"""

from typing import Protocol

from pydantic import BaseModel, model_validator


class ComplementEdge(BaseModel, frozen=True):
    """합의가 방출하는 상보 엣지 — primary(front)가 supporting(보완 지식)과 함께
    접지돼야 함을 나타내는 유향 관계(ADR 0038 결정 1).

    - `intent`: 이 관계가 성립하는 라우팅 라벨(그 intent의 `Resolution`이 이 엣지를 낳음).
    - `primary_id`: 라우팅된 front agent_id(`Resolution.primary`).
    - `supporting_id`: 함께 접지할 이웃 agent_id(진 후보 카드).

    **유향**(primary → supporting) — front가 답하고 complement가 접지하는 관계는 한
    방향이고, 역방향은 그 intent의 판례가 primary를 잠가 발생하지 않는다. N후보 →
    1 primary + (N-1) 잠재 supporting이라 패자 카드당 1엣지(pairwise).
    """

    intent: str
    primary_id: str
    supporting_id: str

    @model_validator(mode="after")
    def _validate(self) -> "ComplementEdge":
        if not self.intent:
            raise ValueError("ComplementEdge.intent는 빈 문자열일 수 없다")
        if not self.primary_id:
            raise ValueError("ComplementEdge.primary_id는 빈 문자열일 수 없다")
        if not self.supporting_id:
            raise ValueError("ComplementEdge.supporting_id는 빈 문자열일 수 없다")
        if self.primary_id == self.supporting_id:
            raise ValueError(
                "primary_id와 supporting_id가 같을 수 없다(자기 접지 거부): "
                f"{self.primary_id!r}"
            )
        return self


class EdgeStore(Protocol):
    """`ComplementEdge` 보관·조회 포트 — `PrecedentStore`·`ConflictCaseStore`와 같은
    포트 패턴(Protocol + `InMemoryEdgeStore`, ADR 0038 결정 2).
    """

    def record(self, edge: ComplementEdge) -> None: ...

    def neighbors(self, intent: str, primary_id: str) -> tuple[str, ...]:
        """그 intent에서 primary_id를 front로 둔 supporting agent_id들.

        멱등(중복 supporting record는 무시)·순서 결정론(삽입 순).
        """
        ...


class InMemoryEdgeStore:
    """append 멱등·순서 결정론 in-memory `EdgeStore` 구현.

    `(intent, primary_id)` 키로 supporting agent_id들을 순서 보존 집합(dict)에
    보관한다 — 같은 supporting을 중복 record해도 neighbors엔 한 번만 남는다.
    """

    def __init__(self) -> None:
        self._edges: dict[tuple[str, str], dict[str, None]] = {}

    def record(self, edge: ComplementEdge) -> None:
        key = (edge.intent, edge.primary_id)
        self._edges.setdefault(key, {})[edge.supporting_id] = None

    def neighbors(self, intent: str, primary_id: str) -> tuple[str, ...]:
        return tuple(self._edges.get((intent, primary_id), {}).keys())
