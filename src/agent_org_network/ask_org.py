import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, assert_never

from agent_org_network.audit import AuditEntry, AuditLog, Clock, default_clock
from agent_org_network.conflict import Candidate, ConflictCase, ConflictCaseStore
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.dispatch import (
    AwaitingWorker,
    Delivered,
    DispatchOutcome,
    EscalatedToManager,
    RuntimeDispatcher,
    WorkTicket,
)
from agent_org_network.router import Router
from agent_org_network.runtime import AnswerMode
from agent_org_network.user import User

if TYPE_CHECKING:
    from datetime import datetime
    from agent_org_network.manager_queue import ManagerQueueStore
    from agent_org_network.notify import Notifier
    from agent_org_network.review import BackupReview, BackupReviewItem, BackupReviewStore


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

    노출 불변식: `kind`+`message`(+ `dispatched`면 불투명 `tracking`)만 노출.
    manager_id·reason·ticket_id·owner·후보 목록 등 *조직 내부 구조*는 절대 싣지
    않는다(test_web `_LEAKY_KEYS`가 강제). `dispatched`는 분산 전송의 AwaitingWorker·
    EscalatedToManager를 함께 투영한 사용자 상태.

    `tracking`(ADR 0011 결정 6-5, 슬라이스2b): 답 *조회용 불투명 추적 토큰* 1개.
    워커가 나중에 회신한 답을 사용자가 받을 경로 — uuid4 hex라 owner_id·ticket_id·
    구조를 비추지 않는다(서버가 tracking→ticket 매핑을 따로 보관, 토큰 자체엔 내부값
    미인코딩). 노출 불변식의 *정밀화*이지 완화가 아니다(불투명 ID 1개 OK, 조직 구조 금지).
    `dispatched`에만 채워지고 contested/unowned는 None.
    """

    kind: PendingKind
    message: str
    tracking: str | None = None


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
        clock: Clock = default_clock,
        case_store: ConflictCaseStore | None = None,
        review_store: "BackupReviewStore | None" = None,
        manager_queue_store: "ManagerQueueStore | None" = None,
        manager_of: "Callable[[str], str | None] | None" = None,
        manager_root: str = "root",
        notifier: "Notifier | None" = None,
    ) -> None:
        # [Minor 2] manager_root 기본값 "root" 주의: 데모의 실제 root는 "root_manager".
        # manager_queue_store 를 주입할 때 manager_root 도 함께 주입하지 않으면
        # escalation 이 존재하지 않는 "root" 큐로 귀속돼 Manager 화면에 표시되지 않는다.
        # manager_queue_store 주입 시 manager_root 도 주입 권장(build_demo 참고).
        self._router = router
        self._dispatcher = dispatcher
        self._audit = audit_log
        self._clock = clock
        self._case_store = case_store
        # 백업 답 검토 저장소(ADR 0012 결정 7). retrieve 덧씌움이 ticket_id로 조회해
        # 검토 결과를 우선 투영한다. 미주입이면 None(하위호환 — 검토 루프 없이 동작).
        self._review_store = review_store
        # Manager 큐(T5.2 ADR 0014): 미해소 escalation 보관소. 미주입이면 하위호환.
        self._manager_queue_store = manager_queue_store
        # owner → manager 콜백(Registry.get_user(owner).manager). 미주입이면 None.
        self._manager_of: Callable[[str], str | None] | None = manager_of
        # root User id — manager가 없는 escalation의 최종 수신자(미아 없음).
        # 기본값 "root"는 하위호환용이며 실제 root id 와 다를 수 있다.
        # manager_queue_store 주입 시 manager_root 도 반드시 주입할 것.
        self._manager_root = manager_root
        # 실시간 push 통지(T7.4·ADR 0022 결정 4) — 처리함/큐 적재 직후 push 발화. 미주입이면
        # 기존 동작(하위호환·게이트 보존 — push는 pull을 *추가*하지 대체하지 않는다). MVP 슬라이스
        # 발화 지점은 ConflictCase open(Contested arm)·Manager escalation 적재(나머지 확장은 후속).
        self._notifier = notifier
        # 답 회수용 불투명 추적 토큰 → WorkTicket 매핑(ADR 0011 결정 6-5). 서버가
        # ticket을 보관하고 사용자엔 *불투명 토큰만* 준다 — 사용자向 응답에 ticket_id·
        # owner 등 내부 구조가 새지 않게 한다(노출 불변식). 토큰은 uuid4 hex(미인코딩).
        # MVP는 정리 없음(프로세스 수명 = 토큰 수명) — TTL/GC·다중 인스턴스 공유는 후속.
        self._tracking: dict[str, WorkTicket] = {}

    def _project_outcome(
        self,
        outcome: DispatchOutcome,
        primary_owner: str,
        primary_agent_id: str,
        tracking: str | None,
    ) -> OrgReply:
        """DispatchOutcome을 사용자向 OrgReply로 *투영*한다(표현 경계).

        Delivered→Answered(mode 보존, owner·agent_id 부착). AwaitingWorker·
        EscalatedToManager→Pending(kind="dispatched", 불투명 `tracking` 동반) — 사용자엔
        둘을 구분 않고 중립 안내만. manager_id/reason 등 기계값은 Pending에 싣지 않는다
        (노출 불변식). `tracking`은 답 회수용 불투명 토큰 1개로, 호출자가 만들어 넘긴다
        (handle은 새 토큰·매핑 저장, retrieve는 기존 토큰 유지). audit 기록은 별개다 —
        handle이 outcome *원형*을 AuditEntry로 넘긴다. match+assert_never로 망라.
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
                return Pending(
                    kind="dispatched",
                    message=_DISPATCHED_MESSAGE,
                    tracking=tracking,
                )
            case _ as never:
                assert_never(never)

    def _apply_approval_gate(self, reply: OrgReply, decision: Routed) -> OrgReply:
        """Approval 게이트를 사용자向 답에 강제 반영한다(T2.5, ADR 0012 mode 강제 패턴).

        `decision.requires_approval`이면 *라우팅 결정*이 답을 `mode="draft_only"`로 내린다 —
        워커 자기보고가 아니라 라우팅이 강제(디스패처가 backup을 강제 하향하는 정신).
        `dataclasses.replace`로 mode만 갈아끼운 새 Answered를 돌려준다(frozen 보존).

        적용 범위:
          - Answered(=Delivered 투영)에만 적용한다. Pending(dispatched, 아직 답 없음)엔 무관 —
            답 자체가 없으니 mode도 없다(답이 회신될 때 retrieve가 같은 게이트를 다시 적용).
          - mode 우선순위(ADR 0012): `backup`은 draft_only로 *덮지 않는다*(backup이 더 강한
            하향 — owner 미검토 답이라 승인 대기보다 약한 신뢰가 맞다). `full`→`draft_only`만
            격상. `draft_only`는 그대로(이미 게이트).
          - `requires_approval`이 False면 reply 그대로.

        collaborators는 여기서 다루지 않는다 — Answered에 싣지 않으므로(노출 불변식: 담당·
        승인·출처만, collaborator는 조직 내부 협업 구조). audit이 decision 원형으로 보관한다.

        주의(T2.5 경계): 이건 *게이트 표시*(draft_only로 보이기)까지다. 실 승인 행위(사람이
        draft를 승인해 full로 풀기)는 T5.2 Manager 큐 영역 — 여기선 승인 *상태만* 노출한다.
        """
        import dataclasses

        if not decision.requires_approval:
            return reply
        if not isinstance(reply, Answered):
            return reply
        if reply.mode == "backup":
            return reply
        return dataclasses.replace(reply, mode="draft_only")

    def retrieve(self, tracking: str) -> OrgReply | None:
        """불투명 추적 토큰으로 답을 *조회*한다(ADR 0011 결정 6-5, pull 한정).

        워커가 나중에 회신하면 사용자가 이 토큰으로 답을 가져온다 — 디스패처 `poll`을
        재노출한 것(새 도메인 아님). 보관한 ticket을 복원해 poll하고, Delivered면
        Answered로, 아직이면 Pending(dispatched, 같은 토큰 유지)로 투영한다. 모르는
        토큰이면 None(존재하지 않는 추적은 조회 실패). 푸시(SSE/WS)는 범위 밖.

        검토 store 덧씌움(ADR 0012 결정 7-3): poll 결과가 Delivered(backup 답)인 경우,
        그 ticket의 BackupReviewItem을 *먼저 조회*해 reviewed면 검토 결과를 우선 투영한다.
          - CorrectBackup: 정정 text + mode=full(owner 실답으로 신뢰 복원).
          - ApproveBackup: 큐의 원 text + mode=full(승인으로 신뢰 복원).
          - DismissBackup: 큐의 원 text + mode=backup 유지(정정 없음·mode 그대로).
          - pending_review(미검토): 큐의 poll 결과 그대로(mode=backup 유지).
        큐 도메인 무변경(덮어쓰지 않고 읽기만 덧씌움) — 큐 멱등·단조성 보존.

        EscalatedToManager 종착도 사용자엔 `dispatched` 유지가 *의도*다 — 워커 미연결과
        Manager escalation의 구분은 감출 내부값(노출 불변식, ADR 0011 결정 4). escalation의
        사용자 통지(무한 조회 대신)는 별도 후속(Manager 큐 T5.2 연계).

        Approval 게이트(T2.5)는 여기 retrieve(dispatched 회신) 경로에선 *아직* 강제하지
        않는다 — `_tracking`은 `tracking→WorkTicket`만 보관하고 `requires_approval`(라우팅
        결정)을 들지 않아서다. 즉답(Delivered) 경로의 Approval은 `handle`이 강제하고(데모는
        `LocalRuntimeDispatcher`라 PRD §7 시나리오 2가 즉답으로 시연됨), 분산 회신 경로의
        Approval 강제는 후속(tracking→requires_approval 보관 자리 추가가 선결).
        """
        ticket = self._tracking.get(tracking)
        if ticket is None:
            return None
        outcome = self._dispatcher.poll(ticket)
        # 검토 store 덧씌움: Delivered backup 답에 검토 결과를 우선 투영(큐 무변경).
        if isinstance(outcome, Delivered) and self._review_store is not None:
            reviewed = self._review_store.get_by_ticket(ticket.ticket_id)
            if reviewed is not None and reviewed.status == "reviewed":
                return self._project_review_outcome(reviewed, ticket, tracking)
        return self._project_outcome(outcome, ticket.owner_id, ticket.agent_id, tracking)

    def _project_review_outcome(
        self, reviewed_item: "BackupReviewItem", ticket: WorkTicket, tracking: str
    ) -> OrgReply:
        """BackupReviewItem(reviewed)을 사용자向 Answered로 투영한다(검토 덧씌움).

        CorrectBackup → 정정 text + mode=full(owner가 직접 발행한 실답 — 신뢰 복원).
        ApproveBackup → 큐의 원 backup text + mode=full(owner 검토 완료 — 신뢰 복원).
        DismissBackup → 큐의 원 backup text + mode=backup(정정 없음 — mode 유지).
        answered_by는 어느 경우든 owner(책임 불변, 정정도 owner가 발행).
        노출 불변식: 검토 내부 구조(item_id·review_service·store 존재)는 밖으로 새지 않음.
        match+assert_never로 BackupReview sealed sum을 망라 — _serialize_backup_review(web.py)와 대칭.
        reviewed인데 review 필드가 None인 경우는 구조적으로 불가하므로 명시 방어.
        """
        from typing import assert_never as _assert_never

        from agent_org_network.review import ApproveBackup, CorrectBackup, DismissBackup

        review = reviewed_item.review
        if review is None:
            # reviewed 상태인데 review 필드가 None — 구조적 불변식 위반(발생 불가).
            # 방어적으로 큐 poll 결과로 폴백한다.
            outcome = self._dispatcher.poll(ticket)
            return self._project_outcome(outcome, ticket.owner_id, ticket.agent_id, tracking)
        match review:
            case CorrectBackup():
                return Answered(
                    text=review.corrected_text,
                    answered_by=(ticket.owner_id, ticket.agent_id),
                    mode="full",
                    sources=review.sources,
                )
            case ApproveBackup():
                return Answered(
                    text=reviewed_item.backup_answer_text,
                    answered_by=(ticket.owner_id, ticket.agent_id),
                    mode="full",
                )
            case DismissBackup():
                return Answered(
                    text=reviewed_item.backup_answer_text,
                    answered_by=(ticket.owner_id, ticket.agent_id),
                    mode="backup",
                )
            case _ as never:
                _assert_never(never)

    def record_review(self, item_id: str, review: "BackupReview") -> None:
        """검토 전이를 적용한다(전이만 — 검토 기록은 BackupReviewStore.history가 담당).

        BackupReviewService를 통해 상태 전이만 수행한다. audit에는 *아무것도 남기지 않는다* —
        검토 기록은 BackupReviewStore.history(append-only 전이 보관소)가 책임지고, audit은
        질문→라우팅→디스패치→답의 절차 전용이다(전이≠기록, ADR 0012 결정 7).
        review_store 미주입이면 no-op(하위호환). 1인칭 검증은 BackupReviewService가 담당.
        """
        from agent_org_network.review import BackupReviewService

        if self._review_store is None:
            return
        svc = BackupReviewService(self._review_store)
        svc.review(item_id, review)

    def _push_manager_notification(self, manager_id: str, subject_ref: str, now: "datetime") -> None:
        """Manager 큐 적재 직후 manager에게 push 통지를 1회 쏜다(T7.4·ADR 0022 결정 4).

        `notifier` 미주입이면 *아무것도 안 한다*(하위호환·게이트 보존). 비None이면
        `Notification(kind="manager_escalated")` 1회 push — `_push_conflict_notification`과
        동형. `manager_id`가 빈 문자열이면 push 안 함(미귀속 가드 — 처리함 pull이 떠받침).
        """
        if self._notifier is None:
            return
        if not manager_id:
            return
        from agent_org_network.notify import Notification

        self._notifier.notify(
            Notification(
                recipient_id=manager_id,
                kind="manager_escalated",
                subject_ref=subject_ref,
                created_at=now,
            )
        )

    def _enqueue_unowned(self, decision: Unowned, question: str) -> None:
        """Unowned → Manager 큐 적재(T5.2). 미주입이면 no-op(하위호환)."""
        if self._manager_queue_store is None:
            return
        from agent_org_network.manager_queue import (
            FromUnowned,
            ManagerItem,
            manager_id_for_unowned,
        )

        mid = manager_id_for_unowned(decision)
        source = FromUnowned(decision=decision, question=question)
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=self._clock(),
        )
        self._manager_queue_store.enqueue(item)
        self._push_manager_notification(mid, item.item_id, item.created_at)

    def _enqueue_dispatch(self, outcome: EscalatedToManager) -> None:
        """EscalatedToManager → Manager 큐 적재(T5.2). 미주입이면 no-op."""
        if self._manager_queue_store is None:
            return
        from agent_org_network.manager_queue import (
            FromDispatch,
            ManagerItem,
            manager_id_for_dispatch,
        )

        mid = manager_id_for_dispatch(outcome, root=self._manager_root)
        source = FromDispatch(outcome=outcome)
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=self._clock(),
        )
        self._manager_queue_store.enqueue(item)
        self._push_manager_notification(mid, item.item_id, item.created_at)

    def enqueue_deadlock(self, case: ConflictCase, reason: str = "") -> None:
        """Deadlocked → Manager 큐 적재(T5.2). web/concur 엔드포인트가 호출한다.

        미주입이면 no-op(하위호환). manager_of 콜백으로 첫 후보 owner의 manager를 찾는다.
        같은 case_id 로 이미 큐에 있으면 재적재하지 않는다(중복 방지 — [Major 1]).
        """
        if self._manager_queue_store is None:
            return
        from agent_org_network.manager_queue import (
            FromDeadlock,
            ManagerItem,
            manager_id_for_deadlock,
        )

        # [Major 1] 중복 방지: 같은 ConflictCase 가 이미 큐에 있으면 no-op.
        # open_for_intent 로 Contested 중복을 막는 것과 대칭.
        if self._manager_queue_store.get_by_case(case.case_id) is not None:
            return

        def _no_manager(uid: str) -> str | None:
            return None

        manager_of_fn = self._manager_of if self._manager_of is not None else _no_manager
        mid = manager_id_for_deadlock(case, manager_of=manager_of_fn, root=self._manager_root)
        source = FromDeadlock(case=case, reason=reason)
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=self._clock(),
        )
        self._manager_queue_store.enqueue(item)
        self._push_manager_notification(mid, item.item_id, item.created_at)

    def handle(self, question: str, user: User) -> OrgReply:
        decision = self._router.route(question)

        # 디스패치 절차의 결말 — Routed일 때만 채워지고(dispatch→poll), Contested/
        # Unowned는 디스패치를 안 하므로 None. audit이 이 원형에서 answer를 파생하고
        # escalation 대상(manager_id·reason)까지 기록한다(2b 선결, ADR 0011).
        outcome: DispatchOutcome | None = None
        match decision:
            case Routed():
                ticket = self._dispatcher.dispatch(question, decision.primary)
                outcome = self._dispatcher.poll(ticket)
                # 미회신(AwaitingWorker/EscalatedToManager)이면 답 회수용 불투명 토큰을
                # 발급해 ticket을 서버에 보관한다 — 사용자가 나중에 retrieve로 답을 가져올
                # 길(6-5). Delivered(즉답)면 토큰 불요(None). 토큰은 ticket_id와 *분리된*
                # 별도 uuid4 hex라 사용자向에 ticket_id조차 노출되지 않는다(노출 불변식).
                tracking: str | None = None
                if not isinstance(outcome, Delivered):
                    tracking = uuid.uuid4().hex
                    self._tracking[tracking] = ticket
                reply = self._project_outcome(
                    outcome,
                    decision.primary.owner,
                    decision.primary.agent_id,
                    tracking,
                )
                # Approval 게이트 강제(T2.5, ADR 0012 mode 강제 패턴): Routed에 Approval이
                # 붙었으면 *라우팅 결정*이 답을 draft_only로 내린다 — 워커 자기보고가 아니라
                # 라우팅이 강제한다(디스패처가 backup을 강제 하향하듯). Delivered(실 답이
                # 도착)에만 적용하고, 아직 답이 없는 Pending(dispatched)엔 무관(답 자체가
                # 없으니 mode도 없음). mode 우선순위(ADR 0012): backup이 더 강한 하향이라
                # draft_only로 *덮지 않는다* — backup 답은 owner 미검토라 승인 대기보다 약한
                # 신뢰가 맞다. full→draft_only만 격상(게이트 표시), backup은 보존.
                reply = self._apply_approval_gate(reply, decision)
                # EscalatedToManager면 Manager 큐에도 적재(T5.2 ADR 0014).
                if isinstance(outcome, EscalatedToManager):
                    self._enqueue_dispatch(outcome)
            case Contested():
                reply = Pending(
                    kind="contested",
                    message="담당을 확인하고 있어요. 정해지면 답변드릴게요.",
                )
                if self._case_store is not None and self._case_store.open_for_intent(decision.intent) is None:
                    case = ConflictCase(
                        intent=decision.intent,
                        question=question,
                        candidates=tuple(
                            Candidate(agent_id=c.agent_id, owner=c.owner)
                            for c in decision.candidates
                        ),
                        opened_at=self._clock(),
                    )
                    self._case_store.open_case(case)
                    # 실시간 push 발화(T7.4·ADR 0022 결정 4) — 다툼 적재 직후 후보 owner들에게
                    # push 통지를 쏜다(처리함 pull 전에도 알리게). notifier 미주입이면 실행 경로
                    # 밖(게이트 보존). 본동작은 tdd-engineer red→green(슬라이스 3).
                    self._push_conflict_notification(case)
            case Unowned():
                reply = Pending(
                    kind="unowned",
                    message="아직 담당이 없어 매니저에게 전달했어요. 답변되면 알림드릴게요.",
                )
                self._enqueue_unowned(decision, question)

        self._audit.record(
            AuditEntry(
                timestamp=self._clock(),
                user_id=user.id,
                question=question,
                intent=decision.intent,
                decision=decision,
                dispatch_outcome=outcome,
            )
        )
        return reply

    def _push_conflict_notification(self, case: ConflictCase) -> None:
        """ConflictCase open 직후 후보 owner들에게 push 통지를 쏜다(T7.4·ADR 0022 결정 4).

        `notifier` 미주입이면 *아무것도 안 한다*(하위호환·게이트 보존 — 기존 호출은 notifier를
        주입하지 않아 이 본문이 실행되지 않는다). 비None이면 각 후보 owner에게
        `Notification(kind="conflict_opened", subject_ref=case.case_id)`를 `notifier.notify`로
        push한다 — 후보 owner가 처리함 pull로 알기 전에 push로도 알린다(push는 pull을 *추가*하지
        대체하지 않는다·미아 없음은 pull이 떠받친다·결정 6). 멱등은 Notifier가 담당
        (`(recipient, kind, subject_ref)` 중복 발송 차단 — 같은 case에 두 번째 질문이 와도 한 번).

        **현재는 발화 자리만(미구현 통과 stub)** — 실 Notification 구성·notify 호출 본문은
        tdd-engineer가 red→green으로 채운다(슬라이스 3). `commit_okf_bundle(..., propagator=None)`
        의 옵셔널 발화와 동형.
        """
        if self._notifier is None:
            return
        from agent_org_network.notify import Notification

        now = self._clock()
        candidate_owners = dict.fromkeys(c.owner for c in case.candidates)
        for owner_id in candidate_owners:
            self._notifier.notify(
                Notification(
                    recipient_id=owner_id,
                    kind="conflict_opened",
                    subject_ref=case.case_id,
                    created_at=now,
                )
            )
