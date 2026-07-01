"""select_session_store·select_token_store 결정론 테스트 — `AON_DB` env 시임.

`runtime_select.select_runtime`·`author_select.select_author`와 대칭인 규약: env
미설정→InMemory 기본(하위호환), `AON_DB` 설정→SqliteSessionStore/SqliteTokenStore
(같은 DB 파일 공유 — 테이블명이 sessions/session_turns vs tokens라 충돌 없음).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_org_network.session import InMemorySessionStore
from agent_org_network.sqlite_stores import SqliteSessionStore, SqliteTokenStore
from agent_org_network.storage_select import select_session_store, select_token_store
from agent_org_network.token import InMemoryTokenStore


def test_AON_DB_미설정이면_InMemorySessionStore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)

    store = select_session_store()

    assert isinstance(store, InMemorySessionStore)


def test_AON_DB_미설정이면_InMemoryTokenStore(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)

    store = select_token_store()

    assert isinstance(store, InMemoryTokenStore)


def test_AON_DB_설정시_SqliteSessionStore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    store = select_session_store()

    assert isinstance(store, SqliteSessionStore)


def test_AON_DB_설정시_SqliteTokenStore(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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

    raw, _ = token_store.issue(
        "owner1", "primary", now=datetime.now(timezone.utc)
    )

    assert db_path.exists()
    # 같은 파일 하나에 두 어댑터가 자기 테이블로 공존 — 재오픈해도 둘 다 보인다.
    reopened_sessions = select_session_store()
    reopened_tokens = select_token_store()
    assert isinstance(reopened_sessions, SqliteSessionStore)
    assert isinstance(reopened_tokens, SqliteTokenStore)
    assert reopened_sessions.get(session.session_id) is not None
    assert (
        reopened_tokens.verify(raw, now=datetime.now(timezone.utc)) is not None
    )


def test_AON_DB_경로의_상위_디렉터리를_자동_생성한다(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "nested" / "dir" / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))
    assert not db_path.parent.exists()

    store = select_session_store()

    assert isinstance(store, SqliteSessionStore)
    assert db_path.parent.exists()
