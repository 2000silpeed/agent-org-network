from __future__ import annotations

from datetime import date
from dataclasses import dataclass

import pytest
import yaml

from agent_org_network.agent_card import AgentCard
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorizationDenied,
    AuthorizationGrant,
    ConflictOpenRequestSnapshot,
    SnapshotCentralAuthorizer,
    canonical_policy_digest,
    load_authority_policy_yaml,
)
from agent_org_network.conflict_open_contract import (
    CONFLICT_OPEN_MANAGER_SELECTION_AVAILABLE,
    CONFLICT_OPEN_REGISTRY_ORG_BINDING_AVAILABLE,
    CONFLICT_OPEN_ROOT_SELECTION_AVAILABLE,
    ConflictOpenCandidateClaim,
    ConflictOpenContractError,
    RegistryConflictOpenSnapshotReader,
    conflict_open_resource,
)
from agent_org_network.question_request import RouteTarget
from agent_org_network.registry import Registry
from agent_org_network.user import User


def _card(card_id: str, owner: str, domains: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=card_id,
        owner=owner,
        team="team",
        summary="summary",
        domains=domains or ["billing"],
        last_reviewed_at=date(2026, 1, 1),
    )


def _registry() -> Registry:
    registry = Registry()
    for user_id in ("requester", "owner-1", "owner-2"):
        registry.register_user(User(id=user_id))
    registry.register(_card("card-1", "owner-1"))
    registry.register(_card("card-2", "owner-2"))
    return registry


def _claims() -> tuple[ConflictOpenCandidateClaim, ...]:
    return tuple(
        ConflictOpenCandidateClaim(
            card, "billing", RouteTarget(intent="billing", agent_id=card, requires_approval=False)
        )
        for card in ("card-1", "card-2")
    )


@dataclass(frozen=True)
class _RequestResolver:
    current: ConflictOpenRequestSnapshot | None

    def resolve_conflict_open_request(
        self, *, request_id: str
    ) -> ConflictOpenRequestSnapshot | None:
        return (
            self.current
            if self.current is not None and self.current.request_id == request_id
            else None
        )


@dataclass(frozen=True)
class _RegistryScope:
    org_id: str

    def proves_registry_org(self, *, registry: Registry, org_id: str) -> bool:
        return org_id == self.org_id


def _scope(org_id: str = "acme") -> _RegistryScope:
    return _RegistryScope(org_id)


def _authorizer(
    *,
    subject_id: str = "requester",
    roles: list[str] | None = None,
    permission_role: str = "requester",
    request: ConflictOpenRequestSnapshot | None = None,
) -> SnapshotCentralAuthorizer:
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "p1",
        "content_sha256": "pending",
        "subject_roles": [
            {"org_id": "acme", "subject_id": subject_id, "roles": roles or ["requester"]}
        ],
        "role_permissions": [{"role": permission_role, "actions": ["conflict.open"]}],
        "route_rules": [],
        "worker_bindings": [],
    }
    document["content_sha256"] = canonical_policy_digest(document)
    return SnapshotCentralAuthorizer(
        load_authority_policy_yaml(yaml.safe_dump(document), expected_org_id="acme"),
        conflict_open_request_resolver=_RequestResolver(
            request
            or ConflictOpenRequestSnapshot(
                org_id="acme",
                request_id="request-1",
                requester_subject_id="requester",
                state_kind="received",
                revision=0,
            )
        ),
    )


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="requester", identity_provider="oidc", identity_session_id="s1"
    )


def test_conflict_open은_requester_owned_question_request만_허용한다() -> None:
    resource = conflict_open_resource(
        org_id="acme", request_id="request-1", requester_subject_id="requester"
    )
    authorizer = _authorizer()
    grant = authorizer.authorize(_principal(), "conflict.open", resource)
    assert isinstance(grant, AuthorizationGrant)
    assert isinstance(
        authorizer.authorize(
            _principal(), "conflict.open", resource.model_copy(update={"kind": "conflict_case"})
        ),
        AuthorizationDenied,
    )
    assert isinstance(
        authorizer.authorize(
            _principal(), "conflict.open", resource.model_copy(update={"owner_subject_id": "other"})
        ),
        AuthorizationDenied,
    )
    assert isinstance(
        authorizer.authorize(_principal(), "question.create", resource), AuthorizationDenied
    )
    assert not authorizer.verify(grant, _principal(), "question.create", resource)


