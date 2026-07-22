from __future__ import annotations

import ast
from dataclasses import dataclass, replace
from pathlib import Path

from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.conflict_escalation_approval_evidence import (
    ESCALATE_ACTION,
    ConflictEscalationApprovalEvidence,
    canonical_escalate_command_digest,
    escalation_cause_digest,
    escalation_resource_fingerprint,
)
from agent_org_network.conflict_escalation_approval_verifier import (
    acquire_escalation_approval,
    escalation_approval_binds,
    reconfirm_escalation_approval,
)
from agent_org_network.conflict_escalation_registry_snapshot import (
    ConflictEscalationRegistrySnapshot,
)
from agent_org_network.durable_conflict_escalation_evidence import DivergentVotes


def _resource() -> ResourceRef:
    return ResourceRef(org_id="acme", kind="conflict_case", resource_id="conflict-1")


def _command() -> dict[str, object]:
    return {"conflict_id": "conflict-1"}


def _cause() -> DivergentVotes:
    return DivergentVotes(
        org_ref="org:1",
        conflict_ref="conflict:1",
        request_ref="request:1",
        awaiting_revision=1,
        concurrence_round=2,
        candidate_snapshot_sha256="b" * 64,
        candidate_owner_count=2,
        baseline_sha256="c" * 64,
        candidate_claim_sha256="d" * 64,
        vote_set_sha256="e" * 64,
        evaluated_at="2026-07-22T00:00:00+00:00",
    )


def _graph_snapshot(*, graph_digest: str = "2" * 64) -> ConflictEscalationRegistrySnapshot:
    return ConflictEscalationRegistrySnapshot(
        org_id="acme",
        candidates=(),
        owner_paths=(),
        manager_subject_ref=None,
        root_subject_ref="subject:root",
        candidate_digest="f" * 64,
        claim_digest="1" * 64,
        graph_digest=graph_digest,
    )


def _valid_evidence() -> ConflictEscalationApprovalEvidence:
    resource = _resource()
    return ConflictEscalationApprovalEvidence(
        evidence_id="evidence-1",
        status="granted",
        action=ESCALATE_ACTION,
        command_digest=canonical_escalate_command_digest(resource=resource, command=_command()),
        resource_fingerprint=escalation_resource_fingerprint(resource),
        escalation_cause_digest=escalation_cause_digest(_cause()),
        graph_selection_digest=_graph_snapshot().graph_digest,
    )


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="operator-1", identity_provider="oidc", identity_session_id="s1"
    )


@dataclass
class _SpyProvider:
    evidence: ConflictEscalationApprovalEvidence
    calls: int = 0

    def acquire_escalate_approval(
        self,
        principal: AuthenticatedPrincipal,
        action: str,
        resource: ResourceRef,
        command_digest: str,
    ) -> ConflictEscalationApprovalEvidence:
        self.calls += 1
        return self.evidence


@dataclass(frozen=True)
class _ImpostorEvidence:
    evidence_id: str
    status: str
    action: str
    command_digest: str
    resource_fingerprint: str
    escalation_cause_digest: str
    graph_selection_digest: str


@dataclass(frozen=True)
class _Resolver:
    current: ConflictEscalationApprovalEvidence | None

    def resolve_escalation_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> ConflictEscalationApprovalEvidence | None:
        return (
            self.current
            if self.current is not None and self.current.evidence_id == evidence_id
            else None
        )


def test_escalation_approval_binds는_4결박_전부_일치할때만_True다() -> None:
    evidence = _valid_evidence()
    assert escalation_approval_binds(
        evidence,
        resource=_resource(),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )


def test_escalation_approval_binds는_resource_불일치면_False다() -> None:
    evidence = _valid_evidence()
    assert not escalation_approval_binds(
        evidence,
        resource=_resource().model_copy(update={"resource_id": "other-conflict"}),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )


def test_escalation_approval_binds는_command_불일치면_False다() -> None:
    evidence = _valid_evidence()
    assert not escalation_approval_binds(
        evidence,
        resource=_resource(),
        command={"conflict_id": "other-conflict"},
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )


def test_escalation_approval_binds는_cause_불일치면_False다() -> None:
    evidence = _valid_evidence()
    assert not escalation_approval_binds(
        evidence,
        resource=_resource(),
        command=_command(),
        cause=replace(_cause(), conflict_ref="conflict:2"),
        graph_snapshot=_graph_snapshot(),
    )


def test_escalation_approval_binds는_graph_selection_불일치면_False다() -> None:
    evidence = _valid_evidence()
    assert not escalation_approval_binds(
        evidence,
        resource=_resource(),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(graph_digest="3" * 64),
    )


def test_escalation_approval_binds는_evidence_type이_다르면_False다() -> None:
    evidence = _valid_evidence()
    impostor = _ImpostorEvidence(
        evidence_id=evidence.evidence_id,
        status=evidence.status,
        action=evidence.action,
        command_digest=evidence.command_digest,
        resource_fingerprint=evidence.resource_fingerprint,
        escalation_cause_digest=evidence.escalation_cause_digest,
        graph_selection_digest=evidence.graph_selection_digest,
    )
    assert not escalation_approval_binds(
        impostor,
        resource=_resource(),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )


def test_acquire_escalation_approval는_취득_1회와_same_evidence_재확인을_요구한다() -> None:
    evidence = _valid_evidence()
    provider = _SpyProvider(evidence)
    resolver = _Resolver(evidence)

    acquired = acquire_escalation_approval(
        _principal(),
        ESCALATE_ACTION,
        _resource(),
        evidence.command_digest,
        provider=provider,
        resolver=resolver,
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )

    assert acquired == evidence
    assert provider.calls == 1


def test_acquire_escalation_approval는_resolver_불일치면_None이고_1회만_취득한다() -> None:
    evidence = _valid_evidence()
    provider = _SpyProvider(evidence)
    resolver = _Resolver(None)

    acquired = acquire_escalation_approval(
        _principal(),
        ESCALATE_ACTION,
        _resource(),
        evidence.command_digest,
        provider=provider,
        resolver=resolver,
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )

    assert acquired is None
    assert provider.calls == 1


def test_acquire_escalation_approval는_binds_불일치면_None이다() -> None:
    evidence = _valid_evidence()
    provider = _SpyProvider(evidence)
    resolver = _Resolver(evidence)

    acquired = acquire_escalation_approval(
        _principal(),
        ESCALATE_ACTION,
        _resource(),
        evidence.command_digest,
        provider=provider,
        resolver=resolver,
        command={"conflict_id": "other-conflict"},
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )

    assert acquired is None


def test_reconfirm_escalation_approval는_write_직전과_replay가_공유하고_재취득하지않는다() -> None:
    evidence = _valid_evidence()
    provider = _SpyProvider(evidence)
    resolver = _Resolver(evidence)

    acquired = acquire_escalation_approval(
        _principal(),
        ESCALATE_ACTION,
        _resource(),
        evidence.command_digest,
        provider=provider,
        resolver=resolver,
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )
    assert acquired is not None
    assert provider.calls == 1

    prewrite_ok = reconfirm_escalation_approval(
        acquired,
        resolver=resolver,
        org_id="acme",
        resource=_resource(),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )
    replay_ok = reconfirm_escalation_approval(
        acquired,
        resolver=resolver,
        org_id="acme",
        resource=_resource(),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )

    assert prewrite_ok
    assert replay_ok
    assert provider.calls == 1


def test_reconfirm_escalation_approval는_resolver_None이나_불일치면_무효다() -> None:
    evidence = _valid_evidence()

    assert not reconfirm_escalation_approval(
        evidence,
        resolver=_Resolver(None),
        org_id="acme",
        resource=_resource(),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )
    assert not reconfirm_escalation_approval(
        evidence,
        resolver=_Resolver(replace(evidence, evidence_id="other-evidence")),
        org_id="acme",
        resource=_resource(),
        command=_command(),
        cause=_cause(),
        graph_snapshot=_graph_snapshot(),
    )


def test_conflict_escalation_approval_verifier_module은_hitl_toggle을_import하지않는다() -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "agent_org_network"
        / "conflict_escalation_approval_verifier.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"hitl", "durable_credentials"}
    imported = {
        alias.name.split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        (node.module or "").split(".")[-1]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert imported.isdisjoint(forbidden)
