from __future__ import annotations

from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from threading import Barrier, Event

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization import CompletionBundle
from agent_org_network.conflict import (
    Candidate,
    CandidateRegistryChanged,
    ConflictCase,
    DivergentVotes,
)
from agent_org_network.demo_question_surfaces import DemoRouteAuthority
from agent_org_network.decision import Unowned
from agent_org_network.dispatch import EscalatedToManager, WorkTicket
from agent_org_network.manager_queue import (
    FromDeadlock,
    FromDispatch,
    FromUnowned,
    InMemoryManagerQueueStore,
    ManagerItem,
)
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConcurrencePending,
    ConflictDispositionIntegrity,
    ConflictDispositionDependency,
    ConflictEscalated,
    InMemoryConflictDispositionStore,
    OwnerPrincipal,
    P17ConflictDispositionApplication,
    SealedConflictClaimAvailable,
)
from agent_org_network.p17_manager_disposition import ExecutionStarted
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingManager,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
)
from agent_org_network.question_resolution import RequestLockPool
from agent_org_network.registry import Registry
from agent_org_network.request_correlation import LinkedEntityMismatchError
from agent_org_network.user import User


NOW = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)


def _ids(*values: str) -> Callable[[], object]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


def _card(agent_id: str, owner: str, *, domains: tuple[str, ...] = ("refund",)) -> AgentCard:
    return AgentCard(
        agent_id=agent_id,
        owner=owner,
        team="support",
        summary=agent_id,
        domains=list(domains),
        last_reviewed_at=date(2026, 7, 1),
    )


def _case() -> ConflictCase:
    return ConflictCase.for_request(
        request_id="request-1",
        intent="refund",
        question="환불 기준은?",
        candidates=(
            Candidate(agent_id="refund-card", owner="owner-a"),
            Candidate(agent_id="finance-card", owner="owner-b"),
        ),
        opened_at=NOW,
        case_id="case-1",
    )


def _deadlock_item(
    *,
    manager_id: str = "manager-a",
    case: ConflictCase | None = None,
    cause: DivergentVotes | CandidateRegistryChanged | None = None,
    reason: str = "divergent_votes",
    item_id: str = "item-1",
    created_at: datetime = NOW,
) -> ManagerItem:
    source_case = case or _case()
    source_cause = cause or DivergentVotes(round=source_case.concurrence_round)
    return ManagerItem.for_request(
        request_id="request-1",
        manager_id=manager_id,
        source=FromDeadlock(case=source_case, reason=reason, cause=source_cause),
        created_at=created_at,
        item_id=item_id,
    )


def test_FromDeadlock_legacy는_cause_none과_free_reason을_보존한다() -> None:
    legacy = FromDeadlock(
        case=ConflictCase(
            intent="refund",
            question="legacy",
            candidates=(),
            opened_at=NOW,
        ),
        reason="owners disagreed freely",
    )

    assert legacy.cause is None
    assert legacy.reason == "owners disagreed freely"


@pytest.mark.parametrize(
    "source",
    [
        FromDeadlock(case=_case(), reason="divergent_votes", cause=None),
        FromDeadlock(
            case=_case(),
            reason="wrong",
            cause=DivergentVotes(round=1),
        ),
        FromDeadlock(
            case=replace(_case(), status="escalated", manager_item_id="other"),
            reason="divergent_votes",
            cause=DivergentVotes(round=1),
        ),
        FromDeadlock(
            case=_case(),
            reason="divergent_votes",
            cause=DivergentVotes(round=2),
        ),
    ],
)
def test_request_aware_FromDeadlock은_exact_cause_reason_open_round를_요구한다(
    source: FromDeadlock,
) -> None:
    with pytest.raises(ValueError):
        ManagerItem.for_request(
            request_id="request-1",
            manager_id="manager-a",
            source=source,
            created_at=NOW,
        )


def test_request_aware_FromDeadlock_writer는_semantic_fingerprint와_두_index를_보존한다() -> None:
    store = InMemoryManagerQueueStore()
    first = _deadlock_item()
    stored, created = store.create_or_get_for_request(first)

    retry = _deadlock_item(
        item_id="retry-item",
        created_at=NOW + timedelta(days=1),
    )
    same, retry_created = store.create_or_get_for_request(retry)

    assert created is True
    assert retry_created is False
    assert same == stored == first
    assert store.get_by_request("request-1") == first
    assert store.get_by_case("case-1") == first

    reordered = replace(
        _case(),
        candidates=tuple(reversed(_case().candidates)),
    )
    with pytest.raises(LinkedEntityMismatchError):
        store.create_or_get_for_request(_deadlock_item(case=reordered))


