"""RB3.2b.3-B transaction-scoped Registry admission seam."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from agent_org_network.central_browser_auth import BrowserSession
from agent_org_network.central_browser_auth_sqlite import CentralBrowserAuthSqliteStore
from agent_org_network.central_registry_admission import (
    SessionDerivedRegistryRegistrationApplication,
    SessionDerivedRegistryRegistrationFactory,
    SessionDerivedRegistryRegistrationUnauthenticated,
    SessionDerivedRegistryRegistrationUnavailable,
)
from agent_org_network.central_authority import canonical_policy_digest
from agent_org_network.agent_card import AgentCard
from agent_org_network.sqlite_production_agent_cards import (
    ProductionAgentCardCommand,
    ProductionAgentCardUnavailable,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    ProductionRegistryUserDenied,
    ProductionRegistryUserRevisionConflict,
    ProductionRegistryUserUnavailable,
    SqliteProductionRegistryUsers,
)


NOW = datetime(2026, 7, 31, 9, 0, tzinfo=UTC)


class _BootstrapAuthorizer:
    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=0, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


def _policy(path: Path, *, allowed: bool = True, version: str = "v1") -> Path:
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": version,
        "content_sha256": "pending",
        "subject_roles": [{"org_id": "acme", "subject_id": "root", "roles": ["admin"]}],
        "role_permissions": [
            {
                "role": "admin",
                "actions": ["user.register", "card.register"] if allowed else ["session.read"],
            }
        ],
        "route_rules": [],
        "worker_bindings": [],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def _session(path: Path) -> str:
    raw_digest = sha256(b"not-a-cookie").hexdigest()
    users = SqliteProductionRegistryUsers(path, authorize=_BootstrapAuthorizer())
    users.register(
        ProductionRegistryUserCommand(
            org_id="acme", principal_id="root", idempotency_key="bootstrap", expected_revision=0,
            user_id="root", email="root@example.test",
        )
    )
    users.close()
    store = CentralBrowserAuthSqliteStore(path)
    established = store.establish_session(
        sha256(b"transaction").hexdigest(),
        BrowserSession(
            session_digest=raw_digest, registry_user_id="root", org_id="acme",
            oidc_identity_binding_digest="1" * 64, csrf_digest="2" * 64,
            registry_fingerprint=sha256(b"placeholder").hexdigest(), registry_revision=1,
            established_at=NOW, expires_at=NOW + timedelta(hours=1),
        ),
        now=NOW,
        precommit_authorize=lambda _transaction: True,
    )
    # Establishment requires a matching durable User fingerprint; insert a direct session only in test setup.
    if established.value != "established":
        from agent_org_network.sqlite_production_registry_users import production_registry_user_fingerprint

        fingerprint = production_registry_user_fingerprint("acme", "root", "root@example.test", None, 1)
        with sqlite3.connect(path) as connection:
            connection.execute(
                "INSERT INTO browser_sessions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (raw_digest, "root", "acme", "1" * 64, "2" * 64, fingerprint, 1,
                 NOW.isoformat(), (NOW + timedelta(hours=1)).isoformat(), None, None),
            )
    store.close()
    return raw_digest


def test_session_derived_authorizer_rereads_active_binding_policy_and_precommit(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")
    app = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: NOW,
    ).create()
    result = app.users.register(
        ProductionRegistryUserCommand(
            org_id="acme", principal_id="root", idempotency_key="user-2", expected_revision=1,
            user_id="other", email="other@example.test", manager_id="root",
        )
    )
    assert result.revision == 2
    app.close()

    expired = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: NOW + timedelta(hours=2),
    ).create()
    with pytest.raises(SessionDerivedRegistryRegistrationUnauthenticated):
        expired.users.register(
            ProductionRegistryUserCommand(
                org_id="acme", principal_id="root", idempotency_key="user-3", expected_revision=2,
                user_id="third", email="third@example.test", manager_id="root",
            )
        )
    expired.close()


def test_session_factory_never_accepts_raw_cookie_or_wrong_digest(tmp_path: Path) -> None:
    path = tmp_path / "empty.db"
    path.touch()
    with pytest.raises(SessionDerivedRegistryRegistrationUnavailable):
        SessionDerivedRegistryRegistrationFactory(
            database_path=path, authority_snapshot_path=tmp_path / "missing.yaml", org_id="acme",
            provider_id="company-oidc", session_digest="raw-cookie", clock=lambda: NOW,
        ).create()


def _card_command(*, expected_revision: int, key: str = "card-1") -> ProductionAgentCardCommand:
    return ProductionAgentCardCommand(
        org_id="acme", principal_id="root", idempotency_key=key, expected_revision=expected_revision,
        card=AgentCard.model_validate({
            "agent_id": "support", "owner": "root", "team": "Support", "summary": "Support",
            "domains": ["support"], "last_reviewed_at": "2026-07-31", "maintainer": None,
            "can_answer": [], "cannot_answer": [], "approval_when": [], "collaborate_when": [],
            "knowledge_sources": [], "trust_labels": [],
        }),
    )


def test_card_replay_accepts_reloaded_current_policy_without_old_evidence_match(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")
    def factory() -> SessionDerivedRegistryRegistrationApplication:
        return SessionDerivedRegistryRegistrationFactory(
            database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
            session_digest=digest, clock=lambda: NOW,
        ).create()
    app = factory()
    first = app.cards.register(_card_command(expected_revision=1))
    assert first.replayed is False and first.revision == 2
    app.close()

    _policy(policy, version="v2")
    replay = factory()
    assert replay.cards.register(_card_command(expected_revision=1)).replayed is True
    assert replay.cards.counts("acme") == {"receipts": 1, "audit": 1, "outbox": 1}
    replay.close()


def test_user_replay_rereads_current_policy_and_precommit_without_new_writes(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")

    def factory() -> SessionDerivedRegistryRegistrationApplication:
        return SessionDerivedRegistryRegistrationFactory(
            database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
            session_digest=digest, clock=lambda: NOW,
        ).create()

    command = ProductionRegistryUserCommand(
        org_id="acme", principal_id="root", idempotency_key="user-replay", expected_revision=1,
        user_id="second", email="second@example.test", manager_id="root",
    )
    first = factory()
    assert first.users.register(command).replayed is False
    first.close()
    _policy(policy, version="v2")
    replay = factory()
    result = replay.users.register(command)
    assert result.replayed is True and result.revision == 2
    assert replay.users.counts("acme") == {"receipts": 2, "audit": 2, "outbox": 2}
    replay.close()
    _policy(policy, allowed=False)
    denied = factory()
    with pytest.raises(ProductionRegistryUserDenied):
        denied.users.register(command)
    assert denied.users.counts("acme") == {"receipts": 2, "audit": 2, "outbox": 2}
    denied.close()


@pytest.mark.parametrize("mutation", ("ended", "expired", "deleted"))
def test_user_precommit_session_loss_is_typed_unauthenticated_and_writes_nothing(
    tmp_path: Path, mutation: str
) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")
    moments = iter((NOW, NOW + timedelta(seconds=2)))
    app = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: next(moments),
    ).create()

    def lose_session(point: str) -> None:
        if point != "after_user":
            return
        if mutation == "ended":
            app.users._connection.execute(  # pyright: ignore[reportPrivateUsage]
                "UPDATE browser_sessions SET ended_at=?,terminal_reason='logout' WHERE session_digest=?",
                (NOW.isoformat(), digest),
            )
        elif mutation == "expired":
            app.users._connection.execute(  # pyright: ignore[reportPrivateUsage]
                "UPDATE browser_sessions SET expires_at=? WHERE session_digest=?",
                ((NOW + timedelta(seconds=1)).isoformat(), digest),
            )
        else:
            app.users._connection.execute(  # pyright: ignore[reportPrivateUsage]
                "DELETE FROM browser_sessions WHERE session_digest=?", (digest,)
            )

    app.users._fault = lose_session  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SessionDerivedRegistryRegistrationUnauthenticated):
        app.users.register(
            ProductionRegistryUserCommand(
                org_id="acme", principal_id="root", idempotency_key=f"lost-{mutation}", expected_revision=1,
                user_id="second", email="second@example.test", manager_id="root",
            )
        )
    assert app.users.revision("acme") == 1
    assert app.users.counts("acme") == {"receipts": 1, "audit": 1, "outbox": 1}
    app.close()


def test_user_exact_replay_is_one_fresh_result_31_replays_and_survives_reopen(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")
    command = ProductionRegistryUserCommand(
        org_id="acme", principal_id="root", idempotency_key="parallel-user", expected_revision=1,
        user_id="parallel", email="parallel@example.test", manager_id="root",
    )

    def invoke(_: int) -> tuple[int, bool]:
        application = SessionDerivedRegistryRegistrationFactory(
            database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
            session_digest=digest, clock=lambda: NOW,
        ).create()
        try:
            result = application.users.register(command)
            return result.revision, result.replayed
        finally:
            application.close()

    with ThreadPoolExecutor(max_workers=32) as executor:
        results = tuple(executor.map(invoke, range(32)))
    assert [replayed for _revision, replayed in results].count(False) == 1
    assert [replayed for _revision, replayed in results].count(True) == 31
    assert {revision for revision, _replayed in results} == {2}
    reopened = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: NOW,
    ).create()
    try:
        assert reopened.users.register(command).replayed is True
        assert reopened.users.revision("acme") == 2
        assert reopened.users.counts("acme") == {"receipts": 2, "audit": 2, "outbox": 2}
    finally:
        reopened.close()


def test_card_authorizer_preserves_typed_unauthenticated_for_the_next_slice(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM browser_sessions WHERE session_digest=?", (digest,))
    application = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: NOW,
    ).create()
    try:
        with pytest.raises(SessionDerivedRegistryRegistrationUnauthenticated):
            application.cards.register(_card_command(expected_revision=1, key="card-no-session"))
        assert application.cards.counts("acme") == {"receipts": 0, "audit": 0, "outbox": 0}
    finally:
        application.close()


def test_user_shared_revision_cas_has_one_request_scoped_winner_and_31_conflicts(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")

    def invoke(index: int) -> str:
        application = SessionDerivedRegistryRegistrationFactory(
            database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
            session_digest=digest, clock=lambda: NOW,
        ).create()
        try:
            application.users.register(
                ProductionRegistryUserCommand(
                    org_id="acme", principal_id="root", idempotency_key=f"cas-{index}", expected_revision=1,
                    user_id=f"candidate-{index}", email=f"candidate-{index}@example.test", manager_id="root",
                )
            )
            return "winner"
        except ProductionRegistryUserRevisionConflict:
            return "conflict"
        finally:
            application.close()

    with ThreadPoolExecutor(max_workers=32) as executor:
        outcomes = tuple(executor.map(invoke, range(32)))
    assert outcomes.count("winner") == 1
    assert outcomes.count("conflict") == 31
    application = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: NOW,
    ).create()
    try:
        assert application.users.revision("acme") == 2
        assert application.users.counts("acme") == {"receipts": 2, "audit": 2, "outbox": 2}
    finally:
        application.close()


def test_precommit_drift_or_current_deny_leaves_registry_write_zero(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")
    moments = iter((NOW, NOW + timedelta(hours=2)))
    app = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: next(moments),
    ).create()
    with pytest.raises(SessionDerivedRegistryRegistrationUnauthenticated):
        app.users.register(
            ProductionRegistryUserCommand(
                org_id="acme", principal_id="root", idempotency_key="precommit-drift", expected_revision=1,
                user_id="denied", email="denied@example.test", manager_id="root",
            )
        )
    assert app.users.revision("acme") == 1
    assert app.users.counts("acme") == {"receipts": 1, "audit": 1, "outbox": 1}
    app.close()

    _policy(policy, allowed=False)
    denied = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=lambda: NOW,
    ).create()
    with pytest.raises(ProductionRegistryUserDenied):
        denied.users.register(
            ProductionRegistryUserCommand(
                org_id="acme", principal_id="root", idempotency_key="policy-denied", expected_revision=1,
                user_id="again", email="again@example.test", manager_id="root",
            )
        )
    assert denied.users.revision("acme") == 1
    denied.close()


def test_ended_session_registry_drift_or_policy_unavailable_never_writes(tmp_path: Path) -> None:
    from agent_org_network.central_browser_auth_sqlite import migrate_browser_auth_schema
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")

    def application(authority: Path = policy) -> SessionDerivedRegistryRegistrationApplication:
        return SessionDerivedRegistryRegistrationFactory(
            database_path=path, authority_snapshot_path=authority, org_id="acme",
            provider_id="company-oidc", session_digest=digest, clock=lambda: NOW,
        ).create()

    def command(key: str) -> ProductionRegistryUserCommand:
        return ProductionRegistryUserCommand(
            org_id="acme", principal_id="root", idempotency_key=key, expected_revision=1,
            user_id="candidate", email="candidate@example.test", manager_id="root",
        )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE browser_sessions SET ended_at=?,terminal_reason='logout' WHERE session_digest=?",
            (NOW.isoformat(), digest),
        )
    ended = application()
    with pytest.raises(SessionDerivedRegistryRegistrationUnauthenticated):
        ended.users.register(command("ended"))
    assert ended.users.revision("acme") == 1
    ended.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE browser_sessions SET ended_at=NULL,terminal_reason=NULL,registry_revision=999 "
            "WHERE session_digest=?",
            (digest,),
        )
    drifted = application()
    with pytest.raises(ProductionRegistryUserDenied):
        drifted.users.register(command("drift"))
    assert drifted.users.revision("acme") == 1
    drifted.close()

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE browser_sessions SET registry_revision=1 WHERE session_digest=?", (digest,)
        )
    unavailable = application(tmp_path / "authority-missing.yaml")
    with pytest.raises(ProductionRegistryUserUnavailable):
        unavailable.users.register(command("unavailable"))
    assert unavailable.users.revision("acme") == 1
    unavailable.close()


@pytest.mark.parametrize("target", ["user", "card"])
@pytest.mark.parametrize("fault", ["policy", "registry_schema", "browser_session"])
def test_precommit_unavailable_is_typed_and_rolls_back(
    target: str, fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import agent_org_network.central_registry_admission as admission
    from agent_org_network.central_browser_auth_sqlite import (
        BrowserAuthSqliteUnavailable,
        migrate_browser_auth_schema,
    )
    from agent_org_network.sqlite_production_agent_cards import SqliteProductionAgentCards

    path = tmp_path / "central.db"
    SqliteProductionRegistryUsers.migrate(path)
    SqliteProductionAgentCards.migrate(path)
    migrate_browser_auth_schema(path)
    digest = _session(path)
    policy = _policy(tmp_path / "authority.yaml")
    reads = 0
    checks = 0

    def clock() -> datetime:
        nonlocal reads
        reads += 1
        if fault == "policy" and reads == 2:
            policy.unlink()
        return NOW

    if fault == "registry_schema":
        original = admission.validate_production_registry_user_connection

        def unavailable_second(connection: sqlite3.Connection) -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise ProductionRegistryUserUnavailable()
            original(connection)

        monkeypatch.setattr(admission, "validate_production_registry_user_connection", unavailable_second)
    elif fault == "browser_session":
        original_browser = admission.validate_browser_auth_connection

        def unavailable_second_browser(connection: sqlite3.Connection) -> None:
            nonlocal checks
            checks += 1
            if checks == 2:
                raise BrowserAuthSqliteUnavailable()
            original_browser(connection)

        monkeypatch.setattr(admission, "validate_browser_auth_connection", unavailable_second_browser)

    app = SessionDerivedRegistryRegistrationFactory(
        database_path=path, authority_snapshot_path=policy, org_id="acme", provider_id="company-oidc",
        session_digest=digest, clock=clock,
    ).create()
    if target == "user":
        with pytest.raises(ProductionRegistryUserUnavailable):
            app.users.register(
                ProductionRegistryUserCommand(
                    org_id="acme", principal_id="root", idempotency_key="user-precommit-unavailable",
                    expected_revision=1, user_id="candidate", email="candidate@example.test", manager_id="root",
                )
            )
        assert app.users.revision("acme") == 1
        assert app.users.counts("acme") == {"receipts": 1, "audit": 1, "outbox": 1}
    else:
        with pytest.raises(ProductionAgentCardUnavailable):
            app.cards.register(_card_command(expected_revision=1, key="card-precommit-unavailable"))
        assert app.cards.revision("acme") == 1
        assert app.cards.counts("acme") == {"receipts": 0, "audit": 0, "outbox": 0}
    app.close()
