"""R5.2 scoped credential issue outer-orchestration contracts (closed pending bridge)."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from collections.abc import Callable
from typing import Final, Protocol, final

from agent_org_network.credential_issue_scoped_operations import (
    AlreadyCommitted,
    ClaimedRecovery,
    CredentialIssueScopedOperations,
    CredentialIssueScopedOperationsCapability,
    NeedsInitialStage,
    ScopedCredentialIssueUnavailable,
    create_credential_issue_scoped_operations,
)
from agent_org_network.sqlite_durable_credential_issue_cleanup import (
    open_sqlite_durable_credential_issue_cleanup_connection,
)


@dataclass(frozen=True)
class ScopedCredentialIssueCommand:
    org_id: str
    target_id: str
    credential_id: str
    agent_card_id: str
    owner_subject_id: str
    role: str
    request_id: str
    attempt: int
    expires_at: str | None
    stage_key: str
    created_at: str


@dataclass(frozen=True)
class Issued:
    credential_id: str
    delivery_ref: str


@dataclass(frozen=True)
class ReleasePending:
    credential_id: str
    delivery_ref: str


@dataclass(frozen=True)
class CleanupRequired:
    target_id: str


@dataclass(frozen=True)
class Denied:
    pass


@dataclass(frozen=True)
class Unavailable:
    pass


@dataclass(frozen=True)
class Conflict:
    pass


ScopedCredentialIssueResult = (
    Issued | ReleasePending | CleanupRequired | Denied | Unavailable | Conflict
)


class _ReleaseDelivery(Protocol):
    def release(self, delivery_ref: str) -> None: ...


_OPERATIONS_SEAL: Final = object()
_BRIDGE_FACTORY_SEAL: Final = object()
_BRIDGE_SEAL: Final = object()
_READINESS_FACTORY_SEAL: Final = object()


@final
class ScopedCredentialIssueOperations:
    """No issue path opens until the scoped bridge supplies all three transitions."""

    def __init__(self, dependencies: tuple[object, ...], seal: object) -> None:
        if seal is not _OPERATIONS_SEAL:
            raise TypeError("scoped issue operations는 factory로만 조립합니다.")
        self._dependencies = dependencies

    def issue(self, command: ScopedCredentialIssueCommand) -> ScopedCredentialIssueResult:
        bridge = self._dependencies[0] if self._dependencies else None
        if type(bridge) is not _PathBoundScopedIssueBridge:
            return Unavailable()
        return bridge.issue(command)


@final
class _PathBoundScopedIssueBridge:
    def __init__(
        self,
        path: Path,
        scoped_operations: CredentialIssueScopedOperations,
        cleanup_readiness: "CredentialIssueCleanupReadinessCapability",
        delivery: _ReleaseDelivery,
        secret_factory: Callable[[], str],
        seal: object,
    ) -> None:
        if seal is not _BRIDGE_SEAL:
            raise TypeError("path-bound scoped bridge는 factory로만 조립합니다.")
        self._path = path
        self._scoped_operations = scoped_operations
        self._cleanup_readiness = cleanup_readiness
        self._delivery = delivery
        self._secret_factory = secret_factory

    def issue(self, command: ScopedCredentialIssueCommand) -> ScopedCredentialIssueResult:
        try:
            reservation = self._scoped_operations.reserve(self._path, command)
            readiness = self._scoped_operations.stage_readiness(
                self._path, command.org_id, command.target_id
            )
            if isinstance(readiness, ClaimedRecovery):
                return Unavailable()
            if isinstance(readiness, NeedsInitialStage):
                raw_secret = self._secret_factory()
                if type(raw_secret) is not str or not raw_secret:
                    return Unavailable()
                self._scoped_operations.stage(
                    self._path, reservation, command.stage_key, raw_secret, command.created_at
                )
            if not isinstance(readiness, AlreadyCommitted):
                try:
                    committed = self._scoped_operations.materialize(
                        self._path, command.org_id, command.target_id, command.created_at
                    )
                except ScopedCredentialIssueUnavailable:
                    if self._cleanup_readiness._is_cleanup_pending(  # pyright: ignore[reportPrivateUsage]
                        command.org_id, command.target_id
                    ):
                        return CleanupRequired(command.target_id)
                    raise
            else:
                committed = self._scoped_operations.read_committed(
                    self._path, command.org_id, command.target_id
                )
            try:
                current = self._scoped_operations.read_committed(
                    self._path, command.org_id, command.target_id
                )
            except ScopedCredentialIssueUnavailable:
                # The read immediately before external release is also the
                # projection/current-proof gate. A missing or drifted committed
                # view must not disclose a persisted ref as release-pending.
                return Unavailable()
            if current.delivery_ref != committed.delivery_ref:
                return ReleasePending(committed.credential_id, committed.delivery_ref)
            try:
                self._delivery.release(current.delivery_ref)
            except Exception:
                return ReleasePending(current.credential_id, current.delivery_ref)
            return Issued(current.credential_id, current.delivery_ref)
        except ScopedCredentialIssueUnavailable:
            return Unavailable()
        except Exception:
            return Conflict()


@final
class ScopedCredentialIssueBridgeCapability:
    def __init__(self, bridge: _PathBoundScopedIssueBridge, seal: object) -> None:
        if seal is not _BRIDGE_FACTORY_SEAL:
            raise TypeError("scoped bridge capability는 factory로만 조립합니다.")
        self._bridge = bridge
        self._claimed = False
        self._claim_lock = RLock()

    def _claim(self) -> _PathBoundScopedIssueBridge:
        with self._claim_lock:
            if self._claimed:
                raise ValueError("scoped bridge capability를 claim할 수 없습니다.")
            self._claimed = True
            return self._bridge


@final
class CredentialIssueCleanupReadinessCapability:
    """Validate-only R4 schema/lifecycle proof."""

    def __init__(self, path: Path, seal: object) -> None:
        if seal is not _READINESS_FACTORY_SEAL:
            raise TypeError("cleanup readiness는 factory로만 조립합니다.")
        self._path = path
        self._claimed = False

    def _claim(self, path: Path) -> "CredentialIssueCleanupReadinessCapability":
        if self._claimed:
            raise ValueError("cleanup readiness capability를 claim할 수 없습니다.")
        if self._path != path:
            raise ValueError("cleanup readiness path가 bridge path와 다릅니다.")
        self._claimed = True
        return self

    def _is_cleanup_pending(self, org_id: str, target_id: str) -> bool:
        connection: sqlite3.Connection | None = None
        try:
            connection = open_sqlite_durable_credential_issue_cleanup_connection(self._path)
            target = connection.execute(
                "SELECT state FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            fence = connection.execute(
                "SELECT state FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            return (
                target is not None
                and fence is not None
                and target["state"] == fence["state"] == "CleanupPending"
            )
        except Exception:
            return False
        finally:
            if connection is not None:
                connection.close()


def create_credential_issue_cleanup_readiness_capability(
    path: object,
) -> CredentialIssueCleanupReadinessCapability:
    if not isinstance(path, Path):
        raise TypeError("path-bound cleanup readiness가 필요합니다.")
    connection = open_sqlite_durable_credential_issue_cleanup_connection(path)
    connection.close()
    return CredentialIssueCleanupReadinessCapability(path, _READINESS_FACTORY_SEAL)


def create_scoped_credential_issue_bridge_capability(
    *,
    path: object,
    scoped_capability: CredentialIssueScopedOperationsCapability,
    delivery: _ReleaseDelivery,
    secret_factory: object,
    cleanup_readiness: CredentialIssueCleanupReadinessCapability,
) -> ScopedCredentialIssueBridgeCapability:
    if (
        not isinstance(path, Path)
        or type(scoped_capability) is not CredentialIssueScopedOperationsCapability
        or not callable(getattr(delivery, "release", None))
        or not callable(secret_factory)
        or type(cleanup_readiness) is not CredentialIssueCleanupReadinessCapability
    ):
        raise TypeError("path-bound scoped bridge의 전체 composition이 필요합니다.")
    readiness = cleanup_readiness._claim(path)  # pyright: ignore[reportPrivateUsage]
    operations = create_credential_issue_scoped_operations(scoped_capability)
    return ScopedCredentialIssueBridgeCapability(
        _PathBoundScopedIssueBridge(
            path,
            operations,
            readiness,
            delivery,
            secret_factory,
            _BRIDGE_SEAL,  # type: ignore[arg-type]
        ),
        _BRIDGE_FACTORY_SEAL,
    )


def create_path_bound_scoped_credential_issue_operations(
    capability: ScopedCredentialIssueBridgeCapability,
) -> ScopedCredentialIssueOperations:
    if type(capability) is not ScopedCredentialIssueBridgeCapability:
        raise TypeError("exact path-bound scoped bridge capability가 필요합니다.")
    return ScopedCredentialIssueOperations((capability._claim(),), _OPERATIONS_SEAL)  # pyright: ignore[reportPrivateUsage]
