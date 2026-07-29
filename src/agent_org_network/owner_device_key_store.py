"""Owner installation keychain domain models and canonical projections."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
import ipaddress
import json
import re
from typing import Literal, cast
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    load_der_private_key,
)
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretBytes,
    field_validator,
    model_validator,
)

from agent_org_network.central_authority import AUTHORITY_ACTION_MANIFEST
from agent_org_network.owner_credential_envelope import (
    SUITE,
    X25519PublicJwk,
    device_key_thumbprint,
    public_jwk,
)

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_B64U32 = re.compile(r"[A-Za-z0-9_-]{43}")
_INSTANT = "%Y-%m-%dT%H:%M:%SZ"


class OwnerDeviceKeyStoreUnavailable(Exception):
    pass


class OwnerDeviceKeyStoreConflict(OwnerDeviceKeyStoreUnavailable):
    pass


def _jcs(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _domain_digest(domain: bytes, value: object) -> str:
    return sha256(domain + _jcs(value)).hexdigest()


def canonicalize_central_origin(
    value: str, *, allow_loopback_http: bool = False
) -> str:
    if (
        type(value) is not str
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or "\\" in value
    ):
        raise OwnerDeviceKeyStoreUnavailable()
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() not in {"https", "http"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path
            or parsed.hostname is None
        ):
            raise OwnerDeviceKeyStoreUnavailable()
        scheme = parsed.scheme.lower()
        raw_host = parsed.hostname
        if "%" in raw_host or "_" in raw_host or raw_host.endswith("."):
            raise OwnerDeviceKeyStoreUnavailable()
        try:
            address = ipaddress.ip_address(raw_host)
            host = address.compressed
            is_loopback = address.is_loopback
            rendered_host = f"[{host}]" if address.version == 6 else host
        except ValueError:
            labels = raw_host.split(".")
            if any(not label for label in labels):
                raise OwnerDeviceKeyStoreUnavailable()
            host = raw_host.encode("idna").decode("ascii").lower()
            if any(
                re.fullmatch(r"(?!-)[a-z0-9-]{1,63}(?<!-)", label) is None
                for label in host.split(".")
            ):
                raise OwnerDeviceKeyStoreUnavailable()
            rendered_host = host
            is_loopback = host == "localhost"
        if is_loopback and not (scheme == "http" and allow_loopback_http):
            raise OwnerDeviceKeyStoreUnavailable()
        if scheme == "http" and not is_loopback:
            raise OwnerDeviceKeyStoreUnavailable()
        port = parsed.port
        default = 443 if scheme == "https" else 80
        suffix = "" if port is None or port == default else f":{port}"
        canonical = f"{scheme}://{rendered_host}{suffix}"
        return canonical
    except OwnerDeviceKeyStoreUnavailable:
        raise
    except Exception as error:
        raise OwnerDeviceKeyStoreUnavailable() from error


class OwnerInstallationPublicBindingV1(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    binding_version: Literal[1] = 1
    central_origin: str
    org_id: str
    owner_user_id: str
    agent_card_id: str
    agent_card_revision: int = Field(gt=0)
    agent_card_digest: str
    device_key_thumbprint: str
    audience: Literal["owner-install"] = "owner-install"

    @field_validator("central_origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        canonical = canonicalize_central_origin(value)
        if canonical != value:
            raise ValueError("canonical central origin required")
        return value

    @field_validator("org_id", "owner_user_id", "agent_card_id")
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator("agent_card_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("device_key_thumbprint")
    @classmethod
    def _thumbprint(cls, value: str) -> str:
        if _B64U32.fullmatch(value) is None:
            raise ValueError("canonical b64url32 required")
        return value


def binding_digest(value: OwnerInstallationPublicBindingV1) -> str:
    return _domain_digest(
        b"aon.owner.binding.v1\0", value.model_dump(mode="json")
    )


def owner_profile_id(value: OwnerInstallationPublicBindingV1) -> str:
    if type(value) is not OwnerInstallationPublicBindingV1:
        raise OwnerDeviceKeyStoreUnavailable()
    return _domain_digest(
        b"aon.owner.installation-profile.v1\0",
        value.model_dump(mode="json"),
    )


def owner_keychain_account_ref(profile_id: str) -> str:
    if type(profile_id) is not str or not 1 <= len(profile_id) <= 256:
        raise OwnerDeviceKeyStoreUnavailable()
    return sha256(profile_id.encode()).hexdigest()


class OwnerCredentialSlotV1(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    slot_version: Literal[1] = 1
    credential_id: str
    credential_generation: int = Field(gt=0)
    credential_secret: SecretBytes
    issued_at: str
    expires_at: str
    scope: tuple[str, ...]
    envelope_suite: Literal[
        "AON-OWNER-PAIR-X25519-HKDF-SHA256-A256GCM-v1"
    ] = SUITE
    envelope_version: Literal[1] = 1
    aad_digest: str
    envelope_digest: str
    redeem_receipt_id: str
    redeem_receipt_digest: str

    @field_validator("credential_id", "redeem_receipt_id")
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator("credential_secret")
    @classmethod
    def _secret(cls, value: SecretBytes) -> SecretBytes:
        if len(value.get_secret_value()) != 32:
            raise ValueError("32-byte credential secret required")
        return value

    @field_validator("aad_digest", "envelope_digest", "redeem_receipt_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _instant(cls, value: str) -> str:
        parsed = datetime.strptime(value, _INSTANT).replace(tzinfo=UTC)
        if parsed.strftime(_INSTANT) != value:
            raise ValueError("canonical UTC second required")
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not value
            or value != tuple(sorted(value))
            or len(set(value)) != len(value)
            or any(action not in AUTHORITY_ACTION_MANIFEST for action in value)
        ):
            raise ValueError("sorted unique action scope required")
        return value

    @model_validator(mode="after")
    def _lifetime(self) -> "OwnerCredentialSlotV1":
        issued = datetime.strptime(self.issued_at, _INSTANT).replace(tzinfo=UTC)
        expires = datetime.strptime(self.expires_at, _INSTANT).replace(tzinfo=UTC)
        if expires != issued + timedelta(days=30):
            raise ValueError("exact 30-day lifetime required")
        return self


def credential_public_projection(value: OwnerCredentialSlotV1) -> dict[str, object]:
    return {
        "aad_digest": value.aad_digest,
        "credential_generation": value.credential_generation,
        "credential_id": value.credential_id,
        "envelope_digest": value.envelope_digest,
        "envelope_suite": value.envelope_suite,
        "envelope_version": value.envelope_version,
        "expires_at": value.expires_at,
        "issued_at": value.issued_at,
        "redeem_receipt_digest": value.redeem_receipt_digest,
        "redeem_receipt_id": value.redeem_receipt_id,
        "scope": list(value.scope),
        "slot_version": value.slot_version,
    }


def credential_public_digest(value: OwnerCredentialSlotV1) -> str:
    return _domain_digest(
        b"aon.owner.credential-public.v1\0", credential_public_projection(value)
    )


def credential_public_digest_from_projection(value: object) -> str:
    if type(value) is not dict:
        raise OwnerDeviceKeyStoreUnavailable()
    projection = cast(dict[str, object], value)
    expected = {
        "aad_digest", "credential_generation", "credential_id",
        "envelope_digest", "envelope_suite", "envelope_version",
        "expires_at", "issued_at", "redeem_receipt_digest",
        "redeem_receipt_id", "scope", "slot_version",
    }
    if set(projection) != expected:
        raise OwnerDeviceKeyStoreUnavailable()
    try:
        scope = projection.get("scope")
        if type(scope) is not list:
            raise OwnerDeviceKeyStoreUnavailable()
        scope_items = cast(list[object], scope)
        if any(type(item) is not str for item in scope_items):
            raise OwnerDeviceKeyStoreUnavailable()
        slot = OwnerCredentialSlotV1.model_validate(
            {
                **projection,
                "scope": tuple(cast(list[str], scope_items)),
                "credential_secret": SecretBytes(b"\0" * 32),
            }
        )
        if credential_public_projection(slot) != projection:
            raise OwnerDeviceKeyStoreUnavailable()
        return credential_public_digest(slot)
    except OwnerDeviceKeyStoreUnavailable:
        raise
    except Exception as error:
        raise OwnerDeviceKeyStoreUnavailable() from error


class OwnerDeviceKeyMaterialV1(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    key_material_version: Literal[1] = 1
    private_key_pkcs8_der: SecretBytes
    public_key: X25519PublicJwk
    device_key_thumbprint: str

    @model_validator(mode="after")
    def _derive(self) -> "OwnerDeviceKeyMaterialV1":
        try:
            private = load_der_private_key(
                self.private_key_pkcs8_der.get_secret_value(), password=None
            )
            if not isinstance(private, X25519PrivateKey):
                raise ValueError
            if (
                public_jwk(private.public_key()) != self.public_key
                or device_key_thumbprint(self.public_key) != self.device_key_thumbprint
                or _B64U32.fullmatch(self.device_key_thumbprint) is None
            ):
                raise ValueError
        except Exception as error:
            raise ValueError("X25519 key material mismatch") from error
        return self

    @classmethod
    def generate(cls) -> "OwnerDeviceKeyMaterialV1":
        private = X25519PrivateKey.generate()
        public = public_jwk(private.public_key())
        return cls(
            private_key_pkcs8_der=SecretBytes(
                private.private_bytes(
                    Encoding.DER, PrivateFormat.PKCS8, NoEncryption()
                )
            ),
            public_key=public,
            device_key_thumbprint=device_key_thumbprint(public),
        )


class OwnerPairingPendingV1(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    pending_version: Literal[1] = 1
    pairing_intent_id: str
    pairing_intent_digest: str
    issue_receipt_id: str
    issue_receipt_digest: str
    redeem_idempotency_key: str
    redeem_command_digest: str
    pairing_expires_at: str

    @field_validator(
        "pairing_intent_id", "issue_receipt_id", "redeem_idempotency_key"
    )
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded reference required")
        return value

    @field_validator(
        "pairing_intent_digest", "issue_receipt_digest", "redeem_command_digest"
    )
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value

    @field_validator("pairing_expires_at")
    @classmethod
    def _instant(cls, value: str) -> str:
        parsed = datetime.strptime(value, _INSTANT).replace(tzinfo=UTC)
        if parsed.strftime(_INSTANT) != value:
            raise ValueError("canonical UTC second required")
        return value


class OwnerInstallationSecretBundleV1(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    bundle_revision: int = Field(gt=0)
    binding: OwnerInstallationPublicBindingV1
    binding_digest: str
    device: OwnerDeviceKeyMaterialV1
    pairing_pending: OwnerPairingPendingV1 | None
    active: OwnerCredentialSlotV1 | None
    pending: OwnerCredentialSlotV1 | None

    @model_validator(mode="after")
    def _invariants(self) -> "OwnerInstallationSecretBundleV1":
        if (
            self.binding_digest != binding_digest(self.binding)
            or self.binding.device_key_thumbprint != self.device.device_key_thumbprint
            or (self.active is None and self.pairing_pending is None)
            or (self.pending is not None and self.active is None)
            or (
                self.pending is not None
                and self.active is not None
                and self.pending.credential_generation
                != self.active.credential_generation + 1
            )
        ):
            raise ValueError("invalid bundle state")
        if self.active is not None and self.pending is not None:
            if any(
                left == right
                for left, right in (
                    (self.active.credential_id, self.pending.credential_id),
                    (
                        self.active.redeem_receipt_id,
                        self.pending.redeem_receipt_id,
                    ),
                    (self.active.aad_digest, self.pending.aad_digest),
                )
            ):
                raise ValueError("active/pending anchors must be distinct")
        return self


def bundle_public_projection(
    value: OwnerInstallationSecretBundleV1,
) -> dict[str, object]:
    return {
        "active": (
            credential_public_projection(value.active)
            if value.active is not None
            else None
        ),
        "binding": value.binding.model_dump(mode="json"),
        "binding_digest": value.binding_digest,
        "bundle_revision": value.bundle_revision,
        "device_key_thumbprint": value.device.device_key_thumbprint,
        "pairing_pending": (
            value.pairing_pending.model_dump(mode="json")
            if value.pairing_pending is not None
            else None
        ),
        "pending": (
            credential_public_projection(value.pending)
            if value.pending is not None
            else None
        ),
        "schema_version": value.schema_version,
    }


def bundle_public_digest(value: OwnerInstallationSecretBundleV1) -> str:
    return _domain_digest(
        b"aon.owner.bundle-public.v1\0", bundle_public_projection(value)
    )


def envelope_digest(canonical_envelope: bytes) -> str:
    return sha256(b"aon.owner.envelope.v1\0" + canonical_envelope).hexdigest()


class OwnerSecretBundleWriteReceipt(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    bundle_revision: int = Field(gt=0)
    bundle_public_digest: str


__all__ = [
    "OwnerCredentialSlotV1",
    "OwnerDeviceKeyMaterialV1",
    "OwnerDeviceKeyStoreConflict",
    "OwnerDeviceKeyStoreUnavailable",
    "OwnerInstallationPublicBindingV1",
    "OwnerInstallationSecretBundleV1",
    "OwnerPairingPendingV1",
    "OwnerSecretBundleWriteReceipt",
    "owner_keychain_account_ref",
    "owner_profile_id",
    "binding_digest",
    "bundle_public_digest",
    "bundle_public_projection",
    "canonicalize_central_origin",
    "credential_public_digest",
    "credential_public_digest_from_projection",
    "credential_public_projection",
    "envelope_digest",
]
