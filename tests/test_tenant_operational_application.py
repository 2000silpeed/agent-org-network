"""R2b S3.2 tenant-only operational application and adapter contract."""

# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Mapping
from typing import Any, cast

from fastapi.testclient import TestClient
import pytest

from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationGrant,
    AuthorizationResult,
    AuthorityPolicySnapshot,
    RolePermission,
    SnapshotCentralAuthorizer,
    ResourceRef,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_authorization import OperationalAction, OperationalAuthorization
from agent_org_network.sqlite_operational_tenant_sources import (
    migrate_sqlite_operational_tenant_sources,
    open_sqlite_operational_tenant_sources,
)
from agent_org_network.sqlite_tenant_port_audit_v2 import migrate_sqlite_tenant_port_audit_v2
from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    migrate_sqlite_durable_tenant_operational_mutations,
)
from agent_org_network.sqlite_durable_tenant_operational_authorization import (
    migrate_sqlite_durable_tenant_operational_authorization,
)
from agent_org_network.sqlite_tenant_operational_mutation_uow import (
    CardRegisterCommand,
    CardTransferOwnerCommand,
    HitlWriteCommand,
    SessionEndCommand,
)
from agent_org_network.tenant_operational_approval import TenantOperationalApprovalEvidence
from agent_org_network.tenant_operational_adapters import (
    SealedTenantOperationalApprovalProvider,
    TenantOperationalMutationTransport,
    create_tenant_operational_http_app,
    create_tenant_operational_mcp_server,
    create_tenant_operational_mutation_http_app,
    create_tenant_operational_mutation_mcp_server,
)
from agent_org_network.tenant_operational_application import (
    SqliteTenantOperationalDependencies,
    TenantOperationalApplication,
    TenantOperationalUnavailable,
    sqlite_tenant_operational_dependencies,
)
from agent_org_network.tenant_operational_ports import (
    ResourceFingerprint,
    SafeAuditEvent,
    TenantOrgId,
)
from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    capture_sqlite_tenant_operational_mutation_scope_snapshot,
)
from pathlib import Path


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="operator", identity_provider="oidc", identity_session_id="s1"
    )


def _authorization() -> OperationalAuthorization:
    binding = SubjectRoleBinding(org_id="acme", subject_id="operator", roles=("operator",))
    permission = RolePermission(
        role="operator",
        actions=("card.read", "org_graph.read", "session.end", "audit.read", "hitl.read"),
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    snapshot = AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="test",
        content_sha256=canonical_policy_digest(document),
        subject_roles=(binding,),
        role_permissions=(permission,),
        route_rules=(),
        worker_bindings=(),
    )
    return OperationalAuthorization(
        configured_org_id="acme", central_authorizer=SnapshotCentralAuthorizer(snapshot)
    )


def _mutation_authorization() -> OperationalAuthorization:
    binding = SubjectRoleBinding(org_id="acme", subject_id="operator", roles=("operator",))
    permission = RolePermission(
        role="operator",
        actions=("card.register", "card.transfer_owner", "session.end", "hitl.write"),
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "mutation",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    snapshot = AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="mutation",
        content_sha256=canonical_policy_digest(document),
        subject_roles=(binding,),
        role_permissions=(permission,),
        route_rules=(),
        worker_bindings=(),
    )
    return OperationalAuthorization(
        configured_org_id="acme", central_authorizer=SnapshotCentralAuthorizer(snapshot)
    )


class _ApprovalSpy:
    def __init__(self) -> None:
        self.calls = 0

    def approve(
        self,
        principal: AuthenticatedPrincipal,
        action: Action,
        resource: ResourceRef,
        command_digest: str,
        resource_fingerprint: ResourceFingerprint,
    ) -> TenantOperationalApprovalEvidence:
        self.calls += 1
        return TenantOperationalApprovalEvidence(
            "evidence:" + str(self.calls),
            "approver",
            cast(OperationalAction, action),
            command_digest,
            resource_fingerprint,
            "2026-07-19T00:00:01.000Z",
        )


