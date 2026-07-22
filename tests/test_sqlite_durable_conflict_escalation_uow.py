from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import (
    SqliteCompletionTransaction,
    SqliteQuestionCompletionUnitOfWork,
)
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
from agent_org_network.durable_conflict_escalation_evidence import DivergentVotes, Pending
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingManager,
    HandlingAssignment,
    QuestionRequest,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_conflict_escalation_baseline import (
    migrate_sqlite_durable_conflict_escalation_baseline_schema,
)
from agent_org_network.sqlite_durable_conflict_escalation_receipts import (
    open_sqlite_durable_conflict_escalation_receipts_connection,
    migrate_sqlite_durable_conflict_escalation_receipts_schema,
    validate_sqlite_durable_conflict_escalation_receipts_connection,
)
from agent_org_network.sqlite_durable_conflict_escalation_uow import (
    DurableConflictEscalateCommand,
    DurableConflictEscalatedToManager,
    DurableConflictEscalatedToRoot,
    DurableConflictEscalationConflict,
    DurableConflictEscalationUnavailable,
    DurableConflictEscalationUnitOfWork,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
    validate_sqlite_durable_linked_aggregates_connection,
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


ORG_ID = _ref("org", "org-1")
REQUEST_ID = _ref("request", "request-1")
CONFLICT_ID = _ref("conflict", "case-1")
MANAGER_REF = _ref("subject", "manager-1")
ROOT_REF = _ref("subject", "root-1")
CLAIMS = (
    ConflictOpenCandidateClaim(
        card_id="card-a",
        intent="refund",
        route=RouteTarget(intent="refund", agent_id="card-a", requires_approval=False),
    ),
)
COMMAND = DurableConflictEscalateCommand(CONFLICT_ID, REQUEST_ID, 1, CLAIMS)
RESOURCE = ResourceRef(org_id=ORG_ID, kind="conflict_case", resource_id=CONFLICT_ID)
CANONICAL_COMMAND = {
    "conflict_id": CONFLICT_ID,
    "request_id": REQUEST_ID,
    "expected_request_revision": 1,
}
COMMAND_DIGEST = canonical_escalate_command_digest(resource=RESOURCE, command=CANONICAL_COMMAND)


def _cause(*, vote_set_sha256: str = VOTE_SHA) -> DivergentVotes:
    return DivergentVotes(
        org_ref=ORG_ID,
        conflict_ref=CONFLICT_ID,
        request_ref=REQUEST_ID,
        awaiting_revision=0,
        concurrence_round=1,
        candidate_snapshot_sha256=SNAPSHOT_SHA,
        candidate_owner_count=2,
        baseline_sha256=BASELINE_SHA,
        candidate_claim_sha256=CLAIM_SHA,
        vote_set_sha256=vote_set_sha256,
        evaluated_at=CAUSE_EVALUATED_AT,
    )


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
        org_id=ORG_ID,
        candidates=(candidate,),
        owner_paths=(path,),
        manager_subject_ref=manager_ref,
        root_subject_ref=ROOT_REF,
        candidate_digest=CANDIDATE_DIGEST,
        claim_digest=CLAIM_SHA,
        graph_digest=MANAGER_GRAPH_DIGEST if manager else ROOT_GRAPH_DIGEST,
    )


def _evidence(
    *, cause: DivergentVotes, graph: ConflictEscalationRegistrySnapshot, evidence_id: str = "evidence-1"
) -> ConflictEscalationApprovalEvidence:
    return ConflictEscalationApprovalEvidence(
        evidence_id=evidence_id,
        status="granted",
        action=ESCALATE_ACTION,
        command_digest=COMMAND_DIGEST,
        resource_fingerprint=escalation_resource_fingerprint(RESOURCE),
        escalation_cause_digest=escalation_cause_digest(cause),
        graph_selection_digest=graph.graph_digest,
    )


