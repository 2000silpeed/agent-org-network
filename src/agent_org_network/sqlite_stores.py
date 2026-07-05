"""SQLite durable 어댑터 — SqliteSessionStore·SqliteTokenStore (T9.8(a), ADR 0024·0026)
+ Phase 12 확장(SqliteAnswerRecordStore·SqliteCorrectionStore·SqliteKnowledgeStore·
SqliteRegistryJournal, ADR 0033·0034).

InMemory(`session.py`·`token.py`·`answer_record.py`·`knowledge_store.py`)와 동작
동치인 영속 구현. 프로세스 재시작에도 상태가 보존된다(재오픈→조회 durable).
stdlib `sqlite3`만 쓴다(새 의존성 0).

`SubprocessGitGateway`의 tmp repo 통합 테스트 정신 — tmp-file DB로 통합 검증한다.

────────────────────────────────────────────────────────────────────────────
Phase 12 확장 스키마 개요(각 클래스 docstring에 상세):

  answer_records   ← AnswerRecordStore 포트(append-only — add만, UPDATE 없음)
  correction_events← CorrectionStore 포트(append-only — append만, 삽입 순서 보존)
  answer_feedback  ← FeedbackStore 포트(upsert 최신 판정 + 이력 전량 보존 — 두 테이블)
  knowledge_bundles← KnowledgeStore 포트(upsert — put은 최신 version만 수용)
  registry_journal ← 카드 라이브 등록·오너 변경의 durable 저널(append-only).
                     Registry 자체는 SQLite화하지 않는다(YAML 시드 + InMemory
                     라이브 구조 유지) — 대신 mutation(register/transfer)을
                     저널로 남기고 재기동 시 `admin_registry.replay_registry_journal`
                     이 YAML 시드 Registry 위에 저널을 순서대로 재생한다(admission
                     경유 — 무효 카드는 복원되지 않는다 불변식 보존).

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

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from agent_org_network.answer_record import (
    AnswerFeedback,
    AnswerRecord,
    CorrectionEvent,
    FeedbackVerdict,
)
from agent_org_network.knowledge_sync import KnowledgeBundleContent, KnowledgeDoc
from agent_org_network.runtime import AnswerMode
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


# ── SqliteAnswerRecordStore — 답변 감사 단위 (Phase 12, ADR 0033) ──────────────

_ANSWER_RECORD_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_records (
    record_id               TEXT PRIMARY KEY,
    question                TEXT NOT NULL,
    answer_text              TEXT NOT NULL,
    answered_by              TEXT NOT NULL,
    agent_id                 TEXT NOT NULL,
    mode                      TEXT NOT NULL,
    session_id                TEXT,
    answered_at               TEXT NOT NULL,
    needs_correction_review  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_answer_records_agent ON answer_records(agent_id);
"""


class SqliteAnswerRecordStore:
    """durable `AnswerRecordStore` — SQLite 백엔드(Phase 12, ADR 0033 결정 4).

    `InMemoryAnswerRecordStore`와 동작 동치. append-only 계약 유지 — `add`는
    새 레코드를 삽입할 뿐 기존 `record_id`의 필드를 UPDATE하지 않는다(전이 ≠
    기록 — 나간 답의 감사 단위는 한 번 적재되면 불변). 동시성은 다른 sqlite
    스토어와 동형(check_same_thread=False 단일 연결 + RLock).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_ANSWER_RECORD_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(self, rec: AnswerRecord) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO answer_records"
                " (record_id, question, answer_text, answered_by, agent_id,"
                "  mode, session_id, answered_at, needs_correction_review)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    rec.record_id,
                    rec.question,
                    rec.answer_text,
                    rec.answered_by,
                    rec.agent_id,
                    rec.mode,
                    rec.session_id,
                    _iso(rec.answered_at),
                    int(rec.needs_correction_review),
                ),
            )
            self._conn.commit()

    def get(self, record_id: str) -> AnswerRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM answer_records WHERE record_id = ?", (record_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def for_agent(self, agent_id: str) -> list[AnswerRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM answer_records WHERE agent_id = ?", (agent_id,)
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, row: sqlite3.Row) -> AnswerRecord:
        mode: AnswerMode = row["mode"]
        return AnswerRecord(
            record_id=row["record_id"],
            question=row["question"],
            answer_text=row["answer_text"],
            answered_by=row["answered_by"],
            agent_id=row["agent_id"],
            mode=mode,
            session_id=row["session_id"],
            answered_at=_parse(row["answered_at"]),
            needs_correction_review=bool(row["needs_correction_review"]),
        )


# ── SqliteCorrectionStore — 사후 정정 이벤트 (Phase 12, ADR 0033) ─────────────

_CORRECTION_EVENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS correction_events (
    seq            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       TEXT NOT NULL UNIQUE,
    record_id      TEXT NOT NULL,
    corrected_text TEXT NOT NULL,
    by_owner       TEXT NOT NULL,
    rationale      TEXT NOT NULL DEFAULT '',
    corrected_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_correction_events_record ON correction_events(record_id);
"""


