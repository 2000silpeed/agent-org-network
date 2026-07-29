from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

from agent_org_network.production_identity_sessions import (
    ProductionIdentityUnavailable,
    SqliteProductionIdentitySessions,
    VerifiedEmailIdentityProof,
)
from agent_org_network.sqlite_production_registry_users import (
    CurrentUserRegistrationAuthorization,
    ProductionRegistryUserCommand,
    SqliteProductionRegistryUsers,
)

_SESSION_ID = "s" * 32


class _Auth:
    def current(self, command: object, transaction: sqlite3.Connection) -> CurrentUserRegistrationAuthorization:
        _ = command, transaction
        return CurrentUserRegistrationAuthorization(
            authority_epoch=1, policy_digest="b" * 64, evidence_digest="a" * 64
        )

    def verify_precommit(
        self, command: object, evidence: CurrentUserRegistrationAuthorization, transaction: sqlite3.Connection
    ) -> bool:
        _ = command, evidence, transaction
        return True


def _registry(path: Path) -> SqliteProductionRegistryUsers:
    SqliteProductionRegistryUsers.migrate(path)
    registry = SqliteProductionRegistryUsers(path, authorize=_Auth())
    registry.register(
        ProductionRegistryUserCommand(
            org_id="acme",
            principal_id="root",
            idempotency_key="user-1",
            expected_revision=0,
            user_id="alice",
            email="alice@company.com",
        )
    )
    return registry


def _proof(**changes: object) -> VerifiedEmailIdentityProof:
    values: dict[str, object] = {
        "provider_id": "corp-oidc",
        "issuer": "https://id.company.test",
        "email": "alice@company.com",
        "email_verified": True,
    }
    values.update(changes)
    return VerifiedEmailIdentityProof(**values)  # type: ignore[arg-type]


def test_verified_email은_opaque_session으로_resolve된다(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "registry.db")
    path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(path)
    now = datetime(2026, 7, 27, tzinfo=UTC)
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=registry,
        configured_org_id="acme",
        provider_id="corp-oidc",
        issuer="https://id.company.test",
        clock=lambda: now,
        _identity_session_id_factory=lambda: _SESSION_ID,
    )
    envelope = sessions.establish(_proof(), expires_at=now + timedelta(hours=1))
    assert envelope.principal.subject_id == "alice"
    assert sessions.resolve(_SESSION_ID) == envelope
    raw = path.read_bytes()
    assert b"alice@company.com" not in raw
    assert b"https://id.company.test" not in raw
    assert b"corp-oidc" not in raw
    assert b"id_token" not in raw
    assert b"subject-claim" not in raw


@pytest.mark.parametrize(
    "proof",
    [
        _proof(email_verified=False),
        _proof(provider_id="other"),
        _proof(issuer="https://evil.test"),
        _proof(email="missing@company.com"),
    ],
)
def test_unverified_provider_issuer_unmatched는_session_write0(
    tmp_path: Path, proof: VerifiedEmailIdentityProof
) -> None:
    registry = _registry(tmp_path / "registry.db")
    path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(path)
    sessions = SqliteProductionIdentitySessions(
        path,
        registry=registry,
        configured_org_id="acme",
        provider_id="corp-oidc",
        issuer="https://id.company.test",
        _identity_session_id_factory=lambda: _SESSION_ID,
    )
    with pytest.raises(ProductionIdentityUnavailable):
        sessions.establish(proof)
    assert sessions.count() == 0


def test_expired_revoked_registry_drift는_resolve를_닫는다(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "registry.db")
    path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(path)
    now = datetime(2026, 7, 27, tzinfo=UTC)
    sessions = SqliteProductionIdentitySessions(
        path, registry=registry, configured_org_id="acme", provider_id="corp-oidc",
        issuer="https://id.company.test", clock=lambda: now,
        _identity_session_id_factory=lambda: _SESSION_ID,
    )
    sessions.establish(_proof(), expires_at=now + timedelta(seconds=1))
    sessions.revoke(_SESSION_ID)
    with pytest.raises(ProductionIdentityUnavailable):
        sessions.resolve(_SESSION_ID)
    with pytest.raises(ProductionIdentityUnavailable):
        sessions.resolve("forged")


def test_session_fault는_partial_row를_남기지_않는다(tmp_path: Path) -> None:
    def fail(_point: str) -> None:
        raise RuntimeError("fault")

    registry = _registry(tmp_path / "registry.db")
    path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(path)
    sessions = SqliteProductionIdentitySessions(
        path, registry=registry, configured_org_id="acme", provider_id="corp-oidc",
        issuer="https://id.company.test",
        fault_injector=fail,
        _identity_session_id_factory=lambda: _SESSION_ID,
    )
    with pytest.raises(RuntimeError):
        sessions.establish(_proof())
    assert sessions.count() == 0


def test_weak_session_id와_collision은_failclosed다(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "registry.db")
    path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(path)
    weak = SqliteProductionIdentitySessions(
        path, registry=registry, configured_org_id="acme", provider_id="corp-oidc",
        issuer="https://id.company.test", _identity_session_id_factory=lambda: "weak",
    )
    with pytest.raises(ProductionIdentityUnavailable):
        weak.establish(_proof())
    sessions = SqliteProductionIdentitySessions(
        path, registry=registry, configured_org_id="acme", provider_id="corp-oidc",
        issuer="https://id.company.test", _identity_session_id_factory=lambda: _SESSION_ID,
    )
    sessions.establish(_proof())
    with pytest.raises(sqlite3.IntegrityError):
        sessions.establish(_proof())
    assert sessions.count() == 1


def test_revoke_fault는_active를_보존하고_retry된다(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "registry.db")
    path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(path)
    fail = True

    def fault(point: str) -> None:
        nonlocal fail
        if point == "revoke_before_commit" and fail:
            fail = False
            raise RuntimeError("commit fault")

    sessions = SqliteProductionIdentitySessions(
        path, registry=registry, configured_org_id="acme", provider_id="corp-oidc",
        issuer="https://id.company.test", fault_injector=fault,
        _identity_session_id_factory=lambda: _SESSION_ID,
    )
    sessions.establish(_proof())
    with pytest.raises(RuntimeError):
        sessions.revoke(_SESSION_ID)
    assert sessions.resolve(_SESSION_ID).principal.subject_id == "alice"
    sessions.revoke(_SESSION_ID)
    with pytest.raises(ProductionIdentityUnavailable):
        sessions.resolve(_SESSION_ID)


def test_resolve는_configured_org_provider_issuer를_exact_재검증한다(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "registry.db")
    path = tmp_path / "identity.db"
    SqliteProductionIdentitySessions.migrate(path)
    issuer = SqliteProductionIdentitySessions(
        path, registry=registry, configured_org_id="acme", provider_id="corp-oidc",
        issuer="https://id.company.test", _identity_session_id_factory=lambda: _SESSION_ID,
    )
    issuer.establish(_proof())
    for configured_org_id, provider_id, configured_issuer in (
        ("other", "corp-oidc", "https://id.company.test"),
        ("acme", "other", "https://id.company.test"),
        ("acme", "corp-oidc", "https://other.test"),
    ):
        resolver = SqliteProductionIdentitySessions(
            path,
            registry=registry,
            configured_org_id=configured_org_id,
            provider_id=provider_id,
            issuer=configured_issuer,
        )
        with pytest.raises(ProductionIdentityUnavailable):
            resolver.resolve(_SESSION_ID)
