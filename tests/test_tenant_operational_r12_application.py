"""R1.2 application approval binding over a named SQLite database."""
# pyright: reportArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3
from typing import Callable, Literal

import pytest

from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    AuthorizationResult,
    AuthorityPolicySnapshot,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.sqlite_durable_tenant_operational_authorization import (
    migrate_sqlite_durable_tenant_operational_authorization,
)
from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    capture_sqlite_tenant_operational_mutation_scope_snapshot,
    migrate_sqlite_durable_tenant_operational_mutations,
)
from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
    open_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_operational_mutation_uow import (
    CardRegisterCommand,
    CardTransferOwnerCommand,
    HitlWriteCommand,
    SessionEndCommand,
    TenantOperationalMutationCommand,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import migrate_sqlite_tenant_port_audit_v2
from agent_org_network.tenant_operational_application import (
    TenantOperationalApplication,
    TenantOperationalUnavailable,
    sqlite_tenant_operational_dependencies,
)
from agent_org_network.tenant_operational_approval import (
    TenantOperationalApprovalDenied,
    TenantOperationalApprovalEvidence,
)
from agent_org_network.tenant_operational_ports import ResourceFingerprint, TenantOrgId


MutationKind = Literal["register", "transfer", "session", "hitl"]


def _principal(subject_id: str = "operator") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id=subject_id, identity_provider="oidc", identity_session_id="s1"
    )


class _SwitchableCentralAuthorizer:
    def __init__(self) -> None:
        binding = SubjectRoleBinding(org_id="acme", subject_id="operator", roles=("operator",))
        permission = RolePermission(
            role="operator",
            actions=("card.register", "card.transfer_owner", "session.end", "hitl.write"),
        )
        document: dict[str, object] = {
            "schema_version": 1,
            "org_id": "acme",
            "policy_version": "r12-test",
            "content_sha256": "pending",
            "subject_roles": [binding.model_dump(mode="json")],
            "role_permissions": [permission.model_dump(mode="json")],
            "route_rules": [],
            "worker_bindings": [],
        }
        self._delegate = SnapshotCentralAuthorizer(
            AuthorityPolicySnapshot(
                schema_version=1,
                org_id="acme",
                policy_version="r12-test",
                content_sha256=canonical_policy_digest(document),
                subject_roles=(binding,),
                role_permissions=(permission,),
                route_rules=(),
                worker_bindings=(),
            )
        )
        self.denied = False
        self.authorize_calls = 0
        self.deny_on_authorize_call: int | None = None

    def authorize(
        self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
    ) -> AuthorizationResult:
        self.authorize_calls += 1
        if self.denied or self.deny_on_authorize_call == self.authorize_calls:
            return AuthorizationDenied(kind="not_found_or_denied")
        return self._delegate.authorize(principal, action, resource)

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
    ) -> bool:
        return not self.denied and self._delegate.verify(grant, principal, action, resource)


class _ApprovalSpy:
    def __init__(self, *, outcome: Literal["approved", "denied"] = "approved") -> None:
        self.calls = 0
        self.outcome = outcome
        self.after_approve: Callable[[], None] | None = None
        self.tamper: Literal["action", "digest", "resource"] | None = None

    def approve(
        self,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
        command_digest: str,
        resource_fingerprint: ResourceFingerprint,
    ) -> TenantOperationalApprovalEvidence | TenantOperationalApprovalDenied:
        self.calls += 1
        if self.outcome == "denied":
            return TenantOperationalApprovalDenied()
        evidence_action = "session.end" if self.tamper == "action" else action
        evidence_digest = "0" * 64 if self.tamper == "digest" else command_digest
        evidence_fingerprint = (
            ResourceFingerprint("1" * 64) if self.tamper == "resource" else resource_fingerprint
        )
        result = TenantOperationalApprovalEvidence(
            evidence_id=f"evidence-{self.calls}",
            approver_subject_id="approver",
            action=evidence_action,
            command_digest=evidence_digest,
            resource_fingerprint=evidence_fingerprint,
            approved_at="2026-07-19T00:00:01.000Z",
        )
        if self.after_approve is not None:
            self.after_approve()
        return result


def _command(
    connection: sqlite3.Connection, kind: MutationKind
) -> TenantOperationalMutationCommand:
    base = {
        "org_id": "acme",
        "command_id": f"{kind}-command",
        "principal_id": "operator",
        "expected_scope": capture_sqlite_tenant_operational_mutation_scope_snapshot(connection),
        "created_at": "2026-07-19T00:00:01.000Z",
    }
    if kind == "register":
        return CardRegisterCommand(**base, card_id="new-card", owner_id="owner-two")
    if kind == "transfer":
        return CardTransferOwnerCommand(**base, card_id="card", owner_id="owner-two")
    if kind == "session":
        return SessionEndCommand(**base, session_id="session")
    return HitlWriteCommand(**base, card_id="card", on=True)


