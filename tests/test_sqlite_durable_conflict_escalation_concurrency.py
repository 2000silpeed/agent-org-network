from __future__ import annotations
# pyright: reportArgumentType=false

import dataclasses
import hashlib
import json
import sqlite3
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.conflict_escalation_approval_evidence import (
    ESCALATE_ACTION,
    ConflictEscalationApprovalEvidence,
    canonical_escalate_command_digest,
    escalation_cause_digest,
    escalation_resource_fingerprint,
)
from agent_org_network.conflict_escalation_registry_snapshot import (
    ConflictEscalationOwnerPath,
    ConflictEscalationRegistrySnapshot,
    ConflictEscalationRegistrySnapshotError,
    ConflictEscalationSnapshotCandidate,
)
from agent_org_network.conflict_open_contract import ConflictOpenCandidateClaim
from agent_org_network.durable_conflict_escalation_evidence import DivergentVotes
from agent_org_network.question_request import (
    AwaitingConflict,
    HandlingAssignment,
    QuestionRequest,
    RouteTarget,
)
from agent_org_network.sqlite_completion import (
    migrate_sqlite_completion_schema,
    validate_sqlite_completion_connection,
)
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    migrate_sqlite_durable_conflict_escalation_receipts_schema,
    open_sqlite_durable_conflict_escalation_receipts_connection,
    validate_sqlite_durable_conflict_escalation_receipts_connection,
)
from agent_org_network.sqlite_durable_conflict_escalation_uow import (
    DurableConflictEscalateCommand,
    DurableConflictEscalatedToManager,
    DurableConflictEscalatedToRoot,
    DurableConflictEscalationConflict,
    DurableConflictEscalationUnitOfWork,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
    validate_sqlite_durable_linked_aggregates_connection,
)

NOW = datetime(2026, 7, 22, tzinfo=UTC)
CAUSE_EVALUATED_AT = "2026-07-22T00:00:00+00:00"
WORKERS = 32

_LOCAL: threading.local = threading.local()


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


@dataclasses.dataclass(frozen=True)
class _Scenario:
    org_id: str
    request_id: str
    conflict_id: str
    manager_ref: str
    root_ref: str
    claims: tuple[ConflictOpenCandidateClaim, ...]
    command: DurableConflictEscalateCommand
    resource: ResourceRef
    canonical_command: dict[str, object]
    command_digest: str
    cause: DivergentVotes
    graph: ConflictEscalationRegistrySnapshot
    evidence: ConflictEscalationApprovalEvidence


