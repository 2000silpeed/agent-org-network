"""Owner composition stays unavailable until an active binding is injected."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from agent_org_network.owner_composition import (
    OwnerInstallationConfigurationError,
    OwnerActiveBindingReadiness,
    OwnerPairingReadiness,
    compose_owner,
    load_owner_installation_config,
    owner_doctor,
)


def _profile(tmp_path: Path) -> Path:
    path = tmp_path / "owner.json"
    path.write_text(
        "{"
        f'"profile":"local-reference","database_path":"{tmp_path / "owner.sqlite3"}",'
        f'"workspace_directory":"{tmp_path / "workspace"}",'
        '"bind_host":"127.0.0.1","port":8012,'
        '"central_url":"https://central.example.test",'
        '"pairing_reference":"pairing-1"}',
        encoding="utf-8",
    )
    return path


class _Ready:
    def __init__(self, value: bool) -> None:
        self.value = value

    def ready(self, config: object) -> bool:
        del config
        return self.value


class _Loader:
    def __init__(self, bundle: object | None) -> None:
        self.bundle = bundle

    def load(self, key: str) -> object | None:
        return self.bundle if key == "pairing-1" else None


def test_profile_reference_alone_never_claims_owner_pairing(tmp_path: Path) -> None:
    config = load_owner_installation_config(_profile(tmp_path))
    assert not compose_owner(config).paired()
    assert not owner_doctor(config)


def test_readiness_seam_requires_workspace_schema_and_active_binding(tmp_path: Path) -> None:
    config = load_owner_installation_config(_profile(tmp_path))
    config.workspace_directory.mkdir()
    connection = sqlite3.connect(config.database_path)
    connection.execute(
        "CREATE TABLE aon_installation_schema (name TEXT PRIMARY KEY, version INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO aon_installation_schema(name,version) VALUES ('owner-installation',1)"
    )
    connection.commit()
    connection.close()

    assert owner_doctor(config, pairing_readiness=_Ready(True))
    assert compose_owner(config, pairing_readiness=_Ready(True)).paired()
    assert not owner_doctor(config, pairing_readiness=_Ready(False))


def test_active_binding_readiness_checks_origin_generation_expiry_and_pending(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    binding = SimpleNamespace(
        central_origin="https://central.example.test",
        device_key_thumbprint="thumbprint",
    )
    active = SimpleNamespace(
        credential_generation=4,
        expires_at="2030-01-01T00:10:00+00:00",
    )
    bundle = SimpleNamespace(
        binding=binding,
        binding_digest="a" * 64,
        device=SimpleNamespace(device_key_thumbprint="thumbprint"),
        active=active,
        pending=None,
        pairing_pending=None,
    )
    readiness = OwnerActiveBindingReadiness(loader=_Loader(bundle), clock=lambda: now)
    config = load_owner_installation_config(_profile(tmp_path))
    assert readiness.ready(config)
    other_profile = _profile(tmp_path)
    other_profile.write_text(
        other_profile.read_text(encoding="utf-8").replace(
            "https://central.example.test", "https://other.example.test"
        ),
        encoding="utf-8",
    )
    assert not readiness.ready(load_owner_installation_config(other_profile))
    assert not OwnerActiveBindingReadiness(
        loader=_Loader(bundle), clock=lambda: now + timedelta(minutes=11)
    ).ready(config)
    pending_bundle = SimpleNamespace(**{**bundle.__dict__, "pending": object()})
    assert not OwnerActiveBindingReadiness(
        loader=_Loader(pending_bundle),
        clock=lambda: now,
    ).ready(config)


def test_invalid_pairing_seam_is_rejected(tmp_path: Path) -> None:
    config = load_owner_installation_config(_profile(tmp_path))
    with pytest.raises(OwnerInstallationConfigurationError):
        compose_owner(config, pairing_readiness=object())  # type: ignore[arg-type]


def test_readiness_protocol_is_explicit() -> None:
    assert hasattr(OwnerPairingReadiness, "ready")
