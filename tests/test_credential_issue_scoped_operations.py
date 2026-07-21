"""R5.2a scoped operation composition gate regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, cast
import hashlib
import json
import sqlite3

import pytest

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
from agent_org_network.credential_issue_materialization_verifier import (
    CurrentCredentialApprovalEvidenceResolver,
    CurrentCredentialPrincipalResolver,
)
from agent_org_network.credential_issue_scoped_operations import (
    CredentialIssueScopedOperations,
    CredentialIssueScopedOperationsCapability,
    create_credential_issue_scoped_operations,
    create_credential_issue_scoped_operations_capability,
)
from agent_org_network.credential_issue_scoped_orchestration import ScopedCredentialIssueCommand
from agent_org_network.durable_credentials import (
    CredentialApprovalEvidence,
    canonical_credential_command_digest,
    resource_fingerprint,
)
from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
    migrate_sqlite_durable_credential_issue_targets_schema,
)
from agent_org_network.sqlite_durable_credential_scope_bindings import (
    CredentialScopeSnapshot,
    CredentialScopeSource,
    migrate_sqlite_durable_credential_scope_bindings,
    reserve_sqlite_credential_scope_binding,
)
from agent_org_network.sqlite_durable_credential_scope_projections import (
    migrate_sqlite_durable_credential_scope_projections,
)


def _parent(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
    CREATE TABLE durable_credentials (credential_id TEXT NOT NULL, org_id TEXT NOT NULL, owner_subject_id TEXT NOT NULL, role TEXT NOT NULL, generation INTEGER NOT NULL, revision INTEGER NOT NULL, status TEXT NOT NULL, secret_hash TEXT NOT NULL, issued_at TEXT NOT NULL, expires_at TEXT, revoked_at TEXT, PRIMARY KEY (org_id, credential_id), CHECK (generation >= 1), CHECK (revision >= 1), CHECK (status IN ('active','revoked')));
    CREATE TABLE credential_command_receipts (org_id TEXT NOT NULL, request_id TEXT NOT NULL, attempt INTEGER NOT NULL, command_digest TEXT NOT NULL, credential_id TEXT NOT NULL, result_revision INTEGER NOT NULL, result_json TEXT NOT NULL, delivery_ref TEXT, PRIMARY KEY (org_id, request_id, attempt));
    CREATE TABLE credential_audit_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, action TEXT NOT NULL, credential_id TEXT NOT NULL, principal_subject_id TEXT NOT NULL, evidence_id TEXT NOT NULL, detail_json TEXT NOT NULL);
    CREATE TABLE credential_outbox_intents (id INTEGER PRIMARY KEY AUTOINCREMENT, org_id TEXT NOT NULL, kind TEXT NOT NULL, credential_id TEXT NOT NULL, payload_json TEXT NOT NULL);
    """)
    connection.commit()
    connection.close()


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="org", subject_id="principal", identity_provider="oidc", identity_session_id="s"
    )


