"""T9.1(d) — SessionAskOrg 단위 테스트.

세션 층이 기존 AskOrg.handle을 감싸기(compose)로 붙임을 확인한다.

불변식:
  - 미아 없음: 래퍼가 라우팅 종착을 안 바꾼다 — AskOrg.handle 결과 그대로 반환.
  - 노출 불변식: 세션 층이 OrgReply에 아무것도 추가하지 않는다(동일 Answered/Pending).
  - Answered → SessionTurn 적재 (agent_id = answered_by 튜플 index 1).
  - Pending → 세션 열리지만 턴 미적재.
  - 기존 라우팅(Contested/Unowned/Dispatched) 종착 동일.
"""

from datetime import datetime, timezone

from agent_org_network.ask_org import Answered, OrgReply, Pending
from agent_org_network.session import (
    InMemorySessionStore,
)
from agent_org_network.user import User

# SessionAskOrg 는 아직 미구현 — red 단계에서 ImportError 확인용
from agent_org_network.session import SessionAskOrg  # type: ignore[attr-defined]

_USER = User(id="alice")
_T0 = datetime(2026, 6, 27, 10, 0, 0, tzinfo=timezone.utc)


def _fixed_clock() -> datetime:
    return _T0


def _make_answered(text: str = "답변이에요", agent_id: str = "contract_ops") -> Answered:
    return Answered(
        text=text,
        answered_by=("legal_lead", agent_id),
        mode="full",
        sources=("출처1",),
    )


def _make_pending_unowned() -> Pending:
    return Pending(kind="unowned", message="담당자 없음")


def _make_pending_contested() -> Pending:
    return Pending(kind="contested", message="다툼 중")


def _make_pending_dispatched() -> Pending:
    return Pending(kind="dispatched", message="전달됨", tracking="abc123")


class FakeAskOrg:
    """결정론 FakeAskOrg — 주입된 OrgReply를 그대로 반환한다."""

    def __init__(self, reply: OrgReply) -> None:
        self._reply = reply
        self.call_count = 0
        self.last_question: str | None = None
        self.last_user: User | None = None

    def handle(self, question: str, user: User) -> OrgReply:
        self.call_count += 1
        self.last_question = question
        self.last_user = user
        return self._reply


# ── 기본 위임·노출 불변식 ─────────────────────────────────────────────


def test_SessionAskOrg_Answered를_그대로_반환한다():
    """노출 불변식: 세션 층이 OrgReply 값을 변경하지 않는다."""
    answered = _make_answered()
    fake = FakeAskOrg(answered)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    reply = wrapper.handle("계약 조건이 뭐예요?", _USER)

    assert reply is answered


def test_SessionAskOrg_Pending를_그대로_반환한다():
    """노출 불변식: Pending도 래핑 전후 동일."""
    pending = _make_pending_unowned()
    fake = FakeAskOrg(pending)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    reply = wrapper.handle("주차장 정기권?", _USER)

    assert reply is pending


def test_SessionAskOrg_기존_handle에_위임한다():
    """위임: 래퍼가 fake.handle 을 정확히 1회 호출한다."""
    answered = _make_answered()
    fake = FakeAskOrg(answered)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("질문이에요", _USER)

    assert fake.call_count == 1
    assert fake.last_question == "질문이에요"
    assert fake.last_user is _USER


# ── 세션 생명주기 ─────────────────────────────────────────────────────


def test_SessionAskOrg_첫_메시지에_세션이_열린다():
    """암묵 시작: 첫 handle 후 세션이 생성되어 있어야 한다."""
    fake = FakeAskOrg(_make_answered())
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("첫 질문", _USER)

    session = store.active_for_user(_USER.id)
    assert session is not None
    assert session.user_id == _USER.id
    assert session.status == "active"


def test_SessionAskOrg_두번째_메시지는_같은_세션을_쓴다():
    """같은 사용자의 연속 메시지는 같은 세션에 적재된다."""
    fake = FakeAskOrg(_make_answered())
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("첫 질문", _USER)
    session1 = store.active_for_user(_USER.id)
    assert session1 is not None

    wrapper.handle("두 번째 질문", _USER)
    session2 = store.active_for_user(_USER.id)
    assert session2 is not None

    assert session1.session_id == session2.session_id


# ── 턴 적재 ───────────────────────────────────────────────────────────


def test_Answered이면_세션에_턴이_적재된다():
    """Answered → SessionTurn이 transcript에 추가된다."""
    answered = _make_answered(text="계약 조건은 이래요", agent_id="contract_ops")
    fake = FakeAskOrg(answered)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("계약 조건이 뭐예요?", _USER)

    session = store.active_for_user(_USER.id)
    assert session is not None
    assert len(session.transcript) == 1
    turn = session.transcript[0]
    assert turn.question == "계약 조건이 뭐예요?"
    assert turn.answer_text == "계약 조건은 이래요"
    assert turn.answered_by == "contract_ops"
    assert turn.at == _T0