class _CauseReader:
    def __init__(self, cause: object, *, second: object | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self._cause = cause
        self._second = second
        self._error = error

    def read_sealed(self, *, org_id: str, conflict_id: str, claims: object) -> object:
        self.calls += 1
        if self._error is not None and self.calls == 1:
            raise self._error
        if self.calls >= 2 and self._second is not None:
            return self._second
        return self._cause


class _GraphReader:
    def __init__(
        self, snapshot: ConflictEscalationRegistrySnapshot, *, verify_error: Exception | None = None
    ) -> None:
        self.snapshot_calls = 0
        self.verify_calls = 0
        self._snapshot = snapshot
        self._verify_error = verify_error

    def snapshot(self, *, org_id: str, claims: object) -> ConflictEscalationRegistrySnapshot:
        self.snapshot_calls += 1
        return self._snapshot

    def verify_current(self, snapshot: ConflictEscalationRegistrySnapshot, *, claims: object) -> None:
        self.verify_calls += 1
        if self._verify_error is not None:
            raise self._verify_error


class _Authority:
    def __init__(self, *, deny_at_call: int | None = None) -> None:
        self.calls = 0
        self._deny_at_call = deny_at_call

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
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
        return self._deny_at_call != self.calls


class _ApprovalProvider:
    def __init__(self, evidence: ConflictEscalationApprovalEvidence) -> None:
        self.calls = 0
        self._evidence = evidence

    def acquire_escalate_approval(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef, command_digest: str
    ) -> ConflictEscalationApprovalEvidence:
        self.calls += 1
        return self._evidence


class _ApprovalResolver:
    def __init__(
        self, evidence: ConflictEscalationApprovalEvidence, *, invalid_after: int | None = None
    ) -> None:
        self.calls = 0
        self._evidence = evidence
        self._invalid_after = invalid_after

    def resolve_escalation_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> ConflictEscalationApprovalEvidence | None:
        self.calls += 1
        if self._invalid_after is not None and self.calls >= self._invalid_after:
            return None
        return self._evidence


def _principal(subject_id: str = "operator-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=ORG_ID, subject_id=subject_id, identity_provider="idp", identity_session_id="s1"
    )


def _prepared(tmp_path: Path) -> tuple[SqliteQuestionCompletionUnitOfWork, QuestionRequest]:
    path = tmp_path / "workflow.sqlite"
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
    received = QuestionRequest.receive(
        org_id=ORG_ID,
        requester_id="user",
        question="secret question",
        request_id_factory=lambda: REQUEST_ID,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    conflict_request = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id=CONFLICT_ID,
            handling=HandlingAssignment(
                kind="conflict_case", ref=CONFLICT_ID, due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(REQUEST_ID, 0, received, conflict_request)
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,?,?,?,?)",
            (CONFLICT_ID, ORG_ID, REQUEST_ID, 0, "open", SNAPSHOT_SHA, NOW.isoformat()),
        )
        tx.commit()
    return completion, conflict_request


def _uow(
    completion: SqliteQuestionCompletionUnitOfWork,
    *,
    cause_reader: _CauseReader,
    graph_reader: _GraphReader,
    authority: _Authority,
    provider: _ApprovalProvider,
    resolver: _ApprovalResolver,
    fault_injector: Callable[[str], None] | None = None,
    receipt_id: str = "receipt-1",
    manager_item_id: str = "manager-item-1",
) -> DurableConflictEscalationUnitOfWork:
    return DurableConflictEscalationUnitOfWork(
        completion=completion,
        cause_reader=cause_reader,
        graph_reader=graph_reader,
        central_authorizer=authority,
        approval_provider=provider,
        approval_resolver=resolver,
        clock=lambda: NOW,
        receipt_id_factory=lambda: receipt_id,
        manager_item_id_factory=lambda: manager_item_id,
        fault_injector=fault_injector,
    )


def _happy_environment(
    *, manager: bool
) -> tuple[_CauseReader, _GraphReader, _Authority, _ApprovalProvider, _ApprovalResolver]:
    cause = _cause()
    graph = _graph(manager=manager)
    evidence = _evidence(cause=cause, graph=graph)
    return (
        _CauseReader(cause),
        _GraphReader(graph),
        _Authority(),
        _ApprovalProvider(evidence),
        _ApprovalResolver(evidence),
    )


def _receipt_graph_row_counts(tx: SqliteCompletionTransaction) -> dict[str, int]:
    return {
        table: tx.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in (
            "durable_conflict_escalation_receipts",
            "durable_conflict_escalation_evidence",
            "durable_conflict_escalation_result_projections",
            "durable_conflict_escalation_audit_intents",
            "durable_conflict_escalation_outbox_intents",
        )
    }


def _assert_no_writes(completion: SqliteQuestionCompletionUnitOfWork) -> None:
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        assert all(count == 0 for count in _receipt_graph_row_counts(tx).values())
        assert tx.execute("SELECT count(*) FROM durable_linked_manager_items").fetchone()[0] == 0
        case = tx.execute(
            "SELECT status,awaiting_revision FROM durable_linked_conflict_cases WHERE conflict_id=?",
            (CONFLICT_ID,),
        ).fetchone()
        assert case["status"] == "open" and case["awaiting_revision"] == 0
        request = tx.select_question_request(REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, AwaitingConflict) and request.revision == 1
        tx.commit()


@pytest.mark.parametrize("manager", [True, False])
def test_fresh_escalate_commits_three_aggregates_and_receipt_graph_atomically(
    tmp_path: Path, manager: bool
) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, resolver = _happy_environment(manager=manager)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
        )
        result = uow.escalate(principal=_principal(), command=COMMAND)
        if manager:
            assert isinstance(result, DurableConflictEscalatedToManager)
            assert result.manager_subject_ref == MANAGER_REF
        else:
            assert isinstance(result, DurableConflictEscalatedToRoot)
            assert result.root_subject_ref == ROOT_REF
        assert result.conflict_id == CONFLICT_ID
        assert result.request_id == REQUEST_ID
        assert result.request_revision == 2

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _receipt_graph_row_counts(tx).values())
            manager_item = tx.execute(
                "SELECT * FROM durable_linked_manager_items WHERE request_id=?", (REQUEST_ID,)
            ).fetchone()
            assert manager_item["manager_item_id"] == result.manager_item_id
            assert manager_item["source_kind"] == "deadlock"
            assert manager_item["status"] == "open"
            assert manager_item["awaiting_revision"] == 0
            case = tx.execute(
                "SELECT status,awaiting_revision FROM durable_linked_conflict_cases WHERE conflict_id=?",
                (CONFLICT_ID,),
            ).fetchone()
            assert case["status"] == "escalated" and case["awaiting_revision"] == 0
            tx.commit()
        request = completion.get(REQUEST_ID)
        assert request is not None
        assert isinstance(request.state, AwaitingManager)
        assert request.state.public_kind == "contested"
        assert request.state.item_id == result.manager_item_id
        assert request.revision == 2
    finally:
        completion.close()


