from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import cast

import pytest
import yaml
from pydantic import ValidationError

from agent_org_network.central_authority import (
    AUTHORITY_ACTION_MANIFEST,
    DYNAMIC_SUBJECT_REQUIREMENTS,
    AuthenticatedPrincipal,
    AuthorityPolicyLoadError,
    AuthorityPolicySnapshot,
    AuthorizationDenied,
    AuthorizationGrant,
    ResourceRef,
    SnapshotCentralAuthorizer,
    canonical_policy_digest,
    load_authority_policy_yaml,
    verify_authorization_grant,
)


def _document() -> dict[str, object]:
    return {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "policy-2026-07-15",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": "user-1", "roles": ["requester"]},
            {"org_id": "acme", "subject_id": "owner-1", "roles": ["owner", "auditor"]},
        ],
        "role_permissions": [
            {
                "role": "requester",
                "actions": ["question.create", "question.read"],
            },
            {
                "role": "owner",
                "actions": ["card.read", "author.write"],
            },
            {"role": "auditor", "actions": ["audit.read", "monitor.read"]},
        ],
        "route_rules": [{"org_id": "acme", "intent": "billing", "agent_card_id": "billing-card"}],
        "worker_bindings": [
            {
                "org_id": "acme",
                "credential_id": "credential-1",
                "owner_subject_id": "owner-1",
                "connection_role": "primary",
                "generation": 3,
            }
        ],
    }


def _yaml(
    mutate: Callable[[dict[str, object]], None] | None = None,
    *,
    refresh_digest: bool = True,
) -> str:
    document = _document()
    if mutate is not None:
        mutate(document)
    if refresh_digest:
        document["content_sha256"] = canonical_policy_digest(document)
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


def _snapshot() -> AuthorityPolicySnapshot:
    return load_authority_policy_yaml(_yaml(), expected_org_id="acme")


def test_loader는_strict_snapshot과_canonical_digest를_만든다() -> None:
    snapshot = _snapshot()

    assert snapshot.schema_version == 1
    assert snapshot.org_id == "acme"
    assert snapshot.content_sha256 == canonical_policy_digest(_document())
    assert {binding.subject_id: binding.roles for binding in snapshot.subject_roles}["user-1"] == (
        "requester",
    )
    assert {permission.role: permission.actions for permission in snapshot.role_permissions}[
        "requester"
    ] == ("question.create", "question.read")

    with pytest.raises(ValidationError):
        AuthorityPolicySnapshot.model_validate({**snapshot.model_dump(), "unexpected": "forbidden"})
    with pytest.raises(ValidationError):
        snapshot.policy_version = "changed"  # type: ignore[misc]


def test_같은_정책은_yaml_순서와_formatting이_달라도_digest가_같다() -> None:
    first = _document()
    second = deepcopy(first)
    second["subject_roles"] = list(reversed(cast(list[object], second["subject_roles"])))
    second["role_permissions"] = list(reversed(cast(list[object], second["role_permissions"])))
    second_subject_roles = second["subject_roles"]
    cast(dict[str, object], second_subject_roles[1])["roles"] = ["requester"]

    assert canonical_policy_digest(first) == canonical_policy_digest(second)

    first["content_sha256"] = canonical_policy_digest(first)
    second["content_sha256"] = canonical_policy_digest(second)
    first_snapshot = load_authority_policy_yaml(
        yaml.safe_dump(first, sort_keys=False), expected_org_id="acme"
    )
    second_snapshot = load_authority_policy_yaml(
        yaml.safe_dump(second, sort_keys=True), expected_org_id="acme"
    )
    assert first_snapshot.content_sha256 == second_snapshot.content_sha256


