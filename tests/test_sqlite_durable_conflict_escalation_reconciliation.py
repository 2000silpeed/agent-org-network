from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
import sqlite3
from dataclasses import dataclass
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
    ConflictEscalationSnapshotCandidate,
)
from agent_org_network.conflict_open_contract import ConflictOpenCandidateClaim
from agent_org_network.durable_conflict_escalation_evidence import DivergentVotes
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingManager,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    migrate_sqlite_durable_conflict_escalation_receipts_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_reconciliation import (
    DurableConflictEscalationReconciliationReport,
    reconcile_sqlite_durable_conflict_escalation_gate,
)
from agent_org_network.sqlite_durable_conflict_escalation_uow import (
    DurableConflictEscalateCommand,
    DurableConflictEscalationResult,
    DurableConflictEscalationUnitOfWork,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)

NOW = datetime(2026, 7, 22, tzinfo=UTC)
CAUSE_EVALUATED_AT = "2026-07-22T00:00:00+00:00"
SNAPSHOT_SHA = "a" * 64
BASELINE_SHA = "b" * 64
CLAIM_SHA = "c" * 64
VOTE_SHA = "d" * 64
CANDIDATE_DIGEST = "e" * 64
MANAGER_GRAPH_DIGEST = "f" * 64
ROOT_GRAPH_DIGEST = "9" * 64


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


MANAGER_REF = _ref("subject", "manager-1")
ROOT_REF = _ref("subject", "root-1")


def _graph(*, manager: bool) -> ConflictEscalationRegistrySnapshot:
    manager_ref = MANAGER_REF if manager else None
    candidate = ConflictEscalationSnapshotCandidate(
        ordinal=1,
        card_ref=_ref("card", "card-a"),
        owner_subject_ref=_ref("subject", "owner-a"),
        domain_ref=_ref("domain", "refund"),
        route_sha256="1" * 64,
        under_claim=True,
    )
    path = ConflictEscalationOwnerPath(
        owner_subject_ref=_ref("subject", "owner-a"),
        path_subject_refs=(MANAGER_REF, ROOT_REF) if manager else (ROOT_REF,),
    )
    return ConflictEscalationRegistrySnapshot(
        org_id=_ref("org", "graph-org"),
        candidates=(candidate,),
        owner_paths=(path,),
        manager_subject_ref=manager_ref,
        root_subject_ref=ROOT_REF,
        candidate_digest=CANDIDATE_DIGEST,
        claim_digest=CLAIM_SHA,
        graph_digest=MANAGER_GRAPH_DIGEST if manager else ROOT_GRAPH_DIGEST,
    )


GRAPH_MANAGER = _graph(manager=True)
GRAPH_ROOT = _graph(manager=False)


class _CauseReader:
    def __init__(self, cause: DivergentVotes) -> None:
        self._cause = cause

    def read_sealed(self, *, org_id: str, conflict_id: str, claims: object) -> DivergentVotes:
        return self._cause


class _GraphReader:
    def __init__(self, snapshot: ConflictEscalationRegistrySnapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self, *, org_id: str, claims: object) -> ConflictEscalationRegistrySnapshot:
        return self._snapshot

    def verify_current(self, snapshot: ConflictEscalationRegistrySnapshot, *, claims: object) -> None:
        return None


class _Authority:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
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
        return True


class _ApprovalProvider:
    def __init__(self, evidence: ConflictEscalationApprovalEvidence) -> None:
        self._evidence = evidence

    def acquire_escalate_approval(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef, command_digest: str
    ) -> ConflictEscalationApprovalEvidence:
        return self._evidence


class _ApprovalResolver:
    def __init__(self, evidence: ConflictEscalationApprovalEvidence) -> None:
        self._evidence = evidence

    def resolve_escalation_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> ConflictEscalationApprovalEvidence | None:
        return self._evidence


@dataclass(frozen=True)
class _OrgKit:
    label: str
    org_id: str
    request_id: str
    conflict_id: str
    command: DurableConflictEscalateCommand
    resource: ResourceRef
    command_digest: str
    cause: DivergentVotes


