from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3

import pytest

from agent_org_network.central_card_ownership import (
    CardOwnerAssignmentApplication,
    CardOwnershipConflict,
    CardOwnershipUnavailable,
    CardOwnerChangeReceipt,
    RevokeCardOwner,
    TransferCardOwner,
    migrate_central_card_ownership_schema,
    ownership_schema_ready,
)
from agent_org_network.agent_card import AgentCard
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


class _Approval:
    calls = 0
    allowed = True

    def authorize(self, **_kwargs: object) -> bool:
        self.calls += 1
        return self.allowed


class _Mutation:
    calls: list[tuple[str, str | None]] = []

    def mutate(
        self, *, transaction: sqlite3.Connection, org_id: str, card_id: str,
        operation: str, from_owner_user_id: str, to_owner_user_id: str | None,
        expected_card_revision: int, expected_generation: int,
    ) -> bool:
        _ = transaction, org_id, card_id, from_owner_user_id, expected_card_revision, expected_generation
        self.calls.append((operation, to_owner_user_id))
        return True


class _UserRegistrationAuthorizer:
    def current(self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64)

    def verify_precommit(self, command: ProductionRegistryUserCommand, evidence: CurrentUserRegistrationAuthorization, transaction: sqlite3.Connection) -> bool:
        _ = command, evidence, transaction
        return True


class _CardRegistrationAuthorizer:
    def current(self, command: ProductionAgentCardCommand, transaction: sqlite3.Connection) -> CurrentCardRegistrationAuthorization:
        _ = command, transaction
        return CurrentCardRegistrationAuthorization(authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64)

    def verify_precommit(self, command: ProductionAgentCardCommand, evidence: CurrentCardRegistrationAuthorization, transaction: sqlite3.Connection) -> bool:
        _ = command, evidence, transaction
        return True


def _database(path: Path) -> None:
    SqliteProductionRegistryUsers.migrate(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserRegistrationAuthorizer())
    users.register(ProductionRegistryUserCommand(
        org_id="acme", principal_id="root", idempotency_key="user-root",
        expected_revision=0, user_id="root", email="root@example.test",
    ))
    users.register(ProductionRegistryUserCommand(
        org_id="acme", principal_id="root", idempotency_key="user-alice",
        expected_revision=1, user_id="alice", email="alice@example.test", manager_id="root",
    ))
    users.close()
    SqliteProductionAgentCards.migrate(path)
    cards = SqliteProductionAgentCards(path, authorize=_CardRegistrationAuthorizer())
    cards.register(ProductionAgentCardCommand(
        org_id="acme", principal_id="root", idempotency_key="card-support", expected_revision=2,
        card=AgentCard(
            agent_id="support", owner="root", team="support", summary="Support",
            domains=["support"], last_reviewed_at=datetime(2026, 8, 1).date(), maintainer="alice",
        ),
    ))
    cards.close()


