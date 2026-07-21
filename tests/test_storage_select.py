"""select_session_store·select_token_store 결정론 테스트 — `AON_DB` env 시임.

`runtime_select.select_runtime`·`author_select.select_author`와 대칭인 규약: env
미설정→InMemory 기본(하위호환), `AON_DB` 설정→SqliteSessionStore/SqliteTokenStore
(같은 DB 파일 공유 — 테이블명이 sessions/session_turns vs tokens라 충돌 없음).

Phase 12 확장(SQLite durable): `select_answer_record_store`·`select_correction_store`·
`select_knowledge_store`·`select_registry_journal`도 같은 env 시임·같은 DB 파일 공유
규약을 따른다(테이블 분리 — `sqlite_stores.py` 스키마 참조).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.answer_record import (
    InMemoryAnswerRecordStore,
    InMemoryCorrectionStore,
    InMemoryFeedbackStore,
)
from agent_org_network.knowledge_store import InMemoryKnowledgeStore
from agent_org_network.question_request import (
    InMemoryQuestionRequestStore,
    QuestionRequest,
)
from agent_org_network.session import InMemorySessionStore
from agent_org_network.sqlite_stores import (
    SqliteAnswerRecordStore,
    SqliteCorrectionStore,
    SqliteKnowledgeStore,
    SqliteQuestionRequestStore,
    SqliteRegistryJournal,
    SqliteSessionStore,
    SqliteTokenStore,
)
from agent_org_network.storage_select import (
    select_answer_record_store,
    select_correction_store,
    select_feedback_store,
    select_knowledge_store,
    select_question_request_store,
    select_registry_journal,
    select_session_store,
    select_token_store,
)
from agent_org_network.token import InMemoryTokenStore


def _question_request() -> QuestionRequest:
    now = datetime(2026, 7, 12, tzinfo=timezone.utc)
    return QuestionRequest.receive(
        org_id="org-selector",
        requester_id="user-selector",
        question="selector 저장소인가요?",
        request_id_factory=lambda: "request-selector",
        clock=lambda: now,
        due_at=now + timedelta(minutes=10),
    )


def test_AON_DB_미설정이면_InMemoryQuestionRequestStore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AON_DB", raising=False)

    assert isinstance(select_question_request_store(), InMemoryQuestionRequestStore)


def test_AON_DB_파일설정시_상위디렉터리를_만들고_SqliteQuestionRequestStore(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "nested-question" / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    store = select_question_request_store()

    assert isinstance(store, SqliteQuestionRequestStore)
    assert db_path.parent.exists()
    store.close()


def test_AON_DB_memory_selector호출끼리는_저장내용공유를_보장하지_않는다(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AON_DB", ":memory:")
    first = select_question_request_store()
    second = select_question_request_store()
    assert isinstance(first, SqliteQuestionRequestStore)
    assert isinstance(second, SqliteQuestionRequestStore)

    first.create(_question_request())

    assert second.get("request-selector") is None
    first.close()
    second.close()


def test_AON_DB_미설정이면_InMemorySessionStore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)

    store = select_session_store()

    assert isinstance(store, InMemorySessionStore)


def test_AON_DB_미설정이면_InMemoryTokenStore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)

    store = select_token_store()

    assert isinstance(store, InMemoryTokenStore)


def test_AON_DB_설정시_SqliteSessionStore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    store = select_session_store()

    assert isinstance(store, SqliteSessionStore)


def test_AON_DB_설정시_SqliteTokenStore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = tmp_path / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    store = select_token_store()

    assert isinstance(store, SqliteTokenStore)


def test_AON_DB_설정시_세션과_토큰이_같은_DB_파일을_공유한다(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    session_store = select_session_store()
    token_store = select_token_store()
    assert isinstance(session_store, SqliteSessionStore)
    assert isinstance(token_store, SqliteTokenStore)

    session = session_store.open_or_get("owner1")
    from datetime import datetime, timezone

    raw, _ = token_store.issue("owner1", "primary", now=datetime.now(timezone.utc))

    assert db_path.exists()
    # 같은 파일 하나에 두 어댑터가 자기 테이블로 공존 — 재오픈해도 둘 다 보인다.
    reopened_sessions = select_session_store()
    reopened_tokens = select_token_store()
    assert isinstance(reopened_sessions, SqliteSessionStore)
    assert isinstance(reopened_tokens, SqliteTokenStore)
    assert reopened_sessions.get(session.session_id) is not None
    assert reopened_tokens.verify(raw, now=datetime.now(timezone.utc)) is not None


def test_AON_DB_경로의_상위_디렉터리를_자동_생성한다(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "nested" / "dir" / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))
    assert not db_path.parent.exists()

    store = select_session_store()

    assert isinstance(store, SqliteSessionStore)
    assert db_path.parent.exists()


# ── Phase 12 확장 — AnswerRecordStore·CorrectionStore·KnowledgeStore·RegistryJournal ──


def test_AON_DB_미설정이면_InMemoryAnswerRecordStore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AON_DB", raising=False)
    assert isinstance(select_answer_record_store(), InMemoryAnswerRecordStore)


def test_AON_DB_미설정이면_InMemoryCorrectionStore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)
    assert isinstance(select_correction_store(), InMemoryCorrectionStore)


def test_AON_DB_미설정이면_InMemoryKnowledgeStore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)
    assert isinstance(select_knowledge_store(), InMemoryKnowledgeStore)


def test_AON_DB_미설정이면_registry_journal_은_None(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)
    assert select_registry_journal() is None


def test_AON_DB_설정시_SqliteAnswerRecordStore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))
    assert isinstance(select_answer_record_store(), SqliteAnswerRecordStore)


def test_AON_DB_설정시_SqliteCorrectionStore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))
    assert isinstance(select_correction_store(), SqliteCorrectionStore)


def test_AON_DB_설정시_SqliteKnowledgeStore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))
    assert isinstance(select_knowledge_store(), SqliteKnowledgeStore)


def test_AON_DB_설정시_SqliteRegistryJournal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))
    journal = select_registry_journal()
    assert isinstance(journal, SqliteRegistryJournal)


def test_AON_DB_설정시_Phase12_스토어_전부_같은_DB_파일을_공유한다(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    answer_store = select_answer_record_store()
    correction_store = select_correction_store()
    knowledge_store = select_knowledge_store()
    journal = select_registry_journal()

    assert isinstance(answer_store, SqliteAnswerRecordStore)
    assert isinstance(correction_store, SqliteCorrectionStore)
    assert isinstance(knowledge_store, SqliteKnowledgeStore)
    assert isinstance(journal, SqliteRegistryJournal)
    assert db_path.exists()


# ── 답변 피드백(계획 §10) — 다른 answer_record 계열과 같은 AON_DB 규약 ──────────


def test_AON_DB_미설정이면_InMemoryFeedbackStore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)
    assert isinstance(select_feedback_store(), InMemoryFeedbackStore)


def test_AON_DB_설정시_SqliteFeedbackStore(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """MCP 배선 라운드(2026-07-05)에 다른 계열과 같은 규약으로 승격됨."""
    from agent_org_network.sqlite_stores import SqliteFeedbackStore

    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))
    assert isinstance(select_feedback_store(), SqliteFeedbackStore)
