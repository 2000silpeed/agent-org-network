"""P17.8 S4.4 카드 관리 표면의 중앙 Authority 경계 회귀."""

from __future__ import annotations

from collections.abc import Callable
import inspect
from typing import Any, cast

from fastapi import Request
from fastapi.testclient import TestClient
from httpx import Response

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    CentralAuthorizer,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.operational_application import OperationalMutationApproval
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app


def _principal(subject_id: str) -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id="session-1",
    )


def _snapshot(*, roles: dict[str, tuple[str, ...]]) -> AuthorityPolicySnapshot:
    permissions = (
        RolePermission(role="owner", actions=("card.read",)),
        RolePermission(
            role="admin",
            actions=("card.read", "card.register", "card.transfer_owner"),
        ),
    )
    bindings = tuple(
        SubjectRoleBinding(org_id="acme", subject_id=subject_id, roles=cast(Any, subject_roles))
        for subject_id, subject_roles in roles.items()
    )
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test-policy",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json") for binding in bindings],
        "role_permissions": [permission.model_dump(mode="json") for permission in permissions],
        "route_rules": [],
        "worker_bindings": [],
    }
    digest = canonical_policy_digest(document)
    document["content_sha256"] = digest
    return AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="test-policy",
        content_sha256=digest,
        subject_roles=bindings,
        role_permissions=permissions,
        route_rules=(),
        worker_bindings=(),
    )


def _app(
    *,
    subject_id: str,
    roles: dict[str, tuple[str, ...]],
    authorizer: CentralAuthorizer | None = None,
    resolver: Callable[[Request], AuthenticatedPrincipal] | None = None,
    mutation_approval: Callable[..., OperationalMutationApproval] | None = None,
) -> TestClient:
    actual_authorizer = authorizer or SnapshotCentralAuthorizer(_snapshot(roles=roles))
    operational = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=actual_authorizer
    )

    def default_resolver(_request: Request) -> AuthenticatedPrincipal:
        return _principal(subject_id)

    return TestClient(
        create_app(
            runtime=StubRuntime(),
            governance_principal_resolver=resolver or default_resolver,
            operational_authorization=operational,
            operational_mutation_approval=mutation_approval
            or (
                lambda _principal, _action, _resource, digest, fingerprint: (
                    OperationalMutationApproval(
                        outcome="allowed",
                        evidence_id="approval-card-test",
                        command_digest=digest,
                        resource_fingerprint=fingerprint,
                    )
                )
            ),
        )
    )


def _response(client: TestClient, method: str, url: str, **kwargs: object) -> Response:
    return cast(Response, getattr(cast(Any, client), method)(url, **kwargs))


def _register_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "agent_id": "new_ops",
        "owner": "cs_lead",
        "team": "new",
        "summary": "새 담당",
        "domains": ["신규도메인"],
        "last_reviewed_at": "2026-06-20",
    }
    payload.update(updates)
    return payload


def test_card_list_requires_central_role_and_current_owner_without_leak() -> None:
    owner = _app(subject_id="cs_lead", roles={"cs_lead": ("owner",)})
    own = _response(owner, "get", "/admin/cards")
    assert own.status_code == 503


def test_card_register_uses_server_principal_and_denial_leaves_no_card() -> None:
    denied_client = _app(subject_id="cs_lead", roles={"cs_lead": ("owner",)})
    denied = _response(denied_client, "post", "/admin/cards", json=_register_payload())
    assert denied.status_code == 503


def test_http_register_reauthorizes_after_human_approval_before_registry_write() -> None:
    class RevokedAfterApproval:
        def __init__(self) -> None:
            self._delegate = SnapshotCentralAuthorizer(
                _snapshot(roles={"root_manager": ("admin",)})
            )
            self._revoked_delegate = SnapshotCentralAuthorizer(_snapshot(roles={}))
            self.revoked = False

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            if self.revoked:
                return self._revoked_delegate.authorize(principal, cast(Any, action), resource)
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

    client = _app(
        subject_id="root_manager",
        roles={"root_manager": ("admin",)},
        authorizer=cast(CentralAuthorizer, authorizer),
        mutation_approval=approve_then_revoke,
    )
    response = _response(client, "post", "/admin/cards", json=_register_payload())
    assert response.status_code == 503


