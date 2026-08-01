"""RB3.2b.5-B durable Central ConflictCase/concurrence contract."""

from __future__ import annotations

from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
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
from agent_org_network.central_inbox_conflict import (
    ConflictAuthorizationProof,
    ConflictConcurrenceApplication,
    ConflictConcurrenceCommand,
    ConflictInboxApplication,
    ConflictNotFound,
    ConflictReadCommand,
    ConflictRouteAuthorization,
    ConflictSessionUnauthenticated,
    ConflictStaleOrConflict,
    ConflictUnavailable,
    FileReloadingConflictAuthority,
    central_inbox_conflict_schema_ready,
    migrate_central_inbox_conflict_schema,
)
from agent_org_network.central_question_lifecycle import (
    CardBinding,
    CentralQuestionLifecycleApplication,
    CentralQuestionLifecycleStore,
    migrate_central_question_lifecycle_schema,
)
from agent_org_network.decision import Contested
from agent_org_network.question_request import (
    AwaitingConflict,
    AwaitingManager,
    DeclinedRequest,
    ReadyToDispatch,
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


NOW = datetime(2026, 7, 31, 13, 0, tzinfo=UTC)


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


class _Router:
    def __init__(
        self, candidates: tuple[AgentCard, ...] | None = None
    ) -> None:
        self._candidates = candidates or (
            _card("refund-a", "owner-a"),
            _card("refund-b", "owner-b"),
        )

    def route(self, question: str) -> Contested:
        assert question
        return Contested(
            candidates=self._candidates,
            intent="refund",
        )


class _RouteAuthority:
    def authorize_route(
        self, org_id: str, intent: str, agent_id: str, transaction: sqlite3.Connection | None
    ) -> str:
        assert org_id == "acme" and intent == "refund" and agent_id
        assert transaction is not None and transaction.in_transaction
        return "route-v1"

    def authorize_manager(
        self, org_id: str, manager_id: str, transaction: sqlite3.Connection | None
    ) -> str:
        assert org_id == "acme" and manager_id
        assert transaction is not None and transaction.in_transaction
        return "manager-v1"


class _Root:
    def resolve_root_manager(self, org_id: str, transaction: sqlite3.Connection) -> str:
        assert org_id == "acme" and transaction.in_transaction
        return "root"


class _Cards:
    def resolve_card_binding(
        self, org_id: str, agent_id: str, transaction: sqlite3.Connection
    ) -> CardBinding:
        assert org_id == "acme" and transaction.in_transaction
        owner = {
            "refund-a": "owner-a",
            "refund-a2": "owner-a",
            "refund-b": "owner-b",
        }[agent_id]
        digest = sha256(f"{agent_id}:{owner}:1".encode()).hexdigest()
        return CardBinding(
            agent_id=agent_id,
            owner_id=owner,
            revision=1,
            card_digest=digest,
            concept_ref="refund",
            coverage_digest=sha256(f"refund:{digest}".encode()).hexdigest(),
        )


class _UserRegistrationAuthorizer:
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


class _CardRegistrationAuthorizer:
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


def _seed_current_cards(database: Path, candidates: tuple[AgentCard, ...]) -> None:
    SqliteProductionRegistryUsers.migrate(database)
    users = SqliteProductionRegistryUsers(
        database, authorize=_UserRegistrationAuthorizer()
    )
    owners = tuple(sorted({candidate.owner for candidate in candidates}))
    for revision, owner in enumerate(owners):
        users.register(
            ProductionRegistryUserCommand(
                org_id="acme",
                principal_id="root",
                idempotency_key=f"seed-user-{owner}",
                expected_revision=revision,
                user_id=owner,
                email=f"{owner}@acme.example",
            )
        )
    users.close()
    SqliteProductionAgentCards.migrate(database)
    cards = SqliteProductionAgentCards(
        database, authorize=_CardRegistrationAuthorizer()
    )
    revision = len(owners)
    for candidate in candidates:
        cards.register(
            ProductionAgentCardCommand(
                org_id="acme",
                principal_id=candidate.owner,
                idempotency_key=f"seed-card-{candidate.agent_id}",
                expected_revision=revision,
                card=candidate,
            )
        )
        revision += 1
    cards.close()


def _legacy_conflict(
    database: Path,
    *,
    candidates: tuple[AgentCard, ...] | None = None,
    seed_current_cards: bool = True,
) -> None:
    selected = candidates or (
        _card("refund-a", "owner-a"),
        _card("refund-b", "owner-b"),
    )
    migrate_central_question_lifecycle_schema(database)
    store = CentralQuestionLifecycleStore(
        database, root_manager_resolver=_Root(), card_binding_resolver=_Cards()
    )
    app = CentralQuestionLifecycleApplication(
        store=store,
        router=_Router(selected),
        route_authority=_RouteAuthority(),
        request_id_factory=lambda: "request-1",
        clock=lambda: NOW,
        deadline=lambda _org, _state, at: at + timedelta(hours=1),
        manager_item_id_factory=lambda: "manager-initial",
        root_manager_resolver=_Root(),
        conflict_case_id_factory=lambda: "case-1",
    )
    created = app.create(
        question="refund conflict",
        org_id="acme",
        requester_id="requester",
        idempotency_key="create-conflict",
    )
    request = app.process_received(created.request.request_id)
    assert isinstance(request.state, AwaitingConflict)
    store.close()
    if seed_current_cards:
        _seed_current_cards(database, selected)


class _ConflictAuthority:
    def __init__(
        self,
        *,
        route: str = "allowed",
        allowed: bool = True,
        manager_id: str = "manager",
    ) -> None:
        self.route = route
        self.allowed = allowed
        self.precommit_allowed = allowed
        self.bindings_current = True
        self.manager_id = manager_id
        self.transaction_states: list[bool] = []

    def authorize_read(
        self,
        command: ConflictReadCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ConflictAuthorizationProof:
        return self._proof(command, resource, "conflict.list", transaction)

    def issue_concurrence_proof(
        self,
        command: ConflictConcurrenceCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> ConflictAuthorizationProof:
        return self._proof(command, resource, "conflict.concur", transaction)

    def verify_concurrence_proof(
        self,
        proof: ConflictAuthorizationProof,
        command: ConflictConcurrenceCommand,
        resource: ResourceRef,
        transaction: sqlite3.Connection,
    ) -> bool:
        self.transaction_states.append(transaction.in_transaction)
        return (
            self.precommit_allowed
            and proof.principal.subject_id == command.expected_actor_id
            and proof.action_grant.resource == resource
            and transaction.in_transaction
        )

    def current_candidate_bindings(
        self,
        org_id: str,
        candidates: tuple[object, ...],
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = candidates
        return (
            org_id == "acme"
            and transaction.in_transaction
            and self.precommit_allowed
            and self.bindings_current
        )

    def authorize_route_target(
        self,
        org_id: str,
        intent: str,
        primary_card_id: str,
        complement_card_ids: tuple[str, ...],
        transaction: sqlite3.Connection,
    ) -> ConflictRouteAuthorization:
        assert org_id == "acme" and intent == "refund" and transaction.in_transaction
        if self.route == "rejected":
            return ConflictRouteAuthorization(kind="rejected", route=None)
        return ConflictRouteAuthorization(
            kind="allowed",
            route=RouteTarget(
                intent=intent,
                agent_id=primary_card_id,
                requires_approval=False,
                authority_version="route-v1:" + ",".join(complement_card_ids),
            ),
        )

    def resolve_authorized_deadlock_manager(
        self,
        org_id: str,
        owner_user_ids: tuple[str, ...],
        item_id: str,
        transaction: sqlite3.Connection,
    ) -> str:
        assert org_id == "acme" and owner_user_ids == ("owner-a", "owner-b")
        assert item_id and transaction.in_transaction
        return self.manager_id

    def verify_deadlock_manager(
        self,
        org_id: str,
        manager_id: str,
        item_id: str,
        transaction: sqlite3.Connection,
    ) -> bool:
        return (
            org_id == "acme"
            and manager_id == self.manager_id
            and bool(item_id)
            and transaction.in_transaction
            and self.precommit_allowed
        )

    def _proof(
        self,
        command: ConflictReadCommand | ConflictConcurrenceCommand,
        resource: ResourceRef,
        action: str,
        transaction: sqlite3.Connection,
    ) -> ConflictAuthorizationProof:
        self.transaction_states.append(transaction.in_transaction)
        if not self.allowed or not transaction.in_transaction:
            raise ConflictUnavailable()
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
        return ConflictAuthorizationProof(
            principal=principal,
            session_grant=AuthorizationGrant(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
                action="session.read",
                resource=session_resource,
                roles=("owner",),
                policy_version="conflict-v1",
                policy_digest="a" * 64,
            ),
            action_grant=AuthorizationGrant(
                org_id=principal.org_id,
                subject_id=principal.subject_id,
                action=action,  # type: ignore[arg-type]
                resource=resource,
                roles=("owner",),
                policy_version="conflict-v1",
                policy_digest="a" * 64,
            ),
        )


def _read(actor: str) -> ConflictReadCommand:
    return ConflictReadCommand(
        identity_session_id=sha256(f"session:{actor}".encode()).hexdigest(),
        expected_org_id="acme",
        expected_actor_id=actor,
    )


def _command(
    actor: str,
    card_id: str,
    *,
    expected_case_revision: int,
    expected_request_revision: int = 1,
    key: str | None = None,
) -> ConflictConcurrenceCommand:
    return ConflictConcurrenceCommand(
        case_id="case-1",
        identity_session_id=sha256(f"session:{actor}".encode()).hexdigest(),
        expected_org_id="acme",
        expected_actor_id=actor,
        on_candidate_card_id=card_id,
        stance="keep_as_complement",
        rationale=f"{actor} concurrence",
        expected_case_revision=expected_case_revision,
        expected_request_revision=expected_request_revision,
        expected_round=1,
        idempotency_key=key or f"vote-{actor}",
    )


def _apps(
    database: Path,
    authority: _ConflictAuthority,
) -> tuple[ConflictInboxApplication, ConflictConcurrenceApplication]:
    return (
        ConflictInboxApplication(database_path=database, authority=authority),
        ConflictConcurrenceApplication(
            database_path=database,
            authority=authority,
            receipt_id_factory=lambda: "receipt-" + sha256(str(datetime.now()).encode()).hexdigest()[:12],
            manager_item_id_factory=lambda: "deadlock-manager-1",
            clock=lambda: NOW + timedelta(minutes=1),
        ),
    )


def test_v14_conflict_migrates_marker_last_with_snapshot_and_metadata_only_grants(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)

    def fail(point: str) -> None:
        if point == "v14-to-v15-before-marker":
            raise RuntimeError("fault")

    with pytest.raises(ConflictUnavailable):
        migrate_central_inbox_conflict_schema(database, fault_injector=fail)
    assert central_inbox_conflict_schema_ready(database) is False
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT candidates_json FROM central_question_conflict_cases WHERE case_id='case-1'"
        ).fetchone() is not None
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='central_inbox_component_schema'"
        ).fetchone() is None

    migrate_central_inbox_conflict_schema(database)
    assert central_inbox_conflict_schema_ready(database) is True
    inbox, _concurrence = _apps(database, _ConflictAuthority())
    detail = inbox.detail(_read("owner-a"), "case-1")
    assert detail is not None
    assert detail.state == "open"
    assert detail.expected_case_revision == 1
    assert detail.expected_request_revision == 1
    assert detail.expected_round == 1
    assert {candidate.owner_user_id for candidate in detail.candidates} == {"owner-a", "owner-b"}
    assert len(detail.evidence_grants) == 2
    assert all(grant.status == "available" and grant.single_use for grant in detail.evidence_grants)
    assert "raw" not in repr(detail).casefold()
    assert "location" not in repr(detail).casefold()


@pytest.mark.parametrize("drift", ("missing", "transfer", "digest", "revision"))
def test_v14_conflict_migration_requires_exact_current_production_card_binding(
    tmp_path: Path, drift: str
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database, seed_current_cards=drift != "missing")
    if drift != "missing":
        with sqlite3.connect(database) as connection:
            if drift == "transfer":
                connection.execute(
                    "UPDATE production_agent_cards SET owner_id='owner-b' "
                    "WHERE org_id='acme' AND agent_id='refund-a'"
                )
            elif drift == "digest":
                connection.execute(
                    "UPDATE production_agent_cards SET card_digest=? "
                    "WHERE org_id='acme' AND agent_id='refund-a'",
                    ("f" * 64,),
                )
            else:
                connection.execute(
                    "UPDATE production_agent_cards SET revision=revision+100 "
                    "WHERE org_id='acme' AND agent_id='refund-a'"
                )

    with pytest.raises(ConflictUnavailable):
        migrate_central_inbox_conflict_schema(database)

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT case_id FROM central_question_conflict_cases"
        ).fetchone() == ("case-1",)
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name='central_inbox_component_schema'"
        ).fetchone() is None


