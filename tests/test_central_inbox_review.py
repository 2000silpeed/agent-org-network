"""RB3.2b.5-D1 durable BackupReview/Reevaluation producer foundation."""

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
    migrate_central_inbox_approval_schema,
)
from agent_org_network.central_inbox_conflict import (
    migrate_central_inbox_conflict_schema,
)
from agent_org_network.central_inbox_review import (
    BackupReviewDispositionApplication,
    BackupReviewDispositionCommand,
    BackupReviewDetail,
    ReevaluationDetail,
    ReevaluationDispositionApplication,
    ReevaluationDispositionCommand,
    ReviewInboxApplication,
    ReviewInboxConflict,
    ReviewInboxNotFound,
    ReviewInboxUnavailable,
    ReviewOutboxProjector,
    ReviewOutboxRecovery,
    ReviewReadCommand,
    ReviewReadProof,
    central_inbox_review_schema_ready,
    migrate_central_inbox_review_schema,
)
from agent_org_network.central_question_lifecycle import (
    ApprovalDispositionApplication,
    ApprovalDispositionAuthorizationProof,
    ApprovalDispositionCommand,
    ApprovalEvaluation,
    CentralQuestionLifecycleUnavailable,
    CentralQuestionLifecycleApplication,
    CentralQuestionLifecycleStore,
    FeedbackAuthorizationProof,
    FeedbackCommand,
    OwnerAnswerCandidate,
    OwnerAnswerIngest,
    OwnerAnswerIngestApplication,
    QuestionFeedbackApplication,
    SqliteProductionCardBindingResolver,
    migrate_central_question_lifecycle_schema,
)
from agent_org_network.decision import Routed
from agent_org_network.question_request import (
    AwaitingAnswer,
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


NOW = datetime(2026, 7, 31, 18, 0, tzinfo=UTC)


def _card(card_id: str = "refund", owner: str = "owner") -> AgentCard:
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
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme",
            principal_id="root",
            idempotency_key="user-owner",
            expected_revision=0,
            user_id="owner",
            email="owner@acme.example",
        )
    )
    users.close()
    SqliteProductionAgentCards.migrate(database)
    cards = SqliteProductionAgentCards(database, authorize=_CardRegistration())
    cards.register(
        ProductionAgentCardCommand(
            org_id="acme",
            principal_id="owner",
            idempotency_key="card-refund",
            expected_revision=1,
            card=_card(),
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
        return Routed(primary=_card(), intent="refund")


class _RouteAuthority:
    def authorize_route(
        self,
        org_id: str,
        intent: str,
        agent_id: str,
        transaction: sqlite3.Connection | None,
    ) -> str:
        assert (org_id, intent, agent_id) == ("acme", "refund", "refund")
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


class _NoApproval:
    def evaluate(
        self, org_id: str, route: RouteTarget, candidate: OwnerAnswerCandidate
    ) -> ApprovalEvaluation:
        assert org_id == "acme" and route.agent_id == "refund" and candidate.text
        return ApprovalEvaluation(kind="no_approval", policy_digest="e" * 64)


class _ApprovalRequired:
    def evaluate(
        self, org_id: str, route: RouteTarget, candidate: OwnerAnswerCandidate
    ) -> ApprovalEvaluation:
        assert org_id == "acme" and route.agent_id == "refund" and candidate.text
        return ApprovalEvaluation(
            kind="approval_required", policy_digest="e" * 64
        )


class _ApprovalAuthority:
    def issue_approval_disposition_proof(
        self,
        principal: AuthenticatedPrincipal,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof:
        assert transaction.in_transaction
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
                roles=("owner",),
                policy_version="v1",
                policy_digest="a" * 64,
            ),
        )

    def verify_approval_disposition_proof(
        self,
        proof: ApprovalDispositionAuthorizationProof,
        request: QuestionRequest,
        item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = proof, request, item
        return transaction.in_transaction


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


class _FeedbackAuthority:
    def issue_feedback_proof(
        self,
        principal: AuthenticatedPrincipal,
        request: QuestionRequest,
        record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> FeedbackAuthorizationProof:
        assert transaction.in_transaction
        session_resource = ResourceRef(
            org_id=request.org_id,
            kind="browser_session",
            resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        resource = ResourceRef(
            org_id=request.org_id,
            kind="question_feedback",
            resource_id=f"{request.request_id}:{record['record_id']}",
            owner_subject_id=principal.subject_id,
        )
        return FeedbackAuthorizationProof(
            principal=principal,
            session_grant=AuthorizationGrant(
                org_id=request.org_id,
                subject_id=principal.subject_id,
                action="session.read",
                resource=session_resource,
                roles=("requester",),
                policy_version="v1",
                policy_digest="a" * 64,
            ),
            feedback_grant=AuthorizationGrant(
                org_id=request.org_id,
                subject_id=principal.subject_id,
                action="feedback.create",
                resource=resource,
                roles=("requester",),
                policy_version="v1",
                policy_digest="a" * 64,
            ),
        )

    def verify_feedback_proof(
        self,
        proof: FeedbackAuthorizationProof,
        request: QuestionRequest,
        record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = proof, request, record
        return transaction.in_transaction


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id="requester",
        identity_provider="browser-session",
        identity_session_id=sha256(b"requester-session").hexdigest(),
    )


def _prepare_database(
    database: Path,
    *,
    install_review_before_source: bool = False,
    mode: str = "backup",
    approval_required: bool = False,
) -> tuple[CentralQuestionLifecycleStore, OwnerAnswerIngest]:
    _seed_registry(database)
    migrate_central_question_lifecycle_schema(database)
    migrate_central_inbox_conflict_schema(database)
    migrate_central_inbox_approval_schema(database)
    if install_review_before_source:
        migrate_central_inbox_review_schema(database)
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
        idempotency_key="create-review-source",
    )
    lifecycle.process_received(created.request.request_id)
    lifecycle.process_ready_to_dispatch(created.request.request_id)
    awaiting = store.get("request-1")
    assert awaiting is not None and isinstance(awaiting.state, AwaitingAnswer)
    command = OwnerAnswerIngest(
        ticket_id=awaiting.state.ticket_id,
        request_id=awaiting.request_id,
        expected_request_revision=awaiting.revision,
        attempt=awaiting.state.attempt,
        route=awaiting.state.route,
        candidate=OwnerAnswerCandidate(
            text=f"{mode} answer",
            sources=("published/refund",),
            mode=mode,  # type: ignore[arg-type]
        ),
        delivery_subject="central-lifecycle",
    )
    OwnerAnswerIngestApplication(
        store=store,
        approval_policy=(
            _ApprovalRequired() if approval_required else _NoApproval()
        ),
        authority=_IngestAuthority(),
        record_id_factory=lambda: "answer-1",
        approval_item_id_factory=lambda: "approval-1",
        clock=lambda: NOW + timedelta(seconds=1),
        approval_deadline=lambda _org, at: at,
    ).ingest(command)
    return store, command


def _submit_feedback(
    store: CentralQuestionLifecycleStore,
    *,
    verdict: str,
    feedback_id: str,
    idempotency_key: str,
) -> FeedbackCommand:
    command = FeedbackCommand(
        request_id="request-1",
        record_id="answer-1",
        principal=_principal(),
        verdict=verdict,  # type: ignore[arg-type]
        comment=f"{verdict} feedback",
        idempotency_key=idempotency_key,
    )
    QuestionFeedbackApplication(
        store=store,
        authority=_FeedbackAuthority(),
        feedback_id_factory=lambda: feedback_id,
        clock=lambda: NOW + timedelta(seconds=2),
    ).submit(command)
    return command


class _ReadAuthority:
    def __init__(self) -> None:
        self.current = True

    def authorize_read(
        self,
        command: ReviewReadCommand,
        action: str,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ReviewReadProof:
        assert transaction.in_transaction
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
        return ReviewReadProof(
            principal=principal,
            session_grant=AuthorizationGrant(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
                action="session.read",
                resource=session_resource,
                roles=("owner",),
                policy_version="v1",
                policy_digest="a" * 64,
            ),
            action_grant=AuthorizationGrant(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
                action=action,  # type: ignore[arg-type]
                resource=resource,
                roles=("owner",),
                policy_version="v1",
                policy_digest="a" * 64,
            ),
        )

    def current_source_binding(
        self, row: sqlite3.Row, transaction: sqlite3.Connection
    ) -> bool:
        _ = row
        return self.current and transaction.in_transaction

    def authorize_disposition(
        self,
        command: object,
        action: str,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ReviewReadProof:
        return self.authorize_read(
            ReviewReadCommand(
                identity_session_id=str(
                    getattr(command, "identity_session_id")
                ),
                expected_org_id=str(getattr(command, "expected_org_id")),
                expected_actor_id=str(getattr(command, "expected_actor_id")),
            ),
            action,
            resource,
            transaction,
        )

    def verify_disposition(
        self,
        proof: ReviewReadProof,
        command: object,
        action: str,
        resource: ResourceRef,
        row: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = proof, command, action, resource, row
        return self.current and transaction.in_transaction


def _read(actor: str = "owner") -> ReviewReadCommand:
    return ReviewReadCommand(
        identity_session_id=sha256(f"session:{actor}".encode()).hexdigest(),
        expected_org_id="acme",
        expected_actor_id=actor,
    )


def test_review_foundation_contract_is_importable() -> None:
    assert ReviewOutboxProjector
    assert ReviewOutboxRecovery
    assert ReviewInboxApplication
    assert central_inbox_review_schema_ready
    assert migrate_central_inbox_review_schema


def test_v16_to_v17_backfills_all_and_only_eligible_sources_deterministically(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    _submit_feedback(
        store,
        verdict="good",
        feedback_id="feedback-good",
        idempotency_key="good-1",
    )
    store.close()

    migrate_central_inbox_review_schema(database)
    migrate_central_inbox_review_schema(database)

    assert central_inbox_review_schema_ready(database) is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT source_kind,source_id,status,attempts FROM "
            "central_inbox_review_outbox_intents ORDER BY source_kind"
        ).fetchall() == [
            ("backup_review", "answer-1", "pending", 0),
            ("reevaluation", "feedback-bad", "pending", 0),
        ]
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_review_outbox_intents "
            "WHERE source_id='feedback-good'"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT name,version FROM central_inbox_review_component_schema"
        ).fetchone() == ("central-inbox-review", 18)


def test_v17_backfill_fault_or_tampered_source_rolls_back_all(
    tmp_path: Path,
) -> None:
    fault_database = tmp_path / "fault.sqlite3"
    store, _ = _prepare_database(fault_database)
    _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    store.close()
    with sqlite3.connect(fault_database) as connection:
        connection.execute(
            "CREATE TABLE aon_installation_schema("
            "name TEXT PRIMARY KEY,version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO aon_installation_schema(name,version) VALUES(?,?)",
            ("central-installation", 16),
        )

    with pytest.raises(ReviewInboxUnavailable):
        migrate_central_inbox_review_schema(
            fault_database,
            fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if point == "v16-to-v17-before-marker"
                else None
            ),
        )
    with sqlite3.connect(fault_database) as connection:
        assert connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE name='central_inbox_review_component_schema'"
        ).fetchone() is None
        assert connection.execute(
            "SELECT version FROM aon_installation_schema "
            "WHERE name='central-installation'"
        ).fetchone() == (16,)

    tampered_database = tmp_path / "tampered.sqlite3"
    store, _ = _prepare_database(tampered_database)
    _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    store.close()
    with sqlite3.connect(tampered_database) as connection:
        connection.execute(
            "DROP TRIGGER central_question_feedback_audits_no_update"
        )
        connection.execute(
            "UPDATE central_question_feedback_audits "
            "SET payload_digest=? WHERE feedback_id='feedback-bad'",
            ("f" * 64,),
        )

    with pytest.raises(ReviewInboxUnavailable):
        migrate_central_inbox_review_schema(tampered_database)
    assert central_inbox_review_schema_ready(tampered_database) is False


def test_post_v17_source_writers_append_same_uow_and_replay_requires_exact_intent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, ingest_command = _prepare_database(
        database, install_review_before_source=True
    )
    feedback_command = _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT source_kind,source_id FROM central_inbox_review_outbox_intents "
            "ORDER BY source_kind"
        ).fetchall() == [
            ("backup_review", "answer-1"),
            ("reevaluation", "feedback-bad"),
        ]
        connection.execute(
            "DROP TRIGGER central_inbox_review_outbox_intents_no_delete"
        )
        connection.execute(
            "DELETE FROM central_inbox_review_outbox_intents "
            "WHERE source_kind='reevaluation'"
        )

    with pytest.raises(CentralQuestionLifecycleUnavailable):
        QuestionFeedbackApplication(
            store=store,
            authority=_FeedbackAuthority(),
            feedback_id_factory=lambda: "unused",
            clock=lambda: NOW + timedelta(seconds=3),
        ).submit(feedback_command)
    store.close()

    second = tmp_path / "ingest-replay.sqlite3"
    second_store, ingest_command = _prepare_database(
        second, install_review_before_source=True
    )
    with sqlite3.connect(second) as connection:
        connection.execute(
            "DROP TRIGGER central_inbox_review_outbox_intents_no_delete"
        )
        connection.execute(
            "DELETE FROM central_inbox_review_outbox_intents "
            "WHERE source_kind='backup_review'"
        )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        OwnerAnswerIngestApplication(
            store=second_store,
            approval_policy=_NoApproval(),
            authority=_IngestAuthority(),
            record_id_factory=lambda: "unused",
            approval_item_id_factory=lambda: "unused",
            clock=lambda: NOW + timedelta(seconds=4),
            approval_deadline=lambda _org, at: at,
        ).ingest(ingest_command)
    second_store.close()


def test_post_v17_approval_edit_writer_appends_and_replays_exact_backup_intent(
    tmp_path: Path,
) -> None:
    database = tmp_path / "approval.sqlite3"
    store, _ = _prepare_database(
        database,
        install_review_before_source=True,
        approval_required=True,
    )
    command = ApprovalDispositionCommand(
        request_id="request-1",
        approval_item_id="approval-1",
        expected_approval_item_revision=1,
        expected_request_revision=3,
        principal=AuthenticatedPrincipal(
            org_id="acme",
            subject_id="owner",
            identity_provider="browser-session",
            identity_session_id=sha256(b"owner-session").hexdigest(),
        ),
        decision="approve_with_edit",
        edited_text="edited backup answer",
        idempotency_key="approve-edit-backup",
    )
    application = ApprovalDispositionApplication(
        store=store,
        authority=_ApprovalAuthority(),
        record_id_factory=lambda: "answer-1",
        clock=lambda: NOW + timedelta(seconds=2),
    )

    first = application.dispose(command)
    replay = application.dispose(command)

    assert first.record_id == "answer-1"
    assert replay.replayed is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT source_kind,source_id,status FROM "
            "central_inbox_review_outbox_intents"
        ).fetchone() == ("backup_review", "answer-1", "pending")
        connection.execute(
            "DROP TRIGGER central_inbox_review_outbox_intents_no_delete"
        )
        connection.execute(
            "DELETE FROM central_inbox_review_outbox_intents "
            "WHERE source_kind='backup_review'"
        )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        application.dispose(command)
    store.close()


def test_projector_lease_failure_expiry_restart_concurrency_and_metadata_reads(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    migrate_central_inbox_review_schema(database)

    projector = ReviewOutboxProjector(
        database_path=database,
        worker_id="worker-1",
        clock=lambda: NOW + timedelta(minutes=1),
        lease_duration=timedelta(seconds=30),
        fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("projection fault"))
            if point == "before-review-projection-commit"
            else None
        ),
    )
    claim = projector.claim_next()
    assert claim is not None and claim.attempt == 1
    with pytest.raises(ReviewInboxUnavailable):
        projector.project(claim)
    assert projector.claim_next() is None

    retry_projector = ReviewOutboxProjector(
        database_path=database,
        worker_id="worker-2",
        clock=lambda: NOW + timedelta(minutes=2),
        lease_duration=timedelta(seconds=30),
    )
    retried = retry_projector.claim_next()
    assert retried is not None and retried.attempt == 2
    retry_projector.project(retried)

    _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    store.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(
            future.result()
            for future in (
                pool.submit(
                    ReviewOutboxProjector(
                        database_path=database,
                        worker_id="race-1",
                        clock=lambda: NOW + timedelta(minutes=2),
                        lease_duration=timedelta(seconds=30),
                    ).claim_next
                ),
                pool.submit(
                    ReviewOutboxProjector(
                        database_path=database,
                        worker_id="race-2",
                        clock=lambda: NOW + timedelta(minutes=2),
                        lease_duration=timedelta(seconds=30),
                    ).claim_next
                ),
            )
        )
    assert sum(claim is not None for claim in claims) == 1
    winner = next(claim for claim in claims if claim is not None)
    ReviewOutboxProjector(
        database_path=database,
        worker_id=winner.worker_id,
        clock=lambda: NOW + timedelta(minutes=2),
        lease_duration=timedelta(seconds=30),
    ).project(winner)
    ReviewOutboxRecovery(
        projector=ReviewOutboxProjector(
            database_path=database,
            worker_id="startup",
            clock=lambda: NOW + timedelta(minutes=3),
            lease_duration=timedelta(seconds=30),
        )
    ).drain()

    authority = _ReadAuthority()
    inbox = ReviewInboxApplication(database_path=database, authority=authority)
    backup = inbox.list_backup_reviews(_read())
    reevaluations = inbox.list_reevaluations(_read())
    assert len(backup) == len(reevaluations) == 1
    backup_detail = inbox.backup_review_detail(_read(), backup[0].review_id)
    reevaluation_detail = inbox.reevaluation_detail(
        _read(), reevaluations[0].reevaluation_id
    )
    assert isinstance(backup_detail, BackupReviewDetail)
    assert isinstance(reevaluation_detail, ReevaluationDetail)
    assert backup_detail.backup_answer_text == "backup answer"
    assert reevaluation_detail.feedback_comment == "bad feedback"
    assert "published/refund" not in repr(backup_detail)
    assert inbox.list_backup_reviews(_read("foreign")) == ()
    assert (
        inbox.backup_review_detail(_read("foreign"), backup[0].review_id) is None
    )
    authority.current = False
    assert inbox.list_reevaluations(_read()) == ()

    assert central_inbox_review_schema_ready(database) is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT source_kind,status,attempts FROM "
            "central_inbox_review_outbox_intents ORDER BY source_kind"
        ).fetchall() == [
            ("backup_review", "delivered", 2),
            ("reevaluation", "delivered", 1),
        ]
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_review_projection_receipts"
        ).fetchone() == (2,)


