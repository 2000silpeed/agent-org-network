from dataclasses import dataclass, field
from typing import Literal, assert_never

from agent_org_network.audit import AuditEntry, AuditLog, Clock, default_clock
from agent_org_network.classifier import Classifier
from agent_org_network.conflict import Candidate, ConflictCase, ConflictCaseStore
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    DispatchOutcome,
    EscalatedToManager,
    RuntimeDispatcher,
)
from agent_org_network.router import Router
from agent_org_network.runtime import AnswerMode
from agent_org_network.user import User


@dataclass(frozen=True)
class Answered:
    text: str
    answered_by: tuple[str, str]
    mode: AnswerMode
    sources: tuple[str, ...] = field(default_factory=tuple)


# Pending kind 세 갈래로 확장(T6.3 슬라이스2). `dispatched`가 분산 전송의 비동기
# 결말을 사용자向으로 흡수한다 — DispatchOutcome의 `AwaitingWorker`(미회신)와
# `EscalatedToManager`(timeout/owner 부재)를 *둘 다* 하나로 모은다. 근거: 사용자
# 관점에선 "담당에게 보냈는데 아직 답이 없다(사람 손으로 넘어가는 중)"로 동일하고,
# 워커 미연결인지 Manager escalation인지는 *감춰야 할 내부값*이다(노출 불변식). kind를
# 둘로 쪼개면(dispatched/escalated) 그 내부 구분이 사용자에게 새어나간다. 그래서 최소화.
PendingKind = Literal["contested", "unowned", "dispatched"]


@dataclass(frozen=True)
class Pending:
    """담당이 사람 손에 있어 즉답이 없는 상태의 사용자向 투영.

    노출 불변식: `kind`+`message`만 노출. manager_id·reason·ticket_id·후보 목록 등
    기계/내부값은 절대 싣지 않는다(test_web `_LEAKY_KEYS`가 강제). `dispatched`는
    분산 전송의 AwaitingWorker·EscalatedToManager를 함께 투영한 사용자 상태.
    """

    kind: PendingKind
    message: str


OrgReply = Answered | Pending


# 분산 전송 결말 → 사용자向 Pending 안내 문구. 둘 다 같은 `dispatched`로 모이되,
# 문구도 내부(워커 미연결 vs escalation)를 비추지 않는 중립 안내로 통일한다.
_DISPATCHED_MESSAGE = "담당에게 질문을 전달했어요. 답변이 준비되면 알림드릴게요."


class AskOrg:
    """사용자 질문을 라우팅하고 담당 답을 *디스패처 경유*로 수집해 OrgReply로 투영한다.

    답 획득 경로(T6.3 슬라이스2): 동기 `AgentRuntime.answer` 직접 호출이 아니라
    `RuntimeDispatcher.dispatch→poll`을 본다. poll 결과 `DispatchOutcome`을 `OrgReply`로
    매핑한다 — Delivered→Answered(mode 보존), AwaitingWorker·EscalatedToManager→
    Pending(kind="dispatched"). escalation/미회신을 동기 Answer로 *위장하지 않는다*
    (ADR 0011 ②). in-process 데모/테스트는 `LocalRuntimeDispatcher`(즉시 Delivered)를
    주입해 Routed→Answered가 한 호출에 끝난다.
    """

    def __init__(
        self,
        router: Router,
        dispatcher: RuntimeDispatcher,
        audit_log: AuditLog,
        classifier: Classifier,
        clock: Clock = default_clock,
        case_store: ConflictCaseStore | None = None,
    ) -> None:
        self._router = router
        self._dispatcher = dispatcher
        self._audit = audit_log
        self._classifier = classifier
        self._clock = clock
        self._case_store = case_store

    def _project_outcome(
        self, outcome: DispatchOutcome, primary_owner: str, primary_agent_id: str
    ) -> OrgReply:
        """DispatchOutcome을 사용자向 OrgReply로 *투영*한다(표현 경계).

        Delivered→Answered(mode 보존, owner·agent_id 부착). AwaitingWorker·
        EscalatedToManager→Pending(kind="dispatched") — 사용자엔 둘을 구분 않고 중립
        안내만. manager_id/reason 등 기계값은 Pending에 싣지 않는다(노출 불변식).
        audit 기록은 별개다 — handle이 outcome *원형*을 AuditEntry로 넘긴다(여기선
        떨궈도 audit엔 manager_id·reason이 남는다). match+assert_never로 망라.
        """
        match outcome:
            case Delivered():
                return Answered(
                    text=outcome.answer.text,
                    answered_by=(primary_owner, primary_agent_id),
                    mode=outcome.answer.mode,
                    sources=outcome.answer.sources,
                )
            case AwaitingWorker() | EscalatedToManager():
                return Pending(kind="dispatched", message=_DISPATCHED_MESSAGE)
            case _ as never:
                assert_never(never)

    def handle(self, question: str, user: User) -> OrgReply:
        intent = self._classifier.classify(question)
        decision = self._router.route(question)

        # 디스패치 절차의 결말 — Routed일 때만 채워지고(dispatch→poll), Contested/
        # Unowned는 디스패치를 안 하므로 None. audit이 이 원형에서 answer를 파생하고
        # escalation 대상(manager_id·reason)까지 기록한다(2b 선결, ADR 0011).
        outcome: DispatchOutcome | None = None
        match decision:
            case Routed():
                ticket = self._dispatcher.dispatch(question, decision.primary)
                outcome = self._dispatcher.poll(ticket)
                reply = self._project_outcome(
                    outcome,
                    decision.primary.owner,
                    decision.primary.agent_id,
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
                dispatch_outcome=outcome,
            )
        )
        return reply
