"""Legacy R3 stage test-support wrapper.

Production R5.2 scoped operations must enter the neutral transition core with
their own existing-reservation guard.  This unscoped entry point is retained only
for R3 regression tests and intentionally cannot establish a scope binding.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agent_org_network._credential_issue_transition_core import (
    CredentialIssueTransitionError,
    ExistingReservedStageRequest,
    ExistingReservedStageGuard,
    StageFaultInjector,
    _TRANSITION_ENTRY_SEAL,  # pyright: ignore[reportPrivateUsage]
    _stage_existing_reserved_credential_issue_target,  # pyright: ignore[reportPrivateUsage]
    reserve_unscoped_target_for_legacy,
)
from agent_org_network.credential_delivery import DeliveryStage, StageMissing
from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
)


class SqliteCredentialIssueStagingError(RuntimeError):
    """Legacy test-support target cannot safely be staged."""


class _LegacyDelivery(Protocol):
    def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing: ...

    def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage: ...


@dataclass(frozen=True)
class CredentialIssueStageRequest:
    reservation: DurableCredentialIssueTargetReservation
    stage_key: str
    raw_secret: str
    now: str


class _LegacyExistingTargetGuard(ExistingReservedStageGuard):
    def validate_existing_reserved_stage(
        self, connection: sqlite3.Connection, request: ExistingReservedStageRequest
    ) -> None:
        del connection, request

    def validate_preexternal_stage(
        self, path: str | Path, request: ExistingReservedStageRequest
    ) -> None:
        del path, request


def _core_request(request: CredentialIssueStageRequest) -> ExistingReservedStageRequest:
    return ExistingReservedStageRequest(
        reservation=request.reservation,
        stage_key=request.stage_key,
        raw_secret=request.raw_secret,
        now=request.now,
        guard=_LegacyExistingTargetGuard(),
    )


def stage_sqlite_durable_credential_issue_target(
    path: str | Path,
    request: CredentialIssueStageRequest,
    delivery: _LegacyDelivery,
    *,
    fault_injector: StageFaultInjector | None = None,
) -> DeliveryStage:
    """Run the former unscoped R3 seam through the neutral transition core."""
    core_request = _core_request(request)
    try:
        reserve_unscoped_target_for_legacy(path, core_request)
        return _stage_existing_reserved_credential_issue_target(
            path,
            core_request,
            delivery,
            fault_injector=fault_injector,
            entry_seal=_TRANSITION_ENTRY_SEAL,
        )
    except CredentialIssueTransitionError as error:
        raise SqliteCredentialIssueStagingError(str(error)) from error


__all__ = (
    "CredentialIssueStageRequest",
    "DeliveryStage",
    "SqliteCredentialIssueStagingError",
    "StageMissing",
    "stage_sqlite_durable_credential_issue_target",
)
