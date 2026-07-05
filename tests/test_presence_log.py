"""Phase 13 SC2 — 프레즌스 이력(`PresenceEvent`/`PresenceLogStore`) + `online_ratio`
(ADR 0035·TRD §4 스코어카드 shape 절).

현재 `Presence`(상태 그릇 — owner별 *현재* 상태 하나만 덮어씀)와 구분되는 *온라인 비율
계산 원천*이다. connect/disconnect를 append-only 이력으로 남기고, `online_ratio`가
그 이력을 `[since, until)` 구간 적분해 온라인 비율을 계산한다.

불변식:
  - 전이 ≠ 기록: `PresenceLogStore.append`는 append-only 기록 — 상태 그릇(Presence)과
    분리된 별 축이다.
  - 미관측 owner(이력 없음)는 None(판정 불가) — 0.0(관측된 0% 온라인)과 구분한다.
"""

from datetime import datetime, timedelta, timezone

from agent_org_network.presence import (
    InMemoryPresenceLogStore,
    PresenceEvent,
    online_ratio,
)


def _t(seconds: int) -> datetime:
    return datetime(2026, 7, 5, 0, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


# ── PresenceEvent 값 객체 ──────────────────────────────────────────────


def test_presence_event는_frozen_값_객체다():
    event = PresenceEvent(owner_id="alice", status="online", at=_t(0))
    assert event.owner_id == "alice"
    assert event.status == "online"
    assert event.at == _t(0)


def test_presence_event는_수정_불가():
    import pydantic

    event = PresenceEvent(owner_id="alice", status="online", at=_t(0))
    try:
        event.status = "offline"  # type: ignore[misc]
    except (pydantic.ValidationError, AttributeError, TypeError):
        pass
    else:
        raise AssertionError("frozen 값 객체가 수정을 허용함")


# ── InMemoryPresenceLogStore — append-only + for_owner ────────────────


def test_append_후_for_owner가_시간순으로_돌려준다():
    store = InMemoryPresenceLogStore()
    e1 = PresenceEvent(owner_id="alice", status="online", at=_t(0))
    e2 = PresenceEvent(owner_id="alice", status="offline", at=_t(10))
    store.append(e1)
    store.append(e2)
    assert store.for_owner("alice") == [e1, e2]


def test_for_owner는_owner별로_독립적이다():
    store = InMemoryPresenceLogStore()
    store.append(PresenceEvent(owner_id="alice", status="online", at=_t(0)))
    store.append(PresenceEvent(owner_id="bob", status="online", at=_t(0)))
    assert len(store.for_owner("alice")) == 1
    assert len(store.for_owner("bob")) == 1


def test_미관측_owner는_빈_이력():
    store = InMemoryPresenceLogStore()
    assert store.for_owner("unknown") == []


# ── online_ratio — 순수 함수 구간 적분 ─────────────────────────────────


def test_미관측_owner는_None을_돌려준다():
    """이력 자체가 없으면 판정 불가(0.0과 구분) — 데이터 없음을 정직하게 표기."""
    assert online_ratio([], since=_t(0), until=_t(100)) is None


def test_전_구간_온라인이면_비율_1점0():
    events = [PresenceEvent(owner_id="alice", status="online", at=_t(0))]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 1.0


def test_전_구간_오프라인이면_비율_0점0():
    events = [PresenceEvent(owner_id="alice", status="offline", at=_t(0))]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.0


def test_절반_온라인_절반_오프라인():
    events = [
        PresenceEvent(owner_id="alice", status="online", at=_t(0)),
        PresenceEvent(owner_id="alice", status="offline", at=_t(50)),
    ]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.5


def test_경계_열린_구간_마지막이_online이면_until까지_온라인():
    """마지막 이벤트가 online이고 그 뒤로 disconnect가 없으면 until까지 온라인 취급(미해제 열린 구간)."""
    events = [
        PresenceEvent(owner_id="alice", status="offline", at=_t(0)),
        PresenceEvent(owner_id="alice", status="online", at=_t(75)),
    ]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.25


def test_경계_since_이전부터_온라인이던_경우_초기_상태로_채택():
    """since 이전 마지막 이벤트가 online이면 그 상태를 구간 시작(since)의 초기 상태로 채택한다."""
    events = [
        PresenceEvent(owner_id="alice", status="online", at=_t(-1000)),
        PresenceEvent(owner_id="alice", status="offline", at=_t(50)),
    ]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.5


def test_경계_since_이전부터_오프라인이던_경우_초기_상태로_채택():
    events = [
        PresenceEvent(owner_id="alice", status="offline", at=_t(-1000)),
        PresenceEvent(owner_id="alice", status="online", at=_t(50)),
    ]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.5


def test_경계_재연결_반복():
    """connect/disconnect가 여러 번 반복되는 구간 — 온라인 구간만 합산."""
    events = [
        PresenceEvent(owner_id="alice", status="online", at=_t(0)),
        PresenceEvent(owner_id="alice", status="offline", at=_t(20)),
        PresenceEvent(owner_id="alice", status="online", at=_t(40)),
        PresenceEvent(owner_id="alice", status="offline", at=_t(60)),
        PresenceEvent(owner_id="alice", status="online", at=_t(80)),
    ]
    # 온라인 구간: [0,20) + [40,60) + [80,100) = 20+20+20 = 60 / 100
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.6


def test_경계_online_online_중복은_멱등():
    """연속된 online 이벤트(재관측 중복)는 상태 변화 없음 — 중복이 비율을 왜곡하지 않는다."""
    events = [
        PresenceEvent(owner_id="alice", status="online", at=_t(0)),
        PresenceEvent(owner_id="alice", status="online", at=_t(30)),
        PresenceEvent(owner_id="alice", status="offline", at=_t(50)),
    ]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.5


def test_since_이후_이벤트만_있고_그_전은_미관측이면_그_시점부터_계산():
    """since 이전 이벤트가 전혀 없으면(첫 이벤트가 since 이후) 첫 이벤트 이전 구간은 미관측
    구간이라 오프라인으로 취급한다(안전측 기본 — Presence 미관측=offline 정신 재사용)."""
    events = [PresenceEvent(owner_id="alice", status="online", at=_t(50))]
    ratio = online_ratio(events, since=_t(0), until=_t(100))
    assert ratio == 0.5