def test_request_aware_FromDeadlock_writer는_FromUnowned충돌과_FromDispatch를_거부한다() -> None:
    store = InMemoryManagerQueueStore()
    store.create_or_get_for_request(_deadlock_item())
    unowned = ManagerItem.for_request(
        request_id="request-1",
        manager_id="root",
        source=FromUnowned(
            decision=Unowned(escalated_to="root", intent="refund"),
            question="환불 기준은?",
        ),
        created_at=NOW,
    )
    with pytest.raises((LinkedEntityMismatchError, ValueError)):
        store.create_or_get_for_request(unowned)

    dispatch = ManagerItem.for_request(
        request_id="request-2",
        manager_id="root",
        source=FromDispatch(
            outcome=EscalatedToManager(
                ticket=WorkTicket.for_request(
                    request_id="request-2",
                    attempt=1,
                    owner_id="owner-a",
                    agent_id="refund-card",
                    question="환불 기준은?",
                    enqueued_at=NOW,
                ),
                manager_id="root",
            )
        ),
        created_at=NOW,
    )
    with pytest.raises(ValueError, match="FromDispatch"):
        store.create_or_get_for_request(dispatch)


def test_request_aware_FromDeadlock_writer는_legacy_same_case_index를_덮어쓰지않는다() -> None:
    store = InMemoryManagerQueueStore()
    legacy_case = ConflictCase(
        intent="refund",
        question="legacy",
        candidates=(),
        opened_at=NOW,
        case_id="case-1",
    )
    legacy = ManagerItem(
        manager_id="root",
        source=FromDeadlock(case=legacy_case, reason="legacy free reason"),
        created_at=NOW,
        item_id="legacy-item",
    )
    stored_legacy, created = store.enqueue_deadlock_if_absent(legacy)
    assert created is True

    with pytest.raises(LinkedEntityMismatchError):
        store.create_or_get_for_request(_deadlock_item())

    assert store.get_by_case("case-1") == stored_legacy
    assert store.get_by_request("request-1") is None


class _DeadlinePolicy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, datetime]] = []

    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        self.calls.append((org_id, state_kind, started_at))
        return started_at + timedelta(hours=1)


class _Starter:
    def ensure_started(self, request_id: str) -> ExecutionStarted:
        del request_id
        return ExecutionStarted()


class _Reader:
    def by_request(self, request_id: str) -> CompletionBundle | None:
        del request_id
        return None

    def by_record(self, record_id: str) -> CompletionBundle | None:
        del record_id
        return None


