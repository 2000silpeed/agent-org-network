"""R5.4a sealed, scoped credential read contract."""

from __future__ import annotations

import runpy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, cast

import pytest

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.credential_scoped_read import (
    CredentialReadList,
    CredentialReadNotFoundOrDenied,
    CredentialReadUnavailable,
    CredentialReadView,
    CredentialScopedReadCapability,
    create_credential_scoped_read,
    create_credential_scoped_read_capability,
)
from agent_org_network.sqlite_durable_credential_scope_bindings import CredentialScopeSnapshot


_SCOPED = runpy.run_path("tests/test_credential_issue_scoped_operations.py")


@dataclass
class _ReadSource:
    available: bool = True
    owner: str = "owner"
    after_resolve: Callable[[], None] | None = None
    after_second_resolve: Callable[[], None] | None = None
    resolve_calls: int = 0

    def resolve_credential_read_scope(
        self, org_id: str, credential_id: str, agent_card_id: str
    ) -> CredentialScopeSnapshot | None:
        if not self.available:
            return None
        source = _SCOPED["_Source"]()
        result = source.resolve_issue_scope(org_id, credential_id, agent_card_id)
        self.resolve_calls += 1
        callback = self.after_second_resolve if self.resolve_calls == 2 else self.after_resolve
        if callback is not None:
            callback()
        if result is None or self.owner == "owner":
            return result
        return CredentialScopeSnapshot(
            result.org_id, result.credential_id, result.agent_card_id, self.owner,
            result.credential_resource_fingerprint, result.card_resource_fingerprint,
            result.owner_resource_fingerprint, result.source_kind, result.source_instance_ref,
            result.scope_revision, result.snapshot_digest, result.captured_at,
        )


def _authorizer() -> SnapshotCentralAuthorizer:
    binding = SubjectRoleBinding(org_id="org", subject_id="principal", roles=("operator",))
    permission = RolePermission(role="operator", actions=("worker_credential.read",))
    document: dict[str, object] = {
        "schema_version": 1, "org_id": "org", "policy_version": "read",
        "content_sha256": "pending", "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")], "route_rules": [], "worker_bindings": [],
    }
    return SnapshotCentralAuthorizer(AuthorityPolicySnapshot(
        schema_version=1, org_id="org", policy_version="read", content_sha256=canonical_policy_digest(document),
        subject_roles=(binding,), role_permissions=(permission,), route_rules=(), worker_bindings=(),
    ))


def _committed(path: Path) -> tuple[Any, _ReadSource]:
    operations, _source, _principal, _evidence, _delivery = _SCOPED["_operations"](path)
    reservation = _SCOPED["_reservation"]()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    reader_source = _ReadSource()
    capability = create_credential_scoped_read_capability(
        server_principal=_SCOPED["_principal"](),
        central_authorizer=_authorizer(),
        reader_source=reader_source,
    )
    return create_credential_scoped_read(capability), reader_source


def test_list_and_get_return_only_secret_free_frozen_lifecycle_view(tmp_path: Path) -> None:
    reader, _source = _committed(tmp_path / "read.sqlite")
    listed = reader.list(tmp_path / "read.sqlite", "org")
    assert listed == CredentialReadList((CredentialReadView("credential", "org", "owner", "role", 1, 1, "active", "2026-07-19T00:00:01.000Z", None),))
    assert reader.get(tmp_path / "read.sqlite", "org", "credential") == listed.credentials[0]
    assert set(CredentialReadView.__annotations__) == {"credential_id", "org_id", "owner_subject_id", "role", "generation", "revision", "status", "issued_at", "revoked_at"}


def test_get_absent_is_neutral_but_unprojected_and_source_drift_are_unavailable(tmp_path: Path) -> None:
    reader, source = _committed(tmp_path / "neutral.sqlite")
    assert reader.get(tmp_path / "neutral.sqlite", "org", "absent") == CredentialReadNotFoundOrDenied()
    import sqlite3
    connection = sqlite3.connect(tmp_path / "neutral.sqlite")
    try:
        connection.execute(
            "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("unprojected", "org", "owner", "role", 1, 1, "active", "h" * 64,
             "2026-07-19T00:00:01.000Z", None, None),
        )
        connection.commit()
    finally:
        connection.close()
    assert reader.get(tmp_path / "neutral.sqlite", "org", "unprojected") == CredentialReadUnavailable()
    assert reader.list(tmp_path / "neutral.sqlite", "org") == CredentialReadUnavailable()
    source.available = False
    assert reader.get(tmp_path / "neutral.sqlite", "org", "credential") == CredentialReadUnavailable()


