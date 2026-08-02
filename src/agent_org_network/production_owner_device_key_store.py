"""OS-keychain/Windows-DPAPI storage for one canonical Owner secret bundle item."""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from contextlib import contextmanager
import ctypes
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
_WINDOWS_MAX_BLOB_BYTES = 256 * 1024


def _windows_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and getattr(info, "st_file_attributes", 0) & marker)


def _safe_windows_regular(path: Path, *, allow_missing: bool = False) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return allow_missing
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and not path.is_symlink() and not _windows_reparse(info)


def _safe_windows_directory(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not path.is_symlink() and not _windows_reparse(info)


class _Backend(Protocol):
    priority: object

    def get_password(self, service: str, username: str) -> str | None: ...
    def set_password(self, service: str, username: str, password: str) -> None: ...
    def delete_password(self, service: str, username: str) -> None: ...


class _WindowsProtectFn(Protocol):
    def __call__(self, *args: object) -> int: ...


class _WindowsFreeFn(Protocol):
    def __call__(self, pointer: object) -> object: ...


class _WindowsDataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", ctypes.c_uint32),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _WindowsDpapiBackend:
    """Current-user DPAPI-backed secret store for Windows native installs.

    The bundle remains encrypted by Windows DPAPI before it reaches disk.  The
    file is only a transport for the DPAPI blob; no plaintext credential or
    private key is written as a fallback.  The public keyring backend remains
    the preferred path on macOS/Linux where the configured native keyring is
    available.
    """

    _root: Path
    _protect: _WindowsProtectFn
    _unprotect: _WindowsProtectFn
    _local_free: _WindowsFreeFn

    def __init__(self, root: Path) -> None:
        if os.name != "nt":
            raise OwnerDeviceKeyStoreUnavailable()
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            protect = crypt32.CryptProtectData
            unprotect = crypt32.CryptUnprotectData
            protect.argtypes = [
                ctypes.POINTER(_WindowsDataBlob), ctypes.c_wchar_p,
                ctypes.POINTER(_WindowsDataBlob), ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(_WindowsDataBlob),
            ]
            protect.restype = ctypes.c_int
            unprotect.argtypes = [
                ctypes.POINTER(_WindowsDataBlob), ctypes.c_wchar_p,
                ctypes.POINTER(_WindowsDataBlob), ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_uint32,
                ctypes.POINTER(_WindowsDataBlob),
            ]
            unprotect.restype = ctypes.c_int
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not _safe_windows_directory(root):
                raise OwnerDeviceKeyStoreUnavailable()
            self._root = root
            self._protect = cast(_WindowsProtectFn, protect)
            self._unprotect = cast(_WindowsProtectFn, unprotect)
            self._local_free = cast(_WindowsFreeFn, kernel32.LocalFree)
        except OwnerDeviceKeyStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerDeviceKeyStoreUnavailable() from error

    @staticmethod
    def _blob(value: bytes) -> tuple[ctypes.Array[ctypes.c_char], _WindowsDataBlob]:
        if not 1 <= len(value) <= _WINDOWS_MAX_BLOB_BYTES:
            raise OwnerDeviceKeyStoreUnavailable()
        buffer = ctypes.create_string_buffer(value)
        pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        return buffer, _WindowsDataBlob(len(value), pointer)

    def _path(self, username: str) -> Path:
        if re.fullmatch(r"[0-9a-f]{64}", username) is None:
            raise OwnerDeviceKeyStoreUnavailable()
        return self._root / f"{username}.dpapi"

    def _protect_value(self, value: str) -> str:
        raw = value.encode("utf-8")
        source_buffer, source = self._blob(raw)
        result = _WindowsDataBlob()
        if not self._protect(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)
        ):
            raise OwnerDeviceKeyStoreUnavailable()
        try:
            encrypted = ctypes.string_at(result.pbData, result.cbData)
        finally:
            self._local_free(result.pbData)
        _ = source_buffer
        return _b64(encrypted)

    def _unprotect_value(self, value: str) -> str:
        encrypted = _unb64(value)
        source_buffer, source = self._blob(encrypted)
        result = _WindowsDataBlob()
        if not self._unprotect(
            ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)
        ):
            raise OwnerDeviceKeyStoreUnavailable()
        try:
            plaintext = ctypes.string_at(result.pbData, result.cbData)
        finally:
            self._local_free(result.pbData)
        _ = source_buffer
        if not 1 <= len(plaintext) <= _WINDOWS_MAX_BLOB_BYTES:
            raise OwnerDeviceKeyStoreUnavailable()
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise OwnerDeviceKeyStoreUnavailable() from error

    def get_password(self, service: str, username: str) -> str | None:
        if service != _SERVICE:
            raise OwnerDeviceKeyStoreUnavailable()
        path = self._path(username)
        try:
            if path.is_symlink():
                raise OwnerDeviceKeyStoreUnavailable()
            if not path.exists():
                return None
            if not _safe_windows_regular(path) or path.stat().st_size > _WINDOWS_MAX_BLOB_BYTES * 2:
                raise OwnerDeviceKeyStoreUnavailable()
            return self._unprotect_value(path.read_text(encoding="ascii"))
        except OwnerDeviceKeyStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerDeviceKeyStoreUnavailable() from error

    def set_password(self, service: str, username: str, password: str) -> None:
        if service != _SERVICE or type(password) is not str:
            raise OwnerDeviceKeyStoreUnavailable()
        path = self._path(username)
        if not _safe_windows_regular(path, allow_missing=True):
            raise OwnerDeviceKeyStoreUnavailable()
        payload = self._protect_value(password).encode("ascii")
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(16)}.tmp")
        descriptor: int | None = None
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            descriptor = None
        except OwnerDeviceKeyStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerDeviceKeyStoreUnavailable() from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def delete_password(self, service: str, username: str) -> None:
        if service != _SERVICE:
            raise OwnerDeviceKeyStoreUnavailable()
        path = self._path(username)
        try:
            if path.exists() and not _safe_windows_regular(path):
                raise OwnerDeviceKeyStoreUnavailable()
            path.unlink(missing_ok=True)
        except OwnerDeviceKeyStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerDeviceKeyStoreUnavailable() from error


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
        if any(
            name in _OVERRIDES or name.startswith("KEYRING_PROPERTY_")
            for name in os.environ
        ):
            raise OwnerDeviceKeyStoreUnavailable()
        root = Path(lock_root)
        if os.name == "nt":
            self._backend = cast(_Backend, _WindowsDpapiBackend(root))
            self._root = root
            return
        try:
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
        if os.name == "nt":
            import msvcrt

            lock_path = self._root / (_account(key) + ".lock")
            if lock_path.is_symlink() or (
                lock_path.exists() and not _safe_windows_regular(lock_path)
            ):
                raise OwnerDeviceKeyStoreUnavailable()
            descriptor = os.open(
                lock_path,
                os.O_CREAT
                | os.O_RDWR
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                if not _safe_windows_regular(lock_path):
                    raise OwnerDeviceKeyStoreUnavailable()
                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                yield
            finally:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                finally:
                    os.close(descriptor)
            return
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
        account = secrets.token_hex(32)
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
