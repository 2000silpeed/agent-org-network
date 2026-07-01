"""ClaudeCodeRuntime 단위 테스트 — FakeRunner 주입으로 결정론 유지.

실제 `claude -p`는 비결정·느리므로 절대 호출하지 않는다. 여기서는 (1) 카드로
페르소나 프롬프트를 제대로 구성하는지, (2) runner stdout을 Answer로 옳게 변환하는지,
(3) 빈/timeout/비정상 종료를 graceful 폴백하는지만 검증한다.
"""

import subprocess
from collections.abc import Iterator
from datetime import date

from agent_org_network.agent_card import AgentCard
from agent_org_network.runtime import (
    Answer,
    AnswerChunk,
    ClaudeCodeRuntime,
    StreamingRuntime,
)


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
    """프롬프트를 받아 고정 응답을 돌려주며, 마지막 user 프롬프트·system_prompt를 기록한다.

    `cwd`는 ClaudeRunner Protocol(ADR 0013 OKF 소비)의 선택 키워드 — 이 테스트들은 OKF
    번들을 두지 않아(okf_root 미주입) cwd가 전달되지 않지만, 시그니처로 흡수해 Protocol에
    부합한다(행위 불변). `system_prompt`는 노출 격리(본 작업)로 추가된 선택 키워드 —
    `ClaudeCodeRuntime`이 페르소나를 system으로 분리해 넘기는지 단언하려 기록한다.
    """

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.last_prompt: str | None = None
        self.last_system: str | None = None

    def __call__(
        self, prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> str:
        self.last_prompt = prompt
        self.last_system = system_prompt
        return self.reply


def test_페르소나는_system에_질문은_user에_분리된다():
    # 노출 격리(본 작업): 페르소나(정체성·도메인·출처·can_answer)는 system_prompt로,
    # 질문은 user 프롬프트로 분리된다 — 둘이 안 섞인다.
    c = card(
        knowledge_sources=["위키/환불정책", "Notion/보상표"],
        can_answer=["환불 가능 여부"],
    )
    runner = _RecordingRunner("응답 본문")
    runtime = ClaudeCodeRuntime(runner=runner)

    runtime.answer("환불 되나요?", c)

    system = runner.last_system
    user = runner.last_prompt
    assert system is not None
    assert user is not None

    # 페르소나·정체성·맥락은 system에 녹는다
    assert "cs_lead" in system
    assert "cs" in system
    assert "cs_ops" in system
    assert "환불 정책과 처리 절차를 안내합니다." in system
    assert "환불" in system and "보상" in system
    assert "위키/환불정책" in system
    assert "Notion/보상표" in system
    assert "환불 가능 여부" in system
    # no-narration·격리 규칙도 system에 실린다
    assert "1인칭" in system
    assert "노출하지" in system or "노출" in system

    # 질문 본문은 user에, 페르소나는 user에 안 섞인다
    assert "환불 되나요?" in user
    assert "cs_lead" not in user
    assert "Notion/보상표" not in user


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

    def _boom(
        _prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> str:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    runtime = ClaudeCodeRuntime(runner=_boom)

    ans = runtime.answer("환불 되나요?", c)

    assert isinstance(ans, Answer)
    assert "cs_ops" in ans.text
    assert ans.sources == ("위키/환불정책",)
    assert ans.mode == "full"


def test_비정상_종료_예외면_폴백_Answer():
    c = card()

    def _boom(
        _prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> str:
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


# ── answer_stream: 스트리밍 오케스트레이션(가짜 스트리밍 러너 주입·결정론) ────────
#
# 실 `claude -p` subprocess 스트리밍은 게이트 밖(비결정·느림)이라 절대 호출하지 않는다.
# 여기서는 (1) ClaudeCodeRuntime이 StreamingRuntime을 만족하는지(isinstance), (2) 주입된
# 가짜 스트리밍 러너의 델타열이 AnswerChunk 시퀀스로 옳게 변환되는지, (3) answer와 같은
# 프롬프트·cwd 접지를 쓰는지, (4) 예외가 그대로 전파되는지(상위가 ErrorEvent로 투영)만
# 검증한다.


class _RecordingStreamRunner:
    """고정 델타열을 yield하며 마지막 프롬프트·cwd·system_prompt를 기록하는 가짜 스트리밍 러너."""

    def __init__(self, deltas: list[str]) -> None:
        self.deltas = deltas
        self.last_prompt: str | None = None
        self.last_cwd: str | None = None
        self.last_system: str | None = None

    def __call__(
        self, prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> Iterator[str]:
        self.last_prompt = prompt
        self.last_cwd = cwd
        self.last_system = system_prompt
        yield from self.deltas


def test_ClaudeCodeRuntime은_StreamingRuntime을_만족한다():
    # @runtime_checkable Protocol — web 엔드포인트가 isinstance로 스트리밍 능력 감지.
    runtime = ClaudeCodeRuntime(stream_runner=_RecordingStreamRunner(["a"]))
    assert isinstance(runtime, StreamingRuntime)


def test_기본_생성자도_StreamingRuntime을_만족한다():
    # 실 스트리밍 헬퍼가 기본값이라 호출하지 않아도 능력 감지는 통과한다.
    assert isinstance(ClaudeCodeRuntime(), StreamingRuntime)


def test_answer_stream이_델타열을_AnswerChunk로_변환한다():
    c = card(knowledge_sources=["위키/환불정책"])
    runner = _RecordingStreamRunner(["네, ", "7일 이내 ", "환불됩니다."])
    runtime = ClaudeCodeRuntime(stream_runner=runner)

    chunks = list(runtime.answer_stream("환불 되나요?", c))

    assert chunks == [
        AnswerChunk(text_delta="네, "),
        AnswerChunk(text_delta="7일 이내 "),
        AnswerChunk(text_delta="환불됩니다."),
    ]


def test_answer_stream은_answer와_같은_user_system_분리를_쓴다():
    # 노출 격리(본 작업): 스트리밍도 비스트리밍과 대칭으로 user 프롬프트·system_prompt를
    # 분리해 넘긴다 — user엔 질문, system엔 페르소나.
    c = card(knowledge_sources=["위키/환불정책"], can_answer=["환불 가능 여부"])
    runner = _RecordingStreamRunner(["응답"])
    runtime = ClaudeCodeRuntime(stream_runner=runner)

    list(runtime.answer_stream("환불 되나요?", c))

    assert runner.last_prompt == runtime.build_user("환불 되나요?", c)
    assert runner.last_system == runtime.build_system(c)
    # 페르소나는 system에, 질문은 user에 — 안 섞인다
    assert runner.last_system is not None and "cs_lead" in runner.last_system
    assert runner.last_prompt is not None and "환불 되나요?" in runner.last_prompt
    assert "cs_lead" not in runner.last_prompt


def test_answer_stream은_번들없으면_cwd_None으로_호출한다():
    # okf_root 미주입 → bundle_dir None → 임시 cwd(러너 기본값 None). 행위 불변 확인.
    c = card()
    runner = _RecordingStreamRunner(["답"])
    runtime = ClaudeCodeRuntime(stream_runner=runner)

    list(runtime.answer_stream("질문?", c))

    assert runner.last_cwd is None


def test_answer_stream_예외는_그대로_전파된다():
    # ADR 0031 결정 5: subprocess 실패·timeout은 폴백 Answer로 감싸지 않고 그대로 전파한다
    # (상위 /ask/stream이 잡아 ErrorEvent SSE 프레임으로 투영). answer의 중립 폴백과 대칭.
    c = card()

    def _boom(
        _prompt: str, *, cwd: str | None = None, system_prompt: str | None = None
    ) -> Iterator[str]:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)
        yield  # pragma: no cover — generator 표식

    runtime = ClaudeCodeRuntime(stream_runner=_boom)

    import pytest

    with pytest.raises(subprocess.TimeoutExpired):
        list(runtime.answer_stream("질문?", c))


def test_answer_stream_델타가_StreamedAnswer로_완성_Answer로_조립된다():
    # 디스패처(StreamedAnswer)가 델타를 합쳐 완성 Answer를 만든다 — 런타임은 델타만 흘린다.
    from agent_org_network.dispatch import LocalStreamingDispatcher

    c = card(knowledge_sources=["위키/환불정책"])
    runner = _RecordingStreamRunner(["네, ", "환불됩니다."])
    runtime = ClaudeCodeRuntime(stream_runner=runner)
    dispatcher = LocalStreamingDispatcher(runtime)

    stream = dispatcher.dispatch_stream("환불 되나요?", c)
    deltas = [chunk.text_delta for chunk in stream]
    completed = stream.completed

    assert deltas == ["네, ", "환불됩니다."]
    assert completed.text == "네, 환불됩니다."
    assert completed.sources == ("위키/환불정책",)
    assert completed.mode == "full"