def _scenario(key: str, *, manager: bool = True) -> _Scenario:
    org_id = _ref("org", f"org-{key}")
    request_id = _ref("request", f"request-{key}")
    conflict_id = _ref("conflict", f"case-{key}")
    manager_ref = _ref("subject", f"manager-{key}")
    root_ref = _ref("subject", f"root-{key}")
    claims = (
        ConflictOpenCandidateClaim(
            card_id=f"card-{key}",
            intent="refund",
            route=RouteTarget(intent="refund", agent_id=f"card-{key}", requires_approval=False),
        ),
    )
    command = DurableConflictEscalateCommand(conflict_id, request_id, 1, claims)
    resource = ResourceRef(org_id=org_id, kind="conflict_case", resource_id=conflict_id)
    canonical_command: dict[str, object] = {
        "conflict_id": conflict_id,
        "request_id": request_id,
        "expected_request_revision": 1,
    }
    command_digest = canonical_escalate_command_digest(resource=resource, command=canonical_command)
    cause = DivergentVotes(
        org_ref=org_id,
        conflict_ref=conflict_id,
        request_ref=request_id,
        awaiting_revision=0,
        concurrence_round=1,
        candidate_snapshot_sha256=_sha(f"snapshot-{key}"),
        candidate_owner_count=2,
        baseline_sha256=_sha(f"baseline-{key}"),
        candidate_claim_sha256=_sha(f"claim-{key}"),
        vote_set_sha256=_sha(f"votes-{key}"),
        evaluated_at=CAUSE_EVALUATED_AT,
    )
    candidate = ConflictEscalationSnapshotCandidate(
        ordinal=1,
        card_ref=_ref("card", f"card-{key}"),
        owner_subject_ref=_ref("subject", f"owner-{key}"),
        domain_ref=_ref("domain", "refund"),
        route_sha256=_sha(f"route-{key}"),
        under_claim=True,
    )
    path = ConflictEscalationOwnerPath(
        owner_subject_ref=_ref("subject", f"owner-{key}"),
        path_subject_refs=(manager_ref, root_ref) if manager else (root_ref,),
    )
    graph = ConflictEscalationRegistrySnapshot(
        org_id=org_id,
        candidates=(candidate,),
        owner_paths=(path,),
        manager_subject_ref=manager_ref if manager else None,
        root_subject_ref=root_ref,
        candidate_digest=_sha(f"candidate-digest-{key}"),
        claim_digest=cause.candidate_claim_sha256,
        graph_digest=_sha(f"graph-digest-{key}"),
    )
    evidence = ConflictEscalationApprovalEvidence(
        evidence_id=f"evidence-{key}",
        status="granted",
        action=ESCALATE_ACTION,
        command_digest=command_digest,
        resource_fingerprint=escalation_resource_fingerprint(resource),
        escalation_cause_digest=escalation_cause_digest(cause),
        graph_selection_digest=graph.graph_digest,
    )
    return _Scenario(
        org_id,
        request_id,
        conflict_id,
        manager_ref,
        root_ref,
        claims,
        command,
        resource,
        canonical_command,
        command_digest,
        cause,
        graph,
        evidence,
    )


def _principal(scenario: _Scenario, subject_id: str = "operator-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=scenario.org_id, subject_id=subject_id, identity_provider="idp", identity_session_id="s1"
    )


def _seed_scenarios(path: Path, scenarios: Sequence[_Scenario]) -> None:
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "seed-record",
        clock=lambda: NOW,
    )
    try:
        for scenario in scenarios:
            received = QuestionRequest.receive(
                org_id=scenario.org_id,
                requester_id="user",
                question="secret question",
                request_id_factory=lambda rid=scenario.request_id: rid,
                clock=lambda: NOW - timedelta(minutes=2),
                due_at=NOW + timedelta(hours=1),
            )
            completion.create(received)
            conflict_request = received.record_initial_routing(
                intent="refund",
                disposition="contested",
                target=AwaitingConflict(
                    case_id=scenario.conflict_id,
                    handling=HandlingAssignment(
                        kind="conflict_case", ref=scenario.conflict_id, due_at=NOW + timedelta(hours=1)
                    ),
                ),
                clock=lambda: NOW - timedelta(minutes=1),
            )
            assert completion.compare_and_set(scenario.request_id, 0, received, conflict_request)
            tx = completion.durable_transaction()
            with tx.scope():
                tx.begin_immediate()
                tx.execute(
                    "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,?,?,?,?)",
                    (
                        scenario.conflict_id,
                        scenario.org_id,
                        scenario.request_id,
                        0,
                        "open",
                        scenario.cause.candidate_snapshot_sha256,
                        NOW.isoformat(),
                    ),
                )
                tx.commit()
    finally:
        completion.close()


class _IdFactory:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            self._count += 1
            return f"{self._prefix}-{self._count}"