def test_bounded_recovery_detects_backlog_without_claiming_n_plus_one(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    migrate_central_inbox_review_schema(database)
    _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    store.close()

    with pytest.raises(ReviewInboxUnavailable):
        ReviewOutboxRecovery(
            projector=ReviewOutboxProjector(
                database_path=database,
                worker_id="bounded-startup",
                clock=lambda: NOW + timedelta(minutes=1),
                lease_duration=timedelta(seconds=30),
            ),
            maximum=1,
        ).drain()

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,attempts,worker_id,lease_until "
            "FROM central_inbox_review_outbox_intents ORDER BY status"
        ).fetchall() == [
            ("delivered", 1, None, None),
            ("pending", 0, None, None),
        ]

    assert (
        ReviewOutboxRecovery(
            projector=ReviewOutboxProjector(
                database_path=database,
                worker_id="restarted-startup",
                clock=lambda: NOW + timedelta(minutes=2),
                lease_duration=timedelta(seconds=30),
            )
        ).drain()
        == 1
    )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT status,attempts,worker_id,lease_until "
            "FROM central_inbox_review_outbox_intents ORDER BY intent_id"
        ).fetchall() == [
            ("delivered", 1, None, None),
            ("delivered", 1, None, None),
        ]


def test_backup_review_disposition_correct_is_append_only_and_canonical(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    migrate_central_inbox_review_schema(database)
    ReviewOutboxRecovery(
        projector=ReviewOutboxProjector(
            database_path=database,
            worker_id="startup",
            clock=lambda: NOW + timedelta(minutes=1),
            lease_duration=timedelta(seconds=30),
        )
    ).drain()
    review_id = ReviewInboxApplication(
        database_path=database, authority=_ReadAuthority()
    ).list_backup_reviews(_read())[0].review_id
    authority = _ReadAuthority()
    application = BackupReviewDispositionApplication(
        database_path=database,
        authority=authority,
        receipt_id_factory=lambda: "backup-disposition-1",
        correction_record_id_factory=lambda: "correction-1",
        clock=lambda: NOW + timedelta(minutes=2),
    )
    command = BackupReviewDispositionCommand(
        review_id=review_id,
        identity_session_id=_read().identity_session_id,
        expected_org_id="acme",
        expected_actor_id="owner",
        kind="correct",
        rationale="  원문 보존 \n",
        corrected_text="  corrected answer \n",
        expected_revision=1,
        idempotency_key="backup-correct-1",
    )

    with pytest.raises(ReviewInboxUnavailable):
        application.dispose(replace(command, corrected_text=""))
    result = application.dispose(command)
    assert result.replayed is False
    assert result.state == "reviewed" and result.revision == 2
    assert application.dispose(command).replayed is True
    projection = store.answered_projection("request-1")
    assert projection is not None
    assert (
        projection.record_id,
        projection.text,
        projection.mode,
        projection.review_status,
    ) == ("correction-1", "  corrected answer \n", "full", "approved")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT text,mode FROM central_question_answer_records "
            "WHERE record_id='answer-1'"
        ).fetchone() == ("backup answer", "backup")
        assert connection.execute(
            "SELECT text,text_digest,mode,supersedes_record_id "
            "FROM central_inbox_answer_correction_records"
        ).fetchone() == (
            "  corrected answer \n",
            sha256("  corrected answer \n".encode()).hexdigest(),
            "full",
            "answer-1",
        )
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_review_outbox_intents"
        ).fetchone() == (1,)
    assert application.dispose(command).replayed is True
    with pytest.raises(ReviewInboxConflict):
        application.dispose(replace(command, rationale="changed"))
    authority.current = False
    with pytest.raises(ReviewInboxNotFound):
        application.dispose(command)
    store.close()


