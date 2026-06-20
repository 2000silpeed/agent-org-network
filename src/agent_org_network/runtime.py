import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Literal, Protocol

from agent_org_network.agent_card import AgentCard

AnswerMode = Literal["draft_only", "full"]


@dataclass(frozen=True)
class Answer:
    text: str
    sources: tuple[str, ...] = field(default_factory=tuple)
    mode: AnswerMode = "full"


class AgentRuntime(Protocol):
    def answer(self, question: str, card: AgentCard) -> Answer: ...


class StubRuntime:
    def answer(self, question: str, card: AgentCard) -> Answer:
        return Answer(
            text=f"[{card.agent_id}] {card.summary}",
            sources=tuple(card.knowledge_sources),
            mode="full",
        )


# `claude -p` 헤드리스 호출 기본값. 응답 외 잡음(상태/로그)이 stdout을 오염하거나
# 프로젝트 CLAUDE.md·MCP가 끼어드는 걸 막으려 임시 디렉터리(cwd)에서 1회성으로 돈다.
DEFAULT_CLAUDE_TIMEOUT_SECONDS = 120


def _run_claude_headless(prompt: str) -> str:
    """`claude -p`를 임시 cwd에서 한 번 돌려 text 응답(stdout)을 돌려준다."""
    with tempfile.TemporaryDirectory() as workdir:
        completed = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_CLAUDE_TIMEOUT_SECONDS,
            cwd=workdir,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"claude -p exited with {completed.returncode}: {completed.stderr.strip()}"
        )
    return completed.stdout


def _build_persona_prompt(question: str, card: AgentCard) -> str:
    """카드로 '담당자 페르소나'를 구성해 그 사람으로서 답하게 하는 프롬프트.

    knowledge_sources는 지금 출처 *레이블*뿐(진짜 문서 RAG 아님)이라 근거 맥락으로만 녹인다.
    """
    lines: list[str] = [
        f"당신은 '{card.team}' 팀의 담당자 {card.owner}(담당 영역 ID: {card.agent_id})입니다.",
        f"역할 요약: {card.summary}",
    ]
    if card.domains:
        lines.append(f"담당 도메인: {', '.join(card.domains)}")
    if card.can_answer:
        lines.append(f"답할 수 있는 것: {', '.join(card.can_answer)}")
    if card.knowledge_sources:
        lines.append(f"근거로 삼을 출처(레이블): {', '.join(card.knowledge_sources)}")
    lines.append("")
    lines.append(
        "위 담당자로서, 회사 동료의 다음 질문에 한국어로 간결하고 실무적으로 답하세요. "
        "모르면 추측하지 말고 모른다고 하세요. 메타 설명 없이 답변 본문만 출력하세요."
    )
    lines.append("")
    lines.append(f"질문: {question}")
    return "\n".join(lines)


class ClaudeCodeRuntime:
    """헤드리스 `claude -p` subprocess로 담당자 답을 실제 생성하는 AgentRuntime 포트.

    T6.1 임시 구현 — 중앙 claude 1회성 호출. (T6.3에서 각 owner PC 분산으로 대체 예정.)
    실제 호출은 비결정·느리므로 `runner`를 주입 가능하게 둬 단위테스트는 FakeRunner로 고정한다.
    """

    def __init__(self, runner: Callable[[str], str] = _run_claude_headless) -> None:
        self._runner = runner

    def build_prompt(self, question: str, card: AgentCard) -> str:
        return _build_persona_prompt(question, card)

    def answer(self, question: str, card: AgentCard) -> Answer:
        prompt = self.build_prompt(question, card)
        sources = tuple(card.knowledge_sources)
        try:
            raw = self._runner(prompt)
        except subprocess.TimeoutExpired:
            return Answer(
                text=f"[{card.agent_id}] 담당자 응답 생성이 시간 내에 끝나지 않았습니다.",
                sources=sources,
                mode="full",
            )
        except Exception as exc:
            return Answer(
                text=f"[{card.agent_id}] 담당자 응답 생성에 실패했습니다: {exc}",
                sources=sources,
                mode="full",
            )
        text = raw.strip()
        if not text:
            return Answer(
                text=f"[{card.agent_id}] 담당자 응답이 비어 있습니다.",
                sources=sources,
                mode="full",
            )
        return Answer(text=text, sources=sources, mode="full")
