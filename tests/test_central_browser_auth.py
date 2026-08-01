"""RB3.2b.2-B digest-only browser OIDC persistence contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
import sqlite3
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_org_network.central_browser_auth import (
    BrowserOidcTransaction,
    BrowserPkceVerifierVault,
    BrowserSession,
    BrowserSessionPrincipal,
    browser_redirect_uri,
    opaque_browser_handle_digest,
)
from agent_org_network.central_browser_auth_sqlite import (
    BrowserAuthSqliteUnavailable,
    BrowserSessionEstablishmentOutcome,
    CentralBrowserAuthSqliteStore,
    browser_auth_schema_ready,
    migrate_browser_auth_schema,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
    production_registry_user_fingerprint,
)


NOW = datetime(2026, 7, 31, 6, 0, tzinfo=UTC)


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _transaction() -> BrowserOidcTransaction:
    return BrowserOidcTransaction(
        transaction_digest=_digest("transaction"),
        provider_digest=_digest("provider"),
        redirect_uri_digest=_digest("https://central.example.test/api/auth/callback"),
        state_digest=_digest("state"),
        nonce_digest=_digest("nonce"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )


def _session() -> BrowserSession:
    return BrowserSession(
        session_digest=_digest("session"),
        registry_user_id="user-1",
        org_id="acme",
        oidc_identity_binding_digest=_digest("identity"),
        csrf_digest=_digest("csrf"),
        registry_fingerprint=_digest("registry"),
        registry_revision=3,
        established_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


class _RegistryAuthorizer:
    def current(
        self, command: ProductionRegistryUserCommand, transaction: sqlite3.Connection
    ) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="a" * 64, evidence_digest="b" * 64
        )

    def verify_precommit(
        self,
        command: ProductionRegistryUserCommand,
        evidence: CurrentUserRegistrationAuthorization,
        transaction: sqlite3.Connection,
    ) -> bool:
        _ = command, evidence, transaction
        return True


def _seed_registry(path: Path) -> BrowserSession:
    SqliteProductionRegistryUsers.migrate_v2(path)
    registry = SqliteProductionRegistryUsers(path, authorize=_RegistryAuthorizer())
    try:
        registered = registry.register(
            ProductionRegistryUserCommand(
                org_id="acme", principal_id="root", idempotency_key="root-1",
                expected_revision=0, user_id="user-1", email="user-1@example.test"
            )
        ).user
    finally:
        registry.close()
    return replace(
        _session(),
        registry_revision=registered.revision,
        registry_fingerprint=production_registry_user_fingerprint(
            registered.org_id, registered.user_id, registered.email,
            registered.manager_id, registered.revision
        ),
    )


def test_browser_values_are_digest_only_and_redirect_is_exact() -> None:
    transaction = _transaction()
    session = _session()
    assert transaction.transaction_digest == _digest("transaction")
    assert BrowserSessionPrincipal.from_session(session) == BrowserSessionPrincipal(
        session_digest=session.session_digest, registry_user_id="user-1", org_id="acme"
    )
    assert browser_redirect_uri("https://central.example.test") == (
        "https://central.example.test/api/auth/callback"
    )
    assert opaque_browser_handle_digest("x" * 43) == _digest("x" * 43)
    with pytest.raises(ValueError):
        browser_redirect_uri("https://central.example.test/path")


def test_pkce_vault_is_bounded_atomic_ttl_and_fail_closed() -> None:
    now = [NOW]
    vault = BrowserPkceVerifierVault(
        ttl=timedelta(minutes=5), clock=lambda: now[0], capacity=1
    )
    first = _digest("first")
    second = _digest("second")
    assert vault.reserve(first, "verifier-1", NOW + timedelta(minutes=5)) is True
    assert vault.reserve(second, "verifier-2", NOW + timedelta(minutes=5)) is False
    assert vault.consume(first) == "verifier-1"
    assert vault.consume(first) is None
    assert vault.reserve(second, "verifier-2", NOW + timedelta(minutes=5)) is True
    now[0] = NOW + timedelta(minutes=6)
    assert vault.consume(second) is None

    def broken(_point: str) -> None:
        raise RuntimeError("fault")

    assert BrowserPkceVerifierVault(fault_injector=broken).reserve(
        _digest("fault"), "verifier", NOW + timedelta(minutes=1)
    ) is False


def test_pkce_vault_after_reserve_fault_removes_and_zeroizes_material() -> None:
    transaction_digest = _digest("after-reserve-fault")
    captured: list[bytearray] = []
    vault: BrowserPkceVerifierVault
    fail_once = [True]

    def broken(point: str) -> None:
        if point == "after-reserve" and fail_once[0]:
            fail_once[0] = False
            captured.append(vault._entries[transaction_digest][0])  # pyright: ignore[reportPrivateUsage]
            raise RuntimeError("fault")

    vault = BrowserPkceVerifierVault(clock=lambda: NOW, fault_injector=broken)
    assert vault.reserve(transaction_digest, "verifier-material", NOW + timedelta(minutes=1)) is False
    assert vault.pending_count == 0
    assert bytes(captured[0]) == b"\x00" * len("verifier-material")
    assert vault.reserve(_digest("next"), "next-verifier", NOW + timedelta(minutes=1)) is True


def test_sqlite_schema_is_canonical_and_readback_is_digest_only(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite3"
    migrate_browser_auth_schema(path)
    assert browser_auth_schema_ready(path)
    store = CentralBrowserAuthSqliteStore(path)
    try:
        transaction = _transaction()
        store.create_transaction(transaction)
        assert store.get_transaction(transaction.transaction_digest) == transaction
    finally:
        store.close()
    restarted = CentralBrowserAuthSqliteStore(path)
    try:
        assert restarted.get_transaction(_transaction().transaction_digest) == _transaction()
    finally:
        restarted.close()

    with sqlite3.connect(path) as connection:
        transaction_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(browser_oidc_transactions)")
        }
        session_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(browser_sessions)")
        }
    forbidden = {"handle", "code", "verifier", "token", "claim", "email", "subject"}
    assert forbidden.isdisjoint(transaction_columns | session_columns)


def test_sqlite_store_rejects_tamper_and_never_recovers_foreign_schema(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite3"
    migrate_browser_auth_schema(path)
    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE browser_sessions ADD COLUMN forged TEXT")
    assert browser_auth_schema_ready(path) is False
    with pytest.raises(BrowserAuthSqliteUnavailable):
        CentralBrowserAuthSqliteStore(path)
    with pytest.raises(BrowserAuthSqliteUnavailable):
        migrate_browser_auth_schema(path)


def test_browser_auth_migration_fault_rolls_back_without_schema_marker(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite3"

    def crash(point: str) -> None:
        assert point == "before-browser-auth-readback"
        raise RuntimeError("fault")

    with pytest.raises(BrowserAuthSqliteUnavailable):
        migrate_browser_auth_schema(path, fault_injector=crash)
    assert browser_auth_schema_ready(path) is False


def test_store_replay_and_terminal_rows_are_monotonic(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite3"
    migrate_browser_auth_schema(path)
    store = CentralBrowserAuthSqliteStore(path)
    try:
        transaction = _transaction()
        store.create_transaction(transaction)
        assert store.mark_transaction_consumed(transaction.transaction_digest, "completed", NOW)
        assert store.mark_transaction_consumed(transaction.transaction_digest, "completed", NOW) is False
        assert store.get_transaction(transaction.transaction_digest) == replace(
            transaction, consumed_at=NOW, terminal_reason="completed"
        )
    finally:
        store.close()


def test_establishment_is_single_commit_with_registry_readback_and_precommit_check(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite3"
    session = _seed_registry(path)
    migrate_browser_auth_schema(path)
    store = CentralBrowserAuthSqliteStore(path)
    try:
        transaction = _transaction()
        store.create_transaction(transaction)
        assert store.establish_session(
            transaction.transaction_digest, session, now=NOW,
            precommit_authorize=lambda _connection: False,
        ) is BrowserSessionEstablishmentOutcome.DENIED
        assert store.get_transaction(transaction.transaction_digest) == transaction
        assert store.get_session(session.session_digest) is None

        assert store.establish_session(
            transaction.transaction_digest,
            replace(session, registry_fingerprint=_digest("stale-registry")),
            now=NOW,
            precommit_authorize=lambda _connection: True,
        ) is BrowserSessionEstablishmentOutcome.DENIED
        assert store.get_transaction(transaction.transaction_digest) == transaction

        def unavailable_precommit(_connection: sqlite3.Connection) -> bool:
            raise RuntimeError("authority unavailable")

        assert store.establish_session(
            transaction.transaction_digest, session, now=NOW,
            precommit_authorize=unavailable_precommit,
        ) is BrowserSessionEstablishmentOutcome.UNAVAILABLE
        assert store.get_transaction(transaction.transaction_digest) == transaction

        assert store.establish_session(
            transaction.transaction_digest, session, now=NOW,
            precommit_authorize=lambda _connection: True,
        ) is BrowserSessionEstablishmentOutcome.ESTABLISHED
        assert store.get_session(session.session_digest) == session
        assert store.end_session(session.session_digest, "logout", NOW)
        assert store.end_session(session.session_digest, "logout", NOW) is False
        assert store.establish_session(
            transaction.transaction_digest, replace(session, session_digest=_digest("second-session")),
            now=NOW, precommit_authorize=lambda _connection: True,
        ) is BrowserSessionEstablishmentOutcome.INVALID
    finally:
        store.close()


def test_concurrent_establish_is_serialized_as_one_success_and_one_replay(tmp_path: Path) -> None:
    path = tmp_path / "central.sqlite3"
    session = _seed_registry(path)
    migrate_browser_auth_schema(path)
    store = CentralBrowserAuthSqliteStore(path)
    try:
        transaction = _transaction()
        store.create_transaction(transaction)
        start = Barrier(3)

        def establish() -> BrowserSessionEstablishmentOutcome:
            start.wait()
            return store.establish_session(
                transaction.transaction_digest,
                session,
                now=NOW,
                precommit_authorize=lambda _connection: True,
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(establish)
            second = executor.submit(establish)
            start.wait()
            outcomes = (first.result(timeout=2), second.result(timeout=2))
        assert sorted(outcomes, key=lambda outcome: outcome.value) == [
            BrowserSessionEstablishmentOutcome.ESTABLISHED,
            BrowserSessionEstablishmentOutcome.INVALID,
        ]
        assert store.get_session(session.session_digest) == session
        consumed = store.get_transaction(transaction.transaction_digest)
        assert consumed == replace(transaction, consumed_at=NOW, terminal_reason="completed")
        with sqlite3.connect(path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM browser_sessions").fetchone() == (1,)
            assert connection.execute(
                "SELECT COUNT(*) FROM browser_oidc_transactions WHERE consumed_at IS NOT NULL"
            ).fetchone() == (1,)
    finally:
        store.close()


def test_concurrent_end_is_one_monotonic_transition_and_clock_rollback_writes_nothing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "central.sqlite3"
    session = _seed_registry(path)
    migrate_browser_auth_schema(path)
    store = CentralBrowserAuthSqliteStore(path)
    try:
        transaction = _transaction()
        store.create_transaction(transaction)
        assert store.establish_session(
            transaction.transaction_digest, session, now=NOW,
            precommit_authorize=lambda _connection: True,
        ) is BrowserSessionEstablishmentOutcome.ESTABLISHED
        with pytest.raises(BrowserAuthSqliteUnavailable):
            store.end_session(session.session_digest, "logout", NOW - timedelta(seconds=1))
        assert store.get_session(session.session_digest) == session

        start = Barrier(3)

        def end() -> bool:
            start.wait()
            return store.end_session(session.session_digest, "logout", NOW + timedelta(seconds=1))

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(end)
            second = executor.submit(end)
            start.wait()
            outcomes = (first.result(timeout=2), second.result(timeout=2))
        assert sorted(outcomes) == [False, True]
        ended = store.get_session(session.session_digest)
        assert ended is not None and ended.terminal_reason == "logout"
    finally:
        store.close()
