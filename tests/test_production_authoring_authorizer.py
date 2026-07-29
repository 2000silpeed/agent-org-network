# pyright: reportUnknownParameterType=false, reportMissingParameterType=false
# pyright: reportArgumentType=false, reportUnknownArgumentType=false
# pyright: reportUnknownLambdaType=false

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest
from pydantic import SecretStr
import yaml

from agent_org_network.central_authority import (
    AuthorizationGrant,
    AuthorityPolicySnapshot,
    SnapshotCentralAuthorizer,
    canonical_policy_digest,
    load_authority_policy_yaml,
)
from agent_org_network.production_authoring_authorizer import (
    ProductionCentralTxCurrentAuthoringAuthorizer,
)
from agent_org_network.production_authoring_identity import (
    AuthoringIdentitySessionRef,
    AuthoringInvocation,
    ProductionAuthoringIdentityVerifier,
)
from agent_org_network.production_identity_sessions import (
    SqliteProductionIdentitySessions,
    VerifiedEmailIdentityProof,
)
from agent_org_network.sqlite_production_agent_cards import (
    CurrentCardRegistrationAuthorization,
    ProductionAgentCardCommand,
    SqliteProductionAgentCards,
)
from agent_org_network.sqlite_production_authoring_runs import (
    AuthoringRunResource,
    AuthoringSourceRef,
    CompleteAuthoringRunCommand,
    ProductionAuthoringRunDenied,
    ProductionAuthoringRunConflict,
    ReviewAuthoringRunCommand,
    SqliteProductionAuthoringRuns,
    StartAuthoringRunCommand,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)


class _UserAllow:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction: sqlite3.Connection) -> bool:
        return True


class _CardAllow:
    def current(self, command, transaction: sqlite3.Connection):
        return CurrentCardRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(self, command, evidence, transaction: sqlite3.Connection) -> bool:
        return True


def _policy(
    *, action: str | tuple[str, ...] = "author.write", version: str = "policy-v1"
):
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": version,
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "owner", "roles": ["owner"]}
        ],
        "role_permissions": [{
            "role": "owner",
            "actions": [action] if type(action) is str else list(action),
        }],
        "route_rules": [],
        "worker_bindings": [],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    return load_authority_policy_yaml(
        yaml.safe_dump(document, sort_keys=False), expected_org_id="acme"
    )


