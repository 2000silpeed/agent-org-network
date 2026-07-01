"""T9.8(a) — SqliteSessionStore·SqliteTokenStore durable 어댑터 통합 테스트 (ADR 0024·0026).

`SubprocessGitGateway` tmp repo 통합 테스트 정신 — `tmp_path` DB 파일로 통합 검증한다.
stdlib sqlite3 만 쓰므로(새 의존성 0) 무조건 실행(skip 불필요).

검증 축:
  - durable: 적재 → store 인스턴스 재생성(재오픈) → 조회가 보존됨(세션·턴·만료·revoke).
  - 동치성: 같은 시나리오를 InMemory·Sqlite 두 구현에 돌려 같은 결과.
  - 의미 보존: 상태 전이(end→transcript 비움)·유휴 타임아웃(주입 clock·30분 슬라이딩)·
    owner 격리·토큰 해시/만료/revoke(append-only).

결정론: 주입 clock·주입 token_factory. 비결정·네트워크 0.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.session import (
    InMemorySessionStore,
    Session,
    SessionStore,
    SessionTurn,
)
from agent_org_network.sqlite_stores import SqliteSessionStore, SqliteTokenStore
from agent_org_network.token import InMemoryTokenStore, TokenStore

# ── 픽스처 ───────────────────────────────────────────────────────────────────

T0 = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)

OWNER_ID = "owner_alice"
ROLE = "primary"


class FrozenClock:
    """주입 clock — now 를 명시적으로 진전시켜 유휴/만료를 결정론으로 만든다."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value


def _turn(q: str = "q", a: str = "a", by: str = "cs", at: datetime = T0) -> SessionTurn:
    return SessionTurn(question=q, answer_text=a, answered_by=by, at=at)


def _token_factory(seq: list[str]) -> "object":
    tokens = list(seq)
    idx = 0

    def factory() -> str:
        nonlocal idx
        val = tokens[idx % len(tokens)]
        idx += 1
        return val

    return factory


# ── Protocol 준수 ────────────────────────────────────────────────────────────

def test_SqliteSessionStore_는_SessionStore_프로토콜을_만족한다(tmp_path: Path) -> None:
    store: SessionStore = SqliteSessionStore(tmp_path / "s.db")
    assert callable(store.open_or_get)
    assert callable(store.get)
    assert callable(store.append_turn)
    assert callable(store.end)
    assert callable(store.active_for_user)


def test_SqliteTokenStore_는_TokenStore_프로토콜을_만족한다(tmp_path: Path) -> None:
    store: TokenStore = SqliteTokenStore(tmp_path / "t.db")
    assert callable(store.issue)
    assert callable(store.verify)
    assert callable(store.revoke)
    assert callable(store.list_active)


# ── 세션: durable(재오픈 보존) ───────────────────────────────────────────────