class SqliteCorrectionStore:
    """durable `CorrectionStore` — SQLite 백엔드(Phase 12, ADR 0033 결정 4).

    `InMemoryCorrectionStore`와 동작 동치. append-only — 원 `AnswerRecord`를
    건드리지 않고 새 이벤트만 쌓는다(UPDATE 없음). `seq`(AUTOINCREMENT)로
    삽입 순서를 보존해 `for_record`가 append 순서 그대로 돌려준다(전이 ≠ 기록,
    `CorrectionEvent`는 새 인스턴스로만 증가한다 — 기존 이벤트 불변).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_CORRECTION_EVENT_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append(self, event: CorrectionEvent) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO correction_events"
                " (event_id, record_id, corrected_text, by_owner, rationale,"
                "  corrected_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.record_id,
                    event.corrected_text,
                    event.by_owner,
                    event.rationale,
                    _iso(event.corrected_at),
                ),
            )
            self._conn.commit()

    def for_record(self, record_id: str) -> list[CorrectionEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM correction_events WHERE record_id = ? ORDER BY seq ASC",
                (record_id,),
            ).fetchall()
        return [self._row_to_event(r) for r in rows]

    def get(self, event_id: str) -> CorrectionEvent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM correction_events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_event(row)

    def _row_to_event(self, row: sqlite3.Row) -> CorrectionEvent:
        return CorrectionEvent(
            event_id=row["event_id"],
            record_id=row["record_id"],
            corrected_text=row["corrected_text"],
            by_owner=row["by_owner"],
            rationale=row["rationale"],
            corrected_at=_parse(row["corrected_at"]),
        )


# ── SqliteFeedbackStore — 질문자 답변 피드백 (계획 §10, ADR 0033 계열) ────────

_ANSWER_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS answer_feedback_latest (
    record_id    TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    comment      TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL,
    PRIMARY KEY (record_id, submitted_by)
);
CREATE TABLE IF NOT EXISTS answer_feedback_history (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id    TEXT NOT NULL,
    submitted_by TEXT NOT NULL,
    verdict      TEXT NOT NULL,
    comment      TEXT NOT NULL DEFAULT '',
    submitted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_answer_feedback_history_record
    ON answer_feedback_history(record_id);
"""


