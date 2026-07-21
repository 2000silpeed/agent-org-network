from __future__ import annotations

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event

import pytest

from agent_org_network.agent_card import AgentCard
from agent_org_network.demo_question_surfaces import DemoRouteAuthority
from agent_org_network.p17_manager_disposition import (
    AuthorityAssignment,
    AuthorityAssignmentConflictError,
    AuthorityAssignmentReceipt,
    AuthorityAssignmentRejected,
)
from agent_org_network.question_resolution import AuthorityGrant
from agent_org_network.registry import Registry
from agent_org_network.request_route_authority import (
    FromOwnerConsensusGrant,
    RequestRouteGrantAssignment,
    RequestRouteGrantConflict,
    RequestRouteGrantReceipt,
    RequestRouteGrantRejected,
)
from agent_org_network.user import User


def _registry() -> Registry:
    registry = Registry()
    registry.register_user(User(id="owner-a"))
    registry.register_user(User(id="owner-b"))
    registry.register(
        AgentCard(
            agent_id="refund-card",
            owner="owner-a",
            team="support",
            summary="refund",
            domains=["refund"],
            last_reviewed_at=date(2026, 7, 1),
        )
    )
    registry.register(
        AgentCard(
            agent_id="finance-card",
            owner="owner-b",
            team="finance",
            summary="finance",
            domains=["refund"],
            last_reviewed_at=date(2026, 7, 1),
        )
    )
    return registry


def _consensus_assignment(
    *,
    request_id: str = "request-1",
    agent_id: str = "refund-card",
    key: str = "conflict-disposition:case-1:1",
) -> RequestRouteGrantAssignment:
    return RequestRouteGrantAssignment(
        org_id="demo-org",
        request_id=request_id,
        intent="refund",
        agent_id=agent_id,
        source=FromOwnerConsensusGrant(case_id="case-1", round=1),
        idempotency_key=key,
    )


def test_request_grant는_same_assignment에_same_receipt이고_first_winner를_지킨다() -> None:
    authority = DemoRouteAuthority(_registry())
    assignment = _consensus_assignment()

    first = authority.grant_for_request(assignment)
    second = authority.grant_for_request(assignment)

    assert isinstance(first, RequestRouteGrantReceipt)
    assert second == first
    assert authority.authorize_for_request(
        "demo-org", "request-1", "refund", "refund-card"
    ) == AuthorityGrant(policy_version=first.grant_version)

    assert isinstance(
        authority.grant_for_request(_consensus_assignment(agent_id="finance-card")),
        RequestRouteGrantConflict,
    )
    assert isinstance(
        authority.grant_for_request(_consensus_assignment(key="another-key")),
        RequestRouteGrantConflict,
    )
    assert (
        authority.authorize_for_request("demo-org", "request-1", "refund", "finance-card") is None
    )


def test_request_grant_policy_reject는_write0와요청key를_반사한다() -> None:
    authority = DemoRouteAuthority(_registry())
    assignment = _consensus_assignment(agent_id="missing-card")

    rejected = authority.grant_for_request(assignment)

    assert rejected == RequestRouteGrantRejected(
        idempotency_key=assignment.idempotency_key,
        reason_code="target_not_found",
    )
    assert (
        authority.authorize_for_request("demo-org", "request-1", "refund", "missing-card") is None
    )
    accepted = authority.grant_for_request(_consensus_assignment())
    assert isinstance(accepted, RequestRouteGrantReceipt)


def test_p17_4_assign_owner_facade는_원_assignment_shape와같은_backing을_보존한다() -> None:
    authority = DemoRouteAuthority(_registry())
    legacy = AuthorityAssignment(
        org_id="demo-org",
        request_id="request-1",
        item_id="item-1",
        intent="refund",
        agent_id="refund-card",
        assigned_by="root",
        idempotency_key="manager-disposition:item-1",
    )

    receipt = authority.assign_owner(legacy)

    assert isinstance(receipt, AuthorityAssignmentReceipt)
    assert receipt.assignment == legacy
    assert authority.authorize_for_request(
        "demo-org", "request-1", "refund", "refund-card"
    ) == AuthorityGrant(policy_version=receipt.grant_version)
    common_conflict = authority.grant_for_request(_consensus_assignment(agent_id="finance-card"))
    assert isinstance(common_conflict, RequestRouteGrantConflict)

    conflicting_legacy = legacy.model_copy(update={"agent_id": "finance-card"})
    with pytest.raises(AuthorityAssignmentConflictError):
        authority.assign_owner(conflicting_legacy)


def test_p17_4_facade_rejected와_alias_safety를_보존한다() -> None:
    authority = DemoRouteAuthority(_registry())
    rejected = authority.assign_owner(
        AuthorityAssignment(
            org_id="demo-org",
            request_id="request-x",
            item_id="item-x",
            intent="refund",
            agent_id="missing-card",
            assigned_by="root",
            idempotency_key="manager-disposition:item-x",
        )
    )
    assert rejected == AuthorityAssignmentRejected(reason_code="target_not_found")

    assignment = _consensus_assignment(request_id="request-2")
    receipt = authority.grant_for_request(assignment)
    assert isinstance(receipt, RequestRouteGrantReceipt)
    original_version = receipt.grant_version
    object.__setattr__(receipt, "grant_version", "forged")
    retry = authority.grant_for_request(assignment)
    assert isinstance(retry, RequestRouteGrantReceipt)
    assert retry.grant_version == original_version


def test_request_authority_32way_first_winner는_한_assignment와version만_남긴다() -> None:
    authority = DemoRouteAuthority(_registry())
    assignments = (
        _consensus_assignment(agent_id="refund-card"),
        _consensus_assignment(agent_id="finance-card", key="conflict-disposition:case-2:1"),
    ) * 16
    barrier = Barrier(33)
    release = Event()

    def grant(assignment: RequestRouteGrantAssignment) -> object:
        barrier.wait()
        assert release.wait(timeout=5)
        return authority.grant_for_request(assignment)

    with ThreadPoolExecutor(max_workers=32) as executor:
        futures = [executor.submit(grant, assignment) for assignment in assignments]
        barrier.wait()
        release.set()
        results = [future.result(timeout=5) for future in futures]

    kinds: Counter[str] = Counter(result.kind for result in results)  # type: ignore[attr-defined]
    assert kinds == Counter({"receipt": 16, "conflict": 16})
    receipts = [result for result in results if isinstance(result, RequestRouteGrantReceipt)]
    assert len({(receipt.assignment, receipt.grant_version) for receipt in receipts}) == 1