def _kit(label: str) -> _OrgKit:
    org_id = _ref("org", f"org-{label}")
    request_id = _ref("request", f"request-{label}")
    conflict_id = _ref("conflict", f"case-{label}")
    claims = (
        ConflictOpenCandidateClaim(
            card_id=f"card-{label}",
            intent="refund",
            route=RouteTarget(intent="refund", agent_id=f"card-{label}", requires_approval=False),
        ),
    )
    command = DurableConflictEscalateCommand(conflict_id, request_id, 1, claims)
    resource = ResourceRef(org_id=org_id, kind="conflict_case", resource_id=conflict_id)
    canonical_command = {
        "conflict_id": conflict_id,
        "request_id": request_id,
        "expected_request_revision": 1,
    }
    command_digest = canonical_escalate_command_digest(resource=resource, command=canonical_command)
    # c.2 reconcile은 evidence.org_id/conflict_id/request_id(=principal/command
    # 값)로 escalation_cause_digest를 재계산하므로, cause의 org_ref/conflict_ref/
    # request_ref는 반드시 이 kit의 org_id/conflict_id/request_id와 같아야 한다.
    cause = DivergentVotes(
        org_ref=org_id,
        conflict_ref=conflict_id,
        request_ref=request_id,
        awaiting_revision=0,
        concurrence_round=1,
        candidate_snapshot_sha256=SNAPSHOT_SHA,
        candidate_owner_count=2,
        baseline_sha256=BASELINE_SHA,
        candidate_claim_sha256=CLAIM_SHA,
        vote_set_sha256=VOTE_SHA,
        evaluated_at=CAUSE_EVALUATED_AT,
    )
    return _OrgKit(label, org_id, request_id, conflict_id, command, resource, command_digest, cause)


def _evidence(kit: _OrgKit, *, graph: ConflictEscalationRegistrySnapshot) -> ConflictEscalationApprovalEvidence:
    return ConflictEscalationApprovalEvidence(
        evidence_id=f"evidence-{kit.label}",
        status="granted",
        action=ESCALATE_ACTION,
        command_digest=kit.command_digest,
        resource_fingerprint=escalation_resource_fingerprint(kit.resource),
        escalation_cause_digest=escalation_cause_digest(kit.cause),
        graph_selection_digest=graph.graph_digest,
    )


def _prepare_db(tmp_path: Path, name: str = "gate.sqlite") -> tuple[Path, SqliteQuestionCompletionUnitOfWork]:
    path = tmp_path / name
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_conflict_escalation_baseline_schema(path)
    migrate_sqlite_durable_conflict_escalation_receipts_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )
    return path, completion


def _seed_open_case(completion: SqliteQuestionCompletionUnitOfWork, kit: _OrgKit) -> None:
    received = QuestionRequest.receive(
        org_id=kit.org_id,
        requester_id="user",
        question="secret question",
        request_id_factory=lambda: kit.request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    conflict_request = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id=kit.conflict_id,
            handling=HandlingAssignment(
                kind="conflict_case", ref=kit.conflict_id, due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(kit.request_id, 0, received, conflict_request)
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,?,?,?,?)",
            (kit.conflict_id, kit.org_id, kit.request_id, 0, "open", SNAPSHOT_SHA, NOW.isoformat()),
        )
        tx.commit()


def _escalate(
    completion: SqliteQuestionCompletionUnitOfWork, kit: _OrgKit, *, manager: bool = True
) -> DurableConflictEscalationResult:
    _seed_open_case(completion, kit)
    graph = GRAPH_MANAGER if manager else GRAPH_ROOT
    evidence = _evidence(kit, graph=graph)
    uow = DurableConflictEscalationUnitOfWork(
        completion=completion,
        cause_reader=_CauseReader(kit.cause),
        graph_reader=_GraphReader(graph),
        central_authorizer=_Authority(),
        approval_provider=_ApprovalProvider(evidence),
        approval_resolver=_ApprovalResolver(evidence),
        clock=lambda: NOW,
        receipt_id_factory=lambda: f"receipt-{kit.label}",
        manager_item_id_factory=lambda: f"manager-item-{kit.label}",
    )
    principal = AuthenticatedPrincipal(
        org_id=kit.org_id, subject_id="operator-1", identity_provider="idp", identity_session_id="s1"
    )
    return uow.escalate(principal=principal, command=kit.command)


def _raw_execute(completion: SqliteQuestionCompletionUnitOfWork, sql: str, params: tuple[object, ...]) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(sql, params)
        tx.commit()


