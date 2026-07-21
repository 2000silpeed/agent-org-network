"""P17.8 production authority composition capability.

This is deliberately a *composition* contract, not an HTTP/OIDC implementation.
It makes a production-shaped bootstrap prove that its central policy, authorizer,
identity resolver, operational boundary, worker boundary, and the exact Question
Surface it returns are one coherent object graph.  The capability is single-use so
copying its public metadata cannot promote a second composition.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import final

from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    SnapshotCentralAuthorizer,
)
from agent_org_network.operational_authorization import OperationalAuthorization
from agent_org_network.question_surface_composition import (
    QuestionSurfaceComposition,
    _question_surface_matches_production_authority,  # pyright: ignore[reportPrivateUsage]
    _register_question_surface_production_authority_claim,  # pyright: ignore[reportPrivateUsage]
)
from agent_org_network.worker_authorization import (
    StrictSnapshotWorkerBindingSource,
    WorkerAuthorization,
)


def _exact_principal(value: object, *, org_id: str) -> AuthenticatedPrincipal | None:
    if type(value) is not AuthenticatedPrincipal:
        return None
    try:
        principal = AuthenticatedPrincipal.model_validate(value, strict=True)
    except Exception:
        return None
    return principal if principal.org_id == org_id else None


@final
class ProductionIdentityResolver:
    """One production composition's server-side principal resolver.

    Token/JWKS verification is intentionally outside this S6 contract.  The caller
    supplies a verifier-backed resolver, while this object prevents a different
    organization or a duck-typed principal from being wired into central mode.
    """

    def __init__(
        self,
        *,
        configured_org_id: str,
        snapshot: AuthorityPolicySnapshot,
        authorizer: SnapshotCentralAuthorizer,
        resolver: Callable[[object], AuthenticatedPrincipal],
    ) -> None:
        self._configured_org_id = configured_org_id
        self._snapshot = snapshot
        self._authorizer = authorizer
        self._resolver = resolver

    def resolve(self, request_or_context: object) -> AuthenticatedPrincipal | None:
        if not self.is_coherent() or not callable(self._resolver):
            return None
        try:
            return _exact_principal(
                self._resolver(request_or_context), org_id=self._configured_org_id
            )
        except Exception:
            return None

    def is_coherent(self) -> bool:
        return bool(
            type(self._configured_org_id) is str
            and bool(self._configured_org_id.strip())
            and type(self._snapshot) is AuthorityPolicySnapshot
            and self._snapshot.org_id == self._configured_org_id
            and type(self._authorizer) is SnapshotCentralAuthorizer
            and getattr(self._authorizer, "_snapshot", None) is self._snapshot
            and callable(self._resolver)
        )


@final
class ProductionAuthorityCapability:
    """Single-use proof that real P17.8 boundaries share one authority graph.

    A capability cannot be substituted by a Protocol-shaped fake: all security
    boundary classes are exact concrete types and their private dependencies are
    checked by identity.  This is a trusted-process wiring seal, not a durable
    epoch, transaction, OIDC adapter, or server runtime claim.
    """

    def __init__(
        self,
        *,
        configured_org_id: str,
        snapshot: AuthorityPolicySnapshot,
        authorizer: SnapshotCentralAuthorizer,
        identity_resolver: ProductionIdentityResolver,
        operational_authorization: OperationalAuthorization,
        worker_authorization: WorkerAuthorization,
        worker_binding_source: StrictSnapshotWorkerBindingSource,
        question_surface: QuestionSurfaceComposition,
    ) -> None:
        self._configured_org_id = configured_org_id
        self._snapshot = snapshot
        self._authorizer = authorizer
        self._identity_resolver = identity_resolver
        self._operational_authorization = operational_authorization
        self._worker_authorization = worker_authorization
        self._worker_binding_source = worker_binding_source
        self._question_surface = question_surface
        self._state = "issued"

    def claim(self, composition: object) -> bool:
        """Bind the capability to the exact bootstrapped Question Surface once."""
        surface = self._question_surface
        # Capability state도 이 exact QSC의 lifecycle lock만으로 보호한다. 따라서
        # claim과 close 사이에 별도 lock 순서가 생기지 않는다.
        with surface._close_lock:  # pyright: ignore[reportPrivateUsage]
            if self._state != "issued":
                return False
            if type(composition) is not QuestionSurfaceComposition or composition is not surface:
                self._state = "revoked"
                return False
            if not self.is_coherent():
                self._state = "revoked"
                return False
            if not _question_surface_matches_production_authority(
                composition,
                central_authorizer=self._authorizer,
                identity_resolver=self._identity_resolver,
                operational_authorization=self._operational_authorization,
            ):
                self._state = "revoked"
                return False
            if not _register_question_surface_production_authority_claim(composition, self):
                self._state = "revoked"
                return False
            self._state = "claimed"
            return True

    def revoke(self) -> None:
        with self._question_surface._close_lock:  # pyright: ignore[reportPrivateUsage]
            self._state = "revoked"

    def matches_configured_org(self, org_id: object) -> bool:
        """Keep bootstrap configuration and the sealed authority graph one-org."""
        return type(org_id) is str and org_id == self._configured_org_id and self.is_coherent()

    def is_coherent(self) -> bool:
        """Recheck identity, not just equivalent configuration values."""
        snapshot = self._snapshot
        authorizer = self._authorizer
        source = self._worker_binding_source
        operational = self._operational_authorization
        worker = self._worker_authorization
        resolver = self._identity_resolver
        return bool(
            type(self._configured_org_id) is str
            and bool(self._configured_org_id.strip())
            and type(snapshot) is AuthorityPolicySnapshot
            and snapshot.org_id == self._configured_org_id
            and type(authorizer) is SnapshotCentralAuthorizer
            and getattr(authorizer, "_snapshot", None) is snapshot
            and type(resolver) is ProductionIdentityResolver
            and resolver.is_coherent()
            and getattr(resolver, "_configured_org_id", None) == self._configured_org_id
            and getattr(resolver, "_snapshot", None) is snapshot
            and getattr(resolver, "_authorizer", None) is authorizer
            and type(operational) is OperationalAuthorization
            and getattr(operational, "_configured_org_id", None) == self._configured_org_id
            and getattr(operational, "_central_authorizer", None) is authorizer
            and type(source) is StrictSnapshotWorkerBindingSource
            and getattr(source, "_source", None) is snapshot
            and type(worker) is WorkerAuthorization
            and getattr(worker, "_configured_org_id", None) == self._configured_org_id
            and getattr(worker, "_central_authorizer", None) is authorizer
            and getattr(worker, "_binding_source", None) is source
            and type(self._question_surface) is QuestionSurfaceComposition
            and _question_surface_matches_production_authority(
                self._question_surface,
                central_authorizer=authorizer,
                identity_resolver=resolver,
                operational_authorization=operational,
            )
        )