def _mutation_application(
    tmp_path: Path,
) -> tuple[TenantOperationalApplication, sqlite3.Connection]:
    connection = sqlite3.connect(tmp_path / "mutation.sqlite", check_same_thread=False)
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    migrate_sqlite_durable_tenant_operational_mutations(connection)
    migrate_sqlite_durable_tenant_operational_authorization(connection)
    source = open_sqlite_operational_tenant_sources(connection)
    assert source.registry("acme").compare_and_set(
        None,
        {
            "users": ["owner", "new-owner"],
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
    return TenantOperationalApplication(
        dependencies=sqlite_tenant_operational_dependencies(connection, TenantOrgId("acme")),
        authorization=_mutation_authorization(),
    ), connection


def _command(connection: sqlite3.Connection, kind: str) -> object:
    base: dict[str, object] = {
        "org_id": "acme",
        "command_id": "command:" + kind,
        "principal_id": "operator",
        "expected_scope": capture_sqlite_tenant_operational_mutation_scope_snapshot(connection),
        "created_at": "2026-07-19T00:00:01.000Z",
    }
    if kind == "register":
        return CardRegisterCommand(**cast(Any, base), card_id="new-card", owner_id="owner")
    if kind == "transfer":
        return CardTransferOwnerCommand(**cast(Any, base), card_id="card", owner_id="new-owner")
    if kind == "session":
        return SessionEndCommand(**cast(Any, base), session_id="session")
    return HitlWriteCommand(**cast(Any, base), card_id="card", on=True)


def _application() -> tuple[TenantOperationalApplication, sqlite3.Connection]:
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    migrate_sqlite_operational_tenant_sources(connection)
    migrate_sqlite_tenant_port_audit_v2(connection)
    source = open_sqlite_operational_tenant_sources(connection)
    assert source.registry("acme").compare_and_set(
        None,
        {"users": ["owner"], "cards": {"card": {"owner": "owner"}}, "manager_refs": {}},
        "2026-07-19T00:00:00.000Z",
    )
    connection.execute(
        "INSERT INTO operational_sessions VALUES (?, ?, ?, ?, ?, ?, ?)",
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
    org = TenantOrgId("acme")
    dependencies = sqlite_tenant_operational_dependencies(connection, org)
    assert (
        dependencies.audit_writer.append(
            org,
            SafeAuditEvent(
                "session.end",
                "session",
                "succeeded",
                ResourceFingerprint.from_scalars("audit", "session"),
            ),
        )
        is None
    )
    return TenantOperationalApplication(
        dependencies=dependencies, authorization=_authorization()
    ), connection


def _mcp_value(server: object, tool: str, arguments: Mapping[str, object]) -> Any:
    _content, structured = asyncio.run(cast(Any, server).call_tool(tool, dict(arguments)))
    assert type(structured) is dict
    return structured.get("result", structured)


def test_six_exact_sqlite_dependencies_and_partial_schema_fail_closed() -> None:
    app, connection = _application()
    assert type(app._d) is SqliteTenantOperationalDependencies  # pyright: ignore[reportPrivateUsage]
    assert len(tuple(app._d.__dict__.values())) == 7  # pyright: ignore[reportPrivateUsage]
    connection.execute("DROP TABLE operational_sessions")
    with pytest.raises(TenantOperationalUnavailable):
        app.session(_principal(), "session")


def test_http_mcp_strict_dto_parity_including_graph_and_audit_detail() -> None:
    application, _connection = _application()
    http = TestClient(
        create_tenant_operational_http_app(application=application, principal_provider=_principal)
    )
    mcp = create_tenant_operational_mcp_server(
        application=application, principal_provider=_principal
    )

    endpoints = (
        ("/tenant-operational/cards", "tenant_cards", {}),
        ("/tenant-operational/cards/card", "tenant_card", {"card_id": "card"}),
        ("/tenant-operational/graph", "tenant_graph", {}),
        ("/tenant-operational/sessions/session", "tenant_session", {"session_id": "session"}),
        ("/tenant-operational/audit", "tenant_audit", {}),
        ("/tenant-operational/audit/0", "tenant_audit_detail", {"sequence": 0}),
        ("/tenant-operational/hitl/card", "tenant_hitl", {"card_id": "card"}),
    )
    for endpoint, tool, arguments in endpoints:
        response = http.get(endpoint)
        assert response.status_code == 200
        assert response.json() == _mcp_value(mcp, tool, arguments)


def test_mutations_are_uow_503_before_principal_or_body_and_mcp_never_calls_write_ports() -> None:
    application, connection = _application()
    calls = 0

    def unavailable_principal() -> AuthenticatedPrincipal:
        nonlocal calls
        calls += 1
        raise AssertionError("mutation must not resolve principal")

    http = TestClient(
        create_tenant_operational_http_app(
            application=application, principal_provider=unavailable_principal
        )
    )
    for path in (
        "/tenant-operational/cards",
        "/tenant-operational/cards/card/transfer",
        "/tenant-operational/sessions/session/end",
        "/tenant-operational/hitl/card",
        "/tenant-operational/audit",
    ):
        response = http.post(path, content=b"{not-json")
        assert response.status_code == 503
        assert response.json()["detail"] == "operational_mutation_uow_unavailable"
    mcp = create_tenant_operational_mcp_server(
        application=application, principal_provider=unavailable_principal
    )
    for tool in (
        "tenant_register_card",
        "tenant_transfer_card_owner",
        "tenant_end_session",
        "tenant_set_hitl",
        "tenant_append_audit",
    ):
        assert _mcp_value(mcp, tool, {}) == "operational_mutation_uow_unavailable"
    assert calls == 0
    assert connection.execute(
        "SELECT status FROM operational_sessions WHERE org_id='acme'"
    ).fetchone() == ("active",)
    assert connection.execute(
        "SELECT count(*) FROM operational_audit_events_v2 WHERE org_id='acme'"
    ).fetchone() == (1,)


def test_r13_transport_shares_command_receipt_and_replay_across_http_and_mcp(
    tmp_path: Path,
) -> None:
    application, connection = _mutation_application(tmp_path)
    approval = _ApprovalSpy()
    transport = TenantOperationalMutationTransport(
        application=application,
        principal_provider=_principal,
        approval=SealedTenantOperationalApprovalProvider(approval),
    )
    try:
        http = TestClient(create_tenant_operational_mutation_http_app(transport=transport))
        mcp = create_tenant_operational_mutation_mcp_server(transport=transport)
        body = {
            "command_id": "shared-command",
            "card_id": "new-card",
            "owner_id": "owner",
            "created_at": "2026-07-19T00:00:01.000Z",
        }
        first = http.post("/tenant-operational/cards", json=body)
        assert first.status_code == 200
        assert first.json()["replayed"] is False
        replay = _mcp_value(mcp, "tenant_register_card", body)
        assert replay == {**first.json(), "replayed": True}
        assert approval.calls == 1
        assert connection.execute(
            "SELECT count(*) FROM durable_tenant_operational_authorization_evidence"
        ).fetchone() == (1,)
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("kind", "path", "tool", "body"),
    [
        (
            "register",
            "/tenant-operational/cards",
            "tenant_register_card",
            {"card_id": "new-card", "owner_id": "owner"},
        ),
        (
            "transfer",
            "/tenant-operational/cards/card/transfer",
            "tenant_transfer_card_owner",
            {"card_id": "card", "owner_id": "new-owner"},
        ),
        (
            "session",
            "/tenant-operational/sessions/session/end",
            "tenant_end_session",
            {"session_id": "session"},
        ),
        (
            "hitl",
            "/tenant-operational/hitl/card",
            "tenant_set_hitl",
            {"card_id": "card", "on": True},
        ),
    ],
)
def test_r13_all_mutation_routes_share_http_mcp_receipt_replay(
    tmp_path: Path, kind: str, path: str, tool: str, body: dict[str, object]
) -> None:
    application, connection = _mutation_application(tmp_path)
    approval = _ApprovalSpy()
    transport = TenantOperationalMutationTransport(
        application=application,
        principal_provider=_principal,
        approval=SealedTenantOperationalApprovalProvider(approval),
    )
    try:
        http = TestClient(create_tenant_operational_mutation_http_app(transport=transport))
        mcp = create_tenant_operational_mutation_mcp_server(transport=transport)
        command = {
            **body,
            "command_id": kind + "-shared-command",
            "created_at": "2026-07-19T00:00:01.000Z",
        }
        first = http.post(path, json=command)
        assert first.status_code == 200
        assert first.json()["replayed"] is False
        replay = _mcp_value(mcp, tool, command)
        assert replay == {**first.json(), "replayed": True}
        assert approval.calls == 1
    finally:
        connection.close()


@pytest.mark.parametrize("created_at", [None, "", "2026-07-19T00:00:01Z", "not-a-time"])
def test_r13_missing_or_malformed_timestamp_is_http_mcp_safe_parity(
    tmp_path: Path, created_at: str | None
) -> None:
    application, connection = _mutation_application(tmp_path)
    transport = TenantOperationalMutationTransport(
        application=application,
        principal_provider=_principal,
        approval=SealedTenantOperationalApprovalProvider(_ApprovalSpy()),
    )
    try:
        command: dict[str, object] = {
            "command_id": "invalid-timestamp",
            "card_id": "new-card",
            "owner_id": "owner",
        }
        if created_at is not None:
            command["created_at"] = created_at
        http = TestClient(create_tenant_operational_mutation_http_app(transport=transport))
        response = http.post("/tenant-operational/cards", json=command)
        assert response.status_code == 503
        assert response.json()["detail"] == "operational_mutation_unavailable"
        mcp = create_tenant_operational_mutation_mcp_server(transport=transport)
        assert _mcp_value(mcp, "tenant_register_card", command) == {
            "code": "operational_mutation_unavailable"
        }
    finally:
        connection.close()


def test_r13_invalid_command_field_is_http_mcp_safe_parity(tmp_path: Path) -> None:
    application, connection = _mutation_application(tmp_path)
    transport = TenantOperationalMutationTransport(
        application=application,
        principal_provider=_principal,
        approval=SealedTenantOperationalApprovalProvider(_ApprovalSpy()),
    )
    try:
        command = {
            "command_id": "",
            "card_id": "new-card",
            "owner_id": "owner",
            "created_at": "2026-07-19T00:00:01.000Z",
        }
        response = TestClient(create_tenant_operational_mutation_http_app(transport=transport)).post(
            "/tenant-operational/cards", json=command
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "operational_mutation_unavailable"
        mcp = create_tenant_operational_mutation_mcp_server(transport=transport)
        assert _mcp_value(mcp, "tenant_register_card", command) == {
            "code": "operational_mutation_unavailable"
        }
    finally:
        connection.close()


def test_r13_partial_capability_precedes_malformed_body_and_principal() -> None:
    application, connection = _application()
    calls = 0

    def unavailable_principal() -> AuthenticatedPrincipal:
        nonlocal calls
        calls += 1
        raise AssertionError("must not resolve principal")

    transport = TenantOperationalMutationTransport(
        application=application,
        principal_provider=unavailable_principal,
        approval=SealedTenantOperationalApprovalProvider(_ApprovalSpy()),
    )
    try:
        response = TestClient(create_tenant_operational_mutation_http_app(transport=transport)).post(
            "/tenant-operational/cards", content=b"{not-json"
        )
        assert response.status_code == 503
        assert response.json()["detail"] == "operational_mutation_uow_unavailable"
        mcp = create_tenant_operational_mutation_mcp_server(transport=transport)
        assert _mcp_value(mcp, "tenant_register_card", {}) == {
            "code": "operational_mutation_uow_unavailable"
        }
        assert calls == 0
    finally:
        connection.close()


def test_reauthorization_and_source_drift_or_foreign_principal_are_unavailable() -> None:
    application, connection = _application()
    foreign = AuthenticatedPrincipal(
        org_id="other", subject_id="operator", identity_provider="oidc", identity_session_id="s2"
    )
    with pytest.raises(TenantOperationalUnavailable):
        application.cards(foreign)
    connection.execute("DROP TABLE operational_sessions")
    with pytest.raises(TenantOperationalUnavailable):
        application.session(_principal(), "session")


def test_hitl_rebuilds_card_resource_after_reread_and_rejects_owner_drift() -> None:
    application, connection = _application()
    source = open_sqlite_operational_tenant_sources(connection)
    authorization = _authorization()
    delegate = cast(SnapshotCentralAuthorizer, authorization._central_authorizer)  # pyright: ignore[reportPrivateUsage]

    class DriftAfterFirstHitlAuthorization:
        def __init__(self) -> None:
            self._drifted = False

        def authorize(
            self, principal: AuthenticatedPrincipal, action: Action, resource: ResourceRef
        ) -> AuthorizationResult:
            result = delegate.authorize(principal, action, resource)
            if action == "hitl.read" and not self._drifted:
                self._drifted = True
                assert source.registry("acme").compare_and_set(
                    0,
                    {
                        "users": ["owner", "new-owner"],
                        "cards": {"card": {"owner": "new-owner"}},
                        "manager_refs": {},
                    },
                    "2026-07-19T00:00:01.000Z",
                )
            return result

        def verify(
            self,
            grant: AuthorizationGrant,
            principal: AuthenticatedPrincipal,
            action: Action,
            resource: ResourceRef,
        ) -> bool:
            return delegate.verify(grant, principal, action, resource)

    boundary = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=DriftAfterFirstHitlAuthorization()
    )
    guarded = TenantOperationalApplication(dependencies=application._d, authorization=boundary)  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(TenantOperationalUnavailable):
        guarded.hitl(_principal(), "card")


@pytest.mark.parametrize("kind", ["register", "transfer", "session", "hitl"])
def test_named_file_plan_approval_commit_and_replay_reauthorizes_without_approval(
    tmp_path: Path, kind: str
) -> None:
    application, connection = _mutation_application(tmp_path)
    try:
        command = _command(connection, kind)
        plan = application.plan_mutation(_principal(), command)  # type: ignore[arg-type]
        approval = _ApprovalSpy()
        assert (
            application.approve_and_commit_mutation(_principal(), plan, approval).replayed is False
        )
        assert approval.calls == 1
        assert (
            application.approve_and_commit_mutation(_principal(), plan, approval).replayed is True
        )
        assert approval.calls == 1
    finally:
        connection.close()


@pytest.mark.parametrize("kind", ["register", "transfer", "session", "hitl"])
def test_replay_rejects_persisted_evidence_tamper_before_provider_call(
    tmp_path: Path, kind: str
) -> None:
    application, connection = _mutation_application(tmp_path)
    try:
        command = _command(connection, kind)
        plan = application.plan_mutation(_principal(), command)  # type: ignore[arg-type]
        approval = _ApprovalSpy()
        application.approve_and_commit_mutation(_principal(), plan, approval)
        connection.execute(
            "UPDATE durable_tenant_operational_authorization_evidence SET post_resource_fingerprint=?",
            ("0" * 64,),
        )
        connection.commit()
        with pytest.raises(TenantOperationalUnavailable):
            application.approve_and_commit_mutation(_principal(), plan, approval)
        assert approval.calls == 1
    finally:
        connection.close()


def test_named_file_requirement_and_r10_catalog_tamper_close_plan_before_authorization(
    tmp_path: Path,
) -> None:
    memory = sqlite3.connect(":memory:")
    try:
        migrate_sqlite_operational_tenant_sources(memory)
        migrate_sqlite_tenant_port_audit_v2(memory)
        migrate_sqlite_durable_tenant_operational_mutations(memory)
        with pytest.raises(RuntimeError, match="named SQLite file"):
            migrate_sqlite_durable_tenant_operational_authorization(memory)
    finally:
        memory.close()
    application, connection = _mutation_application(tmp_path)
    try:
        command = _command(connection, "register")
        connection.execute("DROP TABLE durable_tenant_operational_mutation_outbox_intents")
        with pytest.raises(TenantOperationalUnavailable):
            application.plan_mutation(_principal(), command)  # type: ignore[arg-type]
    finally:
        connection.close()
