"""author 선택 시임 결정론 테스트 — `runtime_select.select_runtime`과 대칭.

`AON_AUTHOR` env 분기를 monkeypatch로 결정론 검증한다(실 LLM 호출·실 네트워크 0 —
어댑터 *생성*까지만 검증하고 transport는 부르지 않는다):
  - 미설정/`claude-api`/`anthropic` → LlmAuthor(AnthropicSdkTransport·프로덕션 기본).
    anthropic extra 미설치 환경이면 SystemExit이 계약이라 skip이 아니라 그 분기를 단언.
  - `claude-code`/`llm` → LlmAuthor(ClaudeCodeTransport 주입).
  - 알 수 없는 값(구 `demo` 포함) → 명시 실패(SystemExit — 실사용 가짜 0·조용한 폴백 없음).
모델 선택(`AON_AUTHOR_MODEL`)도 함께 단언한다.
"""

from __future__ import annotations

import pytest

from agent_org_network.author_select import DEFAULT_AUTHOR_MODEL, select_author
from agent_org_network.okf_authoring import LlmAuthor


def _anthropic_installed() -> bool:
    try:
        import anthropic  # pyright: ignore[reportUnusedImport]  # noqa: F401
    except ImportError:
        return False
    return True


def test_미설정이면_실_LlmAuthor_기본(monkeypatch: pytest.MonkeyPatch) -> None:
    """프로덕션 기본 = 실 추출(실사용 가짜 0 — T11.7d 사용자 결정).

    anthropic extra가 있으면 LlmAuthor(AnthropicSdkTransport), 없으면 명확한
    SystemExit 안내가 계약이다 — 어느 쪽이든 데모 더블로 조용히 떨어지지 않는다.
    """
    monkeypatch.delenv("AON_AUTHOR", raising=False)
    if _anthropic_installed():
        author = select_author()
        assert isinstance(author, LlmAuthor)
    else:
        with pytest.raises(SystemExit):
            select_author()


def test_claude_api_별칭도_기본과_동일(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "  CLAUDE-API  ")
    if _anthropic_installed():
        assert isinstance(select_author(), LlmAuthor)
    else:
        with pytest.raises(SystemExit):
            select_author()


def test_demo는_제거됐다_명시_실패(monkeypatch: pytest.MonkeyPatch) -> None:
    """구 demo 분기는 실사용 가짜 0 결정으로 제거 — 조용한 폴백 대신 명시 실패."""
    monkeypatch.setenv("AON_AUTHOR", "demo")
    with pytest.raises(SystemExit):
        select_author()


def test_claude_code면_LlmAuthor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    author = select_author()
    assert isinstance(author, LlmAuthor)


def test_llm_별칭도_LlmAuthor(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "llm")
    author = select_author()
    assert isinstance(author, LlmAuthor)


def test_기본_모델은_sonnet_5(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    monkeypatch.delenv("AON_AUTHOR_MODEL", raising=False)
    author = select_author()
    assert isinstance(author, LlmAuthor)
    assert author._model == DEFAULT_AUTHOR_MODEL  # pyright: ignore[reportPrivateUsage]
    assert DEFAULT_AUTHOR_MODEL == "claude-sonnet-5"


def test_AON_AUTHOR_MODEL이_모델을_덮는다(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    monkeypatch.setenv("AON_AUTHOR_MODEL", "claude-opus-4-8")
    author = select_author()
    assert isinstance(author, LlmAuthor)
    assert author._model == "claude-opus-4-8"  # pyright: ignore[reportPrivateUsage]


def test_domain_제약은_생성이_아니라_split_호출로_흐른다(monkeypatch: pytest.MonkeyPatch) -> None:
    """유효 domain 제약은 생성자 상태가 아니라 파이프라인이 split 호출 시 card.domains를
    allowed_domains 인자로 넘겨 흐른다(run_authoring_pipeline). select_author는 어댑터
    선택만 책임진다 — 카드 인자 자체를 받지 않는다."""
    monkeypatch.setenv("AON_AUTHOR", "claude-code")
    author = select_author()
    assert isinstance(author, LlmAuthor)


def test_알수없는_값은_명시_실패(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AON_AUTHOR", "gpt-magic")
    with pytest.raises(SystemExit):
        select_author()
