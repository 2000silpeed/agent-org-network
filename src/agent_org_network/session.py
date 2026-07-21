"""상태 세션 — Session 값 객체 + SessionStore 포트 + InMemorySessionStore (T9.1(a), ADR 0024).
SessionAskOrg — AskOrg 감싸기 래퍼 (T9.1(d), ADR 0024 결정 5).

기존 store 패턴(PrecedentStore·ConflictCaseStore·BackupReviewStore·ReevalStore)의
N번째 인스턴스 — 새 메커니즘 0.

불변식:
  - 전이≠기록: 세션 status 전이(active/ended)는 도메인; 트랜스크립트 적재는 별 축.
  - owner 격리: 세션은 사용자 귀속이지 조직 내부 미노출. 각 사용자 세션이 독립.
  - end 후 맥락(트랜스크립트) 비워짐: 끝난 세션은 빈 튜플(위생·노출 표면 축소).
  - 암묵 시작: 첫 메시지에 open_or_get이 세션을 열음(별도 시작 API 불필요).
  - SessionAskOrg 노출 불변식: 세션 층이 OrgReply에 아무것도 더하지 않는다.
  - Answered → 턴 적재 / Pending → 세션 열리되 턴 미적재(동기 Answered에만).
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from agent_org_network.ask_org import AskEvent, OrgReply
    from agent_org_network.user import User

SessionStatus = Literal["active", "ended"]

Clock = Callable[[], datetime]


def default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class SessionTurn:
    question: str
    answer_text: str
    answered_by: str
    at: datetime
    request_id: str | None = None

    def __post_init__(self) -> None:
        from agent_org_network.request_correlation import validate_optional_request_id

        validate_optional_request_id(self.request_id)

    @classmethod
    def for_request(
        cls,
        *,
        request_id: str,
        question: str,
        answer_text: str,
        answered_by: str,
        at: datetime,
    ) -> "SessionTurn":
        """Request-aware transcript 턴 생성 관문."""
        from agent_org_network.request_correlation import require_request_id

        return cls(
            question=question,
            answer_text=answer_text,
            answered_by=answered_by,
            at=at,
            request_id=require_request_id(request_id),
        )


@dataclass(frozen=True)
class Session:
    session_id: str
    user_id: str
    status: SessionStatus
    transcript: tuple[SessionTurn, ...]
    started_at: datetime
    last_active_at: datetime


class SessionStore(Protocol):
    def open_or_get(self, user_id: str) -> Session: ...

    def get(self, session_id: str) -> Session | None: ...

    def append_turn(self, session_id: str, turn: SessionTurn) -> Session: ...

    def end(self, session_id: str) -> Session | None: ...

    def active_for_user(self, user_id: str) -> Session | None: ...


class InMemorySessionStore:
    """in-memory 세션 저장소 — 색인: session_id(get) + user_id(active_for_user).

    BackupReviewStore·ConflictCaseStore와 같은 포트+InMemory 구조.
    active 세션만 _active에 보관; 종료된 세션은 _ended에 옮긴다.

    동시성: web.py 엔드포인트가 def(비 async)라 스레드풀에서 병렬 실행된다.
    _active/_active_by_user/_ended 공유 dict 접근은 전부 `_lock`(RLock)으로
    직렬화한다 — open_or_get의 idle 체크→_auto_end→새 세션 생성, append_turn의
    get→update 사이 TOCTOU 경합을 막는다(공개 시그니처·반환값·예외는 불변).
    """

    # 유휴 자동종료 임계(ADR 0024 결정 B 확정값 30분). 슬라이딩 만료 로직은 T9.1(c)에서
    # last_active_at 비교로 소비한다 — 현재 슬라이스(a)는 값객체+포트+InMemory까지라 미참조.
    IDLE_TIMEOUT_SECONDS: int = 30 * 60

    def __init__(self, clock: Clock = default_clock) -> None:
        self._clock = clock
        self._active: dict[str, Session] = {}
        self._active_by_user: dict[str, str] = {}
        self._ended: dict[str, Session] = {}
        self._lock = threading.RLock()

    def open_or_get(self, user_id: str) -> Session:
        with self._lock:
            session_id = self._active_by_user.get(user_id)
            if session_id and session_id in self._active:
                existing = self._active[session_id]
                if self._is_idle_expired(existing):
                    self._auto_end(existing)
                else:
                    return existing

            now = self._clock()
            new_session = Session(
                session_id=_new_session_id(),
                user_id=user_id,
                status="active",
                transcript=(),
                started_at=now,
                last_active_at=now,
            )
            self._active[new_session.session_id] = new_session
            self._active_by_user[user_id] = new_session.session_id
            return new_session

    def _auto_end(self, session: Session) -> None:
        ended = replace(session, status="ended", transcript=())
        del self._active[session.session_id]
        self._active_by_user.pop(session.user_id, None)
        self._ended[session.session_id] = ended

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._active.get(session_id) or self._ended.get(session_id)

    def append_turn(self, session_id: str, turn: SessionTurn) -> Session:
        with self._lock:
            existing = self._active.get(session_id)
            if existing is None:
                raise ValueError(f"활성 세션 없음: {session_id!r}")
            updated = replace(
                existing,
                transcript=existing.transcript + (turn,),
                last_active_at=self._clock(),
            )
            self._active[session_id] = updated
            return updated

    def end(self, session_id: str) -> Session | None:
        with self._lock:
            existing = self._active.get(session_id)
            if existing is None:
                return None
            ended = replace(existing, status="ended", transcript=())
            del self._active[session_id]
            self._active_by_user.pop(existing.user_id, None)
            self._ended[session_id] = ended
            return ended

    def _is_idle_expired(self, session: Session) -> bool:
        now = self._clock()
        elapsed = (now - session.last_active_at).total_seconds()
        return elapsed >= self.IDLE_TIMEOUT_SECONDS

    def active_for_user(self, user_id: str) -> Session | None:
        with self._lock:
            session_id = self._active_by_user.get(user_id)
            if session_id is None:
                return None
            return self._active.get(session_id)


IDLE_TIMEOUT_SECONDS = InMemorySessionStore.IDLE_TIMEOUT_SECONDS


class _AskHandle(Protocol):
    """SessionAskOrg 가 위임하는 handle 시그니처 최소 Protocol."""

    def handle(self, question: str, user: "User", *, context: str | None = None) -> "OrgReply": ...

    def handle_stream(
        self, question: str, user: "User", *, context: str | None = None
    ) -> "Iterator[AskEvent]": ...


class SessionAskOrg:
    """AskOrg + SessionStore 합성 래퍼 (T9.1(d), ADR 0024 결정 5).

    기존 AskOrg.handle을 수정하지 않고 *감싸기*로 세션 층을 붙인다.

    불변식:
      - 라우팅 종착 무변경: AskOrg.handle 결과를 그대로 반환(위임).
      - 노출 불변식: OrgReply에 아무것도 추가하지 않는다.
      - Answered → SessionTurn 적재(answered_by = agent_id = 튜플 index 1).
      - Pending → 세션 열리되 턴 미적재(dispatched 턴 적재는 후속 Task).
    """

    def __init__(
        self,
        ask: _AskHandle,
        session_store: SessionStore,
        clock: Clock = default_clock,
    ) -> None:
        self._ask = ask
        self._session_store = session_store
        self._clock = clock

    def handle(self, question: str, user: "User") -> "OrgReply":
        from agent_org_network.ask_org import Answered

        session = self._session_store.open_or_get(user.id)
        assembled = assemble_context(session, question)
        reply: OrgReply = self._ask.handle(question, user, context=assembled or None)

        if isinstance(reply, Answered):
            turn = SessionTurn(
                question=question,
                answer_text=reply.text,
                answered_by=reply.answered_by[1],
                at=self._clock(),
            )
            self._session_store.append_turn(session.session_id, turn)

        return reply

    def handle_stream(self, question: str, user: "User") -> "Iterator[AskEvent]":
        """스트리밍으로 위임하되 *완성 답*으로 1회 append_turn한다(ADR 0031 결정 2).

        `handle`의 형제 — 세션을 암묵 시작(open_or_get)하고 과거 턴만 맥락으로 조립해
        (assemble_context는 적재 전 호출이라 맥락 = 과거 턴만·0027 결정 7 보존) dispatch로만
        넘긴다. AskOrg.handle_stream의 이벤트를 그대로 흘리되, meta로 담당을·token으로
        완성 텍스트를 누적해 done을 만나면 *완성 답*으로 SessionTurn을 1회 적재한다(전이≠기록 —
        세션 적재는 스트림 종료 1회). Pending(비스트림)은 턴 미적재(동기 handle과 대칭).
        """
        from agent_org_network.ask_org import DoneEvent, MetaEvent, TokenEvent

        session = self._session_store.open_or_get(user.id)
        assembled = assemble_context(session, question)

        answered_by: str = ""
        text_parts: list[str] = []
        for event in self._ask.handle_stream(question, user, context=assembled or None):
            if isinstance(event, MetaEvent):
                answered_by = event.answered_by[1]
            elif isinstance(event, TokenEvent):
                text_parts.append(event.text)
            elif isinstance(event, DoneEvent):
                turn = SessionTurn(
                    question=question,
                    answer_text="".join(text_parts),
                    answered_by=answered_by,
                    at=self._clock(),
                )
                self._session_store.append_turn(session.session_id, turn)
            yield event


def assemble_context(session: Session, current_question: str) -> str:
    """그 사용자의 발화 스레드를 멀티턴 맥락으로 조립하는 순수 함수(IO 0).

    ADR 0024 결정 3: owner 격리·노출 불변식.
    - 그 사용자 발화 스레드만 포함(다른 사용자 발화·조직 내부 구조 미포함).
    - 종료 세션(transcript 빈 튜플)은 빈 맥락을 반환(맥락 누출 0).

    current_question은 이 함수 내부에서 맥락에 접지 않는다. 맥락은 과거 발화
    스레드(transcript)이고, 현재 질문은 호출자(build_provider_request의 messages)가
    별도로 붙인다. 시그니처에 둔 이유는 향후 list[messages] 조립 여지와 호출
    일관성 확보(ADR 0024 결정 3 시그니처 정합 — 제거 금지).
    """
    if not session.transcript:
        return ""
    parts: list[str] = []
    for turn in session.transcript:
        parts.append(f"User: {turn.question}")
        parts.append(f"Assistant: {turn.answer_text}")
    return "\n".join(parts)
