"""WebSocket 전송층 — owner 워커↔중앙 양방향 프레임 + WS 디스패처 (T6.3 슬라이스2b, ADR 0011 결정 6).

ADR 0011 결정 6: 전송 채널 = WebSocket. owner 워커가 중앙에 *아웃바운드* WS 연결을 걸고
(중앙은 받기만), 중앙 핸들러가 작업을 그 소켓으로 push, 워커는 로컬 claude 답을 회신한다.
실시간 비전(답 토큰 스트리밍·양방향·단일 영속 연결) 때문에 long-poll을 기각하고 WS를 택했다.

이 모듈은 두 가지를 둔다 — (1) **전송 프레임(Transport Frame)** pydantic DTO: 워커↔중앙
와이어 메시지. (2) **`WebSocketDispatcher`**: `InMemoryWorkQueueDispatcher`(작업 큐 도메인,
슬라이스1)를 *합성*해 재사용하고 그 위에 WS 전송만 얹는 `RuntimeDispatcher` 구현.

설계 원칙(결정 6):
  - **WS는 새 큐 도메인이 아니다 — 합성.** 큐 상태기계(queued↔claimed↔answered↔expired·
    단조 종착·timeout escalation·owner별 격리)는 슬라이스1의 `InMemoryWorkQueueDispatcher`가
    소유한다(미아 없음·idempotency 1차 보증). WS는 claim/submit을 *전송*으로 중계할 뿐.
  - **포트 무변경.** `RuntimeDispatcher.claim(owner_id)`은 보존 — WS에선 워커가 직접 부르지
    않고 *중앙 핸들러가 워커 대신 claim해 push*한다(claim 의미 보존, 트리거 주체만 이동).
  - **프레임 ≠ 도메인 값 객체.** 프레임은 와이어 DTO(pydantic), `WorkTicket`/`Answer`는 코어
    값 객체(frozen dataclass). 경계에서 변환(`to_ticket_frame`/`from_answer_frame` 등).

프레임↔도메인 변환과 `WebSocketDispatcher` 동작(연결 레지스트리·push·재동기·release)은
2b-i에서 구현 완료다(결정론 테스트 `test_transport`·`test_server`). 실 owner 워커 프로세스·
실 claude·실 네트워크는 2b-ii(수동 시연)로 분리된다.
"""

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from agent_org_network.dispatch import (
    Clock,
    DelegationSnapshot,
    DispatchOutcome,
    EscalatedToManager,
    InMemoryWorkQueueDispatcher,
    WorkTicket,
    default_clock,
)

if TYPE_CHECKING:
    # 어댑터 시그니처 예고용 — 런타임 import 순환을 피해 타입 체크 시에만 끌어온다.
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.review import BackupReviewStore
    from agent_org_network.runtime import Answer


# ── 전송 프레임(Transport Frame): 와이어 DTO ─────────────────────────────────
#
# 워커↔중앙 WS로 오가는 JSON 메시지. 모두 `type` 판별 필드를 가진 봉투(envelope)이고
# pydantic v2 모델로 검증한다. 도메인 값 객체(WorkTicket·Answer)가 아니라 *전송 DTO*라
# 이 모듈에 격리한다(frozen=True로 우발적 변경 방지, extra="forbid"로 미지 필드 거부).
#
# CONTEXT 유비쿼터스 언어: 이 묶음의 용어는 **Transport Frame**(전송 프레임).


