"""R3.2 production materialization verifier의 결정론 회귀."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, cast

import pytest

import agent_org_network.sqlite_durable_credential_issue_commit as materialization_commit
from agent_org_network.central_authority import (
    AuthenticatedPrincipal,
    AuthorityPolicySnapshot,
    ResourceRef,
    RolePermission,
    SnapshotCentralAuthorizer,
    SubjectRoleBinding,
    canonical_policy_digest,
)
from agent_org_network.credential_issue_materialization_verifier import (
    CredentialIssueMaterializationOperations,
    CredentialIssueMaterializationVerifierCapability,
    CurrentCredentialApprovalEvidenceResolver,
    CurrentCredentialPrincipalResolver,
    CurrentCredentialResourceResolver,
    create_credential_issue_materialization_verifier_capability,
    create_credential_issue_materialization_operations,
)
from agent_org_network.durable_credentials import (
    CredentialApprovalEvidence,
    canonical_credential_command_digest,
    resource_fingerprint,
)
from agent_org_network.sqlite_durable_credential_issue_commit import (
    CredentialIssueMaterializationSnapshot,
    SqliteCredentialIssueCommitError,
)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        org_id="acme", subject_id="operator-1", identity_provider="oidc", identity_session_id="s-1"
    )


def _central_authorizer(*, allowed: bool = True) -> SnapshotCentralAuthorizer:
    binding = SubjectRoleBinding(org_id="acme", subject_id="operator-1", roles=("operator",))
    actions = ("worker_credential.issue",) if allowed else ("worker_credential.read",)
    permission = RolePermission(role="operator", actions=cast(Any, actions))
    document: dict[str, object] = {
        "schema_version": 1,
        "org_id": "acme",
        "policy_version": "test",
        "content_sha256": "pending",
        "subject_roles": [binding.model_dump(mode="json")],
        "role_permissions": [permission.model_dump(mode="json")],
        "route_rules": [],
        "worker_bindings": [],
    }
    return SnapshotCentralAuthorizer(
        AuthorityPolicySnapshot(
            schema_version=1,
            org_id="acme",
            policy_version="test",
            content_sha256=canonical_policy_digest(document),
            subject_roles=(binding,),
            role_permissions=(permission,),
            route_rules=(),
            worker_bindings=(),
        )
    )


class _PrincipalResolver:
    available = True

    def resolve_credential_principal(
        self, *, org_id: str, subject_id: str
    ) -> AuthenticatedPrincipal | None:
        return (
            _principal()
            if self.available and (org_id, subject_id) == ("acme", "operator-1")
            else None
        )


class _ResourceResolver:
    owner = "owner-1"

    def resolve_credential_resource(self, *, org_id: str, credential_id: str) -> ResourceRef | None:
        if (org_id, credential_id) != ("acme", "credential-1"):
            return None
        return ResourceRef(
            org_id="acme",
            kind="worker_credential",
            resource_id="credential-1",
            owner_subject_id=self.owner,
        )


class _EvidenceResolver:
    available = True
    evidence: CredentialApprovalEvidence

    def resolve_credential_approval_evidence(
        self, *, org_id: str, evidence_id: str
    ) -> CredentialApprovalEvidence | None:
        return (
            self.evidence
            if self.available and (org_id, evidence_id) == ("acme", "evidence-1")
            else None
        )


def _snapshot() -> tuple[CredentialIssueMaterializationSnapshot, _EvidenceResolver]:
    resource = ResourceRef(
        org_id="acme",
        kind="worker_credential",
        resource_id="credential-1",
        owner_subject_id="owner-1",
    )
    command = {"owner_subject_id": "owner-1", "role": "worker", "expires_at": None}
    digest = canonical_credential_command_digest(
        action="worker_credential.issue", resource=resource, command=command
    )
    evidence = CredentialApprovalEvidence(
        evidence_id="evidence-1",
        action="worker_credential.issue",
        command_digest=digest,
        resource_fingerprint=resource_fingerprint(resource),
    )
    resolver = _EvidenceResolver()
    resolver.evidence = evidence
    target = {
        "approval_command_digest": digest,
        "approval_evidence_id": evidence.evidence_id,
        "approval_resource_fingerprint": evidence.resource_fingerprint,
        "command_digest": digest,
        "credential_id": "credential-1",
        "expires_at": None,
        "org_id": "acme",
        "owner_subject_id": "owner-1",
        "principal_id": "operator-1",
        "resource_fingerprint": evidence.resource_fingerprint,
        "role": "worker",
        "target_generation": 2,
        "target_id": "target-1",
    }
    import json

    target_json = json.dumps(target, sort_keys=True, separators=(",", ":"))
    return (
        CredentialIssueMaterializationSnapshot(
            target_json=target_json,
            target_id="target-1",
            target_generation=2,
            stage_key="a" * 64,
            secret_hash="b" * 64,
            delivery_ref="delivery:v1:" + "c" * 64,
            claim_generation=1,
            snapshot_digest="d" * 64,
        ),
        resolver,
    )


def _verifier(*, allowed: bool = True):  # type: ignore[no-untyped-def]
    snapshot, evidence = _snapshot()
    principals = _PrincipalResolver()
    resources = _ResourceResolver()
    capability = create_credential_issue_materialization_verifier_capability(
        central_authorizer=_central_authorizer(allowed=allowed),
        principal_resolver=principals,
        resource_resolver=resources,
        approval_evidence_resolver=evidence,
    )
    return snapshot, capability, principals, resources, evidence


def test_exact_composition_rechecks_current_authority_evidence_and_resource_at_prewrite() -> None:
    snapshot, capability, _principals, resources, evidence = _verifier()
    verifier = capability._claim_for_operations()  # pyright: ignore[reportPrivateUsage]
    proof = verifier.prepare(snapshot)
    assert verifier.verify_prewrite(proof, snapshot) is True
    resources.owner = "owner-drift"
    assert verifier.verify_prewrite(proof, snapshot) is False
    resources.owner = "owner-1"
    evidence.available = False
    assert verifier.verify_prewrite(proof, snapshot) is False


@pytest.mark.parametrize("allowed", (False,))
def test_denied_current_central_grant_never_mints_proof(allowed: bool) -> None:
    snapshot, capability, _principals, _resources, _evidence = _verifier(allowed=allowed)
    with pytest.raises(RuntimeError):
        capability._claim_for_operations().prepare(  # pyright: ignore[reportPrivateUsage]
            snapshot
        )


def test_public_operations_claim_exact_capability_once_and_never_accept_a_verifier(
    tmp_path: Path,
) -> None:
    _snapshot, capability, _principals, _resources, _evidence = _verifier()
    operations = create_credential_issue_materialization_operations(capability)
    assert type(operations) is CredentialIssueMaterializationOperations
    assert tuple(inspect.signature(operations.commit).parameters) == (
        "path",
        "org_id",
        "target_id",
        "now",
        "release",
    )
    assert not hasattr(materialization_commit, "commit_sqlite_durable_credential_issue_target")
    with pytest.raises(SqliteCredentialIssueCommitError):
        operations.commit(
            tmp_path / "missing.sqlite", "acme", "target-1", "2026-07-19T00:00:00.000Z"
        )
    with pytest.raises(ValueError):
        create_credential_issue_materialization_operations(capability)
    with pytest.raises(TypeError):
        create_credential_issue_materialization_operations(
            cast(CredentialIssueMaterializationVerifierCapability, object())
        )
    with pytest.raises(TypeError):
        create_credential_issue_materialization_verifier_capability(
            central_authorizer=cast(SnapshotCentralAuthorizer, object()),
            principal_resolver=cast(CurrentCredentialPrincipalResolver, object()),
            resource_resolver=cast(CurrentCredentialResourceResolver, object()),
            approval_evidence_resolver=cast(CurrentCredentialApprovalEvidenceResolver, object()),
        )


def test_capability_constructor_rejects_forged_verifier_and_factory_seal() -> None:
    with pytest.raises(TypeError, match="factory"):
        CredentialIssueMaterializationVerifierCapability(cast(Any, object()), object())
