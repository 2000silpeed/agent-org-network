"""T5.2 Manager 큐 — 슬라이스 a/b/c/d 결정론 테스트.

실 LLM·실 네트워크 0. FakeClassifier·StubRuntime·주입 clock·고정 id 시드.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.conflict import (
    Candidate,
    ConflictCase,
    InMemoryConflictCaseStore,
    InMemoryPrecedentStore,
)
from agent_org_network.decision import Unowned
from agent_org_network.dispatch import EscalatedToManager, WorkTicket
from agent_org_network.manager_queue import (
    AssignOwner,
    Dismiss,
    FromDeadlock,
    FromDispatch,
    FromUnowned,
    InMemoryManagerQueueStore,
    ManagerItem,
    ManagerQueueService,
    ManagerResolution,
    Reroute,
    manager_id_for_deadlock,
    manager_id_for_dispatch,
    manager_id_for_unowned,
)

_NOW = datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)
_CLOCK = lambda: _NOW  # noqa: E731
_DATE = date(2026, 6, 21)


# ── pyright strict helper (starlette TestClient unknown 타입 흡수) ──────────

@dataclass(frozen=True)
class HttpResult:
    status: int
    body: Any


def _result(res: Response) -> HttpResult:
    body: Any = res.json()
    return HttpResult(status=res.status_code, body=body)


# ── 픽스처 헬퍼 ──────────────────────────────────────────────────────────────

def _make_unowned(root: str = "root_manager") -> Unowned:
    return Unowned(escalated_to=root, reason="후보 없음")


def _make_ticket(owner_id: str = "alice", agent_id: str = "agent_a") -> WorkTicket:
    return WorkTicket(
        owner_id=owner_id,
        agent_id=agent_id,
        question="도움 필요해요",
        enqueued_at=_NOW,
        ticket_id="ticket-001",
    )


def _make_case(
    intent: str = "환불",
    candidates: tuple[Candidate, ...] | None = None,
) -> ConflictCase:
    if candidates is None:
        candidates = (
            Candidate(agent_id="cs_ops", owner="cs_lead"),
            Candidate(agent_id="finance_ops", owner="finance_lead"),
        )
    return ConflictCase(
        intent=intent,
        question="보상 기준이 어떻게 되나요?",
        candidates=candidates,
        opened_at=_NOW,
        case_id="case-001",
    )


def _make_escalated(
    manager_id: str | None = "root_manager",
) -> EscalatedToManager:
    return EscalatedToManager(
        ticket=_make_ticket(),
        manager_id=manager_id,
        reason="timeout",
    )


def _no_manager(uid: str) -> str | None:  # noqa: ARG001
    return None


# ────────────────────────────────────────────────────────────────────────────
# 슬라이스 (a): 도메인 타입 + InMemoryManagerQueueStore
# ────────────────────────────────────────────────────────────────────────────


class TestManagerItemDomain:
    def test_question_from_unowned(self) -> None:
        source = FromUnowned(decision=_make_unowned(), question="도와줘요")
        item = ManagerItem(
            manager_id="root_manager",
            source=source,
            created_at=_NOW,
            item_id="item-001",
        )
        assert item.question() == "도와줘요"

    def test_question_from_deadlock(self) -> None:
        case = _make_case()
        source = FromDeadlock(case=case, reason="표 갈림")
        item = ManagerItem(
            manager_id="root_manager",
            source=source,
            created_at=_NOW,
            item_id="item-001",
        )
        assert item.question() == case.question

    def test_question_from_dispatch(self) -> None:
        outcome = _make_escalated()
        source = FromDispatch(outcome=outcome)
        item = ManagerItem(
            manager_id="root_manager",
            source=source,
            created_at=_NOW,
            item_id="item-001",
        )
        assert item.question() == outcome.ticket.question

    def test_resolve_보존_불변(self) -> None:
        source = FromUnowned(decision=_make_unowned(), question="도와줘요")
        item = ManagerItem(
            manager_id="root_manager",
            source=source,
            created_at=_NOW,
            item_id="item-001",
            status="open",
        )
        action = AssignOwner(by_manager="root_manager", primary="cs_ops")
        resolution = ManagerResolution(action=action, resolution=None)
        resolved = item.resolve(resolution)

        assert resolved.item_id == item.item_id
        assert resolved.source is item.source
        assert resolved.status == "resolved"
        assert resolved.resolution == resolution
        # 원본 불변
        assert item.status == "open"
        assert item.resolution is None


class TestInMemoryManagerQueueStore:
    def test_enqueue_and_pending_for_manager(self) -> None:
        store = InMemoryManagerQueueStore()
        source = FromUnowned(decision=_make_unowned(), question="질문")
        item = ManagerItem(
            manager_id="root_manager",
            source=source,
            created_at=_NOW,
            item_id="item-001",
        )
        store.enqueue(item)

        pending = store.pending_for_manager("root_manager")
        assert len(pending) == 1
        assert pending[0].item_id == "item-001"

    def test_manager_격리(self) -> None:
        store = InMemoryManagerQueueStore()
        source = FromUnowned(decision=_make_unowned(), question="질문")
        item_a = ManagerItem(
            manager_id="mgr_a", source=source, created_at=_NOW, item_id="item-a"
        )
        item_b = ManagerItem(
            manager_id="mgr_b", source=source, created_at=_NOW, item_id="item-b"
        )
        store.enqueue(item_a)
        store.enqueue(item_b)

        assert [i.item_id for i in store.pending_for_manager("mgr_a")] == ["item-a"]
        assert [i.item_id for i in store.pending_for_manager("mgr_b")] == ["item-b"]
        assert store.pending_for_manager("mgr_c") == []

    def test_get(self) -> None:
        store = InMemoryManagerQueueStore()
        source = FromUnowned(decision=_make_unowned(), question="질문")
        item = ManagerItem(
            manager_id="root_manager", source=source, created_at=_NOW, item_id="item-001"
        )
        store.enqueue(item)

        assert store.get("item-001") is not None
        assert store.get("없음") is None

    def test_mark_resolved_open에서_제거_history_append(self) -> None:
        store = InMemoryManagerQueueStore()
        source = FromUnowned(decision=_make_unowned(), question="질문")
        item = ManagerItem(
            manager_id="root_manager", source=source, created_at=_NOW, item_id="item-001"
        )
        store.enqueue(item)
        action = AssignOwner(by_manager="root_manager", primary="cs_ops")
        resolved = item.resolve(ManagerResolution(action=action))
        store.mark_resolved(resolved)

        assert store.pending_for_manager("root_manager") == []
        # history: enqueue(open) + mark_resolved(resolved) = 2
        assert len(store.history) == 2
        assert store.history[-1].status == "resolved"
        # get은 resolved된 것도 조회 가능
        got = store.get("item-001")
        assert got is not None
        assert got.status == "resolved"

    def test_get_by_case(self) -> None:
        store = InMemoryManagerQueueStore()
        case = _make_case()
        source = FromDeadlock(case=case, reason="표 갈림")
        item = ManagerItem(
            manager_id="root_manager", source=source, created_at=_NOW, item_id="item-001"
        )
        store.enqueue(item)

        assert store.get_by_case(case.case_id) is not None
        assert store.get_by_case("없는케이스") is None

    def test_history_append_only(self) -> None:
        """enqueue 후 mark_resolved해도 history에 두 항목(적재·완료)이 쌓인다."""
        store = InMemoryManagerQueueStore()
        source = FromUnowned(decision=_make_unowned(), question="질문")
        item = ManagerItem(
            manager_id="root_manager", source=source, created_at=_NOW, item_id="item-001"
        )
        store.enqueue(item)
        action = Dismiss(by_manager="root_manager")
        resolved = item.resolve(ManagerResolution(action=action))
        store.mark_resolved(resolved)

        # history에는 open(적재)·resolved(처리) 두 항목
        assert len(store.history) == 2


# ────────────────────────────────────────────────────────────────────────────
# 슬라이스 (b): manager_id_for_* 함수 + 큐 적재 흐름
# ────────────────────────────────────────────────────────────────────────────


class TestManagerIdFor:
    def test_unowned_은_root(self) -> None:
        decision = _make_unowned(root="root_manager")
        assert manager_id_for_unowned(decision) == "root_manager"

    def test_dispatch_manager_id_그대로(self) -> None:
        outcome = _make_escalated(manager_id="mgr_alice")
        assert manager_id_for_dispatch(outcome, root="root_manager") == "mgr_alice"

    def test_dispatch_manager_none이면_root_보정(self) -> None:
        outcome = _make_escalated(manager_id=None)
        assert manager_id_for_dispatch(outcome, root="root_manager") == "root_manager"

    def test_deadlock_첫_후보_owner의_manager(self) -> None:
        def manager_of(uid: str) -> str | None:
            return "root_manager" if uid == "cs_lead" else None

        case = _make_case()
        result = manager_id_for_deadlock(case, manager_of=manager_of, root="root_manager")
        assert result == "root_manager"

    def test_deadlock_manager_none이면_root(self) -> None:
        case = _make_case()
        result = manager_id_for_deadlock(case, manager_of=_no_manager, root="root_manager")
        assert result == "root_manager"

    def test_deadlock_후보_없으면_root(self) -> None:
        case = _make_case(candidates=())

        def has_manager(uid: str) -> str | None:
            return "some_mgr"

        result = manager_id_for_deadlock(case, manager_of=has_manager, root="root_manager")
        assert result == "root_manager"


class TestEnqueueFlows:
    """세 출처(Unowned/Deadlock/Dispatch)가 큐 적재로 종착 — 미아 없음 불변식."""

    def test_unowned_큐_적재(self) -> None:
        store = InMemoryManagerQueueStore()
        decision = _make_unowned()
        source = FromUnowned(decision=decision, question="도움 필요")
        item = ManagerItem(
            manager_id=manager_id_for_unowned(decision),
            source=source,
            created_at=_NOW,
            item_id="item-u",
        )
        store.enqueue(item)
        pending = store.pending_for_manager("root_manager")
        assert len(pending) == 1
        assert isinstance(pending[0].source, FromUnowned)

    def test_deadlock_큐_적재(self) -> None:
        store = InMemoryManagerQueueStore()
        case = _make_case()

        def always_root(uid: str) -> str | None:
            return "root_manager"

        mid = manager_id_for_deadlock(case, manager_of=always_root, root="root_manager")
        source = FromDeadlock(case=case, reason="표 갈림")
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=_NOW,
            item_id="item-d",
        )
        store.enqueue(item)
        pending = store.pending_for_manager("root_manager")
        assert len(pending) == 1
        assert isinstance(pending[0].source, FromDeadlock)

    def test_dispatch_큐_적재(self) -> None:
        store = InMemoryManagerQueueStore()
        outcome = _make_escalated(manager_id="root_manager")
        mid = manager_id_for_dispatch(outcome, root="root_manager")
        source = FromDispatch(outcome=outcome)
        item = ManagerItem(
            manager_id=mid,
            source=source,
            created_at=_NOW,
            item_id="item-e",
        )
        store.enqueue(item)
        pending = store.pending_for_manager("root_manager")
        assert len(pending) == 1
        assert isinstance(pending[0].source, FromDispatch)


def _build_registry_with_card() -> Any:
    """alice(→root_manager)·cs_ops 카드·registry 반환."""
    from agent_org_network.agent_card import AgentCard
    from agent_org_network.registry import Registry
    from agent_org_network.user import User

    registry = Registry()
    root = User(id="root_manager")
    alice = User(id="alice", manager="root_manager")
    registry.register_user(root)
    registry.register_user(alice)
    card = AgentCard(
        agent_id="cs_ops",
        owner="alice",
        team="cs",
        summary="환불 안내",
        domains=["환불"],
        last_reviewed_at=_DATE,
    )
    registry.register(card)
    registry.validate()
    return registry


class TestAskOrgEnqueue:
    """ask_org.handle이 Unowned/EscalatedToManager 출처를 Manager 큐에 적재한다."""

    def _build_ask_with_queue(
        self,
        registry: Any,
        queue_store: InMemoryManagerQueueStore,
        use_queue_dispatcher: bool = False,
    ) -> Any:
        from agent_org_network.ask_org import AskOrg
        from agent_org_network.audit import JsonlAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import InMemoryConflictCaseStore, InMemoryPrecedentStore
        from agent_org_network.dispatch import InMemoryWorkQueueDispatcher
        from agent_org_network.router import Router

        def manager_of(uid: str) -> str | None:
            return registry.get_user(uid).manager if uid in registry.user_ids() else None

        classifier = FakeClassifier("환불")
        precedents = InMemoryPrecedentStore()
        case_store = InMemoryConflictCaseStore()
        router = Router(registry, classifier, root_user="root_manager", precedents=precedents)
        dispatcher = InMemoryWorkQueueDispatcher(clock=_CLOCK)

        return AskOrg(
            router=router,
            dispatcher=dispatcher,
            audit_log=JsonlAuditLog(Path("logs/audit-test.jsonl")),
            clock=_CLOCK,
            case_store=case_store,
            manager_queue_store=queue_store,
            manager_of=manager_of,
            manager_root="root_manager",
        ), dispatcher

    def test_unowned_큐_적재(self) -> None:
        from agent_org_network.ask_org import AskOrg, Pending
        from agent_org_network.audit import JsonlAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import InMemoryConflictCaseStore, InMemoryPrecedentStore
        from agent_org_network.dispatch import LocalRuntimeDispatcher
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.user import User

        registry = Registry()
        root = User(id="root_manager")
        registry.register_user(root)
        registry.validate()

        classifier = FakeClassifier("미분류")
        precedents = InMemoryPrecedentStore()
        case_store = InMemoryConflictCaseStore()
        router = Router(registry, classifier, root_user="root_manager", precedents=precedents)
        queue_store = InMemoryManagerQueueStore()

        ask = AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=JsonlAuditLog(Path("logs/audit-test.jsonl")),
            clock=_CLOCK,
            case_store=case_store,
            manager_queue_store=queue_store,
            manager_of=_no_manager,
            manager_root="root_manager",
        )
        reply = ask.handle("미분류 질문이에요", User(id="web_guest"))
        assert isinstance(reply, Pending)
        assert reply.kind == "unowned"

        pending = queue_store.pending_for_manager("root_manager")
        assert len(pending) == 1
        assert isinstance(pending[0].source, FromUnowned)

    def test_dispatch_escalated_큐_적재(self) -> None:
        from agent_org_network.ask_org import Pending
        from agent_org_network.user import User

        registry = _build_registry_with_card()
        queue_store = InMemoryManagerQueueStore()
        ask, dispatcher = self._build_ask_with_queue(registry, queue_store)

        # timeout을 음수로 설정 → 즉시 EscalatedToManager
        dispatcher._timeout = timedelta(seconds=-1)  # pyright: ignore[reportPrivateUsage]

        reply = ask.handle("환불 해줘요", User(id="web_guest"))
        assert isinstance(reply, Pending)
        assert reply.kind == "dispatched"

        pending = queue_store.pending_for_manager("root_manager")
        assert len(pending) == 1
        assert isinstance(pending[0].source, FromDispatch)

    def test_pending_투영_불변(self) -> None:
        """큐 적재가 기존 Pending(unowned) 투영을 깨지 않는다 — 미주입 하위호환."""
        from agent_org_network.ask_org import AskOrg, Pending
        from agent_org_network.audit import JsonlAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import InMemoryPrecedentStore
        from agent_org_network.dispatch import LocalRuntimeDispatcher
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.user import User

        registry = Registry()
        root = User(id="root_manager")
        registry.register_user(root)
        registry.validate()

        classifier = FakeClassifier("미분류")
        precedents = InMemoryPrecedentStore()
        router = Router(registry, classifier, root_user="root_manager", precedents=precedents)

        # manager_queue_store 미주입 — 하위호환
        ask = AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=JsonlAuditLog(Path("logs/audit-test.jsonl")),
            clock=_CLOCK,
        )
        reply = ask.handle("미분류 질문이에요", User(id="web_guest"))
        assert isinstance(reply, Pending)
        assert reply.kind == "unowned"


class TestDeadlockEnqueue:
    """합의 교착(Deadlocked) → Manager 큐 적재."""

    def test_deadlocked_큐_적재(self) -> None:
        from agent_org_network.agent_card import AgentCard
        from agent_org_network.ask_org import AskOrg, Pending
        from agent_org_network.audit import JsonlAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.conflict import (
            ConsensusService,
            ConcurOnPrimary,
            Deadlocked,
            InMemoryConflictCaseStore,
            InMemoryPrecedentStore,
        )
        from agent_org_network.dispatch import LocalRuntimeDispatcher
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.user import User

        registry = Registry()
        root = User(id="root_manager")
        alice = User(id="alice", manager="root_manager")
        bob = User(id="bob", manager="root_manager")
        registry.register_user(root)
        registry.register_user(alice)
        registry.register_user(bob)
        card_a = AgentCard(
            agent_id="agent_a", owner="alice", team="t", summary="s", domains=["보상"],
            last_reviewed_at=_DATE,
        )
        card_b = AgentCard(
            agent_id="agent_b", owner="bob", team="t", summary="s", domains=["보상"],
            last_reviewed_at=_DATE,
        )
        registry.register(card_a)
        registry.register(card_b)
        registry.validate()

        classifier = FakeClassifier("보상")
        precedents = InMemoryPrecedentStore()
        case_store = InMemoryConflictCaseStore()
        router = Router(registry, classifier, root_user="root_manager", precedents=precedents)
        queue_store = InMemoryManagerQueueStore()

        def manager_of(uid: str) -> str | None:
            return registry.get_user(uid).manager if uid in registry.user_ids() else None

        ask = AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=JsonlAuditLog(Path("logs/audit-test.jsonl")),
            clock=_CLOCK,
            case_store=case_store,
            manager_queue_store=queue_store,
            manager_of=manager_of,
            manager_root="root_manager",
        )
        consensus = ConsensusService(case_store=case_store, precedents=precedents)

        # 보상 질문 → Contested → case 생성
        reply = ask.handle("보상 기준은?", User(id="web_guest"))
        assert isinstance(reply, Pending)
        assert reply.kind == "contested"

        # case_id 조회
        cases = case_store.open_for_owner("alice")
        assert len(cases) == 1
        case = cases[0]

        # alice → agent_a, bob → agent_b (표 갈림 → Deadlocked)
        consensus.concur(case.case_id, ConcurOnPrimary(by_owner="alice", on_agent="agent_a"))
        outcome = consensus.concur(case.case_id, ConcurOnPrimary(by_owner="bob", on_agent="agent_b"))
        assert isinstance(outcome, Deadlocked)

        # ask.enqueue_deadlock으로 Manager 큐에 적재
        ask.enqueue_deadlock(case, reason=outcome.reason)

        pending = queue_store.pending_for_manager("root_manager")
        assert len(pending) == 1
        assert isinstance(pending[0].source, FromDeadlock)


# ────────────────────────────────────────────────────────────────────────────
# 슬라이스 (c): ManagerQueueService.act
# ────────────────────────────────────────────────────────────────────────────


def _store_with_item(
    source_type: str = "unowned",
    manager_id: str = "root_manager",
) -> tuple[InMemoryManagerQueueStore, ManagerItem]:
    store = InMemoryManagerQueueStore()
    source_obj: FromUnowned | FromDeadlock | FromDispatch
    if source_type == "unowned":
        source_obj = FromUnowned(decision=_make_unowned(), question="질문")
    elif source_type == "deadlock":
        source_obj = FromDeadlock(case=_make_case(), reason="표 갈림")
    else:
        source_obj = FromDispatch(outcome=_make_escalated())
    item = ManagerItem(
        manager_id=manager_id,
        source=source_obj,
        created_at=_NOW,
        item_id="item-001",
    )
    store.enqueue(item)
    return store, item


class TestManagerQueueServiceAct:
    def test_assign_owner_from_unowned_precedent_없음(self) -> None:
        """FromUnowned에는 intent가 없으므로 Precedent 기록 안 함."""
        store, _ = _store_with_item(source_type="unowned")
        precedents = InMemoryPrecedentStore()
        svc = ManagerQueueService(queue_store=store, precedents=precedents)
        action = AssignOwner(by_manager="root_manager", primary="cs_ops", rationale="")
        resolved = svc.act("item-001", action)

        assert resolved.status == "resolved"
        assert resolved.resolution is not None
        assert resolved.resolution.action == action
        assert len(precedents.history) == 0

    def test_assign_owner_from_deadlock_precedent_기록(self) -> None:
        """FromDeadlock(case.intent 있음)에 AssignOwner → Precedent 기록."""
        store, _ = _store_with_item(source_type="deadlock")
        precedents = InMemoryPrecedentStore()
        case_store = InMemoryConflictCaseStore()
        case = _make_case()
        case_store.open_case(case)

        svc = ManagerQueueService(
            queue_store=store, precedents=precedents, case_store=case_store
        )
        action = AssignOwner(by_manager="root_manager", primary="cs_ops")
        resolved = svc.act("item-001", action)

        assert resolved.status == "resolved"
        assert len(precedents.history) == 1
        assert precedents.history[0].resolution.intent == "환불"
        assert precedents.history[0].resolution.primary == "cs_ops"
        assert case_store.get(case.case_id) is None

    def test_assign_owner_from_deadlock_case_resolved(self) -> None:
        """FromDeadlock AssignOwner → ConflictCase mark_resolved."""
        store, _ = _store_with_item(source_type="deadlock")
        case_store = InMemoryConflictCaseStore()
        case = _make_case()
        case_store.open_case(case)
        precedents = InMemoryPrecedentStore()

        svc = ManagerQueueService(
            queue_store=store, precedents=precedents, case_store=case_store
        )
        action = AssignOwner(by_manager="root_manager", primary="cs_ops")
        svc.act("item-001", action)

        assert case_store.get(case.case_id) is None

    def test_reroute_precedent_없음(self) -> None:
        store, _ = _store_with_item(source_type="dispatch")
        precedents = InMemoryPrecedentStore()
        svc = ManagerQueueService(queue_store=store, precedents=precedents)
        action = Reroute(by_manager="root_manager", to_agent="agent_b")
        resolved = svc.act("item-001", action)

        assert resolved.status == "resolved"
        assert len(precedents.history) == 0

    def test_dismiss_precedent_없음(self) -> None:
        store, _ = _store_with_item()
        precedents = InMemoryPrecedentStore()
        svc = ManagerQueueService(queue_store=store, precedents=precedents)
        action = Dismiss(by_manager="root_manager")
        resolved = svc.act("item-001", action)

        assert resolved.status == "resolved"
        assert len(precedents.history) == 0

    def test_1인칭_위반_ValueError(self) -> None:
        store, _ = _store_with_item(manager_id="root_manager")
        svc = ManagerQueueService(queue_store=store)
        action = AssignOwner(by_manager="남의매니저", primary="cs_ops")
        with pytest.raises(ValueError, match="1인칭"):
            svc.act("item-001", action)

    def test_item_id_미존재_ValueError(self) -> None:
        store = InMemoryManagerQueueStore()
        svc = ManagerQueueService(queue_store=store)
        action = AssignOwner(by_manager="root_manager", primary="cs_ops")
        with pytest.raises(ValueError, match="미존재"):
            svc.act("없는아이디", action)

    def test_멱등_이미_resolved(self) -> None:
        store, _ = _store_with_item()
        svc = ManagerQueueService(queue_store=store)
        action = Dismiss(by_manager="root_manager")
        first = svc.act("item-001", action)
        second = svc.act("item-001", action)
        assert second.status == "resolved"
        # history에 resolved 두 번 쌓이지 않음(멱등)
        resolved_count = sum(1 for h in store.history if h.status == "resolved")
        assert resolved_count == 1
        _ = first  # suppress unused warning

    def test_재질문_Routed_회귀(self) -> None:
        """AssignOwner+intent → Precedent → 같은 intent 재질문이 자동 Routed."""
        from agent_org_network.agent_card import AgentCard
        from agent_org_network.ask_org import AskOrg, Answered
        from agent_org_network.audit import JsonlAuditLog
        from agent_org_network.classifier import FakeClassifier
        from agent_org_network.dispatch import LocalRuntimeDispatcher
        from agent_org_network.registry import Registry
        from agent_org_network.router import Router
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.user import User

        registry = Registry()
        root = User(id="root_manager")
        alice = User(id="alice", manager="root_manager")
        registry.register_user(root)
        registry.register_user(alice)
        card_a = AgentCard(
            agent_id="agent_a", owner="alice", team="t", summary="s", domains=["환불"],
            last_reviewed_at=_DATE,
        )
        registry.register(card_a)
        registry.validate()

        precedents = InMemoryPrecedentStore()
        case_store = InMemoryConflictCaseStore()

        # Deadlock 케이스를 직접 만들어 AssignOwner 처리
        case = _make_case(intent="환불")
        case_store.open_case(case)

        queue_store = InMemoryManagerQueueStore()
        source_dl: FromDeadlock = FromDeadlock(case=case, reason="표 갈림")
        item_dl = ManagerItem(
            manager_id="root_manager",
            source=source_dl,
            created_at=_NOW,
            item_id="item-dl",
        )
        queue_store.enqueue(item_dl)

        svc = ManagerQueueService(
            queue_store=queue_store, precedents=precedents, case_store=case_store
        )
        action = AssignOwner(by_manager="root_manager", primary="agent_a")
        svc.act("item-dl", action)

        # Precedent 기록됨
        assert precedents.lookup("환불") is not None

        # 같은 intent 질문이 이제 Routed
        classifier = FakeClassifier("환불")
        router = Router(registry, classifier, root_user="root_manager", precedents=precedents)
        ask = AskOrg(
            router=router,
            dispatcher=LocalRuntimeDispatcher(StubRuntime()),
            audit_log=JsonlAuditLog(Path("logs/audit-test.jsonl")),
            clock=_CLOCK,
        )
        reply = ask.handle("환불 기준 알려줘", User(id="web_guest"))
        assert isinstance(reply, Answered)


# ────────────────────────────────────────────────────────────────────────────
# 슬라이스 (d): web 라우트
# ────────────────────────────────────────────────────────────────────────────


def _make_web_app() -> tuple[Any, InMemoryManagerQueueStore]:
    from agent_org_network.runtime import StubRuntime
    from agent_org_network.web import create_app

    queue_store = InMemoryManagerQueueStore()
    source: FromUnowned = FromUnowned(decision=_make_unowned(), question="도와줘요")
    item = ManagerItem(
        manager_id="root_manager",
        source=source,
        created_at=_NOW,
        item_id="item-web-001",
    )
    queue_store.enqueue(item)

    app = create_app(runtime=StubRuntime(), manager_queue_store=queue_store)
    return app, queue_store


class TestManagerWebRoutes:
    def test_GET_pending_for_manager(self) -> None:
        app, _ = _make_web_app()
        client = TestClient(app)
        res = _result(cast(Response, client.get("/manager/root_manager")))  # pyright: ignore[reportUnknownMemberType]
        assert res.status == 200
        data: list[Any] = res.body
        assert isinstance(data, list)
        assert len(data) == 1
        item: dict[str, Any] = data[0]
        assert item["item_id"] == "item-web-001"
        assert item["manager_id"] == "root_manager"
        assert item["status"] == "open"

    def test_POST_act_AssignOwner(self) -> None:
        app, _ = _make_web_app()
        client = TestClient(app)
        res = _result(
            cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
                "/manager/items/item-web-001/act",
                json={
                    "type": "assign_owner",
                    "by_manager": "root_manager",
                    "primary": "cs_ops",
                    "rationale": "",
                },
            ))
        )
        assert res.status == 200
        data: dict[str, Any] = res.body
        assert data["status"] == "resolved"

    def test_POST_act_resolved_후_pending_에서_사라짐(self) -> None:
        app, _ = _make_web_app()
        client = TestClient(app)
        client.post(  # pyright: ignore[reportUnknownMemberType]
            "/manager/items/item-web-001/act",
            json={"type": "dismiss", "by_manager": "root_manager"},
        )
        res = _result(cast(Response, client.get("/manager/root_manager")))  # pyright: ignore[reportUnknownMemberType]
        assert res.body == []

    def test_POST_act_1인칭_위반_400(self) -> None:
        app, _ = _make_web_app()
        client = TestClient(app)
        res = _result(
            cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
                "/manager/items/item-web-001/act",
                json={"type": "dismiss", "by_manager": "남의매니저"},
            ))
        )
        assert res.status == 400

    def test_POST_act_미존재_404(self) -> None:
        app, _ = _make_web_app()
        client = TestClient(app)
        res = _result(
            cast(Response, client.post(  # pyright: ignore[reportUnknownMemberType]
                "/manager/items/없는아이디/act",
                json={"type": "dismiss", "by_manager": "root_manager"},
            ))
        )
        assert res.status == 404

    def test_운영면_내부값_노출_OK(self) -> None:
        """Manager 큐는 운영 면이라 내부값(manager_id·source) 노출 OK."""
        app, _ = _make_web_app()
        client = TestClient(app)
        res = _result(cast(Response, client.get("/manager/root_manager")))  # pyright: ignore[reportUnknownMemberType]
        data: list[Any] = res.body
        item: dict[str, Any] = data[0]
        assert "manager_id" in item
        assert "source" in item

    def test_채팅_노출_불변식_미침범(self) -> None:
        """채팅(POST /ask) 응답에 manager_id·escalation 내부값이 새지 않는다."""
        from agent_org_network.runtime import StubRuntime
        from agent_org_network.web import create_app

        _LEAKY_KEYS = {
            "confidence", "candidates", "escalated_to", "reason",
            "primary", "intent", "agent_id_internal",
        }
        app = create_app(runtime=StubRuntime())
        client = TestClient(app)
        res = _result(
            cast(Response, client.post("/ask", json={"question": "이 계약 조건 바꿔도 돼?"}))  # pyright: ignore[reportUnknownMemberType]
        )
        data: Any = res.body

        def _all_keys(d: Any) -> set[str]:  # noqa: ANN401
            keys: set[str] = set()
            if isinstance(d, dict):
                for k in d:  # pyright: ignore[reportUnknownVariableType]
                    k_str: str = k if isinstance(k, str) else repr(k)  # pyright: ignore[reportUnknownVariableType,reportUnknownArgumentType]
                    keys.add(k_str)
                    keys |= _all_keys(d[k])
            elif isinstance(d, list):
                for elem in d:  # pyright: ignore[reportUnknownVariableType]
                    keys |= _all_keys(elem)
            return keys

        exposed = _all_keys(data)
        leaked = _LEAKY_KEYS & exposed
        assert not leaked, f"채팅 응답에 내부값 샘: {leaked}"
