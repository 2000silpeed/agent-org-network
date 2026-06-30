"""author 선택 시임 결정론 테스트 — `runtime_select.select_runtime`과 대칭.

`AON_AUTHOR` env 분기를 monkeypatch로 결정론 검증한다(실 transport·subprocess 0):
  - 미설정/`demo` → FakeAuthor(기존 데모 경로·기본).
  - `claude-code`/`llm` → LlmAuthor(ClaudeCodeTransport 주입).
  - 알 수 없는 값 → 명시 실패(SystemExit).
모델 선택(`AON_AUTHOR_MODEL`)도 함께 단언한다.
"""

from __future__ import annotations

from datetime import date

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.author_select import DEFAULT_AUTHOR_MODEL, select_author
from agent_org_network.okf_authoring import FakeAuthor, LlmAuthor


def _card() -> AgentCard:
    return AgentCard(
        agent_id="cs_ops",
        owner="cs_lead",
        team="cs",
        summary="고객지원 운영",
        domains=["환불", "보상"],
        last_reviewed_at=date(2026, 1, 1),
        knowledge_sources=["kb://cs"],
    )


def test_미설정이면_FakeAuthor_기본(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_AUTHOR", raising=False)
    author = select_author(_card())
    assert isinstance(author, FakeAuthor)


def test_demo면_FakeAuthor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "demo")
    author = select_author(_card())
    assert isinstance(author, FakeAuthor)


def test_공백_대문자_demo도_FakeAuthor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "  DEMO  ")
    author = select_author(_card())
    assert isinstance(author, FakeAuthor)


def test_claude_code면_LlmAuthor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    author = select_author(_card())
    assert isinstance(author, LlmAuthor)


def test_llm_별칭도_LlmAuthor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "llm")
    author = select_author(_card())
    assert isinstance(author, LlmAuthor)


def test_LlmAuthor_기본_모델은_빠른_sonnet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    monkeypatch.delenv("AON_AUTHOR_MODEL", raising=False)
    author = select_author(_card())
    assert isinstance(author, LlmAuthor)
    assert author._model == DEFAULT_AUTHOR_MODEL  # pyright: ignore[reportPrivateUsage]
    assert DEFAULT_AUTHOR_MODEL == "claude-sonnet-4-6"


def test_AON_AUTHOR_MODEL이_모델을_덮는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    monkeypatch.setenv("AON_AUTHOR_MODEL", "claude-opus-4-8")
    author = select_author(_card())
    assert isinstance(author, LlmAuthor)
    assert author._model == "claude-opus-4-8"  # pyright: ignore[reportPrivateUsage]


def test_LlmAuthor에_카드_owned_domains_주입(monkeypatch: pytest.MonkeyPatch) -> None:
    """치명 갭 교정 — select_author가 card.domains를 LlmAuthor.owned_domains로 넘긴다.

    그래야 split 프롬프트가 유효 domain을 모델에 알려 admit_okf over-claim 전량 드롭을 막는다."""
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    author = select_author(_card())
    assert isinstance(author, LlmAuthor)
    assert author._owned_domains == ("환불", "보상")  # pyright: ignore[reportPrivateUsage]


def test_알수없는_값은_명시_실패(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "gpt-magic")
    with pytest.raises(SystemExit):
        select_author(_card())
