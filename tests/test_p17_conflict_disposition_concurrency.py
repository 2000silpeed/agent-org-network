from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Barrier, Event, Lock

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.answer_finalization import CompletionBundle
from agent_org_network.conflict import Candidate, ConflictCase
from agent_org_network.demo_question_surfaces import DemoRouteAuthority
from agent_org_network.p17_conflict_disposition import (
    ConcurOnConflict,
    ConcurrenceActionFingerprint,
    ConflictClaimAcquired,
    ConflictClaimConflict,
    ConflictClaimInProgress,
    ConflictConcurrenceAttempt,
    ConflictDispositionConflict,
    ConflictDispositionError,
    ConflictDispositionInProgress,
    ConflictResolved,
    InMemoryConflictDispositionStore,
    OwnerConcurrenceEvidence,
    OwnerPrincipal,
    P17DirectConcurrenceResult,
    P17DirectConflictDispositionApplication,
    SealedConflictClaimAvailable,
    ValidatedOwnerVote,
)
from agent_org_network.p17_manager_disposition import ExecutionStarted
from agent_org_network.question_request import (
    AwaitingConflict,
    HandlingAssignment,
    InMemoryQuestionRequestStore,
    QuestionRequest,
    ReadyToDispatch,
)
from agent_org_network.question_resolution import AuthorityGrant
from agent_org_network.registry import Registry, RegistryError
from agent_org_network.request_route_authority import (
    RequestRouteGrantAssignment,
    RequestRouteGrantResult,
)
from agent_org_network.user import User


NOW = datetime(2026, 7, 13, 11, 0, tzinfo=timezone.utc)
WORKERS = 32


def _ids(*values: str) -> Callable[[], object]:
    iterator: Iterator[str] = iter(values)
    return lambda: next(iterator)


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


def _command(owner: str, target: str, *, org_id: str = "org-1") -> ConcurOnConflict:
    return ConcurOnConflict(
        principal=OwnerPrincipal(org_id=org_id, subject_id=owner),
        case_id="case-1",
        expected_round=1,
        on_agent=target,
    )


def _validated(case: ConflictCase, command: ConcurOnConflict) -> ValidatedOwnerVote:
    return ValidatedOwnerVote(
        request_id="request-1",
        case_id=case.case_id,
        org_id=command.principal.org_id,
        intent=case.intent,
        candidate_snapshot=case.candidates,
        trigger=ConcurrenceActionFingerprint(
            case_id=case.case_id,
            org_id=command.principal.org_id,
            owner_id=command.principal.subject_id,
            expected_round=command.expected_round,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        ),
        evidence=OwnerConcurrenceEvidence(
            round=command.expected_round,
            owner_id=command.principal.subject_id,
            on_agent=command.on_agent,
            stance=command.stance,
            rationale=command.rationale,
        ),
        target_requires_approval=False,
    )


def _store() -> InMemoryConflictDispositionStore:
    store = InMemoryConflictDispositionStore(
        id_factory=_ids("generation-1", "control-or-forward-1")
    )
    _stored, created = store.create_or_get_for_request(_case())
    assert created is True
    first = _command("owner-a", "refund-card")
    store.reserve_validated_concurrence(
        "case-1", first, validate=lambda case, command: _validated(case, command)
    )
    return store


def _race(
    store: InMemoryConflictDispositionStore,
    commands: tuple[ConcurOnConflict, ...],
) -> list[ConflictConcurrenceAttempt]:
    barrier = Barrier(WORKERS + 1)
    release = Event()

    def run(command: ConcurOnConflict) -> ConflictConcurrenceAttempt:
        barrier.wait()
        assert release.wait(timeout=5)
        return store.reserve_validated_concurrence(
            "case-1",
            command,
            validate=lambda case, action: _validated(case, action),
        )

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(run, command) for command in commands]
        barrier.wait()
        release.set()
        return [future.result(timeout=5) for future in futures]


def test_same_final_vote_32way는_claim을_한번만_예약한다() -> None:
    store = _store()
    command = _command("owner-b", "refund-card")

    results = _race(store, (command,) * WORKERS)

    kinds: Counter[str] = Counter(result.kind for result in results)
    assert kinds == Counter({"in_progress": WORKERS - 1, "acquired": 1})
    assert sum(isinstance(result, ConflictClaimAcquired) for result in results) == 1
    assert sum(isinstance(result, ConflictClaimInProgress) for result in results) == 31
    progress = store.progress_history_for_case("case-1")
    assert Counter(entry.kind for entry in progress) == Counter(
        {"vote_stored": 2, "claim_reserved": 1}
    )


