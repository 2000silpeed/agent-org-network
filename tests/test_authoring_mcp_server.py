from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any, cast

from agent_org_network.authoring_application import AuthoringApplication, AuthoringMutation
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.demo import build_demo
from agent_org_network.mcp_server import MCP_AUTHORING_TOOL_ACTIONS, create_authoring_mcp_server
from agent_org_network.operational_application import OperationalMutationApproval
from agent_org_network.operational_authorization import OperationalAuthorization


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="cs_lead", identity_provider="oidc", identity_session_id="mcp"
    )


def _application() -> tuple[AuthoringApplication, Any]:
    bundle = build_demo()
    binding = SubjectRoleBinding(org_id="acme", subject_id="cs_lead", roles=("owner",))
    permission = RolePermission(
        role="owner", actions=("author.read", "author.write", "author.publish")
    )
    raw: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    authority = SnapshotCentralAuthorizer(
        AuthorityPolicySnapshot(
            schema_version=1,
            org_id="acme",
            policy_version="test",
            content_sha256=canonical_policy_digest(raw),
            subject_roles=(binding,),
            role_permissions=(permission,),
            route_rules=(),
            worker_bindings=(),
        )
    )
    return AuthoringApplication(
        authorization=OperationalAuthorization(
            configured_org_id="acme", central_authorizer=authority
        ),
        registry=bundle.registry,
        mutation_approval=lambda _p, _a, _r, digest, fingerprint: OperationalMutationApproval(
            outcome="allowed",
            evidence_id="human-1",
            command_digest=digest,
            resource_fingerprint=fingerprint,
        ),
        audit_log=bundle.audit,
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
    ), bundle.audit


def _text(server: object, tool: str, args: dict[str, object]) -> str:
    content, _ = asyncio.run(cast(Any, server).call_tool(tool, args))
    return "".join(item.text for item in content)


def test_authoring_mcp_uses_closed_actions_and_shared_mutation_boundary() -> None:
    application, audit = _application()
    server = create_authoring_mcp_server(
        application=application,
        principal_provider=_principal,
        read_index=lambda card: f"index:{card.agent_id}",
        publish_owner_side=lambda card, ref: (
            f"git:{card.agent_id}:{ref}",
            AuthoringMutation(card.agent_id, {"git": "done"}),
        ),
        accept_after_git=lambda git, card: f"{git}:index:{card.agent_id}",
    )
    tool_names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert tool_names == {"get_author_index", "publish_authoring"}
    assert tool_names == set(MCP_AUTHORING_TOOL_ACTIONS)
    assert "commit_author_bundle" not in tool_names
    assert "run_authoring" not in tool_names
    assert _text(server, "get_author_index", {"agent_id": "cs_ops"}) == "index:cs_ops"
    assert (
        _text(server, "publish_authoring", {"agent_id": "cs_ops", "change_ref": "draft-1"})
        == "git:cs_ops:draft-1:index:cs_ops"
    )
    assert audit.records()[-1]["action"]["channel"] == "mcp"
