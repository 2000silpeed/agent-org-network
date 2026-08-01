"""RB3.2b.5-C durable Approval inbox and reassignment contract."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import sqlite3

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.central_inbox_approval import (
    ApprovalDispositionInboxApplication,
    ApprovalDispositionInboxCommand,
    ApprovalInboxApplication,
    ApprovalInboxNotFound,
    ApprovalInboxStaleOrConflict,
    ApprovalInboxUnavailable,
    ApprovalReadCommand,
    ApprovalReadProof,
    ApprovalReassignmentApplication,
    ApprovalReassignmentCommand,
    ApprovalTargetAuthorization,
    InboxBoundApprovalDispositionAuthority,
    central_inbox_approval_schema_ready,
    migrate_central_inbox_approval_schema,
)
from agent_org_network.central_inbox_conflict import (
    central_inbox_conflict_schema_ready,
    migrate_central_inbox_conflict_schema,
)
from agent_org_network.central_question_lifecycle import (
    ApprovalDispositionApplication,
    ApprovalDispositionAuthorizationProof,
    ApprovalEvaluation,
    CentralQuestionLifecycleApplication,
    CentralQuestionLifecycleStore,
    OwnerAnswerCandidate,
    OwnerAnswerIngest,
    OwnerAnswerIngestApplication,
    SqliteProductionCardBindingResolver,
    central_question_lifecycle_schema_ready,
    migrate_central_question_lifecycle_schema,
)
from agent_org_network.decision import Routed
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingApproval,
    QuestionRequest,
    RouteTarget,
)
from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    ProductionAgentCardCommand,
    SqliteProductionAgentCards,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)


NOW = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)


def _card(card_id: str, owner: str) -> AgentCard:
    return AgentCard.model_validate(
        {
            "agent_id": card_id,
            "owner": owner,
            "team": "support",
            "summary": card_id,
            "domains": ["refund"],
            "last_reviewed_at": "2026-07-31",
        }
    )


class _UserRegistration:
    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1,
            policy_digest="b" * 64,
            evidence_digest="a" * 64,
        )

    def verify_precommit(
        self, command: object, evidence: object, transaction: sqlite3.Connection
    ) -> bool:
        _ = command, evidence
        return transaction.in_transaction


class _CardRegistration:
    def current(
        self, command: ProductionAgentCardCommand, transaction: sqlite3.Connection
    ) -> CurrentCardRegistrationAuthorization:
        _ = command, transaction
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1,
            policy_digest="d" * 64,
            evidence_digest="c" * 64,
        )

    def verify_precommit(
        self, command: object, evidence: object, transaction: sqlite3.Connection
    ) -> bool:
        _ = command, evidence
        return transaction.in_transaction


def _seed_registry(database: Path) -> None:
    SqliteProductionRegistryUsers.migrate(database)
    users = SqliteProductionRegistryUsers(database, authorize=_UserRegistration())
    for revision, user_id in enumerate(("owner", "second-owner")):
        users.register(
            ProductionRegistryUserCommand(
                org_id="acme",
                principal_id="root",
                idempotency_key=f"user-{user_id}",
                expected_revision=revision,
                user_id=user_id,
                email=f"{user_id}@acme.example",
            )
        )
    users.close()
    SqliteProductionAgentCards.migrate(database)
    cards = SqliteProductionAgentCards(database, authorize=_CardRegistration())
    for revision, card in enumerate(
        (_card("refund", "owner"), _card("second", "second-owner")), start=2
    ):
        cards.register(
            ProductionAgentCardCommand(
                org_id="acme",
                principal_id=card.owner,
                idempotency_key=f"card-{card.agent_id}",
                expected_revision=revision,
                card=card,
            )
        )
    cards.close()


class _Root:
    def resolve_root_manager(
        self, org_id: str, transaction: sqlite3.Connection
    ) -> str:
        assert org_id == "acme" and transaction.in_transaction
        return "owner"


class _Router:
    def route(self, question: str) -> Routed:
        assert question
        return Routed(primary=_card("refund", "owner"), intent="refund")


class _RouteAuthority:
    def authorize_route(
        self,
        org_id: str,
        intent: str,
        agent_id: str,
        transaction: sqlite3.Connection | None,
    ) -> str:
        assert org_id == "acme" and intent == "refund" and agent_id == "refund"
        assert transaction is None or transaction.in_transaction
        return "route-v1"

    def authorize_manager(
        self,
        org_id: str,
        manager_id: str,
        transaction: sqlite3.Connection | None,
    ) -> str:
        _ = org_id, manager_id, transaction
        return "manager-v1"


class _OwnerDelivery:
    def deliver(self, ticket: object) -> None:
        _ = ticket


class _IngestPolicy:
    def evaluate(
        self, org_id: str, route: RouteTarget, candidate: OwnerAnswerCandidate
    ) -> ApprovalEvaluation:
        assert org_id == "acme" and route.agent_id == "refund" and candidate.text
        return ApprovalEvaluation(kind="approval_required", policy_digest="e" * 64)


class _IngestAuthority:
    def authorize_answer_ingest(
        self,
        org_id: str,
        delivery_subject: str,
        owner_id: str,
        agent_id: str,
        transaction: sqlite3.Connection,
    ) -> str:
        assert (org_id, delivery_subject, owner_id, agent_id) == (
            "acme",
            "central-lifecycle",
            "owner",
            "refund",
        )
        assert transaction.in_transaction
        return "ingest-v1"


def _open_approval(
    database: Path,
    *,
    migrate_approval: bool = True,
    migrate_before_ingest: bool = False,
) -> None:
    _seed_registry(database)
    migrate_central_question_lifecycle_schema(database)
    if migrate_before_ingest:
        migrate_central_inbox_conflict_schema(database)
        migrate_central_inbox_approval_schema(database)
    store = CentralQuestionLifecycleStore(
        database,
        root_manager_resolver=_Root(),
        card_binding_resolver=SqliteProductionCardBindingResolver(),
    )
    lifecycle = CentralQuestionLifecycleApplication(
        store=store,
        router=_Router(),
        route_authority=_RouteAuthority(),
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=_Root(),
        work_ticket_id_factory=lambda: "ticket-1",
        owner_delivery=_OwnerDelivery(),
    )
    created = lifecycle.create(
        question="refund question",
        org_id="acme",
        requester_id="requester",
        idempotency_key="create-approval",
    )
    lifecycle.process_received(created.request.request_id)
    lifecycle.process_ready_to_dispatch(created.request.request_id)
    awaiting = store.get("request-1")
    assert awaiting is not None and isinstance(awaiting.state, AwaitingAnswer)
    result = OwnerAnswerIngestApplication(
        store=store,
        approval_policy=_IngestPolicy(),
        authority=_IngestAuthority(),
        record_id_factory=lambda: "unused",
        approval_item_id_factory=lambda: "approval-1",
        clock=lambda: NOW + timedelta(seconds=1),
        approval_deadline=lambda _org, at: at + timedelta(minutes=5),
    ).ingest(
        OwnerAnswerIngest(
            ticket_id=awaiting.state.ticket_id,
            request_id=awaiting.request_id,
            expected_request_revision=awaiting.revision,
            attempt=awaiting.state.attempt,
            route=awaiting.state.route,
            candidate=OwnerAnswerCandidate(
                text="candidate answer",
                sources=("published/refund",),
                mode="full",
            ),
            delivery_subject="central-lifecycle",
        )
    )
    assert isinstance(result.request.state, AwaitingApproval)
    store.close()
    if not migrate_before_ingest:
        migrate_central_inbox_conflict_schema(database)
    if migrate_approval and not migrate_before_ingest:
        migrate_central_inbox_approval_schema(database)


class _Authority:
    def __init__(self) -> None:
        self.current = True
        self.allowed = True
        self.precommit = True
        self.forge_reassignment = False

    def authorize_read(
        self,
        command: ApprovalReadCommand,
        action: str,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ApprovalReadProof:
        if not self.allowed:
            raise ApprovalInboxNotFound()
        principal = AuthenticatedPrincipal(
            org_id=command.expected_org_id,
            subject_id=command.expected_actor_id,
            identity_provider="browser-session",
            identity_session_id=command.identity_session_id,
        )
        session_resource = ResourceRef(
            org_id=principal.org_id,
            kind="browser_session",
            resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        return ApprovalReadProof(
            principal=principal,
            session_grant=AuthorizationGrant(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
                action="session.read",
                resource=session_resource,
                roles=("approver",),
                policy_version="approval-v1",
                policy_digest="a" * 64,
            ),
            action_grant=AuthorizationGrant(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
                action=action,  # type: ignore[arg-type]
                resource=resource,
                roles=("approver",),
                policy_version="approval-v1",
                policy_digest="a" * 64,
            ),
        )

    def issue_disposition_proof(
        self,
        principal: AuthenticatedPrincipal,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof:
        if not self.allowed or not transaction.in_transaction:
            raise ApprovalInboxUnavailable()
        resource = ResourceRef(
            org_id=request.org_id,
            kind="approval_item",
            resource_id=str(item["approval_item_id"]),
            owner_subject_id=principal.subject_id,
        )
        return ApprovalDispositionAuthorizationProof(
            principal=principal,
            grant=AuthorizationGrant(
                org_id=request.org_id,
                subject_id=principal.subject_id,
                action="approval.decide",
                resource=resource,
                roles=("approver",),
                policy_version="approval-v1",
                policy_digest="a" * 64,
            ),
        )

    def verify_disposition_proof(
        self,
        proof: ApprovalDispositionAuthorizationProof,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = proof, request, item
        return self.precommit and transaction.in_transaction

    def issue_reassignment_proof(
        self,
        command: ApprovalReassignmentCommand,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> tuple[ApprovalReadProof, ApprovalTargetAuthorization]:
        proof = self.authorize_read(
            ApprovalReadCommand(
                command.identity_session_id,
                command.expected_org_id,
                command.expected_actor_id,
            ),
            "approval.reassign",
            ResourceRef(
                org_id=request.org_id,
                kind="approval_item",
                resource_id=command.approval_item_id,
                owner_subject_id=command.expected_actor_id,
            ),
            transaction,
        )
        if self.forge_reassignment:
            proof = replace(
                proof,
                action_grant=proof.action_grant.model_copy(
                    update={
                        "resource": ResourceRef(
                            org_id=request.org_id,
                            kind="approval_item",
                            resource_id="forged-item",
                            owner_subject_id=command.expected_actor_id,
                        )
                    }
                ),
            )
        with sqlite3.connect(":memory:"):
            pass
        row = transaction.execute(
            "SELECT revision,card_digest FROM production_agent_cards "
            "WHERE org_id=? AND agent_id=? AND owner_id=?",
            (
                request.org_id,
                command.target_approval_card_id,
                command.target_approver_user_id,
            ),
        ).fetchone()
        if row is None:
            raise ApprovalInboxNotFound()
        return proof, ApprovalTargetAuthorization(
            command.target_approver_user_id,
            command.target_approval_card_id,
            int(row["revision"]),
            str(row["card_digest"]),
            "approval-v1",
            "a" * 64,
        )

    def verify_reassignment_proof(
        self,
        proof: ApprovalReadProof,
        target: ApprovalTargetAuthorization,
        command: ApprovalReassignmentCommand,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = proof, target, command, request, item
        return self.precommit and self.current and transaction.in_transaction

    def current_assignment_binding(
        self, assignment: sqlite3.Row, transaction: sqlite3.Connection
    ) -> bool:
        _ = assignment
        return self.current and transaction.in_transaction


def _read(actor: str) -> ApprovalReadCommand:
    return ApprovalReadCommand(
        identity_session_id=sha256(f"session:{actor}".encode()).hexdigest(),
        expected_org_id="acme",
        expected_actor_id=actor,
    )


def test_approval_inbox_contract_is_importable() -> None:
    assert ApprovalInboxApplication
    assert ApprovalDispositionInboxApplication
    assert ApprovalReassignmentApplication
    assert central_inbox_approval_schema_ready
    assert migrate_central_inbox_approval_schema


def test_v15_to_v16_migration_is_marker_last_and_projects_safe_lazy_detail(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    assert central_inbox_conflict_schema_ready(database) is True
    assert central_inbox_approval_schema_ready(database) is True
    authority = _Authority()
    inbox = ApprovalInboxApplication(database_path=database, authority=authority)

    summaries = inbox.list(_read("owner"))
    detail = inbox.detail(_read("owner"), "approval-1")

    assert len(summaries) == 1
    assert summaries[0].approval_item_id == "approval-1"
    assert summaries[0].request_revision == 3
    assert summaries[0].approval_round == 1
    assert summaries[0].state == "open"
    assert "candidate answer" not in repr(summaries)
    assert detail is not None
    assert detail.request_revision == 3
    assert detail.question == "refund question"
    assert detail.candidate_text == "candidate answer"
    assert detail.assigned_approver_user_id == "owner"
    assert detail.assigned_approval_card_id == "refund"
    assert inbox.list(_read("second-owner")) == ()
    assert inbox.detail(_read("second-owner"), "approval-1") is None


def test_approval_list_fails_closed_on_noncurrent_request_item_binding(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE question_requests SET state_json="
            "replace(state_json,'approval-1','approval-tampered') "
            "WHERE request_id='request-1'"
        )

    inbox = ApprovalInboxApplication(database_path=database, authority=_Authority())
    with pytest.raises(ApprovalInboxUnavailable):
        inbox.list(_read("owner"))


def test_v16_install_then_new_ingest_creates_initial_assignment_in_same_uow(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"

    _open_approval(database, migrate_before_ingest=True)

    assert central_inbox_approval_schema_ready(database) is True
    inbox = ApprovalInboxApplication(database_path=database, authority=_Authority())
    assert [item.approval_item_id for item in inbox.list(_read("owner"))] == [
        "approval-1"
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT approval_item_id,approval_round,assigned_approver_user_id,"
            "assigned_approval_card_id FROM central_inbox_approval_assignments"
        ).fetchone() == ("approval-1", 1, "owner", "refund")


def test_v16_migration_fault_rolls_back_catalog_and_keeps_v15(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database, migrate_approval=False)

    with pytest.raises(ApprovalInboxUnavailable):
        migrate_central_inbox_approval_schema(
            database,
            fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if point == "v15-to-v16-before-marker"
                else None
            ),
        )

    assert central_inbox_conflict_schema_ready(database) is True
    assert central_inbox_approval_schema_ready(database) is False
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT approval_item_id,status,revision "
            "FROM central_question_approval_items"
        ).fetchone() == ("approval-1", "open", 1)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE name='central_inbox_approval_component_schema'"
        ).fetchone() is None


@pytest.mark.parametrize("change", ("transfer", "revoke"))
def test_current_assignment_transfer_or_revoke_hides_list_and_detail(
    tmp_path: Path, change: str
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    inbox = ApprovalInboxApplication(database_path=database, authority=authority)
    assert len(inbox.list(_read("owner"))) == 1

    authority.current = False

    assert inbox.list(_read("owner")) == ()
    assert inbox.detail(_read("owner"), "approval-1") is None


@pytest.mark.parametrize("operation", ("list", "detail"))
def test_read_snapshot_revalidates_catalog_and_parent_lifecycle_after_outer_ready(
    tmp_path: Path, operation: str
) -> None:
    database = tmp_path / f"central-{operation}.sqlite3"
    _open_approval(database)
    assert central_inbox_approval_schema_ready(database) is True
    hook_calls: list[str] = []

    def tamper(point: str) -> None:
        hook_calls.append(point)
        with sqlite3.connect(database) as connection:
            if operation == "list":
                connection.execute("DROP INDEX central_inbox_approval_lineage")
            else:
                connection.execute(
                    "UPDATE question_requests SET state_json="
                    "replace(state_json,'approval-1','approval-tampered') "
                    "WHERE request_id='request-1'"
                )

    inbox = ApprovalInboxApplication(
        database_path=database,
        authority=_Authority(),
        read_snapshot_hook=tamper,
    )

    with pytest.raises(ApprovalInboxUnavailable):
        if operation == "list":
            inbox.list(_read("owner"))
        else:
            inbox.detail(_read("owner"), "approval-1")

    assert hook_calls == [f"{operation}-before-snapshot-validation"]


def _disposition_application(
    database: Path, authority: _Authority
) -> ApprovalDispositionInboxApplication:
    store = CentralQuestionLifecycleStore(
        database,
        root_manager_resolver=_Root(),
        card_binding_resolver=SqliteProductionCardBindingResolver(),
    )
    writer = ApprovalDispositionApplication(
        store=store,
        authority=InboxBoundApprovalDispositionAuthority(authority),
        record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    return ApprovalDispositionInboxApplication(
        database_path=database,
        disposition=writer,
    )


def test_disposition_adapter_uses_existing_writer_and_refreshes_projection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    inbox = ApprovalInboxApplication(database_path=database, authority=authority)
    application = _disposition_application(database, authority)
    command = ApprovalDispositionInboxCommand(
        approval_item_id="approval-1",
        identity_session_id=_read("owner").identity_session_id,
        expected_org_id="acme",
        expected_actor_id="owner",
        kind="approve_with_edit",
        edited_text="approved edit",
        expected_approval_item_revision=1,
        expected_request_revision=3,
        idempotency_key="approve-edit",
    )

    first = application.dispose(command)
    replay = application.dispose(command)

    assert first.state == "approved"
    assert first.approval_item_revision == 2
    assert replay == replace(first, replayed=True)
    assert inbox.list(_read("owner")) == ()
    assert inbox.detail(_read("owner"), "approval-1") is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT text FROM central_question_answer_records"
        ).fetchone() == ("approved edit",)
        assert connection.execute(
            "SELECT count(*) FROM central_question_approval_disposition_receipts"
        ).fetchone() == (1,)


def test_reassign_creates_one_open_successor_and_replay_is_write_zero(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    application = ApprovalReassignmentApplication(
        database_path=database,
        authority=authority,
        approval_item_id_factory=lambda: "approval-2",
        receipt_id_factory=lambda: "reassign-receipt-1",
        clock=lambda: NOW + timedelta(minutes=1),
        deadline=lambda _org, at: at + timedelta(minutes=5),
    )
    command = ApprovalReassignmentCommand(
        approval_item_id="approval-1",
        identity_session_id=_read("owner").identity_session_id,
        expected_org_id="acme",
        expected_actor_id="owner",
        target_approver_user_id="second-owner",
        target_approval_card_id="second",
        expected_approval_item_revision=1,
        expected_request_revision=3,
        idempotency_key="reassign-1",
    )

    first = application.reassign(command)
    replay = application.reassign(command)

    assert first.successor_approval_item_id == "approval-2"
    assert first.request_revision == 4
    assert replay == replace(first, replayed=True)
    inbox = ApprovalInboxApplication(database_path=database, authority=authority)
    assert inbox.list(_read("owner")) == ()
    assert [item.approval_item_id for item in inbox.list(_read("second-owner"))] == [
        "approval-2"
    ]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT approval_item_id,status,revision FROM "
            "central_question_approval_items ORDER BY approval_item_id"
        ).fetchall() == [
            ("approval-1", "superseded", 2),
            ("approval-2", "open", 1),
        ]
        assert connection.execute(
            "SELECT count(*) FROM central_question_answer_records"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT status FROM central_question_work_tickets"
        ).fetchone() == ("completed",)
    assert central_question_lifecycle_schema_ready(database) is True


def test_successor_is_disposed_only_by_existing_writer_and_reject_reason_is_bound(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    reassignment = ApprovalReassignmentApplication(
        database_path=database,
        authority=authority,
        approval_item_id_factory=lambda: "approval-2",
        receipt_id_factory=lambda: "reassign-receipt-1",
        clock=lambda: NOW + timedelta(minutes=1),
        deadline=lambda _org, at: at + timedelta(minutes=5),
    )
    reassignment.reassign(
        ApprovalReassignmentCommand(
            approval_item_id="approval-1",
            identity_session_id=_read("owner").identity_session_id,
            expected_org_id="acme",
            expected_actor_id="owner",
            target_approver_user_id="second-owner",
            target_approval_card_id="second",
            expected_approval_item_revision=1,
            expected_request_revision=3,
            idempotency_key="reassign-1",
        )
    )
    disposition = _disposition_application(database, authority)
    rejected = disposition.dispose(
        ApprovalDispositionInboxCommand(
            approval_item_id="approval-2",
            identity_session_id=_read("second-owner").identity_session_id,
            expected_org_id="acme",
            expected_actor_id="second-owner",
            kind="reject",
            reason_code="policy denied",
            expected_approval_item_revision=1,
            expected_request_revision=4,
            idempotency_key="reject-successor",
        )
    )

    assert rejected.state == "rejected"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,revision FROM central_question_approval_items "
            "WHERE approval_item_id='approval-2'"
        ).fetchone() == ("rejected", 2)
        assert connection.execute(
            "SELECT edited_text,terminal_kind FROM "
            "central_question_approval_disposition_receipts"
        ).fetchone() == ("policy denied", "declined")
        assert connection.execute(
            "SELECT count(*) FROM central_question_answer_records"
        ).fetchone() == (0,)
    assert central_question_lifecycle_schema_ready(database) is True


def test_changed_replay_stale_revocation_fault_and_tamper_fail_closed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    command = ApprovalReassignmentCommand(
        approval_item_id="approval-1",
        identity_session_id=_read("owner").identity_session_id,
        expected_org_id="acme",
        expected_actor_id="owner",
        target_approver_user_id="second-owner",
        target_approval_card_id="second",
        expected_approval_item_revision=1,
        expected_request_revision=3,
        idempotency_key="reassign-1",
    )
    application = ApprovalReassignmentApplication(
        database_path=database,
        authority=authority,
        approval_item_id_factory=lambda: "approval-2",
        receipt_id_factory=lambda: "reassign-receipt-1",
        clock=lambda: NOW + timedelta(minutes=1),
        deadline=lambda _org, at: at + timedelta(minutes=5),
    )
    application.reassign(command)

    with pytest.raises(ApprovalInboxStaleOrConflict):
        application.reassign(
            replace(command, target_approval_card_id="refund")
        )
    authority.precommit = False
    with pytest.raises(ApprovalInboxUnavailable):
        application.reassign(command)
    authority.precommit = True
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE central_inbox_approval_reassignment_audits "
                "SET actor_id='forged'"
            )
        connection.execute(
            "DROP TRIGGER central_inbox_approval_reassignment_audits_no_update"
        )
        connection.execute(
            "UPDATE central_inbox_approval_reassignment_audits "
            "SET actor_id='forged'"
        )
    assert central_inbox_approval_schema_ready(database) is False


def test_reassignment_fault_rolls_back_item_request_receipt_and_audit(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    application = ApprovalReassignmentApplication(
        database_path=database,
        authority=authority,
        approval_item_id_factory=lambda: "approval-fault",
        receipt_id_factory=lambda: "reassign-fault",
        clock=lambda: NOW + timedelta(minutes=1),
        deadline=lambda _org, at: at + timedelta(minutes=5),
        fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("fault"))
            if point == "before-approval-reassignment-commit"
            else None
        ),
    )

    with pytest.raises(ApprovalInboxUnavailable):
        application.reassign(
            ApprovalReassignmentCommand(
                approval_item_id="approval-1",
                identity_session_id=_read("owner").identity_session_id,
                expected_org_id="acme",
                expected_actor_id="owner",
                target_approver_user_id="second-owner",
                target_approval_card_id="second",
                expected_approval_item_revision=1,
                expected_request_revision=3,
                idempotency_key="fault",
            )
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT approval_item_id,status,revision "
            "FROM central_question_approval_items"
        ).fetchall() == [("approval-1", "open", 1)]
        assert connection.execute(
            "SELECT state_kind,revision FROM question_requests"
        ).fetchone() == ("awaiting_approval", 3)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_approval_reassignment_receipts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_approval_reassignment_audits"
        ).fetchone() == (0,)


def test_reassignment_rejects_forged_exact_authority_resource_before_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    authority.forge_reassignment = True

    with pytest.raises(ApprovalInboxUnavailable):
        ApprovalReassignmentApplication(
            database_path=database,
            authority=authority,
            approval_item_id_factory=lambda: "approval-forged",
            receipt_id_factory=lambda: "reassign-forged",
            clock=lambda: NOW + timedelta(minutes=1),
            deadline=lambda _org, at: at + timedelta(minutes=5),
        ).reassign(
            ApprovalReassignmentCommand(
                approval_item_id="approval-1",
                identity_session_id=_read("owner").identity_session_id,
                expected_org_id="acme",
                expected_actor_id="owner",
                target_approver_user_id="second-owner",
                target_approval_card_id="second",
                expected_approval_item_revision=1,
                expected_request_revision=3,
                idempotency_key="forged-proof",
            )
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT approval_item_id,status,revision "
            "FROM central_question_approval_items"
        ).fetchall() == [("approval-1", "open", 1)]
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_approval_reassignment_receipts"
        ).fetchone() == (0,)


def test_terminal_disposition_and_reassignment_concurrency_has_one_winner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _open_approval(database)
    authority = _Authority()
    disposition_application = _disposition_application(database, authority)
    reassignment_application = ApprovalReassignmentApplication(
        database_path=database,
        authority=authority,
        approval_item_id_factory=lambda: "approval-race",
        receipt_id_factory=lambda: "reassign-race",
        clock=lambda: NOW + timedelta(minutes=1),
        deadline=lambda _org, at: at + timedelta(minutes=5),
    )

    def dispose() -> str:
        try:
            disposition_application.dispose(
                ApprovalDispositionInboxCommand(
                    approval_item_id="approval-1",
                    identity_session_id=_read("owner").identity_session_id,
                    expected_org_id="acme",
                    expected_actor_id="owner",
                    kind="approve",
                    expected_approval_item_revision=1,
                    expected_request_revision=3,
                    idempotency_key="race-dispose",
                )
            )
            return "disposed"
        except (ApprovalInboxStaleOrConflict, ApprovalInboxUnavailable):
            return "lost"

    def reassign() -> str:
        try:
            reassignment_application.reassign(
                ApprovalReassignmentCommand(
                    approval_item_id="approval-1",
                    identity_session_id=_read("owner").identity_session_id,
                    expected_org_id="acme",
                    expected_actor_id="owner",
                    target_approver_user_id="second-owner",
                    target_approval_card_id="second",
                    expected_approval_item_revision=1,
                    expected_request_revision=3,
                    idempotency_key="race-reassign",
                )
            )
            return "reassigned"
        except (ApprovalInboxStaleOrConflict, ApprovalInboxUnavailable):
            return "lost"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = tuple(
            future.result()
            for future in (pool.submit(dispose), pool.submit(reassign))
        )

    assert outcomes.count("lost") == 1
    assert set(outcomes) & {"disposed", "reassigned"}
    with sqlite3.connect(database) as connection:
        disposition_count = connection.execute(
            "SELECT count(*) FROM central_question_approval_disposition_receipts"
        ).fetchone()[0]
        reassignment_count = connection.execute(
            "SELECT count(*) FROM central_inbox_approval_reassignment_receipts"
        ).fetchone()[0]
        assert disposition_count + reassignment_count == 1
    assert central_inbox_approval_schema_ready(database) is True
    assert central_question_lifecycle_schema_ready(database) is True
