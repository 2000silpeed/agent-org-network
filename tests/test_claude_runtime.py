"""ClaudeCodeRuntime 단위 테스트 — FakeRunner 주입으로 결정론 유지.

실제 `claude -p`는 비결정·느리므로 절대 호출하지 않는다. 여기서는 (1) 카드로
페르소나 프롬프트를 제대로 구성하는지, (2) runner stdout을 Answer로 옳게 변환하는지,
(3) 빈/timeout/비정상 종료를 graceful 폴백하는지만 검증한다.
"""

import subprocess
from datetime import date

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import Answer, ClaudeCodeRuntime


def card(
    agent_id: str = "cs_ops",
    owner: str = "cs_lead",
    team: str = "cs",
    summary: str = "환불 정책과 처리 절차를 안내합니다.",
    domains: list[str] | None = None,
    can_answer: list[str] | None = None,
    knowledge_sources: list[str] | None = None,
) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team=team,
        summary=summary,
        domains=domains if domains is not None else ["환불", "보상"],
        last_reviewed_at=date(2026, 6, 20),
        can_answer=can_answer or [],
        knowledge_sources=knowledge_sources or [],
    )


class _RecordingRunner:
    """프롬프트를 받아 고정 응답을 돌려주며, 마지막 프롬프트를 기록한다.

    `cwd`는 ClaudeRunner Protocol(ADR 0013 OKF 소비)의 선택 키워드 — 이 테스트들은 OKF
    번들을 두지 않아(okf_root 미주입) cwd가 전달되지 않지만, 시그니처로 흡수해 Protocol에
    부합한다(행위 불변).
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None

    def __call__(self, prompt: str, *, cwd: str | None = None) -> str:
        self.last_prompt = prompt
        return self.reply


def test_프롬프트에_카드_페르소나가_녹는다():
    c = card(
        knowledge_sources=["위키/환불정책", "Notion/보상표"],
        can_answer=["환불 가능 여부"],
    )
    runner = _RecordingRunner("응답 본문")
    runtime = ClaudeCodeRuntime(runner=runner)

    runtime.answer("환불 되나요?", c)

    prompt = runner.last_prompt
    assert prompt is not None
    # 담당자 정체성(owner·team·agent_id)
    assert "cs_lead" in prompt
    assert "cs" in prompt
    assert "cs_ops" in prompt
    # 역할·도메인·출처·can_answer가 맥락으로 들어감
    assert "환불 정책과 처리 절차를 안내합니다." in prompt
    assert "환불" in prompt and "보상" in prompt
    assert "위키/환불정책" in prompt
    assert "Notion/보상표" in prompt
    assert "환불 가능 여부" in prompt
    # 질문 본문 포함
    assert "환불 되나요?" in prompt


def test_runner_응답이_Answer로_변환된다():
    c = card(knowledge_sources=["위키/환불정책"])
    runner = _RecordingRunner("  네, 7일 이내 전액 환불됩니다.\n")
    runtime = ClaudeCodeRuntime(runner=runner)

    ans = runtime.answer("환불 되나요?", c)

    assert isinstance(ans, Answer)
    # stdout은 strip 되어 들어간다
    assert ans.text == "네, 7일 이내 전액 환불됩니다."
    # sources는 카드 knowledge_sources(레이블)
    assert ans.sources == ("위키/환불정책",)
    assert ans.mode == "full"


def test_빈_도메인_출처_없는_카드도_프롬프트_구성된다():
    c = card(domains=[], knowledge_sources=[], can_answer=[])
    runner = _RecordingRunner("답")
    runtime = ClaudeCodeRuntime(runner=runner)

    ans = runtime.answer("질문?", c)

    assert ans.text == "답"
    assert ans.sources == ()
    prompt = runner.last_prompt
    assert prompt is not None
    assert "질문?" in prompt


def test_빈_응답이면_폴백_Answer():
    c = card(knowledge_sources=["위키/환불정책"])
    runtime = ClaudeCodeRuntime(runner=_RecordingRunner("   \n  "))

    ans = runtime.answer("환불 되나요?", c)

    assert ans.mode == "full"
    assert "cs_ops" in ans.text
    assert ans.sources == ("위키/환불정책",)


def test_timeout이면_폴백_Answer():
    c = card(knowledge_sources=["위키/환불정책"])

    def _boom(_prompt: str, *, cwd: str | None = None) -> str:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    runtime = ClaudeCodeRuntime(runner=_boom)

    ans = runtime.answer("환불 되나요?", c)

    assert isinstance(ans, Answer)
    assert "cs_ops" in ans.text
    assert ans.sources == ("위키/환불정책",)
    assert ans.mode == "full"


def test_비정상_종료_예외면_폴백_Answer():
    c = card()

    def _boom(_prompt: str, *, cwd: str | None = None) -> str:
        raise RuntimeError("claude -p exited with 1: boom")

    runtime = ClaudeCodeRuntime(runner=_boom)

    ans = runtime.answer("환불 되나요?", c)

    assert isinstance(ans, Answer)
    assert "cs_ops" in ans.text
    assert ans.mode == "full"


def test_기본_생성자도_조립되고_프롬프트_구성은_runner없이_된다():
    # 기본 생성자는 실제 claude -p 호출 함수를 갖지만 여기서 호출하진 않는다.
    # build_prompt는 runner와 무관하므로 안전하게 검증 가능.
    runtime = ClaudeCodeRuntime()
    prompt = runtime.build_prompt("환불 되나요?", card(knowledge_sources=["위키/환불정책"]))

    assert "cs_ops" in prompt
    assert "위키/환불정책" in prompt
    assert "환불 되나요?" in prompt