def _application(
    path: Path, *, r12: bool = True
) -> tuple[TenantOperationalApplication, sqlite3.Connection, _SwitchableCentralAuthorizer]:
    connection = sqlite3.connect(path)
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    migrate_sqlite_durable_tenant_operational_mutations(connection)
    if r12:
        migrate_sqlite_durable_tenant_operational_authorization(connection)
    source = open_sqlite_operational_tenant_sources(connection)
    assert source.registry("acme").compare_and_set(
        None,
        {
            "users": ["owner", "owner-two", "owner-three"],
            "cards": {"card": {"owner": "owner"}},
            "manager_refs": {},
        },
        "2026-07-19T00:00:00.000Z",
    )
    connection.execute(
        "INSERT INTO operational_sessions VALUES(?,?,?,?,?,?,?)",
        (
            "acme",
            "session",
            "operator",
            "active",
            "2026-07-19T00:00:00.000Z",
            "2026-07-19T00:00:00.000Z",
            0,
        ),
    )
    connection.commit()
    central = _SwitchableCentralAuthorizer()
    application = TenantOperationalApplication(
        dependencies=sqlite_tenant_operational_dependencies(connection, TenantOrgId("acme")),
        authorization=OperationalAuthorization(
            configured_org_id="acme", central_authorizer=central
        ),
    )
    return application, connection, central


def _write_count(connection: sqlite3.Connection) -> tuple[int, int, int]:
    counts = tuple(
        int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
        for table in (
            "durable_tenant_operational_mutation_receipts",
            "durable_tenant_operational_authorization_evidence",
            "durable_tenant_operational_mutation_audit_intents",
        )
    )
    assert len(counts) == 3
    return counts[0], counts[1], counts[2]


@pytest.mark.parametrize("kind", ["register", "transfer", "session", "hitl"])
def test_all_r12_actions_plan_approve_commit_and_persist_bound_evidence(
    tmp_path: Path, kind: MutationKind
) -> None:
    application, connection, _central = _application(tmp_path / f"{kind}.sqlite")
    try:
        plan = application.plan_mutation(_principal(), _command(connection, kind))
        approval = _ApprovalSpy()
        receipt = application.approve_and_commit_mutation(_principal(), plan, approval)
        assert receipt.action == plan.action
        assert receipt.replayed is False
        assert approval.calls == 1
        assert _write_count(connection) == (1, 1, 1)
        action, _uow_digest, approval_digest, pre_fingerprint = connection.execute(
            "SELECT action,command_digest,approval_command_digest,pre_resource_fingerprint "
            "FROM durable_tenant_operational_authorization_evidence"
        ).fetchone()
        assert (action, approval_digest, pre_fingerprint) == (
            plan.action,
            plan.command_digest,
            plan.pre_fingerprint.value,
        )
    finally:
        connection.close()


@pytest.mark.parametrize("tamper", ["action", "digest", "resource"])
def test_tampered_approval_evidence_denies_with_no_write(tmp_path: Path, tamper: str) -> None:
    application, connection, _central = _application(tmp_path / f"evidence-{tamper}.sqlite")
    try:
        approval = _ApprovalSpy()
        approval.tamper = tamper  # type: ignore[assignment]
        plan = application.plan_mutation(_principal(), _command(connection, "transfer"))
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, approval)
        assert approval.calls == 1
        assert _write_count(connection) == (0, 0, 0)
    finally:
        connection.close()


def test_denied_approval_and_tampered_principal_deny_with_no_write(tmp_path: Path) -> None:
    application, connection, _central = _application(tmp_path / "principal.sqlite")
    try:
        plan = application.plan_mutation(_principal(), _command(connection, "register"))
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(
                _principal(), plan, _ApprovalSpy(outcome="denied")
            )
        tampered = replace(plan, command=replace(plan.command, principal_id="other"))
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), tampered, _ApprovalSpy())
        assert _write_count(connection) == (0, 0, 0)
    finally:
        connection.close()


def test_source_owner_revision_and_policy_drift_close_before_commit(tmp_path: Path) -> None:
    application, connection, central = _application(tmp_path / "drift.sqlite")
    try:
        plan = application.plan_mutation(_principal(), _command(connection, "transfer"))
        source = open_sqlite_operational_tenant_sources(connection)
        approval = _ApprovalSpy()

        def drift_owner_and_revision() -> None:
            assert source.registry("acme").compare_and_set(
                0,
                {
                    "users": ["owner", "owner-two"],
                    "cards": {"card": {"owner": "owner-two"}},
                    "manager_refs": {},
                },
                "2026-07-19T00:00:02.000Z",
            )

        approval.after_approve = drift_owner_and_revision
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, approval)
        assert approval.calls == 1
        assert _write_count(connection) == (0, 0, 0)

        application, connection, central = _application(tmp_path / "policy.sqlite")
        plan = application.plan_mutation(_principal(), _command(connection, "session"))
        approval = _ApprovalSpy()
        approval.after_approve = lambda: setattr(central, "denied", True)
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, approval)
        assert approval.calls == 1
        assert _write_count(connection) == (0, 0, 0)
    finally:
        connection.close()


