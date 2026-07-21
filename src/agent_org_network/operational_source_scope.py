"""중앙 운영 원천의 조직 범위를 fail-closed로 증명하는 조립 capability."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Protocol, cast


OperationalSourceKind = Literal["registry", "graph", "session", "audit", "hitl"]


@dataclass(frozen=True)
class OperationalSourceSnapshot:
    """source가 현재 자신의 provenance와 조직 범위를 말하는 validate-only snapshot."""

    source_instance: object
    org_id: str
    revision: str
    snapshot_digest: str
    row_org_ids: tuple[str, ...]


class OperationalScopeSnapshotSource(Protocol):
    def operational_source_scope_snapshot(self) -> OperationalSourceSnapshot: ...


class OperationalSourceScopeProof:
    """특정 source *인스턴스*에만 결박된 composition-owned 증명이다."""

    def __init__(
        self,
        *,
        configured_org_id: str,
        source: OperationalScopeSnapshotSource,
        expected: OperationalSourceSnapshot,
    ) -> None:
        self._configured_org_id = configured_org_id
        self._source = source
        self._bound_instance = expected.source_instance
        self._revision = expected.revision
        self._snapshot_digest = expected.snapshot_digest

    def is_current(self, expected_source: object) -> bool:
        try:
            current = self._source.operational_source_scope_snapshot()
        except Exception:
            return False
        return (
            type(current) is OperationalSourceSnapshot
            and self._bound_instance is expected_source
            and current.source_instance is self._bound_instance
            and current.org_id == self._configured_org_id
            and current.revision == self._revision
            and current.snapshot_digest == self._snapshot_digest
            and all(org_id == self._configured_org_id for org_id in current.row_org_ids)
        )


class OperationalSourceScopeProofs:
    """중앙 OperationalApplication이 소비하는 닫힌 source-proof 집합."""

    def __init__(self, proofs: dict[OperationalSourceKind, OperationalSourceScopeProof]) -> None:
        expected = {"registry", "graph", "session", "audit", "hitl"}
        if set(proofs) != expected or any(
            type(proof) is not OperationalSourceScopeProof for proof in proofs.values()
        ):
            raise ValueError("운영 source proof 집합이 완전하지 않습니다.")
        self._proofs = MappingProxyType(dict(proofs))

    def verify(self, **expected_sources: object) -> bool:
        return bool(expected_sources) and all(
            kind in self._proofs and self._proofs[cast(OperationalSourceKind, kind)].is_current(source)
            for kind, source in expected_sources.items()
        )


def compose_operational_source_scope_proofs(
    *,
    configured_org_id: str,
    registry: OperationalScopeSnapshotSource,
    graph: OperationalScopeSnapshotSource,
    session: OperationalScopeSnapshotSource,
    audit: OperationalScopeSnapshotSource,
    hitl: OperationalScopeSnapshotSource,
) -> OperationalSourceScopeProofs:
    """조립 시 한 번 snapshot을 고정한다. raw callback/source는 받을 수 없다."""
    if type(configured_org_id) is not str or not configured_org_id.strip():
        raise ValueError("configured org가 유효하지 않습니다.")
    sources: dict[OperationalSourceKind, OperationalScopeSnapshotSource] = {
        "registry": registry,
        "graph": graph,
        "session": session,
        "audit": audit,
        "hitl": hitl,
    }
    proofs: dict[OperationalSourceKind, OperationalSourceScopeProof] = {}
    for kind, source in sources.items():
        if not callable(getattr(source, "operational_source_scope_snapshot", None)):
            raise ValueError("raw 운영 source는 중앙 조립에 사용할 수 없습니다.")
        try:
            snapshot = source.operational_source_scope_snapshot()
        except Exception as error:
            raise ValueError("운영 source snapshot을 읽을 수 없습니다.") from error
        if (
            type(snapshot) is not OperationalSourceSnapshot
            or snapshot.org_id != configured_org_id
            or not snapshot.revision
            or not snapshot.snapshot_digest
            or any(org_id != configured_org_id for org_id in snapshot.row_org_ids)
        ):
            raise ValueError("운영 source 조직 범위를 증명할 수 없습니다.")
        proofs[kind] = OperationalSourceScopeProof(
            configured_org_id=configured_org_id, source=source, expected=snapshot
        )
    return OperationalSourceScopeProofs(proofs)
