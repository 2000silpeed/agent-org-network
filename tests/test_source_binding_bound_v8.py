from __future__ import annotations
# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import test_sqlite_reciprocal_review_source_binding_v7 as v7
import agent_org_network.source_binding_bound_v8 as v8_impl
from agent_org_network.source_binding_bound_v8 import (
    BoundV8Denied,
    SourceBindingTerminalizerV8,
    SourceReadGateV8,
    arm_kill_switch_v8,
    migrate_sqlite_source_binding_bound_v8,
    mint_bound_readback_v8,
    mint_serving_attestation_v8,
)
from agent_org_network.trusted_source_integration import (
    _bootstrap_source_integration_profile,
    _bootstrap_trusted_source_integration_registry,
)


def _ready(path: Path) -> tuple[Any, Any, Any, Any, Any]:
    v7._ready_v7(path)
    capability = v7._test_capability(v7._CentralAuthority(), path)
    assert capability is not None
    profile = _bootstrap_source_integration_profile(
        profile_id="fake", profile_version="1", profile_digest="d" * 64,
        org_id="org", source_ref="source:trusted", external_target_fingerprint="target",
        mtls_client_identity_ref="hsm://client", tls_server_identity="fake.example",
        credential_ref="vault://opaque/1", credential_generation=1,
        policy_digest="c" * 64, signing_key_id="fake-key",
    )
    registry, control = _bootstrap_trusted_source_integration_registry(
        profiles={"fake": profile}, signing_keys={"fake-key": b"fake-key"}
    )
    session = registry.open("fake", capability, source_revision="source-revision-1")
    connection = sqlite3.connect(path)
    migrate_sqlite_source_binding_bound_v8(connection)
    env = connection.execute(
        "SELECT authorization_json FROM reciprocal_review_v7_binding_intents"
    ).fetchone()[0]
    receipt = v7.SourceBindingAuthorizationEnvelopeV7.model_validate_json(env)
    connection.close()
    readback = mint_bound_readback_v8(
        session=session, org_id="org", intent_id="v7-receipt", source_ref="source:trusted",
        expected_revision="source-revision-1", content_digest="a" * 64,
        receipt_payload_digest=receipt.payload_digest, generation=1, enforcement_id="enforcement-1",
    )
    return capability, session, control, readback, receipt


def _bound(path: Path) -> tuple[Any, Any, Any, Any, Any, Any]:
    capability, session, control, readback, receipt = _ready(path)
    terminalizer = SourceBindingTerminalizerV8(path, capability, session)
    result = terminalizer.finalize(org_id="org", intent_id="v7-receipt", readback=readback, audit_id="audit", outbox_id="outbox")
    return capability, session, control, readback, receipt, result


def _tamper(readback: Any, field: str, value: object) -> Any:
    forged = object.__new__(type(readback))
    for name in readback.__dataclass_fields__:
        object.__setattr__(forged, name, value if name == field else getattr(readback, name))
    return forged


def test_v8_bound_happy_path_and_every_read_attestation(tmp_path: Path) -> None:
    path = tmp_path / "bound.sqlite"
    capability, session, _control, readback, _receipt, result = _bound(path)
    connection = sqlite3.connect(path)
    enforcement_digest = connection.execute("SELECT enforcement_digest FROM source_binding_v8_terminals").fetchone()[0]
    connection.close()
    attestation = mint_serving_attestation_v8(session=session, readback=readback, enforcement_digest=enforcement_digest)
    assert SourceReadGateV8(path, capability, session).authorize_read(org_id="org", intent_id="v7-receipt", readback=readback, attestation=attestation, attestation_id="read-1")
    assert result.intent_id == "v7-receipt"


@pytest.mark.parametrize("field", ("content_digest", "receipt_payload_digest", "profile_digest", "session_digest", "observed_revision", "generation", "enforcement_id", "enforcement_state", "signature"))
def test_v8_terminal_mismatch_is_bound_zero(tmp_path: Path, field: str) -> None:
    path = tmp_path / f"{field}.sqlite"
    capability, session, _control, readback, _receipt = _ready(path)
    value: object = "bad" if field != "generation" else 9
    if field == "signature":
        value = "bad"
    with pytest.raises(BoundV8Denied):
        SourceBindingTerminalizerV8(path, capability, session).finalize(org_id="org", intent_id="v7-receipt", readback=_tamper(readback, field, value), audit_id="audit", outbox_id="outbox")
    connection = sqlite3.connect(path)
    assert connection.execute("SELECT count(*) FROM source_binding_v8_terminals").fetchone() == (0,)
    connection.close()