def test_final_prewrite_reauthorization_closes_policy_flip_after_post_mirror(
    tmp_path: Path,
) -> None:
    application, connection, central = _application(tmp_path / "final-policy-gap.sqlite")
    try:
        plan = application.plan_mutation(_principal(), _command(connection, "session"))
        # authorize calls: initial plan, approval-time replan, post-approval
        # replan, then the final pre-UoW pre-resource reauthorization.
        central.deny_on_authorize_call = 4
        approval = _ApprovalSpy()
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, approval)
        assert approval.calls == 1
        assert _write_count(connection) == (0, 0, 0)
        assert connection.execute(
            "SELECT status,revision FROM operational_sessions WHERE org_id='acme' AND session_id='session'"
        ).fetchone() == ("active", 0)
    finally:
        connection.close()


def test_exact_replay_requires_current_reauthorization_without_new_approval(tmp_path: Path) -> None:
    application, connection, central = _application(tmp_path / "replay.sqlite")
    try:
        plan = application.plan_mutation(_principal(), _command(connection, "hitl"))
        assert (
            application.approve_and_commit_mutation(_principal(), plan, _ApprovalSpy()).replayed
            is False
        )
        replay_approval = _ApprovalSpy()
        assert (
            application.approve_and_commit_mutation(_principal(), plan, replay_approval).replayed
            is True
        )
        assert replay_approval.calls == 0
        central.denied = True
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, _ApprovalSpy())
        assert _write_count(connection) == (1, 1, 1)
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["register", "transfer", "session", "hitl"])
def test_replay_closes_when_current_post_state_drifted_for_each_action(
    tmp_path: Path, kind: MutationKind
) -> None:
    application, connection, _central = _application(tmp_path / f"post-drift-{kind}.sqlite")
    try:
        plan = application.plan_mutation(_principal(), _command(connection, kind))
        assert (
            application.approve_and_commit_mutation(_principal(), plan, _ApprovalSpy()).replayed
            is False
        )
        source = open_sqlite_operational_tenant_sources(connection)
        if kind in {"register", "transfer"}:
            current = source.registry("acme").read()
            assert current is not None
            revision, _payload = current
            cards: dict[str, object] = (
                {"card": {"owner": "owner"}, "new-card": {"owner": "owner-three"}}
                if kind == "register"
                else {"card": {"owner": "owner-three"}}
            )
            assert source.registry("acme").compare_and_set(
                revision,
                {
                    "users": ["owner", "owner-two", "owner-three"],
                    "cards": cards,
                    "manager_refs": {},
                },
                "2026-07-19T00:00:02.000Z",
            )
        elif kind == "session":
            connection.execute(
                "UPDATE operational_sessions SET status='active', revision=revision+1 "
                "WHERE org_id='acme' AND session_id='session'"
            )
            connection.commit()
        else:
            assert source.hitl("acme").compare_and_set(
                "card", 0, False, True, "2026-07-19T00:00:02.000Z"
            )
        replay_approval = _ApprovalSpy()
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, replay_approval)
        assert replay_approval.calls == 0
        assert _write_count(connection) == (1, 1, 1)
    finally:
        connection.close()


def test_persisted_evidence_tamper_makes_replay_unavailable(tmp_path: Path) -> None:
    application, connection, _central = _application(tmp_path / "evidence-replay.sqlite")
    try:
        plan = application.plan_mutation(_principal(), _command(connection, "session"))
        application.approve_and_commit_mutation(_principal(), plan, _ApprovalSpy())
        connection.execute(
            "UPDATE durable_tenant_operational_authorization_evidence "
            "SET approval_command_digest='0' || substr(approval_command_digest, 2)"
        )
        connection.commit()
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, _ApprovalSpy())
        assert _write_count(connection) == (1, 1, 1)
    finally:
        connection.close()


@pytest.mark.parametrize("tamper", ["r10-only", "catalog", "ddl"])
def test_r12_schema_unavailable_cases_close_before_approval_and_write(
    tmp_path: Path, tamper: str
) -> None:
    application, connection, _central = _application(
        tmp_path / f"schema-{tamper}.sqlite", r12=tamper != "r10-only"
    )
    try:
        if tamper == "catalog":
            connection.execute(
                "UPDATE schema_component_manifests SET manifest_sha256='0' WHERE component_id='durable_tenant_operational_authorization_v1'"
            )
            connection.commit()
        elif tamper == "ddl":
            connection.execute("DROP TABLE durable_tenant_operational_authorization_evidence")
        plan = application.plan_mutation(_principal(), _command(connection, "register"))
        approval = _ApprovalSpy()
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, approval)
        assert approval.calls == 0
        assert connection.execute(
            "SELECT count(*) FROM durable_tenant_operational_mutation_receipts"
        ).fetchone() == (0,)
    finally:
        connection.close()
