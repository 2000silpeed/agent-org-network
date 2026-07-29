"""ADR 0068 Owner Installation Credential Envelope v1 codec."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import json
import os
import re
from typing import Literal, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator, model_validator

from agent_org_network.central_authority import AUTHORITY_ACTION_MANIFEST


SUITE = "AON-OWNER-PAIR-X25519-HKDF-SHA256-A256GCM-v1"
_INFO_PREFIX = b"agent-org-network/owner-install/credential-envelope/v1\x00"
_B64 = re.compile(r"[A-Za-z0-9_-]+")
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


class OwnerCredentialEnvelopeUnavailable(Exception):
    pass


def _b64(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str, size: int) -> bytes:
    if type(value) is not str or _B64.fullmatch(value) is None or "=" in value:
        raise OwnerCredentialEnvelopeUnavailable()
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as error:
        raise OwnerCredentialEnvelopeUnavailable() from error
    if len(decoded) != size or _b64(decoded) != value:
        raise OwnerCredentialEnvelopeUnavailable()
    return decoded


def _jcs(value: object) -> bytes:
    """Exact RFC 8785 subset used by v1: ASCII keys/strings, arrays, integers."""
    if value is None or type(value) in {bool, int, str}:
        return json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    if type(value) is list:
        values = cast(list[object], value)
        return b"[" + b",".join(_jcs(item) for item in values) + b"]"
    if type(value) is dict:
        items = cast(dict[object, object], value)
        if any(type(key) is not str for key in items):
            raise OwnerCredentialEnvelopeUnavailable()
        string_items = cast(dict[str, object], items)
        return (
            b"{"
            + b",".join(
                _jcs(key) + b":" + _jcs(string_items[key])
                for key in sorted(string_items)
            )
            + b"}"
        )
    raise OwnerCredentialEnvelopeUnavailable()


class X25519PublicJwk(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    crv: Literal["X25519"] = "X25519"
    kty: Literal["OKP"] = "OKP"
    x: str

    @field_validator("x")
    @classmethod
    def _x(cls, value: str) -> str:
        _decode(value, 32)
        return value

    def key(self) -> X25519PublicKey:
        try:
            return X25519PublicKey.from_public_bytes(_decode(self.x, 32))
        except ValueError as error:
            raise OwnerCredentialEnvelopeUnavailable() from error


class CredentialEnvelopeAad(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_card_id: str
    audience: Literal["owner-install"] = "owner-install"
    credential_generation: int
    credential_id: str
    device_key_thumbprint: str
    envelope_version: Literal[1] = 1
    expires_at: str
    issued_at: str
    org_id: str
    owner_user_id: str
    scope: tuple[str, ...]

    @field_validator(
        "agent_card_id", "credential_id", "org_id", "owner_user_id"
    )
    @classmethod
    def _ref(cls, value: str) -> str:
        if _REF.fullmatch(value) is None:
            raise ValueError("bounded canonical id required")
        return value

    @field_validator("device_key_thumbprint")
    @classmethod
    def _thumbprint(cls, value: str) -> str:
        _decode(value, 32)
        return value

    @field_validator("credential_generation")
    @classmethod
    def _generation(cls, value: int) -> int:
        if value < 1:
            raise ValueError("positive generation required")
        return value

    @field_validator("issued_at", "expires_at")
    @classmethod
    def _instant(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=UTC
            )
        except ValueError as error:
            raise ValueError("canonical UTC second instant required") from error
        if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
            raise ValueError("canonical UTC second instant required")
        return value

    @field_validator("scope")
    @classmethod
    def _scope(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if (
            not 1 <= len(value) <= 32
            or value != tuple(sorted(value))
            or len(set(value)) != len(value)
            or any(item not in AUTHORITY_ACTION_MANIFEST for item in value)
        ):
            raise ValueError("sorted unique central actions required")
        return value

    @model_validator(mode="after")
    def _lifetime(self) -> "CredentialEnvelopeAad":
        try:
            issued = datetime.strptime(
                self.issued_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC)
            expires = datetime.strptime(
                self.expires_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=UTC)
            if expires <= issued or expires != issued + timedelta(days=30):
                raise ValueError("credential lifetime must be exactly 30 days")
        except (OverflowError, ValueError) as error:
            raise ValueError("credential lifetime must be exactly 30 days") from error
        return self


class OwnerCredentialEnvelope(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    aad: CredentialEnvelopeAad
    ciphertext: str
    ephemeral_public_key: X25519PublicJwk
    kdf_salt: str
    nonce: str
    suite: Literal[
        "AON-OWNER-PAIR-X25519-HKDF-SHA256-A256GCM-v1"
    ] = SUITE

    @field_validator("kdf_salt")
    @classmethod
    def _salt(cls, value: str) -> str:
        _decode(value, 32)
        return value

    @field_validator("nonce")
    @classmethod
    def _nonce(cls, value: str) -> str:
        _decode(value, 12)
        return value

    @field_validator("ciphertext")
    @classmethod
    def _ciphertext(cls, value: str) -> str:
        if not 16 <= len(_decode_variable(value, 16, 1024)) <= 1024:
            raise ValueError("bounded ciphertext and tag required")
        return value


class DecryptedOwnerCredential(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    credential_secret: SecretStr
    credential_type: Literal["bearer"] = "bearer"
    envelope_version: Literal[1] = 1

    @field_validator("credential_secret")
    @classmethod
    def _secret(cls, value: SecretStr) -> SecretStr:
        _decode(value.get_secret_value(), 32)
        return value


class EncryptedOwnerCredential(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    envelope: OwnerCredentialEnvelope
    verifier_key_id: str
    credential_verifier: str


def _decode_variable(value: str, minimum: int, maximum: int) -> bytes:
    if type(value) is not str or _B64.fullmatch(value) is None or "=" in value:
        raise OwnerCredentialEnvelopeUnavailable()
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as error:
        raise OwnerCredentialEnvelopeUnavailable() from error
    if not minimum <= len(decoded) <= maximum or _b64(decoded) != value:
        raise OwnerCredentialEnvelopeUnavailable()
    return decoded


def public_jwk(key: X25519PublicKey) -> X25519PublicJwk:
    return X25519PublicJwk(
        x=_b64(key.public_bytes(Encoding.Raw, PublicFormat.Raw))
    )


def device_key_thumbprint(jwk: X25519PublicJwk) -> str:
    if type(jwk) is not X25519PublicJwk:
        raise OwnerCredentialEnvelopeUnavailable()
    thumbprint_input = _jcs(
        {"crv": jwk.crv, "kty": jwk.kty, "x": jwk.x}
    )
    return _b64(sha256(thumbprint_input).digest())


def generate_device_keypair() -> tuple[X25519PrivateKey, X25519PublicJwk]:
    private = X25519PrivateKey.generate()
    return private, public_jwk(private.public_key())


def _require_private_key(value: object) -> X25519PrivateKey:
    if not isinstance(value, X25519PrivateKey):
        raise OwnerCredentialEnvelopeUnavailable()
    return value


def _derive(
    private: X25519PrivateKey,
    public: X25519PublicKey,
    salt: bytes,
    aad_bytes: bytes,
) -> bytes:
    try:
        shared = private.exchange(public)
    except ValueError as error:
        raise OwnerCredentialEnvelopeUnavailable() from error
    if shared == b"\x00" * 32:
        raise OwnerCredentialEnvelopeUnavailable()
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=_INFO_PREFIX + sha256(aad_bytes).digest(),
    ).derive(shared)


def encrypt_owner_credential(
    aad: CredentialEnvelopeAad,
    device_public_key: X25519PublicJwk,
    *,
    ephemeral_private_factory: Callable[[], X25519PrivateKey] = (
        X25519PrivateKey.generate
    ),
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> OwnerCredentialEnvelope:
    envelope, _secret = _encrypt_owner_credential(
        aad,
        device_public_key,
        ephemeral_private_factory=ephemeral_private_factory,
        random_bytes=random_bytes,
    )
    return envelope


def encrypt_owner_credential_with_verifier(
    aad: CredentialEnvelopeAad,
    device_public_key: X25519PublicJwk,
    *,
    verifier: Callable[[bytes], tuple[str, str]],
) -> EncryptedOwnerCredential:
    envelope, secret = _encrypt_owner_credential(aad, device_public_key)
    key_id, digest = verifier(secret)
    if _REF.fullmatch(key_id) is None or re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise OwnerCredentialEnvelopeUnavailable()
    return EncryptedOwnerCredential(
        envelope=envelope,
        verifier_key_id=key_id,
        credential_verifier=digest,
    )


def _encrypt_owner_credential(
    aad: CredentialEnvelopeAad,
    device_public_key: X25519PublicJwk,
    *,
    ephemeral_private_factory: Callable[[], X25519PrivateKey] = (
        X25519PrivateKey.generate
    ),
    random_bytes: Callable[[int], bytes] = os.urandom,
) -> tuple[OwnerCredentialEnvelope, bytes]:
    if (
        type(aad) is not CredentialEnvelopeAad
        or type(device_public_key) is not X25519PublicJwk
        or aad.device_key_thumbprint != device_key_thumbprint(device_public_key)
    ):
        raise OwnerCredentialEnvelopeUnavailable()
    ephemeral = _require_private_key(ephemeral_private_factory())
    salt, nonce, secret = random_bytes(32), random_bytes(12), random_bytes(32)
    if len(salt) != 32 or len(nonce) != 12 or len(secret) != 32:
        raise OwnerCredentialEnvelopeUnavailable()
    aad_bytes = _jcs(aad.model_dump(mode="json"))
    plaintext = _jcs(
        {
            "credential_secret": _b64(secret),
            "credential_type": "bearer",
            "envelope_version": 1,
        }
    )
    key = _derive(ephemeral, device_public_key.key(), salt, aad_bytes)
    encrypted = AESGCM(key).encrypt(nonce, plaintext, aad_bytes)
    return OwnerCredentialEnvelope(
        aad=aad,
        ciphertext=_b64(encrypted),
        ephemeral_public_key=public_jwk(ephemeral.public_key()),
        kdf_salt=_b64(salt),
        nonce=_b64(nonce),
    ), secret


def decrypt_owner_credential(
    envelope: OwnerCredentialEnvelope,
    device_private_key: X25519PrivateKey,
    *,
    expected_aad: CredentialEnvelopeAad,
) -> DecryptedOwnerCredential:
    private = _require_private_key(device_private_key)
    if (
        type(envelope) is not OwnerCredentialEnvelope
        or type(expected_aad) is not CredentialEnvelopeAad
        or envelope.aad != expected_aad
        or expected_aad.device_key_thumbprint
        != device_key_thumbprint(public_jwk(private.public_key()))
    ):
        raise OwnerCredentialEnvelopeUnavailable()
    aad_bytes = _jcs(envelope.aad.model_dump(mode="json"))
    salt = _decode(envelope.kdf_salt, 32)
    nonce = _decode(envelope.nonce, 12)
    ciphertext = _decode_variable(envelope.ciphertext, 16, 1024)
    key = _derive(
        private,
        envelope.ephemeral_public_key.key(),
        salt,
        aad_bytes,
    )
    try:
        plaintext = AESGCM(key).decrypt(
            nonce, ciphertext, aad_bytes
        )
        decoded: object = json.loads(plaintext)
        if type(decoded) is not dict:
            raise OwnerCredentialEnvelopeUnavailable()
        decoded_object = cast(dict[str, object], decoded)
        if _jcs(decoded_object) != plaintext:
            raise OwnerCredentialEnvelopeUnavailable()
        return DecryptedOwnerCredential.model_validate(
            decoded_object
        )
    except (InvalidTag, ValueError, UnicodeDecodeError) as error:
        raise OwnerCredentialEnvelopeUnavailable() from error


def serialize_owner_credential_envelope(
    envelope: OwnerCredentialEnvelope,
) -> bytes:
    if type(envelope) is not OwnerCredentialEnvelope:
        raise OwnerCredentialEnvelopeUnavailable()
    return _jcs(envelope.model_dump(mode="json"))


def parse_owner_credential_envelope(payload: bytes) -> OwnerCredentialEnvelope:
    if type(payload) is not bytes or not 2 <= len(payload) <= 8 * 1024:
        raise OwnerCredentialEnvelopeUnavailable()

    _validate_json_lexical_depth(payload, maximum=8)

    def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise OwnerCredentialEnvelopeUnavailable()
            result[key] = value
        return result

    try:
        decoded: object = json.loads(payload, object_pairs_hook=exact_object)
        if type(decoded) is not dict:
            raise OwnerCredentialEnvelopeUnavailable()
        envelope_object = cast(dict[str, object], decoded)
        if _json_depth(envelope_object) > 4:
            raise OwnerCredentialEnvelopeUnavailable()
        aad_object = envelope_object.get("aad")
        if type(aad_object) is not dict:
            raise OwnerCredentialEnvelopeUnavailable()
        aad_values = cast(dict[str, object], aad_object)
        scope = aad_values.get("scope")
        if type(scope) is not list:
            raise OwnerCredentialEnvelopeUnavailable()
        aad_values["scope"] = tuple(cast(list[object], scope))
        return OwnerCredentialEnvelope.model_validate(envelope_object)
    except OwnerCredentialEnvelopeUnavailable:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise OwnerCredentialEnvelopeUnavailable() from error


def _validate_json_lexical_depth(payload: bytes, *, maximum: int) -> None:
    depth = 0
    quoted = False
    escaped = False
    for byte in payload:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                quoted = False
        elif byte == 0x22:
            quoted = True
        elif byte in {0x5B, 0x7B}:
            depth += 1
            if depth > maximum:
                raise OwnerCredentialEnvelopeUnavailable()
        elif byte in {0x5D, 0x7D}:
            depth -= 1
            if depth < 0:
                raise OwnerCredentialEnvelopeUnavailable()
    if quoted or escaped or depth != 0:
        raise OwnerCredentialEnvelopeUnavailable()


def _json_depth(value: object) -> int:
    if type(value) is list:
        values = cast(list[object], value)
        return 1 + max((_json_depth(item) for item in values), default=0)
    if type(value) is dict:
        values = cast(dict[str, object], value)
        return 1 + max((_json_depth(item) for item in values.values()), default=0)
    return 0


__all__ = [
    "CredentialEnvelopeAad",
    "DecryptedOwnerCredential",
    "EncryptedOwnerCredential",
    "OwnerCredentialEnvelope",
    "OwnerCredentialEnvelopeUnavailable",
    "SUITE",
    "X25519PublicJwk",
    "decrypt_owner_credential",
    "device_key_thumbprint",
    "encrypt_owner_credential",
    "encrypt_owner_credential_with_verifier",
    "generate_device_keypair",
    "parse_owner_credential_envelope",
    "public_jwk",
    "serialize_owner_credential_envelope",
]
