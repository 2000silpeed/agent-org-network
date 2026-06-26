"""상태 세션 — Session 값 객체 + SessionStore 포트 + InMemorySessionStore (T9.1(a), ADR 0024).

기존 store 패턴(PrecedentStore·ConflictCaseStore·BackupReviewStore·ReevalStore)의
N번째 인스턴스 — 새 메커니즘 0.

불변식:
  - 전이≠기록: 세션 status 전이(active/ended)는 도메인; 트랜스크립트 적재는 별 축.
  - owner 격리: 세션은 사용자 귀속이지 조직 내부 미노출. 각 사용자 세션이 독립.
  - end 후 맥락(트랜스크립트) 비워짐: 끝난 세션은 빈 튜플(위생·노출 표면 축소).
  - 암묵 시작: 첫 메시지에 open_or_get이 세션을 열음(별도 시작 API 불필요).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal, Protocol

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
    """

    # 유휴 자동종료 임계(ADR 0024 결정 B 확정값 30분). 슬라이딩 만료 로직은 T9.1(c)에서
    # last_active_at 비교로 소비한다 — 현재 슬라이스(a)는 값객체+포트+InMemory까지라 미참조.
    IDLE_TIMEOUT_SECONDS: int = 30 * 60

    def __init__(self, clock: Clock = default_clock) -> None:
        self._clock = clock
        self._active: dict[str, Session] = {}
        self._active_by_user: dict[str, str] = {}
        self._ended: dict[str, Session] = {}

    def open_or_get(self, user_id: str) -> Session:
        session_id = self._active_by_user.get(user_id)
        if session_id and session_id in self._active:
            return self._active[session_id]

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

    def get(self, session_id: str) -> Session | None:
        return self._active.get(session_id) or self._ended.get(session_id)

    def append_turn(self, session_id: str, turn: SessionTurn) -> Session:
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
        existing = self._active.get(session_id)
        if existing is None:
            return None
        ended = replace(existing, status="ended", transcript=())
        del self._active[session_id]
        self._active_by_user.pop(existing.user_id, None)
        self._ended[session_id] = ended
        return ended

    def active_for_user(self, user_id: str) -> Session | None:
        session_id = self._active_by_user.get(user_id)
        if session_id is None:
            return None
        return self._active.get(session_id)
