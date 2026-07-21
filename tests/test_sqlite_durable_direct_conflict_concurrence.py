from __future__ import annotations
# pyright: reportArgumentType=false

import hashlib
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from agent_org_network.answer_finalization_sqlite import SqliteQuestionCompletionUnitOfWork
from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.question_request import (
    AwaitingConflict,
    HandlingAssignment,
    QuestionRequest,
    RouteTarget,
)
from agent_org_network.sqlite_completion import migrate_sqlite_completion_schema
from agent_org_network.sqlite_durable_direct_conflict_concurrence import (
    DurableConflictCandidate,
    DurableConflictConcurCommand,
    DurableConflictConcurrencePending,
    DurableConflictConcurrenceResolved,
    DurableDirectConflictConcurrenceConflict,
    DurableDirectConflictConcurrenceUnavailable,
    DurableDirectConflictConcurrenceUnitOfWork,
)
from agent_org_network.sqlite_durable_direct_conflict_uow import (
    migrate_sqlite_durable_direct_conflict_uow_schema,
)
from agent_org_network.sqlite_durable_linked_aggregates import (
    migrate_sqlite_durable_linked_aggregates_schema,
)

NOW = datetime(2026, 7, 16, tzinfo=UTC)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _ref(kind: str, value: str) -> str:
    return f"{kind}:{_sha(value)}"


class _Registry:
    def __init__(self) -> None:
        self.rows = (
            DurableConflictCandidate(
                "card-a",
                "owner-a",
                "refund",
                RouteTarget(intent="refund", agent_id="card-a", requires_approval=False),
            ),
            DurableConflictCandidate(
                "card-b",
                "owner-b",
                "refund",
                RouteTarget(intent="refund", agent_id="card-b", requires_approval=False),
            ),
        )

    def candidates(self, *, org_id: str, conflict_id: str) -> tuple[DurableConflictCandidate, ...]:
        assert org_id == _ref("org", "org-1") and conflict_id == _ref("conflict", "case-1")
        return self.rows


class _Authority:
    def __init__(self) -> None:
        self.calls = 0

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationGrant:
        self.calls += 1
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=action,
            resource=resource,
            roles=("owner",),
            policy_version="v1",
            policy_digest="0" * 64,
        )  # type: ignore[arg-type]

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
    ) -> bool:
        assert isinstance(grant, AuthorizationGrant)
        assert isinstance(principal, AuthenticatedPrincipal)
        assert action == "conflict.concur"
        assert isinstance(resource, ResourceRef)
        return True


def _candidate_hash(rows: tuple[DurableConflictCandidate, ...]) -> str:
    body = [
        {
            "card": _ref("card", row.card_id),
            "owner": _ref("subject", row.owner_subject_id),
            "domain": _sha(row.domain),
            "route": row.route.model_dump(mode="json"),
        }
        for row in rows
    ]
    return _sha(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    )


def _prepared(
    tmp_path: Path,
) -> tuple[
    SqliteQuestionCompletionUnitOfWork, DurableDirectConflictConcurrenceUnitOfWork, _Authority
]:
    path = tmp_path / "workflow.sqlite"
    migrate_sqlite_completion_schema(path)
    migrate_sqlite_durable_linked_aggregates_schema(path)
    migrate_sqlite_durable_direct_conflict_uow_schema(path)
    completion = SqliteQuestionCompletionUnitOfWork(
        path,
        policy=object(),
        approvals=object(),
        responsibility_resolver=object(),
        record_id_factory=lambda: "record",
        clock=lambda: NOW,
    )  # type: ignore[arg-type]
    org_id, request_id, case_id = (
        _ref("org", "org-1"),
        _ref("request", "request-1"),
        _ref("conflict", "case-1"),
    )
    received = QuestionRequest.receive(
        org_id=org_id,
        requester_id="user",
        question="secret question",
        request_id_factory=lambda: request_id,
        clock=lambda: NOW - timedelta(minutes=2),
        due_at=NOW + timedelta(hours=1),
    )
    completion.create(received)
    conflict_request = received.record_initial_routing(
        intent="refund",
        disposition="contested",
        target=AwaitingConflict(
            case_id=case_id,
            handling=HandlingAssignment(
                kind="conflict_case", ref=case_id, due_at=NOW + timedelta(hours=1)
            ),
        ),
        clock=lambda: NOW - timedelta(minutes=1),
    )
    assert completion.compare_and_set(request_id, 0, received, conflict_request)
    registry = _Registry()
    tx = completion.durable_transaction()
    with tx.scope():
        tx.begin_immediate()
        tx.execute(
            "INSERT INTO durable_linked_conflict_cases VALUES(?,?,?,?,?,?,?)",
            (
                case_id,
                org_id,
                request_id,
                1,
                "open",
                _candidate_hash(registry.rows),
                NOW.isoformat(),
            ),
        )
        tx.commit()
    authority = _Authority()
    return (
        completion,
        DurableDirectConflictConcurrenceUnitOfWork(
            completion=completion,
            registry=registry,
            central_authorizer=authority,
            clock=lambda: NOW,
            receipt_id_factory=lambda: "receipt-" + str(authority.calls),
        ),
        authority,
    )