def test_reevaluation_disposition_followup_is_append_only_and_replay_safe(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    migrate_central_inbox_review_schema(database)
    _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    ReviewOutboxRecovery(
        projector=ReviewOutboxProjector(
            database_path=database,
            worker_id="startup",
            clock=lambda: NOW + timedelta(minutes=1),
            lease_duration=timedelta(seconds=30),
        )
    ).drain()
    reevaluation_id = ReviewInboxApplication(
        database_path=database, authority=_ReadAuthority()
    ).list_reevaluations(_read())[0].reevaluation_id
    authority = _ReadAuthority()
    application = ReevaluationDispositionApplication(
        database_path=database,
        authority=authority,
        receipt_id_factory=lambda: "reevaluation-disposition-1",
        reanswer_request_id_factory=lambda: "reanswer-request-1",
        clock=lambda: NOW + timedelta(minutes=2),
    )
    command = ReevaluationDispositionCommand(
        reevaluation_id=reevaluation_id,
        identity_session_id=_read().identity_session_id,
        expected_org_id="acme",
        expected_actor_id="owner",
        kind="request_reanswer",
        rationale="  다시 답변 필요 \n",
        expected_revision=1,
        idempotency_key="reevaluation-1",
    )

    result = application.dispose(command)
    assert result.replayed is False
    assert result.state == "reviewed" and result.revision == 2
    assert application.dispose(command).replayed is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT reevaluation_id,rationale FROM "
            "central_inbox_reanswer_requested_records"
        ).fetchone() == (reevaluation_id, "  다시 답변 필요 \n")
        assert connection.execute(
            "SELECT state_kind FROM question_requests "
            "WHERE request_id='request-1'"
        ).fetchone() == ("answered",)
        assert connection.execute(
            "SELECT verdict,comment FROM central_question_feedback_records"
        ).fetchone() == ("bad", "bad feedback")
        assert connection.execute(
            "SELECT count(*) FROM central_question_answer_records"
        ).fetchone() == (1,)
    assert ReviewInboxApplication(
        database_path=database, authority=authority
    ).list_reevaluations(_read()) == ()
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DROP TRIGGER "
            "central_inbox_reevaluation_disposition_audits_no_update"
        )
        connection.execute(
            "UPDATE central_inbox_reevaluation_disposition_audits "
            "SET command_digest=?",
            ("f" * 64,),
        )
    assert central_inbox_review_schema_ready(database) is False


