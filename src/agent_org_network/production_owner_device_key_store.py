"""OS-keychain-only storage for one canonical Owner secret bundle item."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import contextmanager
import importlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
from typing import Generator, Protocol, cast

from pydantic import SecretBytes

from agent_org_network.owner_device_key_store import (
    OwnerCredentialSlotV1,
    OwnerDeviceKeyMaterialV1,
    OwnerDeviceKeyStoreConflict,
    OwnerDeviceKeyStoreUnavailable,
    OwnerInstallationPublicBindingV1,
    OwnerInstallationSecretBundleV1,
    OwnerPairingPendingV1,
    OwnerSecretBundleWriteReceipt,
    binding_digest,
    bundle_public_digest,
    owner_keychain_account_ref,
)

_ALLOWED = {
    ("keyring.backends.macOS", "Keyring"),
    ("keyring.backends.SecretService", "Keyring"),
}
_OVERRIDES = {"PYTHON_KEYRING_BACKEND", "PYTHON_KEYRING_PATH"}
_SERVICE = "agent-org-network.owner-secret-bundle.v1"


class _Backend(Protocol):
    priority: object

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


def _b64(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _unb64(value: object) -> bytes:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise OwnerDeviceKeyStoreUnavailable()
    try:
        decoded = urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except ValueError as error:
        raise OwnerDeviceKeyStoreUnavailable() from error
    if _b64(decoded) != value:
        raise OwnerDeviceKeyStoreUnavailable()
    return decoded


def _account(key: str) -> str:
    return owner_keychain_account_ref(key)


def encode_owner_secret_bundle(value: OwnerInstallationSecretBundleV1) -> str:
    if type(value) is not OwnerInstallationSecretBundleV1:
        raise OwnerDeviceKeyStoreUnavailable()

    def slot(item: OwnerCredentialSlotV1 | None) -> object:
        if item is None:
            return None
        result = item.model_dump(mode="json", exclude={"credential_secret"})
        result["credential_secret"] = _b64(item.credential_secret.get_secret_value())
        return result

    device = value.device.model_dump(
        mode="json", exclude={"private_key_pkcs8_der"}
    )
    device["private_key_pkcs8_der"] = _b64(
        value.device.private_key_pkcs8_der.get_secret_value()
    )
    payload = {
        "active": slot(value.active),
        "binding": value.binding.model_dump(mode="json"),
        "binding_digest": value.binding_digest,
        "bundle_revision": value.bundle_revision,
        "device": device,
        "pairing_pending": (
            value.pairing_pending.model_dump(mode="json")
            if value.pairing_pending is not None
            else None
        ),
        "pending": slot(value.pending),
        "schema_version": value.schema_version,
    }
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def decode_owner_secret_bundle(raw: str) -> OwnerInstallationSecretBundleV1:
    try:
        def exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, value in pairs:
                if key in result:
                    raise OwnerDeviceKeyStoreUnavailable()
                result[key] = value
            return result

        loaded: object = json.loads(raw, object_pairs_hook=exact_object)
        if type(loaded) is not dict:
            raise OwnerDeviceKeyStoreUnavailable()
        value = cast(dict[str, object], loaded)
        if set(value) != {
            "active",
            "binding",
            "binding_digest",
            "bundle_revision",
            "device",
            "pairing_pending",
            "pending",
            "schema_version",
        }:
            raise OwnerDeviceKeyStoreUnavailable()
        device_raw = value["device"]
        if type(device_raw) is not dict:
            raise OwnerDeviceKeyStoreUnavailable()
        device = cast(dict[str, object], device_raw)
        if set(device) != {
            "device_key_thumbprint",
            "key_material_version",
            "private_key_pkcs8_der",
            "public_key",
        }:
            raise OwnerDeviceKeyStoreUnavailable()
        device_model = OwnerDeviceKeyMaterialV1.model_validate(
            {
                **device,
                "private_key_pkcs8_der": SecretBytes(
                    _unb64(device["private_key_pkcs8_der"])
                ),
            }
        )

        def slot(raw_slot: object) -> OwnerCredentialSlotV1 | None:
            if raw_slot is None:
                return None
            if type(raw_slot) is not dict:
                raise OwnerDeviceKeyStoreUnavailable()
            data = cast(dict[str, object], raw_slot)
            expected = set(OwnerCredentialSlotV1.model_fields)
            if set(data) != expected:
                raise OwnerDeviceKeyStoreUnavailable()
            scope = data.get("scope")
            if type(scope) is not list:
                raise OwnerDeviceKeyStoreUnavailable()
            scope_items = cast(list[object], scope)
            if any(type(item) is not str for item in scope_items):
                raise OwnerDeviceKeyStoreUnavailable()
            return OwnerCredentialSlotV1.model_validate(
                {
                    **data,
                    "scope": tuple(cast(list[str], scope_items)),
                    "credential_secret": SecretBytes(
                        _unb64(data["credential_secret"])
                    ),
                }
            )

        result = OwnerInstallationSecretBundleV1.model_validate(
            {
                "schema_version": value["schema_version"],
                "bundle_revision": value["bundle_revision"],
                "binding": OwnerInstallationPublicBindingV1.model_validate(
                    value["binding"]
                ),
                "binding_digest": value["binding_digest"],
                "device": device_model,
                "pairing_pending": (
                    OwnerPairingPendingV1.model_validate(value["pairing_pending"])
                    if value["pairing_pending"] is not None
                    else None
                ),
                "active": slot(value["active"]),
                "pending": slot(value["pending"]),
            }
        )
        if encode_owner_secret_bundle(result) != raw:
            raise OwnerDeviceKeyStoreUnavailable()
        return result
    except OwnerDeviceKeyStoreUnavailable:
        raise
    except Exception as error:
        raise OwnerDeviceKeyStoreUnavailable() from error


class ProductionOwnerDeviceKeyStore:
    def __init__(self, lock_root: str | Path) -> None:
        if os.name == "nt" or any(
            name in _OVERRIDES or name.startswith("KEYRING_PROPERTY_")
            for name in os.environ
        ):
            raise OwnerDeviceKeyStoreUnavailable()
        try:
            root = Path(lock_root)
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(root, 0o700)
            info = root.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
                raise OwnerDeviceKeyStoreUnavailable()
            module = importlib.import_module("keyring")
            backend = module.get_keyring()
            priority = getattr(backend, "priority", 0)
            if (
                (type(backend).__module__, type(backend).__qualname__) not in _ALLOWED
                or type(priority) not in {int, float}
                or priority <= 0
            ):
                raise OwnerDeviceKeyStoreUnavailable()
            self._backend = cast(_Backend, backend)
            self._root = root
        except OwnerDeviceKeyStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerDeviceKeyStoreUnavailable() from error

    @contextmanager
    def _lock(self, key: str) -> Generator[None]:
        import fcntl

        descriptor = os.open(
            self._root / (_account(key) + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
                raise OwnerDeviceKeyStoreUnavailable()
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _raw(self, key: str) -> str | None:
        try:
            return self._backend.get_password(_SERVICE, _account(key))
        except Exception as error:
            raise OwnerDeviceKeyStoreUnavailable() from error

    def load(self, key: str) -> OwnerInstallationSecretBundleV1 | None:
        with self._lock(key):
            raw = self._raw(key)
            return decode_owner_secret_bundle(raw) if raw is not None else None

    def replace(
        self,
        key: str,
        *,
        expected_revision: int | None,
        value: OwnerInstallationSecretBundleV1,
    ) -> OwnerSecretBundleWriteReceipt:
        if type(value) is not OwnerInstallationSecretBundleV1:
            raise OwnerDeviceKeyStoreUnavailable()
        value = decode_owner_secret_bundle(encode_owner_secret_bundle(value))
        with self._lock(key):
            old_raw = self._raw(key)
            old = decode_owner_secret_bundle(old_raw) if old_raw is not None else None
            if (
                (old is None and (expected_revision is not None or value.bundle_revision != 1))
                or (
                    old is not None
                    and (
                        expected_revision != old.bundle_revision
                        or value.bundle_revision != old.bundle_revision + 1
                    )
                )
                or (
                    old is not None
                    and old.active is not None
                    and value.active != old.active
                )
                or (
                    old is not None
                    and old.active is None
                    and value.active is not None
                    and old.pending is not None
                )
            ):
                raise OwnerDeviceKeyStoreConflict()
            raw = encode_owner_secret_bundle(value)
            try:
                self._backend.set_password(_SERVICE, _account(key), raw)
                if self._raw(key) != raw:
                    raise OwnerDeviceKeyStoreUnavailable()
            except OwnerDeviceKeyStoreUnavailable:
                raise
            except Exception as error:
                raise OwnerDeviceKeyStoreUnavailable() from error
            return OwnerSecretBundleWriteReceipt(
                bundle_revision=value.bundle_revision,
                bundle_public_digest=bundle_public_digest(value),
            )

    def create_pending(
        self,
        key: str,
        *,
        binding: OwnerInstallationPublicBindingV1,
        device: OwnerDeviceKeyMaterialV1,
        pairing_pending: OwnerPairingPendingV1,
    ) -> OwnerSecretBundleWriteReceipt:
        return self.replace(
            key,
            expected_revision=None,
            value=OwnerInstallationSecretBundleV1(
                bundle_revision=1,
                binding=binding,
                binding_digest=binding_digest(binding),
                device=device,
                pairing_pending=pairing_pending,
                active=None,
                pending=None,
            ),
        )

    def store_active(
        self,
        key: str,
        *,
        expected_revision: int,
        slot: OwnerCredentialSlotV1,
        keep_pairing_pending: bool,
    ) -> OwnerSecretBundleWriteReceipt:
        old = self.load(key)
        if (
            old is None
            or old.bundle_revision != expected_revision
            or old.active is not None
            or old.pending is not None
        ):
            raise OwnerDeviceKeyStoreConflict()
        return self.replace(
            key,
            expected_revision=expected_revision,
            value=old.model_copy(
                update={
                    "bundle_revision": expected_revision + 1,
                    "active": slot,
                    "pairing_pending": (
                        old.pairing_pending if keep_pairing_pending else None
                    ),
                }
            ),
        )

    def clear_pairing_pending(
        self, key: str, *, expected_revision: int
    ) -> OwnerSecretBundleWriteReceipt:
        old = self.load(key)
        if old is None or old.bundle_revision != expected_revision or old.active is None:
            raise OwnerDeviceKeyStoreConflict()
        return self.replace(
            key,
            expected_revision=expected_revision,
            value=old.model_copy(
                update={
                    "bundle_revision": expected_revision + 1,
                    "pairing_pending": None,
                }
            ),
        )

    def delete(self, key: str, *, expected_revision: int) -> None:
        with self._lock(key):
            raw = self._raw(key)
            if raw is None or decode_owner_secret_bundle(raw).bundle_revision != expected_revision:
                raise OwnerDeviceKeyStoreConflict()
            try:
                self._backend.delete_password(_SERVICE, _account(key))
                if self._raw(key) is not None:
                    raise OwnerDeviceKeyStoreUnavailable()
            except OwnerDeviceKeyStoreUnavailable:
                raise
            except Exception as error:
                raise OwnerDeviceKeyStoreUnavailable() from error

    def probe(self) -> bool:
        account = "probe-" + secrets.token_hex(16)
        value = secrets.token_urlsafe(32)
        try:
            self._backend.set_password(_SERVICE, account, value)
            if self._backend.get_password(_SERVICE, account) != value:
                raise OwnerDeviceKeyStoreUnavailable()
            self._backend.delete_password(_SERVICE, account)
            return self._backend.get_password(_SERVICE, account) is None
        except OwnerDeviceKeyStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerDeviceKeyStoreUnavailable() from error


__all__ = [
    "ProductionOwnerDeviceKeyStore",
    "decode_owner_secret_bundle",
    "encode_owner_secret_bundle",
]
