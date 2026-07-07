"""Co-grounding(다중 접지) 순수 기계장치 — ADR 0037 결정 3·4.

`GroundingSet`(값 객체)·`GroundingSelector`(포트)·`ContestedGroundingSelector`(v0 구현)·
`assemble_grounding_text`(다중 접지 문자열 조립 헬퍼)를 담는다. 전부 순수·결정론 —
실 KnowledgeStore 다중 조회·크로스머신 배선은 이 모듈 밖(mcp-runtime 슬라이스 D).

Contested arm 행동 변경(슬라이스 C)은 이 모듈이 건드리지 않는다 — 여기 seam은
만들어지되 `AskOrg.handle`/`handle_stream`에는 아직 꽂히지 않는다(inert-but-tested).
"""

from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel, model_validator

from agent_org_network.agent_card import AgentCard
from agent_org_network.complement import EdgeStore
from agent_org_network.decision import Contested, Routed, RoutingDecision

# tie-break 정책 seam(ADR 0037 결정 5) — 동률 후보에서 결정론으로 primary 하나를
# 고른다. stage-2 실 신뢰도는 게이트 밖이므로 여기선 *주입 가능한 정책*으로만 둔다.
TieBreakPolicy = Callable[["tuple[AgentCard, ...]"], AgentCard]


def first_by_agent_id(candidates: "tuple[AgentCard, ...]") -> AgentCard:
    """agent_id 사전순으로 가장 앞선 후보를 primary로 고르는 기본 tie-break 정책.

    결정론 — 후보 튜플 순서가 달라도 같은 입력 집합이면 같은 primary를 낸다.
    """
    return min(candidates, key=lambda card: card.agent_id)


class GroundingSet(BaseModel, frozen=True):
    """co-grounding이 접지할 지식 원천 집합(ADR 0037 결정 4).

    `primary`가 answered_by가 될 카드, `supporting`은 함께 접지하는 인접 카드들
    (primary 제외·중복 금지). `Contested.candidates`·`Routed.collaborators`와
    대칭이되 primary가 확정된다는 점이 다르다(담당 미정이 아니라 근거 확장).
    """

    primary: AgentCard
    supporting: tuple[AgentCard, ...] = ()

    @model_validator(mode="after")
    def _validate_supporting(self) -> "GroundingSet":
        if any(card.agent_id == self.primary.agent_id for card in self.supporting):
            raise ValueError("primary는 supporting에 들 수 없다(중복 귀속 금지)")
        seen: set[str] = set()
        for card in self.supporting:
            if card.agent_id in seen:
                raise ValueError(f"supporting에 중복 agent_id가 있다: {card.agent_id!r}")
            seen.add(card.agent_id)
        return self

    def agent_ids(self) -> tuple[str, ...]:
        """(primary, *supporting) 순서의 agent_id 튜플 — 접지 조립 순서의 기준."""
        return (self.primary.agent_id, *(card.agent_id for card in self.supporting))


class GroundingSelector(Protocol):
    """`RoutingDecision`을 받아 접지 대상 `GroundingSet`을 고르는 정책 포트.

    접지 원천 선택 정책을 하드코딩하지 않는 주입 seam(ADR 0037 결정 4).
    """

    def select(self, decision: RoutingDecision) -> GroundingSet | None: ...


class ContestedGroundingSelector:
    """v0 GroundingSelector — Contested만 전원 GroundingSet으로 접는다.

    `Contested`면 candidates 전원을 접지 대상으로 삼는다(primary = tie_break이
    고른 top-1, supporting = 나머지). `Routed`/`Unowned`면 `None`(단일 접지
    폴백 — 회귀 0). `tie_break`은 주입 seam(기본 `first_by_agent_id`).
    """

    def __init__(self, tie_break: TieBreakPolicy = first_by_agent_id) -> None:
        self._tie_break = tie_break

    def select(self, decision: RoutingDecision) -> GroundingSet | None:
        if not isinstance(decision, Contested):
            return None
        candidates = decision.candidates
        primary = self._tie_break(candidates)
        supporting = tuple(card for card in candidates if card.agent_id != primary.agent_id)
        return GroundingSet(primary=primary, supporting=supporting)