_INVALID_POLICY_CASES: list[tuple[Callable[[dict[str, object]], None], str]] = [
    (lambda document: document.__setitem__("unknown", True), "unknown_key"),
    (
        lambda document: cast(list[dict[str, object]], document["subject_roles"])[0].__setitem__(
            "roles", ["superuser"]
        ),
        "unknown_role",
    ),
    (
        lambda document: cast(list[dict[str, object]], document["role_permissions"])[0].__setitem__(
            "actions", ["question.destroy"]
        ),
        "unknown_action",
    ),
    (
        lambda document: cast(list[object], document["subject_roles"]).append(
            deepcopy(cast(list[object], document["subject_roles"])[0])
        ),
        "duplicate",
    ),
    (
        lambda document: cast(list[dict[str, object]], document["role_permissions"])[0].__setitem__(
            "actions", ["question.create", "question.create"]
        ),
        "duplicate",
    ),
    (
        lambda document: cast(list[dict[str, object]], document["worker_bindings"])[0].__setitem__(
            "credential_id", " "
        ),
        "blank_value",
    ),
    (
        lambda document: cast(list[dict[str, object]], document["route_rules"])[0].__setitem__(
            "org_id", "other-org"
        ),
        "cross_org",
    ),
    (lambda document: document.__setitem__("schema_version", 2), "schema_version"),
    (lambda document: document.__setitem__("policy_version", 7), "invalid_document"),
]


@pytest.mark.parametrize(("mutate", "kind"), _INVALID_POLICY_CASES)
def test_loader는_잘못된_정책을_typed_failure로_거부한다(
    mutate: Callable[[dict[str, object]], None], kind: str
) -> None:
    with pytest.raises(AuthorityPolicyLoadError) as raised:
        load_authority_policy_yaml(_yaml(mutate), expected_org_id="acme")

    assert raised.value.kind == kind


def test_loader는_org_version_declared_digest_불일치를_거부한다() -> None:
    with pytest.raises(AuthorityPolicyLoadError) as cross_org:
        load_authority_policy_yaml(_yaml(), expected_org_id="other-org")
    assert cross_org.value.kind == "cross_org"

    with pytest.raises(AuthorityPolicyLoadError) as digest:
        load_authority_policy_yaml(
            _yaml(
                lambda document: document.__setitem__("policy_version", "changed"),
                refresh_digest=False,
            ),
            expected_org_id="acme",
        )
    assert digest.value.kind == "digest_mismatch"

    with pytest.raises(AuthorityPolicyLoadError) as version:
        load_authority_policy_yaml(
            _yaml(),
            expected_org_id="acme",
            expected_policy_version="policy-next",
        )
    assert version.value.kind == "policy_version"


@pytest.mark.parametrize("text", ["", "[]", "{broken", "null"])
def test_loader는_누락과_yaml_parse_error를_고정된_typed_failure로_감춘다(text: str) -> None:
    with pytest.raises(AuthorityPolicyLoadError) as raised:
        load_authority_policy_yaml(text, expected_org_id="acme")

    assert raised.value.kind in {"missing_policy", "invalid_yaml", "invalid_document"}
    assert "broken" not in str(raised.value)


def test_IdP_role_group_claim은_principal과_중앙_role_mapping에_들어오지_않는다() -> None:
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    with pytest.raises(ValidationError):
        AuthenticatedPrincipal.model_validate(
            {
                **principal.model_dump(),
                "roles": ["admin"],
                "groups": ["admins"],
            }
        )

    grant = SnapshotCentralAuthorizer(_snapshot()).authorize(
        principal,
        "question.create",
        ResourceRef(org_id="acme", kind="question"),
    )
    assert isinstance(grant, AuthorizationGrant)
    assert grant.roles == ("requester",)


def test_authorizer는_exact_grant를_발급하고_verifier가_재사용을_막는다() -> None:
    snapshot = _snapshot()
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="owner-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    resource = ResourceRef(
        org_id="acme",
        kind="agent_card",
        resource_id="billing-card",
        owner_subject_id="owner-1",
    )

    result = SnapshotCentralAuthorizer(snapshot).authorize(principal, "card.read", resource)

    assert isinstance(result, AuthorizationGrant)
    assert SnapshotCentralAuthorizer(snapshot).verify(
        result,
        principal,
        "card.read",
        resource,
    )
    assert result.model_dump() == {
        "org_id": "acme",
        "subject_id": "owner-1",
        "action": "card.read",
        "resource": resource.model_dump(),
        "roles": ("owner",),
        "policy_version": snapshot.policy_version,
        "policy_digest": snapshot.content_sha256,
    }
    assert verify_authorization_grant(result, principal, "card.read", resource, snapshot)
    assert not verify_authorization_grant(
        result,
        principal,
        "author.write",
        resource,
        snapshot,
    )
    assert not verify_authorization_grant(
        result,
        principal,
        "card.read",
        ResourceRef(
            org_id="acme",
            kind="agent_card",
            resource_id="other-card",
            owner_subject_id="owner-1",
        ),
        snapshot,
    )


