import dataclasses
from datetime import date

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import Answer, StubRuntime


def card() -> AgentCard:
    return AgentCard(
        agent_id="contract_ops",
        owner="D",
        team="ops",
        summary="계약 관련 질문 담당",
        domains=["계약 검토"],
        last_reviewed_at=date(2026, 6, 20),
        knowledge_sources=["wiki/contracts", "drive/policy"],
    )


def test_StubRuntime_answer가_Answer_인스턴스를_반환한다():
    result = StubRuntime().answer("아무 질문", card())
    assert isinstance(result, Answer)


def test_StubRuntime_answer_text에_agent_id와_summary가_포함된다():
    c = card()
    result = StubRuntime().answer("아무 질문", c)
    assert c.agent_id in result.text
    assert c.summary in result.text


def test_StubRuntime_answer_mode가_full이다():
    result = StubRuntime().answer("아무 질문", card())
    assert result.mode == "full"


def test_StubRuntime_answer_sources가_knowledge_sources의_tuple이다():
    c = card()
    result = StubRuntime().answer("아무 질문", c)
    assert result.sources == tuple(c.knowledge_sources)


def test_Answer가_frozen이라_필드_재할당시_FrozenInstanceError가_난다():
    answer = Answer(text="hello", sources=(), mode="full")
    with pytest.raises(dataclasses.FrozenInstanceError):
        answer.text = "changed"  # type: ignore[misc]
