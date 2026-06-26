"""T9.1(a) — Session 값 객체 + SessionStore 포트 + InMemorySessionStore 단위 테스트.

불변식:
  - 전이≠기록: 세션 status 전이는 도메인; 트랜스크립트 적재는 별개.
  - owner 격리: 세션은 사용자 귀속; 조직 내부 미노출.
  - end 후 맥락(트랜스크립트) 비워짐.
  - active/ended status 전이 결정론(주입 clock).
"""

from datetime import datetime, timezone

from agent_org_network.session import (
    InMemorySessionStore,
    Session,
    SessionStatus,
    SessionTurn,
)


def fixed_clock() -> datetime:
    return datetime(2026, 6, 27, 9, 0, 0, tzinfo=timezone.utc)


def later_clock(offset_seconds: int = 60):
    def _clock() -> datetime:
        from datetime import timedelta

        return datetime(2026, 6, 27, 9, 0, 0, tzinfo=timezone.utc) + timedelta(
            seconds=offset_seconds
        )

    return _clock


# ── Session 값 객체 ────────────────────────────────────────────────────


def test_Session이_frozen이다():
    session = Session(
        session_id="s1",
        user_id="u1",
        status="active",
        transcript=(),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    import pytest

    with pytest.raises((AttributeError, TypeError)):
        session.status = "ended"  # type: ignore[misc]


def test_Session_status_타입이_Literal이다():
    s = Session(
        session_id="s1",
        user_id="u1",
        status="active",
        transcript=(),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    assert s.status == "active"


def test_SessionTurn이_frozen이다():
    turn = SessionTurn(
        question="질문",
        answer_text="답",
        answered_by="agent_finance",
        at=fixed_clock(),
    )
    import pytest

    with pytest.raises((AttributeError, TypeError)):
        turn.question = "변경"  # type: ignore[misc]


# ── InMemorySessionStore 라이프사이클 ──────────────────────────────────


def test_open_or_get이_첫_메시지에_새_세션을_연다():
    store = InMemorySessionStore(clock=fixed_clock)
    session = store.open_or_get("user_alice")
    assert session.user_id == "user_alice"
    assert session.status == "active"
    assert session.transcript == ()


def test_open_or_get이_같은_사용자_활성_세션_재반환한다():
    store = InMemorySessionStore(clock=fixed_clock)
    s1 = store.open_or_get("user_alice")
    s2 = store.open_or_get("user_alice")
    assert s1.session_id == s2.session_id


def test_다른_사용자는_다른_세션을_얻는다():
    store = InMemorySessionStore(clock=fixed_clock)
    sa = store.open_or_get("user_alice")
    sb = store.open_or_get("user_bob")
    assert sa.session_id != sb.session_id
    assert sa.user_id == "user_alice"
    assert sb.user_id == "user_bob"


def test_get이_session_id로_조회한다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    found = store.get(s.session_id)
    assert found is not None
    assert found.session_id == s.session_id


def test_get이_없는_session_id면_None을_반환한다():
    store = InMemorySessionStore(clock=fixed_clock)
    assert store.get("nonexistent") is None


def test_active_for_user가_활성_세션을_반환한다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    found = store.active_for_user("user_alice")
    assert found is not None
    assert found.session_id == s.session_id


def test_active_for_user가_없는_사용자면_None을_반환한다():
    store = InMemorySessionStore(clock=fixed_clock)
    assert store.active_for_user("user_ghost") is None


# ── append_turn — 전이≠기록 불변식 ────────────────────────────────────


def test_append_turn이_트랜스크립트에_턴을_추가한다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    turn = SessionTurn(
        question="환불 되나요?",
        answer_text="환불 가능합니다.",
        answered_by="agent_finance",
        at=fixed_clock(),
    )
    updated = store.append_turn(s.session_id, turn)
    assert len(updated.transcript) == 1
    assert updated.transcript[0].question == "환불 되나요?"


def test_append_turn이_누적된다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    turn1 = SessionTurn(
        question="질문1",
        answer_text="답1",
        answered_by="agent_a",
        at=fixed_clock(),
    )
    turn2 = SessionTurn(
        question="질문2",
        answer_text="답2",
        answered_by="agent_b",
        at=fixed_clock(),
    )
    store.append_turn(s.session_id, turn1)
    updated = store.append_turn(s.session_id, turn2)
    assert len(updated.transcript) == 2


def test_append_turn이_status를_active로_유지한다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    turn = SessionTurn(
        question="q",
        answer_text="a",
        answered_by="agent_x",
        at=fixed_clock(),
    )
    updated = store.append_turn(s.session_id, turn)
    assert updated.status == "active"


def test_append_turn_전이와_기록이_독립적이다():
    """전이≠기록 불변식: 트랜스크립트 적재는 세션 status 전이와 별개다."""
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    turn = SessionTurn(
        question="q",
        answer_text="a",
        answered_by="agent_x",
        at=fixed_clock(),
    )
    updated = store.append_turn(s.session_id, turn)
    assert updated.status == "active"
    assert len(updated.transcript) == 1


# ── end — 맥락 비움 불변식 ────────────────────────────────────────────


def test_end가_세션을_종료하고_트랜스크립트를_비운다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    turn = SessionTurn(
        question="q",
        answer_text="a",
        answered_by="agent_x",
        at=fixed_clock(),
    )
    store.append_turn(s.session_id, turn)
    ended = store.end(s.session_id)
    assert ended is not None
    assert ended.status == "ended"
    assert ended.transcript == ()


def test_end가_없는_session_id면_None을_반환한다():
    store = InMemorySessionStore(clock=fixed_clock)
    result = store.end("nonexistent")
    assert result is None


def test_end_후_active_for_user가_None을_반환한다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    store.end(s.session_id)
    assert store.active_for_user("user_alice") is None


def test_end_후_같은_사용자_새_메시지는_새_세션을_연다():
    store = InMemorySessionStore(clock=fixed_clock)
    s1 = store.open_or_get("user_alice")
    store.end(s1.session_id)
    s2 = store.open_or_get("user_alice")
    assert s2.session_id != s1.session_id
    assert s2.status == "active"


# ── owner 격리 불변식 ──────────────────────────────────────────────────


def test_사용자_A_세션이_사용자_B_발화를_포함하지_않는다():
    """owner 격리: 각 사용자의 세션은 독립적이다."""
    store = InMemorySessionStore(clock=fixed_clock)
    sa = store.open_or_get("user_alice")
    sb = store.open_or_get("user_bob")

    turn_a = SessionTurn(
        question="alice 질문",
        answer_text="alice 답",
        answered_by="agent_x",
        at=fixed_clock(),
    )
    turn_b = SessionTurn(
        question="bob 질문",
        answer_text="bob 답",
        answered_by="agent_y",
        at=fixed_clock(),
    )
    store.append_turn(sa.session_id, turn_a)
    store.append_turn(sb.session_id, turn_b)

    final_a = store.get(sa.session_id)
    final_b = store.get(sb.session_id)

    assert final_a is not None
    assert final_b is not None

    alice_questions = {t.question for t in final_a.transcript}
    bob_questions = {t.question for t in final_b.transcript}

    assert "alice 질문" in alice_questions
    assert "bob 질문" not in alice_questions

    assert "bob 질문" in bob_questions
    assert "alice 질문" not in bob_questions


# ── SessionStatus 타입 확인 ────────────────────────────────────────────


def test_SessionStatus는_active와_ended만_허용한다():
    assert "active" == SessionStatus.__args__[0]  # type: ignore[attr-defined]
    assert "ended" == SessionStatus.__args__[1]  # type: ignore[attr-defined]


# ── started_at·last_active_at 주입 clock 결정론 ────────────────────────


def test_open_or_get이_clock으로_시각을_기록한다():
    store = InMemorySessionStore(clock=fixed_clock)
    s = store.open_or_get("user_alice")
    assert s.started_at == fixed_clock()
    assert s.last_active_at == fixed_clock()


def test_append_turn이_last_active_at을_갱신한다():
    store = InMemorySessionStore(clock=later_clock(120))
    s = store.open_or_get("user_alice")
    turn = SessionTurn(
        question="q",
        answer_text="a",
        answered_by="agent_x",
        at=later_clock(120)(),
    )
    updated = store.append_turn(s.session_id, turn)
    assert updated.last_active_at == later_clock(120)()