@pytest.mark.parametrize("deny_at_call", [1, 2])
def test_denied_central_authorization_conflicts_with_write_zero(
    tmp_path: Path, deny_at_call: int
) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, _, provider, resolver = _happy_environment(manager=True)
        authority = _Authority(deny_at_call=deny_at_call)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_missing_central_authorizer_is_unavailable_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, _, provider, resolver = _happy_environment(manager=True)
        uow = DurableConflictEscalationUnitOfWork(
            completion=completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            central_authorizer=None,
            approval_provider=provider,
            approval_resolver=resolver,
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-1",
            manager_item_id_factory=lambda: "manager-item-1",
        )
        with pytest.raises(DurableConflictEscalationUnavailable):
            uow.escalate(principal=_principal(), command=COMMAND)
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_unbound_acquired_evidence_conflicts_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, _, _ = _happy_environment(manager=True)
        mismatched = ConflictEscalationApprovalEvidence(
            evidence_id="evidence-1",
            status="granted",
            action=ESCALATE_ACTION,
            command_digest=COMMAND_DIGEST,
            resource_fingerprint=escalation_resource_fingerprint(RESOURCE),
            escalation_cause_digest="0" * 64,
            graph_selection_digest=_graph(manager=True).graph_digest,
        )
        provider = _ApprovalProvider(mismatched)
        resolver = _ApprovalResolver(mismatched)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        assert provider.calls == 1
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_prewrite_reconfirmation_failure_conflicts_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, _ = _happy_environment(manager=True)
        evidence = _evidence(cause=_cause(), graph=_graph(manager=True))
        resolver = _ApprovalResolver(evidence, invalid_after=2)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        assert provider.calls == 1
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_pending_cause_conflicts_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        pending = Pending(
            ORG_ID, CONFLICT_ID, REQUEST_ID, 0, 1, SNAPSHOT_SHA, BASELINE_SHA, CLAIM_SHA, VOTE_SHA,
            CAUSE_EVALUATED_AT, 1, 2,
        )
        cause_reader = _CauseReader(pending)
        graph = _graph(manager=True)
        evidence = _evidence(cause=_cause(), graph=graph)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=_GraphReader(graph),
            authority=_Authority(),
            provider=_ApprovalProvider(evidence),
            resolver=_ApprovalResolver(evidence),
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_cause_reader_error_is_unavailable_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader = _CauseReader(_cause(), error=RuntimeError("boom"))
        graph = _graph(manager=True)
        evidence = _evidence(cause=_cause(), graph=graph)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=_GraphReader(graph),
            authority=_Authority(),
            provider=_ApprovalProvider(evidence),
            resolver=_ApprovalResolver(evidence),
        )
        with pytest.raises(DurableConflictEscalationUnavailable):
            uow.escalate(principal=_principal(), command=COMMAND)
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_cause_drift_between_acquire_and_prewrite_conflicts_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause0 = _cause()
        cause1 = _cause(vote_set_sha256="9" * 64)
        cause_reader = _CauseReader(cause0, second=cause1)
        graph = _graph(manager=True)
        evidence = _evidence(cause=cause0, graph=graph)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=_GraphReader(graph),
            authority=_Authority(),
            provider=_ApprovalProvider(evidence),
            resolver=_ApprovalResolver(evidence),
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_graph_drift_at_prewrite_conflicts_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause = _cause()
        graph = _graph(manager=True)
        evidence = _evidence(cause=cause, graph=graph)
        graph_reader = _GraphReader(
            graph, verify_error=ConflictEscalationRegistrySnapshotError("drift")
        )
        uow = _uow(
            completion,
            cause_reader=_CauseReader(cause),
            graph_reader=graph_reader,
            authority=_Authority(),
            provider=_ApprovalProvider(evidence),
            resolver=_ApprovalResolver(evidence),
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        assert graph_reader.verify_calls == 1
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_replay_of_same_command_is_idempotent_with_zero_new_calls_and_zero_new_rows(
    tmp_path: Path,
) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, resolver = _happy_environment(manager=True)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
        )
        first = uow.escalate(principal=_principal(), command=COMMAND)
        cause_calls, graph_snapshot_calls, graph_verify_calls = (
            cause_reader.calls,
            graph_reader.snapshot_calls,
            graph_reader.verify_calls,
        )
        authority_calls, provider_calls = authority.calls, provider.calls

        replay = uow.escalate(principal=_principal(), command=COMMAND)

        assert replay == first
        assert cause_reader.calls == cause_calls
        assert graph_reader.snapshot_calls == graph_snapshot_calls
        assert graph_reader.verify_calls == graph_verify_calls
        assert authority.calls == authority_calls
        assert provider.calls == provider_calls

        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert all(count == 1 for count in _receipt_graph_row_counts(tx).values())
            assert tx.execute("SELECT count(*) FROM durable_linked_manager_items").fetchone()[0] == 1
            tx.commit()
    finally:
        completion.close()