class _CauseReader:
    """cause0/cause1 재실행을 재현한다. 스레드-로컬 `cause_drift`가 참이면
    같은 스레드의 두 번째 호출(=prewrite cause1)만 다른 값을 반환한다."""

    def __init__(self, causes: dict[str, DivergentVotes]) -> None:
        self._causes = causes
        self.calls = 0
        self._lock = threading.Lock()

    def read_sealed(
        self, *, org_id: str, conflict_id: str, claims: tuple[ConflictOpenCandidateClaim, ...]
    ) -> DivergentVotes:
        with self._lock:
            self.calls += 1
        cause = self._causes[conflict_id]
        seen = getattr(_LOCAL, "cause_calls", 0) + 1
        setattr(_LOCAL, "cause_calls", seen)
        if getattr(_LOCAL, "cause_drift", False) and seen == 2:
            return dataclasses.replace(cause, vote_set_sha256=_sha(cause.vote_set_sha256 + "-drift"))
        return cause


class _GraphReader:
    def __init__(self, graphs: dict[str, ConflictEscalationRegistrySnapshot]) -> None:
        self._graphs = graphs
        self.snapshot_calls = 0
        self.verify_calls = 0
        self._lock = threading.Lock()

    def snapshot(
        self, *, org_id: str, claims: tuple[ConflictOpenCandidateClaim, ...]
    ) -> ConflictEscalationRegistrySnapshot:
        with self._lock:
            self.snapshot_calls += 1
        return self._graphs[org_id]

    def verify_current(
        self,
        snapshot: ConflictEscalationRegistrySnapshot,
        *,
        claims: tuple[ConflictOpenCandidateClaim, ...],
    ) -> None:
        with self._lock:
            self.verify_calls += 1
        if getattr(_LOCAL, "graph_drift", False):
            raise ConflictEscalationRegistrySnapshotError("graph drift")


class _Authority:
    def __init__(self) -> None:
        self.calls = 0
        self._lock = threading.Lock()

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
        with self._lock:
            self.calls += 1
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("operator",),
            policy_version="v1",
            policy_digest="0" * 64,
        )

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
    ) -> bool:
        assert action == "conflict.escalate"
        return not getattr(_LOCAL, "deny_auth", False)


class _ApprovalProvider:
    def __init__(self, evidences: dict[str, ConflictEscalationApprovalEvidence]) -> None:
        self._evidences = evidences
        self.calls = 0
        self._lock = threading.Lock()

    def acquire_escalate_approval(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef, command_digest: str
    ) -> ConflictEscalationApprovalEvidence:
        with self._lock:
            self.calls += 1
        return self._evidences[resource.resource_id]


class _ApprovalResolver:
    """acquire 재확인(1차) + prewrite reconfirm(2차)을 재현한다. 스레드-로컬
    `hitl_drift`가 참이면 같은 스레드의 두 번째 호출만 비일치(None)로 만든다."""

    def __init__(self, evidences: dict[str, ConflictEscalationApprovalEvidence]) -> None:
        self._by_id = {evidence.evidence_id: evidence for evidence in evidences.values()}
        self.calls = 0
        self._lock = threading.Lock()

    def resolve_escalation_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> ConflictEscalationApprovalEvidence | None:
        with self._lock:
            self.calls += 1
        seen = getattr(_LOCAL, "resolver_calls", 0) + 1
        setattr(_LOCAL, "resolver_calls", seen)
        if getattr(_LOCAL, "hitl_drift", False) and seen == 2:
            return None
        return self._by_id.get(evidence_id)


def _open_uow(
    path: Path,
    *,
    index: int,
    cause_reader: _CauseReader,
    graph_reader: _GraphReader,
    authority: _Authority,
    provider: _ApprovalProvider,
    resolver: _ApprovalResolver,
    receipt_ids: _IdFactory,
    manager_item_ids: _IdFactory,
    fault_injector: Callable[[str], None] | None = None,
) -> tuple[SqliteQuestionCompletionUnitOfWork, DurableConflictEscalationUnitOfWork]:
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: f"record-{index}",
        clock=lambda: NOW,
        timeout=10.0,
    )
    uow = DurableConflictEscalationUnitOfWork(
        completion=completion,
        cause_reader=cause_reader,
        graph_reader=graph_reader,
        central_authorizer=authority,
        approval_provider=provider,
        approval_resolver=resolver,
        clock=lambda: NOW,
        receipt_id_factory=receipt_ids,
        manager_item_id_factory=manager_item_ids,
        fault_injector=fault_injector,
    )
    return completion, uow


