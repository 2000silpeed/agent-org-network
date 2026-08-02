"""Durable owner-local pairing recovery state machine."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
from threading import RLock
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_org_network.owner_device_key_store import (
    OwnerInstallationPublicBindingV1,
    OwnerInstallationSecretBundleV1,
    OwnerDeviceKeyStoreConflict,
    binding_digest,
    bundle_public_digest,
    credential_public_digest_from_projection,
    credential_public_projection,
    owner_keychain_account_ref,
    owner_profile_id,
)
from agent_org_network.production_owner_device_key_store import (
    ProductionOwnerDeviceKeyStore,
)


class OwnerPairingRecoveryUnavailable(Exception):
    pass


class OwnerPairingRecoveryConflict(OwnerPairingRecoveryUnavailable):
    pass


class LegacyOwnerPairingRequiresRepair(OwnerPairingRecoveryUnavailable):
    pass


_DIGEST = re.compile(r"[0-9a-f]{64}")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def _validate_ref(value: str) -> str:
    if type(value) is not str or _REF.fullmatch(value) is None:
        raise ValueError("bounded reference required")
    return value


class _Command(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str
    now: datetime
    idempotency_key: str

    @field_validator("profile_id", "idempotency_key")
    @classmethod
    def _ref(cls, value: str) -> str:
        return _validate_ref(value)

    @field_validator("now")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.microsecond != 0
        ):
            raise ValueError("canonical UTC second required")
        return value


class CreateIntentRecovery(_Command, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    binding: OwnerInstallationPublicBindingV1
    pairing_intent_id: str
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    pairing_expires_at: datetime

    @field_validator("pairing_intent_id", "issue_receipt_id")
    @classmethod
    def _refs(cls, value: str) -> str:
        return _validate_ref(value)

    @field_validator("pairing_intent_digest", "issue_receipt_digest")
    @classmethod
    def _digests(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("pairing_expires_at")
    @classmethod
    def _expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("UTC instant required")
        return value


class MarkRedeemSubmitted(_Command, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    expected_state: Literal["intent_issued"]
    expected_updated_at: datetime
    redeem_idempotency_key: str
    redeem_command_digest: str

    @field_validator("expected_updated_at")
    @classmethod
    def _expected_utc(cls, value: datetime) -> datetime:
        return _Command._utc(value)

    @field_validator("redeem_idempotency_key")
    @classmethod
    def _redeem_ref(cls, value: str) -> str:
        return _validate_ref(value)

    @field_validator("redeem_command_digest")
    @classmethod
    def _redeem_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class MarkCredentialStored(_Command, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    expected_state: Literal["redeem_submitted"]
    expected_updated_at: datetime
    credential_id: str
    credential_generation: int = Field(gt=0)
    credential_public_digest: str
    bundle_revision: int = Field(gt=0)
    bundle_public_digest: str

    @field_validator("expected_updated_at")
    @classmethod
    def _expected_utc(cls, value: datetime) -> datetime:
        return _Command._utc(value)

    @field_validator("credential_id")
    @classmethod
    def _credential_ref(cls, value: str) -> str:
        return _validate_ref(value)

    @field_validator("credential_public_digest", "bundle_public_digest")
    @classmethod
    def _public_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class FinalizeFromStoredCredentialCommand(_Command, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    expected_state: Literal["credential_stored"]
    expected_updated_at: datetime
    expected_central_origin: str
    expected_bundle_public_digest: str

    @field_validator("expected_updated_at")
    @classmethod
    def _expected_utc(cls, value: datetime) -> datetime:
        return _Command._utc(value)

    @field_validator("expected_bundle_public_digest")
    @classmethod
    def _bundle_digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class RecoverFromKeychainCommand(_Command, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    expected_central_origin: str


def validate_finalized_owner_bundle_delta(
    stored: OwnerInstallationSecretBundleV1,
    finalized: OwnerInstallationSecretBundleV1,
) -> None:
    if (
        type(stored) is not OwnerInstallationSecretBundleV1
        or type(finalized) is not OwnerInstallationSecretBundleV1
        or stored.pairing_pending is None
        or stored.active is None
        or stored.pending is not None
        or finalized.bundle_revision != stored.bundle_revision + 1
        or finalized.binding != stored.binding
        or finalized.binding_digest != stored.binding_digest
        or finalized.device != stored.device
        or finalized.pairing_pending is not None
        or finalized.active != stored.active
        or finalized.pending is not None
        or bundle_public_digest(finalized) == bundle_public_digest(stored)
    ):
        raise OwnerPairingRecoveryUnavailable()


class OwnerPairingRecoveryResult(BaseModel, frozen=True):
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

    @field_validator("profile_id")
    @classmethod
    def _profile_ref(cls, value: str) -> str:
        return _validate_ref(value)

    @field_validator("credential_id")
    @classmethod
    def _optional_credential_ref(cls, value: str | None) -> str | None:
        return _validate_ref(value) if value is not None else None

    @field_validator(
        "binding_digest",
        "credential_public_digest",
        "bundle_public_digest",
    )
    @classmethod
    def _result_digest(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @model_validator(mode="after")
    def _exact_shape(self) -> "OwnerPairingRecoveryResult":
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


class OwnerPairingRecoverySnapshot(BaseModel, frozen=True):
    """Read-only durable state needed to resume a pairing attempt after restart."""

    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str
    state: Literal["intent_issued", "redeem_submitted", "credential_stored"]
    central_origin: str
    org_id: str
    owner_user_id: str
    agent_card_id: str
    agent_card_revision: int = Field(gt=0)
    agent_card_digest: str
    device_key_thumbprint: str
    binding_digest: str
    pairing_intent_id: str
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    pairing_expires_at: datetime
    redeem_idempotency_key: str | None = None
    redeem_command_digest: str | None = None
    credential_id: str | None = None
    credential_generation: int | None = Field(default=None, gt=0)
    credential_public_digest: str | None = None
    bundle_revision: int | None = Field(default=None, gt=0)
    bundle_public_digest: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator(
        "profile_id", "org_id", "owner_user_id", "agent_card_id",
        "pairing_intent_id", "issue_receipt_id",
    )
    @classmethod
    def _references(cls, value: str) -> str:
        return _validate_ref(value)

    @field_validator("agent_card_digest", "binding_digest", "pairing_intent_digest", "issue_receipt_digest", "redeem_command_digest", "credential_public_digest", "bundle_public_digest")
    @classmethod
    def _digests(cls, value: str | None) -> str | None:
        if value is not None and _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("device_key_thumbprint")
    @classmethod
    def _thumbprint(cls, value: str) -> str:
        if type(value) is not str or len(value) != 43:
            raise ValueError("canonical device thumbprint required")
        return value

    @field_validator("pairing_expires_at", "created_at", "updated_at")
    @classmethod
    def _canonical_instant(cls, value: datetime) -> datetime:
        if (
            value.tzinfo is None
            or value.utcoffset() != timedelta(0)
            or value.microsecond != 0
        ):
            raise ValueError("canonical UTC second required")
        return value

    @model_validator(mode="after")
    def _exact_shape(self) -> "OwnerPairingRecoverySnapshot":
        binding = OwnerInstallationPublicBindingV1(
            central_origin=self.central_origin,
            org_id=self.org_id,
            owner_user_id=self.owner_user_id,
            agent_card_id=self.agent_card_id,
            agent_card_revision=self.agent_card_revision,
            agent_card_digest=self.agent_card_digest,
            device_key_thumbprint=self.device_key_thumbprint,
        )
        if binding_digest(binding) != self.binding_digest:
            raise ValueError("binding digest mismatch")
        if self.pairing_expires_at <= self.created_at or self.updated_at < self.created_at:
            raise ValueError("invalid recovery timestamps")
        redeem_values = (self.redeem_idempotency_key, self.redeem_command_digest)
        if self.state == "intent_issued":
            if any(value is not None for value in redeem_values):
                raise ValueError("redeem projection forbidden")
        elif any(value is None for value in redeem_values):
            raise ValueError("redeem projection required")
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
        elif any(value is not None for value in credential_values):
            raise ValueError("credential projection forbidden")
        if self.credential_id is not None:
            _validate_ref(self.credential_id)
        return self


_SCHEMA = """
CREATE TABLE owner_pairing_recoveries (
 profile_id TEXT PRIMARY KEY,state TEXT NOT NULL,
 central_origin TEXT NOT NULL,org_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,
 agent_card_id TEXT NOT NULL,agent_card_revision INTEGER NOT NULL,
 agent_card_digest TEXT NOT NULL,device_key_thumbprint TEXT NOT NULL,
 binding_digest TEXT NOT NULL,pairing_intent_id TEXT NOT NULL UNIQUE,
 pairing_intent_digest TEXT NOT NULL,issue_receipt_id TEXT NOT NULL,
 issue_receipt_digest TEXT NOT NULL,pairing_expires_at TEXT NOT NULL,
 redeem_idempotency_key TEXT,redeem_command_digest TEXT,
 credential_id TEXT,credential_generation INTEGER,credential_public_digest TEXT,
 bundle_revision INTEGER,bundle_public_digest TEXT,created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 CHECK(state IN ('intent_issued','redeem_submitted','credential_stored')),
 CHECK(agent_card_revision>0),CHECK(length(agent_card_digest)=64),
 CHECK(length(binding_digest)=64),CHECK(length(pairing_intent_digest)=64),
 CHECK(length(issue_receipt_digest)=64),
 CHECK((state='intent_issued' AND redeem_idempotency_key IS NULL AND redeem_command_digest IS NULL
   AND credential_id IS NULL AND credential_generation IS NULL AND credential_public_digest IS NULL
   AND bundle_revision IS NULL AND bundle_public_digest IS NULL)
  OR (state='redeem_submitted' AND redeem_idempotency_key IS NOT NULL AND redeem_command_digest IS NOT NULL
   AND credential_id IS NULL AND credential_generation IS NULL AND credential_public_digest IS NULL
   AND bundle_revision IS NULL AND bundle_public_digest IS NULL)
  OR (state='credential_stored' AND redeem_idempotency_key IS NOT NULL AND redeem_command_digest IS NOT NULL
   AND credential_id IS NOT NULL AND credential_generation>0 AND credential_public_digest IS NOT NULL
   AND bundle_revision>0 AND bundle_public_digest IS NOT NULL))
) STRICT;
CREATE TABLE owner_installation_profiles (
 profile_id TEXT PRIMARY KEY,central_origin TEXT NOT NULL,keychain_account_ref TEXT NOT NULL UNIQUE,
 org_id TEXT NOT NULL,owner_user_id TEXT NOT NULL,agent_card_id TEXT NOT NULL,
 agent_card_revision INTEGER NOT NULL,agent_card_digest TEXT NOT NULL,
 device_key_thumbprint TEXT NOT NULL,binding_digest TEXT NOT NULL,
 credential_id TEXT NOT NULL,credential_generation INTEGER NOT NULL,
 credential_public_digest TEXT NOT NULL,bundle_revision INTEGER NOT NULL,
 bundle_public_digest TEXT NOT NULL,issued_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 scope_json TEXT NOT NULL,slot_version INTEGER NOT NULL,envelope_suite TEXT NOT NULL,
 envelope_version INTEGER NOT NULL,aad_digest TEXT NOT NULL,envelope_digest TEXT NOT NULL,
 redeem_receipt_id TEXT NOT NULL,redeem_receipt_digest TEXT NOT NULL,
 verification TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(central_origin,org_id,owner_user_id,agent_card_id,device_key_thumbprint),
 CHECK(agent_card_revision>0),CHECK(length(agent_card_digest)=64),
 CHECK(length(binding_digest)=64),CHECK(length(credential_public_digest)=64),
 CHECK(length(bundle_public_digest)=64),
 CHECK(slot_version=1),CHECK(envelope_version=1),
 CHECK(length(aad_digest)=64),CHECK(length(envelope_digest)=64),
 CHECK(length(redeem_receipt_digest)=64),
 CHECK(credential_generation>0),CHECK(bundle_revision>0),
 CHECK(verification IN ('paired','recovered_unverified'))
) STRICT;
CREATE TABLE owner_pairing_recovery_receipts (
 idempotency_key TEXT PRIMARY KEY,action TEXT NOT NULL,command_digest TEXT NOT NULL,
 command_json TEXT NOT NULL,resource_digest TEXT NOT NULL,resource_json TEXT NOT NULL,
 result_json TEXT NOT NULL,row_created_at TEXT NOT NULL,row_updated_at TEXT NOT NULL,
 created_at TEXT NOT NULL,receipt_digest TEXT NOT NULL,
 CHECK(action IN ('intent.create','redeem.mark_submitted','credential.mark_stored',
 'pairing.finalize','profile.recover')),
 CHECK(length(command_digest)=64),CHECK(length(resource_digest)=64),
 CHECK(length(receipt_digest)=64)
) STRICT;
CREATE TRIGGER owner_pairing_recoveries_no_delete BEFORE DELETE ON owner_pairing_recoveries
WHEN NOT EXISTS (
 SELECT 1 FROM owner_installation_profiles AS profile
 WHERE profile.profile_id=OLD.profile_id
 AND profile.central_origin=OLD.central_origin
 AND profile.org_id=OLD.org_id
 AND profile.owner_user_id=OLD.owner_user_id
 AND profile.agent_card_id=OLD.agent_card_id
 AND profile.agent_card_revision=OLD.agent_card_revision
 AND profile.agent_card_digest=OLD.agent_card_digest
 AND profile.device_key_thumbprint=OLD.device_key_thumbprint
 AND profile.binding_digest=OLD.binding_digest
 AND profile.credential_id=OLD.credential_id
 AND profile.credential_generation=OLD.credential_generation
 AND profile.credential_public_digest=OLD.credential_public_digest
 AND profile.bundle_revision=OLD.bundle_revision+1
 AND profile.verification='paired'
)
BEGIN SELECT RAISE(ABORT,'invalid terminal delete'); END;
CREATE TRIGGER owner_pairing_recoveries_exact_update BEFORE UPDATE ON owner_pairing_recoveries
WHEN OLD.profile_id!=NEW.profile_id OR OLD.central_origin!=NEW.central_origin
 OR OLD.org_id!=NEW.org_id OR OLD.owner_user_id!=NEW.owner_user_id
 OR OLD.agent_card_id!=NEW.agent_card_id OR OLD.agent_card_revision!=NEW.agent_card_revision
 OR OLD.agent_card_digest!=NEW.agent_card_digest OR OLD.device_key_thumbprint!=NEW.device_key_thumbprint
 OR OLD.binding_digest!=NEW.binding_digest OR OLD.pairing_intent_id!=NEW.pairing_intent_id
 OR OLD.pairing_intent_digest!=NEW.pairing_intent_digest OR OLD.issue_receipt_id!=NEW.issue_receipt_id
 OR OLD.issue_receipt_digest!=NEW.issue_receipt_digest OR OLD.pairing_expires_at!=NEW.pairing_expires_at
 OR OLD.created_at!=NEW.created_at OR NEW.updated_at<=OLD.updated_at
 OR NOT ((OLD.state='intent_issued' AND NEW.state='redeem_submitted')
 OR (OLD.state='redeem_submitted' AND NEW.state='credential_stored'
   AND OLD.redeem_idempotency_key=NEW.redeem_idempotency_key
   AND OLD.redeem_command_digest=NEW.redeem_command_digest))
