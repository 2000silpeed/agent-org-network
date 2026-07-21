"""R1.1 internal-SQL atomic UoW for prevalidated tenant mutation commands."""

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Union

from agent_org_network.sqlite_durable_tenant_operational_mutations import (
    SqliteDurableTenantOperationalMutationsError,
    SqliteTenantOperationalMutationScopeSnapshot,
    capture_sqlite_tenant_operational_mutation_scope_snapshot,
    open_sqlite_durable_tenant_operational_mutations,
)

type FaultInjector = Callable[[str], None]


class SqliteTenantOperationalMutationUowError(RuntimeError):
    pass


@dataclass(frozen=True)
class _Command:
    org_id: str
    command_id: str
    principal_id: str
    expected_scope: SqliteTenantOperationalMutationScopeSnapshot
    created_at: str

    def __post_init__(self) -> None:
        if any(
            type(v) is not str or not v
            for v in (self.org_id, self.command_id, self.principal_id, self.created_at)
        ):
            raise ValueError("prevalidated mutation command scalar가 유효하지 않습니다.")
        if type(self.expected_scope) is not SqliteTenantOperationalMutationScopeSnapshot:
            raise ValueError("prevalidated mutation command scope가 유효하지 않습니다.")


@dataclass(frozen=True)
class CardRegisterCommand(_Command):
    card_id: str
    owner_id: str


@dataclass(frozen=True)
class CardTransferOwnerCommand(_Command):
    card_id: str
    owner_id: str


@dataclass(frozen=True)
class SessionEndCommand(_Command):
    session_id: str


@dataclass(frozen=True)
class HitlWriteCommand(_Command):
    card_id: str
    on: bool

    def __post_init__(self) -> None:
        super().__post_init__()
        if type(self.on) is not bool:
            raise ValueError("HITL on은 bool이어야 합니다.")


TenantOperationalMutationCommand = Union[
    CardRegisterCommand, CardTransferOwnerCommand, SessionEndCommand, HitlWriteCommand
]


@dataclass(frozen=True)
class TenantOperationalMutationReceipt:
    receipt_id: str
    command_id: str
    action: str
    replayed: bool


@dataclass(frozen=True)
class TenantOperationalAuthorizationBinding:
    """Already re-authorized R1.2 evidence; persisted inside the R1.1 transaction."""

    pre_resource_json: str
    post_resource_json: str
    post_resource_fingerprint: str
    pre_resource_fingerprint: str
    evidence_id: str
    approver_subject_id: str
    approval_command_digest: str
    approval_resource_fingerprint: str
    approved_at: str

    def __post_init__(self) -> None:
        values = (
            self.pre_resource_json,
            self.post_resource_json,
            self.post_resource_fingerprint,
            self.pre_resource_fingerprint,
            self.evidence_id,
            self.approver_subject_id,
            self.approval_command_digest,
            self.approval_resource_fingerprint,
            self.approved_at,
        )
        if any(type(value) is not str or not value for value in values) or any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in (
                self.post_resource_fingerprint,
                self.pre_resource_fingerprint,
                self.approval_command_digest,
                self.approval_resource_fingerprint,
            )
        ):
            raise ValueError("R1.2 authorization binding이 strict하지 않습니다.")


def _canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _action(command: TenantOperationalMutationCommand) -> str:
    if type(command) is CardRegisterCommand:
        return "card.register"
    if type(command) is CardTransferOwnerCommand:
        return "card.transfer_owner"
    if type(command) is SessionEndCommand:
        return "session.end"
    if type(command) is HitlWriteCommand:
        return "hitl.write"
    raise SqliteTenantOperationalMutationUowError("unknown prevalidated mutation command입니다.")


def _command_digest(command: TenantOperationalMutationCommand) -> str:
    value = asdict(command)
    # Scope is a prewrite CAS guard, not semantic command identity: own prior
    # commit necessarily advances it and must still permit exact replay.
    value.pop("expected_scope")
    return _digest(value)


def canonical_tenant_operational_mutation_uow_command_digest(
    command: TenantOperationalMutationCommand,
) -> str:
    """Public semantic command identity used for cross-channel replay checks."""
    return _command_digest(command)


def _receipt_id(command: TenantOperationalMutationCommand) -> str:
    return "receipt:" + _digest({"org": command.org_id, "command": command.command_id})


def _audit_digest(action: str, subject: str, fingerprint: str) -> str:
    return _digest(
        {
            "action": action,
            "fingerprint": fingerprint,
            "outcome": "succeeded",
            "subject_id": subject,
        }
    )


def _fault(injector: FaultInjector | None, point: str) -> None:
    if injector is not None:
        injector(point)


def _require_scope(
    connection: sqlite3.Connection, command: TenantOperationalMutationCommand
) -> None:
    current = capture_sqlite_tenant_operational_mutation_scope_snapshot(connection)
    if current != command.expected_scope:
        raise SqliteTenantOperationalMutationUowError(
            "expected source scope snapshot이 stale입니다."
        )