def test_snapshot은_ordered_card_owner_intent_under_claim_route와_two_digest를_발행한다() -> None:
    snapshot = RegistryConflictOpenSnapshotReader(_registry(), _scope()).snapshot(
        org_id="acme", claims=_claims()
    )
    assert [value.owner_subject_id for value in snapshot.candidates] == ["owner-1", "owner-2"]
    assert all(value.under_claim for value in snapshot.candidates)
    assert len(snapshot.candidate_digest) == len(snapshot.claim_digest) == 64
    assert (
        not CONFLICT_OPEN_MANAGER_SELECTION_AVAILABLE and not CONFLICT_OPEN_ROOT_SELECTION_AVAILABLE
    )
    assert not CONFLICT_OPEN_REGISTRY_ORG_BINDING_AVAILABLE


@pytest.mark.parametrize(
    "claims",
    [
        (
            ConflictOpenCandidateClaim(
                "card-1",
                "billing",
                RouteTarget(intent="billing", agent_id="card-1", requires_approval=False),
            ),
        )
        * 2,
        (
            ConflictOpenCandidateClaim(
                "card-1",
                "other",
                RouteTarget(intent="other", agent_id="card-1", requires_approval=False),
            ),
        ),
    ],
)
def test_duplicate_or_under_claim_false_claim은_거부한다(
    claims: tuple[ConflictOpenCandidateClaim, ...],
) -> None:
    with pytest.raises(ConflictOpenContractError):
        RegistryConflictOpenSnapshotReader(_registry(), _scope()).snapshot(
            org_id="acme", claims=claims
        )


def test_verify_current은_card_owner_route_drift를_fail_closed한다() -> None:
    registry = _registry()
    reader = RegistryConflictOpenSnapshotReader(registry, _scope())
    claims = _claims()
    snapshot = reader.snapshot(org_id="acme", claims=claims)
    registry.replace_card(_card("card-1", "owner-2"))
    with pytest.raises(ConflictOpenContractError):
        reader.verify_current(snapshot, claims=claims)


def test_verify_current은_same_card의_route_claim_drift도_거부한다() -> None:
    reader = RegistryConflictOpenSnapshotReader(_registry(), _scope())
    snapshot = reader.snapshot(org_id="acme", claims=_claims())
    changed = list(_claims())
    changed[0] = ConflictOpenCandidateClaim(
        "card-1",
        "billing",
        RouteTarget(intent="billing", agent_id="card-1", requires_approval=True),
    )
    with pytest.raises(ConflictOpenContractError):
        reader.verify_current(snapshot, claims=changed)


@pytest.mark.parametrize("role", ["manager", "admin"])
def test_conflict_open은_policy가_잘못_부여해도_requester_외_role을_거부한다(role: str) -> None:
    resource = conflict_open_resource(
        org_id="acme", request_id="request-1", requester_subject_id=role
    )
    authorizer = _authorizer(
        subject_id=role,
        roles=[role],
        permission_role=role,
        request=ConflictOpenRequestSnapshot(
            org_id="acme",
            request_id="request-1",
            requester_subject_id=role,
            state_kind="received",
            revision=0,
        ),
    )
    assert isinstance(
        authorizer.authorize(
            AuthenticatedPrincipal(
                org_id="acme", subject_id=role, identity_provider="oidc", identity_session_id="s1"
            ),
            "conflict.open",
            resource,
        ),
        AuthorizationDenied,
    )


@pytest.mark.parametrize(
    "update",
    [
        {"org_id": "other"},
        {"requester_subject_id": "other"},
        {"state_kind": "received", "revision": 0},
    ],
)
def test_conflict_open은_resolver의_current_org_requester_received_rev0_exact_proof를_요구한다(
    update: dict[str, object],
) -> None:
    current = ConflictOpenRequestSnapshot(
        org_id="acme",
        request_id="request-1",
        requester_subject_id="requester",
        state_kind="received",
        revision=0,
    )
    if update == {"state_kind": "received", "revision": 0}:
        snapshot = _authorizer()._snapshot  # pyright: ignore[reportPrivateUsage]
        assert snapshot is not None
        authorizer = SnapshotCentralAuthorizer(snapshot)
    else:
        # pydantic rejects non-Received/non-zero at construction, so use valid-but-mismatched proof.
        authorizer = _authorizer(request=current.model_copy(update=update))
    resource = conflict_open_resource(
        org_id="acme", request_id="request-1", requester_subject_id="requester"
    )
    assert isinstance(
        authorizer.authorize(_principal(), "conflict.open", resource), AuthorizationDenied
    )


def test_registry_scope_adapter_proof가_없으면_snapshot을_거부한다() -> None:
    with pytest.raises(ConflictOpenContractError):
        RegistryConflictOpenSnapshotReader(_registry(), _scope("other")).snapshot(
            org_id="acme", claims=_claims()
        )
