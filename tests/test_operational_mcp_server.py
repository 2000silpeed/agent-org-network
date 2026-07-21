"""P17.8 P0 운영 MCP의 권한·승인·감사 경계 회귀."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, cast

import pytest

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    AuthorizationDenied,
    CentralAuthorizer,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.demo import build_demo
from agent_org_network.admin_registry import AdminRegistryService
from agent_org_network.hitl import HitlToggleMap, seed_from_card
import agent_org_network.mcp_server as mcp_server_module
from agent_org_network.mcp_server import MCP_OPERATIONAL_TOOL_ACTIONS, create_operational_mcp_server
from agent_org_network.operational_application import (
    OperationalApplication,
    OperationalMutationApproval,
    MutationApprovalProvider,
)
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.operational_source_scope import (
    OperationalSourceSnapshot,
    compose_operational_source_scope_proofs,
)
from agent_org_network.session import InMemorySessionStore


class _ScopedSource:
    def __init__(self, target: object) -> None:
        self.target = target
        self.revision = "r1"
        self.digest = "d1"
        self.row_org_ids = ("acme",)

    def operational_source_scope_snapshot(self) -> OperationalSourceSnapshot:
        return OperationalSourceSnapshot(
            source_instance=self.target,
            org_id="acme",
            revision=self.revision,
            snapshot_digest=self.digest,
            row_org_ids=self.row_org_ids,
        )


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id="cs_lead",
        identity_provider="oidc",
        identity_session_id="s-1",
    )


def _authority() -> SnapshotCentralAuthorizer:
    binding = SubjectRoleBinding(org_id="acme", subject_id="cs_lead", roles=("operator",))
    permission = RolePermission(
        role="operator",
        actions=(
            "monitor.read",
            "audit.read",
            "org_graph.read",
            "session.end",
            "hitl.read",
            "hitl.write",
            "card.read",
            "card.register",
            "card.transfer_owner",
        ),
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
    digest = canonical_policy_digest(document)
    return SnapshotCentralAuthorizer(
        AuthorityPolicySnapshot(
            schema_version=1,
            org_id="acme",
            policy_version="test",
            content_sha256=digest,
            subject_roles=(binding,),
            role_permissions=(permission,),
            route_rules=(),
            worker_bindings=(),
        )
    )


def _application(
    *,
    approval: str = "allowed",
    audit_enabled: bool = True,
    authority: CentralAuthorizer | None = None,
    mutation_approval: MutationApprovalProvider | None = None,
    source_scope: bool = True,
) -> tuple[OperationalApplication, InMemorySessionStore, Any]:
    bundle = build_demo()
    sessions = InMemorySessionStore()
    toggles = HitlToggleMap()
    explicit: set[str] = set()
    boundary = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=authority or _authority()
    )

    def gate(
        _principal: AuthenticatedPrincipal,
        _action: object,
        _resource: object,
        command_digest: str,
        resource_fingerprint: str,
    ) -> OperationalMutationApproval:
        return OperationalMutationApproval(
            outcome=approval,
            evidence_id="approval-1",
            command_digest=command_digest,
            resource_fingerprint=resource_fingerprint,
        )

    approval_gate = mutation_approval or gate
    def graph_source() -> dict[str, list[object]]:
        return {"nodes": [], "edges": []}
    audit_source = (bundle.audit_reader, bundle.audit)
    source_scope_proofs = (
        compose_operational_source_scope_proofs(
            configured_org_id="acme",
            registry=_ScopedSource(bundle.registry),
            graph=_ScopedSource(graph_source),
            session=_ScopedSource(sessions),
            audit=_ScopedSource(audit_source),
            hitl=_ScopedSource(toggles),
        )
        if source_scope
        else None
    )

    app = OperationalApplication(
        authorization=boundary,
        registry=bundle.registry,
        audit_reader=bundle.audit_reader,
        audit_log=bundle.audit if audit_enabled else None,
        session_store=sessions,
        hitl_is_on=toggles.is_on,
        hitl_set=toggles.set,
        hitl_is_explicit=lambda agent_id: agent_id in explicit,
        hitl_mark_explicit=explicit.add,
        hitl_seed=seed_from_card,
        monitor_summary=lambda index, record: {"index": index, "record": record},
        org_graph=graph_source,
        mutation_approval=approval_gate,
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
        admin_registry_service=AdminRegistryService(bundle.registry, audit_sink=bundle.audit),
        source_scope_proofs=source_scope_proofs,
        audit_source=audit_source,
        hitl_source=toggles,
    )
    return app, sessions, bundle.audit


def _text(server: object, tool: str, arguments: dict[str, object]) -> str:
    content, _structured = asyncio.run(cast(Any, server).call_tool(tool, arguments))
    return "".join(item.text for item in content)


def test_mutation_is_approval_gated_and_success_is_audited() -> None:
    application, sessions, audit = _application()
    session = sessions.open_or_get("cs_lead")
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    assert "종료 완료" in _text(server, "end_session", {"session_id": session.session_id})
    ended = sessions.get(session.session_id)
    assert ended is not None
    assert ended.status == "ended"
    record = audit.records()[-1]
    assert {
        key: value for key, value in record["action"].items() if key != "approval_command_digest"
    } == {
        "kind": "session.end",
        "subject_id": session.session_id,
        "by": "cs_lead",
        "channel": "mcp",
        "outcome": "succeeded",
        "approval_evidence_id": "approval-1",
    }
    assert len(record["action"]["approval_command_digest"]) == 64


def test_raw_operational_application_without_scope_proof_is_unavailable_and_writes_zero() -> None:
    application, sessions, audit = _application(source_scope=False)
    session = sessions.open_or_get("cs_lead")
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    assert _text(server, "end_session", {"session_id": session.session_id}) == (
        "운영 권한 또는 승인 상태를 일시적으로 확인할 수 없습니다."
    )
    active = sessions.get(session.session_id)
    assert active is not None and active.status == "active"
    assert audit.records() == []


def test_operational_mcp_factory_registers_exactly_the_closed_action_matrix() -> None:
    application, _sessions, _audit = _application()
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    assert {tool.name for tool in asyncio.run(server.list_tools())} == set(
        MCP_OPERATIONAL_TOOL_ACTIONS
    )


def test_operational_mcp_factory_rejects_action_map_handler_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application, _sessions, _audit = _application()
    mismatched = dict(MCP_OPERATIONAL_TOOL_ACTIONS)
    mismatched["get_monitor"] = "audit.read"
    monkeypatch.setattr(
        mcp_server_module,
        "MCP_OPERATIONAL_TOOL_ACTIONS",
        MappingProxyType(mismatched),
    )

    with pytest.raises(RuntimeError, match="등록 매트릭스"):
        create_operational_mcp_server(application=application, principal_provider=_principal)


def test_denied_approval_leaves_mutation_unchanged_without_audit() -> None:
    application, sessions, audit = _application(approval="denied")
    session = sessions.open_or_get("cs_lead")
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    text = _text(server, "end_session", {"session_id": session.session_id})

    assert "권한" in text
    active = sessions.get(session.session_id)
    assert active is not None
    assert active.status == "active"
    assert audit.records() == []


def test_mcp_end_session_reauthorizes_after_human_approval_before_store_write() -> None:
    class RevokedAfterApproval:
        def __init__(self) -> None:
            self._delegate = _authority()
            self.revoked = False

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            if self.revoked:
                return AuthorizationDenied(kind="not_found_or_denied")
            return self._delegate.authorize(principal, cast(Any, action), resource)

        def verify(
            self, grant: object, principal: object, action: object, resource: object
        ) -> bool:
            return self._delegate.verify(
                cast(Any, grant), cast(Any, principal), action, cast(Any, resource)
            )

    authorizer = RevokedAfterApproval()

    def approve_then_revoke(
        _principal: AuthenticatedPrincipal,
        _action: object,
        _resource: object,
        digest: str,
        fingerprint: str,
    ) -> OperationalMutationApproval:
        authorizer.revoked = True
        return OperationalMutationApproval(
            outcome="allowed",
            evidence_id="human-approved-before-revoke",
            command_digest=digest,
            resource_fingerprint=fingerprint,
        )

    application, sessions, audit = _application(
        authority=cast(CentralAuthorizer, authorizer), mutation_approval=approve_then_revoke
    )
    session = sessions.open_or_get("cs_lead")
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    assert _text(server, "end_session", {"session_id": session.session_id}) == (
        "운영 대상을 찾을 수 없거나 권한이 없습니다."
    )
    active = sessions.get(session.session_id)
    assert active is not None and active.status == "active"
    assert audit.records() == []


def test_legacy_four_argument_approval_provider_is_unavailable_and_writes_nothing() -> None:
    def legacy(
        _principal: AuthenticatedPrincipal,
        _action: object,
        _resource: object,
        _digest: str,
    ) -> OperationalMutationApproval:
        raise AssertionError("legacy provider must never receive a compatible call")

    application, sessions, audit = _application(mutation_approval=cast(Any, legacy))
    session = sessions.open_or_get("cs_lead")
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    assert _text(server, "end_session", {"session_id": session.session_id}) == (
        "운영 권한 또는 승인 상태를 일시적으로 확인할 수 없습니다."
    )
    active = sessions.get(session.session_id)
    assert active is not None and active.status == "active"
    assert audit.records() == []


def test_missing_audit_sink_blocks_mcp_mutations_before_session_or_hitl_write() -> None:
    application, sessions, audit = _application(audit_enabled=False)
    session = sessions.open_or_get("cs_lead")
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    ended = _text(server, "end_session", {"session_id": session.session_id})
    hitl = _text(server, "set_hitl", {"agent_id": "cs_ops", "on": True})

    assert ended == "운영 권한 또는 승인 상태를 일시적으로 확인할 수 없습니다."
    assert hitl == "운영 권한 또는 승인 상태를 일시적으로 확인할 수 없습니다."
    active = sessions.get(session.session_id)
    assert active is not None
    assert active.status == "active"
    assert audit.records() == []


def test_card_mcp_tools_use_shared_admission_and_audit_boundary() -> None:
    application, _sessions, audit = _application()
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    listed = _text(server, "list_cards", {})
    registered = _text(
        server,
        "register_card",
        {
            "agent_id": "mcp_ops",
            "owner": "cs_lead",
            "team": "operations",
            "summary": "MCP 등록 카드",
            "domains": ["operations"],
            "last_reviewed_at": "2026-07-15",
        },
    )
    fetched = _text(server, "get_card", {"agent_id": "mcp_ops"})
    transferred = _text(
        server, "transfer_card_owner", {"agent_id": "mcp_ops", "new_owner": "legal_lead"}
    )

    assert "cs_ops" in listed
    assert "등록 완료: mcp_ops" in registered
    assert "Agent Card: mcp_ops" in fetched
    assert "이전 완료: mcp_ops" in transferred
    assert {
        key: value
        for key, value in audit.records()[-1]["action"].items()
        if key != "approval_command_digest"
    } == {
        "kind": "card.transfer_owner",
        "subject_id": "mcp_ops",
        "by": "cs_lead",
        "channel": "mcp",
        "outcome": "succeeded",
        "from_owner": "cs_lead",
        "to_owner": "legal_lead",
        "approval_evidence_id": "approval-1",
    }
    assert len(audit.records()[-1]["action"]["approval_command_digest"]) == 64


def test_mcp_replayed_evidence_cannot_end_changed_session_twice() -> None:
    saved: OperationalMutationApproval | None = None

    def replay(
        _principal: AuthenticatedPrincipal,
        _action: object,
        _resource: object,
        digest: str,
        fingerprint: str,
    ) -> OperationalMutationApproval:
        nonlocal saved
        if saved is None:
            saved = OperationalMutationApproval(
                outcome="allowed",
                evidence_id="approval-once",
                command_digest=digest,
                resource_fingerprint=fingerprint,
            )
        return saved

    application, sessions, audit = _application(mutation_approval=replay)
    session = sessions.open_or_get("cs_lead")
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    assert "종료 완료" in _text(server, "end_session", {"session_id": session.session_id})
    assert _text(server, "end_session", {"session_id": session.session_id}) == (
        "운영 대상을 찾을 수 없거나 권한이 없습니다."
    )
    assert len(audit.records()) == 1


def test_card_mcp_denied_principal_never_leaks_cards_or_writes() -> None:
    application, _sessions, audit = _application()
    server = create_operational_mcp_server(
        application=application,
        principal_provider=lambda: AuthenticatedPrincipal(
            org_id="acme",
            subject_id="unprivileged",
            identity_provider="oidc",
            identity_session_id="s-denied",
        ),
    )

    listed = _text(server, "list_cards", {})
    fetched = _text(server, "get_card", {"agent_id": "cs_ops"})
    registered = _text(
        server,
        "register_card",
        {
            "agent_id": "denied_ops",
            "owner": "cs_lead",
            "team": "operations",
            "summary": "권한 없는 등록",
            "domains": ["operations"],
            "last_reviewed_at": "2026-07-15",
        },
    )
    transferred = _text(
        server, "transfer_card_owner", {"agent_id": "cs_ops", "new_owner": "legal_lead"}
    )

    neutral = "운영 대상을 찾을 수 없거나 권한이 없습니다."
    assert (listed, fetched, registered, transferred) == (neutral, neutral, neutral, neutral)
    assert "cs_ops" not in listed
    registry = cast(Any, application)._registry
    assert not registry.has_card("denied_ops")
    assert registry.get("cs_ops").owner == "cs_lead"
    assert audit.records() == []


def test_card_mcp_list_never_returns_partially_authorized_cards() -> None:
    """한 카드라도 현재 owner 기준으로 막히면 목록 전체를 중립 응답으로 닫는다."""
    binding = SubjectRoleBinding(org_id="acme", subject_id="cs_lead", roles=("owner",))
    permission = RolePermission(role="owner", actions=("card.read",))
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "owner-only",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    digest = canonical_policy_digest(document)
    authority = SnapshotCentralAuthorizer(
        AuthorityPolicySnapshot(
            schema_version=1,
            org_id="acme",
            policy_version="owner-only",
            content_sha256=digest,
            subject_roles=(binding,),
            role_permissions=(permission,),
            route_rules=(),
            worker_bindings=(),
        )
    )
    application, _sessions, audit = _application(authority=authority)
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    result = _text(server, "list_cards", {})

    assert result == "운영 대상을 찾을 수 없거나 권한이 없습니다."
    assert "cs_ops" not in result
    assert "contract_ops" not in result
    assert audit.records() == []


def test_card_mcp_authorizer_outage_never_leaks_cards_or_writes() -> None:
    class _UnavailableAuthorizer:
        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            del principal, action, resource
            raise RuntimeError("authority offline")

        def verify(
            self,
            grant: object,
            principal: AuthenticatedPrincipal,
            action: object,
            resource: ResourceRef,
        ) -> bool:
            del grant, principal, action, resource
            raise AssertionError("authorize outage must not verify")

    application, _sessions, audit = _application(
        authority=cast(CentralAuthorizer, _UnavailableAuthorizer())
    )
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    listed = _text(server, "list_cards", {})
    registered = _text(
        server,
        "register_card",
        {
            "agent_id": "offline_ops",
            "owner": "cs_lead",
            "team": "operations",
            "summary": "권한 장애 등록",
            "domains": ["operations"],
            "last_reviewed_at": "2026-07-15",
        },
    )

    unavailable = "운영 권한 또는 승인 상태를 일시적으로 확인할 수 없습니다."
    assert (listed, registered) == (unavailable, unavailable)
    assert "cs_ops" not in listed
    registry = cast(Any, application)._registry
    assert not registry.has_card("offline_ops")
    assert audit.records() == []


def test_card_mcp_transfer_policy_drift_before_write_leaves_state_and_audit_unchanged() -> None:
    class _RevokedAfterFirstGrant:
        def __init__(self) -> None:
            self._delegate = _authority()
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            self._calls += 1
            if self._calls > 1:
                return AuthorizationDenied(kind="not_found_or_denied")
            return self._delegate.authorize(principal, action, resource)

        def verify(
            self,
            grant: object,
            principal: AuthenticatedPrincipal,
            action: object,
            resource: ResourceRef,
        ) -> bool:
            return self._delegate.verify(cast(Any, grant), principal, action, resource)

    application, _sessions, audit = _application(
        authority=cast(CentralAuthorizer, _RevokedAfterFirstGrant())
    )
    server = create_operational_mcp_server(application=application, principal_provider=_principal)

    result = _text(server, "transfer_card_owner", {"agent_id": "cs_ops", "new_owner": "legal_lead"})

    assert result == "운영 대상을 찾을 수 없거나 권한이 없습니다."
    registry = cast(Any, application)._registry
    assert registry.get("cs_ops").owner == "cs_lead"
    assert audit.records() == []


def test_card_mcp_rejects_noncanonical_server_principal_with_safe_error() -> None:
    application, _sessions, audit = _application()
    server = create_operational_mcp_server(
        application=application,
        principal_provider=cast(Any, lambda: object()),
    )

    result = _text(server, "get_card", {"agent_id": "cs_ops"})

    assert result == "운영 요청을 처리하지 못했습니다."
    assert "cs_ops" not in result
    assert audit.records() == []
