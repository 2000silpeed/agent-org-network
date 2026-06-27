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


# ── (b) assemble_context — 맥락 조립 순수 함수 ─────────────────────────


from agent_org_network.session import assemble_context  # noqa: E402


def test_assemble_context가_빈_세션이면_빈_문자열을_반환한다():
    """트랜스크립트 없는 활성 세션 → 빈/최소 맥락."""
    s = Session(
        session_id="s1",
        user_id="u1",
        status="active",
        transcript=(),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    ctx = assemble_context(s, current_question="지금 질문")
    assert ctx == ""


def test_assemble_context가_단일_턴을_조립한다():
    turn = SessionTurn(
        question="환불 되나요?",
        answer_text="환불 가능합니다.",
        answered_by="agent_finance",
        at=fixed_clock(),
    )
    s = Session(
        session_id="s1",
        user_id="u1",
        status="active",
        transcript=(turn,),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    ctx = assemble_context(s, current_question="추가 질문")
    assert "환불 되나요?" in ctx
    assert "환불 가능합니다." in ctx


def test_assemble_context가_복수_턴을_순서대로_조립한다():
    t1 = SessionTurn(question="첫 질문", answer_text="첫 답", answered_by="a", at=fixed_clock())
    t2 = SessionTurn(question="두번째 질문", answer_text="두번째 답", answered_by="b", at=fixed_clock())
    s = Session(
        session_id="s1",
        user_id="u1",
        status="active",
        transcript=(t1, t2),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    ctx = assemble_context(s, current_question="세번째 질문")
    assert ctx.index("첫 질문") < ctx.index("두번째 질문")


def test_assemble_context가_종료_세션이면_빈_문자열을_반환한다():
    """ADR 0024 결정 3: 종료 세션(transcript 빈 튜플) → 빈 맥락(맥락 누출 0)."""
    s = Session(
        session_id="s1",
        user_id="u1",
        status="ended",
        transcript=(),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    ctx = assemble_context(s, current_question="다시 질문")
    assert ctx == ""


def test_assemble_context에_다른_사용자_발화가_섞이지_않는다():
    """owner 격리 적대 테스트(양방향): A 맥락에 B 발화 없음·B 맥락에 A 발화 없음."""
    turn_a = SessionTurn(
        question="alice 전용 질문",
        answer_text="alice 전용 답",
        answered_by="agent_x",
        at=fixed_clock(),
    )
    turn_b = SessionTurn(
        question="bob 전용 질문",
        answer_text="bob 전용 답",
        answered_by="agent_y",
        at=fixed_clock(),
    )
    session_a = Session(
        session_id="sa",
        user_id="user_alice",
        status="active",
        transcript=(turn_a,),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    session_b = Session(
        session_id="sb",
        user_id="user_bob",
        status="active",
        transcript=(turn_b,),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    ctx_a = assemble_context(session_a, current_question="alice 추가 질문")
    ctx_b = assemble_context(session_b, current_question="bob 추가 질문")

    assert "alice 전용 질문" in ctx_a
    assert "bob 전용 질문" not in ctx_a

    assert "bob 전용 질문" in ctx_b
    assert "alice 전용 질문" not in ctx_b


def test_assemble_context가_순수_함수이다():
    """IO 0 — 같은 입력에 항상 같은 출력(결정론)."""
    turn = SessionTurn(question="q", answer_text="a", answered_by="ag", at=fixed_clock())
    s = Session(
        session_id="s1",
        user_id="u1",
        status="active",
        transcript=(turn,),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    assert assemble_context(s, "질문") == assemble_context(s, "질문")


def test_assemble_context가_내부_라우팅_구조를_노출하지_않는다():
    """노출 불변식: 맥락에 조직 내부 구조(session_id 등 내부값)가 섞이지 않는다.
    answered_by는 맥락 라벨로 허용되나, 라우팅 내부 메타(session_id)는 안 된다."""
    turn = SessionTurn(
        question="질문",
        answer_text="답",
        answered_by="agent_finance",
        at=fixed_clock(),
    )
    s = Session(
        session_id="secret-session-id-12345",
        user_id="u1",
        status="active",
        transcript=(turn,),
        started_at=fixed_clock(),
        last_active_at=fixed_clock(),
    )
    ctx = assemble_context(s, current_question="추가 질문")
    assert "secret-session-id-12345" not in ctx


# ── (c) 유휴 타임아웃 자동종료 — 슬라이딩 30분 ──────────────────────────


from datetime import timedelta  # noqa: E402


BASE_TIME = datetime(2026, 6, 27, 9, 0, 0, tzinfo=timezone.utc)
IDLE_SECONDS = 30 * 60  # 1800


def make_clock(offset_seconds: int):
    def _clock() -> datetime:
        return BASE_TIME + timedelta(seconds=offset_seconds)
    return _clock


def test_유휴_임계_너머에서_open_or_get이_자동종료_후_새_세션을_연다():
    """유휴 타임아웃 핵심 — clock이 30분 초과 시 자동 end + 새 세션 생성."""
    tick: list[int] = [0]

    def advancing_clock() -> datetime:
        return BASE_TIME + timedelta(seconds=tick[0])

    store = InMemorySessionStore(clock=advancing_clock)

    s1 = store.open_or_get("user_alice")
    old_id = s1.session_id

    tick[0] = IDLE_SECONDS + 1

    s2 = store.open_or_get("user_alice")
    assert s2.session_id != old_id, "유휴 만료 후 새 session_id를 발급해야 한다"
    assert s2.status == "active"


def test_유휴_임계_정확히_도달하면_자동종료된다():
    """== 경계 단언 — elapsed == IDLE_TIMEOUT_SECONDS(1800)이면 만료(>=이어야 함, >이면 생존 버그)."""
    tick: list[int] = [0]

    def advancing_clock() -> datetime:
        return BASE_TIME + timedelta(seconds=tick[0])

    store = InMemorySessionStore(clock=advancing_clock)
    s1 = store.open_or_get("user_alice")

    tick[0] = IDLE_SECONDS  # elapsed == 1800, 정확히 임계

    s2 = store.open_or_get("user_alice")
    assert s2.session_id != s1.session_id, "elapsed == 임계이면 자동종료 후 새 세션이어야 한다"
    assert s2.status == "active"


def test_유휴_임계_직전이면_같은_세션을_유지한다():
    """슬라이딩 경계 — 임계 직전(exactly 1초 전)이면 세션 유지."""
    tick: list[int] = [0]

    def advancing_clock() -> datetime:
        return BASE_TIME + timedelta(seconds=tick[0])

    store = InMemorySessionStore(clock=advancing_clock)

    s1 = store.open_or_get("user_alice")
    tick[0] = IDLE_SECONDS - 1

    s2 = store.open_or_get("user_alice")
    assert s2.session_id == s1.session_id, "임계 직전에는 같은 세션을 유지해야 한다"


def test_append_turn이_타이머를_리셋한다():
    """슬라이딩 입증 — append_turn 후 last_active_at이 갱신되어 타임아웃 리셋."""
    tick: list[int] = [0]

    def advancing_clock() -> datetime:
        return BASE_TIME + timedelta(seconds=tick[0])

    store = InMemorySessionStore(clock=advancing_clock)
    s1 = store.open_or_get("user_alice")

    tick[0] = IDLE_SECONDS - 10
    turn = SessionTurn(
        question="활동 중",
        answer_text="응답",
        answered_by="agent_x",
        at=advancing_clock(),
    )
    store.append_turn(s1.session_id, turn)

    tick[0] = IDLE_SECONDS + 1
    s2 = store.open_or_get("user_alice")
    assert s2.session_id == s1.session_id, "append_turn 후 타이머가 리셋되어 같은 세션을 유지해야 한다"


def test_유휴_자동종료된_세션은_transcript가_비워진다():
    """자동종료도 맥락 비움 — 종료 후 ended 세션 transcript=()."""
    tick: list[int] = [0]

    def advancing_clock() -> datetime:
        return BASE_TIME + timedelta(seconds=tick[0])

    store = InMemorySessionStore(clock=advancing_clock)
    s1 = store.open_or_get("user_alice")
    turn = SessionTurn(
        question="q",
        answer_text="a",
        answered_by="ag",
        at=advancing_clock(),
    )
    store.append_turn(s1.session_id, turn)

    tick[0] = IDLE_SECONDS + 1
    store.open_or_get("user_alice")

    ended = store.get(s1.session_id)
    assert ended is not None
    assert ended.status == "ended"
    assert ended.transcript == ()


def test_유휴_만료_후_새_세션은_다른_session_id를_갖는다():
    """session_id 단조성 — 만료 후 새 세션은 반드시 새 ID."""
    tick: list[int] = [0]

    def advancing_clock() -> datetime:
        return BASE_TIME + timedelta(seconds=tick[0])

    store = InMemorySessionStore(clock=advancing_clock)
    s1 = store.open_or_get("user_alice")

    tick[0] = IDLE_SECONDS + 1
    s2 = store.open_or_get("user_alice")

    assert s1.session_id != s2.session_id


def test_유휴_만료는_다른_사용자_세션에_영향을_주지_않는다():
    """owner 격리 — alice 유휴 만료가 bob 세션에 영향 없음."""
    tick: list[int] = [0]

    def advancing_clock() -> datetime:
        return BASE_TIME + timedelta(seconds=tick[0])

    store = InMemorySessionStore(clock=advancing_clock)
    store.open_or_get("user_alice")
    s_bob = store.open_or_get("user_bob")

    bob_turn = SessionTurn(
        question="bob 질문",
        answer_text="bob 답",
        answered_by="ag",
        at=advancing_clock(),
    )
    store.append_turn(s_bob.session_id, bob_turn)

    tick[0] = IDLE_SECONDS + 1
    store.open_or_get("user_alice")

    bob_now = store.active_for_user("user_bob")
    assert bob_now is not None
    assert bob_now.session_id == s_bob.session_id


def test_유휴_타임아웃_상수가_30분이다():
    """ADR 0024 결정 B 확인 — IDLE_TIMEOUT_SECONDS = 1800."""
    assert InMemorySessionStore.IDLE_TIMEOUT_SECONDS == 30 * 60