@pytest.mark.parametrize(
    ("subject_id", "action", "resource", "kind"),
    [
        (
            "missing",
            "question.create",
            ResourceRef(org_id="acme", kind="question"),
            "not_found_or_denied",
        ),
        (
            "user-1",
            "audit.read",
            ResourceRef(org_id="acme", kind="audit"),
            "not_found_or_denied",
        ),
        (
            "user-1",
            "question.create",
            ResourceRef(org_id="other-org", kind="question"),
            "not_found_or_denied",
        ),
        (
            "owner-1",
            "card.read",
            ResourceRef(
                org_id="acme",
                kind="agent_card",
                resource_id="billing-card",
                owner_subject_id="another-owner",
            ),
            "not_found_or_denied",
        ),
    ],
)
def test_authorizer는_missing_permission_cross_org_owner_mismatch를_deny_safe한다(
    subject_id: str,
    action: str,
    resource: ResourceRef,
    kind: str,
) -> None:
    result = SnapshotCentralAuthorizer(_snapshot()).authorize(
        AuthenticatedPrincipal(
            org_id="acme",
            subject_id=subject_id,
            identity_provider="company-oidc",
            identity_session_id="session-1",
        ),
        cast(object, action),
        resource,
    )

    assert isinstance(result, AuthorizationDenied)
    assert result.kind == kind


def test_authorizer는_unknown_action과_policy_dependency_error를_deny한다() -> None:
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    resource = ResourceRef(org_id="acme", kind="question")

    unknown = SnapshotCentralAuthorizer(_snapshot()).authorize(
        principal, cast(object, "question.destroy"), resource
    )

    def broken_provider() -> AuthorityPolicySnapshot:
        raise RuntimeError("secret-policy-token")

    unavailable = SnapshotCentralAuthorizer(broken_provider).authorize(
        principal, "question.create", resource
    )

    assert unknown == AuthorizationDenied(kind="not_found_or_denied")
    assert unavailable == AuthorizationDenied(kind="policy_unavailable")
    assert "secret" not in repr(unavailable)


def test_approval_expire는_명시_snapshot_grant가_있을_때만_허용한다() -> None:
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="expiry-service",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    resource = ResourceRef(org_id="acme", kind="approval_item", resource_id="item-1")
    assert SnapshotCentralAuthorizer(_snapshot()).authorize(
        principal, "approval.expire", resource
    ) == AuthorizationDenied(kind="not_found_or_denied")

    def add_expiry_service(document: dict[str, object]) -> None:
        cast(list[dict[str, object]], document["subject_roles"]).append(
            {"org_id": "acme", "subject_id": "expiry-service", "roles": ["operator"]}
        )
        cast(list[dict[str, object]], document["role_permissions"]).append(
            {"role": "operator", "actions": ["approval.expire"]}
        )

    granted = SnapshotCentralAuthorizer(
        load_authority_policy_yaml(_yaml(add_expiry_service), expected_org_id="acme")
    ).authorize(principal, "approval.expire", resource)
    assert isinstance(granted, AuthorizationGrant)
    assert granted.action == "approval.expire"


class _ExplosiveIdentity:
    @property
    def org_id(self) -> str:
        raise RuntimeError("secret-identity-token")