class EdgeGroundingSelector:
    """합의-소싱 `ComplementEdge` 이웃을 `Routed`에서도 접지하는 selector(ADR 0038 결정 4).

    `decision.intent`+`decision.primary.agent_id`로 `EdgeStore.neighbors`를 조회해
    이웃 agent_id들을 얻고 `card_lookup`(보통 Registry.get)으로 카드를 해소한다.

    **선택시점 재검증(생애주기 소멸 규칙, ADR 0038 결정 5)**: `decision.intent in
    card.domains`인 이웃만 supporting에 넣는다 — 카드가 사라졌거나(card_lookup이
    None) 그 사이 domain을 카드에서 뺐으면 자연히 skip된다(watcher·삭제 API 불요,
    publish 수용 시 `concept.domain ∈ card.domains` 재검증과 동형).

    `Routed`가 아니거나 유효 이웃이 0이면 `None`(단일 접지 폴백 — 회귀 0).
    `answered_by`는 여전히 primary 단일(Authority 정합) — supporting은 접지
    출처일 뿐 authority 0.
    """

    def __init__(
        self,
        edge_store: EdgeStore,
        card_lookup: Callable[[str], AgentCard | None],
    ) -> None:
        self._edge_store = edge_store
        self._card_lookup = card_lookup

    def select(self, decision: RoutingDecision) -> GroundingSet | None:
        if not isinstance(decision, Routed):
            return None
        neighbor_ids = self._edge_store.neighbors(decision.intent, decision.primary.agent_id)
        supporting: list[AgentCard] = []
        for agent_id in neighbor_ids:
            card = self._card_lookup(agent_id)
            if card is None:
                continue
            if decision.intent not in card.domains:
                continue
            supporting.append(card)
        if not supporting:
            return None
        return GroundingSet(primary=decision.primary, supporting=tuple(supporting))


class ChainGroundingSelector:
    """여러 `GroundingSelector`를 순서대로 시도하는 합성 selector(ADR 0038 결정 4).

    각 selector를 튜플 순서대로 시도해 첫 non-None `GroundingSet`을 돌린다. 전부
    `None`이면 `None`. `AskOrg`의 **단일** `grounding_selector` seam(ADR 0037 결정 4)을
    보존하며 `EdgeGroundingSelector`(Routed 전용)·`ContestedGroundingSelector`
    (Contested 전용)처럼 처분이 배타인 selector들을 합성한다 — 순서 무관이되 명시
    순서로 결정론을 보장한다. 자신도 `GroundingSelector`를 만족한다(구조적 타이핑).
    """

    def __init__(self, selectors: "tuple[GroundingSelector, ...]") -> None:
        self._selectors = selectors

    def select(self, decision: RoutingDecision) -> GroundingSet | None:
        for selector in self._selectors:
            result = selector.select(decision)
            if result is not None:
                return result
        return None


def assemble_grounding_text(
    grounding_set: GroundingSet, lookup: Callable[[str], str]
) -> str:
    """`GroundingSet`의 각 agent_id 접지 텍스트를 `read_okf_bundle`/`resolve_knowledge_text`와
    같은 포맷("### {agent_id}\\n{body}")으로 조립한다(순서: `agent_ids()` 결정론).

    `lookup`은 agent_id → 접지 본문 텍스트를 돌려주는 주입 함수(실 KnowledgeStore
    다중 조회 배선은 이 함수 밖 — mcp-runtime 슬라이스 D). 순수 조립 로직만 담당.
    """
    sections = [f"### {agent_id}\n{lookup(agent_id)}" for agent_id in grounding_set.agent_ids()]
    return "\n\n".join(sections)