def _authorizer(*, allowed: bool = True) -> SnapshotCentralAuthorizer:
    binding = SubjectRoleBinding(org_id="org", subject_id="principal", roles=("operator",))
    permission = RolePermission(
        role="operator",
        actions=cast(Any, ("worker_credential.issue",) if allowed else ("worker_credential.read",)),
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "org",
        "policy_version": "test",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    return SnapshotCentralAuthorizer(
        AuthorityPolicySnapshot(
            schema_version=1,
            org_id="org",
            policy_version="test",
            content_sha256=canonical_policy_digest(document),
            subject_roles=(binding,),
            role_permissions=(permission,),
            route_rules=(),
            worker_bindings=(),
        )
    )


def _reservation() -> DurableCredentialIssueTargetReservation:
    resource = ResourceRef(
        org_id="org", kind="worker_credential", resource_id="credential", owner_subject_id="owner"
    )
    digest = canonical_credential_command_digest(
        action="worker_credential.issue",
        resource=resource,
        command={"owner_subject_id": "owner", "role": "role", "expires_at": None},
    )
    return DurableCredentialIssueTargetReservation(
        "org",
        "target",
        "credential",
        digest,
        "principal",
        "owner",
        "role",
        None,
        resource_fingerprint(resource),
        "evidence",
        digest,
        resource_fingerprint(resource),
        1,
        "2026-07-19T00:00:00.000Z",
    )


class _Source:
    available = True
    calls = 0
    drop_after_calls = 0

    def resolve_issue_scope(
        self, org_id: str, credential_id: str, agent_card_id: str
    ) -> CredentialScopeSnapshot | None:
        self.calls += 1
        if self.drop_after_calls and self.calls > self.drop_after_calls:
            return None
        if not self.available or (org_id, credential_id, agent_card_id) != (
            "org",
            "credential",
            "card",
        ):
            return None
        values = {
            "agent_card_id": "card",
            "card_resource_fingerprint": "1" * 64,
            "credential_id": "credential",
            "credential_resource_fingerprint": _reservation().resource_fingerprint,
            "org_id": "org",
            "owner_resource_fingerprint": "2" * 64,
            "owner_subject_id": "owner",
            "source_instance_ref": "source",
            "source_kind": "registry",
            "scope_revision": 1,
        }
        digest = hashlib.sha256(
            json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return CredentialScopeSnapshot(
            org_id="org",
            credential_id="credential",
            agent_card_id="card",
            owner_subject_id="owner",
            credential_resource_fingerprint=_reservation().resource_fingerprint,
            card_resource_fingerprint="1" * 64,
            owner_resource_fingerprint="2" * 64,
            source_kind="registry",
            source_instance_ref="source",
            scope_revision=1,
            snapshot_digest=digest,
            captured_at="2026-07-19T00:00:00.000Z",
        )


class _PrincipalResolver:
    available = True
    calls = 0
    drop_after_calls = 0

    def resolve_credential_principal(
        self, *, org_id: str, subject_id: str
    ) -> AuthenticatedPrincipal | None:
        self.calls += 1
        if self.drop_after_calls and self.calls > self.drop_after_calls:
            return None
        return (
            _principal()
            if self.available and (org_id, subject_id) == ("org", "principal")
            else None
        )


class _EvidenceResolver:
    available = True
    calls = 0
    drop_after_calls = 0
    action: Literal["worker_credential.issue", "worker_credential.read"] = "worker_credential.issue"

    def resolve_credential_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> CredentialApprovalEvidence | None:
        self.calls += 1
        if self.drop_after_calls and self.calls > self.drop_after_calls:
            return None
        reservation = _reservation()
        return (
            CredentialApprovalEvidence(
                "evidence",
                self.action,
                reservation.command_digest,
                reservation.resource_fingerprint,
            )
            if self.available and (org_id, evidence_id) == ("org", "evidence")
            else None
        )


class _Delivery:
    def __init__(self) -> None:
        self.recoveries = self.stages = 0

    def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing:
        self.recoveries += 1
        return StageMissing()

    def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage:
        self.stages += 1
        return DeliveryStage(stage_key, "delivery:v1:" + "f" * 64)

    def release(self, delivery_ref: str) -> None:
        pass


class _ApprovalProvider:
    calls = 0
    available = True
    after: object | None = None

    def acquire_issue_approval(
        self, principal: AuthenticatedPrincipal, action: str, resource: ResourceRef, command_digest: str
    ) -> CredentialApprovalEvidence:
        self.calls += 1
        if not self.available:
            raise OSError("approval unavailable")
        result = CredentialApprovalEvidence(
            "evidence", cast(Any, action), command_digest, resource_fingerprint(resource)
        )
        if callable(self.after):
            self.after()
        return result


def _command() -> ScopedCredentialIssueCommand:
    return ScopedCredentialIssueCommand(
        "org", "target", "credential", "card", "owner", "role", "target", 1, None,
        "e" * 64, "2026-07-19T00:00:00.000Z"
    )


def _reservation_operations(path: Path, provider: _ApprovalProvider) -> CredentialIssueScopedOperations:
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_scope_bindings(connection)
    migrate_sqlite_durable_credential_scope_projections(connection)
    connection.close()
    capability = create_credential_issue_scoped_operations_capability(
        binding_source=_Source(), principal_resolver=_PrincipalResolver(), server_principal=_principal(),
        central_authorizer=_authorizer(), approval_resolver=_EvidenceResolver(), delivery=_Delivery(),
        approval_provider=provider,
    )
    return create_credential_issue_scoped_operations(capability)


def test_reserve_acquires_stable_evidence_and_atomically_writes_target_and_binding(tmp_path: Path) -> None:
    provider = _ApprovalProvider()
    path = tmp_path / "reserve.sqlite"
    operations = _reservation_operations(path, provider)
    first = operations.reserve(path, _command())
    replay = operations.reserve(path, _command())
    assert first == replay
    assert provider.calls == 2
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM durable_credential_issue_targets_v1").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM durable_credential_scope_bindings_v1").fetchone()[0] == 1
    finally:
        connection.close()


def _reservation_counts(path: Path) -> tuple[int, int]:
    connection = sqlite3.connect(path)
    try:
        return (
            connection.execute("SELECT count(*) FROM durable_credential_issue_targets_v1").fetchone()[0],
            connection.execute("SELECT count(*) FROM durable_credential_scope_bindings_v1").fetchone()[0],
        )
    finally:
        connection.close()


def test_provider_failure_or_resolver_mismatch_writes_no_target_or_binding(tmp_path: Path) -> None:
    path = tmp_path / "failure.sqlite"
    provider = _ApprovalProvider()
    provider.available = False
    operations = _reservation_operations(path, provider)
    with pytest.raises(Exception):
        operations.reserve(path, _command())
    assert _reservation_counts(path) == (0, 0)

    path = tmp_path / "mismatch.sqlite"
    provider = _ApprovalProvider()
    operations = _reservation_operations(path, provider)
    # The resolver's read action cannot be substituted for the fixed issue action.
    cast(_EvidenceResolver, operations._guard.approval_resolver).action = "worker_credential.read"  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(Exception):
        operations.reserve(path, _command())
    assert _reservation_counts(path) == (0, 0)


def test_scope_drift_after_provider_before_reservation_has_zero_writes(tmp_path: Path) -> None:
    path = tmp_path / "scope-drift.sqlite"
    provider = _ApprovalProvider()
    operations = _reservation_operations(path, provider)
    provider.after = lambda: setattr(operations._guard.binding_source, "available", False)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(Exception):
        operations.reserve(path, _command())
    assert _reservation_counts(path) == (0, 0)


def _operations(
    path: Path, *, allowed: bool = True
) -> tuple[
    CredentialIssueScopedOperations, _Source, _PrincipalResolver, _EvidenceResolver, _Delivery
]:
    _parent(path)
    migrate_sqlite_durable_credential_issue_targets_schema(path)
    connection = sqlite3.connect(path)
    migrate_sqlite_durable_credential_scope_bindings(connection)
    migrate_sqlite_durable_credential_scope_projections(connection)
    connection.close()
    source, principal, evidence, delivery = (
        _Source(),
        _PrincipalResolver(),
        _EvidenceResolver(),
        _Delivery(),
    )
    reservation = _reservation()
    reserve_sqlite_credential_scope_binding(
        path,
        reservation=reservation,
        agent_card_id="card",
        now=reservation.created_at,
        source=source,
    )
    capability = create_credential_issue_scoped_operations_capability(
        binding_source=source,
        principal_resolver=principal,
        server_principal=_principal(),
        central_authorizer=_authorizer(allowed=allowed),
        approval_resolver=evidence,
        delivery=delivery,
    )
    return (
        create_credential_issue_scoped_operations(capability),
        source,
        principal,
        evidence,
        delivery,
    )


def test_partial_or_forged_scoped_composition_cannot_mint_or_reuse_operations() -> None:
    with pytest.raises(TypeError):
        create_credential_issue_scoped_operations_capability(
            binding_source=cast(CredentialScopeSource, object()),
            principal_resolver=cast(CurrentCredentialPrincipalResolver, object()),
            server_principal=cast(AuthenticatedPrincipal, object()),
            central_authorizer=cast(SnapshotCentralAuthorizer, object()),
            approval_resolver=cast(CurrentCredentialApprovalEvidenceResolver, object()),
            delivery=cast(Any, object()),
        )
    with pytest.raises(TypeError):
        create_credential_issue_scoped_operations(
            cast(CredentialIssueScopedOperationsCapability, object())
        )
    capability = create_credential_issue_scoped_operations_capability(
        binding_source=_Source(),
        principal_resolver=_PrincipalResolver(),
        server_principal=_principal(),
        central_authorizer=_authorizer(),
        approval_resolver=_EvidenceResolver(),
        delivery=_Delivery(),
    )
    assert (
        type(create_credential_issue_scoped_operations(capability))
        is CredentialIssueScopedOperations
    )
    with pytest.raises(ValueError):
        create_credential_issue_scoped_operations(capability)


def test_neutral_transition_core_has_no_scope_authority_mcp_or_external_delivery_imports() -> None:
    source = Path("src/agent_org_network/_credential_issue_transition_core.py").read_text()
    for forbidden in ("scope", "authority", "credential_mcp", "release", "abort"):
        assert f"agent_org_network.{forbidden}" not in source
    assert "def stage_existing_reserved_credential_issue_target" not in source


def test_materialization_commit_facade_depends_on_transition_core_not_the_reverse() -> None:
    core = Path("src/agent_org_network/_credential_issue_transition_core.py").read_text()
    facade = Path("src/agent_org_network/sqlite_durable_credential_issue_commit.py").read_text()
    assert "sqlite_durable_credential_issue_commit" not in core
    assert "_credential_issue_transition_core" in facade
    verifier = Path(
        "src/agent_org_network/credential_issue_materialization_verifier.py"
    ).read_text()
    assert "sqlite_durable_credential_issue_commit import" not in verifier
    assert "_credential_issue_transition_core import" in verifier


def test_sealed_scoped_stage_initializes_r51_fence_and_legacy_cannot_be_production_bridge(
    tmp_path: Path,
) -> None:
    operations, _, _, _, delivery = _operations(tmp_path / "credential.sqlite")
    reservation = _reservation()
    assert (
        operations.stage(
            tmp_path / "credential.sqlite",
            reservation,
            "e" * 64,
            "only-in-memory",
            reservation.created_at,
        ).delivery_ref
        == "delivery:v1:" + "f" * 64
    )
    assert delivery.recoveries == delivery.stages == 1
    source = Path("src/agent_org_network/credential_issue_scoped_operations.py").read_text()
    assert "stage_sqlite_durable_credential_issue_target" not in source
    assert "reserve_unscoped_target_for_legacy" not in source


@pytest.mark.parametrize("drift", ("source", "principal", "evidence", "authority"))
def test_scoped_drift_blocks_before_claim_and_external_delivery(tmp_path: Path, drift: str) -> None:
    operations, source, principal, evidence, delivery = _operations(
        tmp_path / f"{drift}.sqlite", allowed=drift != "authority"
    )
    if drift == "source":
        source.available = False
    elif drift == "principal":
        principal.available = False
    elif drift == "evidence":
        evidence.available = False
    with pytest.raises(Exception):
        operations.stage(
            tmp_path / f"{drift}.sqlite",
            _reservation(),
            "e" * 64,
            "only-in-memory",
            "2026-07-19T00:00:00.000Z",
        )
    assert delivery.recoveries == delivery.stages == 0
    assert sqlite3.connect(tmp_path / f"{drift}.sqlite").execute(
        "SELECT count(*) FROM credential_issue_stage_fences_v2"
    ).fetchone() == (0,)


def test_scoped_guard_rechecks_source_immediately_before_external_delivery(tmp_path: Path) -> None:
    path = tmp_path / "preexternal.sqlite"
    operations, source, _, _, delivery = _operations(path)
    source.calls = 0
    source.drop_after_calls = 2
    with pytest.raises(Exception):
        operations.stage(
            path, _reservation(), "e" * 64, "only-in-memory", "2026-07-19T00:00:00.000Z"
        )
    assert delivery.recoveries == delivery.stages == 0
    assert sqlite3.connect(path).execute(
        "SELECT state FROM credential_issue_stage_fences_v2"
    ).fetchone() == ("ClaimedStage",)


@pytest.mark.parametrize(
    "drift_sql",
    (
        "DROP TRIGGER durable_credential_scope_bindings_v1_no_update",
        "UPDATE schema_component_manifests SET manifest_sha256='0' WHERE component_id='durable_credential_scope_bindings_v1'",
    ),
)
def test_r51_catalog_or_trigger_drift_blocks_scoped_stage_before_claim(
    tmp_path: Path, drift_sql: str
) -> None:
    path = tmp_path / "preclaim-drift.sqlite"
    operations, _, _, _, delivery = _operations(path)
    connection = sqlite3.connect(path)
    connection.execute(drift_sql)
    connection.commit()
    connection.close()
    with pytest.raises(Exception):
        operations.stage(
            path, _reservation(), "e" * 64, "only-in-memory", "2026-07-19T00:00:00.000Z"
        )
    assert delivery.recoveries == delivery.stages == 0
    assert sqlite3.connect(path).execute(
        "SELECT count(*) FROM credential_issue_stage_fences_v2"
    ).fetchone() == (0,)


def test_r51_trigger_drift_after_claim_blocks_preexternal_and_leaves_claimed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preexternal-catalog-drift.sqlite"
    operations, _, _, _, delivery = _operations(path)

    def drift(point: str) -> None:
        if point == "before_preexternal_guard":
            connection = sqlite3.connect(path)
            connection.execute("DROP TRIGGER durable_credential_scope_bindings_v1_no_delete")
            connection.commit()
            connection.close()

    with pytest.raises(Exception):
        operations.stage(
            path,
            _reservation(),
            "e" * 64,
            "only-in-memory",
            "2026-07-19T00:00:00.000Z",
            fault_injector=drift,
        )
    assert delivery.recoveries == delivery.stages == 0
    assert sqlite3.connect(path).execute(
        "SELECT state FROM credential_issue_stage_fences_v2"
    ).fetchone() == ("ClaimedStage",)


@pytest.mark.parametrize("drift", ("source", "principal", "approval", "grant"))
def test_scoped_materialize_prepare_drift_only_marks_cleanup_pending(
    tmp_path: Path, drift: str
) -> None:
    path = tmp_path / f"materialize-{drift}.sqlite"
    operations, source, principal, evidence, _delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    if drift == "source":
        source.available = False
    elif drift == "principal":
        principal.available = False
    elif drift == "approval":
        evidence.available = False
    elif drift == "grant":
        capability = create_credential_issue_scoped_operations_capability(
            binding_source=source,
            principal_resolver=principal,
            server_principal=_principal(),
            central_authorizer=_authorizer(allowed=False),
            approval_resolver=evidence,
            delivery=_Delivery(),
        )
        operations = create_credential_issue_scoped_operations(capability)
    with pytest.raises(Exception):
        operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("CleanupPending",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("CleanupPending",)
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "durable_credentials",
                "credential_command_receipts",
                "credential_audit_intents",
                "credential_outbox_intents",
            )
        ) == (0, 0, 0, 0)
    finally:
        connection.close()


