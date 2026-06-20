from dataclasses import dataclass, field
from typing import Literal

from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.router import Router
from agent_org_network.runtime import AgentRuntime, AnswerMode
from agent_org_network.user import User


@dataclass(frozen=True)
class Answered:
    text: str
    answered_by: tuple[str, str]
    mode: AnswerMode
    sources: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Pending:
    kind: Literal["contested", "unowned"]
    message: str


OrgReply = Answered | Pending


class AskOrg:
    def __init__(self, router: Router, runtime: AgentRuntime) -> None:
        self._router = router
        self._runtime = runtime

    def handle(self, question: str, user: User) -> OrgReply:
        decision = self._router.route(question)
        match decision:
            case Routed():
                answer = self._runtime.answer(question, decision.primary)
                return Answered(
                    text=answer.text,
                    answered_by=(decision.primary.owner, decision.primary.agent_id),
                    mode=answer.mode,
                    sources=answer.sources,
                )
            case Contested():
                return Pending(
                    kind="contested",
                    message="담당이 여럿이라 합의 중이에요. 정해지면 답변드릴게요.",
                )
            case Unowned():
                return Pending(
                    kind="unowned",
                    message="아직 담당이 없어 매니저에게 전달했어요. 답변되면 알림드릴게요.",
                )
