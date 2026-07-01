"""SQLite durable 어댑터 — SqliteSessionStore·SqliteTokenStore (T9.8(a), ADR 0024·0026).

InMemory(`session.py`·`token.py`)와 동작 동치인 영속 구현. 프로세스 재시작에도
세션/토큰이 보존된다(재오픈→조회 durable). stdlib `sqlite3`만 쓴다(새 의존성 0).

`SubprocessGitGateway`의 tmp repo 통합 테스트 정신 — tmp-file DB로 통합 검증한다.

────────────────────────────────────────────────────────────────────────────
스키마 확정 (docs/tasks-v0.md T9.8(b) "SQLite 스키마 확정" 결정을 이 구현이 닫는다).

frozen 값 객체에서 도출:

  Session(session_id, user_id, status, transcript, started_at, last_active_at)
  SessionTurn(question, answer_text, answered_by, at)   ← Session 하위 컬렉션
  AdmissionToken(token_id, owner_id, role, token_hash, issued_at, expires_at,
                 revoked, revoked_at)

  ┌── sessions ────────────────────────────────────────────────────────────┐
  │ session_id     TEXT PRIMARY KEY   ← Session.session_id (get 색인)        │
  │ user_id        TEXT NOT NULL      ← Session.user_id                      │
  │ status         TEXT NOT NULL      ← "active" | "ended"                   │
  │ started_at     TEXT NOT NULL      ← ISO8601(tz-aware)                    │
  │ last_active_at TEXT NOT NULL      ← ISO8601 (idle 슬라이딩 비교 원천)     │
  └────────────────────────────────────────────────────────────────────────┘
    INDEX(user_id) WHERE status='active'  ← active_for_user 색인
      (InMemory 의 _active_by_user 에 해당 — user_id 당 active 1개 불변식은
       open_or_get 이 열기 전 기존 active 를 재사용/자동종료해 보장)

  ┌── session_turns ───────────────────────────────────────────────────────┐
  │ session_id  TEXT NOT NULL  ← FK sessions.session_id                     │
  │ turn_index  INTEGER NOT NULL  ← transcript 튜플 순서 보존(0..N-1)        │
  │ question    TEXT NOT NULL  ← SessionTurn.question                       │
  │ answer_text TEXT NOT NULL  ← SessionTurn.answer_text                    │
  │ answered_by TEXT NOT NULL  ← SessionTurn.answered_by                    │
  │ at          TEXT NOT NULL  ← SessionTurn.at ISO8601                     │
  │ PRIMARY KEY(session_id, turn_index)                                    │
  └────────────────────────────────────────────────────────────────────────┘
    transcript 는 tuple[SessionTurn,...] 이므로 별 테이블에 순서(turn_index)로
    적재한다. end/auto_end 시 transcript 를 비우는 의미는 이 테이블의 해당
    session_id 행 전삭제로 재현한다(end 후 맥락 비워짐 불변식·노출 표면 축소).

  ┌── tokens ──────────────────────────────────────────────────────────────┐
  │ token_id    TEXT PRIMARY KEY  ← AdmissionToken.token_id (revoke 색인)   │
  │ owner_id    TEXT NOT NULL     ← owner 귀속                              │
  │ role        TEXT NOT NULL     ← "primary" | "backup"                    │
  │ token_hash  TEXT NOT NULL     ← 해시만 저장(평문 미저장 불변식)          │
  │ issued_at   TEXT NOT NULL     ← ISO8601                                 │
  │ expires_at  TEXT              ← ISO8601 | NULL(만료 없음)               │
  │ revoked     INTEGER NOT NULL  ← 0 | 1 (append-only 표식·삭제 X)          │
  │ revoked_at  TEXT              ← ISO8601 | NULL                          │
  └────────────────────────────────────────────────────────────────────────┘
    UNIQUE INDEX(token_hash)  ← verify 해시 색인(InMemory _by_hash 대응)

도출 근거:
  - 모든 datetime 은 tz-aware ISO8601 TEXT 로 왕복(파싱 시 datetime.fromisoformat).
    값 객체가 timezone.utc aware 를 유지하므로 naive 로 떨어지지 않는다.
  - append-only revoke 는 행 삭제가 아니라 revoked 플래그 UPDATE(Precedent.invalidated
    패턴 — token.py 불변식과 동일).
  - 전이≠기록: durable 보관도 도메인 보관소지 절차(audit) 로그가 아니다.

동시성 (InMemory 에 방금 들어간 RLock 결정과 정합):
  web.py 엔드포인트가 def(비 async)라 스레드풀에서 병렬 실행된다. sqlite3 연결은
  스레드 안전하지 않으므로 `check_same_thread=False` 단일 연결 + `threading.RLock`
  으로 모든 접근을 직렬화한다(InMemory 의 _lock 결정과 동형). open_or_get 의
  idle 체크→auto_end→새 세션 생성, append_turn 의 get→update 사이 TOCTOU 를
  락으로 막는다. 공개 시그니처·반환값·예외는 InMemory 와 불변.
"""

from __future__ import annotations