def _assert_single_commit(path: Path, scenario: _Scenario) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        for table in (
            "durable_conflict_escalation_receipts",
            "durable_conflict_escalation_evidence",
            "durable_conflict_escalation_result_projections",
        ):
            count = connection.execute(
                f"SELECT count(*) FROM {table} WHERE conflict_id=?", (scenario.conflict_id,)
            ).fetchone()[0]
            assert count == 1, f"{table}: expected 1 row for {scenario.conflict_id}, got {count}"
        for table in (
            "durable_conflict_escalation_audit_intents",
            "durable_conflict_escalation_outbox_intents",
        ):
            count = connection.execute(
                f"SELECT count(*) FROM {table} WHERE org_id=? AND command_digest=?",
                (scenario.org_id, scenario.command_digest),
            ).fetchone()[0]
            assert count == 1, f"{table}: expected 1 row for {scenario.command_digest}, got {count}"
        manager_items = connection.execute(
            "SELECT count(*) FROM durable_linked_manager_items WHERE request_id=?",
            (scenario.request_id,),
        ).fetchone()[0]
        assert manager_items == 1
        case = connection.execute(
            "SELECT status FROM durable_linked_conflict_cases WHERE conflict_id=?",
            (scenario.conflict_id,),
        ).fetchone()
        assert case is not None and case["status"] == "escalated"
        request = connection.execute(
            "SELECT revision, state_json FROM question_requests WHERE request_id=?",
            (scenario.request_id,),
        ).fetchone()
        assert request is not None and request["revision"] == 2
        state = json.loads(request["state_json"])
        assert state.get("kind") == "awaiting_manager"
    finally:
        connection.close()


def _assert_open_no_writes(path: Path, scenario: _Scenario) -> None:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        for table in (
            "durable_conflict_escalation_receipts",
            "durable_conflict_escalation_evidence",
            "durable_conflict_escalation_result_projections",
        ):
            count = connection.execute(
                f"SELECT count(*) FROM {table} WHERE conflict_id=?", (scenario.conflict_id,)
            ).fetchone()[0]
            assert count == 0, f"{table}: expected 0 rows for {scenario.conflict_id}, got {count}"
        case = connection.execute(
            "SELECT status, awaiting_revision FROM durable_linked_conflict_cases WHERE conflict_id=?",
            (scenario.conflict_id,),
        ).fetchone()
        assert case is not None and case["status"] == "open" and case["awaiting_revision"] == 0
        request = connection.execute(
            "SELECT revision FROM question_requests WHERE request_id=?", (scenario.request_id,)
        ).fetchone()
        assert request is not None and request["revision"] == 1
    finally:
        connection.close()


def _assert_schema_capable(path: Path, scenario: _Scenario) -> None:
    receipts_connection = open_sqlite_durable_conflict_escalation_receipts_connection(
        path, org_id=scenario.org_id
    )
    try:
        validate_sqlite_durable_conflict_escalation_receipts_connection(
            receipts_connection, org_id=scenario.org_id
        )
    finally:
        receipts_connection.close()
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "verify-record",
        clock=lambda: NOW,
    )
    try:
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.validate_component_in_transaction(
                lambda connection: validate_sqlite_durable_linked_aggregates_connection(
                    connection, org_id=scenario.org_id
                )
            )
            tx.validate_component_in_transaction(validate_sqlite_completion_connection)
            tx.commit()
    finally:
        completion.close()


