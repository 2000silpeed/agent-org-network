from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_org_network.central_authority import AuthenticatedPrincipal, ResourceRef
from agent_org_network.durable_credentials import (
    CredentialApprovalEvidence,
    CredentialCommand,
    CredentialConflictError,
    CredentialDeniedError,
    CredentialUnavailableError,
    DurableCredentialCapability,
    DurableCredentialRegistry,
    SqliteCredentialUnitOfWork,
    canonical_credential_command_digest,
    resource_fingerprint,
)


NOW = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)


def _principal() -> AuthenticatedPrincipal:
    return AuthenticatedPrincipal(
        subject_id="admin-1",
        org_id="org-1",
        identity_provider="test",
        identity_session_id="session-1",
    )


def _registry(tmp_path: Path) -> DurableCredentialRegistry:
    return DurableCredentialRegistry(
        SqliteCredentialUnitOfWork(tmp_path / "credentials.sqlite3"),
        secret_factory=lambda: "raw-secret-that-must-never-be-persisted",
        clock=lambda: NOW,
    )


def _issue_command(
    *, credential_id: str = "cred-1", request_id: str = "request-1"
) -> CredentialCommand:
    return CredentialCommand(org_id="org-1", request_id=request_id, attempt=1)


def _issue_evidence(
    command: CredentialCommand, resource: ResourceRef
) -> CredentialApprovalEvidence:
    digest = canonical_credential_command_digest(
        action="worker_credential.issue",
        resource=resource,
        command={
            "owner_subject_id": resource.owner_subject_id,
            "role": "worker",
            "expires_at": None,
        },
    )
    return CredentialApprovalEvidence(
        evidence_id="approval-issue-1",
        action="worker_credential.issue",
        command_digest=digest,
        resource_fingerprint=resource_fingerprint(resource),
    )


def test_issue는_상태_영수증_감사_outbox를_하나의_uow로_기록하고_비밀은_저장하지않는다(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-1"
    )
    result = registry.issue(
        principal=_principal(),
        credential_id="cred-1",
        owner_subject_id="owner-1",
        role="worker",
        expires_at=None,
        command=_issue_command(),
        resource=resource,
        approval=_issue_evidence(_issue_command(), resource),
    )

    assert result.credential.credential_id == "cred-1"
    assert result.credential.generation == 1
    assert result.credential.revision == 1
    assert result.credential.status == "active"
    assert result.raw_secret == "raw-secret-that-must-never-be-persisted"
    assert registry.audit_intents(org_id="org-1") == (
        {
            "action": "worker_credential.issue",
            "credential_id": "cred-1",
            "evidence_id": "approval-issue-1",
            "org_id": "org-1",
            "principal_subject_id": "admin-1",
        },
    )
    assert registry.outbox_intents(org_id="org-1")[0]["kind"] == "worker_credential.issued"
    assert "raw-secret" not in registry.debug_serialized_database()


def test_issue_재시도는_같은_의미일때만_영수증으로_수렴하고_비밀을_재전달하지않는다(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-1"
    )
    command = _issue_command()
    first = registry.issue(
        principal=_principal(),
        credential_id="cred-1",
        owner_subject_id="owner-1",
        role="worker",
        expires_at=None,
        command=command,
        resource=resource,
        approval=_issue_evidence(command, resource),
    )
    second = registry.issue(
        principal=_principal(),
        credential_id="cred-1",
        owner_subject_id="owner-1",
        role="worker",
        expires_at=None,
        command=command,
        resource=resource,
        approval=_issue_evidence(command, resource),
    )
    assert first.raw_secret is not None
    assert second.credential == first.credential
    assert second.raw_secret is None
    assert (
        len(registry.audit_intents(org_id="org-1"))
        == len(registry.outbox_intents(org_id="org-1"))
        == 1
    )

    with pytest.raises(CredentialConflictError):
        changed_resource = ResourceRef(
            org_id="org-1",
            kind="worker_credential",
            resource_id="cred-1",
            owner_subject_id="other-owner",
        )
        registry.issue(
            principal=_principal(),
            credential_id="cred-1",
            owner_subject_id="other-owner",
            role="worker",
            expires_at=None,
            command=command,
            resource=changed_resource,
            approval=_issue_evidence(command, changed_resource),
        )
    assert len(registry.audit_intents(org_id="org-1")) == 1


