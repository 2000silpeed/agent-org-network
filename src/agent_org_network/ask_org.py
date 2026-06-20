from dataclasses import dataclass, field
from typing import Literal

from agent_org_network.audit import AuditEntry, AuditLog, Clock, default_clock
from agent_org_network.classifier import Classifier
from agent_org_network.conflict import Candidate, ConflictCase, ConflictCaseStore
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.router import Router
from agent_org_network.runtime import AgentRuntime, Answer, AnswerMode
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
    def __init__(
        self,
        router: Router,
        runtime: AgentRuntime,
        audit_log: AuditLog,
        classifier: Classifier,
        clock: Clock = default_clock,
        case_store: ConflictCaseStore | None = None,
    ) -> None:
        self._router = router
        self._runtime = runtime
        self._audit = audit_log
        self._classifier = classifier
        self._clock = clock
        self._case_store = case_store

    def handle(self, question: str, user: User) -> OrgReply:
        intent = self._classifier.classify(question)
        decision = self._router.route(question)

        answer: Answer | None = None
        match decision:
            case Routed():
                answer = self._runtime.answer(question, decision.primary)
                reply: OrgReply = Answered(
                    text=answer.text,
                    answered_by=(decision.primary.owner, decision.primary.agent_id),
                    mode=answer.mode,
                    sources=answer.sources,
                )
            case Contested():
                reply = Pending(
                    kind="contested",
                    message="담당을 확인하고 있어요. 정해지면 답변드릴게요.",
                )
                if self._case_store is not None and self._case_store.open_for_intent(intent) is None:
                    case = ConflictCase(
                        intent=intent,
                        question=question,
                        candidates=tuple(
                            Candidate(agent_id=c.agent_id, owner=c.owner)
                            for c in decision.candidates
                        ),
                        opened_at=self._clock(),
                    )
                    self._case_store.open_case(case)
            case Unowned():
                reply = Pending(
                    kind="unowned",
                    message="아직 담당이 없어 매니저에게 전달했어요. 답변되면 알림드릴게요.",
                )

        self._audit.record(
            AuditEntry(
                timestamp=self._clock(),
                user_id=user.id,
                question=question,
                intent=intent,
                decision=decision,
                answer=answer,
            )
        )
        return reply
