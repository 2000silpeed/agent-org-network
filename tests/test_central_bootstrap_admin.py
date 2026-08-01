"""RB3.2a bootstrap admission application contract."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import cast

import pytest

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    ResourceRef,
)
from agent_org_network.central_bootstrap_admin import (
    BootstrapAdminApplication,
    BootstrapAdminAttestation,
    BootstrapAdminConfig,
    BootstrapAdminConfigurationError,
    BootstrapAdminConflict,
    BootstrapAdminDenied,
    BootstrapAdminResult,
    BootstrapAdminUnavailable,
    BootstrapOidcDeviceDenied,
    VerifiedBootstrapIdentity,
)
from agent_org_network.central_bootstrap_sqlite import (
    CentralBootstrapAdminSealStore,
    migrate_central_bootstrap_admin_schema,
)
from agent_org_network.sqlite_production_registry_users import SqliteProductionRegistryUsers


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)


def _digest(value: str) -> str:
    return sha256(value.encode()).hexdigest()


class _Authority:
    def __init__(self, *, allowed: bool = True, digest: str = "a" * 64) -> None:
        self.allowed = allowed
        self.digest = digest
        self._grants: set[int] = set()

    def authorize(
        self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> AuthorizationGrant | AuthorizationDenied:
        if not self.allowed or action != "user.register" or resource.kind != "user":
            return AuthorizationDenied(kind="not_found_or_denied")
        grant = AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action=cast(object, action),  # type: ignore[arg-type]
            resource=resource,
            roles=("admin",),
            policy_version="test",
            policy_digest=self.digest,
        )
        self._grants.add(id(grant))
        return grant

    def verify(
        self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> bool:
        return id(grant) in self._grants and grant.subject_id == principal.subject_id and grant.action == action and grant.resource == resource


class _Device:
    def __init__(self, identity: VerifiedBootstrapIdentity) -> None:
        self.identity = identity

    def authorize(self, **_kwargs: object) -> VerifiedBootstrapIdentity:
        return self.identity


def _identity(*, email: str = "root@company.test", subject: str = "sub-1") -> VerifiedBootstrapIdentity:
    return VerifiedBootstrapIdentity(
        issuer="https://issuer.test",
        audience="central-client",
        subject=subject,
        email=email,
        email_verified=True,
    )


def _attestation(identity: VerifiedBootstrapIdentity | None = None, **changes: object) -> BootstrapAdminAttestation:
    verified = identity or _identity()
    values: dict[str, object] = {
        "schema_version": 1,
        "attestation_id": "att-1",
        "org_id": "acme",
        "registry_user_id": "root",
        "oidc_provider_id": "oidc-main",
        "oidc_issuer_digest": _digest(verified.issuer),
        "oidc_audience_digest": _digest(verified.audience),
        "oidc_subject_digest": _digest(verified.issuer + "\x00" + verified.subject),
        "verified_email_digest": _digest(verified.email),
        "device_authorization_ref": "device-1",
        "idempotency_key": "register-1",
        "expected_registry_revision": 0,
        "authority_policy_digest": "a" * 64,
    }
    values.update(changes)
    return BootstrapAdminAttestation(**values)  # type: ignore[arg-type]


def _app(tmp_path: Path, *, authority: _Authority | None = None, device: object | None = None, fault: object | None = None) -> tuple[BootstrapAdminApplication, Path]:
    registry_path = tmp_path / "central.sqlite3"
    seal_path = registry_path
    SqliteProductionRegistryUsers.migrate(registry_path)
    migrate_central_bootstrap_admin_schema(registry_path)
    actual_authority = authority or _Authority()
    application = BootstrapAdminApplication(
        config=BootstrapAdminConfig(
            org_id="acme",
            oidc_provider_id="oidc-main",
            oidc_issuer="https://issuer.test",
            oidc_audience="central-client",
            authority_policy_digest="a" * 64,
        ),
        authority=actual_authority,
        device_authorizer=cast(object, device or _Device(_identity())),  # type: ignore[arg-type]
        registry_path=registry_path,
        seals=CentralBootstrapAdminSealStore(seal_path),
        clock=lambda: NOW,
        fault_injector=cast(object, fault) if fault else None,  # type: ignore[arg-type]
    )
    return application, registry_path


def test_first_admission_and_restart_replay_seal_one_registry_evidence_graph(tmp_path: Path) -> None:
    app, registry_path = _app(tmp_path)
    first = app.run(_attestation())
    restarted, _ = _app_from_existing(tmp_path, registry_path)
    replay = restarted.run(_attestation())

    assert first.state == replay.state == "BootstrapSealed"
    assert first.revision == replay.revision == 1
    assert replay.replayed is True
    registry = _read_registry(registry_path)
    assert registry.users("acme")[0].manager_id is None
    assert registry.counts("acme") == {"receipts": 1, "audit": 1, "outbox": 1}
    assert "root@company.test" not in repr(_identity())
    assert "root@company.test" not in repr(first)


def test_crash_after_registry_before_seal_recovers_only_same_command(tmp_path: Path) -> None:
    def crash(point: str) -> None:
        if point == "after_registry_before_seal":
            raise RuntimeError

    crashing, registry_path = _app(tmp_path, fault=crash)
    with pytest.raises(BootstrapAdminUnavailable):
        crashing.run(_attestation())
    recovered, _ = _app_from_existing(tmp_path, registry_path)
    assert recovered.run(_attestation()).replayed is True
    with pytest.raises(BootstrapAdminConflict):
        recovered.run(_attestation(attestation_id="att-2", idempotency_key="register-2"))


def test_identity_policy_or_authority_mismatch_writes_nothing(tmp_path: Path) -> None:
    denied, path = _app(tmp_path, authority=_Authority(allowed=False))
    with pytest.raises(BootstrapAdminDenied):
        denied.run(_attestation())
    assert _read_registry(path).users("acme") == ()


def test_registry_transaction_reauthorization_revoke는_bootstrap_seal과_durable_write를_막는다(
    tmp_path: Path,
) -> None:
    class RevokingAuthority(_Authority):
        def __init__(self) -> None:
            super().__init__()
            self.authorize_calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> AuthorizationGrant | AuthorizationDenied:
            self.authorize_calls += 1
            if self.authorize_calls >= 2:
                return AuthorizationDenied(kind="not_found_or_denied")
            return super().authorize(principal, action, resource)

    authority = RevokingAuthority()
    app, path = _app(tmp_path, authority=authority)

    with pytest.raises(BootstrapAdminDenied):
        app.run(_attestation())

    assert authority.authorize_calls == 2
    assert _read_registry(path).users("acme") == ()
    assert CentralBootstrapAdminSealStore(path).get("acme") is None


def test_registry_transaction_reauthorization_policy_drift는_durable_write를_막는다(
    tmp_path: Path,
) -> None:
    class DriftingAuthority(_Authority):
        def __init__(self) -> None:
            super().__init__()
            self.authorize_calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> AuthorizationGrant | AuthorizationDenied:
            self.authorize_calls += 1
            grant = super().authorize(principal, action, resource)
            if self.authorize_calls >= 2:
                return grant.model_copy(update={"policy_digest": "b" * 64})
            return grant

    authority = DriftingAuthority()
    app, path = _app(tmp_path, authority=authority)

    with pytest.raises(BootstrapAdminDenied):
        app.run(_attestation())

    assert authority.authorize_calls == 2
    assert _read_registry(path).users("acme") == ()
    assert CentralBootstrapAdminSealStore(path).get("acme") is None


@pytest.mark.parametrize("unavailable_call", [2, 3])
def test_registry_transaction_authority_policy_unavailable는_typed_unavailable과_write0을_보존한다(
    tmp_path: Path, unavailable_call: int
) -> None:
    class PolicyUnavailableAtAuthority(_Authority):
        def __init__(self) -> None:
            super().__init__()
            self.authorize_calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> AuthorizationGrant | AuthorizationDenied:
            self.authorize_calls += 1
            if self.authorize_calls == unavailable_call:
                return AuthorizationDenied(kind="policy_unavailable")
            return super().authorize(principal, action, resource)

    authority = PolicyUnavailableAtAuthority()
    app, path = _app(tmp_path, authority=authority)

    with pytest.raises(BootstrapAdminUnavailable):
        app.run(_attestation())

    assert authority.authorize_calls == unavailable_call
    registry = _read_registry(path)
    assert registry.users("acme") == ()
    assert registry.counts("acme") == {"receipts": 0, "audit": 0, "outbox": 0}
    assert CentralBootstrapAdminSealStore(path).get("acme") is None


@pytest.mark.parametrize("fault_call", [2, 3])
def test_registry_transaction_authority_exception은_typed_unavailable과_write0을_보존한다(
    tmp_path: Path, fault_call: int
) -> None:
    class FaultingAuthority(_Authority):
        def __init__(self) -> None:
            super().__init__()
            self.authorize_calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> AuthorizationGrant | AuthorizationDenied:
            self.authorize_calls += 1
            if self.authorize_calls == fault_call:
                raise RuntimeError("authority transport fault")
            return super().authorize(principal, action, resource)

    authority = FaultingAuthority()
    app, path = _app(tmp_path, authority=authority)

    with pytest.raises(BootstrapAdminUnavailable):
        app.run(_attestation())

    assert authority.authorize_calls == fault_call
    registry = _read_registry(path)
    assert registry.users("acme") == ()
    assert registry.counts("acme") == {"receipts": 0, "audit": 0, "outbox": 0}
    assert CentralBootstrapAdminSealStore(path).get("acme") is None

    identity_dir = tmp_path / "identity"
    identity_dir.mkdir()
    mismatch, path = _app(identity_dir, device=_Device(_identity(email="other@company.test")))
    with pytest.raises(BootstrapAdminDenied):
        mismatch.run(_attestation())
    assert _read_registry(path).users("acme") == ()


def test_explicit_device_denial_is_stable_denied(tmp_path: Path) -> None:
    class DeviceDenied:
        def authorize(self, **_kwargs: object) -> VerifiedBootstrapIdentity:
            raise BootstrapOidcDeviceDenied()

    app, _ = _app(tmp_path, device=DeviceDenied())
    with pytest.raises(BootstrapAdminDenied):
        app.run(_attestation())


def test_policy_unavailable_and_malformed_memory_identity_are_fieldless_safe_failures(
    tmp_path: Path,
) -> None:
    class PolicyUnavailable(_Authority):
        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> AuthorizationGrant | AuthorizationDenied:
            _ = principal, action, resource
            return AuthorizationDenied(kind="policy_unavailable")

    unavailable, _ = _app(tmp_path, authority=PolicyUnavailable())
    with pytest.raises(BootstrapAdminUnavailable) as failure:
        unavailable.run(_attestation())
    assert str(failure.value) == ""

    malformed_dir = tmp_path / "malformed"
    malformed_dir.mkdir()
    malformed, _ = _app(malformed_dir, device=_Device(_identity(email="not-an-email")))
    with pytest.raises(BootstrapAdminDenied) as failure:
        malformed.run(_attestation())
    assert "not-an-email" not in str(failure.value)


def test_attestation_loader_rejects_duplicate_and_extra_keys_without_echoing_values(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.yaml"
    duplicate.write_text("schema_version: 1\nschema_version: 1\nsecret: raw-token\n", encoding="utf-8")
    with pytest.raises(BootstrapAdminConfigurationError) as failure:
        BootstrapAdminAttestation.load(duplicate)
    assert "raw-token" not in str(failure.value)

    oversized = tmp_path / "oversized.yaml"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(BootstrapAdminConfigurationError) as failure:
        BootstrapAdminAttestation.load(oversized)
    assert str(failure.value) == ""

    alias_bomb = tmp_path / "alias-bomb.yaml"
    alias_bomb.write_text("seed: &seed [x, x]\nexpansion: [*seed, *seed]\n", encoding="utf-8")
    with pytest.raises(BootstrapAdminConfigurationError) as failure:
        BootstrapAdminAttestation.load(alias_bomb)
    assert str(failure.value) == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("org_id", "other-org"),
        ("subject_id", "other-subject"),
        ("action", "card.register"),
        ("resource", ResourceRef(org_id="acme", kind="user", resource_id="other-user")),
        ("roles", ("owner",)),
        ("policy_digest", "b" * 64),
    ],
)
def test_forged_grant_even_when_verify_true_is_denied_without_registry_write(
    tmp_path: Path, field: str, value: object
) -> None:
    class ForgedGrantAuthority(_Authority):
        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> AuthorizationGrant | AuthorizationDenied:
            values: dict[str, object] = {
                "org_id": principal.org_id,
                "subject_id": principal.subject_id,
                "action": action,
                "resource": resource,
                "roles": ("admin",),
                "policy_version": "test",
                "policy_digest": "a" * 64,
            }
            values[field] = value
            return AuthorizationGrant(**values)  # type: ignore[arg-type]

        def verify(
            self, grant: AuthorizationGrant, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> bool:
            _ = grant, principal, action, resource
            return True

    app, path = _app(tmp_path, authority=ForgedGrantAuthority())
    with pytest.raises(BootstrapAdminDenied):
        app.run(_attestation())
    assert _read_registry(path).users("acme") == ()


def test_same_command_concurrency_converges_to_one_evidence_graph(tmp_path: Path) -> None:
    app, registry_path = _app(tmp_path)

    def run(_index: int) -> BootstrapAdminResult:
        return app.run(_attestation())

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, range(8)))
    assert {result.revision for result in results} == {1}
    assert _read_registry(registry_path).counts("acme") == {"receipts": 1, "audit": 1, "outbox": 1}


def _app_from_existing(tmp_path: Path, registry_path: Path) -> tuple[BootstrapAdminApplication, Path]:
    seal_path = registry_path
    return (
        BootstrapAdminApplication(
            config=BootstrapAdminConfig(
                org_id="acme", oidc_provider_id="oidc-main", oidc_issuer="https://issuer.test",
                oidc_audience="central-client", authority_policy_digest="a" * 64,
            ),
            authority=_Authority(),
            device_authorizer=_Device(_identity()),
            registry_path=registry_path,
            seals=CentralBootstrapAdminSealStore(seal_path),
            clock=lambda: NOW,
        ),
        registry_path,
    )


def _read_registry(path: Path) -> SqliteProductionRegistryUsers:
    class ReadOnly:
        def current(self, *_args: object) -> object:
            raise RuntimeError

        def verify_precommit(self, *_args: object) -> bool:
            return False

    return SqliteProductionRegistryUsers(path, authorize=cast(object, ReadOnly()))  # type: ignore[arg-type]