def test_authorize와_verify는_noncanonical_identity_resource를_고정_deny한다() -> None:
    snapshot = _snapshot()
    authorizer = SnapshotCentralAuthorizer(snapshot)
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    resource = ResourceRef(org_id="acme", kind="question")
    grant = authorizer.authorize(principal, "question.create", resource)
    assert isinstance(grant, AuthorizationGrant)

    explosive_principal = cast(AuthenticatedPrincipal, _ExplosiveIdentity())
    explosive_resource = cast(ResourceRef, _ExplosiveIdentity())
    malformed_principal = AuthenticatedPrincipal.model_construct(
        org_id=" ",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )

    assert authorizer.authorize(explosive_principal, "question.create", resource) == (
        AuthorizationDenied(kind="not_found_or_denied")
    )
    assert authorizer.authorize(principal, "question.create", explosive_resource) == (
        AuthorizationDenied(kind="not_found_or_denied")
    )
    assert authorizer.authorize(malformed_principal, "question.create", resource) == (
        AuthorizationDenied(kind="not_found_or_denied")
    )
    assert not verify_authorization_grant(
        grant,
        explosive_principal,
        "question.create",
        resource,
        snapshot,
    )
    assert not verify_authorization_grant(
        grant,
        principal,
        "question.create",
        explosive_resource,
        snapshot,
    )


def test_dynamic_owner_requirement는_action과_권한_role별로_fail_closed한다() -> None:
    assert DYNAMIC_SUBJECT_REQUIREMENTS["card.read"] == frozenset({"owner"})
    assert DYNAMIC_SUBJECT_REQUIREMENTS["author.write"] == frozenset({"owner"})
    assert DYNAMIC_SUBJECT_REQUIREMENTS["question.read"] == frozenset({"requester"})
    assert "question.create" not in DYNAMIC_SUBJECT_REQUIREMENTS

    snapshot = _snapshot()
    owner = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="owner-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    requester = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="session-2",
    )
    authorizer = SnapshotCentralAuthorizer(snapshot)

    assert authorizer.authorize(
        owner,
        "card.read",
        ResourceRef(org_id="acme", kind="agent_card", resource_id="billing-card"),
    ) == AuthorizationDenied(kind="not_found_or_denied")
    assert authorizer.authorize(
        owner,
        "author.write",
        ResourceRef(org_id="acme", kind="draft", resource_id="draft-1"),
    ) == AuthorizationDenied(kind="not_found_or_denied")
    assert isinstance(
        authorizer.authorize(
            owner,
            "author.write",
            ResourceRef(
                org_id="acme",
                kind="draft",
                resource_id="draft-1",
                owner_subject_id="owner-1",
            ),
        ),
        AuthorizationGrant,
    )
    assert isinstance(
        authorizer.authorize(
            requester,
            "question.create",
            ResourceRef(org_id="acme", kind="question"),
        ),
        AuthorizationGrant,
    )


def test_dynamic_owner_requirement는_granting_role별로만_적용한다() -> None:
    def add_admin(document: dict[str, object]) -> None:
        subject_roles = cast(list[dict[str, object]], document["subject_roles"])
        subject_roles.extend(
            [
                {"org_id": "acme", "subject_id": "admin-1", "roles": ["admin"]},
                {
                    "org_id": "acme",
                    "subject_id": "mixed-1",
                    "roles": ["owner", "admin"],
                },
            ]
        )
        role_permissions = cast(list[dict[str, object]], document["role_permissions"])
        role_permissions.append({"role": "admin", "actions": ["card.read"]})

    snapshot = load_authority_policy_yaml(
        _yaml(add_admin),
        expected_org_id="acme",
    )
    authorizer = SnapshotCentralAuthorizer(snapshot)
    other_owned_card = ResourceRef(
        org_id="acme",
        kind="agent_card",
        resource_id="billing-card",
        owner_subject_id="different-owner",
    )

    admin = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="admin-1",
        identity_provider="company-oidc",
        identity_session_id="session-admin",
    )
    admin_result = authorizer.authorize(admin, "card.read", other_owned_card)
    assert isinstance(admin_result, AuthorizationGrant)
    assert admin_result.roles == ("admin",)

    mixed = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="mixed-1",
        identity_provider="company-oidc",
        identity_session_id="session-mixed",
    )
    mixed_result = authorizer.authorize(mixed, "card.read", other_owned_card)
    assert isinstance(mixed_result, AuthorizationGrant)
    assert mixed_result.roles == ("admin",)
    assert verify_authorization_grant(
        mixed_result,
        mixed,
        "card.read",
        other_owned_card,
        snapshot,
    )

    auditor = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="owner-1",
        identity_provider="company-oidc",
        identity_session_id="session-auditor",
    )
    audit_result = authorizer.authorize(
        auditor,
        "audit.read",
        ResourceRef(
            org_id="acme",
            kind="audit",
            resource_id="entry-1",
            owner_subject_id="different-owner",
        ),
    )
    assert isinstance(audit_result, AuthorizationGrant)
    assert audit_result.roles == ("auditor",)

    owner_only = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="owner-1",
        identity_provider="company-oidc",
        identity_session_id="session-owner",
    )
    assert authorizer.authorize(owner_only, "card.read", other_owned_card) == (
        AuthorizationDenied(kind="not_found_or_denied")
    )