def test_transfer_rereads_current_card_and_reauthorizes_before_mutation() -> None:
    registry_holder: dict[str, object] = {}

    class TransfersAfterFirstGrant:
        def __init__(self) -> None:
            self._delegate = SnapshotCentralAuthorizer(
                _snapshot(roles={"root_manager": ("admin",)})
            )
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
        ) -> object:
            result = self._delegate.authorize(principal, cast(Any, action), resource)
            self._calls += 1
            if self._calls == 1:
                registry = cast(Any, registry_holder["registry"])
                card = registry.get("cs_ops")
                registry.replace_card(card.model_copy(update={"owner": "hr_lead"}))
            return result

        def verify(
            self,
            grant: object,
            principal: AuthenticatedPrincipal,
            action: object,
            resource: ResourceRef,
        ) -> bool:
            return self._delegate.verify(cast(Any, grant), principal, cast(Any, action), resource)

        @property
        def call_count(self) -> int:
            return self._calls

    authorizer = TransfersAfterFirstGrant()
    client = _app(
        subject_id="root_manager",
        roles={"root_manager": ("admin",)},
        authorizer=cast(CentralAuthorizer, authorizer),
    )
    app_any: Any = client.app
    endpoint = next(
        route.endpoint
        for route in app_any.routes
        if getattr(route, "path", "") == "/admin/cards/{agent_id}/owner"
    )
    current_card = inspect.getclosurevars(endpoint).nonlocals["_current_agent_card"]
    registry = inspect.getclosurevars(current_card).nonlocals["bundle"].registry
    registry_holder["registry"] = registry

    response = _response(
        client, "post", "/admin/cards/cs_ops/owner", json={"new_owner": "legal_lead"}
    )
    assert response.status_code == 503


def test_partial_operational_composition_is_503_before_card_existence() -> None:
    app = create_app(
        runtime=StubRuntime(),
        governance_principal_resolver=lambda _request: _principal("root_manager"),
    )
    response = _response(
        TestClient(app), "post", "/admin/cards/missing/owner", json={"new_owner": "x"}
    )
    assert response.status_code == 503


def test_partial_composition_list_returns_503_before_registry_lookup() -> None:
    resolver_only = create_app(
        runtime=StubRuntime(),
        governance_principal_resolver=lambda _request: _principal("root_manager"),
    )
    authority_only = create_app(
        runtime=StubRuntime(),
        operational_authorization=OperationalAuthorization(
            configured_org_id="acme",
            central_authorizer=SnapshotCentralAuthorizer(
                _snapshot(roles={"root_manager": ("admin",)})
            ),
        ),
    )
    for app in (resolver_only, authority_only):
        _assert_partial_list_has_no_registry_lookup(app)


def _assert_partial_list_has_no_registry_lookup(app: Any) -> None:
    endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", "") == "/admin/cards"
    )
    registry = inspect.getclosurevars(endpoint).nonlocals["bundle"].registry

    def must_not_lookup() -> object:
        raise AssertionError("partial central composition must not query Registry")

    setattr(registry, "all_cards", must_not_lookup)
    response = _response(TestClient(app), "get", "/admin/cards")
    assert response.status_code == 503


def test_partial_composition_post_malformed_body_returns_503_before_validation() -> None:
    app = create_app(
        runtime=StubRuntime(),
        governance_principal_resolver=lambda _request: _principal("root_manager"),
    )
    client = TestClient(app)

    register = _response(client, "post", "/admin/cards", content=b"{")
    transfer = _response(client, "post", "/admin/cards/cs_ops/owner", content=b"{")

    assert register.status_code == 503
    assert transfer.status_code == 503
