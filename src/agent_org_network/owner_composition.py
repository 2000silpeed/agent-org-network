"""Fail-closed Card Owner installation composition.

The module has no Central administration, migration, or Authority surface.  A
future paired workspace implementation can extend this explicit boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import re
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

OWNER_SCHEMA_NAME = "owner-installation"
OWNER_SCHEMA_VERSION = 1
_OPAQUE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class OwnerInstallationConfigurationError(ValueError):
    """An Owner installation profile is missing, malformed, or unsafe."""


class OwnerPairingReadiness(Protocol):
    """Read-only seam for an active, Central-issued Owner binding.

    Pair/redeem mutation stays outside this composition slice.  The default
    composition deliberately has no capability and therefore remains
    unavailable instead of treating a profile string as proof of pairing.
    """

    def ready(self, config: "OwnerInstallationConfig") -> bool: ...


@dataclass(frozen=True, slots=True)
class OwnerInstallationConfig:
    profile: Literal["local-reference", "production"]
    database_path: Path
    workspace_directory: Path
    bind_host: str
    port: int
    central_url: str | None = None
    pairing_reference: str | None = None


@dataclass(frozen=True, slots=True)
class OwnerComposition:
    config: OwnerInstallationConfig
    pairing_readiness: OwnerPairingReadiness | None = None

    def schema_ready(self) -> bool:
        return _sqlite_schema_ready(
            self.config.database_path, OWNER_SCHEMA_NAME, OWNER_SCHEMA_VERSION
        )

    def paired(self) -> bool:
        if self.pairing_readiness is None:
            return False
        try:
            return self.config.pairing_reference is not None and bool(
                self.pairing_readiness.ready(self.config)
            )
        except Exception:
            return False


def load_owner_installation_config(profile_path: Path) -> OwnerInstallationConfig:
    values = _read_profile(profile_path)
    allowed = {
        "profile",
        "database_path",
        "workspace_directory",
        "bind_host",
        "port",
        "central_url",
        "pairing_reference",
    }
    if set(values) - allowed:
        raise OwnerInstallationConfigurationError("unknown Owner profile setting")
    profile = _required_string(values, "profile")
    if profile not in {"local-reference", "production"}:
        raise OwnerInstallationConfigurationError("unknown Owner profile")
    database_path = _absolute_path(values, "database_path")
    workspace_directory = _absolute_path(values, "workspace_directory")
    bind_host = _required_string(values, "bind_host")
    port = values.get("port")
    if bind_host != "127.0.0.1" or type(port) is not int or not 1 <= port <= 65535:
        raise OwnerInstallationConfigurationError("unsafe Owner API bind")
    central_url = _optional_string(values, "central_url")
    pairing_reference = _optional_string(values, "pairing_reference")
    if central_url is not None and not _https_origin(central_url):
        raise OwnerInstallationConfigurationError("Owner Central URL must be an HTTPS origin")
    if pairing_reference is not None and _OPAQUE_REFERENCE.fullmatch(pairing_reference) is None:
        raise OwnerInstallationConfigurationError("invalid Owner pairing reference")
    if profile == "production" and (central_url is None or pairing_reference is None):
        raise OwnerInstallationConfigurationError("production Owner settings required")
    return OwnerInstallationConfig(
        profile=cast(Literal["local-reference", "production"], profile),
        database_path=database_path,
        workspace_directory=workspace_directory,
        bind_host=bind_host,
        port=port,
        central_url=central_url,
        pairing_reference=pairing_reference,
    )


def compose_owner(
    config: OwnerInstallationConfig,
    *,
    pairing_readiness: OwnerPairingReadiness | None = None,
) -> OwnerComposition:
    if type(config) is not OwnerInstallationConfig:
        raise OwnerInstallationConfigurationError("validated Owner config required")
    if pairing_readiness is not None and not callable(
        getattr(pairing_readiness, "ready", None)
    ):
        raise OwnerInstallationConfigurationError("validated Owner pairing seam required")
    return OwnerComposition(config=config, pairing_readiness=pairing_readiness)


def owner_doctor(
    config: OwnerInstallationConfig,
    *,
    pairing_readiness: OwnerPairingReadiness | None = None,
) -> bool:
    """Read-only verification; it never creates an unpaired workspace or DB."""
    if type(config) is not OwnerInstallationConfig:
        return False
    if pairing_readiness is None or not callable(
        getattr(pairing_readiness, "ready", None)
    ):
        return False
    try:
        paired = bool(pairing_readiness.ready(config))
    except Exception:
        return False
    return paired and (
        config.workspace_directory.is_dir()
        and config.database_path.is_file()
        and _sqlite_schema_ready(config.database_path, OWNER_SCHEMA_NAME, OWNER_SCHEMA_VERSION)
    )


def _sqlite_schema_ready(database_path: Path, name: str, version: int) -> bool:
    """Read the Owner-local marker only; this slice never initializes it."""
    if not database_path.is_file():
        return False
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        row = connection.execute(
            "SELECT version FROM aon_installation_schema WHERE name = ?", (name,)
        ).fetchone()
        return row is not None and row[0] == version
    except sqlite3.Error:
        return False
    finally:
        if connection is not None:
            connection.close()


def _read_profile(profile_path: Path) -> dict[str, object]:
    if not profile_path.is_file():
        raise OwnerInstallationConfigurationError("explicit Owner profile path required")
    try:
        loaded: object = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OwnerInstallationConfigurationError("invalid Owner profile") from error
    if not isinstance(loaded, dict):
        raise OwnerInstallationConfigurationError("invalid Owner profile")
    result: dict[str, object] = {}
    for key, value in cast(dict[object, object], loaded).items():
        if type(key) is not str:
            raise OwnerInstallationConfigurationError("invalid Owner profile")
        result[key] = value
    return result


def _required_string(values: dict[str, object], name: str) -> str:
    value = values.get(name)
    if type(value) is not str or not value.strip():
        raise OwnerInstallationConfigurationError("required Owner setting missing")
    return value


def _optional_string(values: dict[str, object], name: str) -> str | None:
    if name not in values:
        return None
    return _required_string(values, name)


def _absolute_path(values: dict[str, object], name: str) -> Path:
    path = Path(_required_string(values, name))
    if not path.is_absolute():
        raise OwnerInstallationConfigurationError("absolute Owner path required")
    return path


def _https_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.path
        and not parsed.query
        and not parsed.fragment
        and value == f"https://{parsed.netloc}"
    )