def test_scoped_materialize_prewrite_source_drift_only_marks_cleanup_pending(
    tmp_path: Path,
) -> None:
    path = tmp_path / "materialize-prewrite.sqlite"
    operations, source, _principal, _evidence, _delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    source.calls = 0
    source.drop_after_calls = 2
    with pytest.raises(Exception):
        operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("CleanupPending",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("CleanupPending",)
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "durable_credentials",
                "credential_command_receipts",
                "credential_audit_intents",
                "credential_outbox_intents",
            )
        ) == (0, 0, 0, 0)
    finally:
        connection.close()


@pytest.mark.parametrize("drift", ("principal", "approval"))
def test_scoped_materialize_prewrite_resolver_drift_only_marks_cleanup_pending(
    tmp_path: Path, drift: str
) -> None:
    path = tmp_path / f"materialize-prewrite-{drift}.sqlite"
    operations, _source, principal, evidence, delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    if drift == "principal":
        principal.calls = 0
        principal.drop_after_calls = 1
    else:
        evidence.calls = 0
        evidence.drop_after_calls = 1
    with pytest.raises(Exception):
        operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("CleanupPending",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("CleanupPending",)
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "durable_credentials",
                "credential_command_receipts",
                "credential_audit_intents",
                "credential_outbox_intents",
            )
        ) == (0, 0, 0, 0)
    finally:
        connection.close()
    assert delivery.recoveries == delivery.stages == 1