def test_Answered_턴_answered_by는_agent_id다():
    """answered_by 필드는 Answered.answered_by 튜플의 두 번째(agent_id)다."""
    answered = Answered(
        text="IT 답변",
        answered_by=("it_manager", "it_ops"),
        mode="full",
        sources=(),
    )
    fake = FakeAskOrg(answered)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("계정 문제가 있어요", _USER)

    session = store.active_for_user(_USER.id)
    assert session is not None
    assert session.transcript[0].answered_by == "it_ops"


def test_연속_Answered이면_턴이_누적된다():
    """두 번의 Answered → transcript 길이 2."""
    store = InMemorySessionStore(clock=_fixed_clock)
    # 두 번 다 answered 반환하는 fake — 두 번 별도 호출 허용
    class _MultiAnsweredFake:
        def __init__(self, replies: list[Answered]) -> None:
            self._replies = replies
            self._idx = 0

        def handle(self, question: str, user: User) -> OrgReply:
            r: OrgReply = self._replies[self._idx % len(self._replies)]
            self._idx += 1
            return r

    replies: list[Answered] = [
        _make_answered(text="첫 번째 답", agent_id="contract_ops"),
        _make_answered(text="두 번째 답", agent_id="hr_ops"),
    ]
    fake = _MultiAnsweredFake(replies)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("첫 질문", _USER)
    wrapper.handle("두 번째 질문", _USER)

    session = store.active_for_user(_USER.id)
    assert session is not None
    assert len(session.transcript) == 2
    assert session.transcript[0].answered_by == "contract_ops"
    assert session.transcript[1].answered_by == "hr_ops"


# ── Pending 미적재 ────────────────────────────────────────────────────


def test_Pending_unowned이면_턴이_적재되지_않는다():
    """Pending(kind=unowned) → 세션은 열리지만 턴 미적재."""
    fake = FakeAskOrg(_make_pending_unowned())
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("주차장 정기권?", _USER)

    session = store.active_for_user(_USER.id)
    assert session is not None
    assert len(session.transcript) == 0


def test_Pending_contested이면_턴이_적재되지_않는다():
    """Pending(kind=contested) → 세션 열리되 턴 미적재."""
    fake = FakeAskOrg(_make_pending_contested())
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("보상 기준이 뭐예요?", _USER)

    session = store.active_for_user(_USER.id)
    assert session is not None
    assert len(session.transcript) == 0


def test_Pending_dispatched이면_턴이_적재되지_않는다():
    """Pending(kind=dispatched) → 세션 열리되 턴 미적재. dispatched 적재는 후속."""
    fake = FakeAskOrg(_make_pending_dispatched())
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("계약 검토 요청", _USER)

    session = store.active_for_user(_USER.id)
    assert session is not None
    assert len(session.transcript) == 0


# ── 미아 없음 (라우팅 종착 무변경) ──────────────────────────────────


def test_미아_없음_unowned는_래퍼_통과_후에도_Pending_unowned():
    """0매칭→Unowned 종착이 래퍼 통과 후에도 동일."""
    pending = _make_pending_unowned()
    fake = FakeAskOrg(pending)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    reply = wrapper.handle("아무도 담당 안 하는 질문", _USER)

    assert isinstance(reply, Pending)
    assert reply.kind == "unowned"


def test_미아_없음_contested는_래퍼_통과_후에도_Pending_contested():
    """Contested 종착이 래퍼 통과 후에도 동일."""
    pending = _make_pending_contested()
    fake = FakeAskOrg(pending)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    reply = wrapper.handle("보상 질문", _USER)

    assert isinstance(reply, Pending)
    assert reply.kind == "contested"


def test_미아_없음_dispatched는_래퍼_통과_후에도_Pending_dispatched_with_tracking():
    """dispatched 종착과 tracking 토큰이 래퍼 통과 후에도 동일."""
    pending = _make_pending_dispatched()
    fake = FakeAskOrg(pending)
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    reply = wrapper.handle("비동기 질문", _USER)

    assert isinstance(reply, Pending)
    assert reply.kind == "dispatched"
    assert reply.tracking == "abc123"


# ── owner 격리 ────────────────────────────────────────────────────────


def test_alice와_bob_세션이_격리된다():
    """두 사용자의 세션이 독립 — alice 적재가 bob에 영향 없음."""
    alice = User(id="alice")
    bob = User(id="bob")

    class _UserFake:
        def handle(self, question: str, user: User) -> OrgReply:
            return _make_answered(text=f"{user.id} 답변", agent_id="contract_ops")

    fake = _UserFake()
    store = InMemorySessionStore(clock=_fixed_clock)
    wrapper = SessionAskOrg(ask=fake, session_store=store, clock=_fixed_clock)

    wrapper.handle("alice 질문", alice)
    wrapper.handle("bob 질문", bob)

    alice_session = store.active_for_user(alice.id)
    bob_session = store.active_for_user(bob.id)

    assert alice_session is not None
    assert bob_session is not None
    assert alice_session.session_id != bob_session.session_id
    assert len(alice_session.transcript) == 1
    assert len(bob_session.transcript) == 1
    assert alice_session.transcript[0].question == "alice 질문"
    assert bob_session.transcript[0].question == "bob 질문"
