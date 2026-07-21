"""`user.register` 중앙 Authority 인가 — 정적 role-gated(ADR 0064 결정 ⑥).

`test_operational_card_authorization.py`의 snapshot 헬퍼를 미러한다. `user.register`는
카드 `card.register`와 같은 결의 정적 role-gated action이다 — 특정 주체 귀속(dynamic
subject requirement)이 아니라 역할(admin/operator)로만 판단하고, 신규 User엔 owner
subject가 없으므로 `ResourceRef(kind="user", owner_subject_id=None)`으로도 통과해야 한다.
"""

from __future__ import annotations

from typing import Any, cast

from agent_org_network.central_authority import (
    ACTION_ALLOWED_ROLES,
    ACTION_RESOURCE_KIND_REQUIREMENTS,
    AUTHORITY_ACTION_MANIFEST,
    DYNAMIC_SUBJECT_REQUIREMENTS,
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    AuthorizationGrant,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.operational_authorization import (
    OPERATIONAL_ACTION_MANIFEST,
    OperationalAuthorization,
)


def _principal(subject_id: str, *, org_id: str = "acme") -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id=org_id,
        subject_id=subject_id,
        identity_provider="oidc",
        identity_session_id="session-1",
    )


def _snapshot(*, roles: dict[str, tuple[str, ...]]) -> AuthorityPolicySnapshot:
    permissions = (
        RolePermission(role="owner", actions=("card.read",)),
        RolePermission(
            role="admin",
            actions=("card.register", "user.register"),
        ),
        RolePermission(role="operator", actions=("user.register",)),
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


def _user_resource(user_id: str, *, org_id: str = "acme") -> ResourceRef:
    # ADR 0064 결정 ⑥ ResourceRef 관례: kind="user"·resource_id=신규 id·owner_subject_id=None.
    return ResourceRef(org_id=org_id, kind="user", resource_id=user_id, owner_subject_id=None)


# ── 정적 role-gated 계약 (ADR 0064 결정 ⑥ · domain-architect 지시) ──────────


def test_user_register는_두_manifest에_모두_있다() -> None:
    assert "user.register" in AUTHORITY_ACTION_MANIFEST
    assert "user.register" in OPERATIONAL_ACTION_MANIFEST
    assert OPERATIONAL_ACTION_MANIFEST <= AUTHORITY_ACTION_MANIFEST


def test_user_register는_정적_role_gated_동적_분기에_없다() -> None:
    """카드 `card.register`와 같은 결 — dynamic subject·resource-kind·allowed-roles 분기에
    넣지 않는다(등록은 특정 주체 귀속이 아니라 역할로 판단·신규 User엔 owner subject 없음)."""
    assert "user.register" not in DYNAMIC_SUBJECT_REQUIREMENTS
    assert "user.register" not in ACTION_RESOURCE_KIND_REQUIREMENTS
    assert "user.register" not in ACTION_ALLOWED_ROLES


# ── SnapshotCentralAuthorizer 직접 ───────────────────────────────────────────


def test_admin_role는_user_register_grant를_받는다() -> None:
    authorizer = SnapshotCentralAuthorizer(_snapshot(roles={"root_manager": ("admin",)}))
    result = authorizer.authorize(
        _principal("root_manager"), "user.register", _user_resource("alice")
    )
    assert type(result) is AuthorizationGrant
    assert result.action == "user.register"
    assert "admin" in result.roles


def test_operator_role도_user_register_grant를_받는다() -> None:
    authorizer = SnapshotCentralAuthorizer(_snapshot(roles={"ops": ("operator",)}))
    result = authorizer.authorize(_principal("ops"), "user.register", _user_resource("alice"))
    assert type(result) is AuthorizationGrant


def test_user_register가_없는_role은_거부된다() -> None:
    # owner role은 user.register 권한이 없다 → deny.
    authorizer = SnapshotCentralAuthorizer(_snapshot(roles={"cs_lead": ("owner",)}))
    result = authorizer.authorize(
        _principal("cs_lead"), "user.register", _user_resource("alice")
    )
    assert type(result) is not AuthorizationGrant


def test_user_register는_owner_subject_없이도_통과한다() -> None:
    """정적 role-gated — dynamic subject requirement가 아니므로 owner_subject_id=None OK."""
    authorizer = SnapshotCentralAuthorizer(_snapshot(roles={"root_manager": ("admin",)}))
    result = authorizer.authorize(
        _principal("root_manager"),
        "user.register",
        ResourceRef(org_id="acme", kind="user", resource_id="alice", owner_subject_id=None),
    )
    assert type(result) is AuthorizationGrant


# ── OperationalAuthorization 경계 ────────────────────────────────────────────


def test_operational_user_register_allowed() -> None:
    op = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(_snapshot(roles={"root_manager": ("admin",)})),
    )
    outcome = op.authorize(_principal("root_manager"), "user.register", _user_resource("alice"))
    assert outcome == "allowed"


def test_operational_user_register_denied_for_wrong_role() -> None:
    op = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(_snapshot(roles={"cs_lead": ("owner",)})),
    )
    outcome = op.authorize(_principal("cs_lead"), "user.register", _user_resource("alice"))
    assert outcome == "denied"


def test_operational_user_register_denied_cross_org() -> None:
    op = OperationalAuthorization(
        configured_org_id="acme",
        central_authorizer=SnapshotCentralAuthorizer(_snapshot(roles={"root_manager": ("admin",)})),
    )
    # 타 org principal·resource — configured org와 불일치 → deny.
    outcome = op.authorize(
        _principal("root_manager", org_id="other"),
        "user.register",
        _user_resource("alice", org_id="other"),
    )
    assert outcome == "denied"