def _state_write(connection: sqlite3.Connection, command: TenantOperationalMutationCommand) -> None:
    if type(command) in (CardRegisterCommand, CardTransferOwnerCommand):
        row = connection.execute(
            "SELECT revision,payload_json FROM operational_registry_state WHERE org_id=?",
            (command.org_id,),
        ).fetchone()
        if row is None:
            raise SqliteTenantOperationalMutationUowError("registry state가 없습니다.")
        revision, payload_json = row
        try:
            payload = json.loads(payload_json)
            cards, users = payload["cards"], payload["users"]
        except (TypeError, ValueError, KeyError) as error:
            raise SqliteTenantOperationalMutationUowError(
                "registry state가 strict하지 않습니다."
            ) from error
        if (
            not isinstance(cards, dict)
            or not isinstance(users, list)
            or command.owner_id not in users
        ):
            raise SqliteTenantOperationalMutationUowError("registry target이 유효하지 않습니다.")
        existing = cards.get(command.card_id)
        if type(command) is CardRegisterCommand:
            if existing is not None:
                raise SqliteTenantOperationalMutationUowError(
                    "receipt 없는 same-effect register는 replay가 아닙니다."
                )
        elif not isinstance(existing, dict) or existing.get("owner") == command.owner_id:
            raise SqliteTenantOperationalMutationUowError(
                "receipt 없는 transfer는 replay가 아닙니다."
            )
        cards[command.card_id] = {"owner": command.owner_id}
        encoded = _canonical(payload)
        changed = connection.execute(
            "UPDATE operational_registry_state SET revision=?,payload_json=?,payload_digest=?,updated_at=? WHERE org_id=? AND revision=?",
            (
                revision + 1,
                encoded,
                hashlib.sha256(encoded.encode()).hexdigest(),
                command.created_at,
                command.org_id,
                revision,
            ),
        ).rowcount
        if changed != 1:
            raise SqliteTenantOperationalMutationUowError("registry CAS가 stale입니다.")
    elif type(command) is SessionEndCommand:
        row = connection.execute(
            "SELECT status,revision FROM operational_sessions WHERE org_id=? AND session_id=?",
            (command.org_id, command.session_id),
        ).fetchone()
        if row is None or row[0] != "active":
            raise SqliteTenantOperationalMutationUowError(
                "receipt 없는 session end는 replay가 아닙니다."
            )
        if (
            connection.execute(
                "UPDATE operational_sessions SET status='ended',last_active_at=?,revision=? WHERE org_id=? AND session_id=? AND revision=? AND status='active'",
                (command.created_at, row[1] + 1, command.org_id, command.session_id, row[1]),
            ).rowcount
            != 1
        ):
            raise SqliteTenantOperationalMutationUowError("session CAS가 stale입니다.")
    else:
        assert type(command) is HitlWriteCommand
        row = connection.execute(
            'SELECT revision,"on" FROM operational_hitl_toggles WHERE org_id=? AND agent_id=?',
            (command.org_id, command.card_id),
        ).fetchone()
        if row is None:
            if not command.on:
                raise SqliteTenantOperationalMutationUowError(
                    "receipt 없는 HITL default는 replay가 아닙니다."
                )
            connection.execute(
                "INSERT INTO operational_hitl_toggles VALUES(?,?,?,1,0,?)",
                (command.org_id, command.card_id, 1, command.created_at),
            )
        elif bool(row[1]) == command.on:
            raise SqliteTenantOperationalMutationUowError(
                "receipt 없는 same-effect HITL은 replay가 아닙니다."
            )
        elif (
            connection.execute(
                'UPDATE operational_hitl_toggles SET "on"=?,explicit=1,revision=?,updated_at=? WHERE org_id=? AND agent_id=? AND revision=?',
                (
                    int(command.on),
                    row[0] + 1,
                    command.created_at,
                    command.org_id,
                    command.card_id,
                    row[0],
                ),
            ).rowcount
            != 1
        ):
            raise SqliteTenantOperationalMutationUowError("HITL CAS가 stale입니다.")


