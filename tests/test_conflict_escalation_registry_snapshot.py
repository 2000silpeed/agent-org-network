"""S4.3c.0 graph-aware Conflict escalation Registry snapshot contract."""

from __future__ import annotations

from datetime import date

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.conflict_escalation_registry_snapshot import (
    ConflictEscalationRegistrySnapshotError,
    RegistryConflictEscalationSnapshotReader,
)
from agent_org_network.conflict_open_contract import ConflictOpenCandidateClaim
from agent_org_network.question_request import RouteTarget
from agent_org_network.registry import Registry
from agent_org_network.user import User


class _Scope:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed

    def proves_registry_org(self, *, registry: Registry, org_id: str) -> bool:
        return self.allowed and org_id == "org-1"


def _card(card_id: str, owner: str, *, domains: list[str] | None = None) -> AgentCard:
    return AgentCard(
        agent_id=card_id,
        owner=owner,
        team="team",
        summary="summary",
        domains=domains or ["billing"],
        last_reviewed_at=date(2026, 1, 1),
    )


def _claims() -> tuple[ConflictOpenCandidateClaim, ...]:
    return tuple(
        ConflictOpenCandidateClaim(
            card_id=card_id,
            intent="billing",
            route=RouteTarget(intent="billing", agent_id=card_id, requires_approval=False),
        )
        for card_id in ("card-1", "card-2")
    )


def _registry(*, root: str = "root", owner_2_manager: str = "manager") -> Registry:
    registry = Registry()
    for user in (
        User(id=root),
        User(id="manager", manager=root),
        User(id="owner-1", manager="manager"),
        User(id="owner-2", manager=owner_2_manager),
    ):
        registry.register_user(user)
    registry.register(_card("card-1", "owner-1"))
    registry.register(_card("card-2", "owner-2"))
    return registry


def test_snapshot은_ordered_candidate와_common_manager_graph_proof를_한_guard에서발행한다() -> None:
    snapshot = RegistryConflictEscalationSnapshotReader(_registry(), _Scope()).snapshot(
        org_id="org-1", claims=_claims()
    )
    assert snapshot.manager_subject_ref is not None
    assert snapshot.manager_subject_ref.startswith("subject:")
    assert snapshot.manager_subject_ref != snapshot.root_subject_ref
    assert [candidate.card_ref for candidate in snapshot.candidates] != ["card-1", "card-2"]
    assert all(candidate.under_claim for candidate in snapshot.candidates)
    assert snapshot.candidate_digest and snapshot.claim_digest and snapshot.graph_digest


@pytest.mark.parametrize(
    "kind",
    ("cycle", "self_loop", "missing_manager", "multiple_root", "no_scope", "claim_drift"),
)
def test_graph_or_claim_proof가없으면_fail_closed(kind: str) -> None:
    registry, scope, claims = _registry(), _Scope(), _claims()
    if kind == "cycle":
        registry = Registry()
        for user in (
            User(id="root"),
            User(id="manager", manager="owner-1"),
            User(id="owner-1", manager="manager"),
            User(id="owner-2", manager="manager"),
        ):
            registry.register_user(user)
        registry.register(_card("card-1", "owner-1"))
        registry.register(_card("card-2", "owner-2"))
    elif kind == "self_loop":
        registry = Registry()
        for user in (
            User(id="root"),
            User(id="manager", manager="manager"),
            User(id="owner-1", manager="manager"),
            User(id="owner-2", manager="manager"),
        ):
            registry.register_user(user)
        registry.register(_card("card-1", "owner-1"))
        registry.register(_card("card-2", "owner-2"))
    elif kind == "missing_manager":
        registry = _registry(owner_2_manager="missing")
    elif kind == "multiple_root":
        registry = _registry(owner_2_manager="root-2")
        registry.register_user(User(id="root-2"))
    elif kind == "no_scope":
        scope = _Scope(False)
    else:
        claims = (
            ConflictOpenCandidateClaim(
                "card-1",
                "other",
                RouteTarget(intent="other", agent_id="card-1", requires_approval=False),
            ),
        ) + _claims()[1:]
    with pytest.raises(ConflictEscalationRegistrySnapshotError):
        RegistryConflictEscalationSnapshotReader(registry, scope).snapshot(
            org_id="org-1", claims=claims
        )


def test_verify_current은_owner_graph_drift를거부한다() -> None:
    registry = _registry()
    reader = RegistryConflictEscalationSnapshotReader(registry, _Scope())
    snapshot = reader.snapshot(org_id="org-1", claims=_claims())
    registry.replace_card(_card("card-1", "owner-2"))
    with pytest.raises(ConflictEscalationRegistrySnapshotError):
        reader.verify_current(snapshot, claims=_claims())