def _dispose_manager_item(
    completion: SqliteQuestionCompletionUnitOfWork,
    kit: _OrgKit,
    result: DurableConflictEscalationResult,
    *,
    status: str,
    advance_request: bool,
) -> None:
    _raw_execute(
        completion,
        "UPDATE durable_linked_manager_items SET status=? WHERE manager_item_id=?",
        (status, result.manager_item_id),
    )
    if advance_request:
        request = completion.get(kit.request_id)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
        declined = request.transition(DeclinedRequest(reason_code="manager_disposed"), clock=lambda: NOW)
        assert completion.compare_and_set(kit.request_id, request.revision, request, declined)


def test_green_baseline_escalate_커밋한_db는_capable하고_violation이_없다(tmp_path: Path) -> None:
    path, completion = _prepare_db(tmp_path)
    try:
        _escalate(completion, _kit("a"), manager=True)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path)
    assert isinstance(report, DurableConflictEscalationReconciliationReport)
    assert report.capable is True
    assert report.detail == "capable_v1"
    assert report.escalation_receipts_manifest_present is True
    assert report.violations == ()


@pytest.mark.parametrize("tampered_status", ["open", "resolved"])
def test_escalated_아닌_Case에_receipt가_있으면_손상으로_잡는다(
    tmp_path: Path, tampered_status: str
) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        _escalate(completion, kit, manager=True)
        _raw_execute(
            completion,
            "UPDATE durable_linked_conflict_cases SET status=? WHERE conflict_id=?",
            (tampered_status, kit.conflict_id),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is False
    assert any(v.kind == "receipt_without_escalated_case" for v in report.violations)


def test_escalated_Case에_receipt가_없으면_미아로_잡는다(tmp_path: Path) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        _seed_open_case(completion, kit)
        _raw_execute(
            completion,
            "UPDATE durable_linked_conflict_cases SET status='escalated' WHERE conflict_id=?",
            (kit.conflict_id,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is False
    assert any(v.kind == "escalated_case_without_receipt" for v in report.violations)


@pytest.mark.parametrize(
    "tamper", ["delete", "source_ref", "awaiting_revision", "manager_subject_id"]
)
def test_ManagerItem이_결손되거나_어긋나면_손상으로_잡는다(tmp_path: Path, tamper: str) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        result = _escalate(completion, kit, manager=True)
        if tamper == "delete":
            _raw_execute(
                completion,
                "DELETE FROM durable_linked_manager_items WHERE manager_item_id=?",
                (result.manager_item_id,),
            )
        elif tamper == "source_ref":
            _raw_execute(
                completion,
                "UPDATE durable_linked_manager_items SET source_ref=? WHERE manager_item_id=?",
                (_ref("source", "wrong-conflict"), result.manager_item_id),
            )
        elif tamper == "manager_subject_id":
            # projection.target_subject_ref와의 결박(A2)을 죽이는 변조 — sealed 선택 밖 대상.
            _raw_execute(
                completion,
                "UPDATE durable_linked_manager_items SET manager_subject_id=? WHERE manager_item_id=?",
                (_ref("subject", "impostor"), result.manager_item_id),
            )
        else:
            _raw_execute(
                completion,
                "UPDATE durable_linked_manager_items SET awaiting_revision=99 WHERE manager_item_id=?",
                (result.manager_item_id,),
            )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is False
    assert any(v.kind == "manager_item_missing_or_mismatched" for v in report.violations)


def test_미처분인데_Request의_item_id_결박이_어긋나면_손상으로_잡는다(tmp_path: Path) -> None:
    # open 분기 resting shape의 item_id 절(A3)을 fire — ManagerItem PK만 바꾸면
    # request_id 조회·A2(subject/source_ref/awaiting_revision)는 그대로 통과하고
    # AwaitingManager.item_id ↔ manager_item_id 결박만 어긋난다.
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        result = _escalate(completion, kit, manager=True)
        _raw_execute(
            completion,
            "UPDATE durable_linked_manager_items SET manager_item_id=? WHERE manager_item_id=?",
            (_ref("manager", "drifted-item"), result.manager_item_id),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


def test_미처분_ManagerItem인데_Request가_진행하면_손상으로_잡는다(tmp_path: Path) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        _escalate(completion, kit, manager=True)
        # S4.4 없이 ManagerItem은 open으로 남아 있는데 Request만 corruption으로
        # 앞서갔다고 시뮬레이션한다(정상 경로로는 도달할 수 없는 상태).
        _raw_execute(
            completion,
            "UPDATE question_requests SET revision = revision + 1 WHERE request_id=?",
            (kit.request_id,),
        )
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is False
    assert any(v.kind == "request_state_inconsistent" for v in report.violations)


@pytest.mark.parametrize("status", ["resolved", "dismissed"])
def test_ManagerItem이_처분되면_Request_진행은_오탐이_아니다(tmp_path: Path, status: str) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        result = _escalate(completion, kit, manager=True)
        _dispose_manager_item(completion, kit, result, status=status, advance_request=True)
    finally:
        completion.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is True
    assert report.violations == ()


def test_ManagerItem이_처분됐지만_Request가_아직_AwaitingManager면_결박은_여전히_요구된다(
    tmp_path: Path,
) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        result = _escalate(completion, kit, manager=True)
        _dispose_manager_item(completion, kit, result, status="resolved", advance_request=False)
    finally:
        completion.close()

    # Request가 아직 AwaitingManager고 원래 escalation 결박(item_id/contested/route/
    # attempt/handling)을 그대로 유지하므로 손상이 아니어야 한다.
    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is True
    assert report.violations == ()


def test_c2_manifest가_없으면_capability_uncertain으로_row_열거_0으로_닫는다(
    tmp_path: Path,
) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        _escalate(completion, kit, manager=True)
    finally:
        completion.close()

    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "DELETE FROM schema_component_manifests WHERE component_id='durable_conflict_escalation_receipts_v1'"
        )
        connection.commit()
    finally:
        connection.close()

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is False
    assert len(report.violations) == 1
    assert report.violations[0].kind == "escalation_capability_uncertain"


def test_타org_손상은_보이지_않지만_전역_catalog_손상은_org_무관하게_닫힌다(
    tmp_path: Path,
) -> None:
    path, completion = _prepare_db(tmp_path)
    kit_a = _kit("a")
    kit_b = _kit("b")
    try:
        _escalate(completion, kit_a, manager=True)
        _escalate(completion, kit_b, manager=False)
        _raw_execute(
            completion,
            "UPDATE durable_linked_conflict_cases SET status='open' WHERE conflict_id=?",
            (kit_b.conflict_id,),
        )
    finally:
        completion.close()

    scoped_to_a = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit_a.org_id)
    assert scoped_to_a.capable is True
    assert scoped_to_a.violations == ()

    scoped_to_b = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit_b.org_id)
    assert scoped_to_b.capable is False
    assert any(v.kind == "receipt_without_escalated_case" for v in scoped_to_b.violations)

    connection = sqlite3.connect(str(path))
    try:
        connection.execute(
            "DELETE FROM schema_component_manifests WHERE component_id='durable_conflict_escalation_receipts_v1'"
        )
        connection.commit()
    finally:
        connection.close()

    globally_closed = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit_a.org_id)
    assert globally_closed.capable is False
    assert globally_closed.violations[0].kind == "escalation_capability_uncertain"


