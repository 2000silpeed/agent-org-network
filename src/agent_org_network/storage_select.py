"""영속 store 선택 — `AON_DB` env 기반(`runtime_select.select_runtime`과 대칭인 규약).

답 생성 경로가 `AON_PROVIDER`로 런타임을 고르듯, 세션/토큰 durable 경로는 `AON_DB`로
InMemory 기본과 SQLite durable 어댑터(`sqlite_stores.py`, T9.8) 사이를 고른다.
Phase 12 확장(`AnswerRecordStore`·`CorrectionStore`·`KnowledgeStore`·카드 등록
저널)도 같은 `AON_DB` 파일을 공유한다(테이블 분리 — `sqlite_stores.py` 스키마 참조).

`AON_DB`(SQLite 파일 경로) 미설정 → `InMemorySessionStore()`/`InMemoryTokenStore()`
(기존 기본·하위호환 — 기존 테스트 전부 무변경). 설정 → `SqliteSessionStore(path)`/
`SqliteTokenStore(path)` — **같은 DB 파일을 공유**한다(세션은 `sessions`/`session_turns`
테이블, 토큰은 `tokens` 테이블이라 이름 충돌 없음 — `sqlite_stores.py` 스키마 참조).

명시 주입은 항상 이 선택보다 우선한다(`create_app(session_store=..., token_store=...)`
— 테스트 결정론 보존, 이 모듈은 *미주입일 때만* 호출된다).

경로의 상위 디렉터리가 없으면 자동 생성한다(sqlite3.connect가 없는 디렉터리에서
실패하므로).
"""

from __future__ import annotations

import os
from pathlib import Path

from agent_org_network.answer_record import AnswerRecordStore, CorrectionStore, FeedbackStore
from agent_org_network.knowledge_store import KnowledgeStore
from agent_org_network.session import InMemorySessionStore, SessionStore
from agent_org_network.sqlite_stores import (
    SqliteAnswerRecordStore,
    SqliteCorrectionStore,
    SqliteFeedbackStore,
    SqliteKnowledgeStore,
    SqliteRegistryJournal,
    SqliteSessionStore,
    SqliteTokenStore,
)
from agent_org_network.token import InMemoryTokenStore, TokenStore


def _resolve_db_path() -> Path | None:
    raw = (os.environ.get("AON_DB") or "").strip()
    if not raw:
        return None
    path = Path(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def select_session_store() -> SessionStore:
    """`AON_DB` 미설정 → `InMemorySessionStore()`(기본). 설정 → `SqliteSessionStore(path)`."""
    db_path = _resolve_db_path()
    if db_path is None:
        return InMemorySessionStore()
    return SqliteSessionStore(db_path)


def select_token_store() -> TokenStore:
    """`AON_DB` 미설정 → `InMemoryTokenStore()`(기본). 설정 → `SqliteTokenStore(path)`."""
    db_path = _resolve_db_path()
    if db_path is None:
        return InMemoryTokenStore()
    return SqliteTokenStore(db_path)


def select_token_store_or_none() -> TokenStore | None:
    """`AON_DB` 미설정 → `None`(기존 stub 폴백 보존용). 설정 → `SqliteTokenStore(path)`.

    `create_central_app`(server.py) 전용 seam — 그 함수는 `token_store=None`을
    `WebSocketDispatcher`의 인증 stub 폴백 신호로 쓰므로(하위호환), `AON_DB` 미설정
    시엔 `select_token_store()`처럼 `InMemoryTokenStore()`를 만들지 않고 `None`을
    그대로 돌려준다 — 기존 `create_central_app()`(무인자) 호출의 stub 인증 동작을
    깨지 않는다. `AON_DB` 설정 시에만 durable store를 만들어 실 토큰 검증을 켠다.
    """
    db_path = _resolve_db_path()
    if db_path is None:
        return None
    return SqliteTokenStore(db_path)


def select_answer_record_store() -> AnswerRecordStore:
    """`AON_DB` 미설정 → `InMemoryAnswerRecordStore()`(기본). 설정 → `SqliteAnswerRecordStore(path)`."""
    from agent_org_network.answer_record import InMemoryAnswerRecordStore

    db_path = _resolve_db_path()
    if db_path is None:
        return InMemoryAnswerRecordStore()
    return SqliteAnswerRecordStore(db_path)


def select_correction_store() -> CorrectionStore:
    """`AON_DB` 미설정 → `InMemoryCorrectionStore()`(기본). 설정 → `SqliteCorrectionStore(path)`."""
    from agent_org_network.answer_record import InMemoryCorrectionStore

    db_path = _resolve_db_path()
    if db_path is None:
        return InMemoryCorrectionStore()
    return SqliteCorrectionStore(db_path)


def select_feedback_store() -> FeedbackStore:
    """`AON_DB` 미설정 → `InMemoryFeedbackStore()`(기본). 설정 → `SqliteFeedbackStore(path)`.

    다른 answer_record 계열 스토어(`select_answer_record_store`·`select_correction_store`)와
    같은 규약으로 승격됐다(MCP 배선 라운드·2026-07-05 — 이전엔 SQLite 어댑터가 없어
    항상 InMemory였다). `AON_DB` 켜면 같은 DB 파일의 `answer_feedback_latest`(upsert
    최신 판정)·`answer_feedback_history`(append 이력 전량) 두 테이블에 durable 저장한다.
    명시 주입은 항상 이 선택보다 우선한다(테스트 결정론 보존).
    """
    from agent_org_network.answer_record import InMemoryFeedbackStore

    db_path = _resolve_db_path()
    if db_path is None:
        return InMemoryFeedbackStore()
    return SqliteFeedbackStore(db_path)


def select_knowledge_store() -> KnowledgeStore:
    """`AON_DB` 미설정 → `InMemoryKnowledgeStore()`(기본). 설정 → `SqliteKnowledgeStore(path)`."""
    from agent_org_network.knowledge_store import InMemoryKnowledgeStore

    db_path = _resolve_db_path()
    if db_path is None:
        return InMemoryKnowledgeStore()
    return SqliteKnowledgeStore(db_path)


def select_registry_journal() -> SqliteRegistryJournal | None:
    """`AON_DB` 미설정 → `None`(카드 durable 저널 없음 — 기존 InMemory Registry 그대로).

    설정 → `SqliteRegistryJournal(path)` — 카드 라이브 등록·오너 변경을 durable
    저널에 남긴다(ADR 0034 결정 1·2). `token_store_or_none`과 같은 결의 seam —
    `AON_DB` 미설정이면 저널 자체를 만들지 않아 `AdminRegistryService(journal_sink=
    None)`가 기존 동작(하위호환) 그대로 유지된다.
    """
    db_path = _resolve_db_path()
    if db_path is None:
        return None
    return SqliteRegistryJournal(db_path)