import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_org_network.session import (
    Session,
    SessionTurn,
)
from agent_org_network.token import AdmissionToken, WorkerRole


def _default_clock() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return uuid.uuid4().hex


def _new_token_id() -> str:
    return uuid.uuid4().hex


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _parse_opt(value: str | None) -> datetime | None:
    return _parse(value) if value is not None else None


# ── SqliteSessionStore ──────────────────────────────────────────────────────

_SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    user_id        TEXT NOT NULL,
    status         TEXT NOT NULL,
    started_at     TEXT NOT NULL,
    last_active_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_active_user
    ON sessions(user_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS session_turns (
    session_id  TEXT NOT NULL,
    turn_index  INTEGER NOT NULL,
    question    TEXT NOT NULL,
    answer_text TEXT NOT NULL,
    answered_by TEXT NOT NULL,
    at          TEXT NOT NULL,
    PRIMARY KEY (session_id, turn_index)
);
"""


class SqliteSessionStore:
    """durable SessionStore — SQLite 백엔드(SqliteSessionStore, ADR 0024).

    InMemorySessionStore 와 동작 동치. 색인: session_id(get)·user_id(active_for_user).
    상태 전이(active→ended)·트랜스크립트 순서·유휴 슬라이딩(30분 주입 clock)·end 후
    맥락 비움을 SQL 로 재현한다. 프로세스 재시작에도 보존(재오픈→조회 durable).

    동시성: check_same_thread=False 단일 연결 + RLock 직렬화(InMemory _lock 정합).
    """

    IDLE_TIMEOUT_SECONDS: int = 30 * 60

    def __init__(
        self,
        db_path: str | Path,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SESSION_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── 공개 포트 ────────────────────────────────────────────────────────
    def open_or_get(self, user_id: str) -> Session:
        with self._lock:
            existing = self._active_row_for_user(user_id)
            if existing is not None:
                session = self._row_to_session(existing, load_transcript=True)
                if self._is_idle_expired(session):
                    self._auto_end(session)
                else:
                    return session

            now = self._clock()
            session = Session(
                session_id=_new_session_id(),
                user_id=user_id,
                status="active",
                transcript=(),
                started_at=now,
                last_active_at=now,
            )
            self._conn.execute(
                "INSERT INTO sessions"
                " (session_id, user_id, status, started_at, last_active_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    session.session_id,
                    session.user_id,
                    session.status,
                    _iso(session.started_at),
                    _iso(session.last_active_at),
                ),
            )
            self._conn.commit()
            return session

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_session(row, load_transcript=True)

    def append_turn(self, session_id: str, turn: SessionTurn) -> Session:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"활성 세션 없음: {session_id!r}")

            next_index = self._conn.execute(
                "SELECT COALESCE(MAX(turn_index) + 1, 0) AS n"
                " FROM session_turns WHERE session_id = ?",
                (session_id,),
            ).fetchone()["n"]
            self._conn.execute(
                "INSERT INTO session_turns"
                " (session_id, turn_index, question, answer_text, answered_by, at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    next_index,
                    turn.question,
                    turn.answer_text,
                    turn.answered_by,
                    _iso(turn.at),
                ),
            )
            new_last_active = self._clock()
            self._conn.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                (_iso(new_last_active), session_id),
            )
            self._conn.commit()
            return self._row_to_session(
                self._conn.execute(
                    "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
                ).fetchone(),
                load_transcript=True,
            )

    def end(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ? AND status = 'active'",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            self._clear_and_end(session_id)
            self._conn.commit()
            ended_row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            return self._row_to_session(ended_row, load_transcript=True)

    def active_for_user(self, user_id: str) -> Session | None:
        with self._lock:
            row = self._active_row_for_user(user_id)
            if row is None:
                return None
            return self._row_to_session(row, load_transcript=True)

    # ── 내부 헬퍼(락 보유 상태에서만 호출) ───────────────────────────────
    def _active_row_for_user(self, user_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE user_id = ? AND status = 'active'"
            " ORDER BY started_at DESC LIMIT 1",
            (user_id,),
        ).fetchone()

    def _auto_end(self, session: Session) -> None:
        self._clear_and_end(session.session_id)
        self._conn.commit()

    def _clear_and_end(self, session_id: str) -> None:
        # end 후 맥락(트랜스크립트) 비워짐 불변식 — 턴 전삭제 + status='ended'.
        self._conn.execute(
            "DELETE FROM session_turns WHERE session_id = ?", (session_id,)
        )
        self._conn.execute(
            "UPDATE sessions SET status = 'ended' WHERE session_id = ?",
            (session_id,),
        )

    def _is_idle_expired(self, session: Session) -> bool:
        elapsed = (self._clock() - session.last_active_at).total_seconds()
        return elapsed >= self.IDLE_TIMEOUT_SECONDS

    def _row_to_session(self, row: sqlite3.Row, *, load_transcript: bool) -> Session:
        transcript: tuple[SessionTurn, ...] = ()
        if load_transcript and row["status"] == "active":
            turn_rows = self._conn.execute(
                "SELECT question, answer_text, answered_by, at"
                " FROM session_turns WHERE session_id = ? ORDER BY turn_index ASC",
                (row["session_id"],),
            ).fetchall()
            transcript = tuple(
                SessionTurn(
                    question=t["question"],
                    answer_text=t["answer_text"],
                    answered_by=t["answered_by"],
                    at=_parse(t["at"]),
                )
                for t in turn_rows
            )
        return Session(
            session_id=row["session_id"],
            user_id=row["user_id"],
            status=row["status"],
            transcript=transcript,
            started_at=_parse(row["started_at"]),
            last_active_at=_parse(row["last_active_at"]),
        )


# ── SqliteTokenStore ────────────────────────────────────────────────────────

_TOKEN_SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    token_id    TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL,
    role        TEXT NOT NULL,
    token_hash  TEXT NOT NULL,
    issued_at   TEXT NOT NULL,
    expires_at  TEXT,
    revoked     INTEGER NOT NULL DEFAULT 0,
    revoked_at  TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
"""


def _default_token_factory() -> str:
    import secrets

    return secrets.token_urlsafe(32)


def _hash_token(raw: str) -> str:
    import hashlib

    return hashlib.sha256(raw.encode()).hexdigest()


class SqliteTokenStore:
    """durable TokenStore — SQLite 백엔드(SqliteTokenStore, ADR 0026).

    InMemoryTokenStore 와 동작 동치. 색인: token_hash(verify)·token_id(revoke).
    평문 미저장(해시만)·등록 무결성(만료/revoke/위조/없음→None)·append-only revoke·
    주입 clock/now seam 을 SQL 로 재현한다. 재시작에도 보존(재오픈→verify durable).

    동시성: check_same_thread=False 단일 연결 + RLock 직렬화(InMemory 정합).
    """

    def __init__(
        self,
        db_path: str | Path,
        token_factory: Callable[[], str] = _default_token_factory,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self._token_factory = token_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_TOKEN_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def issue(
        self,
        owner_id: str,
        role: WorkerRole,
        *,
        now: datetime,
        expires_in: timedelta | None = None,
    ) -> tuple[str, AdmissionToken]:
        raw = self._token_factory()
        token_hash = _hash_token(raw)
        token_id = _new_token_id()
        expires_at = (now + expires_in) if expires_in is not None else None

        token = AdmissionToken(
            token_id=token_id,
            owner_id=owner_id,
            role=role,
            token_hash=token_hash,
            issued_at=now,
            expires_at=expires_at,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO tokens"
                " (token_id, owner_id, role, token_hash, issued_at,"
                "  expires_at, revoked, revoked_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 0, NULL)",
                (
                    token.token_id,
                    token.owner_id,
                    token.role,
                    token.token_hash,
                    _iso(token.issued_at),
                    _iso(expires_at) if expires_at is not None else None,
                ),
            )
            self._conn.commit()
        return raw, token

    def verify(self, raw_token: str, *, now: datetime) -> AdmissionToken | None:
        token_hash = _hash_token(raw_token)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tokens WHERE token_hash = ?", (token_hash,)
            ).fetchone()
        if row is None:
            return None
        token = self._row_to_token(row)
        if token.revoked:
            return None
        if token.expires_at is not None and now >= token.expires_at:
            return None
        return token

    def revoke(self, token_id: str, *, now: datetime | None = None) -> AdmissionToken | None:
        """append-only revoke — 삭제 X·revoked=1 표식·멱등.

        now 주입 시 그 시각을 revoked_at 으로 찍는다(issue/verify seam 대칭·durable
        재현성). None이면 생성자 clock(InMemory 와 동형).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
            if row is None:
                return None
            token = self._row_to_token(row)
            if token.revoked:
                return token

            revoked_at = now if now is not None else self._clock()
            self._conn.execute(
                "UPDATE tokens SET revoked = 1, revoked_at = ? WHERE token_id = ?",
                (_iso(revoked_at), token_id),
            )
            self._conn.commit()
            updated = self._conn.execute(
                "SELECT * FROM tokens WHERE token_id = ?", (token_id,)
            ).fetchone()
            return self._row_to_token(updated)

    def list_active(self, now: datetime | None = None) -> list[AdmissionToken]:
        effective_now = now if now is not None else self._clock()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tokens WHERE revoked = 0"
            ).fetchall()
        result: list[AdmissionToken] = []
        for row in rows:
            token = self._row_to_token(row)
            if token.expires_at is not None and effective_now >= token.expires_at:
                continue
            result.append(token)
        return result

    def _row_to_token(self, row: sqlite3.Row) -> AdmissionToken:
        role: WorkerRole = "primary" if row["role"] == "primary" else "backup"
        return AdmissionToken(
            token_id=row["token_id"],
            owner_id=row["owner_id"],
            role=role,
            token_hash=row["token_hash"],
            issued_at=_parse(row["issued_at"]),
            expires_at=_parse_opt(row["expires_at"]),
            revoked=bool(row["revoked"]),
            revoked_at=_parse_opt(row["revoked_at"]),
        )
