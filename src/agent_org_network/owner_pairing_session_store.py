"""Encrypted owner-local pairing and browser-session authority."""

from __future__ import annotations

from base64 import b64decode, b64encode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from threading import Lock, RLock
from typing import Literal, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator

from agent_org_network.owner_local_authoring_repository import (
    OwnerLocalAuthoringKeyProvider,
)


class OwnerPairingSessionStoreUnavailable(Exception):
    pass


class OwnerPairingBootstrap(BaseModel, frozen=True):
    """Decrypted, owner-local browser-session bootstrap; never a central DTO."""

    model_config = ConfigDict(extra="forbid", strict=True)
    audience: Literal["owner-install"]
    org_id: str
    owner_id: str
    agent_id: str
    card_revision: int
    card_digest: str
    device_public_key_digest: str
    identity_provider: str
    central_identity_session: SecretStr
    credential_generation: int
    owner_session_ttl_seconds: int
    allowed_origin: str
    expires_at: datetime

    @field_validator("central_identity_session")
    @classmethod
    def _secret(cls, value: SecretStr) -> SecretStr:
        if re.fullmatch(r"[A-Za-z0-9_-]{32,128}", value.get_secret_value()) is None:
            raise ValueError("opaque decrypted credential required")
        return value

    @field_validator("allowed_origin")
    @classmethod
    def _origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("exact owner origin required")
        return value.rstrip("/")