def _fixture(
    *, root_user_id: str = "root"
) -> tuple[
    P17ConflictDispositionApplication,
    InMemoryQuestionRequestStore,
    InMemoryConflictDispositionStore,
    InMemoryManagerQueueStore,
    _DeadlinePolicy,
    Registry,
]:
    registry = Registry()
    for user in (
        User(id="root"),
        User(id="manager-a", manager="root"),
        User(id="manager-b", manager="root"),
        User(id="owner-a", manager="manager-a"),
        User(id="owner-b", manager="manager-b"),
    ):
        registry.register_user(user)
    registry.register(_card("refund-card", "owner-a"))
    registry.register(_card("finance-card", "owner-b"))

    requests = InMemoryQuestionRequestStore()
    received = QuestionRequest.receive(
        org_id="demo-org",
        requester_id="requester",
        question="환불 기준은?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    requests.create(received)
    awaiting = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id="case-1",
            handling=HandlingAssignment(
                kind="conflict_case",
                ref="case-1",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("request-1", 0, received, awaiting)

    conflicts = InMemoryConflictDispositionStore(
        id_factory=_ids("conflict-generation", "conflict-forward")
    )
    conflicts.create_or_get_for_request(_case())
    managers = InMemoryManagerQueueStore()
    deadlines = _DeadlinePolicy()
    app = P17ConflictDispositionApplication(
        requests=requests,
        conflicts=conflicts,
        managers=managers,
        registry=registry,
        route_authority=DemoRouteAuthority(registry),
        completion_reader=_Reader(),
        deadline_policy=deadlines,
        execution_starter=_Starter(),
        clock=lambda: NOW,
        root_user_id=root_user_id,
        manager_item_id_factory=lambda: "manager-item-1",
    )
    return app, requests, conflicts, managers, deadlines, registry


def _command(owner: str, target: str) -> ConcurOnConflict:
    return ConcurOnConflict(
        principal=OwnerPrincipal(org_id="demo-org", subject_id=owner),
        case_id="case-1",
        expected_round=1,
        on_agent=target,
    )


def test_full_application은_divergent_vote를_ManagerItem_Case_Request_세_link로_escalate한다() -> (
    None
):
    app, requests, conflicts, managers, deadlines, _registry = _fixture()

    first = app.concur(_command("owner-a", "refund-card"))
    escalated = app.concur(_command("owner-b", "finance-card"))

    assert isinstance(first, ConcurrencePending)
    assert escalated == ConflictEscalated(
        request_id="request-1",
        case_id="case-1",
        cause=DivergentVotes(round=1),
        manager_item_id="manager-item-1",
    )
    item = managers.get("manager-item-1")
    assert item is not None
    assert item.manager_id == "manager-a"
    assert item.request_id == "request-1"
    assert item.created_at == NOW
    assert item.source == FromDeadlock(
        case=_case(),
        cause=DivergentVotes(round=1),
        reason="divergent_votes",
    )
    case = conflicts.get_request_case("case-1")
    assert case == _case().escalate("manager-item-1")
    request = requests.get("request-1")
    assert request is not None
    assert request.revision == 2
    assert isinstance(request.state, AwaitingManager)
    assert request.state.item_id == "manager-item-1"
    assert request.state.public_kind == "contested"
    assert deadlines.calls == [("demo-org", "awaiting_manager", NOW)]

    before_history = managers.history
    assert app.concur(_command("owner-b", "finance-card")) == escalated
    assert managers.history == before_history
    assert deadlines.calls == [("demo-org", "awaiting_manager", NOW)]


def test_full_application은_registry_drift를_vote로_쓰지않고_다음_유효후보_Manager로_escalate한다() -> (
    None
):
    app, requests, conflicts, managers, _deadlines, registry = _fixture()
    registry.replace_card(_card("refund-card", "missing-owner"))

    escalated = app.concur(_command("owner-a", "refund-card"))

    assert isinstance(escalated, ConflictEscalated)
    assert escalated.cause == CandidateRegistryChanged(round=1, reason_code="owner_missing")
    item = managers.get(escalated.manager_item_id)
    assert item is not None
    assert item.manager_id == "manager-b"
    assert isinstance(item.source, FromDeadlock)
    assert item.source.reason == "candidate_registry_changed:owner_missing"
    assert requests.get("request-1") is not None
    assert conflicts.progress_history_for_case("case-1")[0].kind == "claim_reserved"


@pytest.mark.parametrize(
    ("refund_card", "finance_card"),
    [
        (
            _card("refund-card", "owner-a", domains=("contract",)),
            _card("finance-card", "missing-owner"),
        ),
        (
            _card("refund-card", "missing-owner"),
            _card("finance-card", "owner-b", domains=("contract",)),
        ),
    ],
)
def test_registry_drift_복합원인은_후보순서와_무관하게_ADR_우선순위를_따른다(
    refund_card: AgentCard,
    finance_card: AgentCard,
) -> None:
    app, _requests, _conflicts, _managers, _deadlines, registry = _fixture()
    registry.replace_card(refund_card)
    registry.replace_card(finance_card)

    escalated = app.concur(_command("owner-a", "refund-card"))

    assert isinstance(escalated, ConflictEscalated)
    assert escalated.cause == CandidateRegistryChanged(
        round=1,
        reason_code="owner_missing",
    )


def test_full_application은_유효후보가_하나도_없으면_현재_root로_escalate한다() -> None:
    app, _requests, _conflicts, managers, _deadlines, registry = _fixture()
    registry.replace_card(_card("refund-card", "missing-a"))
    registry.replace_card(_card("finance-card", "missing-b"))

    escalated = app.concur(_command("owner-a", "refund-card"))

    assert isinstance(escalated, ConflictEscalated)
    item = managers.get(escalated.manager_item_id)
    assert item is not None
    assert item.manager_id == "root"


def test_full_application은_root_User가_없으면_어떤_link도_쓰지않는다() -> None:
    app, requests, conflicts, managers, _deadlines, _registry = _fixture(
        root_user_id="missing-root"
    )
    before_request = requests.get("request-1")
    before_case = conflicts.get_request_case("case-1")
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_command("owner-b", "finance-card"))

    assert managers.get_by_request("request-1") is None
    assert conflicts.get_request_case("case-1") == before_case
    assert requests.get("request-1") == before_request


class _WriteThenFailManagers(InMemoryManagerQueueStore):
    def __init__(self, *, fail_before: bool = False) -> None:
        super().__init__()
        self.fail_before = fail_before
        self.failed = False

    def create_or_get_for_request(self, item: ManagerItem) -> tuple[ManagerItem, bool]:
        if self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("manager pre-write")
        result = super().create_or_get_for_request(item)
        if not self.failed:
            self.failed = True
            raise RuntimeError("manager post-write")
        return result


class _TamperedCreateResultManagers(InMemoryManagerQueueStore):
    def __init__(self, mode: str) -> None:
        super().__init__()
        self.mode = mode

    def create_or_get_for_request(self, item: ManagerItem) -> tuple[ManagerItem, bool]:
        stored, created = super().create_or_get_for_request(item)
        if self.mode == "item":
            return replace(stored, item_id="tampered-item"), created
        return stored, 1  # type: ignore[return-value]


class _WriteThenFailConflicts(InMemoryConflictDispositionStore):
    def __init__(self, *, fail_before: bool = False) -> None:
        super().__init__(id_factory=_ids("conflict-generation", "conflict-forward"))
        self.fail_before = fail_before
        self.failed = False

    def transition_for_claim(self, handle: object, *, target: ConflictCase) -> ConflictCase:
        if self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("case pre-write")
        result = super().transition_for_claim(handle, target=target)  # type: ignore[arg-type]
        if not self.failed:
            self.failed = True
            raise RuntimeError("case post-write")
        return result


class _TamperedSealedConflicts(InMemoryConflictDispositionStore):
    def __init__(self, mode: str) -> None:
        super().__init__(id_factory=_ids("conflict-generation", "conflict-forward"))
        self.mode = mode

    def sealed_claim_for_case(self, case_id: str) -> SealedConflictClaimAvailable | None:
        available = super().sealed_claim_for_case(case_id)
        if available is None or available.claim.kind != "sealed_deadlock":
            return available
        if self.mode == "wrong_case":
            trigger = available.claim.trigger.model_copy(update={"case_id": "other-case"})
            claim = available.claim.model_copy(
                update={
                    "case_id": "other-case",
                    "idempotency_key": "conflict-disposition:other-case:1",
                    "trigger": trigger,
                }
            )
            return available.model_copy(update={"claim": claim})
        bad_handle = available.handle.model_copy(update={"forward_token": ""})
        return available.model_copy(update={"handle": bad_handle})


class _SequencedDeadline(_DeadlinePolicy):
    def __init__(self, values: list[datetime]) -> None:
        super().__init__()
        self.values = values

    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        self.calls.append((org_id, state_kind, started_at))
        return self.values.pop(0)


class _WriteThenFailRequests(InMemoryQuestionRequestStore):
    def __init__(self, *, fail_before: bool = False) -> None:
        super().__init__()
        self.fail_before = fail_before
        self.failed = False
        self.escalation_cas_commits = 0

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        is_escalation = expected_revision == 1 and isinstance(updated.state, AwaitingManager)
        if is_escalation and self.fail_before and not self.failed:
            self.failed = True
            raise RuntimeError("request pre-write")
        result = super().compare_and_set(request_id, expected_revision, current, updated)
        if is_escalation and result:
            self.escalation_cas_commits += 1
        if is_escalation and not self.failed:
            self.failed = True
            raise RuntimeError("request post-write")
        return result


def _fault_fixture(
    *,
    managers: InMemoryManagerQueueStore | None = None,
    conflicts: InMemoryConflictDispositionStore | None = None,
    requests: InMemoryQuestionRequestStore | None = None,
    request_locks: RequestLockPool | None = None,
    deadline_policy: _DeadlinePolicy | None = None,
    clock: Callable[[], datetime] | None = None,
) -> tuple[
    P17ConflictDispositionApplication,
    InMemoryQuestionRequestStore,
    InMemoryConflictDispositionStore,
    InMemoryManagerQueueStore,
    Registry,
]:
    registry = Registry()
    for user in (
        User(id="root"),
        User(id="manager-a", manager="root"),
        User(id="owner-a", manager="manager-a"),
        User(id="owner-b", manager="manager-a"),
    ):
        registry.register_user(user)
    registry.register(_card("refund-card", "owner-a"))
    registry.register(_card("finance-card", "owner-b"))
    request_store = requests or InMemoryQuestionRequestStore()
    received = QuestionRequest.receive(
        org_id="demo-org",
        requester_id="requester",
        question="환불 기준은?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW + timedelta(hours=1),
    )
    request_store.create(received)
    awaiting = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id="case-1",
            handling=HandlingAssignment(
                kind="conflict_case", ref="case-1", due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW,
    )
    assert request_store.compare_and_set("request-1", 0, received, awaiting)
    conflict_store = conflicts or InMemoryConflictDispositionStore(
        id_factory=_ids("conflict-generation", "conflict-forward")
    )
    conflict_store.create_or_get_for_request(_case())
    manager_store = managers or InMemoryManagerQueueStore()
    app = P17ConflictDispositionApplication(
        requests=request_store,
        conflicts=conflict_store,
        managers=manager_store,
        registry=registry,
        route_authority=DemoRouteAuthority(registry),
        completion_reader=_Reader(),
        deadline_policy=deadline_policy or _DeadlinePolicy(),
        execution_starter=_Starter(),
        clock=clock or (lambda: NOW),
        root_user_id="root",
        manager_item_id_factory=lambda: "manager-item-1",
        request_locks=request_locks,
    )
    return app, request_store, conflict_store, manager_store, registry


@pytest.mark.parametrize("mode", ["item", "flag"])
def test_Manager_create_result가_변조되면_Case_Request_write0이다(mode: str) -> None:
    managers = _TamperedCreateResultManagers(mode)
    app, requests, conflicts, _, _registry = _fault_fixture(managers=managers)
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_command("owner-b", "finance-card"))

    assert conflicts.get_request_case("case-1") == _case()
    request = requests.get("request-1")
    assert request is not None
    assert isinstance(request.state, AwaitingConflict)