def test_each_current_owner_votes_once_and_last_unanimous_vote_resolves_case_and_request(
    tmp_path: Path,
) -> None:
    completion, uow, authority = _prepared(tmp_path)
    try:
        command = DurableConflictConcurCommand(
            _ref("conflict", "case-1"), _ref("request", "request-1"), "card-a", 1
        )
        first = uow.concur(
            principal=AuthenticatedPrincipal(
                org_id=_ref("org", "org-1"),
                subject_id="owner-a",
                identity_provider="idp",
                identity_session_id="a",
            ),
            command=command,
        )
        assert isinstance(first, DurableConflictConcurrencePending)
        second = uow.concur(
            principal=AuthenticatedPrincipal(
                org_id=_ref("org", "org-1"),
                subject_id="owner-b",
                identity_provider="idp",
                identity_session_id="b",
            ),
            command=command,
        )
        assert isinstance(second, DurableConflictConcurrenceResolved)
        request = completion.get(_ref("request", "request-1"))
        assert (
            request is not None
            and request.state.kind == "ready_to_dispatch"
            and request.state.attempt == 1
        )
        assert authority.calls == 4  # start + immediately-prewrite for each command
    finally:
        completion.close()


def test_revoked_commit_time_authority_and_receipt_graph_fault_leave_no_vote(
    tmp_path: Path,
) -> None:
    completion, uow, authority = _prepared(tmp_path)
    command = DurableConflictConcurCommand(
        _ref("conflict", "case-1"), _ref("request", "request-1"), "card-a", 1
    )
    try:
        authority.verify = lambda *_: False  # type: ignore[method-assign]
        with pytest.raises(DurableDirectConflictConcurrenceConflict):
            uow.concur(
                principal=AuthenticatedPrincipal(
                    org_id=_ref("org", "org-1"),
                    subject_id="owner-a",
                    identity_provider="idp",
                    identity_session_id="a",
                ),
                command=command,
            )
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert (
                tx.execute("SELECT count(*) FROM durable_direct_conflict_votes").fetchone()[0] == 0
            )
            tx.commit()
    finally:
        completion.close()


def test_replay_is_reauthorized_and_other_semantic_command_conflicts(tmp_path: Path) -> None:
    completion, uow, authority = _prepared(tmp_path)
    command = DurableConflictConcurCommand(
        _ref("conflict", "case-1"), _ref("request", "request-1"), "card-a", 1
    )
    principal = AuthenticatedPrincipal(
        org_id=_ref("org", "org-1"),
        subject_id="owner-a",
        identity_provider="idp",
        identity_session_id="a",
    )
    try:
        first = uow.concur(principal=principal, command=command)
        replay = uow.concur(principal=principal, command=command)
        assert isinstance(first, DurableConflictConcurrencePending)
        assert replay == first
        with pytest.raises(DurableDirectConflictConcurrenceConflict):
            uow.concur(
                principal=principal,
                command=DurableConflictConcurCommand(
                    command.conflict_id,
                    command.request_id,
                    "card-b",
                    command.expected_request_revision,
                ),
            )
        assert authority.calls == 4
    finally:
        completion.close()


