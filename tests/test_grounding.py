"""ADR 0037 슬라이스 A — co-grounding 순수 기계장치 단위 테스트.

결정론: FakeGroundingSelector·주입 tie-break 정책만 사용, 실 LLM/신뢰도 0.
Contested arm 행동 변경(슬라이스 C)은 이 테스트의 범위 밖 — 여기선 selector·
GroundingSet·문자열 조립기만 검증한다(inert-but-tested).
"""

from datetime import date

import pytest
from pydantic import ValidationError

from agent_org_network.agent_card import AgentCard
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.grounding import (
    ContestedGroundingSelector,
    GroundingSet,
    assemble_grounding_text,
    first_by_agent_id,
)


def _card(agent_id: str, owner: str = "alice", domains: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary=f"{agent_id} 담당",
        domains=domains if domains is not None else ["cs"],
        last_reviewed_at=date(2026, 6, 20),
    )


# ── GroundingSet 값 객체 ────────────────────────────────────────────────────


def test_GroundingSet은_frozen이라_수정_불가():
    primary = _card("cs_ops")
    grounding_set = GroundingSet(primary=primary)
    with pytest.raises(ValidationError):
        grounding_set.primary = _card("billing")  # type: ignore[misc]


def test_GroundingSet_agent_ids는_primary_먼저_supporting_순서대로():
    primary = _card("cs_ops")
    supporting = (_card("billing"), _card("legal"))
    grounding_set = GroundingSet(primary=primary, supporting=supporting)

    assert grounding_set.agent_ids() == ("cs_ops", "billing", "legal")


def test_GroundingSet_supporting_기본값은_빈_튜플():
    grounding_set = GroundingSet(primary=_card("cs_ops"))
    assert grounding_set.supporting == ()
    assert grounding_set.agent_ids() == ("cs_ops",)


def test_GroundingSet은_primary가_supporting에_들면_거부한다():
    primary = _card("cs_ops")
    with pytest.raises(ValidationError):
        GroundingSet(primary=primary, supporting=(primary, _card("billing")))


def test_GroundingSet은_supporting_중복을_거부한다():
    primary = _card("cs_ops")
    dup = _card("billing")
    with pytest.raises(ValidationError):
        GroundingSet(primary=primary, supporting=(dup, dup))


# ── ContestedGroundingSelector ──────────────────────────────────────────────


def test_ContestedGroundingSelector는_Contested를_전원_GroundingSet으로_접는다():
    candidates = (_card("cs_ops"), _card("billing"), _card("legal"))
    decision = Contested(candidates=candidates)
    selector = ContestedGroundingSelector(tie_break=first_by_agent_id)

    result = selector.select(decision)

    assert result is not None
    assert set(result.agent_ids()) == {"cs_ops", "billing", "legal"}
    assert len(result.supporting) == 2


def test_ContestedGroundingSelector는_Routed에_None을_반환한다():
    decision = Routed(primary=_card("cs_ops"))
    selector = ContestedGroundingSelector(tie_break=first_by_agent_id)

    assert selector.select(decision) is None


def test_ContestedGroundingSelector는_Unowned에_None을_반환한다():
    decision = Unowned(escalated_to="root_user")
    selector = ContestedGroundingSelector(tie_break=first_by_agent_id)

    assert selector.select(decision) is None


def test_ContestedGroundingSelector_primary는_주입_tie_break_정책이_결정한다():
    """agent_id 사전순 tie-break 정책 주입 — 후보 순서를 뒤섞어도 같은 primary."""
    candidates_a = (_card("zeta"), _card("alpha"), _card("mu"))
    candidates_b = (_card("mu"), _card("zeta"), _card("alpha"))
    selector = ContestedGroundingSelector(tie_break=first_by_agent_id)

    result_a = selector.select(Contested(candidates=candidates_a))
    result_b = selector.select(Contested(candidates=candidates_b))

    assert result_a is not None
    assert result_b is not None
    assert result_a.primary.agent_id == "alpha"
    assert result_b.primary.agent_id == "alpha"


def test_ContestedGroundingSelector는_커스텀_tie_break_정책을_주입받는다():
    """FakeSelector 정신 — 임의 정책(예: 후보 튜플 순서 first)을 주입해 결정론 단언."""

    def _first_in_tuple_order(candidates: tuple[AgentCard, ...]) -> AgentCard:
        return candidates[0]

    candidates = (_card("zeta"), _card("alpha"), _card("mu"))
    selector = ContestedGroundingSelector(tie_break=_first_in_tuple_order)

    result = selector.select(Contested(candidates=candidates))

    assert result is not None
    assert result.primary.agent_id == "zeta"
    assert set(c.agent_id for c in result.supporting) == {"alpha", "mu"}


def test_first_by_agent_id는_결정론적으로_사전순_최소를_고른다():
    candidates = (_card("zeta"), _card("alpha"), _card("mu"))
    assert first_by_agent_id(candidates).agent_id == "alpha"


# ── 다중 접지 문자열 조립 ────────────────────────────────────────────────────


def test_assemble_grounding_text는_섹션을_agent_ids_순서대로_병합한다():
    grounding_set = GroundingSet(primary=_card("cs_ops"), supporting=(_card("billing"),))

    def _lookup(agent_id: str) -> str:
        return {"cs_ops": "고객지원 지식", "billing": "결제 지식"}[agent_id]

    text = assemble_grounding_text(grounding_set, _lookup)

    assert text == "### cs_ops\n고객지원 지식\n\n### billing\n결제 지식"


def test_assemble_grounding_text는_빈_본문을_건너뛰지_않고_섹션_형태를_유지한다():
    grounding_set = GroundingSet(primary=_card("cs_ops"))

    text = assemble_grounding_text(grounding_set, lambda _agent_id: "")

    assert text == "### cs_ops\n"


def test_assemble_grounding_text_순서는_항상_결정론이다():
    grounding_set = GroundingSet(
        primary=_card("cs_ops"), supporting=(_card("billing"), _card("legal"))
    )
    lookups = {"cs_ops": "a", "billing": "b", "legal": "c"}

    text1 = assemble_grounding_text(grounding_set, lambda aid: lookups[aid])
    text2 = assemble_grounding_text(grounding_set, lambda aid: lookups[aid])

    assert text1 == text2 == "### cs_ops\na\n\n### billing\nb\n\n### legal\nc"