@pytest.mark.parametrize("bad", [NOW.replace(tzinfo=None), NOW - timedelta(seconds=1)])
def test_bad_clock은_Item_Case_Request_write0이고_정상화뒤_retry된다(bad: datetime) -> None:
    values = iter((bad, NOW))
    app, requests, conflicts, managers, _registry = _fault_fixture(clock=lambda: next(values))
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_command("owner-b", "finance-card"))
    assert managers.get_by_request("request-1") is None
    assert conflicts.get_request_case("case-1") == _case()
    request = requests.get("request-1")
    assert request is not None
    assert isinstance(request.state, AwaitingConflict)

    assert isinstance(app.concur(_command("owner-b", "finance-card")), ConflictEscalated)


@pytest.mark.parametrize("bad", [NOW.replace(tzinfo=None), NOW - timedelta(seconds=1)])
def test_bad_deadline은_cache하지않고_Case_Request_write0뒤_retry된다(bad: datetime) -> None:
    policy = _SequencedDeadline([bad, NOW + timedelta(hours=1)])
    app, requests, conflicts, managers, _registry = _fault_fixture(deadline_policy=policy)
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_command("owner-b", "finance-card"))
    assert managers.get_by_request("request-1") is not None
    assert conflicts.get_request_case("case-1") == _case()
    request = requests.get("request-1")
    assert request is not None
    assert isinstance(request.state, AwaitingConflict)

    assert isinstance(app.concur(_command("owner-b", "finance-card")), ConflictEscalated)
    assert len(policy.calls) == 2