def test_verifier는_snapshot으로_권한을_재평가해_직접_만든_grant를_거부한다() -> None:
    snapshot = _snapshot()
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="owner-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    resource = ResourceRef(
        org_id="acme",
        kind="agent_card",
        resource_id="billing-card",
        owner_subject_id="owner-1",
    )
    forged = AuthorizationGrant(
        org_id="acme",
        subject_id="owner-1",
        action="card.read",
        resource=resource,
        roles=("owner",),
        policy_version=snapshot.policy_version,
        policy_digest=snapshot.content_sha256,
    )

    assert not verify_authorization_grant(
        forged,
        principal,
        "card.read",
        resource,
        snapshot,
    )


def test_policy_provider는_construction에서_한번만_resolve하고_snapshot을_고정한다() -> None:
    snapshot = _snapshot()
    calls = 0

    def provider() -> AuthorityPolicySnapshot:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("secret-late-policy-token")
        return snapshot

    authorizer = SnapshotCentralAuthorizer(provider)
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    resource = ResourceRef(org_id="acme", kind="question")

    assert isinstance(
        authorizer.authorize(principal, "question.create", resource), AuthorizationGrant
    )
    assert isinstance(
        authorizer.authorize(principal, "question.create", resource), AuthorizationGrant
    )
    assert calls == 1


def test_policy_provider의_초기_예외는_고정_unavailable이고_재호출하지않는다() -> None:
    calls = 0

    def provider() -> AuthorityPolicySnapshot:
        nonlocal calls
        calls += 1
        raise RuntimeError("secret-initial-policy-token")

    authorizer = SnapshotCentralAuthorizer(provider)
    principal = AuthenticatedPrincipal(
        org_id="acme",
        subject_id="user-1",
        identity_provider="company-oidc",
        identity_session_id="session-1",
    )
    resource = ResourceRef(org_id="acme", kind="question")

    assert authorizer.authorize(principal, "question.create", resource) == AuthorizationDenied(
        kind="policy_unavailable"
    )
    assert authorizer.authorize(principal, "question.create", resource) == AuthorizationDenied(
        kind="policy_unavailable"
    )
    assert calls == 1


_MALFORMED_DIGEST_DOCUMENTS: list[object] = [
    [],
    {**_document(), "subject_roles": {}},
    {**_document(), "subject_roles": [[]]},
    {**_document(), "subject_roles": [{1: "not-a-string-key"}]},
    {
        **_document(),
        "subject_roles": [{"org_id": "acme", "subject_id": "user-1", "roles": "requester"}],
    },
    {
        **_document(),
        "role_permissions": [{"role": "requester", "actions": [1]}],
    },
]


@pytest.mark.parametrize("document", _MALFORMED_DIGEST_DOCUMENTS)
def test_canonical_digest는_malformed_shape를_hash하지않고_typed_reject한다(
    document: object,
) -> None:
    with pytest.raises(AuthorityPolicyLoadError):
        canonical_policy_digest(document)


def test_action_manifest는_ADR의_전체_action과_exact하다() -> None:
    assert AUTHORITY_ACTION_MANIFEST == {
        "question.create",
        "question.read",
        "question.stream",
        "approval.list",
        "approval.read",
        "approval.decide",
        "approval.reassign",
        "approval.expire",
        "conflict.open",
        "conflict.list",
        "conflict.concur",
        "conflict.document.read",
        "manager.list",
        "manager.act",
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
        "worker.connect",
        "worker.submit",
        "worker.publish_index",
        "worker.sync_knowledge",
    }