class _Frame(BaseModel):
    """전송 프레임 공통 베이스 — frozen·미지 필드 거부(와이어 안전)."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class TicketFrame(_Frame):
    """`PushWork`에 실리는 작업 추적표의 와이어 표현(WorkTicket의 전송 투영).

    `owner_id`는 *연결 귀속*이라 싣지 않는다 — 그 소켓이 곧 그 owner(6-3). 경계에서
    `WorkTicket`으로 복원할 때 연결의 owner_id를 붙인다.
    """

    ticket_id: str
    agent_id: str
    question: str
    enqueued_at: datetime


class AnswerFrame(_Frame):
    """`SubmitAnswer`에 실리는 답의 와이어 표현(`Answer`의 전송 투영).

    `mode` 보존 — owner 답이 `draft_only`(Approval 게이트)면 그 신뢰 상태가 회신에
    실려 내려온다(결정 6-3, ADR 0011 Approval 연결점). `backup`은 owner 위임 백업
    워커가 회신할 때 실리는 값이나, *백업 사실의 진실은 연결 등급*이라 디스패처가
    submit 시 강제 하향한다(ADR 0012 결정 4 — 워커 자기보고에만 맡기지 않는다).
    """

    text: str
    sources: tuple[str, ...] = ()
    mode: Literal["draft_only", "full", "backup"] = "full"


# ── 워커→중앙(업스트림) 프레임 ───────────────────────────────────────────────


WorkerRole = Literal["primary", "backup"]
#   owner 안에서의 워커 등급(ADR 0012 결정 2). primary=owner PC 워커(실시간), backup=
#   owner가 명시적으로 위임한 격리 백업 인스턴스(스냅샷 기반·신뢰 하향). 신원(어느 owner)
#   은 `owner_id`, 등급은 그 owner 안에서의 push 우선순위. 디스패처가 primary 우선 push하고
#   backup으로 처리된 답은 mode=backup으로 강제 하향한다(결정 4).


class RegisterWorker(_Frame):
    """연결 직후 1회 — 워커가 자기 owner 신원·등급을 선언한다(인증 연결점, 6-5).

    `token`은 owner 신원 인증용(ADR 0009 → T6.5). 이번 슬라이스는 거부 *hook*만 두고
    실 토큰 검증은 T6.5. `role`은 그 owner 안에서의 워커 등급(ADR 0012 결정 2) —
    PC 워커는 기본 `primary`, owner 위임 백업 인스턴스는 `backup`. 하위호환: 미지정이면
    `primary`(기존 워커는 그대로 1차 워커로 등록). 중앙은 이 owner를 등급별로 레지스트리에
    올린다.
    """

    type: Literal["register_worker"] = "register_worker"
    owner_id: str
    token: str | None = None
    role: WorkerRole = "primary"


class SubmitAnswer(_Frame):
    """로컬 claude가 만든 답 회신 — 중앙의 내부 `submit(ticket_id, answer)`을 트리거한다.

    멱등 키 = `ticket_id`(6-4) — 재연결로 중복 도착해도 첫 답이 고정된다(answered 재submit
    무시). 미인증/owner 불일치 연결의 SubmitAnswer는 거부한다(회신이 진짜 그 owner에게서
    왔는지 검증, 6-5).
    """

    type: Literal["submit_answer"] = "submit_answer"
    ticket_id: str
    answer: AnswerFrame


class Heartbeat(_Frame):
    """연결 생존 신호(6-4) — 중앙이 워커별 마지막 수신 시각을 갱신한다."""

    type: Literal["heartbeat"] = "heartbeat"


class Ack(_Frame):
    """작업 수신 확인 — 중앙이 같은 ticket 재push를 멈춘다(at-least-once 흡수, 6-4)."""

    type: Literal["ack"] = "ack"
    ticket_id: str


WorkerFrame = RegisterWorker | SubmitAnswer | Heartbeat | Ack
#   워커→중앙 업스트림 프레임의 sealed 판별 유니온(type 필드로 갈림).


# ── 중앙→워커(다운스트림) 프레임 ─────────────────────────────────────────────


class Welcome(_Frame):
    """등록 수락 — RegisterWorker 인증 통과 후 중앙이 보낸다."""

    type: Literal["welcome"] = "welcome"


class AuthError(_Frame):
    """등록 거부 — 미인증/토큰 불일치(6-5). 이후 SubmitAnswer는 거부된다."""

    type: Literal["auth_error"] = "auth_error"
    reason: str


class PushWork(_Frame):
    """claim으로 꺼낸 작업을 워커에 전달(다운스트림, 6-3).

    중앙 핸들러가 워커 대신 `claim(owner_id)`을 호출해 꺼낸 `WorkTicket`을 `TicketFrame`으로
    실어 push한다(포트의 pull→push 의미 보존, 6-2). 워커는 `Ack`로 응답.
    """

    type: Literal["push_work"] = "push_work"
    ticket: TicketFrame


class Ping(_Frame):
    """중앙발 생존 확인(6-4) — 워커는 Heartbeat/Ack로 응답한다."""

    type: Literal["ping"] = "ping"


CentralFrame = Welcome | AuthError | PushWork | Ping
#   중앙→워커 다운스트림 프레임의 sealed 판별 유니온.


# ── 프레임 ↔ 도메인 값 객체 변환(경계) ───────────────────────────────────────
#
# 프레임(와이어 DTO)과 코어 값 객체(WorkTicket·Answer) 사이를 핸들러가 경계에서 변환한다.
# 도메인 객체가 와이어 포맷에 오염되지 않게(전이 ≠ 전송) 변환을 한곳에 모은다.


def to_ticket_frame(ticket: WorkTicket) -> TicketFrame:
    """`WorkTicket` → `TicketFrame`(push 전 와이어 투영, owner_id는 연결 귀속이라 생략)."""
    return TicketFrame(
        ticket_id=ticket.ticket_id,
        agent_id=ticket.agent_id,
        question=ticket.question,
        enqueued_at=ticket.enqueued_at,
    )


def from_ticket_frame(frame: TicketFrame, owner_id: str) -> WorkTicket:
    """`TicketFrame` + 연결 owner_id → `WorkTicket`(워커 측 복원)."""
    return WorkTicket(
        owner_id=owner_id,
        agent_id=frame.agent_id,
        question=frame.question,
        enqueued_at=frame.enqueued_at,
        ticket_id=frame.ticket_id,
    )


def to_answer_frame(answer: "Answer") -> AnswerFrame:
    """`Answer` → `AnswerFrame`(워커가 submit 전 와이어 투영, mode 보존)."""
    return AnswerFrame(text=answer.text, sources=answer.sources, mode=answer.mode)


def from_answer_frame(frame: AnswerFrame) -> "Answer":
    """`AnswerFrame` → `Answer`(중앙이 submit 받을 때 복원, mode 보존)."""
    from agent_org_network.runtime import Answer as _Answer  # 순환 import 회피

    return _Answer(text=frame.text, sources=frame.sources, mode=frame.mode)


# ── WS 디스패처: WebSocketDispatcher ─────────────────────────────────────────
#
# `InMemoryWorkQueueDispatcher`(작업 큐 도메인, 슬라이스1)를 *합성*해 재사용하고 그 위에
# WS 전송만 얹는 `RuntimeDispatcher` 구현(결정 6-2). 상속이 아니라 위임 — 큐 상태기계는
# 합성한 in-memory 큐가 소유하고(단조 종착·미아 없음 보증), WS는 push/submit을 *전송*으로
# 중계한다. 따라서 기존 in-process 구현·테스트(143 passed)는 포트가 안 바뀌어 그대로 산다.
#
# 연결 레지스트리(`_connections`, ADR 0012 결정 2): owner_id → {등급(role) → send 콜백}.
# owner당 단일 연결에서 *등급별* 연결로 확장한다 — primary(owner PC 워커)와 backup(owner
# 위임 백업 인스턴스)이 같은 owner 아래 따로 등록된다. push 대상 선택은 우선순위 —
# primary가 연결돼 있으면 primary로, 없고 backup이 있으면 backup으로, 둘 다 없으면 큐에
# 대기(기존 AwaitingWorker, timeout이면 EscalatedToManager — 미아 없음 종착 그대로).


SendFrame = Callable[[CentralFrame], None]  # owner 워커 소켓으로 프레임을 내보내는 콜백


class WebSocketDispatcher:
    """`InMemoryWorkQueueDispatcher`를 합성해 WS 전송을 얹는 `RuntimeDispatcher`(stub).

    중앙측(`dispatch`/`poll`)은 합성한 내부 큐에 위임하고, 워커측(`claim`/`submit`)은
    연결된 워커 레지스트리를 거쳐 WS 프레임으로 중계한 뒤 내부 큐의 claim/submit을 호출한다.
    포트 의미 보존(6-2): claim의 pull은 "핸들러가 워커 대신 claim해 push"로, submit은
    "워커가 보낸 SubmitAnswer를 핸들러가 받아 내부 submit 호출"로.

    실패 모드(6-4)는 합성한 큐의 연산으로 흡수한다 — 끊김 시 `release_claims`(claimed→queued
    re-queue), 중복은 `ticket_id` 멱등(answered 재submit 무시, 큐가 보장). 인증(6-5)은
    RegisterWorker 시 owner 검증 hook(실 토큰 T6.5).

    등급 라우팅(ADR 0012 결정 2·4, T6.6 슬라이스 i): 연결 레지스트리를 owner당 *등급별*
    (primary/backup)로 확장한다. push는 우선순위(primary 우선, 없으면 backup)로 선택하고,
    backup 연결로 push된 작업은 submit 시 `mode=backup`으로 강제 하향한다(백업 사실의 진실은
    *연결 등급*이지 워커 자기보고가 아님 — 결정 4). 큐 도메인(claim/submit/단조 종착)은 무변경.

    동작은 2b-i에서 구현 완료 — 합성한 `_queue`(실 객체)가 `dispatch`/`poll`/`claim`/`submit`
    의 도메인을 소유하고, WS층은 그 위에 연결 레지스트리·push·release만 얹는다(큐 도메인을
    재작성하지 않는다는 결정 6-2의 보증).
    """

    def __init__(
        self,
        clock: Clock = default_clock,
        queue: InMemoryWorkQueueDispatcher | None = None,
        staleness_threshold: timedelta | None = None,
        review_store: "BackupReviewStore | None" = None,
    ) -> None:
        # 작업 큐 도메인은 합성으로 재사용 — 큐 상태기계·단조 종착·timeout escalation은
        # 이 객체가 소유한다(WS는 그 위 전송층). 주입 가능하게 둬 결정론 테스트가 고정
        # clock·timeout·manager_of를 박은 큐를 넣을 수 있게 한다(2b-i).
        self._queue = queue if queue is not None else InMemoryWorkQueueDispatcher(clock=clock)
        # owner_id → {등급(role) → send 콜백}(등급별 연결 레지스트리, ADR 0012 결정 2).
        # RegisterWorker 시 그 role로 등록, 끊김/AuthError 시 그 role만 제거. push 대상은
        # 우선순위(primary 우선, 없으면 backup). 둘 다 없으면 작업은 큐에 대기.
        self._connections: dict[str, dict[WorkerRole, SendFrame]] = {}
        # backup 연결로 push된 ticket_id 집합(ADR 0012 결정 4). submit 시 이 집합에 든
        # ticket의 답은 `mode=backup`으로 강제 하향한다 — 백업 사실은 *연결 등급*이 진실이라
        # 디스패처가 책임지고 덮는다(워커가 full로 보내도). primary push는 기록 안 함(mode 보존).
        self._backup_tickets: set[str] = set()
        # primary 회수(t1 경과)로 *backup 전환 대상*이 된 ticket_id 집합(ADR 0012 결정 8).
        # primary가 연결돼 있어도 이 ticket은 primary를 건너뛰고 backup으로 push해야 한다
        # ("느린 primary"를 회수한 작업이라 같은 primary로 되돌리면 도로아미타불). push 대상
        # 선택(`_select_connection`)이 이 신호를 보고 primary를 제외한다. backup으로 push되거나
        # primary로 (재)push될 때 정리(마지막 push 등급이 진실, 결정 4 정밀화).
        self._primary_exhausted: set[str] = set()
        # owner_id → DelegationSnapshot(위임 메타, ADR 0012 결정 3·9). `register_delegation`
        # 으로 *주입*받아 보관한다(카드 자기보고 아님 — Authority 중앙). backup push 직전
        # staleness 판정에 쓴다(snapshot_at이 임계 초과면 backup 거부 → escalation, 결정 9).
        self._delegations: dict[str, DelegationSnapshot] = {}
        # staleness 임계(ADR 0012 결정 9). None이면 staleness 검사를 *하지 않는다*(하위호환
        # — 6.6-i의 위임 없는 backup push 동작 보존). 설정되면 backup push 전 그 owner의
        # 위임 스냅샷이 있어야 하고 snapshot_at이 임계 내여야 push한다(없거나 stale이면 거부).
        self._staleness_threshold = staleness_threshold
        # 백업 답 검토 저장소(ADR 0012 결정 7 — 생성 트리거). backup 연결로 처리된 답이
        # 종착될 때 여기에 BackupReviewItem을 add한다 — "mode=backup 강제 하향"과 한 사건의
        # 두 면. 미주입이면 None(하위호환 — 검토 루프 없이 동작).
        self._review_store = review_store

    # ── 중앙측(질문 측): 내부 큐에 위임 ──────────────────────────────────────

    def dispatch(self, question: str, card: "AgentCard") -> WorkTicket:
        """작업을 큐에 적재하고, 그 owner 워커가 연결돼 있으면 즉시 push한다.

        큐 적재는 합성한 `_queue.dispatch`에 위임(도메인). 연결된 워커가 있으면 claim해
        PushWork를 send 콜백으로 내보낸다. 미연결이면 큐에 대기(기존 AwaitingWorker).
        """
        ticket = self._queue.dispatch(question, card)
        self._push_pending(card.owner)
        return ticket

    def poll(self, ticket: WorkTicket) -> DispatchOutcome:
        """회신·대기·escalation 조회 — t1 경과 backup 전환을 트리거한 뒤 `_queue.poll`에 위임.

        WS여도 결말 판정(Delivered/AwaitingWorker/EscalatedToManager)은 큐 도메인의 몫.
        사용자 답 회수(6-5)도 이 poll의 재노출이다(web 조회 엔드포인트가 ticket으로 호출).

        timeout 분배(ADR 0012 결정 8): poll은 결정론 clock으로 시간이 진전되는 유일한
        지점이라, 여기서 그 owner의 *t1 경과한 primary claim*을 회수해 backup으로 재전환
        한다(`_recover_stale_primary`). primary가 t1 안에 답을 못 하면 그 작업이 backup으로
        넘어가고, backup도 t2(전체 timeout) 안에 못 하면 큐 도메인이 EscalatedToManager로
        종착시킨다(미아 없음). t1 미설정이면 회수 없음(단일 timeout 동작 그대로).
        """
        self._recover_stale_primary(ticket.owner_id)
        outcome = self._queue.poll(ticket)
        # escalation 종착 시 라우팅 표식 정리 — 무한 누적 방지(경계 B와 같은 클래스).
        # answered 종착은 submit이 정리하지만 expired(escalation)는 submit을 안 거치므로
        # 여기서 떨어낸다. poll은 멱등(expired 재poll도 같은 결과)이라 이 discard도 멱등.
        if isinstance(outcome, EscalatedToManager):
            self._backup_tickets.discard(ticket.ticket_id)
            self._primary_exhausted.discard(ticket.ticket_id)
        return outcome

    def register_delegation(self, snapshot: DelegationSnapshot) -> None:
        """owner의 위임 스냅샷 메타를 주입받아 보관한다(ADR 0012 결정 3·9).

        디스패처가 backup push 직전 이 메타로 staleness를 판정한다(snapshot_at이 임계
        초과면 backup 거부 → escalation, 결정 9). 카드 자기보고가 아니라 *주입*이다
        (Authority 중앙 — 위임은 owner의 명시적 opt-in 정책이지 카드 선언이 아님).
        같은 owner를 다시 등록하면 최신 스냅샷으로 갱신한다(동기화 시 snapshot_at 진전).
        """
        self._delegations[snapshot.owner_id] = snapshot

    # ── 워커측(WS 핸들러가 호출): 전송 중계 ──────────────────────────────────

    def claim(self, owner_id: str) -> WorkTicket | None:
        """그 owner 큐의 다음 작업을 꺼낸다 — 합성한 `_queue.claim`에 위임.

        포트 의미 보존(6-2): WS에선 워커가 직접 부르지 않고 *중앙 핸들러가 워커 대신* 이걸
        호출해 PushWork로 내보낸다. 꺼냄(claimed 전이)은 큐 도메인, push는 전송.
        """
        return self._queue.claim(owner_id)

    def submit(self, ticket_id: str, answer: "Answer") -> None:
        """워커가 WS로 보낸 답을 큐에 회신 — 합성한 `_queue.submit`에 위임.

        멱등(6-4): 큐가 ticket_id 기준으로 보장(answered/expired 재submit 무시). 핸들러가
        SubmitAnswer 프레임을 받아 `from_answer_frame`으로 복원한 뒤 이걸 호출한다.

        등급 강제(ADR 0012 결정 4): 그 ticket이 *backup 연결로 push됐으면* `mode=backup`으로
        덮어 회신한다 — 워커가 full/draft_only로 보내도. 백업 답이라는 사실은 연결 등급이
        진실이라(워커 자기보고 아님) 디스패처가 책임진다. primary push 답은 mode 보존.
        멱등은 그대로 큐가 보장하므로 이 강제는 *큐에 넣기 전 값 보정*일 뿐 큐 도메인 무변경.
        """
        is_backup = ticket_id in self._backup_tickets
        if is_backup:
            answer = self._force_backup_mode(answer)
        # 큐에 회신 전 답(mode 보정 완료본)과 backup 여부를 보관해 두고 큐에 넣는다.
        # 큐 멱등(answered/expired 재submit 무시) — 검토 항목 생성도 같이 멱등화.
        answer_to_submit = answer
        self._queue.submit(ticket_id, answer_to_submit)
        # 생성 트리거(ADR 0012 결정 7): backup 답이 종착하면 검토 항목을 자동 생성한다.
        # "mode=backup 강제 하향"과 한 사건의 두 면 — 연결 등급이 진실이라 디스패처가
        # 여기서 책임(워커 자기보고 아님). review_store 미주입이면 no-op(하위호환).
        if is_backup and self._review_store is not None:
            self._add_review_item(ticket_id, answer_to_submit)
        # 종착 후 표식 정리 — 무한 누적 방지(경계 B). backup 표식과 primary 회수 신호
        # 둘 다 떨어낸다(종착한 작업은 더는 라우팅 대상이 아님). submit은 멱등(answered
        # 재submit 무시)이라 이 discard도 멱등.
        self._backup_tickets.discard(ticket_id)
        self._primary_exhausted.discard(ticket_id)

    @staticmethod
    def _force_backup_mode(answer: "Answer") -> "Answer":
        """답의 `mode`를 `backup`으로 덮은 새 Answer를 만든다(text·sources 보존).

        `Answer`는 frozen이라 새 인스턴스로 교체한다(파괴적 변경 X). 이미 backup이면 그대로.
        """
        from agent_org_network.runtime import Answer as _Answer  # 순환 import 회피

        if answer.mode == "backup":
            return answer
        return _Answer(text=answer.text, sources=answer.sources, mode="backup")

    def _add_review_item(self, ticket_id: str, answer: "Answer") -> None:
        """backup 답 종착 시 BackupReviewStore에 검토 항목을 추가한다(생성 트리거).

        ticket_id로 큐에서 해당 WorkTicket 메타(owner_id·agent_id·question)를 복원해
        BackupReviewItem을 구성한다. 위임 스냅샷이 있으면 snapshot_at을 싣고 없으면
        answered_at을 대신 쓴다(staleness 맥락, 결정 9 정신). 멱등: 이미 항목이 있으면
        덮지 않는다(submit 자체가 멱등이라 이중 add는 발생 안 하지만 방어).
        """
        from agent_org_network.review import BackupReviewItem as _BRI  # 순환 import 회피

        assert self._review_store is not None
        # ticket_id로 WorkTicket 메타 복원 — 큐에 보관된 스냅샷에서 가져온다.
        ticket_meta = self._queue.get_ticket(ticket_id)
        if ticket_meta is None:
            return  # 미존재 ticket(멱등 방어)
        # 위임 스냅샷이 있으면 그 snapshot_at, 없으면 clock으로 채운다.
        snapshot = self._delegations.get(ticket_meta.owner_id)
        snapshot_at = snapshot.snapshot_at if snapshot is not None else self._queue.now()
        answered_at = self._queue.now()

        item = _BRI(
            owner_id=ticket_meta.owner_id,
            agent_id=ticket_meta.agent_id,
            question=ticket_meta.question,
            backup_answer_text=answer.text,
            ticket_id=ticket_id,
            snapshot_at=snapshot_at,
            answered_at=answered_at,
            item_id=ticket_id,  # 1 답 1 검토 — ticket_id를 item_id로 재사용
        )
        # 멱등: 이미 동일 item_id로 추가된 항목이 있으면 건너뛴다.
        if self._review_store.get(ticket_id) is None:
            self._review_store.add(item)

    # ── WS 연결 생명주기(중앙 핸들러가 호출) ─────────────────────────────────

    def register(self, frame: RegisterWorker, send: SendFrame) -> CentralFrame:
        """워커 등록 — owner 신원 인증 hook 후 등급별 연결 레지스트리에 올린다.

        인증 통과면 `_connections[owner_id][role] = send`로 *등급별* 등록하고 `Welcome`을,
        실패면 `AuthError`를 돌려준다(6-5, 실 토큰 검증은 T6.5 — 지금은 거부 지점만).
        등급(`frame.role`)은 그 owner 안에서의 push 우선순위(ADR 0012 결정 2) — 같은 owner의
        primary와 backup이 따로 등록된다. 등록 직후 그 owner의 대기 작업이 있으면 우선순위에
        따라 push한다(연결 복구 시 재동기 — backup만 떠 있으면 backup으로, primary가 오면
        그때부터 primary로).
        """
        if not self._authenticate(frame):
            # 인증 거부 — 레지스트리에 올리지 않으므로 이후 작업이 push되지 않는다(6-5).
            return AuthError(reason="미인증 워커 — owner 신원 검증 실패")
        self._connections.setdefault(frame.owner_id, {})[frame.role] = send
        # 재연결 재동기: 등록 직후 그 owner의 대기 작업(미연결 동안 쌓인 것·끊김으로
        # re-queue된 것)을 우선순위 연결로 push한다.
        self._push_pending(frame.owner_id)
        return Welcome()

    def disconnect(self, owner_id: str, role: WorkerRole = "primary") -> list[WorkTicket]:
        """워커 끊김 처리 — 그 등급 연결을 제거하고 in-flight 작업을 re-queue한다.

        등급별 제거(ADR 0012 결정 2): `frame.role` 연결만 레지스트리에서 뺀다 — 같은 owner의
        다른 등급(예: primary 끊겨도 backup) 연결은 남는다. 하위호환: role 미지정이면 primary
        제거(기존 시그니처 `disconnect(owner_id)` 보존).

        결정 6-4(re-queue): 끊김 시 그 owner의 미회신 `claimed` 작업을 `_queue.release_claims`로
        큐에 되돌린다(claimed→queued, 단조성 보존). re-queue는 *owner 단위* 작업 회수라 등급과
        무관 — owner의 어느 워커가 끊겨도 그 owner의 claimed 작업을 되돌린다. 되돌린 뒤 그
        owner에 남은 연결이 있으면(우선순위로) 재push한다(예: primary 끊김→backup으로 재push).
        남은 연결이 없으면 큐 대기 → timeout이면 EscalatedToManager 종착(미아 없음). 반환:
        되돌린 ticket 목록.
        """
        conns = self._connections.get(owner_id)
        if conns is not None:
            conns.pop(role, None)
            if not conns:
                self._connections.pop(owner_id, None)
        if role == "primary":
            # primary 끊김 = t1 회수가 가리키던 "이 느린 primary"가 사라짐. "1회 한정 제외"
            # 표식은 *그 특정 primary*로 안 보낸다는 뜻이라 여기서 만료시킨다(결정 8-2 primary
            # 회복). 그래야 primary가 *재연결*될 때 이 작업이 다시 primary로 간다. 거부 경로가
            # 표식을 안 떼는 것과 짝 — 표식 만료의 단일 지점이 primary 연결 소멸이다.
            self._release_primary_exhausted(owner_id)
        released = self._queue.release_claims(owner_id)
        # 끊긴 워커의 작업을 그 owner에 남은 연결(우선순위)로 즉시 재push — 다른 등급이
        # 살아 있으면 미아 없이 바로 회복(예: primary 끊김 시 backup으로 전환).
        self._push_pending(owner_id)
        return released

    def _release_primary_exhausted(self, owner_id: str) -> None:
        """그 owner의 queued 작업에 걸린 `_primary_exhausted` 표식을 비운다(primary 회복).

        primary 연결이 사라질 때(`disconnect`) 호출 — "현재 그 primary로는 안 보낸다"는
        표식이 primary 부재로 의미를 잃으므로, 그 owner의 *아직 살아 있는*(queued) 작업의
        표식만 떼어 재연결 시 primary로 복귀시킨다. 종착(answered/expired) 작업은 표식이
        이미 submit/poll에서 정리되므로 대상 아님. owner 격리: 다른 owner 표식은 안 건드린다.
        """
        live = {t.ticket_id for t in self._queue.claimable(owner_id)}
        self._primary_exhausted -= live

    # ── 내부 전송 헬퍼 ───────────────────────────────────────────────────────

    def _authenticate(self, frame: RegisterWorker) -> bool:
        """owner 신원 인증 hook(ADR 0009 연결점, 6-5).

        실 토큰 검증은 T6.5 몫 — 지금은 *거부 지점만* 둔다. 빈 owner_id는 신원 미선언이라
        거부한다(미인증/익명 연결 차단의 최소 형태). token 검증 로직이 붙을 자리가 여기다.
        """
        return bool(frame.owner_id)

    def _push_pending(self, owner_id: str) -> None:
        """그 owner의 큐 대기 작업을 *ticket별* 우선순위 연결로 claim해 PushWork로 내보낸다.

        우선순위 선택(ADR 0012 결정 2·8·9): `claimable`로 그 owner의 queued 후보를 본 뒤
        ticket마다 `_select_connection(owner_id, ticket)`으로 대상을 고른다 — 보통 primary
        우선·없으면 backup이되, ① 그 ticket이 t1 회수분(`_primary_exhausted`)이면 primary를
        *건너뛰고* backup으로(결정 8), ② backup으로 가려는데 위임이 stale/부재/대상외면
        backup을 *거부*하고 큐에 그대로 둔다(결정 9). 선택이 None인 작업은 큐에 queued로
        남아(AwaitingWorker → timeout이면 EscalatedToManager, 미아 없음) *건너뛰고 다음
        후보로 진행*한다 — 거부 작업 하나가 뒤의 push 가능한 작업을 막지 않는다(head-of-line
        해소). ticket별로 다른 이유: 한 owner 큐에 새 작업(primary로)과 t1 회수분(backup으로)
        이 섞일 수 있어 owner 단위 단일 선택으로는 표현이 안 된다.

        포트 의미 보존(6-2): 워커가 claim을 직접 부르지 않고 중앙이 워커 대신 *그 ticket만*
        claim(`claim_ticket`)해 push. claim은 큐 도메인 전이(queued→claimed), push는 전송.
        backup으로 push한 작업은 `_backup_tickets`에 기록해 submit 시 mode=backup 강제의
        근거로 삼는다(결정 4). 거부로 push 못 한 작업은 queued 그대로 둬 다음 기회를 남긴다
        (claim하지 않으므로 회수가 불요 — 단조성·미아 없음 보존).

        무한루프 차단: `claimable` 스냅샷을 한 번 떠 순회하고, 거부 작업은 claim하지 않아
        같은 호출 안에서 다시 후보로 잡히지 않는다(push한 작업은 claimed라 다음 스냅샷에서
        빠짐 — 이 메서드는 스냅샷 1회 순회로 종료).
        """
        for ticket in self._queue.claimable(owner_id):
            tid = ticket.ticket_id
            # t1 회수분(primary 제외 요청)인지 — *현재 연결된 그 primary*로는 안 보낸다는
            # 표식(결정 8). 소비는 둘 중 하나에서만 일어난다: ① 이번에 실제로 push했을 때
            # (아래, 라우팅 확정), ② primary가 끊겼다 재연결됐을 때(`disconnect`가 비움 —
            # 새 primary는 그 느린 primary가 아니므로 "1회 한정 제외" 만료, 결정 8-2 primary
            # 회복). 거부(보낼 곳 없음)에선 *소비하지 않는다* — 소비하면 같은 느린 primary로
            # 즉시 되돌아가 회수가 무의미해진다(회수→primary→t1→회수 무한).
            exclude_primary = tid in self._primary_exhausted
            selected = self._select_connection(owner_id, ticket, exclude_primary=exclude_primary)
            if selected is None:
                # 보낼 곳이 없다(미연결·backup 거부) — claim하지 않고 queued로 둔 채 *건너뛴다*.
                # 거부 작업이 뒤의 push 가능한 작업을 막지 않게 다음 후보로 진행(head-of-line
                # 해소). 이 작업은 큐에 남아 자기 timeout으로 escalation(미아 없음). 표식은
                # 유지(위 ② primary 재연결 또는 timeout escalation 종착 시 정리).
                continue
            role, send = selected
            # 이 ticket을 *집어서* claim(queued→claimed). FIFO 첫 작업이 아니어도 된다.
            if not self._queue.claim_ticket(tid):
                # 경합 등으로 이미 claim 불가(queued 아님) — 건너뛴다(멱등·방어).
                continue
            if role == "backup":
                # 백업 연결로 처리된 작업 — submit 회신 시 mode=backup 강제(결정 4).
                self._backup_tickets.add(tid)
            else:
                # primary로 push: 마지막 push 등급이 진실이므로 backup 표식 해제(결정 4 정밀화).
                self._backup_tickets.discard(tid)
            # 어느 등급으로든 push했으면 그 ticket의 primary 회수 표식은 소진(이번 push가
            # 최신 라우팅 — 다음 회수 전까지 이 등급이 진실, "1회 한정 제외"의 정상 소비).
            self._primary_exhausted.discard(tid)
            send(PushWork(ticket=to_ticket_frame(ticket)))

    def _select_connection(
        self, owner_id: str, ticket: WorkTicket, exclude_primary: bool = False
    ) -> tuple[WorkerRole, SendFrame] | None:
        """그 owner의 push 대상 연결을 *이 ticket 기준* 우선순위로 고른다(조회 — 전이 없음).

        등급 라우팅의 단일 결정 지점(ADR 0012 결정 2·8·9) — push든 재동기든 t1 전환이든
        이 하나만 본다. 규칙:
          1. `exclude_primary`(이 ticket이 t1 회수분)면 primary를 건너뛴다(결정 8 — 느린
             primary로 되돌리지 않음). 아니면 primary 우선.
          2. backup으로 가려면 staleness·위임 대상 통과여야 한다(결정 9 — 위임 stale/부재/
             대상외면 backup 거부). primary는 이와 무관(stale은 backup 단계만 가른다).
          3. 어느 쪽도 못 고르면 None(큐 대기 → timeout escalation, 미아 없음).

        순수 조회: 표식(`_primary_exhausted`) 소비·claim 전이는 하지 않는다 — `_push_pending`이
        push 확정 시 표식을 소비·claim하고, primary 끊김 시 `disconnect`가 표식을 만료시킨다
        (거부 경로는 표식 유지 — 같은 느린 primary로 즉시 되돌아가지 않게).
        """
        conns = self._connections.get(owner_id)
        if not conns:
            return None
        # 1. primary — t1 회수 대상이 아니면 우선.
        primary = conns.get("primary")
        if primary is not None and not exclude_primary:
            return ("primary", primary)
        # 2. backup — staleness·위임 대상 통과 시에만.
        backup = conns.get("backup")
        if backup is not None and self._backup_allowed(owner_id, ticket):
            return ("backup", backup)
        return None

    def _backup_allowed(self, owner_id: str, ticket: WorkTicket) -> bool:
        """이 ticket을 그 owner backup으로 push해도 되는가 — 위임 정책 판정(ADR 0012 결정 9).

        `staleness_threshold` 미설정이면 항상 허용(하위호환 — 6.6-i의 위임 없는 backup
        push 보존). 설정됐으면 세 조건을 모두 통과해야 한다:
          1. 그 owner의 위임 스냅샷이 *있어야* 한다(opt-in 위임 — 없으면 backup 단계 건너뜀).
          2. 이 ticket의 `agent_id`가 위임 대상(`DelegationSnapshot.agent_ids`)에 들어야
             한다 — owner가 *그 담당 영역을 백업에 위임했을* 때만 backup이 그 영역을 답한다.
             위임 안 한 영역까지 backup이 owner 이름으로 답하면 안 된다(CONTEXT 위임 정의·
             "모르면 넘김"). 대상 외면 거부.
          3. snapshot_at이 임계 내 fresh여야 한다(stale 거부).
        하나라도 불통이면 거부 → 큐 대기 → timeout escalation("모르면 안전하게 넘긴다",
        PRD §3). primary는 이 판정과 무관(staleness·위임 대상은 backup 단계만 가른다).
        """
        if self._staleness_threshold is None:
            return True
        snapshot = self._delegations.get(owner_id)
        if snapshot is None:
            # 위임 자체가 없는 owner — backup 단계 건너뜀(결정 3·9).
            return False
        if ticket.agent_id not in snapshot.agent_ids:
            # 위임 대상 영역이 아님 — owner가 이 카드를 백업에 위임하지 않았다(결정 9·
            # CONTEXT 위임). backup이 모르는 영역을 답하지 않고 넘긴다.
            return False
        age = self._queue.now() - snapshot.snapshot_at
        return age <= self._staleness_threshold

    def _recover_stale_primary(self, owner_id: str) -> None:
        """그 owner의 t1 경과 primary claim을 회수해 backup으로 재전환한다(ADR 0012 결정 8).

        큐의 `stale_claims`로 *t1 경과한 claimed 작업만* queued로 되돌린 뒤(멀쩡한 primary
        진행분은 안 건드림), 그 ticket을 `_primary_exhausted`로 표시해 재push 시 primary를
        건너뛰고 backup으로 가게 한다. 그리고 `_push_pending`으로 재push한다 — backup이
        연결돼 있고 staleness 통과면 backup으로, 아니면 큐 대기(→ escalation). t1 미설정이면
        `stale_claims`가 빈 리스트라 no-op(단일 timeout 동작 그대로).
        """
        recovered = self._queue.stale_claims(owner_id)
        if not recovered:
            return
        for ticket in recovered:
            self._primary_exhausted.add(ticket.ticket_id)
        self._push_pending(owner_id)