@pytest.mark.parametrize("mode", ["wrong_case", "bad_handle"])
def test_sealed_Deadlock_return이_변조되면_Manager_Case_Request_write0이다(mode: str) -> None:
    conflicts = _TamperedSealedConflicts(mode)
    app, requests, _, managers, _registry = _fault_fixture(conflicts=conflicts)
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_command("owner-b", "finance-card"))

    assert managers.get_by_request("request-1") is None
    assert conflicts.get_request_case("case-1") == _case()
    request = requests.get("request-1")
    assert request is not None
    assert isinstance(request.state, AwaitingConflict)


@pytest.mark.parametrize("fail_before", [True, False])
def test_ManagerItem_pre_post_write_fault는_same_action_retry로_forward_repair한다(
    fail_before: bool,
) -> None:
    managers = _WriteThenFailManagers(fail_before=fail_before)
    app, requests, conflicts, _, _registry = _fault_fixture(managers=managers)
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_command("owner-b", "finance-card"))
    result = app.concur(_command("owner-b", "finance-card"))

    assert isinstance(result, ConflictEscalated)
    assert len([item for item in managers.history if item.status == "open"]) == 1
    assert conflicts.get_request_case("case-1") == _case().escalate("manager-item-1")
    assert requests.get("request-1") is not None