def test_세션과_턴이_재오픈_후_보존된다(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    store = SqliteSessionStore(db, clock=lambda: T0)
    session = store.open_or_get("u1")
    store.append_turn(session.session_id, _turn("Q1", "A1", "cs", T0))
    store.append_turn(session.session_id, _turn("Q2", "A2", "legal", T1))
    sid = session.session_id
    store.close()

    reopened = SqliteSessionStore(db, clock=lambda: T1)
    got = reopened.get(sid)
    assert got is not None
    assert got.user_id == "u1"
    assert got.status == "active"
    assert len(got.transcript) == 2
    assert got.transcript[0].question == "Q1"
    assert got.transcript[0].answer_text == "A1"
    assert got.transcript[1].answered_by == "legal"
    # tz-aware 왕복 보존
    assert got.transcript[1].at == T1
    assert got.started_at == T0


def test_open_or_get_는_재오픈_후_같은_active_세션을_돌려준다(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db, clock=lambda: T0)
    first = store.open_or_get("u1")
    store.close()

    reopened = SqliteSessionStore(db, clock=lambda: T0)
    again = reopened.open_or_get("u1")
    assert again.session_id == first.session_id


def test_end_는_transcript_를_비우고_재오픈_후에도_유지된다(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    store = SqliteSessionStore(db, clock=lambda: T0)
    session = store.open_or_get("u1")
    store.append_turn(session.session_id, _turn())
    ended = store.end(session.session_id)
    assert ended is not None
    assert ended.status == "ended"
    assert ended.transcript == ()
    sid = session.session_id
    store.close()

    reopened = SqliteSessionStore(db, clock=lambda: T0)
    got = reopened.get(sid)
    assert got is not None
    assert got.status == "ended"
    assert got.transcript == ()  # end 후 맥락 비워짐 불변식 durable


def test_유휴_타임아웃_후_open_or_get_은_새_세션을_연다(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    clock = FrozenClock(T0)
    store = SqliteSessionStore(db, clock=clock)
    first = store.open_or_get("u1")

    # 30분 + 1초 경과 → 유휴 만료 → 새 세션
    clock.set(T0 + timedelta(seconds=SqliteSessionStore.IDLE_TIMEOUT_SECONDS + 1))
    second = store.open_or_get("u1")
    assert second.session_id != first.session_id

    # 이전 세션은 auto_end 되어 있고 재오픈 후에도 ended
    store.close()
    reopened = SqliteSessionStore(db, clock=clock)
    prev = reopened.get(first.session_id)
    assert prev is not None
    assert prev.status == "ended"


def test_유휴_전이면_같은_세션을_유지한다(tmp_path: Path) -> None:
    clock = FrozenClock(T0)
    store = SqliteSessionStore(tmp_path / "s.db", clock=clock)
    first = store.open_or_get("u1")
    clock.set(T0 + timedelta(seconds=SqliteSessionStore.IDLE_TIMEOUT_SECONDS - 1))
    second = store.open_or_get("u1")
    assert second.session_id == first.session_id


def test_owner_격리_서로_다른_사용자는_독립_세션(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "s.db", clock=lambda: T0)
    s1 = store.open_or_get("u1")
    s2 = store.open_or_get("u2")
    assert s1.session_id != s2.session_id
    assert store.active_for_user("u1") is not None
    assert store.active_for_user("u1").session_id == s1.session_id  # type: ignore[union-attr]
    assert store.active_for_user("u2").session_id == s2.session_id  # type: ignore[union-attr]


def test_append_turn_은_없는_활성_세션에_ValueError(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "s.db", clock=lambda: T0)
    with pytest.raises(ValueError):
        store.append_turn("nope", _turn())


def test_end_는_없는_세션에_None(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "s.db", clock=lambda: T0)
    assert store.end("nope") is None


# ── 토큰: durable(재오픈 보존) ───────────────────────────────────────────────

def test_토큰_발급_후_재오픈_verify_durable(tmp_path: Path) -> None:
    db = tmp_path / "tokens.db"
    store = SqliteTokenStore(db, token_factory=_token_factory(["raw_1"]))  # type: ignore[arg-type]
    raw, tok = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=2))
    store.close()

    reopened = SqliteTokenStore(db, token_factory=_token_factory(["x"]))  # type: ignore[arg-type]
    verified = reopened.verify(raw, now=T1)
    assert verified is not None
    assert verified.token_id == tok.token_id
    assert verified.owner_id == OWNER_ID
    assert verified.role == ROLE
    # 평문 미저장 — 해시만 왕복
    assert verified.token_hash == tok.token_hash


def test_만료_토큰은_재오픈_후에도_verify_None(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = SqliteTokenStore(db, token_factory=_token_factory(["raw"]))  # type: ignore[arg-type]
    raw, _ = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))
    store.close()

    reopened = SqliteTokenStore(db, token_factory=_token_factory(["x"]))  # type: ignore[arg-type]
    assert reopened.verify(raw, now=T1) is None  # 만료 경계 now >= expires_at
    assert reopened.verify(raw, now=T1 - timedelta(seconds=1)) is not None


def test_revoke_는_재오픈_후에도_verify_거부_append_only(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = SqliteTokenStore(db, token_factory=_token_factory(["raw"]))  # type: ignore[arg-type]
    raw, tok = store.issue(OWNER_ID, ROLE, now=T0)
    revoked = store.revoke(tok.token_id, now=T1)
    assert revoked is not None
    assert revoked.revoked is True
    assert revoked.revoked_at == T1
    store.close()

    reopened = SqliteTokenStore(db, token_factory=_token_factory(["x"]))  # type: ignore[arg-type]
    assert reopened.verify(raw, now=T2) is None
    # append-only: 삭제 안 됨 → 재revoke 멱등으로 여전히 조회됨
    again = reopened.revoke(tok.token_id, now=T2)
    assert again is not None
    assert again.revoked is True
    assert again.revoked_at == T1  # 최초 revoke 시각 보존(멱등)


def test_revoke_now_생략시_생성자_clock(tmp_path: Path) -> None:
    store = SqliteTokenStore(
        tmp_path / "t.db",
        token_factory=_token_factory(["raw"]),  # type: ignore[arg-type]
        clock=lambda: T2,
    )
    _, tok = store.issue(OWNER_ID, ROLE, now=T0)
    revoked = store.revoke(tok.token_id)
    assert revoked is not None
    assert revoked.revoked_at == T2  # 생성자 clock 으로 찍힘


def test_list_active_는_만료_revoke_제외_후_재오픈_보존(tmp_path: Path) -> None:
    db = tmp_path / "t.db"
    store = SqliteTokenStore(db, token_factory=_token_factory(["a", "b", "c"]))  # type: ignore[arg-type]
    _, live = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=5))
    _, expiring = store.issue(OWNER_ID, "backup", now=T0, expires_in=timedelta(hours=1))
    _, revoked_tok = store.issue(OWNER_ID, ROLE, now=T0)
    store.revoke(revoked_tok.token_id, now=T0)
    store.close()

    reopened = SqliteTokenStore(db, token_factory=_token_factory(["x"]))  # type: ignore[arg-type]
    active = reopened.list_active(now=T2)  # T2 시점: expiring 만료됨
    ids = {t.token_id for t in active}
    assert live.token_id in ids
    assert expiring.token_id not in ids  # 만료 제외
    assert revoked_tok.token_id not in ids  # revoke 제외