class SqliteFeedbackStore:
    """durable `FeedbackStore` — SQLite 백엔드(계획 §10.2 "최신 우선(upsert), 이력 보존").

    `InMemoryFeedbackStore`와 동작 동치. 두 테이블로 멱등 정책을 재현한다:
      - `answer_feedback_latest` — `(record_id, submitted_by)` PK upsert. 같은
        질문자가 같은 답에 재제출하면 최신 verdict/comment로 *판정*을 덮는다
        (`latest_for_record`가 record별 최신을 고르는 원천).
      - `answer_feedback_history` — append-only(seq AUTOINCREMENT). 모든 제출을
        전량 보존한다(`for_record`가 append 순서 그대로 돌려줌 — 전이 ≠ 기록:
        판정은 최신, 기록은 전량).

    동시성은 다른 sqlite 스토어와 동형(check_same_thread=False 단일 연결 + RLock).
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_ANSWER_FEEDBACK_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def upsert(self, fb: AnswerFeedback) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO answer_feedback_latest"
                " (record_id, submitted_by, verdict, comment, submitted_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(record_id, submitted_by) DO UPDATE SET"
                "   verdict = excluded.verdict,"
                "   comment = excluded.comment,"
                "   submitted_at = excluded.submitted_at",
                (
                    fb.record_id,
                    fb.submitted_by,
                    fb.verdict,
                    fb.comment,
                    _iso(fb.submitted_at),
                ),
            )
            self._conn.execute(
                "INSERT INTO answer_feedback_history"
                " (record_id, submitted_by, verdict, comment, submitted_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    fb.record_id,
                    fb.submitted_by,
                    fb.verdict,
                    fb.comment,
                    _iso(fb.submitted_at),
                ),
            )
            self._conn.commit()

    def latest_for_record(self, record_id: str) -> AnswerFeedback | None:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM answer_feedback_latest WHERE record_id = ?",
                (record_id,),
            ).fetchall()
        if not rows:
            return None
        return max(
            (self._row_to_feedback(r) for r in rows),
            key=lambda fb: fb.submitted_at,
        )

    def for_record(self, record_id: str) -> list[AnswerFeedback]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM answer_feedback_history"
                " WHERE record_id = ? ORDER BY seq ASC",
                (record_id,),
            ).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    def _row_to_feedback(self, row: sqlite3.Row) -> AnswerFeedback:
        verdict: FeedbackVerdict = "good" if row["verdict"] == "good" else "bad"
        return AnswerFeedback(
            record_id=row["record_id"],
            verdict=verdict,
            comment=row["comment"],
            submitted_by=row["submitted_by"],
            submitted_at=_parse(row["submitted_at"]),
        )


# ── SqliteKnowledgeStore — 중앙 지식 저장소 본문 (Phase 12, ADR 0033) ─────────

_KNOWLEDGE_BUNDLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS knowledge_bundles (
    agent_id   TEXT PRIMARY KEY,
    documents  TEXT NOT NULL,
    version    TEXT NOT NULL,
    synced_at  TEXT NOT NULL
);
"""