@pytest.mark.parametrize("fail_before", [True, False])
def test_Case_pre_post_write_fault는_same_action_retry로_forward_repair한다(
    fail_before: bool,
) -> None:
    conflicts = _WriteThenFailConflicts(fail_before=fail_before)
    app, requests, _, managers, _registry = _fault_fixture(conflicts=conflicts)
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_command("owner-b", "finance-card"))
    result = app.concur(_command("owner-b", "finance-card"))

    assert isinstance(result, ConflictEscalated)
    assert len([case for case in conflicts.history if case.status == "escalated"]) == 1
    assert managers.get_by_request("request-1") is not None
    assert requests.get("request-1") is not None


@pytest.mark.parametrize("fail_before", [True, False])
def test_Request_CAS_pre_post_write_fault는_same_action_retry로_forward_repair한다(
    fail_before: bool,
) -> None:
    requests = _WriteThenFailRequests(fail_before=fail_before)
    app, _, conflicts, managers, _registry = _fault_fixture(requests=requests)
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionDependency):
        app.concur(_command("owner-b", "finance-card"))
    result = app.concur(_command("owner-b", "finance-card"))

    assert isinstance(result, ConflictEscalated)
    assert requests.escalation_cas_commits == 1
    assert len([case for case in conflicts.history if case.status == "escalated"]) == 1
    assert len([item for item in managers.history if item.status == "open"]) == 1


def test_existing_ManagerItem_winner가_다르면_Case와_Request를_전이하지않는다() -> None:
    app, requests, conflicts, managers, _registry = _fault_fixture()
    wrong = ManagerItem.for_request(
        request_id="request-1",
        manager_id="root",
        source=FromUnowned(
            decision=Unowned(escalated_to="root", intent="refund"),
            question="환불 기준은?",
        ),
        created_at=NOW,
        item_id="wrong-item",
    )
    managers.create_or_get_for_request(wrong)
    app.concur(_command("owner-a", "refund-card"))

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_command("owner-b", "finance-card"))

    assert conflicts.get_request_case("case-1") == _case()
    stored_request = requests.get("request-1")
    assert stored_request is not None
    assert isinstance(stored_request.state, AwaitingConflict)


def test_existing_Case_escalation_winner가_다르면_ManagerItem과_Request를_쓰지않는다() -> None:
    managers = _WriteThenFailManagers(fail_before=True)
    app, requests, conflicts, _, _registry = _fault_fixture(managers=managers)
    app.concur(_command("owner-a", "refund-card"))
    with pytest.raises(ConflictDispositionDependency):
        app.concur(_command("owner-b", "finance-card"))
    available = conflicts.sealed_claim_for_case("case-1")
    assert available is not None
    conflicts.transition_for_claim(
        available.handle,
        target=_case().escalate("wrong-item"),
    )

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_command("owner-b", "finance-card"))

    assert managers.get_by_request("request-1") is None
    stored_request = requests.get("request-1")
    assert stored_request is not None
    assert isinstance(stored_request.state, AwaitingConflict)