def _fixture(path: Path):
    SqliteProductionRegistryUsers.migrate(path)
    users = SqliteProductionRegistryUsers(path, authorize=_UserAllow())
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme", principal_id="owner", idempotency_key="user-1",
            expected_revision=0, user_id="owner", email="owner@example.com",
        )
    )
    SqliteProductionAgentCards.migrate(path)
    cards = SqliteProductionAgentCards(path, authorize=_CardAllow())
    cards.register(
        ProductionAgentCardCommand(
            org_id="acme", principal_id="owner", idempotency_key="card-1",
            expected_revision=1,
            card={
                "agent_id": "support", "owner": "owner", "team": "support",
                "summary": "support", "domains": ["support"],
                "last_reviewed_at": "2026-07-28", "maintainer": None,
                "can_answer": [], "cannot_answer": [], "approval_when": [],
                "collaborate_when": [], "knowledge_sources": [], "trust_labels": [],
            },
        )
    )
    SqliteProductionIdentitySessions.migrate(path)
    now = datetime(2026, 7, 28, tzinfo=UTC)
    sessions = SqliteProductionIdentitySessions(
        path, registry=users, configured_org_id="acme", provider_id="corp",
        issuer="https://id.example", clock=lambda: now,
        _identity_session_id_factory=lambda: "s" * 32,
    )
    sessions.establish(
        VerifiedEmailIdentityProof(
            provider_id="corp", issuer="https://id.example",
            email="owner@example.com", email_verified=True,
        ),
        expires_at=now + timedelta(hours=1),
    )
    SqliteProductionAuthoringRuns.migrate(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    row = connection.execute(
        "SELECT revision,card_digest FROM production_agent_cards "
        "WHERE org_id='acme' AND agent_id='support'"
    ).fetchone()
    command = StartAuthoringRunCommand(
        org_id="acme", principal_id="owner", idempotency_key="author-1",
        agent_id="support", expected_card_revision=row[0],
        expected_card_digest=row[1],
        sources=(AuthoringSourceRef(
            source_digest="c" * 64, byte_size=12, media_type="text/markdown"
        ),),
    )
    resource = AuthoringRunResource(
        org_id="acme", agent_id="support", owner_id="owner",
        card_revision=row[0], card_digest=row[1],
    )
    invocation = AuthoringInvocation(
        session=AuthoringIdentitySessionRef(value=SecretStr("s" * 32)),
        org_id="acme", principal_id="owner", identity_provider="corp",
    )
    return connection, now, command, resource, invocation


def _authorizer(
    snapshot: AuthorityPolicySnapshot,
    now: datetime,
    *,
    provider=None,
) -> ProductionCentralTxCurrentAuthoringAuthorizer:
    return ProductionCentralTxCurrentAuthoringAuthorizer(
        policy_snapshot=provider or (lambda: snapshot),
        central_authorizer=SnapshotCentralAuthorizer(snapshot),
        identity_verifier=ProductionAuthoringIdentityVerifier(
            provider_id="corp", issuer="https://id.example", clock=lambda: now
        ),
    )


def test_author_write는_same_tx_sealed_current_evidence_write0이다(tmp_path: Path) -> None:
    connection, now, command, resource, invocation = _fixture(tmp_path / "db.sqlite")
    snapshot = _policy()
    authorizer = _authorizer(snapshot, now)
    before = connection.total_changes
    evidence = authorizer.current(command, resource, "d" * 64, invocation, connection)
    assert evidence.policy_version == snapshot.policy_version
    assert evidence.policy_digest == snapshot.content_sha256
    assert authorizer.verify_precommit(
        command, resource, "d" * 64, evidence, invocation, connection
    )
    assert connection.total_changes == before
    assert "s" * 32 not in repr(evidence)


@pytest.mark.parametrize("drift", ["deny", "policy", "card", "session", "user"])
def test_deny와_current_drift는_failclosed_write0(tmp_path: Path, drift: str) -> None:
    connection, now, command, resource, invocation = _fixture(tmp_path / "db.sqlite")
    snapshot = _policy(action="author.read") if drift == "deny" else _policy()
    current = [snapshot]
    authorizer = _authorizer(snapshot, now, provider=lambda: current[0])
    if drift == "policy":
        current[0] = _policy(version="policy-v2")
    elif drift == "card":
        connection.execute(
            "UPDATE production_agent_cards SET card_digest=? WHERE org_id='acme'",
            ("e" * 64,),
        )
    elif drift == "session":
        connection.execute("UPDATE production_identity_sessions SET active=0")
    elif drift == "user":
        connection.execute(
            "UPDATE production_registry_users SET email='drift@example.com'"
        )
    connection.commit()
    before = connection.total_changes
    with pytest.raises(ProductionAuthoringRunDenied):
        authorizer.current(command, resource, "d" * 64, invocation, connection)
    assert connection.total_changes == before


def test_provider_absent_bad와_unsealed_authorizer_injection은startup_failclosed(
    tmp_path: Path,
) -> None:
    _connection, now, _command, _resource, _invocation = _fixture(
        tmp_path / "db.sqlite"
    )
    snapshot = _policy()
    verifier = ProductionAuthoringIdentityVerifier(
        provider_id="corp", issuer="https://id.example", clock=lambda: now
    )
    for provider in (None, object(), lambda: object()):
        with pytest.raises(ProductionAuthoringRunDenied):
            ProductionCentralTxCurrentAuthoringAuthorizer(
                policy_snapshot=provider,  # type: ignore[arg-type]
                central_authorizer=SnapshotCentralAuthorizer(snapshot),
                identity_verifier=verifier,
            )
    with pytest.raises(ProductionAuthoringRunDenied):
        ProductionCentralTxCurrentAuthoringAuthorizer(
            policy_snapshot=lambda: snapshot,
            central_authorizer=object(),  # type: ignore[arg-type]
            identity_verifier=verifier,
        )


def test_complete_current는_ghost_run을deny한다(tmp_path: Path) -> None:
    connection, now, _command, resource, invocation = _fixture(
        tmp_path / "db.sqlite"
    )
    command = CompleteAuthoringRunCommand(
        organization_id="acme",
        principal_id="owner",
        idempotency_key="complete-1",
        run_id="ar_" + "a" * 64,
        expected_revision=0,
        expected_card_revision=resource.card_revision,
        expected_card_digest=resource.card_digest,
        admitted_bundle_digest="e" * 64,
        document_count=1,
        edge_count=0,
        dropped_count=0,
        author_profile_digest="f" * 64,
    )
    before = connection.total_changes
    with pytest.raises(ProductionAuthoringRunDenied):
        _authorizer(_policy(), now).current(
            command, resource, "d" * 64, invocation, connection
        )
    assert connection.total_changes == before


def test_review_same_receipt_replay만_reviewed에서_current_authorize된다(
    tmp_path: Path,
) -> None:
    path = tmp_path / "db.sqlite"
    connection, now, start, _resource, invocation = _fixture(path)
    connection.close()
    snapshots = [_policy(action=("author.write", "author.publish"))]
    store = SqliteProductionAuthoringRuns(
        path, authorize=_authorizer(snapshots[0], now, provider=lambda: snapshots[0])
    )
    started = store.start(start, invocation=invocation).run
    awaiting = store.complete(
        CompleteAuthoringRunCommand(
            organization_id="acme", principal_id="owner", idempotency_key="complete-1",
            run_id=started.run_id, expected_revision=0,
            expected_card_revision=started.card_revision,
            expected_card_digest=started.card_digest,
            admitted_bundle_digest="e" * 64, document_count=1, edge_count=0,
            dropped_count=0, author_profile_digest="f" * 64,
        ),
        invocation=invocation,
    ).run
    review = ReviewAuthoringRunCommand(
        organization_id="acme", principal_id="owner", idempotency_key="review-1",
        run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
        expected_card_digest=awaiting.card_digest,
        source_digest=awaiting.source_set_digest,
        draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
    )
    assert store.review(review, invocation=invocation).replayed is False
    assert store.review(review, invocation=invocation).replayed is True

    with pytest.raises(ProductionAuthoringRunConflict):
        store.review(
            review.model_copy(update={"idempotency_key": "review-2"}),
            invocation=invocation,
        )
    with pytest.raises(ProductionAuthoringRunConflict):
        store.review(
            review.model_copy(update={"outcome": "Rejected"}), invocation=invocation
        )

    snapshots[0] = _policy(
        action=("author.write", "author.publish"), version="policy-v2"
    )
    with pytest.raises(ProductionAuthoringRunDenied):
        store.review(review, invocation=invocation)
    snapshots[0] = _policy(action=("author.write", "author.publish"))
    drift = sqlite3.connect(path)
    drift.execute(
        "UPDATE production_agent_cards SET card_digest=? "
        "WHERE org_id='acme' AND agent_id='support'",
        ("a" * 64,),
    )
    drift.commit()
    drift.close()
    with pytest.raises(ProductionAuthoringRunConflict):
        store.review(review, invocation=invocation)


def test_review_precommit_gap은_original_command에만_결속된다(tmp_path: Path) -> None:
    path = tmp_path / "db.sqlite"
    connection, now, start, _resource, invocation = _fixture(path)
    connection.close()
    store = SqliteProductionAuthoringRuns(path, authorize=_authorizer(_policy(action=("author.write", "author.publish")), now))
    started = store.start(start, invocation=invocation).run
    awaiting = store.complete(
        CompleteAuthoringRunCommand(
            organization_id="acme", principal_id="owner", idempotency_key="complete-1",
            run_id=started.run_id, expected_revision=0,
            expected_card_revision=started.card_revision,
            expected_card_digest=started.card_digest,
            admitted_bundle_digest="e" * 64, document_count=1, edge_count=0,
            dropped_count=0, author_profile_digest="f" * 64,
        ), invocation=invocation,
    ).run
    review = ReviewAuthoringRunCommand(
        organization_id="acme", principal_id="owner", idempotency_key="review-1",
        run_id=awaiting.run_id, expected_card_revision=awaiting.card_revision,
        expected_card_digest=awaiting.card_digest,
        source_digest=awaiting.source_set_digest,
        draft_digest=awaiting.admitted_bundle_digest, outcome="Approved",
    )
    tx = sqlite3.connect(path)
    tx.execute("PRAGMA foreign_keys=ON")
    authorizer = _authorizer(_policy(action=("author.write", "author.publish")), now)
    resource = AuthoringRunResource(
        org_id=awaiting.org_id, agent_id=awaiting.agent_id, owner_id=awaiting.owner_id,
        card_revision=awaiting.card_revision, card_digest=awaiting.card_digest,
    )
    tx.execute("BEGIN IMMEDIATE")
    evidence = authorizer.current(review, resource, awaiting.source_set_digest, invocation, tx)
    changed = tx.execute(
        "UPDATE production_authoring_runs SET stage='reviewed', revision=2, review_outcome='Approved', reviewed_at='2026-07-28T00:00:00.000Z' WHERE org_id=? AND run_id=?",
        (awaiting.org_id, awaiting.run_id),
    ).rowcount
    assert changed == 1
    assert authorizer.verify_precommit(review, resource, awaiting.source_set_digest, evidence, invocation, tx)
    assert not authorizer.verify_precommit(
        review.model_copy(update={"idempotency_key": "review-2"}), resource,
        awaiting.source_set_digest, evidence, invocation, tx,
    )
    tx.rollback()
    tx.close()


def test_canonical_trigger_recreate뒤_identity_row_drift도deny한다(
    tmp_path: Path,
) -> None:
    connection, now, command, resource, invocation = _fixture(
        tmp_path / "db.sqlite"
    )
    trigger_sql = connection.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE name='production_identity_sessions_identity_immutable'"
    ).fetchone()[0]
    connection.execute(
        "DROP TRIGGER production_identity_sessions_identity_immutable"
    )
    connection.execute(
        "UPDATE production_identity_sessions SET org_id='other'"
    )
    connection.execute(trigger_sql)
    connection.commit()
    with pytest.raises(ProductionAuthoringRunDenied):
        _authorizer(_policy(), now).current(
            command, resource, "d" * 64, invocation, connection
        )


