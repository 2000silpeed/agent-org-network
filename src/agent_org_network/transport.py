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

import secrets
import threading
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
from agent_org_network.knowledge_index import KnowledgeIndex
from agent_org_network.knowledge_sync import SyncKnowledge

if TYPE_CHECKING:
    # 어댑터 시그니처 예고용 — 런타임 import 순환을 피해 타입 체크 시에만 끌어온다.
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.audit import AuditLog
    from agent_org_network.console import ConsoleEvent, ConsoleFeed
    from agent_org_network.git_gateway import ChangeEventListener
    from agent_org_network.hitl import HitlToggleMap
    from agent_org_network.knowledge_store import KnowledgeStore
    from agent_org_network.knowledge_sync import KnowledgeSyncAck, KnowledgeSyncSpec
    from agent_org_network.notify import Notifier
    from agent_org_network.presence import PresenceLogStore, PresenceTracker
    from agent_org_network.registry import Registry
    from agent_org_network.review import BackupReviewStore
    from agent_org_network.runtime import AgentRuntime, Answer
    from agent_org_network.token import TokenStore
    from agent_org_network.two_stage_router import PublishedIndexStore
    from agent_org_network.worker_authorization import (
        DeliveryBinding,
        WorkerAuthorization,
        WorkerConnectionPrincipal,
    )


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

    `context`(ADR 0027 결정 13·T9.7 S1, 옵셔널 필드 추가·하위호환): 그 사용자의 발화
    스레드 — `owner_id`와 달리 연결로부터 복원 불가라 프레임에 *싣는다*. 미주입이면
    None(구버전 wire·단일턴 동형). ⚠️ 와이어 진화 규율(0027 결정 13) — `_Frame`이
    `extra="forbid"`라 *신버전 중앙 × 구버전 워커*는 이 필드가 실린 프레임을 거부한다
    (`ValidationError`). 롤아웃은 워커 선행(forward-compatible first)이어야 안전하다.

    `hitl`(ADR 0025 결정 4·5, T9.7 S2, 같은 옵셔널 진화 규율): 중앙 `HitlToggleMap`이
    dispatch 시점에 계산한 힌트 — 워커는 이 힌트를 *지시받아* 초안 즉시 전송/보류를
    가른다(토글 진실은 중앙, 워커는 소유하지 않음). 기본 False = 기존 즉시 전송 동작.
    """

    ticket_id: str
    agent_id: str
    question: str
    enqueued_at: datetime
    context: str | None = None
    hitl: bool = False


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


class PublishIndex(_Frame):
    """owner 워커가 자기 소유 agent의 KnowledgeIndex를 중앙에 배포하는 업스트림 프레임.

    워커가 *연결·인증 직후* 자기 로컬 OKF에서 `build_knowledge_index_from_okf`로 인덱스를
    도출해 송신한다(ADR 0028 §14 결정 A·E). 중앙은 받아 *보관만* 한다 — OKF 내용을 읽지
    않는다(비소유 강화). `SubmitAnswer`가 `answer: AnswerFrame`을 싣듯 `index: KnowledgeIndex`
    (frozen pydantic v2 값객체)를 통째로 싣는다 — 와이어 DTO를 따로 안 만든다(중첩 직렬화는
    pydantic이 자동 처리, `generated_at: datetime`은 `model_dump(mode="json")`로 ISO).

    봉투(`_Frame`)는 `extra="forbid"`(미지 필드 거부·봉투 무회귀)지만, 중첩 `index`
    (`KnowledgeIndex`)의 미지 필드 정책은 그 모델 현 상태(허용)를 그대로 둔다 — 인덱스
    스키마 진화에 여지를 남긴다(§14 결정 A 주의).

    owner는 프레임에 *다시 싣지 않는다* — 그 소켓이 곧 그 owner(`TicketFrame.owner_id`
    생략 정신). 중앙이 수용 전 *연결 세션의 인증 owner*와 `index.agent_id`의 card.owner를
    대조한다(워커-소유자 스코핑, §14 결정 B).
    """

    type: Literal["publish_index"] = "publish_index"
    index: KnowledgeIndex


class DocumentContent(_Frame):
    """워커가 요청받은 OKF 문서 본문을 회신하는 업스트림 프레임(ADR 0028 §15 결정 A·D).

    `FetchDocument`를 받은 워커가 `okf_root/{agent_id}/{concept_id}.md`를 읽어 본문을
    회신한다 — `SubmitAnswer`와 *같은 봉투*(워커→중앙·`request_id` echo로 correlation,
    `ticket_id` 멱등 키 정신). 워커는 **자기 소유 카드(`self._cards`에 그 agent_id)의
    문서만** 읽는다 — 미소유·파일 없음이면 `found=False`·`content=""`(사칭 차단·워커측
    1차 권한 게이트·예외 아닌 정상 회신, 결정 D). 중앙은 이 본문을 web 응답으로 *통과시킬*
    뿐 **보관하지 않는다**(비소유 중계·저장 0, 결정 E).
    """

    type: Literal["document_content"] = "document_content"
    request_id: str  # FetchDocument의 그 id(correlation echo)
    found: bool  # 파일이 있었나(없으면 found=False·content="")
    content: str = ""  # OKF 문서 본문(found=False면 빈 문자열)


class Heartbeat(_Frame):
    """연결 생존 신호(6-4) — 중앙이 워커별 마지막 수신 시각을 갱신한다."""

    type: Literal["heartbeat"] = "heartbeat"


class Ack(_Frame):
    """작업 수신 확인 — 중앙이 같은 ticket 재push를 멈춘다(at-least-once 흡수, 6-4)."""

    type: Literal["ack"] = "ack"
    ticket_id: str


WorkerFrame = (
    RegisterWorker | SubmitAnswer | PublishIndex | Heartbeat | Ack | DocumentContent | SyncKnowledge
)
#   워커→중앙 업스트림 프레임의 sealed 판별 유니온(type 필드로 갈림).
#   PublishIndex(§14)·DocumentContent(§15)는 추가 변이(새 type 키) — 기존 분기 무변경
#   (ADR 0028 §14 결정 A·§15 결정 A). _Frame extra="forbid"라 *추가*는 안전·*제거/이름변경*만
#   와이어 깨짐(되돌리기 어려움). §15는 양 union을 *동시에* 늘리는 첫 사례라 더 신중히 닫는다.
#   SyncKnowledge(Phase 12 (B)·ADR 0033 결정 3)는 지식 동기화 업스트림 변이(새 type 키
#   "sync_knowledge") — 같은 추가 규율(기존 분기 무회귀). 프레임 DTO 자체는 knowledge_sync.py가
#   소유하고 여기선 union에만 끼운다(server._parse_worker_frame이 그 elif로 복원).


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


class FetchDocument(_Frame):
    """인박스에서 owner가 연관 개념을 클릭한 순간 중앙이 그 문서 본문을 요청하는 다운스트림
    프레임(ADR 0028 §15 결정 A·C).

    `PushWork`와 *같은 봉투*(중앙→워커·소켓이 곧 그 owner). `concept_id = OKF 파일 stem`
    (`okf_index` 도출 규칙)이라 워커가 `okf_root/{agent_id}/{concept_id}.md`를 읽는다.
    `agent_id`를 *싣는다*(생략 안 함) — 한 owner가 여러 카드를 소유할 수 있어(§14 결정 B)
    어느 카드의 문서인지 워커가 알아야 한다(`TicketFrame.owner_id` 생략과 대비). `request_id`
    는 응답(`DocumentContent`)과 짝짓는 correlation 키(`ticket_id`가 `PushWork`↔`SubmitAnswer`
    를 짝짓는 정신, 결정 B).
    """

    type: Literal["fetch_document"] = "fetch_document"
    agent_id: str  # 어느 카드의 문서인가(OKF 디렉터리 키)
    concept_id: str  # OKF 파일 stem(okf_index: concept.id = 파일 stem)
    request_id: str  # 요청/응답 correlation 키(결정 B)


CentralFrame = Welcome | AuthError | PushWork | Ping | FetchDocument
#   중앙→워커 다운스트림 프레임의 sealed 판별 유니온.
#   FetchDocument(§15)는 추가 변이(새 type 키 "fetch_document") — 기존 4종 분기 무변경
#   (ADR 0028 §15 결정 A). 양 union 동시 진화의 다운스트림 쪽(업스트림 쪽은 DocumentContent).


# ── 프레임 ↔ 도메인 값 객체 변환(경계) ───────────────────────────────────────
#
# 프레임(와이어 DTO)과 코어 값 객체(WorkTicket·Answer) 사이를 핸들러가 경계에서 변환한다.
# 도메인 객체가 와이어 포맷에 오염되지 않게(전이 ≠ 전송) 변환을 한곳에 모은다.


def to_ticket_frame(ticket: WorkTicket, hitl: bool = False) -> TicketFrame:
    """`WorkTicket` → `TicketFrame`(push 전 와이어 투영, owner_id는 연결 귀속이라 생략).

    `context`는 `WorkTicket.context`를 그대로 싣는다(ADR 0027 결정 13). `hitl`은 큐 도메인
    밖의 값이라 `WorkTicket`엔 없다 — 호출자(디스패처)가 dispatch 시점에 계산해 주입한다
    (ADR 0025 결정 5, 토글 진실은 중앙). 기본 False = 기존 동작(하위호환).
    """
    return TicketFrame(
        ticket_id=ticket.ticket_id,
        agent_id=ticket.agent_id,
        question=ticket.question,
        enqueued_at=ticket.enqueued_at,
        context=ticket.context,
        hitl=hitl,
    )


def from_ticket_frame(frame: TicketFrame, owner_id: str) -> WorkTicket:
    """`TicketFrame` + 연결 owner_id → `WorkTicket`(워커 측 복원, context 왕복)."""
    return WorkTicket(
        owner_id=owner_id,
        agent_id=frame.agent_id,
        question=frame.question,
        enqueued_at=frame.enqueued_at,
        ticket_id=frame.ticket_id,
        context=frame.context,
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


# ── on-demand 문서 fetch correlation 결과 (ADR 0028 §15 결정 B) ───────────────


class FetchResult:
    """on-demand 문서 fetch의 결말 — "타입이 곧 상태" 정신(found/오프라인/타임아웃).

    web 핸들러가 이 결과를 보고 본문(또는 degradation 메시지)을 응답한다(결정 C·E). 중앙은
    본문을 *통과시킬* 뿐 보관하지 않는다(비소유 중계·저장 0). 세 결말:
      - `delivered`: 워커가 회신했다. `found`/`content`가 `DocumentContent`의 그대로.
      - `offline`: 담당 워커가 미연결이라 보낼 곳이 없었다(에러 아님·degradation, 결정 C).
      - `timeout`: 워커가 타임아웃 안에 응답하지 않았다(에러 아님·degradation, 결정 B).
    """

    def __init__(
        self,
        status: Literal["delivered", "offline", "timeout"],
        *,
        found: bool = False,
        content: str = "",
    ) -> None:
        self.status = status
        self.found = found
        self.content = content


class _FetchSlot:
    """request_id 1회용 correlation 슬롯 — 워커→중앙 `DocumentContent`를 web 핸들러로 잇는다.

    web 핸들러는 *동기 라우트*(FastAPI 스레드풀)에서 돌고 `recv_loop`는 이벤트 루프에서
    돈다 — 스레드 경계를 넘으므로 `threading.Event`로 대기/완료를 잇는다(asyncio.Future는
    다른 스레드에서 await 불가). 핸들러가 `event`로 블록하고, `recv_loop`가 본문을 채운 뒤
    `event.set()`으로 깨운다(set_result 정신). 1회용 — 완료/타임아웃 후 디스패처가 정리한다.
    """

    __slots__ = ("event", "frame")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.frame: "DocumentContent | None" = None


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
        notifier: "Notifier | None" = None,
        registry: "Registry | None" = None,
        published_index_store: "PublishedIndexStore | None" = None,
        propagator: "ChangeEventListener | None" = None,
        token_store: "TokenStore | None" = None,
        hitl_toggles: "HitlToggleMap | None" = None,
        console_feed: "ConsoleFeed | None" = None,
        presence_tracker: "PresenceTracker | None" = None,
        presence_log: "PresenceLogStore | None" = None,
        knowledge_store: "KnowledgeStore | None" = None,
        fallback_runtime: "AgentRuntime | None" = None,
        worker_authorization: "WorkerAuthorization | None" = None,
        worker_principal_resolver: "Callable[[RegisterWorker], WorkerConnectionPrincipal | None] | None" = None,
        worker_audit_log: "AuditLog | None" = None,
    ) -> None:
        # 작업 큐 도메인은 합성으로 재사용 — 큐 상태기계·단조 종착·timeout escalation은
        # 이 객체가 소유한다(WS는 그 위 전송층). 주입 가능하게 둬 결정론 테스트가 고정
        # clock·timeout·manager_of를 박은 큐를 넣을 수 있게 한다(2b-i).
        self._queue = queue if queue is not None else InMemoryWorkQueueDispatcher(clock=clock)
        # 관전 피드 사건의 타임스탬프용 시계(주입 clock 보관 — 결정론 테스트가 고정 clock).
        self._clock = clock
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
        # 실시간 push 통지(T7.4·ADR 0022 결정 4) — backup 답 종착 직후 owner에게 push.
        # 미주입이면 기존 동작(하위호환·게이트 보존). 비None이면 review_store.add 직후 발화.
        self._notifier = notifier
        # PublishIndex 수용 경로(ADR 0028 §14 결정 B·D·F) — 워커가 보낸 인덱스를 스코핑·
        # over-claim 필터·staleness put으로 보관한다. registry(card.owner·domains 권위)와
        # published_index_store(보관소)를 *둘 다* 주입받아야 수용한다(`accept_index`). 둘 중
        # 하나라도 미주입이면 publish 수용 안 함(no-op·하위호환 — publish 모르는 디스패처는
        # 기존 동작 그대로). 핵심 처리 로직은 `accept_published_index`(순수·결정론 단위 테스트)
        # 가 갖고, 디스패처는 *연결 세션의 인증 owner*를 묶어 그 함수로 위임한다.
        self._registry = registry
        self._published_index_store = published_index_store
        # 변경 전파 훅(ADR 0030 S4, T11.7e) — 실 WS 수신 경로(accept_index)가 더 새 인덱스를
        # 수용할 때 이 propagator에게 OkfChangeEvent를 1회 통지한다(accept_published_index에
        # 위임). None이면 발화 없음(하위호환 — publish 수용 자체는 그대로 동작).
        self._propagator = propagator
        # on-demand 문서 fetch correlation(ADR 0028 §15 결정 B): request_id → 1회용 슬롯.
        # web 핸들러가 `fetch_document`로 슬롯을 등록하고 FetchDocument를 push한 뒤 블록,
        # `resolve_fetch`(recv_loop)가 DocumentContent 도착 시 슬롯을 채워 깨운다. 완료/타임
        # 아웃 후 정리(1회용·누수 방지). 본문은 슬롯을 *거쳐 가기만* 할 뿐 디스패처에 남지
        # 않는다(비소유 중계·저장 0·캐시 0, 결정 E). _lock으로 등록/해소 경합을 막는다
        # (스레드풀 핸들러 ↔ 이벤트 루프 recv_loop).
        self._fetch_slots: dict[str, _FetchSlot] = {}
        self._fetch_lock = threading.Lock()
        # 워커 admission 실 검증(T9.5(b), ADR 0026 결정 2) — `RegisterWorker.token`을
        # `TokenStore.verify`로 검증한다. 미주입이면 `_authenticate`가 기존 stub 동작
        # (빈 owner_id만 거부)을 그대로 유지한다(하위호환 — 기존 WS 테스트 전부 보존).
        self._token_store = token_store
        # HITL 토글 진실 — 중앙 보유(ADR 0025 결정 5·T9.7 S2). `_push_pending`이 push 직전
        # `hitl.resolve_mode`(+`seed_from_card`)로 그 ticket의 hitl 힌트를 계산해 프레임에
        # 싣는다. 미주입이면 `is_on`이 항상 False인 것과 동형(힌트 항상 False = 기존 즉시
        # 전송 동작, 하위호환). 워커는 이 힌트를 *지시받아* 따를 뿐 토글을 소유하지 않는다.
        self._hitl_toggles = hitl_toggles
        # 운영자 콘솔 관전 피드(T9.2(c)·ADR 0024) — 워커 register 성공(WorkerConnected)·
        # 연결 종료(WorkerDisconnected) 시 ConsoleEvent를 emit한다(관전 미러). 미주입이면
        # 발화 0(하위호환·기존 WS 테스트 무회귀). emit 실패는 흡수(관전이 전송을 못 깬다).
        self._console_feed = console_feed
        # 프레즌스 추적기(Phase 12 (A)·ADR 0033 결정 5) — 워커 WS 연결/해제를 담당자
        # 온라인/오프라인 1급 상태로 도출한다. register 성공 시 observe_connect, disconnect
        # 시 observe_disconnect. 미주입이면 프레즌스 미배선(하위호환 — 기존 WS 테스트/경로는
        # 프레즌스 없이 그대로 동작하고 `_resolve_hitl_hint`도 프레즌스 결합 없이 기존
        # 계산으로 폴백). WS 연결이 사실상 하트비트라(0011·0012 `_connections` 재사용) 프레즌스는
        # 휘발이 정당하다 — 재시작하면 연결도 끊겨 있으므로 InMemory가 진실 원천으로 충분.
        self._presence_tracker = presence_tracker
        # 프레즌스 이력(Phase 13 SC2·ADR 0035·TRD §4) — 현재 상태 그릇(`_presence_tracker`)
        # 갱신과 별개로, connect/disconnect를 append-only `PresenceEvent` 이력으로도 남긴다
        # (온라인 비율 계산 원천 — `online_ratio`가 소비). 미주입이면 no-op(하위호환 — SC2
        # 전 기존 register/disconnect 경로는 이력 없이 그대로 동작).
        self._presence_log = presence_log
        # 중앙 지식 저장소(Phase 12 (B)(C)·ADR 0033 결정 1·3) — 워커가 동기화한 본문을
        # agent_id별 보관한다. SyncKnowledge 수신부(`accept_knowledge_sync_frame`)가
        # `accept_and_store_knowledge_sync`(M3 계약 — store.put 직접 호출 금지·판정과 보관
        # 분리) 경유로 이 스토어에 put한다. 미주입이면 지식 동기화 수용 안 함(no-op·하위호환).
        self._knowledge_store = knowledge_store
        # 오프라인 폴백 런타임(Phase 12 마지막 조합 지점·ADR 0033 결정 1·5) — 담당 워커가
        # 미연결(또는 backup 거부로 보낼 곳 없음)이라 `dispatch`가 작업을 push하지 못했을 때,
        # 중앙이 이 런타임으로 답을 대신 생성해 큐에 submit한다(→ 이어지는 poll이 Delivered).
        # 그래야 "담당자 PC 꺼져도 답변"이 인프로세스 경로뿐 아니라 *분산 배선*에서도 성립한다.
        # 이 런타임은 중앙 지식 저장소를 접지원으로 소비하는 실 공급자 어댑터(select_runtime)
        # 또는 결정론 StubRuntime(테스트). AgentRuntime 포트·Answer 계약 무변경 — dispatch가
        # 그 포트를 호출할 뿐이다. 미주입이면 폴백 없음(하위호환 — 미연결이면 기존대로 큐
        # 대기→timeout escalation, 노출은 dispatched). 워커가 연결돼 있으면 push가 성공해
        # 폴백이 발동하지 않는다(회귀 0 — 기존 워커 회신 경로 그대로).
        self._fallback_runtime = fallback_runtime
        # P17.8 S5 중앙 워커 모드는 세 dependency가 모두 있을 때만 의미가 있다.
        # 하나라도 빠진 조립은 legacy 토큰 검증으로 fallback하지 않고 register를 거부한다.
        self._worker_authorization = worker_authorization
        self._worker_principal_resolver = worker_principal_resolver
        self._worker_audit_log = worker_audit_log
        self._worker_central_mode = (
            worker_authorization is not None or worker_principal_resolver is not None
        )
        self._connection_principals: dict[str, dict[WorkerRole, WorkerConnectionPrincipal]] = {}
        self._delivery_bindings: dict[str, DeliveryBinding] = {}
        self._delivery_attempts: dict[str, int] = {}

    def _emit_console(self, event: "ConsoleEvent") -> None:
        """관전 피드에 사건을 1건 emit한다 — 실패는 흡수(관전이 전송을 못 깬다·T9.2(c))."""
        if self._console_feed is None:
            return
        try:
            self._console_feed.emit(event)
        except Exception:
            pass

    # ── 중앙측(질문 측): 내부 큐에 위임 ──────────────────────────────────────

    def dispatch(
        self,
        question: str,
        card: "AgentCard",
        context: str | None = None,
        grounding: str | None = None,
    ) -> WorkTicket:
        """작업을 큐에 적재하고, 그 owner 워커가 연결돼 있으면 즉시 push한다.

        큐 적재는 합성한 `_queue.dispatch`에 위임(도메인). 연결된 워커가 있으면 claim해
        PushWork를 send 콜백으로 내보낸다. 미연결이면 큐에 대기(기존 AwaitingWorker).
        context는 `_queue.dispatch(context=)`로 전파된다(ADR 0027 결정 13·T9.7 S1 — WS
        프레임 맥락 전파 실체화. 결정 8의 "인자 흡수(미전파)"를 이 슬라이스가 대체한다).

        grounding(ADR 0037 결정 3): 이번 슬라이스는 시그니처 정합만 — WS 프레임에는
        안 싣는다(실 KnowledgeStore 다중 조회·크로스머신 배선은 mcp-runtime 슬라이스 D).
        오프라인 폴백 경로(`fallback_runtime`)는 중앙이 직접 답을 생성하므로 grounding을
        그대로 전달한다.

        오프라인 폴백(Phase 12 마지막 조합 지점): `_push_pending` 뒤에도 이 ticket이 여전히
        `queued`면 담당 워커가 미연결(또는 backup 거부로 보낼 곳 없음)이라 push되지 못한
        것이다. `fallback_runtime`이 주입돼 있으면 중앙이 그 런타임으로 답을 대신 생성해 큐에
        submit한다 — 이어지는 `poll`이 `Delivered`를 돌려주므로 상위(`AskOrg`)의 기존 Delivered
        경로(Answered 투영·Approval 게이트·`_record_answer`의 presence 기반 needs_correction_review·
        감사 로그)가 그대로 태워진다(2라운드 배선 합류). 워커가 연결돼 push에 성공했으면
        status가 `claimed`라 폴백은 발동하지 않는다(회귀 0 — 기존 워커 회신 경로 그대로).
        """
        ticket = self._queue.dispatch(question, card, context=context)
        self._push_pending(card.owner)
        self._maybe_answer_with_fallback(ticket, card, context, grounding)
        return ticket

    def _maybe_answer_with_fallback(
        self,
        ticket: WorkTicket,
        card: "AgentCard",
        context: str | None,
        grounding: str | None = None,
    ) -> None:
        """담당 워커에 push 못 한 작업을 중앙 폴백 런타임으로 답한다(오프라인 폴백 합류점).

        발동 조건(둘 다 참일 때만): ① `fallback_runtime` 주입됨, ② `_push_pending` 뒤에도
        이 ticket이 여전히 `queued`(보낼 워커 없음·backup 거부). 워커가 연결돼 있으면
        `claimed`라 조기 반환(회귀 0). 답을 만들면 `_queue.submit`으로 큐에 회신해 정상
        answered 종착으로 흘려보낸다 — 별도 경로가 아니라 *같은 큐 도메인*에 합류하므로
        멱등(첫 답 고정)·단조 종착·미아 없음이 그대로 보존된다.

        폴백 답의 mode는 런타임이 낸 그대로(보통 full)다 — 노출·2라운드 판정은 상위
        `AskOrg`가 진다(오프라인이면 `_record_answer`가 presence_of로 needs_correction_review=True를
        찍는다). 여기선 "워커 미연결·폴백"이라는 내부 사실을 답에 심지 않는다(노출 불변식 —
        폴백 답도 담당·승인·출처만 싣는 일반 Answer로 큐에 들어간다). 런타임 예외는 흡수하지
        않는다 — 폴백이 실패하면 작업은 큐에 queued로 남아 기존 timeout escalation으로 종착한다
        (미아 없음). submit 자체가 멱등이라 중복 호출도 첫 답을 안 덮는다.
        """
        if self._fallback_runtime is None:
            return
        if self._queue.status_of(ticket.ticket_id) != "queued":
            # 워커로 push됨(claimed) — 기존 회신 경로가 답을 낸다(폴백 미발동).
            return
        answer = self._fallback_runtime.answer(
            ticket.question, card, context=context, grounding=grounding
        )
        self._queue.submit(ticket.ticket_id, answer)

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

    def bind_registry(self, registry: "Registry") -> None:
        """카드 권위(registry)만 사후 주입한다 — 지식 동기화 수용은 라우터 모드와 무관하다.

        `bind_published_index`는 index 모드(published store 존재)에서만 불려서, 기본 라우터
        모드의 통합 조립은 registry가 영영 미주입 → `accept_knowledge_sync_frame`이 no-op
        (ack 없음)으로 침묵했다(2026-07-05 크로스머신 시연이 잡은 실결함). SyncKnowledge
        수용의 스코핑(card.owner 대조)은 registry만 있으면 되므로 이 seam으로 분리한다.
        """
        self._registry = registry

    def bind_published_index(
        self,
        registry: "Registry",
        store: "PublishedIndexStore",
    ) -> None:
        """publish 수용에 필요한 registry·store를 사후 주입한다(라이브 배선, ADR 0028 §14 결정 F).

        디스패처가 `build_demo`(라우터 store 생성)보다 *먼저* 생성되는 통합 조립
        (`create_central_app`→`create_app`→`build_demo`) 때문에 생성자에 store를 못 넣는
        경우가 있다. 이 seam으로 *같은* store 인스턴스를 라우터·디스패처 양쪽에 꽂는다 —
        그래야 워커 publish(`accept_index`→`put`)가 라우터가 보는 store에 도달한다(T10.4
        Blocker B1: 미주입이면 `accept_index`가 무조건 False·no-op이라 publish가 버려짐).
        생성자 주입(`registry=`·`published_index_store=`)과 동등 — 결정론 테스트는 생성자로,
        통합 조립은 이 사후 바인딩으로 같은 인스턴스를 공유한다.
        """
        self._registry = registry
        self._published_index_store = store

    def bind_propagator(self, propagator: "ChangeEventListener") -> None:
        """reeval 변경 전파기를 사후 주입한다(라이브 배선, ADR 0030 S4, T11.7e).

        `bind_published_index`와 대칭인 닭-달걀 해소 seam이다 — `create_central_app`이
        디스패처를 `create_app`(→`build_demo`)보다 *먼저* 만들어야 하는데, 실 `StalenessPropagator`는
        `build_demo`가 만드는 실 `precedents`(빈 새 통이 아니라 판례가 실제로 담기는 그 store)를
        봐야 `find_by_primary`가 영향 판례를 찾는다. 그래서 `create_app`이 `build_demo` 완료
        *후* propagator를 구성해 이 seam으로 꽂는다 — 생성자 주입(`propagator=`)과 동등하되
        시점만 나중이다. 미호출이면 `self._propagator`는 생성자 기본값(`None`)에 머물러
        `accept_index`가 기존 동작 그대로(하위호환·발화 0) 이어간다.
        """
        self._propagator = propagator

    def accept_index(
        self,
        session_owner_id: str,
        frame: "PublishIndex",
        connection_principal: "WorkerConnectionPrincipal | None" = None,
    ) -> bool:
        """워커가 보낸 PublishIndex를 수용 처리한다 — 스코핑→필터→put(ADR 0028 §14 결정 F).

        WS 핸들러가 *연결 세션의 인증 owner*(`RegisterWorker.owner_id`)와 함께 호출한다 —
        owner는 프레임에 없다(소켓이 곧 그 owner). 처리 로직은 순수 함수
        `accept_published_index`(스코핑[B]·over-claim 필터[D]·staleness put[C])가 갖고, 이
        메서드는 주입된 `registry`·`published_index_store`를 묶어 위임한다. 둘 중 하나라도
        미주입이면 *수용 안 함*(no-op·False — publish 모르는 디스패처는 기존 동작 그대로).
        `self._propagator`도 함께 전달한다(ADR 0030 S4, T11.7e) — 실 WS 수신 경로에서 reeval
        인덱스-수용 훅이 발화하려면 이 전달이 있어야 한다(propagator=None이면 발화 없음,
        `accept_published_index`가 이미 흡수하는 하위호환).
        반환: 스코핑 통과로 put까지 갔으면 True·거부면 False(핸들러는 무시).
        """
        if self._registry is None or self._published_index_store is None:
            return False
        if self._worker_central_mode:
            if not self._authorize_worker_card_action(
                connection_principal, "worker.publish_index", frame.index.agent_id
            ):
                self._record_worker_action(
                    "worker.publish_index", frame.index.agent_id, connection_principal, "rejected"
                )
                return False
            if self._worker_audit_log is None:
                return False
        from agent_org_network.two_stage_router import accept_published_index

        accepted = accept_published_index(
            session_owner_id,
            frame.index,
            self._registry,
            self._published_index_store,
            propagator=self._propagator,
        )
        if self._worker_central_mode:
            self._record_worker_action(
                "worker.publish_index",
                frame.index.agent_id,
                connection_principal,
                "succeeded" if accepted else "rejected",
            )
        return accepted

    # ── 지식 동기화 수용 (Phase 12 (B)·ADR 0033 결정 3·M3 계약) ─────────────────

    def accept_knowledge_sync_frame(
        self,
        session_owner_id: str,
        frame: "SyncKnowledge",
        connection_principal: "WorkerConnectionPrincipal | None" = None,
    ) -> "KnowledgeSyncAck | None":
        """워커가 보낸 SyncKnowledge를 수용 처리한다 — 스코핑→admission→store put(M3 계약).

        WS 핸들러가 *연결 세션의 인증 owner*(`RegisterWorker.owner_id`)와 함께 호출한다 —
        owner는 프레임에 없다(소켓이 곧 그 owner·`PublishIndex.publishable` 정신 재사용).

        ⚠️ **M3 계약(code-reviewer·2026-07-04)**: `store.put`을 직접 호출하지 않고 반드시
        `accept_and_store_knowledge_sync`(knowledge_store.py) 경유한다 — admission 판정과
        보관을 한 조합 함수로 접합해 admission을 우회할 수 없게 한다(전이≠기록·수용 관문 단일화).

        **spec 전달 방식(mcp-runtime-engineer 결정·2026-07-04)**: `KnowledgeSyncSpec`(무엇을
        올릴지의 경계)을 프레임에 실린 `content`의 문서 경로 집합에서 *도출*한다
        (`_spec_from_content`). ADR/plan에 spec 전달의 구체 지침이 없어 가장 단순한 정합
        해법을 택했다 — 워커가 자기 시작 설정(`AON_KNOWLEDGE_PATHS`)으로 명시 지정한 경계
        안의 문서만 SyncKnowledge에 실으므로, 실린 content의 경로들이 곧 그 워커가 자기
        제한한 경계다. **Authority 우회가 아닌 이유**: spec은 "무엇을 올릴지"의 *자기 제한*
        이지 *권한 확장*이 아니다 — 실 권한 게이트는 `accept_knowledge_sync` 안의 워커-소유자
        스코핑(`card.owner == session_owner_id`)이 진다. 자기 제한을 프레임에서 도출해도
        권한을 넓힐 수 없다(스코핑이 owner 사칭을 이미 막고, 민감 필터가 본문을 검사한다).
        경로 자기보고로 넓힐 수 있는 것은 "자기 경계" 뿐인데 그건 이미 owner 자기 것이다.

        registry(card.owner 권위)·knowledge_store가 *둘 다* 주입돼야 수용한다. 하나라도
        미주입이면 None(수용 안 함·no-op·하위호환 — 지식 동기화 모르는 디스패처는 기존 동작).
        반환: 수용/거부를 담은 `KnowledgeSyncAck`(핸들러가 워커에 회신)·미배선이면 None.
        """
        if self._registry is None or self._knowledge_store is None:
            return None
        if self._worker_central_mode:
            if not self._authorize_worker_card_action(
                connection_principal, "worker.sync_knowledge", frame.content.agent_id
            ):
                self._record_worker_action(
                    "worker.sync_knowledge",
                    frame.content.agent_id,
                    connection_principal,
                    "rejected",
                )
                return self._rejected_sync_ack(frame)
            if self._worker_audit_log is None:
                return self._rejected_sync_ack(frame)
        try:
            card = self._registry.get(frame.content.agent_id)
        except KeyError:
            # 미등록 agent_id — admission이 card 없이 판정 불가. 거부 회신(등록 무결성).
            from agent_org_network.knowledge_sync import KnowledgeSyncAck

            return KnowledgeSyncAck(
                agent_id=frame.content.agent_id,
                accepted=False,
                reason=f"미등록 agent_id: {frame.content.agent_id!r}",
            )
        from agent_org_network.knowledge_store import accept_and_store_knowledge_sync

        spec = self._spec_from_content(frame)
        ack = accept_and_store_knowledge_sync(
            session_owner_id, frame, card, spec, self._knowledge_store
        )
        if self._worker_central_mode:
            self._record_worker_action(
                "worker.sync_knowledge",
                frame.content.agent_id,
                connection_principal,
                "succeeded" if ack.accepted else "rejected",
            )
        return ack

    @staticmethod
    def _rejected_sync_ack(frame: "SyncKnowledge") -> "KnowledgeSyncAck":
        from agent_org_network.knowledge_sync import KnowledgeSyncAck

        return KnowledgeSyncAck(
            agent_id=frame.content.agent_id, accepted=False, reason="동기화 권한 없음"
        )

    def _authorize_worker_card_action(
        self,
        principal: "WorkerConnectionPrincipal | None",
        action: str,
        agent_card_id: str,
    ) -> bool:
        authorization = self._worker_authorization
        if authorization is None or self._registry is None or principal is None:
            return False
        current = self._connection_principals.get(principal.owner_id, {}).get(principal.role)
        if current != principal:
            return False
        try:
            card = self._registry.get(agent_card_id)
        except KeyError:
            return False
        return (
            authorization.authorize_delivery(
                principal, action, agent_card_id=card.agent_id, current_owner_id=card.owner
            )
            == "allowed"
        )

    def _record_worker_action(
        self,
        action: str,
        agent_card_id: str,
        principal: "WorkerConnectionPrincipal | None",
        outcome: str,
    ) -> None:
        """비밀·본문 없이 성공/거부 절차 사건을 sink가 있을 때만 남긴다."""
        audit = self._worker_audit_log
        if audit is None or principal is None:
            return
        try:
            from agent_org_network.audit import action_record

            audit.record_action(
                action_record(
                    timestamp=self._clock(),
                    action=action,
                    subject_id=agent_card_id,
                    by=principal.owner_id,
                    detail={
                        "outcome": outcome,
                        "credential_id": principal.credential_id,
                        "credential_generation": principal.credential_generation,
                        "connection_epoch": principal.connection_epoch,
                    },
                )
            )
        except Exception:
            # append-only 기록 장애는 이미 끝난 전송/보관 mutation을 되돌리지 못한다.
            pass

    @staticmethod
    def _spec_from_content(frame: "SyncKnowledge") -> "KnowledgeSyncSpec":
        """프레임 content의 문서 경로들에서 `KnowledgeSyncSpec`을 도출한다(spec 자기 제한).

        각 문서 경로를 그대로 spec.paths로 삼는다 — 워커가 지정 경계 안의 문서만 실었다는
        전제(위 `accept_knowledge_sync_frame` docstring의 Authority 우회 반박 참조). 이러면
        지정 밖 경로 검사(admit_knowledge의 `_path_in_spec`)는 실린 문서들에 대해선 항상
        통과하지만, 민감 필터·워커-소유자 스코핑·agent_id 일치는 여전히 강제된다 — 즉
        "무엇을 올릴지"는 워커 자기 제한이고 "올릴 수 있는지"는 중앙 스코핑·필터가 지킨다.
        """
        from agent_org_network.knowledge_sync import KnowledgeSyncSpec

        return KnowledgeSyncSpec(
            agent_id=frame.content.agent_id,
            paths=tuple(doc.path for doc in frame.content.documents),
        )

    # ── on-demand 문서 fetch (ADR 0028 §15 결정 B·C·E) ───────────────────────

    def fetch_document(
        self, agent_id: str, concept_id: str, *, timeout: float = 5.0
    ) -> FetchResult:
        """`agent_id`의 owner 워커에서 그 개념 문서를 *그때* 끌어온다 — 동기 대기(결정 B·C).

        라우팅(결정 C): `registry.get(agent_id).owner`로 owner를 찾고(중앙 선언·Authority
        중앙) 그 owner의 연결(우선순위 primary→backup·`_select_connection` 정신)로
        `FetchDocument`를 push한다. fetch는 작업 큐 claim/submit과 무관(읽기 중계)이라 큐를
        안 통과 — 연결 레지스트리만 재사용한다. 미연결이면 보낼 곳이 없어 `offline`을
        돌려준다(에러 아님·우아한 degradation, 결정 C — 내용은 owner 환경에만).

        correlation(결정 B): `request_id`를 발급해 1회용 슬롯을 등록하고 `FetchDocument`를
        push한 뒤 `timeout`까지 블록한다(`recv_loop`가 `resolve_fetch`로 슬롯을 채워 깸).
        타임아웃이면 `timeout` 결말(에러 아님·degradation). 어느 경로든 슬롯은 정리한다
        (1회용·누수 방지). 본문은 슬롯을 거쳐 가기만 할 뿐 디스패처에 남지 않는다(비소유
        중계·저장 0, 결정 E).

        registry 미주입이면 owner를 못 찾으므로 `offline`(보낼 곳 없음·하위호환). 권한
        스코핑(요청 owner 자기 케이스)은 *web 핸들러*가 진다(결정 E·이중 게이트의 요청 측).
        """
        if self._registry is None:
            return FetchResult("offline")
        try:
            card = self._registry.get(agent_id)
        except KeyError:
            # 미등록 agent_id — 보낼 곳을 못 정한다(degradation, 미아 없음 무관).
            return FetchResult("offline")
        selected = self._select_fetch_connection(card.owner)
        if selected is None:
            # 담당 워커 미연결 — 우아한 degradation(결정 C).
            return FetchResult("offline")
        send = selected
        request_id = secrets.token_urlsafe(12)
        slot = _FetchSlot()
        with self._fetch_lock:
            self._fetch_slots[request_id] = slot
        try:
            send(FetchDocument(agent_id=agent_id, concept_id=concept_id, request_id=request_id))
            if not slot.event.wait(timeout):
                # 무응답 — 타임아웃 degradation(결정 B). 슬롯은 finally에서 정리.
                return FetchResult("timeout")
            frame = slot.frame
            if frame is None:
                # 깼는데 본문이 없다(방어) — 타임아웃과 같은 degradation 클래스로 둔다.
                return FetchResult("timeout")
            return FetchResult("delivered", found=frame.found, content=frame.content)
        finally:
            with self._fetch_lock:
                self._fetch_slots.pop(request_id, None)

    def resolve_fetch(self, frame: "DocumentContent") -> None:
        """워커가 보낸 `DocumentContent`를 그 `request_id` 슬롯에 채워 핸들러를 깨운다(결정 B).

        `recv_loop`(이벤트 루프)가 호출한다. 알 수 없는 `request_id`(타임아웃으로 이미
        정리됨·중복 도착·위조)는 *조용히 무시*한다 — 멱등(첫 도착이 슬롯을 채우고 깨우면
        web 핸들러가 정리하므로 이후 도착은 슬롯 없음). 본문은 슬롯에 잠깐 담겨 핸들러가
        읽고 가면 사라진다(비소유 중계·저장 0).
        """
        with self._fetch_lock:
            slot = self._fetch_slots.get(frame.request_id)
        if slot is None:
            return  # 미지/만료/중복 — 멱등 무시
        slot.frame = frame
        slot.event.set()

    def _select_fetch_connection(self, owner_id: str) -> SendFrame | None:
        """그 owner의 fetch push 대상 연결을 고른다 — primary 우선, 없으면 backup(결정 C).

        fetch는 작업 큐·등급 강제(mode=backup)·staleness 판정과 무관(읽기 중계라 답 신뢰
        하향이 없다) — 단순히 *연결된* 워커에 보낸다. primary가 있으면 primary, 없고 backup이
        있으면 backup, 둘 다 없으면 None(미연결 → offline degradation).
        """
        conns = self._connections.get(owner_id)
        if not conns:
            return None
        primary = conns.get("primary")
        if primary is not None:
            return primary
        return conns.get("backup")

    # ── 워커측(WS 핸들러가 호출): 전송 중계 ──────────────────────────────────

    def claim(self, owner_id: str) -> WorkTicket | None:
        """그 owner 큐의 다음 작업을 꺼낸다 — 합성한 `_queue.claim`에 위임.

        포트 의미 보존(6-2): WS에선 워커가 직접 부르지 않고 *중앙 핸들러가 워커 대신* 이걸
        호출해 PushWork로 내보낸다. 꺼냄(claimed 전이)은 큐 도메인, push는 전송.
        """
        return self._queue.claim(owner_id)

    def submit(
        self,
        ticket_id: str,
        answer: "Answer",
        connection_principal: "WorkerConnectionPrincipal | None" = None,
    ) -> None:
        """워커가 WS로 보낸 답을 큐에 회신 — 합성한 `_queue.submit`에 위임.

        멱등(6-4): 큐가 ticket_id 기준으로 보장(answered/expired 재submit 무시). 핸들러가
        SubmitAnswer 프레임을 받아 `from_answer_frame`으로 복원한 뒤 이걸 호출한다.

        등급 강제(ADR 0012 결정 4): 그 ticket이 *backup 연결로 push됐으면* `mode=backup`으로
        덮어 회신한다 — 워커가 full/draft_only로 보내도. 백업 답이라는 사실은 연결 등급이
        진실이라(워커 자기보고 아님) 디스패처가 책임진다. primary push 답은 mode 보존.
        멱등은 그대로 큐가 보장하므로 이 강제는 *큐에 넣기 전 값 보정*일 뿐 큐 도메인 무변경.
        """
        if self._worker_central_mode:
            # 등록 시점의 불변 principal이 아직 현재 연결이어야 한다. 동일 owner/role의
            # 재연결 뒤 옛 소켓이 새 세션 principal을 빌려 write하는 일을 막는다.
            if not self._is_current_connection_principal(connection_principal):
                return
            if not self._authorize_submit(ticket_id, connection_principal):
                return
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
        self._delivery_bindings.pop(ticket_id, None)

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
            self._push_backup_review_notification(item.owner_id, item.item_id, item.answered_at)

    def _push_backup_review_notification(
        self, owner_id: str, item_id: str, answered_at: datetime
    ) -> None:
        """BackupReviewItem add 직후 owner에게 push 통지를 1회 쏜다(T7.4·ADR 0022 결정 4).

        `notifier` 미주입이면 *아무것도 안 한다*(하위호환·게이트 보존). `owner_id`가 빈
        문자열이면 push 안 함(미귀속 가드 — 처리함 pull이 떠받침).
        """
        if self._notifier is None:
            return
        if not owner_id:
            return
        from agent_org_network.notify import Notification

        self._notifier.notify(
            Notification(
                recipient_id=owner_id,
                kind="backup_review_added",
                subject_ref=item_id,
                created_at=answered_at,
            )
        )

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
        principal = self._authenticate(frame)
        if principal is None:
            # 인증 거부 — 레지스트리에 올리지 않으므로 이후 작업이 push되지 않는다(6-5).
            return AuthError(reason="미인증 워커 — owner 신원 검증 실패")
        self._connections.setdefault(frame.owner_id, {})[frame.role] = send
        from agent_org_network.worker_authorization import WorkerConnectionPrincipal

        if type(principal) is WorkerConnectionPrincipal:
            self._connection_principals.setdefault(frame.owner_id, {})[frame.role] = principal
        # 관전 피드(T9.2(c)): 인증 통과·등록 성공 시에만 WorkerConnected emit(AuthError는
        # 위에서 이미 return되어 이 지점에 안 옴 — 관전엔 실제 연결 성립만 실린다).
        from agent_org_network.console import WorkerConnected

        self._emit_console(
            WorkerConnected(owner_id=frame.owner_id, role=frame.role, at=self._clock())
        )
        # 프레즌스 온라인 도출(Phase 12 (A)·ADR 0033 결정 5): 워커 WS 연결 성립 =
        # 담당자 온라인. owner_id를 프레즌스 키로 쓴다 — 프레즌스는 owner(담당자) 단위이지
        # 등급(primary/backup) 단위가 아니다(어느 워커든 그 owner가 붙어 있으면 온라인).
        # 미주입이면 no-op(하위호환). 관측 실패가 전송을 못 깨게 관전 emit과 같은 위치.
        if self._presence_tracker is not None:
            self._presence_tracker.observe_connect(frame.owner_id, at=self._clock())
        # 프레즌스 이력 append(Phase 13 SC2) — 상태 그릇 갱신과 별개로 온라인 비율 계산
        # 원천에도 남긴다. 미주입이면 no-op(하위호환).
        if self._presence_log is not None:
            from agent_org_network.presence import PresenceEvent

            self._presence_log.append(
                PresenceEvent(owner_id=frame.owner_id, status="online", at=self._clock())
            )
        # 재연결 재동기: 등록 직후 그 owner의 대기 작업(미연결 동안 쌓인 것·끊김으로
        # re-queue된 것)을 우선순위 연결로 push한다.
        self._push_pending(frame.owner_id)
        return Welcome()

    def disconnect(
        self,
        owner_id: str,
        role: WorkerRole = "primary",
        connection_principal: "WorkerConnectionPrincipal | None" = None,
    ) -> list[WorkTicket]:
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
        if self._worker_central_mode:
            if (
                connection_principal is None
                or connection_principal.owner_id != owner_id
                or connection_principal.role != role
                or not self._is_current_connection_principal(connection_principal)
            ):
                # 이전 epoch의 finally는 새 세션을 지우거나 claimed 작업을 release하지 않는다.
                return []
        conns = self._connections.get(owner_id)
        if conns is not None:
            conns.pop(role, None)
            if not conns:
                self._connections.pop(owner_id, None)
        principals = self._connection_principals.get(owner_id)
        if principals is not None:
            principals.pop(role, None)
            if not principals:
                self._connection_principals.pop(owner_id, None)
        # 프레즌스 오프라인 도출(Phase 12 (A)·ADR 0033 결정 5): 그 owner의 *모든* 등급
        # 연결이 사라졌을 때만 오프라인으로 도출한다 — primary가 끊겨도 backup이 남아 있으면
        # 그 담당자는 여전히 온라인(어느 워커든 붙어 있으면 온라인). grace period 없음(결정 6
        # 기본 — 연결 끊김 즉시 오프라인). 미주입이면 no-op(하위호환).
        if self._presence_tracker is not None and owner_id not in self._connections:
            self._presence_tracker.observe_disconnect(owner_id, at=self._clock())
        # 프레즌스 이력 append(Phase 13 SC2) — 상태 그릇과 같은 조건(전 등급 연결 소멸 시)
        # 에서만 offline 이벤트를 남긴다(상태 그릇·이력 원천 판정 일치). 미주입이면 no-op.
        if self._presence_log is not None and owner_id not in self._connections:
            from agent_org_network.presence import PresenceEvent

            self._presence_log.append(
                PresenceEvent(owner_id=owner_id, status="offline", at=self._clock())
            )
        # 관전 피드(T9.2(c)): 연결 종료 emit(끊김 정리 시점). re-queue·재push와 독립 —
        # 관전엔 "그 등급 연결이 끊겼다"는 사건만 실린다.
        from agent_org_network.console import WorkerDisconnected

        self._emit_console(WorkerDisconnected(owner_id=owner_id, role=role, at=self._clock()))
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

    def _authenticate(self, frame: RegisterWorker) -> "WorkerConnectionPrincipal | bool | None":
        """owner 신원 인증 — `TokenStore.verify` 실 검증(T9.5(b), ADR 0026 결정 2).

        빈 owner_id는 신원 미선언이라 항상 거부(기존 stub 동작 보존). `_token_store`가
        미주입이면 이후 토큰 검증을 하지 않는다(하위호환 — 기존 WS 테스트 전부 보존).

        주입돼 있으면 실 검증: `frame.token`이 None이면 즉시 거부(`TokenStore.verify`는
        None 방어를 하지 않으므로 호출 전 가드 필수 — code-reviewer T9.5a Minor 1 인계).
        `verify`가 None을 돌리면(만료·revoke·위조·미지) 거부. 통과해도 토큰의 owner_id·
        role이 `RegisterWorker` 선언과 *일치*해야 admission(owner 가장·등급 위조 차단).
        """
        if not frame.owner_id:
            return None
        if self._worker_central_mode:
            resolver = self._worker_principal_resolver
            authorization = self._worker_authorization
            if resolver is None or authorization is None or self._registry is None:
                return None
            try:
                principal = resolver(frame)
            except Exception:
                return None
            from agent_org_network.worker_authorization import WorkerConnectionPrincipal

            if type(principal) is not WorkerConnectionPrincipal:
                return None
            if principal.owner_id != frame.owner_id or principal.role != frame.role:
                return None
            if authorization.authorize_connection(principal) != "allowed":
                return None
            return principal
        if self._token_store is None:
            return True
        if frame.token is None:
            return None
        token = self._token_store.verify(frame.token, now=self._queue.now())
        if token is None:
            return None
        return True if token.owner_id == frame.owner_id and token.role == frame.role else None

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
            if self._worker_central_mode:
                from agent_org_network.worker_authorization import DeliveryBinding

                principal = self._connection_principals.get(owner_id, {}).get(role)
                if principal is None:
                    self._queue.release_claims(owner_id)
                    continue
                attempt = self._delivery_attempts.get(tid, 0) + 1
                self._delivery_attempts[tid] = attempt
                self._delivery_bindings[tid] = DeliveryBinding(
                    ticket_id=tid,
                    agent_card_id=ticket.agent_id,
                    owner_id=ticket.owner_id,
                    connection=principal,
                    attempt=attempt,
                )
            send(PushWork(ticket=to_ticket_frame(ticket, hitl=self._resolve_hitl_hint(ticket))))

    def connection_principal(
        self, owner_id: str, role: WorkerRole
    ) -> "WorkerConnectionPrincipal | None":
        """현재 살아 있는 중앙 모드 세션 principal만 반환한다."""
        return self._connection_principals.get(owner_id, {}).get(role)

    def _is_current_connection_principal(
        self, principal: "WorkerConnectionPrincipal | None"
    ) -> bool:
        """호출자가 보유한 중앙 모드 세션이 아직 현재 mapping인지 확인한다."""
        if principal is None:
            return False
        from agent_org_network.worker_authorization import WorkerConnectionPrincipal

        if type(principal) is not WorkerConnectionPrincipal:
            return False
        return (
            self._connection_principals.get(principal.owner_id, {}).get(principal.role) == principal
        )

    def _authorize_submit(
        self, ticket_id: str, principal: "WorkerConnectionPrincipal | None"
    ) -> bool:
        """submit 직전 현재 카드와 push binding을 재확인한다(답 write 전 fence)."""
        authorization = self._worker_authorization
        if authorization is None or self._registry is None or principal is None:
            return False
        ticket = self._queue.get_ticket(ticket_id)
        binding = self._delivery_bindings.get(ticket_id)
        if ticket is None or binding is None:
            return False
        try:
            card = self._registry.get(ticket.agent_id)
        except KeyError:
            return False
        if not authorization.verify_delivery_binding(
            binding,
            principal,
            ticket_id=ticket_id,
            agent_card_id=ticket.agent_id,
            current_owner_id=card.owner,
        ):
            return False
        return (
            authorization.authorize_delivery(
                principal,
                "worker.submit",
                agent_card_id=ticket.agent_id,
                current_owner_id=card.owner,
            )
            == "allowed"
        )

    def _resolve_hitl_hint(self, ticket: WorkTicket) -> bool:
        """이 ticket의 HITL 힌트(보류 여부)를 push 직전에 계산한다(ADR 0025 결정 5·T9.7 S2).

        토글 진실은 중앙(`HitlToggleMap`)에만 있다 — 워커는 이 힌트를 *지시받아* 따를 뿐
        소유하지 않는다. `hitl.resolve_mode`(+`seed_from_card`) 재사용: 카드 `approval_when`
        시드(있으면 True) OR 콘솔 토글(`is_on`)로 최종 힌트를 낸다. `_hitl_toggles`나
        `_registry`가 미주입이면 그 항목은 False로 본다(하위호환 — 기존 즉시 전송 동작).

        프레즌스 결합(Phase 12 (A)·ADR 0033 결정 5): `_presence_tracker`가 주입돼 있으면
        담당자 온라인/오프라인을 힌트 입력에 *OR로 더한다*(`presence_to_hitl` — 온라인이면
        사전 검토 상향). 워커가 온라인이라 이 push가 나가는 상황이므로 실제로는 온라인 분기가
        지배적이다(온라인=사전 검토). 이 결합은 *조이는* 방향만 한다 — 카드 approval_when이
        건 검토는 어떤 프레즌스에서도 안 풀린다(under-claim 단조성은 `resolve_mode`가 보존).
        미주입이면 프레즌스 결합 없이 기존 계산 그대로(회귀 0).
        """
        if self._hitl_toggles is None and self._presence_tracker is None:
            return False
        seeded = False
        if self._registry is not None:
            try:
                card = self._registry.get(ticket.agent_id)
            except KeyError:
                card = None
            if card is not None:
                from agent_org_network.hitl import seed_from_card

                seeded = seed_from_card(card)
        toggle_on = (
            self._hitl_toggles.is_on(ticket.agent_id) if self._hitl_toggles is not None else False
        )
        base_hint = seeded or toggle_on
        if self._presence_tracker is None:
            return base_hint
        from agent_org_network.presence import presence_to_hitl

        status = self._presence_tracker.status(ticket.owner_id)
        return base_hint or presence_to_hitl(status)

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