def test_mid_transaction_fault_rolls_back_every_direct_conflict_artifact(tmp_path: Path) -> None:
    completion, _, authority = _prepared(tmp_path)
    uow = DurableDirectConflictConcurrenceUnitOfWork(
        completion=completion,
        registry=_Registry(),
        central_authorizer=authority,
        clock=lambda: NOW,
        receipt_id_factory=lambda: "fault-receipt",
        fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError(point)) if point == "after_receipt_graph" else None
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="after_receipt_graph"):
            uow.concur(
                principal=AuthenticatedPrincipal(
                    org_id=_ref("org", "org-1"),
                    subject_id="owner-a",
                    identity_provider="idp",
                    identity_session_id="a",
                ),
                command=DurableConflictConcurCommand(
                    _ref("conflict", "case-1"),
                    _ref("request", "request-1"),
                    "card-a",
                    1,
                ),
            )
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            for table in (
                "durable_direct_conflict_votes",
                "durable_direct_conflict_receipts",
                "durable_direct_conflict_audit_intents",
                "durable_direct_conflict_outbox_intents",
                "durable_direct_conflict_result_projections",
            ):
                assert tx.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            tx.commit()
    finally:
        completion.close()


def _resolve(
    uow: DurableDirectConflictConcurrenceUnitOfWork,
) -> tuple[AuthenticatedPrincipal, DurableConflictConcurCommand]:
    command = DurableConflictConcurCommand(
        _ref("conflict", "case-1"), _ref("request", "request-1"), "card-a", 1
    )
    uow.concur(
        principal=AuthenticatedPrincipal(
            org_id=_ref("org", "org-1"),
            subject_id="owner-a",
            identity_provider="idp",
            identity_session_id="a",
        ),
        command=command,
    )
    principal = AuthenticatedPrincipal(
        org_id=_ref("org", "org-1"),
        subject_id="owner-b",
        identity_provider="idp",
        identity_session_id="b",
    )
    assert isinstance(
        uow.concur(principal=principal, command=command), DurableConflictConcurrenceResolved
    )
    return principal, command


@pytest.mark.parametrize("tamper", ["case_reopen", "request_route", "request_trigger"])
def test_consensus_replay_rejects_correct_shape_but_causally_unrelated_state(
    tmp_path: Path, tamper: str
) -> None:
    completion, uow, _ = _prepared(tmp_path)
    try:
        principal, command = _resolve(uow)
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            if tamper == "case_reopen":
                tx.execute(
                    "UPDATE durable_linked_conflict_cases SET status='open' WHERE conflict_id=?",
                    (command.conflict_id,),
                )
            else:
                row = tx.execute(
                    "SELECT state_json FROM question_requests WHERE request_id=?",
                    (command.request_id,),
                ).fetchone()
                assert row is not None
                state = json.loads(row["state_json"])
                if tamper == "request_route":
                    state["route"]["agent_id"] = "different-current-card"
                else:
                    state["trigger_key"] = _ref("receipt", "unrelated")
                    state["handling"]["ref"] = _ref("receipt", "unrelated")
                tx.execute(
                    "UPDATE question_requests SET state_json=? WHERE request_id=?",
                    (json.dumps(state, sort_keys=True, separators=(",", ":")), command.request_id),
                )
            tx.commit()
        with pytest.raises(DurableDirectConflictConcurrenceUnavailable):
            uow.concur(principal=principal, command=command)
    finally:
        completion.close()


def test_noncanonical_offset_second_clock_fails_before_any_direct_conflict_write(
    tmp_path: Path,
) -> None:
    completion, _, authority = _prepared(tmp_path)
    uow = DurableDirectConflictConcurrenceUnitOfWork(
        completion=completion,
        registry=_Registry(),
        central_authorizer=authority,
        clock=lambda: NOW.replace(tzinfo=timezone(timedelta(seconds=1))),
        receipt_id_factory=lambda: "bad-clock",
    )
    try:
        with pytest.raises(DurableDirectConflictConcurrenceConflict, match="canonical"):
            uow.concur(
                principal=AuthenticatedPrincipal(
                    org_id=_ref("org", "org-1"),
                    subject_id="owner-a",
                    identity_provider="idp",
                    identity_session_id="a",
                ),
                command=DurableConflictConcurCommand(
                    _ref("conflict", "case-1"), _ref("request", "request-1"), "card-a", 1
                ),
            )
        tx = completion.durable_transaction()
        with tx.scope():
            tx.begin_immediate()
            assert (
                tx.execute("SELECT count(*) FROM durable_direct_conflict_votes").fetchone()[0] == 0
            )
            tx.commit()
    finally:
        completion.close()