def test_precommit_policy_source_drift와_unsealed_grant는write0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connection, now, command, resource, invocation = _fixture(tmp_path / "db.sqlite")
    snapshot = _policy()
    current = [snapshot]
    central = SnapshotCentralAuthorizer(snapshot)
    authorizer = ProductionCentralTxCurrentAuthoringAuthorizer(
        policy_snapshot=lambda: current[0],
        central_authorizer=central,
        identity_verifier=ProductionAuthoringIdentityVerifier(
            provider_id="corp", issuer="https://id.example", clock=lambda: now
        ),
    )
    evidence = authorizer.current(
        command, resource, "d" * 64, invocation, connection
    )
    assert not authorizer.verify_precommit(
        command, resource, "e" * 64, evidence, invocation, connection
    )
    current[0] = _policy(version="policy-v2")
    assert not authorizer.verify_precommit(
        command, resource, "d" * 64, evidence, invocation, connection
    )

    current[0] = snapshot
    original_authorize = central.authorize

    def unsealed(principal, action, authority_resource):
        sealed = original_authorize(principal, action, authority_resource)
        assert type(sealed) is AuthorizationGrant
        return AuthorizationGrant.model_validate(sealed.model_dump(mode="json"))

    monkeypatch.setattr(
        SnapshotCentralAuthorizer,
        "authorize",
        lambda self, principal, action, authority_resource: unsealed(
            principal, action, authority_resource
        ),
    )
    before = connection.total_changes
    with pytest.raises(ProductionAuthoringRunDenied):
        authorizer.current(command, resource, "d" * 64, invocation, connection)
    assert connection.total_changes == before
