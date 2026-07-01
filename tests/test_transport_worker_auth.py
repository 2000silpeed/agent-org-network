"""T9.5(b) — `WebSocketDispatcher._authenticate` stub → `TokenStore` 실 검증 교체.

ADR 0026 결정 2 의사코드대로: 빈 owner_id 거부(기존) → token None 거부(신규) →
TokenStore.verify(만료/revoke/위조 거부) → 토큰 owner_id·role이 RegisterWorker
선언과 일치해야 admission. `token_store` 미주입이면 기존 stub 동작 그대로
(하위호환 — 기존 WS 테스트 전부 보존).

결정론: 고정 clock(WebSocketDispatcher의 clock == TokenStore 검증에 쓰는 now),
결정론 token_factory, Fake send 콜백. 실 네트워크·실 토큰 랜덤성 0.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Callable

from agent_org_network.agent_card import AgentCard
from agent_org_network.token import InMemoryTokenStore, WorkerRole
from agent_org_network.transport import (
    AuthError,
    CentralFrame,
    PushWork,
    RegisterWorker,
    WebSocketDispatcher,
    Welcome,
)


def _fixed_clock(ts: datetime) -> Callable[[], datetime]:
    return lambda: ts


BASE_TS = datetime(2026, 6, 27, 0, 0, 0, tzinfo=timezone.utc)


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[CentralFrame] = []

    def __call__(self, frame: CentralFrame) -> None:
        self.sent.append(frame)


def _store_with_token(
    owner_id: str = "alice",
    role: WorkerRole = "primary",
    *,
    expires_in: timedelta | None = None,
) -> tuple[InMemoryTokenStore, str]:
    store = InMemoryTokenStore(token_factory=lambda: "raw-token-1")
    raw, _token = store.issue(owner_id, role, now=BASE_TS, expires_in=expires_in)
    return store, raw


def _fixed_card(owner: str = "alice", agent_id: str = "cs_ops") -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="cs",
        summary="s",
        domains=["cs"],
        last_reviewed_at=date(2026, 6, 20),
    )


# ── 유효 토큰 register 통과 ──────────────────────────────────────────────────


def test_유효_토큰이면_register가_Welcome을_반환한다():
    store, raw = _store_with_token(owner_id="alice", role="primary")
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)
    rec = _Recorder()

    reply = dispatcher.register(
        RegisterWorker(owner_id="alice", token=raw, role="primary"), rec
    )

    assert isinstance(reply, Welcome)
    # 등록됐으므로 이후 dispatch가 push된다.
    dispatcher.dispatch("Q", _fixed_card(owner="alice"))
    assert any(isinstance(f, PushWork) for f in rec.sent)


# ── token None 거부 ──────────────────────────────────────────────────────────


def test_token_store_주입시_token_None이면_AuthError로_거부된다():
    store, _raw = _store_with_token(owner_id="alice")
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)
    rec = _Recorder()

    reply = dispatcher.register(RegisterWorker(owner_id="alice", token=None), rec)

    assert isinstance(reply, AuthError)


# ── 무효/위조 토큰 거부 ───────────────────────────────────────────────────────


def test_위조_토큰은_AuthError로_거부된다():
    store, _raw = _store_with_token(owner_id="alice")
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)
    rec = _Recorder()

    reply = dispatcher.register(
        RegisterWorker(owner_id="alice", token="위조된-토큰"), rec
    )

    assert isinstance(reply, AuthError)


# ── revoke된 토큰 거부 ───────────────────────────────────────────────────────


def test_revoke된_토큰은_AuthError로_거부된다():
    store = InMemoryTokenStore(token_factory=lambda: "raw-token-1")
    raw, token = store.issue("alice", "primary", now=BASE_TS)
    store.revoke(token.token_id)
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)
    rec = _Recorder()

    reply = dispatcher.register(RegisterWorker(owner_id="alice", token=raw), rec)

    assert isinstance(reply, AuthError)


def test_별도_토큰_revoke가_다른_owner_토큰에_영향_없다():
    store = InMemoryTokenStore(token_factory=_seq_factory(["tok-a", "tok-b"]))
    raw_a, token_a = store.issue("alice", "primary", now=BASE_TS)
    raw_b, _token_b = store.issue("bob", "primary", now=BASE_TS)
    store.revoke(token_a.token_id)

    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)

    reply_a = dispatcher.register(RegisterWorker(owner_id="alice", token=raw_a), _Recorder())
    reply_b = dispatcher.register(RegisterWorker(owner_id="bob", token=raw_b), _Recorder())

    assert isinstance(reply_a, AuthError)
    assert isinstance(reply_b, Welcome)


def _seq_factory(values: list[str]) -> Callable[[], str]:
    it = iter(values)

    def factory() -> str:
        return next(it)

    return factory


# ── 만료 토큰 거부(clock 진전) ────────────────────────────────────────────────


def test_만료된_토큰은_AuthError로_거부된다():
    store, raw = _store_with_token(owner_id="alice", expires_in=timedelta(hours=1))
    later = BASE_TS + timedelta(hours=2)
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(later), token_store=store)

    reply = dispatcher.register(RegisterWorker(owner_id="alice", token=raw), _Recorder())

    assert isinstance(reply, AuthError)


def test_만료_전이면_register가_통과한다():
    store, raw = _store_with_token(owner_id="alice", expires_in=timedelta(hours=1))
    before = BASE_TS + timedelta(minutes=30)
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(before), token_store=store)

    reply = dispatcher.register(RegisterWorker(owner_id="alice", token=raw), _Recorder())

    assert isinstance(reply, Welcome)


# ── 토큰 owner ≠ RegisterWorker 선언 owner 거부 ───────────────────────────────


def test_토큰_owner와_선언_owner가_다르면_거부된다():
    store, raw = _store_with_token(owner_id="alice")
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)

    # 토큰은 alice 것인데 bob으로 신원 선언 — 가장 시도.
    reply = dispatcher.register(RegisterWorker(owner_id="bob", token=raw), _Recorder())

    assert isinstance(reply, AuthError)


# ── role 불일치 거부 ──────────────────────────────────────────────────────────


def test_토큰_role과_선언_role이_다르면_거부된다():
    store, raw = _store_with_token(owner_id="alice", role="backup")
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)

    # 토큰은 backup 발급인데 primary로 선언 — 등급 위조 시도.
    reply = dispatcher.register(
        RegisterWorker(owner_id="alice", token=raw, role="primary"), _Recorder()
    )

    assert isinstance(reply, AuthError)


def test_토큰_role과_선언_role이_같으면_통과한다():
    store, raw = _store_with_token(owner_id="alice", role="backup")
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)

    reply = dispatcher.register(
        RegisterWorker(owner_id="alice", token=raw, role="backup"), _Recorder()
    )

    assert isinstance(reply, Welcome)


# ── 미인증 연결의 SubmitAnswer 차단(등록 안 됨 → push 대상 아님) ──────────────


def test_인증_실패한_워커는_등록되지_않아_이후_작업이_push되지_않는다():
    store, _raw = _store_with_token(owner_id="alice")
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS), token_store=store)
    rec = _Recorder()

    reply = dispatcher.register(
        RegisterWorker(owner_id="alice", token="위조된-토큰"), rec
    )
    assert isinstance(reply, AuthError)

    dispatcher.dispatch("Q", _fixed_card(owner="alice"))

    # 미인증 연결의 send 콜백은 레지스트리에 없으므로 PushWork를 못 받는다 —
    # 즉 그 워커는 SubmitAnswer를 보낼 기회(작업)조차 갖지 못한다(owner 격리).
    assert [f for f in rec.sent if isinstance(f, PushWork)] == []


# ── 하위호환: token_store 미주입이면 기존 stub 동작 그대로 ───────────────────


def test_token_store_미주입이면_owner_id만_있어도_통과한다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()

    # 기존 스타일: token 없이 owner_id만으로 register(기존 전체 WS 테스트가 이 형태).
    reply = dispatcher.register(RegisterWorker(owner_id="alice"), rec)

    assert isinstance(reply, Welcome)


def test_token_store_미주입이면_빈_owner_id는_여전히_거부된다():
    dispatcher = WebSocketDispatcher(clock=_fixed_clock(BASE_TS))
    rec = _Recorder()

    reply = dispatcher.register(RegisterWorker(owner_id=""), rec)

    assert isinstance(reply, AuthError)