@pytest.mark.parametrize("prewrite", (False, True))
def test_scoped_materialize_rejects_same_evidence_with_wrong_action(
    tmp_path: Path, prewrite: bool
) -> None:
    path = tmp_path / f"materialize-action-{prewrite}.sqlite"
    operations, _source, _principal, evidence, delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    if prewrite:
        evidence.calls = 0
        evidence.drop_after_calls = 0
        original = evidence.resolve_credential_approval_evidence

        def action_drift(*, org_id: str, evidence_id: str) -> CredentialApprovalEvidence | None:
            result = original(org_id=org_id, evidence_id=evidence_id)
            if evidence.calls > 1 and result is not None:
                return CredentialApprovalEvidence(
                    result.evidence_id,
                    "worker_credential.read",
                    result.command_digest,
                    result.resource_fingerprint,
                )
            return result

        evidence.resolve_credential_approval_evidence = action_drift  # type: ignore[method-assign]
    else:
        evidence.action = "worker_credential.read"
    with pytest.raises(Exception):
        operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("CleanupPending",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("CleanupPending",)
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "durable_credentials",
                "credential_command_receipts",
                "credential_audit_intents",
                "credential_outbox_intents",
            )
        ) == (0, 0, 0, 0)
    finally:
        connection.close()
    assert delivery.recoveries == delivery.stages == 1


