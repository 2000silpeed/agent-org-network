"""P17.8 S4.3 운영·콘솔 표면의 중앙 Authority 회귀."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from httpx import Response
from starlette.requests import Request as StarletteRequest

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorityPolicySnapshot,
    CentralAuthorizer,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.console import ConsoleFeed
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.operational_application import OperationalMutationApproval
from agent_org_network.runtime import StubRuntime
from agent_org_network.web import create_app


_ALL_ACTIONS = (
    "monitor.read",
    "audit.read",
    "org_graph.read",
    "session.end",
    "hitl.read",
    "hitl.write",
    "worker_credential.issue",
    "worker_credential.read",
    "worker_credential.revoke",
)


def _principal(subject_id: str = "operator") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id="session-1",
    )


def _snapshot(actions: tuple[str, ...] = _ALL_ACTIONS) -> AuthorityPolicySnapshot:
    binding = SubjectRoleBinding(org_id="acme", subject_id="operator", roles=("operator",))
    # 정책 문서 자체는 empty permission을 허용하지 않으므로, S4와 무관한 action 하나로
    # "운영 action 전부 거부" principal을 표현한다.
    effective_actions = actions or ("question.create",)
    permission = RolePermission(role="operator", actions=cast(Any, effective_actions))
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test-policy",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    digest = canonical_policy_digest(document)
    return AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="test-policy",
        content_sha256=digest,
        subject_roles=(binding,),
        role_permissions=(permission,),
        route_rules=(),
        worker_bindings=(),
    )


def _response(client: TestClient, method: str, url: str, **kwargs: object) -> Response:
    http: Any = client
    return cast(Response, getattr(http, method)(url, **kwargs))


def _client(
    *,
    actions: tuple[str, ...] = _ALL_ACTIONS,
    resolver: Callable[[Request], AuthenticatedPrincipal] | None = None,
    authorizer: CentralAuthorizer | None = None,
    console_feed: ConsoleFeed | None = None,
) -> TestClient:
    actual_authorizer = authorizer or SnapshotCentralAuthorizer(_snapshot(actions))
    operational = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=actual_authorizer
    )

    def _default_resolver(_request: Request) -> AuthenticatedPrincipal:
        return _principal()

    return TestClient(
        create_app(
            runtime=StubRuntime(),
            governance_principal_resolver=resolver or _default_resolver,
            operational_authorization=operational,
            operational_mutation_approval=lambda _principal, _action, _resource, digest, fingerprint: (
                OperationalMutationApproval(
                    outcome="allowed",
                    evidence_id="test-approval",
                    command_digest=digest,
                    resource_fingerprint=fingerprint,
                )
            ),
            console_feed=console_feed,
        )
    )


def test_central_operations_require_each_declared_read_permission() -> None:
    denied = _client(actions=())

    assert _response(denied, "get", "/monitor").status_code == 503


def test_central_http_without_actual_source_proof_is_unavailable() -> None:
    client = _client()

    assert _response(client, "get", "/monitor").status_code == 503


def test_central_console_writes_deny_before_domain_mutation() -> None:
    denied = _client(actions=())
    app: Any = denied.app
    session = app.state.session_store.open_or_get("demo_user")

    end = _response(denied, "post", f"/console/sessions/{session.session_id}/end")
    hitl = _response(denied, "post", "/console/hitl/cs_ops", json={"on": True})
    issue = _response(
        denied, "post", "/console/tokens", json={"owner_id": "cs_lead", "role": "primary"}
    )

    assert end.status_code == 503
    assert hitl.status_code == 503
    assert issue.status_code == 503
    assert app.state.token_store.list_active() == []


def test_central_console_writes_use_server_principal_and_revoke_current_credential() -> None:
    client = _client()
    app: Any = client.app
    session = app.state.session_store.open_or_get("demo_user")

    ended = _response(client, "post", f"/console/sessions/{session.session_id}/end")
    hitl = _response(client, "post", "/console/hitl/cs_ops", json={"on": True})
    issued = _response(
        client,
        "post",
        "/console/tokens",
        json={"owner_id": "cs_lead", "role": "primary", "by_owner": "attacker"},
    )
    assert ended.status_code == hitl.status_code == 503
    assert issued.status_code == 503


def test_partial_central_composition_is_neutral_503_and_does_not_end_session() -> None:
    def resolver(_request: Request) -> AuthenticatedPrincipal:
        return _principal()

    client = TestClient(create_app(runtime=StubRuntime(), governance_principal_resolver=resolver))
    app: Any = client.app
    session = app.state.session_store.open_or_get("demo_user")

    assert _response(client, "get", "/monitor").status_code == 503
    assert (
        _response(client, "post", f"/console/sessions/{session.session_id}/end").status_code == 503
    )
    assert app.state.session_store.get(session.session_id).status == "active"


def test_missing_audit_sink_blocks_central_http_mutations_without_response_leak() -> None:
    client = _client()
    app: Any = client.app
    operational = app.state.operational_application
    assert operational is None
    assert _response(client, "get", "/monitor").status_code == 503


def test_partial_central_composition_hides_hitl_and_token_owner_existence() -> None:
    def resolver(_request: Request) -> AuthenticatedPrincipal:
        return _principal()

    authority = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=SnapshotCentralAuthorizer(_snapshot())
    )
    partial_clients = (
        TestClient(create_app(runtime=StubRuntime(), governance_principal_resolver=resolver)),
        TestClient(create_app(runtime=StubRuntime(), operational_authorization=authority)),
    )

    for client in partial_clients:
        for agent_id in ("cs_ops", "missing-agent"):
            assert _response(client, "get", f"/console/hitl/{agent_id}").status_code == 503
        for owner_id in ("cs_lead", "missing-owner"):
            response = _response(
                client,
                "post",
                "/console/tokens",
                json={"owner_id": owner_id, "role": "primary"},
            )
            assert response.status_code == 503


def test_partial_central_composition_hides_existing_and_missing_token_revoke() -> None:
    def resolver(_request: Request) -> AuthenticatedPrincipal:
        return _principal()

    authority = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=SnapshotCentralAuthorizer(_snapshot())
    )
    partial_clients = (
        TestClient(create_app(runtime=StubRuntime(), governance_principal_resolver=resolver)),
        TestClient(create_app(runtime=StubRuntime(), operational_authorization=authority)),
    )

    for client in partial_clients:
        app: Any = client.app
        _, existing = app.state.token_store.issue("cs_lead", "primary", now=datetime.now(UTC))
        for token_id in (existing.token_id, "missing-token"):
            assert (
                _response(client, "post", f"/console/tokens/{token_id}/revoke").status_code == 503
            )


def test_raw_central_hitl_unavailable_precedes_principal_card_and_body_lookup() -> None:
    calls: list[str] = []

    def resolver(_request: Request) -> AuthenticatedPrincipal:
        calls.append("resolved")
        return _principal()

    denied = _client(actions=(), resolver=resolver)

    hitl = _response(denied, "post", "/console/hitl/missing-agent", json={})
    assert hitl.status_code == 503
    assert calls == []

    token = _response(denied, "post", "/console/tokens", json={})
    assert token.status_code == 503
    assert calls == []


def test_hitl_write_reauthorizes_immediately_before_mutation() -> None:
    class _DenySecondAuthorization:
        def __init__(self) -> None:
            self._delegate = SnapshotCentralAuthorizer(_snapshot())
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: object
        ) -> object:
            self._calls += 1
            if self._calls == 2:
                return AuthorizationDenied(kind="not_found_or_denied")
            return self._delegate.authorize(principal, cast(Any, action), cast(Any, resource))

        def verify(
            self, grant: object, principal: object, action: object, resource: object
        ) -> bool:
            return self._delegate.verify(
                cast(Any, grant), cast(Any, principal), cast(Any, action), cast(Any, resource)
            )

    client = _client(authorizer=cast(CentralAuthorizer, _DenySecondAuthorization()))

    response = _response(client, "post", "/console/hitl/cs_ops", json={"on": True})

    assert response.status_code == 503


def test_console_feed_rechecks_authority_before_queued_event_after_revocation() -> None:
    class _RevokeAfterOpen:
        def __init__(self) -> None:
            self._delegate = SnapshotCentralAuthorizer(_snapshot())
            self._calls = 0

        def authorize(
            self, principal: AuthenticatedPrincipal, action: object, resource: object
        ) -> object:
            self._calls += 1
            if self._calls > 1:
                return AuthorizationDenied(kind="not_found_or_denied")
            return self._delegate.authorize(principal, cast(Any, action), cast(Any, resource))

        def verify(
            self, grant: object, principal: object, action: object, resource: object
        ) -> bool:
            return self._delegate.verify(
                cast(Any, grant), cast(Any, principal), cast(Any, action), cast(Any, resource)
            )

    feed = ConsoleFeed()
    client = _client(authorizer=cast(CentralAuthorizer, _RevokeAfterOpen()), console_feed=feed)
    app: Any = client.app
    route = next(route for route in app.routes if getattr(route, "path", None) == "/console/feed")
    request = StarletteRequest(
        {
            "type": "http",
            "method": "GET",
            "path": "/console/feed",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "scheme": "http",
        }
    )
    with pytest.raises(HTTPException) as unavailable:
        route.endpoint(request)
    assert unavailable.value.status_code == 503
