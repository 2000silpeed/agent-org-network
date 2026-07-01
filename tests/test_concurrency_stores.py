"""InMemorySessionStore·Registry 동시성 결함 회귀 테스트.

web.py 엔드포인트가 def(비 async)라 스레드풀에서 병렬 실행된다. 아래 경합을
threading.Barrier로 동시 출발시켜 재현한다(sleep 기반 타이밍 의존 금지 — 결정론).

불변식:
  - open_or_get: 같은 user_id에 대해 동시 호출해도 세션이 정확히 하나만 열린다
    (TOCTOU: idle 체크 → _auto_end → 새 세션 생성 사이 경합 방지).
  - append_turn: 같은 session_id에 동시 적재해도 턴 소실이 없다(전이≠기록 —
    기록 경로 자체가 스레드 안전해야 함).
  - register: 같은 agent_id로 동시 등록 시 정확히 1개만 성공한다
    ("유효하지 않은 카드는 등록되지 않는다" 불변식의 동시성 버전 — 중복 금지).
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from typing import Any

from agent_org_network.agent_card import AgentCard
from agent_org_network.registry import Registry, RegistryError
from agent_org_network.session import InMemorySessionStore, SessionTurn
from agent_org_network.user import User


class _BarrierGatedDict(dict[str, Any]):
    """check(`__contains__`/`get`)와 act(`__setitem__`) 사이에 barrier로 스레드를
    모아 경합 윈도우를 강제로 벌리는 테스트 전용 dict 서브클래스(락 없는 구현의
    TOCTOU 재현용).

    락으로 직렬화된 구현(수정 후)에서는 스레드가 한 번에 하나씩만 이 지점에
    도달하므로 barrier가 절대 다 채워지지 않는다 — 그 경우 즉시
    BrokenBarrierError로 깨지도록 짧은 timeout을 두고 무시한다(sleep 기반
    "재시도 타이밍" 의존이 아니라, 락이 걸려 데드락될 상황을 벗어나는 세이프티넷
    — 결과 성공/실패 판정은 timeout 값에 의존하지 않는다).
    """

    def __init__(self, barrier: threading.Barrier) -> None:
        super().__init__()
        self._barrier = barrier

    def _pass_gate(self) -> None:
        try:
            self._barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            pass

    def __contains__(self, key: object) -> bool:
        result = super().__contains__(key)
        self._pass_gate()
        return result

    def get(self, key: Any, default: Any = None) -> Any:  # type: ignore[override]
        result = super().get(key, default)
        self._pass_gate()
        return result


def _fixed_clock() -> datetime:
    return datetime(2026, 7, 2, 9, 0, 0, tzinfo=timezone.utc)


def _card(agent_id: str) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner="D",
        team="legal_ops",
        summary="계약 검토",
        domains=["계약 검토"],
        last_reviewed_at=date(2026, 6, 20),
    )


# ── InMemorySessionStore.open_or_get 동시성 ──────────────────────────────


def test_동시_open_or_get이_같은_user_id에_대해_같은_session_id를_반환한다():
    """N 스레드가 같은 user_id로 동시에 open_or_get 호출 → 전부 같은 session_id."""
    store = InMemorySessionStore(clock=_fixed_clock)
    n = 32
    barrier = threading.Barrier(n)
    results: list[str] = [""] * n

    def worker(idx: int) -> None:
        barrier.wait()
        session = store.open_or_get("user_alice")
        results[idx] = session.session_id

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    assert len(set(results)) == 1, f"session_id가 갈라짐: {set(results)}"


# ── InMemorySessionStore.append_turn 동시성 ──────────────────────────────


def test_동시_append_turn이_턴을_소실하지_않는다():
    """N 스레드가 같은 session_id에 동시 append_turn → 최종 턴 수 == N(소실 0).

    _active를 barrier-gated dict로 교체해 get(read)과 __setitem__(write) 사이
    경합 윈도우를 매번 100% 강제 재현한다(락이 걸리면 barrier.wait 자체가 락 내부
    에서 일어나므로 read-modify-write 전체가 직렬화돼 소실이 없어야 green).
    """
    store = InMemorySessionStore(clock=_fixed_clock)
    session = store.open_or_get("user_bob")
    n = 8
    barrier = threading.Barrier(n)
    store._active = _BarrierGatedDict(barrier)  # type: ignore[attr-defined]
    store._active[session.session_id] = session  # type: ignore[attr-defined]

    def worker(idx: int) -> None:
        turn = SessionTurn(
            question=f"질문{idx}",
            answer_text=f"답{idx}",
            answered_by="agent_x",
            at=_fixed_clock(),
        )
        store.append_turn(session.session_id, turn)

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    store._active = dict(store._active)  # type: ignore[attr-defined]  # 검증은 barrier 밖에서
    final = store.get(session.session_id)
    assert final is not None
    assert len(final.transcript) == n, f"턴 소실 발생: {len(final.transcript)} != {n}"


# ── Registry.register 동시성 ─────────────────────────────────────────────


def test_동시_register가_같은_agent_id면_정확히_1개만_성공한다():
    """N 스레드가 같은 agent_id로 동시 register → 성공 1건, 나머지는 RegistryError.

    _cards를 barrier-gated dict로 교체해 중복 체크(`in`)와 쓰기(`[]=`) 사이
    경합 윈도우를 매번 100% 강제 재현한다.
    """
    registry = Registry()
    n = 8
    barrier = threading.Barrier(n)
    registry._cards = _BarrierGatedDict(barrier)  # type: ignore[attr-defined]
    successes: list[bool] = [False] * n
    errors: list[bool] = [False] * n

    def worker(idx: int) -> None:
        card = _card("dup_agent")
        try:
            registry.register(card)
            successes[idx] = True
        except RegistryError:
            errors[idx] = True

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    assert sum(successes) == 1, f"성공 건수 {sum(successes)} != 1"
    assert sum(errors) == n - 1, f"실패 건수 {sum(errors)} != {n - 1}"


def test_동시_register_user가_같은_user_id면_정확히_1개만_성공한다():
    """register_user도 register와 동일한 동시성 보장이 필요하다."""
    registry = Registry()
    n = 8
    barrier = threading.Barrier(n)
    registry._users = _BarrierGatedDict(barrier)  # type: ignore[attr-defined]
    successes: list[bool] = [False] * n
    errors: list[bool] = [False] * n

    def worker(idx: int) -> None:
        user = User(id="dup_user")
        try:
            registry.register_user(user)
            successes[idx] = True
        except RegistryError:
            errors[idx] = True

    with ThreadPoolExecutor(max_workers=n) as pool:
        list(pool.map(worker, range(n)))

    assert sum(successes) == 1, f"성공 건수 {sum(successes)} != 1"
    assert sum(errors) == n - 1, f"실패 건수 {sum(errors)} != {n - 1}"