@pytest.mark.parametrize("drift", ("catalog", "grant"))
def test_scoped_materialize_prewrite_hook_drift_only_marks_cleanup_pending(
    tmp_path: Path, drift: str
) -> None:
    path = tmp_path / f"materialize-hook-{drift}.sqlite"
    operations, _source, _principal, _evidence, delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)

    def inject(connection: sqlite3.Connection) -> None:
        if drift == "catalog":
            connection.execute("DROP TRIGGER durable_credential_scope_bindings_v1_no_update")
        else:
            operations._materialization_guard._central_authorizer = _authorizer(allowed=False)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(Exception):
        operations._materialize_for_test(  # pyright: ignore[reportPrivateUsage]
            path, "org", "target", "2026-07-19T00:00:01.000Z", prewrite_fault_injector=inject
        )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("CleanupPending",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("CleanupPending",)
        assert tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "durable_credentials",
                "credential_command_receipts",
                "credential_audit_intents",
                "credential_outbox_intents",
            )
        ) == (0, 0, 0, 0)
    finally:
        connection.close()
    assert delivery.recoveries == delivery.stages == 1


def test_scoped_public_materialize_has_no_test_only_hook() -> None:
    assert (
        "prewrite_fault_injector"
        not in __import__("inspect")
        .signature(CredentialIssueScopedOperations.materialize)
        .parameters
    )