class SqliteKnowledgeStore:
    """durable `KnowledgeStore` — SQLite 백엔드(Phase 12, ADR 0033 결정 1·3).

    `InMemoryKnowledgeStore`와 동작 동치. `put`은 순수 보관(전이 아님)이라
    *최신 version만 수용하는 upsert*가 정당하다(감사 로그가 아니라 "지금 이
    agent_id의 최신 본문" 하나만 보관하는 그릇 — append-only 계약 대상이 아님).
    `documents`(튜플)는 JSON 배열로 직렬화한다.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_KNOWLEDGE_BUNDLE_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def put(self, content: KnowledgeBundleContent) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT version, synced_at FROM knowledge_bundles WHERE agent_id = ?",
                (content.agent_id,),
            ).fetchone()
            if row is not None:
                if content.version == row["version"]:
                    return
                if content.synced_at < _parse(row["synced_at"]):
                    return
            docs_json = json.dumps(
                [{"path": d.path, "body": d.body} for d in content.documents],
                ensure_ascii=False,
            )
            self._conn.execute(
                "INSERT INTO knowledge_bundles (agent_id, documents, version, synced_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(agent_id) DO UPDATE SET"
                "   documents = excluded.documents,"
                "   version = excluded.version,"
                "   synced_at = excluded.synced_at",
                (
                    content.agent_id,
                    docs_json,
                    content.version,
                    _iso(content.synced_at),
                ),
            )
            self._conn.commit()

    def get(self, agent_id: str) -> KnowledgeBundleContent | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM knowledge_bundles WHERE agent_id = ?", (agent_id,)
            ).fetchone()
        if row is None:
            return None
        return self._row_to_content(row)

    def is_stale(self, agent_id: str, *, now: datetime, threshold_s: int) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT synced_at FROM knowledge_bundles WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
        if row is None:
            return True
        elapsed = (now - _parse(row["synced_at"])).total_seconds()
        return elapsed > threshold_s

    def _row_to_content(self, row: sqlite3.Row) -> KnowledgeBundleContent:
        raw_docs: list[dict[str, str]] = json.loads(row["documents"])
        documents = tuple(
            KnowledgeDoc(path=d["path"], body=d["body"]) for d in raw_docs
        )
        return KnowledgeBundleContent(
            agent_id=row["agent_id"],
            documents=documents,
            version=row["version"],
            synced_at=_parse(row["synced_at"]),
        )


# ── SqliteRegistryJournal — 카드 라이브 등록·오너 변경 durable 저널 ───────────
# (Phase 12, ADR 0034 결정 1·2 — "AON_DB 켜면 SQLite 영속")

RegistryJournalKind = Literal["register", "transfer"]


@dataclass(frozen=True)
class RegistryJournalCandidate:
    """저널에 적재하는 카드 후보 값 — `admin_registry.CardCandidate`의 durable 투영.

    `sqlite_stores.py`는 `admin_registry.py`를 import하지 않는다(순환 회피 —
    `admin_registry.py`가 이 모듈의 `SqliteRegistryJournal`을 참조하는 방향
    하나로 의존을 고정한다). 그래서 `CardCandidate`와 같은 필드를 이 모듈
    안에서 독립적으로 들고, `admin_registry.replay_registry_journal`이
    `CardCandidate(**entry.candidate.__dict__)`로 변환해 admission에 태운다.
    """

    agent_id: str
    owner: str
    team: str
    summary: str
    domains: tuple[str, ...]
    last_reviewed_at: str
    maintainer: str | None = None
    can_answer: tuple[str, ...] = ()
    cannot_answer: tuple[str, ...] = ()
    approval_when: tuple[str, ...] = ()
    collaborate_when: tuple[str, ...] = ()
    knowledge_sources: tuple[str, ...] = ()
    trust_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegistryJournalEntry:
    """저널 한 줄 — `register`(신규 등록) | `transfer`(오너 변경) + 카드 후보 + 메타."""

    kind: RegistryJournalKind
    candidate: RegistryJournalCandidate
    by: str
    at: datetime


_REGISTRY_JOURNAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS registry_journal (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    candidate  TEXT NOT NULL,
    by         TEXT NOT NULL,
    at         TEXT NOT NULL
);
"""


