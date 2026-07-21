from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from agent_org_network.credential_issue_scoped_orchestration import (
    CleanupRequired,
    Issued,
    ReleasePending,
    ScopedCredentialIssueCommand,
    Unavailable,
    create_credential_issue_cleanup_readiness_capability,
    create_path_bound_scoped_credential_issue_operations,
    create_scoped_credential_issue_bridge_capability,
)
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.credential_delivery import DeliveryStage, StageMissing
from agent_org_network.credential_issue_scoped_operations import (
    CredentialIssueScopedOperations,
    create_credential_issue_scoped_operations as create_r52a_operations,
    create_credential_issue_scoped_operations_capability,
)
from agent_org_network.durable_credentials import (
    CredentialApprovalEvidence,
    canonical_credential_command_digest,
    resource_fingerprint,
)
from agent_org_network.sqlite_durable_credential_issue_cleanup import (
    migrate_sqlite_durable_credential_issue_cleanup_schema,
)
from agent_org_network.sqlite_durable_credential_issue_targets import (
    _PARENT_DDLS,  # pyright: ignore[reportPrivateUsage]
    migrate_sqlite_durable_credential_issue_targets_schema,
)
from agent_org_network.sqlite_durable_credential_scope_bindings import (
    CredentialScopeSnapshot,
    migrate_sqlite_durable_credential_scope_bindings,
)
from agent_org_network.sqlite_durable_credential_scope_projections import (
    migrate_sqlite_durable_credential_scope_projections,
)


def test_path_bound_orchestration_rejects_forged_bridge_capability() -> None:
    with pytest.raises(TypeError):
        create_path_bound_scoped_credential_issue_operations(cast(Any, object()))
    source = Path("src/agent_org_network/credential_issue_scoped_orchestration.py").read_text()
    assert "reconcile_credential_cleanup" not in source
    assert ".abort(" not in source


NOW = "2026-07-19T00:00:00.000Z"
REF = "delivery:v1:" + "f" * 64


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="org", subject_id="principal", identity_provider="oidc", identity_session_id="session"
    )


