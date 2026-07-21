"""S3.0 pure tenant operational ports; no legacy or authority dependency."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal, Protocol


def _opaque(value: str, label: str) -> str:
    if type(value) is not str or not value or len(value) > 256:
        raise ValueError(f"{label}는 strict opaque id여야 합니다.")
    return value


@dataclass(frozen=True)
class TenantOrgId:
    value: str
    def __post_init__(self) -> None: _opaque(self.value, "org")


@dataclass(frozen=True)
class ResourceFingerprint:
    value: str
    def __post_init__(self) -> None:
        if type(self.value) is not str or len(self.value) != 64 or any(c not in "0123456789abcdef" for c in self.value):
            raise ValueError("fingerprint는 lowercase sha256이어야 합니다.")
    @classmethod
    def from_scalars(cls, *parts: str) -> "ResourceFingerprint":
        return cls(sha256("\x00".join(_opaque(part, "resource") for part in parts).encode()).hexdigest())


@dataclass(frozen=True)
class ScopedUnavailable:
    kind: Literal["unavailable"] = "unavailable"
    def __post_init__(self) -> None:
        if self.kind != "unavailable":
            raise ValueError("unavailable discriminator가 유효하지 않습니다.")


@dataclass(frozen=True)
class StateCommittedAuditPending:
    """R1 전 state write 뒤 audit append 실패를 정직하게 나타낸다."""
    resource: ResourceFingerprint
    kind: Literal["state_committed_audit_pending"] = "state_committed_audit_pending"
    def __post_init__(self) -> None:
        if type(self.resource) is not ResourceFingerprint or self.kind != "state_committed_audit_pending":
            raise ValueError("pending result가 유효하지 않습니다.")


@dataclass(frozen=True)
class TenantCard:
    card_id: str
    owner_id: str
    fingerprint: ResourceFingerprint
    def __post_init__(self) -> None:
        _opaque(self.card_id, "card")
        _opaque(self.owner_id, "owner")
        if type(self.fingerprint) is not ResourceFingerprint:
            raise ValueError("card fingerprint가 유효하지 않습니다.")


@dataclass(frozen=True)
class TenantSession:
    session_id: str
    user_id: str
    status: Literal["active", "ended"]
    fingerprint: ResourceFingerprint
    def __post_init__(self) -> None:
        _opaque(self.session_id, "session")
        _opaque(self.user_id, "user")
        if self.status not in {"active", "ended"}:
            raise ValueError("session status가 유효하지 않습니다.")
        if type(self.fingerprint) is not ResourceFingerprint:
            raise ValueError("session fingerprint가 유효하지 않습니다.")


@dataclass(frozen=True)
class SafeAuditEvent:
    action: str
    subject_id: str
    outcome: Literal["succeeded", "audit_pending"]
    fingerprint: ResourceFingerprint
    def __post_init__(self) -> None:
        _opaque(self.action, "action")
        _opaque(self.subject_id, "subject")
        if type(self.fingerprint) is not ResourceFingerprint or self.outcome not in {"succeeded", "audit_pending"}:
            raise ValueError("audit event가 유효하지 않습니다.")


class TenantRegistryPort(Protocol):
    def card(self, org: TenantOrgId, card_id: str) -> TenantCard | ScopedUnavailable: ...
    def admit(self, org: TenantOrgId, card: TenantCard) -> TenantCard | StateCommittedAuditPending | ScopedUnavailable: ...
    def transfer(self, org: TenantOrgId, card_id: str, owner_id: str) -> TenantCard | StateCommittedAuditPending | ScopedUnavailable: ...


class TenantGraphPort(Protocol):
    def derive(self, org: TenantOrgId) -> tuple[TenantCard, ...] | ScopedUnavailable: ...


class TenantSessionPort(Protocol):
    def session(self, org: TenantOrgId, session_id: str) -> TenantSession | ScopedUnavailable: ...
    def end(self, org: TenantOrgId, session_id: str) -> TenantSession | StateCommittedAuditPending | ScopedUnavailable: ...


class TenantAuditReaderPort(Protocol):
    def list(self, org: TenantOrgId) -> tuple[SafeAuditEvent, ...] | ScopedUnavailable: ...
    def detail(self, org: TenantOrgId, sequence: int) -> SafeAuditEvent | ScopedUnavailable: ...


class TenantAuditWriterPort(Protocol):
    def append(self, org: TenantOrgId, event: SafeAuditEvent) -> None | ScopedUnavailable: ...


class TenantHitlPort(Protocol):
    def read(self, org: TenantOrgId, card_id: str) -> bool | ScopedUnavailable: ...
    def write(self, org: TenantOrgId, card_id: str, on: bool) -> bool | StateCommittedAuditPending | ScopedUnavailable: ...
