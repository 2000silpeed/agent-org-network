"""`AON_DB` env → create_app/create_central_app 기본 store 배선 (SQLite durable 어댑터 배선).

`runtime_select`·`author_select`와 대칭인 env 시임 규약: env 미설정→InMemory 기본
(기존 동작·무회귀), `AON_DB` 설정→SqliteSessionStore·SqliteTokenStore(같은 DB 파일
공유). 명시 주입은 항상 env보다 우선(테스트 결정론 보존). 재시작(앱 재생성) 후에도
세션·토큰이 보존되는 e2e를 tmp_path DB 파일로 검증한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent_org_network.runtime import StubRuntime
from agent_org_network.session import InMemorySessionStore
from agent_org_network.sqlite_stores import SqliteSessionStore, SqliteTokenStore
from agent_org_network.token import InMemoryTokenStore
from agent_org_network.web import create_app


def _post(client: TestClient, url: str, json: dict[str, Any] | None = None) -> Any:
    http: Any = client
    return http.post(url, json=json) if json is not None else http.post(url)


def _get(client: TestClient, url: str) -> tuple[int, Any]:
    http: Any = client
    res: Any = http.get(url)
    return res.status_code, res.json()


def _post_json(client: TestClient, url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    http: Any = client
    res: Any = http.post(url, json=payload)
    return res.status_code, res.json()


def test_AON_DB_미설정이면_InMemory_기본_기존동작(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AON_DB", raising=False)

    app = create_app(runtime=StubRuntime())

    assert isinstance(app.state.session_store, InMemorySessionStore)
    assert isinstance(app.state.token_store, InMemoryTokenStore)


def test_AON_DB_설정시_create_app_기본_store가_Sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))

    app = create_app(runtime=StubRuntime())

    assert isinstance(app.state.session_store, SqliteSessionStore)
    assert isinstance(app.state.token_store, SqliteTokenStore)


def test_명시_주입이_env보다_우선(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))
    injected_session_store = InMemorySessionStore()
    injected_token_store = InMemoryTokenStore()

    app = create_app(
        runtime=StubRuntime(),
        session_store=injected_session_store,
        token_store=injected_token_store,
    )

    assert app.state.session_store is injected_session_store
    assert app.state.token_store is injected_token_store


def test_AON_DB_설정시_세션과_토큰이_앱_재생성_후에도_보존된다(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    app1 = create_app(runtime=StubRuntime())
    client1 = TestClient(app1)

    # 콘솔 토큰 발급 — durable token store에 적재.
    issued = _post(
        client1, "/console/tokens", {"owner_id": "cs_lead", "role": "primary"}
    ).json()
    raw_token = issued["token"]

    # 세션은 세션 store에 직접 open해 durable 보존을 확인한다(공개 포트 사용).
    session_store1 = app1.state.session_store
    assert isinstance(session_store1, SqliteSessionStore)
    session = session_store1.open_or_get("web_guest")

    # 앱을 재생성(재시작 시뮬레이션) — 같은 AON_DB 경로를 다시 읽는다.
    app2 = create_app(runtime=StubRuntime())
    client2 = TestClient(app2)

    session_store2 = app2.state.session_store
    assert isinstance(session_store2, SqliteSessionStore)
    reopened_session = session_store2.get(session.session_id)
    assert reopened_session is not None
    assert reopened_session.user_id == "web_guest"

    token_store2 = app2.state.token_store
    assert isinstance(token_store2, SqliteTokenStore)
    from datetime import datetime, timezone

    verified = token_store2.verify(raw_token, now=datetime.now(timezone.utc))
    assert verified is not None
    assert verified.owner_id == "cs_lead"

    # 재등록 확인차 revoke 라우트도 재생성된 앱에서 정상 동작(같은 DB 공유 확인).
    revoke_resp = _post(client2, f"/console/tokens/{issued['token_id']}/revoke")
    assert revoke_resp.status_code == 200


def test_create_central_app도_AON_DB_설정시_같은_토큰_store를_dispatcher와_공유(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_org_network.server import create_central_app

    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))

    app = create_central_app()
    client = TestClient(app)

    issued = _post(
        client, "/console/tokens", {"owner_id": "cs_lead", "role": "primary"}
    ).json()
    raw_token = issued["token"]

    http: Any = client
    with http.websocket_connect("/worker") as conn:
        conn.send_json(
            {
                "type": "register_worker",
                "owner_id": "cs_lead",
                "role": "primary",
                "token": raw_token,
            }
        )
        reply = conn.receive_json()
        assert reply["type"] == "welcome"


# ── Phase 12 확장 — AnswerRecordStore·CorrectionStore·KnowledgeStore·카드 저널 ──


def test_AON_DB_미설정이면_감독_저장소도_InMemory_기본(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_org_network.answer_record import (
        InMemoryAnswerRecordStore,
        InMemoryCorrectionStore,
    )

    monkeypatch.delenv("AON_DB", raising=False)

    app = create_app(runtime=StubRuntime())

    assert isinstance(app.state.answer_record_store, InMemoryAnswerRecordStore)
    assert isinstance(app.state.correction_store, InMemoryCorrectionStore)
    assert app.state.registry_journal is None


def test_AON_DB_설정시_감독_저장소가_Sqlite(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_org_network.sqlite_stores import (
        SqliteAnswerRecordStore,
        SqliteCorrectionStore,
        SqliteRegistryJournal,
    )

    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))

    app = create_app(runtime=StubRuntime())

    assert isinstance(app.state.answer_record_store, SqliteAnswerRecordStore)
    assert isinstance(app.state.correction_store, SqliteCorrectionStore)
    assert isinstance(app.state.registry_journal, SqliteRegistryJournal)


def test_감독_저장소_명시_주입이_env보다_우선(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from agent_org_network.answer_record import (
        InMemoryAnswerRecordStore,
        InMemoryCorrectionStore,
    )

    monkeypatch.setenv("AON_DB", str(tmp_path / "aon.db"))
    injected_answer_store = InMemoryAnswerRecordStore()
    injected_correction_store = InMemoryCorrectionStore()

    app = create_app(
        runtime=StubRuntime(),
        answer_record_store=injected_answer_store,
        correction_store=injected_correction_store,
    )

    assert app.state.answer_record_store is injected_answer_store
    assert app.state.correction_store is injected_correction_store


def test_AON_DB_설정시_오너_변경이_앱_재생성_후에도_저널_리플레이로_복원(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """카드 라이브 오너 변경(관리 UI) → 재기동(앱 재생성) → 저널 리플레이로 새 owner
    반영(ADR 0034 결정 1·2 durable 확장 e2e)."""
    db_path = tmp_path / "aon.db"
    monkeypatch.setenv("AON_DB", str(db_path))

    app1 = create_app(runtime=StubRuntime(), session_secret="test-secret")
    client1 = TestClient(app1)
    _post(client1, "/login", {"user_id": "root_manager"})

    # cs_ops의 현재 owner는 데모 시드상 cs_lead — hr_lead로 오너 변경.
    status, _ = _post_json(client1, "/admin/cards/cs_ops/owner", {"new_owner": "hr_lead"})
    assert status == 200
    assert app1.state.registry_journal is not None

    # 재기동(앱 재생성) — 같은 AON_DB 경로를 다시 읽어 저널을 리플레이한다.
    app2 = create_app(runtime=StubRuntime(), session_secret="test-secret")
    client2 = TestClient(app2)
    _post(client2, "/login", {"user_id": "root_manager"})

    _, cards = _get(client2, "/admin/cards")
    cs_ops = next(c for c in cards if c["agent_id"] == "cs_ops")
    assert cs_ops["owner"] == "hr_lead"
    assert cs_ops["owner"] != "cs_lead"