def test_issue_영수증은_후속_revoke_뒤에도_최초_active_결과만_재현한다(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-1"
    )
    issue_command = _issue_command()
    first = registry.issue(
        principal=_principal(),
        credential_id="cred-1",
        owner_subject_id="owner-1",
        role="worker",
        expires_at=None,
        command=issue_command,
        resource=resource,
        approval=_issue_evidence(issue_command, resource),
    )
    revoke_command = CredentialCommand(org_id="org-1", request_id="request-2", attempt=1)
    revoke_digest = canonical_credential_command_digest(
        action="worker_credential.revoke",
        resource=resource,
        command={"expected_generation": 1, "expected_revision": 1},
    )
    revoked = registry.revoke(
        principal=_principal(),
        credential_id="cred-1",
        expected_generation=1,
        expected_revision=1,
        command=revoke_command,
        resource=resource,
        approval=CredentialApprovalEvidence(
            evidence_id="approval-revoke-1",
            action="worker_credential.revoke",
            command_digest=revoke_digest,
            resource_fingerprint=resource_fingerprint(resource),
        ),
    )
    replayed_issue = registry.issue(
        principal=_principal(),
        credential_id="cred-1",
        owner_subject_id="owner-1",
        role="worker",
        expires_at=None,
        command=issue_command,
        resource=resource,
        approval=_issue_evidence(issue_command, resource),
    )
    replayed_revoke = registry.revoke(
        principal=_principal(),
        credential_id="cred-1",
        expected_generation=1,
        expected_revision=1,
        command=revoke_command,
        resource=resource,
        approval=CredentialApprovalEvidence(
            evidence_id="approval-revoke-1",
            action="worker_credential.revoke",
            command_digest=revoke_digest,
            resource_fingerprint=resource_fingerprint(resource),
        ),
    )

    assert first.raw_secret is not None
    assert replayed_issue.raw_secret is None
    assert replayed_issue.credential == first.credential
    assert revoked.status == replayed_revoke.status == "revoked"
    assert replayed_revoke == revoked
    assert (
        len(registry.audit_intents(org_id="org-1"))
        == len(registry.outbox_intents(org_id="org-1"))
        == 2
    )


def test_revoke는_현재_resource와_revision_generation을_cas하고_즉시_재인가한다(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-1"
    )
    command = _issue_command()
    issued = registry.issue(
        principal=_principal(),
        credential_id="cred-1",
        owner_subject_id="owner-1",
        role="worker",
        expires_at=None,
        command=command,
        resource=resource,
        approval=_issue_evidence(command, resource),
    ).credential
    revoke_command = CredentialCommand(org_id="org-1", request_id="request-2", attempt=1)
    revoke_digest = canonical_credential_command_digest(
        action="worker_credential.revoke",
        resource=resource,
        command={"expected_generation": 1, "expected_revision": 1},
    )
    revoked = registry.revoke(
        principal=_principal(),
        credential_id="cred-1",
        expected_generation=1,
        expected_revision=1,
        command=revoke_command,
        resource=resource,
        approval=CredentialApprovalEvidence(
            evidence_id="approval-revoke-1",
            action="worker_credential.revoke",
            command_digest=revoke_digest,
            resource_fingerprint=resource_fingerprint(resource),
        ),
    )
    assert revoked.status == "revoked"
    assert revoked.revision == issued.revision + 1
    with pytest.raises(CredentialConflictError):
        registry.revoke(
            principal=_principal(),
            credential_id="cred-1",
            expected_generation=1,
            expected_revision=1,
            command=CredentialCommand(org_id="org-1", request_id="request-3", attempt=1),
            resource=resource,
            approval=CredentialApprovalEvidence(
                evidence_id="approval-revoke-2",
                action="worker_credential.revoke",
                command_digest=revoke_digest,
                resource_fingerprint=resource_fingerprint(resource),
            ),
        )


def test_approval_or_resource_불일치는_쓰기_없이_거부하고_org_경계를_넘지않는다(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-1"
    )
    command = _issue_command()
    evidence = _issue_evidence(command, resource)
    with pytest.raises(CredentialDeniedError):
        registry.issue(
            principal=AuthenticatedPrincipal(
                subject_id="admin-1",
                org_id="other-org",
                identity_provider="test",
                identity_session_id="session-1",
            ),
            credential_id="cred-1",
            owner_subject_id="owner-1",
            role="worker",
            expires_at=None,
            command=command,
            resource=resource,
            approval=evidence,
        )
    assert registry.list(org_id="org-1") == ()


