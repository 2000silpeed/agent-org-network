"""Governance application 경계의 중앙 권한 grant 검증 보조 계약."""

from __future__ import annotations

from typing import Literal, TypeAlias

from agent_org_network.central_authority import (
    Action,
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    CentralAuthorizer,
    ResourceRef,
)

CentralAuthorizationOutcome: TypeAlias = Literal["allowed", "denied", "unavailable"]


def canonical_authenticated_principal(value: object) -> AuthenticatedPrincipal | None:
    """duck type과 손상된 principal을 허용하지 않는 exact canonicalization."""
    if type(value) is not AuthenticatedPrincipal:
        return None
    try:
        return AuthenticatedPrincipal.model_validate(value, strict=True)
    except Exception:
        return None


def authorize_and_verify(
    authorizer: CentralAuthorizer,
    principal: AuthenticatedPrincipal,
    action: Action,
    resource: ResourceRef,
) -> CentralAuthorizationOutcome:
    """공개 grant 필드와 authorizer의 private seal 검증을 모두 요구한다."""
    try:
        raw = authorizer.authorize(principal, action, resource)
    except Exception:
        return "unavailable"
    if type(raw) is AuthorizationDenied:
        try:
            denied = AuthorizationDenied.model_validate(raw, strict=True)
        except Exception:
            return "denied"
        return "unavailable" if denied.kind == "policy_unavailable" else "denied"
    if type(raw) is not AuthorizationGrant:
        return "denied"
    try:
        grant = AuthorizationGrant.model_validate(raw, strict=True)
        if not (
            grant.org_id == principal.org_id == resource.org_id
            and grant.subject_id == principal.subject_id
            and grant.action == action
            and grant.resource == resource
            and bool(grant.roles)
        ):
            return "denied"
    except Exception:
        return "denied"
    try:
        verifier = authorizer.verify
        if not callable(verifier):
            return "unavailable"
        verified = verifier(raw, principal, action, resource)
    except Exception:
        return "unavailable"
    if type(verified) is not bool:
        return "unavailable"
    return "allowed" if verified else "denied"