def test_32_way_동일_command_경합은_정확히_1_fresh_write와_31_replay로_수렴한다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same-command.sqlite"
    scenario = _scenario("same-command")
    _seed_scenarios(path, [scenario])
    cause_reader = _CauseReader({scenario.conflict_id: scenario.cause})
    graph_reader = _GraphReader({scenario.org_id: scenario.graph})
    authority = _Authority()
    provider = _ApprovalProvider({scenario.conflict_id: scenario.evidence})
    resolver = _ApprovalResolver({scenario.conflict_id: scenario.evidence})
    receipt_ids = _IdFactory("receipt")
    manager_item_ids = _IdFactory("manager-item")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableConflictEscalationUnitOfWork] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path,
            index=index,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
            receipt_ids=receipt_ids,
            manager_item_ids=manager_item_ids,
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(WORKERS)
    principal = _principal(scenario)

    def run(uow: DurableConflictEscalationUnitOfWork) -> object:
        barrier.wait()
        return uow.escalate(principal=principal, command=scenario.command)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, uows))
    finally:
        for completion in completions:
            completion.close()

    assert len(results) == WORKERS
    assert len(set(results)) == 1
    assert provider.calls == 1
    assert authority.calls == 2
    assert cause_reader.calls == 2
    assert graph_reader.snapshot_calls == 1
    assert graph_reader.verify_calls == 1
    assert resolver.calls == 2
    _assert_single_commit(path, scenario)
    _assert_schema_capable(path, scenario)


def _run_drift_arm(
    tmp_path: Path,
    *,
    key: str,
    flag: str,
    drift_indices: frozenset[int],
    expected_message_fragment: str,
    workers: int = WORKERS,
) -> None:
    """모든 `drift_indices` 스레드가 같은 drift를 공유하므로, 어느 순서로
    write-lock을 먼저 얻든 스스로의 drift로 결정론적으로 실패한다(replay로
    새어나가 drift를 우회할 가능성이 없다). Phase A에서 이 스레드들만
    서로 경쟁시켜 전원 Conflict·write 0을 증명한 뒤, Phase B에서 나머지
    정상 스레드로 실제 winner 경쟁을 검증한다 — 두 단계 모두 barrier
    동시-릴리즈이므로 실 다중 커넥션 경합이고, 단언은 순서에 무관하다."""
    path = tmp_path / f"drift-{key}.sqlite"
    scenario = _scenario(key)
    _seed_scenarios(path, [scenario])
    cause_reader = _CauseReader({scenario.conflict_id: scenario.cause})
    graph_reader = _GraphReader({scenario.org_id: scenario.graph})
    authority = _Authority()
    provider = _ApprovalProvider({scenario.conflict_id: scenario.evidence})
    resolver = _ApprovalResolver({scenario.conflict_id: scenario.evidence})
    receipt_ids = _IdFactory("receipt")
    manager_item_ids = _IdFactory("manager-item")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableConflictEscalationUnitOfWork] = []
    for index in range(workers):
        completion, uow = _open_uow(
            path,
            index=index,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
            receipt_ids=receipt_ids,
            manager_item_ids=manager_item_ids,
        )
        completions.append(completion)
        uows.append(uow)
    principal = _principal(scenario)
    drift_order = sorted(drift_indices)
    normal_order = [index for index in range(workers) if index not in drift_indices]
    drift_barrier = threading.Barrier(len(drift_order))
    race_barrier = threading.Barrier(len(normal_order))

    def run_drift(index: int) -> object:
        setattr(_LOCAL, flag, True)
        drift_barrier.wait()
        try:
            return uows[index].escalate(principal=principal, command=scenario.command)
        except DurableConflictEscalationConflict as error:
            return error
        finally:
            setattr(_LOCAL, flag, False)

    def run_normal(index: int) -> object:
        race_barrier.wait()
        return uows[index].escalate(principal=principal, command=scenario.command)

    try:
        with ThreadPoolExecutor(max_workers=len(drift_order)) as pool:
            drift_results = list(pool.map(run_drift, drift_order))
        for result in drift_results:
            assert isinstance(result, DurableConflictEscalationConflict)
            assert expected_message_fragment in str(result)
        _assert_open_no_writes(path, scenario)

        with ThreadPoolExecutor(max_workers=len(normal_order)) as pool:
            race_results = list(pool.map(run_normal, normal_order))
    finally:
        for completion in completions:
            completion.close()

    assert len(set(race_results)) == 1
    _assert_single_commit(path, scenario)


