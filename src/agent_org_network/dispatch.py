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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Literal, Protocol, assert_never

if TYPE_CHECKING:
    # 어댑터(DispatchingRuntime·LocalRuntimeDispatcher) 시그니처 예고용. 런타임 import
    # 순환을 피하려 타입 체크 시에만 끌어온다 — 이 모듈은 runtime.py에 의존하지 않는다
    # (어댑터의 Answer 생성은 함수 안 지역 import, AgentRuntime은 타입 주석만).
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.runtime import AgentRuntime, Answer, AnswerChunk


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
    # 그 사용자의 발화 스레드(멀티턴 맥락, ADR 0027 결정 13·T9.7 S1) — 분산 WS 경로에서
    # owner 워커의 런타임까지 나른다. 마지막 필드(하위호환·기본값 None — 기존 dispatch
    # 호출·테스트가 이 필드 없이도 그대로 동작).
    context: str | None = None


# ── 위임 스냅샷: DelegationSnapshot ──────────────────────────────────────
#
# owner가 백업 워커에 *명시적으로 위임한* 격리 스냅샷의 메타(ADR 0012 결정 3·9).
# 이 레코드 자체는 위임 사실과 최신성만 든다 — 실 지식 본체(문서·인덱스)는 owner별
# 격리 저장소에 있고 백업 인스턴스만 접근한다(중앙 무지식 보존). 중앙/디스패처는 이
# 메타로 "백업이 답할 수 있는가·얼마나 최신인가"만 판단한다. AgentCard 자기보고 필드가
# *아니라* 별 레코드인 이유: 위임은 가용성·보안 정책이지 담당 영역 선언이 아니고
# (Authority 중앙·ADR 0004 정합), 디스패처가 *주입*받아 보관한다(카드 자기보고 금지).


