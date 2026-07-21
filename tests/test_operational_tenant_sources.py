"""ADR 0053 tenant operational dependency conformance."""

from __future__ import annotations

import pytest

from agent_org_network.operational_tenant_sources import (
    InMemoryTenantAuditReaderSource,
    InMemoryTenantAuditWriterSource,
    InMemoryTenantGraphSource,
    InMemoryTenantHitlSource,
    InMemoryTenantRegistrySource,
    InMemoryTenantSessionSource,
    OperationalCentralDependencies,
)


def _dependencies(org_id: str = "acme") -> OperationalCentralDependencies:
    return OperationalCentralDependencies(
        configured_org_id=org_id,
        registry=InMemoryTenantRegistrySource(org_id=org_id),
        graph=InMemoryTenantGraphSource(org_id=org_id),
        session=InMemoryTenantSessionSource(org_id=org_id),
        audit_reader=InMemoryTenantAuditReaderSource(org_id=org_id),
        audit_writer=InMemoryTenantAuditWriterSource(org_id=org_id),
        hitl=InMemoryTenantHitlSource(org_id=org_id),
    )


def test_complete_six_kind_dependencies_issue_exact_current_capabilities() -> None:
    dependencies = _dependencies()

    capabilities = dependencies.validate_scope()

    assert capabilities is not None
    assert capabilities.validates_current_scope() is True
    assert capabilities.registry.binds(dependencies.registry) is True
    assert capabilities.audit_reader.binds(dependencies.audit_writer) is False


def test_mixed_org_row_fault_and_provenance_revision_failure_close_scope() -> None:
    dependencies = _dependencies()
    assert dependencies.validate_scope() is not None

    dependencies.session.set_rows_for_test(("acme", "other"))
    assert dependencies.validate_scope() is None

    dependencies.session.set_rows_for_test(("acme",))
    dependencies.audit_writer.set_fault_for_test(True)
    assert dependencies.validate_scope() is None

    dependencies.audit_writer.set_fault_for_test(False)
    dependencies.audit_writer.advance_revision_for_test()
    capabilities = dependencies.validate_scope()
    assert capabilities is not None
    assert capabilities.audit_writer.validates_current_scope() is True


def test_source_swap_invalidates_previously_issued_capability() -> None:
    dependencies = _dependencies()
    capabilities = dependencies.validate_scope()
    assert capabilities is not None

    replacement = InMemoryTenantRegistrySource(org_id="acme")
    assert capabilities.registry.binds(replacement) is False


def test_raw_partial_or_mismatched_source_is_not_a_central_dependency() -> None:
    with pytest.raises(ValueError):
        OperationalCentralDependencies(
            configured_org_id="acme",
            registry=object(),  # type: ignore[arg-type]
            graph=InMemoryTenantGraphSource(org_id="acme"),
            session=InMemoryTenantSessionSource(org_id="acme"),
            audit_reader=InMemoryTenantAuditReaderSource(org_id="acme"),
            audit_writer=InMemoryTenantAuditWriterSource(org_id="acme"),
            hitl=InMemoryTenantHitlSource(org_id="acme"),
        )

    with pytest.raises(ValueError):
        OperationalCentralDependencies(
            configured_org_id="acme",
            registry=InMemoryTenantRegistrySource(org_id="acme"),
            graph=InMemoryTenantGraphSource(org_id="other"),
            session=InMemoryTenantSessionSource(org_id="acme"),
            audit_reader=InMemoryTenantAuditReaderSource(org_id="acme"),
            audit_writer=InMemoryTenantAuditWriterSource(org_id="acme"),
            hitl=InMemoryTenantHitlSource(org_id="acme"),
        )