def test_v17_to_v18_companion_migration_is_marker_last_and_retryable(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    store.close()

    with pytest.raises(ReviewInboxUnavailable):
        migrate_central_inbox_review_schema(
            database,
            fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if point == "v17-to-v18-before-marker"
                else None
            ),
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM central_inbox_review_component_schema"
        ).fetchone() == (17,)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='central_inbox_backup_review_heads'"
        ).fetchone() is None

    migrate_central_inbox_review_schema(database)
    assert central_inbox_review_schema_ready(database) is True
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT version FROM central_inbox_review_component_schema"
        ).fetchone() == (18,)


@pytest.mark.parametrize("kind", ["approve", "dismiss"])
def test_backup_review_noncorrecting_dispositions_close_only_review(
    tmp_path: Path, kind: str
) -> None:
    database = tmp_path / f"{kind}.sqlite3"
    store, _ = _prepare_database(database)
    migrate_central_inbox_review_schema(database)
    ReviewOutboxRecovery(
        projector=ReviewOutboxProjector(
            database_path=database,
            worker_id="startup",
            clock=lambda: NOW + timedelta(minutes=1),
            lease_duration=timedelta(seconds=30),
        )
    ).drain()
    authority = _ReadAuthority()
    review_id = ReviewInboxApplication(
        database_path=database, authority=authority
    ).list_backup_reviews(_read())[0].review_id
    with sqlite3.connect(database) as connection:
        request_before = connection.execute(
            "SELECT state_kind,revision FROM question_requests"
        ).fetchone()
    application = BackupReviewDispositionApplication(
        database_path=database,
        authority=authority,
        receipt_id_factory=lambda: f"{kind}-receipt",
        correction_record_id_factory=lambda: "must-not-be-used",
        clock=lambda: NOW + timedelta(minutes=2),
    )

    result = application.dispose(
        BackupReviewDispositionCommand(
            review_id=review_id,
            identity_session_id=_read().identity_session_id,
            expected_org_id="acme",
            expected_actor_id="owner",
            kind=kind,  # type: ignore[arg-type]
            rationale="reviewed",
            corrected_text=None,
            expected_revision=1,
            idempotency_key=f"{kind}-1",
        )
    )
    assert result.correction_record_id is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT revision,state FROM central_inbox_backup_review_heads"
        ).fetchone() == (2, "reviewed")
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_answer_correction_records"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT text,mode FROM central_question_answer_records"
        ).fetchone() == ("backup answer", "backup")
        assert connection.execute(
            "SELECT state_kind,revision FROM question_requests"
        ).fetchone() == request_before
    store.close()