@dataclass(frozen=True)
class DelegationSnapshot:
    """owner가 백업 워커에 위임한 격리 스냅샷의 메타(실 데이터 본체는 격리 저장소).

    `agent_ids`는 위임 대상 카드(어느 담당 영역을 백업이 답하나), `snapshot_at`은
    스냅샷을 뜬 시각 — staleness 판정·신뢰 맥락의 기준(ADR 0012 결정 9). 실 데이터
    위치·암호화 키 핸들은 *연결점만*(실 구현 후속, 이 ADR 범위 밖).
    """

    owner_id: str
    agent_ids: tuple[str, ...]
    snapshot_at: datetime


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

    `manager_id`는 이 작업을 떠넘길 Manager(owner의 `manages` 상위 User.id) — T5.2
    Manager 큐가 *기계로 소비*할 1급 식별자(어느 큐에 적재할지). owner의 manager가
    없으면(루트) `None`. `reason`은 그 escalation의 *사람용* 자연어 근거(왜 escalation
    됐나 — 대기 시간·한계). 둘을 분리하는 이유: 큐 라우팅은 식별자로, 운영자 화면 표시는
    문장으로 — `ConsensusOutcome.Deadlocked.reason`이 사람용이듯. 실제 Manager 큐 적재·
    사람 그래프 상향은 T5.2에 위임 — 여긴 처분 상태와 그 대상만 남긴다(ADR 0008 정합).

    노출 불변식: `manager_id`·`reason`은 *기계/운영자용* 내부값이라 사용자向 `Pending`에
    싣지 않는다. `Pending`은 `kind`+`message`만(ask_org가 투영 시 둘을 떨궈낸다).
    """

    ticket: WorkTicket
    manager_id: str | None = None
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

    def dispatch(self, question: str, card: "AgentCard", context: str | None = None) -> WorkTicket: ...

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
        t1: timedelta | None = None,
    ) -> None:
        self._clock = clock
        self._timeout = timeout
        # t1(primary 무응답 한계, ADR 0012 결정 8): primary가 claim한 뒤 t1 안에 답이
        # 없으면 그 claim을 회수해 backup으로 전환할 수 있게 *판정만* 제공한다(회수+재push
        # 오케스트레이션은 transport). None이면 단일 timeout 동작 그대로(하위호환 — t1
        # 분배 없이 enqueued_at 기준 단일 escalation). 전체 escalation 한계는 여전히
        # `timeout`(t2는 그 안에 흡수 — "합이 기존 단일 timeout 근처", 결정 8-1).
        #
        # t1 < timeout 전제(결정 8-1): t1이 전체 timeout 이상이면 backup 전환 단계가
        # *조용히 무력화*된다 — t1 경과 전에 이미 전체 timeout으로 escalation돼 backup이
        # 끼어들 틈이 없다. 그런 설정은 backup 가용성 목적을 죽이므로 구성 오류로 거부한다
        # (조용한 무력화보다 명시적 ValueError). t1=None(하위호환)은 분배 자체가 없어 무관.
        if t1 is not None and t1 >= timeout:
            raise ValueError(
                f"t1({t1})은 timeout({timeout})보다 작아야 한다 — "
                "t1 >= timeout이면 backup 전환 단계가 무력화된다(ADR 0012 결정 8-1)"
            )
        self._t1 = t1
        # owner_id -> 그 owner의 Manager User.id (timeout escalation 대상). 사람
        # 그래프 상향은 본디 Registry/Manager 영역 — 디스패처는 주입으로 받아 결합 회피.
        self._manager_of = manager_of
        self._queues: dict[str, list[WorkTicket]] = {}
        self._claimed: set[str] = set()  # claimed ticket_id 집합
        # claimed ticket_id → claim 시각(주입 clock). t1 경과 판정 기준(결정 8) —
        # enqueued_at(큐 적재)이 아니라 *claim 시점*부터 재므로 primary가 늦게 가져가도
        # primary 처리 시간만 t1로 잰다. release_claims로 되돌리면 함께 정리된다.
        self._claimed_at: dict[str, datetime] = {}
        self._answers: dict[str, "Answer"] = {}
        self._status: dict[str, WorkStatus] = {}  # ticket_id → WorkStatus
        # append-only 정신의 이력(전이 ≠ 기록 — audit과 별개의 도메인 상태 보관).
        self.history: list[WorkTicket] = []

    def now(self) -> datetime:
        """현재 시각(주입 clock) — 합성 측(transport)이 staleness 판정에 같은 시계를 쓰게.

        ADR 0012 결정 8-3·9 정신: t1/t2·staleness 모두 *주입 clock 하나*로 결정론. 큐가
        그 clock을 소유하므로, transport가 별 clock을 또 들지 않고 이 공개 접근으로 같은
        시각을 본다(시각 조회일 뿐 — 단조 종착·멱등·큐 상태기계에 무영향).
        """
        return self._clock()

    def dispatch(self, question: str, card: "AgentCard", context: str | None = None) -> WorkTicket:
        ticket = WorkTicket(
            owner_id=card.owner,
            agent_id=card.agent_id,
            question=question,
            enqueued_at=self._clock(),
            context=context,
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
        # manager_id: T5.2 Manager 큐가 기계 소비할 1급 식별자(어느 큐에 넣을지).
        # 사람 그래프 상향은 본디 Registry/Manager 영역 — 디스패처는 주입 콜백으로
        # 받아 결합을 피한다. owner의 manager가 없으면(루트) None.
        manager_id = self._manager_of(ticket.owner_id) if self._manager_of else None
        manager_part = f", 담당 매니저: {manager_id}" if manager_id else ""
        waited_sec = f"{waited.total_seconds():.0f}" if waited is not None else "?"
        reason = (
            f"owner '{ticket.owner_id}' 미응답 — "
            f"대기 {waited_sec}초 초과(한계 {self._timeout.total_seconds():.0f}초)"
            f"{manager_part}"
        )
        return EscalatedToManager(ticket=ticket, manager_id=manager_id, reason=reason)

    def claim(self, owner_id: str) -> WorkTicket | None:
        queue = self._queues.get(owner_id, [])
        for ticket in queue:
            tid = ticket.ticket_id
            status = self._status.get(tid, "queued")
            if status == "queued":
                self._mark_claimed(tid)
                return ticket
        return None

    def claimable(self, owner_id: str) -> list[WorkTicket]:
        """그 owner 큐의 *queued*(아직 claim 안 된) 작업을 FIFO로 조회한다(전이 없음).

        ADR 0012 결정 8·9, head-of-line 해소: `claim`은 FIFO 첫 queued 하나만 꺼내므로,
        앞의 작업이 "claim해도 push 못 하는"(backup 부재·stale 위임) 경우 뒤의 push 가능한
        작업까지 막혔다(transport `_push_pending`이 거부 시 멈춤). transport가 *어느 작업을
        push할지 ticket별로 고르려면* 후보 목록을 먼저 봐야 한다 — 이 조회가 그 목록을
        준다. 조회일 뿐 전이는 없다(claim은 `claim_ticket`이 ticket별로 수행). claimed/
        answered/expired는 후보가 아니므로 제외. FIFO 순서 보존(큐에 쌓인 순서 그대로).
        """
        return [
            ticket
            for ticket in self._queues.get(owner_id, [])
            if self._status.get(ticket.ticket_id, "queued") == "queued"
        ]

    def claim_ticket(self, ticket_id: str) -> bool:
        """*특정* ticket을 queued→claimed로 전이한다(push 가능 확정 시 transport가 호출).

        ADR 0012 결정 8·9, head-of-line 해소: `claim`(FIFO 첫 queued)과 달리 transport가
        `claimable`로 후보를 본 뒤 *push할 그 작업만* 집어 claim한다 — 앞의 거부 작업을
        건너뛰고 뒤의 push 가능한 작업을 claim할 수 있다(FIFO 첫 작업 강제 해소). queued가
        아니면 no-op False(이미 claimed/answered/expired면 단조성상 무변경 — 멱등). claim
        시각도 `claim`과 동일하게 기록해 t1 경과 판정(`stale_claims`) 대상이 되게 한다.

        반환: 실제로 claim했으면 True, 아니면 False(transport가 push 여부 판단에 사용).
        """
        # `_status`에 *명시적으로 queued로 등록된* 작업만 claim한다. 미존재 ticket은 키가
        # 없으므로 거부(get 기본값에 기대 미등록 ticket을 claim하지 않게 — dispatch만이
        # 작업을 큐에 등록한다). claimed/answered/expired는 단조성상 무변경(멱등).
        if self._status.get(ticket_id) != "queued":
            return False
        self._mark_claimed(ticket_id)
        return True

    def _mark_claimed(self, ticket_id: str) -> None:
        """ticket_id를 claimed로 전이하고 claim 시각을 기록한다(`claim`/`claim_ticket` 공유).

        claim 시각(주입 clock)은 t1 경과 판정 기준(결정 8) — enqueued_at이 아니라 claim
        시점부터 잰다(primary가 늦게 가져가도 처리 시간만 t1로). 재claim 시 갱신된다.
        """
        self._claimed.add(ticket_id)
        self._status[ticket_id] = "claimed"
        self._claimed_at[ticket_id] = self._clock()

    def submit(self, ticket_id: str, answer: "Answer") -> None:
        status = self._status.get(ticket_id, "queued")
        # 단조 종착 + at-least-once 멱등(ADR 0011 결정 6-4): 이미 종착한 작업의
        # 늦은/중복 submit은 무시한다. expired(escalated)는 timeout 부활 방지(슬라이스1),
        # answered는 WS 재연결의 중복 submit이 첫 답을 덮어쓰지 못하게(슬라이스2b). 둘 다
        # ticket_id 기준 멱등 — 같은 작업이 두 번 회신돼도 첫 결말이 고정된다(미아 없음).
        if status in ("expired", "answered"):
            return
        self._answers[ticket_id] = answer
        self._status[ticket_id] = "answered"
        # 종착 — claim 시각 추적 정리(무한 누적 방지, t1 판정 대상 아님).
        self._claimed_at.pop(ticket_id, None)

    def release_claims(self, owner_id: str) -> list[WorkTicket]:
        """그 owner의 미회신 `claimed` 작업을 `queued`로 되돌린다(WS 끊김 회수).

        ADR 0011 결정 6-4 — 워커가 작업을 push 받고(claimed) 답 전에 끊기면, 그 작업이
        영구 미아가 되지 않게 다시 큐에 올린다(claimed→queued). 단조성 보존: 이미
        answered/expired 작업은 손대지 않는다(종착은 부활 불가). 재연결 시 그 작업이
        다시 claim돼 push된다. timeout은 그대로 작동(영영 안 돌아오면 EscalatedToManager).

        반환: 되돌린 ticket 목록(없으면 빈 리스트). FIFO 순서 보존 — 큐에 남은 순서
        그대로 돌리므로 재연결 후 재claim도 같은 순서다.
        """
        released: list[WorkTicket] = []
        for ticket in self._queues.get(owner_id, []):
            tid = ticket.ticket_id
            # claimed(미회신·진행 중)만 되돌린다. queued는 이미 대기라 무변경,
            # answered/expired는 종착이라 단조성상 손대지 않는다(부활 금지).
            if self._status.get(tid) == "claimed":
                self._claimed.discard(tid)
                self._claimed_at.pop(tid, None)
                self._status[tid] = "queued"
                released.append(ticket)
        return released

    def get_ticket(self, ticket_id: str) -> WorkTicket | None:
        """ticket_id로 WorkTicket 메타를 조회한다(전이 없음 — history 검색).

        BackupReviewItem 생성 트리거(ADR 0012 결정 7)가 ticket 메타(owner_id·agent_id·
        question)를 복원할 때 사용한다. dispatch 시점에 history에 기록되므로 상태와 무관.
        """
        for ticket in self.history:
            if ticket.ticket_id == ticket_id:
                return ticket
        return None

    def stale_claims(self, owner_id: str) -> list[WorkTicket]:
        """그 owner의 claimed 작업 중 *claim 후 t1 경과*한 것을 골라 `queued`로 회수한다.

        ADR 0012 결정 8 — primary가 작업을 가져갔는데(claimed) t1 안에 답이 없으면,
        그 작업을 backup으로 *재전환*할 수 있게 claim을 회수한다(claimed→queued, 단조성
        보존). `release_claims`(owner 단위 전량 회수, 끊김용)와 달리 *t1 경과분만* 선택
        회수한다 — 멀쩡히 진행 중인(t1 전) primary 작업은 건드리지 않는다(primary 우선
        보존, 결정 8-2). t1 미설정이면 회수 없음(빈 리스트 — 단일 timeout 동작 그대로).

        반환: 회수한 ticket 목록(transport가 이걸 받아 backup으로 재push). 회수만 하고
        재push·primary 제외 신호는 transport 책임(전이 ≠ 전송). answered/expired는
        종착이라 손대지 않는다(부활 금지). 멱등 — 같은 ticket은 한 번 회수되면 queued라
        다음 호출에선 대상 아님.
        """
        if self._t1 is None:
            return []
        now = self._clock()
        recovered: list[WorkTicket] = []
        for ticket in self._queues.get(owner_id, []):
            tid = ticket.ticket_id
            if self._status.get(tid) != "claimed":
                continue
            claimed_at = self._claimed_at.get(tid)
            if claimed_at is None:
                continue
            if now - claimed_at > self._t1:
                self._claimed.discard(tid)
                self._claimed_at.pop(tid, None)
                self._status[tid] = "queued"
                recovered.append(ticket)
        return recovered


# ── 로컬 즉답 디스패처: LocalRuntimeDispatcher ───────────────────────────
#
# 동기 `AgentRuntime`(StubRuntime·ClaudeCodeRuntime)을 RuntimeDispatcher 포트로
# 감싸 *항상 즉시 Delivered*를 돌려주는 in-process 어댑터. dispatch 시점에 그 자리에서
# 로컬 런타임으로 답을 만들어 큐에 넣자마자 회신 완료 상태로 두고, poll은 곧장
# Delivered(그 Answer)를 반환한다 — 네트워크·워커·대기 없음.
#
# 왜 필요한가: ask_org가 비동기화(아래 ②)되면서 더 이상 `runtime.answer`를 직접
# 부르지 않고 `RuntimeDispatcher.dispatch→poll`만 본다. 그런데 in-process 데모/단위
# 테스트(test_ask_org·test_web)는 *즉답*이 필요하다(Routed→Answered가 한 호출에 끝나야
# 그린). 그래서 동기 런타임을 디스패처 모양으로 입혀, 분산이 아닌 환경에선 dispatch가
# 곧 답이 되게 한다. owner worker가 로컬에서 자기 카드를 직접 돌리는 *디제너레이트
# 케이스*(워커=중앙, 큐 길이 0)이기도 하다 — 분산의 특수형이지 별 경로가 아니다.
#
# AwaitingWorker/EscalatedToManager는 *나지 않는다*(로컬 동기라 미회신·timeout이 구조적
# 으로 불가). 분산 환경에선 `InMemoryWorkQueueDispatcher`(슬라이스1)·네트워크 디스패처
# (슬라이스2)가 이 자리를 대신하고, 그때 비로소 Pending(대기/escalation) 분기가 살아난다.


class LocalRuntimeDispatcher:
    """동기 `AgentRuntime`을 즉시-Delivered 디스패처로 감싸는 in-process 어댑터.

    `dispatch(question, card)`가 그 자리에서 `runtime.answer(question, card)`를 호출해
    답을 만들고, 곧장 회신 완료 상태로 둔다. `poll(ticket)`은 항상 `Delivered`를
    돌려준다(미회신·timeout 없음 — 로컬 동기라 구조적으로 불가).

    용도: ask_org 비동기화 후에도 in-process 데모/단위 테스트가 *즉답*을 받게 하는 다리
    (Routed→Answered가 한 호출에 끝나야 test_ask_org·test_web 그린). 분산(워커·큐·대기)이
    실제로 필요한 슬라이스2에선 네트워크 디스패처가 이 자리를 대신한다.

    포트 패턴은 `InMemoryWorkQueueDispatcher`와 동일(RuntimeDispatcher 구현체).
    """

    def __init__(self, runtime: "AgentRuntime", clock: Clock = default_clock) -> None:
        self._runtime = runtime
        self._clock = clock
        self._answers: dict[str, "Answer"] = {}
        self.history: list[WorkTicket] = []

    def dispatch(self, question: str, card: "AgentCard", context: str | None = None) -> WorkTicket:
        """그 자리에서 로컬 런타임으로 답을 만들고, 회신 완료 상태의 추적표를 돌려준다.

        디제너레이트 케이스(워커=중앙 한몸): 큐 길이 0, 미회신/timeout 구조적 불가.
        dispatch 시점에 runtime.answer를 동기 호출해 _answers에 저장, poll은 항상 Delivered.
        로컬 경로(ADR 0027 결정 7): context를 runtime.answer(context=)로 즉시 전달한다.
        """
        from agent_org_network.runtime import Answer as _Answer  # 순환 import 회피

        answer: _Answer = self._runtime.answer(question, card, context=context)
        ticket = WorkTicket(
            owner_id=card.owner,
            agent_id=card.agent_id,
            question=question,
            enqueued_at=self._clock(),
        )
        self._answers[ticket.ticket_id] = answer
        self.history.append(ticket)
        return ticket

    def poll(self, ticket: WorkTicket) -> DispatchOutcome:
        """항상 Delivered를 반환한다(로컬 동기 — 대기·escalation 없음).

        디제너레이트 케이스: dispatch에서 이미 답이 채워지므로 AwaitingWorker·
        EscalatedToManager는 구조적으로 발생하지 않는다.
        """
        return Delivered(ticket=ticket, answer=self._answers[ticket.ticket_id])

    def claim(self, owner_id: str) -> WorkTicket | None:
        """로컬 즉답엔 회수할 큐가 없다 — 항상 None(워커=중앙이라 claim 불요)."""
        return None

    def submit(self, ticket_id: str, answer: "Answer") -> None:
        """로컬 즉답엔 외부 회신이 없다 — no-op(이미 dispatch에서 답이 채워짐)."""


# ── 로컬 스트리밍 디스패처: LocalStreamingDispatcher ─────────────────────
#
# `LocalRuntimeDispatcher`의 스트리밍 변형(ADR 0031 결정 4). 기존 블로킹 포트
# (dispatch/poll/claim/submit)를 그대로 지원하면서, *옵셔널 스트리밍 능력*
# `dispatch_stream`을 더한다. 카드 런타임을 `isinstance(runtime, StreamingRuntime)`로
# 감지해 — 지원하면 `answer_stream`의 델타를 그대로 흘리고, 미지원이면 `answer`의 완성
# 텍스트를 *한 델타*로 yield한다(폴백 규약·결정 1). 어느 쪽이든 스트림 종료 시 *완성
# `Answer`*를 확정해 audit·게이트·세션 적재가 그 완성 답을 본다(델타 합 = 완성 텍스트).
#
# 로컬 인프로세스 경로 전용 — 그 자리에서 답 생성이라 와이어 직렬화를 안 거친다(0027
# 결정 7·8 로컬 경로 정신). 분산 WS 경로는 비스트림 폴백(이번 범위 밖·후속).


class LocalStreamingDispatcher:
    """동기 `AgentRuntime`을 즉시-Delivered 디스패처로 감싸되 스트리밍 능력을 더한 어댑터.

    블로킹 면(`dispatch`/`poll`)은 `LocalRuntimeDispatcher`와 동형 — Routed→Answered가 한
    호출에 끝나는 in-process 데모/단위 테스트의 즉답을 보존한다. 스트리밍 면(`dispatch_stream`)은
    런타임이 `StreamingRuntime`이면 델타를, 아니면 단일 `answer` 텍스트를 한 델타로 흘린다.

    포트 패턴은 `LocalRuntimeDispatcher`와 동일(RuntimeDispatcher 구현 + 스트리밍 능력).
    """

    def __init__(self, runtime: "AgentRuntime", clock: Clock = default_clock) -> None:
        self._runtime = runtime
        self._clock = clock
        self._answers: dict[str, "Answer"] = {}
        self.history: list[WorkTicket] = []

    def dispatch(self, question: str, card: "AgentCard", context: str | None = None) -> WorkTicket:
        """블로킹 폴백: 그 자리에서 runtime.answer로 답을 만들고 회신 완료 추적표를 돌려준다."""
        answer = self._runtime.answer(question, card, context=context)
        ticket = WorkTicket(
            owner_id=card.owner,
            agent_id=card.agent_id,
            question=question,
            enqueued_at=self._clock(),
        )
        self._answers[ticket.ticket_id] = answer
        self.history.append(ticket)
        return ticket

    def poll(self, ticket: WorkTicket) -> DispatchOutcome:
        """항상 Delivered를 반환한다(로컬 동기 — 대기·escalation 없음)."""
        return Delivered(ticket=ticket, answer=self._answers[ticket.ticket_id])

    def claim(self, owner_id: str) -> WorkTicket | None:
        return None

    def submit(self, ticket_id: str, answer: "Answer") -> None:
        """로컬 즉답엔 외부 회신이 없다 — no-op."""

    def dispatch_stream(
        self, question: str, card: "AgentCard", context: str | None = None
    ) -> "StreamedAnswer":
        """런타임 능력에 따라 델타를 흘리는 *스트림 핸들*을 돌려준다(완성 `Answer` 확정 포함).

        ADR 0031 결정 4: 카드 런타임을 `isinstance(runtime, StreamingRuntime)`로 감지한다.
        지원하면 `answer_stream`의 `AnswerChunk` 델타를 그대로 흘리고, 미지원이면 `answer`의
        완성 텍스트를 한 `AnswerChunk`로 yield한다(폴백 규약). 어느 쪽이든 스트림을 다 흘린
        뒤 `StreamedAnswer.completed`로 *완성 `Answer`*(델타 합 text·카드 파생 sources·mode)를
        확정한다 — audit·게이트·세션 적재가 그 완성 답을 본다(델타 운반과 답 확정을 한 핸들에).

        반환값을 직접 iterate하면 델타가 흐르고, 다 흐른 뒤 `.completed`가 완성 답을 든다.
        폴백 런타임은 `answer`를 *정확히 1회만* 호출해 그 답을 완성 답으로 보관한다(이중 호출 0).
        """
        return StreamedAnswer(self._runtime, question, card, context)


class StreamedAnswer:
    """`dispatch_stream`이 돌려주는 스트림 핸들 — 델타를 흘리고 완성 `Answer`를 확정한다.

    iterate하면 `AnswerChunk` 델타가 순서대로 흐른다. 다 흐른 뒤 `completed` 프로퍼티가
    *완성 `Answer`*를 든다(스트림 종착 = 완성 답). 스트리밍 런타임이면 델타를 합쳐 완성
    답을 만들고, 폴백 런타임이면 `answer`를 1회 호출한 그 답이 완성 답이자 1델타 출처다
    (이중 호출 0). 완성 답이 audit·게이트·노출 투영의 SSOT.
    """

    def __init__(
        self,
        runtime: "AgentRuntime",
        question: str,
        card: "AgentCard",
        context: str | None,
    ) -> None:
        self._runtime = runtime
        self._question = question
        self._card = card
        self._context = context
        self._completed: "Answer | None" = None

    def __iter__(self) -> Iterator["AnswerChunk"]:
        from agent_org_network.runtime import (
            Answer as _Answer,
            AnswerChunk as _AnswerChunk,
            StreamingRuntime,
        )

        if isinstance(self._runtime, StreamingRuntime):
            deltas: list[_AnswerChunk] = []
            for chunk in self._runtime.answer_stream(
                self._question, self._card, context=self._context
            ):
                deltas.append(chunk)
                yield chunk
            self._completed = _Answer(
                text="".join(d.text_delta for d in deltas),
                sources=tuple(self._card.knowledge_sources),
                mode="full",
            )
        else:
            answer = self._runtime.answer(self._question, self._card, context=self._context)
            self._completed = answer
            yield _AnswerChunk(text_delta=answer.text)

    @property
    def completed(self) -> "Answer":
        """스트림을 다 흘린 뒤의 완성 `Answer`. iterate 전에 접근하면 ValueError."""
        if self._completed is None:
            raise ValueError("스트림을 다 흘리기 전에는 완성 Answer가 없다 — 먼저 iterate하라")
        return self._completed


# ── 동기 어댑터(레거시 호환): DispatchingRuntime ─────────────────────────
#
# 기존 동기 포트 AgentRuntime.answer(question, card) -> Answer 를 *디스패처 위에서*
# 보존하는 다리. ADR 0011 슬라이스1의 산물 — `AgentRuntime` 계약(항상 Answer 반환)을
# 요구하는 호출처를 위해 RuntimeDispatcher를 동기 answer로 흡수한다.
#
# 주의(ADR 0011 ②, 위장 금지): 아래 EscalatedToManager/AwaitingWorker → 폴백 Answer
# 변환은 *AgentRuntime 계약을 지키기 위한 어댑터 한정 동작*이지, 도메인 처분을 답으로
# 뭉개도 좋다는 뜻이 아니다. **ask_org는 이 어댑터를 거치지 않는다** — ask_org는 비동기화
# 되어 `RuntimeDispatcher`를 직접 보고 escalation/미회신을 `Pending`으로 표면화한다(아래
# ask_org.py). 이 어댑터는 "동기 answer가 꼭 필요한 비-ask_org 호출처"의 호환 경로로만
# 남는다(현재 그런 호출처는 test_dispatch 단위 테스트뿐 — 프로덕션 경로 아님).


class DispatchingRuntime:
    """RuntimeDispatcher 위에 AgentRuntime(동기 answer)을 얹는 어댑터(레거시 호환).

    `worker`를 주입해 동기 시뮬 — worker가 claim→로컬 런타임으로 답→submit을 동기
    수행한 뒤 poll로 결과를 수집한다. sleep/스레드 없이 결정론을 보장(단위테스트용).
    실제 분산에선 worker가 원격·비동기로 돌지만 포트(동기 answer) 계약은 유지된다.

    EscalatedToManager(timeout/owner 부재)·AwaitingWorker(미회신)는 AgentRuntime 계약상
    Answer로 폴백 표면화한다 — 단 이는 *이 어댑터의 호환 동작*이고, 사용자向 경로(ask_org)는
    이를 쓰지 않고 `Pending`으로 표면화한다(ADR 0011 ②, "Answer 위장 금지"). 즉 이 폴백은
    "동기 answer를 요구하는 호출처"를 깨뜨리지 않기 위한 마지막 안전망일 뿐이다.
    """

    def __init__(
        self,
        dispatcher: RuntimeDispatcher,
        worker: Callable[[RuntimeDispatcher], None] | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._worker = worker

    def answer(self, question: str, card: "AgentCard", context: str | None = None) -> "Answer":
        from agent_org_network.runtime import Answer as _Answer

        ticket = self._dispatcher.dispatch(question, card, context=context)

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