def test_verify_위조_없는_토큰_None(tmp_path: Path) -> None:
    store = SqliteTokenStore(tmp_path / "t.db", token_factory=_token_factory(["raw"]))  # type: ignore[arg-type]
    store.issue(OWNER_ID, ROLE, now=T0)
    assert store.verify("forged", now=T0) is None
    assert store.verify("no_such", now=T0) is None


def test_revoke_없는_token_id_None(tmp_path: Path) -> None:
    store = SqliteTokenStore(tmp_path / "t.db")
    assert store.revoke("nope") is None


# ── 동치성: InMemory ↔ Sqlite 같은 시나리오 같은 결과 ────────────────────────

def _run_session_scenario(store: SessionStore, clock: FrozenClock) -> list[object]:
    """세션 시나리오를 두 구현에 동일하게 돌려 관측치 리스트를 만든다."""
    obs: list[object] = []
    s = store.open_or_get("u1")
    obs.append(("open", s.status, len(s.transcript)))

    s = store.append_turn(s.session_id, _turn("Q1", "A1", "cs", clock()))
    obs.append(("turn1", len(s.transcript), s.transcript[-1].question))

    s2 = store.open_or_get("u1")  # 같은 세션 재사용
    obs.append(("reopen_same", s2.session_id == s.session_id))

    s = store.append_turn(s.session_id, _turn("Q2", "A2", "legal", clock()))
    obs.append(("turn2", len(s.transcript)))

    ended = store.end(s.session_id)
    assert ended is not None
    obs.append(("end", ended.status, len(ended.transcript)))

    # end 후 open_or_get → 새 세션
    s3 = store.open_or_get("u1")
    obs.append(("post_end_new", s3.session_id != s.session_id, s3.status))
    return obs


def test_세션_동치성_InMemory_vs_Sqlite(tmp_path: Path) -> None:
    clock_a = FrozenClock(T0)
    clock_b = FrozenClock(T0)
    mem = InMemorySessionStore(clock=clock_a)
    sql = SqliteSessionStore(tmp_path / "s.db", clock=clock_b)

    obs_mem = _run_session_scenario(mem, clock_a)
    obs_sql = _run_session_scenario(sql, clock_b)
    assert obs_mem == obs_sql


def _run_token_scenario(store: TokenStore) -> list[object]:
    obs: list[object] = []
    raw1, t1 = store.issue(OWNER_ID, ROLE, now=T0, expires_in=timedelta(hours=1))
    obs.append(("issue", t1.owner_id, t1.role, t1.revoked))
    obs.append(("verify_valid", store.verify(raw1, now=T0) is not None))
    obs.append(("verify_expired", store.verify(raw1, now=T1) is None))

    r = store.revoke(t1.token_id, now=T0 + timedelta(minutes=1))
    assert r is not None
    obs.append(("revoke", r.revoked, r.revoked_at))
    obs.append(("verify_revoked", store.verify(raw1, now=T0) is None))

    _, t2 = store.issue(OWNER_ID, "backup", now=T0)
    active = store.list_active(now=T0)
    obs.append(("list_active_ids", sorted(t.token_id == t2.token_id for t in active)))
    obs.append(("list_active_count", len(active)))
    return obs


def test_토큰_동치성_InMemory_vs_Sqlite(tmp_path: Path) -> None:
    # token_id 는 uuid 라 값 자체는 비교 안 하고 구조/상태만 비교하도록 시나리오 구성.
    factory_seq = ["r1", "r2"]
    mem = InMemoryTokenStore(token_factory=_token_factory(factory_seq))  # type: ignore[arg-type]
    sql = SqliteTokenStore(tmp_path / "t.db", token_factory=_token_factory(factory_seq))  # type: ignore[arg-type]

    obs_mem = _run_token_scenario(mem)
    obs_sql = _run_token_scenario(sql)
    assert obs_mem == obs_sql


# ── 값 객체 왕복 타입 안정성 ─────────────────────────────────────────────────

def test_세션_datetime_왕복은_tz_aware_유지(tmp_path: Path) -> None:
    store = SqliteSessionStore(tmp_path / "s.db", clock=lambda: T0)
    s = store.open_or_get("u1")
    got = store.get(s.session_id)
    assert got is not None
    assert got.started_at.tzinfo is not None
    assert got.last_active_at.tzinfo is not None
    assert isinstance(got, Session)
