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
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from agent_org_network.dispatch import (
    Clock,
    DispatchOutcome,
    InMemoryWorkQueueDispatcher,
    WorkTicket,
    default_clock,
)

if TYPE_CHECKING:
    # 어댑터 시그니처 예고용 — 런타임 import 순환을 피해 타입 체크 시에만 끌어온다.
    from agent_org_network.agent_card import AgentCard
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
    실려 내려온다(결정 6-3, ADR 0011 Approval 연결점).
    """

    text: str
    sources: tuple[str, ...] = ()
    mode: Literal["draft_only", "full"] = "full"


# ── 워커→중앙(업스트림) 프레임 ───────────────────────────────────────────────


class RegisterWorker(_Frame):
    """연결 직후 1회 — 워커가 자기 owner 신원을 선언한다(인증 연결점, 6-5).

    `token`은 owner 신원 인증용(ADR 0009 → T6.5). 이번 슬라이스는 거부 *hook*만 두고
    실 토큰 검증은 T6.5. 중앙은 이 owner를 "연결됨"으로 레지스트리에 올린다.
    """

    type: Literal["register_worker"] = "register_worker"
    owner_id: str
    token: str | None = None


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
# 연결 레지스트리(`_connections`): owner_id → 그 워커의 send 콜백(WS로 프레임을 내보내는
# 함수). 중앙 WS 핸들러가 RegisterWorker를 받으면 이 레지스트리에 등록하고, dispatch로
# 새 작업이 들어오면 연결된 워커에게 push한다(미연결이면 큐에 대기 = 기존 AwaitingWorker,
# timeout이면 EscalatedToManager — 기존 결정 3 그대로 작동).


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

    동작은 2b-i에서 구현 완료 — 합성한 `_queue`(실 객체)가 `dispatch`/`poll`/`claim`/`submit`
    의 도메인을 소유하고, WS층은 그 위에 연결 레지스트리·push·release만 얹는다(큐 도메인을
    재작성하지 않는다는 결정 6-2의 보증).
    """

    def __init__(
        self,
        clock: Clock = default_clock,
        queue: InMemoryWorkQueueDispatcher | None = None,
    ) -> None:
        # 작업 큐 도메인은 합성으로 재사용 — 큐 상태기계·단조 종착·timeout escalation은
        # 이 객체가 소유한다(WS는 그 위 전송층). 주입 가능하게 둬 결정론 테스트가 고정
        # clock·timeout·manager_of를 박은 큐를 넣을 수 있게 한다(2b-i).
        self._queue = queue if queue is not None else InMemoryWorkQueueDispatcher(clock=clock)
        # owner_id → 그 워커 소켓으로 프레임을 내보내는 send 콜백(연결 레지스트리).
        # RegisterWorker 시 등록, 끊김/AuthError 시 제거. 미연결이면 작업은 큐에 대기.
        self._connections: dict[str, SendFrame] = {}

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
        """회신·대기·escalation 조회 — 합성한 `_queue.poll`에 그대로 위임한다.

        WS여도 결말 판정(Delivered/AwaitingWorker/EscalatedToManager)은 큐 도메인의 몫.
        사용자 답 회수(6-5)도 이 poll의 재노출이다(web 조회 엔드포인트가 ticket으로 호출).
        """
        return self._queue.poll(ticket)

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
        """
        self._queue.submit(ticket_id, answer)

    # ── WS 연결 생명주기(중앙 핸들러가 호출) ─────────────────────────────────

    def register(self, frame: RegisterWorker, send: SendFrame) -> CentralFrame:
        """워커 등록 — owner 신원 인증 hook 후 연결 레지스트리에 올린다.

        인증 통과면 `_connections[owner_id] = send`로 등록하고 `Welcome`을, 실패면
        `AuthError`를 돌려준다(6-5, 실 토큰 검증은 T6.5 — 지금은 거부 지점만). 등록 직후
        그 owner의 대기 작업이 있으면 push한다(연결 복구 시 재동기).
        """
        if not self._authenticate(frame):
            # 인증 거부 — 레지스트리에 올리지 않으므로 이후 작업이 push되지 않는다(6-5).
            return AuthError(reason="미인증 워커 — owner 신원 검증 실패")
        self._connections[frame.owner_id] = send
        # 재연결 재동기: 등록 직후 그 owner의 대기 작업(미연결 동안 쌓인 것·끊김으로
        # re-queue된 것)을 새 소켓으로 push한다.
        self._push_pending(frame.owner_id)
        return Welcome()

    def disconnect(self, owner_id: str) -> list[WorkTicket]:
        """워커 끊김 처리 — 레지스트리에서 제거하고 in-flight 작업을 re-queue한다.

        결정 6-4: 끊김 시 그 owner의 미회신 `claimed` 작업을 `_queue.release_claims`로
        큐에 되돌린다(claimed→queued, 단조성 보존 — answered/expired는 손대지 않음). 재연결
        시 다시 claim돼 push된다. 영영 안 돌아오면 timeout으로 EscalatedToManager 종착(미아
        없음). 반환: 되돌린 ticket 목록.
        """
        self._connections.pop(owner_id, None)
        return self._queue.release_claims(owner_id)

    # ── 내부 전송 헬퍼 ───────────────────────────────────────────────────────

    def _authenticate(self, frame: RegisterWorker) -> bool:
        """owner 신원 인증 hook(ADR 0009 연결점, 6-5).

        실 토큰 검증은 T6.5 몫 — 지금은 *거부 지점만* 둔다. 빈 owner_id는 신원 미선언이라
        거부한다(미인증/익명 연결 차단의 최소 형태). token 검증 로직이 붙을 자리가 여기다.
        """
        return bool(frame.owner_id)

    def _push_pending(self, owner_id: str) -> None:
        """그 owner가 연결돼 있으면 큐의 대기 작업을 모두 claim해 PushWork로 내보낸다.

        포트 의미 보존(6-2): 워커가 claim을 직접 부르지 않고 중앙이 워커 대신 claim해 push.
        claim은 큐 도메인 전이(queued→claimed), push는 전송. 미연결이면 no-op(작업은 큐에
        대기 = AwaitingWorker, timeout이면 EscalatedToManager — 큐 도메인이 처리).
        """
        send = self._connections.get(owner_id)
        if send is None:
            return
        # claim이 None일 때까지(대기 작업 소진) — 여러 작업이 쌓여 있으면 전부 push.
        while (ticket := self._queue.claim(owner_id)) is not None:
            send(PushWork(ticket=to_ticket_frame(ticket)))