class OwnerPairingSession(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    owner_session: SecretStr
    csrf_token: SecretStr
    org_id: str
    owner_id: str
    agent_id: str
    card_revision: int
    card_digest: str
    device_public_key_digest: str
    credential_generation: int
    allowed_origin: str
    expires_at: datetime
    replayed: bool = False


class ResolvedOwnerPairing(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid", strict=True)
    org_id: str
    owner_id: str
    agent_id: str
    card_revision: int
    card_digest: str
    device_public_key_digest: str
    credential_generation: int
    allowed_origin: str
    expires_at: datetime


_SCHEMA = """
CREATE TABLE owner_pairing_sessions (
 pairing_digest TEXT PRIMARY KEY CHECK(length(pairing_digest)=64),
 subject_digest TEXT NOT NULL CHECK(length(subject_digest)=64),
 binding_digest TEXT NOT NULL UNIQUE CHECK(length(binding_digest)=64),
 session_digest TEXT NOT NULL UNIQUE CHECK(length(session_digest)=64),
 csrf_digest TEXT NOT NULL CHECK(length(csrf_digest)=64),
 credential_generation INTEGER NOT NULL CHECK(credential_generation>0),
 expires_at TEXT NOT NULL,
 revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)),
 key_id TEXT NOT NULL,
 nonce TEXT NOT NULL CHECK(length(nonce)=16),
 ciphertext TEXT NOT NULL CHECK(length(ciphertext) BETWEEN 24 AND 21868)
) STRICT;
CREATE UNIQUE INDEX owner_pairing_sessions_one_active_subject
ON owner_pairing_sessions(subject_digest) WHERE revoked=0;
CREATE TRIGGER owner_pairing_sessions_no_delete
BEFORE DELETE ON owner_pairing_sessions BEGIN SELECT RAISE(ABORT,'immutable'); END;
CREATE TRIGGER owner_pairing_sessions_exact_update
BEFORE UPDATE ON owner_pairing_sessions
WHEN OLD.pairing_digest != NEW.pairing_digest
 OR OLD.binding_digest != NEW.binding_digest
 OR OLD.subject_digest != NEW.subject_digest
 OR OLD.session_digest != NEW.session_digest
 OR OLD.csrf_digest != NEW.csrf_digest
 OR OLD.credential_generation != NEW.credential_generation
 OR OLD.expires_at != NEW.expires_at
 OR OLD.key_id != NEW.key_id OR OLD.nonce != NEW.nonce OR OLD.ciphertext != NEW.ciphertext
 OR OLD.revoked != 0 OR NEW.revoked != 1
BEGIN SELECT RAISE(ABORT,'invalid mutation'); END;
"""
_FD_LOCK = Lock()
MAX_SESSION_ENVELOPE_BYTES = 16 * 1024


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _digest(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _catalog(connection: sqlite3.Connection) -> tuple[object, ...]:
    return (
        tuple(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
            ).fetchall()
        ),
        tuple(connection.execute("PRAGMA table_xinfo(owner_pairing_sessions)")),
        tuple(connection.execute("PRAGMA index_list(owner_pairing_sessions)")),
        tuple(connection.execute("PRAGMA table_list('owner_pairing_sessions')")),
    )


def _expected_catalog() -> tuple[object, ...]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(_SCHEMA)
        return _catalog(connection)
    finally:
        connection.close()


_EXPECTED_CATALOG = _expected_catalog()


def _no_fault(_point: str) -> None:
    return None


def _no_connect_hook() -> None:
    return None


def _canonical_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise OwnerPairingSessionStoreUnavailable()
    return value.astimezone(UTC).isoformat()


def _parse_canonical_utc(value: object) -> datetime:
    if type(value) is not str:
        raise OwnerPairingSessionStoreUnavailable()
    parsed = datetime.fromisoformat(value)
    if _canonical_utc(parsed) != value:
        raise OwnerPairingSessionStoreUnavailable()
    return parsed


class OwnerPairingSessionStore:
    def __init__(
        self,
        path: str | Path,
        *,
        keys: OwnerLocalAuthoringKeyProvider,
        clock: Callable[[], datetime],
        fault: Callable[[str], None] | None = None,
        before_connect_hook: Callable[[], None] | None = None,
        connect_hook: Callable[[], None] | None = None,
    ) -> None:
        if not callable(getattr(keys, "current", None)) or not callable(clock):
            raise OwnerPairingSessionStoreUnavailable()
        target = Path(path)
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if (
                target.parent.is_symlink()
                or target.parent.resolve() != target.parent.absolute()
            ):
                raise OwnerPairingSessionStoreUnavailable()
            if target.exists():
                info = target.lstat()
                if target.is_symlink() or not stat.S_ISREG(info.st_mode):
                    raise OwnerPairingSessionStoreUnavailable()
            else:
                fd = os.open(
                    target,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                )
                os.close(fd)
            os.chmod(target, 0o600, follow_symlinks=False)
            self._path = str(target)
            info = target.lstat()
            self._identity = (info.st_dev, info.st_ino)
            self._keys = keys
            self._clock = clock
            self._fault: Callable[[str], None] = fault or _no_fault
            self._before_connect_hook = before_connect_hook or _no_connect_hook
            self._connect_hook = connect_hook or _no_connect_hook
            self._attested_fd: int | None = None
            self._lock = RLock()
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone()
                if existing is None:
                    connection.executescript(_SCHEMA)
                self._validate(connection)
        except OwnerPairingSessionStoreUnavailable:
            raise
        except Exception as error:
            raise OwnerPairingSessionStoreUnavailable() from error

    def _path_ok(self) -> None:
        try:
            path_info = os.lstat(self._path)
            fd = os.open(self._path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                opened = os.fstat(fd)
            finally:
                os.close(fd)
            if (
                not stat.S_ISREG(path_info.st_mode)
                or stat.S_IMODE(path_info.st_mode) != 0o600
                or (path_info.st_dev, path_info.st_ino) != self._identity
                or (opened.st_dev, opened.st_ino) != self._identity
            ):
                raise OwnerPairingSessionStoreUnavailable()
        except OwnerPairingSessionStoreUnavailable:
            raise
        except OSError as error:
            raise OwnerPairingSessionStoreUnavailable() from error

    def _connect(self) -> sqlite3.Connection:
        with _FD_LOCK:
            self._path_ok()
            before = self._fd_snapshot()
            self._before_connect_hook()
            connection = sqlite3.connect(self._path, timeout=30)
            try:
                self._connect_hook()
                self._path_ok()
                after = self._fd_snapshot()
                candidates = [
                    descriptor
                    for descriptor, identity in after.items()
                    if before.get(descriptor) != identity
                    and identity[:2] == self._identity
                    and stat.S_ISREG(identity[2])
                ]
                if len(candidates) != 1:
                    raise OwnerPairingSessionStoreUnavailable()
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
            raise OwnerPairingSessionStoreUnavailable()
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
            raise OwnerPairingSessionStoreUnavailable() from error
        return result

    def _validate_attested_fd(self) -> None:
        if self._attested_fd is None:
            raise OwnerPairingSessionStoreUnavailable()
        try:
            info = os.fstat(self._attested_fd)
        except OSError as error:
            raise OwnerPairingSessionStoreUnavailable() from error
        if (
            (info.st_dev, info.st_ino) != self._identity
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise OwnerPairingSessionStoreUnavailable()

    @staticmethod
    def _validate(connection: sqlite3.Connection) -> None:
        if _catalog(connection) != _EXPECTED_CATALOG:
            raise OwnerPairingSessionStoreUnavailable()

    @staticmethod
    def _binding(pairing: OwnerPairingBootstrap) -> dict[str, object]:
        return {
            "org_id": pairing.org_id,
            "owner_id": pairing.owner_id,
            "agent_id": pairing.agent_id,
            "card_revision": pairing.card_revision,
            "card_digest": pairing.card_digest,
            "device_public_key_digest": pairing.device_public_key_digest,
            "credential_generation": pairing.credential_generation,
            "allowed_origin": pairing.allowed_origin,
        }

    def consume(self, pairing: OwnerPairingBootstrap) -> OwnerPairingSession:
        if type(pairing) is not OwnerPairingBootstrap:
            raise OwnerPairingSessionStoreUnavailable()
        now = self._clock()
        if (
            now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or pairing.expires_at <= now
        ):
            raise OwnerPairingSessionStoreUnavailable()
        binding = self._binding(pairing)
        binding_digest = sha256(_canonical(binding)).hexdigest()
        subject_digest = sha256(
            _canonical([pairing.org_id, pairing.owner_id, pairing.agent_id])
        ).hexdigest()
        pairing_digest = _digest(pairing.central_identity_session.get_secret_value())
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._path_ok()
                    self._validate_attested_fd()
                    self._validate(connection)
                    existing = connection.execute(
                        "SELECT binding_digest,key_id,nonce,ciphertext,revoked "
                        "FROM owner_pairing_sessions WHERE pairing_digest=?",
                        (pairing_digest,),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != binding_digest or existing[4] != 0:
                            raise OwnerPairingSessionStoreUnavailable()
                        envelope = self._decrypt(
                            pairing_digest, existing[1], existing[2], existing[3]
                        )
                        connection.commit()
                        return self._result(envelope, replayed=True)
                    current = connection.execute(
                        "SELECT pairing_digest,credential_generation FROM owner_pairing_sessions "
                        "WHERE subject_digest=? AND revoked=0",
                        (subject_digest,),
                    ).fetchone()
                    if current is not None:
                        if current[1] >= pairing.credential_generation:
                            raise OwnerPairingSessionStoreUnavailable()
                        changed = connection.execute(
                            "UPDATE owner_pairing_sessions SET revoked=1 "
                            "WHERE pairing_digest=? AND revoked=0",
                            (current[0],),
                        ).rowcount
                        if changed != 1:
                            raise OwnerPairingSessionStoreUnavailable()
                    owner_session = secrets.token_urlsafe(32)
                    csrf_token = secrets.token_urlsafe(32)
                    expires_at = min(
                        pairing.expires_at,
                        now + timedelta(seconds=pairing.owner_session_ttl_seconds),
                    )
                    envelope = {
                        **binding,
                        "owner_session": owner_session,
                        "csrf_token": csrf_token,
                        "expires_at": _canonical_utc(expires_at),
                    }
                    key = self._keys.current()
                    nonce = os.urandom(12)
                    aad = _canonical([pairing_digest, key.key_id])
                    ciphertext = AESGCM(key.key).encrypt(
                        nonce, _canonical(envelope), aad
                    )
                    self._fault("before_insert")
                    connection.execute(
                        "INSERT INTO owner_pairing_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            pairing_digest,
                            subject_digest,
                            binding_digest,
                            _digest(owner_session),
                            _digest(csrf_token),
                            pairing.credential_generation,
                            _canonical_utc(expires_at),
                            0,
                            key.key_id,
                            b64encode(nonce).decode(),
                            b64encode(ciphertext).decode(),
                        ),
                    )
                    self._fault("before_commit")
                    self._path_ok()
                    self._validate_attested_fd()
                    self._validate(connection)
                    connection.commit()
                    return self._result(envelope, replayed=False)
            except OwnerPairingSessionStoreUnavailable:
                raise
            except Exception as error:
                raise OwnerPairingSessionStoreUnavailable() from error

    def _decrypt(
        self, pairing_digest: str, key_id: str, nonce: str, ciphertext: str
    ) -> dict[str, object]:
        key = self._keys.current()
        if key.key_id != key_id:
            raise OwnerPairingSessionStoreUnavailable()
        plaintext = AESGCM(key.key).decrypt(
            self._canonical_b64(nonce, minimum=12, maximum=12),
            self._canonical_b64(
                ciphertext, minimum=17, maximum=MAX_SESSION_ENVELOPE_BYTES + 16
            ),
            _canonical([pairing_digest, key_id]),
        )
        if len(plaintext) > MAX_SESSION_ENVELOPE_BYTES:
            raise OwnerPairingSessionStoreUnavailable()
        decoded: object = json.loads(plaintext)
        if type(decoded) is not dict:
            raise OwnerPairingSessionStoreUnavailable()
        value = cast(dict[str, object], decoded)
        if _canonical(value) != plaintext:
            raise OwnerPairingSessionStoreUnavailable()
        return value

    @staticmethod
    def _canonical_b64(value: str, *, minimum: int, maximum: int) -> bytes:
        decoded = b64decode(value, validate=True)
        if (
            not minimum <= len(decoded) <= maximum
            or b64encode(decoded).decode("ascii") != value
        ):
            raise OwnerPairingSessionStoreUnavailable()
        return decoded

    @staticmethod
    def _result(
        envelope: dict[str, object], *, replayed: bool
    ) -> OwnerPairingSession:
        return OwnerPairingSession.model_validate(
            {
                **envelope,
                "owner_session": SecretStr(str(envelope["owner_session"])),
                "csrf_token": SecretStr(str(envelope["csrf_token"])),
                "expires_at": _parse_canonical_utc(envelope["expires_at"]),
                "replayed": replayed,
            }
        )

    def resolve(self, owner_session: str) -> ResolvedOwnerPairing:
        return self._verify(
            owner_session, csrf_token="", origin="", mutation=False
        )

    def verify_mutation(
        self, owner_session: str, csrf_token: str, origin: str
    ) -> ResolvedOwnerPairing:
        return self._verify(
            owner_session, csrf_token=csrf_token, origin=origin, mutation=True
        )

    def _verify(
        self,
        owner_session: str,
        csrf_token: str,
        origin: str,
        *,
        mutation: bool,
    ) -> ResolvedOwnerPairing:
        now = self._clock()
        parsed_origin = urlsplit(origin) if type(origin) is str else None
        if (
            type(owner_session) is not str
            or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", owner_session) is None
            or now.tzinfo is None
            or now.utcoffset() != timedelta(0)
            or (
                mutation
                and (
                    type(csrf_token) is not str
                    or re.fullmatch(r"[A-Za-z0-9_-]{32,128}", csrf_token) is None
                    or parsed_origin is None
                    or parsed_origin.scheme != "https"
                    or not parsed_origin.hostname
                    or parsed_origin.username is not None
                    or parsed_origin.password is not None
                    or parsed_origin.path not in {"", "/"}
                    or bool(parsed_origin.query)
                    or bool(parsed_origin.fragment)
                    or origin.rstrip("/") != origin
                )
            )
        ):
            raise OwnerPairingSessionStoreUnavailable()
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN")
                    self._path_ok()
                    self._validate_attested_fd()
                    self._validate(connection)
                    row = connection.execute(
                        "SELECT pairing_digest,csrf_digest,expires_at,revoked,key_id,nonce,ciphertext "
                        "FROM owner_pairing_sessions WHERE session_digest=?",
                        (_digest(owner_session),),
                    ).fetchone()
                    if (
                        row is None
                        or row[3] != 0
                        or _parse_canonical_utc(row[2]) <= now
                    ):
                        raise OwnerPairingSessionStoreUnavailable()
                    envelope = self._decrypt(row[0], row[4], row[5], row[6])
                    envelope_expiry = _parse_canonical_utc(envelope["expires_at"])
                    if (
                        envelope["expires_at"] != row[2]
                        or envelope_expiry <= now
                        or
                        not hmac.compare_digest(
                            str(envelope["owner_session"]), owner_session
                        )
                        or (
                            mutation
                            and (
                                not hmac.compare_digest(row[1], _digest(csrf_token))
                                or not hmac.compare_digest(
                                    str(envelope["csrf_token"]), csrf_token
                                )
                                or origin != envelope["allowed_origin"]
                            )
                        )
                    ):
                        raise OwnerPairingSessionStoreUnavailable()
                    connection.commit()
                    resolved = {
                        key: value
                        for key, value in envelope.items()
                        if key not in {"owner_session", "csrf_token"}
                    }
                    resolved["expires_at"] = _parse_canonical_utc(
                        envelope["expires_at"]
                    )
                    self._path_ok()
                    self._validate_attested_fd()
                    self._validate(connection)
                    return ResolvedOwnerPairing.model_validate(resolved)
            except OwnerPairingSessionStoreUnavailable:
                raise
            except (InvalidTag, ValueError, KeyError, sqlite3.Error) as error:
                raise OwnerPairingSessionStoreUnavailable() from error

    def revoke(self, owner_session: str) -> None:
        with self._lock:
            try:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    self._path_ok()
                    self._validate_attested_fd()
                    self._validate(connection)
                    changed = connection.execute(
                        "UPDATE owner_pairing_sessions SET revoked=1 "
                        "WHERE session_digest=? AND revoked=0",
                        (_digest(owner_session),),
                    ).rowcount
                    if changed != 1:
                        raise OwnerPairingSessionStoreUnavailable()
                    self._fault("before_commit")
                    self._path_ok()
                    self._validate_attested_fd()
                    self._validate(connection)
                    connection.commit()
            except OwnerPairingSessionStoreUnavailable:
                raise
            except Exception as error:
                raise OwnerPairingSessionStoreUnavailable() from error


__all__ = [
    "OwnerPairingBootstrap",
    "OwnerPairingSession",
    "OwnerPairingSessionStore",
    "OwnerPairingSessionStoreUnavailable",
    "ResolvedOwnerPairing",
]