def test_escalated_case_with_different_command_digest_conflicts(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, resolver = _happy_environment(manager=True)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
        )
        uow.escalate(principal=_principal(), command=COMMAND)
        different = DurableConflictEscalateCommand(CONFLICT_ID, REQUEST_ID, 2, CLAIMS)
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=different)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert (
                tx.execute("SELECT count(*) FROM durable_conflict_escalation_receipts").fetchone()[0]
                == 1
            )
            tx.commit()
    finally:
        completion.close()


def test_case_cas_race_conflicts_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, resolver = _happy_environment(manager=True)
        tx = completion.durable_transaction()

        def fault(point: str) -> None:
            if point == "after_manager_item":
                tx.execute(
                    "UPDATE durable_linked_conflict_cases SET status='resolved' WHERE conflict_id=?",
                    (CONFLICT_ID,),
                )

        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
            fault_injector=fault,
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        _assert_no_writes(completion)
    finally:
        completion.close()


def test_request_cas_race_conflicts_with_write_zero(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, resolver = _happy_environment(manager=True)
        tx = completion.durable_transaction()

        def fault(point: str) -> None:
            if point == "after_case_escalated":
                tx.execute(
                    "UPDATE question_requests SET revision = revision + 1 WHERE request_id=?",
                    (REQUEST_ID,),
                )

        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
            fault_injector=fault,
        )
        with pytest.raises(DurableConflictEscalationConflict):
            uow.escalate(principal=_principal(), command=COMMAND)
        tx2 = completion.durable_transaction()
        with tx2.scope():
            tx2.begin_immediate()
            assert all(count == 0 for count in _receipt_graph_row_counts(tx2).values())
            assert tx2.execute("SELECT count(*) FROM durable_linked_manager_items").fetchone()[0] == 0
            case = tx2.execute(
                "SELECT status FROM durable_linked_conflict_cases WHERE conflict_id=?", (CONFLICT_ID,)
            ).fetchone()
            assert case["status"] == "open"
            tx2.commit()
    finally:
        completion.close()


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
def test_each_fault_point_rolls_back_every_escalation_artifact_and_keeps_schema_capable(
    tmp_path: Path, point: str
) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, resolver = _happy_environment(manager=True)

        def raise_at_point(injected: str) -> None:
            if injected == point:
                raise RuntimeError(injected)

        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
            fault_injector=raise_at_point,
        )
        with pytest.raises(RuntimeError, match=point):
            uow.escalate(principal=_principal(), command=COMMAND)
        _assert_no_writes(completion)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            tx.validate_component_in_transaction(
                lambda connection: validate_sqlite_durable_conflict_escalation_receipts_connection(
                    connection, org_id=ORG_ID
                )
            )
            tx.validate_component_in_transaction(
                lambda connection: validate_sqlite_durable_linked_aggregates_connection(
                    connection, org_id=ORG_ID
                )
            )
            tx.commit()
    finally:
        completion.close()


def test_committed_receipt_graph_passes_full_schema_validation(tmp_path: Path) -> None:
    completion, _ = _prepared(tmp_path)
    try:
        cause_reader, graph_reader, authority, provider, resolver = _happy_environment(manager=True)
        uow = _uow(
            completion,
            cause_reader=cause_reader,
            graph_reader=graph_reader,
            authority=authority,
            provider=provider,
            resolver=resolver,
        )
        uow.escalate(principal=_principal(), command=COMMAND)
    finally:
        completion.close()

    connection = open_sqlite_durable_conflict_escalation_receipts_connection(
        tmp_path / "workflow.sqlite", org_id=ORG_ID
    )
    try:
        validate_sqlite_durable_conflict_escalation_receipts_connection(connection, org_id=ORG_ID)
    finally:
        connection.close()