def test_전_sweep은_한_read_transaction으로_묶인다(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path, completion = _prepare_db(tmp_path)
    kit = _kit("a")
    try:
        _escalate(completion, kit, manager=True)
    finally:
        completion.close()

    statements: list[str] = []
    real_connect = sqlite3.connect

    def _trace(sql: str) -> None:
        statements.append(sql.strip().split()[0].upper())

    def _spy_connect(database: str, *, uri: bool = False, timeout: float = 5.0) -> sqlite3.Connection:
        connection = real_connect(database, uri=uri, timeout=timeout)
        if uri and "mode=ro" in database:
            connection.set_trace_callback(_trace)
        return connection

    monkeypatch.setattr(
        "agent_org_network.sqlite_durable_conflict_escalation_reconciliation.sqlite3.connect",
        _spy_connect,
    )

    report = reconcile_sqlite_durable_conflict_escalation_gate(path, org_id=kit.org_id)
    assert report.capable is True

    begin_indices = [i for i, s in enumerate(statements) if s == "BEGIN"]
    commit_indices = [i for i, s in enumerate(statements) if s == "COMMIT"]
    assert len(begin_indices) == 1
    assert len(commit_indices) == 1
    begin_at, commit_at = begin_indices[0], commit_indices[0]
    assert begin_at < commit_at
    assert commit_at == len(statements) - 1
    writes = {"INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "ALTER"}
    assert not writes.intersection(statements[begin_at + 1 : commit_at])
