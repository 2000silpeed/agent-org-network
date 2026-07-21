"""Neutral R3 Credential Issue Target stage-transition core.

This namespace deliberately has no scope, authority, MCP, release, or abort
dependency.  A caller supplies the guard that proves an existing reservation is
allowed to acquire its stage fence; the core owns every fence state transition.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Final, Protocol, cast

from agent_org_network.credential_delivery import DeliveryStage, StageMissing
from agent_org_network.sqlite_durable_credential_issue_targets import (
    DurableCredentialIssueTargetReservation,
    SqliteDurableCredentialIssueTargetsSchemaError,
    open_sqlite_durable_credential_issue_targets_connection,
    validate_durable_credential_issue_target_reservation,
    validate_sqlite_durable_credential_issue_targets_connection,
)


class CredentialIssueTransitionError(RuntimeError):
    """An existing Credential Issue Target cannot safely enter Staged."""


_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z")
_LOCKS_GUARD = threading.Lock()
_TARGET_LOCKS: dict[tuple[str, str, str], threading.RLock] = {}


class _Delivery(Protocol):
    def recover_stage(self, stage_key: str) -> DeliveryStage | StageMissing: ...

    def stage_once(self, raw_secret: str, stage_key: str) -> DeliveryStage: ...


class ExistingReservedStageGuard(Protocol):
    """Caller-owned proof checked inside the claim transaction before mutation."""

    def validate_existing_reserved_stage(
        self, connection: sqlite3.Connection, request: ExistingReservedStageRequest
    ) -> None: ...

    def validate_preexternal_stage(
        self, path: str | Path, request: ExistingReservedStageRequest
    ) -> None: ...


type StageFaultInjector = Callable[[str], None]
_TRANSITION_ENTRY_SEAL = object()


@dataclass(frozen=True)
class ExistingReservedStageRequest:
    """Stage input for a target that was reserved before entering this core."""

    reservation: DurableCredentialIssueTargetReservation
    stage_key: str
    raw_secret: str
    now: str
    guard: ExistingReservedStageGuard


@dataclass(frozen=True)
class CommittedCredentialDelivery:
    """Secret-free core result; external delivery is intentionally outside the core."""

    org_id: str
    target_id: str
    credential_id: str
    delivery_ref: str


def _target_lock(path: str | Path, request: ExistingReservedStageRequest) -> threading.RLock:
    key = (
        str(Path(path).expanduser().resolve()),
        request.reservation.org_id,
        request.reservation.target_id,
    )
    with _LOCKS_GUARD:
        return _TARGET_LOCKS.setdefault(key, threading.RLock())


def _secret_hash(raw_secret: str) -> str:
    if type(raw_secret) is not str or not raw_secret:
        raise CredentialIssueTransitionError("raw secret이 canonical하지 않습니다.")
    return hashlib.sha256(raw_secret.encode()).hexdigest()


def _validate_request(request: ExistingReservedStageRequest) -> str:
    try:
        validate_durable_credential_issue_target_reservation(request.reservation)
    except SqliteDurableCredentialIssueTargetsSchemaError as error:
        raise CredentialIssueTransitionError(
            "target reservation이 canonical하지 않습니다."
        ) from error
    if (
        _SHA256.fullmatch(request.stage_key) is None
        or _TIMESTAMP.fullmatch(request.now) is None
        or not callable(getattr(request.guard, "validate_existing_reserved_stage", None))
    ):
        raise CredentialIssueTransitionError("stage request 또는 guard가 canonical하지 않습니다.")
    return _secret_hash(request.raw_secret)


def _same_target(row: sqlite3.Row, request: ExistingReservedStageRequest) -> bool:
    return row["target_json"] == request.reservation.target_json()


def reserve_unscoped_target_for_legacy(
    path: str | Path, request: ExistingReservedStageRequest
) -> None:
    """Legacy test-support preparation; production scope proof is intentionally absent."""
    _validate_request(request)
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        target = connection.execute(
            "SELECT * FROM durable_credential_issue_targets_v1 WHERE org_id=? AND command_digest=?",
            (request.reservation.org_id, request.reservation.command_digest),
        ).fetchone()
        if target is None:
            if (
                connection.execute(
                    "SELECT 1 FROM durable_credentials WHERE org_id=? AND credential_id=?",
                    (request.reservation.org_id, request.reservation.credential_id),
                ).fetchone()
                is not None
            ):
                raise CredentialIssueTransitionError("actual durable credential과 충돌합니다.")
            if request.now != request.reservation.created_at:
                raise CredentialIssueTransitionError("initial stage timestamp가 target과 다릅니다.")
            connection.execute(
                "INSERT INTO durable_credential_issue_targets_v1 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                request.reservation.row(),
            )
        elif not _same_target(target, request):
            raise CredentialIssueTransitionError("same semantic target이 다릅니다.")
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _claim(
    path: str | Path, request: ExistingReservedStageRequest, secret_hash: str
) -> tuple[bool, bool, str | None, DeliveryStage | None]:
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        target = connection.execute(
            "SELECT * FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
            (request.reservation.org_id, request.reservation.target_id),
        ).fetchone()
        if target is None or not _same_target(target, request):
            raise CredentialIssueTransitionError("existing target이 exact하지 않습니다.")
        request.guard.validate_existing_reserved_stage(connection, request)
        fence = connection.execute(
            "SELECT * FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
            (request.reservation.org_id, request.reservation.target_id),
        ).fetchone()
        if fence is None:
            if target["state"] != "Reserved":
                raise CredentialIssueTransitionError("reserved target만 fence를 초기화합니다.")
            connection.execute(
                "INSERT INTO credential_issue_stage_fences_v2 VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request.reservation.org_id,
                    request.reservation.target_id,
                    request.stage_key,
                    secret_hash,
                    None,
                    0,
                    None,
                    "PendingStage",
                    request.now,
                    request.now,
                ),
            )
            fence = connection.execute(
                "SELECT * FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
                (request.reservation.org_id, request.reservation.target_id),
            ).fetchone()
        if (
            fence is None
            or fence["stage_key"] != request.stage_key
            or fence["secret_hash"] != secret_hash
        ):
            raise CredentialIssueTransitionError("stage semantic 또는 secret hash가 다릅니다.")
        if request.now < fence["updated_at"]:
            raise CredentialIssueTransitionError("stage retry timestamp가 역행합니다.")
        if fence["state"] == "Staged":
            if not isinstance(fence["delivery_ref"], str):
                raise CredentialIssueTransitionError("staged delivery ref가 없습니다.")
            connection.commit()
            return False, False, None, DeliveryStage(request.stage_key, fence["delivery_ref"])
        if fence["state"] not in {"PendingStage", "ClaimedStage"}:
            raise CredentialIssueTransitionError("stage fence state가 claim 불가입니다.")
        token = secrets.token_urlsafe(32)
        generation = fence["claim_generation"] + 1
        changed = connection.execute(
            "UPDATE credential_issue_stage_fences_v2 SET state='ClaimedStage',claim_generation=?,claim_token_hash=?,updated_at=? WHERE org_id=? AND target_id=? AND state=? AND claim_generation=?",
            (
                generation,
                hashlib.sha256(token.encode()).hexdigest(),
                request.now,
                request.reservation.org_id,
                request.reservation.target_id,
                fence["state"],
                fence["claim_generation"],
            ),
        ).rowcount
        if changed != 1:
            connection.commit()
            return False, False, None, None
        if fence["state"] == "PendingStage":
            target_changed = connection.execute(
                "UPDATE durable_credential_issue_targets_v1 SET state='StageClaimed',updated_at=? WHERE org_id=? AND target_id=? AND state='Reserved'",
                (request.now, request.reservation.org_id, request.reservation.target_id),
            )
            if target_changed.rowcount != 1:
                raise CredentialIssueTransitionError("target claim CAS가 충돌합니다.")
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        connection.commit()
        return fence["state"] == "PendingStage", True, token, None
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _persist_stage(
    path: str | Path,
    request: ExistingReservedStageRequest,
    stage: DeliveryStage,
    secret_hash: str,
    claim_token: str,
    fault_injector: StageFaultInjector | None,
) -> DeliveryStage:
    if type(stage) is not DeliveryStage or stage.stage_key != request.stage_key:
        raise CredentialIssueTransitionError("delivery stage key가 다릅니다.")
    connection = open_sqlite_durable_credential_issue_targets_connection(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        if fault_injector is not None:
            fault_injector("before_stage_cas")
        changed = connection.execute(
            "UPDATE credential_issue_stage_fences_v2 SET state='Staged',delivery_ref=?,updated_at=? WHERE org_id=? AND target_id=? AND state='ClaimedStage' AND stage_key=? AND secret_hash=? AND claim_token_hash=?",
            (
                stage.delivery_ref,
                request.now,
                request.reservation.org_id,
                request.reservation.target_id,
                request.stage_key,
                secret_hash,
                hashlib.sha256(claim_token.encode()).hexdigest(),
            ),
        ).rowcount
        if changed == 1:
            target_changed = connection.execute(
                "UPDATE durable_credential_issue_targets_v1 SET state='Staged',updated_at=? WHERE org_id=? AND target_id=? AND state='StageClaimed'",
                (request.now, request.reservation.org_id, request.reservation.target_id),
            )
            if target_changed.rowcount != 1:
                raise CredentialIssueTransitionError("target stage CAS가 충돌합니다.")
        row = connection.execute(
            "SELECT delivery_ref,state FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
            (request.reservation.org_id, request.reservation.target_id),
        ).fetchone()
        if row is None or row["state"] != "Staged" or row["delivery_ref"] != stage.delivery_ref:
            raise CredentialIssueTransitionError("stage persist CAS가 충돌합니다.")
        validate_sqlite_durable_credential_issue_targets_connection(connection)
        if fault_injector is not None:
            fault_injector("after_stage_cas")
        connection.commit()
        return stage
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def _stage_existing_reserved_credential_issue_target(  # pyright: ignore[reportUnusedFunction]
    path: str | Path,
    request: ExistingReservedStageRequest,
    delivery: _Delivery,
    *,
    fault_injector: StageFaultInjector | None = None,
    entry_seal: object,
) -> DeliveryStage:
    """Claim/recover/stage one pre-reserved target under its exact caller proof."""
    if entry_seal is not _TRANSITION_ENTRY_SEAL:
        raise CredentialIssueTransitionError("sealed transition entry가 필요합니다.")
    with _target_lock(path, request):
        secret_hash = _validate_request(request)
        winner, owned, claim_token, persisted = _claim(path, request, secret_hash)
        if persisted is not None:
            return persisted
        if not owned or claim_token is None:
            raise CredentialIssueTransitionError("claim ownership을 얻지 못했습니다.")
        if fault_injector is not None:
            fault_injector("before_preexternal_guard")
        try:
            request.guard.validate_preexternal_stage(path, request)
        except CredentialIssueTransitionError:
            raise
        except Exception as error:
            raise CredentialIssueTransitionError(
                "pre-external stage guard가 unavailable입니다."
            ) from error
        try:
            recovered = delivery.recover_stage(request.stage_key)
        except Exception as error:
            raise CredentialIssueTransitionError(
                "delivery recovery가 unavailable입니다."
            ) from error
        if type(recovered) is DeliveryStage:
            return _persist_stage(
                path, request, recovered, secret_hash, claim_token, fault_injector
            )
        if type(recovered) is not StageMissing:
            raise CredentialIssueTransitionError("delivery recovery 결과가 canonical하지 않습니다.")
        if not winner:
            raise CredentialIssueTransitionError("claimed stage recovery가 missing입니다.")
        try:
            staged = delivery.stage_once(request.raw_secret, request.stage_key)
        except Exception as error:
            raise CredentialIssueTransitionError("delivery stage가 unavailable입니다.") from error
        return _persist_stage(path, request, staged, secret_hash, claim_token, fault_injector)


class SqliteCredentialIssueCommitError(RuntimeError):
    """A Staged target cannot be atomically materialized."""


SQLITE_DURABLE_CREDENTIAL_ISSUE_COMMIT_FAULT_POINTS: Final = (
    "after_target_committing",
    "after_fence_committing",
    "after_target_committed",
    "after_credential_insert",
    "after_companion_persist",
    "after_receipt_insert",
    "after_audit_insert",
    "after_outbox_insert",
    "after_fence_committed",
    "after_companion_readback",
)
type CommitFaultInjector = Callable[[str], None]
type MaterializationPrewriteFaultInjector = Callable[[sqlite3.Connection], None]
_VERIFICATION_SEAL = object()
_PRODUCTION_COMMIT_SEAL = object()


@dataclass(frozen=True)
class CredentialIssueMaterializationSnapshot:
    """Secret-free immutable input to current authorization/evidence checks."""

    target_json: str
    target_id: str
    target_generation: int
    stage_key: str
    secret_hash: str
    delivery_ref: str
    claim_generation: int
    snapshot_digest: str


@dataclass(frozen=True)
class MaterializationVerification:
    """Opaque proof minted only by an internal sealed verifier implementation."""

    snapshot_digest: str
    seal: object


class CredentialIssueMaterializationVerifier(Protocol):
    def prepare(
        self, snapshot: CredentialIssueMaterializationSnapshot
    ) -> MaterializationVerification: ...

    def verify_prewrite(
        self,
        proof: MaterializationVerification,
        snapshot: CredentialIssueMaterializationSnapshot,
    ) -> bool: ...


class CredentialIssueMaterializationGuard(Protocol):
    """Connection-bound permit; production implementations own current scope proof."""

    def prepare(
        self, connection: sqlite3.Connection, snapshot: CredentialIssueMaterializationSnapshot
    ) -> str | None: ...

    def verify_prewrite(
        self,
        connection: sqlite3.Connection,
        snapshot: CredentialIssueMaterializationSnapshot,
        permit_digest: str,
    ) -> bool: ...


class CredentialIssueMaterializationCompanion(Protocol):
    """Neutral transaction companion; production adapters supply durable writes."""

    def persist(self, connection: sqlite3.Connection, target: sqlite3.Row, now: str) -> None: ...

    def verify(self, connection: sqlite3.Connection, target: sqlite3.Row, now: str) -> bool: ...


class _LegacyNoopMaterializationCompanion:
    def persist(self, connection: sqlite3.Connection, target: sqlite3.Row, now: str) -> None:
        del connection, target, now

    def verify(self, connection: sqlite3.Connection, target: sqlite3.Row, now: str) -> bool:
        del connection, target, now
        return True


class _LegacyMaterializationGuard:
    def __init__(self, verifier: CredentialIssueMaterializationVerifier) -> None:
        self._verifier = verifier
        self._proofs: dict[str, MaterializationVerification] = {}

    def prepare(
        self, connection: sqlite3.Connection, snapshot: CredentialIssueMaterializationSnapshot
    ) -> str | None:
        del connection
        proof = _prepare_proof(self._verifier, snapshot)
        if proof is None:
            return None
        self._proofs[snapshot.snapshot_digest] = proof
        return snapshot.snapshot_digest

    def verify_prewrite(
        self,
        connection: sqlite3.Connection,
        snapshot: CredentialIssueMaterializationSnapshot,
        permit_digest: str,
    ) -> bool:
        del connection
        proof = self._proofs.pop(permit_digest, None)
        return (
            proof is not None
            and permit_digest == snapshot.snapshot_digest
            and _verify_proof(self._verifier, proof, snapshot)
        )


def _verified_materialization_verification(  # pyright: ignore[reportUnusedFunction]
    snapshot: CredentialIssueMaterializationSnapshot,
) -> MaterializationVerification:
    """Internal minting primitive; it is deliberately not a production API."""
    return MaterializationVerification(snapshot.snapshot_digest, _VERIFICATION_SEAL)


def _snapshot(target: sqlite3.Row, fence: sqlite3.Row) -> CredentialIssueMaterializationSnapshot:
    values = {
        "target_json": target["target_json"],
        "target_id": target["target_id"],
        "target_generation": target["target_generation"],
        "stage_key": fence["stage_key"],
        "secret_hash": fence["secret_hash"],
        "delivery_ref": fence["delivery_ref"],
        "claim_generation": fence["claim_generation"],
    }
    raw = json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return CredentialIssueMaterializationSnapshot(
        **values, snapshot_digest=sha256(raw.encode()).hexdigest()
    )


def _prepare_proof(
    verifier: object, snapshot: CredentialIssueMaterializationSnapshot
) -> MaterializationVerification | None:
    prepare = getattr(verifier, "prepare", None)
    verify = getattr(verifier, "verify_prewrite", None)
    if not callable(prepare) or not callable(verify):
        return None
    try:
        proof = prepare(snapshot)
        if (
            type(proof) is not MaterializationVerification
            or proof.seal is not _VERIFICATION_SEAL
            or proof.snapshot_digest != snapshot.snapshot_digest
        ):
            return None
        return proof
    except Exception:
        return None


def _verify_proof(
    verifier: object,
    proof: MaterializationVerification,
    snapshot: CredentialIssueMaterializationSnapshot,
) -> bool:
    verify = getattr(verifier, "verify_prewrite", None)
    try:
        return callable(verify) and verify(proof, snapshot) is True
    except Exception:
        return False


def _fault(injector: CommitFaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _cleanup_pending(connection: sqlite3.Connection, org_id: str, target_id: str, now: str) -> bool:
    """Persist only an R4 cleanup obligation; never call an external abort."""
    try:
        target = connection.execute(
            "SELECT state FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
            (org_id, target_id),
        ).fetchone()
        fence = connection.execute(
            "SELECT state FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
            (org_id, target_id),
        ).fetchone()
        if target is None or fence is None:
            return False
        if target[0] not in {"Staged", "Committing"} or fence[0] not in {"Staged", "Committing"}:
            return False
        if (
            connection.execute(
                "UPDATE durable_credential_issue_targets_v1 SET state='CleanupPending',updated_at=? WHERE org_id=? AND target_id=?",
                (now, org_id, target_id),
            ).rowcount
            != 1
        ):
            return False
        if (
            connection.execute(
                "UPDATE credential_issue_stage_fences_v2 SET state='CleanupPending',updated_at=? WHERE org_id=? AND target_id=?",
                (now, org_id, target_id),
            ).rowcount
            != 1
        ):
            return False
        return True
    except sqlite3.Error:
        return False


def _canonical_committed_delivery_ref(
    connection: sqlite3.Connection, target: sqlite3.Row, fence: sqlite3.Row
) -> str:
    """Read back the complete aggregate, without repairing a corrupt replay."""
    result = json.dumps(
        {"credential_id": target["credential_id"], "revision": 1}, separators=(",", ":")
    )
    credential = connection.execute(
        "SELECT owner_subject_id,role,generation,revision,status,secret_hash,issued_at,expires_at,revoked_at FROM durable_credentials WHERE org_id=? AND credential_id=?",
        (target["org_id"], target["credential_id"]),
    ).fetchone()
    receipt = connection.execute(
        "SELECT request_id,attempt,command_digest,credential_id,result_revision,result_json,delivery_ref FROM credential_command_receipts WHERE org_id=? AND request_id=? AND attempt=?",
        (target["org_id"], target["target_id"], target["target_generation"]),
    ).fetchone()
    receipt_count = connection.execute(
        "SELECT count(*) FROM credential_command_receipts WHERE org_id=? AND credential_id=?",
        (target["org_id"], target["credential_id"]),
    ).fetchone()
    audits = connection.execute(
        "SELECT org_id,action,credential_id,principal_subject_id,evidence_id,detail_json FROM credential_audit_intents WHERE org_id=? AND credential_id=?",
        (target["org_id"], target["credential_id"]),
    ).fetchall()
    outbox = connection.execute(
        "SELECT org_id,kind,credential_id,payload_json FROM credential_outbox_intents WHERE org_id=? AND credential_id=?",
        (target["org_id"], target["credential_id"]),
    ).fetchall()
    if (
        target["state"] != "Committed"
        or fence["state"] != "Committed"
        or not isinstance(fence["delivery_ref"], str)
        or credential is None
        or tuple(credential)
        != (
            target["owner_subject_id"],
            target["role"],
            target["target_generation"],
            1,
            "active",
            fence["secret_hash"],
            target["updated_at"],
            target["expires_at"],
            None,
        )
        or receipt is None
        or receipt_count is None
        or receipt_count[0] != 1
        or tuple(receipt)
        != (
            target["target_id"],
            target["target_generation"],
            target["command_digest"],
            target["credential_id"],
            1,
            result,
            fence["delivery_ref"],
        )
        or len(audits) != 1
        or tuple(audits[0])
        != (
            target["org_id"],
            "worker_credential.issue",
            target["credential_id"],
            target["principal_id"],
            target["approval_evidence_id"],
            "{}",
        )
        or len(outbox) != 1
        or tuple(outbox[0])
        != (target["org_id"], "credential_issued", target["credential_id"], "{}")
    ):
        raise SqliteCredentialIssueCommitError(
            "committed aggregate readback이 canonical하지 않습니다."
        )
    return fence["delivery_ref"]


def _cleanup_drift_path(path: str | Path, org_id: str, target_id: str, now: str) -> bool:
    """Use no schema repair path when normal validate-only open has failed."""
    try:
        uri = f"{Path(path).expanduser().resolve(strict=False).as_uri()}?mode=rw"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        cleaned = _cleanup_pending(connection, org_id, target_id, now)
        connection.commit()
        return cleaned
    except sqlite3.Error:
        if connection.in_transaction:
            connection.rollback()
        return False
    finally:
        connection.close()


def _verify_current_snapshot(
    connection: sqlite3.Connection,
    guard: CredentialIssueMaterializationGuard,
    target: sqlite3.Row,
    fence: sqlite3.Row,
    *,
    required_state: str,
    prewrite_fault_injector: MaterializationPrewriteFaultInjector | None = None,
) -> bool:
    """Prepare, reread exact immutable material, then verify at the write edge."""
    try:
        snapshot = _snapshot(target, fence)
    except Exception:
        return False
    permit_digest = guard.prepare(connection, snapshot)
    if permit_digest != snapshot.snapshot_digest:
        return False
    current_target = connection.execute(
        "SELECT * FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
        (target["org_id"], target["target_id"]),
    ).fetchone()
    current_fence = connection.execute(
        "SELECT * FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
        (target["org_id"], target["target_id"]),
    ).fetchone()
    if (
        current_target is None
        or current_fence is None
        or current_target["state"] != required_state
        or current_fence["state"] != required_state
    ):
        return False
    try:
        if prewrite_fault_injector is not None:
            prewrite_fault_injector(connection)
        return _snapshot(current_target, current_fence) == snapshot and guard.verify_prewrite(
            connection, snapshot, cast(str, permit_digest)
        )
    except Exception:
        return False


def _materialize_sqlite_durable_credential_issue_target(
    path: str | Path,
    org_id: str,
    target_id: str,
    now: str,
    verifier: CredentialIssueMaterializationVerifier | CredentialIssueMaterializationGuard,
    fault_injector: CommitFaultInjector | None = None,
    guard: CredentialIssueMaterializationGuard | None = None,
    companion: CredentialIssueMaterializationCompanion | None = None,
    prewrite_fault_injector: MaterializationPrewriteFaultInjector | None = None,
) -> CommittedCredentialDelivery:
    """Private deterministic seam for one exact Staged target commit.

    Production callers must use ``CredentialIssueMaterializationOperations``.
    """
    active_guard: CredentialIssueMaterializationGuard = (
        guard if guard is not None else _LegacyMaterializationGuard(verifier)  # type: ignore[arg-type]
    )
    active_companion: CredentialIssueMaterializationCompanion = (
        companion if companion is not None else _LegacyNoopMaterializationCompanion()
    )
    try:
        connection = open_sqlite_durable_credential_issue_targets_connection(path)
    except Exception as error:
        if _cleanup_drift_path(path, org_id, target_id, now):
            raise SqliteCredentialIssueCommitError(
                "post-stage snapshot drift가 cleanup을 필요로 합니다."
            ) from error
        raise SqliteCredentialIssueCommitError("v2 staged schema가 unavailable입니다.") from error

    delivery_ref: str | None = None
    try:
        connection.execute("BEGIN IMMEDIATE")
        try:
            validate_sqlite_durable_credential_issue_targets_connection(connection)
        except Exception as error:
            # A stage already exists, so retain its ref for the dedicated cleanup
            # workflow instead of materializing from drifted evidence/snapshot.
            cleaned = _cleanup_pending(connection, org_id, target_id, now)
            connection.commit()
            if cleaned:
                raise SqliteCredentialIssueCommitError(
                    "post-stage snapshot drift가 cleanup을 필요로 합니다."
                ) from error
            raise SqliteCredentialIssueCommitError(
                "v2 staged schema가 unavailable입니다."
            ) from error

        target = connection.execute(
            "SELECT * FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
            (org_id, target_id),
        ).fetchone()
        fence = connection.execute(
            "SELECT * FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
            (org_id, target_id),
        ).fetchone()
        if target is None or fence is None:
            raise SqliteCredentialIssueCommitError("exact Staged target과 fence가 필요합니다.")
        if target["state"] == "Committed" and fence["state"] == "Committed":
            if not _verify_current_snapshot(
                connection,
                active_guard,
                target,
                fence,
                required_state="Committed",
                prewrite_fault_injector=prewrite_fault_injector,
            ):
                raise SqliteCredentialIssueCommitError(
                    "current materialization verification이 필요합니다."
                )
            companion_verify = getattr(active_companion, "verify", None)
            if callable(companion_verify) and companion_verify(connection, target, now) is not True:
                raise SqliteCredentialIssueCommitError("materialization companion readback이 canonical하지 않습니다.")
            delivery_ref = _canonical_committed_delivery_ref(connection, target, fence)
            connection.commit()
        elif target["state"] != "Staged" or fence["state"] != "Staged":
            _cleanup_pending(connection, org_id, target_id, now)
            connection.commit()
            raise SqliteCredentialIssueCommitError(
                "post-stage lifecycle drift가 cleanup을 필요로 합니다."
            )
        else:
            if not _verify_current_snapshot(
                connection,
                active_guard,
                target,
                fence,
                required_state="Staged",
                prewrite_fault_injector=prewrite_fault_injector,
            ):
                _cleanup_pending(connection, org_id, target_id, now)
                connection.commit()
                raise SqliteCredentialIssueCommitError(
                    "current materialization verification이 cleanup을 필요로 합니다."
                )
            # The schema's active-target trigger forbids a credential while the
            # target is active.  Moving target to Committed immediately before
            # INSERT is safe because no observer can see this uncommitted state.
            if (
                connection.execute(
                    "UPDATE durable_credential_issue_targets_v1 SET state='Committing',updated_at=? WHERE org_id=? AND target_id=? AND state='Staged'",
                    (now, org_id, target_id),
                ).rowcount
                != 1
            ):
                raise SqliteCredentialIssueCommitError("target commit CAS가 충돌합니다.")
            _fault(fault_injector, "after_target_committing")
            if (
                connection.execute(
                    "UPDATE credential_issue_stage_fences_v2 SET state='Committing',updated_at=? WHERE org_id=? AND target_id=? AND state='Staged'",
                    (now, org_id, target_id),
                ).rowcount
                != 1
            ):
                raise SqliteCredentialIssueCommitError("fence commit CAS가 충돌합니다.")
            _fault(fault_injector, "after_fence_committing")
            if (
                connection.execute(
                    "UPDATE durable_credential_issue_targets_v1 SET state='Committed',updated_at=? WHERE org_id=? AND target_id=? AND state='Committing'",
                    (now, org_id, target_id),
                ).rowcount
                != 1
            ):
                raise SqliteCredentialIssueCommitError("target final CAS가 충돌합니다.")
            _fault(fault_injector, "after_target_committed")
            connection.execute(
                "INSERT INTO durable_credentials VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    target["credential_id"],
                    org_id,
                    target["owner_subject_id"],
                    target["role"],
                    target["target_generation"],
                    1,
                    "active",
                    fence["secret_hash"],
                    now,
                    target["expires_at"],
                    None,
                ),
            )
            _fault(fault_injector, "after_credential_insert")
            result = json.dumps(
                {"credential_id": target["credential_id"], "revision": 1}, separators=(",", ":")
            )
            connection.execute(
                "INSERT INTO credential_command_receipts VALUES(?,?,?,?,?,?,?,?)",
                (
                    org_id,
                    target_id,
                    target["target_generation"],
                    target["command_digest"],
                    target["credential_id"],
                    1,
                    result,
                    fence["delivery_ref"],
                ),
            )
            _fault(fault_injector, "after_receipt_insert")
            connection.execute(
                "INSERT INTO credential_audit_intents(org_id,action,credential_id,principal_subject_id,evidence_id,detail_json) VALUES(?,?,?,?,?,?)",
                (
                    org_id,
                    "worker_credential.issue",
                    target["credential_id"],
                    target["principal_id"],
                    target["approval_evidence_id"],
                    "{}",
                ),
            )
            _fault(fault_injector, "after_audit_insert")
            connection.execute(
                "INSERT INTO credential_outbox_intents(org_id,kind,credential_id,payload_json) VALUES(?,?,?,?)",
                (org_id, "credential_issued", target["credential_id"], "{}"),
            )
            _fault(fault_injector, "after_outbox_insert")
            if (
                connection.execute(
                    "UPDATE credential_issue_stage_fences_v2 SET state='Committed',updated_at=? WHERE org_id=? AND target_id=? AND state='Committing'",
                    (now, org_id, target_id),
                ).rowcount
                != 1
            ):
                raise SqliteCredentialIssueCommitError("fence final CAS가 충돌합니다.")
            _fault(fault_injector, "after_fence_committed")
            active_companion.persist(connection, target, now)
            _fault(fault_injector, "after_companion_persist")
            companion_verify = getattr(active_companion, "verify", None)
            if callable(companion_verify) and companion_verify(connection, target, now) is not True:
                raise SqliteCredentialIssueCommitError("materialization companion readback이 canonical하지 않습니다.")
            _fault(fault_injector, "after_companion_readback")
            validate_sqlite_durable_credential_issue_targets_connection(connection)
            committed_target = connection.execute(
                "SELECT * FROM durable_credential_issue_targets_v1 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            committed_fence = connection.execute(
                "SELECT * FROM credential_issue_stage_fences_v2 WHERE org_id=? AND target_id=?",
                (org_id, target_id),
            ).fetchone()
            if committed_target is None or committed_fence is None:
                raise SqliteCredentialIssueCommitError(
                    "committed aggregate readback이 canonical하지 않습니다."
                )
            delivery_ref = _canonical_committed_delivery_ref(
                connection, committed_target, committed_fence
            )
            connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()

    return CommittedCredentialDelivery(org_id, target_id, target["credential_id"], delivery_ref)


def _commit_sqlite_durable_credential_issue_target_with_production_verifier(  # pyright: ignore[reportUnusedFunction]
    path: str | Path,
    org_id: str,
    target_id: str,
    now: str,
    verifier: CredentialIssueMaterializationVerifier,
    *,
    production_seal: object,
) -> CommittedCredentialDelivery:
    """Production-only bridge; test fakes use the separately named support seam."""
    if production_seal is not _PRODUCTION_COMMIT_SEAL:
        raise TypeError("R3.2 credential materialization commit은 sealed operations만 허용합니다.")
    return _materialize_sqlite_durable_credential_issue_target(
        path, org_id, target_id, now, verifier
    )
