"""Encrypted-at-rest repository owned only by the Card Owner installation."""

from __future__ import annotations

from base64 import b64decode, b64encode
from collections.abc import Mapping
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal, Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, Field, field_validator


class OwnerLocalAuthoringError(Exception):
    pass


class OwnerLocalAuthoringUnavailable(OwnerLocalAuthoringError):
    pass


class OwnerLocalAuthoringConflict(OwnerLocalAuthoringError):
    pass


_OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_DIGEST = re.compile(r"[0-9a-f]{64}")
_TEMP_NAME = re.compile(r"\.tmp-[0-9]+-[0-9a-f]{24}")
_QUARANTINE_NAME = re.compile(r"\.quarantine-[0-9a-f]{48}")
_ENVELOPE_FIELDS = frozenset(
    {"version", "key_id", "artifact", "nonce", "ciphertext"}
)
# O3a central source admission allows at most 100 MiB total.  A single local
# artifact uses the same ceiling; base64 plus bounded metadata sets the disk cap.
MAX_PLAINTEXT_BYTES = 100 * 1024 * 1024
MAX_ENVELOPE_BYTES = 140 * 1024 * 1024


class AuthoringArtifactRef(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    organization_id: str
    agent_id: str
    run_id: str
    revision: int
    artifact_kind: Literal["raw_source", "full_draft_bundle"]
    artifact_digest: str

    @field_validator("organization_id", "agent_id", "run_id")
    @classmethod
    def _opaque(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque reference required")
        return value

    @field_validator("revision")
    @classmethod
    def _revision(cls, value: int) -> int:
        if not 0 <= value <= 1_000_000:
            raise ValueError("bounded nonnegative revision required")
        return value

    @field_validator("artifact_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if _DIGEST.fullmatch(value) is None:
            raise ValueError("lowercase sha256 required")
        return value


class OwnerLocalAuthoringKey(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    key_id: str
    key: bytes = Field(repr=False)

    @field_validator("key_id")
    @classmethod
    def _key_id(cls, value: str) -> str:
        if _OPAQUE.fullmatch(value) is None:
            raise ValueError("bounded opaque key reference required")
        return value

    @field_validator("key")
    @classmethod
    def _key(cls, value: bytes) -> bytes:
        if len(value) != 32:
            raise ValueError("AES-256 key required")
        return value


class OwnerLocalAuthoringKeyProvider(Protocol):
    def current(self) -> OwnerLocalAuthoringKey: ...


class OwnerLocalAuthoringPutResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    artifact: AuthoringArtifactRef
    replayed: bool = False


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


class OwnerLocalAuthoringRepository:
    def __init__(
        self, root: str | Path, *, keys: OwnerLocalAuthoringKeyProvider
    ) -> None:
        if not callable(getattr(keys, "current", None)):
            raise ValueError("explicit current key provider required")
        self._keys = keys
        self._root = Path(root)
        if self._root.is_symlink():
            raise OwnerLocalAuthoringUnavailable()
        try:
            self._root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self._root.is_symlink() or not self._root.is_dir():
                raise OwnerLocalAuthoringUnavailable()
            self._root_fd = os.open(
                self._root,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            os.fchmod(self._root_fd, 0o700)
            fcntl.flock(self._root_fd, fcntl.LOCK_EX)
            try:
                self._cleanup_temps()
            finally:
                fcntl.flock(self._root_fd, fcntl.LOCK_UN)
        except OwnerLocalAuthoringUnavailable:
            raise
        except OSError as error:
            raise OwnerLocalAuthoringUnavailable() from error

    @staticmethod
    def _identity(ref: AuthoringArtifactRef) -> bytes:
        return _canonical_json(ref.model_dump(mode="json"))

    def path_for(self, ref: AuthoringArtifactRef) -> Path:
        return self._root / self._name(ref)

    def _name(self, ref: AuthoringArtifactRef) -> str:
        if type(ref) is not AuthoringArtifactRef:
            raise OwnerLocalAuthoringUnavailable()
        return sha256(self._identity(ref)).hexdigest() + ".aon"

    def put(
        self, ref: AuthoringArtifactRef, plaintext: bytes
    ) -> OwnerLocalAuthoringPutResult:
        if type(ref) is not AuthoringArtifactRef or type(plaintext) is not bytes:
            raise OwnerLocalAuthoringUnavailable()
        fcntl.flock(self._root_fd, fcntl.LOCK_EX)
        try:
            return self._put_locked(ref, plaintext)
        finally:
            fcntl.flock(self._root_fd, fcntl.LOCK_UN)

    def _put_locked(
        self, ref: AuthoringArtifactRef, plaintext: bytes
    ) -> OwnerLocalAuthoringPutResult:
        if len(plaintext) > MAX_PLAINTEXT_BYTES:
            raise OwnerLocalAuthoringConflict()
        name = self._name(ref)
        existing_stat = self._stat_entry(name)
        if existing_stat is not None:
            existing, _ = self._read_locked(ref)
            if existing != plaintext:
                raise OwnerLocalAuthoringConflict()
            return OwnerLocalAuthoringPutResult(artifact=ref, replayed=True)
        if sha256(plaintext).hexdigest() != ref.artifact_digest:
            raise OwnerLocalAuthoringConflict()
        key = self._current_key()
        nonce = os.urandom(12)
        aad = self._aad(ref, key.key_id)
        ciphertext = AESGCM(key.key).encrypt(nonce, plaintext, aad)
        envelope = _canonical_json(
            {
                "version": 1,
                "key_id": key.key_id,
                "artifact": ref.model_dump(mode="json"),
                "nonce": b64encode(nonce).decode("ascii"),
                "ciphertext": b64encode(ciphertext).decode("ascii"),
            }
        )
        if len(envelope) > MAX_ENVELOPE_BYTES:
            raise OwnerLocalAuthoringConflict()
        temporary_name = f".tmp-{os.getpid()}-{os.urandom(12).hex()}"
        descriptor = -1
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=self._root_fd,
            )
            written = 0
            while written < len(envelope):
                written += os.write(descriptor, envelope[written:])
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            if self._stat_entry(name) is not None:
                raise OwnerLocalAuthoringConflict()
            os.replace(
                temporary_name,
                name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            os.fsync(self._root_fd)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary_name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass
            raise
        return OwnerLocalAuthoringPutResult(artifact=ref)

    def read(self, ref: AuthoringArtifactRef) -> bytes:
        fcntl.flock(self._root_fd, fcntl.LOCK_SH)
        try:
            plaintext, _ = self._read_locked(ref)
            return plaintext
        finally:
            fcntl.flock(self._root_fd, fcntl.LOCK_UN)

    def _read_locked(
        self, ref: AuthoringArtifactRef
    ) -> tuple[bytes, os.stat_result]:
        return self._read_named_locked(ref, self._name(ref))

    def _read_named_locked(
        self, ref: AuthoringArtifactRef, name: str
    ) -> tuple[bytes, os.stat_result]:
        try:
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=self._root_fd,
            )
            try:
                opened_stat = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(opened_stat.st_mode)
                    or opened_stat.st_size > MAX_ENVELOPE_BYTES
                ):
                    raise ValueError
                chunks: list[bytes] = []
                while chunk := os.read(descriptor, 1024 * 1024):
                    chunks.append(chunk)
                raw = b"".join(chunks)
            finally:
                os.close(descriptor)
            if len(raw) > MAX_ENVELOPE_BYTES:
                raise ValueError
            value = cast(
                object,
                json.loads(raw, object_pairs_hook=self._reject_duplicate_keys),
            )
            if not isinstance(value, dict):
                raise ValueError
            envelope: Mapping[str, object] = cast(dict[str, object], value)
            if frozenset(envelope) != _ENVELOPE_FIELDS:
                raise ValueError
            if envelope["version"] != 1:
                raise ValueError
            artifact = AuthoringArtifactRef.model_validate(envelope["artifact"])
            if artifact != ref:
                raise ValueError
            key = self._current_key()
            if envelope["key_id"] != key.key_id:
                raise ValueError
            nonce = b64decode(str(envelope["nonce"]), validate=True)
            ciphertext = b64decode(str(envelope["ciphertext"]), validate=True)
            if len(nonce) != 12:
                raise ValueError
            plaintext = AESGCM(key.key).decrypt(
                nonce, ciphertext, self._aad(ref, key.key_id)
            )
            if sha256(plaintext).hexdigest() != ref.artifact_digest:
                raise ValueError
            if len(plaintext) > MAX_PLAINTEXT_BYTES:
                raise ValueError
            if _canonical_json(envelope) != raw:
                raise ValueError
            return plaintext, opened_stat
        except (InvalidTag, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise OwnerLocalAuthoringUnavailable() from error

    def delete(self, ref: AuthoringArtifactRef) -> bool:
        fcntl.flock(self._root_fd, fcntl.LOCK_EX)
        try:
            name = self._name(ref)
            current = self._stat_entry(name)
            if current is None:
                return False
            quarantine = f".quarantine-{os.urandom(24).hex()}"
            os.rename(
                name,
                quarantine,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            _, opened = self._read_named_locked(ref, quarantine)
            current = self._stat_entry(quarantine)
            if current is None or (
                current.st_dev,
                current.st_ino,
            ) != (opened.st_dev, opened.st_ino):
                raise OwnerLocalAuthoringUnavailable()
            os.unlink(quarantine, dir_fd=self._root_fd)
            os.fsync(self._root_fd)
            return True
        except OSError as error:
            raise OwnerLocalAuthoringUnavailable() from error
        finally:
            fcntl.flock(self._root_fd, fcntl.LOCK_UN)

    def _current_key(self) -> OwnerLocalAuthoringKey:
        try:
            key = self._keys.current()
        except Exception as error:
            if isinstance(error, OwnerLocalAuthoringUnavailable):
                raise
            raise OwnerLocalAuthoringUnavailable() from error
        if type(key) is not OwnerLocalAuthoringKey:
            raise OwnerLocalAuthoringUnavailable()
        return key

    @classmethod
    def _aad(cls, ref: AuthoringArtifactRef, key_id: str) -> bytes:
        return _canonical_json(
            {
                "version": 1,
                "key_id": key_id,
                "artifact": ref.model_dump(mode="json"),
            }
        )

    def _cleanup_temps(self) -> None:
        for name in os.listdir(self._root_fd):
            if name.startswith(".quarantine-"):
                if _QUARANTINE_NAME.fullmatch(name) is None:
                    raise OwnerLocalAuthoringUnavailable()
                entry = self._stat_entry(name)
                if entry is None or not stat.S_ISREG(entry.st_mode):
                    raise OwnerLocalAuthoringUnavailable()
                # A quarantine means a prior delete did not prove safe removal.
                # Never infer that its encrypted or foreign contents are disposable.
                raise OwnerLocalAuthoringUnavailable()
            if not name.startswith(".tmp-"):
                continue
            if _TEMP_NAME.fullmatch(name) is None:
                raise OwnerLocalAuthoringUnavailable()
            entry = self._stat_entry(name)
            if entry is None or not stat.S_ISREG(entry.st_mode):
                raise OwnerLocalAuthoringUnavailable()
            os.unlink(name, dir_fd=self._root_fd)
        os.fsync(self._root_fd)

    def _stat_entry(self, name: str) -> os.stat_result | None:
        try:
            entry = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(entry.st_mode):
            raise OwnerLocalAuthoringUnavailable()
        return entry

    @staticmethod
    def _reject_duplicate_keys(
        pairs: list[tuple[str, object]],
    ) -> dict[str, object]:
        value: dict[str, object] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate envelope key")
            value[key] = item
        return value


__all__ = [
    "AuthoringArtifactRef",
    "MAX_ENVELOPE_BYTES",
    "MAX_PLAINTEXT_BYTES",
    "OwnerLocalAuthoringConflict",
    "OwnerLocalAuthoringError",
    "OwnerLocalAuthoringKey",
    "OwnerLocalAuthoringKeyProvider",
    "OwnerLocalAuthoringPutResult",
    "OwnerLocalAuthoringRepository",
    "OwnerLocalAuthoringUnavailable",
]
