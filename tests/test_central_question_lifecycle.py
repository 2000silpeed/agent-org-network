"""RB3.2b.4-B1 durable Central Question lifecycle seams."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import Barrier, Thread
from typing import NoReturn, cast

import pytest
import yaml

from agent_org_network.agent_card import AgentCard
import agent_org_network.central_question_lifecycle as lifecycle_module
from agent_org_network.central_authority import (
    AuthenticatedPrincipal, AuthorizationGrant, ResourceRef, SnapshotCentralAuthorizer,
    canonical_policy_digest, load_authority_policy_yaml,
)
from agent_org_network.central_browser_auth import BrowserOidcTransaction, BrowserSession
from agent_org_network.central_browser_auth_sqlite import CentralBrowserAuthSqliteStore, migrate_browser_auth_schema
from agent_org_network.central_question_lifecycle import (
    ApprovalDispositionAuthorizationProof,
    ApprovalEvaluation,
    ApprovalDispositionApplication,
    ApprovalDispositionCommand,
    CardBinding,
    CentralQuestionLifecycleApplication,
    CentralQuestionLifecycleConflict,
    CentralQuestionLifecycleStore,
    CentralQuestionLifecycleUnavailable,
    OwnerAnswerCandidate,
    OwnerAnswerIngest,
    OwnerAnswerIngestApplication,
    FeedbackAuthorizationProof,
    FeedbackCommand,
    QuestionFeedbackApplication,
    QuestionFeedbackConflict,
    QuestionFeedbackNotFound,
    QuestionCreateApplication,
    QuestionCreateAuthorizationProof,
    QuestionCreateCommand,
    QuestionCreateForbidden,
    FileReloadingQuestionFeedbackAuthority,
    SqliteQuestionFeedbackAuthority,
    central_question_lifecycle_schema_ready,
    migrate_central_question_lifecycle_schema,
)
from agent_org_network.decision import Contested, Routed, Unowned
from agent_org_network.question_request import (
    AwaitingAnswer,
    AwaitingApproval,
    AwaitingConflict,
    AwaitingManager,
    AnsweredRequest,
    DeclinedRequest,
    HandlingAssignment,
    QuestionRequest,
    ReadyToDispatch,
    Received,
    RouteTarget,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization, ProductionRegistryUserCommand, SqliteProductionRegistryUsers,
    production_registry_user_fingerprint,
)


NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


class Router:
    def __init__(
        self,
        decision: Routed | Unowned | Contested,
        observed: CentralQuestionLifecycleStore | None = None,
        after_route: Barrier | None = None,
    ) -> None:
        self.decision = decision
        self.observed = observed
        self.after_route = after_route
        self.calls = 0

    def route(self, question: str) -> Routed | Unowned | Contested:
        self.calls += 1
        if self.observed is not None:
            request = self.observed.get("request-1")
            assert request is not None and isinstance(request.state, Received)
        if self.after_route is not None:
            self.after_route.wait()
        return self.decision


class RouteAuthority:
    def __init__(self) -> None:
        self.calls = 0

    def authorize_route(
        self, org_id: str, intent: str, agent_id: str, transaction: sqlite3.Connection | None
    ) -> str | None:
        self.calls += 1
        return "policy-v1"

    def authorize_manager(
        self, org_id: str, manager_id: str, transaction: sqlite3.Connection | None
    ) -> str | None:
        self.calls += 1
        return "policy-v1"


class RootManagerResolver:
    def __init__(self, root: str = "root") -> None:
        self.root = root
        self.calls = 0
        self.transaction_states: list[bool] = []

    def resolve_root_manager(self, org_id: str, transaction: sqlite3.Connection) -> str:
        self.calls += 1
        self.transaction_states.append(transaction.in_transaction)
        if org_id != "acme":
            raise RuntimeError("unknown org")
        return self.root


class CardBindingResolver:
    def __init__(self, *, owners: dict[str, str] | None = None, revision: int = 1) -> None:
        self.owners = owners or {"refund": "owner", "second": "second-owner"}
        self.revision = revision
        self.transaction_states: list[bool] = []

    def resolve_card_binding(
        self, org_id: str, agent_id: str, transaction: sqlite3.Connection
    ) -> CardBinding:
        self.transaction_states.append(transaction.in_transaction)
        owner_id = self.owners.get(agent_id)
        if org_id != "acme" or owner_id is None:
            raise RuntimeError("card unavailable")
        return CardBinding(agent_id=agent_id, owner_id=owner_id, revision=self.revision)


class FailingRouter:
    def route(self, question: str) -> NoReturn:
        raise RuntimeError(question)


class OwnerDelivery:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.tickets: list[object] = []

    def deliver(self, ticket: object) -> None:
        self.tickets.append(ticket)
        if self.fail:
            raise RuntimeError("delivery unavailable")


class IngestAuthority:
    def __init__(self, version: str = "ingest-v1") -> None:
        self.version = version
        self.calls = 0

    def authorize_answer_ingest(
        self, org_id: str, delivery_subject: str, owner_id: str, agent_id: str,
        transaction: sqlite3.Connection,
    ) -> str | None:
        self.calls += 1
        assert org_id == "acme" and delivery_subject == "central-lifecycle"
        assert owner_id == "owner" and agent_id == "refund" and transaction.in_transaction
        return self.version


class IngestPolicy:
    def __init__(self, kind: str = "no_approval", digest: str = "policy-digest-v1") -> None:
        self.kind = kind
        self.digest = digest

    def evaluate(self, org_id: str, route: RouteTarget, candidate: OwnerAnswerCandidate) -> ApprovalEvaluation:
        assert org_id == "acme" and route.agent_id == "refund" and candidate.text
        return ApprovalEvaluation(kind=self.kind, policy_digest=self.digest)  # type: ignore[arg-type]


class DispositionAuthority:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.precommit_allowed = allowed
        self.revoke_after_issue = False

    def issue_approval_disposition_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> ApprovalDispositionAuthorizationProof:
        if not self.allowed or principal.subject_id != "approver" or not transaction.in_transaction:
            raise CentralQuestionLifecycleUnavailable("approval denied")
        if self.revoke_after_issue:
            self.precommit_allowed = False
        resource = ResourceRef(
            org_id=request.org_id, kind="approval_item",
            resource_id=str(item["approval_item_id"]), owner_subject_id=principal.subject_id,
        )
        return ApprovalDispositionAuthorizationProof(
            principal=principal,
            grant=AuthorizationGrant(
                org_id=request.org_id, subject_id=principal.subject_id, action="approval.decide",
                resource=resource, roles=("approver",), policy_version="approval-policy-v1",
                policy_digest="a" * 64,
            ),
        )

    def verify_approval_disposition_proof(
        self, proof: ApprovalDispositionAuthorizationProof, request: QuestionRequest, item: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        return self.precommit_allowed and proof.principal.subject_id == "approver" and transaction.in_transaction


class FeedbackAuthority:
    def __init__(self, *, allowed: bool = True) -> None:
        self.allowed = allowed
        self.precommit_allowed = allowed

    def issue_feedback_proof(
        self, principal: AuthenticatedPrincipal, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> FeedbackAuthorizationProof:
        if not self.allowed or principal.subject_id != "user" or not transaction.in_transaction:
            raise QuestionFeedbackNotFound()
        session_resource = ResourceRef(
            org_id=request.org_id, kind="browser_session", resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        feedback_resource = ResourceRef(
            org_id=request.org_id, kind="question_feedback",
            resource_id=f"{request.request_id}:{record['record_id']}", owner_subject_id=principal.subject_id,
        )
        return FeedbackAuthorizationProof(
            principal,
            AuthorizationGrant(
                org_id=request.org_id, subject_id=principal.subject_id, action="session.read",
                resource=session_resource, roles=("requester",), policy_version="feedback-v1", policy_digest="b" * 64,
            ),
            AuthorizationGrant(
                org_id=request.org_id, subject_id=principal.subject_id, action="feedback.create",
                resource=feedback_resource, roles=("requester",), policy_version="feedback-v1", policy_digest="b" * 64,
            ),
        )

    def verify_feedback_proof(
        self, proof: FeedbackAuthorizationProof, request: QuestionRequest, record: sqlite3.Row,
        transaction: sqlite3.Connection,
    ) -> bool:
        return self.precommit_allowed and proof.principal.subject_id == "user" and transaction.in_transaction


class QuestionCreateAuthority:
    def __init__(self, *, allowed: bool = True, revoke_after_issue: bool = False) -> None:
        self.allowed = allowed
        self.precommit_allowed = allowed
        self.revoke_after_issue = revoke_after_issue
        self.issue_transaction_states: list[bool] = []
        self.verify_transaction_states: list[bool] = []

    def issue_question_create_proof(
        self, command: QuestionCreateCommand, transaction: sqlite3.Connection,
    ) -> QuestionCreateAuthorizationProof:
        self.issue_transaction_states.append(transaction.in_transaction)
        if not self.allowed or not transaction.in_transaction:
            raise QuestionCreateForbidden()
        if self.revoke_after_issue:
            self.precommit_allowed = False
        principal = AuthenticatedPrincipal(
            org_id=command.expected_org_id, subject_id=command.expected_requester_id,
            identity_provider="browser-session", identity_session_id=command.identity_session_id,
        )
        session = ResourceRef(
            org_id=principal.org_id, kind="browser_session", resource_id=principal.identity_session_id,
            owner_subject_id=principal.subject_id,
        )
        create = ResourceRef(org_id=principal.org_id, kind="question", owner_subject_id=principal.subject_id)
        return QuestionCreateAuthorizationProof(
            principal,
            AuthorizationGrant(
                org_id=principal.org_id, subject_id=principal.subject_id, action="session.read",
                resource=session, roles=("requester",), policy_version="create-v1", policy_digest="c" * 64,
            ),
            AuthorizationGrant(
                org_id=principal.org_id, subject_id=principal.subject_id, action="question.create",
                resource=create, roles=("requester",), policy_version="create-v1", policy_digest="c" * 64,
            ),
        )

    def verify_question_create_proof(
        self, proof: QuestionCreateAuthorizationProof, command: QuestionCreateCommand,
        transaction: sqlite3.Connection,
    ) -> bool:
        self.verify_transaction_states.append(transaction.in_transaction)
        return (
            self.precommit_allowed
            and transaction.in_transaction
            and proof.principal.org_id == command.expected_org_id
            and proof.principal.subject_id == command.expected_requester_id
        )


class _FeedbackRegistryAuthorizer:
    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection,
    ) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64)

    def verify_precommit(
        self, command: ProductionRegistryUserCommand, evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


def _production_feedback_authority(database: Path, tmp_path: Path) -> tuple[FileReloadingQuestionFeedbackAuthority, AuthenticatedPrincipal, Path]:
    SqliteProductionRegistryUsers.migrate_v2(database)
    registry = SqliteProductionRegistryUsers(database, authorize=_FeedbackRegistryAuthorizer())
    try:
        user = registry.register(ProductionRegistryUserCommand(
            org_id="acme", principal_id="root", idempotency_key="feedback-user", expected_revision=0,
            user_id="user", email="user@example.test",
        )).user
    finally:
        registry.close()
    migrate_browser_auth_schema(database)
    session_digest = sha256(b"feedback-session").hexdigest()
    session = BrowserSession(
        session_digest=session_digest, registry_user_id="user", org_id="acme",
        oidc_identity_binding_digest=sha256(b"identity").hexdigest(), csrf_digest=sha256(b"csrf").hexdigest(),
        registry_fingerprint=production_registry_user_fingerprint(
            user.org_id, user.user_id, user.email, user.manager_id, user.revision,
        ), registry_revision=user.revision, established_at=NOW, expires_at=NOW + timedelta(hours=1),
    )
    browser = CentralBrowserAuthSqliteStore(database)
    try:
        transaction = BrowserOidcTransaction(
            transaction_digest=sha256(b"feedback-transaction").hexdigest(),
            provider_digest=sha256(b"provider").hexdigest(), redirect_uri_digest=sha256(b"redirect").hexdigest(),
            state_digest=sha256(b"state").hexdigest(), nonce_digest=sha256(b"nonce").hexdigest(),
            created_at=NOW, expires_at=NOW + timedelta(minutes=5),
        )
        browser.create_transaction(transaction)
        assert browser.establish_session(transaction.transaction_digest, session, now=NOW, precommit_authorize=lambda _connection: True).value == "established"
    finally:
        browser.close()
    document: dict[str, object] = {
        "schema_version": 1, "org_id": "acme", "policy_version": "feedback-v1", "content_sha256": "pending",
        "subject_roles": [{"org_id": "acme", "subject_id": "user", "roles": ["requester"]}],
        "role_permissions": [{"role": "requester", "actions": ["session.read", "feedback.create"]}],
        "route_rules": [], "worker_bindings": [],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    policy = tmp_path / "feedback-authority.yaml"
    policy.write_text(yaml.safe_dump(document), encoding="utf-8")
    return (
        FileReloadingQuestionFeedbackAuthority(authority_policy_path=policy, configured_org_id="acme", clock=lambda: NOW),
        AuthenticatedPrincipal(org_id="acme", subject_id="user", identity_provider="company-oidc", identity_session_id=session_digest),
        policy,
    )


def _requester_principal(identity_session_id: str = "requester-session-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="user", identity_provider="company-oidc", identity_session_id=identity_session_id,
    )


def _approver_principal(
    subject_id: str = "approver", identity_session_id: str = "approval-session-1",
) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id=subject_id, identity_provider="company-oidc",
        identity_session_id=identity_session_id,
    )


def _card() -> AgentCard:
    return AgentCard.model_validate({
        "agent_id": "refund", "owner": "owner", "team": "support", "summary": "refund",
        "domains": ["refund"], "last_reviewed_at": "2026-07-31",
    })


def _second_card() -> AgentCard:
    return AgentCard.model_validate({
        "agent_id": "second", "owner": "second-owner", "team": "support", "summary": "second",
        "domains": ["refund"], "last_reviewed_at": "2026-07-31",
    })


def _application(tmp_path: Path, decision: Routed | Unowned | Contested, *, observed: CentralQuestionLifecycleStore | None = None, resolver: RootManagerResolver | None = None) -> tuple[CentralQuestionLifecycleApplication, CentralQuestionLifecycleStore, Router, RouteAuthority]:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    root_manager = resolver or RootManagerResolver()
    store = CentralQuestionLifecycleStore(
        database, root_manager_resolver=root_manager, card_binding_resolver=CardBindingResolver()
    )
    router = Router(decision, observed)
    authority = RouteAuthority()
    application = CentralQuestionLifecycleApplication(
        store=store, router=router, route_authority=authority,
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "manager-1", root_manager_resolver=root_manager,
    )
    return application, store, router, authority


def _question_create_application(
    tmp_path: Path, authority: QuestionCreateAuthority,
) -> tuple[QuestionCreateApplication, CentralQuestionLifecycleStore]:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    store = CentralQuestionLifecycleStore(database)
    return (
        QuestionCreateApplication(
            store=store, authority=authority, request_id_factory=lambda: "session-request-1",
            clock=lambda: NOW, deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        ),
        store,
    )


def _question_create_command(
    *, question: str = "refund", idempotency_key: str = "browser-create-1",
) -> QuestionCreateCommand:
    return QuestionCreateCommand(
        question=question, idempotency_key=idempotency_key, identity_session_id="d" * 64,
        expected_org_id="acme", expected_requester_id="user",
    )


def test_session_derived_question_create_commits_and_replays_original_received(tmp_path: Path) -> None:
    authority = QuestionCreateAuthority()
    application, store = _question_create_application(tmp_path, authority)
    command = _question_create_command()

    created = application.create(command)
    replayed = application.create(command)

    assert created.replayed is False and isinstance(created.request.state, Received)
    assert created.request.session_id == command.identity_session_id
    assert replayed.replayed is True and replayed.request == created.request
    assert authority.issue_transaction_states == [True, True]
    assert authority.verify_transaction_states == [True, True]
    store.close()


def test_session_derived_question_create_revocation_inside_uow_rolls_back_request_and_receipt(tmp_path: Path) -> None:
    authority = QuestionCreateAuthority(revoke_after_issue=True)
    application, store = _question_create_application(tmp_path, authority)

    with pytest.raises(QuestionCreateForbidden):
        application.create(_question_create_command())

    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM question_requests").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM central_question_create_receipts").fetchone() == (0,)
    assert authority.issue_transaction_states == [True]
    assert authority.verify_transaction_states == [True]
    store.close()


def test_session_derived_question_create_does_not_replay_after_current_proof_is_revoked(tmp_path: Path) -> None:
    authority = QuestionCreateAuthority()
    application, store = _question_create_application(tmp_path, authority)
    command = _question_create_command()
    created = application.create(command)
    authority.allowed = False

    with pytest.raises(QuestionCreateForbidden):
        application.create(command)

    assert store.get(created.request.request_id) == created.request
    store.close()


def test_received_and_create_receipt_commit_before_router_then_restart_replays_original_received(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    observer = CentralQuestionLifecycleStore(database, root_manager_resolver=RootManagerResolver())
    application, store, router, _authority = _application(
        tmp_path, Unowned(escalated_to="root", intent="refund"), observed=observer
    )

    first = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="create-1")
    assert first.replayed is False
    assert router.calls == 0
    assert isinstance(first.request.state, Received)
    evolved = application.process_received("request-1")
    assert isinstance(evolved.state, AwaitingManager)
    assert store.manager_item_id("request-1") == "manager-1"

    store.close()
    reopened = CentralQuestionLifecycleStore(database, root_manager_resolver=RootManagerResolver())
    replaying = CentralQuestionLifecycleApplication(
        store=reopened, router=router, route_authority=RouteAuthority(),
        request_id_factory=lambda: "unexpected", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "unexpected", root_manager_resolver=RootManagerResolver(),
    )
    replay = replaying.create(question="refund", org_id="acme", requester_id="user", idempotency_key="create-1")
    assert replay.replayed is True
    assert isinstance(replay.request.state, Received)
    assert router.calls == 1
    observer.close()
    reopened.close()


def test_exact_single_greeting_is_durable_declined_without_router_authority_or_manager(tmp_path: Path) -> None:
    application, store, router, authority = _application(tmp_path, Unowned(escalated_to="root", intent="ignored"))

    created = application.create(question="  안녕하세요  ", org_id="acme", requester_id="user", idempotency_key="greeting-1")
    result = application.process_received(created.request.request_id)

    assert isinstance(result.state, DeclinedRequest)
    assert result.state.reason_code == "non_actionable_conversation"
    assert result.revision == 1
    assert router.calls == authority.calls == 0
    assert store.manager_item_id("request-1") is None
    store.close()


def test_routed_stops_at_ready_to_dispatch_and_never_calls_owner_runtime(tmp_path: Path) -> None:
    application, store, router, authority = _application(
        tmp_path, Routed(primary=_card(), intent="refund")
    )

    created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="route-1")
    result = application.process_received(created.request.request_id)

    assert isinstance(result.state, ReadyToDispatch)
    assert result.state.route.agent_id == "refund"
    assert router.calls == 1
    # A preview only supplies the frozen authority version; the authoritative
    # route check happens again inside the disposition UoW.
    assert authority.calls == 2
    store.close()


def test_same_idempotency_key_with_different_question_conflicts_before_router(tmp_path: Path) -> None:
    application, store, router, _authority = _application(tmp_path, Unowned(escalated_to="root", intent="refund"))
    application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="same")
    with pytest.raises(CentralQuestionLifecycleConflict):
        application.create(question="other", org_id="acme", requester_id="user", idempotency_key="same")
    assert router.calls == 0
    store.close()


def test_unowned_transition_fault_rolls_back_manager_and_received_transition(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    def fail(point: str) -> None:
        if point == "before-unowned-commit":
            raise RuntimeError("fault")
    resolver = RootManagerResolver()
    store = CentralQuestionLifecycleStore(database, fault_injector=fail, root_manager_resolver=resolver)
    app = CentralQuestionLifecycleApplication(
        store=store, router=Router(Unowned(escalated_to="root", intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "manager-1", root_manager_resolver=resolver,
    )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        app.process_received(app.create(question="refund", org_id="acme", requester_id="user", idempotency_key="fault").request.request_id)
    request = store.get("request-1")
    assert request is not None and isinstance(request.state, Received)
    assert store.manager_item_id("request-1") is None
    store.close()


def test_record_initial_rejects_unlinked_or_mutually_exclusive_aggregates(tmp_path: Path) -> None:
    application, store, _router, authority = _application(
        tmp_path, Unowned(escalated_to="root", intent="refund")
    )
    current = application.create(
        question="refund", org_id="acme", requester_id="user", idempotency_key="invalid-aggregate"
    ).request
    at = NOW
    awaiting_manager = current.record_initial_routing(
        intent="refund",
        disposition="unowned",
        target=AwaitingManager(
            item_id="manager-1",
            public_kind="unowned",
            handling=HandlingAssignment(kind="manager_item", ref="manager-1", due_at=at + timedelta(minutes=5)),
        ),
        clock=lambda: at,
    )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        store.record_initial(current, awaiting_manager, authority=authority)

    ready = current.record_initial_routing(
        intent="refund",
        disposition="routed",
        target=ReadyToDispatch(
            route=RouteTarget(
                intent="refund", agent_id="refund", requires_approval=False, authority_version="policy-v1"
            ),
            attempt=1,
            trigger_key="request-dispatch:request-1:1",
            handling=HandlingAssignment(kind="system", ref="request-dispatch:request-1:1", due_at=at + timedelta(minutes=5)),
        ),
        clock=lambda: at,
    )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        store.record_initial(current, ready, manager=("manager-1", "root"), authority=authority)
    persisted = store.get(current.request_id)
    assert persisted == current
    assert store.manager_item_id(current.request_id) is None
    store.close()


def test_missing_awaiting_manager_item_fails_closed_on_reopen(tmp_path: Path) -> None:
    application, store, _router, _authority = _application(
        tmp_path, Unowned(escalated_to="root", intent="refund")
    )
    request = application.create(
        question="refund", org_id="acme", requester_id="user", idempotency_key="delete-manager"
    )
    application.process_received(request.request.request_id)
    store.close()
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_manager_items SET item_id='forged-manager' WHERE request_id='request-1'"
        )
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        CentralQuestionLifecycleStore(database, root_manager_resolver=RootManagerResolver())
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_manager_items SET item_id='manager-1' WHERE request_id='request-1'"
        )
    assert central_question_lifecycle_schema_ready(database) is True
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("DELETE FROM central_question_manager_items WHERE request_id='request-1'")
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        CentralQuestionLifecycleStore(database, root_manager_resolver=RootManagerResolver())


def test_unowned_root_resolution_uses_the_active_commit_transaction(tmp_path: Path) -> None:
    resolver = RootManagerResolver()
    application, store, _router, _authority = _application(
        tmp_path, Unowned(escalated_to="root", intent="refund"), resolver=resolver
    )

    created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="root-tx")
    result = application.process_received(created.request.request_id)

    assert isinstance(result.state, AwaitingManager)
    assert resolver.transaction_states == [True]
    store.close()


def test_router_fault_leaves_committed_received_for_restart_recovery(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    resolver = RootManagerResolver()
    first = CentralQuestionLifecycleStore(database, root_manager_resolver=resolver)
    failing = CentralQuestionLifecycleApplication(
        store=first, router=FailingRouter(), route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "manager-1", root_manager_resolver=resolver,
    )
    created = failing.create(question="refund", org_id="acme", requester_id="user", idempotency_key="restart")
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        failing.process_received(created.request.request_id)
    assert isinstance(first.get("request-1").state, Received)  # type: ignore[union-attr]
    first.close()

    recovered, reopened, router, _authority = _application(tmp_path, Unowned(escalated_to="root", intent="refund"))
    result = recovered.process_received("request-1")
    assert isinstance(result.state, AwaitingManager)
    assert router.calls == 1
    reopened.close()


def test_catalog_rejects_added_lifecycle_trigger_and_orphan_receipt(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TRIGGER unexpected_lifecycle_trigger BEFORE INSERT ON central_question_create_receipts BEGIN SELECT 1; END")
    assert central_question_lifecycle_schema_ready(database) is False

    database = tmp_path / "orphan.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO central_question_create_receipts(org_id,requester_id,idempotency_key,question_json,received_json,request_id,created_at) VALUES ('acme','user','key','{\"question\":\"q\"}','{}','missing','2026-07-31T09:00:00+00:00')")
    assert central_question_lifecycle_schema_ready(database) is False


def test_manager_resolver_mismatch_leaves_received_and_tampered_manager_binding_fails_closed(tmp_path: Path) -> None:
    mismatch = RootManagerResolver(root="canonical-root")
    application, store, _router, _authority = _application(
        tmp_path, Unowned(escalated_to="router-root", intent="refund"), resolver=mismatch
    )
    created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="root-mismatch")
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        application.process_received(created.request.request_id)
    assert isinstance(store.get(created.request.request_id).state, Received)  # type: ignore[union-attr]
    store.close()

    tamper_path = tmp_path / "tamper"
    tamper_path.mkdir()
    application, store, _router, _authority = _application(tamper_path, Unowned(escalated_to="root", intent="refund"))
    created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="tamper")
    application.process_received(created.request.request_id)
    with sqlite3.connect(tamper_path / "central.sqlite3") as connection:
        connection.execute("UPDATE central_question_manager_items SET manager_id='forged' WHERE request_id='request-1'")
    store.close()
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        CentralQuestionLifecycleStore(tamper_path / "central.sqlite3", root_manager_resolver=RootManagerResolver())


def test_runtime_contested_decision_preserves_durable_received(tmp_path: Path) -> None:
    application, store, _router, _authority = _application(
        tmp_path, Contested(candidates=(_card(),), intent="refund")
    )

    created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="contested")
    with pytest.raises(CentralQuestionLifecycleUnavailable, match="contested"):
        application.process_received(created.request.request_id)

    persisted = store.get(created.request.request_id)
    assert persisted is not None and isinstance(persisted.state, Received)
    assert store.manager_item_id(created.request.request_id) is None
    store.close()


def test_contested_commits_request_unique_immutable_case_with_current_bindings(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    root = RootManagerResolver()
    bindings = CardBindingResolver()
    store = CentralQuestionLifecycleStore(
        database, root_manager_resolver=root, card_binding_resolver=bindings
    )
    application = CentralQuestionLifecycleApplication(
        store=store,
        router=Router(Contested(candidates=(_card(), _second_card()), intent="refund")),
        route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=root,
        conflict_case_id_factory=lambda: "case-1",
    )

    created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="contested-case")
    result = application.process_received(created.request.request_id)

    assert isinstance(result.state, AwaitingConflict)
    assert result.state.case_id == "case-1"
    assert store.conflict_case_id("request-1") == "case-1"
    assert bindings.transaction_states[:2] == [True, True]
    store.close()


def test_ready_to_dispatch_uses_a_durable_lease_and_recovers_at_least_once_after_expiry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    root = RootManagerResolver()
    bindings = CardBindingResolver()
    store = CentralQuestionLifecycleStore(
        database, root_manager_resolver=root, card_binding_resolver=bindings
    )
    delivery = OwnerDelivery(fail=True)
    application = CentralQuestionLifecycleApplication(
        store=store,
        router=Router(Routed(primary=_card(), intent="refund")),
        route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=root,
        work_ticket_id_factory=lambda: "ticket-1",
        owner_delivery=delivery,
        delivery_worker_id="worker-1",
        delivery_lease_for=timedelta(minutes=1),
    )
    created = application.create(question="refund", org_id="acme", requester_id="user", idempotency_key="ticket")
    assert isinstance(application.process_received(created.request.request_id).state, ReadyToDispatch)

    with pytest.raises(CentralQuestionLifecycleUnavailable, match="delivery"):
        application.process_ready_to_dispatch(created.request.request_id)
    persisted = store.get(created.request.request_id)
    assert persisted is not None and isinstance(persisted.state, AwaitingAnswer)
    assert persisted.state.ticket_id == "ticket-1"
    assert len(delivery.tickets) == 1

    delivery.fail = False
    # The uncertain call stays leased.  It must not be retried before expiry.
    still_leased = application.process_ready_to_dispatch(created.request.request_id)
    assert isinstance(still_leased.state, AwaitingAnswer)
    assert len(delivery.tickets) == 1

    recovered = CentralQuestionLifecycleApplication(
        store=store,
        router=Router(Routed(primary=_card(), intent="refund")),
        route_authority=RouteAuthority(),
        request_id_factory=lambda: "unexpected",
        clock=lambda: NOW + timedelta(minutes=1),
        deadline=lambda _org, _state, at: at + timedelta(minutes=5),
        manager_item_id_factory=lambda: "unexpected",
        root_manager_resolver=root,
        work_ticket_id_factory=lambda: "unexpected",
        owner_delivery=delivery,
        delivery_worker_id="worker-2",
        delivery_lease_for=timedelta(minutes=1),
    ).process_ready_to_dispatch(created.request.request_id)
    assert isinstance(recovered.state, AwaitingAnswer) and recovered.state.ticket_id == "ticket-1"
    assert len(delivery.tickets) == 2
    # An acknowledged stable ticket is never sent again.
    application.process_ready_to_dispatch(created.request.request_id)
    assert len(delivery.tickets) == 2
    store.close()


def test_delivery_claim_is_atomic_across_processes_and_only_claimant_calls_owner(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    root = RootManagerResolver()
    first = CentralQuestionLifecycleStore(database, root_manager_resolver=root, card_binding_resolver=CardBindingResolver())
    second = CentralQuestionLifecycleStore(database, root_manager_resolver=root, card_binding_resolver=CardBindingResolver())
    delivery = OwnerDelivery()
    initial = CentralQuestionLifecycleApplication(
        store=first, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=root, work_ticket_id_factory=lambda: "ticket-1",
    )
    request = initial.create(question="refund", org_id="acme", requester_id="user", idempotency_key="lease")
    initial.process_received(request.request.request_id)
    initial.process_ready_to_dispatch(request.request.request_id)

    barrier = Barrier(2)
    applications = tuple(
        CentralQuestionLifecycleApplication(
            store=store, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
            request_id_factory=lambda: "unexpected", clock=lambda: NOW,
            deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "unexpected",
            root_manager_resolver=root, work_ticket_id_factory=lambda: "unexpected", owner_delivery=delivery,
            delivery_worker_id=worker, delivery_lease_for=timedelta(minutes=1),
        )
        for store, worker in ((first, "worker-1"), (second, "worker-2"))
    )
    failures: list[Exception] = []
    def run(app: CentralQuestionLifecycleApplication) -> None:
        try:
            barrier.wait()
            app.process_ready_to_dispatch("request-1")
        except Exception as error:  # pragma: no cover - asserted below
            failures.append(error)
    threads = tuple(Thread(target=run, args=(app,)) for app in applications)
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert len(delivery.tickets) == 1
    first.close()
    second.close()


def test_delivery_lease_survives_restart_and_redelivers_the_same_ticket_only_after_expiry(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    root = RootManagerResolver()
    store = CentralQuestionLifecycleStore(database, root_manager_resolver=root, card_binding_resolver=CardBindingResolver())
    failed_delivery = OwnerDelivery(fail=True)
    first = CentralQuestionLifecycleApplication(
        store=store, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=root, work_ticket_id_factory=lambda: "ticket-1", owner_delivery=failed_delivery,
        delivery_worker_id="worker-before-restart", delivery_lease_for=timedelta(minutes=1),
    )
    request = first.create(question="refund", org_id="acme", requester_id="user", idempotency_key="restart-lease")
    first.process_received(request.request.request_id)
    with pytest.raises(CentralQuestionLifecycleUnavailable, match="delivery"):
        first.process_ready_to_dispatch(request.request.request_id)
    assert [ticket.ticket_id for ticket in failed_delivery.tickets] == ["ticket-1"]  # type: ignore[union-attr]
    store.close()

    reopened = CentralQuestionLifecycleStore(database, root_manager_resolver=root, card_binding_resolver=CardBindingResolver())
    recovered_delivery = OwnerDelivery()
    before_expiry = CentralQuestionLifecycleApplication(
        store=reopened, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "unexpected", clock=lambda: NOW + timedelta(seconds=30),
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "unexpected",
        root_manager_resolver=root, work_ticket_id_factory=lambda: "unexpected", owner_delivery=recovered_delivery,
        delivery_worker_id="worker-after-restart", delivery_lease_for=timedelta(minutes=1),
    )
    before_expiry.process_ready_to_dispatch("request-1")
    assert recovered_delivery.tickets == []
    after_expiry = CentralQuestionLifecycleApplication(
        store=reopened, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "unexpected", clock=lambda: NOW + timedelta(minutes=1),
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "unexpected",
        root_manager_resolver=root, work_ticket_id_factory=lambda: "unexpected", owner_delivery=recovered_delivery,
        delivery_worker_id="worker-after-restart", delivery_lease_for=timedelta(minutes=1),
    )
    after_expiry.process_ready_to_dispatch("request-1")
    assert [ticket.ticket_id for ticket in recovered_delivery.tickets] == ["ticket-1"]  # type: ignore[union-attr]
    reopened.close()


def test_delivery_claim_tamper_and_catalog_drift_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    root = RootManagerResolver()
    store = CentralQuestionLifecycleStore(database, root_manager_resolver=root, card_binding_resolver=CardBindingResolver())
    app = CentralQuestionLifecycleApplication(
        store=store, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=root, work_ticket_id_factory=lambda: "ticket-1", owner_delivery=OwnerDelivery(),
        delivery_worker_id="worker-1", delivery_lease_for=timedelta(minutes=1),
    )
    request = app.create(question="refund", org_id="acme", requester_id="user", idempotency_key="tamper-lease")
    app.process_received(request.request.request_id)
    app.process_ready_to_dispatch(request.request.request_id)
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE central_question_work_ticket_delivery_claims SET status='unknown'")
    assert central_question_lifecycle_schema_ready(database) is False
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE central_question_work_ticket_delivery_claims SET status='delivered'")
    assert central_question_lifecycle_schema_ready(database) is True
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE central_question_work_ticket_delivery_claims SET ack_receipt='forged' WHERE ticket_id='ticket-1'")
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        CentralQuestionLifecycleStore(database, root_manager_resolver=root, card_binding_resolver=CardBindingResolver())


def _awaiting_answer_for_ingest(
    tmp_path: Path, *, fault_injector: object | None = None,
    card_binding_resolver: CardBindingResolver | None = None,
) -> tuple[CentralQuestionLifecycleStore, QuestionRequest]:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    store = CentralQuestionLifecycleStore(
        database,
        root_manager_resolver=RootManagerResolver(),
        card_binding_resolver=card_binding_resolver or CardBindingResolver(),
        fault_injector=fault_injector,  # type: ignore[arg-type]
    )
    app = CentralQuestionLifecycleApplication(
        store=store, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-1", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "manager-1",
        root_manager_resolver=RootManagerResolver(), work_ticket_id_factory=lambda: "ticket-1",
        owner_delivery=OwnerDelivery(),
    )
    created = app.create(question="refund", org_id="acme", requester_id="user", idempotency_key="ingest")
    app.process_received(created.request.request_id)
    app.process_ready_to_dispatch(created.request.request_id)
    request = store.get("request-1")
    assert request is not None and isinstance(request.state, AwaitingAnswer)
    return store, request


def _ingest_application(
    store: CentralQuestionLifecycleStore, policy: IngestPolicy, authority: IngestAuthority,
) -> OwnerAnswerIngestApplication:
    return OwnerAnswerIngestApplication(
        store=store, approval_policy=policy, authority=authority,
        record_id_factory=lambda: "record-1", approval_item_id_factory=lambda: "approval-1",
        clock=lambda: NOW + timedelta(seconds=1),
        approval_deadline=lambda _org, at: at + timedelta(minutes=5),
    )


def _owner_answer_command(request: QuestionRequest, *, text: str = "refund answer") -> OwnerAnswerIngest:
    assert isinstance(request.state, AwaitingAnswer)
    return OwnerAnswerIngest(
        ticket_id=request.state.ticket_id, request_id=request.request_id,
        expected_request_revision=request.revision, attempt=request.state.attempt, route=request.state.route,
        candidate=OwnerAnswerCandidate(text=text, sources=("published/refund",), mode="full"),
        delivery_subject="central-lifecycle",
    )


def test_owner_answer_ingest_finalizes_no_approval_atomically_and_replays(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    policy = IngestPolicy()
    authority = IngestAuthority()
    app = _ingest_application(store, policy, authority)

    result = app.ingest(_owner_answer_command(request))

    assert result.replayed is False and result.record_id == "record-1" and result.approval_item_id is None
    assert isinstance(result.request.state, AnsweredRequest)
    projection = store.answered_projection("request-1")
    assert projection is not None
    assert projection.record_id == "record-1" and projection.answered_by == {"owner": "owner", "agent_id": "refund"}
    replay = app.ingest(_owner_answer_command(request))
    assert replay.replayed is True and replay.request == result.request
    with pytest.raises(CentralQuestionLifecycleConflict):
        app.ingest(_owner_answer_command(request, text="changed"))
    store.close()


def test_owner_answer_ingest_opens_approval_without_answer_record(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    app = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority())

    result = app.ingest(_owner_answer_command(request))

    assert result.record_id is None and result.approval_item_id == "approval-1" and result.replayed is False
    assert isinstance(result.request.state, AwaitingApproval)
    assert result.request.state.draft_ref == "approval-1"
    assert store.answered_projection("request-1") is None
    assert app.ingest(_owner_answer_command(request)).replayed is True
    store.close()


def test_owner_answer_ingest_replay_fails_closed_on_current_policy_drift_and_tampered_record(
    tmp_path: Path,
) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    policy = IngestPolicy()
    app = _ingest_application(store, policy, IngestAuthority())
    app.ingest(_owner_answer_command(request))
    policy.digest = "policy-digest-v2"
    with pytest.raises(CentralQuestionLifecycleConflict):
        app.ingest(_owner_answer_command(request))
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_answer_records SET text='forged text' WHERE record_id='record-1'"
        )
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        store.answered_projection("request-1")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_answer_records SET text='refund answer' WHERE record_id='record-1'"
        )
    assert central_question_lifecycle_schema_ready(database) is True
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_answer_records SET candidate_digest='forged' WHERE record_id='record-1'"
        )
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        CentralQuestionLifecycleStore(
            database, root_manager_resolver=RootManagerResolver(), card_binding_resolver=CardBindingResolver()
        )


def test_owner_answer_ingest_fault_rolls_back_every_linked_write(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "after-answer-record":
            raise RuntimeError("fault")
    store, request = _awaiting_answer_for_ingest(tmp_path, fault_injector=fail)
    app = _ingest_application(store, IngestPolicy(), IngestAuthority())

    with pytest.raises(CentralQuestionLifecycleUnavailable):
        app.ingest(_owner_answer_command(request))

    persisted = store.get("request-1")
    assert persisted == request
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM central_question_answer_ingest_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM central_question_answer_records").fetchone() == (0,)
        assert connection.execute("SELECT status FROM central_question_work_tickets WHERE ticket_id='ticket-1'").fetchone() == ("pending",)
    store.close()


def test_approval_disposition_approves_or_rejects_the_open_item_once(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    assert pending.approval_item_id == "approval-1"
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    command = ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-1",
    )
    result = app.dispose(command)
    assert isinstance(result.request.state, AnsweredRequest) and result.record_id == "approved-record"
    projection = store.answered_projection("request-1")
    assert projection is not None and projection.review_status == "approved"
    assert app.dispose(command).replayed is True
    with pytest.raises(CentralQuestionLifecycleConflict):
        app.dispose(ApprovalDispositionCommand(
            request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
            expected_request_revision=pending.request.revision, principal=_approver_principal("other"), decision="approve",
            edited_text=None, idempotency_key="approval-2",
        ))
    store.close()


def _answered_feedback_target(tmp_path: Path, *, fault_injector: object | None = None) -> tuple[CentralQuestionLifecycleStore, QuestionRequest]:
    store, request = _awaiting_answer_for_ingest(tmp_path, fault_injector=fault_injector)  # type: ignore[arg-type]
    result = _ingest_application(store, IngestPolicy(), IngestAuthority()).ingest(_owner_answer_command(request))
    assert isinstance(result.request.state, AnsweredRequest) and result.record_id == "record-1"
    return store, result.request


def test_question_feedback_appends_immutable_evidence_and_replays_without_normalizing_comment(tmp_path: Path) -> None:
    store, request = _answered_feedback_target(tmp_path)
    command = FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=_requester_principal(), verdict="good",
        comment="  정확합니다  ", idempotency_key="feedback-1",
    )
    app = QuestionFeedbackApplication(
        store=store, authority=FeedbackAuthority(), feedback_id_factory=lambda: "feedback-1", clock=lambda: NOW,
    )
    result = app.submit(command)
    replay = app.submit(command)
    assert result.feedback_id == "feedback-1" and result.replayed is False and replay.replayed is True
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        assert connection.execute(
            "SELECT comment,verdict FROM central_question_feedback_records"
        ).fetchone() == ("  정확합니다  ", "good")
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_receipts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_audits").fetchone() == (1,)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE central_question_feedback_records SET comment='forged'")
    assert store.get("request-1") == request
    store.close()


@pytest.mark.parametrize(
    ("column", "forged"), [("org_id", "other-org"), ("requester_id", "other-user")],
)
def test_paired_feedback_evidence_org_or_requester_tamper_fails_canonical_request_binding(
    tmp_path: Path, column: str, forged: str,
) -> None:
    store, request = _answered_feedback_target(tmp_path)
    app = QuestionFeedbackApplication(
        store=store, authority=FeedbackAuthority(), feedback_id_factory=lambda: "feedback-1", clock=lambda: NOW,
    )
    command = FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=_requester_principal(), verdict="good",
        comment="", idempotency_key="feedback-paired-tamper",
    )
    app.submit(command)
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        for trigger in (
            "central_question_feedback_records_no_update", "central_question_feedback_records_no_delete",
            "central_question_feedback_receipts_no_update", "central_question_feedback_receipts_no_delete",
            "central_question_feedback_audits_no_update", "central_question_feedback_audits_no_delete",
        ):
            connection.execute(f"DROP TRIGGER {trigger}")
        for table in (
            "central_question_feedback_records", "central_question_feedback_receipts", "central_question_feedback_audits",
        ):
            connection.execute(f"UPDATE {table} SET {column}=?", (forged,))
        for ddl in lifecycle_module._FEEDBACK_TRIGGERS:  # pyright: ignore[reportPrivateUsage]
            connection.execute(ddl)
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        store.get(request.request_id)
    store.close()


@pytest.mark.parametrize(
    ("table", "column", "forged"), [
        ("central_question_feedback_records", "org_id", "other-org"),
        ("central_question_feedback_receipts", "requester_id", "other-user"),
        ("central_question_feedback_audits", "org_id", "other-org"),
    ],
)
def test_single_feedback_evidence_org_or_requester_tamper_fails_reopen(
    tmp_path: Path, table: str, column: str, forged: str,
) -> None:
    store, request = _answered_feedback_target(tmp_path)
    QuestionFeedbackApplication(
        store=store, authority=FeedbackAuthority(), feedback_id_factory=lambda: "feedback-single", clock=lambda: NOW,
    ).submit(FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=_requester_principal(), verdict="good",
        comment="", idempotency_key="feedback-single-tamper",
    ))
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        for ddl in lifecycle_module._FEEDBACK_TRIGGERS:  # pyright: ignore[reportPrivateUsage]
            name = ddl.split()[2]
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute(f"UPDATE {table} SET {column}=?", (forged,))
        for ddl in lifecycle_module._FEEDBACK_TRIGGERS:  # pyright: ignore[reportPrivateUsage]
            connection.execute(ddl)
    assert central_question_lifecycle_schema_ready(database) is False
    store.close()


def test_file_reloading_feedback_authority_uses_current_sqlite_session_and_rechecks_policy_on_replay(
    tmp_path: Path,
) -> None:
    store, request = _answered_feedback_target(tmp_path)
    authority, principal, policy_path = _production_feedback_authority(tmp_path / "central.sqlite3", tmp_path)
    app = QuestionFeedbackApplication(
        store=store, authority=authority, feedback_id_factory=lambda: "production-feedback", clock=lambda: NOW,
    )
    command = FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=principal, verdict="good",
        comment="", idempotency_key="production-feedback",
    )
    assert app.submit(command).replayed is False
    store.close()
    restarted = CentralQuestionLifecycleStore(
        tmp_path / "central.sqlite3", root_manager_resolver=RootManagerResolver(), card_binding_resolver=CardBindingResolver(),
    )
    app = QuestionFeedbackApplication(
        store=restarted, authority=authority, feedback_id_factory=lambda: "must-not-write", clock=lambda: NOW,
    )
    assert app.submit(command).replayed is True
    loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    document = cast(dict[str, object], loaded)
    document["role_permissions"] = [{"role": "requester", "actions": ["session.read"]}]
    document["content_sha256"] = canonical_policy_digest(document)
    policy_path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(QuestionFeedbackNotFound):
        app.submit(command)
    restarted.close()


@pytest.mark.parametrize("session_update", [
    ("expires_at", (NOW - timedelta(seconds=1)).isoformat()),
    ("ended_at", NOW.isoformat()),
    ("registry_fingerprint", "0" * 64),
    ("registry_user_id", "foreign"),
])
def test_sqlite_feedback_authority_rejects_expired_ended_stale_or_foreign_current_session(
    tmp_path: Path, session_update: tuple[str, str],
) -> None:
    store, request = _answered_feedback_target(tmp_path)
    _, principal, policy_path = _production_feedback_authority(tmp_path / "central.sqlite3", tmp_path)
    snapshot = load_authority_policy_yaml(policy_path.read_text(encoding="utf-8"), expected_org_id="acme")
    authority = SqliteQuestionFeedbackAuthority(
        authorizer=SnapshotCentralAuthorizer(snapshot), configured_org_id="acme", clock=lambda: NOW,
    )
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        connection.execute(f"UPDATE browser_sessions SET {session_update[0]}=?", (session_update[1],))
    app = QuestionFeedbackApplication(
        store=store, authority=authority, feedback_id_factory=lambda: "should-not-write", clock=lambda: NOW,
    )
    with pytest.raises(QuestionFeedbackNotFound):
        app.submit(FeedbackCommand(
            request_id=request.request_id, record_id="record-1", principal=principal, verdict="good",
            comment="", idempotency_key="production-session-denial",
        ))
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_records").fetchone() == (0,)
    store.close()


def test_concurrent_identical_feedback_with_production_authority_converges_to_one_evidence_triplet(
    tmp_path: Path,
) -> None:
    store, request = _answered_feedback_target(tmp_path)
    authority, principal, _ = _production_feedback_authority(tmp_path / "central.sqlite3", tmp_path)
    second = CentralQuestionLifecycleStore(
        tmp_path / "central.sqlite3", root_manager_resolver=RootManagerResolver(), card_binding_resolver=CardBindingResolver(),
    )
    command = FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=principal, verdict="good",
        comment="", idempotency_key="concurrent-production-feedback",
    )
    start = Barrier(3)

    def submit(local_store: CentralQuestionLifecycleStore) -> lifecycle_module.QuestionFeedbackResult:
        start.wait()
        return QuestionFeedbackApplication(
            store=local_store, authority=authority, feedback_id_factory=lambda: "concurrent-feedback", clock=lambda: NOW,
        ).submit(command)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(submit, store)
        second_future = executor.submit(submit, second)
        start.wait()
        results = (first.result(timeout=5), second_future.result(timeout=5))
    assert sorted(result.replayed for result in results) == [False, True]
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_records").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_receipts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_audits").fetchone() == (1,)
    second.close()
    store.close()


def test_question_feedback_conflict_hidden_path_and_fault_leave_no_partial_evidence(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "after-feedback-receipt":
            raise RuntimeError("fault")

    fault_dir = tmp_path / "fault"
    fault_dir.mkdir()
    store, request = _answered_feedback_target(fault_dir, fault_injector=fail)
    app = QuestionFeedbackApplication(
        store=store, authority=FeedbackAuthority(), feedback_id_factory=lambda: "feedback-1", clock=lambda: NOW,
    )
    command = FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=_requester_principal(), verdict="bad",
        comment="", idempotency_key="feedback-1",
    )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        app.submit(command)
    with sqlite3.connect(fault_dir / "central.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM central_question_feedback_records").fetchone() == (0,)
    store.close()

    store, request = _answered_feedback_target(tmp_path)
    app = QuestionFeedbackApplication(
        store=store, authority=FeedbackAuthority(), feedback_id_factory=lambda: "feedback-2", clock=lambda: NOW,
    )
    accepted = FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=_requester_principal(), verdict="good",
        comment="", idempotency_key="feedback-conflict",
    )
    app.submit(accepted)
    with pytest.raises(QuestionFeedbackConflict):
        app.submit(FeedbackCommand(
            request_id=request.request_id, record_id="record-1", principal=_requester_principal(), verdict="bad",
            comment="", idempotency_key="feedback-conflict",
        ))
    with pytest.raises(QuestionFeedbackNotFound):
        app.submit(FeedbackCommand(
            request_id=request.request_id, record_id="not-found", principal=_requester_principal(), verdict="good",
            comment="", idempotency_key="feedback-hidden",
        ))
    store.close()


def test_v12_feedback_catalog_forward_migration_is_marker_last_and_rolls_back_on_fault(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute("DROP TABLE central_question_feedback_receipts")
        connection.execute("DROP TABLE central_question_feedback_records")

    with pytest.raises(CentralQuestionLifecycleUnavailable):
        migrate_central_question_lifecycle_schema(
            database,
            fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError("fault"))
            if point == "v12-to-current-after-schema" else None,
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='central_question_feedback_records'"
        ).fetchone() is None

    migrate_central_question_lifecycle_schema(database)
    assert central_question_lifecycle_schema_ready(database) is True


def test_v13_feedback_audit_org_migration_is_atomic(tmp_path: Path) -> None:
    store, request = _answered_feedback_target(tmp_path)
    QuestionFeedbackApplication(
        store=store, authority=FeedbackAuthority(), feedback_id_factory=lambda: "feedback-v13", clock=lambda: NOW,
    ).submit(FeedbackCommand(
        request_id=request.request_id, record_id="record-1", principal=_requester_principal(), verdict="good",
        comment="", idempotency_key="feedback-v13",
    ))
    database = tmp_path / "central.sqlite3"
    store.close()
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        audit = connection.execute("SELECT * FROM central_question_feedback_audits").fetchone()
        assert audit is not None
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute(lifecycle_module._V13_TABLES[14])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V13_FEEDBACK_TRIGGERS[4])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V13_FEEDBACK_TRIGGERS[5])  # pyright: ignore[reportPrivateUsage]
        connection.execute(
            "INSERT INTO central_question_feedback_audits(receipt_id,feedback_id,request_id,record_id,requester_id,verdict,payload_digest,submitted_at) VALUES (?,?,?,?,?,?,?,?)",
            tuple(value for key, value in zip(audit.keys(), audit) if key != "org_id"),
        )
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        migrate_central_question_lifecycle_schema(
            database,
            fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError("fault"))
            if point == "v13-to-current-after-schema" else None,
        )
    migrate_central_question_lifecycle_schema(database)
    assert central_question_lifecycle_schema_ready(database) is True

def test_approval_disposition_replay_reauthenticates_same_actor_on_a_new_session(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    original = ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-new-session",
    )
    app.dispose(original)

    replay = app.dispose(ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision,
        principal=_approver_principal(identity_session_id="approval-session-2"), decision="approve",
        edited_text=None, idempotency_key="approval-new-session",
    ))

    assert replay.replayed is True and replay.record_id == "approved-record"
    store.close()


def test_approval_disposition_edits_or_rejects_with_exact_terminal_evidence(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "edited-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    command = ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve_with_edit",
        edited_text="edited refund answer", idempotency_key="approval-edit",
    )

    result = app.dispose(command)

    projection = store.answered_projection("request-1")
    assert result.record_id == "edited-record" and projection is not None
    assert projection.text == "edited refund answer" and projection.review_status == "approved"
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        receipt = connection.execute(
            "SELECT decision_kind,edited_text,terminal_kind,record_id FROM central_question_approval_disposition_receipts"
        ).fetchone()
        assert receipt == ("approve_with_edit", "edited refund answer", "answered", "edited-record")
        connection.execute("UPDATE central_question_answer_records SET text='forged' WHERE record_id='edited-record'")
    assert central_question_lifecycle_schema_ready(tmp_path / "central.sqlite3") is False
    store.close()


def test_approval_disposition_rejection_creates_no_answer_record(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "must-not-be-used",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    command = ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="reject",
        edited_text=None, idempotency_key="approval-reject",
    )

    result = app.dispose(command)

    assert result.record_id is None and isinstance(result.request.state, DeclinedRequest)
    assert result.request.state.reason_code == "approval_rejected"
    assert app.dispose(command).replayed is True
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM central_question_answer_records").fetchone() == (0,)
        assert connection.execute(
            "SELECT status,revision FROM central_question_approval_items WHERE approval_item_id='approval-1'"
        ).fetchone() == ("rejected", 2)
    store.close()


def test_approval_disposition_replay_requires_the_current_frozen_card_binding(tmp_path: Path) -> None:
    binding = CardBindingResolver()
    store, request = _awaiting_answer_for_ingest(tmp_path, card_binding_resolver=binding)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    command = ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-binding",
    )
    app.dispose(command)
    binding.revision = 2

    with pytest.raises(CentralQuestionLifecycleConflict):
        app.dispose(command)

    store.close()


def test_approval_disposition_fault_rolls_back_every_terminal_write(tmp_path: Path) -> None:
    def fail(point: str) -> None:
        if point == "after-approval-disposition-receipt":
            raise RuntimeError("fault")

    store, request = _awaiting_answer_for_ingest(tmp_path, fault_injector=fail)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )

    with pytest.raises(CentralQuestionLifecycleUnavailable):
        app.dispose(ApprovalDispositionCommand(
            request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
            expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
            edited_text=None, idempotency_key="approval-fault",
        ))

    assert store.get("request-1") == pending.request
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        assert connection.execute(
            "SELECT status,revision FROM central_question_approval_items WHERE approval_item_id='approval-1'"
        ).fetchone() == ("open", 1)
        assert connection.execute("SELECT COUNT(*) FROM central_question_answer_records").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM central_question_approval_disposition_receipts").fetchone() == (0,)
    store.close()


def test_approval_disposition_precommit_proof_revocation_rolls_back(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    authority = DispositionAuthority()
    authority.revoke_after_issue = True
    app = ApprovalDispositionApplication(
        store=store, authority=authority, record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )

    with pytest.raises(CentralQuestionLifecycleUnavailable):
        app.dispose(ApprovalDispositionCommand(
            request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
            expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
            edited_text=None, idempotency_key="approval-revoked",
        ))

    assert store.get("request-1") == pending.request
    with sqlite3.connect(tmp_path / "central.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM central_question_approval_disposition_receipts").fetchone() == (0,)
    store.close()


def _downgrade_open_approval_to_v8(database: Path) -> str:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        item = connection.execute(
            "SELECT * FROM central_question_approval_items WHERE approval_item_id='approval-1'"
        ).fetchone()
        assert item is not None
        created_at = str(item["created_at"])
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute("DROP TABLE central_question_feedback_receipts")
        connection.execute("DROP TABLE central_question_feedback_records")
        connection.execute("DROP TABLE central_question_approval_items")
        connection.execute("DROP TABLE central_question_answer_records")
        connection.execute(lifecycle_module._V8_TABLES[7])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V8_TABLES[8])  # pyright: ignore[reportPrivateUsage]
        connection.execute(
            "INSERT INTO central_question_approval_items(approval_item_id,request_id,ticket_id,org_id,owner_id,agent_id,route_json,attempt,candidate_json,candidate_digest,policy_digest,binding_version,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item["approval_item_id"], item["request_id"], item["ticket_id"], item["org_id"],
                item["owner_id"], item["agent_id"], item["route_json"], item["attempt"],
                item["candidate_json"], item["candidate_digest"], item["policy_digest"], item["binding_version"],
                item["status"], item["created_at"],
            ),
        )
    return created_at


def test_v8_open_approval_fixture_migrates_with_explicit_columns_and_disposes(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    assert isinstance(pending.request.state, AwaitingApproval)
    database = tmp_path / "central.sqlite3"
    store.close()
    created_at = _downgrade_open_approval_to_v8(database)

    migrate_central_question_lifecycle_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT revision,created_at,candidate_json FROM central_question_approval_items WHERE approval_item_id='approval-1'"
        ).fetchone() == (1, created_at, '{"mode":"full","sources":["published/refund"],"text":"refund answer"}')
    reopened = CentralQuestionLifecycleStore(
        database, root_manager_resolver=RootManagerResolver(), card_binding_resolver=CardBindingResolver()
    )
    result = ApprovalDispositionApplication(
        store=reopened, authority=DispositionAuthority(), record_id_factory=lambda: "migrated-record",
        clock=lambda: NOW + timedelta(minutes=1),
    ).dispose(ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-after-v8-migration",
    ))
    assert result.record_id == "migrated-record"
    reopened.close()


def test_v8_approval_migration_fault_rolls_back_to_the_exact_v8_fixture(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    database = tmp_path / "central.sqlite3"
    store.close()
    _downgrade_open_approval_to_v8(database)

    def fail(point: str) -> None:
        if point == "v8-to-current-after-schema":
            raise RuntimeError("fault")

    with pytest.raises(CentralQuestionLifecycleUnavailable):
        migrate_central_question_lifecycle_schema(database, fault_injector=fail)
    with sqlite3.connect(database) as connection:
        assert {row[1] for row in connection.execute("PRAGMA table_info(central_question_approval_items)")} == {
            "approval_item_id", "request_id", "ticket_id", "org_id", "owner_id", "agent_id", "route_json",
            "attempt", "candidate_json", "candidate_digest", "policy_digest", "binding_version", "status", "created_at",
        }
        assert connection.execute("SELECT created_at FROM central_question_approval_items").fetchone() is not None


def test_v9_open_approval_catalog_forwards_only_when_no_unprovable_terminal_receipt_exists(
    tmp_path: Path,
) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    database = tmp_path / "central.sqlite3"
    store.close()
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute("DROP TABLE central_question_feedback_receipts")
        connection.execute("DROP TABLE central_question_feedback_records")
        connection.execute(lifecycle_module._V9_TABLES[10])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V9_TABLES[11])  # pyright: ignore[reportPrivateUsage]

    migrate_central_question_lifecycle_schema(database)

    with sqlite3.connect(database) as connection:
        assert {row[1] for row in connection.execute(
            "PRAGMA table_info(central_question_approval_disposition_receipts)"
        )} >= {"receipt_id", "identity_session_id", "authority_policy_version", "authority_policy_digest"}


def _downgrade_resolved_disposition_to_v11(database: Path) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        receipts = tuple(connection.execute("SELECT * FROM central_question_approval_disposition_receipts"))
        audits = tuple(connection.execute("SELECT * FROM central_question_approval_disposition_audits"))
        connection.execute("DROP TABLE central_question_approval_disposition_audits")
        connection.execute("DROP TABLE central_question_approval_disposition_receipts")
        connection.execute("DROP TABLE central_question_feedback_audits")
        connection.execute("DROP TABLE central_question_feedback_receipts")
        connection.execute("DROP TABLE central_question_feedback_records")
        connection.execute(lifecycle_module._V11_TABLES[10])  # pyright: ignore[reportPrivateUsage]
        connection.execute(lifecycle_module._V11_TABLES[11])  # pyright: ignore[reportPrivateUsage]
        connection.executemany(
            "INSERT INTO central_question_approval_disposition_receipts(receipt_id,org_id,request_id,approval_item_id,actor_id,identity_session_id,authority_policy_version,authority_policy_digest,expected_approval_item_revision,expected_request_revision,decision_kind,decision_digest,edited_text,idempotency_key,candidate_digest,policy_digest,binding_version,terminal_kind,record_id,resolved_item_revision,terminal_request_revision,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                    tuple(
                        row[column]
                        for column in row.keys()
                        if column
                        not in {
                            "authority_proof_digest",
                            "authority_policy_revision_id",
                            "authority_policy_epoch",
                        }
                    )
                for row in receipts
            ],
        )
        connection.executemany(
            "INSERT INTO central_question_approval_disposition_audits(receipt_id,approval_item_id,request_id,actor_id,identity_session_id,authority_policy_version,authority_policy_digest,decision_kind,decision_digest,terminal_kind,record_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                tuple(row[column] for column in row.keys() if column != "authority_proof_digest")
                for row in audits
            ],
        )


def test_v11_resolved_disposition_migrates_to_a_canonical_authority_proof_commitment(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    app.dispose(ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-v11-forward",
    ))
    database = tmp_path / "central.sqlite3"
    store.close()
    _downgrade_resolved_disposition_to_v11(database)

    migrate_central_question_lifecycle_schema(database)

    with sqlite3.connect(database) as connection:
        receipt_digest = connection.execute(
            "SELECT authority_proof_digest FROM central_question_approval_disposition_receipts"
        ).fetchone()
        audit_digest = connection.execute(
            "SELECT authority_proof_digest FROM central_question_approval_disposition_audits"
        ).fetchone()
        assert receipt_digest is not None and audit_digest == receipt_digest
        assert len(receipt_digest[0]) == 64 and receipt_digest[0] == receipt_digest[0].lower()
    assert central_question_lifecycle_schema_ready(database) is True


def test_v11_authority_proof_commitment_migration_fault_rolls_back(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    ).dispose(ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-v11-fault",
    ))
    database = tmp_path / "central.sqlite3"
    store.close()
    _downgrade_resolved_disposition_to_v11(database)

    with pytest.raises(CentralQuestionLifecycleUnavailable):
        migrate_central_question_lifecycle_schema(
            database, fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError("fault"))
            if point == "v11-to-current-after-schema" else None,
        )

    with sqlite3.connect(database) as connection:
        assert "authority_proof_digest" not in {
            row[1] for row in connection.execute("PRAGMA table_info(central_question_approval_disposition_receipts)")
        }


@pytest.mark.parametrize("forged_digest", ["A" * 64, "g" * 64])
def test_approval_authority_proof_digest_rejects_noncanonical_receipt_and_audit_values(
    tmp_path: Path, forged_digest: str,
) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    command = ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-proof-digest",
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    app.dispose(command)
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_approval_disposition_receipts SET authority_proof_digest=?", (forged_digest,)
        )
        connection.execute(
            "UPDATE central_question_approval_disposition_audits SET authority_proof_digest=?", (forged_digest,)
        )
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        app.dispose(command)
    store.close()


def test_paired_historical_session_proof_tamper_breaks_the_recomputed_commitment(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    command = ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-paired-proof-tamper",
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    app.dispose(command)
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_approval_disposition_receipts SET identity_session_id='forged-session'"
        )
        connection.execute(
            "UPDATE central_question_approval_disposition_audits SET identity_session_id='forged-session'"
        )
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        app.dispose(command)
    store.close()


def test_resolved_disposition_receipt_and_audit_are_bidirectionally_fail_closed(tmp_path: Path) -> None:
    store, request = _awaiting_answer_for_ingest(tmp_path)
    pending = _ingest_application(store, IngestPolicy(kind="approval_required"), IngestAuthority()).ingest(
        _owner_answer_command(request)
    )
    app = ApprovalDispositionApplication(
        store=store, authority=DispositionAuthority(), record_id_factory=lambda: "approved-record",
        clock=lambda: NOW + timedelta(minutes=1),
    )
    app.dispose(ApprovalDispositionCommand(
        request_id="request-1", approval_item_id="approval-1", expected_approval_item_revision=1,
        expected_request_revision=pending.request.revision, principal=_approver_principal(), decision="approve",
        edited_text=None, idempotency_key="approval-audit-tamper",
    ))
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE central_question_approval_disposition_audits SET actor_id='forged'")
    assert central_question_lifecycle_schema_ready(database) is False
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE central_question_approval_disposition_audits SET actor_id='approver'")
        connection.execute("DELETE FROM central_question_approval_disposition_audits")
    assert central_question_lifecycle_schema_ready(database) is False
    store.close()


def test_answer_ingest_reverse_receipt_graph_rejects_fk_valid_extra_and_cross_request_tamper(
    tmp_path: Path,
) -> None:
    store, first_request = _awaiting_answer_for_ingest(tmp_path)
    _ingest_application(store, IngestPolicy(), IngestAuthority()).ingest(_owner_answer_command(first_request))
    second = CentralQuestionLifecycleApplication(
        store=store, router=Router(Routed(primary=_card(), intent="refund")), route_authority=RouteAuthority(),
        request_id_factory=lambda: "request-2", clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(minutes=5), manager_item_id_factory=lambda: "manager-2",
        root_manager_resolver=RootManagerResolver(), work_ticket_id_factory=lambda: "ticket-2",
        owner_delivery=OwnerDelivery(),
    )
    created = second.create(question="refund two", org_id="acme", requester_id="user", idempotency_key="ingest-2")
    second.process_received(created.request.request_id)
    second.process_ready_to_dispatch(created.request.request_id)
    second_request = store.get("request-2")
    assert second_request is not None and isinstance(second_request.state, AwaitingAnswer)
    database = tmp_path / "central.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        digest = connection.execute(
            "SELECT candidate_digest FROM central_question_answer_ingest_receipts WHERE ticket_id='ticket-1'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO central_question_answer_records(record_id,request_id,ticket_id,org_id,owner_id,agent_id,text,sources_json,mode,review_status,candidate_digest,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("extra-record", "request-2", "ticket-2", "acme", "owner", "refund", "extra",
             '["published/refund"]', "full", "not_required", digest, NOW.isoformat()),
        )
    assert central_question_lifecycle_schema_ready(database) is False
    with pytest.raises(CentralQuestionLifecycleUnavailable):
        store.get("request-2")
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM central_question_answer_records WHERE record_id='extra-record'")
        connection.execute(
            "UPDATE central_question_answer_ingest_receipts SET request_id='request-2' WHERE ticket_id='ticket-1'"
        )
    assert central_question_lifecycle_schema_ready(database) is False
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_answer_ingest_receipts SET request_id='request-1' WHERE ticket_id='ticket-1'"
        )
    assert central_question_lifecycle_schema_ready(database) is True
    store.close()


def test_two_store_processes_converge_on_one_manager_winner(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    migrate_central_question_lifecycle_schema(database)
    resolver = RootManagerResolver()
    first = CentralQuestionLifecycleStore(database, root_manager_resolver=resolver)
    second = CentralQuestionLifecycleStore(database, root_manager_resolver=resolver)
    creator_router = Router(Unowned(escalated_to="root", intent="refund"))
    concurrent_router = Router(
        Unowned(escalated_to="root", intent="refund"), after_route=Barrier(2)
    )
    def application_for(
        store: CentralQuestionLifecycleStore, manager_item_id: str, router: Router
    ) -> CentralQuestionLifecycleApplication:
        return CentralQuestionLifecycleApplication(
            store=store,
            router=router,
            route_authority=RouteAuthority(),
            request_id_factory=lambda: "request-1",
            clock=lambda: NOW,
            deadline=lambda _org, _state, at: at + timedelta(minutes=5),
            manager_item_id_factory=lambda: manager_item_id,
            root_manager_resolver=resolver,
        )
    creator = application_for(first, "manager-1", creator_router)
    creator.create(question="refund", org_id="acme", requester_id="user", idempotency_key="concurrent")
    left = application_for(first, "manager-1", concurrent_router)
    right = application_for(second, "manager-2", concurrent_router)
    barrier = Barrier(2)
    results: list[object] = []
    def run(application: CentralQuestionLifecycleApplication) -> None:
        barrier.wait()
        results.append(application.process_received("request-1"))
    threads = (Thread(target=run, args=(left,)), Thread(target=run, args=(right,)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 2 and all(isinstance(result.state, AwaitingManager) for result in results)  # type: ignore[union-attr]
    winner = first.manager_item_id("request-1")
    assert winner in {"manager-1", "manager-2"}
    assert {result.state.item_id for result in results} == {winner}  # type: ignore[union-attr]
    assert concurrent_router.calls == 2
    first.close()
    second.close()
