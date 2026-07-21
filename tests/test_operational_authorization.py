from __future__ import annotations

from typing import cast

from agent_org_network.central_authority import (
    AUTHORITY_ACTION_MANIFEST,
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    AuthorizationDenied,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_authorization import (
    OPERATIONAL_ACTION_MANIFEST,
    OperationalAuthorization,
    OperationalAuthorizationOutcome,
)


def _snapshot() -> AuthorityPolicySnapshot:
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "2026-07-15",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "operator-1", "roles": ["operator"]},
            {"org_id": "acme", "subject_id": "admin-1", "roles": ["admin"]},
        ],
        "role_permissions": [
            {"role": "operator", "actions": ["monitor.read", "session.end"]},
            {"role": "admin", "actions": ["card.register", "author.publish"]},
        ],
        "route_rules": [],
        "worker_bindings": [],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    return AuthorityPolicySnapshot(
        schema_version=1,
        org_id="acme",
        policy_version="2026-07-15",
        content_sha256=document["content_sha256"],
        subject_roles=(
            SubjectRoleBinding(org_id="acme", subject_id="operator-1", roles=("operator",)),
            SubjectRoleBinding(org_id="acme", subject_id="admin-1", roles=("admin",)),
        ),
        role_permissions=(
            RolePermission(role="operator", actions=("monitor.read", "session.end")),
            RolePermission(role="admin", actions=("card.register", "author.publish")),
        ),
        route_rules=(),
        worker_bindings=(),
    )


def _principal(subject_id: str = "operator-1") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme",
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id="session-1",
    )


def _resource() -> ResourceRef:
    return ResourceRef(org_id="acme", kind="monitor", resource_id="monitor-1")


def test_actual_snapshot_authorizer_allows_exact_operational_action() -> None:
    boundary = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(_snapshot()),
    )

    assert boundary.authorize(_principal(), "monitor.read", _resource()) == "allowed"


def test_missing_central_authorizer_is_explicitly_unavailable_and_has_no_write_surface() -> None:
    boundary = OperationalAuthorization(configured_org_id="acme", central_authorizer=None)

    assert boundary.authorize(_principal(), "monitor.read", _resource()) == "unavailable"


def test_nonexact_or_wrong_org_inputs_fail_closed() -> None:
    boundary = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(_snapshot()),
    )
    other_org = ResourceRef(org_id="other", kind="monitor", resource_id="monitor-1")

    assert boundary.authorize(object(), "monitor.read", _resource()) == "denied"
    assert boundary.authorize(_principal(), "monitor.read", object()) == "denied"
    assert boundary.authorize(_principal(), "monitor.read", other_org) == "denied"
    assert boundary.authorize(_principal(), "unknown.action", _resource()) == "denied"


class _ForgedGrantAuthorizer:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> AuthorizationGrant:
        return AuthorizationGrant(
            org_id=principal.org_id,
            subject_id=principal.subject_id,
            action="monitor.read",
            resource=resource,
            roles=("operator",),
            policy_version="forged",
            policy_digest="0" * 64,
        )

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        return True


class _UnavailableAuthorizer:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> AuthorizationDenied:
        return AuthorizationDenied(kind="policy_unavailable")

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        return True


class _NoVerifyAuthorizer:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> AuthorizationGrant:
        return _ForgedGrantAuthorizer().authorize(principal, action, resource)


class _VerifyRaisesAuthorizer:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> AuthorizationGrant:
        return _ForgedGrantAuthorizer().authorize(principal, action, resource)

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        raise RuntimeError("internal policy detail")


class _AuthorizeRaisesAuthorizer:
    def authorize(
        self, principal: AuthenticatedPrincipal, action: object, resource: ResourceRef
    ) -> AuthorizationGrant:
        raise RuntimeError("internal policy detail")

    def verify(
        self,
        grant: AuthorizationGrant,
        principal: AuthenticatedPrincipal,
        action: object,
        resource: ResourceRef,
    ) -> bool:
        return True


def test_public_grant_tampering_and_missing_verify_fail_closed() -> None:
    forged = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=cast(CentralAuthorizer, _ForgedGrantAuthorizer()),
    )
    no_verify = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=cast(CentralAuthorizer, _NoVerifyAuthorizer())
    )

    assert forged.authorize(_principal(), "session.end", _resource()) == "denied"
    assert no_verify.authorize(_principal(), "monitor.read", _resource()) == "unavailable"


def test_authorizer_exceptions_are_unavailable_without_exposing_the_cause() -> None:
    verify_raises = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=cast(CentralAuthorizer, _VerifyRaisesAuthorizer()),
    )
    authorize_raises = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=cast(CentralAuthorizer, _AuthorizeRaisesAuthorizer()),
    )

    assert verify_raises.authorize(_principal(), "monitor.read", _resource()) == "unavailable"
    assert authorize_raises.authorize(_principal(), "monitor.read", _resource()) == "unavailable"


def test_policy_unavailable_is_distinct_from_permission_denial() -> None:
    unavailable = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=cast(CentralAuthorizer, _UnavailableAuthorizer()),
    )
    denied = OperationalAuthorization(
        configured_org_id="acme", central_authorizer=SnapshotCentralAuthorizer(_snapshot())
    )

    assert unavailable.authorize(_principal(), "monitor.read", _resource()) == "unavailable"
    assert denied.authorize(_principal(), "audit.read", _resource()) == "denied"


def test_all_s4_actions_are_registered_in_both_manifests() -> None:
    assert OPERATIONAL_ACTION_MANIFEST == {
        "supervision.read",
        "supervision.correct",
        "scorecard.read",
        "monitor.read",
        "audit.read",
        "org_graph.read",
        "session.end",
        "hitl.read",
        "hitl.write",
        "worker_credential.issue",
        "worker_credential.read",
        "worker_credential.revoke",
        "card.read",
        "card.register",
        "card.transfer_owner",
        "user.register",
        "author.read",
        "author.write",
        "author.publish",
    }
    assert OPERATIONAL_ACTION_MANIFEST <= AUTHORITY_ACTION_MANIFEST


def test_outcome_type_is_closed() -> None:
    outcome: OperationalAuthorizationOutcome = "allowed"
    assert outcome == "allowed"
