from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from agent_org_network.central_authority import ResourceRef
from agent_org_network.conflict_escalation_approval_evidence import (
    ESCALATE_ACTION,
    ConflictEscalationApprovalEvidence,
    canonical_escalate_command_digest,
    escalation_cause_digest,
    escalation_resource_fingerprint,
)
from agent_org_network.durable_conflict_escalation_evidence import (
    CandidateRegistryChanged,
    DivergentVotes,
)

_DIGEST = "a" * 64


def _divergent_votes(*, evaluated_at: str = "2026-07-22T00:00:00+00:00") -> DivergentVotes:
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
        evaluated_at=evaluated_at,
    )


def _resource() -> ResourceRef:
    return ResourceRef(org_id="acme", kind="conflict_case", resource_id="conflict-1")


def _evidence(**overrides: object) -> ConflictEscalationApprovalEvidence:
    fields: dict[str, object] = {
        "evidence_id": "evidence-1",
        "status": "granted",
        "action": ESCALATE_ACTION,
        "command_digest": _DIGEST,
        "resource_fingerprint": _DIGEST,
        "escalation_cause_digest": _DIGEST,
        "graph_selection_digest": _DIGEST,
    }
    fields.update(overrides)
    return ConflictEscalationApprovalEvidence(**fields)  # type: ignore[arg-type]


def test_evidence는_frozen이다() -> None:
    evidence = _evidence()
    with pytest.raises(Exception):
        evidence.evidence_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    "overrides",
    [
        {"evidence_id": " "},
        {"status": "expired"},
        {"action": "conflict.open"},
        {"command_digest": "short"},
        {"command_digest": "Z" * 64},
        {"resource_fingerprint": "g" * 63},
        {"escalation_cause_digest": "not-hex"},
        {"graph_selection_digest": "A" * 64},
    ],
)
def test_evidence는_shape_위반을_생성_실패로_거부한다(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _evidence(**overrides)


def test_canonical_escalate_command_digest는_같은_입력에_같은_digest_다른_입력에_다른_digest를_낸다() -> None:
    resource = _resource()
    first = canonical_escalate_command_digest(resource=resource, command={"a": 1})
    same = canonical_escalate_command_digest(resource=resource, command={"a": 1})
    different_command = canonical_escalate_command_digest(resource=resource, command={"a": 2})
    different_resource = canonical_escalate_command_digest(
        resource=resource.model_copy(update={"resource_id": "other"}), command={"a": 1}
    )

    assert first == same
    assert first != different_command
    assert first != different_resource
    assert len(first) == 64


def test_escalation_resource_fingerprint는_결정론이다() -> None:
    resource = _resource()
    assert escalation_resource_fingerprint(resource) == escalation_resource_fingerprint(resource)
    assert escalation_resource_fingerprint(resource) != escalation_resource_fingerprint(
        resource.model_copy(update={"resource_id": "other"})
    )


def test_escalation_cause_digest는_evaluated_at만_달라도_같은_digest다() -> None:
    first = escalation_cause_digest(_divergent_votes(evaluated_at="2026-07-22T00:00:00+00:00"))
    second = escalation_cause_digest(_divergent_votes(evaluated_at="2026-07-23T09:30:00+00:00"))

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    "overrides",
    [
        {"org_ref": "org:other"},
        {"conflict_ref": "conflict:2"},
        {"request_ref": "request:2"},
        {"awaiting_revision": 2},
        {"concurrence_round": 3},
        {"candidate_snapshot_sha256": "f" * 64},
        {"candidate_owner_count": 3},
        {"baseline_sha256": "1" * 64},
        {"candidate_claim_sha256": "2" * 64},
        {"vote_set_sha256": "3" * 64},
    ],
)
def test_escalation_cause_digest는_다른_안정_필드에_다른_digest를_낸다(
    overrides: dict[str, object],
) -> None:
    base = _divergent_votes()
    changed = replace(base, **overrides)

    assert escalation_cause_digest(base) != escalation_cause_digest(changed)


def test_escalation_cause_digest는_DivergentVotes와_CandidateRegistryChanged를_구분한다() -> None:
    divergent = _divergent_votes()
    registry_changed = CandidateRegistryChanged(
        org_ref=divergent.org_ref,
        conflict_ref=divergent.conflict_ref,
        request_ref=divergent.request_ref,
        awaiting_revision=divergent.awaiting_revision,
        concurrence_round=divergent.concurrence_round,
        candidate_snapshot_sha256=divergent.candidate_snapshot_sha256,
        current_candidate_snapshot_sha256="9" * 64,
        baseline_sha256=divergent.baseline_sha256,
        candidate_claim_sha256=divergent.candidate_claim_sha256,
        vote_set_sha256=divergent.vote_set_sha256,
        evaluated_at=divergent.evaluated_at,
    )

    assert escalation_cause_digest(divergent) != escalation_cause_digest(registry_changed)


def test_conflict_escalation_approval_evidence_module은_hitl_toggle을_import하지않는다() -> None:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "agent_org_network"
        / "conflict_escalation_approval_evidence.py"
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