def test_list_is_all_or_nothing_when_one_current_resource_is_denied(tmp_path: Path) -> None:
    reader, _source = _committed(tmp_path / "all-or-nothing.sqlite")
    source = reader._reader_source  # pyright: ignore[reportPrivateUsage]
    source.available = False
    assert reader.list(tmp_path / "all-or-nothing.sqlite", "org") == CredentialReadUnavailable()


def test_factory_rejects_issue_only_source_partial_fake_and_reuse(tmp_path: Path) -> None:
    principal = AuthenticatedPrincipal(org_id="org", subject_id="principal", identity_provider="oidc", identity_session_id="s")
    kwargs = {"server_principal": principal, "central_authorizer": _authorizer()}
    with pytest.raises(TypeError):
        create_credential_scoped_read_capability(reader_source=_SCOPED["_Source"](), **kwargs)
    with pytest.raises(TypeError):
        create_credential_scoped_read_capability(reader_source=object(), **kwargs)
    capability = create_credential_scoped_read_capability(reader_source=_ReadSource(), **kwargs)
    reader = create_credential_scoped_read(capability)
    assert type(reader).__name__ == "CredentialScopedRead"
    with pytest.raises(ValueError):
        create_credential_scoped_read(capability)
    with pytest.raises(TypeError):
        CredentialScopedReadCapability(cast(Any, object()), object())


def test_projection_accepts_revoked_lifecycle_without_changing_immutable_issuance_anchor(tmp_path: Path) -> None:
    reader, _source = _committed(tmp_path / "revoked.sqlite")
    import sqlite3
    connection = sqlite3.connect(tmp_path / "revoked.sqlite")
    try:
        connection.execute("UPDATE durable_credentials SET revision=2,status='revoked',revoked_at='2026-07-19T00:00:02.000Z' WHERE org_id='org' AND credential_id='credential'")
        connection.commit()
    finally:
        connection.close()
    result = reader.get(tmp_path / "revoked.sqlite", "org", "credential")
    assert isinstance(result, CredentialReadView) and (result.generation, result.revision, result.status) == (1, 2, "revoked")


@pytest.mark.parametrize("operation", ("get", "list"))
def test_source_callback_lifecycle_mutation_never_returns_stale_view(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / f"mutation-{operation}.sqlite"
    reader, source = _committed(path)

    def revoke_from_separate_connection() -> None:
        import sqlite3
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "UPDATE durable_credentials SET revision=2,status='revoked',revoked_at='2026-07-19T00:00:02.000Z' "
                "WHERE org_id='org' AND credential_id='credential'"
            )
            connection.commit()
        finally:
            connection.close()

    source.after_resolve = revoke_from_separate_connection
    result = reader.get(path, "org", "credential") if operation == "get" else reader.list(path, "org")
    assert result == CredentialReadUnavailable()


@pytest.mark.parametrize("operation", ("get", "list"))
def test_second_source_callback_catalog_mutation_closes_before_final_anchor(
    tmp_path: Path, operation: str
) -> None:
    path = tmp_path / f"catalog-mutation-{operation}.sqlite"
    reader, source = _committed(path)

    def add_projection_index_from_separate_connection() -> None:
        import sqlite3
        connection = sqlite3.connect(path)
        try:
            connection.execute(
                "CREATE INDEX injected_projection_index ON durable_credential_scope_projections_v1(org_id)"
            )
            connection.commit()
        finally:
            connection.close()

    source.after_second_resolve = add_projection_index_from_separate_connection
    result = reader.get(path, "org", "credential") if operation == "get" else reader.list(path, "org")
    assert result == CredentialReadUnavailable()
