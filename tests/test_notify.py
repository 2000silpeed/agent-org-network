"""T7.4 슬라이스 1~3 + T8.2 슬라이스 D~F — 실시간 충돌 푸시 통지 결정론 테스트 (ADR 0022).

전부 결정론: FakeChannel(in-memory inbox)·주입 clock·주입 구독 맵.
실 네트워크·실 채널·실 LLM 0.

커버 범위:
  슬라이스 1 (FakeChannel.send / for_recipient):
    - send → inbox 적재·delivered 누적
    - 여러 recipient 격리
    - 같은 recipient 복수 통지 순서
  슬라이스 2 (Notifier.notify):
    - 구독 recipient → send 도달
    - 미구독 recipient → skip(send 0회)
    - 멱등: 같은 (recipient,kind,subject_ref) 두 번 → send 1회
    - 다른 subject_ref는 각각 send
    - 채널 send 예외 → Notifier가 삼킴(다른 통지 계속)
    - 실패 후 재시도(멱등 키 미기록 확인)
  슬라이스 3 (발화 지점):
    - reeval: OKF 커밋→StalenessPropagator(notifier 주입)→ReevalItem 적재 + owner 통지
    - reeval: notifier 미주입 → 적재만(통지 0)
    - reeval: 멱등(같은 발화 두 번 → 통지 1회)
    - conflict: Contested→ask_org(notifier 주입)→ConflictCase open + 후보 owner 통지
    - conflict: notifier 미주입 → 통지 0
    - conflict: 채널 실패해도 처리함 적재(미아 없음 회귀)
  슬라이스 D (render_mcp_notification·T8.2):
    - kind별 4종 안내 문자열 + (대상: subject_ref) 손잡이
    - 노출 불변식: subject_ref 보존 + 내부값 9종(confidence·candidate·reason·
      manager_id·ticket_id·intent·primary·question·escalated_to) 비노출
  슬라이스 E (McpChannel transport 주입·T8.2):
    - Fake send_fn 주입 → (recipient_id, render 결과)로 정확히 1회 호출
    - send_fn 미주입 → NotImplementedError(no-op 아님)
  슬라이스 F (fire-and-forget·T8.2):
    - send_fn 예외 → Notifier가 삼킴(호출자로 안 샘) + 멱등 키 미기록 → 재시도 가능
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Any

import pytest

from agent_org_network.notify import (
    FakeChannel,
    McpChannel,
    Notification,
    Notifier,
    render_mcp_notification,
)

_TS = datetime(2026, 6, 23, 9, 0, 0, tzinfo=timezone.utc)
_CLOCK = lambda: _TS  # noqa: E731


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 1 — FakeChannel
# ══════════════════════════════════════════════════════════════════════════════


def _notif(
    recipient_id: str = "owner_a",
    kind: str = "reeval_flagged",
    subject_ref: str = "intent_a",
) -> Notification:
    return Notification(
        recipient_id=recipient_id,
        kind=kind,  # type: ignore[arg-type]
        subject_ref=subject_ref,
        created_at=_TS,
    )


class TestSlice1FakeChannel:
    def test_send_후_for_recipient로_조회된다(self) -> None:
        ch = FakeChannel()
        n = _notif("alice")
        ch.send(n)
        assert ch.for_recipient("alice") == [n]

    def test_send_후_delivered에_누적된다(self) -> None:
        ch = FakeChannel()
        n = _notif("alice")
        ch.send(n)
        assert ch.delivered == [n]

    def test_여러_recipient_격리된다(self) -> None:
        ch = FakeChannel()
        na = _notif("alice")
        nb = _notif("bob")
        ch.send(na)
        ch.send(nb)
        assert ch.for_recipient("alice") == [na]
        assert ch.for_recipient("bob") == [nb]

    def test_같은_recipient_복수_통지_순서_보존(self) -> None:
        ch = FakeChannel()
        n1 = _notif("alice", subject_ref="ref_1")
        n2 = _notif("alice", subject_ref="ref_2")
        ch.send(n1)
        ch.send(n2)
        assert ch.for_recipient("alice") == [n1, n2]

    def test_미전달_recipient_빈_리스트(self) -> None:
        ch = FakeChannel()
        assert ch.for_recipient("nobody") == []

    def test_delivered는_전체_누적(self) -> None:
        ch = FakeChannel()
        n1 = _notif("alice")
        n2 = _notif("bob")
        ch.send(n1)
        ch.send(n2)
        assert ch.delivered == [n1, n2]


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 2 — Notifier.notify
# ══════════════════════════════════════════════════════════════════════════════


class TestSlice2Notifier:
    def test_구독된_recipient는_send_도달한다(self) -> None:
        ch = FakeChannel()
        notifier = Notifier(subscriptions={"alice": ch})
        n = _notif("alice")
        notifier.notify(n)
        assert ch.for_recipient("alice") == [n]

    def test_미구독_recipient는_send_호출_0회(self) -> None:
        ch = FakeChannel()
        notifier = Notifier(subscriptions={"alice": ch})
        n = _notif("bob")
        notifier.notify(n)
        assert ch.delivered == []

    def test_같은_키_두_번_notify는_send_1회(self) -> None:
        ch = FakeChannel()
        notifier = Notifier(subscriptions={"alice": ch})
        n = _notif("alice", kind="reeval_flagged", subject_ref="ref_x")
        notifier.notify(n)
        notifier.notify(n)
        assert len(ch.for_recipient("alice")) == 1

    def test_다른_subject_ref는_각각_send된다(self) -> None:
        ch = FakeChannel()
        notifier = Notifier(subscriptions={"alice": ch})
        n1 = _notif("alice", subject_ref="ref_1")
        n2 = _notif("alice", subject_ref="ref_2")
        notifier.notify(n1)
        notifier.notify(n2)
        assert len(ch.for_recipient("alice")) == 2

    def test_채널_send_예외를_Notifier가_삼킨다(self) -> None:
        class BrokenChannel:
            def send(self, notification: Notification) -> None:
                raise RuntimeError("채널 다운")

        notifier = Notifier(subscriptions={"alice": BrokenChannel()})
        n = _notif("alice")
        notifier.notify(n)  # 예외가 전파되지 않아야 한다

    def test_채널_예외_후_다른_통지는_계속된다(self) -> None:
        class BrokenChannel:
            def send(self, notification: Notification) -> None:
                raise RuntimeError("채널 다운")

        ch_good = FakeChannel()
        notifier = Notifier(subscriptions={"alice": BrokenChannel(), "bob": ch_good})
        notifier.notify(_notif("alice"))
        notifier.notify(_notif("bob"))
        assert len(ch_good.for_recipient("bob")) == 1

    def test_send_실패_후_재시도_시_멱등_키_미기록이라_재전송된다(self) -> None:
        """send 실패 시 멱등 키를 기록하지 않아야 다음 발화에 재시도된다."""
        call_count = 0

        class UnreliableChannel:
            def send(self, notification: Notification) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise RuntimeError("첫 번째 실패")

        notifier = Notifier(subscriptions={"alice": UnreliableChannel()})
        n = _notif("alice")
        notifier.notify(n)  # 첫 실패 — 삼킴
        notifier.notify(n)  # 두 번째: 멱등 키 없으면 재시도
        assert call_count == 2

    def test_구독_없이_Notifier_생성_가능(self) -> None:
        notifier = Notifier()
        n = _notif("alice")
        notifier.notify(n)  # 오류 없이 skip

    def test_같은_recipient_다른_kind는_각각_send된다(self) -> None:
        ch = FakeChannel()
        notifier = Notifier(subscriptions={"alice": ch})
        n1 = _notif("alice", kind="reeval_flagged", subject_ref="ref_x")
        n2 = _notif("alice", kind="conflict_opened", subject_ref="ref_x")
        notifier.notify(n1)
        notifier.notify(n2)
        assert len(ch.for_recipient("alice")) == 2

    def test_같은_키_32_way_동시_notify는_send_한_번이다(self) -> None:
        send_started = Event()
        release_send = Event()
        calls_lock = Lock()
        calls = 0

        class BlockingChannel:
            def send(self, notification: Notification) -> None:
                del notification
                nonlocal calls
                with calls_lock:
                    calls += 1
                send_started.set()
                assert release_send.wait(timeout=5)

        notifier = Notifier(subscriptions={"alice": BlockingChannel()})
        notification = _notif("alice", subject_ref="same-ref")

        with ThreadPoolExecutor(max_workers=32) as pool:
            first = pool.submit(notifier.notify, notification)
            assert send_started.wait(timeout=5)
            others = [pool.submit(notifier.notify, notification) for _ in range(31)]
            _, blocked = wait(others, timeout=1)
            all_duplicates_skipped = not blocked
            release_send.set()
            first.result(timeout=5)
            for future in others:
                future.result(timeout=5)

        assert all_duplicates_skipped
        assert calls == 1

    def test_같은_키_동기_재진입은_중복이나_deadlock이_없다(self) -> None:
        calls = 0
        notifier: Notifier

        class ReentrantChannel:
            def send(self, notification: Notification) -> None:
                nonlocal calls
                calls += 1
                notifier.notify(notification)

        notifier = Notifier(subscriptions={"alice": ReentrantChannel()})

        notifier.notify(_notif("alice", subject_ref="same-ref"))

        assert calls == 1

    def test_다른_키_동기_재진입은_각각_send된다(self) -> None:
        delivered: list[Notification] = []
        inner = _notif("alice", subject_ref="inner-ref")
        notifier: Notifier

        class ReentrantChannel:
            def send(self, notification: Notification) -> None:
                delivered.append(notification)
                if notification.subject_ref == "outer-ref":
                    notifier.notify(inner)

        notifier = Notifier(subscriptions={"alice": ReentrantChannel()})
        outer = _notif("alice", subject_ref="outer-ref")

        notifier.notify(outer)

        assert delivered == [outer, inner]

    def test_한_키_send_대기_중에도_다른_키는_진행한다(self) -> None:
        outer_started = Event()
        release_outer = Event()
        inner_delivered = Event()

        class BlockingOneKeyChannel:
            def send(self, notification: Notification) -> None:
                if notification.subject_ref == "outer-ref":
                    outer_started.set()
                    assert release_outer.wait(timeout=5)
                    return
                inner_delivered.set()

        notifier = Notifier(subscriptions={"alice": BlockingOneKeyChannel()})
        outer = _notif("alice", subject_ref="outer-ref")
        inner = _notif("alice", subject_ref="inner-ref")

        with ThreadPoolExecutor(max_workers=2) as pool:
            outer_future = pool.submit(notifier.notify, outer)
            assert outer_started.wait(timeout=5)
            inner_future = pool.submit(notifier.notify, inner)
            progressed_independently = inner_delivered.wait(timeout=1)
            release_outer.set()
            outer_future.result(timeout=5)
            inner_future.result(timeout=5)

        assert progressed_independently


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 3 — 발화 지점: reeval.py StalenessPropagator
# ══════════════════════════════════════════════════════════════════════════════


class TestSlice3ReevalNotification:
    def _make_propagator(self, notifier: Notifier | None = None) -> Any:
        from agent_org_network.conflict import (
            InMemoryPrecedentStore,
            Resolution,
        )
        from agent_org_network.audit import InMemoryAuditLog
        from agent_org_network.reeval import InMemoryReevalStore, StalenessPropagator

        precedents = InMemoryPrecedentStore()
        precedents.record(Resolution(intent="refund_policy", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        audit = InMemoryAuditLog()
        return (
            StalenessPropagator(
                precedents=precedents,
                audit_reader=audit,
                reeval_store=reeval_store,
                owner_of=lambda agent_id: "cs_lead",
                clock=_CLOCK,
                notifier=notifier,
            ),
            reeval_store,
        )

    def test_notifier_주입_시_ReevalItem_적재_및_owner_통지된다(self) -> None:
        from agent_org_network.git_gateway import OkfChangeEvent

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch})
        propagator, reeval_store = self._make_propagator(notifier=notifier)

        event = OkfChangeEvent(
            agent_id="cs_ops",
            new_sha="sha_new",
            parent_sha=None,
            changed_paths=("policy.md",),
            author="builder",
            committed_at=_TS,
        )
        propagator.on_okf_committed(event)

        pending = reeval_store.pending_for_owner("cs_lead")
        assert len(pending) == 1
        notifications = ch.for_recipient("cs_lead")
        assert len(notifications) == 1
        assert notifications[0].kind == "reeval_flagged"
        assert notifications[0].recipient_id == "cs_lead"
        assert notifications[0].created_at == _TS

    def test_notifier_미주입이면_적재만_통지_0(self) -> None:
        from agent_org_network.git_gateway import OkfChangeEvent

        ch = FakeChannel()
        propagator, reeval_store = self._make_propagator(notifier=None)

        event = OkfChangeEvent(
            agent_id="cs_ops",
            new_sha="sha_new",
            parent_sha=None,
            changed_paths=("policy.md",),
            author="builder",
            committed_at=_TS,
        )
        propagator.on_okf_committed(event)

        pending = reeval_store.pending_for_owner("cs_lead")
        assert len(pending) == 1
        assert ch.delivered == []

    def test_같은_발화_두_번_통지는_1회(self) -> None:
        from agent_org_network.git_gateway import OkfChangeEvent
        from agent_org_network.conflict import (
            InMemoryPrecedentStore,
            Resolution,
        )
        from agent_org_network.audit import InMemoryAuditLog
        from agent_org_network.reeval import InMemoryReevalStore, StalenessPropagator

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch})

        precedents = InMemoryPrecedentStore()
        precedents.record(Resolution(intent="refund_policy", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        audit = InMemoryAuditLog()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda agent_id: "cs_lead",
            clock=_CLOCK,
            notifier=notifier,
        )

        event = OkfChangeEvent(
            agent_id="cs_ops",
            new_sha="sha_new",
            parent_sha=None,
            changed_paths=("policy.md",),
            author="builder",
            committed_at=_TS,
        )
        propagator.on_okf_committed(event)
        # 두 번째 — 판례는 이미 needs_review라 ReevalItem 적재 skip → 통지 발화도 skip
        propagator.on_okf_committed(event)

        assert len(ch.for_recipient("cs_lead")) == 1

    def test_owner_of_None이면_미귀속이라_통지_0(self) -> None:
        """owner_of가 None을 반환(미귀속)하면 recipient_id가 빈 문자열이라 push 0 —
        처리함 적재는 그대로다(M1 가드·미아 없음은 pull이 떠받침·ADR 0022 결정 2·6)."""
        from agent_org_network.git_gateway import OkfChangeEvent
        from agent_org_network.conflict import (
            InMemoryPrecedentStore,
            Resolution,
        )
        from agent_org_network.audit import InMemoryAuditLog
        from agent_org_network.reeval import InMemoryReevalStore, StalenessPropagator

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch})

        precedents = InMemoryPrecedentStore()
        precedents.record(Resolution(intent="refund_policy", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        audit = InMemoryAuditLog()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda agent_id: None,
            clock=_CLOCK,
            notifier=notifier,
        )

        event = OkfChangeEvent(
            agent_id="cs_ops",
            new_sha="sha_new",
            parent_sha=None,
            changed_paths=("policy.md",),
            author="builder",
            committed_at=_TS,
        )
        propagator.on_okf_committed(event)

        # 적재는 그대로(미귀속 owner_id="" — pull이 떠받침)
        assert len(reeval_store.pending_for_owner("")) == 1
        # 통지는 0(M1 가드 — recipient_id 빈 문자열엔 push 안 함)
        assert ch.delivered == []


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 3 — 발화 지점: ask_org.py AskOrg (Contested arm)
# ══════════════════════════════════════════════════════════════════════════════


class TestSlice3ConflictNotification:
    def _make_ask_org(self, notifier: Notifier | None = None) -> Any:
        from datetime import date
        from agent_org_network.agent_card import AgentCard
        from agent_org_network.ask_org import AskOrg
        from agent_org_network.audit import InMemoryAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import InMemoryConflictCaseStore
        from agent_org_network.dispatch import LocalRuntimeDispatcher
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.user import User

        registry = Registry()
        registry.register(
            AgentCard(
                agent_id="cs_ops",
                owner="cs_lead",
                team="ops",
                summary="CS",
                domains=["환불"],
                last_reviewed_at=date(2026, 6, 20),
            )
        )
        registry.register(
            AgentCard(
                agent_id="legal_ops",
                owner="legal_lead",
                team="legal",
                summary="법무",
                domains=["환불"],
                last_reviewed_at=date(2026, 6, 20),
            )
        )
        registry.register_user(User(id="cs_lead", email="cs@example.com"))
        registry.register_user(User(id="legal_lead", email="legal@example.com"))

        classifier = FakeClassifier("환불")
        router = Router(registry, classifier, root_user="root")
        case_store = InMemoryConflictCaseStore()
        return AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=InMemoryAuditLog(),
            clock=_CLOCK,
            case_store=case_store,
            notifier=notifier,
        ), case_store

    def test_notifier_주입_시_ConflictCase_open_및_후보_owner_통지된다(self) -> None:
        from agent_org_network.user import User

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch, "legal_lead": ch})
        ask_org, case_store = self._make_ask_org(notifier=notifier)

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("환불 문의", user)

        cases = case_store.open_for_owner("cs_lead")
        assert len(cases) == 1
        # 두 후보 owner 각각에게 통지
        cs_notifs = ch.for_recipient("cs_lead")
        legal_notifs = ch.for_recipient("legal_lead")
        assert len(cs_notifs) == 1
        assert cs_notifs[0].kind == "conflict_opened"
        assert len(legal_notifs) == 1
        assert legal_notifs[0].kind == "conflict_opened"
        # subject_ref은 case_id
        assert cs_notifs[0].subject_ref == cases[0].case_id
        assert legal_notifs[0].subject_ref == cases[0].case_id

    def test_notifier_미주입이면_ConflictCase_open_통지_0(self) -> None:
        from agent_org_network.user import User

        ch = FakeChannel()
        ask_org, case_store = self._make_ask_org(notifier=None)

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("환불 문의", user)

        cases = case_store.open_for_owner("cs_lead")
        assert len(cases) == 1
        assert ch.delivered == []

    def test_채널_실패해도_ConflictCase_처리함_적재_미아_없음(self) -> None:
        """통지 채널이 실패해도 ConflictCase는 처리함에 남아있다(미아 없음 회귀)."""
        from agent_org_network.user import User

        class BrokenChannel:
            def send(self, notification: Notification) -> None:
                raise RuntimeError("채널 다운")

        notifier = Notifier(
            subscriptions={"cs_lead": BrokenChannel(), "legal_lead": BrokenChannel()}
        )
        ask_org, case_store = self._make_ask_org(notifier=notifier)

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("환불 문의", user)

        cases = case_store.open_for_owner("cs_lead")
        assert len(cases) == 1

    def test_같은_다툼_두_번_발화_통지는_1회(self) -> None:
        """같은 case에 두 번째 질문이 Contested를 내도 이미 open이면 case_store skip → 통지 1회."""
        from agent_org_network.user import User

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch, "legal_lead": ch})
        ask_org, _ = self._make_ask_org(notifier=notifier)

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("환불 문의", user)
        ask_org.handle("환불 문의 재질문", user)

        cs_notifs = ch.for_recipient("cs_lead")
        assert len(cs_notifs) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 A — Manager enqueue 발화 (ask_org.py)
# ══════════════════════════════════════════════════════════════════════════════


class TestSliceAManagerNotification:
    def _make_ask_org_with_manager(self, notifier: "Notifier | None" = None) -> "tuple[Any, Any]":
        from datetime import date

        from agent_org_network.agent_card import AgentCard
        from agent_org_network.ask_org import AskOrg
        from agent_org_network.audit import InMemoryAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import InMemoryConflictCaseStore
        from agent_org_network.dispatch import LocalRuntimeDispatcher
        from agent_org_network.manager_queue import InMemoryManagerQueueStore
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.user import User

        registry = Registry()
        registry.register(
            AgentCard(
                agent_id="cs_ops",
                owner="cs_lead",
                team="ops",
                summary="CS",
                domains=["환불"],
                last_reviewed_at=date(2026, 6, 20),
            )
        )
        registry.register_user(User(id="cs_lead", email="cs@example.com"))

        classifier = FakeClassifier("없는도메인")
        router = Router(registry, classifier, root_user="root_manager")
        manager_queue_store = InMemoryManagerQueueStore()
        return (
            AskOrg(
                router=router,
                dispatcher=LocalRuntimeDispatcher(StubRuntime()),
                audit_log=InMemoryAuditLog(),
                clock=_CLOCK,
                case_store=InMemoryConflictCaseStore(),
                manager_queue_store=manager_queue_store,
                manager_root="root_manager",
                notifier=notifier,
            ),
            manager_queue_store,
        )

    def test_Unowned_enqueue_후_manager에게_escalated_통지된다(self) -> None:
        from agent_org_network.user import User

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"root_manager": ch})
        ask_org, manager_queue_store = self._make_ask_org_with_manager(notifier=notifier)

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("아무도_없는_도메인 문의", user)

        items = manager_queue_store.pending_for_manager("root_manager")
        assert len(items) == 1
        notifs = ch.for_recipient("root_manager")
        assert len(notifs) == 1
        assert notifs[0].kind == "manager_escalated"
        assert notifs[0].subject_ref == items[0].item_id
        assert notifs[0].created_at == _TS

    def test_Unowned_enqueue_notifier_미주입이면_통지_0(self) -> None:
        from agent_org_network.user import User

        ch = FakeChannel()
        ask_org, manager_queue_store = self._make_ask_org_with_manager(notifier=None)

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("아무도_없는_도메인 문의", user)

        items = manager_queue_store.pending_for_manager("root_manager")
        assert len(items) == 1
        assert ch.delivered == []

    def test_채널_실패해도_Manager_큐_적재_미아_없음(self) -> None:
        from agent_org_network.user import User

        class BrokenChannel:
            def send(self, notification: Notification) -> None:
                raise RuntimeError("채널 다운")

        notifier = Notifier(subscriptions={"root_manager": BrokenChannel()})
        ask_org, manager_queue_store = self._make_ask_org_with_manager(notifier=notifier)

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("아무도_없는_도메인 문의", user)

        items = manager_queue_store.pending_for_manager("root_manager")
        assert len(items) == 1

    def _make_ask_org_for_deadlock(
        self, notifier: "Notifier | None" = None
    ) -> "tuple[Any, Any, Any]":
        from datetime import date

        from agent_org_network.agent_card import AgentCard
        from agent_org_network.ask_org import AskOrg
        from agent_org_network.audit import InMemoryAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import InMemoryConflictCaseStore
        from agent_org_network.dispatch import LocalRuntimeDispatcher
        from agent_org_network.manager_queue import InMemoryManagerQueueStore
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.user import User

        registry = Registry()
        registry.register(
            AgentCard(
                agent_id="cs_ops",
                owner="cs_lead",
                team="ops",
                summary="CS",
                domains=["환불"],
                last_reviewed_at=date(2026, 6, 20),
            )
        )
        registry.register(
            AgentCard(
                agent_id="legal_ops",
                owner="legal_lead",
                team="legal",
                summary="법무",
                domains=["환불"],
                last_reviewed_at=date(2026, 6, 20),
            )
        )
        registry.register_user(User(id="cs_lead", email="cs@example.com"))
        registry.register_user(User(id="legal_lead", email="legal@example.com"))

        classifier = FakeClassifier("환불")
        router = Router(registry, classifier, root_user="root_manager")
        case_store = InMemoryConflictCaseStore()
        manager_queue_store = InMemoryManagerQueueStore()
        ask_org = AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=InMemoryAuditLog(),
            clock=_CLOCK,
            case_store=case_store,
            manager_queue_store=manager_queue_store,
            manager_root="root_manager",
            manager_of=lambda uid: None,
            notifier=notifier,
        )
        return ask_org, case_store, manager_queue_store

    def test_Deadlock_enqueue_후_manager에게_escalated_통지된다(self) -> None:
        from agent_org_network.user import User

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"root_manager": ch})
        ask_org, case_store, manager_queue_store = self._make_ask_org_for_deadlock(
            notifier=notifier
        )

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("환불 문의", user)
        cases = case_store.open_for_owner("cs_lead")
        assert len(cases) == 1
        case = cases[0]
        ask_org.enqueue_deadlock(case, reason="교착")

        items = manager_queue_store.pending_for_manager("root_manager")
        assert len(items) == 1
        notifs = ch.for_recipient("root_manager")
        manager_notifs = [n for n in notifs if n.kind == "manager_escalated"]
        assert len(manager_notifs) == 1
        assert manager_notifs[0].subject_ref == items[0].item_id

    def test_같은_case_deadlock_두_번_enqueue_통지는_1회(self) -> None:
        from agent_org_network.user import User

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"root_manager": ch})
        ask_org, case_store, manager_queue_store = self._make_ask_org_for_deadlock(
            notifier=notifier
        )

        user = User(id="user_1", email="user@example.com")
        ask_org.handle("환불 문의", user)
        cases = case_store.open_for_owner("cs_lead")
        case = cases[0]

        ask_org.enqueue_deadlock(case, reason="교착1")
        ask_org.enqueue_deadlock(case, reason="교착2")

        items = manager_queue_store.pending_for_manager("root_manager")
        assert len(items) == 1
        manager_notifs = [n for n in ch.delivered if n.kind == "manager_escalated"]
        assert len(manager_notifs) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 B — BackupReview add 발화 (transport.py)
# ══════════════════════════════════════════════════════════════════════════════


class TestSliceBBackupReviewNotification:
    def _make_dispatcher(self, notifier: "Notifier | None" = None) -> "tuple[Any, Any]":
        from agent_org_network.dispatch import InMemoryWorkQueueDispatcher
        from agent_org_network.review import InMemoryBackupReviewStore
        from agent_org_network.transport import WebSocketDispatcher

        review_store = InMemoryBackupReviewStore()
        dispatcher = WebSocketDispatcher(
            clock=_CLOCK,
            queue=InMemoryWorkQueueDispatcher(clock=_CLOCK),
            review_store=review_store,
            notifier=notifier,
        )
        return dispatcher, review_store

    def _submit_backup_answer(self, dispatcher: "Any") -> "Any":
        from datetime import date
        from agent_org_network.agent_card import AgentCard
        from agent_org_network.runtime import Answer
        from agent_org_network.transport import RegisterWorker

        card = AgentCard(
            agent_id="cs_ops",
            owner="cs_lead",
            team="ops",
            summary="CS",
            domains=["환불"],
            last_reviewed_at=date(2026, 6, 20),
        )
        ticket = dispatcher.dispatch("환불 문의", card)

        send_frames: list[Any] = []

        def send_backup(frame: Any) -> None:
            send_frames.append(frame)

        dispatcher.register(
            RegisterWorker(owner_id="cs_lead", role="backup", token="tok"),
            send_backup,
        )

        answer = Answer(text="백업 답", mode="full")
        dispatcher.submit(ticket.ticket_id, answer)
        return ticket

    def test_backup_답_종착_후_owner에게_backup_review_added_통지된다(self) -> None:
        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch})
        dispatcher, review_store = self._make_dispatcher(notifier=notifier)

        self._submit_backup_answer(dispatcher)

        items = review_store.pending_for_owner("cs_lead")
        assert len(items) == 1
        notifs = ch.for_recipient("cs_lead")
        assert len(notifs) == 1
        assert notifs[0].kind == "backup_review_added"
        assert notifs[0].subject_ref == items[0].item_id
        assert notifs[0].created_at == _TS

    def test_backup_답_종착_notifier_미주입이면_통지_0(self) -> None:
        ch = FakeChannel()
        dispatcher, review_store = self._make_dispatcher(notifier=None)

        self._submit_backup_answer(dispatcher)

        items = review_store.pending_for_owner("cs_lead")
        assert len(items) == 1
        assert ch.delivered == []

    def test_채널_실패해도_BackupReview_적재_미아_없음(self) -> None:
        class BrokenChannel:
            def send(self, notification: Notification) -> None:
                raise RuntimeError("채널 다운")

        notifier = Notifier(subscriptions={"cs_lead": BrokenChannel()})
        dispatcher, review_store = self._make_dispatcher(notifier=notifier)

        self._submit_backup_answer(dispatcher)

        items = review_store.pending_for_owner("cs_lead")
        assert len(items) == 1


# ══════════════════════════════════════════════════════════════════════════════
# 슬라이스 C — m1: reeval 발화 subject_ref 축 네임스페이스 (reeval.py)
# ══════════════════════════════════════════════════════════════════════════════


def _make_fixed_audit(entries: "list[dict[str, Any]]") -> Any:
    class _FixedAudit:
        def records(self) -> "list[dict[str, Any]]":
            return entries

        def record_at(self, index: int) -> "dict[str, Any] | None":
            if 0 <= index < len(entries):
                return entries[index]
            return None

    return _FixedAudit()


def _routed_audit_record(
    agent_id: str = "cs_ops",
    owner: str = "cs_lead",
    snapshot_sha: str | None = "sha_old",
) -> "dict[str, Any]":
    answer: "dict[str, Any]" = {"text": "기존 답", "mode": "full", "sources": []}
    if snapshot_sha is not None:
        answer["snapshot_sha"] = snapshot_sha
    return {
        "timestamp": "2026-06-24T09:00:00+00:00",
        "user_id": "user_1",
        "question": "환불 문의",
        "intent": "refund_policy",
        "decision": {
            "disposition": "routed",
            "primary": agent_id,
            "owner": owner,
            "confidence": 0.9,
            "reason": "판례",
            "requires_approval": False,
            "collaborators": [],
        },
        "answer": answer,
        "dispatch": {"disposition": "delivered"},
    }


class TestSliceCReevalSubjectRefNamespace:
    def _make_propagator_with_audit(
        self, intent: str = "refund_policy", notifier: "Notifier | None" = None
    ) -> "tuple[Any, Any]":
        from agent_org_network.conflict import InMemoryPrecedentStore, Resolution
        from agent_org_network.reeval import InMemoryReevalStore, StalenessPropagator

        audit = _make_fixed_audit([_routed_audit_record()])
        precedents = InMemoryPrecedentStore()
        precedents.record(Resolution(intent=intent, primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda agent_id: "cs_lead",
            clock=_CLOCK,
            notifier=notifier,
        )
        return propagator, reeval_store

    def test_Precedent_축_통지_subject_ref에_precedent_prefix가_붙는다(self) -> None:
        from agent_org_network.git_gateway import OkfChangeEvent

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch})
        propagator, _ = self._make_propagator_with_audit(notifier=notifier)

        event = OkfChangeEvent(
            agent_id="cs_ops",
            new_sha="sha_new",
            parent_sha=None,
            changed_paths=("policy.md",),
            author="builder",
            committed_at=_TS,
        )
        propagator.on_okf_committed(event)

        notifs = ch.for_recipient("cs_lead")
        precedent_notifs = [n for n in notifs if n.subject_ref.startswith("precedent:")]
        assert len(precedent_notifs) >= 1
        assert precedent_notifs[0].subject_ref == "precedent:refund_policy"

    def test_Answer_축_통지_subject_ref에_answer_prefix가_붙는다(self) -> None:
        from agent_org_network.git_gateway import OkfChangeEvent

        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch})
        propagator, _ = self._make_propagator_with_audit(notifier=notifier)

        event = OkfChangeEvent(
            agent_id="cs_ops",
            new_sha="sha_new",
            parent_sha=None,
            changed_paths=("policy.md",),
            author="builder",
            committed_at=_TS,
        )
        propagator.on_okf_committed(event)

        notifs = ch.for_recipient("cs_lead")
        answer_notifs = [n for n in notifs if n.subject_ref.startswith("answer:")]
        assert len(answer_notifs) >= 1
        assert answer_notifs[0].subject_ref == "answer:0"

    def test_intent가_숫자_문자열이어도_Precedent_Answer_축_멱등_충돌_없이_둘_다_발화된다(
        self,
    ) -> None:
        """m1 핵심: intent="0", Answer subject_ref="0"이 같아도 prefix로 충돌 방지 — 통지 2통."""
        from agent_org_network.conflict import InMemoryPrecedentStore, Resolution
        from agent_org_network.git_gateway import OkfChangeEvent
        from agent_org_network.reeval import InMemoryReevalStore, StalenessPropagator

        audit = _make_fixed_audit(
            [_routed_audit_record(agent_id="cs_ops", owner="cs_lead", snapshot_sha="sha_old")]
        )
        precedents = InMemoryPrecedentStore()
        precedents.record(Resolution(intent="0", primary="cs_ops"))
        reeval_store = InMemoryReevalStore()
        ch = FakeChannel()
        notifier = Notifier(subscriptions={"cs_lead": ch})
        propagator = StalenessPropagator(
            precedents=precedents,
            audit_reader=audit,
            reeval_store=reeval_store,
            owner_of=lambda agent_id: "cs_lead",
            clock=_CLOCK,
            notifier=notifier,
        )

        event = OkfChangeEvent(
            agent_id="cs_ops",
            new_sha="sha_new",
            parent_sha=None,
            changed_paths=("policy.md",),
            author="builder",
            committed_at=_TS,
        )
        propagator.on_okf_committed(event)

        notifs = ch.for_recipient("cs_lead")
        assert len(notifs) == 2
        subject_refs = {n.subject_ref for n in notifs}
        assert "precedent:0" in subject_refs
        assert "answer:0" in subject_refs


# ══════════════════════════════════════════════════════════════════════════════
# T8.2 슬라이스 D — render_mcp_notification: kind별 렌더 + 노출 불변식
# ══════════════════════════════════════════════════════════════════════════════

# 통지 렌더 출력에 절대 새면 안 되는 내부값 토큰 목록.
# Notification 모델이 이 필드 자체를 안 담지만, 렌더 로직이 실수로 노출할 가능성을
# 회귀 방어한다. test_mcp_server.py _LEAKY_TOKENS의 통지 대칭 버전.
_NOTIF_LEAKY_TOKENS = (
    "confidence",
    "candidate",
    "reason",
    "manager_id",
    "ticket_id",
    "intent",  # subject_ref 손잡이는 OK이되 "intent" 리터럴 키 노출 금지
    "primary",
    "question",  # 사용자 질문 원문
    "escalated_to",
)


class TestSliceDRenderMcpNotification:
    """render_mcp_notification 순수 함수 — kind별 렌더 + 노출 불변식."""

    def _make_notif(self, kind: str, subject_ref: str = "ref_abc") -> Notification:
        return Notification(
            recipient_id="owner_x",
            kind=kind,  # type: ignore[arg-type]
            subject_ref=subject_ref,
            created_at=_TS,
        )

    # ── 체크리스트 #1: kind별 4종 렌더 ───────────────────────────────────

    def test_conflict_opened_렌더_사람이_읽는_문자열을_낸다(self) -> None:
        n = self._make_notif("conflict_opened", "case_001")
        result = render_mcp_notification(n)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_conflict_opened_렌더에_subject_ref_손잡이가_들어간다(self) -> None:
        n = self._make_notif("conflict_opened", "case_001")
        result = render_mcp_notification(n)
        assert "(대상: case_001)" in result

    def test_backup_review_added_렌더_사람이_읽는_문자열을_낸다(self) -> None:
        n = self._make_notif("backup_review_added", "item_bkp_02")
        result = render_mcp_notification(n)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_backup_review_added_렌더에_subject_ref_손잡이가_들어간다(self) -> None:
        n = self._make_notif("backup_review_added", "item_bkp_02")
        result = render_mcp_notification(n)
        assert "(대상: item_bkp_02)" in result

    def test_reeval_flagged_렌더_사람이_읽는_문자열을_낸다(self) -> None:
        n = self._make_notif("reeval_flagged", "precedent:refund_policy")
        result = render_mcp_notification(n)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_reeval_flagged_렌더에_subject_ref_손잡이가_들어간다(self) -> None:
        n = self._make_notif("reeval_flagged", "precedent:refund_policy")
        result = render_mcp_notification(n)
        assert "(대상: precedent:refund_policy)" in result

    def test_manager_escalated_렌더_사람이_읽는_문자열을_낸다(self) -> None:
        n = self._make_notif("manager_escalated", "item_mgr_03")
        result = render_mcp_notification(n)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_manager_escalated_렌더에_subject_ref_손잡이가_들어간다(self) -> None:
        n = self._make_notif("manager_escalated", "item_mgr_03")
        result = render_mcp_notification(n)
        assert "(대상: item_mgr_03)" in result

    def test_approval_assignment_ready_렌더_사람이_읽는_문자열을_낸다(self) -> None:
        n = self._make_notif("approval_assignment_ready", "approval-2")
        result = render_mcp_notification(n)
        assert "승인 처리함" in result
        assert "(대상: approval-2)" in result

    def test_approval_assignment_ready_렌더에_본문_토큰이_없다(self) -> None:
        n = self._make_notif("approval_assignment_ready", "approval-2")
        result = render_mcp_notification(n)
        for token in _NOTIF_LEAKY_TOKENS:
            assert token not in result, f"토큰 '{token}'이 렌더 출력에 노출됨: {result!r}"

    # ── 체크리스트 #2(a): subject_ref 보존 손잡이 ──────────────────────────

    def test_subject_ref_가_렌더_출력에_보존된다(self) -> None:
        """노출 불변식(a) — subject_ref 손잡이가 출력에 들어간다."""
        ref = "unique_case_xyz_999"
        n = self._make_notif("conflict_opened", ref)
        result = render_mcp_notification(n)
        assert ref in result

    # ── 체크리스트 #2(b): 조직 내부값·비밀 토큰 비노출 ─────────────────────

    def test_conflict_opened_렌더에_내부값_토큰이_없다(self) -> None:
        """노출 불변식(b) — 조직 내부값이 출력에 새지 않는다."""
        n = self._make_notif("conflict_opened", "case_001")
        result = render_mcp_notification(n)
        for token in _NOTIF_LEAKY_TOKENS:
            assert token not in result, f"토큰 '{token}'이 렌더 출력에 노출됨: {result!r}"

    def test_backup_review_added_렌더에_내부값_토큰이_없다(self) -> None:
        n = self._make_notif("backup_review_added", "item_bkp_02")
        result = render_mcp_notification(n)
        for token in _NOTIF_LEAKY_TOKENS:
            assert token not in result, f"토큰 '{token}'이 렌더 출력에 노출됨: {result!r}"

    def test_reeval_flagged_렌더에_내부값_토큰이_없다(self) -> None:
        n = self._make_notif("reeval_flagged", "precedent:refund_policy")
        result = render_mcp_notification(n)
        for token in _NOTIF_LEAKY_TOKENS:
            assert token not in result, f"토큰 '{token}'이 렌더 출력에 노출됨: {result!r}"

    def test_manager_escalated_렌더에_내부값_토큰이_없다(self) -> None:
        n = self._make_notif("manager_escalated", "item_mgr_03")
        result = render_mcp_notification(n)
        for token in _NOTIF_LEAKY_TOKENS:
            assert token not in result, f"토큰 '{token}'이 렌더 출력에 노출됨: {result!r}"


# ══════════════════════════════════════════════════════════════════════════════
# T8.2 슬라이스 E — McpChannel: Fake transport 주입 + NotImplementedError
# ══════════════════════════════════════════════════════════════════════════════


class TestSliceEMcpChannel:
    """McpChannel — Fake send_fn 주입 호출 인자 검증 + send_fn 미주입 에러."""

    def _make_notif(
        self, kind: str = "conflict_opened", subject_ref: str = "case_001"
    ) -> Notification:
        return Notification(
            recipient_id="owner_x",
            kind=kind,  # type: ignore[arg-type]
            subject_ref=subject_ref,
            created_at=_TS,
        )

    # ── 체크리스트 #3: Fake transport 호출 인자 ────────────────────────────

    def test_send_fn_주입_후_send_호출_시_정확히_1회_호출된다(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake(rid: str, payload: str) -> None:
            calls.append((rid, payload))

        n = self._make_notif("reeval_flagged", "precedent:refund_policy")
        McpChannel(fake).send(n)
        assert len(calls) == 1

    def test_send_fn에_recipient_id가_정확히_전달된다(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake(rid: str, payload: str) -> None:
            calls.append((rid, payload))

        n = self._make_notif("manager_escalated", "item_mgr_03")
        McpChannel(fake).send(n)
        assert calls[0][0] == n.recipient_id

    def test_send_fn에_render_결과가_payload로_전달된다(self) -> None:
        calls: list[tuple[str, str]] = []

        def fake(rid: str, payload: str) -> None:
            calls.append((rid, payload))

        n = self._make_notif("backup_review_added", "item_bkp_02")
        McpChannel(fake).send(n)
        assert calls[0][1] == render_mcp_notification(n)

    def test_send_fn_호출_인자가_recipient_id와_render_결과_쌍이다(self) -> None:
        """체크리스트 #3 핵심: (recipient_id, render_mcp_notification(n)) 쌍."""
        calls: list[tuple[str, str]] = []

        def fake(rid: str, payload: str) -> None:
            calls.append((rid, payload))

        n = self._make_notif("conflict_opened", "case_007")
        McpChannel(fake).send(n)
        expected = (n.recipient_id, render_mcp_notification(n))
        assert calls[0] == expected

    # ── 체크리스트 #4: send_fn 미주입 = NotImplementedError ────────────────

    def test_send_fn_미주입_시_NotImplementedError를_던진다(self) -> None:
        n = self._make_notif()
        with pytest.raises(NotImplementedError):
            McpChannel().send(n)

    def test_send_fn_None_명시_주입도_NotImplementedError를_던진다(self) -> None:
        n = self._make_notif()
        with pytest.raises(NotImplementedError):
            McpChannel(None).send(n)