def _authorizer() -> SnapshotCentralAuthorizer:
    binding = SubjectRoleBinding(org_id="org", subject_id="principal", roles=("operator",))
    permission = RolePermission(role="operator", actions=cast(Any, ("worker_credential.issue",)))
    document = {
        "schema_version": 1,
        "org_id": "org",
        "policy_version": "test",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    return SnapshotCentralAuthorizer(AuthorityPolicySnapshot(
        schema_version=1, org_id="org", policy_version="test",
        content_sha256=canonical_policy_digest(document), subject_roles=(binding,),
        role_permissions=(permission,), route_rules=(), worker_bindings=(),
    ))


class _ScopeSource:
    def __init__(self) -> None:
        self.available = True
        self.calls = 0
        self.drop_after_calls = 0

    def resolve_issue_scope(self, org_id: str, credential_id: str, agent_card_id: str) -> CredentialScopeSnapshot | None:
        self.calls += 1
        if (
            not self.available
            or (self.drop_after_calls and self.calls > self.drop_after_calls)
            or (org_id, credential_id, agent_card_id) != ("org", "credential", "card")
        ):
            return None
        resource = ResourceRef(org_id="org", kind="worker_credential", resource_id="credential", owner_subject_id="owner")
        values = {"agent_card_id": "card", "card_resource_fingerprint": "1" * 64,
            "credential_id": "credential", "credential_resource_fingerprint": resource_fingerprint(resource),
            "org_id": "org", "owner_resource_fingerprint": "2" * 64,
            "owner_subject_id": "owner", "source_instance_ref": "source", "source_kind": "registry", "scope_revision": 1}
        return CredentialScopeSnapshot(
            org_id="org", credential_id="credential", agent_card_id="card", owner_subject_id="owner",
            credential_resource_fingerprint=resource_fingerprint(resource), card_resource_fingerprint="1" * 64,
            owner_resource_fingerprint="2" * 64, source_kind="registry", source_instance_ref="source",
            scope_revision=1, snapshot_digest=hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest(), captured_at=NOW,
        )


class _PrincipalResolver:
    def __init__(self) -> None:
        self.available = True
        self.calls = 0

    def resolve_credential_principal(self, *, org_id: str, subject_id: str) -> AuthenticatedPrincipal | None:
        self.calls += 1
        return _principal() if self.available and (org_id, subject_id) == ("org", "principal") else None


class _EvidenceResolver:
    def __init__(self) -> None:
        self.available = True
        self.calls = 0

    def resolve_credential_approval_evidence(self, *, org_id: str, evidence_id: str) -> CredentialApprovalEvidence | None:
        self.calls += 1
        resource = ResourceRef(org_id="org", kind="worker_credential", resource_id="credential", owner_subject_id="owner")
        digest = canonical_credential_command_digest(
            action="worker_credential.issue", resource=resource,
            command={"owner_subject_id": "owner", "role": "role", "expires_at": None},
        )
        if not self.available or (org_id, evidence_id) != ("org", "evidence"):
            return None
        return CredentialApprovalEvidence("evidence", "worker_credential.issue", digest, resource_fingerprint(resource))


class _ApprovalProvider:
    def __init__(self) -> None:
        self.calls = 0

    def acquire_issue_approval(self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef, command_digest: str) -> CredentialApprovalEvidence:
        self.calls += 1
        return CredentialApprovalEvidence("evidence", cast(Any, action), command_digest, resource_fingerprint(resource))


class _Delivery:
    def __init__(self) -> None:
        self.recoveries = self.stages = self.releases = 0
        self.release_error = False
        self.refs: list[str] = []

    def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing:
        self.recoveries += 1
        return StageMissing()

    def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
        self.stages += 1
        return DeliveryStage(stage_key, REF)

    def release(self, delivery_ref: str) -> None:
        self.releases += 1
        self.refs.append(delivery_ref)
        if self.release_error:
            raise OSError("release unavailable")


class _SecretFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        return "only-in-memory"


def _command() -> ScopedCredentialIssueCommand:
    return ScopedCredentialIssueCommand("org", "target", "credential", "card", "owner", "role", "target", 1, None, "e" * 64, NOW)


def _r4_r51_r52a_path(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        for ddl in _PARENT_DDLS.values():
            connection.execute(ddl)
    finally:
        connection.close()
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = sqlite3.connect(path)
    try:
        migrate_sqlite_durable_credential_issue_cleanup_schema(connection)
        migrate_sqlite_durable_credential_scope_bindings(connection)
        migrate_sqlite_durable_credential_scope_projections(connection)
    finally:
        connection.close()


def _bridge(path: Path) -> tuple[object, CredentialIssueScopedOperations, _ScopeSource, _PrincipalResolver, _EvidenceResolver, _ApprovalProvider, _Delivery, _SecretFactory]:
    _r4_r51_r52a_path(path)
    source, principal, evidence, provider, delivery, secrets = _ScopeSource(), _PrincipalResolver(), _EvidenceResolver(), _ApprovalProvider(), _Delivery(), _SecretFactory()
    scoped = create_r52a_operations(create_credential_issue_scoped_operations_capability(
        binding_source=source, principal_resolver=principal, server_principal=_principal(), central_authorizer=_authorizer(),
        approval_resolver=evidence, approval_provider=provider, delivery=delivery,
    ))
    readiness = create_credential_issue_cleanup_readiness_capability(path)
    capability = create_scoped_credential_issue_bridge_capability(path=path, scoped_capability=create_credential_issue_scoped_operations_capability(
        binding_source=source, principal_resolver=principal, server_principal=_principal(), central_authorizer=_authorizer(),
        approval_resolver=evidence, approval_provider=provider, delivery=delivery,
    ), delivery=delivery, secret_factory=secrets, cleanup_readiness=readiness)
    return create_path_bound_scoped_credential_issue_operations(capability), scoped, source, principal, evidence, provider, delivery, secrets


def test_path_bound_bridge_first_issue_replay_and_already_staged_are_secret_free_after_stage(tmp_path: Path) -> None:
    bridge, _scoped, _source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(tmp_path / "bridge.sqlite")
    assert bridge.issue(_command()) == Issued("credential", REF)  # type: ignore[union-attr]
    assert (secrets.calls, delivery.recoveries, delivery.stages, delivery.releases, delivery.refs) == (1, 1, 1, 1, [REF])
    # Committed semantic replay is readback-only: the bridge must not re-enter materialization.
    def unexpected_materialize(*args: Any) -> object:
        raise AssertionError("rematerialize")
    inner = cast(Any, bridge)._dependencies[0]
    inner._scoped_operations.materialize = unexpected_materialize
    assert bridge.issue(_command()) == Issued("credential", REF)  # type: ignore[union-attr]
    assert (secrets.calls, delivery.recoveries, delivery.stages, delivery.releases, delivery.refs) == (1, 1, 1, 2, [REF, REF])

    bridge, scoped, _source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(tmp_path / "staged.sqlite")
    reservation = scoped.reserve(tmp_path / "staged.sqlite", _command())
    scoped.stage(tmp_path / "staged.sqlite", reservation, _command().stage_key, "direct-test-secret", NOW)
    assert bridge.issue(_command()) == Issued("credential", REF)  # type: ignore[union-attr]
    assert (secrets.calls, delivery.stages, delivery.releases) == (0, 1, 1)


def test_path_bound_bridge_claimed_recovery_and_release_pending_never_stage_or_cleanup(tmp_path: Path) -> None:
    bridge, scoped, _source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(tmp_path / "claimed.sqlite")
    reservation = scoped.reserve(tmp_path / "claimed.sqlite", _command())
    with pytest.raises(Exception):
        scoped.stage(
            tmp_path / "claimed.sqlite", reservation, _command().stage_key, "direct-test-secret", NOW,
            fault_injector=lambda point: (_ for _ in ()).throw(RuntimeError(point)) if point == "before_stage_cas" else None,
        )
    delivery.stages = delivery.recoveries = 0
    assert bridge.issue(_command()) == Unavailable()  # type: ignore[union-attr]
    assert (secrets.calls, delivery.stages, delivery.releases) == (0, 0, 0)

    bridge, _scoped, _source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(tmp_path / "release.sqlite")
    delivery.release_error = True
    assert bridge.issue(_command()) == ReleasePending("credential", REF)  # type: ignore[union-attr]
    assert (secrets.calls, delivery.stages, delivery.releases, delivery.refs) == (1, 1, 1, [REF])


def test_path_bound_bridge_materialization_drift_returns_cleanup_required_without_delivery_action(
    tmp_path: Path,
) -> None:
    path = tmp_path / "materialize-cleanup.sqlite"
    bridge, scoped, source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(path)
    reservation = scoped.reserve(path, _command())
    scoped.stage(path, reservation, _command().stage_key, "direct-test-secret", NOW)
    delivery.stages = delivery.recoveries = 0
    source.calls = 0
    # reserve/readiness and materialize prepare consume six current-scope reads; prewrite drifts.
    source.drop_after_calls = 6
    result = bridge.issue(_command())  # type: ignore[union-attr]
    assert result == CleanupRequired("target")
    assert (secrets.calls, delivery.stages, delivery.releases) == (0, 0, 0)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("CleanupPending",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("CleanupPending",)
    finally:
        connection.close()


@pytest.mark.parametrize("tamper", ("catalog", "row"))
def test_path_bound_bridge_tampered_cleanup_pending_is_unavailable_without_delivery_action(
    tmp_path: Path, tamper: str
) -> None:
    path = tmp_path / f"materialize-{tamper}-tamper.sqlite"
    bridge, scoped, _source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(path)
    reservation = scoped.reserve(path, _command())
    scoped.stage(path, reservation, _command().stage_key, "direct-test-secret", NOW)
    delivery.stages = delivery.recoveries = 0
    inner = cast(Any, bridge)._dependencies[0]
    operations = inner._scoped_operations

    def inject(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TRIGGER durable_credential_issue_targets_v1_immutable_snapshot")
        if tamper == "row":
            connection.execute(
                "UPDATE durable_credential_issue_targets_v1 SET target_sha256=?", ("0" * 64,)
            )

    def materialize(path: Any, org_id: Any, target_id: Any, now: Any) -> object:
        return operations._materialize_for_test(
            path, org_id, target_id, now, prewrite_fault_injector=inject
        )

    operations.materialize = materialize
    assert bridge.issue(_command()) == Unavailable()  # type: ignore[union-attr]
    assert (secrets.calls, delivery.stages, delivery.releases, hasattr(delivery, "abort")) == (0, 0, 0, False)


def test_path_bound_cleanup_classifier_never_creates_a_missing_database(tmp_path: Path) -> None:
    path = tmp_path / "removed.sqlite"
    bridge, _scoped, _source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(path)
    path.unlink()
    inner = cast(Any, bridge)._dependencies[0]
    assert inner._cleanup_readiness._is_cleanup_pending("org", "target") is False
    assert bridge.issue(_command()) == Unavailable()  # type: ignore[union-attr]
    assert not path.exists()
    assert (secrets.calls, delivery.stages, delivery.releases, hasattr(delivery, "abort")) == (0, 0, 0, False)


def test_path_bound_bridge_pre_release_read_drift_is_unavailable_without_persisted_ref(tmp_path: Path) -> None:
    bridge, _scoped, source, _principal_resolver, _evidence, _provider, delivery, secrets = _bridge(tmp_path / "drift.sqlite")
    # The first committed read succeeds; the second read, immediately before release, fails closed.
    original = source.resolve_issue_scope
    def drift(org_id: str, credential_id: str, agent_card_id: str) -> CredentialScopeSnapshot | None:
        result = original(org_id, credential_id, agent_card_id)
        if source.calls >= 14:
            source.available = False
        return result
    source.resolve_issue_scope = drift  # type: ignore[method-assign]
    assert bridge.issue(_command()) == Unavailable()  # type: ignore[union-attr]
    assert (secrets.calls, delivery.stages, delivery.releases) == (1, 1, 0)


def test_cleanup_readiness_is_single_use_path_bound_and_closed_before_scoped_reads(tmp_path: Path) -> None:
    first, second = tmp_path / "first.sqlite", tmp_path / "second.sqlite"
    _r4_r51_r52a_path(first)
    _r4_r51_r52a_path(second)
    source, principal, evidence, provider, delivery, secrets = _ScopeSource(), _PrincipalResolver(), _EvidenceResolver(), _ApprovalProvider(), _Delivery(), _SecretFactory()

    def scoped_capability() -> object:
        return create_credential_issue_scoped_operations_capability(
            binding_source=source, principal_resolver=principal, server_principal=_principal(), central_authorizer=_authorizer(),
            approval_resolver=evidence, approval_provider=provider, delivery=delivery,
        )

    readiness = create_credential_issue_cleanup_readiness_capability(first)
    with pytest.raises(ValueError):
        create_scoped_credential_issue_bridge_capability(
            path=second, scoped_capability=cast(Any, scoped_capability()), delivery=delivery,
            secret_factory=secrets, cleanup_readiness=readiness,
        )
    assert (source.calls, principal.calls, evidence.calls, delivery.stages, secrets.calls) == (0, 0, 0, 0, 0)

    capability = create_scoped_credential_issue_bridge_capability(
        path=first, scoped_capability=cast(Any, scoped_capability()), delivery=delivery,
        secret_factory=secrets, cleanup_readiness=readiness,
    )
    assert capability is not None
    with pytest.raises(ValueError):
        create_scoped_credential_issue_bridge_capability(
            path=first, scoped_capability=cast(Any, scoped_capability()), delivery=delivery,
            secret_factory=secrets, cleanup_readiness=readiness,
        )
    with pytest.raises(TypeError):
        create_scoped_credential_issue_bridge_capability(
            path=first, scoped_capability=cast(Any, scoped_capability()), delivery=delivery,
            secret_factory=secrets, cleanup_readiness=cast(Any, object()),
        )
    assert (source.calls, principal.calls, evidence.calls, delivery.stages, secrets.calls) == (0, 0, 0, 0, 0)