_DRIFT_ARM_INDICES: frozenset[int] = frozenset({0, 1, 2, 3})


def test_권한_drift_스레드는_경쟁_중_Conflict_write_0이고_winner_커밋을_오염시키지_않는다(
    tmp_path: Path,
) -> None:
    _run_drift_arm(
        tmp_path,
        key="auth-drift",
        flag="deny_auth",
        drift_indices=_DRIFT_ARM_INDICES,
        expected_message_fragment="권한이 거부",
    )


def test_graph_drift_스레드는_경쟁_중_Conflict_write_0이고_winner_커밋을_오염시키지_않는다(
    tmp_path: Path,
) -> None:
    _run_drift_arm(
        tmp_path,
        key="graph-drift",
        flag="graph_drift",
        drift_indices=_DRIFT_ARM_INDICES,
        expected_message_fragment="Registry graph가 바뀌었습니다",
    )


def test_HITL_drift_스레드는_경쟁_중_Conflict_write_0이고_winner_커밋을_오염시키지_않는다(
    tmp_path: Path,
) -> None:
    _run_drift_arm(
        tmp_path,
        key="hitl-drift",
        flag="hitl_drift",
        drift_indices=_DRIFT_ARM_INDICES,
        expected_message_fragment="사람 승인 재확인에 실패",
    )


def test_cause_drift_스레드는_경쟁_중_Conflict_write_0이고_winner_커밋을_오염시키지_않는다(
    tmp_path: Path,
) -> None:
    # 스펙의 "ownership drift"는 별도 arm이 아니라 graph arm(verify_current의
    # owner_paths)과 이 cause arm(candidate_owner_count)으로 검출·커버된다.
    _run_drift_arm(
        tmp_path,
        key="cause-drift",
        flag="cause_drift",
        drift_indices=_DRIFT_ARM_INDICES,
        expected_message_fragment="원인이 취득 이후 바뀌었습니다",
    )


_FAULT_POINTS = (
    "after_receipt",
    "after_evidence",
    "after_result_projection",
    "after_audit_intent",
    "after_outbox_intent",
    "after_manager_item",
    "after_case_escalated",
    "after_request_awaiting_manager",
)


@pytest.mark.parametrize("point", _FAULT_POINTS)
def test_write_fault_지점을_경쟁_중_주입해도_partial_write_0이고_다른_스레드가_정상_winner가_된다(
    tmp_path: Path, point: str
) -> None:
    """fault 스레드가 SQLite write-lock을 쥔 채(`BEGIN IMMEDIATE` 이후) 주입
    지점에서 일시 정지한 동안 정상 스레드들을 제출한 뒤 rollback을 트리거한다.
    정상 스레드가 lock 큐에 실제 도달했는지는 스케줄링에 달려 있어 강제하지
    않는다 — 검증하는 불변식(부분 쓰기 커밋 시 정상 스레드의 REPLAY 재구성이
    결손 행으로 실패)은 타이밍과 무관하게 성립한다."""
    workers = 8
    fault_index = 0
    path = tmp_path / f"fault-{point}.sqlite"
    scenario = _scenario(f"fault-{point}")
    _seed_scenarios(path, [scenario])
    cause_reader = _CauseReader({scenario.conflict_id: scenario.cause})
    graph_reader = _GraphReader({scenario.org_id: scenario.graph})
    authority = _Authority()
    provider = _ApprovalProvider({scenario.conflict_id: scenario.evidence})
    resolver = _ApprovalResolver({scenario.conflict_id: scenario.evidence})
    receipt_ids = _IdFactory("receipt")
    manager_item_ids = _IdFactory("manager-item")
    paused = threading.Event()
    release = threading.Event()

    def pause_then_raise(injected: str) -> None:
        if injected == point:
            paused.set()
            assert release.wait(timeout=5)
            raise RuntimeError(injected)

    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableConflictEscalationUnitOfWork] = []
    for index in range(workers):
        completion, uow = _open_uow(
            path,
            index=index,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
            receipt_ids=receipt_ids,
            manager_item_ids=manager_item_ids,
            fault_injector=pause_then_raise if index == fault_index else None,
        )
        completions.append(completion)
        uows.append(uow)
    principal = _principal(scenario)

    def run_fault() -> object:
        try:
            return uows[fault_index].escalate(principal=principal, command=scenario.command)
        except RuntimeError as error:
            return error

    def run_normal(index: int) -> object:
        return uows[index].escalate(principal=principal, command=scenario.command)

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            fault_future = pool.submit(run_fault)
            assert paused.wait(timeout=5)
            normal_futures = [
                pool.submit(run_normal, index) for index in range(workers) if index != fault_index
            ]
            release.set()
            fault_result = fault_future.result(timeout=5)
            normal_results = [future.result(timeout=5) for future in normal_futures]
    finally:
        for completion in completions:
            completion.close()

    assert type(fault_result) is RuntimeError and str(fault_result) == point
    assert len(set(normal_results)) == 1
    _assert_single_commit(path, scenario)
    _assert_schema_capable(path, scenario)


