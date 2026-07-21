from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from agent_org_network.authoring_application import AuthoringApplication, AuthoringMutation
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
from agent_org_network.operational_application import (
    OperationalDeniedError,
    OperationalMutationApproval,
)
from agent_org_network.operational_authorization import OperationalAuthorization


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="cs_lead", identity_provider="oidc", identity_session_id="s"
    )


def _authorizer() -> SnapshotCentralAuthorizer:
    binding = SubjectRoleBinding(org_id="acme", subject_id="cs_lead", roles=("owner",))
    permission = RolePermission(
        role="owner", actions=("author.read", "author.write", "author.publish")
    )
    doc: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
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
            org_id="acme",
            policy_version="test",
            content_sha256=canonical_policy_digest(doc),
            subject_roles=(binding,),
            role_permissions=(permission,),
            route_rules=(),
            worker_bindings=(),
        )
    )


def _app(
    *, authorizer: object | None = None, approval: str = "allowed"
) -> tuple[AuthoringApplication, Any]:
    bundle = build_demo()
    boundary = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=cast(CentralAuthorizer, authorizer or _authorizer()),
    )
    app = AuthoringApplication(
        authorization=boundary,
        registry=bundle.registry,
        mutation_approval=lambda _p, _a, _r, digest, fingerprint: OperationalMutationApproval(
            outcome=approval,
            evidence_id="human-1",
            command_digest=digest,
            resource_fingerprint=fingerprint,
        ),
        audit_log=bundle.audit,
        clock=lambda: datetime(2026, 7, 15, tzinfo=UTC),
    )
    return app, bundle.audit


def test_query_rechecks_current_grant_before_sensitive_dto() -> None:
    class RevokeSecond:
        def __init__(self) -> None:
            self.delegate = _authorizer()
            self.calls = 0

        def authorize(self, p: AuthenticatedPrincipal, a: object, r: ResourceRef) -> object:
            self.calls += 1
            return (
                AuthorizationDenied(kind="not_found_or_denied")
                if self.calls == 2
                else self.delegate.authorize(p, a, r)
            )

        def verify(self, *args: object) -> bool:
            return self.delegate.verify(*cast(tuple[object, object, object, object], args))  # type: ignore[arg-type]

    revoked = RevokeSecond()
    app, _audit = _app(authorizer=revoked)
    with pytest.raises(OperationalDeniedError):
        app.query(_principal(), "cs_ops", lambda _card: {"body": "sensitive"})
    assert revoked.calls == 2


def test_mutation_requires_human_evidence_and_audits_only_success() -> None:
    app, audit = _app()
    result = app.mutate(
        _principal(),
        "cs_ops",
        lambda card: (card.agent_id, AuthoringMutation(card.agent_id, {"kind": "publish"})),
        channel="mcp",
        command={"operation": "publish", "version": 1},
    )
    assert result == "cs_ops"
    assert audit.records()[-1]["action"]["approval_evidence_id"] == "human-1"

    denied, denied_audit = _app(approval="denied")
    before = list(denied_audit.records())
    with pytest.raises(OperationalDeniedError):
        denied.mutate(
            _principal(),
            "cs_ops",
            lambda card: (card.agent_id, AuthoringMutation(card.agent_id, {})),
            channel="http",
            command={"operation": "publish", "version": 1},
        )
    assert denied_audit.records() == before
