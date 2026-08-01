"""Narrow verified-OIDC to current Registry User principal binding."""

from __future__ import annotations

import hashlib

from agent_org_network.central_authority import AuthenticatedPrincipal
from agent_org_network.oidc import OidcClaims
from agent_org_network.sqlite_production_registry_users import (
    ProductionRegistryUser,
    SqliteProductionRegistryUsers,
)


class QuestionGatewayUnavailable(RuntimeError):
    """The Central identity boundary cannot safely resolve a principal."""


class OidcPrincipalUnauthenticated(QuestionGatewayUnavailable):
    """Verified-claims envelope is not a current valid OIDC identity."""


class RegistryPrincipalForbidden(QuestionGatewayUnavailable):
    """A valid identity is not a current Registry User for this organization."""


class RegistryPrincipalUnavailable(QuestionGatewayUnavailable):
    """The Registry dependency returned no trustworthy principal result."""


class CurrentRegistryOidcPrincipalResolver:
    """Resolve verified claims to the current configured-org Registry User."""

    def __init__(
        self,
        registry: SqliteProductionRegistryUsers,
        *,
        org_id: str,
        provider_id: str,
        issuer: str,
        audience: str,
    ) -> None:
        if (
            type(registry) is not SqliteProductionRegistryUsers
            or not org_id
            or not provider_id
            or not issuer
            or not audience
        ):
            raise QuestionGatewayUnavailable()
        self._registry = registry
        self._org = org_id
        self._provider = provider_id
        self._issuer = issuer
        self._audience = audience

    def resolve(self, claims: OidcClaims) -> AuthenticatedPrincipal:
        if (
            type(claims) is not OidcClaims
            or not claims.email_verified
            or claims.iss != self._issuer
            or claims.aud != self._audience
        ):
            raise OidcPrincipalUnauthenticated()
        try:
            user = self._registry.user_by_global_email(claims.email)
        except Exception as error:
            raise RegistryPrincipalUnavailable() from error
        if user is None:
            raise RegistryPrincipalForbidden()
        if type(user) is not ProductionRegistryUser:
            raise RegistryPrincipalUnavailable()
        if user.org_id != self._org:
            raise RegistryPrincipalForbidden()
        try:
            session = hashlib.sha256(
                f"{claims.iss}\x00{claims.sub}\x00{claims.aud}".encode()
            ).hexdigest()
            return AuthenticatedPrincipal(
                org_id=user.org_id,
                subject_id=user.user_id,
                identity_provider=self._provider,
                identity_session_id=session,
            )
        except Exception as error:
            raise RegistryPrincipalUnavailable() from error


__all__ = [
    "CurrentRegistryOidcPrincipalResolver",
    "OidcPrincipalUnauthenticated",
    "QuestionGatewayUnavailable",
    "RegistryPrincipalForbidden",
    "RegistryPrincipalUnavailable",
]