def test_disposition_fault_rolls_back_and_concurrent_cas_has_one_winner(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    migrate_central_inbox_review_schema(database)
    ReviewOutboxRecovery(
        projector=ReviewOutboxProjector(
            database_path=database,
            worker_id="startup",
            clock=lambda: NOW + timedelta(minutes=1),
            lease_duration=timedelta(seconds=30),
        )
    ).drain()
    authority = _ReadAuthority()
    review_id = ReviewInboxApplication(
        database_path=database, authority=authority
    ).list_backup_reviews(_read())[0].review_id
    base = BackupReviewDispositionCommand(
        review_id=review_id,
        identity_session_id=_read().identity_session_id,
        expected_org_id="acme",
        expected_actor_id="owner",
        kind="approve",
        rationale="ok",
        corrected_text=None,
        expected_revision=1,
        idempotency_key="fault-1",
    )
    with pytest.raises(ReviewInboxUnavailable):
        BackupReviewDispositionApplication(
            database_path=database,
            authority=authority,
            receipt_id_factory=lambda: "fault-receipt",
            correction_record_id_factory=lambda: "unused",
            clock=lambda: NOW + timedelta(minutes=2),
            fault_injector=lambda point: (
                (_ for _ in ()).throw(RuntimeError("fault"))
                if point == "before-backup-review-disposition-commit"
                else None
            ),
        ).dispose(base)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT revision,state,disposition_receipt_id "
            "FROM central_inbox_backup_review_heads"
        ).fetchone() == (1, "open", None)
        assert connection.execute(
            "SELECT count(*) FROM "
            "central_inbox_backup_review_disposition_receipts"
        ).fetchone() == (0,)

    def dispose(index: int) -> object:
        return BackupReviewDispositionApplication(
            database_path=database,
            authority=authority,
            receipt_id_factory=lambda: f"race-receipt-{index}",
            correction_record_id_factory=lambda: f"unused-{index}",
            clock=lambda: NOW + timedelta(minutes=3),
        ).dispose(
            replace(
                base,
                idempotency_key=f"race-{index}",
                rationale=f"race {index}",
            )
        )

    outcomes: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dispose, index) for index in (1, 2)]
        for future in futures:
            try:
                future.result()
                outcomes.append("success")
            except ReviewInboxConflict:
                outcomes.append("conflict")
    assert sorted(outcomes) == ["conflict", "success"]
    store.close()


