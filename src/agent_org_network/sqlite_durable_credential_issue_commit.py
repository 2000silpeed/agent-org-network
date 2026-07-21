"""Legacy R3.2 materialization compatibility and test-support facade.

The transaction/readback state machine belongs to the neutral transition core.
This module preserves R3.2 imports while scoped production operations are moved.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import agent_org_network._credential_issue_transition_core as _transition_core

from agent_org_network._credential_issue_transition_core import (
    CommitFaultInjector,
    CredentialIssueMaterializationSnapshot,
    CredentialIssueMaterializationVerifier,
    MaterializationVerification,
    SQLITE_DURABLE_CREDENTIAL_ISSUE_COMMIT_FAULT_POINTS,
    SqliteCredentialIssueCommitError,
)

_PRODUCTION_COMMIT_SEAL = _transition_core._PRODUCTION_COMMIT_SEAL  # pyright: ignore[reportPrivateUsage]
_verified_materialization_verification = _transition_core._verified_materialization_verification  # pyright: ignore[reportPrivateUsage]


def _commit_sqlite_durable_credential_issue_target_test_support(  # pyright: ignore[reportUnusedFunction]
    path: str | Path,
    org_id: str,
    target_id: str,
    now: str,
    verifier: CredentialIssueMaterializationVerifier,
    *,
    release: Callable[[str], None] | None = None,
    fault_injector: CommitFaultInjector | None = None,
) -> None:
    result = _transition_core._materialize_sqlite_durable_credential_issue_target(  # pyright: ignore[reportPrivateUsage]
        path, org_id, target_id, now, verifier, fault_injector=fault_injector
    )
    if release is not None:
        try:
            release(result.delivery_ref)
        except Exception as error:
            raise SqliteCredentialIssueCommitError(
                "persisted delivery release가 unavailable입니다."
            ) from error


def _commit_sqlite_durable_credential_issue_target_with_production_verifier(  # pyright: ignore[reportUnusedFunction]
    path: str | Path,
    org_id: str,
    target_id: str,
    now: str,
    verifier: CredentialIssueMaterializationVerifier,
    *,
    production_seal: object,
    release: Callable[[str], None] | None = None,
) -> None:
    result = (
        _transition_core._commit_sqlite_durable_credential_issue_target_with_production_verifier(  # pyright: ignore[reportPrivateUsage]
            path, org_id, target_id, now, verifier, production_seal=production_seal
        )
    )
    if release is not None:
        try:
            release(result.delivery_ref)
        except Exception as error:
            raise SqliteCredentialIssueCommitError(
                "persisted delivery release가 unavailable입니다."
            ) from error


__all__ = (
    "CommitFaultInjector",
    "CredentialIssueMaterializationSnapshot",
    "CredentialIssueMaterializationVerifier",
    "MaterializationVerification",
    "SQLITE_DURABLE_CREDENTIAL_ISSUE_COMMIT_FAULT_POINTS",
    "SqliteCredentialIssueCommitError",
)