class SqliteRegistryJournal:
    """카드 라이브 등록·오너 변경의 durable 저널(append-only, ADR 0034 결정 1·2).

    Registry 자체를 SQLite화하지 않는다 — 기존 "InMemory Registry + YAML 시드"
    구조를 유지하되, 라이브 mutation(등록·오너 변경)만 이 저널에 순서대로 남긴다.
    중앙 기동 시 YAML 시드 로드 → `admin_registry.replay_registry_journal`이 이
    저널을 처음부터 순서대로 재생해 라이브 상태를 복원한다(admission 경유 —
    무효 카드/오너는 복원되지 않는다).

    `AdminRegistryService(journal_sink=...)`로 주입하면 `register_card`·
    `transfer_ownership` 성공 시 자동으로 이 저널에 append된다.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_REGISTRY_JOURNAL_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def append_register(
        self,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str] | tuple[str, ...],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None = None,
        can_answer: list[str] | tuple[str, ...] = (),
        cannot_answer: list[str] | tuple[str, ...] = (),
        approval_when: list[str] | tuple[str, ...] = (),
        collaborate_when: list[str] | tuple[str, ...] = (),
        knowledge_sources: list[str] | tuple[str, ...] = (),
        trust_labels: list[str] | tuple[str, ...] = (),
    ) -> None:
        self._append(
            "register",
            agent_id=agent_id,
            owner=owner,
            team=team,
            summary=summary,
            domains=domains,
            last_reviewed_at=last_reviewed_at,
            by=by,
            at=at,
            maintainer=maintainer,
            can_answer=can_answer,
            cannot_answer=cannot_answer,
            approval_when=approval_when,
            collaborate_when=collaborate_when,
            knowledge_sources=knowledge_sources,
            trust_labels=trust_labels,
        )

    def append_transfer(
        self,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str] | tuple[str, ...],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None = None,
        can_answer: list[str] | tuple[str, ...] = (),
        cannot_answer: list[str] | tuple[str, ...] = (),
        approval_when: list[str] | tuple[str, ...] = (),
        collaborate_when: list[str] | tuple[str, ...] = (),
        knowledge_sources: list[str] | tuple[str, ...] = (),
        trust_labels: list[str] | tuple[str, ...] = (),
    ) -> None:
        self._append(
            "transfer",
            agent_id=agent_id,
            owner=owner,
            team=team,
            summary=summary,
            domains=domains,
            last_reviewed_at=last_reviewed_at,
            by=by,
            at=at,
            maintainer=maintainer,
            can_answer=can_answer,
            cannot_answer=cannot_answer,
            approval_when=approval_when,
            collaborate_when=collaborate_when,
            knowledge_sources=knowledge_sources,
            trust_labels=trust_labels,
        )

    def _append(
        self,
        kind: RegistryJournalKind,
        *,
        agent_id: str,
        owner: str,
        team: str,
        summary: str,
        domains: list[str] | tuple[str, ...],
        last_reviewed_at: str,
        by: str,
        at: datetime,
        maintainer: str | None,
        can_answer: list[str] | tuple[str, ...],
        cannot_answer: list[str] | tuple[str, ...],
        approval_when: list[str] | tuple[str, ...],
        collaborate_when: list[str] | tuple[str, ...],
        knowledge_sources: list[str] | tuple[str, ...],
        trust_labels: list[str] | tuple[str, ...],
    ) -> None:
        candidate_json = json.dumps(
            {
                "agent_id": agent_id,
                "owner": owner,
                "team": team,
                "summary": summary,
                "domains": list(domains),
                "last_reviewed_at": last_reviewed_at,
                "maintainer": maintainer,
                "can_answer": list(can_answer),
                "cannot_answer": list(cannot_answer),
                "approval_when": list(approval_when),
                "collaborate_when": list(collaborate_when),
                "knowledge_sources": list(knowledge_sources),
                "trust_labels": list(trust_labels),
            },
            ensure_ascii=False,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO registry_journal (kind, candidate, by, at)"
                " VALUES (?, ?, ?, ?)",
                (kind, candidate_json, by, _iso(at)),
            )
            self._conn.commit()

    def entries(self) -> list[RegistryJournalEntry]:
        """append 순서 그대로(seq ASC) 전 저널 항목을 돌려준다(리플레이 원천)."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM registry_journal ORDER BY seq ASC"
            ).fetchall()
        result: list[RegistryJournalEntry] = []
        for row in rows:
            raw: dict[str, object] = json.loads(row["candidate"])
            kind: RegistryJournalKind = "register" if row["kind"] == "register" else "transfer"
            candidate = RegistryJournalCandidate(
                agent_id=str(raw["agent_id"]),
                owner=str(raw["owner"]),
                team=str(raw["team"]),
                summary=str(raw["summary"]),
                domains=tuple(raw.get("domains") or ()),  # type: ignore[arg-type]
                last_reviewed_at=str(raw["last_reviewed_at"]),
                maintainer=raw.get("maintainer"),  # type: ignore[arg-type]
                can_answer=tuple(raw.get("can_answer") or ()),  # type: ignore[arg-type]
                cannot_answer=tuple(raw.get("cannot_answer") or ()),  # type: ignore[arg-type]
                approval_when=tuple(raw.get("approval_when") or ()),  # type: ignore[arg-type]
                collaborate_when=tuple(raw.get("collaborate_when") or ()),  # type: ignore[arg-type]
                knowledge_sources=tuple(raw.get("knowledge_sources") or ()),  # type: ignore[arg-type]
                trust_labels=tuple(raw.get("trust_labels") or ()),  # type: ignore[arg-type]
            )
            result.append(
                RegistryJournalEntry(
                    kind=kind,
                    candidate=candidate,
                    by=str(row["by"]),
                    at=_parse(row["at"]),
                )
            )
        return result