def test_reevaluation_acknowledge_closes_without_followup(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    store, _ = _prepare_database(database)
    migrate_central_inbox_review_schema(database)
    _submit_feedback(
        store,
        verdict="bad",
        feedback_id="feedback-bad",
        idempotency_key="bad-1",
    )
    ReviewOutboxRecovery(
        projector=ReviewOutboxProjector(
            database_path=database,
            worker_id="startup",
            clock=lambda: NOW + timedelta(minutes=1),
            lease_duration=timedelta(seconds=30),
        )
    ).drain()
    authority = _ReadAuthority()
    reevaluation_id = ReviewInboxApplication(
        database_path=database, authority=authority
    ).list_reevaluations(_read())[0].reevaluation_id

    result = ReevaluationDispositionApplication(
        database_path=database,
        authority=authority,
        receipt_id_factory=lambda: "ack-receipt",
        reanswer_request_id_factory=lambda: "must-not-be-used",
        clock=lambda: NOW + timedelta(minutes=2),
    ).dispose(
        ReevaluationDispositionCommand(
            reevaluation_id=reevaluation_id,
            identity_session_id=_read().identity_session_id,
            expected_org_id="acme",
            expected_actor_id="owner",
            kind="acknowledge",
            rationale="확인",
            expected_revision=1,
            idempotency_key="ack-1",
        )
    )
    assert result.reanswer_requested_id is None
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_reanswer_requested_records"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT revision,state FROM central_inbox_reevaluation_heads"
        ).fetchone() == (2, "reviewed")
    store.close()