def test_conflicting_final_vote_32way는_한_action만_claim을_소유한다() -> None:
    store = _store()
    same = _command("owner-b", "refund-card")
    divergent = _command("owner-b", "finance-card")

    results = _race(store, (same, divergent) * (WORKERS // 2))

    conflicts = [result for result in results if isinstance(result, ConflictClaimConflict)]
    assert len(conflicts) == WORKERS // 2
    winners = [result for result in results if not isinstance(result, ConflictClaimConflict)]
    if isinstance(winners[0], SealedConflictClaimAvailable):
        assert all(result == winners[0] for result in winners)
    else:
        assert sum(isinstance(result, ConflictClaimAcquired) for result in winners) == 1
        assert sum(isinstance(result, ConflictClaimInProgress) for result in winners) == 15
    progress = store.progress_history_for_case("case-1")
    kinds = Counter(entry.kind for entry in progress)
    assert kinds["vote_stored"] == 2
    assert kinds["claim_reserved"] == 1
    assert kinds["claim_sealed"] in (0, 1)


def test_registry_snapshot은_same_thread_mutation을_거부하고_other_thread를_대기시킨다() -> None:
    registry = Registry()
    registry.register_user(User(id="owner-a"))
    registry.register_user(User(id="owner-b"))
    original = AgentCard(
        agent_id="refund-card",
        owner="owner-a",
        team="support",
        summary="refund",
        domains=["refund"],
        last_reviewed_at=date(2026, 7, 1),
    )
    registry.register(original)
    replacement = original.model_copy(update={"owner": "owner-b"})

    with registry.consistency_guard():
        with pytest.raises(RegistryError, match="snapshot"):
            registry.replace_card(replacement)

    entered = Event()
    release = Event()
    mutation_finished = Event()

    def hold_snapshot() -> None:
        with registry.consistency_guard():
            entered.set()
            assert release.wait(timeout=5)

    def mutate() -> None:
        assert entered.wait(timeout=5)
        registry.replace_card(replacement)
        mutation_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_snapshot)
        mutator = executor.submit(mutate)
        assert entered.wait(timeout=5)
        assert mutation_finished.wait(timeout=0.05) is False
        release.set()
        holder.result(timeout=5)
        mutator.result(timeout=5)

    assert registry.get("refund-card").owner == "owner-b"


class _Deadline:
    def deadline_for(self, org_id: str, state_kind: str, started_at: datetime) -> datetime:
        del org_id, state_kind
        return started_at.replace(hour=started_at.hour + 1)


class _Reader:
    def by_request(self, request_id: str) -> CompletionBundle | None:
        del request_id
        return None

    def by_record(self, record_id: str) -> CompletionBundle | None:
        del record_id
        return None


class _CountingStarter:
    def __init__(self) -> None:
        self._lock = Lock()
        self.calls = 0

    def ensure_started(self, request_id: str) -> ExecutionStarted:
        del request_id
        with self._lock:
            self.calls += 1
        return ExecutionStarted()


class _CountingRequests(InMemoryQuestionRequestStore):
    def __init__(self) -> None:
        super().__init__()
        self._count_lock = Lock()
        self.ready_commits = 0

    def compare_and_set(
        self,
        request_id: str,
        expected_revision: int,
        current: QuestionRequest,
        updated: QuestionRequest,
    ) -> bool:
        result = super().compare_and_set(request_id, expected_revision, current, updated)
        if result and isinstance(updated.state, ReadyToDispatch):
            with self._count_lock:
                self.ready_commits += 1
        return result


class _BlockingReadRequests(_CountingRequests):
    def __init__(self) -> None:
        super().__init__()
        self.block_next_read = False
        self.read_entered = Event()
        self.release_read = Event()

    def get(self, request_id: str) -> QuestionRequest | None:
        stored = super().get(request_id)
        if self.block_next_read:
            self.block_next_read = False
            self.read_entered.set()
            assert self.release_read.wait(timeout=5)
        return stored


class _CountingAuthority:
    def __init__(self, delegate: DemoRouteAuthority) -> None:
        self.delegate = delegate
        self._lock = Lock()
        self.grant_calls = 0

    def authorize(self, org_id: str, intent: str, agent_id: str) -> AuthorityGrant | None:
        return self.delegate.authorize(org_id, intent, agent_id)

    def grant_for_request(
        self,
        assignment: RequestRouteGrantAssignment,
    ) -> RequestRouteGrantResult:
        with self._lock:
            self.grant_calls += 1
        return self.delegate.grant_for_request(assignment)

    def authorize_for_request(
        self,
        org_id: str,
        request_id: str,
        intent: str,
        agent_id: str,
    ) -> AuthorityGrant | None:
        return self.delegate.authorize_for_request(org_id, request_id, intent, agent_id)


def _full_application(
    *,
    registry: Registry | None = None,
    requests: _CountingRequests | None = None,
) -> tuple[
    P17DirectConflictDispositionApplication,
    _CountingRequests,
    InMemoryConflictDispositionStore,
    _CountingAuthority,
    _CountingStarter,
]:
    registry = registry or Registry()
    for owner in ("owner-a", "owner-b"):
        registry.register_user(User(id=owner))
    for agent_id, owner in (("refund-card", "owner-a"), ("finance-card", "owner-b")):
        registry.register(
            AgentCard(
                agent_id=agent_id,
                owner=owner,
                team="support",
                summary=agent_id,
                domains=["refund"],
                last_reviewed_at=date(2026, 7, 1),
            )
        )
    requests = requests or _CountingRequests()
    received = QuestionRequest.receive(
        org_id="demo-org",
        requester_id="requester",
        question="환불 기준은?",
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        due_at=NOW.replace(hour=NOW.hour + 1),
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
                due_at=NOW.replace(hour=NOW.hour + 1),
            ),
        ),
        clock=lambda: NOW,
    )
    assert requests.compare_and_set("request-1", 0, received, awaiting)
    conflicts = InMemoryConflictDispositionStore(
        id_factory=_ids("generation-1", "control-or-forward-1", "forward-1")
    )
    conflicts.create_or_get_for_request(_case())
    authority = _CountingAuthority(DemoRouteAuthority(registry))
    starter = _CountingStarter()
    app = P17DirectConflictDispositionApplication(
        requests=requests,
        conflicts=conflicts,
        registry=registry,
        route_authority=authority,
        completion_reader=_Reader(),
        deadline_policy=_Deadline(),
        execution_starter=starter,
        clock=lambda: NOW,
    )
    return app, requests, conflicts, authority, starter