BEGIN SELECT RAISE(ABORT,'invalid transition'); END;
CREATE TRIGGER owner_pairing_recovery_receipts_no_update BEFORE UPDATE ON owner_pairing_recovery_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_pairing_recovery_receipts_no_delete BEFORE DELETE ON owner_pairing_recovery_receipts
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_installation_profiles_no_delete BEFORE DELETE ON owner_installation_profiles
BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_installation_profiles_no_update BEFORE UPDATE ON owner_installation_profiles
BEGIN SELECT RAISE(ABORT,'immutable'); END;
"""


def _jcs(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest(domain: bytes, value: object) -> str:
    return sha256(domain + _jcs(value)).hexdigest()


def _receipt_digest(
    *,
    idempotency_key: str,
    action: str,
    command_digest: str,
    resource_digest: str,
    result_json: str,
    created_at: str,
    row_created_at: str,
    row_updated_at: str,
) -> str:
    return _digest(
        b"aon.owner.local.receipt.v2\0",
        {
            "action": action,
            "command_digest": command_digest,
            "created_at": created_at,
            "idempotency_key": idempotency_key,
            "resource_digest": resource_digest,
            "result_digest": sha256(result_json.encode()).hexdigest(),
            "row_created_at": row_created_at,
            "row_updated_at": row_updated_at,
        },
    )


def _instant(value: datetime) -> str:
    if (
        value.tzinfo is None
        or value.utcoffset() != timedelta(0)
        or value.microsecond != 0
    ):
        raise OwnerPairingRecoveryUnavailable()
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: object) -> datetime:
    if type(value) is not str:
        raise OwnerPairingRecoveryUnavailable()
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        raise OwnerPairingRecoveryUnavailable() from error
    if _instant(parsed) != value:
        raise OwnerPairingRecoveryUnavailable()
    return parsed


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ))
    details: list[object] = []
    for table in ("owner_pairing_recoveries", "owner_installation_profiles", "owner_pairing_recovery_receipts"):
        details.extend((tuple(connection.execute(f"PRAGMA table_xinfo('{table}')")),
                        tuple(connection.execute(f"PRAGMA index_list('{table}')")),
                        tuple(connection.execute(f"PRAGMA foreign_key_list('{table}')")),
                        tuple(connection.execute(f"PRAGMA table_list('{table}')"))))
    return objects, *details


def _expected_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    connection.executescript(_SCHEMA)
    result = _catalog(connection)
    connection.close()
    return result


_EXPECTED = _expected_catalog()
_ResultKind = Literal[
    "intent_created",
    "redeem_submitted",
    "credential_stored",
    "finalized",
    "recovered_unverified",
    "replayed",
]
_RecoveryCommand = (
    CreateIntentRecovery
    | MarkRedeemSubmitted
    | MarkCredentialStored
    | FinalizeFromStoredCredentialCommand
    | RecoverFromKeychainCommand
)


def _no_fault(_point: str) -> None:
    return None


class OwnerPairingRecoveryStore:
    def __init__(
        self,
        path: str | Path,
        *,
        device_keys: ProductionOwnerDeviceKeyStore | None = None,
        clock: Callable[[], datetime] | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        if device_keys is not None and type(device_keys) is not ProductionOwnerDeviceKeyStore:
            raise OwnerPairingRecoveryUnavailable()
        self._path = Path(path)
        self._device_keys = device_keys
        self._clock = clock
        self._fault: Callable[[str], None] = fault or _no_fault
        self._lock = RLock()
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._path.parent.is_symlink() or self._path.parent.resolve() != self._path.parent.absolute():
                raise OwnerPairingRecoveryUnavailable()
            if self._path.exists() and (self._path.is_symlink() or not stat.S_ISREG(self._path.stat().st_mode)):
                raise OwnerPairingRecoveryUnavailable()
            if not self._path.exists():
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.close(descriptor)
            os.chmod(self._path, 0o600)
            info = self._path.stat()
            self._identity = (info.st_dev, info.st_ino)
            with sqlite3.connect(self._path) as connection:
                self._initialize(connection)
                self._validate(connection)
        except (OwnerPairingRecoveryUnavailable, LegacyOwnerPairingRequiresRepair):
            raise
        except Exception as error:
            raise OwnerPairingRecoveryUnavailable() from error

    def _initialize(self, connection: sqlite3.Connection) -> None:
        names = tuple(row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
        ))
        if not names:
            connection.executescript(_SCHEMA)
            return
        if "owner_pairing_sessions" in names:
            try:
                from agent_org_network.owner_pairing_session_store import (
                    _EXPECTED_CATALOG,  # pyright: ignore[reportPrivateUsage]
                    _catalog,  # pyright: ignore[reportPrivateUsage]
                )
                if _catalog(connection) != _EXPECTED_CATALOG:
                    raise OwnerPairingRecoveryUnavailable()
            except ImportError as error:
                raise OwnerPairingRecoveryUnavailable() from error
            if connection.execute("SELECT 1 FROM owner_pairing_sessions LIMIT 1").fetchone():
                raise LegacyOwnerPairingRequiresRepair()
            connection.executescript(
                "DROP TRIGGER owner_pairing_sessions_no_delete;"
                "DROP TRIGGER owner_pairing_sessions_exact_update;"
                "DROP INDEX owner_pairing_sessions_one_active_subject;"
                "DROP TABLE owner_pairing_sessions;"
                + _SCHEMA
            )

    def _validate_path(self) -> None:
        info = self._path.lstat()
        if (info.st_dev, info.st_ino) != self._identity or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise OwnerPairingRecoveryUnavailable()

    @staticmethod
    def _validate_catalog(connection: sqlite3.Connection) -> None:
        if _catalog(connection) != _EXPECTED:
            raise OwnerPairingRecoveryUnavailable()

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        OwnerPairingRecoveryStore._validate_catalog(connection)
        OwnerPairingRecoveryStore._validate_receipts(connection)

    @staticmethod
    def _decode_canonical(value: object) -> object:
        if type(value) is not str:
            raise OwnerPairingRecoveryUnavailable()
        decoded: object = json.loads(value)
        if _jcs(decoded).decode() != value:
            raise OwnerPairingRecoveryUnavailable()
        return decoded

    @staticmethod
    def _validate_action_projection(
        *,
        action: object,
        command_value: object,
        command_digest: object,
        resource_value: object,
        resource_digest: object,
        result_value: object,
    ) -> tuple[str, dict[str, object]]:
        domains = {
            "intent.create": b"aon.owner.local.intent-create.v2\0",
            "redeem.mark_submitted": b"aon.owner.local.redeem-submitted.v2\0",
            "credential.mark_stored": b"aon.owner.local.credential-stored.v2\0",
            "pairing.finalize": b"aon.owner.local.pairing-finalize.v2\0",
            "profile.recover": b"aon.owner.local.profile-recover.v2\0",
        }
        command_fields = {
            "intent.create": {
                "profile_id", "binding", "pairing_intent_id",
                "pairing_intent_digest", "issue_receipt_id",
                "issue_receipt_digest", "pairing_expires_at",
            },
            "redeem.mark_submitted": {
                "profile_id", "expected_state", "expected_updated_at",
                "redeem_idempotency_key", "redeem_command_digest",
            },
            "credential.mark_stored": {
                "profile_id", "expected_state", "expected_updated_at",
                "credential_id", "credential_generation",
                "credential_public_digest", "bundle_revision",
                "bundle_public_digest",
            },
            "pairing.finalize": {
                "profile_id", "expected_state", "expected_updated_at",
                "expected_central_origin", "expected_bundle_public_digest",
            },
            "profile.recover": {
                "profile_id", "expected_central_origin",
            },
        }
        post_state = {
            "intent.create": "intent_issued",
            "redeem.mark_submitted": "redeem_submitted",
            "credential.mark_stored": "credential_stored",
            "pairing.finalize": None,
            "profile.recover": None,
        }
        result_kind = {
            "intent.create": "intent_created",
            "redeem.mark_submitted": "redeem_submitted",
            "credential.mark_stored": "credential_stored",
            "pairing.finalize": "finalized",
            "profile.recover": "recovered_unverified",
        }
        if (
            type(action) is not str
            or action not in domains
            or type(command_value) is not dict
            or type(resource_value) is not dict
            or type(result_value) is not dict
            or type(command_digest) is not str
            or type(resource_digest) is not str
        ):
            raise OwnerPairingRecoveryUnavailable()
        command = cast(dict[str, object], command_value)
        resource = cast(dict[str, object], resource_value)
        _validate_ref(cast(str, resource.get("profile_id")))
        _validate_ref(cast(str, command.get("profile_id")))
        if action == "intent.create":
            for field in ("pairing_intent_id", "issue_receipt_id"):
                _validate_ref(cast(str, resource.get(field)))
                _validate_ref(cast(str, command.get(field)))
        elif action == "redeem.mark_submitted":
            _validate_ref(cast(str, resource.get("redeem_idempotency_key")))
            _validate_ref(cast(str, command.get("redeem_idempotency_key")))
        elif action == "credential.mark_stored":
            _validate_ref(cast(str, resource.get("credential_id")))
            _validate_ref(cast(str, command.get("credential_id")))
        else:
            for field in (
                "org_id", "owner_user_id", "agent_card_id", "credential_id",
                "redeem_receipt_id",
            ):
                _validate_ref(cast(str, resource.get(field)))
        if (
            set(command) != command_fields[action]
            or _digest(domains[action], command) != command_digest
            or _digest(b"aon.owner.local.recovery-row.v2\0", resource)
            != resource_digest
            or command.get("profile_id") != resource.get("profile_id")
            or resource.get("state") != post_state[action]
        ):
            raise OwnerPairingRecoveryUnavailable()
        if action == "intent.create":
            binding_value = command.get("binding")
            if type(binding_value) is not dict:
                raise OwnerPairingRecoveryUnavailable()
            binding = OwnerInstallationPublicBindingV1.model_validate(binding_value)
            binding_projection = binding.model_dump(mode="json")
            for field in (
                "central_origin", "org_id", "owner_user_id", "agent_card_id",
                "agent_card_revision", "agent_card_digest",
                "device_key_thumbprint",
            ):
                if resource.get(field) != binding_projection[field]:
                    raise OwnerPairingRecoveryUnavailable()
            if (
                resource.get("binding_digest") != binding_digest(binding)
                or resource.get("pairing_intent_id")
                != command.get("pairing_intent_id")
                or resource.get("pairing_intent_digest")
                != command.get("pairing_intent_digest")
                or resource.get("issue_receipt_id")
                != command.get("issue_receipt_id")
                or resource.get("issue_receipt_digest")
                != command.get("issue_receipt_digest")
                or resource.get("pairing_expires_at")
                != command.get("pairing_expires_at")
            ):
                raise OwnerPairingRecoveryUnavailable()
        elif action == "redeem.mark_submitted":
            if (
                command.get("expected_state") != "intent_issued"
                or resource.get("redeem_idempotency_key")
                != command.get("redeem_idempotency_key")
                or resource.get("redeem_command_digest")
                != command.get("redeem_command_digest")
            ):
                raise OwnerPairingRecoveryUnavailable()
        elif action == "credential.mark_stored" and (
            command.get("expected_state") != "redeem_submitted"
            or any(
                resource.get(field) != command.get(field)
                for field in (
                    "credential_id", "credential_generation",
                    "credential_public_digest", "bundle_revision",
                    "bundle_public_digest",
                )
            )
        ):
            raise OwnerPairingRecoveryUnavailable()
        elif action in {"pairing.finalize", "profile.recover"}:
            verification = (
                "paired" if action == "pairing.finalize" else "recovered_unverified"
            )
            profile_fields = {
                "profile_id", "central_origin", "keychain_account_ref", "org_id",
                "owner_user_id", "agent_card_id", "agent_card_revision",
                "agent_card_digest", "device_key_thumbprint", "binding_digest",
                "credential_id", "credential_generation",
                "credential_public_digest", "bundle_revision",
                "bundle_public_digest", "issued_at", "expires_at", "scope_json",
                "slot_version", "envelope_suite", "envelope_version",
                "aad_digest", "envelope_digest", "redeem_receipt_id",
                "redeem_receipt_digest", "verification",
            }
            if (
                set(resource) != profile_fields
                or
                resource.get("central_origin")
                != command.get("expected_central_origin")
                or resource.get("verification") != verification
                or resource.get("state") is not None
            ):
                raise OwnerPairingRecoveryUnavailable()
            if action == "pairing.finalize" and (
                command.get("expected_state") != "credential_stored"
            ):
                raise OwnerPairingRecoveryUnavailable()
        result = OwnerPairingRecoveryResult.model_validate(result_value)
        expected_result = {
            "kind": result_kind[action],
            "profile_id": resource.get("profile_id"),
            "state": resource.get("state"),
            "binding_digest": resource.get("binding_digest"),
            "credential_id": resource.get("credential_id"),
            "credential_generation": resource.get("credential_generation"),
            "credential_public_digest": resource.get("credential_public_digest"),
            "bundle_revision": resource.get("bundle_revision"),
            "bundle_public_digest": resource.get("bundle_public_digest"),
            "verification": resource.get("verification"),
        }
        if result.model_dump(mode="json") != expected_result:
            raise OwnerPairingRecoveryUnavailable()
        return result.profile_id, resource

    @staticmethod
    def _validate_receipts(connection: sqlite3.Connection) -> None:
        expected_actions = (
            "intent.create",
            "redeem.mark_submitted",
            "credential.mark_stored",
        )
        rank = {
            "intent_issued": 0,
            "redeem_submitted": 1,
            "credential_stored": 2,
        }
        chains: dict[
            str,
            list[
                tuple[
                    str,
                    dict[str, object],
                    dict[str, object],
                    str,
                    str,
                ]
            ],
        ] = {}
        terminals: dict[
            str, tuple[str, dict[str, object], dict[str, object], str, str]
        ] = {}
        rows = connection.execute(
            "SELECT idempotency_key,action,command_digest,command_json,"
            "resource_digest,resource_json,"
            "result_json,row_created_at,row_updated_at,created_at,receipt_digest "
            "FROM owner_pairing_recovery_receipts ORDER BY created_at,rowid"
        ).fetchall()
        for (
            idempotency_key, action, command_digest, command_json,
            resource_digest, resource_json,
            result_json, row_created_at, row_updated_at, receipt_created_at,
            receipt_digest,
        ) in rows:
            _validate_ref(cast(str, idempotency_key))
            command_value = OwnerPairingRecoveryStore._decode_canonical(command_json)
            resource_value = OwnerPairingRecoveryStore._decode_canonical(resource_json)
            result_value = OwnerPairingRecoveryStore._decode_canonical(result_json)
            profile_id, resource = OwnerPairingRecoveryStore._validate_action_projection(
                action=action, command_value=command_value,
                command_digest=command_digest, resource_value=resource_value,
                resource_digest=resource_digest, result_value=result_value,
            )
            if (
                type(command_value) is not dict
                or receipt_created_at != row_updated_at
                or receipt_digest != _receipt_digest(
                    idempotency_key=str(idempotency_key),
                    action=str(action),
                    command_digest=str(command_digest),
                    resource_digest=str(resource_digest),
                    result_json=str(result_json),
                    created_at=str(receipt_created_at),
                    row_created_at=str(row_created_at),
                    row_updated_at=str(row_updated_at),
                )
            ):
                raise OwnerPairingRecoveryUnavailable()
            _parse_instant(row_created_at)
            _parse_instant(row_updated_at)
            if action in {"pairing.finalize", "profile.recover"}:
                if (
                    profile_id in terminals
                    or row_created_at != row_updated_at
                ):
                    raise OwnerPairingRecoveryUnavailable()
                terminals[profile_id] = (
                    str(action), resource, cast(dict[str, object], command_value),
                    str(row_created_at), str(row_updated_at),
                )
                continue
            chains.setdefault(profile_id, []).append(
                (
                    str(action),
                    resource,
                    cast(dict[str, object], command_value),
                    str(row_created_at),
                    str(row_updated_at),
                )
            )
        for profile_id, chain in chains.items():
            if (
                tuple(item[0] for item in chain) != expected_actions[: len(chain)]
                or chain[0][3] != chain[0][4]
            ):
                raise OwnerPairingRecoveryUnavailable()
            for index in range(1, len(chain)):
                previous = chain[index - 1][1]
                current_snapshot = chain[index][1]
                previous_updated = chain[index - 1][4]
                current_command = chain[index][2]
                current_created = chain[index][3]
                current_updated = chain[index][4]
                allowed = (
                    {"state", "redeem_idempotency_key", "redeem_command_digest"}
                    if index == 1
                    else {
                        "state", "credential_id", "credential_generation",
                        "credential_public_digest", "bundle_revision",
                        "bundle_public_digest",
                    }
                )
                if any(
                    previous.get(key) != current_snapshot.get(key)
                    for key in set(previous) | set(current_snapshot)
                    if key not in allowed
                ):
                    raise OwnerPairingRecoveryUnavailable()
                if (
                    current_created != chain[0][3]
                    or current_command.get("expected_updated_at")
                    != previous_updated
                    or _parse_instant(current_updated)
                    <= _parse_instant(previous_updated)
                ):
                    raise OwnerPairingRecoveryUnavailable()
            terminal = terminals.get(profile_id)
            if terminal is not None:
                if (
                    terminal[0] != "pairing.finalize"
                    or len(chain) != len(expected_actions)
                    or terminal[2].get("expected_updated_at") != chain[-1][4]
                    or terminal[3] != terminal[4]
                    or _parse_instant(terminal[4])
                    <= _parse_instant(chain[-1][4])
                ):
                    raise OwnerPairingRecoveryUnavailable()
                continue
            current_row = connection.execute(
                "SELECT * FROM owner_pairing_recoveries WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            if current_row is None:
                raise OwnerPairingRecoveryUnavailable()
            columns = tuple(
                item[1]
                for item in connection.execute(
                    "PRAGMA table_xinfo(owner_pairing_recoveries)"
                ).fetchall()
            )
            current = {
                columns[index]: value
                for index, value in enumerate(current_row)
                if columns[index] not in {"created_at", "updated_at"}
            }
            if (
                current != chain[-1][1]
                or rank[str(current["state"])] != len(chain) - 1
                or current_row[-2] != chain[-1][3]
                or current_row[-1] != chain[-1][4]
            ):
                raise OwnerPairingRecoveryUnavailable()
        if any(
            action == "pairing.finalize" and profile_id not in chains
            for profile_id, (action, _resource, _command, _created, _updated)
            in terminals.items()
        ):
            raise OwnerPairingRecoveryUnavailable()
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT * FROM owner_pairing_recoveries ORDER BY profile_id"
            ).fetchall()
            for row in rows:
                for field in (
                    "profile_id", "org_id", "owner_user_id", "agent_card_id",
                    "pairing_intent_id", "issue_receipt_id",
                ):
                    _validate_ref(row[field])
                if row["redeem_idempotency_key"] is not None:
                    _validate_ref(row["redeem_idempotency_key"])
                if row["credential_id"] is not None:
                    _validate_ref(row["credential_id"])
                binding = OwnerInstallationPublicBindingV1(
                    central_origin=row["central_origin"],
                    org_id=row["org_id"],
                    owner_user_id=row["owner_user_id"],
                    agent_card_id=row["agent_card_id"],
                    agent_card_revision=row["agent_card_revision"],
                    agent_card_digest=row["agent_card_digest"],
                    device_key_thumbprint=row["device_key_thumbprint"],
                )
                if binding_digest(binding) != row["binding_digest"]:
                    raise OwnerPairingRecoveryUnavailable()
            profiles = connection.execute(
                "SELECT * FROM owner_installation_profiles ORDER BY profile_id"
            ).fetchall()
            profile_by_id = {row["profile_id"]: row for row in profiles}
            if set(profile_by_id) != set(terminals):
                raise OwnerPairingRecoveryUnavailable()
            for profile_id, row in profile_by_id.items():
                _validate_ref(profile_id)
                terminal = terminals[profile_id]
                binding = OwnerInstallationPublicBindingV1(
                    central_origin=row["central_origin"],
                    org_id=row["org_id"],
                    owner_user_id=row["owner_user_id"],
                    agent_card_id=row["agent_card_id"],
                    agent_card_revision=row["agent_card_revision"],
                    agent_card_digest=row["agent_card_digest"],
                    device_key_thumbprint=row["device_key_thumbprint"],
                )
                scope_value: object = json.loads(row["scope_json"])
                scope = (
                    cast(list[object], scope_value)
                    if type(scope_value) is list
                    else None
                )
                resource = {
                    key: row[key]
                    for key in row.keys()
                    if key not in {"created_at", "updated_at"}
                }
                credential_projection = {
                    "aad_digest": row["aad_digest"],
                    "credential_generation": row["credential_generation"],
                    "credential_id": row["credential_id"],
                    "envelope_digest": row["envelope_digest"],
                    "envelope_suite": row["envelope_suite"],
                    "envelope_version": row["envelope_version"],
                    "expires_at": row["expires_at"],
                    "issued_at": row["issued_at"],
                    "redeem_receipt_digest": row["redeem_receipt_digest"],
                    "redeem_receipt_id": row["redeem_receipt_id"],
                    "scope": scope,
                    "slot_version": row["slot_version"],
                }
                if (
                    resource != terminal[1]
                    or row["created_at"] != terminal[3]
                    or row["updated_at"] != terminal[4]
                    or binding_digest(binding) != row["binding_digest"]
                    or owner_keychain_account_ref(profile_id)
                    != row["keychain_account_ref"]
                    or scope is None
                    or any(type(action) is not str for action in scope)
                    or scope != sorted(set(cast(list[str], scope)))
                    or _jcs(scope).decode() != row["scope_json"]
                    or credential_public_digest_from_projection(
                        credential_projection
                    )
                    != row["credential_public_digest"]
                    or _parse_instant(row["issued_at"])
                    >= _parse_instant(row["expires_at"])
                ):
                    raise OwnerPairingRecoveryUnavailable()
            receipts = connection.execute(
                "SELECT resource_digest,result_json FROM "
                "owner_pairing_recovery_receipts ORDER BY idempotency_key"
            ).fetchall()
            by_profile = {row["profile_id"]: row for row in rows}
            for receipt in receipts:
                parsed = OwnerPairingRecoveryResult.model_validate_json(
                    receipt["result_json"]
                )
                if (
                    _jcs(parsed.model_dump(mode="json")).decode()
                    != receipt["result_json"]
                ):
                    raise OwnerPairingRecoveryUnavailable()
                current = by_profile.get(parsed.profile_id)
                if (
                    current is not None
                    and parsed.state == current["state"]
                    and _digest(
                        b"aon.owner.local.recovery-row.v2\0",
                        {
                            key: current[key]
                            for key in current.keys()
                            if key not in {"created_at", "updated_at"}
                        },
                    )
                    != receipt["resource_digest"]
                ):
                    raise OwnerPairingRecoveryUnavailable()
        finally:
            connection.row_factory = None

    @staticmethod
    def _command_digest(domain: bytes, command: BaseModel) -> str:
        return _digest(domain, OwnerPairingRecoveryStore._command_projection(command))

    @staticmethod
    def _command_projection(command: BaseModel) -> dict[str, object]:
        return command.model_dump(mode="json", exclude={"now", "idempotency_key"})

    @staticmethod
    def _resource(row: sqlite3.Row) -> dict[str, object]:
        return {key: row[key] for key in row.keys() if key not in {"created_at", "updated_at"}}

    def _result(self, row: sqlite3.Row, kind: _ResultKind) -> OwnerPairingRecoveryResult:
        return OwnerPairingRecoveryResult(
            kind=kind, profile_id=row["profile_id"], state=row["state"],
            binding_digest=row["binding_digest"], credential_id=row["credential_id"],
            credential_generation=row["credential_generation"],
            credential_public_digest=row["credential_public_digest"],
            bundle_revision=row["bundle_revision"], bundle_public_digest=row["bundle_public_digest"],
            verification=None,
        )

    def _replay(self, connection: sqlite3.Connection, command: _RecoveryCommand, action: str, command_digest: str) -> OwnerPairingRecoveryResult | None:
        receipt = connection.execute(
            "SELECT action,command_digest,command_json,resource_digest,resource_json,"
            "result_json,row_created_at,row_updated_at,created_at "
            "FROM owner_pairing_recovery_receipts WHERE idempotency_key=?",
            (command.idempotency_key,),
        ).fetchone()
        if receipt is None:
            return None
        if receipt[0] != action or receipt[1] != command_digest:
            raise OwnerPairingRecoveryConflict()
        if receipt[8] != receipt[7]:
            raise OwnerPairingRecoveryUnavailable()
        _parse_instant(receipt[6])
        _parse_instant(receipt[7])
        command_value = self._decode_canonical(receipt[2])
        if command_value != self._command_projection(command):
            raise OwnerPairingRecoveryConflict()
        resource_value = self._decode_canonical(receipt[4])
        result_value = self._decode_canonical(receipt[5])
        profile_id, resource = self._validate_action_projection(
            action=receipt[0], command_value=command_value,
            command_digest=receipt[1], resource_value=resource_value,
            resource_digest=receipt[3], result_value=result_value,
        )
        historical = OwnerPairingRecoveryResult.model_validate(result_value)
        if historical.state is None:
            row = connection.execute(
                "SELECT * FROM owner_installation_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            if row is None or self._resource(row) != resource:
                raise OwnerPairingRecoveryUnavailable()
            return historical.model_copy(update={"kind": "replayed"})
        row = connection.execute("SELECT * FROM owner_pairing_recoveries WHERE profile_id=?", (profile_id,)).fetchone()
        state_rank = {"intent_issued": 0, "redeem_submitted": 1, "credential_stored": 2}
        current = self._resource(row) if row is not None else None
        if (
            current is None
            or state_rank[str(current["state"])] < state_rank[str(resource["state"])]
        ):
            raise OwnerPairingRecoveryUnavailable()
        return historical.model_copy(update={"kind": "replayed"})

    def read_snapshot(self, profile_id: str) -> OwnerPairingRecoverySnapshot | None:
        """Return the current non-terminal recovery row for restart/resume orchestration."""
        if type(profile_id) is not str:
            raise OwnerPairingRecoveryUnavailable()
        try:
            _validate_ref(profile_id)
        except ValueError as error:
            raise OwnerPairingRecoveryUnavailable() from error
        with self._lock:
            try:
                self._validate_path()
                with sqlite3.connect(self._path, timeout=30) as connection:
                    self._validate(connection)
                    connection.row_factory = sqlite3.Row
                    row = connection.execute(
                        "SELECT * FROM owner_pairing_recoveries WHERE profile_id=?",
                        (profile_id,),
                    ).fetchone()
                    if row is None:
                        return None
                    return OwnerPairingRecoverySnapshot(
                        profile_id=row["profile_id"],
                        state=row["state"],
                        central_origin=row["central_origin"],
                        org_id=row["org_id"],
                        owner_user_id=row["owner_user_id"],
                        agent_card_id=row["agent_card_id"],
                        agent_card_revision=row["agent_card_revision"],
                        agent_card_digest=row["agent_card_digest"],
                        device_key_thumbprint=row["device_key_thumbprint"],
                        binding_digest=row["binding_digest"],
                        pairing_intent_id=row["pairing_intent_id"],
                        pairing_intent_digest=row["pairing_intent_digest"],
                        issue_receipt_id=row["issue_receipt_id"],
                        issue_receipt_digest=row["issue_receipt_digest"],
                        pairing_expires_at=_parse_instant(row["pairing_expires_at"]),
                        redeem_idempotency_key=row["redeem_idempotency_key"],
                        redeem_command_digest=row["redeem_command_digest"],
                        credential_id=row["credential_id"],
                        credential_generation=row["credential_generation"],
                        credential_public_digest=row["credential_public_digest"],
                        bundle_revision=row["bundle_revision"],
                        bundle_public_digest=row["bundle_public_digest"],
                        created_at=_parse_instant(row["created_at"]),
                        updated_at=_parse_instant(row["updated_at"]),
                    )
            except OwnerPairingRecoveryUnavailable:
                raise
            except (sqlite3.Error, ValueError, TypeError) as error:
                raise OwnerPairingRecoveryUnavailable() from error

    def read_terminal_profile(
        self,
        profile_id: str,
        *,
        expected_central_origin: str,
    ) -> OwnerPairingRecoveryResult | None:
        """Read a finalized local profile without creating a recovery receipt.

        A finalized pair has no non-terminal recovery snapshot by design.  This
        read seam verifies the immutable terminal projection against the active
        keychain bundle before returning a redacted replay result; it never
        upgrades a caller-provided binding or repairs either store.
        """
        if type(profile_id) is not str or type(expected_central_origin) is not str:
            raise OwnerPairingRecoveryUnavailable()
        try:
            _validate_ref(profile_id)
        except ValueError as error:
            raise OwnerPairingRecoveryUnavailable() from error
        with self._lock:
            try:
                self._validate_path()
                with sqlite3.connect(self._path, timeout=30) as connection:
                    self._validate(connection)
                    connection.row_factory = sqlite3.Row
                    row = connection.execute(
                        "SELECT * FROM owner_installation_profiles WHERE profile_id=?",
                        (profile_id,),
                    ).fetchone()
                    if row is None:
                        return None
                    resource = self._resource(row)
                    if resource["central_origin"] != expected_central_origin:
                        raise OwnerPairingRecoveryUnavailable()
                    if self._device_keys is None or self._clock is None:
                        raise OwnerPairingRecoveryUnavailable()
                    bundle = self._device_keys.load(profile_id)
                    if bundle is None or bundle.active is None:
                        raise OwnerPairingRecoveryUnavailable()
                    if (
                        owner_profile_id(bundle.binding) != profile_id
                        or bundle.binding_digest != resource["binding_digest"]
                        or bundle.binding.central_origin != expected_central_origin
                        or bundle.pending is not None
                        or bundle.pairing_pending is not None
                        or bundle_public_digest(bundle) != resource["bundle_public_digest"]
                        or bundle.active.credential_id != resource["credential_id"]
                        or bundle.active.credential_generation != resource["credential_generation"]
                        or credential_public_digest_from_projection(
                            credential_public_projection(bundle.active)
                        )
                        != resource["credential_public_digest"]
                        or _parse_instant(bundle.active.expires_at) <= self._clock()
                    ):
                        raise OwnerPairingRecoveryUnavailable()
                    for field in (
                        "central_origin", "org_id", "owner_user_id", "agent_card_id",
                        "agent_card_revision", "agent_card_digest", "device_key_thumbprint",
                    ):
                        if getattr(bundle.binding, field) != resource[field]:
                            raise OwnerPairingRecoveryUnavailable()
                    verification = resource["verification"]
                    if verification not in {"paired", "recovered_unverified"}:
                        raise OwnerPairingRecoveryUnavailable()
                    return OwnerPairingRecoveryResult(
                        kind="replayed",
                        profile_id=profile_id,
                        state=None,
                        binding_digest=bundle.binding_digest,
                        credential_id=bundle.active.credential_id,
                        credential_generation=bundle.active.credential_generation,
                        credential_public_digest=credential_public_digest_from_projection(
                            credential_public_projection(bundle.active)
                        ),
                        bundle_revision=bundle.bundle_revision,
                        bundle_public_digest=bundle_public_digest(bundle),
                        verification=cast(
                            Literal["paired", "recovered_unverified"], verification
                        ),
                    )
            except OwnerPairingRecoveryUnavailable:
                raise
            except (sqlite3.Error, ValueError, TypeError) as error:
                raise OwnerPairingRecoveryUnavailable() from error

    def _transact(self, command: _RecoveryCommand, *, action: str, domain: bytes, mutate: Callable[[sqlite3.Connection], None]) -> OwnerPairingRecoveryResult:
        digest = self._command_digest(domain, command)
        with self._lock:
            try:
                self._validate_path()
                connection = sqlite3.connect(self._path, timeout=30)
                with connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._validate(connection)
                    connection.row_factory = sqlite3.Row
                    replay = self._replay(connection, command, action, digest)
                    if replay is not None:
                        return replay
                    mutate(connection)
                    self._fault("after_mutation")
                    row = connection.execute("SELECT * FROM owner_pairing_recoveries WHERE profile_id=?", (command.profile_id,)).fetchone()
                    if row is None:
                        raise OwnerPairingRecoveryUnavailable()
                    resource_digest = _digest(b"aon.owner.local.recovery-row.v2\0", self._resource(row))
                    result_kind: _ResultKind
                    if action == "intent.create":
                        result_kind = "intent_created"
                    elif action == "redeem.mark_submitted":
                        result_kind = "redeem_submitted"
                    else:
                        result_kind = "credential_stored"
                    result = self._result(row, result_kind)
                    resource_json = _jcs(self._resource(row)).decode()
                    command_json = _jcs(self._command_projection(command)).decode()
                    result_json = _jcs(result.model_dump(mode="json")).decode()
                    created_at = _instant(command.now)
                    receipt_digest = _receipt_digest(
                        idempotency_key=command.idempotency_key,
                        action=action,
                        command_digest=digest,
                        resource_digest=resource_digest,
                        result_json=result_json,
                        created_at=created_at,
                        row_created_at=row["created_at"],
                        row_updated_at=row["updated_at"],
                    )
                    connection.execute("INSERT INTO owner_pairing_recovery_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (command.idempotency_key, action, digest, command_json, resource_digest, resource_json,
                         result_json, row["created_at"], row["updated_at"],
                         created_at, receipt_digest))
                    self._fault("before_commit")
                    connection.row_factory = None
                    self._validate(connection)
                return result
            except OwnerPairingRecoveryUnavailable:
                raise
            except sqlite3.IntegrityError as error:
                raise OwnerPairingRecoveryConflict() from error
            except Exception as error:
                raise OwnerPairingRecoveryUnavailable() from error

    def create_intent_recovery(self, command: CreateIntentRecovery) -> OwnerPairingRecoveryResult:
        if type(command) is not CreateIntentRecovery or command.pairing_expires_at <= command.now:
            raise OwnerPairingRecoveryUnavailable()
        bind = binding_digest(command.binding)
        def insert(connection: sqlite3.Connection) -> None:
            b = command.binding
            connection.execute(
                "INSERT INTO owner_pairing_recoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,NULL,?,?)",
                (command.profile_id, "intent_issued", b.central_origin, b.org_id, b.owner_user_id,
                 b.agent_card_id, b.agent_card_revision, b.agent_card_digest, b.device_key_thumbprint,
                 bind, command.pairing_intent_id, command.pairing_intent_digest,
                 command.issue_receipt_id, command.issue_receipt_digest, _instant(command.pairing_expires_at),
                 _instant(command.now), _instant(command.now)),
            )
        return self._transact(command, action="intent.create", domain=b"aon.owner.local.intent-create.v2\0", mutate=insert)

    def mark_redeem_submitted(self, command: MarkRedeemSubmitted) -> OwnerPairingRecoveryResult:
        if type(command) is not MarkRedeemSubmitted or command.now <= command.expected_updated_at:
            raise OwnerPairingRecoveryUnavailable()
        def update(connection: sqlite3.Connection) -> None:
            changed = connection.execute(
                "UPDATE owner_pairing_recoveries SET state='redeem_submitted',redeem_idempotency_key=?,redeem_command_digest=?,updated_at=? WHERE profile_id=? AND state=? AND updated_at=?",
                (command.redeem_idempotency_key, command.redeem_command_digest, _instant(command.now),
                 command.profile_id, command.expected_state, _instant(command.expected_updated_at)),
            ).rowcount
            if changed != 1:
                raise OwnerPairingRecoveryConflict()
        return self._transact(command, action="redeem.mark_submitted", domain=b"aon.owner.local.redeem-submitted.v2\0", mutate=update)

    def mark_credential_stored(self, command: MarkCredentialStored) -> OwnerPairingRecoveryResult:
        if type(command) is not MarkCredentialStored or command.now <= command.expected_updated_at:
            raise OwnerPairingRecoveryUnavailable()
        def update(connection: sqlite3.Connection) -> None:
            changed = connection.execute(
                "UPDATE owner_pairing_recoveries SET state='credential_stored',credential_id=?,credential_generation=?,credential_public_digest=?,bundle_revision=?,bundle_public_digest=?,updated_at=? WHERE profile_id=? AND state=? AND updated_at=?",
                (command.credential_id, command.credential_generation, command.credential_public_digest,
                 command.bundle_revision, command.bundle_public_digest, _instant(command.now),
                 command.profile_id, command.expected_state, _instant(command.expected_updated_at)),
            ).rowcount
            if changed != 1:
                raise OwnerPairingRecoveryConflict()
        return self._transact(command, action="credential.mark_stored", domain=b"aon.owner.local.credential-stored.v2\0", mutate=update)

    def recover_from_keychain(
        self, command: RecoverFromKeychainCommand
    ) -> OwnerPairingRecoveryResult:
        if (
            type(command) is not RecoverFromKeychainCommand
            or self._device_keys is None
            or self._clock is None
        ):
            raise OwnerPairingRecoveryUnavailable()
        action = "profile.recover"
        domain = b"aon.owner.local.profile-recover.v2\0"
        digest = self._command_digest(domain, command)
        with self._lock:
            try:
                self._validate_path()
                with sqlite3.connect(self._path, timeout=30) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._validate(connection)
                    connection.row_factory = sqlite3.Row
                    replay = self._replay(
                        connection, command, action, digest
                    )
                    if replay is not None:
                        return replay
                    if connection.execute(
                        "SELECT 1 FROM owner_pairing_recoveries "
                        "WHERE profile_id=?",
                        (command.profile_id,),
                    ).fetchone() is not None or connection.execute(
                        "SELECT 1 FROM owner_installation_profiles "
                        "WHERE profile_id=?",
                        (command.profile_id,),
                    ).fetchone() is not None:
                        raise OwnerPairingRecoveryConflict()

                    bundle = self._device_keys.load(command.profile_id)
                    now = self._clock()
                    now_text = _instant(now)
                    if (
                        bundle is None
                        or owner_profile_id(bundle.binding)
                        != command.profile_id
                        or bundle.binding.central_origin
                        != command.expected_central_origin
                        or bundle.binding_digest
                        != binding_digest(bundle.binding)
                        or bundle.active is None
                        or bundle.pending is not None
                        or bundle.pairing_pending is not None
                        or _parse_instant(bundle.active.expires_at) <= now
                    ):
                        raise OwnerPairingRecoveryUnavailable()

                    active = bundle.active
                    public = credential_public_projection(active)
                    public_digest = (
                        credential_public_digest_from_projection(public)
                    )
                    public_bundle_digest = bundle_public_digest(bundle)
                    profile = {
                        "profile_id": command.profile_id,
                        "central_origin": bundle.binding.central_origin,
                        "keychain_account_ref": owner_keychain_account_ref(
                            command.profile_id
                        ),
                        "org_id": bundle.binding.org_id,
                        "owner_user_id": bundle.binding.owner_user_id,
                        "agent_card_id": bundle.binding.agent_card_id,
                        "agent_card_revision": bundle.binding.agent_card_revision,
                        "agent_card_digest": bundle.binding.agent_card_digest,
                        "device_key_thumbprint":
                            bundle.binding.device_key_thumbprint,
                        "binding_digest": bundle.binding_digest,
                        "credential_id": active.credential_id,
                        "credential_generation": active.credential_generation,
                        "credential_public_digest": public_digest,
                        "bundle_revision": bundle.bundle_revision,
                        "bundle_public_digest": public_bundle_digest,
                        "issued_at": active.issued_at,
                        "expires_at": active.expires_at,
                        "scope_json": _jcs(public["scope"]).decode(),
                        "slot_version": public["slot_version"],
                        "envelope_suite": public["envelope_suite"],
                        "envelope_version": public["envelope_version"],
                        "aad_digest": public["aad_digest"],
                        "envelope_digest": public["envelope_digest"],
                        "redeem_receipt_id": public["redeem_receipt_id"],
                        "redeem_receipt_digest":
                            public["redeem_receipt_digest"],
                        "verification": "recovered_unverified",
                    }
                    connection.execute(
                        "INSERT INTO owner_installation_profiles VALUES("
                        + ",".join("?" for _ in range(28))
                        + ")",
                        (*profile.values(), now_text, now_text),
                    )
                    result = OwnerPairingRecoveryResult(
                        kind="recovered_unverified",
                        profile_id=command.profile_id,
                        state=None,
                        binding_digest=bundle.binding_digest,
                        credential_id=active.credential_id,
                        credential_generation=active.credential_generation,
                        credential_public_digest=public_digest,
                        bundle_revision=bundle.bundle_revision,
                        bundle_public_digest=public_bundle_digest,
                        verification="recovered_unverified",
                    )
                    command_json = _jcs(
                        self._command_projection(command)
                    ).decode()
                    resource_json = _jcs(profile).decode()
                    result_json = _jcs(
                        result.model_dump(mode="json")
                    ).decode()
                    resource_digest = _digest(
                        b"aon.owner.local.recovery-row.v2\0", profile
                    )
                    receipt_digest = _receipt_digest(
                        idempotency_key=command.idempotency_key,
                        action=action,
                        command_digest=digest,
                        resource_digest=resource_digest,
                        result_json=result_json,
                        created_at=now_text,
                        row_created_at=now_text,
                        row_updated_at=now_text,
                    )
                    connection.execute(
                        "INSERT INTO owner_pairing_recovery_receipts "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            command.idempotency_key,
                            action,
                            digest,
                            command_json,
                            resource_digest,
                            resource_json,
                            result_json,
                            now_text,
                            now_text,
                            now_text,
                            receipt_digest,
                        ),
                    )
                    self._fault("before_commit")
                    connection.row_factory = None
                    self._validate(connection)
                return result
            except (OwnerPairingRecoveryUnavailable, OwnerPairingRecoveryConflict):
                raise
            except sqlite3.IntegrityError as error:
                raise OwnerPairingRecoveryConflict() from error
            except Exception as error:
                raise OwnerPairingRecoveryUnavailable() from error

    def finalize(
        self, command: FinalizeFromStoredCredentialCommand
    ) -> OwnerPairingRecoveryResult:
        if (
            type(command) is not FinalizeFromStoredCredentialCommand
            or self._device_keys is None
            or self._clock is None
        ):
            raise OwnerPairingRecoveryUnavailable()
        action = "pairing.finalize"
        domain = b"aon.owner.local.pairing-finalize.v2\0"
        digest = self._command_digest(domain, command)
        with self._lock:
            try:
                self._validate_path()
                with sqlite3.connect(self._path, timeout=30) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._validate(connection)
                    connection.row_factory = sqlite3.Row
                    replay = self._replay(
                        connection, command, action, digest
                    )
                    if replay is not None:
                        return replay
                    recovery = connection.execute(
                        "SELECT * FROM owner_pairing_recoveries "
                        "WHERE profile_id=?",
                        (command.profile_id,),
                    ).fetchone()
                    if recovery is None:
                        raise OwnerPairingRecoveryConflict()
                    recovery_snapshot = dict(recovery)
                    if (
                        recovery["state"] != command.expected_state
                        or recovery["updated_at"]
                        != _instant(command.expected_updated_at)
                        or recovery["central_origin"]
                        != command.expected_central_origin
                    ):
                        raise OwnerPairingRecoveryConflict()
                    preflight_now = self._clock()
                    _instant(preflight_now)

                bundle = self._device_keys.load(command.profile_id)
                if (
                    bundle is None
                    or owner_profile_id(bundle.binding) != command.profile_id
                    or bundle.binding.central_origin
                    != command.expected_central_origin
                    or bundle.binding_digest != recovery_snapshot["binding_digest"]
                    or any(
                        getattr(bundle.binding, field)
                        != recovery_snapshot[field]
                        for field in (
                            "central_origin", "org_id", "owner_user_id",
                            "agent_card_id", "agent_card_revision",
                            "agent_card_digest", "device_key_thumbprint",
                        )
                    )
                    or bundle.active is None
                    or _parse_instant(bundle.active.expires_at) <= preflight_now
                    or bundle.pending is not None
                    or bundle.active.credential_id
                    != recovery_snapshot["credential_id"]
                    or bundle.active.credential_generation
                    != recovery_snapshot["credential_generation"]
                    or credential_public_digest_from_projection(
                        credential_public_projection(bundle.active)
                    )
                    != recovery_snapshot["credential_public_digest"]
                ):
                    raise OwnerPairingRecoveryUnavailable()
                if bundle.pairing_pending is not None:
                    pending = bundle.pairing_pending
                    predicted_final = bundle.model_copy(
                        update={
                            "bundle_revision": bundle.bundle_revision + 1,
                            "pairing_pending": None,
                        }
                    )
                    if (
                        bundle.bundle_revision
                        != recovery_snapshot["bundle_revision"]
                        or bundle_public_digest(bundle)
                        != recovery_snapshot["bundle_public_digest"]
                        or pending.pairing_intent_id
                        != recovery_snapshot["pairing_intent_id"]
                        or pending.pairing_intent_digest
                        != recovery_snapshot["pairing_intent_digest"]
                        or pending.issue_receipt_id
                        != recovery_snapshot["issue_receipt_id"]
                        or pending.issue_receipt_digest
                        != recovery_snapshot["issue_receipt_digest"]
                        or pending.redeem_idempotency_key
                        != recovery_snapshot["redeem_idempotency_key"]
                        or pending.redeem_command_digest
                        != recovery_snapshot["redeem_command_digest"]
                        or bundle_public_digest(predicted_final)
                        != command.expected_bundle_public_digest
                    ):
                        raise OwnerPairingRecoveryUnavailable()
                    stored_bundle = bundle
                    self._fault("before_keychain_clear")
                    try:
                        self._device_keys.clear_pairing_pending(
                            command.profile_id,
                            expected_revision=bundle.bundle_revision,
                        )
                    except OwnerDeviceKeyStoreConflict:
                        finalized = self._device_keys.load(command.profile_id)
                        if finalized is None:
                            raise OwnerPairingRecoveryUnavailable()
                        validate_finalized_owner_bundle_delta(
                            stored_bundle, finalized
                        )
                        if (
                            bundle_public_digest(finalized)
                            != command.expected_bundle_public_digest
                        ):
                            raise OwnerPairingRecoveryUnavailable()
                    else:
                        finalized = self._device_keys.load(command.profile_id)
                        if finalized is None:
                            raise OwnerPairingRecoveryUnavailable()
                        validate_finalized_owner_bundle_delta(
                            stored_bundle, finalized
                        )
                    self._fault("after_keychain_clear")
                else:
                    finalized = bundle
                    if (
                        finalized.bundle_revision
                        != recovery_snapshot["bundle_revision"] + 1
                    ):
                        raise OwnerPairingRecoveryUnavailable()
                if (
                    bundle_public_digest(finalized)
                    != command.expected_bundle_public_digest
                ):
                    raise OwnerPairingRecoveryUnavailable()

                with sqlite3.connect(self._path, timeout=30) as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._validate(connection)
                    connection.row_factory = sqlite3.Row
                    replay = self._replay(
                        connection, command, action, digest
                    )
                    if replay is not None:
                        return replay
                    row = connection.execute(
                        "SELECT * FROM owner_pairing_recoveries "
                        "WHERE profile_id=? AND state=? AND updated_at=?",
                        (
                            command.profile_id,
                            command.expected_state,
                            _instant(command.expected_updated_at),
                        ),
                    ).fetchone()
                    now = self._clock()
                    now_text = _instant(now)
                    active = finalized.active
                    if (
                        row is None
                        or active is None
                        or _parse_instant(active.expires_at) <= now
                        or any(
                            row[key] != recovery_snapshot[key]
                            for key in row.keys()
                        )
                    ):
                        raise OwnerPairingRecoveryConflict()
                    public = credential_public_projection(active)
                    active_public_digest = (
                        credential_public_digest_from_projection(public)
                    )
                    final_bundle_digest = bundle_public_digest(finalized)
                    profile = {
                        "profile_id": command.profile_id,
                        "central_origin": finalized.binding.central_origin,
                        "keychain_account_ref": owner_keychain_account_ref(
                            command.profile_id
                        ),
                        "org_id": finalized.binding.org_id,
                        "owner_user_id": finalized.binding.owner_user_id,
                        "agent_card_id": finalized.binding.agent_card_id,
                        "agent_card_revision": finalized.binding.agent_card_revision,
                        "agent_card_digest": finalized.binding.agent_card_digest,
                        "device_key_thumbprint": finalized.binding.device_key_thumbprint,
                        "binding_digest": finalized.binding_digest,
                        "credential_id": active.credential_id,
                        "credential_generation": active.credential_generation,
                        "credential_public_digest": active_public_digest,
                        "bundle_revision": finalized.bundle_revision,
                        "bundle_public_digest": final_bundle_digest,
                        "issued_at": active.issued_at,
                        "expires_at": active.expires_at,
                        "scope_json": _jcs(public["scope"]).decode(),
                        "slot_version": public["slot_version"],
                        "envelope_suite": public["envelope_suite"],
                        "envelope_version": public["envelope_version"],
                        "aad_digest": public["aad_digest"],
                        "envelope_digest": public["envelope_digest"],
                        "redeem_receipt_id": public["redeem_receipt_id"],
                        "redeem_receipt_digest": public["redeem_receipt_digest"],
                        "verification": "paired",
                    }
                    connection.execute(
                        "INSERT INTO owner_installation_profiles VALUES("
                        + ",".join("?" for _ in range(28))
                        + ")",
                        (*profile.values(), now_text, now_text),
                    )
                    result = OwnerPairingRecoveryResult(
                        kind="finalized", profile_id=command.profile_id,
                        state=None, binding_digest=finalized.binding_digest,
                        credential_id=active.credential_id,
                        credential_generation=active.credential_generation,
                        credential_public_digest=active_public_digest,
                        bundle_revision=finalized.bundle_revision,
                        bundle_public_digest=final_bundle_digest,
                        verification="paired",
                    )
                    command_json = _jcs(
                        self._command_projection(command)
                    ).decode()
                    resource_json = _jcs(profile).decode()
                    result_json = _jcs(result.model_dump(mode="json")).decode()
                    resource_digest = _digest(
                        b"aon.owner.local.recovery-row.v2\0", profile
                    )
                    receipt_digest = _receipt_digest(
                        idempotency_key=command.idempotency_key,
                        action=action, command_digest=digest,
                        resource_digest=resource_digest,
                        result_json=result_json, created_at=now_text,
                        row_created_at=now_text, row_updated_at=now_text,
                    )
                    connection.execute(
                        "INSERT INTO owner_pairing_recovery_receipts "
                        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            command.idempotency_key, action, digest,
                            command_json, resource_digest, resource_json,
                            result_json, now_text, now_text, now_text,
                            receipt_digest,
                        ),
                    )
                    deleted = connection.execute(
                        "DELETE FROM owner_pairing_recoveries "
                        "WHERE profile_id=? AND state=? AND updated_at=?",
                        (
                            command.profile_id, command.expected_state,
                            _instant(command.expected_updated_at),
                        ),
                    ).rowcount
                    if deleted != 1:
                        raise OwnerPairingRecoveryConflict()
                    self._fault("before_commit")
                    connection.row_factory = None
                    self._validate(connection)
                return result
            except (OwnerPairingRecoveryUnavailable, OwnerPairingRecoveryConflict):
                raise
            except Exception as error:
                raise OwnerPairingRecoveryUnavailable() from error


__all__ = [
    "CreateIntentRecovery", "FinalizeFromStoredCredentialCommand",
    "LegacyOwnerPairingRequiresRepair", "MarkCredentialStored",
    "MarkRedeemSubmitted", "OwnerPairingRecoveryConflict",
    "OwnerPairingRecoveryResult", "OwnerPairingRecoverySnapshot",
    "OwnerPairingRecoveryStore",
    "OwnerPairingRecoveryUnavailable", "RecoverFromKeychainCommand",
    "validate_finalized_owner_bundle_delta",
]
