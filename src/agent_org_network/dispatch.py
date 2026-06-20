"""분산 전송 — owner 워커의 역방향 아웃바운드 연결 + 중앙 작업 큐 (T6.3, ADR 0011).

owner PC는 서버를 노출하지 않는다(NAT/방화벽 뒤·고정 IP 없음·상시 가동 X). 대신
owner PC의 작은 워커가 중앙에 *아웃바운드*로 연결해 작업을 가져가고, 로컬 claude
(ADR 0010의 `ClaudeCodeRuntime` 재사용)로 답을 만들어 중앙에 회신한다. 중앙은 질문을
owner별 작업 큐에 적재하고 회신을 비동기로 수집한다.

이 모듈은 **타입 shape(포트 + 값 객체 + InMemory stub)만** 둔다 — 실제 전송·워커·
블로킹 대기·escalation 처리는 후속 엔지니어가 구현한다(T6.3 슬라이스들). 기존 동기
포트 `AgentRuntime.answer`(runtime.py·ask_org.py가 의존)는 건드리지 않고, 그 위에
얹을 어댑터 `DispatchingRuntime`의 시그니처만 여기 stub으로 예고한다.

포트 패턴은 `AuditLog`·`PrecedentStore`·`ConflictCaseStore`와 동일 — Protocol +
InMemory 구현(ADR 0008). 전이 ≠ 기록 — 작업 큐는 미해소 작업의 도메인 보관소지
절차 로그(AuditLog)가 아니다.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, Protocol, assert_never

if TYPE_CHECKING:
    # 어댑터(DispatchingRuntime) 시그니처 예고용. 런타임 import 순환을 피하려 타입 체크
    # 시에만 끌어온다 — 이 모듈은 runtime.py에 의존하지 않는다(어댑터 구현은 후속).
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.runtime import Answer


Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_ticket_id() -> str:
    return uuid.uuid4().hex


# ── 작업 추적표: WorkTicket ──────────────────────────────────────────────
#
# 중앙이 질문을 owner 큐에 넣을 때 즉시 돌려받는 추적표. 답이 아니라 "이 작업을
# 추적하는 손잡이"다(비동기 — 답은 즉시 보장되지 않는다). owner_id 귀속을 실어
# 신원/책임 연결점(ADR 0009 인증)을 예고한다 — 회신이 진짜 그 owner에게서
# 왔는지 검증할 자리. card 본문이 아니라 식별자(agent_id·owner)만 들고, 큐 적재
# 시각은 주입 clock으로 결정론(timeout 판정의 기준). question 원문은 워커가 로컬
# claude에 넘길 프롬프트 입력이라 보관한다.


@dataclass(frozen=True)
class WorkTicket:
    """owner 작업 큐에 적재된 한 작업의 추적표(답 아님 — 비동기 손잡이).

    `owner_id`로 어느 owner 큐의 작업인지, `agent_id`로 어느 카드(담당 영역)의
    답인지 식별한다. `poll`이 이 추적표로 회신·대기·timeout escalation을 조회한다.
    """

    owner_id: str
    agent_id: str
    question: str
    enqueued_at: datetime
    ticket_id: str = field(default_factory=_new_ticket_id)


# ── 작업 큐 상태: WorkStatus ─────────────────────────────────────────────
#
# 큐에 적재된 작업의 수명. "타입이 곧 상태"(RoutingDecision·ConsensusOutcome
# 정신)를 따르되, queued↔claimed↔answered는 같은 작업의 *수명*이라 별 타입(sum)이
# 아니라 Literal 라벨로 둔다(ConflictCase.status와 같은 판단). 단 최종 처분
# (회신/escalation)은 별 타입 sum(DispatchOutcome)으로 갈라 망라성을 강제한다.


WorkStatus = Literal["queued", "claimed", "answered", "expired"]
#   - queued:   큐에 들어가 워커가 아직 가져가지 않음(owner 부재 시 여기 대기)
#   - claimed:  워커가 가져가 로컬 claude로 답 생성 중
#   - answered: 워커가 회신 완료(Answer 도착)
#   - expired:  timeout — owner 부재/회신 지연 → escalation 대상


# ── 디스패치 결과: DispatchOutcome ───────────────────────────────────────
#
# `poll(ticket)`이 돌려주는 결말. sealed sum(match 망라성 강제) — RoutingDecision·
# ConsensusOutcome과 같은 "타입이 곧 상태". 세 갈래:
#   - Delivered:        워커가 회신함 → Answer 도착(mode 보존 = Approval 게이트 합류 자리).
#   - AwaitingWorker:   아직 회신 없음(워커 미연결·생성 중) → 큐에 대기, 계속 poll.
#   - EscalatedToManager: timeout/owner 부재 → 미아·합의 실패와 같은 처분(Escalation).
#                         실제 Manager 큐 연결은 T5.2에 위임(자리만).


@dataclass(frozen=True)
class Delivered:
    """워커가 owner 환경(로컬 claude)에서 답을 만들어 회신함.

    `answer`는 runtime.py의 `Answer`(text·sources·mode). mode를 보존해 `draft_only`
    (Approval 게이트가 걸린 owner 답)면 사람 승인 전까지 초안임을 합류시킬 자리를
    남긴다 — Approval 평가 자체는 T2.5/Routed 영역(여긴 운반만).
    """

    ticket: WorkTicket
    answer: "Answer"


@dataclass(frozen=True)
class AwaitingWorker:
    """아직 회신이 없다 — 워커 미연결(owner PC 꺼짐)이거나 답 생성 중.

    `waited` 는 적재 후 경과 시간(주입 clock 기준). 호출자는 계속 poll하거나,
    정책상 한계를 넘으면 디스패처가 다음 poll에서 EscalatedToManager로 전이시킨다.
    """

    ticket: WorkTicket
    waited: timedelta


@dataclass(frozen=True)
class EscalatedToManager:
    """timeout/owner 부재 → Escalation(미아·합의 실패와 같은 종착 처분).

    `reason`은 T5.2 Manager 큐로 넘길 때 근거(ConsensusOutcome.Deadlocked.reason과
    같은 역할). 실제 Manager 큐 적재·사람 그래프 상향은 T5.2에 위임 — 여긴 처분
    상태만 남긴다(ADR 0008의 Deadlocked 정합).
    """

    ticket: WorkTicket
    reason: str = ""


DispatchOutcome = Delivered | AwaitingWorker | EscalatedToManager


# ── 디스패처 포트: RuntimeDispatcher ─────────────────────────────────────
#
# 중앙이 owner별로 작업을 라우팅하고 답을 수집하는 추상화. AgentRuntime(동기
# answer)을 폐기하지 않고 그 위에 얹는 비동기 하부 — dispatch로 큐에 넣고 즉시
# 추적표를 받고, poll로 회신·대기·escalation을 조회한다. 워커 측(claim/submit)은
# owner 워커가 중앙에 아웃바운드 연결해 호출하는 면이다.


class RuntimeDispatcher(Protocol):
    """질문을 owner 작업 큐에 적재하고 비동기로 답을 수집하는 포트.

    중앙 측(질문 측):
      - dispatch(question, card) -> WorkTicket : owner 큐에 적재, 추적표 즉시 반환.
      - poll(ticket) -> DispatchOutcome        : 회신·대기·escalation 조회.
    워커 측(owner PC 워커가 아웃바운드로 호출):
      - claim(owner_id) -> WorkTicket | None   : 그 owner 큐의 다음 작업을 가져감.
      - submit(ticket_id, answer) -> None       : 로컬 claude가 만든 답을 회신.

    포트 패턴은 ConflictCaseStore·PrecedentStore와 동일(Protocol + InMemory).
    """

    def dispatch(self, question: str, card: "AgentCard") -> WorkTicket: ...

    def poll(self, ticket: WorkTicket) -> DispatchOutcome: ...

    def claim(self, owner_id: str) -> WorkTicket | None: ...

    def submit(self, ticket_id: str, answer: "Answer") -> None: ...


class InMemoryWorkQueueDispatcher:
    """in-process 작업 큐 디스패처 stub — 결정론 테스트·walking skeleton 첫 슬라이스용.

    실제 네트워크 전송·워커 프로세스·연결 수명 없이 *큐 적재→회수→회신→escalation
    분기*의 구조만 보인다(다른 PC 도달·연결 유지는 네트워크 슬라이스로 분리, ADR 0011).
    owner별 FIFO 큐(`_queues`)에 작업을 쌓고, 워커가 claim으로 빼 submit으로 회신을
    채운다. timeout은 주입 clock + 정책으로 판정(결정론).

    구현은 후속 엔지니어 몫 — 여기선 시그니처와 상태 그릇만 둔다(stub).
    """

    def __init__(
        self,
        clock: Clock = default_clock,
        timeout: timedelta = timedelta(seconds=120),
        manager_of: Callable[[str], str] | None = None,
    ) -> None:
        self._clock = clock
        self._timeout = timeout
        # owner_id -> 그 owner의 Manager User.id (timeout escalation 대상). 사람
        # 그래프 상향은 본디 Registry/Manager 영역 — 디스패처는 주입으로 받아 결합 회피.
        self._manager_of = manager_of
        self._queues: dict[str, list[WorkTicket]] = {}
        self._claimed: set[str] = set()  # claimed ticket_id 집합
        self._answers: dict[str, "Answer"] = {}
        self._status: dict[str, WorkStatus] = {}  # ticket_id → WorkStatus
        # append-only 정신의 이력(전이 ≠ 기록 — audit과 별개의 도메인 상태 보관).
        self.history: list[WorkTicket] = []

    def dispatch(self, question: str, card: "AgentCard") -> WorkTicket:
        ticket = WorkTicket(
            owner_id=card.owner,
            agent_id=card.agent_id,
            question=question,
            enqueued_at=self._clock(),
        )
        self._queues.setdefault(card.owner, []).append(ticket)
        self._status[ticket.ticket_id] = "queued"
        self.history.append(ticket)
        return ticket

    def poll(self, ticket: WorkTicket) -> DispatchOutcome:
        status = self._status.get(ticket.ticket_id, "queued")

        # 이미 종착한 경우 멱등 반환 — 단조성 보장
        if status == "expired":
            return self._make_escalated(ticket)
        if status == "answered":
            return Delivered(ticket=ticket, answer=self._answers[ticket.ticket_id])

        waited = self._clock() - ticket.enqueued_at
        if waited > self._timeout:
            self._status[ticket.ticket_id] = "expired"
            return self._make_escalated(ticket, waited)
        return AwaitingWorker(ticket=ticket, waited=waited)

    def _make_escalated(
        self, ticket: WorkTicket, waited: timedelta | None = None
    ) -> EscalatedToManager:
        manager_part = ""
        if self._manager_of is not None:
            manager = self._manager_of(ticket.owner_id)
            manager_part = f", 담당 매니저: {manager}"
        waited_sec = f"{waited.total_seconds():.0f}" if waited is not None else "?"
        reason = (
            f"owner '{ticket.owner_id}' 미응답 — "
            f"대기 {waited_sec}초 초과(한계 {self._timeout.total_seconds():.0f}초)"
            f"{manager_part}"
        )
        return EscalatedToManager(ticket=ticket, reason=reason)

    def claim(self, owner_id: str) -> WorkTicket | None:
        queue = self._queues.get(owner_id, [])
        for ticket in queue:
            tid = ticket.ticket_id
            status = self._status.get(tid, "queued")
            if status == "queued":
                self._claimed.add(tid)
                self._status[tid] = "claimed"
                return ticket
        return None

    def submit(self, ticket_id: str, answer: "Answer") -> None:
        status = self._status.get(ticket_id, "queued")
        # expired(escalated) 작업에 늦은 submit은 무시 — 단조성 보장
        if status == "expired":
            return
        self._answers[ticket_id] = answer
        self._status[ticket_id] = "answered"


# ── 동기 어댑터 예고: DispatchingRuntime ─────────────────────────────────
#
# 기존 동기 포트 AgentRuntime.answer(question, card) -> Answer 를 보존하는 다리.
# ask_org·router는 이 동기 계약에 묶여 있어(ADR 0007·0010 "포트는 안 바뀐다"),
# 분산을 끼우되 진입점을 흔들지 않으려 디스패처 위에 얇게 얹는다 — 내부에서
# dispatch 후 회신이 올 때까지 블로킹 poll, timeout이면 escalation을 Answer로
# 표면화(또는 도메인 신호로). 이는 walking skeleton 단계의 다리일 뿐 — 진짜 비동기
# (ask_org가 Pending(kind="dispatched")류로 즉시 회신)는 후속 결정(ADR 0011 Consequences).
#
# 여기선 시그니처만 예고한다. 실제 구현은 후속 엔지니어가 runtime.py 통합 시점에
# 작성(이 모듈은 runtime.py를 import하지 않는다 — 어댑터 본체는 통합 PR에서).


class DispatchingRuntime:
    """RuntimeDispatcher 위에 AgentRuntime(동기 answer)을 얹는 어댑터.

    `worker`를 주입해 동기 시뮬 — worker가 claim→로컬 런타임으로 답→submit을 동기
    수행한 뒤 poll로 결과를 수집한다. sleep/스레드 없이 결정론을 보장(단위테스트용).
    실제 분산에선 worker가 원격·비동기로 돌지만 포트(동기 answer) 계약은 유지된다.

    EscalatedToManager(timeout/owner 부재) → 폴백 Answer로 표면화해 호출자를 보호한다.
    """

    def __init__(
        self,
        dispatcher: RuntimeDispatcher,
        worker: Callable[[RuntimeDispatcher], None] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._worker = worker

    def answer(self, question: str, card: "AgentCard") -> "Answer":
        from agent_org_network.runtime import Answer as _Answer

        ticket = self._dispatcher.dispatch(question, card)

        if self._worker is not None:
            self._worker(self._dispatcher)

        outcome = self._dispatcher.poll(ticket)

        match outcome:
            case Delivered():
                return outcome.answer
            case EscalatedToManager():
                return _Answer(
                    text=f"[escalated] {outcome.reason}",
                    sources=(),
                    mode="full",
                )
            case AwaitingWorker():
                return _Answer(
                    text=f"[awaiting] owner '{ticket.owner_id}' 워커 응답 대기 중",
                    sources=(),
                    mode="full",
                )
            case _ as never:
                assert_never(never)