def test_v8_read_denies_stale_attestation_kill_session_close_and_authority_close(tmp_path: Path) -> None:
    path = tmp_path / "deny.sqlite"
    capability, session, control, readback, _receipt, _result = _bound(path)
    connection = sqlite3.connect(path)
    enforcement_digest = connection.execute("SELECT enforcement_digest FROM source_binding_v8_terminals").fetchone()[0]
    connection.close()
    gate = SourceReadGateV8(path, capability, session)
    stale = mint_serving_attestation_v8(session=session, readback=readback, enforcement_digest=enforcement_digest, ttl=timedelta(seconds=-1))
    with pytest.raises(BoundV8Denied):
        gate.authorize_read(org_id="org", intent_id="v7-receipt", readback=readback, attestation=stale, attestation_id="stale")
    live = mint_serving_attestation_v8(session=session, readback=readback, enforcement_digest=enforcement_digest)
    now = datetime.now(UTC)
    at = now.replace(microsecond=now.microsecond // 1000 * 1000)
    connection = sqlite3.connect(path)
    arm_kill_switch_v8(connection, org_id="org", source_ref="source:trusted", kill_id="kill", reason_digest="k" * 64, at=at)
    connection.commit()
    connection.close()
    with pytest.raises(BoundV8Denied):
        gate.authorize_read(org_id="org", intent_id="v7-receipt", readback=readback, attestation=live, attestation_id="kill")
    control.revoke("fake")
    with pytest.raises(Exception):
        gate.authorize_read(org_id="org", intent_id="v7-receipt", readback=readback, attestation=live, attestation_id="revoked")


@pytest.mark.parametrize("point", ("after_enforcement", "after_terminal", "after_audit", "after_outbox"))
def test_v8_terminal_fault_rolls_back_the_entire_graph(tmp_path: Path, point: str) -> None:
    path = tmp_path / f"{point}.sqlite"
    capability, session, _control, readback, _receipt = _ready(path)
    def fault(actual: str) -> None:
        if actual == point:
            raise RuntimeError(point)
    with pytest.raises(RuntimeError):
        SourceBindingTerminalizerV8(path, capability, session, fault_injector=fault).finalize(org_id="org", intent_id="v7-receipt", readback=readback, audit_id="audit", outbox_id="outbox")
    c = sqlite3.connect(path)
    assert tuple(c.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("source_binding_v8_enforcement", "source_binding_v8_terminals", "source_binding_v8_audit", "source_binding_v8_outbox")) == (0, 0, 0, 0)
    c.close()


def test_v8_32_same_terminal_converges_and_different_terminal_is_denied(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite"
    capability, session, _control, readback, _receipt = _ready(path)
    def run(_: int) -> object:
        try:
            return SourceBindingTerminalizerV8(path, capability, session).finalize(org_id="org", intent_id="v7-receipt", readback=readback, audit_id="audit", outbox_id="outbox")
        except BoundV8Denied:
            return None
    with ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(run, range(32)))
    assert sum(result is not None for result in results) == 1
    with pytest.raises(BoundV8Denied):
        SourceBindingTerminalizerV8(path, capability, session).finalize(org_id="org", intent_id="v7-receipt", readback=readback, audit_id="different-audit", outbox_id="different-outbox")


def test_v8_v7_only_cannot_authorize_a_read_and_catalog_tamper_is_denied(tmp_path: Path) -> None:
    path = tmp_path / "spoof.sqlite"
    capability, session, _control, readback, _receipt = _ready(path)
    attestation = mint_serving_attestation_v8(session=session, readback=readback, enforcement_digest="e" * 64)
    with pytest.raises(BoundV8Denied):
        SourceReadGateV8(path, capability, session).authorize_read(org_id="org", intent_id="v7-receipt", readback=readback, attestation=attestation, attestation_id="spoof")
    c = sqlite3.connect(path)
    c.execute("DROP TRIGGER source_binding_v8_terminals_update")
    c.commit()
    c.close()
    with pytest.raises(BoundV8Denied):
        SourceBindingTerminalizerV8(path, capability, session).finalize(org_id="org", intent_id="v7-receipt", readback=readback, audit_id="audit", outbox_id="outbox")


def test_v8_trigger_restored_coordinated_terminal_graph_tamper_is_denied(tmp_path: Path) -> None:
    path = tmp_path / "graph.sqlite"
    capability, session, _control, readback, _receipt, _result = _bound(path)
    c = sqlite3.connect(path)
    c.execute("DROP TRIGGER source_binding_v8_terminals_update")
    c.execute("UPDATE source_binding_v8_terminals SET terminal_digest='f' || substr(terminal_digest,2)")
    c.execute(v8_impl._T["source_binding_v8_terminals_update"])
    c.commit()
    c.close()
    attestation = mint_serving_attestation_v8(session=session, readback=readback, enforcement_digest="e" * 64)
    with pytest.raises(BoundV8Denied):
        SourceReadGateV8(path, capability, session).authorize_read(org_id="org", intent_id="v7-receipt", readback=readback, attestation=attestation, attestation_id="tamper")