def test_owner_assignment_backfill_transfer_revoke_and_graph_are_generation_bound(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _database(database)
    migrate_central_card_ownership_schema(database, clock=lambda: datetime(2026, 8, 1, tzinfo=UTC))
    assert ownership_schema_ready(database)
    app = CardOwnerAssignmentApplication(
        database, _Approval(), mutation=_Mutation(), clock=lambda: datetime(2026, 8, 1, tzinfo=UTC)
    )
    first = app.current(org_id="acme", card_id="support")
    assert first.generation == 1 and first.owner_user_id == "root"
    transferred = app.apply(
        org_id="acme", actor_user_id="root", card_id="support",
        command=TransferCardOwner(
            expected_card_revision=3, expected_generation=1, target_owner_user_id="alice",
            approval_evidence_id="evidence-transfer", approval_evidence_digest="b" * 64,
        ),
        idempotency_key="transfer-1",
    )
    assert isinstance(transferred, CardOwnerChangeReceipt)
    assert transferred.generation == 2 and transferred.owner_user_id == "alice"
    assert app.current(org_id="acme", card_id="support").generation == 2
    replay = app.apply(
        org_id="acme", actor_user_id="root", card_id="support",
        command=TransferCardOwner(
            expected_card_revision=3, expected_generation=1, target_owner_user_id="alice",
            approval_evidence_id="evidence-transfer", approval_evidence_digest="b" * 64,
        ),
        idempotency_key="transfer-1",
    )
    assert replay.replayed is True
    revoked = app.apply(
        org_id="acme", actor_user_id="root", card_id="support",
        command=RevokeCardOwner(
            expected_card_revision=3, expected_generation=2,
            approval_evidence_id="evidence-revoke", approval_evidence_digest="c" * 64,
        ),
        idempotency_key="revoke-1",
    )
    assert revoked.operation == "revoked"
    graph = app.graph(org_id="acme")
    assert graph.cards == ({
        "kind": "agent_card", "card_id": "support", "card_revision": 3,
        "assignment_status": "revoked", "assignment_generation": 2,
        "current_owner_user_id": None, "recorded_owner_user_id": "root", "team": "support",
    },)
    assert graph.edges == (
        {"from_id": "root", "to_id": "alice", "kind": "manages"},
        {"from_id": "alice", "to_id": "support", "kind": "maintains"},
    )
    assert app.scorecard(org_id="acme") == ()


def test_transfer_to_current_owner_is_rejected_without_writes(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _database(database)
    migrate_central_card_ownership_schema(database)
    approval = _Approval()
    app = CardOwnerAssignmentApplication(database, approval, mutation=_Mutation())
    before = app.current(org_id="acme", card_id="support")
    with pytest.raises(CardOwnershipConflict):
        app.apply(
            org_id="acme", actor_user_id="root", card_id="support",
            command=TransferCardOwner(
                expected_card_revision=3, expected_generation=1,
                target_owner_user_id="root", approval_evidence_id="evidence",
                approval_evidence_digest="b" * 64,
            ), idempotency_key="self-transfer",
        )
    assert app.current(org_id="acme", card_id="support") == before
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_card_owner_change_receipts"
        ).fetchone() == (0,)


def test_transfer_requires_same_uow_mutation_seam_by_default(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _database(database)
    migrate_central_card_ownership_schema(database)
    app = CardOwnerAssignmentApplication(database, _Approval())
    with pytest.raises(CardOwnershipUnavailable, match="same-UoW"):
        app.apply(
            org_id="acme", actor_user_id="root", card_id="support",
            command=TransferCardOwner(
                expected_card_revision=3, expected_generation=1,
                target_owner_user_id="alice", approval_evidence_id="evidence",
                approval_evidence_digest="b" * 64,
            ), idempotency_key="unavailable-transfer",
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_card_owner_change_receipts"
        ).fetchone() == (0,)


def test_idempotent_replay_rechecks_current_approval(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _database(database)
    migrate_central_card_ownership_schema(database)
    approval = _Approval()
    app = CardOwnerAssignmentApplication(database, approval, mutation=_Mutation())
    command = TransferCardOwner(
        expected_card_revision=3, expected_generation=1,
        target_owner_user_id="alice", approval_evidence_id="evidence-transfer",
        approval_evidence_digest="b" * 64,
    )
    first = app.apply(
        org_id="acme", actor_user_id="root", card_id="support",
        command=command, idempotency_key="replay-approval",
    )
    assert first.replayed is False
    approval.allowed = False
    with pytest.raises(CardOwnershipConflict):
        app.apply(
            org_id="acme", actor_user_id="root", card_id="support",
            command=command, idempotency_key="replay-approval",
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM central_card_owner_change_receipts"
        ).fetchone() == (1,)


def test_revoke_requires_same_uow_mutation_or_explicit_metadata_only(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _database(database)
    migrate_central_card_ownership_schema(database)
    app = CardOwnerAssignmentApplication(database, _Approval())
    with pytest.raises(CardOwnershipUnavailable, match="same-UoW"):
        app.apply(
            org_id="acme", actor_user_id="root", card_id="support",
            command=RevokeCardOwner(
                expected_card_revision=3, expected_generation=1,
                approval_evidence_id="evidence-revoke", approval_evidence_digest="c" * 64,
            ), idempotency_key="unavailable-revoke",
        )
    metadata_app = CardOwnerAssignmentApplication(database, _Approval(), metadata_only=True)
    result = metadata_app.apply(
        org_id="acme", actor_user_id="root", card_id="support",
        command=RevokeCardOwner(
            expected_card_revision=3, expected_generation=1,
            approval_evidence_id="evidence-revoke", approval_evidence_digest="c" * 64,
        ), idempotency_key="metadata-revoke",
    )
    assert result.operation == "revoked"


def test_migration_rejects_production_card_row_drift_before_owned_write(tmp_path: Path) -> None:
    database = tmp_path / "central.sqlite3"
    _database(database)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE production_agent_cards SET card_digest=? WHERE org_id=? AND agent_id=?",
            ("f" * 64, "acme", "support"),
        )
    with pytest.raises(CardOwnershipUnavailable):
        migrate_central_card_ownership_schema(database)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE 'central_card_owner_%'"
        ).fetchone() == (0,)
