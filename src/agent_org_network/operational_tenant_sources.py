"""ADR 0053의 tenant-bound operational source conformance ports.

이 모듈은 legacy Registry/SessionStore/audit/HITL/callback을 감싸지 않는다. 테스트용
in-memory source도 실제 tenant provenance와 row scope를 상태로 보유해 validate-only
capability를 발급한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Literal


OperationalTenantSourceKind = Literal[
    "registry", "graph", "session", "audit_reader", "audit_writer", "hitl"
]


@dataclass(frozen=True)
class TenantSourceState:
    org_id: str
    revision: int
    provenance: str
    row_org_ids: tuple[str, ...]
    fault: bool = False


class _InMemoryTenantSource:
    _kind: OperationalTenantSourceKind

    def __init__(self, *, org_id: str, rows: tuple[str, ...] | None = None) -> None:
        self._state = TenantSourceState(
            org_id=org_id,
            revision=1,
            provenance=_provenance(self._kind, org_id, 1),
            row_org_ids=rows if rows is not None else (org_id,),
        )

    def set_rows_for_test(self, row_org_ids: tuple[str, ...]) -> None:
        self._replace(row_org_ids=row_org_ids)

    def set_fault_for_test(self, fault: bool) -> None:
        self._replace(fault=fault)

    def advance_revision_for_test(self) -> None:
        self._replace(revision=self._state.revision + 1)

    def _replace(
        self,
        *,
        row_org_ids: tuple[str, ...] | None = None,
        fault: bool | None = None,
        revision: int | None = None,
    ) -> None:
        next_revision = self._state.revision if revision is None else revision
        self._state = TenantSourceState(
            org_id=self._state.org_id,
            revision=next_revision,
            provenance=_provenance(self._kind, self._state.org_id, next_revision),
            row_org_ids=self._state.row_org_ids if row_org_ids is None else row_org_ids,
            fault=self._state.fault if fault is None else fault,
        )

    @property
    def org_id(self) -> str:
        return self._state.org_id

    @property
    def kind(self) -> OperationalTenantSourceKind:
        return self._kind

    def validate_scope_for(self, configured_org_id: str) -> bool:
        state = self._state
        return (
            not state.fault
            and state.org_id == configured_org_id
            and state.provenance == _provenance(self._kind, state.org_id, state.revision)
            and bool(state.row_org_ids)
            and all(row_org_id == configured_org_id for row_org_id in state.row_org_ids)
        )


class InMemoryTenantRegistrySource(_InMemoryTenantSource):
    _kind: OperationalTenantSourceKind = "registry"


class InMemoryTenantGraphSource(_InMemoryTenantSource):
    _kind: OperationalTenantSourceKind = "graph"


class InMemoryTenantSessionSource(_InMemoryTenantSource):
    _kind: OperationalTenantSourceKind = "session"


class InMemoryTenantAuditReaderSource(_InMemoryTenantSource):
    _kind: OperationalTenantSourceKind = "audit_reader"


class InMemoryTenantAuditWriterSource(_InMemoryTenantSource):
    _kind: OperationalTenantSourceKind = "audit_writer"


class InMemoryTenantHitlSource(_InMemoryTenantSource):
    _kind: OperationalTenantSourceKind = "hitl"


@dataclass(frozen=True)
class OperationalSourceCapability:
    kind: OperationalTenantSourceKind
    org_id: str
    _source: _InMemoryTenantSource

    def validates_current_scope(self) -> bool:
        return self._source.kind == self.kind and self._source.validate_scope_for(self.org_id)

    def binds(self, source: object) -> bool:
        return source is self._source


@dataclass(frozen=True)
class OperationalSourceCapabilities:
    registry: OperationalSourceCapability
    graph: OperationalSourceCapability
    session: OperationalSourceCapability
    audit_reader: OperationalSourceCapability
    audit_writer: OperationalSourceCapability
    hitl: OperationalSourceCapability

    def validates_current_scope(self) -> bool:
        capabilities = (
            self.registry,
            self.graph,
            self.session,
            self.audit_reader,
            self.audit_writer,
            self.hitl,
        )
        return all(capability.validates_current_scope() for capability in capabilities)


class OperationalCentralDependencies:
    """완전한 six-kind tenant ports만 받는 중앙 조립물."""

    def __init__(
        self,
        *,
        configured_org_id: str,
        registry: InMemoryTenantRegistrySource,
        graph: InMemoryTenantGraphSource,
        session: InMemoryTenantSessionSource,
        audit_reader: InMemoryTenantAuditReaderSource,
        audit_writer: InMemoryTenantAuditWriterSource,
        hitl: InMemoryTenantHitlSource,
    ) -> None:
        if type(configured_org_id) is not str or not configured_org_id.strip():
            raise ValueError("configured org가 유효하지 않습니다.")
        expected = (
            (registry, InMemoryTenantRegistrySource, "registry"),
            (graph, InMemoryTenantGraphSource, "graph"),
            (session, InMemoryTenantSessionSource, "session"),
            (audit_reader, InMemoryTenantAuditReaderSource, "audit_reader"),
            (audit_writer, InMemoryTenantAuditWriterSource, "audit_writer"),
            (hitl, InMemoryTenantHitlSource, "hitl"),
        )
        if any(type(source) is not source_type for source, source_type, _ in expected):
            raise ValueError("raw 또는 다른 tenant source를 중앙 조립에 사용할 수 없습니다.")
        if any(source.org_id != configured_org_id for source, _, _ in expected):
            raise ValueError("중앙 source 조직이 configured org와 일치하지 않습니다.")
        self.configured_org_id = configured_org_id
        self.registry = registry
        self.graph = graph
        self.session = session
        self.audit_reader = audit_reader
        self.audit_writer = audit_writer
        self.hitl = hitl

    def validate_scope(self) -> OperationalSourceCapabilities | None:
        sources = (
            (self.registry, "registry"),
            (self.graph, "graph"),
            (self.session, "session"),
            (self.audit_reader, "audit_reader"),
            (self.audit_writer, "audit_writer"),
            (self.hitl, "hitl"),
        )
        if not all(source.validate_scope_for(self.configured_org_id) for source, _ in sources):
            return None
        capabilities = OperationalSourceCapabilities(
            registry=OperationalSourceCapability("registry", self.configured_org_id, self.registry),
            graph=OperationalSourceCapability("graph", self.configured_org_id, self.graph),
            session=OperationalSourceCapability("session", self.configured_org_id, self.session),
            audit_reader=OperationalSourceCapability(
                "audit_reader", self.configured_org_id, self.audit_reader
            ),
            audit_writer=OperationalSourceCapability(
                "audit_writer", self.configured_org_id, self.audit_writer
            ),
            hitl=OperationalSourceCapability("hitl", self.configured_org_id, self.hitl),
        )
        return capabilities if capabilities.validates_current_scope() else None


def _provenance(kind: OperationalTenantSourceKind, org_id: str, revision: int) -> str:
    return sha256(f"{kind}:{org_id}:{revision}".encode()).hexdigest()