@pytest.mark.parametrize("point", ("after_projection_insert", "after_projection_readback"))
def test_projection_faults_roll_back_the_full_committed_aggregate(
    tmp_path: Path, point: str
) -> None:
    path = tmp_path / f"projection-{point}.sqlite"
    operations, _source, _principal, _evidence, _delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    with pytest.raises(Exception):
        operations._materialize_for_test(  # pyright: ignore[reportPrivateUsage]
            path,
            "org",
            "target",
            "2026-07-19T00:00:01.000Z",
            fault_injector=lambda actual: (
                (_ for _ in ()).throw(RuntimeError(actual)) if actual == point else None
            ),
        )
    connection = sqlite3.connect(path)
    try:
        for table in (
            "durable_credentials",
            "credential_command_receipts",
            "credential_audit_intents",
            "credential_outbox_intents",
            "durable_credential_scope_projections_v1",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("Staged",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("Staged",)
    finally:
        connection.close()


def test_projection_materialize_replay_is_one_row_and_keeps_the_same_delivery_ref(
    tmp_path: Path,
) -> None:
    path = tmp_path / "projection-replay.sqlite"
    operations, _source, _principal, _evidence, _delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    first = operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    again = operations.materialize(path, "org", "target", "2026-07-19T00:00:02.000Z")
    assert again == first
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM durable_credential_scope_projections_v1"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT committed_at FROM durable_credential_scope_projections_v1"
        ).fetchone() == ("2026-07-19T00:00:01.000Z",)
    finally:
        connection.close()


def test_committed_projection_absence_is_rejected_without_repair(tmp_path: Path) -> None:
    path = tmp_path / "projection-absence.sqlite"
    operations, _source, _principal, _evidence, _delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER durable_credential_scope_projections_v1_no_delete")
        connection.execute("DELETE FROM durable_credential_scope_projections_v1")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(Exception):
        operations.materialize(path, "org", "target", "2026-07-19T00:00:02.000Z")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM durable_credential_scope_projections_v1"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_scoped_committed_read_returns_same_ref_and_source_drift_keeps_committed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "committed-read.sqlite"
    operations, source, _principal, _evidence, delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    committed = operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    again = operations.read_committed(path, "org", "target")
    assert again == committed
    assert delivery.recoveries == delivery.stages == 1
    source.available = False
    with pytest.raises(Exception):
        operations.read_committed(path, "org", "target")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("Committed",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("Committed",)
    finally:
        connection.close()


@pytest.mark.parametrize("drift", ("principal", "approval", "grant", "catalog"))
def test_scoped_committed_read_drift_never_changes_committed(tmp_path: Path, drift: str) -> None:
    path = tmp_path / f"committed-{drift}.sqlite"
    operations, source, principal, evidence, delivery = _operations(path)
    reservation = _reservation()
    operations.stage(path, reservation, "e" * 64, "only-in-memory", reservation.created_at)
    operations.materialize(path, "org", "target", "2026-07-19T00:00:01.000Z")
    if drift == "principal":
        principal.available = False
    elif drift == "approval":
        evidence.available = False
    elif drift == "grant":
        capability = create_credential_issue_scoped_operations_capability(
            binding_source=source,
            principal_resolver=principal,
            server_principal=_principal(),
            central_authorizer=_authorizer(allowed=False),
            approval_resolver=evidence,
            delivery=_Delivery(),
        )
        operations = create_credential_issue_scoped_operations(capability)
    else:
        connection = sqlite3.connect(path)
        connection.execute("DROP TRIGGER durable_credential_scope_bindings_v1_no_update")
        connection.commit()
        connection.close()
    with pytest.raises(Exception):
        operations.read_committed(path, "org", "target")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1"
        ).fetchone() == ("Committed",)
        assert connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2"
        ).fetchone() == ("Committed",)
    finally:
        connection.close()
    assert delivery.recoveries == delivery.stages == 1
