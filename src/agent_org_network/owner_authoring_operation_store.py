"""Encrypted durable operation state for Card Owner OKF authoring."""

from __future__ import annotations

from base64 import b64decode, b64encode
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import stat
from threading import Lock, RLock
from collections.abc import Callable
from typing import Literal
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from agent_org_network.owner_local_authoring_repository import (
    OwnerLocalAuthoringKeyProvider,
)


class OwnerAuthoringOperationStoreUnavailable(Exception):
    pass


class OwnerAuthoringOperationState(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)

    identity_digest: str
    stage: Literal["started", "draft_ready", "completed"]
    payload: dict[str, object]

    @field_validator("identity_digest")
    @classmethod
    def _digest(cls, value: str) -> str:
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise ValueError("lowercase sha256 required")
        return value

    @model_validator(mode="after")
    def _payload_shape(self) -> "OwnerAuthoringOperationState":
        expected = {
            "started": {"start_command", "run"},
            "draft_ready": {
                "start_command",
                "run",
                "draft_ref",
                "draft_bundle_base64",
                "complete_command",
            },
            "completed": {
                "start_command",
                "run",
                "draft_ref",
                "draft_bundle_base64",
                "complete_command",
                "result",
            },
        }[self.stage]
        if set(self.payload) != expected:
            raise ValueError("exact stage payload required")
        return self


_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner_authoring_operations (
 operation_key TEXT PRIMARY KEY,
 identity_digest TEXT NOT NULL,
 stage TEXT NOT NULL,
 key_id TEXT NOT NULL,
 nonce TEXT NOT NULL,
 ciphertext TEXT NOT NULL,
 CHECK(length(operation_key)=64),
 CHECK(length(identity_digest)=64),
 CHECK(stage IN ('started','draft_ready','completed')),
 CHECK(length(nonce)=16),
 CHECK(length(ciphertext)>0)
) STRICT;
CREATE TRIGGER IF NOT EXISTS owner_authoring_operations_no_delete
BEFORE DELETE ON owner_authoring_operations BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER IF NOT EXISTS owner_authoring_operations_exact_update
BEFORE UPDATE ON owner_authoring_operations
WHEN OLD.operation_key != NEW.operation_key
 OR OLD.identity_digest != NEW.identity_digest
 OR NOT (
   (OLD.stage='started' AND NEW.stage='draft_ready')
   OR (OLD.stage='draft_ready' AND NEW.stage='completed')
 )