# ══════════════════════════════════════════════════════════════════════════════
# T8.2 슬라이스 F — fire-and-forget 전파 + 멱등 재시도 회귀
# ══════════════════════════════════════════════════════════════════════════════


class TestSliceFFireAndForget:
    """체크리스트 #5: McpChannel boom_fn → Notifier가 삼킴 + 멱등 키 미기록 재시도."""

    def _make_notif(self, subject_ref: str = "case_ff") -> Notification:
        return Notification(
            recipient_id="owner_ff",
            kind="conflict_opened",
            subject_ref=subject_ref,
            created_at=_TS,
        )

    def test_McpChannel_boom_fn_예외가_Notifier_호출자로_새지_않는다(self) -> None:
        """체크리스트 #5(a): send_fn이 던져도 Notifier.notify가 삼킨다(fire-and-forget)."""
        call_count = 0

        def boom(rid: str, payload: str) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("MCP transport 다운")

        notifier = Notifier(subscriptions={"owner_ff": McpChannel(boom)})
        n = self._make_notif()
        notifier.notify(n)  # 예외가 새지 않아야 한다
        assert call_count == 1

    def test_McpChannel_boom_fn_실패_후_멱등_키_미기록으로_재시도_시_다시_send_시도된다(
        self,
    ) -> None:
        """체크리스트 #5(b): 전송 실패(예외) → 멱등 키 미기록 → 재시도 시 다시 send_fn 호출."""
        call_count = 0

        def boom_first(rid: str, payload: str) -> None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("첫 번째 MCP 실패")

        notifier = Notifier(subscriptions={"owner_ff": McpChannel(boom_first)})
        n = self._make_notif()
        notifier.notify(n)  # 첫 번째 실패 — 삼킴
        notifier.notify(n)  # 두 번째: 멱등 키 미기록이라 재시도
        assert call_count == 2