def test_audit와_outbox_조회는_명시한_org에만_범위가_한정된다(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    for org_id in ("org-1", "org-2"):
        resource = ResourceRef(
            org_id=org_id,
            kind="worker_credential",
            resource_id=f"cred-{org_id}",
            owner_subject_id=f"owner-{org_id}",
        )
        credential_id = f"cred-{org_id}"
        owner_subject_id = f"owner-{org_id}"
        command = CredentialCommand(org_id=org_id, request_id=f"request-{org_id}", attempt=1)
        digest = canonical_credential_command_digest(
            action="worker_credential.issue",
            resource=resource,
            command={
                "owner_subject_id": resource.owner_subject_id,
                "role": "worker",
                "expires_at": None,
            },
        )
        registry.issue(
            principal=AuthenticatedPrincipal(
                subject_id=f"admin-{org_id}",
                org_id=org_id,
                identity_provider="test",
                identity_session_id=f"session-{org_id}",
            ),
            credential_id=credential_id,
            owner_subject_id=owner_subject_id,
            role="worker",
            expires_at=None,
            command=command,
            resource=resource,
            approval=CredentialApprovalEvidence(
                evidence_id=f"approval-{org_id}",
                action="worker_credential.issue",
                command_digest=digest,
                resource_fingerprint=resource_fingerprint(resource),
            ),
        )

    assert [item["org_id"] for item in registry.audit_intents(org_id="org-1")] == ["org-1"]
    assert [item["org_id"] for item in registry.outbox_intents(org_id="org-2")] == ["org-2"]


def test_revoke는_현재_owner가_바뀌면_승인과_명령이_있어도_쓰기_없이_차단한다(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)
    issued_resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-1"
    )
    issue_command = _issue_command()
    registry.issue(
        principal=_principal(),
        credential_id="cred-1",
        owner_subject_id="owner-1",
        role="worker",
        expires_at=None,
        command=issue_command,
        resource=issued_resource,
        approval=_issue_evidence(issue_command, issued_resource),
    )
    stale_owner_resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-2"
    )
    revoke_command = CredentialCommand(org_id="org-1", request_id="request-2", attempt=1)
    digest = canonical_credential_command_digest(
        action="worker_credential.revoke",
        resource=stale_owner_resource,
        command={"expected_generation": 1, "expected_revision": 1},
    )
    with pytest.raises(CredentialDeniedError):
        registry.revoke(
            principal=_principal(),
            credential_id="cred-1",
            expected_generation=1,
            expected_revision=1,
            command=revoke_command,
            resource=stale_owner_resource,
            approval=CredentialApprovalEvidence(
                evidence_id="approval-stale-owner",
                action="worker_credential.revoke",
                command_digest=digest,
                resource_fingerprint=resource_fingerprint(stale_owner_resource),
            ),
        )
    current = registry.read(org_id="org-1", credential_id="cred-1")
    assert current is not None and current.status == "active"
    assert (
        len(registry.audit_intents(org_id="org-1"))
        == len(registry.outbox_intents(org_id="org-1"))
        == 1
    )


def test_secret_factory_장애는_모든_intent와_상태를_rollback한다(tmp_path: Path) -> None:
    registry = DurableCredentialRegistry(
        SqliteCredentialUnitOfWork(tmp_path / "credentials.sqlite3"),
        secret_factory=lambda: (_ for _ in ()).throw(OSError("unavailable")),
        clock=lambda: NOW,
    )
    resource = ResourceRef(
        org_id="org-1", kind="worker_credential", resource_id="cred-1", owner_subject_id="owner-1"
    )
    command = _issue_command()
    with pytest.raises(CredentialUnavailableError):
        registry.issue(
            principal=_principal(),
            credential_id="cred-1",
            owner_subject_id="owner-1",
            role="worker",
            expires_at=None,
            command=command,
            resource=resource,
            approval=_issue_evidence(command, resource),
        )
    assert registry.list(org_id="org-1") == ()
    assert registry.audit_intents(org_id="org-1") == registry.outbox_intents(org_id="org-1") == ()


def test_mcp_노출_전_capability는_정확한_registry만_한번_claim한다(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    capability = DurableCredentialCapability(registry)
    assert not capability.claim(_registry(tmp_path))
    assert not capability.claim(registry)

    fresh = DurableCredentialCapability(registry)
    assert fresh.claim(registry)
    assert not fresh.claim(registry)