def _race_application(
    app: P17DirectConflictDispositionApplication,
    commands: tuple[ConcurOnConflict, ...],
) -> list[P17DirectConcurrenceResult | ConflictDispositionError]:
    barrier = Barrier(WORKERS + 1)
    release = Event()

    def run(command: ConcurOnConflict) -> P17DirectConcurrenceResult | ConflictDispositionError:
        barrier.wait()
        assert release.wait(timeout=5)
        try:
            return app.concur(command)
        except ConflictDispositionError as error:
            return error

    with ThreadPoolExecutor(max_workers=WORKERS) as executor:
        futures = [executor.submit(run, command) for command in commands]
        barrier.wait()
        release.set()
        return [future.result(timeout=5) for future in futures]


def test_full_application_same_final_vote_32way는_grant_evidence_CAS_terminal을_한번만쓴다() -> (
    None
):
    app, requests, conflicts, authority, starter = _full_application()
    app.concur(_command("owner-a", "refund-card", org_id="demo-org"))

    results = _race_application(
        app,
        (_command("owner-b", "refund-card", org_id="demo-org"),) * WORKERS,
    )

    assert all(isinstance(result, ConflictResolved) for result in results)
    assert authority.grant_calls == 1
    assert requests.ready_commits == 1
    evidence = conflicts.resolution_evidence_for_request("request-1")
    assert evidence is not None and evidence.route.agent_id == "refund-card"
    assert (
        sum(
            entry.kind == "resolution_evidence_recorded"
            for entry in conflicts.progress_history_for_case("case-1")
        )
        == 1
    )
    case = conflicts.get_request_case("case-1")
    assert case is not None and case.status == "resolved"
    assert starter.calls == WORKERS


def test_full_application_conflicting_final_vote_32way는_한결론만_전이한다() -> None:
    app, requests, conflicts, authority, _starter = _full_application()
    app.concur(_command("owner-a", "refund-card", org_id="demo-org"))
    same = _command("owner-b", "refund-card", org_id="demo-org")
    divergent = _command("owner-b", "finance-card", org_id="demo-org")

    results = _race_application(app, (same, divergent) * (WORKERS // 2))

    resolved = sum(isinstance(result, ConflictResolved) for result in results)
    in_progress = sum(isinstance(result, ConflictDispositionInProgress) for result in results)
    conflicts_count = sum(isinstance(result, ConflictDispositionConflict) for result in results)
    assert conflicts_count == WORKERS // 2
    assert (resolved, in_progress) in ((WORKERS // 2, 0), (0, WORKERS // 2))
    case = conflicts.get_request_case("case-1")
    assert case is not None
    if resolved:
        assert case.status == "resolved"
        assert authority.grant_calls == 1
        assert requests.ready_commits == 1
        assert conflicts.resolution_evidence_for_request("request-1") is not None
    else:
        assert case.status == "open"
        assert authority.grant_calls == 0
        assert requests.ready_commits == 0
        assert conflicts.resolution_evidence_for_request("request-1") is None


def test_blocking_request_read중_registry_mutation이_완료되어_outer_guard가_없다() -> None:
    registry = Registry()
    requests = _BlockingReadRequests()
    app, _requests, _conflicts, _authority, _starter = _full_application(
        registry=registry,
        requests=requests,
    )
    app.concur(_command("owner-a", "refund-card", org_id="demo-org"))
    requests.block_next_read = True
    mutation_finished = Event()

    def mutate_registry() -> None:
        current = registry.get("refund-card")
        registry.replace_card(current.model_copy(update={"summary": "updated refund"}))
        mutation_finished.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolution = executor.submit(
            app.concur,
            _command("owner-b", "refund-card", org_id="demo-org"),
        )
        assert requests.read_entered.wait(timeout=5)
        mutation = executor.submit(mutate_registry)
        completed_while_request_blocked = mutation_finished.wait(timeout=0.2)
        requests.release_read.set()
        result = resolution.result(timeout=5)
        mutation.result(timeout=5)

    assert completed_while_request_blocked is True
    assert isinstance(result, ConflictResolved)