def test_conflict_list_and_detail_hide_foreign_nonparticipant(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    inbox, _concurrence = _apps(database, _ConflictAuthority())

    assert [item.case_id for item in inbox.list(_read("owner-a"))] == ["case-1"]
    assert inbox.detail(_read("owner-a"), "case-1") is not None
    assert inbox.list(_read("foreign")) == ()
    assert inbox.detail(_read("foreign"), "case-1") is None


@pytest.mark.parametrize("change", ("transfer", "revoke"))
def test_current_card_transfer_or_revoke_hides_old_owner_conflict(
    tmp_path: Path, change: str
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    inbox, _concurrence = _apps(database, authority)
    assert len(inbox.list(_read("owner-a"))) == 1
    assert inbox.detail(_read("owner-a"), "case-1") is not None

    authority.bindings_current = False

    assert inbox.list(_read("owner-a")) == ()
    assert inbox.detail(_read("owner-a"), "case-1") is None


def test_partial_then_unanimous_concurrence_resolves_agreed_and_replay_is_write_zero(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    inbox, concurrence = _apps(database, authority)

    first = concurrence.concur(
        _command("owner-a", "refund-a", expected_case_revision=1)
    )
    assert first.outcome == "still_open"
    assert first.case_revision == 2 and first.request_revision == 1
    second_command = _command("owner-b", "refund-a", expected_case_revision=2)
    second = concurrence.concur(second_command)
    assert second.outcome == "agreed"
    assert second.case_revision == 3 and second.request_revision == 2
    lifecycle = CentralQuestionLifecycleStore(
        database, root_manager_resolver=_Root(), card_binding_resolver=_Cards()
    )
    agreed_request = lifecycle.get("request-1")
    assert agreed_request is not None and isinstance(
        agreed_request.state, ReadyToDispatch
    )
    assert agreed_request.state.route.authority_version == "route-v1:refund-b"
    lifecycle.close()
    replay = concurrence.concur(second_command)
    assert replay == replace(second, replayed=True)
    detail = inbox.detail(_read("owner-a"), "case-1")
    assert detail is not None and detail.state == "resolved"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state_kind,state_json,revision FROM question_requests WHERE request_id='request-1'"
        ).fetchone()
        assert row is not None and row[0] == "ready_to_dispatch" and row[2] == 2
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_concurrences"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_receipts"
        ).fetchone() == (2,)


@pytest.mark.parametrize(
    ("route_kind", "second_card", "outcome", "request_state"),
    (
        ("rejected", "refund-a", "route_rejected", DeclinedRequest),
        ("allowed", "refund-b", "deadlocked", AwaitingManager),
    ),
)
def test_complete_round_route_rejection_or_divergence_has_one_exact_terminal(
    tmp_path: Path,
    route_kind: str,
    second_card: str,
    outcome: str,
    request_state: type[object],
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority(route=route_kind)
    _inbox, concurrence = _apps(database, authority)

    concurrence.concur(_command("owner-a", "refund-a", expected_case_revision=1))
    result = concurrence.concur(
        _command("owner-b", second_card, expected_case_revision=2)
    )

    assert result.outcome == outcome
    store = CentralQuestionLifecycleStore(
        database, root_manager_resolver=_Root(), card_binding_resolver=_Cards()
    )
    request = store.get("request-1")
    assert request is not None and isinstance(request.state, request_state)
    if outcome == "deadlocked":
        assert isinstance(request.state, AwaitingManager)
        assert request.state.public_kind == "contested"
        assert store.manager_item_id("request-1") == "deadlock-manager-1"
    else:
        assert isinstance(request.state, DeclinedRequest)
        assert request.state.reason_code == "route_rejected"
    store.close()


def test_revoked_precommit_and_fault_leave_no_vote_receipt_or_request_change(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    authority.precommit_allowed = False
    _inbox, concurrence = _apps(database, authority)

    with pytest.raises(ConflictUnavailable):
        concurrence.concur(_command("owner-a", "refund-a", expected_case_revision=1))

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_concurrences"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_receipts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT state_kind,revision FROM question_requests WHERE request_id='request-1'"
        ).fetchone() == ("awaiting_conflict", 1)


def test_distinct_owner_is_one_participant_even_when_owner_has_multiple_candidates(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(
        database,
        candidates=(
            _card("refund-a", "owner-a"),
            _card("refund-a2", "owner-a"),
            _card("refund-b", "owner-b"),
        ),
    )
    migrate_central_inbox_conflict_schema(database)
    _inbox, concurrence = _apps(database, _ConflictAuthority())

    first = concurrence.concur(
        _command("owner-a", "refund-a2", expected_case_revision=1)
    )
    second = concurrence.concur(
        _command("owner-b", "refund-a2", expected_case_revision=2)
    )

    assert first.outcome == "still_open"
    assert second.outcome == "agreed"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_concurrences"
        ).fetchone() == (2,)


def test_changed_replay_and_stale_case_revision_are_conflict(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    _inbox, concurrence = _apps(database, _ConflictAuthority())
    command = _command("owner-a", "refund-a", expected_case_revision=1)
    concurrence.concur(command)

    with pytest.raises(ConflictStaleOrConflict):
        concurrence.concur(
            replace(command, rationale="changed")
        )
    with pytest.raises(ConflictStaleOrConflict):
        concurrence.concur(
            _command(
                "owner-b",
                "refund-a",
                expected_case_revision=1,
                key="stale-owner-b",
            )
        )


def test_identical_concurrence_concurrency_converges_to_fresh_and_replay(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    command = _command("owner-a", "refund-a", expected_case_revision=1)

    def invoke(_index: int):
        return _apps(database, authority)[1].concur(command)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(invoke, range(2)))

    assert sorted(result.replayed for result in results) == [False, True]
    assert len({result.receipt_id for result in results}) == 1
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_concurrences"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_receipts"
        ).fetchone() == (1,)


def test_terminal_fault_rolls_back_vote_request_case_manager_and_receipt(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    _inbox, first = _apps(database, authority)
    first.concur(_command("owner-a", "refund-a", expected_case_revision=1))
    failing = ConflictConcurrenceApplication(
        database_path=database,
        authority=authority,
        receipt_id_factory=lambda: "receipt-fault",
        manager_item_id_factory=lambda: "deadlock-manager-fault",
        clock=lambda: NOW + timedelta(minutes=1),
        fault_injector=lambda point: (
            (_ for _ in ()).throw(RuntimeError("fault"))
            if point == "after-conflict-request-cas"
            else None
        ),
    )

    with pytest.raises(ConflictUnavailable):
        failing.concur(
            _command(
                "owner-b",
                "refund-b",
                expected_case_revision=2,
                key="fault-owner-b",
            )
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state_kind,revision FROM question_requests WHERE request_id='request-1'"
        ).fetchone() == ("awaiting_conflict", 1)
        assert connection.execute(
            "SELECT state,revision FROM central_inbox_conflict_cases WHERE case_id='case-1'"
        ).fetchone() == ("open", 2)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_concurrences"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM central_question_manager_items"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_receipts"
        ).fetchone() == (1,)


@pytest.mark.parametrize("terminal", ("route", "deadlock"))
def test_terminal_final_precommit_policy_or_graph_drift_rolls_back_every_write(
    tmp_path: Path, terminal: str
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    _inbox, first = _apps(database, authority)
    first.concur(_command("owner-a", "refund-a", expected_case_revision=1))

    def drift(point: str) -> None:
        if point == "before-conflict-receipt-commit":
            if terminal == "route":
                authority.route = "rejected"
            else:
                authority.manager_id = "changed-manager"

    application = ConflictConcurrenceApplication(
        database_path=database,
        authority=authority,
        receipt_id_factory=lambda: f"receipt-{terminal}-drift",
        manager_item_id_factory=lambda: f"manager-item-{terminal}-drift",
        clock=lambda: NOW + timedelta(minutes=1),
        fault_injector=drift,
    )
    second_card = "refund-a" if terminal == "route" else "refund-b"

    with pytest.raises(ConflictUnavailable):
        application.concur(
            _command(
                "owner-b",
                second_card,
                expected_case_revision=2,
                key=f"{terminal}-drift",
            )
        )

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state_kind,revision FROM question_requests WHERE request_id='request-1'"
        ).fetchone() == ("awaiting_conflict", 1)
        assert connection.execute(
            "SELECT state,revision FROM central_inbox_conflict_cases WHERE case_id='case-1'"
        ).fetchone() == ("open", 2)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_concurrences"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM central_question_manager_items"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM central_inbox_conflict_receipts"
        ).fetchone() == (1,)


def test_restart_replay_reauthorizes_and_revoked_session_leaks_no_result(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    command = _command("owner-a", "refund-a", expected_case_revision=1)
    first = _apps(database, authority)[1].concur(command)

    replayed = _apps(database, authority)[1].concur(command)
    assert replayed == replace(first, replayed=True)
    authority.allowed = False
    with pytest.raises(ConflictUnavailable):
        _apps(database, authority)[1].concur(command)


def test_catalog_trigger_and_reverse_link_tamper_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    assert central_inbox_conflict_schema_ready(database) is True
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE central_inbox_conflict_evidence_grants SET status='consumed'"
            )
        connection.execute("DROP TRIGGER central_inbox_conflict_receipts_no_update")
    assert central_inbox_conflict_schema_ready(database) is False


def test_deadlock_link_must_exactly_join_current_manager_item_and_request(
    tmp_path: Path,
) -> None:
    database = tmp_path / "central.sqlite3"
    _legacy_conflict(database)
    migrate_central_inbox_conflict_schema(database)
    authority = _ConflictAuthority()
    _inbox, concurrence = _apps(database, authority)
    concurrence.concur(_command("owner-a", "refund-a", expected_case_revision=1))
    concurrence.concur(_command("owner-b", "refund-b", expected_case_revision=2))
    assert central_inbox_conflict_schema_ready(database) is True

    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE central_question_manager_items SET manager_id='tampered-manager' "
            "WHERE request_id='request-1'"
        )

    assert central_inbox_conflict_schema_ready(database) is False
    with pytest.raises(ConflictUnavailable):
        _apps(database, authority)[0].list(_read("owner-a"))


