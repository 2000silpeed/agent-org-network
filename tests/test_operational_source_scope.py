"""P17.9 R2 operational source scope proof regression."""

from __future__ import annotations

from agent_org_network.operational_source_scope import (
    OperationalSourceSnapshot,
    compose_operational_source_scope_proofs,
)


class FakeScopedSource:
    def __init__(self, *, org_id: str = "acme") -> None:
        self.org_id = org_id
        self.revision = "revision-1"
        self.digest = "digest-1"
        self.row_org_ids: tuple[str, ...] = (org_id,)
        self.fault = False
        self.provenance_instance: object = self

    def operational_source_scope_snapshot(self) -> OperationalSourceSnapshot:
        if self.fault:
            raise RuntimeError("source unavailable")
        return OperationalSourceSnapshot(
            source_instance=self.provenance_instance,
            org_id=self.org_id,
            revision=self.revision,
            snapshot_digest=self.digest,
            row_org_ids=self.row_org_ids,
        )


def _proofs(source: FakeScopedSource):
    return compose_operational_source_scope_proofs(
        configured_org_id="acme",
        registry=source,
        graph=source,
        session=source,
        audit=source,
        hitl=source,
    )


def test_source_proof_rejects_revision_digest_drift_mixed_org_and_fault() -> None:
    source = FakeScopedSource()
    proofs = _proofs(source)

    assert proofs.verify(registry=source, session=source) is True

    source.revision = "revision-2"
    assert proofs.verify(registry=source) is False

    source.revision = "revision-1"
    source.digest = "digest-2"
    assert proofs.verify(audit=source) is False

    source.digest = "digest-1"
    source.row_org_ids = ("acme", "other")
    assert proofs.verify(hitl=source) is False

    source.row_org_ids = ("acme",)
    source.fault = True
    assert proofs.verify(graph=source) is False


def test_source_proof_rejects_current_snapshot_from_swapped_instance() -> None:
    source = FakeScopedSource()
    proofs = _proofs(source)
    source.provenance_instance = object()

    assert proofs.verify(registry=source) is False


def test_source_proof_requires_scoped_source_not_raw_legacy_object() -> None:
    raw_legacy_source = object()

    try:
        compose_operational_source_scope_proofs(
            configured_org_id="acme",
            registry=raw_legacy_source,  # type: ignore[arg-type]
            graph=raw_legacy_source,  # type: ignore[arg-type]
            session=raw_legacy_source,  # type: ignore[arg-type]
            audit=raw_legacy_source,  # type: ignore[arg-type]
            hitl=raw_legacy_source,  # type: ignore[arg-type]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("raw legacy source를 central proof로 조립하면 안 됩니다.")