BEGIN SELECT RAISE(ABORT,'invalid transition'); END;
"""
MAX_OPERATION_CIPHERTEXT_BYTES = 140 * 1024 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    objects = tuple(
        connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        ).fetchall()
    )
    table_xinfo = tuple(
        connection.execute("PRAGMA table_xinfo(owner_authoring_operations)").fetchall()
    )
    index_list = tuple(
        connection.execute("PRAGMA index_list(owner_authoring_operations)").fetchall()
    )
    index_xinfo = tuple(
        (
            row[1],
            tuple(connection.execute(f"PRAGMA index_xinfo('{row[1]}')").fetchall()),
        )
        for row in index_list
    )
    foreign_keys = tuple(
        connection.execute(
            "PRAGMA foreign_key_list(owner_authoring_operations)"
        ).fetchall()
    )
    table_list = tuple(
        connection.execute("PRAGMA table_list('owner_authoring_operations')").fetchall()
    )
    return objects, table_xinfo, index_list, index_xinfo, foreign_keys, table_list


def _canonical_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _catalog(connection)
    finally:
        connection.close()


_EXPECTED_CATALOG = _canonical_catalog()
_FD_ATTEST_LOCK = Lock()


def _no_fault(_point: str) -> None:
    return None


def _no_connect_hook() -> None:
    return None


class OwnerAuthoringOperationStore:
    def __init__(
        self,
        path: str | Path,
        *,
        keys: OwnerLocalAuthoringKeyProvider,
        fault: Callable[[str], None] | None = None,
        before_connect_hook: Callable[[], None] | None = None,
        connect_hook: Callable[[], None] | None = None,
    ) -> None:
        target = Path(path)
        parent = target.parent
        try:
            if parent.resolve() != parent.absolute() or parent.is_symlink():
                raise OwnerAuthoringOperationStoreUnavailable()
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if target.exists():
                info = target.lstat()
                if target.is_symlink() or not stat.S_ISREG(info.st_mode):
                    raise OwnerAuthoringOperationStoreUnavailable()
            else:
                descriptor = os.open(
                    target,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.close(descriptor)
            os.chmod(target, 0o600, follow_symlinks=False)
        except OwnerAuthoringOperationStoreUnavailable:
            raise
        except OSError as error:
            raise OwnerAuthoringOperationStoreUnavailable() from error
        self._path = str(target)
        initial = target.lstat()
        self._device_inode = (initial.st_dev, initial.st_ino)
        self._keys = keys
        self._fault: Callable[[str], None] = fault or _no_fault
        self._before_connect_hook: Callable[[], None] = (
            before_connect_hook or _no_connect_hook
        )
        self._connect_hook: Callable[[], None] = connect_hook or _no_connect_hook
        self._attested_fd: int | None = None
        self._lock = RLock()
        try:
            with self._connect() as connection:
                connection.executescript(_SCHEMA)
                self._validate_path()
                self._validate(connection)
        except Exception as error:
            raise OwnerAuthoringOperationStoreUnavailable() from error

    def _validate_path(self) -> None:
        try:
            info = os.lstat(self._path)
            descriptor = os.open(
                self._path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                opened = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            if (
                stat.S_IMODE(info.st_mode) != 0o600
                or not stat.S_ISREG(info.st_mode)
                or (info.st_dev, info.st_ino) != self._device_inode
                or (opened.st_dev, opened.st_ino) != self._device_inode
            ):
                raise OwnerAuthoringOperationStoreUnavailable()
        except OwnerAuthoringOperationStoreUnavailable:
            raise
        except OSError as error:
            raise OwnerAuthoringOperationStoreUnavailable() from error

    def _connect(self) -> sqlite3.Connection:
        with _FD_ATTEST_LOCK:
            self._validate_path()
            before = self._fd_snapshot()
            self._before_connect_hook()
            connection = sqlite3.connect(self._path)
            try:
                self._connect_hook()
                self._validate_path()
                after = self._fd_snapshot()
                candidates = [
                    descriptor
                    for descriptor, identity in after.items()
                    if before.get(descriptor) != identity
                    and identity[:2] == self._device_inode
                    and stat.S_ISREG(identity[2])
                ]
                if len(candidates) != 1:
                    raise OwnerAuthoringOperationStoreUnavailable()
                self._attested_fd = candidates[0]
                self._validate_attested_fd()
                return connection
            except Exception:
                connection.close()
                raise

    @staticmethod
    def _fd_snapshot() -> dict[int, tuple[int, int, int, int]]:
        directory = Path("/proc/self/fd")
        if not directory.is_dir():
            directory = Path("/dev/fd")
        if not directory.is_dir():
            raise OwnerAuthoringOperationStoreUnavailable()
        result: dict[int, tuple[int, int, int, int]] = {}
        try:
            for name in os.listdir(directory):
                if not name.isdigit():
                    continue
                descriptor = int(name)
                try:
                    info = os.fstat(descriptor)
                except OSError:
                    continue
                result[descriptor] = (
                    info.st_dev,
                    info.st_ino,
                    info.st_mode,
                    info.st_nlink,
                )
        except OSError as error:
            raise OwnerAuthoringOperationStoreUnavailable() from error
        return result

    def _validate_attested_fd(self) -> None:
        if self._attested_fd is None:
            raise OwnerAuthoringOperationStoreUnavailable()
        try:
            info = os.fstat(self._attested_fd)
        except OSError as error:
            raise OwnerAuthoringOperationStoreUnavailable() from error
        if (
            (info.st_dev, info.st_ino) != self._device_inode
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OwnerAuthoringOperationStoreUnavailable()

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if _catalog(connection) != _EXPECTED_CATALOG:
            raise OwnerAuthoringOperationStoreUnavailable()

    @staticmethod
    def operation_key(org_id: str, agent_id: str, idempotency_key: str) -> str:
        return sha256(_canonical([org_id, agent_id, idempotency_key])).hexdigest()

    def load(
        self, operation_key: str, identity_digest: str
    ) -> OwnerAuthoringOperationState | None:
        with self._lock:
            try:
                with self._connect() as connection:
                    self._validate(connection)
                    row = connection.execute(
                        "SELECT identity_digest,stage,key_id,nonce,ciphertext "
                        "FROM owner_authoring_operations WHERE operation_key=?",
                        (operation_key,),
                    ).fetchone()
                if row is None:
                    return None
                if row[0] != identity_digest:
                    raise OwnerAuthoringOperationStoreUnavailable()
                key = self._keys.current()
                if row[2] != key.key_id:
                    raise OwnerAuthoringOperationStoreUnavailable()
                if (
                    len(row[3]) != 16
                    or len(row[4]) > (MAX_OPERATION_CIPHERTEXT_BYTES * 4 // 3 + 32)
                    or b64encode(b64decode(row[3], validate=True)).decode() != row[3]
                    or b64encode(b64decode(row[4], validate=True)).decode() != row[4]
                ):
                    raise OwnerAuthoringOperationStoreUnavailable()
                aad = _canonical([operation_key, row[0], row[1], row[2]])
                plaintext = AESGCM(key.key).decrypt(
                    b64decode(row[3], validate=True),
                    b64decode(row[4], validate=True),
                    aad,
                )
                if len(plaintext) > MAX_OPERATION_CIPHERTEXT_BYTES:
                    raise OwnerAuthoringOperationStoreUnavailable()
                state = OwnerAuthoringOperationState.model_validate_json(plaintext)
                if _canonical(state.model_dump(mode="json")) != plaintext:
                    raise OwnerAuthoringOperationStoreUnavailable()
                if state.identity_digest != row[0] or state.stage != row[1]:
                    raise OwnerAuthoringOperationStoreUnavailable()
                return state
            except OwnerAuthoringOperationStoreUnavailable:
                raise
            except (InvalidTag, ValueError, sqlite3.Error) as error:
                raise OwnerAuthoringOperationStoreUnavailable() from error

    def save(
        self,
        operation_key: str,
        state: OwnerAuthoringOperationState,
        *,
        expected_stage: str | None,
    ) -> None:
        with self._lock:
            try:
                key = self._keys.current()
                nonce = os.urandom(12)
                aad = _canonical(
                    [operation_key, state.identity_digest, state.stage, key.key_id]
                )
                ciphertext = AESGCM(key.key).encrypt(
                    nonce, _canonical(state.model_dump(mode="json")), aad
                )
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._fault("after_begin")
                    self._validate_path()
                    self._validate_attested_fd()
                    self._validate(connection)
                    current = connection.execute(
                        "SELECT identity_digest,stage FROM owner_authoring_operations "
                        "WHERE operation_key=?",
                        (operation_key,),
                    ).fetchone()
                    if expected_stage is None:
                        if current is not None:
                            raise OwnerAuthoringOperationStoreUnavailable()
                        connection.execute(
                            "INSERT INTO owner_authoring_operations VALUES (?,?,?,?,?,?)",
                            (
                                operation_key,
                                state.identity_digest,
                                state.stage,
                                key.key_id,
                                b64encode(nonce).decode(),
                                b64encode(ciphertext).decode(),
                            ),
                        )
                        self._fault("after_insert")
                    else:
                        if (
                            current is None
                            or current[0] != state.identity_digest
                            or current[1] != expected_stage
                        ):
                            raise OwnerAuthoringOperationStoreUnavailable()
                        changed = connection.execute(
                            "UPDATE owner_authoring_operations SET stage=?,key_id=?,nonce=?,"
                            "ciphertext=? WHERE operation_key=? AND identity_digest=? AND stage=?",
                            (
                                state.stage,
                                key.key_id,
                                b64encode(nonce).decode(),
                                b64encode(ciphertext).decode(),
                                operation_key,
                                state.identity_digest,
                                expected_stage,
                            ),
                        ).rowcount
                        if changed != 1:
                            raise OwnerAuthoringOperationStoreUnavailable()
                        self._fault("after_update")
                    self._validate_path()
                    self._validate_attested_fd()
                    self._validate(connection)
                    self._fault("before_commit")
                    connection.commit()
            except OwnerAuthoringOperationStoreUnavailable:
                raise
            except Exception as error:
                raise OwnerAuthoringOperationStoreUnavailable() from error


__all__ = [
    "OwnerAuthoringOperationState",
    "OwnerAuthoringOperationStore",
    "OwnerAuthoringOperationStoreUnavailable",
]