def test_32_way_다른_Case_병행_escalate는_전부_성공하고_교차_오염이_없다(tmp_path: Path) -> None:
    path = tmp_path / "different-cases.sqlite"
    scenarios = [_scenario(f"case-{index}", manager=index % 2 == 0) for index in range(WORKERS)]
    _seed_scenarios(path, scenarios)
    causes = {scenario.conflict_id: scenario.cause for scenario in scenarios}
    graphs = {scenario.org_id: scenario.graph for scenario in scenarios}
    evidences = {scenario.conflict_id: scenario.evidence for scenario in scenarios}
    cause_reader = _CauseReader(causes)
    graph_reader = _GraphReader(graphs)
    authority = _Authority()
    provider = _ApprovalProvider(evidences)
    resolver = _ApprovalResolver(evidences)
    receipt_ids = _IdFactory("receipt")
    manager_item_ids = _IdFactory("manager-item")
    completions: list[SqliteQuestionCompletionUnitOfWork] = []
    uows: list[DurableConflictEscalationUnitOfWork] = []
    for index in range(WORKERS):
        completion, uow = _open_uow(
            path,
            index=index,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
            receipt_ids=receipt_ids,
            manager_item_ids=manager_item_ids,
        )
        completions.append(completion)
        uows.append(uow)
    barrier = threading.Barrier(WORKERS)

    def run(item: tuple[_Scenario, DurableConflictEscalationUnitOfWork]) -> object:
        scenario, uow = item
        barrier.wait()
        return uow.escalate(principal=_principal(scenario), command=scenario.command)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            results = list(pool.map(run, zip(scenarios, uows, strict=True)))
    finally:
        for completion in completions:
            completion.close()

    assert len(results) == WORKERS
    typed_results: list[DurableConflictEscalatedToManager | DurableConflictEscalatedToRoot] = []
    for scenario, result in zip(scenarios, results, strict=True):
        assert isinstance(result, DurableConflictEscalatedToManager | DurableConflictEscalatedToRoot)
        assert result.conflict_id == scenario.conflict_id
        assert result.request_id == scenario.request_id
        _assert_single_commit(path, scenario)
        typed_results.append(result)
    assert provider.calls == WORKERS
    assert authority.calls == WORKERS * 2
    receipt_ids_seen = {result.receipt_id for result in typed_results}
    assert len(receipt_ids_seen) == WORKERS
    manager_item_ids_seen = {result.manager_item_id for result in typed_results}
    assert len(manager_item_ids_seen) == WORKERS
