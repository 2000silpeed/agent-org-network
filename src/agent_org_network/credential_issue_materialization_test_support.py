"""Test-only deterministic seam for R3.2 commit fault and forged-proof tests.

Production code must use ``CredentialIssueMaterializationOperations.commit``.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from agent_org_network.sqlite_durable_credential_issue_commit import (
    CommitFaultInjector,
    CredentialIssueMaterializationVerifier,
    _commit_sqlite_durable_credential_issue_target_test_support,  # pyright: ignore[reportPrivateUsage]
    _verified_materialization_verification,  # pyright: ignore[reportPrivateUsage]
)


def commit_staged_credential_issue_for_test(
    path: str | Path,
    org_id: str,
    target_id: str,
    now: str,
    verifier: CredentialIssueMaterializationVerifier,
    *,
    release: Callable[[str], None] | None = None,
    fault_injector: CommitFaultInjector | None = None,
) -> None:
    _commit_sqlite_durable_credential_issue_target_test_support(
        path, org_id, target_id, now, verifier, release=release, fault_injector=fault_injector
    )


def verified_materialization_proof_for_test(snapshot: object) -> object:
    return _verified_materialization_verification(snapshot)  # type: ignore[arg-type]