def test_production_adapter_distinguishes_unauthenticated_from_dependency_and_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    authority = FileReloadingConflictAuthority(
        authority_policy_path=tmp_path / "routing_rules.yaml",
        configured_org_id="acme",
        clock=lambda: NOW,
    )
    command = _read("owner-a")
    resource = ResourceRef(
        org_id="acme",
        kind="conflict_inbox",
        resource_id="owner-a",
        owner_subject_id="owner-a",
    )
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row

    def no_current_session(
        _connection: sqlite3.Connection, _session_digest: str, *, now: datetime
    ) -> None:
        _ = now
        return None

    monkeypatch.setattr(
        "agent_org_network.central_browser_auth_sqlite.read_current_browser_session_connection",
        no_current_session,
    )
    with pytest.raises(ConflictSessionUnauthenticated):
        authority.authorize_read(command, resource, connection)

    def unavailable_session_dependency(
        _connection: sqlite3.Connection, _session_digest: str, *, now: datetime
    ) -> None:
        _ = now
        raise RuntimeError("dependency")

    monkeypatch.setattr(
        "agent_org_network.central_browser_auth_sqlite.read_current_browser_session_connection",
        unavailable_session_dependency,
    )
    with pytest.raises(ConflictUnavailable) as unavailable:
        authority.authorize_read(command, resource, connection)
    assert type(unavailable.value) is ConflictUnavailable

    with pytest.raises(ConflictNotFound):
        authority.authorize_read(replace(command, expected_org_id="foreign"), resource, connection)
    connection.close()
