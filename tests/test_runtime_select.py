"""select_runtime 결정론 테스트 — claude-code 분기 git_gateway 주입 검증.

실 SDK·실 claude·네트워크 0. (1) 미설정/claude-code 분기에서 주입한 git_gateway가
ClaudeCodeRuntime에 전달돼 커밋 스냅샷 모드가 켜지는지, (2) git_gateway 미주입이면
기존 동작(working tree 직독)인지만 고정 검증한다. 다른 공급자 분기는 SDK 의존이라
이 게이트(결정론)에 안 들인다(extra 설치·OAuth 영역).
"""

import pytest

from agent_org_network.git_gateway import FakeGitGateway
from agent_org_network.runtime import ClaudeCodeRuntime
from agent_org_network.runtime_select import select_runtime


def test_claude_code_분기에서_git_gateway가_런타임에_주입된다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AON_PROVIDER 미설정(레거시 claude-code 기본) — 주입한 게이트웨이가 런타임으로 흘러야 한다.
    monkeypatch.delenv("AON_PROVIDER", raising=False)
    monkeypatch.delenv("AON_RUNTIME", raising=False)
    gw = FakeGitGateway()

    runtime = select_runtime("okf", git_gateway=gw)

    assert isinstance(runtime, ClaudeCodeRuntime)
    assert runtime._git_gateway is gw  # pyright: ignore[reportPrivateUsage]


def test_git_gateway_미주입이면_None으로_기존동작(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 게이트웨이를 안 넘기면 _git_gateway=None — 기존 working tree 직독 경로(하위호환).
    monkeypatch.delenv("AON_PROVIDER", raising=False)
    monkeypatch.delenv("AON_RUNTIME", raising=False)

    runtime = select_runtime("okf")

    assert isinstance(runtime, ClaudeCodeRuntime)
    assert runtime._git_gateway is None  # pyright: ignore[reportPrivateUsage]


def test_claude_code_명시값도_git_gateway를_주입한다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AON_PROVIDER=claude-code 명시도 claude-code 분기 — 게이트웨이 주입 동일.
    monkeypatch.setenv("AON_PROVIDER", "claude-code")
    gw = FakeGitGateway()

    runtime = select_runtime("okf", git_gateway=gw)

    assert isinstance(runtime, ClaudeCodeRuntime)
    assert runtime._git_gateway is gw  # pyright: ignore[reportPrivateUsage]
