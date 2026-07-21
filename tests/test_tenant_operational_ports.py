from __future__ import annotations

import pytest
import inspect
from typing import get_type_hints
from pathlib import Path

from agent_org_network.tenant_operational_ports import (
    ResourceFingerprint,
    SafeAuditEvent,
    ScopedUnavailable,
    StateCommittedAuditPending,
    TenantOrgId,
    TenantCard,
    TenantSession,
)


def test_strict_opaque_ids_and_frozen_safe_result_grammar() -> None:
    fingerprint = ResourceFingerprint.from_scalars("acme", "session", "s1")
    event = SafeAuditEvent("session.end", "s1", "succeeded", fingerprint)
    pending = StateCommittedAuditPending(fingerprint)
    assert TenantOrgId("acme").value == "acme"
    assert event.outcome == "succeeded"
    assert pending.kind == "state_committed_audit_pending"
    with pytest.raises(ValueError):
        TenantOrgId("")


@pytest.mark.parametrize("value", ["", "A" * 64, "z" * 64, 1])
def test_fingerprint_and_discriminators_are_runtime_strict(value: object) -> None:
    with pytest.raises(ValueError):
        ResourceFingerprint(value)  # type: ignore[arg-type]
    fingerprint = ResourceFingerprint.from_scalars("acme", "x")
    with pytest.raises(ValueError):
        TenantSession("s", "u", "bad", fingerprint)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SafeAuditEvent("a", "s", "bad", fingerprint)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ScopedUnavailable("bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        StateCommittedAuditPending("raw")  # type: ignore[arg-type]


def test_all_six_ports_expose_only_tenant_operations_and_pending_mutations() -> None:
    import agent_org_network.tenant_operational_ports as ports

    expected = {
        "TenantRegistryPort": {"card", "admit", "transfer"},
        "TenantGraphPort": {"derive"},
        "TenantSessionPort": {"session", "end"},
        "TenantAuditReaderPort": {"list", "detail"},
        "TenantAuditWriterPort": {"append"},
        "TenantHitlPort": {"read", "write"},
    }
    unavailable = ports.ScopedUnavailable
    pending = ports.StateCommittedAuditPending
    expected_types = {
        ("TenantRegistryPort", "card"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("card_id", str)), ports.TenantCard | unavailable),
        ("TenantRegistryPort", "admit"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("card", ports.TenantCard)), ports.TenantCard | pending | unavailable),
        ("TenantRegistryPort", "transfer"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("card_id", str), ("owner_id", str)), ports.TenantCard | pending | unavailable),
        ("TenantGraphPort", "derive"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId)), tuple[ports.TenantCard, ...] | unavailable),
        ("TenantSessionPort", "session"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("session_id", str)), ports.TenantSession | unavailable),
        ("TenantSessionPort", "end"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("session_id", str)), ports.TenantSession | pending | unavailable),
        ("TenantAuditReaderPort", "list"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId)), tuple[ports.SafeAuditEvent, ...] | unavailable),
        ("TenantAuditReaderPort", "detail"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("sequence", int)), ports.SafeAuditEvent | unavailable),
        ("TenantAuditWriterPort", "append"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("event", ports.SafeAuditEvent)), None | unavailable),
        ("TenantHitlPort", "read"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("card_id", str)), bool | unavailable),
        ("TenantHitlPort", "write"): ((("self", inspect.Signature.empty), ("org", ports.TenantOrgId), ("card_id", str), ("on", bool)), bool | pending | unavailable),
    }
    for name, methods in expected.items():
        protocol = getattr(ports, name)
        public = {key for key, value in protocol.__dict__.items() if callable(value) and not key.startswith("_")}
        assert public == methods
        for method in methods:
            operation = getattr(protocol, method)
            assert callable(operation)
            signature = inspect.signature(operation)
            params, result = expected_types[(name, method)]
            hints = get_type_hints(operation)
            assert tuple((parameter.name, hints.get(parameter.name, inspect.Signature.empty)) for parameter in signature.parameters.values()) == params
            assert hints["return"] == result
    for name, method in (("TenantRegistryPort", "admit"), ("TenantRegistryPort", "transfer"), ("TenantSessionPort", "end"), ("TenantHitlPort", "write")):
        assert "StateCommittedAuditPending" in str(inspect.signature(getattr(getattr(ports, name), method)).return_annotation)
    for name, method in (("TenantRegistryPort", "card"), ("TenantGraphPort", "derive"), ("TenantSessionPort", "session"), ("TenantAuditReaderPort", "list"), ("TenantAuditReaderPort", "detail"), ("TenantAuditWriterPort", "append"), ("TenantHitlPort", "read")):
        assert "StateCommittedAuditPending" not in str(inspect.signature(getattr(getattr(ports, name), method)).return_annotation)


def test_raw_fingerprints_and_forbidden_dependencies_are_rejected() -> None:
    fingerprint = ResourceFingerprint.from_scalars("acme", "x")
    with pytest.raises(ValueError):
        TenantCard("c", "u", "raw")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        TenantSession("s", "u", "active", "raw")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SafeAuditEvent("", "s", "succeeded", fingerprint)
    source = Path(__file__).parents[1] / "src" / "agent_org_network" / "tenant_operational_ports.py"
    text = source.read_text()
    for forbidden in ("sqlite", "json", "Authority", "OperationalApplication", "from agent_org_network.registry"):
        assert forbidden not in text