def execute_sqlite_tenant_operational_mutation(
    connection: sqlite3.Connection,
    command: TenantOperationalMutationCommand,
    *,
    fault_injector: FaultInjector | None = None,
    authorization_binding: TenantOperationalAuthorizationBinding | None = None,
) -> TenantOperationalMutationReceipt:
    """Commit one prevalidated command plus immutable receipt/audit/outbox atomically."""
    if type(command) not in (
        CardRegisterCommand,
        CardTransferOwnerCommand,
        SessionEndCommand,
        HitlWriteCommand,
    ):
        raise SqliteTenantOperationalMutationUowError("typed command만 허용합니다.")
    try:
        open_sqlite_durable_tenant_operational_mutations(connection).validate_only()
        if authorization_binding is not None:
            from agent_org_network.sqlite_durable_tenant_operational_authorization import (
                open_sqlite_durable_tenant_operational_authorization,
            )

            open_sqlite_durable_tenant_operational_authorization(connection)
        connection.execute("BEGIN IMMEDIATE")
        action, digest, receipt_id = (
            _action(command),
            _command_digest(command),
            _receipt_id(command),
        )
        old = connection.execute(
            "SELECT principal_id,action,command_digest FROM durable_tenant_operational_mutation_receipts WHERE org_id=? AND command_id=?",
            (command.org_id, command.command_id),
        ).fetchone()
        if old is not None:
            if tuple(old) != (command.principal_id, action, digest):
                raise SqliteTenantOperationalMutationUowError(
                    "command id가 다른 principal/action/digest로 재사용되었습니다."
                )
            if authorization_binding is not None:
                evidence = connection.execute(
                    "SELECT org_id,principal_id,action,command_digest,pre_resource_json,post_resource_json,post_resource_fingerprint,pre_resource_fingerprint,evidence_id,approver_subject_id,approval_command_digest,approval_resource_fingerprint,approved_at FROM durable_tenant_operational_authorization_evidence WHERE receipt_id=?",
                    (receipt_id,),
                ).fetchone()
                expected = (
                    command.org_id,
                    command.principal_id,
                    action,
                    digest,
                    authorization_binding.pre_resource_json,
                    authorization_binding.post_resource_json,
                    authorization_binding.post_resource_fingerprint,
                    authorization_binding.pre_resource_fingerprint,
                    authorization_binding.evidence_id,
                    authorization_binding.approver_subject_id,
                    authorization_binding.approval_command_digest,
                    authorization_binding.approval_resource_fingerprint,
                    authorization_binding.approved_at,
                )
                if evidence is None or tuple(evidence) != expected:
                    raise SqliteTenantOperationalMutationUowError(
                        "R1.2 replay evidence가 exact하지 않습니다."
                    )
            connection.commit()
            return TenantOperationalMutationReceipt(receipt_id, command.command_id, action, True)
        _require_scope(connection, command)
        _state_write(connection, command)
        _fault(fault_injector, "after_state")
        # The receipt records the exact prewrite scope supplied by the caller; post-write scope is only readback evidence.
        connection.execute(
            "INSERT INTO durable_tenant_operational_mutation_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                command.org_id,
                command.command_id,
                command.principal_id,
                action,
                digest,
                command.expected_scope.database_identity,
                command.expected_scope.schema_manifest_digest,
                command.expected_scope.source_revision,
                command.expected_scope.snapshot_digest,
                command.created_at,
            ),
        )
        _fault(fault_injector, "after_receipt")
        if authorization_binding is not None:
            connection.execute(
                "INSERT INTO durable_tenant_operational_authorization_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    command.org_id,
                    command.principal_id,
                    action,
                    digest,
                    authorization_binding.pre_resource_json,
                    authorization_binding.post_resource_json,
                    authorization_binding.post_resource_fingerprint,
                    authorization_binding.pre_resource_fingerprint,
                    authorization_binding.evidence_id,
                    authorization_binding.approver_subject_id,
                    authorization_binding.approval_command_digest,
                    authorization_binding.approval_resource_fingerprint,
                    authorization_binding.approved_at,
                ),
            )
            _fault(fault_injector, "after_evidence")
        sequence = connection.execute(
            "SELECT count(*) FROM operational_audit_events_v2 WHERE org_id=?", (command.org_id,)
        ).fetchone()[0]
        event_digest = _audit_digest(action, command.principal_id, digest)
        connection.execute(
            "INSERT INTO operational_audit_events_v2 VALUES(?,?,?,?,?,?,?,?)",
            (
                command.org_id,
                sequence,
                action,
                command.principal_id,
                "succeeded",
                digest,
                event_digest,
                command.created_at,
            ),
        )
        connection.execute(
            "INSERT INTO durable_tenant_operational_mutation_audit_intents VALUES(?,?,?,?,?,?,?,?,?)",
            (
                receipt_id,
                command.org_id,
                sequence,
                action,
                command.principal_id,
                "succeeded",
                digest,
                event_digest,
                command.created_at,
            ),
        )
        _fault(fault_injector, "after_audit")
        connection.execute(
            "INSERT INTO durable_tenant_operational_mutation_outbox_intents VALUES(?,?,?,?,?)",
            (receipt_id, command.org_id, action, digest, command.created_at),
        )
        _fault(fault_injector, "after_outbox")
        if (
            connection.execute(
                "SELECT 1 FROM durable_tenant_operational_mutation_outbox_intents WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
            is None
        ):
            raise SqliteTenantOperationalMutationUowError(
                "durable mutation readback이 실패했습니다."
            )
        _fault(fault_injector, "after_readback")
        _fault(fault_injector, "before_commit")
        connection.commit()
        return TenantOperationalMutationReceipt(receipt_id, command.command_id, action, False)
    except Exception as error:
        if connection.in_transaction:
            connection.rollback()
        if isinstance(error, SqliteTenantOperationalMutationUowError):
            raise
        if isinstance(error, SqliteDurableTenantOperationalMutationsError):
            raise SqliteTenantOperationalMutationUowError(
                "R1.0 capability가 unavailable입니다."
            ) from error
        raise