def test_existing_Request_winner가_다르면_Case와_Item을_그대로_보존한다() -> None:
    conflicts = _WriteThenFailConflicts(fail_before=False)
    app, requests, _, managers, _registry = _fault_fixture(conflicts=conflicts)
    app.concur(_command("owner-a", "refund-card"))
    with pytest.raises(ConflictDispositionDependency):
        app.concur(_command("owner-b", "finance-card"))
    current = requests.get("request-1")
    assert current is not None
    assert isinstance(current.state, AwaitingConflict)
    wrong = current.transition(
        AwaitingManager(
            item_id="wrong-item",
            public_kind="contested",
            handling=HandlingAssignment(
                kind="manager_item",
                ref="wrong-item",
                due_at=NOW + timedelta(hours=1),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("request-1", 1, current, wrong)

    with pytest.raises(ConflictDispositionIntegrity):
        app.concur(_command("owner-b", "finance-card"))

    assert conflicts.get_request_case("case-1") == _case().escalate("manager-item-1")
    assert managers.get_by_request("request-1") is not None


def test_divergent_final_vote_32way는_ManagerItem_Case_Request를_각한번만_쓴다() -> None:
    requests = _WriteThenFailRequests()
    requests.failed = True
    request_locks = RequestLockPool()
    app, _, conflicts, managers, _registry = _fault_fixture(
        requests=requests,
        request_locks=request_locks,
    )
    app.concur(_command("owner-a", "refund-card"))

    results = _race_same_action(app, _command("owner-b", "finance-card"))

    assert all(isinstance(result, ConflictEscalated) for result in results)
    assert len([item for item in managers.history if item.status == "open"]) == 1
    assert len([case for case in conflicts.history if case.status == "escalated"]) == 1
    assert requests.escalation_cas_commits == 1
    assert request_locks.active_count == 0


def test_registry_drift_32way는_ManagerItem_Case_Request를_각한번만_쓴다() -> None:
    requests = _WriteThenFailRequests()
    requests.failed = True
    app, _, conflicts, managers, registry = _fault_fixture(requests=requests)
    registry.replace_card(_card("refund-card", "owner-b"))
    command = _command("owner-a", "refund-card")

    results = _race_same_action(app, command)

    assert all(isinstance(result, ConflictEscalated) for result in results)
    assert len([item for item in managers.history if item.status == "open"]) == 1
    assert len([case for case in conflicts.history if case.status == "escalated"]) == 1
    assert requests.escalation_cas_commits == 1


def test_서로다른_full_application_32way도_세_link의_한_winner로_수렴한다() -> None:
    requests = _WriteThenFailRequests()
    requests.failed = True
    first_locks = RequestLockPool()
    first, _, conflicts, managers, registry = _fault_fixture(
        requests=requests,
        request_locks=first_locks,
    )
    second_locks = RequestLockPool()
    second = P17ConflictDispositionApplication(
        requests=requests,
        conflicts=conflicts,
        managers=managers,
        registry=registry,
        route_authority=DemoRouteAuthority(registry),
        completion_reader=_Reader(),
        deadline_policy=_DeadlinePolicy(),
        execution_starter=_Starter(),
        clock=lambda: NOW,
        root_user_id="root",
        manager_item_id_factory=lambda: "manager-item-2",
        request_locks=second_locks,
    )
    first.concur(_command("owner-a", "refund-card"))

    results = _race_across_apps(
        (first, second),
        _command("owner-b", "finance-card"),
    )

    assert all(isinstance(result, ConflictEscalated) for result in results)
    assert (
        len({result.manager_item_id for result in results if isinstance(result, ConflictEscalated)})
        == 1
    )
    assert len([item for item in managers.history if item.status == "open"]) == 1
    assert len([case for case in conflicts.history if case.status == "escalated"]) == 1
    assert requests.escalation_cas_commits == 1
    assert first_locks.active_count == second_locks.active_count == 0


def _race_same_action(
    app: P17ConflictDispositionApplication,
    command: ConcurOnConflict,
) -> list[object]:
    barrier = Barrier(33)
    release = Event()

    def run() -> object:
        barrier.wait(timeout=5)
        assert release.wait(timeout=5)
        return app.concur(command)

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(run) for _ in range(32)]
        barrier.wait(timeout=5)
        release.set()
        return [future.result(timeout=5) for future in futures]


def _race_across_apps(
    apps: tuple[P17ConflictDispositionApplication, ...],
    command: ConcurOnConflict,
) -> list[object]:
    barrier = Barrier(33)
    release = Event()

    def run(app: P17ConflictDispositionApplication) -> object:
        barrier.wait(timeout=5)
        assert release.wait(timeout=5)
        return app.concur(command)

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(run, apps[index % len(apps)]) for index in range(32)]
        barrier.wait(timeout=5)
        release.set()
        return [future.result(timeout=5) for future in futures]
