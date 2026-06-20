from dataclasses import dataclass, field
from typing import Literal, Protocol

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
