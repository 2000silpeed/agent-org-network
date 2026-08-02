"""Fail-closed Card Owner installation composition.

The module has no Central administration, migration, or Authority surface.  A
future paired workspace implementation can extend this explicit boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import re
from collections.abc import Callable
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

OWNER_SCHEMA_NAME = "owner-installation"
OWNER_SCHEMA_VERSION = 1
_OPAQUE_REFERENCE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_PAIRING_CODE = re.compile(r"[A-Za-z0-9_-]{32,128}")
_THUMBPRINT = re.compile(r"[A-Za-z0-9_-]{40,64}")


class OwnerInstallationConfigurationError(ValueError):
    """An Owner installation profile is missing, malformed, or unsafe."""


class OwnerPairingReadiness(Protocol):
    """Read-only seam for an active, Central-issued Owner binding.

    Pair/redeem mutation stays outside this composition slice.  The default
    composition deliberately has no capability and therefore remains
    unavailable instead of treating a profile string as proof of pairing.
    """

    def ready(self, config: "OwnerInstallationConfig") -> bool: ...


class OwnerBundleLoader(Protocol):
    """Narrow read-only adapter over an Owner secret-bundle store."""

    def load(self, key: str) -> object | None: ...


class OwnerPairingRequest(BaseModel, frozen=True):
    """One-shot public pairing anchors plus an in-memory pairing code.

    The code is accepted only as ``SecretStr`` in process memory; profiles and
    Owner API responses never contain this value.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    intent_id: str
    pairing_code: SecretStr
    central_origin: str
    org_id: str
    owner_user_id: str
    agent_card_id: str
    agent_card_revision: int
    agent_card_digest: str
    device_key_thumbprint: str
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    pairing_expires_at: datetime
    redeem_idempotency_key: str

    @field_validator(
        "intent_id",
        "org_id",
        "owner_user_id",
        "agent_card_id",
        "issue_receipt_id",
        "redeem_idempotency_key",
    )
    @classmethod
    def _reference(cls, value: str) -> str:
        if _OPAQUE_REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded Owner pairing reference required")
        return value

    @field_validator("pairing_code")
    @classmethod
    def _pairing_code(cls, value: SecretStr) -> SecretStr:
        if _PAIRING_CODE.fullmatch(value.get_secret_value()) is None:
            raise ValueError("opaque pairing code required")
        return value

    @field_validator("central_origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        if not _https_origin(value):
            raise ValueError("Owner pairing Central origin must be HTTPS")
        return value

    @field_validator("agent_card_revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if value < 1:
            raise ValueError("positive Card revision required")
        return value

    @field_validator(
        "agent_card_digest", "pairing_intent_digest", "issue_receipt_digest"
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 digest required")
        return value

    @field_validator("device_key_thumbprint")
    @classmethod
    def _thumbprint(cls, value: str) -> str:
        if _THUMBPRINT.fullmatch(value) is None:
            raise ValueError("canonical device thumbprint required")
        return value

    @field_validator("pairing_expires_at", mode="before")
    @classmethod
    def _parse_expiry(cls, value: object) -> object:
        if type(value) is not str:
            return value
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value

    @field_validator("pairing_expires_at")
    @classmethod
    def _expiry(cls, value: datetime) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.microsecond != 0
        ):
            raise ValueError("canonical UTC second required")
        return value.astimezone(UTC)


class OwnerPairingResultProjection(BaseModel, frozen=True):
    """Secret-free result permitted to leave the Owner pairing boundary."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: Literal[
        "intent_created",
        "redeem_submitted",
        "credential_stored",
        "finalized",
        "recovered_unverified",
        "replayed",
    ]
    profile_id: str
    state: Literal["intent_issued", "redeem_submitted", "credential_stored"] | None
    binding_digest: str
    credential_id: str | None = None
    credential_generation: int | None = None
    credential_public_digest: str | None = None
    bundle_revision: int | None = None
    bundle_public_digest: str | None = None
    verification: Literal["paired", "recovered_unverified"] | None = None

    @field_validator("profile_id", "credential_id")
    @classmethod
    def _result_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _OPAQUE_REFERENCE.fullmatch(value) is None:
            raise ValueError("bounded Owner result reference required")
        return value

    @field_validator(
        "binding_digest", "credential_public_digest", "bundle_public_digest"
    )
    @classmethod
    def _result_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 digest required")
        return value

    @model_validator(mode="after")
    def _exact_shape(self) -> "OwnerPairingResultProjection":
        if self.kind in {"intent_created", "redeem_submitted", "credential_stored"}:
            expected_state = {
                "intent_created": "intent_issued",
                "redeem_submitted": "redeem_submitted",
                "credential_stored": "credential_stored",
            }[self.kind]
            if self.state != expected_state:
                raise ValueError("result action/state mismatch")
        credential_values = (
            self.credential_id,
            self.credential_generation,
            self.credential_public_digest,
            self.bundle_revision,
            self.bundle_public_digest,
        )
        if self.state == "credential_stored":
            if any(value is None for value in credential_values):
                raise ValueError("stored credential projection required")
        elif self.state is not None and any(value is not None for value in credential_values):
            raise ValueError("credential projection forbidden")
        if self.state is not None and self.verification is not None:
            raise ValueError("recovery result cannot be verified")
        if self.state is None:
            if any(value is None for value in credential_values):
                raise ValueError("profile credential projection required")
            if self.kind == "finalized" and self.verification != "paired":
                raise ValueError("finalized verification required")
            if (
                self.kind == "recovered_unverified"
                and self.verification != "recovered_unverified"
            ):
                raise ValueError("recovery verification required")
            if self.kind not in {"finalized", "recovered_unverified", "replayed"}:
                raise ValueError("profile result kind required")
        if self.credential_generation is not None and self.credential_generation <= 0:
            raise ValueError("positive credential generation required")
        if self.bundle_revision is not None and self.bundle_revision <= 0:
            raise ValueError("positive bundle revision required")
        return self


class OwnerPairingAdapter(Protocol):
    """Injected adapter that owns concrete keychain/recovery orchestration."""

    def pair(
        self,
        config: "OwnerInstallationConfig",
        request: OwnerPairingRequest,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class OwnerInstallationConfig:
    profile: Literal["local-reference", "production"]
    database_path: Path
    workspace_directory: Path
    bind_host: str
    port: int
    central_url: str | None = None
    pairing_reference: str | None = None
    secret_store_directory: Path | None = None


@dataclass(frozen=True, slots=True)
class OwnerActiveBindingReadiness:
    """Validate a loaded active Owner binding without importing a keychain.

    The store implementation remains an installation concern. This adapter
    only consumes its already-validated public shape and never returns or
    serializes credential material.
    """

    loader: OwnerBundleLoader
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    def ready(self, config: OwnerInstallationConfig) -> bool:
        key = config.pairing_reference
        if key is None or config.central_url is None:
            return False
        try:
            bundle = self.loader.load(key)
            if bundle is None:
                return False
            binding = getattr(bundle, "binding", None)
            active = getattr(bundle, "active", None)
            if (
                getattr(binding, "central_origin", None) != config.central_url
                or not _DIGEST.fullmatch(str(getattr(bundle, "binding_digest", "")))
                or active is None
                or getattr(bundle, "pending", None) is not None
                or getattr(bundle, "pairing_pending", None) is not None
                or getattr(binding, "device_key_thumbprint", None)
                != getattr(getattr(bundle, "device", None), "device_key_thumbprint", None)
            ):
                return False
            generation = getattr(active, "credential_generation", None)
            expires_at = getattr(active, "expires_at", None)
            if type(generation) is not int or generation <= 0 or type(expires_at) is not str:
                return False
            expiry = datetime.fromisoformat(expires_at)
            now = self.clock()
            if (
                expiry.tzinfo is None
                or expiry.utcoffset() != timedelta(0)
                or now.tzinfo is None
                or now.utcoffset() != timedelta(0)
                or expiry <= now
            ):
                return False
            return True
        except Exception:
            return False


@dataclass(frozen=True, slots=True)
class OwnerComposition:
    config: OwnerInstallationConfig
    pairing_readiness: OwnerPairingReadiness | None = None
    pairing_adapter: OwnerPairingAdapter | None = None

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
        "secret_store_directory",
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
    secret_store_directory = (
        _absolute_path(values, "secret_store_directory")
        if "secret_store_directory" in values
        else None
    )
    if central_url is not None and not _https_origin(central_url):
        raise OwnerInstallationConfigurationError("Owner Central URL must be an HTTPS origin")
    if pairing_reference is not None and _OPAQUE_REFERENCE.fullmatch(pairing_reference) is None:
        raise OwnerInstallationConfigurationError("invalid Owner pairing reference")
    if profile == "production" and (
        central_url is None
        or pairing_reference is None
        or secret_store_directory is None
    ):
        raise OwnerInstallationConfigurationError("production Owner settings required")
    return OwnerInstallationConfig(
        profile=cast(Literal["local-reference", "production"], profile),
        database_path=database_path,
        workspace_directory=workspace_directory,
        bind_host=bind_host,
        port=port,
        central_url=central_url,
        pairing_reference=pairing_reference,
        secret_store_directory=secret_store_directory,
    )


def compose_owner(
    config: OwnerInstallationConfig,
    *,
    pairing_readiness: OwnerPairingReadiness | None = None,
    pairing_adapter: OwnerPairingAdapter | None = None,
) -> OwnerComposition:
    if type(config) is not OwnerInstallationConfig:
        raise OwnerInstallationConfigurationError("validated Owner config required")
    if pairing_readiness is not None and not callable(
        getattr(pairing_readiness, "ready", None)
    ):
        raise OwnerInstallationConfigurationError("validated Owner pairing seam required")
    if pairing_adapter is not None and not callable(
        getattr(pairing_adapter, "pair", None)
    ):
        raise OwnerInstallationConfigurationError("validated Owner pairing adapter required")
    return OwnerComposition(
        config=config,
        pairing_readiness=pairing_readiness,
        pairing_adapter=pairing_adapter,
    )


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
